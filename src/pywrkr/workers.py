"""Worker functions and benchmark runners for pywrkr."""

import asyncio
import base64
import json
import logging
import math
import random
import signal
import ssl
import statistics
import sys
import time
import uuid
from urllib.parse import urlparse

import aiohttp

from pywrkr import reporting as _reporting
from pywrkr.config import (
    AutofindConfig,
    BenchmarkConfig,
    LatencyBreakdown,
    StepResult,
    WorkerStats,
)
from pywrkr.reporting import (
    _format_latency_short,
    compute_percentiles,
    evaluate_thresholds,
    format_bytes,
    format_duration,
    print_autofind_summary,
    print_results,
    print_threshold_results,
)
from pywrkr.traffic_profiles import RateLimiter

logger = logging.getLogger(__name__)


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
        active_users: dict | None = None,
    ) -> None:
        """Initialize the dashboard with stats, config, and timing state."""
        self.all_stats = all_stats
        self.config = config
        self.start_time = start_time
        self.active_users = active_users

    def _build_display(self) -> "Panel":  # noqa: F821
        """Build the rich Panel for the current dashboard state."""
        from rich.panel import Panel
        from rich.table import Table

        elapsed = time.monotonic() - self.start_time
        total_req = sum(ws.total_requests for ws in self.all_stats)
        total_err = sum(ws.errors for ws in self.all_stats)
        rps = total_req / elapsed if elapsed > 0 else 0.0
        total_bytes = sum(ws.total_bytes for ws in self.all_stats)
        transfer_rate = total_bytes / elapsed if elapsed > 0 else 0.0
        error_rate = (total_err / total_req * 100) if total_req > 0 else 0.0

        all_latencies = []
        for ws in self.all_stats:
            all_latencies.extend(ws.latencies)

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

        duration = self.config.duration
        if duration and duration > 0:
            pct = min(elapsed / duration * 100, 100.0)
            bar_filled = int(pct / 100 * 20)
            bar_empty = 20 - bar_filled
            filled = "\u2588" * bar_filled
            empty = "\u2591" * bar_empty
            progress_str = (
                f"Elapsed: {elapsed:.1f}s / {duration:.1f}s  [{filled}{empty}] {pct:.1f}%"
            )
        elif self.config.num_requests:
            total_n = self.config.num_requests
            pct = min(total_req / total_n * 100, 100.0) if total_n > 0 else 100.0
            bar_filled = int(pct / 100 * 20)
            bar_empty = 20 - bar_filled
            filled = "\u2588" * bar_filled
            empty = "\u2591" * bar_empty
            progress_str = f"Progress: {total_req}/{total_n}  [{filled}{empty}] {pct:.1f}%"
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
            table.add_row("Active Users:", f"{self.active_users['count']}")

        table.add_row("Requests:", f"{total_req:,}")
        table.add_row("Errors:", f"{total_err:,} ({error_rate:.1f}%)")
        table.add_row("RPS:", f"{rps:,.1f}")
        table.add_row("Transfer:", f"{format_bytes(transfer_rate)}/s")
        table.add_row("", "")

        if all_latencies:
            pairs = compute_percentiles(all_latencies)
            pair_dict = dict(pairs)
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
            bar_len = min(max_bar, max(1, int(rps / max(rps, 1) * max_bar)))
            bar = "\u2588" * bar_len + "\u2591" * (max_bar - bar_len)
            table.add_row("Throughput:", f"{bar} {rps:.0f} req/s")

        return Panel(table, title="pywrkr Live Dashboard", border_style="green")

    async def run(self, stop_event: asyncio.Event) -> None:
        """Update the dashboard every 0.5s until stop_event is set."""
        from rich.live import Live

        with Live(self._build_display(), refresh_per_second=2) as live:
            while not stop_event.is_set():
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=0.5)
                    break
                except asyncio.TimeoutError:
                    pass
                live.update(self._build_display())


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------


def make_url(url: str, random_param: bool) -> str:
    """Return the URL, optionally appending a unique cache-busting query parameter."""
    if not random_param:
        return url
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}_cb={uuid.uuid4().hex}"


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


def _merge_all_stats(all_stats: list[WorkerStats]) -> WorkerStats:
    """Merge a list of WorkerStats into a single aggregated WorkerStats.

    Combines all counters, latencies, timelines, and breakdowns from
    multiple workers into one unified stats object.
    """
    merged = WorkerStats()
    for ws in all_stats:
        merged.total_requests += ws.total_requests
        merged.total_bytes += ws.total_bytes
        merged.errors += ws.errors
        merged.content_length_errors += ws.content_length_errors
        merged.latencies.extend(ws.latencies)
        merged.rps_timeline.extend(ws.rps_timeline)
        for k, v in ws.error_types.items():
            merged.error_types[k] += v
        for k, v in ws.status_codes.items():
            merged.status_codes[k] += v
        for k, v in ws.step_latencies.items():
            merged.step_latencies[k].extend(v)
        merged.breakdowns.extend(ws.breakdowns)
    return merged


def _create_ssl_context(config: BenchmarkConfig) -> "ssl.SSLContext | None":
    """Create an SSL context based on the benchmark configuration.

    Returns None for plain HTTP. For HTTPS, creates a context with
    certificate verification controlled by config.ssl_config.
    """
    from urllib.parse import urlparse

    parsed = urlparse(config.url)
    if parsed.scheme != "https":
        return None

    ssl_ctx = ssl.create_default_context()
    if not config.ssl_config.verify:
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE
    elif config.ssl_config.ca_bundle:
        ssl_ctx.load_verify_locations(config.ssl_config.ca_bundle)
    return ssl_ctx


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
        ctx.get("request_start", end)

        # DNS time
        dns = 0.0
        if ctx.get("dns_start") is not None and ctx.get("dns_end") is not None:
            dns = ctx["dns_end"] - ctx["dns_start"]

        # TCP connect time (includes TLS if HTTPS)
        connect_total = 0.0
        if ctx.get("conn_start") is not None and ctx.get("conn_end") is not None:
            connect_total = ctx["conn_end"] - ctx["conn_start"]

        # TLS is approximated as connect_total - pure TCP time
        # Pure TCP is the portion before TLS starts; since aiohttp's
        # connection_create spans TCP+TLS, and DNS is separate,
        # we approximate: connect = TCP portion, tls = remainder
        # A simple heuristic: if DNS was resolved (new connection), then
        # tls = connect_total - dns_to_conn gap. For HTTP (no TLS), tls=0.
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


def aggregate_breakdowns(breakdowns: list[LatencyBreakdown]) -> dict:
    """Compute aggregate statistics for a list of LatencyBreakdown objects.

    Returns a dict with keys: dns, connect, tls, ttfb, transfer, total.
    Each has: avg, min, max, p50, p95, count.
    Also includes: new_connections, reused_connections.
    """
    if not breakdowns:
        return {}

    new_conns = sum(1 for b in breakdowns if not b.is_reused)
    reused_conns = sum(1 for b in breakdowns if b.is_reused)

    phases = {
        "dns": [b.dns for b in breakdowns],
        "connect": [b.connect for b in breakdowns],
        "tls": [b.tls for b in breakdowns],
        "ttfb": [b.ttfb for b in breakdowns],
        "transfer": [b.transfer for b in breakdowns],
        "total": [b.dns + b.connect + b.tls + b.ttfb + b.transfer for b in breakdowns],
    }

    result = {
        "new_connections": new_conns,
        "reused_connections": reused_conns,
    }

    for name, values in phases.items():
        if not values:
            continue
        sorted_vals = sorted(values)
        n = len(sorted_vals)
        p50_idx = min(int(math.ceil(50 / 100 * n)) - 1, n - 1)
        p95_idx = min(int(math.ceil(95 / 100 * n)) - 1, n - 1)
        result[name] = {
            "avg": statistics.mean(values),
            "min": min(values),
            "max": max(values),
            "p50": sorted_vals[p50_idx],
            "p95": sorted_vals[p95_idx],
            "count": n,
        }

    return result


# ---------------------------------------------------------------------------
# Workers
# ---------------------------------------------------------------------------


async def worker(
    config: BenchmarkConfig,
    stats: WorkerStats,
    connector: aiohttp.TCPConnector,
    stop_event: asyncio.Event,
    request_counter: dict | None = None,
    rate_limiter: RateLimiter | None = None,
) -> None:
    """Async worker coroutine that sends HTTP requests in a loop.

    Executes HTTP requests against the configured URL until the stop condition
    is met (duration elapsed, request count reached, or stop_event set).

    Args:
        config: Benchmark configuration including URL, method, headers, and timeouts.
        stats: WorkerStats instance where request results are recorded. Shared state
            is safe here because asyncio is single-threaded (no GIL contention).
        connector: aiohttp TCP connector with connection pooling.
        stop_event: Event that signals all workers to stop (set on SIGINT/SIGTERM
            or when the benchmark duration expires).
        request_counter: Optional shared dict with 'remaining' key for request-count
            mode (-n flag). Workers atomically decrement this counter.
        rate_limiter: Optional RateLimiter for throttling request rate.

    Raises:
        asyncio.CancelledError: Propagated if the task is cancelled externally.
            The worker breaks cleanly and records final RPS timeline data.

    Concurrency notes:
        Each worker runs as an independent asyncio task sharing a connector's
        connection pool. Stats mutation is safe because all tasks run on the
        same event loop thread.
    """
    start_time = time.monotonic()
    interval_start = start_time
    interval_count = 0

    req_headers = _build_request_headers(config)

    expected_length: int | None = None

    session_kwargs: dict = {"connector": connector}
    if config.latency_breakdown:
        trace_config = create_trace_config(stats)
        session_kwargs["trace_configs"] = [trace_config]

    async with aiohttp.ClientSession(**session_kwargs) as session:
        while not stop_event.is_set():
            # Check termination: duration mode or request-count mode
            if config.duration is not None:
                elapsed = time.monotonic() - start_time
                if elapsed >= config.duration:
                    break
                remaining = config.duration - elapsed
                effective_timeout = min(config.timeout_sec, remaining + 1)
            else:
                effective_timeout = config.timeout_sec

            # Request-count mode: atomically claim a request slot
            if request_counter is not None:
                if request_counter["remaining"] <= 0:
                    break
                request_counter["remaining"] -= 1

            # Rate limiting: wait for permission before sending
            if rate_limiter is not None:
                await rate_limiter.acquire()
                if stop_event.is_set():
                    break

            client_timeout = aiohttp.ClientTimeout(total=effective_timeout)
            request_url = make_url(config.url, config.random_param)
            req_start = time.monotonic()
            trace_ctx = {} if config.latency_breakdown else None
            try:
                async with session.request(
                    config.method,
                    request_url,
                    headers=req_headers,
                    data=config.body,
                    ssl=config.ssl_config.verify,
                    timeout=client_timeout,
                    trace_request_ctx=trace_ctx,
                ) as resp:
                    data = await resp.read()
                    latency = time.monotonic() - req_start
                    stats.total_requests += 1
                    stats.total_bytes += len(data)
                    stats.latencies.append(latency)
                    stats.status_codes[resp.status] += 1

                    # Content-length verification (ab -l style)
                    if config.verify_content_length:
                        cl = resp.headers.get("Content-Length")
                        if cl is not None:
                            declared = int(cl)
                            if expected_length is None:
                                expected_length = declared
                            if declared != expected_length or len(data) != declared:
                                stats.content_length_errors += 1

                    if resp.status >= 400:
                        stats.errors += 1
                        stats.error_types[f"HTTP {resp.status}"] += 1

                    if config.verbosity >= 4:
                        logger.debug(
                            f"[v4] {config.method} {request_url} -> {resp.status} "
                            f"({len(data)}B, {format_duration(latency)})"
                        )
                    elif config.verbosity >= 3:
                        logger.debug(f"[v3] {resp.status}")

            except asyncio.CancelledError:
                break
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
                latency = time.monotonic() - req_start
                stats.total_requests += 1
                stats.errors += 1
                error_name = type(e).__name__
                stats.error_types[error_name] += 1
                stats.latencies.append(latency)
                logger.warning("Request error: %s: %s", error_name, e)

            interval_count += 1
            now = time.monotonic()
            if now - interval_start >= 1.0:
                stats.rps_timeline.append((interval_start, interval_count))
                interval_start = now
                interval_count = 0

        if interval_count > 0:
            stats.rps_timeline.append((interval_start, interval_count))


async def user_worker(
    user_id: int,
    config: BenchmarkConfig,
    stats: WorkerStats,
    connector: aiohttp.TCPConnector,
    stop_event: asyncio.Event,
    start_time: float,
    active_users: dict,
    rate_limiter: RateLimiter | None = None,
) -> None:
    """Simulate a single virtual user with configurable think time.

    Models a real user session: send a request, wait (think time with jitter),
    repeat. The user is counted as active from start until the coroutine exits.

    Args:
        user_id: Numeric identifier for this virtual user (used for logging).
        config: Benchmark configuration including URL, method, think time settings.
        stats: WorkerStats instance for recording request results.
        connector: Shared aiohttp TCP connector with connection pooling.
        stop_event: Cancellation signal; checked between requests and during think time.
        start_time: Monotonic timestamp when the benchmark started, used to check
            duration limits.
        active_users: Shared dict with 'count' key tracking currently active users.
            Incremented on entry, decremented in finally block.
        rate_limiter: Optional rate limiter, applied only when think_time is 0
            (otherwise think time naturally throttles request rate).

    Raises:
        asyncio.CancelledError: Handled internally; breaks the request loop cleanly.

    Concurrency notes:
        Think time uses asyncio.wait_for on the stop_event, allowing immediate
        exit when the benchmark ends rather than sleeping through the full delay.
    """
    req_headers = _build_request_headers(config)

    expected_length: int | None = None
    active_users["count"] += 1

    session_kwargs: dict = {"connector": connector}
    if config.latency_breakdown:
        trace_config = create_trace_config(stats)
        session_kwargs["trace_configs"] = [trace_config]

    try:
        async with aiohttp.ClientSession(**session_kwargs) as session:
            while not stop_event.is_set():
                elapsed = time.monotonic() - start_time
                if config.duration is not None and elapsed >= config.duration:
                    break

                remaining = (config.duration - elapsed) if config.duration else config.timeout_sec
                effective_timeout = min(config.timeout_sec, remaining + 1)

                # Rate limiting for user workers: applies when think_time is 0
                if rate_limiter is not None and config.think_time == 0:
                    await rate_limiter.acquire()
                    if stop_event.is_set():
                        break

                client_timeout = aiohttp.ClientTimeout(total=effective_timeout)
                request_url = make_url(config.url, config.random_param)

                req_start = time.monotonic()
                trace_ctx = {} if config.latency_breakdown else None
                try:
                    async with session.request(
                        config.method,
                        request_url,
                        headers=req_headers,
                        data=config.body,
                        ssl=config.ssl_config.verify,
                        timeout=client_timeout,
                        trace_request_ctx=trace_ctx,
                    ) as resp:
                        data = await resp.read()
                        latency = time.monotonic() - req_start
                        stats.total_requests += 1
                        stats.total_bytes += len(data)
                        stats.latencies.append(latency)
                        stats.status_codes[resp.status] += 1

                        if config.verify_content_length:
                            cl = resp.headers.get("Content-Length")
                            if cl is not None:
                                declared = int(cl)
                                if expected_length is None:
                                    expected_length = declared
                                if declared != expected_length or len(data) != declared:
                                    stats.content_length_errors += 1

                        if resp.status >= 400:
                            stats.errors += 1
                            stats.error_types[f"HTTP {resp.status}"] += 1

                except asyncio.CancelledError:
                    break
                except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
                    latency = time.monotonic() - req_start
                    stats.total_requests += 1
                    stats.errors += 1
                    error_name = type(e).__name__
                    stats.error_types[error_name] += 1
                    stats.latencies.append(latency)
                    logger.warning("User %d request error: %s: %s", user_id, error_name, e)

                # Record for timeline
                now = time.monotonic()
                stats.rps_timeline.append((now, 1))

                # Think time: simulate user pause between requests
                if config.think_time > 0 and not stop_event.is_set():
                    jitter = config.think_time_jitter
                    lo = config.think_time * (1 - jitter)
                    hi = config.think_time * (1 + jitter)
                    delay = random.uniform(lo, hi)
                    try:
                        await asyncio.wait_for(stop_event.wait(), timeout=delay)
                        break  # stop_event was set during think time
                    except asyncio.TimeoutError:
                        pass  # think time elapsed, continue
    finally:
        active_users["count"] -= 1


async def scenario_worker(
    user_id: int,
    config: BenchmarkConfig,
    stats: WorkerStats,
    connector: aiohttp.TCPConnector,
    stop_event: asyncio.Event,
    start_time: float,
    active_users: dict,
    request_counter: dict | None = None,
) -> None:
    """Execute a scripted multi-step scenario in a loop.

    Iterates through scenario steps sequentially, executing each HTTP request
    with step-specific headers, body, and assertions. After completing all steps,
    loops back to the first step. Continues until stop condition is met.

    Args:
        user_id: Numeric identifier for this scenario user.
        config: Benchmark configuration; must have config.scenario set.
        stats: WorkerStats for recording per-step latencies and overall results.
        connector: Shared aiohttp TCP connector.
        stop_event: Cancellation signal checked between steps.
        start_time: Monotonic timestamp of benchmark start for duration checks.
        active_users: Shared dict tracking active user count.
        request_counter: Optional shared counter for request-count mode.

    Raises:
        asyncio.CancelledError: Handled internally; exits the scenario loop cleanly.

    Concurrency notes:
        Per-step latencies are tracked in stats.step_latencies[step_name] for
        detailed per-step reporting. Think time between steps respects both
        step-level and scenario-level think_time settings.
    """
    scenario = config.scenario
    if not scenario:
        return

    base_headers = _build_request_headers(config)

    parsed = urlparse(config.url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"

    active_users["count"] += 1
    try:
        async with aiohttp.ClientSession(connector=connector) as session:
            while not stop_event.is_set():
                for step in scenario.steps:
                    if stop_event.is_set():
                        break

                    if config.duration is not None:
                        elapsed = time.monotonic() - start_time
                        if elapsed >= config.duration:
                            return

                    if request_counter is not None:
                        if request_counter["remaining"] <= 0:
                            return
                        request_counter["remaining"] -= 1

                    remaining = (
                        (config.duration - (time.monotonic() - start_time))
                        if config.duration
                        else config.timeout_sec
                    )
                    effective_timeout = min(config.timeout_sec, remaining + 1)
                    client_timeout = aiohttp.ClientTimeout(total=effective_timeout)

                    request_url = make_url(f"{base_url}{step.path}", config.random_param)

                    req_headers = dict(base_headers)
                    req_headers.update(step.headers)

                    body = None
                    if step.body is not None:
                        if isinstance(step.body, dict):
                            body = json.dumps(step.body).encode()
                            if "Content-Type" not in req_headers:
                                req_headers["Content-Type"] = "application/json"
                        elif isinstance(step.body, str):
                            body = step.body.encode()
                        else:
                            body = step.body

                    step_name = step.name or f"{step.method} {step.path}"
                    req_start = time.monotonic()
                    try:
                        async with session.request(
                            step.method,
                            request_url,
                            headers=req_headers,
                            data=body,
                            ssl=config.ssl_config.verify,
                            timeout=client_timeout,
                        ) as resp:
                            data = await resp.read()
                            latency = time.monotonic() - req_start
                            stats.total_requests += 1
                            stats.total_bytes += len(data)
                            stats.latencies.append(latency)
                            stats.step_latencies[step_name].append(latency)
                            stats.status_codes[resp.status] += 1

                            assertion_failed = False
                            if step.assert_status is not None and resp.status != step.assert_status:
                                stats.errors += 1
                                err_msg = (
                                    f"AssertStatus: expected {step.assert_status},"
                                    f" got {resp.status}"
                                )
                                stats.error_types[err_msg] += 1
                                assertion_failed = True

                            if step.assert_body_contains is not None:
                                body_text = data.decode("utf-8", errors="replace")
                                if step.assert_body_contains not in body_text:
                                    stats.errors += 1
                                    err_msg = f"AssertBody: '{step.assert_body_contains}' not found"
                                    stats.error_types[err_msg] += 1
                                    assertion_failed = True

                            if not assertion_failed and resp.status >= 400:
                                stats.errors += 1
                                stats.error_types[f"HTTP {resp.status}"] += 1

                    except asyncio.CancelledError:
                        return
                    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
                        latency = time.monotonic() - req_start
                        stats.total_requests += 1
                        stats.errors += 1
                        error_name = type(e).__name__
                        stats.error_types[error_name] += 1
                        stats.latencies.append(latency)
                        stats.step_latencies[step_name].append(latency)
                        logger.warning(
                            "Scenario user %d step '%s' error: %s: %s",
                            user_id,
                            step_name,
                            error_name,
                            e,
                        )

                    now = time.monotonic()
                    stats.rps_timeline.append((now, 1))

                    think = step.think_time if step.think_time is not None else scenario.think_time
                    if think <= 0 and config.think_time > 0:
                        think = config.think_time
                    if think > 0 and not stop_event.is_set():
                        jitter = config.think_time_jitter
                        lo = think * (1 - jitter)
                        hi = think * (1 + jitter)
                        delay = random.uniform(lo, hi)
                        try:
                            await asyncio.wait_for(stop_event.wait(), timeout=delay)
                            return
                        except asyncio.TimeoutError:
                            pass
    finally:
        active_users["count"] -= 1


async def show_progress(
    start: float,
    duration: float | None,
    total_requests: int | None,
    all_stats: list[WorkerStats],
    stop: asyncio.Event,
    active_users: dict | None = None,
) -> None:
    """Display a text-based progress line during benchmark execution."""
    while not stop.is_set():
        await asyncio.sleep(1)
        elapsed = time.monotonic() - start
        total_req = sum(ws.total_requests for ws in all_stats)
        total_err = sum(ws.errors for ws in all_stats)
        rps = total_req / elapsed if elapsed > 0 else 0

        users_str = ""
        if active_users is not None:
            users_str = f" | {active_users['count']:>5} users"

        if duration is not None:
            pct = min(elapsed / duration * 100, 100)
            sys.stdout.write(
                f"\r  [{pct:5.1f}%] {total_req:>8} requests "
                f"| {rps:>8.1f} req/s | {total_err} errors{users_str} "
            )
        elif total_requests is not None:
            pct = min(total_req / total_requests * 100, 100) if total_requests > 0 else 100
            sys.stdout.write(
                f"\r  [{pct:5.1f}%] {total_req:>8}/{total_requests} requests "
                f"| {rps:>8.1f} req/s | {total_err} errors{users_str} "
            )
        sys.stdout.flush()
    sys.stdout.write("\r" + " " * 80 + "\r")
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# Main benchmark runner
# ---------------------------------------------------------------------------


async def run_benchmark(config: BenchmarkConfig) -> tuple[WorkerStats, int]:
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

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    # Create rate limiter if requested (shared across all workers)
    rate_limiter: RateLimiter | None = None
    if config.rate is not None:
        ramp_duration = config.duration if config.rate_ramp is not None else None
        rate_limiter = RateLimiter(
            rate=config.rate,
            end_rate=config.rate_ramp,
            ramp_duration=ramp_duration,
            traffic_profile=config.traffic_profile,
            duration=config.duration,
        )

    # Distribute connections across worker groups
    conns_per_group = max(1, config.connections // config.threads)
    remainder = config.connections % config.threads

    # Shared counter for request-count mode
    request_counter = None
    if config.num_requests is not None:
        request_counter = {"remaining": config.num_requests}

    all_stats: list[WorkerStats] = []
    tasks = []
    start_time = time.monotonic()
    ssl_ctx = _create_ssl_context(config)
    connectors: list[aiohttp.TCPConnector] = []

    for i in range(config.threads):
        n_conns = conns_per_group + (1 if i < remainder else 0)
        if n_conns == 0:
            continue

        connector = aiohttp.TCPConnector(
            limit=n_conns,
            ssl=ssl_ctx,
            force_close=not config.keepalive,
            enable_cleanup_closed=True,
        )
        connectors.append(connector)

        for j in range(n_conns):
            ws = WorkerStats()
            all_stats.append(ws)
            if config.scenario:
                _active = {"count": 0}
                tasks.append(
                    asyncio.create_task(
                        scenario_worker(
                            j,
                            config,
                            ws,
                            connector,
                            stop_event,
                            start_time,
                            _active,
                            request_counter,
                        )
                    )
                )
            else:
                tasks.append(
                    asyncio.create_task(
                        worker(config, ws, connector, stop_event, request_counter, rate_limiter)
                    )
                )

    if config.live_dashboard and _reporting.RICH_AVAILABLE:
        dashboard = LiveDashboard(all_stats, config, start_time)
        progress_task = asyncio.create_task(dashboard.run(stop_event))
    else:
        if config.live_dashboard and not _reporting.RICH_AVAILABLE:
            logger.warning("--live requires 'rich' package. Install with: pip install pywrkr[tui]")
            logger.warning("Falling back to standard progress display.")
        progress_task = asyncio.create_task(
            show_progress(start_time, config.duration, config.num_requests, all_stats, stop_event)
        )

    await asyncio.gather(*tasks, return_exceptions=True)
    stop_event.set()
    await progress_task

    # Clean up connectors
    for conn in connectors:
        await conn.close()

    end_time = time.monotonic()
    actual_duration = end_time - start_time

    merged = _merge_all_stats(all_stats)

    print_results(merged, actual_duration, config.connections, start_time, config, rate_limiter)

    # Evaluate SLO thresholds
    exit_code = 0
    if config.thresholds:
        th_results = evaluate_thresholds(config.thresholds, merged, actual_duration)
        print_threshold_results(th_results, file=sys.stdout)
        if any(not passed for _, _, passed in th_results):
            exit_code = 2

    return merged, exit_code


# ---------------------------------------------------------------------------
# User simulation runner
# ---------------------------------------------------------------------------


async def run_user_simulation(config: BenchmarkConfig) -> tuple[WorkerStats, int]:
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
        All users share a single TCPConnector with limit=num_users. Ramp-up
        is implemented by sleeping between task creation calls, so early
        users begin sending requests while later users are still being launched.
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
        logger.info("")

    stop_event = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    # Create rate limiter if requested (shared across all user workers)
    rate_limiter: RateLimiter | None = None
    if config.rate is not None:
        ramp_duration = duration if config.rate_ramp is not None else None
        rate_limiter = RateLimiter(
            rate=config.rate,
            end_rate=config.rate_ramp,
            ramp_duration=ramp_duration,
            traffic_profile=config.traffic_profile,
            duration=duration,
        )

    ssl_ctx = _create_ssl_context(config)

    connector = aiohttp.TCPConnector(
        limit=num_users,
        ssl=ssl_ctx,
        force_close=not config.keepalive,
        enable_cleanup_closed=True,
    )

    all_stats: list[WorkerStats] = []
    tasks = []
    active_users: dict = {"count": 0}
    start_time = time.monotonic()

    # Ramp-up: stagger user launches
    ramp_delay = config.ramp_up / num_users if config.ramp_up > 0 and num_users > 1 else 0

    if quiet:
        # In quiet mode, create a minimal stop-waiter instead of progress display
        async def _wait_stop(stop):
            await stop.wait()

        progress_task = asyncio.create_task(_wait_stop(stop_event))
    elif config.live_dashboard and _reporting.RICH_AVAILABLE:
        dashboard = LiveDashboard(all_stats, config, start_time, active_users)
        progress_task = asyncio.create_task(dashboard.run(stop_event))
    else:
        if config.live_dashboard and not _reporting.RICH_AVAILABLE:
            logger.warning("--live requires 'rich' package. Install with: pip install pywrkr[tui]")
            logger.warning("Falling back to standard progress display.")
        progress_task = asyncio.create_task(
            show_progress(start_time, duration, None, all_stats, stop_event, active_users)
        )

    for i in range(num_users):
        if stop_event.is_set():
            break
        ws = WorkerStats()
        all_stats.append(ws)
        if config.scenario:
            tasks.append(
                asyncio.create_task(
                    scenario_worker(i, config, ws, connector, stop_event, start_time, active_users)
                )
            )
        else:
            tasks.append(
                asyncio.create_task(
                    user_worker(
                        i, config, ws, connector, stop_event, start_time, active_users, rate_limiter
                    )
                )
            )
        if ramp_delay > 0 and i < num_users - 1:
            await asyncio.sleep(ramp_delay)

    await asyncio.gather(*tasks, return_exceptions=True)
    stop_event.set()
    await progress_task
    await connector.close()

    end_time = time.monotonic()
    actual_duration = end_time - start_time

    merged = _merge_all_stats(all_stats)

    if not quiet:
        print_results(merged, actual_duration, num_users, start_time, config, rate_limiter)

    # Evaluate SLO thresholds
    exit_code = 0
    if config.thresholds:
        th_results = evaluate_thresholds(config.thresholds, merged, actual_duration)
        if not quiet:
            print_threshold_results(th_results, file=sys.stdout)
        if any(not passed for _, _, passed in th_results):
            exit_code = 2

    return merged, exit_code


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

    if stats.latencies:
        sorted_lat = sorted(stats.latencies)
        n = len(sorted_lat)
        p50 = sorted_lat[min(int(math.ceil(50 / 100 * n)) - 1, n - 1)]
        p95 = sorted_lat[min(int(math.ceil(95 / 100 * n)) - 1, n - 1)]
        p99 = sorted_lat[min(int(math.ceil(99 / 100 * n)) - 1, n - 1)]
    else:
        p50 = p95 = p99 = 0.0

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
    """
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
    with open(config.json_output, "w") as f:
        json.dump(data, f, indent=2)
    logger.info("  JSON results written to %s", config.json_output)
