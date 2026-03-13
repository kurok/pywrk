# Reporting Module Improvements Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Improve reporting.py readability, type safety, and maintainability through 5 targeted changes without over-engineering.

**Architecture:** Extract the 400-line Gatling HTML template into a separate `string.Template`-based file, add type hints, consolidate duplicated colors and metric definitions, and reuse the existing percentile computation function.

**Tech Stack:** Python 3.10+ stdlib only (`string.Template`, `importlib.resources`, `typing.TextIO`)

---

## Chunk 1: Foundation (color constants, type hints, percentile reuse)

### Task 1: Extract color constants

**Files:**
- Modify: `src/pywrkr/reporting.py:1-10` (add constants after imports)

- [ ] **Step 1: Add color constants at module top**

After the existing imports and `RICH_AVAILABLE`/`OTEL_AVAILABLE` blocks (line 40), add:

```python
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
```

- [ ] **Step 2: Replace hardcoded colors in histogram logic**

In `generate_gatling_html_report()`, replace the histogram color assignment block (lines 441-446):

```python
# Before:
if edge_s < p50:
    hist_colors.append("rgba(76, 175, 80, 0.8)")
elif edge_s < p95:
    hist_colors.append("rgba(255, 193, 7, 0.8)")
else:
    hist_colors.append("rgba(244, 67, 54, 0.8)")

# After:
if edge_s < p50:
    hist_colors.append(COLOR_GREEN)
elif edge_s < p95:
    hist_colors.append(COLOR_YELLOW)
else:
    hist_colors.append(COLOR_RED)
```

- [ ] **Step 3: Replace hardcoded colors in status code logic**

In `generate_gatling_html_report()`, replace status code color block (lines 471-478):

```python
# Before:
if 200 <= code < 300:
    sc_colors.append("rgba(76, 175, 80, 0.85)")
elif 300 <= code < 400:
    sc_colors.append("rgba(33, 150, 243, 0.85)")
elif 400 <= code < 500:
    sc_colors.append("rgba(255, 152, 0, 0.85)")
else:
    sc_colors.append("rgba(244, 67, 54, 0.85)")

# After:
if 200 <= code < 300:
    sc_colors.append(STATUS_COLOR_2XX)
elif 300 <= code < 400:
    sc_colors.append(STATUS_COLOR_3XX)
elif 400 <= code < 500:
    sc_colors.append(STATUS_COLOR_4XX)
else:
    sc_colors.append(STATUS_COLOR_5XX)
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_reporting.py -v -n 0`
Expected: All tests PASS (this is a pure refactor of string values)

- [ ] **Step 5: Lint**

Run: `ruff check src/pywrkr/reporting.py && ruff format --check src/pywrkr/reporting.py`
Expected: No issues

- [ ] **Step 6: Commit**

```bash
git add src/pywrkr/reporting.py
git commit -m "refactor: extract chart color constants in reporting"
```

---

### Task 2: Add TextIO type hints

**Files:**
- Modify: `src/pywrkr/reporting.py` (import + 6 function signatures)

- [ ] **Step 1: Add TextIO import**

At the top of `reporting.py`, add to imports:

```python
from typing import TextIO
```

- [ ] **Step 2: Update function signatures**

**Annotation-only changes** (these already have `file` param, just add type):

1. `print_latency_histogram(latencies: list[float], buckets: int = 20, file: TextIO = sys.stdout) -> None:` (line 69)
2. `print_percentiles(latencies: list[float], file: TextIO = sys.stdout) -> None:` (line 246)
3. `print_rps_timeline(timeline: list[tuple[float, int]], start: float, duration: float, file: TextIO = sys.stdout) -> None:` (line 256)
4. `print_threshold_results(results: "list[tuple[Threshold, float, bool]]", file: TextIO = sys.stdout) -> None:` (line 217)

**Parameter addition + behavior change** (these currently hardcode `out = sys.stdout`):

5. `print_multi_url_summary(results: "list[MultiUrlResult]", file: TextIO = sys.stdout) -> None:` (line 1164) — ADD `file` param to signature, replace `out = sys.stdout` on line 1166 with `out = file`
6. `print_results(...)` (line 922) — ADD `file: TextIO = sys.stdout` param to signature, replace `out = sys.stdout` on line 933 with `out = file`

- [ ] **Step 3: Run tests**

Run: `python -m pytest tests/test_reporting.py -v -n 0`
Expected: All tests PASS (existing tests already pass `file=buf` via keyword)

- [ ] **Step 4: Lint**

Run: `ruff check src/pywrkr/reporting.py && ruff format --check src/pywrkr/reporting.py`

- [ ] **Step 5: Commit**

```bash
git add src/pywrkr/reporting.py
git commit -m "refactor: add TextIO type hints to reporting print functions"
```

---

### Task 3: Reuse compute_percentiles in print_multi_url_summary

**Files:**
- Modify: `src/pywrkr/reporting.py:1180-1191`
- Test: `tests/test_reporting.py`

- [ ] **Step 1: Write the test**

Add to `tests/test_reporting.py`:

```python
from pywrkr.reporting import print_multi_url_summary
from pywrkr.multi_url import MultiUrlResult


class TestPrintMultiUrlSummary(unittest.TestCase):
    """Tests for print_multi_url_summary."""

    def test_output_contains_url_and_stats(self):
        stats = WorkerStats()
        stats.total_requests = 500
        stats.total_bytes = 25000
        stats.errors = 2
        # Latencies from 50ms to 149ms — p50 should be ~99ms, p95 ~144ms
        stats.latencies.extend([0.05 + i * 0.001 for i in range(100)])
        stats.status_codes[200] = 498
        stats.status_codes[500] = 2

        result = MultiUrlResult(
            url="http://localhost:8080/api",
            method="GET",
            stats=stats,
            duration=10.0,
            exit_code=0,
        )
        buf = io.StringIO()
        print_multi_url_summary([result], file=buf)
        output = buf.getvalue()
        self.assertIn("MULTI-URL COMPARISON", output)
        self.assertIn("localhost:8080/api", output)
        self.assertIn("500", output)  # total requests
        # Verify percentile durations appear (formatted as ms)
        self.assertIn("ms", output)  # latency values should be in ms range
```

- [ ] **Step 2: Run test to verify it passes (baseline)**

Run: `python -m pytest tests/test_reporting.py::TestPrintMultiUrlSummary -v -n 0`
Expected: PASS (this tests current behavior, not the refactor)

- [ ] **Step 3: Replace manual percentile calculation**

In `print_multi_url_summary()`, replace lines 1185-1191:

```python
# Before:
p50 = p95 = p99 = 0.0
if r.stats.latencies:
    sorted_lat = sorted(r.stats.latencies)
    n = len(sorted_lat)
    p50 = sorted_lat[min(int(math.ceil(50 / 100 * n)) - 1, n - 1)]
    p95 = sorted_lat[min(int(math.ceil(95 / 100 * n)) - 1, n - 1)]
    p99 = sorted_lat[min(int(math.ceil(99 / 100 * n)) - 1, n - 1)]

# After:
p50 = p95 = p99 = 0.0
if r.stats.latencies:
    pct_map = dict(compute_percentiles(r.stats.latencies))
    p50 = pct_map.get(50, 0.0)
    p95 = pct_map.get(95, 0.0)
    p99 = pct_map.get(99, 0.0)
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_reporting.py -v -n 0`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/pywrkr/reporting.py tests/test_reporting.py
git commit -m "refactor: reuse compute_percentiles in multi-URL summary"
```

---

## Chunk 2: DRY up export metrics

### Task 4: Consolidate OTel and Prometheus metric definitions

**Files:**
- Modify: `src/pywrkr/reporting.py:808-919` (both export functions)
- Test: `tests/test_reporting.py`

- [ ] **Step 1: Write the test for metric value resolution**

Add to `tests/test_reporting.py`:

```python
from pywrkr.reporting import _resolve_metric_value, _EXPORT_METRICS


class TestExportMetrics(unittest.TestCase):
    """Tests for shared export metric definitions."""

    def test_resolve_flat_metric(self):
        results = {"total_requests": 1000, "total_errors": 5}
        val = _resolve_metric_value(results, "total_requests", None, 1)
        self.assertEqual(val, 1000)

    def test_resolve_nested_metric(self):
        results = {"percentiles": {"p50": 0.05, "p95": 0.1}}
        val = _resolve_metric_value(results, "percentiles", "p50", 1000)
        self.assertAlmostEqual(val, 50.0)

    def test_resolve_missing_returns_zero(self):
        results = {}
        val = _resolve_metric_value(results, "total_requests", None, 1)
        self.assertEqual(val, 0)

    def test_export_metrics_list_not_empty(self):
        self.assertGreater(len(_EXPORT_METRICS), 0)

    def test_all_metrics_have_valid_type(self):
        for spec in _EXPORT_METRICS:
            self.assertIn(spec.metric_type, ("counter", "gauge"))

    def test_otel_names_are_explicit(self):
        for spec in _EXPORT_METRICS:
            self.assertTrue(spec.otel_name.startswith("pywrkr."))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_reporting.py::TestExportMetrics -v -n 0`
Expected: FAIL with `ImportError` (functions don't exist yet)

- [ ] **Step 3: Define the shared metric spec and resolver**

Add above the export functions in `reporting.py`:

```python
from typing import NamedTuple


class _MetricSpec(NamedTuple):
    """Specification for an exportable benchmark metric."""

    name_suffix: str  # used for Prometheus: "pywrkr_" + name_suffix
    otel_name: str  # explicit OTel metric name (dots, not derived)
    results_key: str
    nested_key: str | None
    multiplier: float
    metric_type: str  # "counter" or "gauge"
    description: str


_EXPORT_METRICS: list[_MetricSpec] = [
    _MetricSpec("requests_total", "pywrkr.requests.total", "total_requests", None, 1, "counter", "Total requests"),
    _MetricSpec("errors_total", "pywrkr.errors.total", "total_errors", None, 1, "counter", "Total errors"),
    _MetricSpec("requests_per_sec", "pywrkr.requests_per_sec", "requests_per_sec", None, 1, "gauge", "Requests per second"),
    _MetricSpec("transfer_bytes_per_sec", "pywrkr.transfer_bytes_per_sec", "transfer_per_sec_bytes", None, 1, "gauge", "Transfer bytes per second"),
    _MetricSpec("duration_sec", "pywrkr.duration_sec", "duration_sec", None, 1, "gauge", "Benchmark duration in seconds"),
    _MetricSpec("latency_p50_ms", "pywrkr.latency.p50", "percentiles", "p50", 1000, "gauge", "p50 latency in ms"),
    _MetricSpec("latency_p95_ms", "pywrkr.latency.p95", "percentiles", "p95", 1000, "gauge", "p95 latency in ms"),
    _MetricSpec("latency_p99_ms", "pywrkr.latency.p99", "percentiles", "p99", 1000, "gauge", "p99 latency in ms"),
    _MetricSpec("latency_mean_ms", "pywrkr.latency.mean", "latency", "mean", 1000, "gauge", "Mean latency in ms"),
    _MetricSpec("latency_max_ms", "pywrkr.latency.max", "latency", "max", 1000, "gauge", "Max latency in ms"),
]


def _resolve_metric_value(
    results: dict, results_key: str, nested_key: str | None, multiplier: float
) -> float:
    """Resolve a metric value from the results dict."""
    if nested_key is not None:
        val = results.get(results_key, {}).get(nested_key, 0)
    else:
        val = results.get(results_key, 0)
    return val * multiplier
```

- [ ] **Step 4: Refactor export_to_otel to use shared spec**

Replace the body of `export_to_otel()` (keeping the `if not OTEL_AVAILABLE` guard and `try/except`):

```python
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

        for spec in _EXPORT_METRICS:
            value = _resolve_metric_value(results, spec.results_key, spec.nested_key, spec.multiplier)
            otel_name = spec.otel_name
            if spec.metric_type == "counter":
                counter = meter.create_counter(otel_name, description=spec.description)
                counter.add(value, attributes=attributes)
            else:
                gauge = meter.create_up_down_counter(otel_name, description=spec.description)
                gauge.add(value, attributes=attributes)

        provider.force_flush()
        provider.shutdown()
    except Exception as e:
        print(f"Warning: failed to export metrics to OTel endpoint {endpoint}: {e}")
```

- [ ] **Step 5: Refactor export_to_prometheus to use shared spec**

```python
def export_to_prometheus(results: dict, endpoint: str, tags: dict[str, str]) -> None:
    """Export benchmark metrics to a Prometheus Pushgateway-compatible endpoint."""
    import urllib.error
    import urllib.request

    try:
        lines: list[str] = []
        labels_parts = [f'{k}="{v}"' for k, v in sorted(tags.items())]
        labels_str = "{" + ",".join(labels_parts) + "}" if labels_parts else ""

        for spec in _EXPORT_METRICS:
            value = _resolve_metric_value(results, spec.results_key, spec.nested_key, spec.multiplier)
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
    except Exception as e:
        print(f"Warning: failed to export metrics to Prometheus endpoint {endpoint}: {e}")
```

- [ ] **Step 6: Run tests**

Run: `python -m pytest tests/test_reporting.py -v -n 0`
Expected: All tests PASS

- [ ] **Step 7: Lint**

Run: `ruff format src/pywrkr/reporting.py tests/test_reporting.py && ruff check src/pywrkr/reporting.py tests/test_reporting.py`

- [ ] **Step 8: Commit**

```bash
git add src/pywrkr/reporting.py tests/test_reporting.py
git commit -m "refactor: consolidate export metric definitions into shared spec"
```

---

## Chunk 3: Extract HTML template

### Task 5: Create template infrastructure

**Files:**
- Create: `src/pywrkr/templates/__init__.py`
- Create: `src/pywrkr/templates/gatling_report.html`
- Modify: `pyproject.toml`

- [ ] **Step 1: Create templates package**

Create `src/pywrkr/templates/__init__.py`:

```python
"""HTML report templates for pywrkr."""
```

- [ ] **Step 2: Add package data to pyproject.toml**

In `pyproject.toml`, add after the `[tool.setuptools.packages.find]` section:

```toml
[tool.setuptools.package-data]
"pywrkr.templates" = ["*.html"]
```

- [ ] **Step 3: Commit infrastructure**

```bash
git add src/pywrkr/templates/__init__.py pyproject.toml
git commit -m "chore: add templates package for HTML report extraction"
```

---

### Task 6: Extract Gatling HTML template

**Files:**
- Create: `src/pywrkr/templates/gatling_report.html` (string.Template syntax with `$variable`)
- Modify: `src/pywrkr/reporting.py:392-794` (replace f-string with template load + substitute)
- Test: `tests/test_reporting.py`

- [ ] **Step 1: Write test for Gatling HTML report generation**

Add to `tests/test_reporting.py`:

```python
from pywrkr.reporting import generate_gatling_html_report


class TestGatlingHtmlReport(unittest.TestCase):
    """Tests for generate_gatling_html_report."""

    def _make_stats(self):
        stats = WorkerStats()
        stats.total_requests = 1000
        stats.total_bytes = 500000
        stats.errors = 10
        stats.latencies.extend([0.05 + i * 0.001 for i in range(200)])
        stats.status_codes[200] = 950
        stats.status_codes[500] = 50
        stats.rps_timeline = [(1000.0 + i, 10) for i in range(100)]
        return stats

    def test_returns_valid_html(self):
        stats = self._make_stats()
        config = BenchmarkConfig(url="http://localhost:8080/api", method="POST")
        html = generate_gatling_html_report(stats, 10.0, 4, config, start_time=1000.0)
        self.assertIn("<!DOCTYPE html>", html)
        self.assertIn("</html>", html)
        self.assertIn("pywrkr", html)

    def test_contains_chart_data(self):
        stats = self._make_stats()
        config = BenchmarkConfig(url="http://localhost:8080/", method="GET")
        html = generate_gatling_html_report(stats, 10.0, 4, config, start_time=1000.0)
        self.assertIn("histChart", html)
        self.assertIn("pctChart", html)
        self.assertIn("rpsChart", html)
        self.assertIn("scChart", html)

    def test_contains_indicators(self):
        stats = self._make_stats()
        config = BenchmarkConfig(url="http://localhost:8080/", method="GET")
        html = generate_gatling_html_report(stats, 10.0, 4, config, start_time=1000.0)
        self.assertIn("Total Requests", html)
        self.assertIn("1,000", html)
        self.assertIn("Errors", html)

    def test_escapes_html_in_url(self):
        stats = self._make_stats()
        config = BenchmarkConfig(url="http://example.com/<script>alert(1)</script>", method="GET")
        html = generate_gatling_html_report(stats, 10.0, 4, config, start_time=1000.0)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_empty_latencies(self):
        stats = WorkerStats()
        stats.total_requests = 0
        config = BenchmarkConfig(url="http://localhost/", method="GET")
        html = generate_gatling_html_report(stats, 0.0, 1, config, start_time=0.0)
        self.assertIn("<!DOCTYPE html>", html)
```

- [ ] **Step 2: Run tests to verify they pass (baseline)**

Run: `python -m pytest tests/test_reporting.py::TestGatlingHtmlReport -v -n 0`
Expected: PASS (tests current f-string implementation)

- [ ] **Step 3: Create the HTML template file**

Create `src/pywrkr/templates/gatling_report.html` by converting the f-string (lines 506-793) to `string.Template` syntax:
- Replace all `{variable}` with `$variable`
- Replace all `{{` with `{` and `}}` with `}` (literal braces in CSS/JS become actual braces)
- Replace inline conditionals with pre-computed `$placeholder` variables

The template uses these `$`-substituted variables:
- `$title`, `$method`, `$url_display`, `$mode`, `$connections`, `$timestamp`
- `$total_requests`, `$duration_display`, `$rps_display`
- `$errors_display`, `$errors_class`
- `$mean_latency`, `$p95_latency`, `$p95_class`, `$p99_latency`, `$p99_class`
- `$transfer_rate`
- `$hist_labels_json`, `$hist_counts_json`, `$hist_colors_json`
- `$pct_labels_json`, `$pct_values_json`
- `$rps_labels_json`, `$rps_values_json`
- `$sc_labels_json`, `$sc_values_json`, `$sc_colors_json`
- `$bd_labels_json`, `$bd_values_json`, `$has_breakdown_json`, `$bd_card_display`
- `$error_table_html`
- `$bd_bar_colors_json` (the 5-color array for breakdown chart)

**Note:** The full template is the HTML from the current f-string with `$`-syntax. It is ~290 lines. The implementer should copy the HTML from the current f-string, then perform the substitution transformations listed above.

**Important: `$` escaping.** Any literal `$` characters in the JavaScript/CSS must be escaped as `$$` in `string.Template` syntax. Scan the template for any `$` in JS (e.g., jQuery selectors, template literals) and escape them. The current f-string has no `$` literals, but verify after conversion.

- [ ] **Step 4: Add template loader to reporting.py**

Add a helper function near the top of `reporting.py` (after the color constants):

```python
import importlib.resources
from string import Template


def _load_template(name: str) -> Template:
    """Load an HTML template from the templates package."""
    ref = importlib.resources.files("pywrkr.templates").joinpath(name)
    return Template(ref.read_text(encoding="utf-8"))
```

- [ ] **Step 5: Refactor generate_gatling_html_report**

Replace the f-string body (lines 506-793) with:
1. Keep all the data preparation logic (lines 411-502) — histogram buckets, RPS timeline, status codes, breakdown, error rate, mode
2. Pre-compute all conditional values into strings:

```python
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

    import json as _json

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
        "bd_bar_colors_json": _json.dumps([
            COLOR_BLUE, COLOR_GREEN, COLOR_PURPLE, COLOR_ORANGE, COLOR_CYAN
        ]),
        "error_table_html": error_table_html,
    }

    template = _load_template("gatling_report.html")
    return template.safe_substitute(context)
```

- [ ] **Step 6: Run tests**

Run: `python -m pytest tests/test_reporting.py -v -n 0`
Expected: All tests PASS

- [ ] **Step 7: Run full test suite**

Run: `python -m pytest tests/ -v -n 0`
Expected: All tests PASS

- [ ] **Step 8: Lint and format**

Run: `ruff format src/pywrkr/reporting.py src/pywrkr/templates/__init__.py tests/test_reporting.py && ruff check src/pywrkr/reporting.py src/pywrkr/templates/__init__.py tests/test_reporting.py`

- [ ] **Step 9: Commit**

```bash
git add src/pywrkr/templates/gatling_report.html src/pywrkr/reporting.py tests/test_reporting.py
git commit -m "refactor: extract Gatling HTML report to string.Template file"
```

---

## Chunk 4: Final validation

### Task 7: Full validation pass

- [ ] **Step 1: Run full test suite**

Run: `python -m pytest tests/ -v`
Expected: All tests PASS

- [ ] **Step 2: Run linter on all changed files**

Run: `ruff format src/pywrkr/ tests/ && ruff check src/pywrkr/ tests/`
Expected: No issues

- [ ] **Step 3: Verify template loads from package**

Run: `python -c "from pywrkr.reporting import generate_gatling_html_report; print('Template loads OK')"`
Expected: Prints "Template loads OK"

- [ ] **Step 4: Verify no regressions in HTML output**

Run a quick smoke test generating actual HTML:
```python
python -c "
from pywrkr.config import WorkerStats, BenchmarkConfig
from pywrkr.reporting import generate_gatling_html_report
stats = WorkerStats()
stats.total_requests = 100
stats.latencies.extend([0.05 + i * 0.001 for i in range(100)])
stats.status_codes[200] = 100
config = BenchmarkConfig(url='http://test.example.com/', method='GET')
html = generate_gatling_html_report(stats, 5.0, 2, config, start_time=0.0)
assert '<!DOCTYPE html>' in html
assert 'Chart' in html
assert 'test.example.com' in html
print(f'HTML report generated: {len(html)} chars, looks good')
"
```
