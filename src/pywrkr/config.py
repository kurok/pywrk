"""Data structures and scenario loading for pywrkr."""

from __future__ import annotations

import json
import logging
import os
import random
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, TypeVar

from pywrkr.assertions import parse_step_assertions
from pywrkr.templating import (
    EXTRACT_SOURCES,
    ON_EXTRACT_FAILURE_CHOICES,
    ON_TEMPLATE_ERROR_CHOICES,
    Extractor,
    compile_extractor,
    is_valid_var_name,
)

if TYPE_CHECKING:
    from pywrkr.assertions import StepAssertions
    from pywrkr.compare import FailOn
    from pywrkr.feeders import Feeder
    from pywrkr.traffic_profiles import TrafficProfile

logger = logging.getLogger(__name__)

T = TypeVar("T")

# ---------------------------------------------------------------------------
# Default constants
# ---------------------------------------------------------------------------
DEFAULT_CONNECTIONS = 10
DEFAULT_DURATION = 10.0
DEFAULT_THREADS = 4
DEFAULT_TIMEOUT = 30.0
DEFAULT_THINK_TIME_JITTER = 0.5
DEFAULT_MASTER_PORT = 9220

# ---------------------------------------------------------------------------
# Reservoir sampling defaults – controls memory bounds for collected data
# ---------------------------------------------------------------------------
# Maximum number of latency/breakdown samples kept in memory per WorkerStats.
# Reservoir sampling preserves statistical accuracy for percentile estimation
# while bounding memory usage.  100k entries ≈ 800 KB for floats.
DEFAULT_RESERVOIR_SIZE = 100_000

# -- WebSocket defaults ------------------------------------------------------
#: Seconds between sends on one socket. One per second is a realistic
#: client heartbeat and keeps a 500-socket run from saturating a laptop.
DEFAULT_WS_MESSAGE_INTERVAL = 1.0
#: Seconds to wait for the peer's close frame before dropping the transport.
DEFAULT_WS_CLOSE_TIMEOUT = 5.0
#: Frame size ceiling. aiohttp's own default; stated here so it is tunable.
DEFAULT_WS_MAX_MESSAGE_SIZE = 4 * 1024 * 1024
#: Pause before reopening a socket under --ws-reconnect. Without it a target
#: that refuses the upgrade turns the run into a reconnect spin loop.
DEFAULT_WS_RECONNECT_DELAY = 1.0

# Maximum number of unique error type keys tracked per WorkerStats.
# Once the cap is reached, new error strings are folded into a catch-all key.
DEFAULT_MAX_ERROR_TYPES = 1_000
DEFAULT_AUTOFIND_MAX_ERROR_RATE = 1.0
DEFAULT_AUTOFIND_MAX_P95 = 5.0
DEFAULT_AUTOFIND_STEP_DURATION = 30.0
DEFAULT_AUTOFIND_START_USERS = 10
DEFAULT_AUTOFIND_MAX_USERS = 10000
DEFAULT_AUTOFIND_STEP_MULTIPLIER = 2.0

#: Accepted values for the scenario-level ``session`` option. ``persistent``
#: carries a VU's cookies across iterations; ``fresh_per_iteration`` empties the
#: jar at the top of each iteration so every pass looks like a new visitor.
SESSION_CHOICES = ("persistent", "fresh_per_iteration")


@dataclass
class SSLConfig:
    """SSL/TLS configuration for HTTP connections."""

    verify: bool = False  # Whether to verify SSL certificates
    ca_bundle: str | None = None  # Path to CA bundle file

    @classmethod
    def from_env(cls) -> "SSLConfig":
        """Create SSLConfig from environment variables.

        Environment variables:
            PYWRKR_SSL_VERIFY: Set to '1', 'true', or 'yes' to enable SSL
                verification.  Any other non-empty value triggers a warning.
            PYWRKR_CA_BUNDLE: Path to a custom CA bundle file.
        """
        _TRUTHY = {"1", "true", "yes"}
        _FALSY = {"0", "false", "no", ""}

        verify_env = os.environ.get("PYWRKR_SSL_VERIFY", "").lower()
        verify = verify_env in _TRUTHY
        if verify_env and verify_env not in _TRUTHY and verify_env not in _FALSY:
            logger.warning(
                "Unrecognised PYWRKR_SSL_VERIFY value %r — treating as disabled. "
                "Use one of: 1, true, yes, 0, false, no.",
                verify_env,
            )

        ca_bundle = os.environ.get("PYWRKR_CA_BUNDLE") or None
        if ca_bundle and not os.path.isfile(ca_bundle):
            logger.warning("PYWRKR_CA_BUNDLE path does not exist: %s", ca_bundle)

        config = cls(verify=verify, ca_bundle=ca_bundle)
        logger.debug("SSL config from environment: verify=%s, ca_bundle=%s", verify, ca_bundle)
        return config


@dataclass
class RequestResult:
    """Result of a single HTTP request."""

    status: int
    latency: float  # seconds
    bytes_read: int
    error: str | None = None


#: Every phase a backend may report. Kept here rather than in backends.py so
#: config has no import dependency on it.
LATENCY_PHASES = ("dns", "connect", "tls", "ttfb", "transfer", "total")


@dataclass
class LatencyBreakdown:
    """Per-request latency breakdown into phases."""

    dns: float = 0.0  # DNS lookup time (seconds)
    connect: float = 0.0  # TCP connect time (seconds)
    tls: float = 0.0  # TLS handshake time (seconds)
    ttfb: float = 0.0  # Time to first byte (seconds)
    transfer: float = 0.0  # Response body transfer time (seconds)
    is_reused: bool = False  # True if the connection was reused (DNS/connect/TLS will be 0)
    # Which phases this sample actually measured. A backend without connection
    # hooks (httpx) reports a shorter list, so the aggregator can omit those
    # phases instead of averaging in zeros that never happened.
    available: tuple[str, ...] = LATENCY_PHASES


class ReservoirSampler(list):
    """Fixed-capacity list that uses reservoir sampling to maintain a
    statistically representative sample.

    Behaves like a regular ``list`` (supports iteration, indexing, ``len``,
    ``sorted()``, etc.) so existing code that reads ``stats.latencies`` or
    ``stats.breakdowns`` works unchanged.

    When the number of items added exceeds *capacity*, new items randomly
    replace existing ones with decreasing probability, preserving a uniform
    sample of all items seen so far (Algorithm R – Vitter 1985).

    Attributes:
        capacity: Maximum number of items retained.
        total_seen: Total number of items offered via ``append``.
    """

    __slots__ = ("capacity", "total_seen")

    def __init__(self, capacity: int = DEFAULT_RESERVOIR_SIZE, iterable=()):
        super().__init__()
        self.capacity = capacity
        self.total_seen = 0
        for item in iterable:
            self.append(item)

    # -- core mutation via append (the hot path) ----------------------------

    def append(self, item):
        self.total_seen += 1
        if len(self) < self.capacity:
            super().append(item)
        else:
            j = random.randint(0, self.total_seen - 1)
            if j < self.capacity:
                self[j] = item

    def extend(self, iterable):
        for item in iterable:
            self.append(item)

    # -- helpers for merge / serialization ----------------------------------

    @classmethod
    def from_list(
        cls, items: list, capacity: int = DEFAULT_RESERVOIR_SIZE, total_seen: int | None = None
    ) -> "ReservoirSampler":
        """Reconstruct a sampler from an already-sampled list.

        Used during deserialization and merge operations.  If *total_seen*
        is ``None`` it defaults to ``len(items)`` (i.e. no sampling occurred).
        """
        sampler = cls.__new__(cls)
        list.__init__(sampler, items[:capacity])
        sampler.capacity = capacity
        sampler.total_seen = total_seen if total_seen is not None else len(items)
        return sampler

    def __eq__(self, other):
        if isinstance(other, ReservoirSampler):
            return (
                list.__eq__(self, other)
                and self.capacity == other.capacity
                and self.total_seen == other.total_seen
            )
        return list.__eq__(self, other)

    __hash__ = None  # type: ignore[assignment]

    def __repr__(self):
        return (
            f"ReservoirSampler(capacity={self.capacity}, "
            f"total_seen={self.total_seen}, len={len(self)})"
        )


def normalize_timeline(timeline: list) -> list:
    """Rebase a worker's rps_timeline onto a common ``[0, duration)`` axis.

    Workers record timeline entries as ``(time.monotonic(), count)``. The
    monotonic clock has a per-process, host-relative origin, so timestamps from
    different workers are not directly comparable; bucketing them against another
    clock collapses or scatters the merged timeline. Subtracting each worker's
    own earliest timestamp turns its entries into seconds since that worker's
    benchmark start, so all workers can be bucketed on a shared axis.
    """
    if not timeline:
        return []
    origin = min(ts for ts, _ in timeline)
    return [(ts - origin, count) for ts, count in timeline]


def merge_reservoirs(samplers: list, capacity: int) -> "ReservoirSampler":
    """Merge already-sampled reservoirs proportionally to each source's volume.

    Naively concatenating per-worker samples (the old ``extend`` approach)
    discards ``total_seen`` and over-represents workers whose reservoir filled
    up: each retained item from a full reservoir stands in for many original
    observations, so a low-volume worker's samples drown out a high-volume
    worker's once both are at capacity. This combines the per-source samples
    with each source weighted by its true ``total_seen`` and sets the merged
    ``total_seen`` to the sum of inputs, so merged percentiles reflect the true
    combined distribution and downstream throughput math stays correct.
    """
    total_seen = sum(getattr(s, "total_seen", len(s)) for s in samplers)
    if total_seen <= 0:
        return ReservoirSampler.from_list([], capacity=capacity, total_seen=0)

    pool: list = []
    for s in samplers:
        if not s:
            continue
        seen = getattr(s, "total_seen", len(s))
        # Allocate output slots proportionally to this source's true weight.
        k = round(capacity * seen / total_seen)
        if k <= 0:
            continue
        items = list(s)
        if k >= len(items):
            pool.extend(items)
        else:
            pool.extend(random.sample(items, k))

    # Rounding can push the combined allocation a few slots past capacity;
    # subsample uniformly rather than letting ``from_list`` truncate to the
    # first ``capacity`` items (which would bias the sample toward whichever
    # workers were merged first and could drop a later worker entirely).
    if len(pool) > capacity:
        pool = random.sample(pool, capacity)

    return ReservoirSampler.from_list(pool, capacity=capacity, total_seen=total_seen)


_MAX_STEP_NAMES = 500


def merge_stats(all_stats: "list[WorkerStats]") -> "WorkerStats":
    """Merge a list of WorkerStats into one aggregated WorkerStats.

    Step latencies are capped at _MAX_STEP_NAMES unique keys to prevent
    unbounded memory growth from dynamic step names.
    """
    merged = WorkerStats()
    lat_capacity = merged.latencies.capacity
    bd_capacity = merged.breakdowns.capacity
    for ws in all_stats:
        merged.total_requests += ws.total_requests
        merged.total_bytes += ws.total_bytes
        merged.errors += ws.errors
        merged.content_length_errors += ws.content_length_errors
        merged.extract_failures += ws.extract_failures
        merged.template_errors += ws.template_errors
        merged.rps_timeline.extend(normalize_timeline(ws.rps_timeline))
        for k, v in ws.error_types.items():
            merged.error_types[k] += v
        for k, v in ws.status_codes.items():
            merged.status_codes[k] += v
        for k, v in ws.http_versions.items():
            merged.http_versions[k] += v
        for k, v in ws.step_latencies.items():
            if k not in merged.step_latencies:
                if len(merged.step_latencies) >= _MAX_STEP_NAMES:
                    k = "[other steps]"
                else:
                    merged.step_latencies[k] = []
            merged.step_latencies[k].extend(v)
        for k, count in ws.step_errors.items():
            if k not in merged.step_errors and len(merged.step_errors) >= _MAX_STEP_NAMES:
                k = "[other steps]"
            merged.step_errors[k] += count
    merged.latencies = merge_reservoirs([ws.latencies for ws in all_stats], lat_capacity)
    merged.breakdowns = merge_reservoirs([ws.breakdowns for ws in all_stats], bd_capacity)
    ws_parts = [s.ws for s in all_stats if s.ws is not None]
    if ws_parts:
        merged.ws = merge_ws_stats(ws_parts)
    return merged


class CappedErrorDict(defaultdict):
    """A ``defaultdict(int)`` that stops accepting new keys after a limit.

    Once *max_keys* distinct keys exist, any new key is silently redirected
    to a catch-all ``"[other errors]"`` bucket so memory stays bounded.

    Usage::

        d = CappedErrorDict(max_keys=1000)
        d[some_error_string] += 1   # works normally until limit hit
    """

    _OVERFLOW_KEY = "[other errors]"

    def __init__(self, max_keys: int = DEFAULT_MAX_ERROR_TYPES):
        super().__init__(int)
        self.max_keys = max_keys

    def __getitem__(self, key):
        # If the key already exists, return it directly (fast path).
        if key in self:
            return super().__getitem__(key)
        # Key is new — check capacity.
        if len(self) >= self.max_keys and key != self._OVERFLOW_KEY:
            # Redirect reads of unknown keys to the overflow bucket.
            return super().__getitem__(self._OVERFLOW_KEY) if self._OVERFLOW_KEY in self else 0
        # Under capacity — create a new entry via defaultdict machinery.
        return super().__getitem__(key)

    def __setitem__(self, key, value):
        # If the key already exists, update it.
        if key in self:
            super().__setitem__(key, value)
            return
        # Key is new — check capacity.
        if len(self) >= self.max_keys and key != self._OVERFLOW_KEY:
            # Redirect writes to the overflow bucket.
            super().__setitem__(self._OVERFLOW_KEY, value)
        else:
            super().__setitem__(key, value)


class ActiveUsers:
    """Thread-safe (single-threaded asyncio) counter for active virtual users."""

    __slots__ = ("count",)

    def __init__(self) -> None:
        self.count: int = 0


class RequestCounter:
    """Shared mutable counter for request-count mode."""

    __slots__ = ("remaining",)

    def __init__(self, total: int) -> None:
        self.remaining: int = total


@dataclass
class WsStats:
    """WebSocket-specific counters, carried alongside :class:`WorkerStats`.

    Handshake and round-trip latencies are kept apart even though one of them
    also lands in ``WorkerStats.latencies``: a run that connects quickly and
    replies slowly, and one that does the reverse, must not look the same.
    """

    connections_opened: int = 0
    connections_failed: int = 0
    connections_dropped: int = 0
    reconnects: int = 0
    peak_concurrent: int = 0
    messages_sent: int = 0
    messages_received: int = 0
    bytes_sent: int = 0
    bytes_received: int = 0
    reply_timeouts: int = 0
    unexpected_replies: int = 0
    close_frames_sent: int = 0
    #: Close frames the peer never answered. A server that does not read its
    #: sockets shows up here rather than as a silent zero.
    close_unacked: int = 0
    close_codes: dict[str, int] = field(default_factory=dict)
    handshake_latencies: ReservoirSampler = field(
        default_factory=lambda: ReservoirSampler(DEFAULT_RESERVOIR_SIZE)
    )
    rtt_latencies: ReservoirSampler = field(
        default_factory=lambda: ReservoirSampler(DEFAULT_RESERVOIR_SIZE)
    )
    #: "rtt" or "handshake" -- which of the two ``WorkerStats.latencies`` holds.
    latency_metric: str = "handshake"
    #: "messages" or "connections" -- what ``total_requests`` counts.
    primary_metric: str = "connections"

    def record_close(self, code: "int | None") -> None:
        """Record one close code, ignoring a socket that reported none.

        Exactly one of the two coroutines that can see a peer's CLOSE frame
        records it -- whichever reads it first -- so a code of None means the
        other one already did, not that the close was anonymous. Sockets whose
        close went unanswered are counted by ``close_unacked`` instead.
        """
        if code is None:
            return
        key = str(code)
        self.close_codes[key] = self.close_codes.get(key, 0) + 1


def merge_ws_stats(all_stats: "list[WsStats]") -> "WsStats":
    """Merge per-socket WebSocket counters into one."""
    merged = WsStats()
    if not all_stats:
        return merged
    for s in all_stats:
        merged.connections_opened += s.connections_opened
        merged.connections_failed += s.connections_failed
        merged.connections_dropped += s.connections_dropped
        merged.reconnects += s.reconnects
        merged.messages_sent += s.messages_sent
        merged.messages_received += s.messages_received
        merged.bytes_sent += s.bytes_sent
        merged.bytes_received += s.bytes_received
        merged.reply_timeouts += s.reply_timeouts
        merged.unexpected_replies += s.unexpected_replies
        merged.close_frames_sent += s.close_frames_sent
        merged.close_unacked += s.close_unacked
        for code, count in s.close_codes.items():
            merged.close_codes[code] = merged.close_codes.get(code, 0) + count
    # Peak concurrency is a maximum, not a sum: every socket sees the same
    # shared counter, so adding them would multiply the peak by the fleet size.
    merged.peak_concurrent = max(s.peak_concurrent for s in all_stats)
    merged.handshake_latencies = _merge_ws_reservoir([s.handshake_latencies for s in all_stats])
    merged.rtt_latencies = _merge_ws_reservoir([s.rtt_latencies for s in all_stats])
    merged.latency_metric = all_stats[0].latency_metric
    merged.primary_metric = all_stats[0].primary_metric
    return merged


def _merge_ws_reservoir(samplers: list) -> "ReservoirSampler":
    return merge_reservoirs(samplers, DEFAULT_RESERVOIR_SIZE)


@dataclass
class WorkerStats:
    """Aggregated statistics collected by a single worker."""

    total_requests: int = 0
    total_bytes: int = 0
    errors: int = 0
    error_types: CappedErrorDict = field(
        default_factory=lambda: CappedErrorDict(DEFAULT_MAX_ERROR_TYPES)
    )
    status_codes: dict[int, int] = field(default_factory=lambda: defaultdict(int))
    # Negotiated protocol per response ("1.1", "2", ...). With --http2 against a
    # server that only offers h1, this is what makes the fallback visible rather
    # than silently counted as HTTP/2.
    http_versions: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    latencies: ReservoirSampler = field(
        default_factory=lambda: ReservoirSampler(DEFAULT_RESERVOIR_SIZE)
    )
    rps_timeline: list[tuple[float, int]] = field(default_factory=list)
    content_length_errors: int = 0
    # Scenario correlation counters: extraction rules that produced no value,
    # and ${var} placeholders that referenced an unbound variable.
    extract_failures: int = 0
    template_errors: int = 0
    step_latencies: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    # Errors attributed to a named scenario step, so the per-step table can show
    # which step is failing rather than only which is slow.
    step_errors: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    breakdowns: ReservoirSampler = field(
        default_factory=lambda: ReservoirSampler(DEFAULT_RESERVOIR_SIZE)
    )
    # Latencies since the last streaming-export tick. None (the default) means
    # nobody is streaming, and the hot path skips the append entirely.
    window_latencies: "list[float] | None" = None
    # WebSocket counters, present only on a ws:// run. Kept as a sidecar rather
    # than folded into the fields above so the HTTP hot path pays nothing and
    # every downstream consumer (JSON, HTML, compare, exporters) reaches WS
    # metrics through the same WorkerStats it already handles.
    ws: "WsStats | None" = None


@dataclass
class BenchmarkConfig:
    """Full configuration for a benchmark run."""

    url: str
    connections: int = DEFAULT_CONNECTIONS
    duration: float | None = DEFAULT_DURATION
    num_requests: int | None = None  # ab-style -n mode
    threads: int = DEFAULT_THREADS
    method: str = "GET"
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes | None = None
    timeout_sec: float = DEFAULT_TIMEOUT
    keepalive: bool = True
    basic_auth: str | None = None  # "user:pass"
    cookies: list[str] = field(default_factory=list)  # ["name=value", ...]
    # Honor Set-Cookie per virtual user (jar per VU). False installs a
    # DummyCookieJar so only the static `cookies` header is ever sent.
    session_cookies: bool = True
    # Use the HTTP/2-capable backend (optional `pywrkr[http2]` extra).
    http2: bool = False
    verify_content_length: bool = False
    verbosity: int = 0
    csv_output: str | None = None  # file path for CSV percentile output
    html_output: bool = False
    json_output: str | None = None  # file path for JSON output
    # User simulation mode
    users: int | None = None  # number of virtual users
    ramp_up: float = 0.0  # seconds to ramp up all users
    think_time: float = 0.0  # mean think time between requests per user (seconds)
    # jitter factor (0-1): actual = think * uniform(1-jitter, 1+jitter)
    think_time_jitter: float = DEFAULT_THINK_TIME_JITTER
    random_param: bool = False  # append random _cb=<uuid> query param per request (cache-buster)
    live_dashboard: bool = False  # show live TUI dashboard (requires rich)
    # Rate limiting mode
    rate: float | None = None  # target requests per second (None = unlimited)
    rate_ramp: float | None = (
        None  # ramp rate target: linearly increase from rate to rate_ramp over duration
    )
    # Traffic profile (advanced traffic shaping)
    traffic_profile: TrafficProfile | None = None
    # Scenario mode
    scenario: "Scenario | None" = None
    # Latency breakdown mode
    latency_breakdown: bool = False
    # Gatling-style HTML report
    html_report: str | None = None  # file path for interactive HTML report
    # Autofind mode: suppress output when used as a sub-step
    _quiet: bool = False
    ssl_config: SSLConfig = field(default_factory=SSLConfig)
    # Observability export
    tags: dict[str, str] = field(default_factory=dict)
    otel_endpoint: str | None = None
    prom_remote_write: str | None = None
    # Seconds between streaming metric exports; None exports only at the end.
    export_interval: float | None = None
    # SLO thresholds
    thresholds: "list[Threshold]" = field(default_factory=list)
    # Baseline regression gate: compare this run against previous results and
    # fail on the given deltas.
    baseline: str | None = None  # path or glob to baseline --json file(s)
    save_baseline: str | None = None  # write this run's results here
    fail_on: "list[FailOn]" = field(default_factory=list)
    strict_config: bool = False  # treat a config mismatch as an error, not a warning
    compare_format: str = "table"
    # WebSocket mode settings; None for an ordinary HTTP run.
    websocket: "WebSocketConfig | None" = None
    # Read the response body. False releases the connection instead, which is
    # opt-in because it changes what is measured -- see needs_body() and the
    # README. A step that inspects the body reads it regardless.
    read_body: bool = True


@dataclass
class WebSocketConfig:
    """Everything that only applies to a ``ws://``/``wss://`` run."""

    #: Payloads to send, cycled in order. Empty means "connect and listen",
    #: which is the shape of a fan-out or connection-capacity test.
    messages: list[str] = field(default_factory=list)
    #: Seconds between sends on one socket. 0 sends as fast as the socket takes.
    message_interval: float = DEFAULT_WS_MESSAGE_INTERVAL
    #: Wait for a reply to each message and measure the round trip. Without
    #: this the latency metric is the handshake, not the message.
    expect_reply: bool = False
    reply_timeout: float = DEFAULT_TIMEOUT
    subprotocols: list[str] = field(default_factory=list)
    #: aiohttp heartbeat: seconds between client pings. None disables them.
    ping_interval: float | None = None
    max_message_size: int = DEFAULT_WS_MAX_MESSAGE_SIZE
    close_timeout: float = DEFAULT_WS_CLOSE_TIMEOUT
    #: Reopen a socket the server closed, instead of leaving the slot empty.
    reconnect: bool = False
    reconnect_delay: float = DEFAULT_WS_RECONNECT_DELAY


@dataclass
class Threshold:
    """An SLO threshold expression (e.g. 'p95 < 300ms')."""

    metric: str  # e.g. "p95"
    operator: str  # e.g. "<"
    value: float  # in seconds for latency, percent for error_rate, raw for rps
    raw_expr: str  # original string for display
    #: Scenario step this applies to, from ``step:<name> <metric> ...``. None is
    #: the aggregate across every request, which for a scenario is a blend of
    #: all its steps.
    step: "str | None" = None


@dataclass
class AutofindConfig:
    """Configuration for auto-ramping / step load mode."""

    url: str
    max_error_rate: float = DEFAULT_AUTOFIND_MAX_ERROR_RATE  # percent
    max_p95: float = DEFAULT_AUTOFIND_MAX_P95  # seconds
    step_duration: float = DEFAULT_AUTOFIND_STEP_DURATION
    start_users: int = DEFAULT_AUTOFIND_START_USERS
    max_users: int = DEFAULT_AUTOFIND_MAX_USERS
    step_multiplier: float = DEFAULT_AUTOFIND_STEP_MULTIPLIER
    think_time: float = 1.0
    think_time_jitter: float = DEFAULT_THINK_TIME_JITTER
    random_param: bool = False
    timeout_sec: float = DEFAULT_TIMEOUT
    keepalive: bool = True
    ssl_config: SSLConfig = field(default_factory=SSLConfig)
    json_output: str | None = None
    # Observability settings, carried through to each step's benchmark config.
    # Without these an autofind session exported nothing at all, which is the
    # run you most want to watch live.
    tags: dict[str, str] = field(default_factory=dict)
    otel_endpoint: str | None = None
    prom_remote_write: str | None = None
    export_interval: float | None = None


@dataclass
class StepResult:
    """Result of a single autofind step."""

    users: int
    rps: float
    p50: float
    p95: float
    p99: float
    error_rate: float
    total_requests: int
    total_errors: int
    passed: bool
    #: Whether this step produced any latency samples. False means its p50/p95/
    #: p99 are placeholders, not measurements, so the step cannot be judged
    #: sustainable however small those numbers look.
    measured: bool = True
    latency_samples: int = 0


@dataclass
class ScenarioStep:
    """A single step in a scripted scenario."""

    path: str
    method: str = "GET"
    body: str | dict | list | None = None
    headers: dict[str, str] = field(default_factory=dict)
    assert_status: int | None = None
    assert_body_contains: str | None = None
    think_time: float | None = None  # per-step override
    name: str | None = None
    # Correlation: variable name -> compiled extraction rule, applied to this
    # step's response so later steps can reference it as ${name}.
    extract: dict[str, Extractor] = field(default_factory=dict)
    # Every assertion on this step, compiled. Built from the two legacy fields
    # above when not supplied, so a hand-constructed
    # ``ScenarioStep(path=..., assert_status=200)`` still asserts.
    assertions: "StepAssertions" = None  # type: ignore[assignment]
    # -- WebSocket step (``ws:`` in the scenario file) --------------------
    # A ws:// or wss:// URL turns this into a WebSocket step: open a socket,
    # optionally send `send`, optionally wait for a message matching
    # `expect_message_contains`, and hold it open for `hold` seconds. `path`
    # carries the same value so every existing consumer (naming, per-step
    # stats, template validation) keeps working unchanged.
    ws: str | None = None
    send: str | None = None
    expect_message_contains: str | None = None
    hold: float = 0.0

    @property
    def is_websocket(self) -> bool:
        return self.ws is not None

    def __post_init__(self) -> None:
        if self.assertions is None:
            from pywrkr.assertions import StepAssertions

            self.assertions = StepAssertions(
                status=self.assert_status, body_contains=self.assert_body_contains
            )


@dataclass
class Scenario:
    """A scripted multi-step scenario."""

    name: str = "Unnamed Scenario"
    base_url: str | None = None  # optional base URL for scenario steps
    think_time: float = 0.0
    steps: list[ScenarioStep] = field(default_factory=list)
    # What to do when an extract rule yields nothing: "abort_iteration" (default)
    # or "continue".
    on_extract_failure: str = ON_EXTRACT_FAILURE_CHOICES[0]
    # What to do when a ${var} references an unbound variable:
    # "abort_iteration" (default) or "keep_literal".
    on_template_error: str = ON_TEMPLATE_ERROR_CHOICES[0]
    # Cookie lifetime within a VU: "persistent" (default) keeps the jar across
    # iterations, "fresh_per_iteration" empties it so each iteration looks like a
    # brand-new visitor.
    session: str = SESSION_CHOICES[0]
    # Named data sets feeding ${name.field}; each user draws one row per set per
    # iteration.
    data: "dict[str, Feeder]" = field(default_factory=dict)


def parse_extract_spec(raw: object, where: str) -> dict[str, Extractor]:
    """Validate and compile a raw ``extract`` mapping from a scenario file.

    Compiling here means a bad regex or an unsupported JSONPath is reported as
    a scenario-file error before the benchmark starts, instead of failing once
    per request mid-run.

    Args:
        raw: The value of the step's ``extract`` key (``None`` if absent).
        where: Human-readable location prefix for error messages, e.g. ``"Step 3"``.

    Raises:
        ValueError: The mapping, a variable name, or an expression is invalid.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"{where} 'extract' must be an object, got {type(raw).__name__}")

    extractors: dict[str, Extractor] = {}
    for var_name, spec in raw.items():
        if not is_valid_var_name(var_name):
            raise ValueError(
                f"{where} 'extract' variable name {var_name!r} is not a valid ${{name}} "
                f"identifier (letters, digits and underscore; must not start with a digit)"
            )
        if not isinstance(spec, dict):
            raise ValueError(
                f"{where} extract {var_name!r} must be an object like "
                f'{{"json": "$.token"}}, got {type(spec).__name__}'
            )
        unknown = [k for k in spec if k not in EXTRACT_SOURCES]
        if unknown:
            raise ValueError(
                f"{where} extract {var_name!r} has unknown key(s) {', '.join(map(repr, unknown))}; "
                f"expected one of {', '.join(EXTRACT_SOURCES)}"
            )
        sources = [k for k in EXTRACT_SOURCES if k in spec]
        if len(sources) != 1:
            raise ValueError(
                f"{where} extract {var_name!r} must specify exactly one of "
                f"{', '.join(EXTRACT_SOURCES)}, got {len(sources)}"
            )
        try:
            extractors[var_name] = compile_extractor(var_name, sources[0], spec[sources[0]])
        except ValueError as exc:
            raise ValueError(f"{where} extract {var_name!r}: {exc}") from None
    return extractors


def _parse_choice(data: dict, key: str, choices: tuple[str, ...]) -> str:
    """Read an enum-like scenario option, defaulting to ``choices[0]``."""
    value = data.get(key, choices[0])
    if value not in choices:
        raise ValueError(f"Scenario {key!r} must be one of {', '.join(choices)}, got {value!r}")
    return value


def parse_data_spec(raw: object, base_dir: str) -> "dict[str, Feeder]":
    """Load the scenario's ``data`` block into feeders.

    Each entry names a data set and gives its ``file`` plus an optional
    ``strategy``. Relative paths resolve against the scenario file's own
    directory, so a scenario and its CSV can be moved together.

    Raises:
        ValueError: The block, an entry, or a data file is malformed. Files are
            read here so a missing or broken one is a startup error.
    """
    from pywrkr.feeders import FEEDER_STRATEGIES, load_feeder

    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"Scenario 'data' must be an object, got {type(raw).__name__}")

    feeders: dict[str, Feeder] = {}
    for name, spec in raw.items():
        if not isinstance(spec, dict):
            raise ValueError(
                f"Scenario data {name!r} must be an object like "
                f'{{"file": "users.csv", "strategy": "unique"}}, got {type(spec).__name__}'
            )
        unknown = [k for k in spec if k not in ("file", "strategy")]
        if unknown:
            raise ValueError(
                f"Scenario data {name!r} has unknown key(s) {', '.join(map(repr, unknown))}; "
                f"expected 'file' and optional 'strategy'"
            )
        file_path = spec.get("file")
        if not isinstance(file_path, str) or not file_path.strip():
            raise ValueError(f"Scenario data {name!r} needs a 'file' path")
        strategy = spec.get("strategy", FEEDER_STRATEGIES[0])
        if not isinstance(strategy, str):
            raise ValueError(
                f"Scenario data {name!r} 'strategy' must be a string, got {type(strategy).__name__}"
            )
        if not os.path.isabs(file_path):
            file_path = os.path.join(base_dir, file_path)
        feeders[name] = load_feeder(name, file_path, strategy)
    return feeders


def _validate_step_templates(
    steps: "list[ScenarioStep]",
    feeders: "dict[str, Feeder]",
    strict_datasets: bool = True,
) -> None:
    """Check every placeholder in the scenario before the run starts.

    Function calls and ``${dataset.field}`` references can be verified without
    sending a request, so a typo becomes a startup error naming the step instead
    of a template failure repeated once per iteration. Plain ``${var}``
    references are left alone — those are bound by ``extract`` at runtime.

    *strict_datasets* is False while the scenario file is loading, because
    ``--data`` may still supply a set the file does not declare; the full check
    runs again from :func:`validate_scenario_templates` once the CLI has merged.

    Raises:
        ValueError: A placeholder names an unknown function, a field the data
            file does not have, or (when *strict_datasets*) an undeclared data
            set.
    """
    from pywrkr.templating import iter_placeholders, validate_function_call

    def check(text: str, where: str) -> None:
        for match in iter_placeholders(text):
            func = match.group("func")
            if func is not None:
                try:
                    validate_function_call(func, match.group("args"))
                except ValueError as exc:
                    raise ValueError(f"{where}: {exc}") from None
                continue
            dataset = match.group("dataset")
            if dataset is None:
                continue
            field_name = match.group("field")
            feeder = feeders.get(dataset)
            if feeder is None:
                if not strict_datasets:
                    continue
                declared = ", ".join(sorted(feeders)) or "none"
                raise ValueError(
                    f"{where}: ${{{dataset}.{field_name}}} references data set {dataset!r}, "
                    f"which is not declared by the scenario's 'data' block or --data "
                    f"(declared: {declared})"
                )
            if field_name not in feeder.fields:
                raise ValueError(
                    f"{where}: data set {dataset!r} has no field {field_name!r} "
                    f"(available: {', '.join(feeder.fields)})"
                )

    def walk(value: object, where: str) -> None:
        if isinstance(value, str):
            check(value, where)
        elif isinstance(value, dict):
            for key, item in value.items():
                if isinstance(key, str):
                    check(key, where)
                walk(item, where)
        elif isinstance(value, list):
            for item in value:
                walk(item, where)

    for i, step in enumerate(steps):
        where = f"Step {i} ({step.name or step.path})"
        check(step.path, where)
        for header_name, header_value in step.headers.items():
            check(header_name, where)
            check(header_value, where)
        walk(step.body, where)


def validate_scenario_templates(scenario: "Scenario") -> None:
    """Re-check a scenario's placeholders once its data sets are final.

    Call after merging ``--data`` into a loaded scenario: only then is it known
    whether a ``${dataset.field}`` reference is genuinely undeclared.

    Raises:
        ValueError: A placeholder names an unknown function, an undeclared data
            set, or a field its data file does not have.
    """
    _validate_step_templates(scenario.steps, scenario.data, strict_datasets=True)


def _normalize_ws_step(step_data: dict, index: int) -> dict:
    """Validate a ``ws:`` step and give it a ``path`` so the rest of the
    pipeline (naming, per-step stats, ``${var}`` validation) is unchanged.

    HTTP-only keys are rejected rather than ignored: a ``method: POST`` on a
    WebSocket step that quietly did nothing would be worse than an error.
    """
    ws_url = step_data["ws"]
    if not isinstance(ws_url, str):
        raise ValueError(f"Step {index} 'ws' must be a string, got {type(ws_url).__name__}")
    if "path" in step_data and step_data["path"] != ws_url:
        raise ValueError(f"Step {index} cannot set both 'ws' and a different 'path'")

    for key in ("method", "body", "assert_status", "assert_body_contains"):
        if key in step_data:
            raise ValueError(
                f"Step {index} sets '{key}', which describes an HTTP request and "
                "does not apply to a 'ws' step"
            )

    send = step_data.get("send")
    if send is not None and not isinstance(send, str):
        raise ValueError(f"Step {index} 'send' must be a string, got {type(send).__name__}")

    expect = step_data.get("expect_message_contains")
    if expect is not None and not isinstance(expect, str):
        raise ValueError(
            f"Step {index} 'expect_message_contains' must be a string, got {type(expect).__name__}"
        )
    if expect is not None and send is None:
        raise ValueError(
            f"Step {index} sets 'expect_message_contains' without 'send'; there is nothing "
            "to expect a reply to. Use 'hold' to listen to server-pushed messages instead"
        )

    hold = step_data.get("hold")
    if hold is not None and isinstance(hold, str):
        from pywrkr.assertions import parse_duration

        try:
            hold = parse_duration(hold, f"Step {index} 'hold'")
        except ValueError as e:
            raise ValueError(str(e)) from None
    if hold is not None and (isinstance(hold, bool) or not isinstance(hold, (int, float))):
        raise ValueError(f"Step {index} 'hold' must be a duration, got {type(hold).__name__}")
    if hold is not None and hold < 0:
        raise ValueError(f"Step {index} 'hold' must be >= 0")

    normalized = dict(step_data)
    normalized["path"] = ws_url
    normalized["hold"] = hold
    return normalized


def load_scenario(path: str) -> Scenario:
    """Load a scenario from a JSON or YAML file."""
    logger.debug("Loading scenario from %s", path)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Scenario file not found: {path}")

    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    ext = os.path.splitext(path)[1].lower()
    if ext in (".yaml", ".yml"):
        try:
            import yaml
        except ImportError:
            raise ImportError(
                "pyyaml is required for YAML scenario files. Install with: pip install pyyaml"
            ) from None
        data = yaml.safe_load(content)
    elif ext == ".json":
        data = json.loads(content)
    else:
        # Try JSON first, then YAML
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            try:
                import yaml

                data = yaml.safe_load(content)
            except ImportError:
                raise ValueError(
                    f"Could not parse scenario file: {path}. "
                    f"Not valid JSON, and pyyaml is not installed for YAML parsing."
                ) from None

    if not isinstance(data, dict):
        raise ValueError(
            f"Scenario file must contain a JSON/YAML object, got {type(data).__name__}"
        )

    if "steps" not in data or not isinstance(data["steps"], list):
        raise ValueError("Scenario file must contain a 'steps' list")

    if len(data["steps"]) == 0:
        raise ValueError("Scenario file must contain at least one step")

    steps = []
    for i, step_data in enumerate(data["steps"]):
        if not isinstance(step_data, dict):
            raise ValueError(f"Step {i} must be a dict, got {type(step_data).__name__}")
        if "path" not in step_data and "ws" not in step_data:
            raise ValueError(f"Step {i} must have a 'path' or 'ws' field")
        if "ws" in step_data:
            step_data = _normalize_ws_step(step_data, i)

        # Validate value types before constructing the step so configuration
        # errors are reported clearly at load time instead of crashing deep in
        # aiohttp at request time.
        # Use a distinct name so this does not shadow the `path` function
        # parameter (the scenario file path), which is reused in the trailing
        # log message after the loop.
        step_path = step_data["path"]
        if not isinstance(step_path, str):
            raise ValueError(f"Step {i} 'path' must be a string, got {type(step_path).__name__}")

        if "method" in step_data and not isinstance(step_data["method"], str):
            raise ValueError(
                f"Step {i} 'method' must be a string, got {type(step_data['method']).__name__}"
            )

        if "headers" in step_data and not isinstance(step_data["headers"], dict):
            raise ValueError(
                f"Step {i} 'headers' must be an object, got {type(step_data['headers']).__name__}"
            )

        body = step_data.get("body")
        # bool is a subclass of int; reject it explicitly along with int/float.
        if body is not None and not isinstance(body, (str, dict, list)):
            raise ValueError(
                f"Step {i} 'body' must be a string, object, array, or null, "
                f"got {type(body).__name__}"
            )

        think_time = step_data.get("think_time")
        if think_time is not None and (
            isinstance(think_time, bool) or not isinstance(think_time, (int, float))
        ):
            raise ValueError(
                f"Step {i} 'think_time' must be a number or null, got {type(think_time).__name__}"
            )

        extract = parse_extract_spec(step_data.get("extract"), f"Step {i}")
        assertions = parse_step_assertions(step_data, f"Step {i}")

        steps.append(
            ScenarioStep(
                path=step_data["path"],
                method=step_data.get("method", "GET"),
                body=step_data.get("body"),
                headers=step_data.get("headers", {}),
                assert_status=step_data.get("assert_status"),
                assert_body_contains=step_data.get("assert_body_contains"),
                think_time=step_data.get("think_time"),
                name=step_data.get(
                    "name", f"Step {i + 1}: {step_data.get('method', 'GET')} {step_data['path']}"
                ),
                extract=extract,
                assertions=assertions,
                ws=step_data.get("ws"),
                send=step_data.get("send"),
                expect_message_contains=step_data.get("expect_message_contains"),
                hold=step_data.get("hold", 0.0) or 0.0,
            )
        )

    feeders = parse_data_spec(data.get("data"), os.path.dirname(os.path.abspath(path)))
    # Not strict about unknown data sets yet: --data may still supply one.
    _validate_step_templates(steps, feeders, strict_datasets=False)

    scenario = Scenario(
        name=data.get("name", "Unnamed Scenario"),
        base_url=data.get("base_url"),
        think_time=data.get("think_time", 0.0),
        steps=steps,
        on_extract_failure=_parse_choice(data, "on_extract_failure", ON_EXTRACT_FAILURE_CHOICES),
        on_template_error=_parse_choice(data, "on_template_error", ON_TEMPLATE_ERROR_CHOICES),
        session=_parse_choice(data, "session", SESSION_CHOICES),
        data=feeders,
    )
    logger.info(
        "Loaded scenario %r with %d steps from %s",
        scenario.name,
        len(steps),
        path,
    )
    return scenario
