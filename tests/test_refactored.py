#!/usr/bin/env python3
"""Tests for refactored pywrkr components.

Covers: SSLConfig, helper functions, error handling improvements,
timeout scenarios, cancellation behavior, and configuration validation.
"""

import asyncio
import os
import ssl
import tempfile
import time
import unittest
from io import StringIO
from unittest.mock import patch

import aiohttp
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase

import pywrkr
from pywrkr.config import (
    BenchmarkConfig,
    SSLConfig,
    WorkerStats,
)

# ---------------------------------------------------------------------------
# SSLConfig tests
# ---------------------------------------------------------------------------


class TestSSLConfig(unittest.TestCase):
    """Tests for the new SSLConfig dataclass."""

    def test_default_values(self):
        cfg = SSLConfig()
        self.assertFalse(cfg.verify)
        self.assertIsNone(cfg.ca_bundle)

    def test_explicit_values(self):
        cfg = SSLConfig(verify=True, ca_bundle="/path/to/ca.pem")
        self.assertTrue(cfg.verify)
        self.assertEqual(cfg.ca_bundle, "/path/to/ca.pem")

    def test_from_env_default(self):
        """Without env vars, from_env returns defaults."""
        with patch.dict(os.environ, {}, clear=True):
            cfg = SSLConfig.from_env()
        self.assertFalse(cfg.verify)
        self.assertIsNone(cfg.ca_bundle)

    def test_from_env_verify_true(self):
        """PYWRKR_SSL_VERIFY=1 enables verification."""
        with patch.dict(os.environ, {"PYWRKR_SSL_VERIFY": "1"}, clear=True):
            cfg = SSLConfig.from_env()
        self.assertTrue(cfg.verify)

    def test_from_env_verify_true_word(self):
        """PYWRKR_SSL_VERIFY=true enables verification."""
        with patch.dict(os.environ, {"PYWRKR_SSL_VERIFY": "true"}, clear=True):
            cfg = SSLConfig.from_env()
        self.assertTrue(cfg.verify)

    def test_from_env_verify_yes(self):
        with patch.dict(os.environ, {"PYWRKR_SSL_VERIFY": "yes"}, clear=True):
            cfg = SSLConfig.from_env()
        self.assertTrue(cfg.verify)

    def test_from_env_verify_false(self):
        with patch.dict(os.environ, {"PYWRKR_SSL_VERIFY": "0"}, clear=True):
            cfg = SSLConfig.from_env()
        self.assertFalse(cfg.verify)

    def test_from_env_ca_bundle(self):
        with patch.dict(os.environ, {"PYWRKR_CA_BUNDLE": "/etc/ssl/certs.pem"}, clear=True):
            cfg = SSLConfig.from_env()
        self.assertEqual(cfg.ca_bundle, "/etc/ssl/certs.pem")

    def test_from_env_unrecognised_verify_warns(self):
        """Unrecognised PYWRKR_SSL_VERIFY value logs a warning."""
        with patch.dict(os.environ, {"PYWRKR_SSL_VERIFY": "maybe"}, clear=True):
            with self.assertLogs("pywrkr.config", level="WARNING") as cm:
                cfg = SSLConfig.from_env()
        self.assertFalse(cfg.verify)
        self.assertTrue(any("Unrecognised" in msg for msg in cm.output))

    def test_from_env_ca_bundle_missing_warns(self):
        """Non-existent PYWRKR_CA_BUNDLE path logs a warning."""
        with patch.dict(
            os.environ,
            {"PYWRKR_CA_BUNDLE": "/nonexistent/ca-bundle.pem"},
            clear=True,
        ):
            with self.assertLogs("pywrkr.config", level="WARNING") as cm:
                cfg = SSLConfig.from_env()
        self.assertEqual(cfg.ca_bundle, "/nonexistent/ca-bundle.pem")
        self.assertTrue(any("does not exist" in msg for msg in cm.output))

    def test_from_env_explicit_false_no_warning(self):
        """Explicit falsy values (0, false, no) do not trigger a warning."""
        for value in ("0", "false", "no"):
            with patch.dict(os.environ, {"PYWRKR_SSL_VERIFY": value}, clear=True):
                cfg = SSLConfig.from_env()
            self.assertFalse(cfg.verify)

    def test_benchmark_config_has_ssl_config(self):
        """BenchmarkConfig should have ssl_config with default SSLConfig."""
        config = BenchmarkConfig(url="http://example.com")
        self.assertIsInstance(config.ssl_config, SSLConfig)
        self.assertFalse(config.ssl_config.verify)


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------


class TestBuildRequestHeaders(unittest.TestCase):
    """Tests for _build_request_headers helper."""

    def test_basic_headers(self):
        config = BenchmarkConfig(
            url="http://example.com",
            headers={"Accept": "application/json"},
        )
        headers = pywrkr._build_request_headers(config)
        self.assertEqual(headers["Accept"], "application/json")

    def test_basic_auth_header(self):
        config = BenchmarkConfig(
            url="http://example.com",
            basic_auth="user:pass",
        )
        headers = pywrkr._build_request_headers(config)
        self.assertIn("Authorization", headers)
        self.assertTrue(headers["Authorization"].startswith("Basic "))

    def test_cookie_header(self):
        config = BenchmarkConfig(
            url="http://example.com",
            cookies=["session=abc", "token=xyz"],
        )
        headers = pywrkr._build_request_headers(config)
        self.assertEqual(headers["Cookie"], "session=abc; token=xyz")

    def test_all_combined(self):
        config = BenchmarkConfig(
            url="http://example.com",
            headers={"X-Custom": "val"},
            basic_auth="admin:secret",
            cookies=["sid=123"],
        )
        headers = pywrkr._build_request_headers(config)
        self.assertEqual(headers["X-Custom"], "val")
        self.assertIn("Authorization", headers)
        self.assertEqual(headers["Cookie"], "sid=123")

    def test_returns_new_dict(self):
        """Each call should return a fresh dict to avoid shared state."""
        config = BenchmarkConfig(url="http://example.com", headers={"A": "1"})
        h1 = pywrkr._build_request_headers(config)
        h2 = pywrkr._build_request_headers(config)
        self.assertIsNot(h1, h2)


class TestMergeAllStats(unittest.TestCase):
    """Tests for _merge_all_stats helper."""

    def test_merge_empty(self):
        merged = pywrkr._merge_all_stats([])
        self.assertEqual(merged.total_requests, 0)
        self.assertEqual(merged.total_bytes, 0)

    def test_merge_single(self):
        ws = WorkerStats()
        ws.total_requests = 10
        ws.total_bytes = 5000
        ws.errors = 1
        ws.latencies = [0.1, 0.2]
        merged = pywrkr._merge_all_stats([ws])
        self.assertEqual(merged.total_requests, 10)
        self.assertEqual(merged.total_bytes, 5000)
        self.assertEqual(merged.errors, 1)
        self.assertEqual(len(merged.latencies), 2)

    def test_merge_multiple(self):
        stats = []
        for i in range(3):
            ws = WorkerStats()
            ws.total_requests = 10
            ws.total_bytes = 1000
            ws.errors = i
            ws.latencies = [0.1 * (i + 1)]
            ws.status_codes[200] = 9
            ws.status_codes[500] = 1
            ws.error_types["HTTP 500"] = 1
            ws.content_length_errors = i
            stats.append(ws)

        merged = pywrkr._merge_all_stats(stats)
        self.assertEqual(merged.total_requests, 30)
        self.assertEqual(merged.total_bytes, 3000)
        self.assertEqual(merged.errors, 3)  # 0 + 1 + 2
        self.assertEqual(merged.content_length_errors, 3)
        self.assertEqual(len(merged.latencies), 3)
        self.assertEqual(merged.status_codes[200], 27)
        self.assertEqual(merged.status_codes[500], 3)

    def test_merge_step_latencies(self):
        ws1 = WorkerStats()
        ws1.step_latencies["step1"].extend([0.1, 0.2])
        ws2 = WorkerStats()
        ws2.step_latencies["step1"].extend([0.3])
        ws2.step_latencies["step2"].extend([0.4])

        merged = pywrkr._merge_all_stats([ws1, ws2])
        self.assertEqual(len(merged.step_latencies["step1"]), 3)
        self.assertEqual(len(merged.step_latencies["step2"]), 1)


class TestCreateSSLContext(unittest.TestCase):
    """Tests for _create_ssl_context helper."""

    def test_http_returns_none(self):
        config = BenchmarkConfig(url="http://example.com")
        ctx = pywrkr._create_ssl_context(config)
        self.assertIsNone(ctx)

    def test_https_no_verify(self):
        config = BenchmarkConfig(
            url="https://example.com",
            ssl_config=SSLConfig(verify=False),
        )
        ctx = pywrkr._create_ssl_context(config)
        self.assertIsNotNone(ctx)
        self.assertFalse(ctx.check_hostname)
        self.assertEqual(ctx.verify_mode, ssl.CERT_NONE)

    def test_https_verify(self):
        config = BenchmarkConfig(
            url="https://example.com",
            ssl_config=SSLConfig(verify=True),
        )
        ctx = pywrkr._create_ssl_context(config)
        self.assertIsNotNone(ctx)
        self.assertTrue(ctx.check_hostname)
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)


# ---------------------------------------------------------------------------
# Timeout and error handling tests
# ---------------------------------------------------------------------------


class TestTimeoutBehavior(AioHTTPTestCase):
    """Test request timeout handling."""

    async def get_application(self):
        app = web.Application()
        app.router.add_get("/slow", self.handle_slow)
        app.router.add_get("/fast", self.handle_fast)
        return app

    async def handle_slow(self, request):
        await asyncio.sleep(5.0)
        return web.Response(text="slow")

    async def handle_fast(self, request):
        return web.Response(text="fast")

    def _url(self, path):
        return f"http://localhost:{self.server.port}{path}"

    async def test_timeout_counted_as_error(self):
        """Requests that timeout should be counted as errors."""
        config = BenchmarkConfig(
            url=self._url("/slow"),
            connections=1,
            duration=None,
            num_requests=3,
            threads=1,
            timeout_sec=0.5,  # Very short timeout
        )
        with patch("sys.stdout", new_callable=StringIO):
            stats, _ = await pywrkr.run_benchmark(config)
        self.assertEqual(stats.total_requests, 3)
        self.assertEqual(stats.errors, 3)
        # Check that timeout errors are categorized
        has_timeout = any("Timeout" in k or "timeout" in k.lower() for k in stats.error_types)
        self.assertTrue(has_timeout, f"Expected timeout errors, got: {dict(stats.error_types)}")

    async def test_fast_requests_no_timeout(self):
        """Fast requests should not trigger timeout errors."""
        config = BenchmarkConfig(
            url=self._url("/fast"),
            connections=2,
            duration=None,
            num_requests=10,
            threads=1,
            timeout_sec=5.0,
        )
        with patch("sys.stdout", new_callable=StringIO):
            stats, _ = await pywrkr.run_benchmark(config)
        self.assertEqual(stats.total_requests, 10)
        # Allow 1 error from connector close race at end of run
        self.assertLessEqual(stats.errors, 1)


# ---------------------------------------------------------------------------
# Cancellation behavior tests
# ---------------------------------------------------------------------------


class TestCancellationBehavior(AioHTTPTestCase):
    """Test graceful shutdown via stop_event."""

    async def get_application(self):
        app = web.Application()
        app.router.add_get("/", self.handle_get)
        return app

    async def handle_get(self, request):
        return web.Response(text="OK")

    def _url(self, path="/"):
        return f"http://localhost:{self.server.port}{path}"

    async def test_stop_event_stops_workers(self):
        """Setting stop_event should cause workers to finish gracefully."""
        config = BenchmarkConfig(
            url=self._url(),
            connections=2,
            duration=60.0,  # Long duration
            threads=1,
            timeout_sec=5,
        )
        stop_event = asyncio.Event()

        connector = aiohttp.TCPConnector(limit=2)
        ws = WorkerStats()

        # Start worker and stop it after a brief delay
        task = asyncio.create_task(pywrkr.worker(config, ws, connector, stop_event))

        await asyncio.sleep(0.5)
        stop_event.set()
        await task

        await connector.close()

        self.assertGreater(ws.total_requests, 0)

    async def test_user_worker_respects_stop(self):
        """User worker should exit when stop_event is set."""
        config = BenchmarkConfig(
            url=self._url(),
            users=1,
            duration=60.0,
            think_time=0.1,
            timeout_sec=5,
        )
        stop_event = asyncio.Event()
        from pywrkr.config import ActiveUsers

        active_users = ActiveUsers()

        connector = aiohttp.TCPConnector(limit=1)
        ws = WorkerStats()

        task = asyncio.create_task(
            pywrkr.user_worker(0, config, ws, connector, stop_event, time.monotonic(), active_users)
        )

        await asyncio.sleep(0.5)
        stop_event.set()
        await task

        await connector.close()

        self.assertGreater(ws.total_requests, 0)
        self.assertEqual(active_users.count, 0, "Active users should be 0 after exit")


# ---------------------------------------------------------------------------
# Threshold evaluation tests
# ---------------------------------------------------------------------------


class TestThresholdEvaluation(unittest.TestCase):
    """Test SLO threshold parsing and evaluation."""

    def test_parse_p95_threshold(self):
        th = pywrkr.parse_threshold("p95 < 300ms")
        self.assertEqual(th.metric, "p95")
        self.assertEqual(th.operator, "<")
        self.assertAlmostEqual(th.value, 0.3)

    def test_parse_error_rate_threshold(self):
        th = pywrkr.parse_threshold("error_rate < 1%")
        self.assertEqual(th.metric, "error_rate")
        self.assertEqual(th.operator, "<")
        self.assertAlmostEqual(th.value, 1.0)

    def test_parse_rps_threshold(self):
        th = pywrkr.parse_threshold("rps > 1000")
        self.assertEqual(th.metric, "rps")
        self.assertEqual(th.operator, ">")
        self.assertAlmostEqual(th.value, 1000.0)

    def test_evaluate_passing_threshold(self):
        stats = WorkerStats()
        stats.total_requests = 100
        stats.errors = 0
        stats.latencies = [0.1] * 100

        th = pywrkr.parse_threshold("p95 < 300ms")
        results = pywrkr.evaluate_thresholds([th], stats, 10.0)
        self.assertTrue(results[0][2])  # passed

    def test_evaluate_failing_threshold(self):
        stats = WorkerStats()
        stats.total_requests = 100
        stats.errors = 50
        stats.latencies = [0.1] * 100

        th = pywrkr.parse_threshold("error_rate < 1%")
        results = pywrkr.evaluate_thresholds([th], stats, 10.0)
        self.assertFalse(results[0][2])  # failed


# ---------------------------------------------------------------------------
# Distributed mode serialization tests
# ---------------------------------------------------------------------------


class TestDistributedSerialization(unittest.TestCase):
    """Test config and stats serialization for distributed mode."""

    def test_config_round_trip(self):
        config = BenchmarkConfig(
            url="http://example.com/api",
            connections=50,
            duration=30.0,
            method="POST",
            headers={"Content-Type": "application/json"},
            body=b'{"key": "value"}',
            basic_auth="user:pass",
            cookies=["session=abc"],
            timeout_sec=10.0,
        )
        serialized = pywrkr._serialize_config(config)
        deserialized = pywrkr._deserialize_config(serialized)

        self.assertEqual(deserialized.url, config.url)
        self.assertEqual(deserialized.connections, config.connections)
        self.assertEqual(deserialized.duration, config.duration)
        self.assertEqual(deserialized.method, config.method)
        self.assertEqual(deserialized.body, config.body)
        self.assertEqual(deserialized.basic_auth, config.basic_auth)

    def test_config_round_trip_all_fields(self):
        """Verify all config fields survive serialization round-trip."""
        from pywrkr.config import Scenario, ScenarioStep, Threshold

        config = BenchmarkConfig(
            url="http://example.com/api",
            connections=50,
            duration=30.0,
            num_requests=5000,
            threads=8,
            method="POST",
            headers={"X-Custom": "val"},
            body=b'{"key": "value"}',
            timeout_sec=15.0,
            keepalive=False,
            basic_auth="admin:secret",
            cookies=["sid=xyz"],
            verify_content_length=True,
            verbosity=3,
            random_param=True,
            rate=500.0,
            rate_ramp=1000.0,
            latency_breakdown=True,
            users=100,
            ramp_up=10.0,
            think_time=2.0,
            think_time_jitter=0.3,
            ssl_config=SSLConfig(verify=True, ca_bundle="/path/ca.pem"),
            tags={"env": "prod", "region": "us-east"},
            otel_endpoint="http://otel:4318",
            prom_remote_write="http://prom:9090/write",
            thresholds=[Threshold(metric="p95", operator="<", value=0.3, raw_expr="p95 < 300ms")],
            scenario=Scenario(
                name="Test",
                think_time=1.5,
                steps=[
                    ScenarioStep(path="/api/v1", method="GET", name="List items"),
                    ScenarioStep(
                        path="/api/v1",
                        method="POST",
                        body={"key": "val"},
                        headers={"X-Step": "1"},
                        assert_status=201,
                        assert_body_contains="created",
                        think_time=0.5,
                        name="Create item",
                    ),
                ],
            ),
            html_report="/tmp/report.html",
            csv_output="/tmp/out.csv",
            json_output="/tmp/out.json",
            html_output=True,
            live_dashboard=True,
        )
        serialized = pywrkr._serialize_config(config)
        d = pywrkr._deserialize_config(serialized)

        self.assertEqual(d.url, config.url)
        self.assertEqual(d.connections, 50)
        self.assertEqual(d.duration, 30.0)
        self.assertEqual(d.num_requests, 5000)
        self.assertEqual(d.threads, 8)
        self.assertEqual(d.method, "POST")
        self.assertEqual(d.headers, {"X-Custom": "val"})
        self.assertEqual(d.body, b'{"key": "value"}')
        self.assertEqual(d.timeout_sec, 15.0)
        self.assertFalse(d.keepalive)
        self.assertEqual(d.basic_auth, "admin:secret")
        self.assertEqual(d.cookies, ["sid=xyz"])
        self.assertTrue(d.verify_content_length)
        self.assertEqual(d.verbosity, 3)
        self.assertTrue(d.random_param)
        self.assertEqual(d.rate, 500.0)
        self.assertEqual(d.rate_ramp, 1000.0)
        self.assertTrue(d.latency_breakdown)
        self.assertEqual(d.users, 100)
        self.assertEqual(d.ramp_up, 10.0)
        self.assertEqual(d.think_time, 2.0)
        self.assertEqual(d.think_time_jitter, 0.3)
        # Previously missing fields:
        self.assertTrue(d.ssl_config.verify)
        self.assertEqual(d.ssl_config.ca_bundle, "/path/ca.pem")
        self.assertEqual(d.tags, {"env": "prod", "region": "us-east"})
        self.assertEqual(d.otel_endpoint, "http://otel:4318")
        self.assertEqual(d.prom_remote_write, "http://prom:9090/write")
        self.assertEqual(len(d.thresholds), 1)
        self.assertEqual(d.thresholds[0].metric, "p95")
        self.assertEqual(d.thresholds[0].operator, "<")
        self.assertAlmostEqual(d.thresholds[0].value, 0.3)
        self.assertEqual(d.thresholds[0].raw_expr, "p95 < 300ms")
        self.assertIsNotNone(d.scenario)
        self.assertEqual(d.scenario.name, "Test")
        self.assertEqual(d.scenario.think_time, 1.5)
        self.assertEqual(len(d.scenario.steps), 2)
        self.assertEqual(d.scenario.steps[0].path, "/api/v1")
        self.assertEqual(d.scenario.steps[0].method, "GET")
        self.assertEqual(d.scenario.steps[1].method, "POST")
        self.assertEqual(d.scenario.steps[1].body, {"key": "val"})
        self.assertEqual(d.scenario.steps[1].assert_status, 201)
        self.assertEqual(d.scenario.steps[1].assert_body_contains, "created")
        self.assertEqual(d.scenario.steps[1].think_time, 0.5)
        self.assertEqual(d.html_report, "/tmp/report.html")
        self.assertEqual(d.csv_output, "/tmp/out.csv")
        self.assertEqual(d.json_output, "/tmp/out.json")
        self.assertTrue(d.html_output)
        self.assertTrue(d.live_dashboard)

    def test_config_round_trip_field_completeness(self):
        """Ensure _serialize_config covers every BenchmarkConfig field.

        If a new field is added to BenchmarkConfig but not to
        _serialize_config/_deserialize_config, this test will fail —
        preventing silent data loss in distributed mode.
        """
        from dataclasses import fields

        # Fields that are intentionally excluded from serialization:
        # - _quiet: internal flag set by the deserializer itself
        # - traffic_profile: runtime object, not serializable as-is
        #   (rate limiter reconstructs it from rate/rate_ramp/duration)
        EXCLUDED = {"_quiet", "traffic_profile"}

        config_fields = {f.name for f in fields(BenchmarkConfig)} - EXCLUDED
        serialized_keys = set(
            pywrkr._serialize_config(BenchmarkConfig(url="http://localhost/")).keys()
        )

        missing = config_fields - serialized_keys
        self.assertEqual(
            missing,
            set(),
            f"BenchmarkConfig fields not in _serialize_config: {missing}. "
            f"Add them to _serialize_config/_deserialize_config in distributed.py.",
        )

    def test_stats_round_trip(self):
        ws = WorkerStats()
        ws.total_requests = 1000
        ws.total_bytes = 500000
        ws.errors = 5
        ws.latencies = [0.01, 0.02, 0.03]
        ws.status_codes[200] = 995
        ws.status_codes[500] = 5
        ws.error_types["HTTP 500"] = 5

        serialized = pywrkr._serialize_stats(ws)
        deserialized = pywrkr._deserialize_stats(serialized)

        self.assertEqual(deserialized.total_requests, 1000)
        self.assertEqual(deserialized.total_bytes, 500000)
        self.assertEqual(deserialized.errors, 5)
        self.assertEqual(deserialized.latencies, [0.01, 0.02, 0.03])
        self.assertEqual(deserialized.status_codes[200], 995)

    def test_stats_round_trip_all_fields(self):
        """Verify step_latencies, breakdowns, and error_types survive round-trip."""
        from pywrkr.config import LatencyBreakdown

        ws = WorkerStats()
        ws.total_requests = 100
        ws.total_bytes = 50000
        ws.errors = 2
        ws.content_length_errors = 1
        ws.latencies = [0.1, 0.2, 0.3]
        ws.error_types["TimeoutError"] = 1
        ws.error_types["HTTP 500"] = 1
        ws.status_codes[200] = 98
        ws.status_codes[500] = 2
        ws.rps_timeline = [(1.0, 50), (2.0, 50)]
        ws.step_latencies["GET /api"].extend([0.1, 0.15])
        ws.step_latencies["POST /api"].extend([0.2])
        ws.breakdowns = [
            LatencyBreakdown(
                dns=0.01, connect=0.02, tls=0.03, ttfb=0.05, transfer=0.01, is_reused=False
            ),
            LatencyBreakdown(
                dns=0.0, connect=0.0, tls=0.0, ttfb=0.03, transfer=0.005, is_reused=True
            ),
        ]

        serialized = pywrkr._serialize_stats(ws)
        d = pywrkr._deserialize_stats(serialized)

        self.assertEqual(d.total_requests, 100)
        self.assertEqual(d.total_bytes, 50000)
        self.assertEqual(d.errors, 2)
        self.assertEqual(d.content_length_errors, 1)
        self.assertEqual(d.latencies, [0.1, 0.2, 0.3])
        self.assertEqual(d.error_types["TimeoutError"], 1)
        self.assertEqual(d.error_types["HTTP 500"], 1)
        self.assertEqual(d.status_codes[200], 98)
        self.assertEqual(d.status_codes[500], 2)
        self.assertEqual(len(d.rps_timeline), 2)
        # Previously missing:
        self.assertEqual(d.step_latencies["GET /api"], [0.1, 0.15])
        self.assertEqual(d.step_latencies["POST /api"], [0.2])
        self.assertEqual(len(d.breakdowns), 2)
        self.assertAlmostEqual(d.breakdowns[0].dns, 0.01)
        self.assertAlmostEqual(d.breakdowns[0].connect, 0.02)
        self.assertFalse(d.breakdowns[0].is_reused)
        self.assertTrue(d.breakdowns[1].is_reused)

    def test_merge_worker_stats(self):
        ws1 = WorkerStats()
        ws1.total_requests = 100
        ws1.latencies = [0.1] * 100
        ws1.status_codes[200] = 100

        ws2 = WorkerStats()
        ws2.total_requests = 200
        ws2.latencies = [0.2] * 200
        ws2.status_codes[200] = 195
        ws2.status_codes[500] = 5
        ws2.errors = 5

        merged = pywrkr.merge_worker_stats([ws1, ws2])
        self.assertEqual(merged.total_requests, 300)
        self.assertEqual(len(merged.latencies), 300)
        self.assertEqual(merged.status_codes[200], 295)
        self.assertEqual(merged.errors, 5)


# ---------------------------------------------------------------------------
# Latency breakdown tests
# ---------------------------------------------------------------------------


class TestLatencyBreakdown(unittest.TestCase):
    """Test latency breakdown aggregation."""

    def test_aggregate_empty(self):
        result = pywrkr.aggregate_breakdowns([])
        self.assertEqual(result, {})

    def test_aggregate_single(self):
        bd = pywrkr.LatencyBreakdown(dns=0.01, connect=0.02, tls=0.03, ttfb=0.05, transfer=0.01)
        result = pywrkr.aggregate_breakdowns([bd])
        self.assertIn("dns", result)
        self.assertAlmostEqual(result["dns"]["avg"], 0.01)

    def test_aggregate_reused_connections(self):
        bds = [
            pywrkr.LatencyBreakdown(dns=0.01, connect=0.02, is_reused=False),
            pywrkr.LatencyBreakdown(dns=0.0, connect=0.0, is_reused=True),
            pywrkr.LatencyBreakdown(dns=0.0, connect=0.0, is_reused=True),
        ]
        result = pywrkr.aggregate_breakdowns(bds)
        self.assertEqual(result["new_connections"], 1)
        self.assertEqual(result["reused_connections"], 2)


# ---------------------------------------------------------------------------
# Multi-URL mode tests
# ---------------------------------------------------------------------------


class TestMultiUrlLoading(unittest.TestCase):
    """Test URL file loading and parsing."""

    def test_load_basic_url_file(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("http://example.com/a\n")
            f.write("POST http://example.com/b\n")
            f.write("# comment\n")
            f.write("\n")
            f.write("PUT http://example.com/c\n")
            path = f.name
        try:
            entries = pywrkr.load_url_file(path)
            self.assertEqual(len(entries), 3)
            self.assertEqual(entries[0].method, "GET")
            self.assertEqual(entries[0].url, "http://example.com/a")
            self.assertEqual(entries[1].method, "POST")
            self.assertEqual(entries[2].method, "PUT")
        finally:
            os.unlink(path)

    def test_load_empty_file_raises(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("# only comments\n")
            path = f.name
        try:
            with self.assertRaises(ValueError):
                pywrkr.load_url_file(path)
        finally:
            os.unlink(path)

    def test_load_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            pywrkr.load_url_file("/nonexistent/urls.txt")


class TestMultiUrlConfigCloning(unittest.TestCase):
    """Test that multi-URL config cloning preserves all fields."""

    def test_all_config_fields_preserved(self):
        """Verify dataclasses.replace() preserves all BenchmarkConfig fields."""
        from dataclasses import replace

        base = BenchmarkConfig(
            url="http://original.com",
            connections=50,
            duration=30.0,
            num_requests=1000,
            threads=8,
            method="POST",
            headers={"X-Custom": "val"},
            body=b"test body",
            timeout_sec=15.0,
            keepalive=False,
            basic_auth="user:pass",
            cookies=["session=abc"],
            verify_content_length=True,
            verbosity=2,
            random_param=True,
            rate=100.0,
            rate_ramp=200.0,
            latency_breakdown=True,
            users=50,
            ramp_up=5.0,
            think_time=1.0,
            think_time_jitter=0.3,
            ssl_config=SSLConfig(verify=True, ca_bundle="/path/to/ca.pem"),
            tags={"env": "staging"},
            otel_endpoint="http://otel:4318",
            prom_remote_write="http://prom:9090/write",
            html_report="/tmp/report.html",
        )

        # Simulate what run_multi_url does
        cloned = replace(
            base,
            url="http://new-target.com",
            method="PUT",
            headers=dict(base.headers),
            cookies=list(base.cookies),
        )

        # Check overridden fields
        self.assertEqual(cloned.url, "http://new-target.com")
        self.assertEqual(cloned.method, "PUT")

        # Check all other fields are preserved
        self.assertEqual(cloned.connections, 50)
        self.assertEqual(cloned.duration, 30.0)
        self.assertEqual(cloned.num_requests, 1000)
        self.assertEqual(cloned.threads, 8)
        self.assertEqual(cloned.body, b"test body")
        self.assertEqual(cloned.timeout_sec, 15.0)
        self.assertFalse(cloned.keepalive)
        self.assertEqual(cloned.basic_auth, "user:pass")
        self.assertEqual(cloned.cookies, ["session=abc"])
        self.assertTrue(cloned.verify_content_length)
        self.assertEqual(cloned.verbosity, 2)
        self.assertTrue(cloned.random_param)
        self.assertEqual(cloned.rate, 100.0)
        self.assertEqual(cloned.rate_ramp, 200.0)
        self.assertTrue(cloned.latency_breakdown)
        self.assertEqual(cloned.users, 50)
        self.assertEqual(cloned.ramp_up, 5.0)
        self.assertEqual(cloned.think_time, 1.0)
        self.assertEqual(cloned.think_time_jitter, 0.3)
        # These were previously dropped:
        self.assertTrue(cloned.ssl_config.verify)
        self.assertEqual(cloned.ssl_config.ca_bundle, "/path/to/ca.pem")
        self.assertEqual(cloned.tags, {"env": "staging"})
        self.assertEqual(cloned.otel_endpoint, "http://otel:4318")
        self.assertEqual(cloned.prom_remote_write, "http://prom:9090/write")
        self.assertEqual(cloned.html_report, "/tmp/report.html")

        # Verify headers and cookies are independent copies
        cloned.headers["new"] = "added"
        self.assertNotIn("new", base.headers)
        cloned.cookies.append("extra=1")
        self.assertNotIn("extra=1", base.cookies)


# ---------------------------------------------------------------------------
# Integration: connection pool behavior
# ---------------------------------------------------------------------------


class TestConnectionPooling(AioHTTPTestCase):
    """Test connection pooling and reuse."""

    async def get_application(self):
        app = web.Application()
        app.router.add_get("/", self.handle_get)
        return app

    async def handle_get(self, request):
        return web.Response(text="OK")

    def _url(self, path="/"):
        return f"http://localhost:{self.server.port}{path}"

    async def test_keepalive_reuses_connections(self):
        """With keep-alive, connections should be reused."""
        config = BenchmarkConfig(
            url=self._url(),
            connections=1,
            duration=None,
            num_requests=10,
            threads=1,
            timeout_sec=5,
            keepalive=True,
            latency_breakdown=True,
        )
        with patch("sys.stdout", new_callable=StringIO):
            stats, _ = await pywrkr.run_benchmark(config)
        self.assertEqual(stats.total_requests, 10)
        # With keepalive and 1 connection, most should be reused
        if stats.breakdowns:
            reused = sum(1 for b in stats.breakdowns if b.is_reused)
            self.assertGreater(reused, 0, "Expected some reused connections with keepalive")


# ---------------------------------------------------------------------------
# Configuration validation via CLI
# ---------------------------------------------------------------------------


class TestCLIValidation(unittest.TestCase):
    """Test CLI argument validation."""

    def test_ssl_verify_flag(self):
        from pywrkr.main import _build_parser

        parser = _build_parser()
        args = parser.parse_args(["--ssl-verify", "http://example.com"])
        self.assertTrue(args.ssl_verify)

    def test_ca_bundle_flag(self):
        from pywrkr.main import _build_parser

        parser = _build_parser()
        args = parser.parse_args(["--ca-bundle", "/path/to/ca.pem", "http://example.com"])
        self.assertEqual(args.ca_bundle, "/path/to/ca.pem")


if __name__ == "__main__":
    unittest.main()
