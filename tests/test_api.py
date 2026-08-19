#!/usr/bin/env python3
"""Tests for the public Python API (pywrkr.run / pywrkr.arun)."""

import asyncio
import contextlib
import io
import json
import os
import re
import signal
import tempfile
import unittest
import warnings
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase

import pywrkr
from pywrkr.api import EXIT_THRESHOLD_BREACH, Latency, Percentiles, Result, ThresholdVerdict


class TestPublicSurface(unittest.TestCase):
    def test_documented_names_are_exported(self):
        for name in (
            "run",
            "arun",
            "Config",
            "Result",
            "Latency",
            "Percentiles",
            "ThresholdVerdict",
            "LiveStats",
            "load_scenario",
            "__version__",
        ):
            self.assertIn(name, pywrkr.__all__, name)
            self.assertTrue(hasattr(pywrkr, name), name)

    def test_every_exported_name_resolves(self):
        missing = [name for name in pywrkr.__all__ if not hasattr(pywrkr, name)]
        self.assertEqual(missing, [])

    def test_config_is_the_benchmark_config(self):
        # One config type, so the library and the CLI cannot drift apart.
        self.assertIs(pywrkr.Config, pywrkr.BenchmarkConfig)

    def test_worker_internals_are_no_longer_public(self):
        for name in ("worker", "user_worker", "scenario_worker", "make_url", "LiveDashboard"):
            self.assertNotIn(name, pywrkr.__all__, name)

    def test_deprecated_names_still_import_with_a_warning(self):
        for name in ("worker", "user_worker", "scenario_worker", "make_url", "LiveDashboard"):
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                value = getattr(pywrkr, name)
            self.assertIsNotNone(value, name)
            self.assertEqual(len(caught), 1, name)
            self.assertTrue(issubclass(caught[0].category, DeprecationWarning), name)
            self.assertIn("pywrkr.workers", str(caught[0].message))

    def test_unknown_attribute_still_raises(self):
        with self.assertRaises(AttributeError):
            pywrkr.definitely_not_a_thing

    def test_py_typed_marker_is_shipped(self):
        marker = os.path.join(os.path.dirname(pywrkr.__file__), "py.typed")
        self.assertTrue(os.path.isfile(marker))


class TestResultShape(unittest.TestCase):
    def _result(self, **overrides):
        data = {
            "duration_sec": 2.0,
            "total_requests": 100,
            "total_errors": 5,
            "requests_per_sec": 50.0,
            "total_bytes": 2048,
            "status_codes": {"200": 95, "500": 5},
            "error_types": {"HTTP 500": 5},
            "http_versions": {"1.1": 100},
            "rps_timeline": [[0.0, 50], [1.0, 50]],
            "latency": {"min": 0.001, "max": 0.5, "mean": 0.02, "median": 0.018, "stdev": 0.01},
            "percentiles": {"p50": 0.018, "p95": 0.05, "p99": 0.12, "p99.9": 0.4},
            "step_stats": {"login": {"count": 50, "errors": 0}},
        }
        data.update(overrides)
        return Result(_data=data, stats=pywrkr.WorkerStats())

    def test_headline_numbers(self):
        r = self._result()
        self.assertEqual(r.total_requests, 100)
        self.assertEqual(r.total_errors, 5)
        self.assertEqual(r.requests_per_sec, 50.0)
        self.assertEqual(r.duration, 2.0)
        self.assertEqual(r.total_bytes, 2048)
        self.assertAlmostEqual(r.error_rate, 5.0)

    def test_error_rate_with_no_requests(self):
        self.assertEqual(self._result(total_requests=0, total_errors=0).error_rate, 0.0)

    def test_latency_is_typed(self):
        latency = self._result().latency
        self.assertIsInstance(latency, Latency)
        self.assertEqual(latency.mean, 0.02)

    def test_percentiles_by_attribute_and_key(self):
        pct = self._result().percentiles
        self.assertIsInstance(pct, Percentiles)
        self.assertEqual(pct.p95, 0.05)
        self.assertEqual(pct["p99.9"], 0.4)
        self.assertIn("p99.9", pct)
        self.assertEqual(pct.get("p99.99", 1.0), 1.0)
        self.assertEqual(pct.as_dict()["p50"], 0.018)

    def test_missing_percentile_reads_as_zero_by_attribute(self):
        self.assertEqual(self._result(percentiles={}).percentiles.p95, 0.0)

    def test_status_codes_are_ints(self):
        self.assertEqual(self._result().status_codes, {200: 95, 500: 5})

    def test_timeline_and_steps(self):
        r = self._result()
        self.assertEqual(r.rps_timeline, [(0.0, 50), (1.0, 50)])
        self.assertEqual(r.steps["login"]["count"], 50)
        self.assertEqual(r.http_versions, {"1.1": 100})

    def test_to_dict_is_a_copy(self):
        r = self._result()
        first = r.to_dict()
        first["total_requests"] = 999
        first["latency"]["mean"] = 999
        self.assertEqual(r.to_dict()["total_requests"], 100)
        self.assertEqual(r.to_dict()["latency"]["mean"], 0.02)

    def test_steps_is_a_copy(self):
        r = self._result()
        r.steps["login"]["count"] = 0
        self.assertEqual(r.steps["login"]["count"], 50)

    def test_to_json_round_trips(self):
        r = self._result()
        self.assertEqual(json.loads(r.to_json()), r.to_dict())

    def test_verdicts(self):
        passing = Result(_data={}, stats=pywrkr.WorkerStats(), thresholds=())
        self.assertTrue(passing.passed)
        self.assertEqual(passing.exit_code, 0)

        failing = Result(
            _data={},
            stats=pywrkr.WorkerStats(),
            thresholds=(ThresholdVerdict("p95 < 1ms", "p95", 0.5, False),),
        )
        self.assertFalse(failing.passed)
        self.assertEqual(failing.exit_code, EXIT_THRESHOLD_BREACH)


class TestArgumentHandling(unittest.IsolatedAsyncioTestCase):
    async def test_rejects_a_non_url_target(self):
        for bad in (None, 42, "", "   "):
            with self.assertRaises(TypeError, msg=repr(bad)):
                await pywrkr.arun(bad)

    async def test_rejects_unknown_options(self):
        with self.assertRaises(TypeError) as ctx:
            await pywrkr.arun("http://example.com", nonsense=1)
        self.assertIn("nonsense", str(ctx.exception))

    async def test_rejects_keywords_alongside_a_config(self):
        config = pywrkr.Config(url="http://example.com")
        with self.assertRaises(TypeError) as ctx:
            await pywrkr.arun(config, connections=5)
        self.assertIn("takes no other keyword arguments", str(ctx.exception))

    async def test_threshold_strings_are_parsed(self):
        from pywrkr.api import _build_config

        config = _build_config("http://example.com", {"thresholds": ["p95 < 300ms"]})
        self.assertEqual(len(config.thresholds), 1)
        self.assertEqual(config.thresholds[0].metric, "p95")

    async def test_threshold_objects_pass_through(self):
        from pywrkr.api import _build_config

        parsed = pywrkr.parse_threshold("rps > 10")
        config = _build_config("http://example.com", {"thresholds": [parsed]})
        self.assertIs(config.thresholds[0], parsed)

    async def test_a_bad_threshold_expression_raises(self):
        with self.assertRaises(ValueError):
            await pywrkr.arun("http://example.com", thresholds=["nonsense"])

    async def test_library_mode_is_quiet_by_construction(self):
        from pywrkr.api import _build_config

        self.assertTrue(_build_config("http://example.com", {})._quiet)

    async def test_the_caller_s_config_is_not_mutated(self):
        from pywrkr.api import _build_config

        config = pywrkr.Config(url="http://example.com")
        _build_config(config, {})
        self.assertFalse(getattr(config, "_quiet", False))


class TestRunFromEventLoop(unittest.TestCase):
    def test_run_refuses_a_running_loop(self):
        async def inner():
            with self.assertRaises(RuntimeError) as ctx:
                pywrkr.run("http://example.com", duration=1)
            return str(ctx.exception)

        message = asyncio.run(inner())
        self.assertIn("running event loop", message)
        self.assertIn("arun", message)


# ---------------------------------------------------------------------------
# Against a real server
# ---------------------------------------------------------------------------


class TestApiIntegration(AioHTTPTestCase):
    async def get_application(self):
        app = web.Application()
        app.router.add_get("/", lambda r: self._ok())
        app.router.add_get("/next", lambda r: self._ok())
        return app

    async def _ok(self):
        return web.json_response({"ok": True})

    def _url(self):
        return f"http://127.0.0.1:{self.server.port}/"

    async def test_arun_returns_a_populated_result(self):
        result = await pywrkr.arun(self._url(), connections=4, duration=1, threads=1)
        self.assertIsInstance(result, Result)
        self.assertGreater(result.total_requests, 0)
        self.assertEqual(result.total_errors, 0)
        self.assertGreater(result.requests_per_sec, 0)
        self.assertGreater(result.percentiles.p95, 0)
        self.assertEqual(result.status_codes, {200: result.total_requests})
        self.assertTrue(result.passed)
        self.assertEqual(result.exit_code, 0)

    async def test_arun_writes_nothing_to_stdout(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            await pywrkr.arun(self._url(), connections=2, duration=1, threads=1)
        self.assertEqual(buf.getvalue(), "")

    async def test_arun_leaves_signal_handlers_alone(self):
        watched = (signal.SIGINT, signal.SIGTERM)
        before = {s: signal.getsignal(s) for s in watched}
        await pywrkr.arun(self._url(), connections=2, duration=1, threads=1)
        self.assertEqual({s: signal.getsignal(s) for s in watched}, before)

    async def test_arun_never_exits(self):
        # A breached threshold is a verdict on the result, not sys.exit.
        result = await pywrkr.arun(
            self._url(), connections=2, duration=1, threads=1, thresholds=["p95 < 1us"]
        )
        self.assertFalse(result.passed)
        self.assertEqual(result.exit_code, EXIT_THRESHOLD_BREACH)
        self.assertEqual(len(result.thresholds), 1)
        verdict = result.thresholds[0]
        self.assertEqual(verdict.expression, "p95 < 1us")
        self.assertEqual(verdict.metric, "p95")
        self.assertGreater(verdict.actual, 0)

    async def test_thresholds_that_pass(self):
        result = await pywrkr.arun(
            self._url(),
            connections=2,
            duration=1,
            threads=1,
            thresholds=["p95 < 10s", "error_rate < 1%"],
        )
        self.assertTrue(result.passed)
        self.assertEqual([v.passed for v in result.thresholds], [True, True])

    async def test_on_tick_receives_live_stats(self):
        ticks = []
        await pywrkr.arun(self._url(), connections=2, duration=2, threads=1, on_tick=ticks.append)
        self.assertGreater(len(ticks), 0)
        first = ticks[0]
        self.assertIsInstance(first, pywrkr.LiveStats)
        self.assertGreater(first.total_requests, 0)
        self.assertGreater(first.requests_per_sec, 0)
        self.assertGreater(first.elapsed, 0)

    async def test_a_raising_on_tick_does_not_kill_the_run(self):
        def boom(_stats):
            raise ValueError("callback exploded")

        with self.assertLogs("pywrkr.workers", level="ERROR"):
            result = await pywrkr.arun(
                self._url(), connections=2, duration=2, threads=1, on_tick=boom
            )
        self.assertGreater(result.total_requests, 0)

    async def test_accepts_a_prebuilt_config(self):
        config = pywrkr.Config(url=self._url(), connections=3, duration=1, threads=1)
        result = await pywrkr.arun(config)
        self.assertGreater(result.total_requests, 0)

    async def test_user_simulation_mode(self):
        result = await pywrkr.arun(self._url(), users=3, duration=1, think_time=0.0, ramp_up=0.0)
        self.assertGreater(result.total_requests, 3)

    async def test_scenario_mode_reports_steps(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "flow.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "name": "flow",
                    "steps": [
                        {"name": "one", "path": "/", "assert_status": 200},
                        {"name": "two", "path": "/next", "assert_status": 200},
                    ],
                },
                handle,
            )
        config = pywrkr.Config(
            url=self._url(),
            users=2,
            duration=1,
            think_time=0.0,
            ramp_up=0.0,
            scenario=pywrkr.load_scenario(path),
        )
        result = await pywrkr.arun(config)
        self.assertEqual(set(result.steps), {"one", "two"})
        self.assertEqual(result.total_errors, 0)

    async def test_result_dict_matches_the_cli_json_schema(self):
        """The golden check: one schema, two front-ends.

        The API result and the CLI's --json file must have the same shape for
        the same kind of run, or consumers cannot move between them.
        """
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        json_path = os.path.join(tmp.name, "cli.json")

        cli_config = pywrkr.Config(
            url=self._url(), connections=3, duration=1, threads=1, json_output=json_path
        )
        with patch("sys.stdout", new_callable=io.StringIO):
            await pywrkr.run_benchmark(cli_config)
        with open(json_path, encoding="utf-8") as handle:
            cli_data = json.load(handle)

        api_data = (await pywrkr.arun(self._url(), connections=3, duration=1, threads=1)).to_dict()

        self.assertEqual(sorted(cli_data), sorted(api_data))
        # Fixed-shape blocks must match exactly.
        for key in ("latency", "config"):
            self.assertEqual(sorted(cli_data[key]), sorted(api_data[key]), key)
        self.assertEqual(cli_data["schema_version"], api_data["schema_version"])
        self.assertEqual(cli_data["config"], api_data["config"])

        # Percentiles are compared by vocabulary, not by key set: the tail
        # percentiles only appear once a run has enough samples to resolve them
        # (p99.9 needs 1000), so two independently measured runs can honestly
        # differ there. Asserting equality made this test flaky.
        for data in (cli_data, api_data):
            self.assertTrue(all(re.fullmatch(r"p\d+(\.\d+)?", k) for k in data["percentiles"]))
            for core in ("p50", "p95", "p99"):
                self.assertIn(core, data["percentiles"])

    async def test_result_json_is_loadable_by_the_compare_machinery(self):
        # A Result written to disk must work as a `pywrkr compare` baseline.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "baseline.json")
        result = await pywrkr.arun(self._url(), connections=2, duration=1, threads=1)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(result.to_json())

        loaded = pywrkr.load_results(path)
        report = pywrkr.compare_results(
            loaded, result.to_dict(), [pywrkr.parse_fail_on("p95 > +1%")]
        )
        self.assertEqual(report.config_warnings, [])
        self.assertFalse(report.regressed)


class TestSyncRun(AioHTTPTestCase):
    async def get_application(self):
        app = web.Application()
        app.router.add_get("/", lambda r: web.Response(text="ok"))
        return app

    def test_run_spins_its_own_loop(self):
        # Exercised outside the async test methods, since run() refuses to be
        # called from a running loop.
        url = f"http://127.0.0.1:{self.server.port}/"

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            result = _run_in_thread(url)
        self.assertGreater(result.total_requests, 0)
        self.assertEqual(buf.getvalue(), "")


def _run_in_thread(url):
    """Call pywrkr.run() off the test's event loop and return its Result."""
    import threading

    box = {}

    def target():
        box["result"] = pywrkr.run(url, connections=2, duration=1, threads=1)

    thread = threading.Thread(target=target)
    thread.start()
    thread.join(timeout=30)
    return box["result"]


if __name__ == "__main__":
    unittest.main()
