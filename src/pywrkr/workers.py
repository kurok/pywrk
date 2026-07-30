"""Worker functions and benchmark runners for pywrkr."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import random
import signal
import sys
import time
import uuid
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlparse, urlsplit, urlunsplit

import aiohttp

from pywrkr.assertions import StepAssertions, evaluate_assertions
from pywrkr.backends import (
    Backend,
    BackendSession,
    build_ssl_context,
    create_backend,
    create_cookie_jar,
    target_is_ip_literal,
)
from pywrkr.config import (
    _MAX_STEP_NAMES,
    ActiveUsers,
    AutofindConfig,
    BenchmarkConfig,
    LatencyBreakdown,
    RequestCounter,
    StepResult,
    WorkerStats,
    merge_stats,
)
from pywrkr.feeders import DataRuntime
from pywrkr.reporting import (
    RICH_AVAILABLE,
    _format_latency_short,
    aggregate_breakdowns,
    compute_percentiles,
    describe_session_mode,
    evaluate_thresholds,
    format_bytes,
    format_duration,
    print_autofind_summary,
    print_results,
    print_threshold_results,
    run_baseline_gate,
    run_observability_exports,
)
from pywrkr.streaming import StreamingExporter
from pywrkr.templating import (
    TemplateError,
    TemplateFunctions,
    apply_extractors,
    substitute,
    substitute_structure,
)
from pywrkr.traffic_profiles import RateLimiter

# Re-export aggregate_breakdowns for backward compatibility
__all__ = ["aggregate_breakdowns"]


@dataclass(frozen=True)
class LiveStats:
    """A snapshot of a run in flight, handed to an ``on_tick`` callback."""

    elapsed: float
    total_requests: int
    total_errors: int
    requests_per_sec: float
    active_users: "int | None" = None


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

# Verbosity level tags used in log messages
_V3_TAG = "[v3]"
_V4_TAG = "[v4]"

# Exception families the default (aiohttp) backend raises for a failed request.
# Each backend supplies its own; this is the fallback for direct callers.
_DEFAULT_TRANSPORT_ERRORS: tuple[type[BaseException], ...] = (
    aiohttp.ClientError,
    asyncio.TimeoutError,
    OSError,
)

# Progress bar rendering
_PROGRESS_BAR_WIDTH = 20
_PROGRESS_FILLED_CHAR = "\u2588"
_PROGRESS_EMPTY_CHAR = "\u2591"

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _build_progress_bar(current: float, total: float) -> tuple[str, float]:
    """Build a text progress bar and return (bar_string, percentage).

    Args:
        current: Current progress value (e.g. elapsed time or completed requests).
        total: Total target value.

    Returns:
        Tuple of (bar_string, percentage) where bar_string is a rendered
        progress bar of ``_PROGRESS_BAR_WIDTH`` characters and percentage is
        clamped to [0, 100].
    """
    pct = min(current / total * 100, 100.0) if total > 0 else 100.0
    bar_filled = int(pct / 100 * _PROGRESS_BAR_WIDTH)
    bar_empty = _PROGRESS_BAR_WIDTH - bar_filled
    filled = _PROGRESS_FILLED_CHAR * bar_filled
    empty = _PROGRESS_EMPTY_CHAR * bar_empty
    return f"[{filled}{empty}]", pct


def _normalize_config_duration(config: BenchmarkConfig) -> float | None:
    """Return the effective duration from config, normalized early.

    Centralizes the ``config.duration`` check so callers don't need to
    repeat the conditional logic.
    """
    if config.duration is not None and config.duration > 0:
        return config.duration
    return None


def _record_step_latency(stats: WorkerStats, step_name: str, latency: float) -> None:
    """Record a latency sample for a named step, with bounded keys.

    If the number of unique step names exceeds ``_MAX_STEP_NAMES``, new
    step names are folded into a catch-all ``[other steps]`` bucket to
    prevent unbounded memory growth from dynamic step names.
    """
    if step_name in stats.step_latencies:
        stats.step_latencies[step_name].append(latency)
    elif len(stats.step_latencies) < _MAX_STEP_NAMES:
        stats.step_latencies[step_name].append(latency)
    else:
        stats.step_latencies["[other steps]"].append(latency)


def _record_step_error(stats: WorkerStats, step_name: str) -> None:
    """Attribute one error to a named step, obeying the same key cap."""
    if step_name in stats.step_errors or len(stats.step_errors) < _MAX_STEP_NAMES:
        stats.step_errors[step_name] += 1
    else:
        stats.step_errors["[other steps]"] += 1


def _record_extract_failures(
    stats: WorkerStats,
    failures: list[str],
    step_name: str,
    user_id: int,
    already_counted: bool,
) -> None:
    """Book failed ``extract`` rules into the stats and the error breakdown.

    ``extract_failures`` counts individual rules that produced no value, and
    every one of them gets its own diagnostic key. ``errors`` is left alone when
    the iteration has already been charged (*already_counted*) — otherwise a
    single broken login would be counted once for the 4xx, once for each
    unresolved rule, and again for the ``${var}`` that follows, pushing the error
    rate past 100%.
    """
    stats.extract_failures += len(failures)
    if not already_counted:
        stats.errors += 1
    for failure in failures:
        stats.error_types[f"ExtractFailure: {failure}"] += 1
    logger.warning(
        "Scenario user %d step '%s' extraction failed: %s",
        user_id,
        step_name,
        "; ".join(failures),
    )


# ---------------------------------------------------------------------------
# Live TUI Dashboard
# ---------------------------------------------------------------------------


class LiveDashboard:
    """Real-time terminal dashboard using rich library."""

    def __init__(
        self,
        all_stats: list[WorkerStats],
        config: BenchmarkConfig,
        start_time: float,
        active_users: ActiveUsers | None = None,
    ) -> None:
        """Initialize the dashboard with stats, config, and timing state."""
        self.all_stats = all_stats
        self.config = config
        self.start_time = start_time
        self.active_users = active_users
        # Highest RPS observed so far, used to normalize the throughput bar so
        # it conveys relative load instead of always rendering full.
        self._peak_rps = 0.0

    def _build_display(
        self,
        pct_pairs: "list[tuple[int, float]] | None" = None,
    ) -> "Panel":  # noqa: F821
        """Build the rich Panel for the current dashboard state.

        ``pct_pairs`` is a pre-computed list of (percentile, value) tuples
        produced by ``compute_percentiles``.  Callers that run the sort in an
        executor pass the result here; passing ``None`` renders a placeholder.
        """
        from rich.panel import Panel
        from rich.table import Table

        elapsed = time.monotonic() - self.start_time
        total_req = sum(ws.total_requests for ws in self.all_stats)
        total_err = sum(ws.errors for ws in self.all_stats)
        rps = total_req / elapsed if elapsed > 0 else 0.0
        total_bytes = sum(ws.total_bytes for ws in self.all_stats)
        transfer_rate = total_bytes / elapsed if elapsed > 0 else 0.0
        error_rate = (total_err / total_req * 100) if total_req > 0 else 0.0

        status_codes: dict[int, int] = {}
        for ws in self.all_stats:
            for code, count in ws.status_codes.items():
                status_codes[code] = status_codes.get(code, 0) + count

        if self.config.users:
            mode_str = f"{self.config.users} users, {self.config.duration}s duration"
        elif self.config.num_requests:
            mode_str = f"{self.config.num_requests} requests"
        else:
            mode_str = f"{self.config.duration}s duration"

        duration = _normalize_config_duration(self.config)
        if duration is not None:
            bar, pct = _build_progress_bar(elapsed, duration)
            progress_str = f"Elapsed: {elapsed:.1f}s / {duration:.1f}s  {bar} {pct:.1f}%"
        elif self.config.num_requests:
            total_n = self.config.num_requests
            bar, pct = _build_progress_bar(total_req, total_n)
            progress_str = f"Progress: {total_req}/{total_n}  {bar} {pct:.1f}%"
        else:
            progress_str = f"Elapsed: {elapsed:.1f}s"

        table = Table(show_header=False, show_edge=False, pad_edge=False, box=None)
        table.add_column("key", style="bold cyan", width=16)
        table.add_column("value")

        table.add_row("Target:", self.config.url)
        table.add_row("Mode:", mode_str)
        table.add_row("", progress_str)
        table.add_row("", "")

        if self.active_users is not None:
            table.add_row("Active Users:", f"{self.active_users.count}")

        table.add_row("Requests:", f"{total_req:,}")
        table.add_row("Errors:", f"{total_err:,} ({error_rate:.1f}%)")
        table.add_row("RPS:", f"{rps:,.1f}")
        table.add_row("Transfer:", f"{format_bytes(transfer_rate)}/s")
        table.add_row("", "")

        if pct_pairs:
            pair_dict = dict(pct_pairs)
            p50 = format_duration(pair_dict.get(50, 0))
            p95 = format_duration(pair_dict.get(95, 0))
            p99 = format_duration(pair_dict.get(99, 0))
            table.add_row("Latency", f"p50: {p50}  p95: {p95}  p99: {p99}")
        else:
            table.add_row("Latency", "(no data yet)")

        table.add_row("", "")

        if status_codes:
            codes_str = "  ".join(
                f"{code}: {count}" for code, count in sorted(status_codes.items())
            )
            table.add_row("Status Codes:", codes_str)

        max_bar = 24
        if rps > 0:
            # Normalize against the observed peak RPS so the bar reflects
            # current load relative to the run's high-water mark, rather than
            # always rendering full (rps / max(rps, 1) == 1.0 for any rps >= 1).
            self._peak_rps = max(self._peak_rps, rps)
            bar_len = min(max_bar, max(1, int(rps / max(self._peak_rps, 1e-9) * max_bar)))
            bar = "\u2588" * bar_len + "\u2591" * (max_bar - bar_len)
            table.add_row("Throughput:", f"{bar} {rps:.0f} req/s")

        return Panel(table, title="pywrkr Live Dashboard", border_style="green")

    def _sample_percentiles(self) -> "list[tuple[int, float]] | None":
        """Gather all latency samples and compute percentiles.

        Intentionally a plain (non-async) method so it can be dispatched to a
        thread-pool executor, keeping the sort off the asyncio event loop.
        """
        all_latencies: list[float] = []
        for ws in self.all_stats:
            all_latencies.extend(ws.latencies)
        return compute_percentiles(all_latencies) if all_latencies else None

    async def run(self, stop_event: asyncio.Event) -> None:
        """Update the dashboard every 0.5s until stop_event is set."""
        from rich.live import Live

        pct_pairs = None
        with Live(self._build_display(pct_pairs), refresh_per_second=2) as live:
            while not stop_event.is_set():
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(stop_event.wait(), timeout=0.5)
                    break
                pct_pairs = await asyncio.to_thread(self._sample_percentiles)
                live.update(self._build_display(pct_pairs))


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------


def make_url(url: str, random_param: bool) -> str:
    """Return the URL, optionally appending a unique cache-busting query parameter.

    The cache-busting ``_cb`` parameter is appended to the query component
    (not the raw end of the string), so URLs containing a ``#fragment`` are
    handled correctly: ``_cb`` lands in the query and the fragment is
    preserved.
    """
    if not random_param:
        return url
    parts = urlsplit(url)
    cb = f"_cb={uuid.uuid4().hex}"
    new_query = f"{parts.query}&{cb}" if parts.query else cb
    return urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))


def _build_request_headers(config: BenchmarkConfig) -> dict[str, str]:
    """Build the common request headers from benchmark config.

    Assembles headers from config.headers, adds Basic auth and cookie
    headers if configured. Returns a new dict each call to avoid
    shared mutable state between workers.
    """
    headers = dict(config.headers)
    if config.basic_auth:
        encoded = base64.b64encode(config.basic_auth.encode()).decode()
        headers["Authorization"] = f"Basic {encoded}"
    if config.cookies:
        headers["Cookie"] = "; ".join(config.cookies)
    return headers


# ---------------------------------------------------------------------------
# Latency breakdown tracing
# ---------------------------------------------------------------------------


def create_trace_config(stats: WorkerStats) -> aiohttp.TraceConfig:
    """Create an aiohttp TraceConfig that captures per-request latency breakdown.

    The trace context (a dict) stores timing data per request. When the request
    ends, a LatencyBreakdown is computed and appended to stats.breakdowns.
    """
    trace_config = aiohttp.TraceConfig()

    async def on_request_start(session, trace_ctx, params):
        ctx = trace_ctx.trace_request_ctx
        ctx["request_start"] = time.monotonic()
        ctx["dns_start"] = None
        ctx["dns_end"] = None
        ctx["conn_start"] = None
        ctx["conn_end"] = None
        ctx["headers_sent"] = None
        ctx["first_byte"] = None
        ctx["is_reused"] = True  # assume reused; set to False if we see connection creation

    async def on_dns_resolvehost_start(session, trace_ctx, params):
        ctx = trace_ctx.trace_request_ctx
        ctx["dns_start"] = time.monotonic()
        ctx["is_reused"] = False

    async def on_dns_resolvehost_end(session, trace_ctx, params):
        ctx = trace_ctx.trace_request_ctx
        ctx["dns_end"] = time.monotonic()

    async def on_connection_create_start(session, trace_ctx, params):
        ctx = trace_ctx.trace_request_ctx
        ctx["conn_start"] = time.monotonic()
        ctx["is_reused"] = False

    async def on_connection_create_end(session, trace_ctx, params):
        ctx = trace_ctx.trace_request_ctx
        ctx["conn_end"] = time.monotonic()

    async def on_request_headers_sent(session, trace_ctx, params):
        ctx = trace_ctx.trace_request_ctx
        ctx["headers_sent"] = time.monotonic()

    async def on_response_chunk_received(session, trace_ctx, params):
        ctx = trace_ctx.trace_request_ctx
        if ctx.get("first_byte") is None:
            ctx["first_byte"] = time.monotonic()

    async def on_request_end(session, trace_ctx, params):
        ctx = trace_ctx.trace_request_ctx
        end = time.monotonic()

        # Defensive: if on_request_start never fired, skip breakdown
        if ctx.get("request_start") is None:
            logger.debug("Trace context missing 'request_start'; skipping breakdown")
            return

        # DNS time
        dns = 0.0
        if ctx.get("dns_start") is not None and ctx.get("dns_end") is not None:
            dns = ctx["dns_end"] - ctx["dns_start"]

        # TCP connect time (includes TLS if HTTPS)
        connect_total = 0.0
        if ctx.get("conn_start") is not None and ctx.get("conn_end") is not None:
            connect_total = ctx["conn_end"] - ctx["conn_start"]

        # NOTE: TLS time is an *estimate*.  aiohttp's connection_create
        # callback spans TCP+TLS combined and does not provide a separate
        # TLS-only signal.  The value reported here (currently 0.0) is a
        # best-effort approximation; treat the ``tls`` field in
        # LatencyBreakdown as ``tls_estimated`` in any analysis or reports.
        tls = 0.0
        tcp_connect = connect_total  # default: entire connect time is TCP

        # TTFB: from headers_sent to first byte received
        ttfb = 0.0
        headers_sent = ctx.get("headers_sent")
        first_byte = ctx.get("first_byte")
        if headers_sent is not None and first_byte is not None:
            ttfb = first_byte - headers_sent
        elif headers_sent is not None:
            # No chunks received (empty body) -- use end time
            ttfb = end - headers_sent

        # Transfer time: from first byte to end
        transfer = 0.0
        if first_byte is not None:
            transfer = end - first_byte

        bd = LatencyBreakdown(
            dns=max(dns, 0.0),
            connect=max(tcp_connect, 0.0),
            tls=max(tls, 0.0),
            ttfb=max(ttfb, 0.0),
            transfer=max(transfer, 0.0),
            is_reused=ctx.get("is_reused", True),
        )
        stats.breakdowns.append(bd)

    trace_config.on_request_start.append(on_request_start)
    trace_config.on_dns_resolvehost_start.append(on_dns_resolvehost_start)
    trace_config.on_dns_resolvehost_end.append(on_dns_resolvehost_end)
    trace_config.on_connection_create_start.append(on_connection_create_start)
    trace_config.on_connection_create_end.append(on_connection_create_end)
    trace_config.on_request_headers_sent.append(on_request_headers_sent)
    trace_config.on_response_chunk_received.append(on_response_chunk_received)
    trace_config.on_request_end.append(on_request_end)

    return trace_config


# ---------------------------------------------------------------------------
# Shared request execution helper
# ---------------------------------------------------------------------------


class _RequestResult:
    """Result from _execute_request; avoids creating dataclass per request."""

    __slots__ = (
        "latency",
        "status",
        "data_len",
        "error_name",
        "cancelled",
        "body",
        "headers",
        "counted_error",
    )

    def __init__(self) -> None:
        self.latency: float = 0.0
        self.status: int = 0
        self.data_len: int = 0
        self.error_name: str | None = None
        self.cancelled: bool = False
        # Response payload/headers, retained only when capture_response is set
        # (scenario steps with extract rules) so plain benchmarks keep no
        # per-request references alive.
        self.body: bytes | None = None
        self.headers: dict[str, str] | None = None
        # True when this request already contributed to stats.errors, so callers
        # adding their own failure modes do not double-count it.
        self.counted_error: bool = False


async def _execute_request(
    session: "BackendSession",
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes | None,
    ssl_verify: bool,
    timeout: float,
    stats: WorkerStats,
    config: BenchmarkConfig,
    trace_ctx: dict[str, object] | None,
    expected_length_ref: list[int | None],
    step_name: str | None = None,
    assertions: "StepAssertions | None" = None,
    log_prefix: str = "",
    capture_response: bool = False,
    transport_errors: tuple[type[BaseException], ...] = _DEFAULT_TRANSPORT_ERRORS,
) -> _RequestResult:
    """Execute a single HTTP request and record stats.

    This is the shared core extracted from worker(), user_worker(), and
    scenario_worker(). Handles: request execution, latency recording, status
    code counting, content-length verification, error handling, and step
    latency tracking.

    The request goes through a :class:`~pywrkr.backends.BackendSession`, so the
    same loop drives HTTP/1.1 and HTTP/2. *transport_errors* comes from the
    backend, since each client library raises its own exception family.

    The response body is always read; *capture_response* only controls whether
    the bytes and headers are handed back for scenario variable extraction.

    Returns a _RequestResult with outcome details.
    """
    result = _RequestResult()
    req_start = time.monotonic()
    try:
        resp = await session.send(method, url, headers, body, timeout, trace_ctx)
        data = resp.body
        latency = time.monotonic() - req_start
        result.latency = latency
        result.status = resp.status
        result.data_len = len(data)
        if capture_response:
            result.body = data
            result.headers = dict(resp.headers)

        stats.total_requests += 1
        stats.total_bytes += len(data)
        stats.latencies.append(latency)
        if stats.window_latencies is not None:
            stats.window_latencies.append(latency)
        stats.status_codes[resp.status] += 1
        stats.http_versions[resp.http_version] += 1
        if step_name:
            _record_step_latency(stats, step_name, latency)

        # Content-length verification (ab -l style).
        #
        # ab's -l only checks that the *declared* Content-Length is
        # consistent across responses; it does NOT require the received
        # body length to equal the declared value. HTTP clients transparently
        # decompress gzip/deflate bodies, so ``len(data)`` is the
        # decompressed size while Content-Length is the on-wire compressed
        # size -- comparing them would flag every compressed response as an
        # error. We therefore compare declared values for consistency only.
        if config.verify_content_length:
            cl = resp.headers.get("Content-Length")
            if cl is not None:
                try:
                    declared = int(cl)
                except (TypeError, ValueError):
                    # Malformed Content-Length header: count it and skip
                    # the comparison rather than letting ValueError escape
                    # and kill the worker.
                    stats.content_length_errors += 1
                else:
                    if expected_length_ref[0] is None:
                        expected_length_ref[0] = declared
                    if declared != expected_length_ref[0]:
                        stats.content_length_errors += 1

        # Assertion checks (scenario mode). Every failed rule gets its own key
        # in the error breakdown, but the request is only counted as one error
        # however many of them broke.
        assertion_failed = False
        if assertions is not None and assertions.any:
            failures = evaluate_assertions(assertions, resp.status, data, resp.headers, latency)
            if failures:
                stats.errors += 1
                assertion_failed = True
                for failure in failures:
                    stats.error_types[failure.key] += 1
                if step_name:
                    _record_step_error(stats, step_name)
                if config.verbosity >= 2:
                    logger.warning(
                        "%sassertion failed: %s",
                        log_prefix,
                        "; ".join(f.message for f in failures),
                    )

        if not assertion_failed and resp.status >= 400:
            stats.errors += 1
            stats.error_types[f"HTTP {resp.status}"] += 1
            result.counted_error = True
            if step_name:
                _record_step_error(stats, step_name)
        else:
            result.counted_error = assertion_failed

        if config.verbosity >= 4:
            logger.debug(
                "%s %s %s -> %s (%dB, %s)",
                _V4_TAG,
                method,
                url,
                resp.status,
                len(data),
                format_duration(latency),
            )
        elif config.verbosity >= 3:
            logger.debug("%s %s", _V3_TAG, resp.status)

    except asyncio.CancelledError:
        result.cancelled = True
    except transport_errors as e:
        latency = time.monotonic() - req_start
        result.latency = latency
        error_name = type(e).__name__
        result.error_name = error_name
        result.counted_error = True
        stats.total_requests += 1
        stats.errors += 1
        stats.error_types[error_name] += 1
        stats.latencies.append(latency)
        if step_name:
            _record_step_error(stats, step_name)
            # Go through the helper so the error path obeys the same
            # `_MAX_STEP_NAMES` cap as the success path. Appending directly
            # let a long-running benchmark with many distinct error step
            # names grow `step_latencies` without bound.
            _record_step_latency(stats, step_name, latency)
        logger.warning("%sRequest error: %s: %s", log_prefix, error_name, e)

    return result


# Cookie/SSL construction lives with the backends now, since each client library
# builds them differently. Aliased here because these were part of this module's
# surface before the split.
_target_is_ip_literal = target_is_ip_literal
_create_cookie_jar = create_cookie_jar
_create_ssl_context = build_ssl_context


async def _think_time_wait(
    think: float,
    jitter: float,
    stop_event: asyncio.Event,
) -> bool:
    """Sleep for think time with jitter. Returns True if stop_event was set."""
    if think <= 0 or stop_event.is_set():
        return stop_event.is_set()
    lo = think * (1 - jitter)
    hi = think * (1 + jitter)
    delay = random.uniform(lo, hi)
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=delay)
        return True  # stop_event was set
    except asyncio.TimeoutError:
        return False  # think time elapsed normally


def _calc_effective_timeout(
    config: BenchmarkConfig,
    start_time: float,
) -> float:
    """Calculate effective request timeout considering remaining duration.

    Returns at least 0.1s to avoid zero/negative timeouts when the
    benchmark has overrun its duration window.
    """
    if config.duration is not None:
        remaining = config.duration - (time.monotonic() - start_time)
        return max(0.1, min(config.timeout_sec, remaining + 1))
    return config.timeout_sec


# ---------------------------------------------------------------------------
# Workers
# ---------------------------------------------------------------------------


async def worker(
    config: BenchmarkConfig,
    stats: WorkerStats,
    backend: Backend,
    stop_event: asyncio.Event,
    request_counter: RequestCounter | None = None,
    rate_limiter: RateLimiter | None = None,
) -> None:
    """Async worker coroutine that sends HTTP requests in a loop.

    Executes HTTP requests against the configured URL until the stop condition
    is met (duration elapsed, request count reached, or stop_event set).

    NOTE: user_worker and scenario_worker share this loop skeleton (rate
    limiting, duration tracking, stats recording). Any change to the loop
    logic here must be mirrored in both. Known divergence: rps_timeline is
    batched per-second here but recorded per-request in user_worker /
    scenario_worker. See issue #123 for the consolidation plan.
    """
    logger.debug("Worker starting (target=%s)", config.url)
    start_time = time.monotonic()
    interval_start = start_time
    interval_count = 0

    req_headers = _build_request_headers(config)
    expected_length_ref: list[int | None] = [None]
    client_timeout = config.timeout_sec

    # Plain mode has no virtual-user identity to isolate, so it keeps the client
    # library's default jar unless cookie sessions were explicitly turned off.
    async with backend.create_session(stats, isolate_cookies=False) as session:
        while not stop_event.is_set():
            if config.duration is not None:
                elapsed = time.monotonic() - start_time
                if elapsed >= config.duration:
                    break

            if request_counter is not None:
                if request_counter.remaining <= 0:
                    break
                request_counter.remaining -= 1

            if rate_limiter is not None:
                await rate_limiter.acquire()
                if stop_event.is_set():
                    break

            if config.duration is not None:
                client_timeout = _calc_effective_timeout(config, start_time)
            request_url = make_url(config.url, config.random_param)
            trace_ctx = {} if config.latency_breakdown else None

            result = await _execute_request(
                session,
                config.method,
                request_url,
                req_headers,
                config.body,
                config.ssl_config.verify,
                client_timeout,
                stats,
                config,
                trace_ctx,
                expected_length_ref,
                transport_errors=backend.transport_errors,
            )
            if result.cancelled:
                break

            interval_count += 1
            now = time.monotonic()
            if now - interval_start >= 1.0:
                stats.rps_timeline.append((interval_start, interval_count))
                interval_start = now
                interval_count = 0

        if interval_count > 0:
            stats.rps_timeline.append((interval_start, interval_count))

    logger.debug(
        "Worker finished: %d requests, %d errors",
        stats.total_requests,
        stats.errors,
    )


async def user_worker(
    user_id: int,
    config: BenchmarkConfig,
    stats: WorkerStats,
    backend: Backend,
    stop_event: asyncio.Event,
    start_time: float,
    active_users: ActiveUsers,
    rate_limiter: RateLimiter | None = None,
) -> None:
    """Simulate a single virtual user with configurable think time.

    NOTE: Shares a loop skeleton with worker and scenario_worker (rate
    limiting, duration tracking, stats recording). Any change to that logic
    must be mirrored across all three. Known divergence: rps_timeline is
    recorded per-request here but batched per-second in worker. See #123.
    """
    logger.debug("User %d starting", user_id)
    req_headers = _build_request_headers(config)
    expected_length_ref: list[int | None] = [None]
    active_users.count += 1
    client_timeout = config.timeout_sec
    interval_start = start_time
    interval_count = 0

    try:
        # One jar per virtual user: Set-Cookie from this user's responses is
        # replayed only to this user, so N VUs look like N distinct clients.
        async with backend.create_session(stats) as session:
            while not stop_event.is_set():
                elapsed = time.monotonic() - start_time
                if config.duration is not None and elapsed >= config.duration:
                    break

                # The rate limiter is authoritative whenever present. Previously
                # it was only consulted when think_time == 0, so `-u N --rate R`
                # with the default think_time=1.0 silently ignored --rate.
                # Think time (applied below) is now additive on top of the rate
                # cap rather than overriding it.
                if rate_limiter is not None:
                    await rate_limiter.acquire()
                    if stop_event.is_set():
                        break

                if config.duration is not None:
                    client_timeout = _calc_effective_timeout(config, start_time)
                request_url = make_url(config.url, config.random_param)
                trace_ctx = {} if config.latency_breakdown else None

                result = await _execute_request(
                    session,
                    config.method,
                    request_url,
                    req_headers,
                    config.body,
                    config.ssl_config.verify,
                    client_timeout,
                    stats,
                    config,
                    trace_ctx,
                    expected_length_ref,
                    log_prefix=f"User {user_id} ",
                    transport_errors=backend.transport_errors,
                )
                if result.cancelled:
                    break

                interval_count += 1
                now = time.monotonic()
                if now - interval_start >= 1.0:
                    stats.rps_timeline.append((interval_start, interval_count))
                    interval_start = now
                    interval_count = 0

                if await _think_time_wait(config.think_time, config.think_time_jitter, stop_event):
                    break

        if interval_count > 0:
            stats.rps_timeline.append((interval_start, interval_count))
    finally:
        active_users.count -= 1
        logger.debug(
            "User %d finished: %d requests, %d errors",
            user_id,
            stats.total_requests,
            stats.errors,
        )


def _prepare_step_body(step_body, headers: dict) -> bytes | None:
    """Serialize a scenario step body and set Content-Type if needed."""
    if step_body is None:
        return None
    if isinstance(step_body, (dict, list)):
        if "Content-Type" not in headers:
            headers["Content-Type"] = "application/json"
        return json.dumps(step_body).encode()
    if isinstance(step_body, str):
        return step_body.encode()
    return step_body


def _render_step(
    step,
    base_headers: dict[str, str],
    variables: dict[str, str],
    keep_literal: bool,
    rows: "dict[str, dict[str, str]] | None" = None,
    functions: "TemplateFunctions | None" = None,
) -> tuple[str, dict[str, str], bytes | None]:
    """Expand placeholders in one step against the user's current bindings.

    Substitution covers the path, header names and values, and the body (walking
    into JSON object/array bodies so placeholders work at any depth), resolving
    extracted variables, the user's current data rows, and generator functions.

    Returns:
        ``(path, headers, body)`` ready to hand to ``_execute_request``.

    Raises:
        TemplateError: A placeholder cannot be expanded and *keep_literal* is
            False.
    """
    path = substitute(step.path, variables, keep_literal, rows, functions)

    headers = dict(base_headers)
    for key, value in step.headers.items():
        headers[substitute(key, variables, keep_literal, rows, functions)] = substitute(
            value, variables, keep_literal, rows, functions
        )

    # substitute() short-circuits on strings without "${", so this stays cheap
    # for the (common) steps that carry no placeholders at all.
    body = substitute_structure(step.body, variables, keep_literal, rows, functions)

    return path, headers, _prepare_step_body(body, headers)


async def _run_ws_step(
    step,
    ws_url: str,
    headers: dict[str, str],
    session,
    config: BenchmarkConfig,
    stats: WorkerStats,
    step_name: str,
    user_id: int,
    variables: dict[str, str],
    keep_literal: bool,
    rows,
    functions,
    stop_event: asyncio.Event,
):
    """Execute one scenario ``ws:`` step against this user's session."""
    from pywrkr.websockets import WsStats, WsStepOutcome, execute_ws_step

    client = session.raw_websocket_session()
    if client is None:
        # --http2 selects the httpx backend, which has no WebSocket client.
        # Saying so beats an AttributeError deep in the step loop.
        return WsStepOutcome(
            ok=False, error="WsUnsupportedBackend: ws: steps require the aiohttp backend"
        )

    if stats.ws is None:
        # A scenario measures the whole step, not a handshake or a round trip,
        # and its `total_requests` counts steps like every other step type.
        stats.ws = WsStats(latency_metric="step", primary_metric="steps")
    send = step.send
    if send is not None:
        send = substitute(send, variables, keep_literal, rows, functions)

    outcome = await execute_ws_step(
        client,
        ws_url,
        headers,
        send=send,
        expect_contains=step.expect_message_contains,
        hold=step.hold,
        timeout=config.timeout_sec,
        stats=stats,
        ws_stats=stats.ws,
        stop=stop_event,
    )
    if not outcome.ok:
        logger.warning("Scenario user %d step '%s' failed: %s", user_id, step_name, outcome.error)
    return outcome


async def scenario_worker(
    user_id: int,
    config: BenchmarkConfig,
    stats: WorkerStats,
    backend: Backend,
    stop_event: asyncio.Event,
    start_time: float,
    active_users: ActiveUsers,
    request_counter: RequestCounter | None = None,
    rate_limiter: RateLimiter | None = None,
    data_runtime: "DataRuntime | None" = None,
) -> None:
    """Execute a scripted multi-step scenario in a loop.

    *data_runtime* is shared by every virtual user: one row cursor per data set,
    so ``unique`` really is unique across users, plus the run's generator
    functions, so ``counter()`` is monotonic across the run. Each iteration draws
    one row per data set; when a consuming data set runs dry this user stops
    instead of replaying stale rows.

    Variables extracted by a step's ``extract`` rules are bound in a dict that
    is private to this coroutine (i.e. per virtual user) and cleared at the top
    of every iteration, so tokens never leak between users and each iteration
    starts from the same known state. Cookies get the same treatment through a
    per-VU jar, which ``session: fresh_per_iteration`` additionally empties
    between iterations.

    NOTE: Shares a loop skeleton with worker and user_worker (rate limiting,
    duration tracking, stats recording). Any change to that logic must be
    mirrored across all three. Known divergence: rps_timeline is recorded
    per-request here but batched per-second in worker. See #123.
    """
    logger.debug("Scenario user %d starting", user_id)
    scenario = config.scenario
    if not scenario:
        return

    base_headers = _build_request_headers(config)
    parsed = urlparse(config.url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    expected_length_ref: list[int | None] = [None]
    client_timeout = config.timeout_sec
    interval_start = start_time
    interval_count = 0

    # Per-VU correlation scope; reset at the start of every iteration.
    variables: dict[str, str] = {}
    keep_literal = scenario.on_template_error == "keep_literal"
    abort_on_extract_failure = scenario.on_extract_failure == "abort_iteration"
    fresh_session_per_iteration = scenario.session == "fresh_per_iteration"
    runtime = data_runtime if data_runtime is not None else DataRuntime.for_feeders(scenario.data)

    active_users.count += 1
    try:
        async with backend.create_session(stats) as session:
            while not stop_event.is_set():
                rows = runtime.next_rows()
                if rows is None:
                    logger.info(
                        "Scenario user %d stopping: data set(s) %s exhausted",
                        user_id,
                        ", ".join(runtime.exhausted_feeders) or "unknown",
                    )
                    return
                variables.clear()
                if fresh_session_per_iteration:
                    session.clear_cookies()
                iteration_aborted = False
                # An iteration contributes at most one error to the aggregate
                # total: a failed request, a failed extraction, and the ${var}
                # that could not resolve because of it are one broken flow, not
                # three. The dedicated counters still record every occurrence.
                iteration_error_counted = False
                # Think time of the step the iteration reached, reused to pace a
                # retry after an abort.
                pace = 0.0
                for step in scenario.steps:
                    if stop_event.is_set():
                        break

                    if config.duration is not None:
                        elapsed = time.monotonic() - start_time
                        if elapsed >= config.duration:
                            return

                    if request_counter is not None:
                        if request_counter.remaining <= 0:
                            return
                        request_counter.remaining -= 1

                    # Honor --rate in scenario mode too. Without this the rate
                    # limiter is built and the banner advertises it, but it is
                    # never consulted, so --rate was silently ignored for every
                    # scenario run.
                    if rate_limiter is not None:
                        await rate_limiter.acquire()
                        if stop_event.is_set():
                            return

                    if config.duration is not None:
                        client_timeout = _calc_effective_timeout(config, start_time)

                    step_name = step.name or f"{step.method} {step.path}"
                    think = step.think_time if step.think_time is not None else scenario.think_time
                    if think <= 0 and config.think_time > 0:
                        think = config.think_time
                    pace = think

                    try:
                        step_path, req_headers, body = _render_step(
                            step,
                            base_headers,
                            variables,
                            keep_literal,
                            rows,
                            runtime.functions,
                        )
                    except TemplateError as exc:
                        stats.template_errors += 1
                        stats.error_types[f"TemplateError: {exc}"] += 1
                        if not iteration_error_counted:
                            stats.errors += 1
                            iteration_error_counted = True
                        logger.warning(
                            "Scenario user %d step '%s' template error: %s", user_id, step_name, exc
                        )
                        iteration_aborted = True
                        break

                    if step.is_websocket:
                        # A ws: step carries an absolute URL, so it does not get
                        # the scenario's base_url prepended, and it opens its
                        # socket on this user's own session -- which is what
                        # carries the cookies an earlier login step set.
                        outcome = await _run_ws_step(
                            step,
                            step_path,
                            req_headers,
                            session,
                            config,
                            stats,
                            step_name,
                            user_id,
                            variables,
                            keep_literal,
                            rows,
                            runtime.functions,
                            stop_event,
                        )
                        if not outcome.ok:
                            _record_step_error(stats, step_name)
                            stats.error_types[outcome.error or "WsError"] += 1
                            if not iteration_error_counted:
                                stats.errors += 1
                                iteration_error_counted = True
                            iteration_aborted = True
                            break
                        stats.total_requests += 1
                        _record_step_latency(stats, step_name, outcome.latency)
                        stats.latencies.append(outcome.latency)
                        if stats.window_latencies is not None:
                            stats.window_latencies.append(outcome.latency)
                        if step.extract:
                            values, failures = apply_extractors(step.extract, outcome.body, {})
                            variables.update(values)
                            if failures:
                                _record_extract_failures(
                                    stats, failures, step_name, user_id, iteration_error_counted
                                )
                                iteration_error_counted = True
                                if abort_on_extract_failure:
                                    iteration_aborted = True
                                    break
                        if await _think_time_wait(think, config.think_time_jitter, stop_event):
                            return
                        continue

                    request_url = make_url(f"{base_url}{step_path}", config.random_param)

                    # Fresh dict per request so TraceConfig callbacks don't
                    # bleed timing from the previous step into this one.
                    trace_ctx = {} if config.latency_breakdown else None

                    result = await _execute_request(
                        session,
                        step.method,
                        request_url,
                        req_headers,
                        body,
                        config.ssl_config.verify,
                        client_timeout,
                        stats,
                        config,
                        trace_ctx,
                        expected_length_ref,
                        step_name=step_name,
                        assertions=step.assertions,
                        log_prefix=f"Scenario user {user_id} step '{step_name}' ",
                        capture_response=bool(step.extract),
                        transport_errors=backend.transport_errors,
                    )
                    if result.cancelled:
                        return
                    if result.counted_error:
                        iteration_error_counted = True

                    interval_count += 1
                    now = time.monotonic()
                    if now - interval_start >= 1.0:
                        stats.rps_timeline.append((interval_start, interval_count))
                        interval_start = now
                        interval_count = 0

                    if step.extract:
                        if result.error_name is None:
                            values, failures = apply_extractors(
                                step.extract, result.body, result.headers
                            )
                            variables.update(values)
                            if failures:
                                _record_extract_failures(
                                    stats, failures, step_name, user_id, iteration_error_counted
                                )
                                iteration_error_counted = True
                                iteration_aborted = abort_on_extract_failure
                        else:
                            # The request itself failed, so there is no response
                            # to extract from and the transport error is already
                            # counted. Skip the rest of the iteration so later
                            # steps do not run without their variables.
                            iteration_aborted = abort_on_extract_failure

                    if iteration_aborted:
                        break

                    if await _think_time_wait(think, config.think_time_jitter, stop_event):
                        return

                if iteration_aborted and not stop_event.is_set():
                    # Pace the retry like a completed iteration, and always yield
                    # at least once: an iteration that aborts before sending
                    # anything would otherwise monopolise the event loop when
                    # there is no think time.
                    await asyncio.sleep(0)
                    if await _think_time_wait(pace, config.think_time_jitter, stop_event):
                        return

    finally:
        if interval_count > 0:
            stats.rps_timeline.append((interval_start, interval_count))
        active_users.count -= 1
        logger.debug(
            "Scenario user %d finished: %d requests, %d errors",
            user_id,
            stats.total_requests,
            stats.errors,
        )


async def show_progress(
    start: float,
    duration: float | None,
    total_requests: int | None,
    all_stats: list[WorkerStats],
    stop: asyncio.Event,
    active_users: ActiveUsers | None = None,
    on_tick: "Callable[[LiveStats], None] | None" = None,
    silent: bool = False,
) -> None:
    """Display a text-based progress line during benchmark execution.

    *on_tick* receives a :class:`LiveStats` snapshot once a second, which is how
    a library caller subscribes to progress. *silent* suppresses the terminal
    line, so a library run writes nothing to stdout.
    """
    while not stop.is_set():
        # Wait up to 1s but wake immediately when stop is set, so the task
        # exits promptly instead of finishing a full sleep after the run ends.
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=1)
            break
        elapsed = time.monotonic() - start
        total_req = sum(ws.total_requests for ws in all_stats)
        total_err = sum(ws.errors for ws in all_stats)
        rps = total_req / elapsed if elapsed > 0 else 0

        if on_tick is not None:
            # A caller-supplied callback must never take the run down with it.
            try:
                on_tick(
                    LiveStats(
                        elapsed=elapsed,
                        total_requests=total_req,
                        total_errors=total_err,
                        requests_per_sec=rps,
                        active_users=active_users.count if active_users is not None else None,
                    )
                )
            except Exception:
                logger.exception("on_tick callback raised; continuing the run")

        if silent:
            continue

        users_str = ""
        if active_users is not None:
            users_str = f" | {active_users.count:>5} users"

        if duration is not None:
            _, pct = _build_progress_bar(elapsed, duration)
            sys.stdout.write(
                f"\r  [{pct:5.1f}%] {total_req:>8} requests "
                f"| {rps:>8.1f} req/s | {total_err} errors{users_str} "
            )
        elif total_requests is not None:
            _, pct = _build_progress_bar(total_req, total_requests)
            sys.stdout.write(
                f"\r  [{pct:5.1f}%] {total_req:>8}/{total_requests} requests "
                f"| {rps:>8.1f} req/s | {total_err} errors{users_str} "
            )
        sys.stdout.flush()
    if not silent:
        sys.stdout.write("\r" + " " * 80 + "\r")
        sys.stdout.flush()


# ---------------------------------------------------------------------------
# Shared runner helpers
# ---------------------------------------------------------------------------


def _setup_signal_handlers(stop_event: asyncio.Event) -> None:
    """Register SIGINT/SIGTERM handlers that set the stop event."""
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)


def _create_streaming_exporter(
    config: BenchmarkConfig,
    all_stats: list[WorkerStats],
    start_time: float,
    *,
    active_users: "ActiveUsers | None" = None,
    rate_limiter: "RateLimiter | None" = None,
) -> "StreamingExporter | None":
    """Build the streaming exporter, or None when nothing asked for one."""
    if not config.export_interval:
        return None
    if not (config.otel_endpoint or config.prom_remote_write):
        return None
    return StreamingExporter(
        config,
        all_stats,
        config.export_interval,
        active_users=active_users,
        rate_limiter=rate_limiter,
        start_time=start_time,
    )


def _create_rate_limiter(config: BenchmarkConfig, duration: float | None) -> RateLimiter | None:
    """Create a rate limiter from config, or None if rate limiting is disabled."""
    if config.rate is None:
        return None
    ramp_duration = duration if config.rate_ramp is not None else None
    return RateLimiter(
        rate=config.rate,
        end_rate=config.rate_ramp,
        ramp_duration=ramp_duration,
        traffic_profile=config.traffic_profile,
        duration=duration,
    )


def _create_progress_task(
    config: BenchmarkConfig,
    all_stats: list[WorkerStats],
    start_time: float,
    stop_event: asyncio.Event,
    *,
    duration: float | None = None,
    num_requests: int | None = None,
    active_users: ActiveUsers | None = None,
    quiet: bool = False,
    on_tick: "Callable[[LiveStats], None] | None" = None,
) -> asyncio.Task:
    """Create the progress display or live dashboard task."""
    if quiet and on_tick is None:

        async def _wait_stop(stop):
            await stop.wait()

        return asyncio.create_task(_wait_stop(stop_event))

    if quiet:
        # Silent, but still driving the caller's callback.
        return asyncio.create_task(
            show_progress(
                start_time,
                duration,
                num_requests,
                all_stats,
                stop_event,
                active_users,
                on_tick=on_tick,
                silent=True,
            )
        )

    if config.live_dashboard and RICH_AVAILABLE:
        dashboard = LiveDashboard(all_stats, config, start_time, active_users)
        return asyncio.create_task(dashboard.run(stop_event))

    if config.live_dashboard and not RICH_AVAILABLE:
        logger.warning("--live requires 'rich' package. Install with: pip install pywrkr[tui]")
        logger.warning("Falling back to standard progress display.")

    return asyncio.create_task(
        show_progress(
            start_time,
            duration,
            num_requests,
            all_stats,
            stop_event,
            active_users,
            on_tick=on_tick,
        )
    )


async def _finalize_run(
    tasks: list[asyncio.Task],
    stop_event: asyncio.Event,
    progress_task: asyncio.Task,
    backend: Backend,
    all_stats: list[WorkerStats],
    start_time: float,
    config: BenchmarkConfig,
    rate_limiter: RateLimiter | None,
    concurrency: int,
    *,
    quiet: bool = False,
    on_complete: "Callable[[WorkerStats, float, int], None] | None" = None,
    streaming: "StreamingExporter | None" = None,
) -> tuple[WorkerStats, int]:
    """Await workers, merge stats, print results, and evaluate thresholds.

    *on_complete* receives ``(stats, actual_duration, concurrency)`` — the exact
    numbers this run reported with, so a library caller can build the same
    result the CLI prints rather than re-deriving the duration.
    """
    worker_crashed = False
    try:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        # Sample end_time immediately after the workers finish, BEFORE tearing
        # down the progress task. The progress/dashboard task sleeps in ~1s
        # increments, so awaiting it can block up to ~1s past the real end of
        # load, which would inflate the reported duration and deflate RPS.
        end_time = time.monotonic()

        # Surface (rather than silently swallow) any unexpected worker crash.
        # _execute_request only catches network errors; any other exception
        # escaping a worker would otherwise be discarded here, masking dropped
        # load as a clean, successful run.
        for i, r in enumerate(results):
            if isinstance(r, BaseException) and not isinstance(r, asyncio.CancelledError):
                worker_crashed = True
                logger.error("Worker %d crashed: %s: %s", i, type(r).__name__, r)

        # Signal all workers to stop before accessing shared stats to avoid
        # race conditions where a still-running worker mutates stats during merge.
        stop_event.set()
        _ = await progress_task
    finally:
        # Cancellation-safe teardown: ensure the progress/dashboard task is
        # always stopped (it loops on `while not stop.is_set()`), even if this
        # coroutine is cancelled while awaiting gather above.
        stop_event.set()
        if not progress_task.done():
            progress_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                _ = await progress_task
        await backend.aclose()

    if streaming is not None:
        # Emits a final snapshot, so a run cut short by SIGINT still leaves its
        # last state in the TSDB.
        await streaming.aclose()

    actual_duration = end_time - start_time
    merged = merge_stats(all_stats)
    logger.debug(
        "Merged stats from %d workers: %d total requests, %d latency samples, %d breakdown samples",
        len(all_stats),
        merged.total_requests,
        len(merged.latencies),
        len(merged.breakdowns),
    )

    if not quiet:
        print_results(merged, actual_duration, concurrency, start_time, config, rate_limiter)
        summary = streaming.summary() if streaming is not None else None
        if summary:
            print(f"\n  {summary}", file=sys.stdout)
    if streaming is not None and not streaming.all_delivered:
        logger.warning(
            "Streaming export incomplete: %d failed, %d never delivered, %d dropped. "
            "The collector was unreachable or could not keep up.",
            streaming.failed,
            streaming.undelivered,
            streaming.dropped,
        )

    exit_code = 0
    if config.thresholds:
        th_results = evaluate_thresholds(config.thresholds, merged, actual_duration)
        if not quiet:
            print_threshold_results(th_results, file=sys.stdout)
        if any(not passed for _, _, passed in th_results):
            exit_code = 2

    # Baseline gate. An absolute threshold breach (2) is the stronger statement,
    # so it keeps precedence over a relative regression (3).
    baseline_code = run_baseline_gate(merged, actual_duration, concurrency, config, rate_limiter)
    if baseline_code and exit_code != 2:
        exit_code = baseline_code

    # A crashed worker means load/data was silently dropped; surface a
    # non-zero exit code without clobbering a threshold failure (2).
    if worker_crashed:
        exit_code = max(exit_code, 1)

    # Observability exports: run after printing so errors don't suppress output.
    # A misconfigured or unreachable endpoint produces exit code 1 (unless a
    # threshold failure already set a higher code).
    if not run_observability_exports(merged, actual_duration, concurrency, config, rate_limiter):
        exit_code = max(exit_code, 1)

    if on_complete is not None:
        on_complete(merged, actual_duration, concurrency)

    return merged, exit_code


# ---------------------------------------------------------------------------
# Main benchmark runner
# ---------------------------------------------------------------------------


async def run_benchmark(
    config: BenchmarkConfig,
    *,
    on_tick: "Callable[[LiveStats], None] | None" = None,
    install_signal_handlers: bool = True,
    on_complete: "Callable[[WorkerStats, float, int], None] | None" = None,
) -> tuple[WorkerStats, int]:
    """Run a fixed-concurrency benchmark and return merged stats with exit code.

    Creates N worker tasks distributed across thread groups, each sharing a
    connection pool via aiohttp.TCPConnector. Supports duration-based and
    request-count modes, with optional rate limiting and live dashboard.

    Args:
        config: Full benchmark configuration including URL, concurrency,
            duration/request count, and output options.

    Returns:
        Tuple of (merged_stats, exit_code) where exit_code is 0 for success
        or 2 if any SLO threshold was breached.

    Raises:
        No exceptions are raised to the caller. Network errors, timeouts,
        and HTTP errors are captured in WorkerStats.

    Concurrency notes:
        Workers are distributed across thread groups. Each group shares a
        TCPConnector with a connection limit equal to the number of workers
        in that group. Signal handlers (SIGINT/SIGTERM) set a stop_event
        that all workers check between requests.
    """
    # Sub-runs (distributed workers, autofind steps) and library callers set
    # _quiet: they own the reporting, so this one stays silent.
    quiet = getattr(config, "_quiet", False)

    mode_str = (
        f"{config.num_requests} requests" if config.num_requests else f"{config.duration}s duration"
    )
    logger.info("Running benchmark: %s", config.url)
    logger.info(
        "  %d worker groups, %d connections, %s", config.threads, config.connections, mode_str
    )
    logger.info(
        "  Method: %s, Timeout: %ss, Keep-Alive: %s",
        config.method,
        config.timeout_sec,
        "yes" if config.keepalive else "no",
    )
    if config.rate is not None:
        rate_str = f"{config.rate:,.0f} req/s"
        if config.rate_ramp is not None:
            rate_str += f" -> {config.rate_ramp:,.0f} req/s (ramp)"
        logger.info("  Rate Limit: %s", rate_str)
    if config.random_param:
        logger.info("  Cache-Buster: random _cb= parameter per request")
    if config.basic_auth:
        logger.info("  Auth: Basic (user=%s)", config.basic_auth.split(":")[0])
    if config.cookies:
        logger.info("  Cookies: %d", len(config.cookies))
    logger.info("")

    stop_event = asyncio.Event()
    if install_signal_handlers:
        # A library caller must not have the process's SIGINT/SIGTERM handlers
        # replaced out from under it.
        _setup_signal_handlers(stop_event)

    rate_limiter = _create_rate_limiter(config, config.duration)

    # Distribute connections across worker groups. Do NOT floor the per-group
    # size to 1: with the old `max(1, ...)` floor, when connections < threads
    # every group still got at least 1 worker PLUS the remainder was added,
    # over-provisioning workers (e.g. -c 1 -t 4 spawned 5 workers). The empty
    # groups are skipped below, so this yields exactly `connections` workers.
    conns_per_group = config.connections // config.threads
    remainder = config.connections % config.threads

    # Shared counter for request-count mode
    request_counter: RequestCounter | None = None
    if config.num_requests is not None:
        request_counter = RequestCounter(config.num_requests)

    all_stats: list[WorkerStats] = []
    tasks = []
    start_time = time.monotonic()
    # One runtime for the whole run so row cursors and counters are shared.
    data_runtime = DataRuntime.for_feeders(config.scenario.data if config.scenario else None)

    backend = create_backend(config, config.connections)

    # Log actual worker distribution across groups
    group_sizes = []
    for i in range(config.threads):
        n_conns = conns_per_group + (1 if i < remainder else 0)
        if n_conns == 0:
            continue
        group_sizes.append(n_conns)

        for j in range(n_conns):
            ws = WorkerStats()
            all_stats.append(ws)
            if config.scenario:
                _active = ActiveUsers()
                tasks.append(
                    asyncio.create_task(
                        scenario_worker(
                            j,
                            config,
                            ws,
                            backend,
                            stop_event,
                            start_time,
                            _active,
                            request_counter,
                            rate_limiter,
                            data_runtime,
                        )
                    )
                )
            else:
                tasks.append(
                    asyncio.create_task(
                        worker(config, ws, backend, stop_event, request_counter, rate_limiter)
                    )
                )

    logger.debug(
        "Worker distribution: %d groups, sizes=%s, total=%d workers",
        len(group_sizes),
        group_sizes,
        sum(group_sizes),
    )

    progress_task = _create_progress_task(
        config,
        all_stats,
        start_time,
        stop_event,
        duration=config.duration,
        num_requests=config.num_requests,
        quiet=quiet,
        on_tick=on_tick,
    )

    streaming = _create_streaming_exporter(config, all_stats, start_time, rate_limiter=rate_limiter)
    if streaming is not None:
        await streaming.start()

    return await _finalize_run(
        tasks,
        stop_event,
        progress_task,
        backend,
        all_stats,
        start_time,
        config,
        rate_limiter,
        config.connections,
        quiet=quiet,
        on_complete=on_complete,
        streaming=streaming,
    )


# ---------------------------------------------------------------------------
# User simulation runner
# ---------------------------------------------------------------------------


async def run_user_simulation(
    config: BenchmarkConfig,
    *,
    on_tick: "Callable[[LiveStats], None] | None" = None,
    install_signal_handlers: bool = True,
    on_complete: "Callable[[WorkerStats, float, int], None] | None" = None,
) -> tuple[WorkerStats, int]:
    """Run a virtual-user load test with ramp-up and think time.

    Creates one task per virtual user, optionally staggering their start
    times over a ramp-up period. Each user sends requests with configurable
    think time between them.

    Args:
        config: Benchmark configuration. Must have config.users set.
            config.duration defaults to 60s if not specified.

    Returns:
        Tuple of (merged_stats, exit_code) where exit_code is 0 for success
        or 2 if any SLO threshold was breached.

    Concurrency notes:
        All users share a single TCPConnector with
        limit=min(num_users, config.connections). Ramp-up is implemented by
        sleeping between task creation calls, so early users begin sending
        requests while later users are still being launched.
    """
    num_users = config.users
    duration = config.duration or 60.0
    quiet = getattr(config, "_quiet", False)

    if not quiet:
        logger.info("Running user simulation: %s", config.url)
        logger.info("  %d virtual users, %s duration", num_users, format_duration(duration))
        logger.info(
            "  Ramp-up: %s, Think time: %s (jitter: %s)",
            format_duration(config.ramp_up),
            format_duration(config.think_time),
            f"{config.think_time_jitter:.0%}",
        )
        logger.info(
            "  Method: %s, Timeout: %ss, Keep-Alive: %s",
            config.method,
            config.timeout_sec,
            "yes" if config.keepalive else "no",
        )
        if config.rate is not None:
            rate_str = f"{config.rate:,.0f} req/s"
            if config.rate_ramp is not None:
                rate_str += f" -> {config.rate_ramp:,.0f} req/s (ramp)"
            logger.info("  Rate Limit: %s", rate_str)
        if config.random_param:
            logger.info("  Cache-Buster: random _cb= parameter per request")
        logger.info("  Sessions: %s", describe_session_mode(config))
        if config.scenario is not None and config.scenario.data:
            for name, feeder in config.scenario.data.items():
                logger.info(
                    "  Data: %s -- %d rows, strategy=%s", name, len(feeder.rows), feeder.strategy
                )
        logger.info("")

    stop_event = asyncio.Event()
    if install_signal_handlers:
        # A library caller must not have the process's SIGINT/SIGTERM handlers
        # replaced out from under it.
        _setup_signal_handlers(stop_event)

    rate_limiter = _create_rate_limiter(config, duration)

    # Users make sequential requests with think time, so they don't all need
    # a connection simultaneously.  Cap the pool at the configured connections
    # value (defaults to 10) or num_users, whichever is smaller.
    pool_limit = min(num_users, config.connections)
    backend = create_backend(config, pool_limit)
    logger.debug(
        "User simulation: %d users, pool_limit=%d",
        num_users,
        pool_limit,
    )

    all_stats: list[WorkerStats] = []
    tasks = []
    active_users = ActiveUsers()
    start_time = time.monotonic()
    # One runtime for the whole run: row cursors are shared so `unique` is unique
    # across users, and counter() is monotonic across the run rather than per user.
    data_runtime = DataRuntime.for_feeders(config.scenario.data if config.scenario else None)

    # Ramp-up: stagger user launches
    ramp_delay = config.ramp_up / num_users if config.ramp_up > 0 and num_users > 1 else 0

    progress_task = _create_progress_task(
        config,
        all_stats,
        start_time,
        stop_event,
        duration=duration,
        active_users=active_users,
        quiet=quiet,
        on_tick=on_tick,
    )

    streaming = _create_streaming_exporter(
        config, all_stats, start_time, active_users=active_users, rate_limiter=rate_limiter
    )
    if streaming is not None:
        await streaming.start()

    # Guard the window between backend creation and _finalize_run (which owns
    # backend close + task teardown on normal completion). If this coroutine is
    # cancelled during ramp-up -- a window that can span seconds to minutes --
    # the pool and already-spawned worker tasks would otherwise be orphaned
    # (leaked sockets, never-cancelled tasks).
    try:
        for i in range(num_users):
            if stop_event.is_set():
                break
            ws = WorkerStats()
            if streaming is not None:
                streaming.enable_window_capture(ws)
            all_stats.append(ws)
            if config.scenario:
                tasks.append(
                    asyncio.create_task(
                        scenario_worker(
                            i,
                            config,
                            ws,
                            backend,
                            stop_event,
                            start_time,
                            active_users,
                            None,
                            rate_limiter,
                            data_runtime,
                        )
                    )
                )
            else:
                tasks.append(
                    asyncio.create_task(
                        user_worker(
                            i,
                            config,
                            ws,
                            backend,
                            stop_event,
                            start_time,
                            active_users,
                            rate_limiter,
                        )
                    )
                )
            if ramp_delay > 0 and i < num_users - 1:
                await asyncio.sleep(ramp_delay)

        return await _finalize_run(
            tasks,
            stop_event,
            progress_task,
            backend,
            all_stats,
            start_time,
            config,
            rate_limiter,
            num_users,
            quiet=quiet,
            on_complete=on_complete,
            streaming=streaming,
        )
    except BaseException:
        # Cancellation (or any other failure) before/within _finalize_run:
        # stop and reap outstanding worker tasks, cancel the progress task,
        # and close the pool if _finalize_run did not already do so.
        stop_event.set()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if not progress_task.done():
            progress_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                _ = await progress_task
        # aclose() is idempotent on both backends, so no "already closed" check.
        await backend.aclose()
        raise


# ---------------------------------------------------------------------------
# Autofind (auto-ramping / step load)
# ---------------------------------------------------------------------------


def _step_passed(step: StepResult, config: AutofindConfig) -> bool:
    """Check whether a step result meets the autofind thresholds."""
    if step.error_rate > config.max_error_rate:
        return False
    if step.p95 > config.max_p95:
        return False
    return True


def _extract_step_result(
    stats: WorkerStats, duration: float, num_users: int, config: AutofindConfig
) -> StepResult:
    """Extract a StepResult from merged WorkerStats."""
    rps = stats.total_requests / duration if duration > 0 else 0.0
    error_rate = (stats.errors / stats.total_requests * 100) if stats.total_requests > 0 else 0.0

    pct_map = dict(compute_percentiles(stats.latencies)) if stats.latencies else {}
    p50 = pct_map.get(50, 0.0)
    p95 = pct_map.get(95, 0.0)
    p99 = pct_map.get(99, 0.0)

    result = StepResult(
        users=num_users,
        rps=rps,
        p50=p50,
        p95=p95,
        p99=p99,
        error_rate=error_rate,
        total_requests=stats.total_requests,
        total_errors=stats.errors,
        passed=True,  # will be set below
    )
    result.passed = _step_passed(result, config)
    return result


async def run_autofind(config: AutofindConfig) -> list[StepResult]:
    """Auto-ramp load to find maximum sustainable capacity.

    Starts with start_users, doubles (or multiplies by step_multiplier) each
    step. When a step fails thresholds, binary-searches between the last good
    and first bad user count to refine the answer.

    Raises:
        ValueError: If ``step_multiplier`` is not greater than 1.0, or
            ``start_users`` is less than 1. Validating here (not only in the
            CLI) protects programmatic callers: with step_multiplier <= 1 the
            phase-1 ramp shrinks rather than grows and oscillates 0<->1
            forever, never terminating.
    """
    if config.step_multiplier <= 1.0:
        raise ValueError(
            f"step_multiplier ({config.step_multiplier}) must be > 1.0; "
            "values <= 1.0 cause the autofind ramp to never terminate"
        )
    if config.start_users < 1:
        raise ValueError(f"start_users ({config.start_users}) must be >= 1")

    logger.info("Autofind: ramping load on %s", config.url)
    logger.info(
        "  Thresholds: max error rate=%s%%, max p95=%ss", config.max_error_rate, config.max_p95
    )
    logger.info(
        "  Step duration: %ss, start users: %s, max users: %s",
        config.step_duration,
        config.start_users,
        config.max_users,
    )
    logger.info("  Step multiplier: %sx", config.step_multiplier)
    logger.info("")

    steps: list[StepResult] = []
    last_good: int | None = None
    first_bad: int | None = None
    current_users = config.start_users

    async def _run_step(num_users: int) -> StepResult:
        bench_config = BenchmarkConfig(
            url=config.url,
            users=num_users,
            duration=config.step_duration,
            think_time=config.think_time,
            think_time_jitter=config.think_time_jitter,
            timeout_sec=config.timeout_sec,
            keepalive=config.keepalive,
            random_param=config.random_param,
            ramp_up=0.0,
            ssl_config=config.ssl_config,
            otel_endpoint=config.otel_endpoint,
            prom_remote_write=config.prom_remote_write,
            export_interval=config.export_interval,
            # step_users makes the ramp legible in a live dashboard: each step's
            # series is separable instead of one smeared line.
            tags={**config.tags, "step_users": str(num_users)},
            _quiet=True,
        )
        stats, _ = await run_user_simulation(bench_config)
        return _extract_step_result(stats, config.step_duration, num_users, config)

    # Phase 1: Exponential ramp-up
    while current_users <= config.max_users:
        logger.info("  Step: testing %d users ...", current_users)
        result = await _run_step(current_users)
        steps.append(result)
        status = "OK" if result.passed else "FAIL"
        logger.info(
            "  %s rps, p95=%s, err=%s%% -> %s",
            f"{result.rps:.1f}",
            _format_latency_short(result.p95),
            f"{result.error_rate:.1f}",
            status,
        )

        if result.passed:
            last_good = current_users
            next_users = int(current_users * config.step_multiplier)
            if next_users == current_users:
                next_users = current_users + 1
            current_users = next_users
        else:
            first_bad = current_users
            break
    else:
        # Reached max_users without failure
        print_autofind_summary(steps, last_good)
        if config.json_output:
            _write_autofind_json(config, steps, last_good)
        return steps

    # Phase 2: Binary search refinement between last_good and first_bad
    if last_good is not None and first_bad is not None and first_bad - last_good > 1:
        lo, hi = last_good, first_bad
        while hi - lo > max(1, lo // 10):  # refine until gap is <10% of lo
            mid = (lo + hi) // 2
            if mid == lo or mid == hi:
                break
            logger.info("  Refine: testing %d users ...", mid)
            result = await _run_step(mid)
            steps.append(result)
            status = "OK" if result.passed else "FAIL"
            logger.info(
                "  %s rps, p95=%s, err=%s%% -> %s",
                f"{result.rps:.1f}",
                _format_latency_short(result.p95),
                f"{result.error_rate:.1f}",
                status,
            )

            if result.passed:
                lo = mid
                last_good = mid
            else:
                hi = mid

    print_autofind_summary(steps, last_good)
    if config.json_output:
        _write_autofind_json(config, steps, last_good)
    return steps


def _write_autofind_json(
    config: AutofindConfig, steps: list[StepResult], max_users: int | None
) -> None:
    """Write autofind results to a JSON file."""
    data = {
        "url": config.url,
        "max_error_rate": config.max_error_rate,
        "max_p95": config.max_p95,
        "step_duration": config.step_duration,
        "max_sustainable_users": max_users,
        "steps": [
            {
                "users": s.users,
                "rps": round(s.rps, 2),
                "p50": round(s.p50, 4),
                "p95": round(s.p95, 4),
                "p99": round(s.p99, 4),
                "error_rate": round(s.error_rate, 2),
                "total_requests": s.total_requests,
                "total_errors": s.total_errors,
                "passed": s.passed,
            }
            for s in steps
        ],
    }
    if config.json_output is None:
        return
    with open(config.json_output, "w") as f:
        json.dump(data, f, indent=2)
    logger.info("  JSON results written to %s", config.json_output)
