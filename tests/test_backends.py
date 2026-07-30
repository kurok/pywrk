#!/usr/bin/env python3
"""Tests for the pluggable HTTP backends and --http2."""

import asyncio
import json
import os
import tempfile
import unittest
from io import StringIO
from unittest.mock import patch

import pywrkr
from pywrkr.backends import (
    ALL_PHASES,
    HTTP2_INSTALL_HINT,
    HTTPX_PHASES,
    AiohttpBackend,
    BackendResponse,
    BackendUnavailableError,
    HttpxBackend,
    _import_httpx,
    build_ssl_context,
    create_backend,
    create_cookie_jar,
    http2_available,
    normalize_http_version,
    target_is_ip_literal,
)
from pywrkr.config import LatencyBreakdown
from pywrkr.main import _build_parser, _parse_and_validate_args

HTTP2_READY = http2_available()
requires_http2 = unittest.skipUnless(HTTP2_READY, "requires pywrkr[http2]")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


class TestNormalizeHttpVersion(unittest.TestCase):
    def test_http1(self):
        for raw in ("1.1", "HTTP/1.1", "http/1.0", "1.0"):
            self.assertEqual(normalize_http_version(raw), "1.1", raw)

    def test_http2(self):
        # httpx says "HTTP/2", an ASGI scope says "2", ALPN says "h2".
        for raw in ("2", "HTTP/2", "h2", "2.0"):
            self.assertEqual(normalize_http_version(raw), "2", raw)

    def test_http3(self):
        for raw in ("HTTP/3", "h3"):
            self.assertEqual(normalize_http_version(raw), "3", raw)

    def test_unknown(self):
        for raw in (None, "", "weird"):
            self.assertEqual(normalize_http_version(raw), "unknown", repr(raw))


class TestTargetIsIpLiteral(unittest.TestCase):
    def test_ipv4_and_ipv6(self):
        self.assertTrue(target_is_ip_literal("http://127.0.0.1:8080/x"))
        self.assertTrue(target_is_ip_literal("http://[::1]:8080/"))

    def test_hostname(self):
        self.assertFalse(target_is_ip_literal("https://api.example.com/"))

    def test_no_host(self):
        self.assertFalse(target_is_ip_literal("nonsense"))


class TestBuildSslContext(unittest.TestCase):
    def test_plain_http_gets_none(self):
        self.assertIsNone(build_ssl_context(pywrkr.BenchmarkConfig(url="http://example.com/")))

    def test_https_unverified(self):
        ctx = build_ssl_context(pywrkr.BenchmarkConfig(url="https://example.com/"))
        self.assertIsNotNone(ctx)
        self.assertFalse(ctx.check_hostname)

    def test_https_verified(self):
        config = pywrkr.BenchmarkConfig(
            url="https://example.com/", ssl_config=pywrkr.SSLConfig(verify=True)
        )
        self.assertTrue(build_ssl_context(config).check_hostname)


class TestCookieJarFactory(unittest.IsolatedAsyncioTestCase):
    async def test_sessions_off_yields_dummy(self):
        import aiohttp

        config = pywrkr.BenchmarkConfig(url="http://example.com/", session_cookies=False)
        self.assertIsInstance(create_cookie_jar(config), aiohttp.DummyCookieJar)

    async def test_plain_mode_keeps_the_library_default(self):
        config = pywrkr.BenchmarkConfig(url="http://example.com/")
        self.assertIsNone(create_cookie_jar(config, isolate_cookies=False))

    async def test_ip_target_opens_the_jar(self):
        config = pywrkr.BenchmarkConfig(url="http://127.0.0.1:8080/")
        self.assertTrue(create_cookie_jar(config)._unsafe)


# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------


class TestCreateBackend(unittest.IsolatedAsyncioTestCase):
    async def test_default_is_aiohttp(self):
        backend = create_backend(pywrkr.BenchmarkConfig(url="http://example.com/"), 4)
        self.addAsyncCleanup(backend.aclose)
        self.assertIsInstance(backend, AiohttpBackend)
        self.assertEqual(backend.name, "aiohttp")
        self.assertEqual(backend.phases, ALL_PHASES)
        self.assertIn("HTTP/1.1", backend.describe)

    async def test_connector_limit_is_the_pool_limit(self):
        backend = create_backend(pywrkr.BenchmarkConfig(url="http://example.com/"), 7)
        self.addAsyncCleanup(backend.aclose)
        self.assertEqual(backend.connector.limit, 7)

    async def test_aclose_is_idempotent(self):
        backend = create_backend(pywrkr.BenchmarkConfig(url="http://example.com/"), 1)
        await backend.aclose()
        await backend.aclose()

    @requires_http2
    async def test_http2_selects_httpx(self):
        backend = create_backend(pywrkr.BenchmarkConfig(url="https://example.com/", http2=True), 4)
        self.addAsyncCleanup(backend.aclose)
        self.assertIsInstance(backend, HttpxBackend)
        self.assertEqual(backend.name, "httpx")
        # No hooks for the connection phases.
        self.assertEqual(backend.phases, HTTPX_PHASES)
        self.assertNotIn("dns", backend.phases)

    @requires_http2
    async def test_describe_distinguishes_alpn_from_prior_knowledge(self):
        tls = create_backend(pywrkr.BenchmarkConfig(url="https://example.com/", http2=True), 1)
        clear = create_backend(pywrkr.BenchmarkConfig(url="http://example.com/", http2=True), 1)
        self.addAsyncCleanup(tls.aclose)
        self.addAsyncCleanup(clear.aclose)
        self.assertIn("ALPN", tls.describe)
        self.assertIn("prior knowledge", clear.describe)

    async def test_missing_extra_names_the_pip_command(self):
        import sys

        with patch.dict(sys.modules, {"httpx": None}):
            with self.assertRaises(BackendUnavailableError) as ctx:
                _import_httpx()
        self.assertIn(HTTP2_INSTALL_HINT, str(ctx.exception))


class TestHttp2Cli(unittest.TestCase):
    def _parse(self, argv):
        parser = _build_parser()
        return _parse_and_validate_args(parser, parser.parse_args(argv))

    def test_default_is_off(self):
        config, _ = self._parse(["http://example.com"])
        self.assertFalse(config.http2)

    @requires_http2
    def test_flag_reaches_the_config(self):
        config, _ = self._parse(["--http2", "http://example.com"])
        self.assertTrue(config.http2)

    def test_missing_extra_is_a_startup_error(self):
        with (
            patch("pywrkr.main.http2_available", return_value=False),
            self.assertRaises(SystemExit),
            patch("sys.stderr", new_callable=StringIO) as err,
        ):
            self._parse(["--http2", "http://example.com"])
        self.assertIn(HTTP2_INSTALL_HINT, err.getvalue())

    @requires_http2
    def test_latency_breakdown_combination_warns(self):
        with self.assertLogs("pywrkr.main", level="WARNING") as logs:
            self._parse(["--http2", "--latency-breakdown", "http://example.com"])
        self.assertIn("only TTFB and transfer", logs.output[0])


# ---------------------------------------------------------------------------
# Phase availability
# ---------------------------------------------------------------------------


class TestPhaseAvailability(unittest.TestCase):
    def test_full_breakdown_reports_every_phase(self):
        samples = [
            LatencyBreakdown(dns=0.001, connect=0.002, tls=0.003, ttfb=0.01, transfer=0.005)
            for _ in range(5)
        ]
        agg = pywrkr.aggregate_breakdowns(samples)
        for phase in ("dns", "connect", "tls", "ttfb", "transfer", "total"):
            self.assertIn(phase, agg)
        self.assertIn("new_connections", agg)

    def test_reduced_breakdown_omits_unmeasured_phases(self):
        # The httpx backend cannot see DNS/TCP/TLS. Reporting them as 0.00us
        # would invent an impossibly fast connection phase.
        samples = [
            LatencyBreakdown(ttfb=0.01, transfer=0.005, available=HTTPX_PHASES) for _ in range(5)
        ]
        agg = pywrkr.aggregate_breakdowns(samples)
        self.assertIn("ttfb", agg)
        self.assertIn("transfer", agg)
        self.assertIn("total", agg)
        for phase in ("dns", "connect", "tls"):
            self.assertNotIn(phase, agg)

    def test_connection_counts_omitted_when_unobservable(self):
        # "200 new connections" under HTTP/2 would be flatly wrong: that is one
        # connection carrying 200 streams.
        samples = [LatencyBreakdown(ttfb=0.01, available=HTTPX_PHASES) for _ in range(3)]
        agg = pywrkr.aggregate_breakdowns(samples)
        self.assertNotIn("new_connections", agg)
        self.assertNotIn("reused_connections", agg)

    def test_mixed_samples_intersect(self):
        samples = [
            LatencyBreakdown(dns=0.001, ttfb=0.01),
            LatencyBreakdown(ttfb=0.01, available=HTTPX_PHASES),
        ]
        agg = pywrkr.aggregate_breakdowns(samples)
        self.assertIn("ttfb", agg)
        self.assertNotIn("dns", agg)

    def test_empty(self):
        self.assertEqual(pywrkr.aggregate_breakdowns([]), {})


class TestProtocolReporting(unittest.TestCase):
    def _stats(self, versions):
        stats = pywrkr.WorkerStats()
        stats.total_requests = sum(versions.values())
        for version, count in versions.items():
            stats.http_versions[version] = count
        return stats

    def _render(self, versions, http2=True):
        from pywrkr.reporting import _print_console_results

        out = StringIO()
        _print_console_results(
            self._stats(versions),
            1.0,
            1,
            0.0,
            pywrkr.BenchmarkConfig(url="http://example.com/", http2=http2),
            None,
            out,
        )
        return out.getvalue()

    def test_protocol_section_shown_for_http2(self):
        text = self._render({"2": 100})
        self.assertIn("NEGOTIATED PROTOCOL", text)
        self.assertIn("HTTP/2:", text)

    def test_h1_fallback_is_warned_about(self):
        text = self._render({"1.1": 100})
        self.assertIn("did not use HTTP/2", text)
        self.assertIn("not HTTP/2 numbers", text)

    def test_mixed_protocols_warn_about_the_h1_share(self):
        text = self._render({"2": 90, "1.1": 10})
        self.assertIn("10 request(s) did not use HTTP/2", text)

    def test_no_warning_when_all_http2(self):
        self.assertNotIn("did not use HTTP/2", self._render({"2": 50}))

    def test_section_hidden_for_a_plain_http1_run(self):
        self.assertNotIn("NEGOTIATED PROTOCOL", self._render({"1.1": 50}, http2=False))

    def test_json_carries_the_counts(self):
        results = pywrkr.build_results_dict(self._stats({"2": 7}), 1.0, 1)
        self.assertEqual(results["http_versions"], {"2": 7})


class TestStatsPlumbing(unittest.TestCase):
    def test_merge_sums_protocol_counts(self):
        a, b = pywrkr.WorkerStats(), pywrkr.WorkerStats()
        a.http_versions["2"] = 5
        b.http_versions["2"] = 3
        b.http_versions["1.1"] = 2
        merged = pywrkr.merge_stats([a, b])
        self.assertEqual(dict(merged.http_versions), {"2": 8, "1.1": 2})

    def test_distributed_round_trip(self):
        from pywrkr.distributed import _deserialize_stats, _serialize_stats

        stats = pywrkr.WorkerStats()
        stats.http_versions["2"] = 11
        restored = _deserialize_stats(json.loads(json.dumps(_serialize_stats(stats))))
        self.assertEqual(dict(restored.http_versions), {"2": 11})

    def test_config_round_trip(self):
        from pywrkr.distributed import _deserialize_config, _serialize_config

        config = pywrkr.BenchmarkConfig(url="https://example.com", http2=True)
        restored = _deserialize_config(json.loads(json.dumps(_serialize_config(config))))
        self.assertTrue(restored.http2)

    def test_config_default_when_absent(self):
        from pywrkr.distributed import _deserialize_config, _serialize_config

        payload = _serialize_config(pywrkr.BenchmarkConfig(url="https://example.com"))
        del payload["http2"]
        self.assertFalse(_deserialize_config(payload).http2)


# ---------------------------------------------------------------------------
# Integration against a real HTTP/2 server
# ---------------------------------------------------------------------------


class _H2Server:
    """A hypercorn server speaking cleartext HTTP/2 (h2c) on an ephemeral port.

    Cleartext keeps the fixture cert-free: there is no ALPN handshake, so the
    client reaches HTTP/2 by prior knowledge, which is exactly the path
    ``--http2`` takes against an ``http://`` target.
    """

    def __init__(self) -> None:
        self.port = 0
        self._shutdown = asyncio.Event()
        self._task: "asyncio.Task | None" = None
        self.protocols: list[str] = []

    async def _app(self, scope, receive, send):
        if scope["type"] != "http":
            return
        self.protocols.append(scope.get("http_version", "?"))
        # Drain the request body. Leaving it unread makes hypercorn's h2 stream
        # bookkeeping blow up on requests that actually carry one.
        while True:
            message = await receive()
            if message["type"] != "http.request" or not message.get("more_body"):
                break
        body = b'{"ok": true}'
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    async def start(self) -> None:
        import socket

        from hypercorn.asyncio import serve
        from hypercorn.config import Config

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        self.port = sock.getsockname()[1]
        sock.close()

        config = Config()
        config.bind = [f"127.0.0.1:{self.port}"]
        config.accesslog = None
        config.errorlog = None
        self._task = asyncio.create_task(
            serve(self._app, config, shutdown_trigger=self._shutdown.wait)
        )
        await self._wait_ready()

    async def _wait_ready(self) -> None:
        for _ in range(100):
            try:
                _, writer = await asyncio.open_connection("127.0.0.1", self.port)
                writer.close()
                await writer.wait_closed()
                return
            except OSError:
                await asyncio.sleep(0.05)
        raise RuntimeError("h2 test server did not come up")

    async def stop(self) -> None:
        self._shutdown.set()
        if self._task is not None:
            with __import__("contextlib").suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._task, timeout=5)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"


@requires_http2
class TestHttp2Integration(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server = _H2Server()
        await self.server.start()
        self.addAsyncCleanup(self.server.stop)

    def _config(self, **kwargs):
        defaults = {"connections": 4, "duration": 1.0, "threads": 1, "timeout_sec": 5}
        return pywrkr.BenchmarkConfig(url=self.server.url, **{**defaults, **kwargs})

    async def _run(self, **kwargs):
        with patch("sys.stdout", new_callable=StringIO) as out:
            stats, code = await pywrkr.run_benchmark(self._config(**kwargs))
        return stats, code, out.getvalue()

    async def test_http2_is_actually_negotiated(self):
        stats, code, text = await self._run(http2=True)
        self.assertEqual(code, 0)
        self.assertGreater(stats.total_requests, 0)
        self.assertEqual(stats.errors, 0)
        # Both ends agree it was HTTP/2.
        self.assertEqual(dict(stats.http_versions), {"2": stats.total_requests})
        self.assertTrue(all(v == "2" for v in self.server.protocols))
        self.assertIn("NEGOTIATED PROTOCOL", text)
        self.assertNotIn("did not use HTTP/2", text)

    async def test_default_backend_stays_http1(self):
        stats, code, _ = await self._run()
        self.assertEqual(code, 0)
        self.assertEqual(dict(stats.http_versions), {"1.1": stats.total_requests})
        self.assertTrue(all(v == "1.1" for v in self.server.protocols))

    async def test_request_count_mode(self):
        stats, _, _ = await self._run(http2=True, duration=None, num_requests=50)
        self.assertEqual(stats.total_requests, 50)
        self.assertEqual(stats.errors, 0)

    async def test_user_simulation_mode(self):
        config = self._config(http2=True, users=3, think_time=0.0, ramp_up=0.0, duration=1.0)
        with patch("sys.stdout", new_callable=StringIO):
            stats, code = await pywrkr.run_user_simulation(config)
        self.assertEqual(code, 0)
        self.assertGreater(stats.total_requests, 3)
        self.assertEqual(dict(stats.http_versions), {"2": stats.total_requests})

    async def test_rate_limited_mode(self):
        stats, _, _ = await self._run(http2=True, rate=50.0, duration=1.0)
        self.assertGreater(stats.total_requests, 0)
        self.assertEqual(stats.errors, 0)

    async def test_thresholds_apply(self):
        stats, code, _ = await self._run(
            http2=True, thresholds=[pywrkr.parse_threshold("p95 < 1us")]
        )
        self.assertEqual(code, 2)

    async def test_scenario_mode(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "scenario.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "name": "h2 flow",
                    "steps": [
                        {"name": "one", "path": "/", "assert_status": 200},
                        {
                            "name": "two",
                            "path": "/next",
                            "assert_status": 200,
                            "extract": {"ok": {"json": "$.ok"}},
                        },
                    ],
                },
                handle,
            )
        config = self._config(
            http2=True,
            users=2,
            think_time=0.0,
            ramp_up=0.0,
            duration=1.0,
            scenario=pywrkr.load_scenario(path),
        )
        with patch("sys.stdout", new_callable=StringIO):
            stats, code = await pywrkr.run_user_simulation(config)
        self.assertEqual(code, 0)
        self.assertEqual(stats.errors, 0)
        self.assertEqual(set(stats.step_latencies), {"one", "two"})
        self.assertEqual(dict(stats.http_versions), {"2": stats.total_requests})

    async def test_latency_breakdown_degrades_without_fake_zeros(self):
        stats, _, text = await self._run(
            http2=True, latency_breakdown=True, num_requests=25, duration=None
        )
        self.assertGreater(len(stats.breakdowns), 0)
        for sample in stats.breakdowns:
            self.assertEqual(sample.available, HTTPX_PHASES)
            self.assertGreater(sample.ttfb, 0.0)
        agg = pywrkr.aggregate_breakdowns(list(stats.breakdowns))
        self.assertIn("ttfb", agg)
        for phase in ("dns", "connect", "tls"):
            self.assertNotIn(phase, agg)
        self.assertIn("TTFB:", text)
        self.assertNotIn("DNS Lookup:", text)
        self.assertIn("not observable on this backend", text)

    async def test_post_body_round_trips(self):
        stats, _, _ = await self._run(
            http2=True, method="POST", body=b'{"hello": 1}', num_requests=10, duration=None
        )
        self.assertEqual(stats.total_requests, 10)
        self.assertEqual(stats.errors, 0)

    async def test_json_output_records_the_protocol(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "results.json")
        await self._run(http2=True, num_requests=20, duration=None, json_output=path)
        with open(path, encoding="utf-8") as handle:
            results = json.load(handle)
        self.assertEqual(results["http_versions"], {"2": 20})


class TestBackendResponseShape(unittest.TestCase):
    def test_is_slotted(self):
        resp = BackendResponse(status=200, body=b"x", headers={}, http_version="2")
        self.assertFalse(hasattr(resp, "__dict__"))


if __name__ == "__main__":
    unittest.main()
