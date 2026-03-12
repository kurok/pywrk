"""Unit tests for workers.py utility functions."""

import unittest

from pywrkr.config import BenchmarkConfig, LatencyBreakdown, WorkerStats
from pywrkr.workers import (
    _build_request_headers,
    _merge_all_stats,
    aggregate_breakdowns,
    make_url,
)


class TestMakeUrl(unittest.TestCase):
    """Tests for make_url cache-buster function."""

    def test_no_random_param(self):
        url = make_url("http://example.com/api", False)
        self.assertEqual(url, "http://example.com/api")

    def test_random_param_added(self):
        url = make_url("http://example.com/api", True)
        self.assertIn("_cb=", url)
        self.assertIn("?", url)

    def test_random_param_with_existing_query(self):
        url = make_url("http://example.com/api?key=val", True)
        self.assertIn("&_cb=", url)

    def test_random_param_unique(self):
        url1 = make_url("http://example.com/api", True)
        url2 = make_url("http://example.com/api", True)
        # Should have different cache-buster values
        self.assertNotEqual(url1, url2)


class TestBuildRequestHeaders(unittest.TestCase):
    """Tests for _build_request_headers."""

    def test_default_headers(self):
        config = BenchmarkConfig(url="http://example.com/")
        headers = _build_request_headers(config)
        # Default config has no headers, so result should be empty dict
        self.assertIsInstance(headers, dict)

    def test_custom_headers(self):
        config = BenchmarkConfig(
            url="http://example.com/",
            headers={"X-Custom": "value", "Accept": "application/json"},
        )
        headers = _build_request_headers(config)
        self.assertEqual(headers["X-Custom"], "value")
        self.assertEqual(headers["Accept"], "application/json")

    def test_basic_auth(self):
        config = BenchmarkConfig(url="http://example.com/", basic_auth="user:pass")
        headers = _build_request_headers(config)
        self.assertIn("Authorization", headers)
        self.assertTrue(headers["Authorization"].startswith("Basic "))

    def test_cookies(self):
        config = BenchmarkConfig(url="http://example.com/", cookies=["session=abc", "token=xyz"])
        headers = _build_request_headers(config)
        self.assertIn("Cookie", headers)
        self.assertIn("session=abc", headers["Cookie"])
        self.assertIn("token=xyz", headers["Cookie"])


class TestMergeAllStats(unittest.TestCase):
    """Tests for _merge_all_stats."""

    def test_merge_empty(self):
        merged = _merge_all_stats([])
        self.assertEqual(merged.total_requests, 0)

    def test_merge_sums_counters(self):
        ws1 = WorkerStats()
        ws1.total_requests = 50
        ws1.errors = 2
        ws1.total_bytes = 1000

        ws2 = WorkerStats()
        ws2.total_requests = 75
        ws2.errors = 1
        ws2.total_bytes = 2000

        merged = _merge_all_stats([ws1, ws2])
        self.assertEqual(merged.total_requests, 125)
        self.assertEqual(merged.errors, 3)
        self.assertEqual(merged.total_bytes, 3000)

    def test_merge_combines_latencies(self):
        ws1 = WorkerStats()
        ws1.latencies.extend([0.1, 0.2])
        ws2 = WorkerStats()
        ws2.latencies.extend([0.3])

        merged = _merge_all_stats([ws1, ws2])
        self.assertEqual(len(merged.latencies), 3)


class TestAggregateBreakdowns(unittest.TestCase):
    """Tests for aggregate_breakdowns."""

    def test_empty(self):
        result = aggregate_breakdowns([])
        self.assertEqual(result, {})

    def test_single_breakdown(self):
        bd = LatencyBreakdown(dns=0.01, connect=0.02, tls=0.03, ttfb=0.04, transfer=0.05)
        result = aggregate_breakdowns([bd])
        self.assertAlmostEqual(result["dns"]["avg"], 0.01)
        self.assertAlmostEqual(result["connect"]["avg"], 0.02)

    def test_multiple_breakdowns(self):
        breakdowns = [
            LatencyBreakdown(dns=0.01, connect=0.02, tls=0.0, ttfb=0.04, transfer=0.05),
            LatencyBreakdown(dns=0.03, connect=0.04, tls=0.0, ttfb=0.06, transfer=0.07),
        ]
        result = aggregate_breakdowns(breakdowns)
        self.assertEqual(result["dns"]["count"], 2)
        # Average DNS should be (0.01 + 0.03) / 2 = 0.02
        self.assertAlmostEqual(result["dns"]["avg"], 0.02)

    def test_reuse_tracking(self):
        breakdowns = [
            LatencyBreakdown(is_reused=True),
            LatencyBreakdown(is_reused=True),
            LatencyBreakdown(is_reused=False),
        ]
        result = aggregate_breakdowns(breakdowns)
        self.assertEqual(result["reused_connections"], 2)
        self.assertEqual(result["new_connections"], 1)


if __name__ == "__main__":
    unittest.main()
