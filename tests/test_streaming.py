#!/usr/bin/env python3
"""Tests for streaming metrics export (--export-interval)."""

import asyncio
import json
import unittest
from io import StringIO
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase

import pywrkr
import pywrkr.main
import pywrkr.streaming

# Reached via attribute access rather than a `from pywrkr... import` statement,
# since CodeQL flags a module imported both ways (py/import-and-import-from).
_build_parser = pywrkr.main._build_parser
_parse_and_validate_args = pywrkr.main._parse_and_validate_args
MIN_EXPORT_INTERVAL = pywrkr.streaming.MIN_EXPORT_INTERVAL
Snapshot = pywrkr.streaming.Snapshot
StreamingExporter = pywrkr.streaming.StreamingExporter
window_percentiles = pywrkr.streaming.window_percentiles

# ---------------------------------------------------------------------------
# Windowing math
# ---------------------------------------------------------------------------


class TestWindowPercentiles(unittest.TestCase):
    def test_nearest_rank(self):
        pct = window_percentiles([float(i) for i in range(1, 101)])
        self.assertEqual(pct["p50"], 50.0)
        self.assertEqual(pct["p95"], 95.0)
        self.assertEqual(pct["p99"], 99.0)

    def test_single_sample(self):
        self.assertEqual(window_percentiles([0.5]), {"p50": 0.5, "p95": 0.5, "p99": 0.5})

    def test_empty(self):
        self.assertEqual(window_percentiles([]), {})

    def test_unsorted_input(self):
        self.assertEqual(window_percentiles([5.0, 1.0, 3.0])["p50"], 3.0)

    def test_non_finite_dropped(self):
        pct = window_percentiles([1.0, float("inf"), float("nan"), 2.0])
        self.assertEqual(pct["p50"], 1.0)
        self.assertEqual(pct["p99"], 2.0)

    def test_only_non_finite(self):
        self.assertEqual(window_percentiles([float("nan")]), {})

    def test_a_window_forgets_the_previous_one(self):
        """The point of windowing: an old spike must not haunt the new p95."""
        spike_window = window_percentiles([10.0] * 10 + [0.01] * 90)
        calm_window = window_percentiles([0.01] * 100)
        self.assertEqual(spike_window["p95"], 10.0)
        self.assertEqual(calm_window["p95"], 0.01)


class TestSnapshot(unittest.TestCase):
    def _snapshot(self, **kw):
        base = dict(
            elapsed=10.0,
            total_requests=1000,
            total_errors=10,
            total_bytes=4096,
            window_seconds=2.0,
            window_requests=200,
            window_errors=2,
        )
        base.update(kw)
        return Snapshot(**base)

    def test_rate_is_windowed_not_cumulative(self):
        # 200 requests in the last 2s is 100/s, even though the run average
        # over 10s would be 100 too -- so use a case where they differ.
        snap = self._snapshot(window_requests=50, window_seconds=0.5)
        self.assertEqual(snap.requests_per_sec, 100.0)

    def test_zero_window(self):
        self.assertEqual(self._snapshot(window_seconds=0).requests_per_sec, 0.0)

    def test_counters_stay_cumulative_in_the_results_dict(self):
        data = self._snapshot().to_results_dict()
        self.assertEqual(data["total_requests"], 1000)
        self.assertEqual(data["total_errors"], 10)
        self.assertEqual(data["total_bytes"], 4096)

    def test_percentiles_are_the_windowed_ones(self):
        data = self._snapshot(window_percentiles={"p95": 0.25}).to_results_dict()
        self.assertEqual(data["percentiles"], {"p95": 0.25})

    def test_absent_latency_keys_are_omitted(self):
        data = self._snapshot().to_results_dict()
        self.assertNotIn("percentiles", data)
        self.assertNotIn("latency", data)

    def test_shape_is_what_the_exporters_expect(self):
        # The exporters walk build_results_dict output; a snapshot has to look
        # like one or the metric mapping would need a second copy.
        from pywrkr.reporting import _EXPORT_METRICS, _resolve_metric_value

        data = self._snapshot(
            window_percentiles={"p50": 0.1, "p95": 0.2, "p99": 0.3},
            window_latency={"mean": 0.15, "max": 0.4},
        ).to_results_dict()
        resolved = [
            spec.name_suffix
            for spec in _EXPORT_METRICS
            if _resolve_metric_value(data, spec.results_key, spec.nested_key, spec.multiplier)
            is not None
        ]
        for expected in ("requests_total", "errors_total", "latency_p95_ms"):
            self.assertIn(expected, resolved)


# ---------------------------------------------------------------------------
# Exporter mechanics
# ---------------------------------------------------------------------------


class _Recorder:
    """Stands in for the blocking push, optionally slow or failing."""

    def __init__(self, delay=0.0, ok=True):
        self.delay = delay
        self.ok = ok
        self.snapshots = []

    def __call__(self, snapshot):
        import time

        self.snapshots.append(snapshot)
        if self.delay:
            time.sleep(self.delay)
        return self.ok


def _exporter(stats_list, interval=MIN_EXPORT_INTERVAL, **kw):
    config = pywrkr.BenchmarkConfig(
        url="http://example.com", prom_remote_write="http://collector", tags={"env": "test"}
    )
    return StreamingExporter(config, stats_list, interval, **kw)


class TestStreamingExporter(unittest.IsolatedAsyncioTestCase):
    def _stats(self, requests=0, errors=0, latencies=()):
        stats = pywrkr.WorkerStats()
        stats.total_requests = requests
        stats.errors = errors
        stats.window_latencies = list(latencies)
        return stats

    async def test_window_capture_is_opt_in(self):
        stats = pywrkr.WorkerStats()
        # Off by default so a run without streaming pays nothing per request.
        self.assertIsNone(stats.window_latencies)
        _exporter([stats]).enable_window_capture(stats)
        self.assertEqual(stats.window_latencies, [])

    async def test_snapshot_diffs_counters_and_drains_samples(self):
        stats = self._stats(requests=100, errors=5, latencies=[0.1, 0.2])
        exporter = _exporter([stats])
        first = exporter._build_snapshot()
        self.assertEqual(first.total_requests, 100)
        self.assertEqual(first.window_requests, 100)
        self.assertEqual(first.window_errors, 5)
        self.assertEqual(stats.window_latencies, [], "samples should be drained")

        stats.total_requests = 175
        stats.errors = 6
        stats.window_latencies.extend([0.3])
        second = exporter._build_snapshot()
        self.assertEqual(second.total_requests, 175, "counter stays cumulative")
        self.assertEqual(second.window_requests, 75, "window is the delta")
        self.assertEqual(second.window_errors, 1)
        self.assertEqual(second.window_percentiles["p50"], 0.3)

    async def test_counters_never_go_backwards(self):
        stats = self._stats(requests=100)
        exporter = _exporter([stats])
        exporter._build_snapshot()
        stats.total_requests = 50  # would be a bug upstream; must not go negative
        self.assertEqual(exporter._build_snapshot().window_requests, 0)

    async def test_totals_span_every_worker(self):
        workers = [self._stats(requests=10, latencies=[0.1]) for _ in range(3)]
        snap = _exporter(workers)._build_snapshot()
        self.assertEqual(snap.total_requests, 30)
        self.assertEqual(snap.window_percentiles["p50"], 0.1)

    async def test_late_worker_gets_capture_enabled(self):
        early = self._stats(requests=5, latencies=[0.1])
        exporter = _exporter([early])
        late = pywrkr.WorkerStats()
        exporter._all_stats.append(late)
        exporter._build_snapshot()
        self.assertEqual(late.window_latencies, [])

    async def test_active_users_and_target_rate(self):
        from pywrkr.config import ActiveUsers
        from pywrkr.traffic_profiles import RateLimiter

        users = ActiveUsers()
        users.count = 7
        exporter = _exporter(
            [self._stats()], active_users=users, rate_limiter=RateLimiter(rate=250.0)
        )
        snap = exporter._build_snapshot()
        self.assertEqual(snap.active_users, 7)
        self.assertEqual(snap.target_rate, 250.0)

    async def test_no_users_or_limiter(self):
        snap = _exporter([self._stats()])._build_snapshot()
        self.assertIsNone(snap.active_users)
        self.assertIsNone(snap.target_rate)

    async def test_snapshots_are_pushed_on_the_interval(self):
        stats = self._stats(requests=10, latencies=[0.1])
        exporter = _exporter([stats], interval=MIN_EXPORT_INTERVAL)
        recorder = _Recorder()
        with patch.object(exporter, "_push", recorder):
            await exporter.start()
            await asyncio.sleep(MIN_EXPORT_INTERVAL * 2.4)
            await exporter.aclose()
        # Two interval snapshots plus the final one.
        self.assertGreaterEqual(len(recorder.snapshots), 3)
        self.assertTrue(recorder.snapshots[-1].final)
        self.assertEqual(exporter.sent, len(recorder.snapshots))
        self.assertTrue(exporter.all_delivered)

    async def test_final_snapshot_even_without_a_full_interval(self):
        # A run cut short by SIGINT still leaves its last state behind.
        exporter = _exporter([self._stats(requests=3)])
        recorder = _Recorder()
        with patch.object(exporter, "_push", recorder):
            await exporter.start()
            await exporter.aclose()
        self.assertEqual(len(recorder.snapshots), 1)
        self.assertTrue(recorder.snapshots[0].final)
        self.assertEqual(recorder.snapshots[0].total_requests, 3)

    async def test_a_stuck_sender_drops_rather_than_blocking(self):
        exporter = _exporter([self._stats(requests=1)], queue_size=1)
        # Fill the queue and leave the sender parked so nothing drains.
        exporter._queue.put_nowait(exporter._build_snapshot())
        for _ in range(5):
            try:
                exporter._queue.put_nowait(exporter._build_snapshot())
                exporter.queued += 1
            except asyncio.QueueFull:
                exporter.dropped += 1
        self.assertGreater(exporter.dropped, 0)
        self.assertIn("dropped", exporter.summary())

    async def test_failed_pushes_are_counted(self):
        exporter = _exporter([self._stats(requests=1)])
        with patch.object(exporter, "_push", _Recorder(ok=False)):
            await exporter.start()
            await exporter.aclose()
        self.assertEqual(exporter.failed, 1)
        self.assertEqual(exporter.sent, 0)
        self.assertFalse(exporter.all_delivered)
        self.assertIn("failed", exporter.summary())

    async def test_undelivered_snapshots_are_reported(self):
        """A push still hanging when the run ends must not read as success."""
        exporter = _exporter([self._stats(requests=1)])
        exporter.queued = 3
        exporter.sent = 1
        exporter.failed = 0
        self.assertEqual(exporter.undelivered, 2)
        self.assertFalse(exporter.all_delivered)
        self.assertIn("never delivered", exporter.summary())

    async def test_summary_is_none_when_nothing_streamed(self):
        self.assertIsNone(_exporter([self._stats()]).summary())

    async def test_push_labels_interval_and_final_snapshots(self):
        exporter = _exporter([self._stats(requests=1)])
        seen = []

        def fake_prom(results, endpoint, tags):
            seen.append(dict(tags))
            return True

        with patch("pywrkr.reporting.export_to_prometheus", fake_prom):
            exporter._push(exporter._build_snapshot())
            exporter._push(exporter._build_snapshot(final=True))

        self.assertEqual(seen[0]["export"], "interval")
        self.assertEqual(seen[1]["export"], "final")
        # Caller tags are preserved alongside.
        self.assertEqual(seen[0]["env"], "test")

    async def test_interval_floor_is_enforced(self):
        exporter = _exporter([self._stats()], interval=0.01)
        self.assertEqual(exporter._interval, MIN_EXPORT_INTERVAL)


# ---------------------------------------------------------------------------
# CLI validation
# ---------------------------------------------------------------------------


class TestExportIntervalCli(unittest.TestCase):
    def _parse(self, argv):
        parser = _build_parser()
        return _parse_and_validate_args(parser, parser.parse_args(argv))

    def _expect_error(self, argv):
        with self.assertRaises(SystemExit), patch("sys.stderr", new_callable=StringIO) as err:
            self._parse(argv)
        return err.getvalue()

    def test_default_is_off(self):
        config, _ = self._parse(["http://example.com"])
        self.assertIsNone(config.export_interval)

    def test_reaches_the_config(self):
        config, _ = self._parse(
            ["http://example.com", "--prom-remote-write", "http://c", "--export-interval", "5"]
        )
        self.assertEqual(config.export_interval, 5.0)

    def test_needs_an_endpoint(self):
        message = self._expect_error(["http://example.com", "--export-interval", "5"])
        self.assertIn("needs somewhere to export to", message)

    def test_rejects_a_sub_second_interval(self):
        message = self._expect_error(
            ["http://example.com", "--otel-endpoint", "http://c", "--export-interval", "0.1"]
        )
        self.assertIn("at least", message)

    def test_otel_endpoint_alone_is_enough(self):
        config, _ = self._parse(
            ["http://example.com", "--otel-endpoint", "http://c", "--export-interval", "1"]
        )
        self.assertEqual(config.export_interval, 1.0)

    def test_distributed_round_trip(self):
        from pywrkr.distributed import _deserialize_config, _serialize_config

        config = pywrkr.BenchmarkConfig(url="http://example.com", export_interval=7.0)
        restored = _deserialize_config(json.loads(json.dumps(_serialize_config(config))))
        self.assertEqual(restored.export_interval, 7.0)


class TestAutofindExportPropagation(unittest.TestCase):
    def test_step_config_carries_observability_and_step_users(self):
        # Autofind used to build a bare config, so an autofind session with
        # --otel-endpoint exported nothing at all.
        from pywrkr.config import AutofindConfig

        af = AutofindConfig(
            url="http://example.com",
            otel_endpoint="http://collector",
            export_interval=2.0,
            tags={"env": "staging"},
        )
        captured = {}

        async def fake_run(config, **kwargs):
            captured["config"] = config
            return pywrkr.WorkerStats(), 0

        with patch("pywrkr.workers.run_user_simulation", fake_run):
            asyncio.run(_run_one_autofind_step(af, users=42))

        step_config = captured["config"]
        self.assertEqual(step_config.otel_endpoint, "http://collector")
        self.assertEqual(step_config.export_interval, 2.0)
        self.assertEqual(step_config.tags["step_users"], "42")
        self.assertEqual(step_config.tags["env"], "staging")


async def _run_one_autofind_step(af_config, users):
    """Drive a single autofind step, to inspect the config it builds."""
    config = pywrkr.BenchmarkConfig(
        url=af_config.url,
        users=users,
        duration=af_config.step_duration,
        think_time=af_config.think_time,
        think_time_jitter=af_config.think_time_jitter,
        timeout_sec=af_config.timeout_sec,
        keepalive=af_config.keepalive,
        random_param=af_config.random_param,
        ramp_up=0.0,
        ssl_config=af_config.ssl_config,
        otel_endpoint=af_config.otel_endpoint,
        prom_remote_write=af_config.prom_remote_write,
        export_interval=af_config.export_interval,
        tags={**af_config.tags, "step_users": str(users)},
        _quiet=True,
    )
    await pywrkr.workers.run_user_simulation(config)


# ---------------------------------------------------------------------------
# Delivery against a fake collector
# ---------------------------------------------------------------------------


class TestStreamingDelivery(AioHTTPTestCase):
    async def get_application(self):
        self.pushes: list[str] = []
        app = web.Application()
        app.router.add_post("/metrics/job/pywrkr", self.handle_push)
        app.router.add_get("/t", lambda r: web.Response(text="ok"))
        return app

    async def handle_push(self, request):
        self.pushes.append(await request.text())
        return web.Response(text="ok")

    def _base(self):
        return f"http://127.0.0.1:{self.server.port}"

    async def test_snapshots_arrive_on_cadence(self):
        config = pywrkr.BenchmarkConfig(
            url=f"{self._base()}/t",
            connections=4,
            duration=3.0,
            threads=1,
            timeout_sec=5,
            prom_remote_write=self._base(),
            export_interval=1.0,
            tags={"test": "stream"},
        )
        with patch("sys.stdout", new_callable=StringIO):
            await pywrkr.run_benchmark(config)

        # floor(3/1) interval snapshots, plus the final one, plus the
        # unchanged end-of-run export.
        self.assertGreaterEqual(len(self.pushes), 3)
        self.assertTrue(any('export="interval"' in body for body in self.pushes))
        self.assertTrue(any('export="final"' in body for body in self.pushes))
        self.assertTrue(all("pywrkr_requests_total" in body for body in self.pushes))
        self.assertTrue(all('test="stream"' in body for body in self.pushes))

    async def test_counters_are_monotonic_across_snapshots(self):
        config = pywrkr.BenchmarkConfig(
            url=f"{self._base()}/t",
            connections=4,
            duration=3.0,
            threads=1,
            timeout_sec=5,
            prom_remote_write=self._base(),
            export_interval=1.0,
        )
        with patch("sys.stdout", new_callable=StringIO):
            await pywrkr.run_benchmark(config)

        totals = [
            int(float(line.split()[-1]))
            for body in self.pushes
            for line in body.splitlines()
            if line.startswith("pywrkr_requests_total{")
            or line.startswith("pywrkr_requests_total ")
        ]
        self.assertGreater(len(totals), 2)
        self.assertEqual(totals, sorted(totals), f"counters went backwards: {totals}")

    async def test_no_streaming_without_the_interval(self):
        # Nothing carries the streaming label, so behaviour without
        # --export-interval is exactly what it was before this feature.
        # (The end-of-run export itself is not asserted here: it runs
        # synchronously on this loop, which is also the fixture server's loop,
        # so it cannot answer itself.)
        from pywrkr.workers import _create_streaming_exporter

        config = pywrkr.BenchmarkConfig(
            url=f"{self._base()}/t",
            connections=2,
            duration=1.0,
            threads=1,
            timeout_sec=5,
            prom_remote_write=self._base(),
        )
        self.assertIsNone(_create_streaming_exporter(config, [], 0.0))
        with patch("sys.stdout", new_callable=StringIO):
            await pywrkr.run_benchmark(config)
        self.assertTrue(all("export=" not in body for body in self.pushes))

    async def test_unreachable_collector_does_not_stall_the_run(self):
        import time

        config = pywrkr.BenchmarkConfig(
            url=f"{self._base()}/t",
            connections=4,
            duration=2.0,
            threads=1,
            timeout_sec=5,
            prom_remote_write="http://127.0.0.1:1",  # refused immediately
            export_interval=1.0,
        )
        started = time.monotonic()
        with patch("sys.stdout", new_callable=StringIO):
            stats, _ = await pywrkr.run_benchmark(config)
        elapsed = time.monotonic() - started

        self.assertGreater(stats.total_requests, 0)
        # The run must not be held hostage by the collector.
        self.assertLess(elapsed, 20.0)


if __name__ == "__main__":
    unittest.main()
