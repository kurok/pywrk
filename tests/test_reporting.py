"""Tests for reporting.py formatting, percentiles, thresholds, and output functions."""

import io
import json
import os
import tempfile
import unittest

from pywrkr.config import BenchmarkConfig, Threshold, WorkerStats
from pywrkr.multi_url import MultiUrlResult
from pywrkr.reporting import (
    _EXPORT_METRICS,
    _resolve_metric_value,
    build_results_dict,
    compute_percentiles,
    evaluate_thresholds,
    format_bytes,
    format_duration,
    parse_threshold,
    print_latency_histogram,
    print_multi_url_summary,
    print_percentiles,
    print_threshold_results,
    write_csv_output,
    write_json_output,
)


class TestFormatBytes(unittest.TestCase):
    """Tests for format_bytes helper."""

    def test_bytes(self):
        self.assertEqual(format_bytes(0), "0.00B")
        self.assertEqual(format_bytes(512), "512.00B")

    def test_kilobytes(self):
        self.assertEqual(format_bytes(1024), "1.00KB")
        self.assertEqual(format_bytes(1536), "1.50KB")

    def test_megabytes(self):
        self.assertEqual(format_bytes(1024 * 1024), "1.00MB")

    def test_gigabytes(self):
        self.assertEqual(format_bytes(1024**3), "1.00GB")

    def test_terabytes(self):
        self.assertEqual(format_bytes(1024**4), "1.00TB")

    def test_negative(self):
        # Negative values should still format correctly
        result = format_bytes(-512)
        self.assertIn("512", result)
        self.assertIn("B", result)


class TestFormatDuration(unittest.TestCase):
    """Tests for format_duration helper."""

    def test_microseconds(self):
        self.assertEqual(format_duration(0.0005), "500.00us")
        self.assertEqual(format_duration(0.000001), "1.00us")

    def test_milliseconds(self):
        self.assertEqual(format_duration(0.1), "100.00ms")
        self.assertEqual(format_duration(0.999), "999.00ms")

    def test_seconds(self):
        self.assertEqual(format_duration(1.0), "1.00s")
        self.assertEqual(format_duration(60.5), "60.50s")


class TestComputePercentiles(unittest.TestCase):
    """Tests for compute_percentiles."""

    def test_empty_list(self):
        self.assertEqual(compute_percentiles([]), [])

    def test_single_value(self):
        result = compute_percentiles([0.5])
        # All percentiles should be the same single value
        for pct, val in result:
            self.assertEqual(val, 0.5)

    def test_known_distribution(self):
        # 100 values from 0.01 to 1.00
        latencies = [i / 100 for i in range(1, 101)]
        result = dict(compute_percentiles(latencies))
        # p50 should be ~0.50
        self.assertAlmostEqual(result[50], 0.50, delta=0.02)
        # p95 should be ~0.95
        self.assertAlmostEqual(result[95], 0.95, delta=0.02)
        # p99 should be ~0.99
        self.assertAlmostEqual(result[99], 0.99, delta=0.02)

    def test_returns_expected_percentiles(self):
        result = compute_percentiles([1.0, 2.0, 3.0])
        pcts = [p for p, _ in result]
        self.assertEqual(pcts, [50, 75, 90, 95, 99, 99.9, 99.99])


class TestParseThreshold(unittest.TestCase):
    """Tests for parse_threshold."""

    def test_p95_milliseconds(self):
        th = parse_threshold("p95 < 300ms")
        self.assertEqual(th.metric, "p95")
        self.assertEqual(th.operator, "<")
        self.assertAlmostEqual(th.value, 0.3)

    def test_p99_seconds(self):
        th = parse_threshold("p99 <= 1s")
        self.assertEqual(th.metric, "p99")
        self.assertEqual(th.operator, "<=")
        self.assertAlmostEqual(th.value, 1.0)

    def test_error_rate_percent(self):
        th = parse_threshold("error_rate < 5%")
        self.assertEqual(th.metric, "error_rate")
        self.assertAlmostEqual(th.value, 5.0)

    def test_rps_no_unit(self):
        th = parse_threshold("rps >= 1000")
        self.assertEqual(th.metric, "rps")
        self.assertEqual(th.operator, ">=")
        self.assertAlmostEqual(th.value, 1000.0)

    def test_microseconds(self):
        th = parse_threshold("p50 < 500us")
        self.assertAlmostEqual(th.value, 0.0005)

    def test_invalid_expression(self):
        with self.assertRaises(ValueError):
            parse_threshold("invalid threshold")

    def test_invalid_unit_for_latency(self):
        with self.assertRaises(ValueError):
            parse_threshold("p95 < 5%")

    def test_avg_latency(self):
        th = parse_threshold("avg_latency < 100ms")
        self.assertEqual(th.metric, "avg_latency")
        self.assertAlmostEqual(th.value, 0.1)


class TestEvaluateThresholds(unittest.TestCase):
    """Tests for evaluate_thresholds."""

    def _make_stats(self, latencies, errors=0, total=100):
        stats = WorkerStats()
        stats.latencies.extend(latencies)
        stats.total_requests = total
        stats.errors = errors
        return stats

    def test_passing_p95(self):
        stats = self._make_stats([0.1] * 100)
        thresholds = [Threshold(metric="p95", operator="<", value=0.5, raw_expr="p95 < 500ms")]
        results = evaluate_thresholds(thresholds, stats, 10.0)
        self.assertTrue(results[0][2])  # passed

    def test_failing_p95(self):
        stats = self._make_stats([1.0] * 100)
        thresholds = [Threshold(metric="p95", operator="<", value=0.5, raw_expr="p95 < 500ms")]
        results = evaluate_thresholds(thresholds, stats, 10.0)
        self.assertFalse(results[0][2])  # failed

    def test_error_rate(self):
        stats = self._make_stats([0.1] * 100, errors=10, total=100)
        thresholds = [
            Threshold(metric="error_rate", operator="<", value=5.0, raw_expr="error_rate < 5%")
        ]
        results = evaluate_thresholds(thresholds, stats, 10.0)
        self.assertFalse(results[0][2])  # 10% > 5%

    def test_rps_threshold(self):
        stats = self._make_stats([0.1] * 100, total=1000)
        thresholds = [Threshold(metric="rps", operator=">=", value=50.0, raw_expr="rps >= 50")]
        results = evaluate_thresholds(thresholds, stats, 10.0)
        # 1000 reqs / 10s = 100 rps >= 50
        self.assertTrue(results[0][2])


class TestPrintLatencyHistogram(unittest.TestCase):
    """Tests for print_latency_histogram."""

    def test_empty_latencies(self):
        buf = io.StringIO()
        print_latency_histogram([], file=buf)
        self.assertEqual(buf.getvalue(), "")

    def test_single_value(self):
        buf = io.StringIO()
        print_latency_histogram([0.5], file=buf)
        self.assertIn("All requests", buf.getvalue())

    def test_histogram_output(self):
        buf = io.StringIO()
        latencies = [i * 0.01 for i in range(1, 101)]
        print_latency_histogram(latencies, buckets=5, file=buf)
        output = buf.getvalue()
        self.assertIn("Latency Distribution", output)
        self.assertIn("#", output)


class TestPrintPercentiles(unittest.TestCase):
    """Tests for print_percentiles."""

    def test_output_contains_percentile_labels(self):
        buf = io.StringIO()
        latencies = [i * 0.01 for i in range(1, 101)]
        print_percentiles(latencies, file=buf)
        output = buf.getvalue()
        self.assertIn("p50", output)
        self.assertIn("p99", output)


class TestPrintThresholdResults(unittest.TestCase):
    """Tests for print_threshold_results."""

    def test_pass_and_fail_output(self):
        buf = io.StringIO()
        results = [
            (Threshold(metric="p95", operator="<", value=0.5, raw_expr="p95 < 500ms"), 0.3, True),
            (
                Threshold(metric="error_rate", operator="<", value=5.0, raw_expr="error_rate < 5%"),
                10.0,
                False,
            ),
        ]
        print_threshold_results(results, file=buf)
        output = buf.getvalue()
        self.assertIn("PASS", output)
        self.assertIn("FAIL", output)


class TestWriteCsvOutput(unittest.TestCase):
    """Tests for write_csv_output."""

    def test_csv_file_created(self):
        stats = WorkerStats()
        stats.latencies.extend([0.1, 0.2, 0.3])
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
            path = f.name
        try:
            write_csv_output(path, stats)
            self.assertTrue(os.path.exists(path))
            with open(path) as f:
                content = f.read()
            self.assertIn("percentage", content.lower())
        finally:
            os.unlink(path)


class TestWriteJsonOutput(unittest.TestCase):
    """Tests for write_json_output."""

    def test_json_file_created(self):
        data = {"total_requests": 100, "rps": 50.0}
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            write_json_output(path, data)
            self.assertTrue(os.path.exists(path))
            with open(path) as f:
                loaded = json.load(f)
            self.assertEqual(loaded["total_requests"], 100)
        finally:
            os.unlink(path)


class TestBuildResultsDict(unittest.TestCase):
    """Tests for build_results_dict."""

    def test_contains_expected_keys(self):
        stats = WorkerStats()
        stats.total_requests = 1000
        stats.total_bytes = 50000
        stats.errors = 5
        stats.latencies.extend([0.05, 0.1, 0.15])
        config = BenchmarkConfig(url="http://localhost/")
        result = build_results_dict(stats, 10.0, 4, config)
        self.assertIn("total_requests", result)
        self.assertIn("requests_per_sec", result)
        self.assertIn("latency", result)
        self.assertIn("percentiles", result)


class TestPrintMultiUrlSummary(unittest.TestCase):
    """Tests for print_multi_url_summary."""

    def test_output_contains_url_and_stats(self):
        stats = WorkerStats()
        stats.total_requests = 500
        stats.total_bytes = 25000
        stats.errors = 2
        # Latencies from 50ms to 149ms
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
        self.assertIn("ms", output)


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


if __name__ == "__main__":
    unittest.main()
