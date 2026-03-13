"""Output formatting, reporting, and observability exports for pywrkr."""

import csv
import json
import math
import re
import statistics
import sys
from collections import defaultdict

# Optional third-party imports
try:
    from rich.live import Live  # noqa: F401
    from rich.panel import Panel  # noqa: F401
    from rich.table import Table  # noqa: F401
    from rich.text import Text  # noqa: F401

    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False

try:
    from opentelemetry import metrics as otel_metrics  # noqa: F401
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource

    OTEL_AVAILABLE = True
except ImportError:
    OTEL_AVAILABLE = False

from pywrkr.config import (
    BenchmarkConfig,
    StepResult,
    Threshold,
    WorkerStats,
)
from pywrkr.traffic_profiles import RateLimiter

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


# ---------------------------------------------------------------------------
# Report printers
# ---------------------------------------------------------------------------


def print_latency_histogram(latencies: list[float], buckets: int = 20, file=sys.stdout) -> None:
    """Print an ASCII histogram of latency distribution."""
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
        elif unit == "s" or unit is None:
            pass  # already seconds
        elif unit == "%":
            raise ValueError(f"Invalid unit '%' for latency metric {metric!r} in: {expr!r}")
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


def print_percentiles(latencies: list[float], file=sys.stdout) -> None:
    """Print latency percentiles table."""
    pairs = compute_percentiles(latencies)
    if not pairs:
        return
    print("  Latency Percentiles:", file=file)
    for p, val in pairs:
        print(f"    p{p:<6} {format_duration(val):>12}", file=file)


def print_rps_timeline(
    timeline: list[tuple[float, int]], start: float, duration: float, file=sys.stdout
) -> None:
    """Print requests-per-second timeline."""
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


def build_results_dict(
    stats: WorkerStats,
    duration: float,
    connections: int,
    config: BenchmarkConfig | None = None,
    rate_limiter: RateLimiter | None = None,
) -> dict:
    """Build a structured results dict for JSON/HTML/programmatic use."""
    from pywrkr.workers import aggregate_breakdowns

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
        if config.traffic_profile is not None:
            result["traffic_profile"] = config.traffic_profile.describe()
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
            idx = min(int(math.ceil(pct / 100 * n)) - 1, n - 1)
            writer.writerow([pct, round(sorted_lat[idx] * 1000, 3)])


def write_json_output(path: str, results: dict) -> None:
    """Write benchmark results as JSON to a file."""
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
        # Create ~20 buckets
        lo, hi = sorted_lat[0], sorted_lat[-1]
        n_buckets = min(30, max(10, len(sorted_lat) // 50))
        step = (hi - lo) / n_buckets if n_buckets > 0 and hi > lo else 1
        if step <= 0:
            step = 1
        buckets_hist: list[int] = [0] * n_buckets
        for lat in sorted_lat:
            idx = min(int((lat - lo) / step), n_buckets - 1)
            buckets_hist[idx] += 1
        for i in range(n_buckets):
            edge_ms = (lo + i * step) * 1000
            hist_labels.append(f"{edge_ms:.0f}")
            hist_counts.append(buckets_hist[i])
            # Color by latency: green < p50, yellow < p95, red >= p95
            p50 = percentiles.get("p50", 0)
            p95 = percentiles.get("p95", 0)
            edge_s = lo + i * step
            if edge_s < p50:
                hist_colors.append(COLOR_GREEN)
            elif edge_s < p95:
                hist_colors.append(COLOR_YELLOW)
            else:
                hist_colors.append(COLOR_RED)

    # -- Percentile curve --
    pct_labels = ["p50", "p75", "p90", "p95", "p99"]
    pct_values = [round(percentiles.get(p, 0) * 1000, 2) for p in pct_labels]

    # -- RPS timeline --
    rps_labels: list[str] = []
    rps_values: list[float] = []
    if stats.rps_timeline:
        bucket_size = max(1, int(duration / 40))
        time_buckets: dict[int, int] = defaultdict(int)
        for ts, count in stats.rps_timeline:
            bucket = int((ts - start_time) / bucket_size)
            time_buckets[bucket] += count
        for b in sorted(time_buckets.keys()):
            rps_labels.append(f"{b * bucket_size}s")
            rps_values.append(round(time_buckets[b] / bucket_size, 1))

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

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>pywrkr Report &mdash; {_html_escape(url)}</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
<style>
  :root {{
    --bg: #1a1a2e; --surface: #16213e; --card: #0f3460;
    --accent: #e94560; --green: #4caf50; --yellow: #ffc107;
    --text: #eee; --muted: #999; --border: #234;
  }}
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
         background: var(--bg); color: var(--text); padding: 0; }}
  .header {{ background: linear-gradient(135deg, var(--surface), var(--card));
             padding: 24px 32px; border-bottom: 3px solid var(--accent); }}
  .header h1 {{ font-size: 1.6em; font-weight: 600; }}
  .header h1 span {{ color: var(--accent); }}
  .header .meta {{ color: var(--muted); font-size: 0.85em; margin-top: 6px; }}
  .container {{ max-width: 1200px; margin: 0 auto; padding: 24px; }}
  .indicators {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
                 gap: 16px; margin-bottom: 28px; }}
  .indicator {{ background: var(--surface); border-radius: 10px; padding: 18px 20px;
               border-left: 4px solid var(--accent); }}
  .indicator .label {{ font-size: 0.75em; text-transform: uppercase; letter-spacing: 1px;
                       color: var(--muted); margin-bottom: 6px; }}
  .indicator .value {{ font-size: 1.6em; font-weight: 700; }}
  .indicator .value.green {{ color: var(--green); }}
  .indicator .value.red {{ color: var(--accent); }}
  .indicator .value.yellow {{ color: var(--yellow); }}
  .charts {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-bottom: 28px; }}
  .chart-card {{ background: var(--surface); border-radius: 10px; padding: 20px; }}
  .chart-card h3 {{ font-size: 0.95em; margin-bottom: 14px; color: var(--muted);
                    text-transform: uppercase; letter-spacing: 0.5px; }}
  .chart-card.full {{ grid-column: 1 / -1; }}
  .errors-table {{ width: 100%; border-collapse: collapse; margin-top: 10px; }}
  .errors-table th, .errors-table td {{ padding: 8px 12px; text-align: left;
                                        border-bottom: 1px solid var(--border); }}
  .errors-table th {{ color: var(--muted); font-size: 0.8em; text-transform: uppercase; }}
  .footer {{ text-align: center; padding: 20px; color: var(--muted); font-size: 0.8em; }}
  .footer a {{ color: var(--accent); text-decoration: none; }}
  @media (max-width: 768px) {{ .charts {{ grid-template-columns: 1fr; }} }}
</style>
</head>
<body>
<div class="header">
  <h1><span>pywrkr</span> Benchmark Report</h1>
  <div class="meta">
    {_html_escape(method)} {_html_escape(url)} &bull; {mode} &bull;
    {connections} connections &bull; {timestamp}
  </div>
</div>
<div class="container">

<!-- Indicators -->
<div class="indicators">
  <div class="indicator">
    <div class="label">Total Requests</div>
    <div class="value">{stats.total_requests:,}</div>
  </div>
  <div class="indicator">
    <div class="label">Duration</div>
    <div class="value">{duration:.1f}s</div>
  </div>
  <div class="indicator">
    <div class="label">Requests/sec</div>
    <div class="value green">{results.get("requests_per_sec", 0):,.1f}</div>
  </div>
  <div class="indicator">
    <div class="label">Errors</div>
    <div class="value {"red" if stats.errors else "green"}">{stats.errors:,} ({
        error_rate:.1f}%)</div>
  </div>
  <div class="indicator">
    <div class="label">Mean Latency</div>
    <div class="value">{format_duration(latency.get("mean", 0))}</div>
  </div>
  <div class="indicator">
    <div class="label">p95 Latency</div>
    <div class="value {"yellow" if percentiles.get("p95", 0) > 1 else ""}">{
        format_duration(percentiles.get("p95", 0))
    }</div>
  </div>
  <div class="indicator">
    <div class="label">p99 Latency</div>
    <div class="value {"red" if percentiles.get("p99", 0) > 2 else ""}">{
        format_duration(percentiles.get("p99", 0))
    }</div>
  </div>
  <div class="indicator">
    <div class="label">Transfer</div>
    <div class="value">{format_bytes(results.get("transfer_per_sec_bytes", 0))}/s</div>
  </div>
</div>

<!-- Charts -->
<div class="charts">
  <!-- Response Time Distribution -->
  <div class="chart-card">
    <h3>Response Time Distribution</h3>
    <canvas id="histChart"></canvas>
  </div>
  <!-- Percentiles -->
  <div class="chart-card">
    <h3>Response Time Percentiles</h3>
    <canvas id="pctChart"></canvas>
  </div>
  <!-- RPS Timeline -->
  <div class="chart-card full">
    <h3>Requests per Second Over Time</h3>
    <canvas id="rpsChart" height="80"></canvas>
  </div>
  <!-- Status Codes -->
  <div class="chart-card">
    <h3>Status Code Distribution</h3>
    <canvas id="scChart"></canvas>
  </div>
  <!-- Latency Breakdown -->
  <div class="chart-card" id="bdCard" style="{
        "display:block" if has_breakdown else "display:none"
    }">
    <h3>Latency Breakdown (avg)</h3>
    <canvas id="bdChart"></canvas>
  </div>
</div>

<!-- Error Details -->
{
        ""
        if not error_types
        else '''
<div class="chart-card full" style="margin-bottom:28px">
  <h3>Error Details</h3>
  <table class="errors-table">
    <tr><th>Error</th><th>Count</th></tr>
    '''
        + "".join(
            f"<tr><td>{_html_escape(e)}</td><td>{c:,}</td></tr>"
            for e, c in sorted(error_types.items(), key=lambda x: -x[1])
        )
        + '''
  </table>
</div>
'''
    }

</div>
<div class="footer">
  Generated by <a href="https://github.com/kurok/pywrkr">pywrkr</a>
  &mdash; Python HTTP benchmarking tool
</div>

<script>
const chartDefaults = {{
  color: '#999',
  borderColor: 'rgba(255,255,255,0.1)',
  font: {{ family: "'Segoe UI', system-ui, sans-serif" }}
}};
Chart.defaults.color = chartDefaults.color;
Chart.defaults.borderColor = chartDefaults.borderColor;

// Response Time Distribution
new Chart(document.getElementById('histChart'), {{
  type: 'bar',
  data: {{
    labels: {_json.dumps(hist_labels)},
    datasets: [{{
      label: 'Requests',
      data: {_json.dumps(hist_counts)},
      backgroundColor: {_json.dumps(hist_colors)},
      borderRadius: 3,
    }}]
  }},
  options: {{
    responsive: true,
    plugins: {{
      legend: {{ display: false }},
      tooltip: {{ callbacks: {{ title: (items) => items[0].label + ' ms' }} }}
    }},
    scales: {{
      x: {{ title: {{ display: true, text: 'Response Time (ms)' }},
            ticks: {{ maxRotation: 45, autoSkip: true, maxTicksLimit: 15 }} }},
      y: {{ title: {{ display: true, text: 'Count' }}, beginAtZero: true }}
    }}
  }}
}});

// Percentile Chart
new Chart(document.getElementById('pctChart'), {{
  type: 'line',
  data: {{
    labels: {_json.dumps(pct_labels)},
    datasets: [{{
      label: 'Latency (ms)',
      data: {_json.dumps(pct_values)},
      borderColor: '#e94560',
      backgroundColor: 'rgba(233,69,96,0.15)',
      fill: true,
      tension: 0.3,
      pointRadius: 5,
      pointBackgroundColor: '#e94560',
    }}]
  }},
  options: {{
    responsive: true,
    plugins: {{ legend: {{ display: false }} }},
    scales: {{
      y: {{ title: {{ display: true, text: 'Response Time (ms)' }}, beginAtZero: true }}
    }}
  }}
}});

// RPS Timeline
new Chart(document.getElementById('rpsChart'), {{
  type: 'line',
  data: {{
    labels: {_json.dumps(rps_labels)},
    datasets: [{{
      label: 'Req/s',
      data: {_json.dumps(rps_values)},
      borderColor: '#4caf50',
      backgroundColor: 'rgba(76,175,80,0.12)',
      fill: true,
      tension: 0.2,
      pointRadius: 2,
    }}]
  }},
  options: {{
    responsive: true,
    plugins: {{ legend: {{ display: false }} }},
    scales: {{
      x: {{ title: {{ display: true, text: 'Time' }},
            ticks: {{ autoSkip: true, maxTicksLimit: 20 }} }},
      y: {{ title: {{ display: true, text: 'Requests/sec' }}, beginAtZero: true }}
    }}
  }}
}});

// Status Codes
new Chart(document.getElementById('scChart'), {{
  type: 'doughnut',
  data: {{
    labels: {_json.dumps(sc_labels)},
    datasets: [{{
      data: {_json.dumps(sc_values)},
      backgroundColor: {_json.dumps(sc_colors)},
      borderWidth: 0,
    }}]
  }},
  options: {{
    responsive: true,
    plugins: {{
      legend: {{ position: 'right', labels: {{ padding: 16 }} }}
    }}
  }}
}});

// Latency Breakdown
if ({_json.dumps(has_breakdown)}) {{
  new Chart(document.getElementById('bdChart'), {{
    type: 'bar',
    data: {{
      labels: {_json.dumps(bd_labels)},
      datasets: [{{
        label: 'Avg (ms)',
        data: {_json.dumps(bd_values)},
        backgroundColor: [
          'rgba(33,150,243,0.8)', 'rgba(76,175,80,0.8)', 'rgba(156,39,176,0.8)',
          'rgba(255,152,0,0.8)', 'rgba(0,188,212,0.8)'
        ],
        borderRadius: 4,
      }}]
    }},
    options: {{
      indexAxis: 'y',
      responsive: true,
      plugins: {{ legend: {{ display: false }} }},
      scales: {{
        x: {{ title: {{ display: true, text: 'Time (ms)' }}, beginAtZero: true }}
      }}
    }}
  }});
}}
</script>
</body>
</html>"""
    return html


def _html_escape(s: str) -> str:
    """Escape HTML special characters."""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def write_html_report(path: str, html: str) -> None:
    """Write HTML report to a file."""
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)


def export_to_otel(results: dict, endpoint: str, tags: dict[str, str]) -> None:
    """Export benchmark metrics to an OpenTelemetry collector via OTLP/HTTP."""
    if not OTEL_AVAILABLE:
        print(
            "Warning: opentelemetry packages not installed. Install with: pip install pywrkr[otel]"
        )
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
    import urllib.error
    import urllib.request

    try:
        # Build Prometheus text format
        lines: list[str] = []
        labels_parts = [f'{k}="{v}"' for k, v in sorted(tags.items())]
        labels_str = "{" + ",".join(labels_parts) + "}" if labels_parts else ""

        def _add(name: str, value: float, mtype: str = "gauge", help_text: str = ""):
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} {mtype}")
            lines.append(f"{name}{labels_str} {value}")

        _add("pywrkr_requests_total", results.get("total_requests", 0), "counter", "Total requests")
        _add("pywrkr_errors_total", results.get("total_errors", 0), "counter", "Total errors")
        _add(
            "pywrkr_requests_per_sec",
            results.get("requests_per_sec", 0),
            "gauge",
            "Requests per second",
        )
        _add(
            "pywrkr_transfer_bytes_per_sec",
            results.get("transfer_per_sec_bytes", 0),
            "gauge",
            "Transfer bytes per second",
        )
        _add(
            "pywrkr_duration_sec",
            results.get("duration_sec", 0),
            "gauge",
            "Benchmark duration in seconds",
        )

        percentiles = results.get("percentiles", {})
        latency = results.get("latency", {})
        _add(
            "pywrkr_latency_p50_ms", percentiles.get("p50", 0) * 1000, "gauge", "p50 latency in ms"
        )
        _add(
            "pywrkr_latency_p95_ms", percentiles.get("p95", 0) * 1000, "gauge", "p95 latency in ms"
        )
        _add(
            "pywrkr_latency_p99_ms", percentiles.get("p99", 0) * 1000, "gauge", "p99 latency in ms"
        )
        _add("pywrkr_latency_mean_ms", latency.get("mean", 0) * 1000, "gauge", "Mean latency in ms")
        _add("pywrkr_latency_max_ms", latency.get("max", 0) * 1000, "gauge", "Max latency in ms")

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


def print_results(
    stats: WorkerStats,
    duration: float,
    connections: int,
    start_time: float,
    config: BenchmarkConfig,
    rate_limiter: RateLimiter | None = None,
) -> None:
    """Print full benchmark results to stdout."""
    from pywrkr.workers import aggregate_breakdowns

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
    print(f"  Total Requests:    {stats.total_requests:,}", file=out)
    print(f"  Total Errors:      {stats.errors:,}", file=out)
    if stats.content_length_errors:
        print(f"  Content-Len Errs:  {stats.content_length_errors:,}", file=out)
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
            print(
                f"    Stdev:     {format_duration(statistics.stdev(stats.latencies)):>12}", file=out
            )

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
                print(
                    f"    {label + ':':18s} {format_duration(d['avg']):>12}"
                    f"  (min={format_duration(d['min'])},"
                    f" max={format_duration(d['max'])},"
                    f" p50={format_duration(d['p50'])},"
                    f" p95={format_duration(d['p95'])})",
                    file=out,
                )
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

    # Interactive HTML report (Gatling-style)
    if config.html_report:
        html = generate_gatling_html_report(
            stats, duration, connections, config, rate_limiter, start_time
        )
        write_html_report(config.html_report, html)
        print(f"\n  HTML report written to: {config.html_report}", file=out)

    # Observability exports
    if config.otel_endpoint or config.prom_remote_write:
        results = build_results_dict(stats, duration, connections, config, rate_limiter)
        if config.otel_endpoint:
            export_to_otel(results, config.otel_endpoint, config.tags)
        if config.prom_remote_write:
            export_to_prometheus(results, config.prom_remote_write, config.tags)


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


def print_multi_url_summary(results: "list[MultiUrlResult]") -> None:  # noqa: F821
    """Print a comparison table across all URLs."""
    out = sys.stdout
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
            sorted_lat = sorted(r.stats.latencies)
            n = len(sorted_lat)
            p50 = sorted_lat[min(int(math.ceil(50 / 100 * n)) - 1, n - 1)]
            p95 = sorted_lat[min(int(math.ceil(95 / 100 * n)) - 1, n - 1)]
            p99 = sorted_lat[min(int(math.ceil(99 / 100 * n)) - 1, n - 1)]

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
