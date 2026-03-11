#!/usr/bin/env python3
"""
pywrkr - A Python HTTP benchmarking tool inspired by wrk and Apache ab,
with extended statistics.

Usage:
    python pywrkr.py -c 100 -d 10 -t 4 http://localhost:8080/
    python pywrkr.py -n 1000 -c 50 http://localhost:8080/
"""

import argparse
import asyncio
import base64
import csv
import io
import json
import math
import os
import random
import re
import signal
import ssl
import statistics
import sys
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from urllib.parse import urlparse


try:
    import aiohttp
except ImportError:
    print("Error: aiohttp is required. Install it with: pip install aiohttp")
    sys.exit(1)

try:
    from rich.live import Live
    from rich.table import Table
    from rich.panel import Panel
    from rich.text import Text
    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False

# Optional OpenTelemetry imports
try:
    from opentelemetry import metrics as otel_metrics
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.sdk.resources import Resource
    OTEL_AVAILABLE = True
except ImportError:
    OTEL_AVAILABLE = False


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class RequestResult:
    status: int
    latency: float  # seconds
    bytes_read: int
    error: str | None = None


@dataclass
class LatencyBreakdown:
    """Per-request latency breakdown into phases."""
    dns: float = 0.0       # DNS lookup time (seconds)
    connect: float = 0.0   # TCP connect time (seconds)
    tls: float = 0.0       # TLS handshake time (seconds)
    ttfb: float = 0.0      # Time to first byte (seconds)
    transfer: float = 0.0  # Response body transfer time (seconds)
    is_reused: bool = False # True if the connection was reused (DNS/connect/TLS will be 0)


@dataclass
class WorkerStats:
    results: list[RequestResult] = field(default_factory=list)
    total_requests: int = 0
    total_bytes: int = 0
    errors: int = 0
    error_types: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    status_codes: dict[int, int] = field(default_factory=lambda: defaultdict(int))
    latencies: list[float] = field(default_factory=list)
    rps_timeline: list[tuple[float, int]] = field(default_factory=list)
    content_length_errors: int = 0
    step_latencies: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    breakdowns: list[LatencyBreakdown] = field(default_factory=list)


@dataclass
class BenchmarkConfig:
    url: str
    connections: int = 10
    duration: float | None = 10.0
    num_requests: int | None = None  # ab-style -n mode
    threads: int = 4
    method: str = "GET"
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes | None = None
    timeout_sec: float = 30.0
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
    think_time_jitter: float = 0.5  # jitter factor (0-1): actual = think * uniform(1-jitter, 1+jitter)
    random_param: bool = False  # append random _cb=<uuid> query param per request (cache-buster)
    live_dashboard: bool = False  # show live TUI dashboard (requires rich)
    # Rate limiting mode
    rate: float | None = None  # target requests per second (None = unlimited)
    rate_ramp: float | None = None  # ramp rate target: linearly increase from rate to rate_ramp over duration
    # Scenario mode
    scenario: "Scenario | None" = None
    # Latency breakdown mode
    latency_breakdown: bool = False
    # Autofind mode: suppress output when used as a sub-step
    _quiet: bool = False
    # Observability export
    tags: dict[str, str] = field(default_factory=dict)
    otel_endpoint: str | None = None
    prom_remote_write: str | None = None
    # SLO thresholds
    thresholds: "list[Threshold]" = field(default_factory=list)


@dataclass
class Threshold:
    """An SLO threshold expression (e.g. 'p95 < 300ms')."""
    metric: str       # e.g. "p95"
    operator: str     # e.g. "<"
    value: float      # in seconds for latency, percent for error_rate, raw for rps
    raw_expr: str     # original string for display


@dataclass
class AutofindConfig:
    """Configuration for auto-ramping / step load mode."""
    url: str
    max_error_rate: float = 1.0  # percent
    max_p95: float = 5.0  # seconds
    step_duration: float = 30.0
    start_users: int = 10
    max_users: int = 10000
    step_multiplier: float = 2.0
    think_time: float = 1.0
    think_time_jitter: float = 0.5
    random_param: bool = False
    timeout_sec: float = 30.0
    keepalive: bool = True
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
            raise ImportError("pyyaml is required for YAML scenario files. Install with: pip install pyyaml")
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
                raise ValueError(f"Could not parse scenario file: {path}. "
                                 f"Not valid JSON, and pyyaml is not installed for YAML parsing.")

    if not isinstance(data, dict):
        raise ValueError(f"Scenario file must contain a JSON/YAML object, got {type(data).__name__}")

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
        steps.append(ScenarioStep(
            path=step_data["path"],
            method=step_data.get("method", "GET"),
            body=step_data.get("body"),
            headers=step_data.get("headers", {}),
            assert_status=step_data.get("assert_status"),
            assert_body_contains=step_data.get("assert_body_contains"),
            think_time=step_data.get("think_time"),
            name=step_data.get("name", f"Step {i + 1}: {step_data.get('method', 'GET')} {step_data['path']}"),
        ))

    return Scenario(
        name=data.get("name", "Unnamed Scenario"),
        think_time=data.get("think_time", 0.0),
        steps=steps,
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
        active_users: dict | None = None,
    ):
        self.all_stats = all_stats
        self.config = config
        self.start_time = start_time
        self.active_users = active_users

    def _build_display(self) -> "Panel":
        """Build the rich Panel for the current dashboard state."""
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
            progress_str = (
                f"Elapsed: {elapsed:.1f}s / {duration:.1f}s  "
                f"[{'\u2588' * bar_filled}{'\u2591' * bar_empty}] {pct:.1f}%"
            )
        elif self.config.num_requests:
            total_n = self.config.num_requests
            pct = min(total_req / total_n * 100, 100.0) if total_n > 0 else 100.0
            bar_filled = int(pct / 100 * 20)
            bar_empty = 20 - bar_filled
            progress_str = (
                f"Progress: {total_req}/{total_n}  "
                f"[{'\u2588' * bar_filled}{'\u2591' * bar_empty}] {pct:.1f}%"
            )
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
            bar = '\u2588' * bar_len + '\u2591' * (max_bar - bar_len)
            table.add_row("Throughput:", f"{bar} {rps:.0f} req/s")

        return Panel(table, title="pywrkr Live Dashboard", border_style="green")

    async def run(self, stop_event: asyncio.Event):
        """Update the dashboard every 0.5s until stop_event is set."""
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


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

class RateLimiter:
    """Token-bucket-style rate limiter that distributes requests evenly over time.

    Supports a fixed rate or a linear ramp from *start_rate* to *end_rate*
    over *ramp_duration* seconds.
    """

    def __init__(
        self,
        rate: float,
        end_rate: float | None = None,
        ramp_duration: float | None = None,
    ):
        self.start_rate = rate
        self.end_rate = end_rate
        self.ramp_duration = ramp_duration
        self._lock = asyncio.Lock()
        self._start_time: float | None = None
        self._last_time: float = 0.0
        self.waits: int = 0  # how many times we actually slept

    def _current_rate(self, now: float) -> float:
        """Return the target rate at time *now*."""
        if self.end_rate is not None and self.ramp_duration and self.ramp_duration > 0:
            if self._start_time is None:
                return self.start_rate
            elapsed = now - self._start_time
            progress = min(elapsed / self.ramp_duration, 1.0)
            return self.start_rate + (self.end_rate - self.start_rate) * progress
        return self.start_rate

    async def acquire(self):
        async with self._lock:
            now = time.monotonic()
            if self._start_time is None:
                self._start_time = now
                self._last_time = now
                return

            rate = self._current_rate(now)
            if rate <= 0:
                return
            interval = 1.0 / rate
            wait = self._last_time + interval - now
            if wait > 0:
                self.waits += 1
                await asyncio.sleep(wait)
            self._last_time = time.monotonic()


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
        start = ctx.get("request_start", end)

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
            # No chunks received (empty body) — use end time
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
# Formatting helpers
# ---------------------------------------------------------------------------

def format_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:.2f}{unit}"
        n /= 1024
    return f"{n:.2f}TB"


def format_duration(secs: float) -> str:
    if secs < 0.001:
        return f"{secs * 1_000_000:.2f}us"
    if secs < 1:
        return f"{secs * 1000:.2f}ms"
    return f"{secs:.2f}s"


# ---------------------------------------------------------------------------
# Report printers
# ---------------------------------------------------------------------------

def print_latency_histogram(latencies: list[float], buckets: int = 20, file=sys.stdout):
    if not latencies:
        return
    mn, mx = min(latencies), max(latencies)
    if mn == mx:
        print(f"  All requests: {format_duration(mn)}", file=file)
        return
    width = (mx - mn) / buckets
    counts = [0] * buckets
    for lat in latencies:
        idx = min(int((lat - mn) / width), buckets - 1)
        counts[idx] += 1
    max_count = max(counts)
    bar_max = 40
    print("  Latency Distribution (histogram):", file=file)
    for i, count in enumerate(counts):
        lo = mn + i * width
        hi = lo + width
        bar_len = int(count / max_count * bar_max) if max_count else 0
        bar = "#" * bar_len
        pct = count / len(latencies) * 100
        print(
            f"    {format_duration(lo):>10} - {format_duration(hi):>10} "
            f"| {bar:<{bar_max}} | {count:>6} ({pct:5.1f}%)",
            file=file,
        )


def compute_percentiles(latencies: list[float]) -> list[tuple[float, float]]:
    """Return list of (percentile, value) pairs."""
    if not latencies:
        return []
    sorted_lat = sorted(latencies)
    n = len(sorted_lat)
    percentiles = [50, 75, 90, 95, 99, 99.9, 99.99]
    result = []
    for p in percentiles:
        idx = min(int(math.ceil(p / 100 * n)) - 1, n - 1)
        result.append((p, sorted_lat[idx]))
    return result


# ---------------------------------------------------------------------------
# Threshold support (SLO pass/fail)
# ---------------------------------------------------------------------------

_THRESHOLD_PATTERN = re.compile(
    r"^\s*(p50|p75|p90|p95|p99|avg_latency|max_latency|min_latency|error_rate|rps)"
    r"\s*(<=?|>=?)\s*"
    r"([0-9]*\.?[0-9]+)\s*(ms|s|us|%)?\s*$"
)

_LATENCY_METRICS = {"p50", "p75", "p90", "p95", "p99",
                    "avg_latency", "max_latency", "min_latency"}

_PERCENTILE_MAP = {"p50": 50, "p75": 75, "p90": 90, "p95": 95, "p99": 99}


def parse_threshold(expr: str) -> "Threshold":
    """Parse a threshold expression like 'p95 < 300ms' into a Threshold."""
    m = _THRESHOLD_PATTERN.match(expr)
    if not m:
        raise ValueError(f"Invalid threshold expression: {expr!r}")
    metric, operator, raw_value, unit = m.groups()
    value = float(raw_value)

    # Convert time units to seconds for latency metrics
    if metric in _LATENCY_METRICS:
        if unit == "ms":
            value /= 1000.0
        elif unit == "us":
            value /= 1_000_000.0
        elif unit == "s" or unit is None:
            pass  # already seconds
        elif unit == "%":
            raise ValueError(
                f"Invalid unit '%' for latency metric {metric!r} in: {expr!r}")
    elif metric == "error_rate":
        # '%' is optional; value is always a percentage number
        if unit in ("ms", "s", "us"):
            raise ValueError(
                f"Invalid unit {unit!r} for error_rate in: {expr!r}")
    elif metric == "rps":
        if unit in ("ms", "s", "us", "%"):
            raise ValueError(
                f"Invalid unit {unit!r} for rps in: {expr!r}")

    return Threshold(metric=metric, operator=operator, value=value, raw_expr=expr.strip())


def evaluate_thresholds(
    thresholds: "list[Threshold]",
    stats: "WorkerStats",
    duration: float,
) -> "list[tuple[Threshold, float, bool]]":
    """Evaluate thresholds against benchmark results.

    Returns list of (threshold, actual_value, passed) tuples.
    """
    # Pre-compute percentiles from latencies
    pct_map: dict[float, float] = {}
    if stats.latencies:
        for p, v in compute_percentiles(stats.latencies):
            pct_map[p] = v

    results: list[tuple[Threshold, float, bool]] = []
    for th in thresholds:
        actual = _get_metric_value(th.metric, stats, duration, pct_map)
        passed = _compare(actual, th.operator, th.value)
        results.append((th, actual, passed))
    return results


def _get_metric_value(
    metric: str,
    stats: "WorkerStats",
    duration: float,
    pct_map: dict[float, float],
) -> float:
    """Extract the actual metric value from stats."""
    if metric in _PERCENTILE_MAP:
        pct_key = _PERCENTILE_MAP[metric]
        return pct_map.get(pct_key, 0.0)
    if metric == "avg_latency":
        return (sum(stats.latencies) / len(stats.latencies)) if stats.latencies else 0.0
    if metric == "max_latency":
        return max(stats.latencies) if stats.latencies else 0.0
    if metric == "min_latency":
        return min(stats.latencies) if stats.latencies else 0.0
    if metric == "error_rate":
        if stats.total_requests == 0:
            return 0.0
        return stats.errors / stats.total_requests * 100
    if metric == "rps":
        return stats.total_requests / duration if duration > 0 else 0.0
    return 0.0


def _compare(actual: float, operator: str, threshold: float) -> bool:
    if operator == "<":
        return actual < threshold
    if operator == ">":
        return actual > threshold
    if operator == "<=":
        return actual <= threshold
    if operator == ">=":
        return actual >= threshold
    return False


def print_threshold_results(
    results: "list[tuple[Threshold, float, bool]]",
    file=sys.stdout,
) -> None:
    """Print a summary table of threshold evaluation results."""
    if not results:
        return
    print(file=file)
    print("  SLO Threshold Results:", file=file)
    print(f"  {'Expression':<30} {'Actual':>12}   {'Status':>6}", file=file)
    print(f"  {'-' * 30} {'-' * 12}   {'-' * 6}", file=file)
    for th, actual, passed in results:
        status = "PASS" if passed else "FAIL"
        # Format actual value based on metric type
        if th.metric in _LATENCY_METRICS:
            actual_str = format_duration(actual)
        elif th.metric == "error_rate":
            actual_str = f"{actual:.2f}%"
        elif th.metric == "rps":
            actual_str = f"{actual:.2f}"
        else:
            actual_str = f"{actual:.4f}"
        print(f"  {th.raw_expr:<30} {actual_str:>12}   {status:>6}", file=file)

    all_passed = all(passed for _, _, passed in results)
    summary = "ALL PASSED" if all_passed else "SOME FAILED"
    print(f"\n  Thresholds: {summary}", file=file)


def print_percentiles(latencies: list[float], file=sys.stdout):
    pairs = compute_percentiles(latencies)
    if not pairs:
        return
    print("  Latency Percentiles:", file=file)
    for p, val in pairs:
        print(f"    p{p:<6} {format_duration(val):>12}", file=file)


def print_rps_timeline(timeline: list[tuple[float, int]], start: float, duration: float, file=sys.stdout):
    if not timeline:
        return
    bucket_size = max(1, int(duration / 20))
    buckets: dict[int, int] = defaultdict(int)
    for ts, count in timeline:
        bucket = int((ts - start) / bucket_size)
        buckets[bucket] += count
    if not buckets:
        return
    max_rps = max(buckets.values()) / bucket_size
    bar_max = 40
    print(f"  Requests/sec Timeline ({bucket_size}s buckets):", file=file)
    for i in range(max(buckets.keys()) + 1):
        rps = buckets.get(i, 0) / bucket_size
        bar_len = int(rps / max_rps * bar_max) if max_rps else 0
        bar = "#" * bar_len
        t_start = i * bucket_size
        print(f"    {t_start:>4}s | {bar:<{bar_max}} | {rps:>8.1f} req/s", file=file)


def build_results_dict(stats: WorkerStats, duration: float, connections: int, config: BenchmarkConfig | None = None, rate_limiter: RateLimiter | None = None) -> dict:
    """Build a structured results dict for JSON/HTML/programmatic use."""
    rps = stats.total_requests / duration if duration > 0 else 0
    transfer_rate = stats.total_bytes / duration if duration > 0 else 0
    result: dict = {
        "duration_sec": round(duration, 3),
        "connections": connections,
        "total_requests": stats.total_requests,
        "total_errors": stats.errors,
        "requests_per_sec": round(rps, 2),
        "transfer_per_sec_bytes": round(transfer_rate, 2),
        "total_bytes": stats.total_bytes,
        "content_length_errors": stats.content_length_errors,
        "status_codes": dict(stats.status_codes),
        "error_types": dict(stats.error_types),
    }
    if config is not None and config.tags:
        result["tags"] = dict(config.tags)
    if config is not None and config.rate is not None:
        result["target_rps"] = config.rate
        if config.rate_ramp is not None:
            result["ramp_target_rps"] = config.rate_ramp
        if rate_limiter is not None:
            result["rate_limit_waits"] = rate_limiter.waits
    if stats.latencies:
        result["latency"] = {
            "min": round(min(stats.latencies), 6),
            "max": round(max(stats.latencies), 6),
            "mean": round(statistics.mean(stats.latencies), 6),
            "median": round(statistics.median(stats.latencies), 6),
            "stdev": round(statistics.stdev(stats.latencies), 6) if len(stats.latencies) > 1 else 0,
        }
        result["percentiles"] = {
            f"p{p}": round(v, 6) for p, v in compute_percentiles(stats.latencies)
        }
    # Per-step latency stats for scenario mode
    if stats.step_latencies:
        step_stats = {}
        for step_name, lats in stats.step_latencies.items():
            if lats:
                step_stats[step_name] = {
                    "count": len(lats),
                    "min": round(min(lats), 6),
                    "max": round(max(lats), 6),
                    "mean": round(statistics.mean(lats), 6),
                    "median": round(statistics.median(lats), 6),
                }
                if len(lats) > 1:
                    step_stats[step_name]["stdev"] = round(statistics.stdev(lats), 6)
        result["step_stats"] = step_stats
    # Latency breakdown
    if stats.breakdowns:
        agg = aggregate_breakdowns(stats.breakdowns)
        bd_json: dict = {
            "new_connections": agg.get("new_connections", 0),
            "reused_connections": agg.get("reused_connections", 0),
        }
        for phase in ("dns", "connect", "tls", "ttfb", "transfer", "total"):
            if phase in agg:
                bd_json[phase] = {
                    k: round(v, 6) for k, v in agg[phase].items()
                }
        result["latency_breakdown"] = bd_json
    return result


def write_csv_output(path: str, stats: WorkerStats):
    """Write ab-style CSV with percentile served times."""
    if not stats.latencies:
        return
    sorted_lat = sorted(stats.latencies)
    n = len(sorted_lat)
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["Percentage", "Time (ms)"])
        for pct in range(1, 101):
            idx = min(int(math.ceil(pct / 100 * n)) - 1, n - 1)
            writer.writerow([pct, round(sorted_lat[idx] * 1000, 3)])


def write_json_output(path: str, results: dict):
    with open(path, "w") as f:
        json.dump(results, f, indent=2)


def generate_html_report(stats: WorkerStats, duration: float, connections: int) -> str:
    """Generate an ab-style HTML table report."""
    results = build_results_dict(stats, duration, connections)
    rows = []
    for key, val in results.items():
        if isinstance(val, dict):
            for k2, v2 in val.items():
                rows.append(f"<tr><td>{key}.{k2}</td><td>{v2}</td></tr>")
        else:
            rows.append(f"<tr><td>{key}</td><td>{val}</td></tr>")
    return (
        "<html><head><title>pywrkr benchmark results</title></head><body>\n"
        "<h1>pywrkr Benchmark Results</h1>\n"
        "<table border='1' cellpadding='4'>\n"
        "<tr><th>Metric</th><th>Value</th></tr>\n"
        + "\n".join(rows)
        + "\n</table></body></html>"
    )


def export_to_otel(results: dict, endpoint: str, tags: dict[str, str]) -> None:
    """Export benchmark metrics to an OpenTelemetry collector via OTLP/HTTP."""
    if not OTEL_AVAILABLE:
        print("Warning: opentelemetry packages not installed. "
              "Install with: pip install pywrkr[otel]")
        return

    try:
        resource_attrs = {"service.name": "pywrkr"}
        resource_attrs.update(tags)
        resource = Resource.create(resource_attrs)
        exporter = OTLPMetricExporter(endpoint=endpoint)
        reader = PeriodicExportingMetricReader(exporter, export_interval_millis=1000)
        provider = MeterProvider(resource=resource, metric_readers=[reader])
        meter = provider.get_meter("pywrkr")

        attributes = dict(tags)

        # Counters
        req_counter = meter.create_counter("pywrkr.requests.total", description="Total requests")
        req_counter.add(results.get("total_requests", 0), attributes=attributes)

        err_counter = meter.create_counter("pywrkr.errors.total", description="Total errors")
        err_counter.add(results.get("total_errors", 0), attributes=attributes)

        # Gauges via UpDownCounter (set once)
        def _gauge(name, value, desc=""):
            g = meter.create_up_down_counter(name, description=desc)
            g.add(value, attributes=attributes)

        _gauge("pywrkr.requests_per_sec", results.get("requests_per_sec", 0))
        _gauge("pywrkr.transfer_bytes_per_sec", results.get("transfer_per_sec_bytes", 0))
        _gauge("pywrkr.duration_sec", results.get("duration_sec", 0))

        percentiles = results.get("percentiles", {})
        latency = results.get("latency", {})
        _gauge("pywrkr.latency.p50", percentiles.get("p50", 0) * 1000)
        _gauge("pywrkr.latency.p95", percentiles.get("p95", 0) * 1000)
        _gauge("pywrkr.latency.p99", percentiles.get("p99", 0) * 1000)
        _gauge("pywrkr.latency.mean", latency.get("mean", 0) * 1000)
        _gauge("pywrkr.latency.max", latency.get("max", 0) * 1000)

        # Force flush and shutdown
        provider.force_flush()
        provider.shutdown()
    except Exception as e:
        print(f"Warning: failed to export metrics to OTel endpoint {endpoint}: {e}")


def export_to_prometheus(results: dict, endpoint: str, tags: dict[str, str]) -> None:
    """Export benchmark metrics to a Prometheus Pushgateway-compatible endpoint."""
    import urllib.request
    import urllib.error

    try:
        # Build Prometheus text format
        lines: list[str] = []
        labels_parts = [f'{k}="{v}"' for k, v in sorted(tags.items())]
        labels_str = "{" + ",".join(labels_parts) + "}" if labels_parts else ""

        def _add(name: str, value: float, mtype: str = "gauge", help_text: str = ""):
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} {mtype}")
            lines.append(f"{name}{labels_str} {value}")

        _add("pywrkr_requests_total", results.get("total_requests", 0),
             "counter", "Total requests")
        _add("pywrkr_errors_total", results.get("total_errors", 0),
             "counter", "Total errors")
        _add("pywrkr_requests_per_sec", results.get("requests_per_sec", 0),
             "gauge", "Requests per second")
        _add("pywrkr_transfer_bytes_per_sec", results.get("transfer_per_sec_bytes", 0),
             "gauge", "Transfer bytes per second")
        _add("pywrkr_duration_sec", results.get("duration_sec", 0),
             "gauge", "Benchmark duration in seconds")

        percentiles = results.get("percentiles", {})
        latency = results.get("latency", {})
        _add("pywrkr_latency_p50_ms", percentiles.get("p50", 0) * 1000,
             "gauge", "p50 latency in ms")
        _add("pywrkr_latency_p95_ms", percentiles.get("p95", 0) * 1000,
             "gauge", "p95 latency in ms")
        _add("pywrkr_latency_p99_ms", percentiles.get("p99", 0) * 1000,
             "gauge", "p99 latency in ms")
        _add("pywrkr_latency_mean_ms", latency.get("mean", 0) * 1000,
             "gauge", "Mean latency in ms")
        _add("pywrkr_latency_max_ms", latency.get("max", 0) * 1000,
             "gauge", "Max latency in ms")

        body = "\n".join(lines) + "\n"
        url = endpoint.rstrip("/") + "/metrics/job/pywrkr"
        req = urllib.request.Request(
            url,
            data=body.encode("utf-8"),
            method="POST",
            headers={"Content-Type": "text/plain; version=0.0.4"},
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"Warning: failed to export metrics to Prometheus endpoint {endpoint}: {e}")


def print_results(stats: WorkerStats, duration: float, connections: int, start_time: float, config: BenchmarkConfig, rate_limiter: RateLimiter | None = None):
    out = sys.stdout

    rps = stats.total_requests / duration if duration > 0 else 0
    transfer_rate = stats.total_bytes / duration if duration > 0 else 0

    print("=" * 70, file=out)
    print("  BENCHMARK RESULTS", file=out)
    print("=" * 70, file=out)

    if config.scenario:
        mode = f"scenario '{config.scenario.name}'"
        if config.users:
            mode += f", {config.users} virtual users, {config.duration}s"
        elif config.duration:
            mode += f", {config.duration}s duration"
    elif config.users:
        mode = f"{config.users} virtual users, {config.duration}s"
    elif config.num_requests:
        mode = f"{config.num_requests} requests"
    else:
        mode = f"{config.duration}s duration"
    print(f"\n  Mode:              {mode}", file=out)
    print(f"  Duration:          {format_duration(duration)}", file=out)
    if config.users:
        print(f"  Virtual Users:     {config.users}", file=out)
        print(f"  Ramp-up:           {format_duration(config.ramp_up)}", file=out)
        print(f"  Think Time:        {format_duration(config.think_time)} "
              f"(+/-{config.think_time_jitter:.0%})", file=out)
        if config.users > 0:
            print(f"  Avg Reqs/User:     {stats.total_requests / config.users:,.1f}", file=out)
    else:
        print(f"  Connections:       {connections}", file=out)
    print(f"  Keep-Alive:        {'yes' if config.keepalive else 'no'}", file=out)
    print(f"  Total Requests:    {stats.total_requests:,}", file=out)
    print(f"  Total Errors:      {stats.errors:,}", file=out)
    if stats.content_length_errors:
        print(f"  Content-Len Errs:  {stats.content_length_errors:,}", file=out)
    print(f"  Requests/sec:      {rps:,.2f}", file=out)
    if config.rate is not None:
        print(f"  Target RPS:        {config.rate:,.2f}", file=out)
        if config.rate_ramp is not None:
            print(f"  Ramp Target RPS:   {config.rate_ramp:,.2f}", file=out)
        if rate_limiter is not None:
            print(f"  Rate Limit Waits:  {rate_limiter.waits:,}", file=out)
    print(f"  Transfer/sec:      {format_bytes(transfer_rate)}/s", file=out)
    print(f"  Total Transfer:    {format_bytes(stats.total_bytes)}", file=out)

    # Latency stats
    if stats.latencies:
        print(f"\n{'=' * 70}", file=out)
        print("  LATENCY STATISTICS", file=out)
        print(f"{'=' * 70}", file=out)
        print(f"    Min:       {format_duration(min(stats.latencies)):>12}", file=out)
        print(f"    Max:       {format_duration(max(stats.latencies)):>12}", file=out)
        print(f"    Mean:      {format_duration(statistics.mean(stats.latencies)):>12}", file=out)
        print(f"    Median:    {format_duration(statistics.median(stats.latencies)):>12}", file=out)
        if len(stats.latencies) > 1:
            print(f"    Stdev:     {format_duration(statistics.stdev(stats.latencies)):>12}", file=out)

        print(file=out)
        print_percentiles(stats.latencies, file=out)

        # ab-style "percentage of requests served within" table
        if stats.latencies:
            sorted_lat = sorted(stats.latencies)
            n = len(sorted_lat)
            print(file=out)
            print("  Percentage of requests served within a certain time:", file=out)
            for pct in [50, 66, 75, 80, 90, 95, 98, 99, 100]:
                idx = min(int(math.ceil(pct / 100 * n)) - 1, n - 1)
                print(f"    {pct:>3}%    {format_duration(sorted_lat[idx]):>12}", file=out)

        print(file=out)
        print_latency_histogram(stats.latencies, file=out)

    # Latency breakdown
    if stats.breakdowns:
        agg = aggregate_breakdowns(stats.breakdowns)
        print(f"\n{'=' * 70}", file=out)
        print("  LATENCY BREAKDOWN (averages)", file=out)
        print(f"{'=' * 70}", file=out)
        for phase, label in [
            ("dns", "DNS Lookup"),
            ("connect", "TCP Connect"),
            ("tls", "TLS Handshake"),
            ("ttfb", "TTFB"),
            ("transfer", "Transfer"),
            ("total", "Total"),
        ]:
            if phase in agg:
                d = agg[phase]
                print(f"    {label + ':':18s} {format_duration(d['avg']):>12}"
                      f"  (min={format_duration(d['min'])},"
                      f" max={format_duration(d['max'])},"
                      f" p50={format_duration(d['p50'])},"
                      f" p95={format_duration(d['p95'])})", file=out)
        new_c = agg.get("new_connections", 0)
        reused_c = agg.get("reused_connections", 0)
        print(f"\n    New Connections:    {new_c:,}", file=out)
        print(f"    Reused Connections: {reused_c:,}", file=out)

    # Status codes
    if stats.status_codes:
        print(f"\n{'=' * 70}", file=out)
        print("  STATUS CODE DISTRIBUTION", file=out)
        print(f"{'=' * 70}", file=out)
        for code in sorted(stats.status_codes):
            count = stats.status_codes[code]
            pct = count / stats.total_requests * 100 if stats.total_requests else 0
            print(f"    {code}: {count:>10,} ({pct:5.1f}%)", file=out)

    # Errors
    if stats.error_types:
        print(f"\n{'=' * 70}", file=out)
        print("  ERROR DISTRIBUTION", file=out)
        print(f"{'=' * 70}", file=out)
        for err, count in sorted(stats.error_types.items(), key=lambda x: -x[1]):
            print(f"    {err}: {count:>10,}", file=out)

    # Per-step stats (scenario mode)
    if stats.step_latencies:
        print(f"\n{'=' * 70}", file=out)
        print("  PER-STEP LATENCY", file=out)
        print(f"{'=' * 70}", file=out)
        for step_name, lats in stats.step_latencies.items():
            if lats:
                mean_lat = statistics.mean(lats)
                print(f"    {step_name}:", file=out)
                print(f"      Count: {len(lats):,}  "
                      f"Mean: {format_duration(mean_lat)}  "
                      f"Min: {format_duration(min(lats))}  "
                      f"Max: {format_duration(max(lats))}", file=out)

    # RPS timeline
    if stats.rps_timeline:
        print(f"\n{'=' * 70}", file=out)
        print("  THROUGHPUT TIMELINE", file=out)
        print(f"{'=' * 70}", file=out)
        print_rps_timeline(stats.rps_timeline, start_time, duration, file=out)

    print(f"\n{'=' * 70}", file=out)

    # CSV output
    if config.csv_output:
        write_csv_output(config.csv_output, stats)
        print(f"\n  CSV percentile data written to: {config.csv_output}", file=out)

    # JSON output
    if config.json_output:
        results = build_results_dict(stats, duration, connections, config, rate_limiter)
        write_json_output(config.json_output, results)
        print(f"\n  JSON results written to: {config.json_output}", file=out)

    # HTML output
    if config.html_output:
        html = generate_html_report(stats, duration, connections)
        print(f"\n{html}", file=out)

    # Observability exports
    if config.otel_endpoint or config.prom_remote_write:
        results = build_results_dict(stats, duration, connections, config, rate_limiter)
        if config.otel_endpoint:
            export_to_otel(results, config.otel_endpoint, config.tags)
        if config.prom_remote_write:
            export_to_prometheus(results, config.prom_remote_write, config.tags)


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
):
    start_time = time.monotonic()
    interval_start = start_time
    interval_count = 0

    cookie_header = "; ".join(config.cookies) if config.cookies else None
    req_headers = dict(config.headers)
    if config.basic_auth:
        encoded = base64.b64encode(config.basic_auth.encode()).decode()
        req_headers["Authorization"] = f"Basic {encoded}"
    if cookie_header:
        req_headers["Cookie"] = cookie_header

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
                    ssl=False,
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
                        print(f"  [v4] {config.method} {request_url} -> {resp.status} "
                              f"({len(data)}B, {format_duration(latency)})")
                    elif config.verbosity >= 3:
                        print(f"  [v3] {resp.status}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                latency = time.monotonic() - req_start
                stats.total_requests += 1
                stats.errors += 1
                error_name = type(e).__name__
                stats.error_types[error_name] += 1
                stats.latencies.append(latency)
                if config.verbosity >= 2:
                    print(f"  [v2] WARNING: {error_name}: {e}")

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
):
    """Simulate a single virtual user with think time between requests."""
    cookie_header = "; ".join(config.cookies) if config.cookies else None
    req_headers = dict(config.headers)
    if config.basic_auth:
        encoded = base64.b64encode(config.basic_auth.encode()).decode()
        req_headers["Authorization"] = f"Basic {encoded}"
    if cookie_header:
        req_headers["Cookie"] = cookie_header

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
                        ssl=False,
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
                except Exception as e:
                    latency = time.monotonic() - req_start
                    stats.total_requests += 1
                    stats.errors += 1
                    error_name = type(e).__name__
                    stats.error_types[error_name] += 1
                    stats.latencies.append(latency)

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
):
    """Execute a scripted scenario: iterate through steps in order, then repeat."""
    scenario = config.scenario
    if not scenario:
        return

    cookie_header = "; ".join(config.cookies) if config.cookies else None
    base_headers = dict(config.headers)
    if config.basic_auth:
        encoded = base64.b64encode(config.basic_auth.encode()).decode()
        base_headers["Authorization"] = f"Basic {encoded}"
    if cookie_header:
        base_headers["Cookie"] = cookie_header

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

                    remaining = (config.duration - (time.monotonic() - start_time)) if config.duration else config.timeout_sec
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
                            ssl=False,
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
                                err_msg = f"AssertStatus: expected {step.assert_status}, got {resp.status}"
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
                    except Exception as e:
                        latency = time.monotonic() - req_start
                        stats.total_requests += 1
                        stats.errors += 1
                        error_name = type(e).__name__
                        stats.error_types[error_name] += 1
                        stats.latencies.append(latency)
                        stats.step_latencies[step_name].append(latency)

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
):
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

async def run_benchmark(config: BenchmarkConfig):
    parsed = urlparse(config.url)
    use_ssl = parsed.scheme == "https"

    mode_str = (
        f"{config.num_requests} requests"
        if config.num_requests
        else f"{config.duration}s duration"
    )
    print(f"Running benchmark: {config.url}")
    print(f"  {config.threads} worker groups, {config.connections} connections, {mode_str}")
    print(f"  Method: {config.method}, Timeout: {config.timeout_sec}s, "
          f"Keep-Alive: {'yes' if config.keepalive else 'no'}")
    if config.rate is not None:
        rate_str = f"{config.rate:,.0f} req/s"
        if config.rate_ramp is not None:
            rate_str += f" -> {config.rate_ramp:,.0f} req/s (ramp)"
        print(f"  Rate Limit: {rate_str}")
    if config.random_param:
        print(f"  Cache-Buster: random _cb= parameter per request")
    if config.basic_auth:
        print(f"  Auth: Basic (user={config.basic_auth.split(':')[0]})")
    if config.cookies:
        print(f"  Cookies: {len(config.cookies)}")
    print()

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

    for i in range(config.threads):
        n_conns = conns_per_group + (1 if i < remainder else 0)
        if n_conns == 0:
            continue

        ssl_ctx = ssl.create_default_context() if use_ssl else None
        if ssl_ctx:
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE

        connector = aiohttp.TCPConnector(
            limit=n_conns,
            ssl=ssl_ctx,
            force_close=not config.keepalive,
            enable_cleanup_closed=True,
        )

        for j in range(n_conns):
            ws = WorkerStats()
            all_stats.append(ws)
            if config.scenario:
                _active = {"count": 0}
                tasks.append(
                    asyncio.create_task(
                        scenario_worker(j, config, ws, connector, stop_event,
                                        start_time, _active, request_counter)
                    )
                )
            else:
                tasks.append(
                    asyncio.create_task(
                        worker(config, ws, connector, stop_event, request_counter, rate_limiter)
                    )
                )

    if config.live_dashboard and RICH_AVAILABLE:
        dashboard = LiveDashboard(all_stats, config, start_time)
        progress_task = asyncio.create_task(dashboard.run(stop_event))
    else:
        if config.live_dashboard and not RICH_AVAILABLE:
            print("Warning: --live requires 'rich' package. "
                  "Install with: pip install pywrkr[tui]")
            print("Falling back to standard progress display.")
        progress_task = asyncio.create_task(
            show_progress(start_time, config.duration, config.num_requests, all_stats, stop_event)
        )

    await asyncio.gather(*tasks, return_exceptions=True)
    stop_event.set()
    await progress_task

    end_time = time.monotonic()
    actual_duration = end_time - start_time

    # Merge stats
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

async def run_user_simulation(config: BenchmarkConfig):
    """Run a virtual user load test with ramp-up and think time."""
    parsed = urlparse(config.url)
    use_ssl = parsed.scheme == "https"
    num_users = config.users
    duration = config.duration or 60.0
    quiet = getattr(config, '_quiet', False)

    if not quiet:
        print(f"Running user simulation: {config.url}")
        print(f"  {num_users} virtual users, {format_duration(duration)} duration")
        print(f"  Ramp-up: {format_duration(config.ramp_up)}, "
              f"Think time: {format_duration(config.think_time)} "
              f"(jitter: {config.think_time_jitter:.0%})")
        print(f"  Method: {config.method}, Timeout: {config.timeout_sec}s, "
              f"Keep-Alive: {'yes' if config.keepalive else 'no'}")
        if config.rate is not None:
            rate_str = f"{config.rate:,.0f} req/s"
            if config.rate_ramp is not None:
                rate_str += f" -> {config.rate_ramp:,.0f} req/s (ramp)"
            print(f"  Rate Limit: {rate_str}")
        if config.random_param:
            print(f"  Cache-Buster: random _cb= parameter per request")
        print()

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
        )

    ssl_ctx = ssl.create_default_context() if use_ssl else None
    if ssl_ctx:
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE

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
    elif config.live_dashboard and RICH_AVAILABLE:
        dashboard = LiveDashboard(all_stats, config, start_time, active_users)
        progress_task = asyncio.create_task(dashboard.run(stop_event))
    else:
        if config.live_dashboard and not RICH_AVAILABLE:
            print("Warning: --live requires 'rich' package. "
                  "Install with: pip install pywrkr[tui]")
            print("Falling back to standard progress display.")
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
                    user_worker(i, config, ws, connector, stop_event, start_time, active_users, rate_limiter)
                )
            )
        if ramp_delay > 0 and i < num_users - 1:
            await asyncio.sleep(ramp_delay)

    await asyncio.gather(*tasks, return_exceptions=True)
    stop_event.set()
    await progress_task

    end_time = time.monotonic()
    actual_duration = end_time - start_time

    # Merge stats
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

def _format_latency_short(secs: float) -> str:
    """Format latency for autofind summary table (compact)."""
    if secs < 1.0:
        return f"{secs * 1000:.0f}ms"
    return f"{secs:.1f}s"


def _step_passed(step: StepResult, config: AutofindConfig) -> bool:
    """Check whether a step result meets the autofind thresholds."""
    if step.error_rate > config.max_error_rate:
        return False
    if step.p95 > config.max_p95:
        return False
    return True


def _extract_step_result(stats: WorkerStats, duration: float, num_users: int,
                         config: AutofindConfig) -> StepResult:
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


def print_autofind_summary(steps: list[StepResult], max_users: int | None):
    """Print the autofind summary table."""
    print()
    print("=" * 60)
    print("  AUTOFIND RESULTS")
    print("=" * 60)
    if max_users is not None and max_users > 0:
        print(f"  Maximum sustainable load: {max_users} users")
    else:
        print("  Maximum sustainable load: could not be determined")
    print()
    print("  Step Results:")
    print(f"  {'Users':>5} | {'RPS':>8} | {'p50':>7} | {'p95':>7} | {'p99':>7} | {'Errors':>6} | Status")
    for s in steps:
        status = "OK" if s.passed else "FAIL"
        print(f"  {s.users:>5} | {s.rps:>8.1f} | {_format_latency_short(s.p50):>7} | "
              f"{_format_latency_short(s.p95):>7} | {_format_latency_short(s.p99):>7} | "
              f"{s.error_rate:>5.1f}% | {status}")
    print("=" * 60)


async def run_autofind(config: AutofindConfig):
    """Auto-ramp load to find maximum sustainable capacity.

    Starts with start_users, doubles (or multiplies by step_multiplier) each
    step. When a step fails thresholds, binary-searches between the last good
    and first bad user count to refine the answer.
    """
    print(f"Autofind: ramping load on {config.url}")
    print(f"  Thresholds: max error rate={config.max_error_rate}%, "
          f"max p95={config.max_p95}s")
    print(f"  Step duration: {config.step_duration}s, "
          f"start users: {config.start_users}, max users: {config.max_users}")
    print(f"  Step multiplier: {config.step_multiplier}x")
    print()

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
        print(f"  Step: testing {current_users} users ...", end=" ", flush=True)
        result = await _run_step(current_users)
        steps.append(result)
        status = "OK" if result.passed else "FAIL"
        print(f"{result.rps:.1f} rps, p95={_format_latency_short(result.p95)}, "
              f"err={result.error_rate:.1f}% -> {status}")

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
            print(f"  Refine: testing {mid} users ...", end=" ", flush=True)
            result = await _run_step(mid)
            steps.append(result)
            status = "OK" if result.passed else "FAIL"
            print(f"{result.rps:.1f} rps, p95={_format_latency_short(result.p95)}, "
                  f"err={result.error_rate:.1f}% -> {status}")

            if result.passed:
                lo = mid
                last_good = mid
            else:
                hi = mid

    print_autofind_summary(steps, last_good)
    if config.json_output:
        _write_autofind_json(config, steps, last_good)
    return steps


def _write_autofind_json(config: AutofindConfig, steps: list[StepResult],
                         max_users: int | None):
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
    print(f"\n  JSON results written to {config.json_output}")


# ---------------------------------------------------------------------------
# Multi-URL mode (--url-file)
# ---------------------------------------------------------------------------

@dataclass
class UrlEntry:
    """A single entry from a URL file."""
    url: str
    method: str = "GET"


def load_url_file(path: str) -> list[UrlEntry]:
    """Load URLs from a text file.

    Format (one per line):
        http://example.com/api/v1
        POST http://example.com/api/v1/data
        # comments and blank lines are ignored
    """
    if not os.path.isfile(path):
        raise FileNotFoundError(f"URL file not found: {path}")

    entries: list[UrlEntry] = []
    with open(path, "r") as f:
        for line_num, raw_line in enumerate(f, 1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(None, 1)
            if len(parts) == 2 and parts[0].upper() in (
                "GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS",
            ):
                entries.append(UrlEntry(url=parts[1], method=parts[0].upper()))
            elif len(parts) >= 1:
                entries.append(UrlEntry(url=parts[0]))
            else:
                raise ValueError(f"Invalid line {line_num} in URL file: {raw_line!r}")

    if not entries:
        raise ValueError(f"URL file is empty: {path}")
    return entries


@dataclass
class MultiUrlResult:
    """Result for a single URL in multi-URL mode."""
    url: str
    method: str
    stats: WorkerStats
    duration: float
    exit_code: int


def print_multi_url_summary(results: list[MultiUrlResult]):
    """Print a comparison table across all URLs."""
    out = sys.stdout
    print(f"\n{'=' * 90}", file=out)
    print("  MULTI-URL COMPARISON SUMMARY", file=out)
    print(f"{'=' * 90}", file=out)

    # Header
    print(f"\n  {'#':>3}  {'Method':<7} {'URL':<40} {'Reqs':>7} {'RPS':>9} "
          f"{'p50':>9} {'p95':>9} {'p99':>9} {'Errs':>6}", file=out)
    print(f"  {'─' * 3}  {'─' * 7} {'─' * 40} {'─' * 7} {'─' * 9} "
          f"{'─' * 9} {'─' * 9} {'─' * 9} {'─' * 6}", file=out)

    for i, r in enumerate(results, 1):
        rps = r.stats.total_requests / r.duration if r.duration > 0 else 0
        url_display = r.url if len(r.url) <= 40 else r.url[:37] + "..."

        # Compute percentiles
        p50 = p95 = p99 = 0.0
        if r.stats.latencies:
            sorted_lat = sorted(r.stats.latencies)
            n = len(sorted_lat)
            p50 = sorted_lat[min(int(math.ceil(50 / 100 * n)) - 1, n - 1)]
            p95 = sorted_lat[min(int(math.ceil(95 / 100 * n)) - 1, n - 1)]
            p99 = sorted_lat[min(int(math.ceil(99 / 100 * n)) - 1, n - 1)]

        err_pct = (r.stats.errors / r.stats.total_requests * 100) if r.stats.total_requests > 0 else 0

        print(f"  {i:>3}  {r.method:<7} {url_display:<40} "
              f"{r.stats.total_requests:>7,} {rps:>9,.1f} "
              f"{format_duration(p50):>9} {format_duration(p95):>9} {format_duration(p99):>9} "
              f"{err_pct:>5.1f}%", file=out)

    print(f"\n{'=' * 90}", file=out)

    # Totals
    total_reqs = sum(r.stats.total_requests for r in results)
    total_errs = sum(r.stats.errors for r in results)
    total_bytes = sum(r.stats.total_bytes for r in results)
    print(f"  Total: {len(results)} endpoints, {total_reqs:,} requests, "
          f"{total_errs:,} errors, {format_bytes(total_bytes)} transferred", file=out)
    print(f"{'=' * 90}\n", file=out)


def build_multi_url_json(results: list[MultiUrlResult]) -> dict:
    """Build a JSON-serializable dict for multi-URL results."""
    endpoints = []
    for r in results:
        entry = build_results_dict(r.stats, r.duration, 0)
        entry["url"] = r.url
        entry["method"] = r.method
        endpoints.append(entry)

    return {
        "mode": "multi_url",
        "endpoint_count": len(results),
        "total_requests": sum(r.stats.total_requests for r in results),
        "total_errors": sum(r.stats.errors for r in results),
        "endpoints": endpoints,
    }


async def run_multi_url(
    url_entries: list[UrlEntry],
    base_config: BenchmarkConfig,
) -> list[MultiUrlResult]:
    """Run benchmarks sequentially for each URL and collect results."""
    results: list[MultiUrlResult] = []

    for i, entry in enumerate(url_entries, 1):
        print(f"\n{'─' * 70}")
        print(f"  Endpoint {i}/{len(url_entries)}: {entry.method} {entry.url}")
        print(f"{'─' * 70}\n")

        # Clone config with this URL and method
        config = BenchmarkConfig(
            url=entry.url,
            connections=base_config.connections,
            duration=base_config.duration,
            num_requests=base_config.num_requests,
            threads=base_config.threads,
            method=entry.method,
            headers=dict(base_config.headers),
            body=base_config.body,
            timeout_sec=base_config.timeout_sec,
            keepalive=base_config.keepalive,
            basic_auth=base_config.basic_auth,
            cookies=list(base_config.cookies),
            verify_content_length=base_config.verify_content_length,
            verbosity=base_config.verbosity,
            random_param=base_config.random_param,
            rate=base_config.rate,
            rate_ramp=base_config.rate_ramp,
            latency_breakdown=base_config.latency_breakdown,
            users=base_config.users,
            ramp_up=base_config.ramp_up,
            think_time=base_config.think_time,
            think_time_jitter=base_config.think_time_jitter,
            thresholds=base_config.thresholds,
            tags=base_config.tags,
        )

        start = time.monotonic()
        if config.users is not None:
            stats, exit_code = await run_user_simulation(config)
        else:
            stats, exit_code = await run_benchmark(config)
        duration = time.monotonic() - start

        results.append(MultiUrlResult(
            url=entry.url,
            method=entry.method,
            stats=stats,
            duration=duration,
            exit_code=exit_code,
        ))

    # Print comparison summary
    print_multi_url_summary(results)

    # JSON output
    if base_config.json_output:
        data = build_multi_url_json(results)
        write_json_output(base_config.json_output, data)
        print(f"  JSON results written to: {base_config.json_output}")

    return results


# ---------------------------------------------------------------------------
# Distributed mode (master/worker)
# ---------------------------------------------------------------------------

def _serialize_config(config: BenchmarkConfig) -> dict:
    """Serialize a BenchmarkConfig to a JSON-safe dict for network transport."""
    return {
        "url": config.url,
        "connections": config.connections,
        "duration": config.duration,
        "num_requests": config.num_requests,
        "threads": config.threads,
        "method": config.method,
        "headers": dict(config.headers),
        "body": base64.b64encode(config.body).decode() if config.body else None,
        "timeout_sec": config.timeout_sec,
        "keepalive": config.keepalive,
        "basic_auth": config.basic_auth,
        "cookies": list(config.cookies),
        "verify_content_length": config.verify_content_length,
        "verbosity": config.verbosity,
        "random_param": config.random_param,
        "rate": config.rate,
        "rate_ramp": config.rate_ramp,
        "latency_breakdown": config.latency_breakdown,
        "users": config.users,
        "ramp_up": config.ramp_up,
        "think_time": config.think_time,
        "think_time_jitter": config.think_time_jitter,
    }


def _deserialize_config(data: dict) -> BenchmarkConfig:
    """Deserialize a dict back into a BenchmarkConfig."""
    body = base64.b64decode(data["body"]) if data.get("body") else None
    return BenchmarkConfig(
        url=data["url"],
        connections=data.get("connections", 10),
        duration=data.get("duration"),
        num_requests=data.get("num_requests"),
        threads=data.get("threads", 4),
        method=data.get("method", "GET"),
        headers=data.get("headers", {}),
        body=body,
        timeout_sec=data.get("timeout_sec", 30.0),
        keepalive=data.get("keepalive", True),
        basic_auth=data.get("basic_auth"),
        cookies=data.get("cookies", []),
        verify_content_length=data.get("verify_content_length", False),
        verbosity=data.get("verbosity", 0),
        random_param=data.get("random_param", False),
        rate=data.get("rate"),
        rate_ramp=data.get("rate_ramp"),
        latency_breakdown=data.get("latency_breakdown", False),
        users=data.get("users"),
        ramp_up=data.get("ramp_up", 0.0),
        think_time=data.get("think_time", 0.0),
        think_time_jitter=data.get("think_time_jitter", 0.5),
        _quiet=True,
    )


def _serialize_stats(stats: WorkerStats) -> dict:
    """Serialize WorkerStats to a JSON-safe dict."""
    return {
        "total_requests": stats.total_requests,
        "total_bytes": stats.total_bytes,
        "errors": stats.errors,
        "content_length_errors": stats.content_length_errors,
        "latencies": stats.latencies,
        "error_types": dict(stats.error_types),
        "status_codes": {str(k): v for k, v in stats.status_codes.items()},
        "rps_timeline": stats.rps_timeline,
    }


def _deserialize_stats(data: dict) -> WorkerStats:
    """Deserialize a dict back into WorkerStats."""
    ws = WorkerStats()
    ws.total_requests = data.get("total_requests", 0)
    ws.total_bytes = data.get("total_bytes", 0)
    ws.errors = data.get("errors", 0)
    ws.content_length_errors = data.get("content_length_errors", 0)
    ws.latencies = data.get("latencies", [])
    for k, v in data.get("error_types", {}).items():
        ws.error_types[k] = v
    for k, v in data.get("status_codes", {}).items():
        ws.status_codes[int(k)] = v
    ws.rps_timeline = [tuple(x) for x in data.get("rps_timeline", [])]
    return ws


async def _send_msg(writer: asyncio.StreamWriter, obj: dict) -> None:
    """Send a length-prefixed JSON message."""
    payload = json.dumps(obj).encode()
    writer.write(len(payload).to_bytes(4, "big") + payload)
    await writer.drain()


async def _recv_msg(reader: asyncio.StreamReader) -> dict:
    """Receive a length-prefixed JSON message."""
    length_bytes = await reader.readexactly(4)
    length = int.from_bytes(length_bytes, "big")
    payload = await reader.readexactly(length)
    return json.loads(payload.decode())


def merge_worker_stats(stats_list: list[WorkerStats]) -> WorkerStats:
    """Merge multiple WorkerStats into one."""
    merged = WorkerStats()
    for ws in stats_list:
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


async def run_master(config: BenchmarkConfig, host: str, port: int, expect_workers: int):
    """Run in master mode: wait for workers, distribute config, collect results."""
    print(f"Master: listening on {host}:{port}, waiting for {expect_workers} worker(s)...")

    worker_connections: list[tuple[asyncio.StreamReader, asyncio.StreamWriter]] = []
    ready_event = asyncio.Event()

    async def handle_worker(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        addr = writer.get_extra_info("peername")
        worker_connections.append((reader, writer))
        print(f"  Worker connected: {addr[0]}:{addr[1]} "
              f"({len(worker_connections)}/{expect_workers})")
        if len(worker_connections) >= expect_workers:
            ready_event.set()

    server = await asyncio.start_server(handle_worker, host, port)
    async with server:
        # Wait for all workers with a timeout
        try:
            await asyncio.wait_for(ready_event.wait(), timeout=300)
        except asyncio.TimeoutError:
            print(f"Master: timed out waiting for workers "
                  f"({len(worker_connections)}/{expect_workers} connected)")
            for _, w in worker_connections:
                w.close()
            return

        print(f"\nMaster: all {expect_workers} workers connected. Distributing config...")
        config_data = _serialize_config(config)
        for _, writer in worker_connections:
            await _send_msg(writer, {"type": "config", "config": config_data})

        print("Master: benchmark running on all workers...")

        # Collect results
        all_stats: list[WorkerStats] = []
        for i, (reader, writer) in enumerate(worker_connections):
            try:
                msg = await asyncio.wait_for(_recv_msg(reader), timeout=config.duration * 3 + 120 if config.duration else 600)
                if msg.get("type") == "result":
                    ws = _deserialize_stats(msg["stats"])
                    all_stats.append(ws)
                    addr = writer.get_extra_info("peername")
                    print(f"  Worker {addr[0]}:{addr[1]} finished: "
                          f"{ws.total_requests:,} requests, {ws.errors} errors")
                else:
                    print(f"  Worker {i}: unexpected message type: {msg.get('type')}")
            except Exception as e:
                print(f"  Worker {i}: error receiving results: {e}")
            finally:
                writer.close()

    if not all_stats:
        print("Master: no results received from workers.")
        return

    # Merge and report
    merged = merge_worker_stats(all_stats)
    total_duration = max(
        sum(ws.total_requests for ws in all_stats) / (merged.total_requests / max(merged.latencies) if merged.latencies else 1),
        config.duration or 10.0,
    ) if merged.latencies else (config.duration or 10.0)

    # Use actual duration from config for reporting
    actual_duration = config.duration or 10.0

    print(f"\nMaster: {len(all_stats)} worker(s) reported. Merged results:\n")
    # Override _quiet for printing
    report_config = BenchmarkConfig(
        url=config.url,
        connections=config.connections * len(all_stats),
        duration=config.duration,
        method=config.method,
        keepalive=config.keepalive,
        users=config.users,
        ramp_up=config.ramp_up,
        think_time=config.think_time,
        think_time_jitter=config.think_time_jitter,
        csv_output=config.csv_output,
        json_output=config.json_output,
        html_output=config.html_output,
        tags=config.tags,
        otel_endpoint=config.otel_endpoint,
        prom_remote_write=config.prom_remote_write,
        thresholds=config.thresholds,
        rate=config.rate,
        rate_ramp=config.rate_ramp,
    )
    print_results(merged, actual_duration, report_config.connections,
                  time.monotonic() - actual_duration, report_config)

    # Evaluate SLO thresholds
    exit_code = 0
    if config.thresholds:
        th_results = evaluate_thresholds(config.thresholds, merged, actual_duration)
        print_threshold_results(th_results, file=sys.stdout)
        if any(not passed for _, _, passed in th_results):
            exit_code = 2

    return merged, exit_code


async def run_worker_node(master_host: str, master_port: int):
    """Run in worker mode: connect to master, receive config, run benchmark, send results."""
    print(f"Worker: connecting to master at {master_host}:{master_port}...")

    reader, writer = await asyncio.open_connection(master_host, master_port)
    print("Worker: connected to master, waiting for config...")

    msg = await _recv_msg(reader)
    if msg.get("type") != "config":
        print(f"Worker: unexpected message type: {msg.get('type')}")
        writer.close()
        return

    config = _deserialize_config(msg["config"])
    print(f"Worker: received config. Target: {config.url}")
    print(f"Worker: starting benchmark...")

    # Run the appropriate benchmark
    if config.users is not None:
        stats, _ = await run_user_simulation(config)
    else:
        stats, _ = await run_benchmark(config)

    print(f"Worker: benchmark complete. {stats.total_requests:,} requests, "
          f"{stats.errors} errors")

    # Send results back to master
    await _send_msg(writer, {"type": "result", "stats": _serialize_stats(stats)})
    writer.close()
    print("Worker: results sent to master. Done.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_header(s: str) -> tuple[str, str]:
    if ":" not in s:
        raise argparse.ArgumentTypeError(f"Invalid header format: {s} (expected 'Name: Value')")
    name, value = s.split(":", 1)
    return name.strip(), value.strip()


def main():
    parser = argparse.ArgumentParser(
        description="pywrkr - HTTP benchmarking tool with extended statistics (wrk + ab features)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Duration mode (wrk-style):
  %(prog)s http://localhost:8080/
  %(prog)s -c 200 -d 30 http://localhost:8080/api

  # Request-count mode (ab-style):
  %(prog)s -n 1000 -c 50 http://localhost:8080/

  # POST with auth, cookies, and JSON output:
  %(prog)s -n 500 -c 20 -m POST -b '{"key":"val"}' \\
      -H "Content-Type: application/json" \\
      -A user:pass -C "session=abc123" \\
      --json results.json http://localhost:8080/api

  # User simulation: 1500 users, 5 min, 30s ramp-up, 1s think time:
  %(prog)s -u 1500 -d 300 --ramp-up 30 --think-time 1.0 http://localhost:8080/

  # Cache-busting: append random query param to bypass HTTP caches:
  %(prog)s -R -c 100 -d 10 http://localhost:8080/
  %(prog)s -R -u 300 -d 300 --think-time 1.0 https://example.com/
        """,
    )
    parser.add_argument("url", nargs="?", default=None, help="Target URL to benchmark")
    parser.add_argument("-c", "--connections", type=int, default=10,
                        help="Number of concurrent connections (default: 10)")
    parser.add_argument("-d", "--duration", type=float, default=None,
                        help="Duration of test in seconds (default: 10, ignored if -n is set)")
    parser.add_argument("-n", "--num-requests", type=int, default=None,
                        help="Total number of requests to make (ab-style, overrides -d)")
    parser.add_argument("-t", "--threads", type=int, default=4,
                        help="Number of worker groups (default: 4)")
    parser.add_argument("-m", "--method", default="GET",
                        help="HTTP method (default: GET)")
    parser.add_argument("-H", "--header", action="append", type=parse_header,
                        default=[], dest="headers",
                        help="HTTP header (e.g. -H 'Content-Type: application/json')")
    parser.add_argument("-b", "--body", default=None,
                        help="Request body string")
    parser.add_argument("-p", "--post-file", default=None,
                        help="File containing POST body data (ab-style)")
    parser.add_argument("-A", "--basic-auth", default=None, metavar="user:pass",
                        help="Basic HTTP authentication (ab-style)")
    parser.add_argument("-C", "--cookie", action="append", default=[], dest="cookies",
                        help="Cookie 'name=value' (repeatable, ab-style)")
    parser.add_argument("-k", "--keepalive", action="store_true", default=True,
                        help="Enable keep-alive (default: on)")
    parser.add_argument("--no-keepalive", action="store_true", default=False,
                        help="Disable keep-alive (close connection after each request)")
    parser.add_argument("-l", "--verify-length", action="store_true", default=False,
                        help="Verify response Content-Length consistency (ab-style)")
    parser.add_argument("-v", "--verbosity", type=int, default=0,
                        help="Verbosity level: 2=warnings, 3=status codes, 4=headers+body info")
    parser.add_argument("--timeout", type=float, default=30,
                        help="Request timeout in seconds (default: 30)")
    parser.add_argument("-e", "--csv", default=None, metavar="FILE",
                        help="Write CSV percentile table to FILE (ab-style)")
    parser.add_argument("-w", "--html", action="store_true", default=False,
                        help="Print results as HTML table (ab-style)")
    parser.add_argument("--json", default=None, metavar="FILE",
                        help="Write JSON results to FILE")
    # User simulation mode
    parser.add_argument("-u", "--users", type=int, default=None,
                        help="Number of virtual users (enables user simulation mode)")
    parser.add_argument("--ramp-up", type=float, default=0,
                        help="Ramp-up period in seconds to start all users (default: 0)")
    parser.add_argument("--think-time", type=float, default=1.0,
                        help="Mean think time in seconds between requests per user (default: 1.0)")
    parser.add_argument("--think-jitter", type=float, default=0.5,
                        help="Think time jitter factor 0-1 (default: 0.5, e.g. 1s +/-50%%)")
    parser.add_argument("-R", "--random-param", action="store_true", default=False,
                        help="Append a unique random query parameter (_cb=<uuid>) to each request "
                             "URL to bypass HTTP caching")
    # Rate limiting mode
    parser.add_argument("--rate", type=float, default=None,
                        help="Target requests per second (constant rate mode)")
    parser.add_argument("--rate-ramp", type=float, default=None,
                        help="Linearly ramp rate from --rate to this value over the duration")
    # Scenario mode
    parser.add_argument("--scenario", default=None, metavar="FILE",
                        help="Path to a JSON/YAML scenario file for scripted multi-step requests")
    parser.add_argument("--live", action="store_true", default=False,
                        help="Show a live TUI dashboard during the benchmark "
                             "(requires rich: pip install pywrkr[tui])")
    parser.add_argument("--latency-breakdown", action="store_true", default=False,
                        help="Show detailed latency breakdown per phase "
                             "(DNS, TCP connect, TLS, TTFB, transfer)")
    # Observability export
    parser.add_argument("--tag", action="append", default=[], dest="tags",
                        help="Metadata tag as key=value (repeatable, e.g. --tag environment=prod)")
    parser.add_argument("--otel-endpoint", default=None, metavar="URL",
                        help="Export metrics to an OpenTelemetry collector via OTLP/HTTP")
    parser.add_argument("--prom-remote-write", default=None, metavar="URL",
                        help="Push metrics to a Prometheus Pushgateway-compatible endpoint")
    # Autofind mode
    parser.add_argument("--autofind", action="store_true", default=False,
                        help="Auto-ramp load to find maximum sustainable capacity")
    parser.add_argument("--max-error-rate", type=float, default=1.0,
                        help="Autofind: stop when error rate exceeds this percent (default: 1.0)")
    parser.add_argument("--max-p95", type=float, default=5.0,
                        help="Autofind: stop when p95 latency exceeds this in seconds (default: 5.0)")
    parser.add_argument("--step-duration", type=float, default=30.0,
                        help="Autofind: duration of each step test in seconds (default: 30)")
    parser.add_argument("--start-users", type=int, default=10,
                        help="Autofind: starting number of users (default: 10)")
    parser.add_argument("--max-users", type=int, default=10000,
                        help="Autofind: maximum users to try (default: 10000)")
    parser.add_argument("--step-multiplier", type=float, default=2.0,
                        help="Autofind: multiply users by this each step (default: 2.0)")
    # SLO Thresholds
    parser.add_argument("--threshold", "--th", action="append", default=[], dest="thresholds",
                        help="SLO threshold expression (repeatable, e.g. --threshold 'p95 < 300ms'). "
                             "Exit code 2 if any threshold is breached.")
    # Multi-URL mode
    parser.add_argument("--url-file", default=None, metavar="FILE",
                        help="File with URLs to benchmark (one per line, optional METHOD prefix). "
                             "Runs each URL sequentially with the same settings and prints a comparison.")
    # Distributed mode
    parser.add_argument("--master", action="store_true", default=False,
                        help="Run as master node in distributed mode")
    parser.add_argument("--expect-workers", type=int, default=None, metavar="N",
                        help="Number of workers the master should wait for (required with --master)")
    parser.add_argument("--bind", default="0.0.0.0", metavar="HOST",
                        help="Master bind address (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=9220, metavar="PORT",
                        help="Master/worker port (default: 9220)")
    parser.add_argument("--worker", default=None, metavar="HOST:PORT",
                        help="Run as worker node, connecting to master at HOST:PORT")

    args = parser.parse_args()

    # --- Worker mode: connect to master, no URL needed ---
    if args.worker is not None:
        if ":" not in args.worker:
            parser.error("--worker requires HOST:PORT format (e.g. --worker 192.168.1.1:9220)")
        host, port_str = args.worker.rsplit(":", 1)
        try:
            w_port = int(port_str)
        except ValueError:
            parser.error(f"Invalid port in --worker: {port_str}")
        asyncio.run(run_worker_node(host, w_port))
        sys.exit(0)

    # --- Multi-URL mode: --url-file does not require positional url ---
    if args.url_file is not None:
        try:
            url_entries = load_url_file(args.url_file)
        except (FileNotFoundError, ValueError) as e:
            parser.error(str(e))
        # Validate all URLs in the file
        for entry in url_entries:
            p = urlparse(entry.url)
            if p.scheme not in ("http", "https"):
                parser.error(f"Invalid URL scheme in url-file: {entry.url}")

    # --- Master mode: requires URL and --expect-workers ---
    if args.master:
        if args.expect_workers is None or args.expect_workers < 1:
            parser.error("--master requires --expect-workers N (N >= 1)")
        if args.url is None:
            parser.error("--master requires a target URL")

    # URL is required for all non-worker, non-url-file modes
    if args.url is None and args.url_file is None:
        parser.error("the following arguments are required: url (or --url-file)")

    if args.url is not None:
        parsed = urlparse(args.url)
        if parsed.scheme not in ("http", "https"):
            parser.error(f"Invalid URL scheme: {parsed.scheme}. Use http:// or https://")

    # Determine mode
    if args.autofind:
        # Autofind mode: users/duration are managed internally
        pass
    elif args.users is not None:
        # User simulation mode: duration is required
        if args.num_requests is not None:
            parser.error("Cannot use -n with -u (user simulation). Use -d for duration.")
        if args.duration is None:
            parser.error("User simulation mode (-u) requires -d (duration).")
    elif args.num_requests is not None and args.duration is not None:
        parser.error("Cannot use both -n (request count) and -d (duration). Pick one.")

    duration = args.duration
    if args.users is None and args.num_requests is None and duration is None:
        duration = 10.0  # default

    # Body from file or string
    body = None
    if args.post_file:
        if not os.path.isfile(args.post_file):
            parser.error(f"Post file not found: {args.post_file}")
        with open(args.post_file, "rb") as f:
            body = f.read()
    elif args.body:
        body = args.body.encode()

    # Load scenario if specified
    scenario = None
    if hasattr(args, 'scenario') and args.scenario:
        scenario = load_scenario(args.scenario)

    headers = dict(args.headers)
    keepalive = not args.no_keepalive

    # Parse --tag key=value pairs
    tags: dict[str, str] = {}
    for tag_str in args.tags:
        if "=" not in tag_str:
            parser.error(f"Invalid tag format: {tag_str!r} (expected 'key=value')")
        key, value = tag_str.split("=", 1)
        tags[key.strip()] = value.strip()

    # Parse --threshold expressions
    thresholds: list[Threshold] = []
    for expr in args.thresholds:
        try:
            thresholds.append(parse_threshold(expr))
        except ValueError as e:
            parser.error(str(e))

    config = BenchmarkConfig(
        url=args.url or "",
        connections=args.connections,
        duration=duration,
        num_requests=args.num_requests,
        threads=args.threads,
        method=args.method.upper(),
        headers=headers,
        body=body,
        timeout_sec=args.timeout,
        keepalive=keepalive,
        basic_auth=args.basic_auth,
        cookies=args.cookies,
        verify_content_length=args.verify_length,
        verbosity=args.verbosity,
        csv_output=args.csv,
        html_output=args.html,
        json_output=args.json,
        users=args.users,
        ramp_up=args.ramp_up,
        think_time=args.think_time,
        think_time_jitter=args.think_jitter,
        random_param=args.random_param,
        live_dashboard=args.live,
        rate=args.rate,
        rate_ramp=args.rate_ramp,
        scenario=scenario,
        latency_breakdown=args.latency_breakdown,
        tags=tags,
        otel_endpoint=args.otel_endpoint,
        prom_remote_write=args.prom_remote_write,
        thresholds=thresholds,
    )

    # Validate rate options
    if config.rate_ramp is not None and config.rate is None:
        parser.error("--rate-ramp requires --rate")
    if config.rate_ramp is not None and config.duration is None:
        parser.error("--rate-ramp requires -d (duration)")

    if config.scenario and config.users is None and config.duration is None and config.num_requests is None:
        config.duration = 10.0

    if args.url_file is not None:
        results = asyncio.run(run_multi_url(url_entries, config))
        # Exit code 2 if any endpoint had threshold breaches
        exit_code = max((r.exit_code for r in results), default=0)
        sys.exit(exit_code)
    elif args.master:
        result = asyncio.run(run_master(config, args.bind, args.port, args.expect_workers))
        if result:
            _, exit_code = result
            sys.exit(exit_code)
        sys.exit(1)
    elif args.autofind:
        af_config = AutofindConfig(
            url=args.url,
            max_error_rate=args.max_error_rate,
            max_p95=args.max_p95,
            step_duration=args.step_duration,
            start_users=args.start_users,
            max_users=args.max_users,
            step_multiplier=args.step_multiplier,
            think_time=args.think_time,
            think_time_jitter=args.think_jitter,
            random_param=args.random_param,
            timeout_sec=args.timeout,
            keepalive=keepalive,
            json_output=args.json,
        )
        asyncio.run(run_autofind(af_config))
    elif config.users is not None:
        _, exit_code = asyncio.run(run_user_simulation(config))
        sys.exit(exit_code)
    else:
        _, exit_code = asyncio.run(run_benchmark(config))
        sys.exit(exit_code)


if __name__ == "__main__":
    main()
