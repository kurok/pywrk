# Reporting Module Improvements

**Date:** 2026-03-13
**Scope:** `src/pywrkr/reporting.py` + new template file + `pyproject.toml`

## Problem

`reporting.py` is 1,234 lines. The main pain points:

- `generate_gatling_html_report()` is ~400 lines dominated by an inline HTML/CSS/JS f-string
- Magic RGBA color strings duplicated across histogram, status codes, and breakdown charts
- `file` parameters lack `TextIO` type hints across 6+ functions
- `print_multi_url_summary()` manually recomputes percentiles instead of reusing `compute_percentiles()`
- OTel and Prometheus exporters define the same metric list independently

## Changes

### 1. Extract HTML template to separate file

**Current:** Lines 506-793 are a single f-string inside `generate_gatling_html_report()`.

**Proposed:**
- Create `src/pywrkr/templates/gatling_report.html` using `string.Template` (`$variable` syntax)
- This avoids the `{{`/`}}` brace escaping problem — CSS/JS curly braces are literal, only `$var` is substituted
- `generate_gatling_html_report()` builds a context dict, then calls `template.safe_substitute(**context)`
- Template loaded via `importlib.resources.files()` (Python 3.10+) so it works from installed packages
- Update `pyproject.toml` to include `templates/*.html` as package data

**Pre-computed context variables** (~20 values the function must prepare before substitution):
- `title`, `method`, `url`, `mode`, `connections`, `timestamp` — simple strings
- `total_requests`, `duration`, `rps`, `errors`, `error_rate` — formatted indicator values
- `mean_latency`, `p95_latency`, `p99_latency`, `transfer_rate` — formatted with `format_duration()`/`format_bytes()`
- `p95_class`, `p99_class`, `errors_class` — CSS class names pre-computed from conditionals (e.g., `"red"` if errors else `"green"`)
- `hist_labels_json`, `hist_counts_json`, `hist_colors_json` — JSON-serialized chart data
- `pct_labels_json`, `pct_values_json` — percentile chart data
- `rps_labels_json`, `rps_values_json` — RPS timeline data
- `sc_labels_json`, `sc_values_json`, `sc_colors_json` — status code pie data
- `bd_labels_json`, `bd_values_json`, `has_breakdown_json` — breakdown chart data
- `bd_card_display` — pre-computed style string (`"display:block"` or `"display:none"`)
- `error_table_html` — pre-rendered HTML for the error details section (the comprehension block)

The function shrinks from ~400 lines to ~120 lines of data preparation + template load.

### 2. Add `TextIO` type hints

**Current:** Functions using `file=sys.stdout` lack type annotation on `file`.

**Proposed:** Add `from typing import TextIO` and annotate all `file` params:
- `print_latency_histogram(latencies, buckets=20, file: TextIO = sys.stdout)`
- `print_percentiles(latencies, file: TextIO = sys.stdout)`
- `print_rps_timeline(timeline, start, duration, file: TextIO = sys.stdout)`
- `print_threshold_results(results, file: TextIO = sys.stdout)`
- `print_results(stats, duration, connections, start_time, config, rate_limiter=None, file: TextIO = sys.stdout)` — note: currently hardcodes `out = sys.stdout`, change to use the `file` param
- `print_multi_url_summary(results, file: TextIO = sys.stdout)`

### 3. Extract color constants

**Current:** RGBA strings like `"rgba(76, 175, 80, 0.8)"` appear in histogram coloring, status code coloring, and breakdown chart data.

**Proposed:** Add a constants section at module top:

```python
# Chart color constants
COLOR_GREEN = "rgba(76, 175, 80, 0.8)"
COLOR_YELLOW = "rgba(255, 193, 7, 0.8)"
COLOR_RED = "rgba(244, 67, 54, 0.8)"
COLOR_BLUE = "rgba(33, 150, 243, 0.8)"
COLOR_ORANGE = "rgba(255, 152, 0, 0.8)"
COLOR_PURPLE = "rgba(156, 39, 176, 0.8)"
COLOR_CYAN = "rgba(0, 188, 212, 0.8)"

# Status code color mapping (opacity 0.85 for pie chart)
STATUS_COLOR_2XX = "rgba(76, 175, 80, 0.85)"
STATUS_COLOR_3XX = "rgba(33, 150, 243, 0.85)"
STATUS_COLOR_4XX = "rgba(255, 152, 0, 0.85)"
STATUS_COLOR_5XX = "rgba(244, 67, 54, 0.85)"
```

Note: histogram coloring logic (green/yellow/red based on p50/p95 thresholds) currently lives inside the f-string. After extraction, this logic moves to data preparation where it builds `hist_colors` list using these constants, then serializes to JSON for the template.

### 4. Reuse `compute_percentiles()` in `print_multi_url_summary()`

**Current:** Lines 1186-1191 manually compute p50/p95/p99 with inline index math.

**Proposed:** Replace with:
```python
pct_map = dict(compute_percentiles(r.stats.latencies))
p50 = pct_map.get(50, 0.0)
p95 = pct_map.get(95, 0.0)
p99 = pct_map.get(99, 0.0)
```

### 5. DRY up metric definitions in OTel/Prometheus exports

**Current:** Both `export_to_otel()` and `export_to_prometheus()` independently define which metrics to export, with the same keys, multipliers, and descriptions.

**Proposed:** Define a shared metric spec:

```python
_EXPORT_METRICS: list[tuple[str, str, str | None, float, str, str]] = [
    # (name_suffix, results_path, nested_key, multiplier, metric_type, description)
    ("requests_total", "total_requests", None, 1, "counter", "Total requests"),
    ("errors_total", "total_errors", None, 1, "counter", "Total errors"),
    ("requests_per_sec", "requests_per_sec", None, 1, "gauge", "Requests per second"),
    ("transfer_bytes_per_sec", "transfer_per_sec_bytes", None, 1, "gauge", "Transfer bytes/sec"),
    ("duration_sec", "duration_sec", None, 1, "gauge", "Benchmark duration in seconds"),
    ("latency_p50_ms", "percentiles", "p50", 1000, "gauge", "p50 latency in ms"),
    ("latency_p95_ms", "percentiles", "p95", 1000, "gauge", "p95 latency in ms"),
    ("latency_p99_ms", "percentiles", "p99", 1000, "gauge", "p99 latency in ms"),
    ("latency_mean_ms", "latency", "mean", 1000, "gauge", "Mean latency in ms"),
    ("latency_max_ms", "latency", "max", 1000, "gauge", "Max latency in ms"),
]
```

Value resolution: for each metric, `results[results_path]` is fetched. If `nested_key` is not None, do `results[results_path][nested_key]`. Multiply by `multiplier`.

**Naming convention mapping:**
- OTel: prefix `pywrkr.` + replace `_` with `.` → `pywrkr.requests.total`
- Prometheus: prefix `pywrkr_` + keep `_` → `pywrkr_requests_total`

Both exporters iterate `_EXPORT_METRICS` and apply their naming convention.

## File changes

| File | Action |
|------|--------|
| `src/pywrkr/reporting.py` | Edit: all 5 changes |
| `src/pywrkr/templates/__init__.py` | Create: empty package init |
| `src/pywrkr/templates/gatling_report.html` | Create: extracted HTML template using `string.Template` syntax |
| `pyproject.toml` | Edit: add `templates/*.html` to package data |

## Testing

- Run full test suite: `python -m pytest tests/ -v`
- **Validation strategy for template extraction:** Generate HTML report before and after the refactor with the same input data, diff the output to confirm functional equivalence (whitespace differences are acceptable)
- No new dependencies introduced (`string.Template` and `importlib.resources` are stdlib)
