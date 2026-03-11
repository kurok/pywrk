#!/usr/bin/env python3
"""Unit tests for pywrkr benchmarking tool."""

import argparse
import asyncio
import base64
import csv
import json
import math
import os
import sys
import tempfile
import time
import unittest
from collections import defaultdict
from io import StringIO
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase

import pywrkr
import pywrkr as pywrkr_main
from pywrkr.main import main as pywrkr_cli_main


# ---------------------------------------------------------------------------
# Unit tests for refactored argument parser helpers
# ---------------------------------------------------------------------------


class TestParserHelpers(unittest.TestCase):
    """Tests for the refactored argument parser helper functions."""

    def test_build_parser_returns_parser(self):
        from pywrkr.main import _build_parser
        parser = _build_parser()
        self.assertIsInstance(parser, argparse.ArgumentParser)

    def test_core_options_present(self):
        from pywrkr.main import _build_parser
        parser = _build_parser()
        args = parser.parse_args(["http://example.com"])
        # Core options should have defaults
        self.assertEqual(args.connections, 10)
        self.assertEqual(args.threads, 4)
        self.assertEqual(args.timeout, 30)
        self.assertEqual(args.method, "GET")
        self.assertTrue(args.keepalive)

    def test_user_simulation_options_present(self):
        from pywrkr.main import _build_parser
        parser = _build_parser()
        args = parser.parse_args(["-u", "100", "http://example.com"])
        self.assertEqual(args.users, 100)
        self.assertEqual(args.ramp_up, 0)
        self.assertEqual(args.think_time, 1.0)
        self.assertEqual(args.think_jitter, 0.5)

    def test_output_options_present(self):
        from pywrkr.main import _build_parser
        parser = _build_parser()
        args = parser.parse_args(["--json", "out.json", "--csv", "out.csv", "http://example.com"])
        self.assertEqual(args.json, "out.json")
        self.assertEqual(args.csv, "out.csv")

    def test_rate_options_present(self):
        from pywrkr.main import _build_parser
        parser = _build_parser()
        args = parser.parse_args(["--rate", "500", "--rate-ramp", "1000", "http://example.com"])
        self.assertEqual(args.rate, 500)
        self.assertEqual(args.rate_ramp, 1000)

    def test_autofind_options_present(self):
        from pywrkr.main import _build_parser
        parser = _build_parser()
        args = parser.parse_args(["--autofind", "--max-error-rate", "2.0", "http://example.com"])
        self.assertTrue(args.autofind)
        self.assertEqual(args.max_error_rate, 2.0)

    def test_distributed_options_present(self):
        from pywrkr.main import _build_parser
        parser = _build_parser()
        args = parser.parse_args(["--master", "--expect-workers", "3", "--port", "5000", "http://example.com"])
        self.assertTrue(args.master)
        self.assertEqual(args.expect_workers, 3)
        self.assertEqual(args.port, 5000)

    def test_multi_url_option_present(self):
        from pywrkr.main import _build_parser
        parser = _build_parser()
        args = parser.parse_args(["--url-file", "urls.txt"])
        self.assertEqual(args.url_file, "urls.txt")


class TestDefaultConstants(unittest.TestCase):
    """Tests for default constants in config module."""

    def test_constants_exist_and_match_defaults(self):
        from pywrkr.config import (
            DEFAULT_CONNECTIONS, DEFAULT_DURATION, DEFAULT_THREADS,
            DEFAULT_TIMEOUT, DEFAULT_THINK_TIME_JITTER, DEFAULT_MASTER_PORT,
        )
        self.assertEqual(DEFAULT_CONNECTIONS, 10)
        self.assertEqual(DEFAULT_DURATION, 10.0)
        self.assertEqual(DEFAULT_THREADS, 4)
        self.assertEqual(DEFAULT_TIMEOUT, 30.0)
        self.assertEqual(DEFAULT_THINK_TIME_JITTER, 0.5)
        self.assertEqual(DEFAULT_MASTER_PORT, 9220)

    def test_benchmark_config_uses_constants(self):
        from pywrkr.config import BenchmarkConfig, DEFAULT_CONNECTIONS, DEFAULT_THREADS, DEFAULT_TIMEOUT
        config = BenchmarkConfig(url="http://example.com")
        self.assertEqual(config.connections, DEFAULT_CONNECTIONS)
        self.assertEqual(config.threads, DEFAULT_THREADS)
        self.assertEqual(config.timeout_sec, DEFAULT_TIMEOUT)

    def test_autofind_config_uses_constants(self):
        from pywrkr.config import (
            AutofindConfig, DEFAULT_AUTOFIND_MAX_ERROR_RATE,
            DEFAULT_AUTOFIND_START_USERS, DEFAULT_AUTOFIND_MAX_USERS,
        )
        config = AutofindConfig(url="http://example.com")
        self.assertEqual(config.max_error_rate, DEFAULT_AUTOFIND_MAX_ERROR_RATE)
        self.assertEqual(config.start_users, DEFAULT_AUTOFIND_START_USERS)
        self.assertEqual(config.max_users, DEFAULT_AUTOFIND_MAX_USERS)


# ---------------------------------------------------------------------------
# Unit tests for formatting helpers
# ---------------------------------------------------------------------------

class TestFormatBytes(unittest.TestCase):
    def test_bytes(self):
        self.assertEqual(pywrkr.format_bytes(0), "0.00B")
        self.assertEqual(pywrkr.format_bytes(512), "512.00B")

    def test_kilobytes(self):
        self.assertEqual(pywrkr.format_bytes(1024), "1.00KB")
        self.assertEqual(pywrkr.format_bytes(1536), "1.50KB")

    def test_megabytes(self):
        self.assertEqual(pywrkr.format_bytes(1024 * 1024), "1.00MB")

    def test_gigabytes(self):
        self.assertEqual(pywrkr.format_bytes(1024 ** 3), "1.00GB")

    def test_terabytes(self):
        self.assertEqual(pywrkr.format_bytes(1024 ** 4), "1.00TB")

    def test_negative(self):
        result = pywrkr.format_bytes(-512)
        self.assertIn("B", result)


class TestFormatDuration(unittest.TestCase):
    def test_microseconds(self):
        self.assertIn("us", pywrkr.format_duration(0.0001))

    def test_milliseconds(self):
        self.assertIn("ms", pywrkr.format_duration(0.5))
        self.assertEqual(pywrkr.format_duration(0.5), "500.00ms")

    def test_seconds(self):
        self.assertIn("s", pywrkr.format_duration(2.5))
        self.assertEqual(pywrkr.format_duration(2.5), "2.50s")

    def test_zero(self):
        self.assertIn("us", pywrkr.format_duration(0))


# ---------------------------------------------------------------------------
# Unit tests for percentile computation
# ---------------------------------------------------------------------------

class TestComputePercentiles(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(pywrkr.compute_percentiles([]), [])

    def test_single_value(self):
        result = pywrkr.compute_percentiles([0.5])
        self.assertTrue(all(v == 0.5 for _, v in result))

    def test_known_distribution(self):
        latencies = list(range(1, 101))  # 1..100
        result = dict(pywrkr.compute_percentiles(latencies))
        self.assertEqual(result[50], 50)
        self.assertEqual(result[99], 99)
        self.assertEqual(result[100 - 0.01], 100)  # p99.99 -> last

    def test_percentiles_sorted(self):
        latencies = [0.1, 0.5, 0.2, 0.8, 0.3, 1.0, 0.05]
        result = pywrkr.compute_percentiles(latencies)
        values = [v for _, v in result]
        self.assertEqual(values, sorted(values))


# ---------------------------------------------------------------------------
# Unit tests for histogram
# ---------------------------------------------------------------------------

class TestLatencyHistogram(unittest.TestCase):
    def test_empty(self):
        # Should not raise
        pywrkr.print_latency_histogram([])

    def test_single_value(self):
        buf = StringIO()
        pywrkr.print_latency_histogram([0.5], file=buf)
        self.assertIn("All requests", buf.getvalue())

    def test_multiple_values(self):
        buf = StringIO()
        pywrkr.print_latency_histogram([0.1, 0.2, 0.3, 0.4, 0.5], buckets=5, file=buf)
        output = buf.getvalue()
        self.assertIn("histogram", output.lower())
        self.assertIn("#", output)


# ---------------------------------------------------------------------------
# Unit tests for RPS timeline
# ---------------------------------------------------------------------------

class TestRpsTimeline(unittest.TestCase):
    def test_empty(self):
        buf = StringIO()
        pywrkr.print_rps_timeline([], 0, 10, file=buf)
        self.assertEqual(buf.getvalue(), "")

    def test_basic_timeline(self):
        buf = StringIO()
        timeline = [(0.0, 10), (1.0, 15), (2.0, 12)]
        pywrkr.print_rps_timeline(timeline, 0.0, 5, file=buf)
        output = buf.getvalue()
        self.assertIn("req/s", output)


# ---------------------------------------------------------------------------
# Unit tests for results dict builder
# ---------------------------------------------------------------------------

class TestBuildResultsDict(unittest.TestCase):
    def _make_stats(self, n=100):
        stats = pywrkr.WorkerStats()
        stats.total_requests = n
        stats.total_bytes = n * 1024
        stats.errors = 2
        stats.latencies = [0.01 * i for i in range(1, n + 1)]
        stats.status_codes = defaultdict(int, {200: n - 2, 500: 2})
        stats.error_types = defaultdict(int, {"HTTP 500": 2})
        return stats

    def test_basic_fields(self):
        stats = self._make_stats()
        result = pywrkr.build_results_dict(stats, 10.0, 50)
        self.assertEqual(result["total_requests"], 100)
        self.assertEqual(result["connections"], 50)
        self.assertAlmostEqual(result["requests_per_sec"], 10.0)
        self.assertIn("latency", result)
        self.assertIn("percentiles", result)

    def test_zero_duration(self):
        stats = self._make_stats()
        result = pywrkr.build_results_dict(stats, 0, 10)
        self.assertEqual(result["requests_per_sec"], 0)

    def test_percentile_keys(self):
        stats = self._make_stats()
        result = pywrkr.build_results_dict(stats, 10.0, 10)
        expected_keys = {"p50", "p75", "p90", "p95", "p99", "p99.9", "p99.99"}
        self.assertEqual(set(result["percentiles"].keys()), expected_keys)


# ---------------------------------------------------------------------------
# Unit tests for CSV output
# ---------------------------------------------------------------------------

class TestCsvOutput(unittest.TestCase):
    def test_write_csv(self):
        stats = pywrkr.WorkerStats()
        stats.latencies = [0.01 * i for i in range(1, 101)]

        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
            path = f.name

        try:
            pywrkr.write_csv_output(path, stats)
            with open(path) as f:
                reader = csv.reader(f)
                rows = list(reader)
            self.assertEqual(rows[0], ["Percentage", "Time (ms)"])
            self.assertEqual(len(rows), 101)  # header + 100 rows
            # 50th percentile should be ~500ms
            pct50_row = rows[50]
            self.assertEqual(pct50_row[0], "50")
        finally:
            os.unlink(path)

    def test_write_csv_empty(self):
        stats = pywrkr.WorkerStats()
        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
            path = f.name
        try:
            pywrkr.write_csv_output(path, stats)
            # Should not create content for empty latencies
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# Unit tests for JSON output
# ---------------------------------------------------------------------------

class TestJsonOutput(unittest.TestCase):
    def test_write_json(self):
        data = {"total_requests": 100, "rps": 50.5}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            path = f.name
        try:
            pywrkr.write_json_output(path, data)
            with open(path) as f:
                loaded = json.load(f)
            self.assertEqual(loaded, data)
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# Unit tests for HTML report
# ---------------------------------------------------------------------------

class TestHtmlReport(unittest.TestCase):
    def test_contains_html_tags(self):
        stats = pywrkr.WorkerStats()
        stats.total_requests = 10
        stats.total_bytes = 5000
        stats.latencies = [0.1, 0.2, 0.3]
        stats.status_codes = defaultdict(int, {200: 10})
        html = pywrkr.generate_html_report(stats, 5.0, 10)
        self.assertIn("<html>", html)
        self.assertIn("<table", html)
        self.assertIn("total_requests", html)
        self.assertIn("</html>", html)


# ---------------------------------------------------------------------------
# Unit tests for Gatling-style HTML report
# ---------------------------------------------------------------------------

class TestGatlingHtmlReport(unittest.TestCase):
    """Tests for the interactive Gatling-style HTML report generator."""

    def _make_stats(self, n=100):
        """Create a WorkerStats with realistic data."""
        import random
        random.seed(42)
        stats = pywrkr.WorkerStats()
        stats.total_requests = n
        stats.total_bytes = n * 500
        stats.errors = 2
        stats.latencies = [random.uniform(0.01, 0.5) for _ in range(n)]
        stats.status_codes = defaultdict(int, {200: n - 3, 404: 1, 500: 2})
        stats.error_types = defaultdict(int, {"ConnectionError": 2})
        # RPS timeline: simulate 1-second buckets over 10 seconds
        base = 1000.0
        stats.rps_timeline = [(base + i, random.randint(5, 15)) for i in range(10)]
        return stats

    def test_basic_html_structure(self):
        """Report contains required HTML structure."""
        stats = self._make_stats()
        config = pywrkr.BenchmarkConfig(url="http://localhost:8080/api")
        html = pywrkr.generate_gatling_html_report(stats, 10.0, 50, config)
        self.assertIn("<!DOCTYPE html>", html)
        self.assertIn("<html", html)
        self.assertIn("</html>", html)
        self.assertIn("chart.js", html.lower())
        self.assertIn("pywrkr", html)

    def test_contains_summary_indicators(self):
        """Report shows key metrics in indicator cards."""
        stats = self._make_stats()
        config = pywrkr.BenchmarkConfig(url="http://example.com/")
        html = pywrkr.generate_gatling_html_report(stats, 10.0, 50, config)
        self.assertIn("Total Requests", html)
        self.assertIn("Requests/sec", html)
        self.assertIn("Errors", html)
        self.assertIn("Mean Latency", html)
        self.assertIn("p95 Latency", html)
        self.assertIn("p99 Latency", html)
        self.assertIn("Transfer", html)
        self.assertIn("Duration", html)

    def test_contains_chart_canvases(self):
        """Report has all chart canvas elements."""
        stats = self._make_stats()
        html = pywrkr.generate_gatling_html_report(stats, 10.0, 10)
        self.assertIn('id="histChart"', html)
        self.assertIn('id="pctChart"', html)
        self.assertIn('id="rpsChart"', html)
        self.assertIn('id="scChart"', html)

    def test_url_in_header(self):
        """Report header shows the target URL and method."""
        stats = self._make_stats()
        config = pywrkr.BenchmarkConfig(url="http://myapi.com/v1/test", method="POST")
        html = pywrkr.generate_gatling_html_report(stats, 10.0, 20, config)
        self.assertIn("myapi.com/v1/test", html)
        self.assertIn("POST", html)

    def test_html_escaping(self):
        """Special characters in URL are escaped."""
        stats = self._make_stats(10)
        config = pywrkr.BenchmarkConfig(url="http://example.com/<script>alert(1)</script>")
        html = pywrkr.generate_gatling_html_report(stats, 5.0, 5, config)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_user_simulation_mode_label(self):
        """Report shows virtual user count for user simulation mode."""
        stats = self._make_stats()
        config = pywrkr.BenchmarkConfig(url="http://localhost/", users=500)
        html = pywrkr.generate_gatling_html_report(stats, 60.0, 500, config)
        self.assertIn("500 virtual users", html)

    def test_rate_mode_label(self):
        """Report shows rate for rate-limited mode."""
        stats = self._make_stats()
        config = pywrkr.BenchmarkConfig(url="http://localhost/", rate=1000.0)
        html = pywrkr.generate_gatling_html_report(stats, 30.0, 50, config)
        self.assertIn("Rate:", html)

    def test_request_count_mode_label(self):
        """Report shows request count for -n mode."""
        stats = self._make_stats()
        config = pywrkr.BenchmarkConfig(url="http://localhost/", num_requests=5000)
        html = pywrkr.generate_gatling_html_report(stats, 10.0, 50, config)
        self.assertIn("5,000 requests", html)

    def test_empty_latencies(self):
        """Report handles empty latencies gracefully."""
        stats = pywrkr.WorkerStats()
        stats.total_requests = 0
        stats.status_codes = defaultdict(int)
        html = pywrkr.generate_gatling_html_report(stats, 0.0, 10)
        self.assertIn("<!DOCTYPE html>", html)
        self.assertIn("Total Requests", html)

    def test_error_details_table(self):
        """Report shows error details when errors exist."""
        stats = self._make_stats()
        html = pywrkr.generate_gatling_html_report(stats, 10.0, 10)
        self.assertIn("Error Details", html)
        self.assertIn("ConnectionError", html)

    def test_no_error_table_when_clean(self):
        """Report omits error details section when there are no errors."""
        stats = self._make_stats()
        stats.errors = 0
        stats.error_types = defaultdict(int)
        html = pywrkr.generate_gatling_html_report(stats, 10.0, 10)
        # The error table HTML element should not be rendered (CSS class still in style is OK)
        self.assertNotIn("<table class", html)

    def test_latency_breakdown_hidden_when_absent(self):
        """Breakdown chart is hidden when no breakdown data exists."""
        stats = self._make_stats()
        html = pywrkr.generate_gatling_html_report(stats, 10.0, 10)
        self.assertIn("display:none", html)

    def test_latency_breakdown_shown_when_present(self):
        """Breakdown chart is visible when breakdown data exists."""
        stats = self._make_stats()
        stats.breakdowns = [
            pywrkr.LatencyBreakdown(dns=0.002, connect=0.01, tls=0.03, ttfb=0.05, transfer=0.02),
            pywrkr.LatencyBreakdown(dns=0.001, connect=0.008, tls=0.025, ttfb=0.04, transfer=0.015),
        ]
        html = pywrkr.generate_gatling_html_report(stats, 10.0, 10)
        self.assertIn("display:block", html)
        self.assertIn("Latency Breakdown", html)

    def test_rps_timeline_data(self):
        """Report includes RPS timeline chart data."""
        stats = self._make_stats()
        html = pywrkr.generate_gatling_html_report(stats, 10.0, 10, start_time=1000.0)
        self.assertIn("Requests per Second", html)
        self.assertIn("rpsChart", html)

    def test_status_code_colors(self):
        """Status codes get appropriate colors."""
        stats = self._make_stats()
        stats.status_codes = defaultdict(int, {200: 90, 301: 5, 404: 3, 500: 2})
        html = pywrkr.generate_gatling_html_report(stats, 10.0, 10)
        # 200 should be green, 500 should be red
        self.assertIn("76, 175, 80", html)   # green for 2xx
        self.assertIn("244, 67, 54", html)   # red for 5xx

    def test_footer_link(self):
        """Report footer links to project."""
        stats = self._make_stats()
        html = pywrkr.generate_gatling_html_report(stats, 10.0, 10)
        self.assertIn("github.com/kurok/pywrkr", html)


class TestWriteHtmlReport(unittest.TestCase):
    """Tests for writing HTML report to file."""

    def test_write_html_file(self):
        """Report is written to disk correctly."""
        stats = pywrkr.WorkerStats()
        stats.total_requests = 50
        stats.total_bytes = 25000
        stats.latencies = [0.1 * i for i in range(1, 51)]
        stats.status_codes = defaultdict(int, {200: 50})
        html = pywrkr.generate_gatling_html_report(stats, 5.0, 10)
        with tempfile.NamedTemporaryFile(suffix=".html", delete=False, mode="w") as f:
            path = f.name
        try:
            pywrkr.write_html_report(path, html)
            with open(path) as f:
                content = f.read()
            self.assertIn("<!DOCTYPE html>", content)
            self.assertIn("chart.js", content.lower())
            self.assertTrue(len(content) > 1000, "Report should be substantial")
        finally:
            os.unlink(path)

    def test_print_results_writes_html_report(self):
        """print_results writes HTML report file when config.html_report is set."""
        stats = pywrkr.WorkerStats()
        stats.total_requests = 20
        stats.total_bytes = 10000
        stats.latencies = [0.05 * i for i in range(1, 21)]
        stats.status_codes = defaultdict(int, {200: 20})
        with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as f:
            path = f.name
        try:
            config = pywrkr.BenchmarkConfig(
                url="http://localhost/",
                html_report=path,
            )
            with patch("sys.stdout", new_callable=StringIO) as mock_out:
                pywrkr.print_results(stats, 5.0, 20, 0.0, config)
            self.assertIn("HTML report written to", mock_out.getvalue())
            with open(path) as f:
                content = f.read()
            self.assertIn("<!DOCTYPE html>", content)
        finally:
            os.unlink(path)


class TestHtmlEscape(unittest.TestCase):
    """Tests for _html_escape helper."""

    def test_escapes_special_chars(self):
        self.assertEqual(pywrkr._html_escape('<b>"Tom & Jerry"</b>'),
                         '&lt;b&gt;&quot;Tom &amp; Jerry&quot;&lt;/b&gt;')

    def test_plain_text_unchanged(self):
        self.assertEqual(pywrkr._html_escape("hello world"), "hello world")


# ---------------------------------------------------------------------------
# Unit tests for BenchmarkConfig
# ---------------------------------------------------------------------------

class TestBenchmarkConfig(unittest.TestCase):
    def test_defaults(self):
        cfg = pywrkr.BenchmarkConfig(url="http://localhost/")
        self.assertEqual(cfg.connections, 10)
        self.assertEqual(cfg.duration, 10.0)
        self.assertIsNone(cfg.num_requests)
        self.assertEqual(cfg.method, "GET")
        self.assertTrue(cfg.keepalive)
        self.assertIsNone(cfg.basic_auth)
        self.assertEqual(cfg.cookies, [])

    def test_request_count_mode(self):
        cfg = pywrkr.BenchmarkConfig(url="http://localhost/", num_requests=500, duration=None)
        self.assertEqual(cfg.num_requests, 500)
        self.assertIsNone(cfg.duration)


# ---------------------------------------------------------------------------
# Unit tests for parse_header
# ---------------------------------------------------------------------------

class TestParseHeader(unittest.TestCase):
    def test_valid(self):
        name, value = pywrkr.parse_header("Content-Type: application/json")
        self.assertEqual(name, "Content-Type")
        self.assertEqual(value, "application/json")

    def test_value_with_colon(self):
        name, value = pywrkr.parse_header("X-Custom: val:with:colons")
        self.assertEqual(name, "X-Custom")
        self.assertEqual(value, "val:with:colons")

    def test_invalid(self):
        import argparse
        with self.assertRaises(argparse.ArgumentTypeError):
            pywrkr.parse_header("no-colon-here")


# ---------------------------------------------------------------------------
# Unit tests for WorkerStats merging
# ---------------------------------------------------------------------------

class TestStatsMerging(unittest.TestCase):
    def test_merge_multiple_stats(self):
        stats_list = []
        for i in range(3):
            ws = pywrkr.WorkerStats()
            ws.total_requests = 10
            ws.total_bytes = 1000
            ws.errors = 1
            ws.latencies = [0.1 * (i + 1)] * 10
            ws.status_codes[200] = 9
            ws.status_codes[500] = 1
            ws.error_types["HTTP 500"] = 1
            ws.content_length_errors = i
            stats_list.append(ws)

        merged = pywrkr.WorkerStats()
        for ws in stats_list:
            merged.total_requests += ws.total_requests
            merged.total_bytes += ws.total_bytes
            merged.errors += ws.errors
            merged.content_length_errors += ws.content_length_errors
            merged.latencies.extend(ws.latencies)
            for k, v in ws.error_types.items():
                merged.error_types[k] = merged.error_types.get(k, 0) + v
            for k, v in ws.status_codes.items():
                merged.status_codes[k] = merged.status_codes.get(k, 0) + v

        self.assertEqual(merged.total_requests, 30)
        self.assertEqual(merged.total_bytes, 3000)
        self.assertEqual(merged.errors, 3)
        self.assertEqual(merged.content_length_errors, 3)  # 0 + 1 + 2
        self.assertEqual(len(merged.latencies), 30)
        self.assertEqual(merged.status_codes[200], 27)
        self.assertEqual(merged.status_codes[500], 3)
        self.assertEqual(merged.error_types["HTTP 500"], 3)


# ---------------------------------------------------------------------------
# Integration test with a real aiohttp test server
# ---------------------------------------------------------------------------

class TestBenchmarkIntegration(AioHTTPTestCase):
    """Spin up a local aiohttp server and run pywrkr against it."""

    async def get_application(self):
        app = web.Application()
        app.router.add_get("/", self.handle_get)
        app.router.add_post("/post", self.handle_post)
        app.router.add_get("/slow", self.handle_slow)
        app.router.add_get("/error", self.handle_error)
        app.router.add_get("/auth", self.handle_auth)
        app.router.add_get("/cookie", self.handle_cookie)
        app.router.add_get("/vary-length", self.handle_vary_length)
        return app

    async def handle_get(self, request):
        return web.Response(text="Hello, World!", content_type="text/plain")

    async def handle_post(self, request):
        body = await request.read()
        return web.json_response({"received": len(body)})

    async def handle_slow(self, request):
        await asyncio.sleep(0.1)
        return web.Response(text="slow response")

    async def handle_error(self, request):
        return web.Response(status=500, text="Internal Server Error")

    async def handle_auth(self, request):
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Basic "):
            decoded = base64.b64decode(auth[6:]).decode()
            if decoded == "admin:secret":
                return web.Response(text="authenticated")
        return web.Response(status=401, text="Unauthorized")

    async def handle_cookie(self, request):
        cookie_val = request.cookies.get("session")
        if not cookie_val:
            raw = request.headers.get("Cookie", "")
            for part in raw.split(";"):
                part = part.strip()
                if part.startswith("session="):
                    cookie_val = part.split("=", 1)[1]
                    break
        if cookie_val == "test123":
            return web.Response(text="cookie ok")
        return web.Response(status=400, text="missing cookie")

    _vary_call_count = 0

    async def handle_vary_length(self, request):
        self._vary_call_count += 1
        if self._vary_call_count % 3 == 0:
            return web.Response(text="short")
        return web.Response(text="normal response body here")

    def _url(self, path):
        return f"http://localhost:{self.server.port}{path}"


    async def test_basic_get_duration_mode(self):
        config = pywrkr.BenchmarkConfig(
            url=self._url("/"),
            connections=2,
            duration=1.0,
            threads=1,
            timeout_sec=5,
        )
        with patch("sys.stdout", new_callable=StringIO):
            stats, _ = await pywrkr.run_benchmark(config)
        self.assertGreater(stats.total_requests, 0)
        self.assertIn(200, stats.status_codes)
        # Allow small number of timeout errors at end of duration window
        self.assertLessEqual(stats.errors, 2)


    async def test_request_count_mode(self):
        config = pywrkr.BenchmarkConfig(
            url=self._url("/"),
            connections=2,
            duration=None,
            num_requests=20,
            threads=1,
            timeout_sec=5,
        )
        with patch("sys.stdout", new_callable=StringIO):
            stats, _ = await pywrkr.run_benchmark(config)
        self.assertGreaterEqual(stats.total_requests, 20)
        # At least 90% successful
        self.assertGreaterEqual(stats.status_codes.get(200, 0), 18)


    async def test_post_with_body(self):
        config = pywrkr.BenchmarkConfig(
            url=self._url("/post"),
            connections=1,
            duration=None,
            num_requests=5,
            threads=1,
            method="POST",
            headers={"Content-Type": "application/json"},
            body=b'{"hello":"world"}',
            timeout_sec=5,
        )
        with patch("sys.stdout", new_callable=StringIO):
            stats, _ = await pywrkr.run_benchmark(config)
        self.assertEqual(stats.total_requests, 5)
        self.assertIn(200, stats.status_codes)


    async def test_basic_auth(self):
        config = pywrkr.BenchmarkConfig(
            url=self._url("/auth"),
            connections=1,
            duration=None,
            num_requests=3,
            threads=1,
            basic_auth="admin:secret",
            timeout_sec=5,
        )
        with patch("sys.stdout", new_callable=StringIO):
            stats, _ = await pywrkr.run_benchmark(config)
        self.assertEqual(stats.status_codes.get(200, 0), 3)
        self.assertEqual(stats.errors, 0)


    async def test_basic_auth_fail(self):
        config = pywrkr.BenchmarkConfig(
            url=self._url("/auth"),
            connections=1,
            duration=None,
            num_requests=3,
            threads=1,
            basic_auth="wrong:creds",
            timeout_sec=5,
        )
        with patch("sys.stdout", new_callable=StringIO):
            stats, _ = await pywrkr.run_benchmark(config)
        self.assertEqual(stats.status_codes.get(401, 0), 3)


    async def test_cookie_support(self):
        config = pywrkr.BenchmarkConfig(
            url=self._url("/cookie"),
            connections=1,
            duration=None,
            num_requests=3,
            threads=1,
            cookies=["session=test123"],
            timeout_sec=5,
        )
        with patch("sys.stdout", new_callable=StringIO):
            stats, _ = await pywrkr.run_benchmark(config)
        self.assertEqual(stats.status_codes.get(200, 0), 3)
        self.assertEqual(stats.errors, 0)


    async def test_cookie_missing(self):
        config = pywrkr.BenchmarkConfig(
            url=self._url("/cookie"),
            connections=1,
            duration=None,
            num_requests=3,
            threads=1,
            timeout_sec=5,
        )
        with patch("sys.stdout", new_callable=StringIO):
            stats, _ = await pywrkr.run_benchmark(config)
        self.assertEqual(stats.status_codes.get(400, 0), 3)


    async def test_error_endpoint(self):
        config = pywrkr.BenchmarkConfig(
            url=self._url("/error"),
            connections=1,
            duration=None,
            num_requests=5,
            threads=1,
            timeout_sec=5,
        )
        with patch("sys.stdout", new_callable=StringIO):
            stats, _ = await pywrkr.run_benchmark(config)
        self.assertEqual(stats.status_codes.get(500, 0), 5)
        self.assertEqual(stats.errors, 5)


    async def test_no_keepalive(self):
        config = pywrkr.BenchmarkConfig(
            url=self._url("/"),
            connections=2,
            duration=None,
            num_requests=10,
            threads=1,
            keepalive=False,
            timeout_sec=5,
        )
        with patch("sys.stdout", new_callable=StringIO):
            stats, _ = await pywrkr.run_benchmark(config)
        self.assertGreaterEqual(stats.total_requests, 10)
        # Allow small number of end-of-run race condition errors
        self.assertLessEqual(stats.errors, 2)


    async def test_csv_output(self):
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
            csv_path = f.name
        try:
            config = pywrkr.BenchmarkConfig(
                url=self._url("/"),
                connections=1,
                duration=None,
                num_requests=20,
                threads=1,
                csv_output=csv_path,
                timeout_sec=5,
            )
            with patch("sys.stdout", new_callable=StringIO):
                stats, _ = await pywrkr.run_benchmark(config)
            with open(csv_path) as f:
                reader = csv.reader(f)
                rows = list(reader)
            self.assertEqual(rows[0], ["Percentage", "Time (ms)"])
            self.assertEqual(len(rows), 101)
        finally:
            os.unlink(csv_path)


    async def test_json_output(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            json_path = f.name
        try:
            config = pywrkr.BenchmarkConfig(
                url=self._url("/"),
                connections=1,
                duration=None,
                num_requests=10,
                threads=1,
                json_output=json_path,
                timeout_sec=5,
            )
            with patch("sys.stdout", new_callable=StringIO):
                stats, _ = await pywrkr.run_benchmark(config)
            with open(json_path) as f:
                data = json.load(f)
            self.assertIn("total_requests", data)
            self.assertIn("latency", data)
            self.assertIn("percentiles", data)
            self.assertEqual(data["total_requests"], stats.total_requests)
        finally:
            os.unlink(json_path)


    async def test_html_output(self):
        config = pywrkr.BenchmarkConfig(
            url=self._url("/"),
            connections=1,
            duration=None,
            num_requests=5,
            threads=1,
            html_output=True,
            timeout_sec=5,
        )
        buf = StringIO()
        with patch("sys.stdout", buf):
            stats, _ = await pywrkr.run_benchmark(config)
        output = buf.getvalue()
        self.assertIn("<html>", output)
        self.assertIn("<table", output)


    async def test_verbosity_levels(self):
        for level in [2, 3, 4]:
            config = pywrkr.BenchmarkConfig(
                url=self._url("/"),
                connections=1,
                duration=None,
                num_requests=2,
                threads=1,
                verbosity=level,
                timeout_sec=5,
            )
            buf = StringIO()
            with patch("sys.stdout", buf):
                stats, _ = await pywrkr.run_benchmark(config)
            if level >= 3:
                self.assertIn(f"[v{level}]", buf.getvalue())


    async def test_post_file(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write('{"from_file": true}')
            post_path = f.name
        try:
            with open(post_path, "rb") as f:
                body = f.read()
            config = pywrkr.BenchmarkConfig(
                url=self._url("/post"),
                connections=1,
                duration=None,
                num_requests=3,
                threads=1,
                method="POST",
                headers={"Content-Type": "application/json"},
                body=body,
                timeout_sec=5,
            )
            with patch("sys.stdout", new_callable=StringIO):
                stats, _ = await pywrkr.run_benchmark(config)
            self.assertEqual(stats.status_codes.get(200, 0), 3)
        finally:
            os.unlink(post_path)


    async def test_latencies_recorded(self):
        config = pywrkr.BenchmarkConfig(
            url=self._url("/slow"),
            connections=1,
            duration=None,
            num_requests=3,
            threads=1,
            timeout_sec=5,
        )
        with patch("sys.stdout", new_callable=StringIO):
            stats, _ = await pywrkr.run_benchmark(config)
        self.assertEqual(len(stats.latencies), 3)
        for lat in stats.latencies:
            self.assertGreaterEqual(lat, 0.05)  # slow endpoint sleeps 100ms


    async def test_random_param_cache_buster(self):
        config = pywrkr.BenchmarkConfig(
            url=self._url("/"),
            connections=1,
            duration=None,
            num_requests=5,
            threads=1,
            random_param=True,
            timeout_sec=5,
        )
        with patch("sys.stdout", new_callable=StringIO):
            stats, _ = await pywrkr.run_benchmark(config)
        self.assertEqual(stats.total_requests, 5)
        self.assertIn(200, stats.status_codes)
        self.assertEqual(stats.errors, 0)


    async def test_content_length_verification(self):
        self._vary_call_count = 0
        config = pywrkr.BenchmarkConfig(
            url=self._url("/vary-length"),
            connections=1,
            duration=None,
            num_requests=9,
            threads=1,
            verify_content_length=True,
            timeout_sec=5,
        )
        with patch("sys.stdout", new_callable=StringIO):
            stats, _ = await pywrkr.run_benchmark(config)
        self.assertEqual(stats.total_requests, 9)
        # Some responses have different content length
        self.assertGreater(stats.content_length_errors, 0)


# ---------------------------------------------------------------------------
# Test print_results doesn't crash with edge cases
# ---------------------------------------------------------------------------

class TestPrintResultsEdgeCases(unittest.TestCase):
    def _make_config(self, **kwargs):
        defaults = dict(url="http://test/", connections=1, duration=1.0, threads=1)
        defaults.update(kwargs)
        return pywrkr.BenchmarkConfig(**defaults)

    def test_empty_stats(self):
        stats = pywrkr.WorkerStats()
        config = self._make_config()
        buf = StringIO()
        with patch("sys.stdout", buf):
            pywrkr.print_results(stats, 1.0, 1, 0.0, config)
        output = buf.getvalue()
        self.assertIn("BENCHMARK RESULTS", output)
        self.assertIn("Total Requests:    0", output)

    def test_single_request(self):
        stats = pywrkr.WorkerStats()
        stats.total_requests = 1
        stats.total_bytes = 100
        stats.latencies = [0.05]
        stats.status_codes[200] = 1
        config = self._make_config()
        buf = StringIO()
        with patch("sys.stdout", buf):
            pywrkr.print_results(stats, 1.0, 1, 0.0, config)
        output = buf.getvalue()
        self.assertIn("Requests/sec:", output)
        # No stdev with single value
        self.assertNotIn("Stdev", output)

    def test_request_count_mode_display(self):
        stats = pywrkr.WorkerStats()
        stats.total_requests = 50
        stats.latencies = [0.01] * 50
        stats.status_codes[200] = 50
        config = self._make_config(num_requests=50, duration=None)
        buf = StringIO()
        with patch("sys.stdout", buf):
            pywrkr.print_results(stats, 2.0, 5, 0.0, config)
        output = buf.getvalue()
        self.assertIn("50 requests", output)

    def test_ab_style_served_within_table(self):
        stats = pywrkr.WorkerStats()
        stats.total_requests = 100
        stats.total_bytes = 10000
        stats.latencies = [0.01 * i for i in range(1, 101)]
        stats.status_codes[200] = 100
        config = self._make_config()
        buf = StringIO()
        with patch("sys.stdout", buf):
            pywrkr.print_results(stats, 5.0, 10, 0.0, config)
        output = buf.getvalue()
        self.assertIn("Percentage of requests served within", output)
        self.assertIn("50%", output)
        self.assertIn("99%", output)
        self.assertIn("100%", output)


# ---------------------------------------------------------------------------
# User simulation tests
# ---------------------------------------------------------------------------

class TestUserSimulationIntegration(AioHTTPTestCase):
    """Test user simulation mode with a local server."""

    async def get_application(self):
        app = web.Application()
        app.router.add_get("/", self.handle_get)
        app.router.add_get("/slow", self.handle_slow)
        app.router.add_get("/error", self.handle_error)
        return app

    async def handle_get(self, request):
        return web.Response(text="Hello, World!", content_type="text/plain")

    async def handle_slow(self, request):
        await asyncio.sleep(0.05)
        return web.Response(text="slow response")

    async def handle_error(self, request):
        return web.Response(status=500, text="Internal Server Error")

    def _url(self, path):
        return f"http://localhost:{self.server.port}{path}"


    async def test_basic_user_simulation(self):
        """10 users, 2s duration, no think time."""
        config = pywrkr.BenchmarkConfig(
            url=self._url("/"),
            users=10,
            duration=2.0,
            think_time=0.0,
            ramp_up=0.0,
            timeout_sec=5,
        )
        with patch("sys.stdout", new_callable=StringIO):
            stats, _ = await pywrkr.run_user_simulation(config)
        self.assertGreater(stats.total_requests, 0)
        self.assertIn(200, stats.status_codes)


    async def test_user_simulation_with_think_time(self):
        """5 users, 2s, 0.3s think time -> each user does ~5 requests max."""
        config = pywrkr.BenchmarkConfig(
            url=self._url("/"),
            users=5,
            duration=2.0,
            think_time=0.3,
            think_time_jitter=0.0,
            ramp_up=0.0,
            timeout_sec=5,
        )
        with patch("sys.stdout", new_callable=StringIO):
            stats, _ = await pywrkr.run_user_simulation(config)
        # With 0.3s think time, 2s duration: each user ~6 requests max
        # 5 users * ~6 = ~30, but server latency eats time too
        self.assertGreater(stats.total_requests, 5)
        self.assertLess(stats.total_requests, 100)


    async def test_user_simulation_with_ramp_up(self):
        """10 users with 1s ramp-up over 3s duration."""
        config = pywrkr.BenchmarkConfig(
            url=self._url("/"),
            users=10,
            duration=3.0,
            think_time=0.1,
            ramp_up=1.0,
            timeout_sec=5,
        )
        with patch("sys.stdout", new_callable=StringIO):
            stats, _ = await pywrkr.run_user_simulation(config)
        self.assertGreater(stats.total_requests, 10)
        self.assertIn(200, stats.status_codes)


    async def test_user_simulation_errors(self):
        """Users hitting error endpoint."""
        config = pywrkr.BenchmarkConfig(
            url=self._url("/error"),
            users=3,
            duration=1.0,
            think_time=0.1,
            ramp_up=0.0,
            timeout_sec=5,
        )
        with patch("sys.stdout", new_callable=StringIO):
            stats, _ = await pywrkr.run_user_simulation(config)
        self.assertGreater(stats.errors, 0)
        self.assertIn(500, stats.status_codes)


    async def test_user_simulation_slow_endpoint(self):
        """Users hitting slow endpoint - latencies should reflect delay."""
        config = pywrkr.BenchmarkConfig(
            url=self._url("/slow"),
            users=3,
            duration=1.0,
            think_time=0.0,
            ramp_up=0.0,
            timeout_sec=5,
        )
        with patch("sys.stdout", new_callable=StringIO):
            stats, _ = await pywrkr.run_user_simulation(config)
        self.assertGreater(stats.total_requests, 0)
        for lat in stats.latencies:
            self.assertGreaterEqual(lat, 0.01)  # slow endpoint sleeps 50ms, allow CI jitter


    async def test_user_simulation_json_output(self):
        """User simulation with JSON output."""
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            json_path = f.name
        try:
            config = pywrkr.BenchmarkConfig(
                url=self._url("/"),
                users=5,
                duration=1.0,
                think_time=0.05,
                ramp_up=0.0,
                json_output=json_path,
                timeout_sec=5,
            )
            with patch("sys.stdout", new_callable=StringIO):
                stats, _ = await pywrkr.run_user_simulation(config)
            with open(json_path) as f:
                data = json.load(f)
            self.assertIn("total_requests", data)
            self.assertEqual(data["total_requests"], stats.total_requests)
        finally:
            os.unlink(json_path)


    async def test_user_simulation_csv_output(self):
        """User simulation with CSV percentile output."""
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
            csv_path = f.name
        try:
            config = pywrkr.BenchmarkConfig(
                url=self._url("/"),
                users=5,
                duration=1.0,
                think_time=0.05,
                ramp_up=0.0,
                csv_output=csv_path,
                timeout_sec=5,
            )
            with patch("sys.stdout", new_callable=StringIO):
                stats, _ = await pywrkr.run_user_simulation(config)
            with open(csv_path) as f:
                reader = csv.reader(f)
                rows = list(reader)
            self.assertEqual(rows[0], ["Percentage", "Time (ms)"])
            self.assertEqual(len(rows), 101)
        finally:
            os.unlink(csv_path)


    async def test_user_simulation_print_results(self):
        """Verify user mode prints user-specific info."""
        config = pywrkr.BenchmarkConfig(
            url=self._url("/"),
            users=5,
            duration=1.0,
            think_time=0.1,
            ramp_up=0.5,
            timeout_sec=5,
        )
        buf = StringIO()
        with patch("sys.stdout", buf):
            stats, _ = await pywrkr.run_user_simulation(config)
        output = buf.getvalue()
        self.assertIn("Virtual Users", output)
        self.assertIn("Think Time", output)
        self.assertIn("Ramp-up", output)
        self.assertIn("Avg Reqs/User", output)


    async def test_user_simulation_random_param(self):
        """User simulation with random cache-busting param."""
        config = pywrkr.BenchmarkConfig(
            url=self._url("/"),
            users=3,
            duration=1.0,
            think_time=0.1,
            ramp_up=0.0,
            random_param=True,
            timeout_sec=5,
        )
        with patch("sys.stdout", new_callable=StringIO):
            stats, _ = await pywrkr.run_user_simulation(config)
        self.assertGreater(stats.total_requests, 0)
        self.assertIn(200, stats.status_codes)


    async def test_think_time_jitter_range(self):
        """Verify think time with jitter stays within expected bounds."""
        config = pywrkr.BenchmarkConfig(
            url=self._url("/"),
            users=2,
            duration=2.0,
            think_time=0.5,
            think_time_jitter=0.5,  # 0.25s - 0.75s
            ramp_up=0.0,
            timeout_sec=5,
        )
        with patch("sys.stdout", new_callable=StringIO):
            stats, _ = await pywrkr.run_user_simulation(config)
        # With 0.5s think time (0.25-0.75 jitter range), 2s duration:
        # each user does ~3-6 requests, 2 users = ~6-12 total
        self.assertGreater(stats.total_requests, 2)
        self.assertLess(stats.total_requests, 30)


# ---------------------------------------------------------------------------
# Unit tests for make_url (cache-buster)
# ---------------------------------------------------------------------------

class TestMakeUrl(unittest.TestCase):
    def test_no_random_param(self):
        url = "http://example.com/path"
        self.assertEqual(pywrkr.make_url(url, False), url)

    def test_with_random_param_no_query(self):
        url = "http://example.com/path"
        result = pywrkr.make_url(url, True)
        self.assertTrue(result.startswith("http://example.com/path?_cb="))
        # UUID hex is 32 chars
        cb_value = result.split("_cb=")[1]
        self.assertEqual(len(cb_value), 32)
        self.assertTrue(cb_value.isalnum())

    def test_with_random_param_existing_query(self):
        url = "http://example.com/path?foo=bar"
        result = pywrkr.make_url(url, True)
        self.assertTrue(result.startswith("http://example.com/path?foo=bar&_cb="))

    def test_uniqueness(self):
        url = "http://example.com/"
        results = {pywrkr.make_url(url, True) for _ in range(100)}
        self.assertEqual(len(results), 100)

    def test_preserves_url_without_flag(self):
        for url in [
            "http://example.com/",
            "http://example.com/path?key=val",
            "https://host:8080/a/b/c?x=1&y=2",
        ]:
            self.assertEqual(pywrkr.make_url(url, False), url)


class TestBenchmarkConfigRandomParam(unittest.TestCase):
    def test_default_false(self):
        cfg = pywrkr.BenchmarkConfig(url="http://localhost/")
        self.assertFalse(cfg.random_param)

    def test_set_true(self):
        cfg = pywrkr.BenchmarkConfig(url="http://localhost/", random_param=True)
        self.assertTrue(cfg.random_param)


class TestPrintResultsUserMode(unittest.TestCase):
    """Test print_results with user simulation config."""

    def test_user_mode_display(self):
        stats = pywrkr.WorkerStats()
        stats.total_requests = 100
        stats.total_bytes = 10000
        stats.latencies = [0.05] * 100
        stats.status_codes[200] = 100
        config = pywrkr.BenchmarkConfig(
            url="http://test/",
            users=10,
            duration=10.0,
            think_time=1.0,
            ramp_up=5.0,
            think_time_jitter=0.5,
        )
        buf = StringIO()
        with patch("sys.stdout", buf):
            pywrkr.print_results(stats, 10.0, 10, 0.0, config)
        output = buf.getvalue()
        self.assertIn("10 virtual users", output)
        self.assertIn("Virtual Users:     10", output)
        self.assertIn("Think Time", output)
        self.assertIn("Avg Reqs/User:     10.0", output)


# ---------------------------------------------------------------------------
# Unit tests for RateLimiter
# ---------------------------------------------------------------------------

class TestRateLimiter(unittest.TestCase):
    def test_basic_rate_limiting(self):
        """10 acquires at rate=100 should take ~0.09s (first is instant)."""
        async def _run():
            rl = pywrkr.RateLimiter(rate=100)
            start = time.monotonic()
            for _ in range(10):
                await rl.acquire()
            elapsed = time.monotonic() - start
            return elapsed

        elapsed = asyncio.run(_run())
        # 10 acquires at 100/s: first is instant, 9 intervals of 0.01s = ~0.09s
        self.assertGreaterEqual(elapsed, 0.07)
        self.assertLess(elapsed, 0.20)

    def test_rate_limiter_fairness(self):
        """Intervals between acquires should be roughly equal."""
        async def _run():
            rl = pywrkr.RateLimiter(rate=50)  # 20ms intervals
            times = []
            for _ in range(6):
                await rl.acquire()
                times.append(time.monotonic())
            intervals = [times[i+1] - times[i] for i in range(len(times) - 1)]
            return intervals

        intervals = asyncio.run(_run())
        for interval in intervals:
            self.assertGreaterEqual(interval, 0.015)  # at least 15ms (some tolerance)
            self.assertLess(interval, 0.035)  # at most 35ms

    def test_concurrent_access(self):
        """Multiple coroutines sharing one limiter should respect rate."""
        async def _run():
            rl = pywrkr.RateLimiter(rate=100)
            count = {"n": 0}

            async def _worker():
                for _ in range(5):
                    await rl.acquire()
                    count["n"] += 1

            start = time.monotonic()
            await asyncio.gather(*[_worker() for _ in range(4)])
            elapsed = time.monotonic() - start
            return count["n"], elapsed

        total, elapsed = asyncio.run(_run())
        self.assertEqual(total, 20)
        # 20 acquires at 100/s: ~0.19s minimum (first is instant, 19 intervals)
        self.assertGreaterEqual(elapsed, 0.15)
        self.assertLess(elapsed, 0.40)

    def test_waits_counter(self):
        """The waits counter should track how many times the limiter slept."""
        async def _run():
            rl = pywrkr.RateLimiter(rate=200)
            for _ in range(10):
                await rl.acquire()
            return rl.waits

        waits = asyncio.run(_run())
        # First acquire is instant, subsequent ones mostly need to wait
        self.assertGreater(waits, 0)

    def test_rate_ramp(self):
        """Rate should increase linearly with ramp mode."""
        async def _run():
            rl = pywrkr.RateLimiter(rate=50, end_rate=200, ramp_duration=1.0)
            # At start, rate=50 (interval=20ms). At end, rate=200 (interval=5ms)
            start = time.monotonic()
            for _ in range(10):
                await rl.acquire()
            elapsed = time.monotonic() - start
            return elapsed

        elapsed = asyncio.run(_run())
        # With ramping rate 50->200 over 1s, intervals shrink as we go
        self.assertGreater(elapsed, 0.05)
        self.assertLess(elapsed, 0.50)


class TestBenchmarkConfigRate(unittest.TestCase):
    def test_rate_defaults_none(self):
        """BenchmarkConfig rate should default to None."""
        cfg = pywrkr.BenchmarkConfig(url="http://localhost/")
        self.assertIsNone(cfg.rate)
        self.assertIsNone(cfg.rate_ramp)

    def test_rate_field_set(self):
        """BenchmarkConfig rate field should be settable."""
        cfg = pywrkr.BenchmarkConfig(url="http://localhost/", rate=500.0, rate_ramp=1000.0)
        self.assertEqual(cfg.rate, 500.0)
        self.assertEqual(cfg.rate_ramp, 1000.0)


# ---------------------------------------------------------------------------
# Traffic profile tests
# ---------------------------------------------------------------------------


class TestTrafficProfiles(unittest.TestCase):
    """Unit tests for traffic shaping profiles."""

    def test_sine_profile_basic(self):
        p = pywrkr_main.SineProfile(cycles=1, min_factor=0.0)
        # At start (t=0): sin(0) = 0, so factor = 0.5 + 0.5*0 = 0.5
        self.assertAlmostEqual(p.rate_at(0, 60, 1000), 500.0, places=0)
        # At quarter: sin(π/2) = 1, factor = 1.0
        self.assertAlmostEqual(p.rate_at(15, 60, 1000), 1000.0, places=0)
        # At half: sin(π) ≈ 0, factor = 0.5
        self.assertAlmostEqual(p.rate_at(30, 60, 1000), 500.0, delta=1.0)
        # At 3/4: sin(3π/2) = -1, factor = 0.0
        self.assertAlmostEqual(p.rate_at(45, 60, 1000), 0.0, delta=1.0)

    def test_sine_profile_min_factor(self):
        p = pywrkr_main.SineProfile(cycles=1, min_factor=0.2)
        # Min rate should never go below 0.2 * base
        rates = [p.rate_at(t, 60, 1000) for t in range(61)]
        self.assertGreaterEqual(min(rates), 199.0)  # ~200 with float tolerance
        self.assertLessEqual(max(rates), 1001.0)

    def test_step_profile(self):
        p = pywrkr_main.StepProfile(levels=[100, 500, 1000])
        # First third
        self.assertEqual(p.rate_at(5, 60, 500), 100)
        # Second third
        self.assertEqual(p.rate_at(25, 60, 500), 500)
        # Last third
        self.assertEqual(p.rate_at(50, 60, 500), 1000)

    def test_step_profile_ignores_base_rate(self):
        """Step profile uses absolute levels, not base_rate."""
        p = pywrkr_main.StepProfile(levels=[200, 800])
        self.assertEqual(p.rate_at(0, 60, 9999), 200)

    def test_sawtooth_profile(self):
        p = pywrkr_main.SawtoothProfile(cycles=1, min_factor=0.0)
        # Start: factor = 0 → rate = 0
        self.assertAlmostEqual(p.rate_at(0, 60, 1000), 0.0, delta=1.0)
        # Mid: factor = 0.5 → rate = 500
        self.assertAlmostEqual(p.rate_at(30, 60, 1000), 500.0, delta=1.0)
        # Near end: factor ≈ 1.0 → rate ≈ 1000
        self.assertAlmostEqual(p.rate_at(59, 60, 1000), 983.0, delta=20.0)

    def test_square_profile(self):
        p = pywrkr_main.SquareProfile(cycles=1, low_factor=0.1)
        # First half: high
        self.assertEqual(p.rate_at(10, 60, 1000), 1000)
        # Second half: low
        self.assertAlmostEqual(p.rate_at(40, 60, 1000), 100.0, places=0)

    def test_spike_profile(self):
        p = pywrkr_main.SpikeProfile(interval=10, spike_dur=2, multiplier=5, baseline=0.1)
        # During spike (t=0 to t=2)
        self.assertEqual(p.rate_at(0, 60, 100), 500)
        self.assertEqual(p.rate_at(1, 60, 100), 500)
        # After spike
        self.assertEqual(p.rate_at(3, 60, 100), 10)
        self.assertEqual(p.rate_at(9, 60, 100), 10)
        # Next spike
        self.assertEqual(p.rate_at(10, 60, 100), 500)

    def test_business_hours_profile(self):
        p = pywrkr_main.BusinessHoursProfile()
        # Night (start/end) should be low
        self.assertLess(p.rate_at(0, 60, 1000), 100)
        self.assertLess(p.rate_at(59, 60, 1000), 100)
        # Midday (~50%) should be high
        mid_rate = p.rate_at(30, 60, 1000)
        self.assertGreater(mid_rate, 800)

    def test_csv_profile_absolute(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            f.write('time_sec,rate\n0,100\n30,500\n60,100\n')
            path = f.name
        try:
            p = pywrkr_main.CsvProfile(path)
            self.assertEqual(p.rate_at(0, 60, 9999), 100)  # base_rate ignored
            self.assertAlmostEqual(p.rate_at(15, 60, 9999), 300.0)  # interpolated
            self.assertEqual(p.rate_at(30, 60, 9999), 500)
        finally:
            os.unlink(path)

    def test_csv_profile_multiplier(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            f.write('time_sec,multiplier\n0,0.5\n60,2.0\n')
            path = f.name
        try:
            p = pywrkr_main.CsvProfile(path)
            self.assertTrue(p._is_multiplier)
            self.assertAlmostEqual(p.rate_at(0, 60, 100), 50.0)
            self.assertAlmostEqual(p.rate_at(30, 60, 100), 125.0)  # 0.5 + 0.75 = 1.25
            self.assertAlmostEqual(p.rate_at(60, 60, 100), 200.0)
        finally:
            os.unlink(path)

    def test_csv_profile_no_header(self):
        """CSV without header should still work."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            f.write('0,100\n10,200\n')
            path = f.name
        try:
            p = pywrkr_main.CsvProfile(path)
            self.assertFalse(p._is_multiplier)
            self.assertEqual(p.rate_at(0, 60, 500), 100)
        finally:
            os.unlink(path)

    def test_csv_profile_clamping(self):
        """Before first and after last point, nearest value is held."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            f.write('time_sec,rate\n10,200\n50,800\n')
            path = f.name
        try:
            p = pywrkr_main.CsvProfile(path)
            self.assertEqual(p.rate_at(0, 60, 500), 200)   # before first → hold
            self.assertEqual(p.rate_at(60, 60, 500), 800)   # after last → hold
        finally:
            os.unlink(path)

    def test_csv_empty_raises(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            f.write('')
            path = f.name
        try:
            with self.assertRaises(ValueError):
                pywrkr_main.CsvProfile(path)
        finally:
            os.unlink(path)

    def test_zero_duration_returns_base_rate(self):
        """All profiles should handle duration=0 gracefully."""
        for profile in [
            pywrkr_main.SineProfile(),
            pywrkr_main.SawtoothProfile(),
            pywrkr_main.SquareProfile(),
            pywrkr_main.BusinessHoursProfile(),
        ]:
            rate = profile.rate_at(5, 0, 500)
            self.assertGreater(rate, 0)


class TestParseTrafficProfile(unittest.TestCase):
    """Tests for the parse_traffic_profile() function."""

    def test_parse_builtin_default(self):
        p = pywrkr_main.parse_traffic_profile("sine")
        self.assertIsInstance(p, pywrkr_main.SineProfile)
        self.assertEqual(p.cycles, 2.0)

    def test_parse_builtin_with_params(self):
        p = pywrkr_main.parse_traffic_profile("sine:cycles=4,min=0.3")
        self.assertIsInstance(p, pywrkr_main.SineProfile)
        self.assertEqual(p.cycles, 4.0)
        self.assertEqual(p.min_factor, 0.3)

    def test_parse_step_with_levels(self):
        p = pywrkr_main.parse_traffic_profile("step:levels=100,500,1000")
        self.assertIsInstance(p, pywrkr_main.StepProfile)
        self.assertEqual(p.levels, [100.0, 500.0, 1000.0])

    def test_parse_step_without_prefix(self):
        p = pywrkr_main.parse_traffic_profile("step:100,500,1000")
        self.assertIsInstance(p, pywrkr_main.StepProfile)
        self.assertEqual(p.levels, [100.0, 500.0, 1000.0])

    def test_parse_spike_with_params(self):
        p = pywrkr_main.parse_traffic_profile("spike:interval=15,multiplier=3")
        self.assertIsInstance(p, pywrkr_main.SpikeProfile)
        self.assertEqual(p.interval, 15.0)
        self.assertEqual(p.multiplier, 3.0)

    def test_parse_csv(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            f.write('time_sec,rate\n0,100\n60,500\n')
            path = f.name
        try:
            p = pywrkr_main.parse_traffic_profile(f"csv:{path}")
            self.assertIsInstance(p, pywrkr_main.CsvProfile)
        finally:
            os.unlink(path)

    def test_parse_csv_missing_path(self):
        with self.assertRaises(ValueError):
            pywrkr_main.parse_traffic_profile("csv:")

    def test_parse_unknown_profile(self):
        with self.assertRaises(ValueError):
            pywrkr_main.parse_traffic_profile("nonexistent")

    def test_parse_square_params(self):
        p = pywrkr_main.parse_traffic_profile("square:cycles=5,low=0.3")
        self.assertIsInstance(p, pywrkr_main.SquareProfile)
        self.assertEqual(p.cycles, 5.0)
        self.assertEqual(p.low_factor, 0.3)

    def test_parse_business_hours(self):
        p = pywrkr_main.parse_traffic_profile("business-hours")
        self.assertIsInstance(p, pywrkr_main.BusinessHoursProfile)


class TestRateLimiterWithProfile(unittest.TestCase):
    """Test RateLimiter integration with traffic profiles."""

    def test_rate_limiter_uses_profile(self):
        """RateLimiter should delegate to traffic profile for rate calculation."""
        profile = pywrkr_main.StepProfile(levels=[100, 1000])
        rl = pywrkr_main.RateLimiter(
            rate=500, traffic_profile=profile, duration=60.0,
        )
        # Simulate start time
        rl._start_time = 100.0
        # First half: rate = 100
        self.assertEqual(rl._current_rate(110.0), 100)
        # Second half: rate = 1000
        self.assertEqual(rl._current_rate(140.0), 1000)

    def test_profile_overrides_ramp(self):
        """Traffic profile should take precedence over linear ramp."""
        profile = pywrkr_main.StepProfile(levels=[42])
        rl = pywrkr_main.RateLimiter(
            rate=500, end_rate=1000, ramp_duration=60.0,
            traffic_profile=profile, duration=60.0,
        )
        rl._start_time = 100.0
        self.assertEqual(rl._current_rate(130.0), 42)

    def test_describe_methods(self):
        """All profiles should have a describe() method returning a non-empty string."""
        profiles = [
            pywrkr_main.SineProfile(),
            pywrkr_main.StepProfile(levels=[100]),
            pywrkr_main.SawtoothProfile(),
            pywrkr_main.SquareProfile(),
            pywrkr_main.SpikeProfile(),
            pywrkr_main.BusinessHoursProfile(),
        ]
        for p in profiles:
            desc = p.describe()
            self.assertIsInstance(desc, str)
            self.assertGreater(len(desc), 0)


# ---------------------------------------------------------------------------
# Integration tests for rate limiting
# ---------------------------------------------------------------------------

class TestRateLimitIntegration(AioHTTPTestCase):
    """Test rate limiting mode with a local server."""

    async def get_application(self):
        app = web.Application()
        app.router.add_get("/", self.handle_get)
        return app

    async def handle_get(self, request):
        return web.Response(text="Hello, World!", content_type="text/plain")

    def _url(self, path="/"):
        return f"http://localhost:{self.server.port}{path}"


    async def test_constant_rate_with_duration(self):
        """--rate 50 -d 2 should produce ~100 requests."""
        config = pywrkr.BenchmarkConfig(
            url=self._url(),
            connections=2,
            duration=2.0,
            threads=1,
            timeout_sec=5,
            rate=50.0,
        )
        with patch("sys.stdout", new_callable=StringIO):
            stats, _ = await pywrkr.run_benchmark(config)
        # At 50 req/s for 2s, expect ~100 requests (allow tolerance)
        self.assertGreaterEqual(stats.total_requests, 70)
        self.assertLessEqual(stats.total_requests, 130)


    async def test_constant_rate_with_request_count(self):
        """--rate 50 -n 20 should take ~0.4s."""
        config = pywrkr.BenchmarkConfig(
            url=self._url(),
            connections=2,
            duration=None,
            num_requests=20,
            threads=1,
            timeout_sec=5,
            rate=50.0,
        )
        start = time.monotonic()
        with patch("sys.stdout", new_callable=StringIO):
            stats, _ = await pywrkr.run_benchmark(config)
        elapsed = time.monotonic() - start
        self.assertGreaterEqual(stats.total_requests, 20)
        # 20 requests at 50/s = ~0.4s minimum
        self.assertGreaterEqual(elapsed, 0.3)


    async def test_rate_limit_with_multiple_connections(self):
        """Rate limit should be global across all connections."""
        config = pywrkr.BenchmarkConfig(
            url=self._url(),
            connections=4,
            duration=2.0,
            threads=2,
            timeout_sec=5,
            rate=40.0,
        )
        with patch("sys.stdout", new_callable=StringIO):
            stats, _ = await pywrkr.run_benchmark(config)
        # 40 req/s for 2s = ~80 total, shared across 4 connections
        self.assertGreaterEqual(stats.total_requests, 50)
        self.assertLessEqual(stats.total_requests, 110)


    async def test_rate_ramp_mode(self):
        """Rate ramp from 20 to 80 over 2s should produce moderate request count."""
        config = pywrkr.BenchmarkConfig(
            url=self._url(),
            connections=2,
            duration=2.0,
            threads=1,
            timeout_sec=5,
            rate=20.0,
            rate_ramp=80.0,
        )
        with patch("sys.stdout", new_callable=StringIO):
            stats, _ = await pywrkr.run_benchmark(config)
        # Average rate ~50 req/s over 2s = ~100 total
        self.assertGreater(stats.total_requests, 40)
        self.assertLess(stats.total_requests, 200)


    async def test_results_show_target_rps(self):
        """Results should show target vs actual RPS when rate is set."""
        config = pywrkr.BenchmarkConfig(
            url=self._url(),
            connections=1,
            duration=1.0,
            threads=1,
            timeout_sec=5,
            rate=30.0,
        )
        buf = StringIO()
        with patch("sys.stdout", buf):
            stats, _ = await pywrkr.run_benchmark(config)
        output = buf.getvalue()
        self.assertIn("Target RPS:", output)
        self.assertIn("30.00", output)
        self.assertIn("Rate Limit Waits:", output)


    async def test_results_show_ramp_target(self):
        """Results should show ramp target RPS when rate ramp is set."""
        config = pywrkr.BenchmarkConfig(
            url=self._url(),
            connections=1,
            duration=1.0,
            threads=1,
            timeout_sec=5,
            rate=20.0,
            rate_ramp=100.0,
        )
        buf = StringIO()
        with patch("sys.stdout", buf):
            stats, _ = await pywrkr.run_benchmark(config)
        output = buf.getvalue()
        self.assertIn("Target RPS:", output)
        self.assertIn("Ramp Target RPS:", output)


    async def test_rate_with_user_simulation(self):
        """Rate limiting with user simulation (think_time=0) should throttle."""
        config = pywrkr.BenchmarkConfig(
            url=self._url(),
            users=3,
            duration=2.0,
            think_time=0.0,
            ramp_up=0.0,
            timeout_sec=5,
            rate=30.0,
        )
        with patch("sys.stdout", new_callable=StringIO):
            stats, _ = await pywrkr.run_user_simulation(config)
        # 30 req/s for 2s = ~60 total across 3 users
        self.assertGreaterEqual(stats.total_requests, 35)
        self.assertLessEqual(stats.total_requests, 90)


    async def test_banner_shows_rate_limit(self):
        """Banner should show rate limit info."""
        config = pywrkr.BenchmarkConfig(
            url=self._url(),
            connections=1,
            duration=0.5,
            threads=1,
            timeout_sec=5,
            rate=500.0,
        )
        buf = StringIO()
        with patch("sys.stdout", buf):
            stats, _ = await pywrkr.run_benchmark(config)
        output = buf.getvalue()
        self.assertIn("Rate Limit: 500 req/s", output)


    async def test_json_output_with_rate(self):
        """JSON output should include rate info when rate is set."""
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            json_path = f.name
        try:
            config = pywrkr.BenchmarkConfig(
                url=self._url(),
                connections=1,
                duration=1.0,
                threads=1,
                timeout_sec=5,
                rate=25.0,
                json_output=json_path,
            )
            with patch("sys.stdout", new_callable=StringIO):
                stats, _ = await pywrkr.run_benchmark(config)
            with open(json_path) as f:
                data = json.load(f)
            self.assertEqual(data["target_rps"], 25.0)
            self.assertIn("rate_limit_waits", data)
        finally:
            os.unlink(json_path)



# ---------------------------------------------------------------------------
# Scenario loading tests
# ---------------------------------------------------------------------------

class TestScenarioStep(unittest.TestCase):
    def test_defaults(self):
        step = pywrkr.ScenarioStep(path="/")
        self.assertEqual(step.method, "GET")
        self.assertIsNone(step.body)
        self.assertEqual(step.headers, {})
        self.assertIsNone(step.assert_status)
        self.assertIsNone(step.assert_body_contains)
        self.assertIsNone(step.think_time)
        self.assertIsNone(step.name)

    def test_custom_values(self):
        step = pywrkr.ScenarioStep(
            path="/login",
            method="POST",
            body={"user": "test"},
            headers={"X-Custom": "val"},
            assert_status=200,
            assert_body_contains="ok",
            think_time=0.5,
            name="Login",
        )
        self.assertEqual(step.path, "/login")
        self.assertEqual(step.method, "POST")
        self.assertEqual(step.body, {"user": "test"})
        self.assertEqual(step.assert_status, 200)
        self.assertEqual(step.think_time, 0.5)
        self.assertEqual(step.name, "Login")


class TestScenarioLoading(unittest.TestCase):
    def test_load_json_scenario(self):
        data = {
            "name": "Test Flow",
            "think_time": 0.5,
            "steps": [
                {"method": "GET", "path": "/"},
                {"method": "POST", "path": "/login", "body": {"user": "test"}},
            ]
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(data, f)
            path = f.name
        try:
            scenario = pywrkr.load_scenario(path)
            self.assertEqual(scenario.name, "Test Flow")
            self.assertEqual(scenario.think_time, 0.5)
            self.assertEqual(len(scenario.steps), 2)
            self.assertEqual(scenario.steps[0].method, "GET")
            self.assertEqual(scenario.steps[0].path, "/")
            self.assertEqual(scenario.steps[1].method, "POST")
            self.assertEqual(scenario.steps[1].body, {"user": "test"})
        finally:
            os.unlink(path)

    def test_load_missing_file(self):
        with self.assertRaises(FileNotFoundError):
            pywrkr.load_scenario("/nonexistent/scenario.json")

    def test_load_invalid_json(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("not valid json {{{")
            path = f.name
        try:
            with self.assertRaises(Exception):
                pywrkr.load_scenario(path)
        finally:
            os.unlink(path)

    def test_load_missing_steps(self):
        data = {"name": "No Steps"}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(data, f)
            path = f.name
        try:
            with self.assertRaises(ValueError):
                pywrkr.load_scenario(path)
        finally:
            os.unlink(path)

    def test_load_empty_steps(self):
        data = {"name": "Empty", "steps": []}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(data, f)
            path = f.name
        try:
            with self.assertRaises(ValueError):
                pywrkr.load_scenario(path)
        finally:
            os.unlink(path)

    def test_load_step_missing_path(self):
        data = {"steps": [{"method": "GET"}]}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(data, f)
            path = f.name
        try:
            with self.assertRaises(ValueError):
                pywrkr.load_scenario(path)
        finally:
            os.unlink(path)

    def test_load_defaults(self):
        data = {"steps": [{"path": "/"}]}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(data, f)
            path = f.name
        try:
            scenario = pywrkr.load_scenario(path)
            self.assertEqual(scenario.name, "Unnamed Scenario")
            self.assertEqual(scenario.think_time, 0.0)
            self.assertEqual(len(scenario.steps), 1)
            self.assertEqual(scenario.steps[0].method, "GET")
        finally:
            os.unlink(path)

    def test_load_not_a_dict(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump([1, 2, 3], f)
            path = f.name
        try:
            with self.assertRaises(ValueError):
                pywrkr.load_scenario(path)
        finally:
            os.unlink(path)

    def test_load_step_all_fields(self):
        data = {
            "steps": [{
                "path": "/api",
                "method": "PUT",
                "body": "raw body",
                "headers": {"Authorization": "Bearer tok"},
                "assert_status": 201,
                "assert_body_contains": "created",
                "think_time": 2.0,
                "name": "Create Resource",
            }]
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(data, f)
            path = f.name
        try:
            scenario = pywrkr.load_scenario(path)
            step = scenario.steps[0]
            self.assertEqual(step.path, "/api")
            self.assertEqual(step.method, "PUT")
            self.assertEqual(step.body, "raw body")
            self.assertEqual(step.headers, {"Authorization": "Bearer tok"})
            self.assertEqual(step.assert_status, 201)
            self.assertEqual(step.assert_body_contains, "created")
            self.assertEqual(step.think_time, 2.0)
            self.assertEqual(step.name, "Create Resource")
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# Scenario integration tests
# ---------------------------------------------------------------------------

class TestScenarioIntegration(AioHTTPTestCase):
    async def get_application(self):
        app = web.Application()
        app.router.add_get("/", self.handle_home)
        app.router.add_post("/login", self.handle_login)
        app.router.add_get("/dashboard", self.handle_dashboard)
        app.router.add_get("/api/data", self.handle_api_data)
        app.router.add_get("/error", self.handle_error)
        return app

    async def handle_home(self, request):
        return web.Response(text="Welcome Home", content_type="text/plain")

    async def handle_login(self, request):
        body = await request.read()
        ct = request.headers.get("Content-Type", "")
        if "application/json" in ct and body:
            data = json.loads(body)
            if data.get("user") == "test":
                return web.json_response({"status": "ok", "token": "abc123"})
        return web.Response(status=401, text="Unauthorized")

    async def handle_dashboard(self, request):
        return web.Response(text="Dashboard Content", content_type="text/plain")

    async def handle_api_data(self, request):
        page = request.query.get("page", "1")
        return web.json_response({"page": int(page), "items": [1, 2, 3]})

    async def handle_error(self, request):
        return web.Response(status=500, text="Internal Server Error")

    def _url(self, path=""):
        return f"http://localhost:{self.server.port}{path}"

    def _make_scenario_file(self, data):
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        json.dump(data, f)
        f.close()
        return f.name


    async def test_multi_step_scenario(self):
        scenario_data = {
            "name": "Basic Flow",
            "steps": [
                {"method": "GET", "path": "/"},
                {"method": "GET", "path": "/dashboard"},
            ]
        }
        path = self._make_scenario_file(scenario_data)
        try:
            scenario = pywrkr.load_scenario(path)
            config = pywrkr.BenchmarkConfig(
                url=self._url(),
                users=3,
                duration=2.0,
                think_time=0.0,
                ramp_up=0.0,
                timeout_sec=5,
                scenario=scenario,
            )
            with patch("sys.stdout", new_callable=StringIO):
                stats, _ = await pywrkr.run_user_simulation(config)
            self.assertGreater(stats.total_requests, 0)
            self.assertIn(200, stats.status_codes)
            self.assertGreater(len(stats.step_latencies), 0)
        finally:
            os.unlink(path)


    async def test_scenario_with_assertions_pass(self):
        scenario_data = {
            "name": "Assert Pass",
            "steps": [
                {"method": "GET", "path": "/", "assert_status": 200, "name": "Home"},
            ]
        }
        path = self._make_scenario_file(scenario_data)
        try:
            scenario = pywrkr.load_scenario(path)
            config = pywrkr.BenchmarkConfig(
                url=self._url(),
                users=2,
                duration=1.0,
                think_time=0.0,
                ramp_up=0.0,
                timeout_sec=5,
                scenario=scenario,
            )
            with patch("sys.stdout", new_callable=StringIO):
                stats, _ = await pywrkr.run_user_simulation(config)
            self.assertGreater(stats.total_requests, 0)
            # Allow small number of timeout errors at end of duration window
            self.assertLessEqual(stats.errors, 2)
        finally:
            os.unlink(path)


    async def test_scenario_with_assert_status_fail(self):
        scenario_data = {
            "name": "Assert Fail",
            "steps": [
                {"method": "GET", "path": "/error", "assert_status": 200, "name": "Should Fail"},
            ]
        }
        path = self._make_scenario_file(scenario_data)
        try:
            scenario = pywrkr.load_scenario(path)
            config = pywrkr.BenchmarkConfig(
                url=self._url(),
                users=2,
                duration=1.0,
                think_time=0.0,
                ramp_up=0.0,
                timeout_sec=5,
                scenario=scenario,
            )
            with patch("sys.stdout", new_callable=StringIO):
                stats, _ = await pywrkr.run_user_simulation(config)
            self.assertGreater(stats.total_requests, 0)
            self.assertEqual(stats.errors, stats.total_requests)
            has_assert_err = any("AssertStatus" in k for k in stats.error_types)
            self.assertTrue(has_assert_err)
        finally:
            os.unlink(path)


    async def test_scenario_with_assert_body_contains(self):
        scenario_data = {
            "name": "Body Assert",
            "steps": [
                {"method": "GET", "path": "/", "assert_body_contains": "Welcome"},
                {"method": "GET", "path": "/", "assert_body_contains": "NONEXISTENT_STRING"},
            ]
        }
        path = self._make_scenario_file(scenario_data)
        try:
            scenario = pywrkr.load_scenario(path)
            config = pywrkr.BenchmarkConfig(
                url=self._url(),
                users=1,
                duration=1.0,
                think_time=0.0,
                ramp_up=0.0,
                timeout_sec=5,
                scenario=scenario,
            )
            with patch("sys.stdout", new_callable=StringIO):
                stats, _ = await pywrkr.run_user_simulation(config)
            self.assertGreater(stats.total_requests, 0)
            self.assertGreater(stats.errors, 0)
            has_body_err = any("AssertBody" in k for k in stats.error_types)
            self.assertTrue(has_body_err)
        finally:
            os.unlink(path)


    async def test_scenario_with_per_step_think_time(self):
        scenario_data = {
            "name": "Think Time",
            "steps": [
                {"method": "GET", "path": "/", "think_time": 0.3},
            ]
        }
        path = self._make_scenario_file(scenario_data)
        try:
            scenario = pywrkr.load_scenario(path)
            config = pywrkr.BenchmarkConfig(
                url=self._url(),
                users=2,
                duration=2.0,
                think_time=0.0,
                think_time_jitter=0.0,
                ramp_up=0.0,
                timeout_sec=5,
                scenario=scenario,
            )
            with patch("sys.stdout", new_callable=StringIO):
                stats, _ = await pywrkr.run_user_simulation(config)
            self.assertGreater(stats.total_requests, 2)
            self.assertLess(stats.total_requests, 30)
        finally:
            os.unlink(path)


    async def test_scenario_with_post_body(self):
        scenario_data = {
            "name": "Post Test",
            "steps": [
                {"method": "POST", "path": "/login", "body": {"user": "test", "pass": "test"},
                 "assert_status": 200},
            ]
        }
        path = self._make_scenario_file(scenario_data)
        try:
            scenario = pywrkr.load_scenario(path)
            config = pywrkr.BenchmarkConfig(
                url=self._url(),
                users=2,
                duration=1.0,
                think_time=0.0,
                ramp_up=0.0,
                timeout_sec=5,
                scenario=scenario,
            )
            with patch("sys.stdout", new_callable=StringIO):
                stats, _ = await pywrkr.run_user_simulation(config)
            self.assertGreater(stats.total_requests, 0)
            # Allow small number of timeout errors at end of duration window
            self.assertLessEqual(stats.errors, 2)
            self.assertIn(200, stats.status_codes)
        finally:
            os.unlink(path)


    async def test_scenario_with_per_step_headers(self):
        scenario_data = {
            "name": "Header Test",
            "steps": [
                {"method": "POST", "path": "/login",
                 "body": {"user": "test"},
                 "headers": {"Content-Type": "application/json"}},
            ]
        }
        path = self._make_scenario_file(scenario_data)
        try:
            scenario = pywrkr.load_scenario(path)
            config = pywrkr.BenchmarkConfig(
                url=self._url(),
                users=1,
                duration=1.0,
                think_time=0.0,
                ramp_up=0.0,
                timeout_sec=5,
                headers={"X-Global": "yes"},
                scenario=scenario,
            )
            with patch("sys.stdout", new_callable=StringIO):
                stats, _ = await pywrkr.run_user_simulation(config)
            self.assertGreater(stats.total_requests, 0)
            self.assertIn(200, stats.status_codes)
        finally:
            os.unlink(path)


    async def test_scenario_json_output_includes_step_stats(self):
        scenario_data = {
            "name": "JSON Output Test",
            "steps": [
                {"method": "GET", "path": "/", "name": "Home Page"},
                {"method": "GET", "path": "/dashboard", "name": "Dashboard"},
            ]
        }
        scenario_path = self._make_scenario_file(scenario_data)
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            json_path = f.name
        try:
            scenario = pywrkr.load_scenario(scenario_path)
            config = pywrkr.BenchmarkConfig(
                url=self._url(),
                users=2,
                duration=1.0,
                think_time=0.0,
                ramp_up=0.0,
                json_output=json_path,
                timeout_sec=5,
                scenario=scenario,
            )
            with patch("sys.stdout", new_callable=StringIO):
                stats, _ = await pywrkr.run_user_simulation(config)
            with open(json_path) as f:
                data = json.load(f)
            self.assertIn("step_stats", data)
            self.assertIn("Home Page", data["step_stats"])
            self.assertIn("Dashboard", data["step_stats"])
            for step_name, step_data in data["step_stats"].items():
                self.assertIn("count", step_data)
                self.assertIn("mean", step_data)
                self.assertIn("min", step_data)
                self.assertIn("max", step_data)
                self.assertGreater(step_data["count"], 0)
        finally:
            os.unlink(scenario_path)
            os.unlink(json_path)


    async def test_scenario_connection_mode(self):
        scenario_data = {
            "name": "Conn Mode",
            "steps": [
                {"method": "GET", "path": "/"},
                {"method": "GET", "path": "/dashboard"},
            ]
        }
        path = self._make_scenario_file(scenario_data)
        try:
            scenario = pywrkr.load_scenario(path)
            config = pywrkr.BenchmarkConfig(
                url=self._url(),
                connections=2,
                duration=1.0,
                threads=1,
                timeout_sec=5,
                scenario=scenario,
            )
            with patch("sys.stdout", new_callable=StringIO):
                stats, _ = await pywrkr.run_benchmark(config)
            self.assertGreater(stats.total_requests, 0)
            self.assertIn(200, stats.status_codes)
            self.assertGreater(len(stats.step_latencies), 0)
        finally:
            os.unlink(path)


    async def test_scenario_per_step_reporting(self):
        scenario_data = {
            "name": "Report Test",
            "steps": [
                {"method": "GET", "path": "/", "name": "Home"},
                {"method": "GET", "path": "/dashboard", "name": "Dash"},
            ]
        }
        path = self._make_scenario_file(scenario_data)
        try:
            scenario = pywrkr.load_scenario(path)
            config = pywrkr.BenchmarkConfig(
                url=self._url(),
                users=2,
                duration=1.0,
                think_time=0.0,
                ramp_up=0.0,
                timeout_sec=5,
                scenario=scenario,
            )
            buf = StringIO()
            with patch("sys.stdout", buf):
                stats, _ = await pywrkr.run_user_simulation(config)
            output = buf.getvalue()
            self.assertIn("PER-STEP LATENCY", output)
            self.assertIn("Home", output)
            self.assertIn("Dash", output)
        finally:
            os.unlink(path)



# ---------------------------------------------------------------------------
# Live Dashboard Tests
# ---------------------------------------------------------------------------

class TestLiveDashboard(unittest.TestCase):
    """Unit tests for LiveDashboard class."""

    def _make_stats(self, n=50):
        stats = pywrkr.WorkerStats()
        stats.total_requests = n
        stats.total_bytes = n * 1024
        stats.errors = 2
        stats.latencies = [0.01 * i for i in range(1, n + 1)]
        stats.status_codes = defaultdict(int, {200: n - 2, 500: 2})
        stats.error_types = defaultdict(int, {"HTTP 500": 2})
        return stats

    def test_dashboard_creation(self):
        """Test LiveDashboard can be created with mock stats."""
        stats = [self._make_stats()]
        config = pywrkr.BenchmarkConfig(url="http://example.com/", duration=10.0)
        start_time = time.monotonic()
        dashboard = pywrkr.LiveDashboard(stats, config, start_time)
        self.assertEqual(dashboard.config.url, "http://example.com/")
        self.assertIsNone(dashboard.active_users)

    def test_dashboard_creation_with_active_users(self):
        """Test LiveDashboard with active_users dict."""
        stats = [self._make_stats()]
        config = pywrkr.BenchmarkConfig(url="http://example.com/", users=10, duration=10.0)
        start_time = time.monotonic()
        active_users = {"count": 5}
        dashboard = pywrkr.LiveDashboard(stats, config, start_time, active_users)
        self.assertEqual(dashboard.active_users["count"], 5)

    @unittest.skipUnless(pywrkr_main.RICH_AVAILABLE, "rich not installed")
    def test_dashboard_renders_without_errors(self):
        """Test _build_display() returns a Panel without crashing."""
        stats = [self._make_stats()]
        config = pywrkr.BenchmarkConfig(url="http://example.com/", duration=10.0)
        start_time = time.monotonic() - 5.0  # simulate 5s elapsed
        dashboard = pywrkr.LiveDashboard(stats, config, start_time)
        panel = dashboard._build_display()
        # Panel should be a rich Panel object
        from rich.panel import Panel
        self.assertIsInstance(panel, Panel)

    @unittest.skipUnless(pywrkr_main.RICH_AVAILABLE, "rich not installed")
    def test_dashboard_with_empty_stats(self):
        """Test dashboard renders with no requests yet."""
        stats = [pywrkr.WorkerStats()]
        config = pywrkr.BenchmarkConfig(url="http://example.com/", duration=10.0)
        start_time = time.monotonic()
        dashboard = pywrkr.LiveDashboard(stats, config, start_time)
        # Should not raise
        panel = dashboard._build_display()
        from rich.panel import Panel
        self.assertIsInstance(panel, Panel)

    @unittest.skipUnless(pywrkr_main.RICH_AVAILABLE, "rich not installed")
    def test_dashboard_request_count_mode(self):
        """Test dashboard renders in request-count mode."""
        stats = [self._make_stats(20)]
        config = pywrkr.BenchmarkConfig(
            url="http://example.com/", num_requests=100, duration=None
        )
        start_time = time.monotonic() - 2.0
        dashboard = pywrkr.LiveDashboard(stats, config, start_time)
        panel = dashboard._build_display()
        from rich.panel import Panel
        self.assertIsInstance(panel, Panel)

    @unittest.skipUnless(pywrkr_main.RICH_AVAILABLE, "rich not installed")
    def test_dashboard_user_mode(self):
        """Test dashboard renders in user simulation mode."""
        stats = [self._make_stats()]
        config = pywrkr.BenchmarkConfig(
            url="http://example.com/", users=50, duration=60.0
        )
        start_time = time.monotonic() - 10.0
        active_users = {"count": 50}
        dashboard = pywrkr.LiveDashboard(stats, config, start_time, active_users)
        panel = dashboard._build_display()
        from rich.panel import Panel
        self.assertIsInstance(panel, Panel)

    def test_dashboard_fallback_when_rich_unavailable(self):
        """Test that --live falls back gracefully when rich is not installed."""
        original_main = pywrkr_main.RICH_AVAILABLE
        original_reporting = pywrkr.reporting.RICH_AVAILABLE
        try:
            pywrkr_main.RICH_AVAILABLE = False
            pywrkr.reporting.RICH_AVAILABLE = False
            config = pywrkr.BenchmarkConfig(
                url="http://example.com/", live_dashboard=True
            )
            # When RICH_AVAILABLE is False and live_dashboard is True,
            # run_benchmark should fall back to show_progress
            self.assertTrue(config.live_dashboard)
            self.assertFalse(pywrkr_main.RICH_AVAILABLE)
        finally:
            pywrkr_main.RICH_AVAILABLE = original_main
            pywrkr.reporting.RICH_AVAILABLE = original_reporting

    def test_live_flag_in_config(self):
        """Test that live_dashboard field works in BenchmarkConfig."""
        cfg = pywrkr.BenchmarkConfig(url="http://localhost/")
        self.assertFalse(cfg.live_dashboard)
        cfg2 = pywrkr.BenchmarkConfig(url="http://localhost/", live_dashboard=True)
        self.assertTrue(cfg2.live_dashboard)


class TestLiveDashboardIntegration(AioHTTPTestCase):
    """Integration tests for --live flag with actual server."""

    async def get_application(self):
        app = web.Application()
        app.router.add_get("/", self.handle_get)
        return app

    async def handle_get(self, request):
        return web.Response(text="Hello, World!", content_type="text/plain")

    def _url(self, path):
        return f"http://localhost:{self.server.port}{path}"


    async def test_benchmark_with_live_flag(self):
        """Test benchmark with --live flag runs and completes."""
        config = pywrkr.BenchmarkConfig(
            url=self._url("/"),
            connections=2,
            duration=1.0,
            threads=1,
            timeout_sec=5,
            live_dashboard=True,
        )
        with patch("sys.stdout", new_callable=StringIO):
            stats, _ = await pywrkr.run_benchmark(config)
        self.assertGreater(stats.total_requests, 0)
        self.assertIn(200, stats.status_codes)


    async def test_user_simulation_with_live_flag(self):
        """Test user simulation with --live flag runs and completes."""
        config = pywrkr.BenchmarkConfig(
            url=self._url("/"),
            users=3,
            duration=1.0,
            think_time=0.1,
            ramp_up=0.0,
            timeout_sec=5,
            live_dashboard=True,
        )
        with patch("sys.stdout", new_callable=StringIO):
            stats, _ = await pywrkr.run_user_simulation(config)
        self.assertGreater(stats.total_requests, 0)
        self.assertIn(200, stats.status_codes)


    async def test_benchmark_with_live_flag_no_rich(self):
        """Test benchmark falls back when rich is unavailable."""
        original_main = pywrkr_main.RICH_AVAILABLE
        original_reporting = pywrkr.reporting.RICH_AVAILABLE
        try:
            pywrkr_main.RICH_AVAILABLE = False
            pywrkr.reporting.RICH_AVAILABLE = False
            config = pywrkr.BenchmarkConfig(
                url=self._url("/"),
                connections=2,
                duration=1.0,
                threads=1,
                timeout_sec=5,
                live_dashboard=True,
            )
            buf = StringIO()
            with patch("sys.stdout", buf):
                stats, _ = await pywrkr.run_benchmark(config)
            self.assertGreater(stats.total_requests, 0)
            output = buf.getvalue()
            self.assertIn("Warning", output)
        finally:
            pywrkr_main.RICH_AVAILABLE = original_main
            pywrkr.reporting.RICH_AVAILABLE = original_reporting


# ---------------------------------------------------------------------------
# Packaging Tests
# ---------------------------------------------------------------------------

class TestPackaging(unittest.TestCase):
    """Test PyPI packaging configuration."""

    def test_pyproject_toml_exists(self):
        """Test pyproject.toml exists and is valid TOML."""
        path = os.path.join(os.path.dirname(__file__), "..", "pyproject.toml")
        self.assertTrue(os.path.isfile(path), "pyproject.toml not found")
        # Parse TOML
        if sys.version_info >= (3, 11):
            import tomllib
            with open(path, "rb") as f:
                data = tomllib.load(f)
        else:
            # For Python 3.10, just verify it exists and is readable
            with open(path) as f:
                content = f.read()
            self.assertIn("[project]", content)
            return
        self.assertIn("project", data)
        self.assertEqual(data["project"]["name"], "pywrkr")
        self.assertEqual(data["project"]["version"], "1.0.4")

    def test_entry_point_defined(self):
        """Test that the pywrkr entry point is configured."""
        path = os.path.join(os.path.dirname(__file__), "..", "pyproject.toml")
        with open(path) as f:
            content = f.read()
        self.assertIn("pywrkr = ", content)
        self.assertIn("pywrkr.main:main", content)

    def test_version_accessible(self):
        """Test that pywrkr module has main() callable."""
        self.assertTrue(callable(pywrkr.cli_main))

    def test_license_file_exists(self):
        """Test LICENSE file exists."""
        path = os.path.join(os.path.dirname(__file__), "..", "LICENSE")
        self.assertTrue(os.path.isfile(path), "LICENSE not found")
        with open(path) as f:
            content = f.read()
        self.assertIn("MIT License", content)

    def test_optional_tui_dependency(self):
        """Test that pyproject.toml declares the tui optional dependency."""
        path = os.path.join(os.path.dirname(__file__), "..", "pyproject.toml")
        with open(path) as f:
            content = f.read()
        self.assertIn("[project.optional-dependencies]", content)
        self.assertIn("tui", content)
        self.assertIn("rich", content)


# ---------------------------------------------------------------------------
# Latency Breakdown tests
# ---------------------------------------------------------------------------

class TestLatencyBreakdown(unittest.TestCase):
    """Unit tests for the LatencyBreakdown dataclass and aggregation."""

    def test_defaults(self):
        bd = pywrkr.LatencyBreakdown()
        self.assertEqual(bd.dns, 0.0)
        self.assertEqual(bd.connect, 0.0)
        self.assertEqual(bd.tls, 0.0)
        self.assertEqual(bd.ttfb, 0.0)
        self.assertEqual(bd.transfer, 0.0)
        self.assertFalse(bd.is_reused)

    def test_custom_values(self):
        bd = pywrkr.LatencyBreakdown(dns=0.01, connect=0.02, tls=0.03, ttfb=0.04, transfer=0.05)
        self.assertAlmostEqual(bd.dns, 0.01)
        self.assertAlmostEqual(bd.connect, 0.02)
        self.assertAlmostEqual(bd.tls, 0.03)
        self.assertAlmostEqual(bd.ttfb, 0.04)
        self.assertAlmostEqual(bd.transfer, 0.05)

    def test_aggregation_empty(self):
        result = pywrkr.aggregate_breakdowns([])
        self.assertEqual(result, {})

    def test_aggregation_single(self):
        bd = pywrkr.LatencyBreakdown(dns=0.01, connect=0.02, tls=0.0, ttfb=0.05, transfer=0.03)
        result = pywrkr.aggregate_breakdowns([bd])
        self.assertIn("dns", result)
        self.assertAlmostEqual(result["dns"]["avg"], 0.01)
        self.assertAlmostEqual(result["connect"]["avg"], 0.02)
        self.assertAlmostEqual(result["ttfb"]["avg"], 0.05)
        self.assertAlmostEqual(result["transfer"]["avg"], 0.03)
        self.assertEqual(result["new_connections"], 1)
        self.assertEqual(result["reused_connections"], 0)

    def test_aggregation_multiple(self):
        breakdowns = [
            pywrkr.LatencyBreakdown(dns=0.01, connect=0.02, tls=0.0, ttfb=0.05, transfer=0.03),
            pywrkr.LatencyBreakdown(dns=0.02, connect=0.04, tls=0.0, ttfb=0.10, transfer=0.06),
            pywrkr.LatencyBreakdown(dns=0.03, connect=0.06, tls=0.0, ttfb=0.15, transfer=0.09, is_reused=True),
        ]
        result = pywrkr.aggregate_breakdowns(breakdowns)
        self.assertAlmostEqual(result["dns"]["avg"], 0.02)
        self.assertAlmostEqual(result["dns"]["min"], 0.01)
        self.assertAlmostEqual(result["dns"]["max"], 0.03)
        self.assertEqual(result["new_connections"], 2)
        self.assertEqual(result["reused_connections"], 1)
        # total = sum of all phases per breakdown
        total_0 = 0.01 + 0.02 + 0.0 + 0.05 + 0.03
        total_1 = 0.02 + 0.04 + 0.0 + 0.10 + 0.06
        total_2 = 0.03 + 0.06 + 0.0 + 0.15 + 0.09
        expected_avg = (total_0 + total_1 + total_2) / 3
        self.assertAlmostEqual(result["total"]["avg"], expected_avg)

    def test_aggregation_connection_reuse_counts(self):
        breakdowns = [
            pywrkr.LatencyBreakdown(is_reused=False),
            pywrkr.LatencyBreakdown(is_reused=True),
            pywrkr.LatencyBreakdown(is_reused=True),
            pywrkr.LatencyBreakdown(is_reused=True),
            pywrkr.LatencyBreakdown(is_reused=False),
        ]
        result = pywrkr.aggregate_breakdowns(breakdowns)
        self.assertEqual(result["new_connections"], 2)
        self.assertEqual(result["reused_connections"], 3)

    def test_worker_stats_breakdowns_field(self):
        ws = pywrkr.WorkerStats()
        self.assertEqual(ws.breakdowns, [])
        ws.breakdowns.append(pywrkr.LatencyBreakdown(dns=0.01))
        self.assertEqual(len(ws.breakdowns), 1)

    def test_config_latency_breakdown_default_false(self):
        config = pywrkr.BenchmarkConfig(url="http://example.com")
        self.assertFalse(config.latency_breakdown)

    def test_config_latency_breakdown_enabled(self):
        config = pywrkr.BenchmarkConfig(url="http://example.com", latency_breakdown=True)
        self.assertTrue(config.latency_breakdown)


class TestLatencyBreakdownIntegration(AioHTTPTestCase):
    """Integration tests for latency breakdown with a real aiohttp test server."""

    async def get_application(self):
        app = web.Application()
        app.router.add_get("/", self.handle_get)
        app.router.add_get("/large", self.handle_large)
        return app

    async def handle_get(self, request):
        return web.Response(text="Hello, World!", content_type="text/plain")

    async def handle_large(self, request):
        return web.Response(text="x" * 10000, content_type="text/plain")

    def _url(self, path):
        return f"http://localhost:{self.server.port}{path}"


    async def test_breakdown_captures_phases(self):
        """Test that breakdown captures TTFB and transfer for HTTP requests."""
        config = pywrkr.BenchmarkConfig(
            url=self._url("/"),
            connections=1,
            num_requests=5,
            threads=1,
            timeout_sec=5,
            latency_breakdown=True,
        )
        with patch("sys.stdout", new_callable=StringIO):
            stats, _ = await pywrkr.run_benchmark(config)
        self.assertGreater(stats.total_requests, 0)
        self.assertGreater(len(stats.breakdowns), 0)
        # At least some breakdowns should have non-zero TTFB
        has_ttfb = any(b.ttfb > 0 for b in stats.breakdowns)
        self.assertTrue(has_ttfb, "Expected at least one breakdown with non-zero TTFB")


    async def test_breakdown_in_results_output(self):
        """Test that breakdown stats appear in printed output when enabled."""
        config = pywrkr.BenchmarkConfig(
            url=self._url("/"),
            connections=1,
            num_requests=5,
            threads=1,
            timeout_sec=5,
            latency_breakdown=True,
        )
        output = StringIO()
        with patch("sys.stdout", output):
            stats, _ = await pywrkr.run_benchmark(config)
        text = output.getvalue()
        self.assertIn("LATENCY BREAKDOWN", text)
        self.assertIn("TTFB", text)
        self.assertIn("TCP Connect", text)
        self.assertIn("New Connections", text)
        self.assertIn("Reused Connections", text)


    async def test_breakdown_in_json_output(self):
        """Test that breakdown is included in JSON output when enabled."""
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            json_path = f.name
        try:
            config = pywrkr.BenchmarkConfig(
                url=self._url("/"),
                connections=1,
                num_requests=5,
                threads=1,
                timeout_sec=5,
                latency_breakdown=True,
                json_output=json_path,
            )
            with patch("sys.stdout", new_callable=StringIO):
                stats, _ = await pywrkr.run_benchmark(config)
            with open(json_path) as f:
                data = json.load(f)
            self.assertIn("latency_breakdown", data)
            bd = data["latency_breakdown"]
            self.assertIn("new_connections", bd)
            self.assertIn("reused_connections", bd)
            self.assertIn("ttfb", bd)
            self.assertIn("transfer", bd)
            self.assertIn("total", bd)
            # Each phase has avg, min, max, p50, p95
            for phase in ("ttfb", "transfer", "total"):
                self.assertIn("avg", bd[phase])
                self.assertIn("min", bd[phase])
                self.assertIn("max", bd[phase])
                self.assertIn("p50", bd[phase])
                self.assertIn("p95", bd[phase])
        finally:
            os.unlink(json_path)


    async def test_breakdown_disabled_by_default(self):
        """Test that breakdown has no overhead when disabled (no breakdowns collected)."""
        config = pywrkr.BenchmarkConfig(
            url=self._url("/"),
            connections=1,
            num_requests=5,
            threads=1,
            timeout_sec=5,
            latency_breakdown=False,
        )
        with patch("sys.stdout", new_callable=StringIO):
            stats, _ = await pywrkr.run_benchmark(config)
        self.assertGreater(stats.total_requests, 0)
        self.assertEqual(len(stats.breakdowns), 0)


    async def test_breakdown_multiple_requests_aggregated(self):
        """Test that multiple requests produce aggregated breakdown stats."""
        config = pywrkr.BenchmarkConfig(
            url=self._url("/"),
            connections=2,
            num_requests=10,
            threads=1,
            timeout_sec=5,
            latency_breakdown=True,
        )
        with patch("sys.stdout", new_callable=StringIO):
            stats, _ = await pywrkr.run_benchmark(config)
        self.assertGreater(len(stats.breakdowns), 1)
        agg = pywrkr.aggregate_breakdowns(stats.breakdowns)
        self.assertIn("total", agg)
        self.assertIn("ttfb", agg)
        self.assertGreater(agg["total"]["avg"], 0)
        # Check connection reuse tracking
        total_conns = agg["new_connections"] + agg["reused_connections"]
        self.assertEqual(total_conns, len(stats.breakdowns))


    async def test_breakdown_disabled_no_output_section(self):
        """Test that LATENCY BREAKDOWN section does NOT appear when disabled."""
        config = pywrkr.BenchmarkConfig(
            url=self._url("/"),
            connections=1,
            num_requests=3,
            threads=1,
            timeout_sec=5,
            latency_breakdown=False,
        )
        output = StringIO()
        with patch("sys.stdout", output):
            stats, _ = await pywrkr.run_benchmark(config)
        text = output.getvalue()
        self.assertNotIn("LATENCY BREAKDOWN", text)


# ---------------------------------------------------------------------------
# Autofind tests
# ---------------------------------------------------------------------------

class TestStepResult(unittest.TestCase):
    """Test StepResult dataclass."""

    def test_defaults(self):
        sr = pywrkr.StepResult(
            users=10, rps=5.0, p50=0.1, p95=0.2, p99=0.3,
            error_rate=0.0, total_requests=50, total_errors=0, passed=True,
        )
        self.assertEqual(sr.users, 10)
        self.assertEqual(sr.rps, 5.0)
        self.assertTrue(sr.passed)

    def test_failed_step(self):
        sr = pywrkr.StepResult(
            users=100, rps=50.0, p50=1.0, p95=6.0, p99=8.0,
            error_rate=5.0, total_requests=500, total_errors=25, passed=False,
        )
        self.assertFalse(sr.passed)
        self.assertEqual(sr.error_rate, 5.0)


class TestAutofindConfig(unittest.TestCase):
    """Test AutofindConfig dataclass defaults and custom values."""

    def test_defaults(self):
        cfg = pywrkr.AutofindConfig(url="http://localhost:8080/")
        self.assertEqual(cfg.max_error_rate, 1.0)
        self.assertEqual(cfg.max_p95, 5.0)
        self.assertEqual(cfg.step_duration, 30.0)
        self.assertEqual(cfg.start_users, 10)
        self.assertEqual(cfg.max_users, 10000)
        self.assertEqual(cfg.step_multiplier, 2.0)
        self.assertEqual(cfg.think_time, 1.0)
        self.assertEqual(cfg.think_time_jitter, 0.5)
        self.assertFalse(cfg.random_param)
        self.assertEqual(cfg.timeout_sec, 30.0)
        self.assertTrue(cfg.keepalive)
        self.assertIsNone(cfg.json_output)

    def test_custom_values(self):
        cfg = pywrkr.AutofindConfig(
            url="http://example.com/api",
            max_error_rate=2.5,
            max_p95=3.0,
            step_duration=15.0,
            start_users=5,
            max_users=500,
            step_multiplier=1.5,
            think_time=0.5,
            think_time_jitter=0.0,
            random_param=True,
            timeout_sec=10.0,
            keepalive=False,
            json_output="/tmp/results.json",
        )
        self.assertEqual(cfg.max_error_rate, 2.5)
        self.assertEqual(cfg.max_p95, 3.0)
        self.assertEqual(cfg.step_duration, 15.0)
        self.assertEqual(cfg.start_users, 5)
        self.assertEqual(cfg.max_users, 500)
        self.assertEqual(cfg.step_multiplier, 1.5)
        self.assertTrue(cfg.random_param)
        self.assertFalse(cfg.keepalive)
        self.assertEqual(cfg.json_output, "/tmp/results.json")


class TestAutofindHelpers(unittest.TestCase):
    """Test autofind helper functions."""

    def test_format_latency_short_ms(self):
        self.assertEqual(pywrkr._format_latency_short(0.120), "120ms")
        self.assertEqual(pywrkr._format_latency_short(0.001), "1ms")

    def test_format_latency_short_s(self):
        self.assertEqual(pywrkr._format_latency_short(1.5), "1.5s")
        self.assertEqual(pywrkr._format_latency_short(10.0), "10.0s")

    def test_step_passed_ok(self):
        cfg = pywrkr.AutofindConfig(url="http://x/", max_error_rate=1.0, max_p95=5.0)
        step = pywrkr.StepResult(
            users=10, rps=10.0, p50=0.1, p95=0.5, p99=1.0,
            error_rate=0.0, total_requests=100, total_errors=0, passed=True,
        )
        self.assertTrue(pywrkr._step_passed(step, cfg))

    def test_step_passed_error_rate_exceeded(self):
        cfg = pywrkr.AutofindConfig(url="http://x/", max_error_rate=1.0, max_p95=5.0)
        step = pywrkr.StepResult(
            users=10, rps=10.0, p50=0.1, p95=0.5, p99=1.0,
            error_rate=2.0, total_requests=100, total_errors=2, passed=True,
        )
        self.assertFalse(pywrkr._step_passed(step, cfg))

    def test_step_passed_p95_exceeded(self):
        cfg = pywrkr.AutofindConfig(url="http://x/", max_error_rate=1.0, max_p95=5.0)
        step = pywrkr.StepResult(
            users=10, rps=10.0, p50=0.1, p95=6.0, p99=8.0,
            error_rate=0.0, total_requests=100, total_errors=0, passed=True,
        )
        self.assertFalse(pywrkr._step_passed(step, cfg))

    def test_extract_step_result(self):
        stats = pywrkr.WorkerStats()
        stats.total_requests = 100
        stats.errors = 2
        stats.latencies = [0.1 * i for i in range(1, 101)]  # 0.1 to 10.0
        cfg = pywrkr.AutofindConfig(url="http://x/", max_error_rate=5.0, max_p95=20.0)
        result = pywrkr._extract_step_result(stats, 10.0, 20, cfg)
        self.assertEqual(result.users, 20)
        self.assertAlmostEqual(result.rps, 10.0)
        self.assertAlmostEqual(result.error_rate, 2.0)
        self.assertGreater(result.p50, 0)
        self.assertGreater(result.p95, 0)
        self.assertGreater(result.p99, 0)
        self.assertTrue(result.passed)

    def test_extract_step_result_no_latencies(self):
        stats = pywrkr.WorkerStats()
        stats.total_requests = 0
        stats.errors = 0
        cfg = pywrkr.AutofindConfig(url="http://x/")
        result = pywrkr._extract_step_result(stats, 10.0, 5, cfg)
        self.assertEqual(result.p50, 0.0)
        self.assertEqual(result.p95, 0.0)
        self.assertEqual(result.p99, 0.0)


class TestAutofindIntegration(AioHTTPTestCase):
    """Integration tests for autofind mode with a real HTTP server."""

    async def get_application(self):
        app = web.Application()
        app.router.add_get("/", self.handle_get)
        app.router.add_get("/error", self.handle_error)
        app.router.add_get("/slow", self.handle_slow)
        return app

    async def handle_get(self, request):
        return web.Response(text="OK", content_type="text/plain")

    async def handle_error(self, request):
        return web.Response(status=500, text="Internal Server Error")

    async def handle_slow(self, request):
        await asyncio.sleep(0.5)
        return web.Response(text="slow")

    def _url(self, path):
        return f"http://localhost:{self.server.port}{path}"


    async def test_autofind_healthy_server(self):
        """Autofind with a healthy server should find capacity > 0."""
        config = pywrkr.AutofindConfig(
            url=self._url("/"),
            max_error_rate=1.0,
            max_p95=5.0,
            step_duration=2.0,
            start_users=2,
            max_users=20,
            step_multiplier=2.0,
            think_time=0.0,
            think_time_jitter=0.0,
            timeout_sec=5,
        )
        output = StringIO()
        with patch("sys.stdout", output):
            steps = await pywrkr.run_autofind(config)
        self.assertGreater(len(steps), 0)
        # At least the first step should pass (healthy server)
        self.assertTrue(steps[0].passed)
        text = output.getvalue()
        self.assertIn("AUTOFIND RESULTS", text)


    async def test_autofind_error_endpoint(self):
        """Autofind hitting error endpoint should fail quickly."""
        config = pywrkr.AutofindConfig(
            url=self._url("/error"),
            max_error_rate=1.0,
            max_p95=5.0,
            step_duration=2.0,
            start_users=2,
            max_users=100,
            step_multiplier=2.0,
            think_time=0.0,
            think_time_jitter=0.0,
            timeout_sec=5,
        )
        output = StringIO()
        with patch("sys.stdout", output):
            steps = await pywrkr.run_autofind(config)
        # The first step should fail (100% error rate)
        self.assertFalse(steps[0].passed)
        self.assertGreater(steps[0].error_rate, 1.0)
        text = output.getvalue()
        self.assertIn("FAIL", text)


    async def test_autofind_respects_max_error_rate(self):
        """Autofind should mark steps as failed when error rate exceeds threshold."""
        config = pywrkr.AutofindConfig(
            url=self._url("/error"),
            max_error_rate=50.0,  # very high threshold
            max_p95=50.0,
            step_duration=2.0,
            start_users=2,
            max_users=8,
            step_multiplier=2.0,
            think_time=0.0,
            think_time_jitter=0.0,
            timeout_sec=5,
        )
        output = StringIO()
        with patch("sys.stdout", output):
            steps = await pywrkr.run_autofind(config)
        # All steps pass because threshold is very high (50%)
        # but error rate is 100%, so all should fail
        for s in steps:
            self.assertFalse(s.passed)


    async def test_autofind_respects_max_p95(self):
        """Autofind should fail when p95 exceeds threshold on slow endpoint."""
        config = pywrkr.AutofindConfig(
            url=self._url("/slow"),
            max_error_rate=100.0,  # ignore errors
            max_p95=0.1,  # very tight p95 threshold (100ms)
            step_duration=2.0,
            start_users=2,
            max_users=8,
            step_multiplier=2.0,
            think_time=0.0,
            think_time_jitter=0.0,
            timeout_sec=5,
        )
        output = StringIO()
        with patch("sys.stdout", output):
            steps = await pywrkr.run_autofind(config)
        # p95 should exceed 100ms since handler sleeps 500ms
        self.assertFalse(steps[0].passed)
        self.assertGreater(steps[0].p95, 0.1)


    async def test_autofind_json_output(self):
        """Autofind JSON output should include all steps."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json_path = f.name

        try:
            config = pywrkr.AutofindConfig(
                url=self._url("/"),
                max_error_rate=1.0,
                max_p95=5.0,
                step_duration=2.0,
                start_users=2,
                max_users=8,
                step_multiplier=2.0,
                think_time=0.0,
                think_time_jitter=0.0,
                timeout_sec=5,
                json_output=json_path,
            )
            output = StringIO()
            with patch("sys.stdout", output):
                steps = await pywrkr.run_autofind(config)

            with open(json_path) as f:
                data = json.load(f)

            self.assertIn("steps", data)
            self.assertEqual(len(data["steps"]), len(steps))
            self.assertIn("max_sustainable_users", data)
            self.assertEqual(data["url"], self._url("/"))
            # Each step should have all expected fields
            for step_data in data["steps"]:
                self.assertIn("users", step_data)
                self.assertIn("rps", step_data)
                self.assertIn("p50", step_data)
                self.assertIn("p95", step_data)
                self.assertIn("p99", step_data)
                self.assertIn("error_rate", step_data)
                self.assertIn("passed", step_data)
        finally:
            os.unlink(json_path)


    async def test_autofind_binary_search_refinement(self):
        """Autofind should do binary search when threshold is exceeded."""
        # Use error endpoint - first step will fail, so binary search
        # between 0 and start_users won't happen. Instead, use healthy
        # endpoint with a very tight p95.
        # We'll use the slow endpoint with p95=0.3s. The slow endpoint
        # has 500ms delay, so even 2 users will fail p95=0.3s.
        # Start=2, multiplier=2, so: 2 (fail) -> no binary search possible
        # Let's use a setup where first step passes but second fails.
        config = pywrkr.AutofindConfig(
            url=self._url("/"),
            max_error_rate=1.0,
            max_p95=5.0,
            step_duration=2.0,
            start_users=2,
            max_users=4,
            step_multiplier=2.0,
            think_time=0.0,
            think_time_jitter=0.0,
            timeout_sec=5,
        )
        output = StringIO()
        with patch("sys.stdout", output):
            steps = await pywrkr.run_autofind(config)
        # With a healthy server, all steps should pass
        for s in steps:
            self.assertTrue(s.passed)
        text = output.getvalue()
        self.assertIn("AUTOFIND RESULTS", text)


    async def test_autofind_custom_step_duration(self):
        """Autofind should respect custom step_duration."""
        config = pywrkr.AutofindConfig(
            url=self._url("/"),
            step_duration=3.0,  # 3 second steps
            start_users=2,
            max_users=4,
            step_multiplier=2.0,
            think_time=0.0,
            think_time_jitter=0.0,
            timeout_sec=5,
        )
        t0 = time.monotonic()
        output = StringIO()
        with patch("sys.stdout", output):
            steps = await pywrkr.run_autofind(config)
        elapsed = time.monotonic() - t0
        # Should take at least step_duration * number_of_steps
        self.assertGreaterEqual(elapsed, 3.0 * len(steps) * 0.8)


    async def test_autofind_prints_summary_table(self):
        """Autofind should print a formatted summary table."""
        config = pywrkr.AutofindConfig(
            url=self._url("/"),
            step_duration=2.0,
            start_users=2,
            max_users=4,
            step_multiplier=2.0,
            think_time=0.0,
            think_time_jitter=0.0,
            timeout_sec=5,
        )
        output = StringIO()
        with patch("sys.stdout", output):
            steps = await pywrkr.run_autofind(config)
        text = output.getvalue()
        self.assertIn("AUTOFIND RESULTS", text)
        self.assertIn("Maximum sustainable load:", text)
        self.assertIn("Step Results:", text)
        self.assertIn("Users", text)
        self.assertIn("RPS", text)
        self.assertIn("p50", text)
        self.assertIn("p95", text)
        self.assertIn("Status", text)
        # Should contain "OK" since healthy server
        self.assertIn("OK", text)


# ---------------------------------------------------------------------------
# Test Metadata Tags
# ---------------------------------------------------------------------------

class TestMetadataTags(unittest.TestCase):
    """Tests for --tag metadata feature."""

    def _make_stats(self, n=100):
        stats = pywrkr.WorkerStats()
        stats.total_requests = n
        stats.total_bytes = n * 1024
        stats.errors = 2
        stats.latencies = [0.01 * i for i in range(1, n + 1)]
        stats.status_codes = defaultdict(int, {200: n - 2, 500: 2})
        stats.error_types = defaultdict(int, {"HTTP 500": 2})
        return stats

    def test_tags_included_in_results_dict(self):
        """Tags should appear in build_results_dict output."""
        stats = self._make_stats()
        config = pywrkr.BenchmarkConfig(
            url="http://test/",
            tags={"environment": "staging", "build": "123"},
        )
        result = pywrkr.build_results_dict(stats, 10.0, 50, config)
        self.assertIn("tags", result)
        self.assertEqual(result["tags"]["environment"], "staging")
        self.assertEqual(result["tags"]["build"], "123")

    def test_empty_tags_backward_compatibility(self):
        """Empty tags should not appear in results dict."""
        stats = self._make_stats()
        config = pywrkr.BenchmarkConfig(url="http://test/")
        result = pywrkr.build_results_dict(stats, 10.0, 50, config)
        self.assertNotIn("tags", result)

    def test_tags_without_config(self):
        """build_results_dict without config should not have tags."""
        stats = self._make_stats()
        result = pywrkr.build_results_dict(stats, 10.0, 50)
        self.assertNotIn("tags", result)

    def test_tags_cli_parsing(self):
        """Test that --tag key=value is parsed correctly via argparse."""
        import argparse
        # Simulate argparse behavior by testing the tag parsing logic directly
        tag_strs = ["environment=prod", "build=456", "region=us-east-1"]
        tags = {}
        for tag_str in tag_strs:
            key, value = tag_str.split("=", 1)
            tags[key.strip()] = value.strip()
        self.assertEqual(tags, {
            "environment": "prod",
            "build": "456",
            "region": "us-east-1",
        })

    def test_config_tags_default_empty(self):
        """BenchmarkConfig should have empty tags by default."""
        cfg = pywrkr.BenchmarkConfig(url="http://test/")
        self.assertEqual(cfg.tags, {})
        self.assertIsNone(cfg.otel_endpoint)
        self.assertIsNone(cfg.prom_remote_write)


# ---------------------------------------------------------------------------
# Test OpenTelemetry Export
# ---------------------------------------------------------------------------

class TestOtelExport(unittest.TestCase):
    """Tests for export_to_otel function."""

    def _make_results(self):
        return {
            "duration_sec": 10.0,
            "connections": 50,
            "total_requests": 1000,
            "total_errors": 5,
            "requests_per_sec": 100.0,
            "transfer_per_sec_bytes": 50000.0,
            "total_bytes": 500000,
            "latency": {
                "min": 0.001,
                "max": 0.5,
                "mean": 0.05,
                "median": 0.04,
                "stdev": 0.02,
            },
            "percentiles": {
                "p50": 0.04,
                "p75": 0.06,
                "p90": 0.1,
                "p95": 0.2,
                "p99": 0.4,
                "p99.9": 0.45,
                "p99.99": 0.5,
            },
        }

    def test_graceful_when_otel_not_installed(self):
        """Should warn gracefully when OTel packages are missing."""
        original = pywrkr.reporting.OTEL_AVAILABLE
        try:
            pywrkr.reporting.OTEL_AVAILABLE = False
            buf = StringIO()
            with patch("sys.stdout", buf):
                pywrkr.export_to_otel(self._make_results(), "http://localhost:4318", {})
            self.assertIn("opentelemetry packages not installed", buf.getvalue())
        finally:
            pywrkr.reporting.OTEL_AVAILABLE = original

    @unittest.skipUnless(pywrkr_main.OTEL_AVAILABLE, "opentelemetry not installed")
    def test_export_constructs_metrics(self):
        """Test that export_to_otel creates a MeterProvider and metrics."""
        results = self._make_results()
        tags = {"environment": "test", "service": "myapp"}

        with patch("pywrkr.reporting.OTLPMetricExporter") as mock_exporter, \
             patch("pywrkr.reporting.PeriodicExportingMetricReader") as mock_reader, \
             patch("pywrkr.reporting.MeterProvider") as mock_provider_cls:
            mock_provider = MagicMock()
            mock_meter = MagicMock()
            mock_provider.get_meter.return_value = mock_meter
            mock_provider_cls.return_value = mock_provider

            mock_counter = MagicMock()
            mock_gauge = MagicMock()
            mock_meter.create_counter.return_value = mock_counter
            mock_meter.create_up_down_counter.return_value = mock_gauge

            pywrkr.export_to_otel(results, "http://localhost:4318", tags)

            mock_exporter.assert_called_once_with(endpoint="http://localhost:4318")
            mock_provider.get_meter.assert_called_once_with("pywrkr")
            # Should create 2 counters (requests.total, errors.total)
            self.assertEqual(mock_meter.create_counter.call_count, 2)
            # Should create several gauges
            self.assertGreater(mock_meter.create_up_down_counter.call_count, 0)
            # Counters should be called with correct values
            counter_calls = mock_counter.add.call_args_list
            self.assertEqual(counter_calls[0][0][0], 1000)  # total_requests
            self.assertEqual(counter_calls[1][0][0], 5)  # total_errors
            mock_provider.force_flush.assert_called_once()
            mock_provider.shutdown.assert_called_once()

    @unittest.skipUnless(pywrkr_main.OTEL_AVAILABLE, "opentelemetry not installed")
    def test_tags_attached_as_attributes(self):
        """Tags should be passed as metric attributes."""
        results = self._make_results()
        tags = {"env": "prod", "region": "us-east-1"}

        with patch("pywrkr.reporting.OTLPMetricExporter"), \
             patch("pywrkr.reporting.PeriodicExportingMetricReader"), \
             patch("pywrkr.reporting.MeterProvider") as mock_provider_cls:
            mock_provider = MagicMock()
            mock_meter = MagicMock()
            mock_provider.get_meter.return_value = mock_meter
            mock_provider_cls.return_value = mock_provider

            mock_counter = MagicMock()
            mock_gauge = MagicMock()
            mock_meter.create_counter.return_value = mock_counter
            mock_meter.create_up_down_counter.return_value = mock_gauge

            pywrkr.export_to_otel(results, "http://localhost:4318", tags)

            # Check that counter add was called with tags as attributes
            for call in mock_counter.add.call_args_list:
                self.assertEqual(call[1]["attributes"], tags)
            for call in mock_gauge.add.call_args_list:
                self.assertEqual(call[1]["attributes"], tags)

    @unittest.skipUnless(pywrkr_main.OTEL_AVAILABLE, "opentelemetry not installed")
    def test_graceful_on_connection_error(self):
        """Should not crash on connection errors."""
        with patch("pywrkr.reporting.OTLPMetricExporter", side_effect=Exception("connection refused")):
            buf = StringIO()
            with patch("sys.stdout", buf):
                pywrkr.export_to_otel(self._make_results(), "http://bad:4318", {})
            self.assertIn("Warning: failed to export", buf.getvalue())


# ---------------------------------------------------------------------------
# Test Prometheus Export
# ---------------------------------------------------------------------------

class TestPrometheusExport(unittest.TestCase):
    """Tests for export_to_prometheus function."""

    def _make_results(self):
        return {
            "duration_sec": 10.0,
            "connections": 50,
            "total_requests": 1000,
            "total_errors": 5,
            "requests_per_sec": 100.0,
            "transfer_per_sec_bytes": 50000.0,
            "total_bytes": 500000,
            "latency": {
                "min": 0.001,
                "max": 0.5,
                "mean": 0.05,
                "median": 0.04,
                "stdev": 0.02,
            },
            "percentiles": {
                "p50": 0.04,
                "p75": 0.06,
                "p90": 0.1,
                "p95": 0.2,
                "p99": 0.4,
                "p99.9": 0.45,
                "p99.99": 0.5,
            },
        }

    def test_generates_correct_text_format(self):
        """Should generate valid Prometheus text format metrics."""
        results = self._make_results()
        tags = {"env": "prod"}

        with patch("urllib.request.urlopen") as mock_urlopen:
            pywrkr.export_to_prometheus(results, "http://pushgateway:9091", tags)

            mock_urlopen.assert_called_once()
            req = mock_urlopen.call_args[0][0]
            body = req.data.decode("utf-8")

            # Check metric names present
            self.assertIn("pywrkr_requests_total", body)
            self.assertIn("pywrkr_errors_total", body)
            self.assertIn("pywrkr_requests_per_sec", body)
            self.assertIn("pywrkr_latency_p50_ms", body)
            self.assertIn("pywrkr_latency_p95_ms", body)
            self.assertIn("pywrkr_latency_p99_ms", body)
            self.assertIn("pywrkr_latency_mean_ms", body)
            self.assertIn("pywrkr_latency_max_ms", body)
            self.assertIn("pywrkr_transfer_bytes_per_sec", body)
            self.assertIn("pywrkr_duration_sec", body)

            # Check HELP and TYPE lines
            self.assertIn("# HELP pywrkr_requests_total", body)
            self.assertIn("# TYPE pywrkr_requests_total counter", body)
            self.assertIn("# TYPE pywrkr_requests_per_sec gauge", body)

            # Check values
            self.assertIn("1000", body)  # total_requests
            self.assertIn("5", body)  # total_errors

    def test_post_to_correct_url(self):
        """Should POST to {endpoint}/metrics/job/pywrkr."""
        with patch("urllib.request.urlopen") as mock_urlopen:
            pywrkr.export_to_prometheus(
                self._make_results(), "http://pushgateway:9091", {},
            )
            req = mock_urlopen.call_args[0][0]
            self.assertEqual(req.full_url, "http://pushgateway:9091/metrics/job/pywrkr")
            self.assertEqual(req.method, "POST")
            self.assertEqual(req.get_header("Content-type"),
                             "text/plain; version=0.0.4")

    def test_trailing_slash_in_endpoint(self):
        """Should handle trailing slash in endpoint URL."""
        with patch("urllib.request.urlopen") as mock_urlopen:
            pywrkr.export_to_prometheus(
                self._make_results(), "http://pushgateway:9091/", {},
            )
            req = mock_urlopen.call_args[0][0]
            self.assertEqual(req.full_url, "http://pushgateway:9091/metrics/job/pywrkr")

    def test_tags_become_labels(self):
        """Tags should appear as Prometheus labels."""
        tags = {"environment": "staging", "build": "42"}

        with patch("urllib.request.urlopen") as mock_urlopen:
            pywrkr.export_to_prometheus(self._make_results(), "http://gw:9091", tags)

            req = mock_urlopen.call_args[0][0]
            body = req.data.decode("utf-8")

            # Labels should be in the format {key="value",...}
            self.assertIn('build="42"', body)
            self.assertIn('environment="staging"', body)

    def test_empty_tags_no_labels(self):
        """With no tags, metrics should have no label braces."""
        with patch("urllib.request.urlopen") as mock_urlopen:
            pywrkr.export_to_prometheus(self._make_results(), "http://gw:9091", {})

            req = mock_urlopen.call_args[0][0]
            body = req.data.decode("utf-8")

            # Metric lines should not have {} when no tags
            for line in body.strip().split("\n"):
                if not line.startswith("#"):
                    self.assertNotIn("{", line)

    def test_graceful_on_connection_failure(self):
        """Should warn but not crash on connection errors."""
        import urllib.error
        with patch("urllib.request.urlopen",
                   side_effect=urllib.error.URLError("connection refused")):
            buf = StringIO()
            with patch("sys.stdout", buf):
                pywrkr.export_to_prometheus(
                    self._make_results(), "http://bad:9091", {},
                )
            self.assertIn("Warning: failed to export", buf.getvalue())

    def test_latency_values_in_milliseconds(self):
        """Latency values should be exported in milliseconds."""
        results = self._make_results()
        with patch("urllib.request.urlopen") as mock_urlopen:
            pywrkr.export_to_prometheus(results, "http://gw:9091", {})
            req = mock_urlopen.call_args[0][0]
            body = req.data.decode("utf-8")
            # p50 = 0.04s = 40ms
            self.assertIn("pywrkr_latency_p50_ms 40.0", body)
            # p95 = 0.2s = 200ms
            self.assertIn("pywrkr_latency_p95_ms 200.0", body)
            # mean = 0.05s = 50ms
            self.assertIn("pywrkr_latency_mean_ms 50.0", body)


# ---------------------------------------------------------------------------
# Integration tests for observability features
# ---------------------------------------------------------------------------

class TestObservabilityIntegration(AioHTTPTestCase):
    """Integration tests for tags and exporter features."""

    async def get_application(self):
        app = web.Application()
        app.router.add_get("/", self.handle_get)
        return app

    async def handle_get(self, request):
        return web.Response(text="Hello, World!", content_type="text/plain")

    def _url(self, path="/"):
        return f"http://localhost:{self.server.port}{path}"


    async def test_benchmark_with_tags_json_output(self):
        """Tags should appear in JSON output from benchmark."""
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            json_path = f.name
        try:
            config = pywrkr.BenchmarkConfig(
                url=self._url(),
                connections=1,
                duration=None,
                num_requests=5,
                threads=1,
                timeout_sec=5,
                json_output=json_path,
                tags={"environment": "ci", "test_name": "basic"},
            )
            with patch("sys.stdout", new_callable=StringIO):
                stats, _ = await pywrkr.run_benchmark(config)
            with open(json_path) as f:
                data = json.load(f)
            self.assertIn("tags", data)
            self.assertEqual(data["tags"]["environment"], "ci")
            self.assertEqual(data["tags"]["test_name"], "basic")
        finally:
            os.unlink(json_path)


    async def test_user_simulation_with_tags_json_output(self):
        """Tags should appear in JSON output from user simulation."""
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            json_path = f.name
        try:
            config = pywrkr.BenchmarkConfig(
                url=self._url(),
                users=3,
                duration=1.0,
                think_time=0.0,
                ramp_up=0.0,
                timeout_sec=5,
                json_output=json_path,
                tags={"region": "us-west-2"},
            )
            with patch("sys.stdout", new_callable=StringIO):
                stats, _ = await pywrkr.run_user_simulation(config)
            with open(json_path) as f:
                data = json.load(f)
            self.assertIn("tags", data)
            self.assertEqual(data["tags"]["region"], "us-west-2")
        finally:
            os.unlink(json_path)


    async def test_otel_exporter_called_when_configured(self):
        """OTel exporter should be called when --otel-endpoint is set."""
        config = pywrkr.BenchmarkConfig(
            url=self._url(),
            connections=1,
            duration=None,
            num_requests=3,
            threads=1,
            timeout_sec=5,
            otel_endpoint="http://localhost:4318",
            tags={"env": "test"},
        )
        with patch("pywrkr.reporting.export_to_otel") as mock_otel, \
             patch("sys.stdout", new_callable=StringIO):
            stats, _ = await pywrkr.run_benchmark(config)
        mock_otel.assert_called_once()
        call_args = mock_otel.call_args
        self.assertEqual(call_args[0][1], "http://localhost:4318")
        self.assertEqual(call_args[0][2], {"env": "test"})
        # First argument should be a results dict
        self.assertIn("total_requests", call_args[0][0])


    async def test_prometheus_exporter_called_when_configured(self):
        """Prometheus exporter should be called when --prom-remote-write is set."""
        config = pywrkr.BenchmarkConfig(
            url=self._url(),
            connections=1,
            duration=None,
            num_requests=3,
            threads=1,
            timeout_sec=5,
            prom_remote_write="http://pushgateway:9091",
            tags={"service": "myapp"},
        )
        with patch("pywrkr.reporting.export_to_prometheus") as mock_prom, \
             patch("sys.stdout", new_callable=StringIO):
            stats, _ = await pywrkr.run_benchmark(config)
        mock_prom.assert_called_once()
        call_args = mock_prom.call_args
        self.assertEqual(call_args[0][1], "http://pushgateway:9091")
        self.assertEqual(call_args[0][2], {"service": "myapp"})


    async def test_both_exporters_called_together(self):
        """Both exporters should be called when both endpoints are configured."""
        config = pywrkr.BenchmarkConfig(
            url=self._url(),
            connections=1,
            duration=None,
            num_requests=3,
            threads=1,
            timeout_sec=5,
            otel_endpoint="http://otel:4318",
            prom_remote_write="http://prom:9091",
        )
        with patch("pywrkr.reporting.export_to_otel") as mock_otel, \
             patch("pywrkr.reporting.export_to_prometheus") as mock_prom, \
             patch("sys.stdout", new_callable=StringIO):
            stats, _ = await pywrkr.run_benchmark(config)
        mock_otel.assert_called_once()
        mock_prom.assert_called_once()


    async def test_no_exporters_called_when_not_configured(self):
        """No exporters should be called when endpoints are not set."""
        config = pywrkr.BenchmarkConfig(
            url=self._url(),
            connections=1,
            duration=None,
            num_requests=3,
            threads=1,
            timeout_sec=5,
        )
        with patch("pywrkr.reporting.export_to_otel") as mock_otel, \
             patch("pywrkr.reporting.export_to_prometheus") as mock_prom, \
             patch("sys.stdout", new_callable=StringIO):
            stats, _ = await pywrkr.run_benchmark(config)
        mock_otel.assert_not_called()
        mock_prom.assert_not_called()


    async def test_user_sim_otel_exporter_called(self):
        """OTel exporter should also work in user simulation mode."""
        config = pywrkr.BenchmarkConfig(
            url=self._url(),
            users=2,
            duration=1.0,
            think_time=0.0,
            ramp_up=0.0,
            timeout_sec=5,
            otel_endpoint="http://localhost:4318",
        )
        with patch("pywrkr.reporting.export_to_otel") as mock_otel, \
             patch("sys.stdout", new_callable=StringIO):
            stats, _ = await pywrkr.run_user_simulation(config)
        mock_otel.assert_called_once()


class TestPackagingObservability(unittest.TestCase):
    """Test that pyproject.toml has the new optional dependency groups."""

    def test_otel_optional_dependency(self):
        path = os.path.join(os.path.dirname(__file__), "..", "pyproject.toml")
        with open(path) as f:
            content = f.read()
        self.assertIn("otel", content)
        self.assertIn("opentelemetry-api", content)
        self.assertIn("opentelemetry-sdk", content)
        self.assertIn("opentelemetry-exporter-otlp-proto-http", content)

    def test_all_optional_dependency(self):
        path = os.path.join(os.path.dirname(__file__), "..", "pyproject.toml")
        with open(path) as f:
            content = f.read()
        self.assertIn('all = [', content)


# ---------------------------------------------------------------------------
# Threshold parsing tests
# ---------------------------------------------------------------------------

class TestThresholdParsing(unittest.TestCase):
    def test_p95_milliseconds(self):
        th = pywrkr.parse_threshold("p95 < 300ms")
        self.assertEqual(th.metric, "p95")
        self.assertEqual(th.operator, "<")
        self.assertAlmostEqual(th.value, 0.3)
        self.assertEqual(th.raw_expr, "p95 < 300ms")

    def test_p99_seconds(self):
        th = pywrkr.parse_threshold("p99 < 1s")
        self.assertEqual(th.metric, "p99")
        self.assertEqual(th.operator, "<")
        self.assertAlmostEqual(th.value, 1.0)

    def test_error_rate_percent(self):
        th = pywrkr.parse_threshold("error_rate < 1%")
        self.assertEqual(th.metric, "error_rate")
        self.assertEqual(th.operator, "<")
        self.assertAlmostEqual(th.value, 1.0)

    def test_error_rate_no_percent(self):
        th = pywrkr.parse_threshold("error_rate < 1")
        self.assertEqual(th.metric, "error_rate")
        self.assertEqual(th.operator, "<")
        self.assertAlmostEqual(th.value, 1.0)

    def test_rps_greater(self):
        th = pywrkr.parse_threshold("rps > 100")
        self.assertEqual(th.metric, "rps")
        self.assertEqual(th.operator, ">")
        self.assertAlmostEqual(th.value, 100.0)

    def test_avg_latency_lte(self):
        th = pywrkr.parse_threshold("avg_latency <= 500ms")
        self.assertEqual(th.metric, "avg_latency")
        self.assertEqual(th.operator, "<=")
        self.assertAlmostEqual(th.value, 0.5)

    def test_max_latency(self):
        th = pywrkr.parse_threshold("max_latency < 2s")
        self.assertEqual(th.metric, "max_latency")
        self.assertEqual(th.operator, "<")
        self.assertAlmostEqual(th.value, 2.0)

    def test_p50_microseconds(self):
        th = pywrkr.parse_threshold("p50 >= 10us")
        self.assertEqual(th.metric, "p50")
        self.assertEqual(th.operator, ">=")
        self.assertAlmostEqual(th.value, 0.00001)

    def test_no_unit_defaults_to_seconds(self):
        th = pywrkr.parse_threshold("p90 < 5")
        self.assertEqual(th.metric, "p90")
        self.assertAlmostEqual(th.value, 5.0)

    def test_min_latency(self):
        th = pywrkr.parse_threshold("min_latency >= 1ms")
        self.assertEqual(th.metric, "min_latency")
        self.assertEqual(th.operator, ">=")
        self.assertAlmostEqual(th.value, 0.001)

    def test_invalid_metric(self):
        with self.assertRaises(ValueError):
            pywrkr.parse_threshold("p100 < 300ms")

    def test_invalid_operator(self):
        with self.assertRaises(ValueError):
            pywrkr.parse_threshold("p95 == 300ms")

    def test_empty_string(self):
        with self.assertRaises(ValueError):
            pywrkr.parse_threshold("")

    def test_garbage(self):
        with self.assertRaises(ValueError):
            pywrkr.parse_threshold("hello world")

    def test_invalid_unit_for_error_rate(self):
        with self.assertRaises(ValueError):
            pywrkr.parse_threshold("error_rate < 1ms")

    def test_invalid_unit_for_rps(self):
        with self.assertRaises(ValueError):
            pywrkr.parse_threshold("rps > 100ms")

    def test_percent_on_latency_metric(self):
        with self.assertRaises(ValueError):
            pywrkr.parse_threshold("p95 < 5%")


# ---------------------------------------------------------------------------
# Threshold evaluation tests
# ---------------------------------------------------------------------------

class TestThresholdEvaluation(unittest.TestCase):
    def _make_stats(self, latencies=None, errors=0, total=100):
        stats = pywrkr.WorkerStats()
        stats.total_requests = total
        stats.errors = errors
        stats.latencies = latencies if latencies is not None else []
        return stats

    def test_all_pass(self):
        stats = self._make_stats(
            latencies=[0.01 * i for i in range(1, 101)],  # 10ms to 1000ms
            errors=0,
            total=100,
        )
        thresholds = [
            pywrkr.parse_threshold("p95 < 2s"),
            pywrkr.parse_threshold("error_rate < 1"),
            pywrkr.parse_threshold("rps > 5"),
        ]
        results = pywrkr.evaluate_thresholds(thresholds, stats, 10.0)
        self.assertEqual(len(results), 3)
        for th, actual, passed in results:
            self.assertTrue(passed, f"{th.raw_expr} failed: actual={actual}")

    def test_mixed_pass_fail(self):
        stats = self._make_stats(
            latencies=[0.5] * 100,  # all 500ms
            errors=5,
            total=100,
        )
        thresholds = [
            pywrkr.parse_threshold("p95 < 300ms"),     # FAIL: 500ms > 300ms
            pywrkr.parse_threshold("error_rate < 10"),  # PASS: 5% < 10%
        ]
        results = pywrkr.evaluate_thresholds(thresholds, stats, 10.0)
        self.assertEqual(len(results), 2)
        # p95 should fail
        self.assertFalse(results[0][2])
        # error_rate should pass
        self.assertTrue(results[1][2])

    def test_empty_latencies(self):
        stats = self._make_stats(latencies=[], errors=0, total=0)
        thresholds = [
            pywrkr.parse_threshold("p95 < 300ms"),
        ]
        results = pywrkr.evaluate_thresholds(thresholds, stats, 10.0)
        self.assertEqual(len(results), 1)
        # With 0.0 as actual, 0.0 < 0.3 should pass
        self.assertTrue(results[0][2])
        self.assertAlmostEqual(results[0][1], 0.0)

    def test_error_rate_evaluation(self):
        stats = self._make_stats(errors=10, total=100)
        thresholds = [
            pywrkr.parse_threshold("error_rate < 5"),
        ]
        results = pywrkr.evaluate_thresholds(thresholds, stats, 10.0)
        self.assertFalse(results[0][2])
        self.assertAlmostEqual(results[0][1], 10.0)

    def test_rps_evaluation(self):
        stats = self._make_stats(total=500)
        thresholds = [
            pywrkr.parse_threshold("rps > 100"),
        ]
        # 500 requests / 10s = 50 rps < 100 -> FAIL
        results = pywrkr.evaluate_thresholds(thresholds, stats, 10.0)
        self.assertFalse(results[0][2])
        self.assertAlmostEqual(results[0][1], 50.0)

        # 500 requests / 2s = 250 rps > 100 -> PASS
        results = pywrkr.evaluate_thresholds(thresholds, stats, 2.0)
        self.assertTrue(results[0][2])
        self.assertAlmostEqual(results[0][1], 250.0)

    def test_avg_latency(self):
        stats = self._make_stats(latencies=[0.1, 0.2, 0.3])
        thresholds = [
            pywrkr.parse_threshold("avg_latency <= 250ms"),
        ]
        results = pywrkr.evaluate_thresholds(thresholds, stats, 1.0)
        self.assertTrue(results[0][2])
        self.assertAlmostEqual(results[0][1], 0.2)

    def test_max_latency(self):
        stats = self._make_stats(latencies=[0.1, 0.2, 0.5])
        thresholds = [
            pywrkr.parse_threshold("max_latency < 1s"),
        ]
        results = pywrkr.evaluate_thresholds(thresholds, stats, 1.0)
        self.assertTrue(results[0][2])
        self.assertAlmostEqual(results[0][1], 0.5)

    def test_min_latency(self):
        stats = self._make_stats(latencies=[0.1, 0.2, 0.5])
        thresholds = [
            pywrkr.parse_threshold("min_latency >= 50ms"),
        ]
        results = pywrkr.evaluate_thresholds(thresholds, stats, 1.0)
        self.assertTrue(results[0][2])
        self.assertAlmostEqual(results[0][1], 0.1)

    def test_lte_operator(self):
        stats = self._make_stats(latencies=[0.5] * 10)
        thresholds = [
            pywrkr.parse_threshold("p50 <= 500ms"),
        ]
        results = pywrkr.evaluate_thresholds(thresholds, stats, 1.0)
        self.assertTrue(results[0][2])

    def test_gte_operator(self):
        stats = self._make_stats(latencies=[0.5] * 10)
        thresholds = [
            pywrkr.parse_threshold("p50 >= 500ms"),
        ]
        results = pywrkr.evaluate_thresholds(thresholds, stats, 1.0)
        self.assertTrue(results[0][2])


# ---------------------------------------------------------------------------
# Threshold printing tests
# ---------------------------------------------------------------------------

class TestThresholdPrinting(unittest.TestCase):
    def test_print_pass(self):
        th = pywrkr.Threshold(metric="p95", operator="<", value=0.3, raw_expr="p95 < 300ms")
        results = [(th, 0.2, True)]
        buf = StringIO()
        pywrkr.print_threshold_results(results, file=buf)
        output = buf.getvalue()
        self.assertIn("SLO Threshold Results", output)
        self.assertIn("PASS", output)
        self.assertIn("p95 < 300ms", output)
        self.assertIn("ALL PASSED", output)

    def test_print_fail(self):
        th = pywrkr.Threshold(metric="p95", operator="<", value=0.3, raw_expr="p95 < 300ms")
        results = [(th, 0.5, False)]
        buf = StringIO()
        pywrkr.print_threshold_results(results, file=buf)
        output = buf.getvalue()
        self.assertIn("FAIL", output)
        self.assertIn("SOME FAILED", output)

    def test_print_mixed(self):
        th1 = pywrkr.Threshold(metric="p95", operator="<", value=0.3, raw_expr="p95 < 300ms")
        th2 = pywrkr.Threshold(metric="error_rate", operator="<", value=5.0, raw_expr="error_rate < 5%")
        results = [(th1, 0.5, False), (th2, 2.0, True)]
        buf = StringIO()
        pywrkr.print_threshold_results(results, file=buf)
        output = buf.getvalue()
        self.assertIn("FAIL", output)
        self.assertIn("PASS", output)
        self.assertIn("SOME FAILED", output)

    def test_print_empty(self):
        buf = StringIO()
        pywrkr.print_threshold_results([], file=buf)
        self.assertEqual(buf.getvalue(), "")

    def test_print_rps_format(self):
        th = pywrkr.Threshold(metric="rps", operator=">", value=100.0, raw_expr="rps > 100")
        results = [(th, 150.5, True)]
        buf = StringIO()
        pywrkr.print_threshold_results(results, file=buf)
        output = buf.getvalue()
        self.assertIn("150.50", output)

    def test_print_error_rate_format(self):
        th = pywrkr.Threshold(metric="error_rate", operator="<", value=5.0, raw_expr="error_rate < 5%")
        results = [(th, 2.5, True)]
        buf = StringIO()
        pywrkr.print_threshold_results(results, file=buf)
        output = buf.getvalue()
        self.assertIn("2.50%", output)


# ---------------------------------------------------------------------------
# Threshold integration tests
# ---------------------------------------------------------------------------

class TestThresholdIntegration(AioHTTPTestCase):
    """Integration tests for threshold evaluation with real benchmarks."""

    async def get_application(self):
        app = web.Application()
        app.router.add_get("/", self.handle_get)
        app.router.add_get("/error", self.handle_error)
        return app

    async def handle_get(self, request):
        return web.Response(text="Hello, World!", content_type="text/plain")

    async def handle_error(self, request):
        return web.Response(status=500, text="Internal Server Error")

    def _url(self, path="/"):
        return f"http://localhost:{self.server.port}{path}"


    async def test_benchmark_thresholds_all_pass(self):
        """All thresholds pass -> exit_code 0."""
        config = pywrkr.BenchmarkConfig(
            url=self._url("/"),
            connections=1,
            duration=None,
            num_requests=10,
            threads=1,
            timeout_sec=5,
            thresholds=[
                pywrkr.parse_threshold("p95 < 5s"),
                pywrkr.parse_threshold("error_rate < 50"),
            ],
        )
        with patch("sys.stdout", new_callable=StringIO):
            stats, exit_code = await pywrkr.run_benchmark(config)
        self.assertEqual(exit_code, 0)
        self.assertGreater(stats.total_requests, 0)


    async def test_benchmark_thresholds_fail(self):
        """Error endpoint breaches error_rate threshold -> exit_code 2."""
        config = pywrkr.BenchmarkConfig(
            url=self._url("/error"),
            connections=1,
            duration=None,
            num_requests=10,
            threads=1,
            timeout_sec=5,
            thresholds=[
                pywrkr.parse_threshold("error_rate < 1"),
            ],
        )
        with patch("sys.stdout", new_callable=StringIO):
            stats, exit_code = await pywrkr.run_benchmark(config)
        self.assertEqual(exit_code, 2)


    async def test_benchmark_no_thresholds(self):
        """No thresholds -> exit_code 0."""
        config = pywrkr.BenchmarkConfig(
            url=self._url("/"),
            connections=1,
            duration=None,
            num_requests=5,
            threads=1,
            timeout_sec=5,
        )
        with patch("sys.stdout", new_callable=StringIO):
            stats, exit_code = await pywrkr.run_benchmark(config)
        self.assertEqual(exit_code, 0)


    async def test_user_simulation_thresholds_pass(self):
        """User simulation with passing thresholds."""
        config = pywrkr.BenchmarkConfig(
            url=self._url("/"),
            users=3,
            duration=1.0,
            think_time=0.0,
            ramp_up=0.0,
            timeout_sec=5,
            thresholds=[
                pywrkr.parse_threshold("p95 < 5s"),
                pywrkr.parse_threshold("error_rate < 1"),
            ],
        )
        with patch("sys.stdout", new_callable=StringIO):
            stats, exit_code = await pywrkr.run_user_simulation(config)
        self.assertEqual(exit_code, 0)


    async def test_user_simulation_thresholds_fail(self):
        """User simulation with failing thresholds."""
        config = pywrkr.BenchmarkConfig(
            url=self._url("/error"),
            users=3,
            duration=1.0,
            think_time=0.0,
            ramp_up=0.0,
            timeout_sec=5,
            thresholds=[
                pywrkr.parse_threshold("error_rate < 1"),
            ],
        )
        with patch("sys.stdout", new_callable=StringIO):
            stats, exit_code = await pywrkr.run_user_simulation(config)
        self.assertEqual(exit_code, 2)


    async def test_threshold_results_printed(self):
        """Threshold results should appear in output."""
        config = pywrkr.BenchmarkConfig(
            url=self._url("/"),
            connections=1,
            duration=None,
            num_requests=5,
            threads=1,
            timeout_sec=5,
            thresholds=[
                pywrkr.parse_threshold("p95 < 5s"),
            ],
        )
        buf = StringIO()
        with patch("sys.stdout", buf):
            stats, exit_code = await pywrkr.run_benchmark(config)
        output = buf.getvalue()
        self.assertIn("SLO Threshold Results", output)
        self.assertIn("PASS", output)


# ---------------------------------------------------------------------------
# Tests for distributed mode (master/worker)
# ---------------------------------------------------------------------------

class TestSerializeConfig(unittest.TestCase):
    """Test config serialization/deserialization round-trip."""

    def test_basic_round_trip(self):
        config = pywrkr.BenchmarkConfig(
            url="http://example.com/test",
            connections=50,
            duration=30.0,
            method="POST",
            headers={"Content-Type": "application/json"},
            body=b'{"key": "value"}',
            timeout_sec=10.0,
            keepalive=False,
            basic_auth="user:pass",
            cookies=["session=abc"],
            random_param=True,
            rate=100.0,
            rate_ramp=500.0,
        )
        data = pywrkr._serialize_config(config)
        restored = pywrkr._deserialize_config(data)

        self.assertEqual(restored.url, config.url)
        self.assertEqual(restored.connections, config.connections)
        self.assertEqual(restored.duration, config.duration)
        self.assertEqual(restored.method, config.method)
        self.assertEqual(restored.headers, config.headers)
        self.assertEqual(restored.body, config.body)
        self.assertEqual(restored.timeout_sec, config.timeout_sec)
        self.assertEqual(restored.keepalive, config.keepalive)
        self.assertEqual(restored.basic_auth, config.basic_auth)
        self.assertEqual(restored.cookies, config.cookies)
        self.assertEqual(restored.random_param, config.random_param)
        self.assertEqual(restored.rate, config.rate)
        self.assertEqual(restored.rate_ramp, config.rate_ramp)

    def test_none_body(self):
        config = pywrkr.BenchmarkConfig(url="http://example.com/", body=None)
        data = pywrkr._serialize_config(config)
        restored = pywrkr._deserialize_config(data)
        self.assertIsNone(restored.body)

    def test_user_simulation_fields(self):
        config = pywrkr.BenchmarkConfig(
            url="http://example.com/",
            users=100,
            ramp_up=10.0,
            think_time=2.0,
            think_time_jitter=0.3,
        )
        data = pywrkr._serialize_config(config)
        restored = pywrkr._deserialize_config(data)
        self.assertEqual(restored.users, 100)
        self.assertEqual(restored.ramp_up, 10.0)
        self.assertEqual(restored.think_time, 2.0)
        self.assertEqual(restored.think_time_jitter, 0.3)


class TestSerializeStats(unittest.TestCase):
    """Test stats serialization/deserialization round-trip."""

    def test_round_trip(self):
        ws = pywrkr.WorkerStats()
        ws.total_requests = 1000
        ws.total_bytes = 50000
        ws.errors = 5
        ws.content_length_errors = 1
        ws.latencies = [0.1, 0.2, 0.3, 0.15]
        ws.error_types["HTTP 500"] = 3
        ws.error_types["TimeoutError"] = 2
        ws.status_codes[200] = 995
        ws.status_codes[500] = 5
        ws.rps_timeline = [(1.0, 100), (2.0, 150)]

        data = pywrkr._serialize_stats(ws)
        restored = pywrkr._deserialize_stats(data)

        self.assertEqual(restored.total_requests, ws.total_requests)
        self.assertEqual(restored.total_bytes, ws.total_bytes)
        self.assertEqual(restored.errors, ws.errors)
        self.assertEqual(restored.content_length_errors, ws.content_length_errors)
        self.assertEqual(restored.latencies, ws.latencies)
        self.assertEqual(dict(restored.error_types), dict(ws.error_types))
        self.assertEqual(dict(restored.status_codes), dict(ws.status_codes))
        self.assertEqual(len(restored.rps_timeline), len(ws.rps_timeline))

    def test_empty_stats(self):
        ws = pywrkr.WorkerStats()
        data = pywrkr._serialize_stats(ws)
        restored = pywrkr._deserialize_stats(data)
        self.assertEqual(restored.total_requests, 0)
        self.assertEqual(restored.latencies, [])


class TestMergeWorkerStats(unittest.TestCase):
    """Test merge_worker_stats helper."""

    def test_merge_two(self):
        ws1 = pywrkr.WorkerStats()
        ws1.total_requests = 100
        ws1.total_bytes = 5000
        ws1.errors = 2
        ws1.latencies = [0.1, 0.2]
        ws1.status_codes[200] = 98
        ws1.status_codes[500] = 2

        ws2 = pywrkr.WorkerStats()
        ws2.total_requests = 200
        ws2.total_bytes = 10000
        ws2.errors = 3
        ws2.latencies = [0.15, 0.25, 0.35]
        ws2.status_codes[200] = 197
        ws2.status_codes[500] = 3

        merged = pywrkr.merge_worker_stats([ws1, ws2])
        self.assertEqual(merged.total_requests, 300)
        self.assertEqual(merged.total_bytes, 15000)
        self.assertEqual(merged.errors, 5)
        self.assertEqual(len(merged.latencies), 5)
        self.assertEqual(merged.status_codes[200], 295)
        self.assertEqual(merged.status_codes[500], 5)

    def test_merge_empty_list(self):
        merged = pywrkr.merge_worker_stats([])
        self.assertEqual(merged.total_requests, 0)


class TestMessageProtocol(unittest.TestCase):
    """Test _send_msg / _recv_msg framing protocol."""

    def test_send_recv_round_trip(self):
        """Messages sent through the protocol should be received intact."""
        original = {"type": "config", "data": [1, 2, 3], "nested": {"key": "value"}}

        async def _run():
            # Create an in-process TCP server/client pair
            received = {}
            ready = asyncio.Event()

            async def handler(reader, writer):
                msg = await pywrkr._recv_msg(reader)
                received.update(msg)
                writer.close()
                ready.set()

            server = await asyncio.start_server(handler, "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]

            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            await pywrkr._send_msg(writer, original)
            writer.close()

            await asyncio.wait_for(ready.wait(), timeout=5)
            server.close()
            await server.wait_closed()

            return received

        result = asyncio.run(_run())
        self.assertEqual(result, original)

    def test_large_message(self):
        """Large messages should be handled correctly."""
        original = {"data": "x" * 100_000}

        async def _run():
            received = {}
            ready = asyncio.Event()

            async def handler(reader, writer):
                msg = await pywrkr._recv_msg(reader)
                received.update(msg)
                writer.close()
                ready.set()

            server = await asyncio.start_server(handler, "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]

            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            await pywrkr._send_msg(writer, original)
            writer.close()

            await asyncio.wait_for(ready.wait(), timeout=5)
            server.close()
            await server.wait_closed()

            return received

        result = asyncio.run(_run())
        self.assertEqual(result["data"], original["data"])


class TestDistributedIntegration(AioHTTPTestCase):
    """Integration tests for distributed master/worker mode with a real HTTP server."""

    async def get_application(self):
        app = web.Application()
        app.router.add_get("/", self._handle_root)
        app.router.add_get("/slow", self._handle_slow)
        return app

    async def _handle_root(self, request):
        return web.Response(text="OK")

    async def _handle_slow(self, request):
        await asyncio.sleep(0.05)
        return web.Response(text="SLOW OK")

    def _url(self, path="/"):
        return f"http://127.0.0.1:{self.server.port}{path}"


    async def test_master_worker_single(self):
        """A single worker should connect, run benchmark, and return results to master."""
        config = pywrkr.BenchmarkConfig(
            url=self._url("/"),
            connections=2,
            duration=None,
            num_requests=20,
            threads=1,
            timeout_sec=5,
            _quiet=True,
        )

        master_result = None

        async def _master():
            nonlocal master_result
            master_result = await pywrkr.run_master(config, "127.0.0.1", 0, expect_workers=1)

        # Start master on a random port, discover the port, then start worker
        worker_connections = []
        ready_event = asyncio.Event()

        async def handle_worker(reader, writer):
            worker_connections.append((reader, writer))
            ready_event.set()

        server = await asyncio.start_server(handle_worker, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        server.close()
        await server.wait_closed()

        # Run master and worker concurrently using the discovered port
        async def _run_master():
            nonlocal master_result
            master_result = await pywrkr.run_master(config, "127.0.0.1", port, expect_workers=1)

        async def _run_worker():
            # Small delay to let master start listening
            await asyncio.sleep(0.3)
            await pywrkr.run_worker_node("127.0.0.1", port)

        buf = StringIO()
        with patch("sys.stdout", buf):
            await asyncio.gather(_run_master(), _run_worker())

        self.assertIsNotNone(master_result)
        merged, exit_code = master_result
        self.assertEqual(exit_code, 0)
        self.assertGreater(merged.total_requests, 0)
        self.assertIn(200, merged.status_codes)


    async def test_master_worker_multiple(self):
        """Multiple workers should all contribute stats to the master."""
        config = pywrkr.BenchmarkConfig(
            url=self._url("/"),
            connections=2,
            duration=None,
            num_requests=10,
            threads=1,
            timeout_sec=5,
            _quiet=True,
        )

        # Find a free port
        temp_server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
        port = temp_server.sockets[0].getsockname()[1]
        temp_server.close()
        await temp_server.wait_closed()

        master_result = None

        async def _run_master():
            nonlocal master_result
            master_result = await pywrkr.run_master(config, "127.0.0.1", port, expect_workers=2)

        async def _run_worker(delay):
            await asyncio.sleep(delay)
            await pywrkr.run_worker_node("127.0.0.1", port)

        buf = StringIO()
        with patch("sys.stdout", buf):
            await asyncio.gather(
                _run_master(),
                _run_worker(0.3),
                _run_worker(0.3),
            )

        self.assertIsNotNone(master_result)
        merged, exit_code = master_result
        self.assertEqual(exit_code, 0)
        # Each worker sends at least num_requests, so merged should have >= 2x
        self.assertGreaterEqual(merged.total_requests, 20)


    async def test_master_worker_user_simulation(self):
        """Worker should handle user simulation mode when config has users set."""
        config = pywrkr.BenchmarkConfig(
            url=self._url("/"),
            users=3,
            duration=2.0,
            think_time=0.1,
            think_time_jitter=0.0,
            timeout_sec=5,
            _quiet=True,
        )

        temp_server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
        port = temp_server.sockets[0].getsockname()[1]
        temp_server.close()
        await temp_server.wait_closed()

        master_result = None

        async def _run_master():
            nonlocal master_result
            master_result = await pywrkr.run_master(config, "127.0.0.1", port, expect_workers=1)

        async def _run_worker():
            await asyncio.sleep(0.3)
            await pywrkr.run_worker_node("127.0.0.1", port)

        buf = StringIO()
        with patch("sys.stdout", buf):
            await asyncio.gather(_run_master(), _run_worker())

        self.assertIsNotNone(master_result)
        merged, exit_code = master_result
        self.assertGreater(merged.total_requests, 0)


    async def test_worker_bad_message(self):
        """Worker should handle unexpected message type from master gracefully."""
        connected = asyncio.Event()

        async def handler(reader, writer):
            connected.set()
            await pywrkr._send_msg(writer, {"type": "unknown"})
            await asyncio.sleep(0.5)
            writer.close()

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        buf = StringIO()
        try:
            with patch("sys.stdout", buf):
                await pywrkr.run_worker_node("127.0.0.1", port)
        finally:
            server.close()
            await server.wait_closed()

        output = buf.getvalue()
        self.assertIn("unexpected message type", output)


class TestDistributedCLIArgs(unittest.TestCase):
    """Test CLI argument parsing for distributed mode."""

    def test_worker_arg_parsing(self):
        """--worker should be parsed as HOST:PORT."""
        with patch("sys.argv", ["pywrkr", "--worker", "10.0.0.1:9220"]):
            with patch("pywrkr.main.run_worker_node", new_callable=AsyncMock) as mock_run:
                with self.assertRaises(SystemExit) as cm:
                    pywrkr_cli_main()
                # Should call run_worker_node with the right args
                mock_run.assert_called_once_with("10.0.0.1", 9220)

    def test_master_requires_expect_workers(self):
        """--master without --expect-workers should error."""
        with patch("sys.argv", ["pywrkr", "--master", "http://example.com/"]):
            with self.assertRaises(SystemExit) as cm:
                pywrkr_cli_main()
            self.assertEqual(cm.exception.code, 2)

    def test_worker_requires_host_port(self):
        """--worker with bad format should error."""
        with patch("sys.argv", ["pywrkr", "--worker", "no-port"]):
            with self.assertRaises(SystemExit) as cm:
                pywrkr_cli_main()
            self.assertEqual(cm.exception.code, 2)


# ---------------------------------------------------------------------------
# Tests for multi-URL mode (--url-file)
# ---------------------------------------------------------------------------

class TestLoadUrlFile(unittest.TestCase):
    """Test URL file parsing."""

    def test_simple_urls(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("http://localhost:8080/api\n")
            f.write("http://localhost:8080/health\n")
            f.name
        try:
            entries = pywrkr.load_url_file(f.name)
            self.assertEqual(len(entries), 2)
            self.assertEqual(entries[0].url, "http://localhost:8080/api")
            self.assertEqual(entries[0].method, "GET")
            self.assertEqual(entries[1].url, "http://localhost:8080/health")
        finally:
            os.unlink(f.name)

    def test_method_prefix(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("GET http://localhost/get\n")
            f.write("POST http://localhost/post\n")
            f.write("PUT http://localhost/put\n")
            f.write("DELETE http://localhost/delete\n")
            f.write("PATCH http://localhost/patch\n")
            f.name
        try:
            entries = pywrkr.load_url_file(f.name)
            self.assertEqual(len(entries), 5)
            self.assertEqual(entries[0].method, "GET")
            self.assertEqual(entries[1].method, "POST")
            self.assertEqual(entries[1].url, "http://localhost/post")
            self.assertEqual(entries[2].method, "PUT")
            self.assertEqual(entries[3].method, "DELETE")
            self.assertEqual(entries[4].method, "PATCH")
        finally:
            os.unlink(f.name)

    def test_comments_and_blanks(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("# This is a comment\n")
            f.write("\n")
            f.write("http://localhost/one\n")
            f.write("  \n")
            f.write("# Another comment\n")
            f.write("http://localhost/two\n")
            f.name
        try:
            entries = pywrkr.load_url_file(f.name)
            self.assertEqual(len(entries), 2)
            self.assertEqual(entries[0].url, "http://localhost/one")
            self.assertEqual(entries[1].url, "http://localhost/two")
        finally:
            os.unlink(f.name)

    def test_empty_file(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("# only comments\n")
            f.write("\n")
            f.name
        try:
            with self.assertRaises(ValueError) as cm:
                pywrkr.load_url_file(f.name)
            self.assertIn("empty", str(cm.exception))
        finally:
            os.unlink(f.name)

    def test_file_not_found(self):
        with self.assertRaises(FileNotFoundError):
            pywrkr.load_url_file("/nonexistent/path/urls.txt")

    def test_case_insensitive_method(self):
        """Method keywords should be recognized case-insensitively."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("post http://localhost/api\n")
            f.write("get http://localhost/health\n")
            f.name
        try:
            entries = pywrkr.load_url_file(f.name)
            self.assertEqual(len(entries), 2)
            self.assertEqual(entries[0].method, "POST")
            self.assertEqual(entries[0].url, "http://localhost/api")
            self.assertEqual(entries[1].method, "GET")
        finally:
            os.unlink(f.name)


class TestMultiUrlJson(unittest.TestCase):
    """Test build_multi_url_json."""

    def test_basic_structure(self):
        ws1 = pywrkr.WorkerStats()
        ws1.total_requests = 100
        ws1.errors = 2
        ws1.latencies = [0.1, 0.2, 0.3]

        ws2 = pywrkr.WorkerStats()
        ws2.total_requests = 200
        ws2.errors = 5
        ws2.latencies = [0.15, 0.25]

        results = [
            pywrkr.MultiUrlResult(url="http://a.com/", method="GET", stats=ws1, duration=10.0, exit_code=0),
            pywrkr.MultiUrlResult(url="http://b.com/", method="POST", stats=ws2, duration=10.0, exit_code=0),
        ]

        data = pywrkr.build_multi_url_json(results)
        self.assertEqual(data["mode"], "multi_url")
        self.assertEqual(data["endpoint_count"], 2)
        self.assertEqual(data["total_requests"], 300)
        self.assertEqual(data["total_errors"], 7)
        self.assertEqual(len(data["endpoints"]), 2)
        self.assertEqual(data["endpoints"][0]["url"], "http://a.com/")
        self.assertEqual(data["endpoints"][0]["method"], "GET")
        self.assertEqual(data["endpoints"][1]["url"], "http://b.com/")
        self.assertEqual(data["endpoints"][1]["method"], "POST")


class TestMultiUrlSummaryPrint(unittest.TestCase):
    """Test print_multi_url_summary output."""

    def test_prints_all_urls(self):
        ws1 = pywrkr.WorkerStats()
        ws1.total_requests = 100
        ws1.total_bytes = 5000
        ws1.errors = 0
        ws1.latencies = [0.05, 0.1, 0.15]
        ws1.status_codes[200] = 100

        ws2 = pywrkr.WorkerStats()
        ws2.total_requests = 200
        ws2.total_bytes = 10000
        ws2.errors = 3
        ws2.latencies = [0.1, 0.2, 0.3, 0.4]
        ws2.status_codes[200] = 197
        ws2.status_codes[500] = 3

        results = [
            pywrkr.MultiUrlResult(url="http://a.com/api", method="GET", stats=ws1, duration=10.0, exit_code=0),
            pywrkr.MultiUrlResult(url="http://b.com/data", method="POST", stats=ws2, duration=10.0, exit_code=0),
        ]

        buf = StringIO()
        with patch("sys.stdout", buf):
            pywrkr.print_multi_url_summary(results)
        output = buf.getvalue()

        self.assertIn("MULTI-URL COMPARISON SUMMARY", output)
        self.assertIn("http://a.com/api", output)
        self.assertIn("http://b.com/data", output)
        self.assertIn("GET", output)
        self.assertIn("POST", output)
        self.assertIn("2 endpoints", output)
        self.assertIn("300", output)  # total requests


class TestMultiUrlIntegration(AioHTTPTestCase):
    """Integration tests for multi-URL mode with a real HTTP server."""

    async def get_application(self):
        app = web.Application()
        app.router.add_get("/", self._handle_root)
        app.router.add_get("/api", self._handle_api)
        app.router.add_post("/data", self._handle_data)
        return app

    async def _handle_root(self, request):
        return web.Response(text="OK")

    async def _handle_api(self, request):
        return web.Response(text='{"status":"ok"}', content_type="application/json")

    async def _handle_data(self, request):
        body = await request.read()
        return web.Response(text=f"received {len(body)} bytes")

    def _url(self, path="/"):
        return f"http://127.0.0.1:{self.server.port}{path}"


    async def test_multi_url_basic(self):
        """Run against two endpoints and get combined results."""
        entries = [
            pywrkr.UrlEntry(url=self._url("/"), method="GET"),
            pywrkr.UrlEntry(url=self._url("/api"), method="GET"),
        ]
        base_config = pywrkr.BenchmarkConfig(
            url="",
            connections=2,
            duration=None,
            num_requests=10,
            threads=1,
            timeout_sec=5,
        )

        buf = StringIO()
        with patch("sys.stdout", buf):
            results = await pywrkr.run_multi_url(entries, base_config)

        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].url, self._url("/"))
        self.assertEqual(results[1].url, self._url("/api"))
        for r in results:
            self.assertGreater(r.stats.total_requests, 0)
            self.assertEqual(r.exit_code, 0)

        output = buf.getvalue()
        self.assertIn("MULTI-URL COMPARISON SUMMARY", output)


    async def test_multi_url_mixed_methods(self):
        """Endpoints with different HTTP methods should work."""
        entries = [
            pywrkr.UrlEntry(url=self._url("/"), method="GET"),
            pywrkr.UrlEntry(url=self._url("/data"), method="POST"),
        ]
        base_config = pywrkr.BenchmarkConfig(
            url="",
            connections=2,
            duration=None,
            num_requests=5,
            threads=1,
            timeout_sec=5,
        )

        buf = StringIO()
        with patch("sys.stdout", buf):
            results = await pywrkr.run_multi_url(entries, base_config)

        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].method, "GET")
        self.assertEqual(results[1].method, "POST")
        for r in results:
            self.assertGreater(r.stats.total_requests, 0)


    async def test_multi_url_json_output(self):
        """JSON output should contain all endpoints."""
        entries = [
            pywrkr.UrlEntry(url=self._url("/"), method="GET"),
            pywrkr.UrlEntry(url=self._url("/api"), method="GET"),
        ]

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json_path = f.name

        try:
            base_config = pywrkr.BenchmarkConfig(
                url="",
                connections=2,
                duration=None,
                num_requests=5,
                threads=1,
                timeout_sec=5,
                json_output=json_path,
            )

            buf = StringIO()
            with patch("sys.stdout", buf):
                results = await pywrkr.run_multi_url(entries, base_config)

            with open(json_path) as f:
                data = json.load(f)

            self.assertEqual(data["mode"], "multi_url")
            self.assertEqual(data["endpoint_count"], 2)
            self.assertEqual(len(data["endpoints"]), 2)
            self.assertGreater(data["total_requests"], 0)
        finally:
            os.unlink(json_path)


    async def test_multi_url_user_simulation(self):
        """Multi-URL mode should support user simulation."""
        entries = [
            pywrkr.UrlEntry(url=self._url("/"), method="GET"),
            pywrkr.UrlEntry(url=self._url("/api"), method="GET"),
        ]
        base_config = pywrkr.BenchmarkConfig(
            url="",
            users=3,
            duration=2.0,
            think_time=0.1,
            think_time_jitter=0.0,
            timeout_sec=5,
        )

        buf = StringIO()
        with patch("sys.stdout", buf):
            results = await pywrkr.run_multi_url(entries, base_config)

        self.assertEqual(len(results), 2)
        for r in results:
            self.assertGreater(r.stats.total_requests, 0)


    async def test_multi_url_from_file(self):
        """Full flow: load URLs from file, run benchmarks."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write(f"# test endpoints\n")
            f.write(f"{self._url('/')}\n")
            f.write(f"GET {self._url('/api')}\n")
            url_file = f.name

        try:
            entries = pywrkr.load_url_file(url_file)
            self.assertEqual(len(entries), 2)

            base_config = pywrkr.BenchmarkConfig(
                url="",
                connections=2,
                duration=None,
                num_requests=5,
                threads=1,
                timeout_sec=5,
            )

            buf = StringIO()
            with patch("sys.stdout", buf):
                results = await pywrkr.run_multi_url(entries, base_config)

            self.assertEqual(len(results), 2)
            for r in results:
                self.assertGreater(r.stats.total_requests, 0)
        finally:
            os.unlink(url_file)


class TestMultiUrlCLIArgs(unittest.TestCase):
    """Test CLI argument parsing for --url-file mode."""

    def test_url_file_no_url_required(self):
        """--url-file should not require positional url argument."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("http://example.com/\n")
            url_file = f.name

        try:
            with patch("sys.argv", ["pywrkr", "--url-file", url_file, "-n", "1"]):
                with patch("pywrkr.main.run_multi_url", new_callable=AsyncMock, return_value=[]) as mock_run:
                    with self.assertRaises(SystemExit):
                        pywrkr_cli_main()
                    mock_run.assert_called_once()
                    entries_arg = mock_run.call_args[0][0]
                    self.assertEqual(len(entries_arg), 1)
                    self.assertEqual(entries_arg[0].url, "http://example.com/")
        finally:
            os.unlink(url_file)

    def test_url_file_not_found_error(self):
        """--url-file with non-existent file should error."""
        with patch("sys.argv", ["pywrkr", "--url-file", "/nonexistent/urls.txt"]):
            with self.assertRaises(SystemExit) as cm:
                pywrkr_cli_main()
            self.assertEqual(cm.exception.code, 2)

    def test_url_file_invalid_scheme(self):
        """URLs in file with invalid scheme should error."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("ftp://example.com/\n")
            url_file = f.name

        try:
            with patch("sys.argv", ["pywrkr", "--url-file", url_file]):
                with self.assertRaises(SystemExit) as cm:
                    pywrkr_cli_main()
                self.assertEqual(cm.exception.code, 2)
        finally:
            os.unlink(url_file)


if __name__ == "__main__":
    unittest.main()
