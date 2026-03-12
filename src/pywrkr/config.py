"""Data structures and scenario loading for pywrkr."""

import json
import logging
import os
import random
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TypeVar

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

# Maximum number of unique error type keys tracked per WorkerStats.
# Once the cap is reached, new error strings are folded into a catch-all key.
DEFAULT_MAX_ERROR_TYPES = 1_000
DEFAULT_AUTOFIND_MAX_ERROR_RATE = 1.0
DEFAULT_AUTOFIND_MAX_P95 = 5.0
DEFAULT_AUTOFIND_STEP_DURATION = 30.0
DEFAULT_AUTOFIND_START_USERS = 10
DEFAULT_AUTOFIND_MAX_USERS = 10000
DEFAULT_AUTOFIND_STEP_MULTIPLIER = 2.0


@dataclass
class SSLConfig:
    """SSL/TLS configuration for HTTP connections."""

    verify: bool = False  # Whether to verify SSL certificates
    ca_bundle: str | None = None  # Path to CA bundle file

    @classmethod
    def from_env(cls) -> "SSLConfig":
        """Create SSLConfig from environment variables.

        Environment variables:
            PYWRKR_SSL_VERIFY: Set to '1' or 'true' to enable SSL verification.
            PYWRKR_CA_BUNDLE: Path to a custom CA bundle file.
        """
        import os

        verify_env = os.environ.get("PYWRKR_SSL_VERIFY", "").lower()
        verify = verify_env in ("1", "true", "yes")
        ca_bundle = os.environ.get("PYWRKR_CA_BUNDLE") or None
        return cls(verify=verify, ca_bundle=ca_bundle)


@dataclass
class RequestResult:
    """Result of a single HTTP request."""

    status: int
    latency: float  # seconds
    bytes_read: int
    error: str | None = None


@dataclass
class LatencyBreakdown:
    """Per-request latency breakdown into phases."""

    dns: float = 0.0  # DNS lookup time (seconds)
    connect: float = 0.0  # TCP connect time (seconds)
    tls: float = 0.0  # TLS handshake time (seconds)
    ttfb: float = 0.0  # Time to first byte (seconds)
    transfer: float = 0.0  # Response body transfer time (seconds)
    is_reused: bool = False  # True if the connection was reused (DNS/connect/TLS will be 0)


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
    def from_list(cls, items: list, capacity: int = DEFAULT_RESERVOIR_SIZE,
                  total_seen: int | None = None) -> "ReservoirSampler":
        """Reconstruct a sampler from an already-sampled list.

        Used during deserialization and merge operations.  If *total_seen*
        is ``None`` it defaults to ``len(items)`` (i.e. no sampling occurred).
        """
        sampler = cls.__new__(cls)
        list.__init__(sampler, items[:capacity])
        sampler.capacity = capacity
        sampler.total_seen = total_seen if total_seen is not None else len(items)
        return sampler

    def __repr__(self):
        return (f"ReservoirSampler(capacity={self.capacity}, "
                f"total_seen={self.total_seen}, len={len(self)})")


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


@dataclass
class WorkerStats:
    """Aggregated statistics collected by a single worker."""

    results: list[RequestResult] = field(default_factory=list)
    total_requests: int = 0
    total_bytes: int = 0
    errors: int = 0
    error_types: CappedErrorDict = field(
        default_factory=lambda: CappedErrorDict(DEFAULT_MAX_ERROR_TYPES)
    )
    status_codes: dict[int, int] = field(default_factory=lambda: defaultdict(int))
    latencies: ReservoirSampler = field(
        default_factory=lambda: ReservoirSampler(DEFAULT_RESERVOIR_SIZE)
    )
    rps_timeline: list[tuple[float, int]] = field(default_factory=list)
    content_length_errors: int = 0
    step_latencies: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    breakdowns: ReservoirSampler = field(
        default_factory=lambda: ReservoirSampler(DEFAULT_RESERVOIR_SIZE)
    )


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
    traffic_profile: "TrafficProfile | None" = None  # noqa: F821
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
    # SLO thresholds
    thresholds: "list[Threshold]" = field(default_factory=list)


@dataclass
class Threshold:
    """An SLO threshold expression (e.g. 'p95 < 300ms')."""

    metric: str  # e.g. "p95"
    operator: str  # e.g. "<"
    value: float  # in seconds for latency, percent for error_rate, raw for rps
    raw_expr: str  # original string for display


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


@dataclass
class ScenarioStep:
    """A single step in a scripted scenario."""

    path: str
    method: str = "GET"
    body: str | dict | None = None
    headers: dict[str, str] = field(default_factory=dict)
    assert_status: int | None = None
    assert_body_contains: str | None = None
    think_time: float | None = None  # per-step override
    name: str | None = None


@dataclass
class Scenario:
    """A scripted multi-step scenario."""

    name: str = "Unnamed Scenario"
    think_time: float = 0.0
    steps: list[ScenarioStep] = field(default_factory=list)


def load_scenario(path: str) -> Scenario:
    """Load a scenario from a JSON or YAML file."""
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Scenario file not found: {path}")

    with open(path, "r") as f:
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
        if "path" not in step_data:
            raise ValueError(f"Step {i} must have a 'path' field")
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
            )
        )

    return Scenario(
        name=data.get("name", "Unnamed Scenario"),
        think_time=data.get("think_time", 0.0),
        steps=steps,
    )
