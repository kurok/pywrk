"""Output formatting, reporting, and observability exports for pywrkr."""

import csv
import importlib.resources
import importlib.util
import json
import logging
import math
import re
import statistics
import sys
from collections import defaultdict
from string import Template
from typing import NamedTuple, TextIO
from urllib.parse import urlparse

from pywrkr.compare import (
    EXIT_USAGE,
    SCHEMA_VERSION,
    ResultsError,
    compare_results,
    load_baseline,
    render_report,
)
from pywrkr.config import (
    BenchmarkConfig,
    LatencyBreakdown,
    StepResult,
    Threshold,
    WorkerStats,
)
from pywrkr.traffic_profiles import RateLimiter

_logger = logging.getLogger(__name__)

# Optional third-party availability flags
RICH_AVAILABLE = importlib.util.find_spec("rich") is not None

try:
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource

    OTEL_AVAILABLE = True
except ImportError:
    OTEL_AVAILABLE = False

# ---------------------------------------------------------------------------
# Chart color constants
# ---------------------------------------------------------------------------

COLOR_GREEN = "rgba(76, 175, 80, 0.8)"
COLOR_YELLOW = "rgba(255, 193, 7, 0.8)"
COLOR_RED = "rgba(244, 67, 54, 0.8)"
COLOR_BLUE = "rgba(33, 150, 243, 0.8)"
COLOR_ORANGE = "rgba(255, 152, 0, 0.8)"
COLOR_PURPLE = "rgba(156, 39, 176, 0.8)"
COLOR_CYAN = "rgba(0, 188, 212, 0.8)"

# Status code colors (slightly higher opacity for pie chart)
STATUS_COLOR_2XX = "rgba(76, 175, 80, 0.85)"
STATUS_COLOR_3XX = "rgba(33, 150, 243, 0.85)"
STATUS_COLOR_4XX = "rgba(255, 152, 0, 0.85)"
STATUS_COLOR_5XX = "rgba(244, 67, 54, 0.85)"

# ---------------------------------------------------------------------------
# Latency breakdown aggregation
# ---------------------------------------------------------------------------


def aggregate_breakdowns(breakdowns: list[LatencyBreakdown]) -> dict:
    """Compute aggregate statistics for a list of LatencyBreakdown objects.

    Returns a dict with keys: dns, connect, tls, ttfb, transfer, total.
    Each has: avg, min, max, p50, p95, count.
    Also includes: new_connections, reused_connections.
    """
    if not breakdowns:
        return {}

    # Only report phases every sample actually measured. The httpx backend has
    # no hooks for DNS/TCP/TLS, and averaging in zeros for those would invent a
    # suspiciously fast connection phase rather than admitting it is unknown.
    measurable = set(getattr(breakdowns[0], "available", None) or ())
    for b in breakdowns[1:]:
        measurable &= set(getattr(b, "available", None) or ())

    phases = {
        "dns": [b.dns for b in breakdowns],
        "connect": [b.connect for b in breakdowns],
        "tls": [b.tls for b in breakdowns],
        "ttfb": [b.ttfb for b in breakdowns],
        "transfer": [b.transfer for b in breakdowns],
        "total": [b.dns + b.connect + b.tls + b.ttfb + b.transfer for b in breakdowns],
    }
    phases = {name: values for name, values in phases.items() if name in measurable}

    result: dict = {}
    # Connection reuse is only knowable from the connection-level hooks. Under
    # HTTP/2 in particular, "200 new connections" would be flatly wrong: that is
    # one connection carrying 200 streams.
    if "connect" in measurable:
        result["new_connections"] = sum(1 for b in breakdowns if not b.is_reused)
        result["reused_connections"] = sum(1 for b in breakdowns if b.is_reused)

    for name, values in phases.items():
        if not values:
            continue
        sorted_vals = sorted(values)
        n = len(sorted_vals)
        result[name] = {
            "avg": statistics.mean(values),
            "min": min(values),
            "max": max(values),
            "p50": sorted_vals[_nearest_rank_idx(50, n)],
            "p95": sorted_vals[_nearest_rank_idx(95, n)],
            "count": n,
        }

    return result


# ---------------------------------------------------------------------------
# Template loader
# ---------------------------------------------------------------------------


def _load_template(name: str) -> Template:
    """Load an HTML template from the templates package."""
    ref = importlib.resources.files("pywrkr.templates").joinpath(name)
    return Template(ref.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def format_bytes(n: float) -> str:
    """Format byte count to human-readable string (B/KB/MB/GB/TB)."""
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:.2f}{unit}"
        n /= 1024
    return f"{n:.2f}TB"


def format_duration(secs: float) -> str:
    """Format seconds to human-readable duration string (us/ms/s)."""
    if secs < 0.001:
        return f"{secs * 1_000_000:.2f}us"
    if secs < 1:
        return f"{secs * 1000:.2f}ms"
    return f"{secs:.2f}s"


def describe_session_mode(config: BenchmarkConfig) -> str:
    """Describe how cookies are scoped for this run, for banners and summaries."""
    if not config.session_cookies:
        return "off (static -C cookies only)"
    if config.scenario is not None and config.scenario.session == "fresh_per_iteration":
        return "cookie jar per user, cleared each iteration"
    return "cookie jar per user"


# ---------------------------------------------------------------------------
# Report printers
# ---------------------------------------------------------------------------


def print_latency_histogram(
    latencies: list[float], buckets: int = 20, file: TextIO = sys.stdout
) -> None:
    """Print an ASCII histogram of latency distribution."""
    # Drop non-finite samples (inf/NaN) so width math and int() bucketing
    # cannot crash or silently corrupt the histogram.
    latencies = [x for x in latencies if math.isfinite(x)]
    if not latencies:
        return
    mn, mx = min(latencies), max(latencies)
    if mn == mx:
        print(f"  All requests: {format_duration(mn)}", file=file)
        return
    width = (mx - mn) / buckets
    if not math.isfinite(width) or width <= 0:
        return
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


def _nearest_rank_idx(percentile: float, n: int) -> int:
    """Return the nearest-rank index for *percentile* in a sorted list of length *n*."""
    return min(int(math.ceil(percentile / 100 * n)) - 1, n - 1)


def compute_percentiles(latencies: list[float]) -> list[tuple[float, float]]:
    """Return list of (percentile, value) pairs.

    Non-finite samples (inf/NaN) are dropped before sorting so they cannot
    poison the nearest-rank result. High tail percentiles are only reported
    when the sample size can actually resolve them: p99.9 requires n >= 1000
    and p99.99 requires n >= 10000. For smaller samples these collapse to the
    max (and to each other / p99), so they are omitted rather than implying
    tail resolution the data does not support.
    """
    finite = [x for x in latencies if math.isfinite(x)]
    if not finite:
        return []
    sorted_lat = sorted(finite)
    n = len(sorted_lat)
    percentiles = [50, 75, 90, 95, 99]
    if n >= 1000:
        percentiles.append(99.9)
    if n >= 10000:
        percentiles.append(99.99)
    result = []
    for p in percentiles:
        result.append((p, sorted_lat[_nearest_rank_idx(p, n)]))
    return result


# ---------------------------------------------------------------------------
# Threshold support (SLO pass/fail)
# ---------------------------------------------------------------------------

_THRESHOLD_PATTERN = re.compile(
    r"^\s*(p50|p75|p90|p95|p99|avg_latency|max_latency|min_latency|error_rate|rps)"
    r"\s*(<=?|>=?)\s*"
    r"([0-9]*\.?[0-9]+)\s*(ms|s|us|%)?\s*$"
)

_LATENCY_METRICS = {"p50", "p75", "p90", "p95", "p99", "avg_latency", "max_latency", "min_latency"}

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
        elif unit == "%":
            raise ValueError(f"Invalid unit '%' for latency metric {metric!r} in: {expr!r}")
        elif unit is None:
            # A bare latency number is interpreted as seconds, which silently
            # turns a misremembered 'p99<5' (meant as 5ms) into a green gate.
            # Keep the seconds semantics for backward compatibility, but warn.
            _logger.warning(
                "Threshold %r has no time unit; interpreting %s as %s seconds. "
                "Add an explicit unit (e.g. '%sms') to avoid silently gating in seconds.",
                expr.strip(),
                raw_value,
                raw_value,
                raw_value,
            )
        # else: unit == "s" -> already in seconds, nothing to convert
    elif metric == "error_rate":
        # '%' is optional; value is always a percentage number
        if unit in ("ms", "s", "us"):
            raise ValueError(f"Invalid unit {unit!r} for error_rate in: {expr!r}")
    elif metric == "rps":
        if unit in ("ms", "s", "us", "%"):
            raise ValueError(f"Invalid unit {unit!r} for rps in: {expr!r}")

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
    """Compare actual value against threshold using the given operator."""
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
    file: TextIO = sys.stdout,
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


def print_percentiles(latencies: list[float], file: TextIO = sys.stdout) -> None:
    """Print latency percentiles table."""
    pairs = compute_percentiles(latencies)
    if not pairs:
        return
    print("  Latency Percentiles:", file=file)
    for p, val in pairs:
        print(f"    p{p:<6} {format_duration(val):>12}", file=file)


def _bucket_timeline(timeline: list[tuple[float, int]], bucket_size: int) -> dict[int, int]:
    """Bucket an rps_timeline into ``{bucket_index: summed_count}``.

    Timestamps are bucketed relative to the timeline's own earliest entry, not
    an external clock. ``merge_stats`` already rebases each worker's timeline
    onto a ``[0, duration)`` axis (see ``normalize_timeline``), so subtracting a
    monotonic ``start_time`` again would push every bucket index far negative —
    which empties the console timeline and produces negative HTML x-axis labels.
    """
    buckets: dict[int, int] = defaultdict(int)
    if not timeline:
        return buckets
    origin = min(ts for ts, _ in timeline)
    for ts, count in timeline:
        buckets[int((ts - origin) / bucket_size)] += count
    return buckets


def print_rps_timeline(
    timeline: list[tuple[float, int]], start: float, duration: float, file: TextIO = sys.stdout
) -> None:
    """Print requests-per-second timeline.

    ``start`` is accepted for backward compatibility; bucketing is done relative
    to the timeline's own origin via :func:`_bucket_timeline`.
    """
    if not timeline:
        return
    bucket_size = max(1, int(duration / 20))
    # A non-empty timeline always yields at least one bucket (origin -> 0).
    buckets = _bucket_timeline(timeline, bucket_size)

    def _bucket_span(i: int) -> float:
        # The final partial bucket spans only the remaining real time, so
        # dividing by the full bucket_size would understate its throughput.
        span = bucket_size
        if duration > 0:
            span = min(bucket_size, duration - i * bucket_size)
        return span if span > 0 else bucket_size

    max_rps = max(count / _bucket_span(i) for i, count in buckets.items())
    bar_max = 40
    print(f"  Requests/sec Timeline ({bucket_size}s buckets):", file=file)
    for i in range(max(buckets.keys()) + 1):
        rps = buckets.get(i, 0) / _bucket_span(i)
        bar_len = int(rps / max_rps * bar_max) if max_rps else 0
        bar = "#" * bar_len
        t_start = i * bucket_size
        print(f"    {t_start:>4}s | {bar:<{bar_max}} | {rps:>8.1f} req/s", file=file)


def _config_snapshot(config: BenchmarkConfig, connections: int) -> dict:
    """Capture the load shape a run was asked for.

    Only the fields that make two runs comparable — comparing a 10-user run to a
    1000-user baseline is arithmetically fine and completely meaningless. The
    host is recorded but not the full URL, so query-string noise does not make
    every run look incomparable.
    """
    if config.users is not None:
        mode = "users"
    elif config.num_requests is not None:
        mode = "requests"
    else:
        mode = "duration"
    if config.scenario is not None:
        mode = f"scenario:{mode}"
    return {
        "mode": mode,
        "connections": connections,
        "users": config.users,
        "duration": config.duration,
        "num_requests": config.num_requests,
        "rate": config.rate,
        "url_host": urlparse(config.url).netloc or None,
    }


def build_results_dict(
    stats: WorkerStats,
    duration: float,
    connections: int,
    config: BenchmarkConfig | None = None,
    rate_limiter: RateLimiter | None = None,
) -> dict:
    """Build a structured results dict for JSON/HTML/programmatic use."""
    rps = stats.total_requests / duration if duration > 0 else 0
    transfer_rate = stats.total_bytes / duration if duration > 0 else 0
    result: dict = {
        # Lets `pywrkr compare` reject files it cannot read. Files written before
        # this key existed have the same shape and are read as version 1.
        "schema_version": SCHEMA_VERSION,
        "duration_sec": round(duration, 3),
        "connections": connections,
        "total_requests": stats.total_requests,
        "total_errors": stats.errors,
        "requests_per_sec": round(rps, 2),
        "transfer_per_sec_bytes": round(transfer_rate, 2),
        "total_bytes": stats.total_bytes,
        "content_length_errors": stats.content_length_errors,
        # Scenario correlation counters (always present so CI consumers can rely
        # on the key; 0 for non-scenario runs).
        "extract_failures": stats.extract_failures,
        "template_errors": stats.template_errors,
        "status_codes": dict(stats.status_codes),
        # Negotiated protocol per response. With --http2 this is what shows a
        # server that only offered HTTP/1.1, rather than the run silently
        # claiming to be an HTTP/2 test.
        "http_versions": dict(stats.http_versions),
        "error_types": dict(stats.error_types),
        # Throughput timeline as [seconds_from_start, requests_in_interval] pairs.
        # Always present (empty list when not collected) so JSON/CI consumers can
        # rely on the key. Timestamps are already rebased to a [0, duration) axis
        # by merge_stats(); see normalize_timeline.
        "rps_timeline": [[round(ts, 3), count] for ts, count in stats.rps_timeline],
    }
    if config is not None:
        # Snapshot of what was asked for, so `pywrkr compare` can tell whether
        # two runs are even comparable.
        result["config"] = _config_snapshot(config, connections)
    if config is not None and config.tags:
        result["tags"] = dict(config.tags)
    if config is not None and config.rate is not None:
        result["target_rps"] = config.rate
        if config.rate_ramp is not None:
            result["ramp_target_rps"] = config.rate_ramp
        if config.traffic_profile is not None:
            result["traffic_profile"] = config.traffic_profile.describe()
        if rate_limiter is not None:
            result["rate_limit_waits"] = rate_limiter.waits
    finite_latencies = [x for x in stats.latencies if math.isfinite(x)]
    if finite_latencies:
        pct_pairs = compute_percentiles(finite_latencies)
        pct_map = dict(pct_pairs)
        # Use the nearest-rank p50 as the median so latency.median and
        # percentiles.p50 agree (the ab-style table and print_percentiles
        # also use nearest-rank).
        median = pct_map.get(50, statistics.median(finite_latencies))
        result["latency"] = {
            "min": round(min(finite_latencies), 6),
            "max": round(max(finite_latencies), 6),
            "mean": round(statistics.mean(finite_latencies), 6),
            "median": round(median, 6),
            "stdev": (
                round(statistics.stdev(finite_latencies), 6) if len(finite_latencies) > 1 else 0
            ),
        }
        result["percentiles"] = {f"p{p}": round(v, 6) for p, v in pct_pairs}
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
        bd_json: dict = {}
        # Omitted entirely when the backend cannot observe connection reuse,
        # rather than reported as zero.
        for key in ("new_connections", "reused_connections"):
            if key in agg:
                bd_json[key] = agg[key]
        for phase in ("dns", "connect", "tls", "ttfb", "transfer", "total"):
            if phase in agg:
                bd_json[phase] = {k: round(v, 6) for k, v in agg[phase].items()}
        result["latency_breakdown"] = bd_json
    return result


def write_csv_output(path: str, stats: WorkerStats) -> None:
    """Write ab-style CSV with percentile served times."""
    if not stats.latencies:
        return
    sorted_lat = sorted(stats.latencies)
    n = len(sorted_lat)
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["Percentage", "Time (ms)"])
        for pct in range(1, 101):
            writer.writerow([pct, round(sorted_lat[_nearest_rank_idx(pct, n)] * 1000, 3)])


def write_json_output(path: str, results: dict) -> None:
    """Write benchmark results as JSON to a file.

    Uses ``allow_nan=False`` so a non-finite float (inf/NaN) fails loudly with
    a ``ValueError`` instead of silently writing the non-standard
    ``Infinity``/``NaN`` tokens that strict JSON parsers reject.
    """
    with open(path, "w") as f:
        json.dump(results, f, indent=2, allow_nan=False)


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
        "<tr><th>Metric</th><th>Value</th></tr>\n" + "\n".join(rows) + "\n</table></body></html>"
    )


def generate_gatling_html_report(
    stats: WorkerStats,
    duration: float,
    connections: int,
    config: BenchmarkConfig | None = None,
    rate_limiter: RateLimiter | None = None,
    start_time: float = 0.0,
) -> str:
    """Generate a Gatling-style interactive HTML report with charts.

    Produces a self-contained HTML file using Chart.js (loaded from CDN)
    with:
    - Summary indicators (requests, errors, RPS, mean/p95/p99 latency)
    - Response time distribution histogram
    - Response time percentiles chart
    - Requests per second timeline
    - Status code breakdown (pie chart)
    - Latency breakdown by phase (if available)
    """
    results = build_results_dict(stats, duration, connections, config, rate_limiter)
    latency = results.get("latency", {})
    percentiles = results.get("percentiles", {})
    status_codes = results.get("status_codes", {})
    error_types = results.get("error_types", {})

    # -- Histogram buckets --
    hist_labels: list[str] = []
    hist_counts: list[int] = []
    hist_colors: list[str] = []
    if stats.latencies:
        sorted_lat = sorted(stats.latencies)
        lo, hi = sorted_lat[0], sorted_lat[-1]
        if hi > lo:
            # Create ~20 buckets
            n_buckets = min(30, max(10, len(sorted_lat) // 50))
            step = (hi - lo) / n_buckets
            buckets_hist: list[int] = [0] * n_buckets
            for lat in sorted_lat:
                idx = min(int((lat - lo) / step), n_buckets - 1)
                buckets_hist[idx] += 1
            p50 = percentiles.get("p50", 0)
            p95 = percentiles.get("p95", 0)
            for i in range(n_buckets):
                edge_ms = (lo + i * step) * 1000
                hist_labels.append(f"{edge_ms:.0f}")
                hist_counts.append(buckets_hist[i])
                # Color by latency: green < p50, yellow < p95, red >= p95
                edge_s = lo + i * step
                if edge_s < p50:
                    hist_colors.append(COLOR_GREEN)
                elif edge_s < p95:
                    hist_colors.append(COLOR_YELLOW)
                else:
                    hist_colors.append(COLOR_RED)
        else:
            # Degenerate distribution: every observed latency is the same
            # value. Falling through to the bucket loop with hi == lo would
            # produce a step of 1 second and stretch the histogram across
            # an arbitrary range with all bars red. Render a single green
            # bar at the actual value instead.
            hist_labels.append(f"{lo * 1000:.0f}")
            hist_counts.append(len(sorted_lat))
            hist_colors.append(COLOR_GREEN)

    # -- Percentile curve --
    pct_labels = ["p50", "p75", "p90", "p95", "p99"]
    pct_values = [round(percentiles.get(p, 0) * 1000, 2) for p in pct_labels]

    # -- RPS timeline --
    rps_labels: list[str] = []
    rps_values: list[float] = []
    if stats.rps_timeline:
        bucket_size = max(1, int(duration / 40))
        time_buckets = _bucket_timeline(stats.rps_timeline, bucket_size)
        for b in sorted(time_buckets.keys()):
            # Divide the final partial bucket by its real span, not the full
            # bucket_size, so the last bar reflects the true rate.
            span = bucket_size
            if duration > 0:
                span = min(bucket_size, duration - b * bucket_size)
            rps_labels.append(f"{b * bucket_size}s")
            rps_values.append(round(time_buckets[b] / max(span, 1e-9), 1))

    # -- Status code pie --
    sc_labels = [str(c) for c in sorted(status_codes.keys())]
    sc_values = [status_codes[int(c)] for c in sc_labels]
    sc_colors = []
    for c in sc_labels:
        code = int(c)
        if 200 <= code < 300:
            sc_colors.append(STATUS_COLOR_2XX)
        elif 300 <= code < 400:
            sc_colors.append(STATUS_COLOR_3XX)
        elif 400 <= code < 500:
            sc_colors.append(STATUS_COLOR_4XX)
        else:
            sc_colors.append(STATUS_COLOR_5XX)

    # -- Latency breakdown --
    bd = results.get("latency_breakdown", {})
    bd_phases = ["dns", "connect", "tls", "ttfb", "transfer"]
    bd_labels = ["DNS", "Connect", "TLS", "TTFB", "Transfer"]
    bd_values = [round(bd.get(p, {}).get("avg", 0) * 1000, 2) for p in bd_phases]
    has_breakdown = any(v > 0 for v in bd_values)

    # -- Error rate --
    error_rate = (stats.errors / stats.total_requests * 100) if stats.total_requests else 0

    # -- Mode description --
    mode = "Duration mode"
    if config:
        if config.users:
            mode = f"{config.users} virtual users"
        elif config.num_requests:
            mode = f"{config.num_requests:,} requests"
        elif config.rate:
            mode = f"Rate: {config.rate} req/s"

    url = config.url if config else "N/A"
    method = config.method if config else "GET"
    timestamp = __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    import json as _json

    # Pre-compute conditional CSS classes and display values
    errors_class = "red" if stats.errors else "green"
    p95_class = "yellow" if percentiles.get("p95", 0) > 1 else ""
    p99_class = "red" if percentiles.get("p99", 0) > 2 else ""
    bd_card_display = "display:block" if has_breakdown else "display:none"

    # Pre-render error table HTML
    error_table_html = ""
    if error_types:
        rows = "".join(
            f"<tr><td>{_html_escape(e)}</td><td>{c:,}</td></tr>"
            for e, c in sorted(error_types.items(), key=lambda x: -x[1])
        )
        error_table_html = (
            '<div class="chart-card full" style="margin-bottom:28px">\n'
            "  <h3>Error Details</h3>\n"
            '  <table class="errors-table">\n'
            "    <tr><th>Error</th><th>Count</th></tr>\n"
            f"    {rows}\n"
            "  </table>\n"
            "</div>"
        )

    # Build template context
    context = {
        "title": _html_escape(url),
        "method": _html_escape(method),
        "url_display": _html_escape(url),
        "mode": mode,
        "connections": connections,
        "timestamp": timestamp,
        "total_requests": f"{stats.total_requests:,}",
        "duration_display": f"{duration:.1f}",
        "rps_display": f"{results.get('requests_per_sec', 0):,.1f}",
        "errors_display": f"{stats.errors:,} ({error_rate:.1f}%)",
        "errors_class": errors_class,
        "mean_latency": format_duration(latency.get("mean", 0)),
        "p95_latency": format_duration(percentiles.get("p95", 0)),
        "p95_class": p95_class,
        "p99_latency": format_duration(percentiles.get("p99", 0)),
        "p99_class": p99_class,
        "transfer_rate": format_bytes(results.get("transfer_per_sec_bytes", 0)),
        "hist_labels_json": _json.dumps(hist_labels),
        "hist_counts_json": _json.dumps(hist_counts),
        "hist_colors_json": _json.dumps(hist_colors),
        "pct_labels_json": _json.dumps(pct_labels),
        "pct_values_json": _json.dumps(pct_values),
        "rps_labels_json": _json.dumps(rps_labels),
        "rps_values_json": _json.dumps(rps_values),
        "sc_labels_json": _json.dumps(sc_labels),
        "sc_values_json": _json.dumps(sc_values),
        "sc_colors_json": _json.dumps(sc_colors),
        "bd_labels_json": _json.dumps(bd_labels),
        "bd_values_json": _json.dumps(bd_values),
        "has_breakdown_json": _json.dumps(has_breakdown),
        "bd_card_display": bd_card_display,
        "bd_bar_colors_json": _json.dumps(
            [COLOR_BLUE, COLOR_GREEN, COLOR_PURPLE, COLOR_ORANGE, COLOR_CYAN]
        ),
        "error_table_html": error_table_html,
    }

    template = _load_template("gatling_report.html")
    return template.safe_substitute(context)


def _html_escape(s: str) -> str:
    """Escape HTML special characters."""
    from html import escape

    return escape(s)


def write_html_report(path: str, html: str) -> None:
    """Write HTML report to a file."""
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)


# ---------------------------------------------------------------------------
# Shared export metric definitions
# ---------------------------------------------------------------------------


class _MetricSpec(NamedTuple):
    """Specification for an exportable benchmark metric."""

    name_suffix: str  # used for Prometheus: "pywrkr_" + name_suffix
    otel_name: str  # explicit OTel metric name
    results_key: str
    nested_key: str | None
    multiplier: float
    metric_type: str  # "counter" or "gauge"
    description: str


_EXPORT_METRICS: list[_MetricSpec] = [
    _MetricSpec(
        "requests_total",
        "pywrkr.requests.total",
        "total_requests",
        None,
        1,
        "counter",
        "Total requests",
    ),
    _MetricSpec(
        "errors_total", "pywrkr.errors.total", "total_errors", None, 1, "counter", "Total errors"
    ),
    _MetricSpec(
        "requests_per_sec",
        "pywrkr.requests_per_sec",
        "requests_per_sec",
        None,
        1,
        "gauge",
        "Requests per second",
    ),
    _MetricSpec(
        "transfer_bytes_per_sec",
        "pywrkr.transfer_bytes_per_sec",
        "transfer_per_sec_bytes",
        None,
        1,
        "gauge",
        "Transfer bytes per second",
    ),
    _MetricSpec(
        "duration_sec",
        "pywrkr.duration_sec",
        "duration_sec",
        None,
        1,
        "gauge",
        "Benchmark duration in seconds",
    ),
    _MetricSpec(
        "latency_p50_ms",
        "pywrkr.latency.p50",
        "percentiles",
        "p50",
        1000,
        "gauge",
        "p50 latency in ms",
    ),
    _MetricSpec(
        "latency_p95_ms",
        "pywrkr.latency.p95",
        "percentiles",
        "p95",
        1000,
        "gauge",
        "p95 latency in ms",
    ),
    _MetricSpec(
        "latency_p99_ms",
        "pywrkr.latency.p99",
        "percentiles",
        "p99",
        1000,
        "gauge",
        "p99 latency in ms",
    ),
    _MetricSpec(
        "latency_mean_ms",
        "pywrkr.latency.mean",
        "latency",
        "mean",
        1000,
        "gauge",
        "Mean latency in ms",
    ),
    _MetricSpec(
        "latency_max_ms", "pywrkr.latency.max", "latency", "max", 1000, "gauge", "Max latency in ms"
    ),
]


# Valid Prometheus label name per the text exposition format spec.
_PROM_LABEL_NAME_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def _escape_prom_label_value(v: str) -> str:
    """Escape a label value per the Prometheus text exposition format.

    Backslash -> double-backslash, double-quote -> escaped quote, newline -> ``\\n``.
    Order matters: backslashes must be escaped first.
    """
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _resolve_metric_value(
    results: dict, results_key: str, nested_key: str | None, multiplier: float
) -> float | None:
    """Resolve a metric value from the results dict.

    Returns ``None`` when the underlying data is absent (the parent key is
    missing, or a nested key is missing) so callers can distinguish
    "no data collected" from a real zero and skip emitting the metric.
    """
    if nested_key is not None:
        parent = results.get(results_key)
        if not isinstance(parent, dict) or nested_key not in parent:
            return None
        val = parent[nested_key]
    else:
        if results_key not in results:
            return None
        val = results[results_key]
    return val * multiplier


def export_to_otel(results: dict, endpoint: str, tags: dict[str, str]) -> bool:
    """Export benchmark metrics to an OpenTelemetry collector via OTLP/HTTP.

    Returns True on success, False on any error (including missing packages).
    """
    if not OTEL_AVAILABLE:
        _logger.error(
            "OTel export failed: opentelemetry packages not installed. "
            "Install with: pip install pywrkr[otel]"
        )
        return False

    try:
        resource_attrs = {"service.name": "pywrkr"}
        resource_attrs.update(tags)
        resource = Resource.create(resource_attrs)
        exporter = OTLPMetricExporter(endpoint=endpoint)
        reader = PeriodicExportingMetricReader(exporter, export_interval_millis=1000)
        provider = MeterProvider(resource=resource, metric_readers=[reader])
        meter = provider.get_meter("pywrkr")
        attributes = dict(tags)

        for spec in _EXPORT_METRICS:
            value = _resolve_metric_value(
                results, spec.results_key, spec.nested_key, spec.multiplier
            )
            if value is None:
                continue
            if spec.metric_type == "counter":
                counter = meter.create_counter(spec.otel_name, description=spec.description)
                counter.add(value, attributes=attributes)
            else:
                gauge = meter.create_up_down_counter(spec.otel_name, description=spec.description)
                gauge.add(value, attributes=attributes)

        provider.force_flush()
        provider.shutdown()
        return True
    except Exception as e:
        _logger.error("OTel export failed (endpoint=%s): %s", endpoint, e)
        return False


def export_to_prometheus(results: dict, endpoint: str, tags: dict[str, str]) -> bool:
    """Export benchmark metrics to a Prometheus Pushgateway-compatible endpoint.

    Returns True on success, False on any error.
    """
    import urllib.error
    import urllib.request

    try:
        lines: list[str] = []
        labels_parts = [
            f'{k}="{_escape_prom_label_value(v)}"'
            for k, v in sorted(tags.items())
            if _PROM_LABEL_NAME_PATTERN.match(k)
        ]
        labels_str = "{" + ",".join(labels_parts) + "}" if labels_parts else ""

        for spec in _EXPORT_METRICS:
            value = _resolve_metric_value(
                results, spec.results_key, spec.nested_key, spec.multiplier
            )
            if value is None:
                continue
            prom_name = "pywrkr_" + spec.name_suffix
            lines.append(f"# HELP {prom_name} {spec.description}")
            lines.append(f"# TYPE {prom_name} {spec.metric_type}")
            lines.append(f"{prom_name}{labels_str} {value}")

        body = "\n".join(lines) + "\n"
        url = endpoint.rstrip("/") + "/metrics/job/pywrkr"
        req = urllib.request.Request(
            url,
            data=body.encode("utf-8"),
            method="POST",
            headers={"Content-Type": "text/plain; version=0.0.4"},
        )
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception as e:
        _logger.error("Prometheus export failed (endpoint=%s): %s", endpoint, e)
        return False


def run_baseline_gate(
    stats: WorkerStats,
    duration: float,
    connections: int,
    config: BenchmarkConfig,
    rate_limiter: "RateLimiter | None" = None,
    file: TextIO | None = None,
) -> int:
    """Write ``--save-baseline`` and apply ``--baseline`` in one pass.

    Folding the comparison into the run turns a three-step CI recipe (run,
    dump JSON, diff it) into one command.

    Sub-runs are skipped: a distributed worker and an autofind step each see
    only part of the picture, and gating (or worse, overwriting the baseline
    file) from several of them at once would be meaningless. The master applies
    the gate to the merged result instead.

    Returns:
        An exit code: 0 when there is nothing to do or nothing regressed,
        ``EXIT_REGRESSION`` when a ``--fail-on`` rule fired, ``EXIT_USAGE``
        when the baseline could not be read or the configs differ under
        ``--strict-config``.
    """
    if not config.save_baseline and not config.baseline:
        return 0
    if getattr(config, "_quiet", False):
        return 0

    out = file if file is not None else sys.stdout
    results = build_results_dict(stats, duration, connections, config, rate_limiter)

    if config.save_baseline:
        try:
            write_json_output(config.save_baseline, results)
        except OSError as exc:
            print(f"\n  ERROR: could not write baseline: {exc}", file=sys.stderr)
            return EXIT_USAGE
        print(f"\n  Baseline written to: {config.save_baseline}", file=out)

    if not config.baseline:
        return 0

    try:
        baseline, sources = load_baseline(config.baseline)
    except ResultsError as exc:
        print(f"\n  ERROR: {exc}", file=sys.stderr)
        return EXIT_USAGE

    report = compare_results(baseline, results, config.fail_on, sources)
    render_report(report, config.compare_format, file=out)

    if report.config_warnings and config.strict_config:
        print(
            "\n  ERROR: --strict-config is set and the run configurations differ",
            file=sys.stderr,
        )
        return EXIT_USAGE
    return report.exit_code


def run_observability_exports(
    stats: WorkerStats,
    duration: float,
    connections: int,
    config: BenchmarkConfig,
    rate_limiter: "RateLimiter | None" = None,
) -> bool:
    """Run configured OTel/Prometheus exports. Returns True if all succeeded."""
    if not (config.otel_endpoint or config.prom_remote_write):
        return True
    results = build_results_dict(stats, duration, connections, config, rate_limiter)
    ok = True
    if config.otel_endpoint:
        ok = export_to_otel(results, config.otel_endpoint, config.tags) and ok
    if config.prom_remote_write:
        ok = export_to_prometheus(results, config.prom_remote_write, config.tags) and ok
    return ok


def _print_console_results(
    stats: WorkerStats,
    duration: float,
    connections: int,
    start_time: float,
    config: BenchmarkConfig,
    rate_limiter: RateLimiter | None = None,
    file: TextIO | None = None,
) -> None:
    """Render all console output for a completed benchmark run.

    Pure console I/O — no files are written and no network calls are made.
    Callers that need to suppress or redirect output pass ``file``.
    """
    out = file if file is not None else sys.stdout

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
        print(
            f"  Think Time:        {format_duration(config.think_time)} "
            f"(+/-{config.think_time_jitter:.0%})",
            file=out,
        )
        if config.users > 0:
            print(f"  Avg Reqs/User:     {stats.total_requests / config.users:,.1f}", file=out)
    else:
        print(f"  Connections:       {connections}", file=out)
    print(f"  Keep-Alive:        {'yes' if config.keepalive else 'no'}", file=out)
    if config.users:
        # Only meaningful where there are virtual users to isolate; plain
        # connection mode has no per-user identity.
        print(f"  Sessions:          {describe_session_mode(config)}", file=out)
    print(f"  Total Requests:    {stats.total_requests:,}", file=out)
    print(f"  Total Errors:      {stats.errors:,}", file=out)
    if stats.content_length_errors:
        print(f"  Content-Len Errs:  {stats.content_length_errors:,}", file=out)
    if stats.extract_failures:
        print(f"  Extract Failures:  {stats.extract_failures:,}", file=out)
    if stats.template_errors:
        print(f"  Template Errors:   {stats.template_errors:,}", file=out)
    print(f"  Requests/sec:      {rps:,.2f}", file=out)
    if config.rate is not None:
        print(f"  Target RPS:        {config.rate:,.2f}", file=out)
        if config.rate_ramp is not None:
            print(f"  Ramp Target RPS:   {config.rate_ramp:,.2f}", file=out)
        if config.traffic_profile is not None:
            print(f"  Traffic Profile:   {config.traffic_profile.describe()}", file=out)
        if rate_limiter is not None:
            print(f"  Rate Limit Waits:  {rate_limiter.waits:,}", file=out)
    print(f"  Transfer/sec:      {format_bytes(transfer_rate)}/s", file=out)
    print(f"  Total Transfer:    {format_bytes(stats.total_bytes)}", file=out)

    # Latency stats (computed on finite samples only to avoid NaN/inf poisoning)
    finite_latencies = [x for x in stats.latencies if math.isfinite(x)]
    if finite_latencies:
        pct_map = dict(compute_percentiles(finite_latencies))
        # Nearest-rank p50, consistent with the ab-style table / percentiles.
        median = pct_map.get(50, statistics.median(finite_latencies))
        print(f"\n{'=' * 70}", file=out)
        print("  LATENCY STATISTICS", file=out)
        print(f"{'=' * 70}", file=out)
        print(f"    Min:       {format_duration(min(finite_latencies)):>12}", file=out)
        print(f"    Max:       {format_duration(max(finite_latencies)):>12}", file=out)
        print(f"    Mean:      {format_duration(statistics.mean(finite_latencies)):>12}", file=out)
        print(f"    Median:    {format_duration(median):>12}", file=out)
        if len(finite_latencies) > 1:
            print(
                f"    Stdev:     {format_duration(statistics.stdev(finite_latencies)):>12}",
                file=out,
            )

        print(file=out)
        print_percentiles(finite_latencies, file=out)

        # ab-style "percentage of requests served within" table
        sorted_lat = sorted(finite_latencies)
        n = len(sorted_lat)
        print(file=out)
        print("  Percentage of requests served within a certain time:", file=out)
        for pct in [50, 66, 75, 80, 90, 95, 98, 99, 100]:
            print(
                f"    {pct:>3}%    {format_duration(sorted_lat[_nearest_rank_idx(pct, n)]):>12}",
                file=out,
            )

        print(file=out)
        print_latency_histogram(finite_latencies, file=out)

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
                print(
                    f"    {label + ':':18s} {format_duration(d['avg']):>12}"
                    f"  (min={format_duration(d['min'])},"
                    f" max={format_duration(d['max'])},"
                    f" p50={format_duration(d['p50'])},"
                    f" p95={format_duration(d['p95'])})",
                    file=out,
                )
        if "new_connections" in agg:
            print(f"\n    New Connections:    {agg['new_connections']:,}", file=out)
            print(f"    Reused Connections: {agg['reused_connections']:,}", file=out)
        else:
            print(
                "\n    Connection reuse:   not observable on this backend",
                file=out,
            )

    # Negotiated protocol. Only interesting when HTTP/2 was asked for, or when
    # a run somehow saw more than one protocol.
    if stats.http_versions and (config.http2 or len(stats.http_versions) > 1):
        print(f"\n{'=' * 70}", file=out)
        print("  NEGOTIATED PROTOCOL", file=out)
        print(f"{'=' * 70}", file=out)
        total_versioned = sum(stats.http_versions.values()) or 1
        for version, count in sorted(stats.http_versions.items()):
            pct = count / total_versioned * 100
            print(f"    HTTP/{version}: {count:>10,} ({pct:5.1f}%)", file=out)
        fallback = sum(c for v, c in stats.http_versions.items() if v != "2")
        if config.http2 and fallback:
            print(
                f"\n    WARNING: {fallback:,} request(s) did not use HTTP/2. The server "
                f"offered HTTP/1.1;\n             these numbers are not HTTP/2 numbers.",
                file=out,
            )

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
                print(
                    f"      Count: {len(lats):,}  "
                    f"Mean: {format_duration(mean_lat)}  "
                    f"Min: {format_duration(min(lats))}  "
                    f"Max: {format_duration(max(lats))}",
                    file=out,
                )

    # RPS timeline
    if stats.rps_timeline:
        print(f"\n{'=' * 70}", file=out)
        print("  THROUGHPUT TIMELINE", file=out)
        print(f"{'=' * 70}", file=out)
        print_rps_timeline(stats.rps_timeline, start_time, duration, file=out)

    print(f"\n{'=' * 70}", file=out)


def _write_output_files(
    stats: WorkerStats,
    duration: float,
    connections: int,
    start_time: float,
    config: BenchmarkConfig,
    rate_limiter: RateLimiter | None = None,
    file: TextIO | None = None,
) -> None:
    """Write any configured output files (CSV, JSON, HTML).

    Only file I/O — no console output except the brief confirmation
    lines that tell the user where each file was written.
    Observability exports (OTel, Prometheus) are handled separately by
    ``run_observability_exports`` (called from ``_finalize_run``) so that
    their failures can influence the process exit code.
    """
    out = file if file is not None else sys.stdout

    if config.csv_output:
        write_csv_output(config.csv_output, stats)
        print(f"\n  CSV percentile data written to: {config.csv_output}", file=out)

    if config.json_output:
        results = build_results_dict(stats, duration, connections, config, rate_limiter)
        write_json_output(config.json_output, results)
        print(f"\n  JSON results written to: {config.json_output}", file=out)

    if config.html_output:
        html = generate_html_report(stats, duration, connections)
        print(f"\n{html}", file=out)

    if config.html_report:
        html = generate_gatling_html_report(
            stats, duration, connections, config, rate_limiter, start_time
        )
        write_html_report(config.html_report, html)
        print(f"\n  HTML report written to: {config.html_report}", file=out)


def print_results(
    stats: WorkerStats,
    duration: float,
    connections: int,
    start_time: float,
    config: BenchmarkConfig,
    rate_limiter: RateLimiter | None = None,
    file: TextIO | None = None,
) -> None:
    """Print full benchmark results to stdout and write any configured output files."""
    _print_console_results(stats, duration, connections, start_time, config, rate_limiter, file)
    _write_output_files(stats, duration, connections, start_time, config, rate_limiter, file)


# ---------------------------------------------------------------------------
# Autofind reporting
# ---------------------------------------------------------------------------


def _format_latency_short(secs: float) -> str:
    """Format latency for autofind summary table (compact)."""
    if secs < 1.0:
        return f"{secs * 1000:.0f}ms"
    return f"{secs:.1f}s"


def print_autofind_summary(steps: list[StepResult], max_users: int | None) -> None:
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
    print(
        f"  {'Users':>5} | {'RPS':>8} | {'p50':>7}"
        f" | {'p95':>7} | {'p99':>7} | {'Errors':>6} | Status"
    )
    for s in steps:
        status = "OK" if s.passed else "FAIL"
        print(
            f"  {s.users:>5} | {s.rps:>8.1f} | {_format_latency_short(s.p50):>7} | "
            f"{_format_latency_short(s.p95):>7} | {_format_latency_short(s.p99):>7} | "
            f"{s.error_rate:>5.1f}% | {status}"
        )
    print("=" * 60)


# ---------------------------------------------------------------------------
# Multi-URL reporting
# ---------------------------------------------------------------------------


def print_multi_url_summary(results: "list[MultiUrlResult]", file: TextIO | None = None) -> None:  # noqa: F821
    """Print a comparison table across all URLs."""
    out = file if file is not None else sys.stdout
    print(f"\n{'=' * 90}", file=out)
    print("  MULTI-URL COMPARISON SUMMARY", file=out)
    print(f"{'=' * 90}", file=out)

    # Header
    print(
        f"\n  {'#':>3}  {'Method':<7} {'URL':<40} {'Reqs':>7} {'RPS':>9} "
        f"{'p50':>9} {'p95':>9} {'p99':>9} {'Errs':>6}",
        file=out,
    )
    d = "\u2500"
    print(f"  {d * 3}  {d * 7} {d * 40} {d * 7} {d * 9} {d * 9} {d * 9} {d * 9} {d * 6}", file=out)

    for i, r in enumerate(results, 1):
        rps = r.stats.total_requests / r.duration if r.duration > 0 else 0
        url_display = r.url if len(r.url) <= 40 else r.url[:37] + "..."

        # Compute percentiles
        p50 = p95 = p99 = 0.0
        if r.stats.latencies:
            pct_map = dict(compute_percentiles(r.stats.latencies))
            p50 = pct_map.get(50, 0.0)
            p95 = pct_map.get(95, 0.0)
            p99 = pct_map.get(99, 0.0)

        err_pct = (
            (r.stats.errors / r.stats.total_requests * 100) if r.stats.total_requests > 0 else 0
        )

        print(
            f"  {i:>3}  {r.method:<7} {url_display:<40} "
            f"{r.stats.total_requests:>7,} {rps:>9,.1f} "
            f"{format_duration(p50):>9} {format_duration(p95):>9} {format_duration(p99):>9} "
            f"{err_pct:>5.1f}%",
            file=out,
        )

    print(f"\n{'=' * 90}", file=out)

    # Totals
    total_reqs = sum(r.stats.total_requests for r in results)
    total_errs = sum(r.stats.errors for r in results)
    total_bytes = sum(r.stats.total_bytes for r in results)
    print(
        f"  Total: {len(results)} endpoints, {total_reqs:,} requests, "
        f"{total_errs:,} errors, {format_bytes(total_bytes)} transferred",
        file=out,
    )
    print(f"{'=' * 90}\n", file=out)


def build_multi_url_json(results: "list[MultiUrlResult]") -> dict:  # noqa: F821
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
