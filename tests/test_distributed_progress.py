"""Tests for worker→master progress streaming during a distributed run (#215).

Two properties carry the weight here, and neither is obvious from reading the
code: the cluster-wide counters must never go backwards however the workers'
reports interleave, and a worker that goes quiet must not keep inflating the
current window with figures from before it stopped.
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from aiohttp import web

from pywrkr.config import BenchmarkConfig
from pywrkr.distributed import (
    MSG_PROGRESS,
    PROGRESS_INTERVAL_KEY,
    ProgressAggregator,
    WorkerProgress,
    _deserialize_progress,
    _MasterExporter,
    _ProgressReporter,
    _serialize_snapshot,
    progress_interval_for,
    requested_progress_interval,
    run_master,
    run_worker_node,
)
from pywrkr.streaming import Snapshot


def snapshot(**kwargs) -> Snapshot:
    defaults = dict(
        elapsed=1.0,
        total_requests=100,
        total_errors=2,
        total_bytes=4096,
        window_seconds=1.0,
        window_requests=100,
        window_errors=2,
        window_samples=(0.01, 0.02, 0.03),
    )
    defaults.update(kwargs)
    return Snapshot(**defaults)


def progress(seq=1, at=0.0, **kwargs) -> WorkerProgress:
    defaults = dict(
        elapsed=1.0,
        total_requests=100,
        total_errors=0,
        total_bytes=1000,
        window_seconds=1.0,
        window_requests=100,
        window_errors=0,
        window_samples=[0.01, 0.02],
        active_users=10,
    )
    defaults.update(kwargs)
    return WorkerProgress(seq=seq, received_at=at, **defaults)


# ---------------------------------------------------------------------------
# Wire format
# ---------------------------------------------------------------------------


class TestSnapshotSerialization(unittest.TestCase):
    def test_a_snapshot_round_trips(self):
        payload = _serialize_snapshot(snapshot(), seq=7)
        restored = _deserialize_progress(payload, received_at=5.0)
        self.assertEqual(payload["type"], MSG_PROGRESS)
        self.assertEqual(restored.seq, 7)
        self.assertEqual(restored.total_requests, 100)
        self.assertEqual(restored.total_errors, 2)
        self.assertEqual(restored.total_bytes, 4096)
        self.assertEqual(restored.window_samples, [0.01, 0.02, 0.03])
        self.assertEqual(restored.received_at, 5.0)

    def test_the_payload_is_json_safe(self):
        import json

        json.dumps(_serialize_snapshot(snapshot(), seq=1))

    def test_raw_samples_travel_because_percentiles_cannot_be_merged(self):
        """Averaging two workers' p95s is not the cluster's p95."""
        payload = _serialize_snapshot(snapshot(window_samples=(0.5, 0.6)), seq=1)
        self.assertEqual(payload["window_samples"], [0.5, 0.6])

    def test_a_malformed_payload_degrades_to_zeros_rather_than_raising(self):
        restored = _deserialize_progress({"type": MSG_PROGRESS}, received_at=1.0)
        self.assertEqual(restored.total_requests, 0)
        self.assertEqual(restored.window_samples, [])
        self.assertIsNone(restored.active_users)

    def test_junk_values_are_rejected_per_field(self):
        restored = _deserialize_progress(
            {
                "seq": "nope",
                "total_requests": None,
                "window_samples": [0.1, "x", None, 0.2],
                "active_users": "many",
            },
            received_at=1.0,
        )
        self.assertEqual(restored.seq, 0)
        self.assertEqual(restored.total_requests, 0)
        self.assertEqual(restored.window_samples, [0.1, 0.2])
        self.assertIsNone(restored.active_users)

    def test_a_bool_is_not_accepted_as_a_number(self):
        restored = _deserialize_progress({"total_requests": True}, received_at=0.0)
        self.assertEqual(restored.total_requests, 0)


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


class TestProgressAggregator(unittest.TestCase):
    def agg(self, workers=2, interval=1.0) -> ProgressAggregator:
        return ProgressAggregator(workers, interval)

    def test_nothing_reported_yields_no_snapshot(self):
        self.assertIsNone(self.agg().build(now=1.0, elapsed=1.0))

    def test_counters_are_the_sum_of_each_worker_s_latest(self):
        agg = self.agg()
        agg.record(0, progress(seq=1, at=1.0, total_requests=100))
        agg.record(1, progress(seq=1, at=1.0, total_requests=150))
        first = agg.build(now=1.0, elapsed=1.0)
        self.assertEqual(first.total_requests, 250)

        # Second interval: both workers report their new cumulative totals.
        agg.record(0, progress(seq=2, at=2.0, total_requests=200))
        agg.record(1, progress(seq=2, at=2.0, total_requests=300))
        second = agg.build(now=2.0, elapsed=2.0)
        self.assertEqual(second.total_requests, 500)

    def test_reports_are_not_accumulated_on_top_of_each_other(self):
        """Summing every message received would climb at several times the truth."""
        agg = self.agg(workers=1)
        for seq, total in enumerate([100, 200, 300], start=1):
            agg.record(0, progress(seq=seq, at=float(seq), total_requests=total))
            built = agg.build(now=float(seq), elapsed=float(seq))
        self.assertEqual(built.total_requests, 300)

    def test_totals_never_decrease_when_workers_report_out_of_step(self):
        """The property that matters: rate() on a counter that goes backwards lies."""
        agg = self.agg(workers=3)
        totals = []
        schedule = [
            (0, 1, 100),
            (1, 1, 100),
            (0, 2, 250),
            (2, 1, 100),
            (1, 2, 260),
            (0, 3, 400),
            (2, 2, 300),
        ]
        for index, seq, total in schedule:
            agg.record(index, progress(seq=seq, at=1.0, total_requests=total))
            totals.append(agg.build(now=1.0, elapsed=1.0).total_requests)
        self.assertEqual(totals, sorted(totals), totals)

    def test_an_out_of_order_report_is_ignored(self):
        agg = self.agg(workers=1)
        agg.record(0, progress(seq=5, at=1.0, total_requests=500))
        agg.record(0, progress(seq=2, at=1.1, total_requests=200))
        self.assertEqual(agg.build(now=1.1, elapsed=1.1).total_requests, 500)

    def test_the_same_report_is_not_counted_in_two_windows(self):
        """A worker that misses an interval must not have its traffic replayed."""
        agg = self.agg(workers=1)
        agg.record(0, progress(seq=1, at=1.0, window_requests=80))
        first = agg.build(now=1.0, elapsed=1.0)
        second = agg.build(now=2.0, elapsed=2.0)
        self.assertEqual(first.window_requests, 80)
        self.assertEqual(second.window_requests, 0)
        # Cumulative totals are unaffected: those requests really happened.
        self.assertEqual(second.total_requests, first.total_requests)

    def test_window_samples_are_pooled_across_workers(self):
        agg = self.agg()
        agg.record(0, progress(seq=1, at=1.0, window_samples=[0.01] * 10))
        agg.record(1, progress(seq=1, at=1.0, window_samples=[1.0] * 10))
        built = agg.build(now=1.0, elapsed=1.0)
        # A pooled p95 lands in the slow worker's range; averaging the two
        # workers' own p95s would have produced something in between.
        self.assertGreater(built.window_percentiles["p95"], 0.5)
        self.assertLess(built.window_percentiles["p50"], 0.5)

    def test_active_users_are_summed_across_reporting_workers(self):
        agg = self.agg()
        agg.record(0, progress(seq=1, at=1.0, active_users=25))
        agg.record(1, progress(seq=1, at=1.0, active_users=30))
        self.assertEqual(agg.build(now=1.0, elapsed=1.0).active_users, 55)

    def test_a_worker_that_stops_reporting_is_named_stale(self):
        agg = self.agg(interval=1.0)
        agg.record(0, progress(seq=1, at=0.0))
        agg.record(1, progress(seq=1, at=10.0))
        self.assertEqual(agg.stale_workers(now=10.0), [0])

    def test_a_stale_worker_leaves_the_window_but_stays_in_the_totals(self):
        """Its requests happened; it is only the current window it cannot speak for."""
        agg = self.agg(interval=1.0)
        agg.record(0, progress(seq=1, at=0.0, total_requests=100, window_requests=100))
        agg.record(1, progress(seq=1, at=10.0, total_requests=200, window_requests=50))
        built = agg.build(now=10.0, elapsed=10.0)
        self.assertEqual(built.total_requests, 300)
        self.assertEqual(built.window_requests, 50)

    def test_a_stale_worker_is_excluded_from_active_users(self):
        agg = self.agg(interval=1.0)
        agg.record(0, progress(seq=1, at=0.0, active_users=100))
        agg.record(1, progress(seq=1, at=10.0, active_users=7))
        self.assertEqual(agg.build(now=10.0, elapsed=10.0).active_users, 7)

    def test_reporting_counts_the_workers_heard_from(self):
        agg = self.agg(workers=3)
        self.assertEqual(agg.reporting, 0)
        agg.record(0, progress())
        agg.record(2, progress())
        self.assertEqual(agg.reporting, 2)

    def test_the_final_flag_is_carried(self):
        agg = self.agg(workers=1)
        agg.record(0, progress(seq=1, at=1.0))
        self.assertTrue(agg.build(now=1.0, elapsed=1.0, final=True).final)


# ---------------------------------------------------------------------------
# Sampling on the worker side
# ---------------------------------------------------------------------------


class TestExporterSinks(unittest.IsolatedAsyncioTestCase):
    def config(self, **kwargs) -> BenchmarkConfig:
        return BenchmarkConfig(url="http://x/", export_interval=1.0, **kwargs)

    async def test_a_sink_receives_the_snapshots(self):
        from pywrkr.config import WorkerStats
        from pywrkr.streaming import StreamingExporter

        seen: list[Snapshot] = []
        exporter = StreamingExporter(self.config(), [WorkerStats()], 1.0, keep_samples=True)
        exporter.add_sink(lambda s: (seen.append(s), True)[1])
        await exporter.start()
        await exporter.aclose()
        self.assertTrue(seen)
        self.assertTrue(seen[-1].final)

    async def test_without_endpoints_nothing_is_pushed_but_sinks_still_run(self):
        """A worker reporting only to its master has no endpoint of its own."""
        from pywrkr.config import WorkerStats
        from pywrkr.streaming import StreamingExporter

        seen: list[Snapshot] = []
        exporter = StreamingExporter(self.config(), [WorkerStats()], 1.0)
        exporter.add_sink(lambda s: (seen.append(s), True)[1])
        with patch.object(exporter, "_push", side_effect=AssertionError("must not push")):
            await exporter.start()
            await exporter.aclose()
        self.assertTrue(seen)

    async def test_an_endpoint_and_a_sink_both_get_the_snapshot(self):
        """A worker with its own endpoint must not lose it by also reporting up."""
        from pywrkr.config import WorkerStats
        from pywrkr.streaming import StreamingExporter

        pushed, forwarded = [], []
        exporter = StreamingExporter(
            self.config(otel_endpoint="http://collector/"), [WorkerStats()], 1.0
        )
        exporter.add_sink(lambda s: (forwarded.append(s), True)[1])
        with patch.object(exporter, "_push", side_effect=lambda s: (pushed.append(s), True)[1]):
            await exporter.start()
            await exporter.aclose()
        self.assertTrue(pushed)
        self.assertTrue(forwarded)

    def test_samples_are_capped_so_one_interval_cannot_flood_the_wire(self):
        from pywrkr.config import WorkerStats
        from pywrkr.streaming import _MAX_WIRE_SAMPLES, StreamingExporter

        exporter = StreamingExporter(self.config(), [WorkerStats()], 1.0, keep_samples=True)
        capped = exporter._wire_samples([float(i) for i in range(_MAX_WIRE_SAMPLES * 5)])
        self.assertEqual(len(capped), _MAX_WIRE_SAMPLES)
        # An even stride, not the first N: the first N would be whichever
        # requests happened to land earliest, not the interval.
        self.assertGreater(max(capped), _MAX_WIRE_SAMPLES * 4)

    def test_samples_are_absent_unless_asked_for(self):
        from pywrkr.config import WorkerStats
        from pywrkr.streaming import StreamingExporter

        exporter = StreamingExporter(self.config(), [WorkerStats()], 1.0)
        self.assertEqual(exporter._wire_samples([0.1, 0.2]), ())


class TestProgressReporter(unittest.IsolatedAsyncioTestCase):
    class FakeWriter:
        def __init__(self):
            self.sent = []

        def write(self, data):
            self.sent.append(data)

        async def drain(self):
            return None

    async def test_snapshots_are_sent_in_order_with_increasing_seq(self):
        writer = self.FakeWriter()
        reporter = _ProgressReporter(writer)
        await reporter.start()
        for _ in range(3):
            reporter.submit(snapshot())
        await reporter.aclose()
        self.assertEqual(reporter.sent, 3)
        self.assertEqual(reporter.dropped, 0)

    async def test_submit_never_blocks_the_run_when_the_queue_fills(self):
        """Sampling runs on the benchmark's own loop; blocking it distorts the run."""
        writer = self.FakeWriter()
        reporter = _ProgressReporter(writer)
        # No sender task started, so nothing drains the queue.
        for _ in range(reporter._QUEUE_SIZE + 5):
            reporter.submit(snapshot())
        self.assertEqual(reporter.dropped, 5)

    async def test_the_newest_interval_survives_a_full_queue(self):
        writer = self.FakeWriter()
        reporter = _ProgressReporter(writer)
        for i in range(reporter._QUEUE_SIZE + 3):
            reporter.submit(snapshot(total_requests=i))
        seqs = []
        while not reporter._queue.empty():
            seqs.append(reporter._queue.get_nowait()["seq"])
        self.assertEqual(seqs[-1], reporter._QUEUE_SIZE + 3)

    async def test_a_dead_master_does_not_take_the_run_down(self):
        class BrokenWriter(self.FakeWriter):
            def write(self, data):
                raise ConnectionResetError("master went away")

        reporter = _ProgressReporter(BrokenWriter())
        await reporter.start()
        reporter.submit(snapshot())
        await reporter.aclose()
        self.assertEqual(reporter.failed, 1)
        self.assertEqual(reporter.sent, 0)

    async def test_submitting_from_another_thread_reaches_the_sender(self):
        """Snapshots arrive on an executor thread, so submit() must work from one.

        Note what this does *not* prove. The bug it was written for -- a
        cross-thread ``asyncio.Queue.put_nowait`` setting the getter's future
        without waking a loop parked in ``select()`` -- does not reproduce here,
        because ``run_in_executor`` schedules its own completion callback and
        wakes the loop anyway. Removing ``call_soon_threadsafe`` from submit()
        leaves this test green. The condition is only reached from the real
        exporter, where the put happens inside the executor call rather than
        after it, and it is
        TestDistributedStreamingEndToEnd.test_the_master_streams_the_cluster_during_the_run
        that catches it -- by timing out, since a loop that never wakes cannot
        fail an assertion.
        """
        writer = self.FakeWriter()
        reporter = _ProgressReporter(writer)
        await reporter.start()
        loop = asyncio.get_running_loop()

        await loop.run_in_executor(None, reporter.submit, snapshot())
        # Poll rather than await queue.join(): call_soon_threadsafe defers the
        # enqueue, so join() can see an empty queue and return before the item
        # has landed. That made this test pass on 3.10-3.12 and fail on 3.13
        # purely on scheduling order.
        for _ in range(300):
            if reporter.sent:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(reporter.sent, 1)
        await reporter.aclose()

    async def test_submitting_from_the_loop_still_works(self):
        writer = self.FakeWriter()
        reporter = _ProgressReporter(writer)
        await reporter.start()
        reporter.submit(snapshot())
        await reporter.aclose()
        self.assertEqual(reporter.sent, 1)

    async def test_closing_an_unstarted_reporter_is_safe(self):
        await _ProgressReporter(self.FakeWriter()).aclose()


# ---------------------------------------------------------------------------
# Master export loop
# ---------------------------------------------------------------------------


class TestMasterExporter(unittest.IsolatedAsyncioTestCase):
    def make(self, **cfg):
        config = BenchmarkConfig(
            url="http://x/", otel_endpoint="http://collector/", export_interval=0.1, **cfg
        )
        agg = ProgressAggregator(1, 0.1)
        return config, agg, _MasterExporter(config, agg, 0.1, start=0.0)

    async def test_it_exports_on_its_own_interval(self):
        config, agg, exporter = self.make()
        agg.record(0, progress(seq=1, at=999.0))
        calls = []
        with patch(
            "pywrkr.reporting.export_to_otel",
            side_effect=lambda r, e, t: (calls.append(t), True)[1],
        ):
            await exporter.start()
            await asyncio.sleep(0.35)
            await exporter.aclose(now=999.0)
        self.assertGreaterEqual(len(calls), 2)
        self.assertTrue(all(t["role"] == "master" for t in calls))

    async def test_the_last_export_is_tagged_final(self):
        config, agg, exporter = self.make()
        agg.record(0, progress(seq=1, at=999.0))
        calls = []
        with patch(
            "pywrkr.reporting.export_to_otel",
            side_effect=lambda r, e, t: (calls.append(t), True)[1],
        ):
            await exporter.start()
            await exporter.aclose(now=999.0)
        self.assertEqual(calls[-1]["export"], "final")

    async def test_nothing_is_exported_before_any_worker_reports(self):
        config, agg, exporter = self.make()
        calls = []
        with patch(
            "pywrkr.reporting.export_to_otel",
            side_effect=lambda r, e, t: (calls.append(t), True)[1],
        ):
            await exporter.start()
            await asyncio.sleep(0.25)
            await exporter.aclose(now=1.0)
        self.assertEqual(calls, [])

    async def test_the_reporting_worker_count_is_tagged(self):
        config = BenchmarkConfig(url="http://x/", otel_endpoint="http://c/", export_interval=1.0)
        agg = ProgressAggregator(3, 1.0)
        agg.record(0, progress(seq=1, at=100.0))
        agg.record(1, progress(seq=1, at=100.0))
        agg.record(2, progress(seq=1, at=0.0))  # stale
        exporter = _MasterExporter(config, agg, 1.0, start=0.0)
        calls = []
        with patch(
            "pywrkr.reporting.export_to_otel",
            side_effect=lambda r, e, t: (calls.append(t), True)[1],
        ):
            await exporter.start()
            await exporter.aclose(now=100.0)
        self.assertEqual(calls[-1]["workers_reporting"], "2")

    async def test_a_failed_export_is_counted_not_raised(self):
        config, agg, exporter = self.make()
        agg.record(0, progress(seq=1, at=999.0))
        with patch("pywrkr.reporting.export_to_otel", return_value=False):
            await exporter.start()
            await exporter.aclose(now=999.0)
        self.assertGreaterEqual(exporter.failed, 1)

    async def test_closing_twice_is_safe(self):
        config, agg, exporter = self.make()
        with patch("pywrkr.reporting.export_to_otel", return_value=True):
            await exporter.start()
            await exporter.aclose(now=1.0)
            await exporter.aclose(now=1.0)


# ---------------------------------------------------------------------------
# Protocol compatibility
# ---------------------------------------------------------------------------


class TestProtocolGating(unittest.TestCase):
    """The interval in the config message is what makes both directions safe."""

    def config(self, **kwargs) -> BenchmarkConfig:
        return BenchmarkConfig(url="http://x/", **kwargs)

    def test_no_progress_is_requested_without_an_export_endpoint(self):
        """Nowhere to put the data means nothing to ask for."""
        self.assertIsNone(progress_interval_for(self.config(export_interval=1.0)))

    def test_no_progress_is_requested_without_an_interval(self):
        self.assertIsNone(progress_interval_for(self.config(otel_endpoint="http://c/")))

    def test_progress_is_requested_when_both_are_present(self):
        self.assertEqual(
            progress_interval_for(self.config(otel_endpoint="http://c/", export_interval=2.5)), 2.5
        )

    def test_prometheus_alone_is_enough(self):
        self.assertEqual(
            progress_interval_for(self.config(prom_remote_write="http://p/", export_interval=1.0)),
            1.0,
        )

    def test_a_config_message_without_the_key_asks_for_nothing(self):
        """No key is exactly what an older master's config message looks like."""
        self.assertIsNone(requested_progress_interval({"type": "config", "config": {}}))

    def test_a_config_message_with_the_key_asks_for_that_interval(self):
        self.assertEqual(requested_progress_interval({PROGRESS_INTERVAL_KEY: 0.5}), 0.5)

    def test_a_nonsense_interval_asks_for_nothing(self):
        for value in (0, -1, "1.0", None, True, [], {}):
            with self.subTest(value=value):
                self.assertIsNone(requested_progress_interval({PROGRESS_INTERVAL_KEY: value}))

    def test_the_master_puts_the_key_in_the_config_message_only_when_it_wants_it(self):
        """Ties the two halves together: what the master sends is what the
        worker reads, so neither side can drift into asking for the wrong thing.
        """
        wants = self.config(otel_endpoint="http://c/", export_interval=3.0)
        does_not = self.config(export_interval=3.0)

        for config, expected in ((wants, 3.0), (does_not, None)):
            interval = progress_interval_for(config)
            message: dict = {"type": "config", "config": {}}
            if interval:
                message[PROGRESS_INTERVAL_KEY] = interval
            self.assertEqual(requested_progress_interval(message), expected)


# ---------------------------------------------------------------------------
# End to end
# ---------------------------------------------------------------------------


class TestDistributedStreamingEndToEnd(unittest.IsolatedAsyncioTestCase):
    """One real cluster, every property asserted from it.

    Deliberately a single run rather than one per assertion: each cluster costs
    a few seconds of real load and a port, and the assertions are all about the
    same sequence of exports.
    """

    PORT = 9281

    async def asyncSetUp(self):
        async def handler(request):
            await asyncio.sleep(0.002)
            return web.json_response({"ok": True})

        app = web.Application()
        app.router.add_get("/", handler)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.target = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/"

    async def asyncTearDown(self):
        await self.runner.cleanup()

    async def run_cluster(self, port: int, workers: int, duration: float, **cfg):
        exports: list[dict] = []
        config = BenchmarkConfig(
            url=self.target, connections=4, duration=duration, _quiet=True, **cfg
        )

        def record(results, endpoint, tags):
            exports.append(
                {
                    "role": tags.get("role"),
                    "export": tags.get("export"),
                    "workers": tags.get("workers_reporting"),
                    "total": results["total_requests"],
                    "p95": results.get("percentiles", {}).get("p95"),
                }
            )
            return True

        ready = asyncio.Event()
        with patch("pywrkr.reporting.export_to_otel", side_effect=record):
            master = asyncio.create_task(
                run_master(config, "127.0.0.1", port, expect_workers=workers, ready=ready)
            )
            await ready.wait()
            nodes = [
                asyncio.create_task(run_worker_node("127.0.0.1", port)) for _ in range(workers)
            ]
            try:
                result = await asyncio.wait_for(master, timeout=60)
            finally:
                # Bounded: a worker wedged in its own teardown must surface as a
                # failed test, not as a job that hangs until CI kills it.
                for node in nodes:
                    node.cancel()
                await asyncio.wait_for(asyncio.gather(*nodes, return_exceptions=True), timeout=20)
        return result, exports

    async def test_the_master_streams_the_cluster_during_the_run(self):
        result, exports = await self.run_cluster(
            self.PORT,
            workers=2,
            duration=3.0,
            otel_endpoint="http://127.0.0.1:4318/v1/metrics",
            export_interval=0.5,
        )
        self.assertIsNotNone(result)
        merged, _ = result
        master_exports = [e for e in exports if e["role"] == "master"]

        # Exported during the run, not only at the end.
        intervals = [e for e in master_exports if e["export"] == "interval"]
        self.assertGreaterEqual(len(intervals), 2, master_exports)

        # A final cluster snapshot closes it out, which is what leaves a killed
        # run with its last state instead of nothing.
        self.assertEqual(master_exports[-1]["export"], "final")

        # Cumulative totals never go backwards -- rate() on a counter that does
        # would report negative throughput.
        totals = [e["total"] for e in master_exports]
        self.assertEqual(totals, sorted(totals), totals)

        # The streamed numbers agree with the authoritative end-of-run merge. If
        # they disagreed, one of the two would be lying to the operator.
        self.assertEqual(max(totals), merged.total_requests)

        # Percentiles come from pooled raw samples, so they are present and real.
        self.assertTrue([e for e in intervals if e["p95"]], master_exports)

        # Both workers were counted every interval.
        self.assertTrue(all(e["workers"] == "2" for e in intervals), master_exports)

    async def test_a_plain_distributed_run_sends_no_progress_traffic(self):
        """Without an export endpoint the wire is byte-for-byte what it was."""
        from pywrkr import distributed as dist

        seen: list[str] = []
        real_recv = dist._recv_msg

        async def spy(reader):
            msg = await real_recv(reader)
            seen.append(msg.get("type", "?"))
            return msg

        with patch.object(dist, "_recv_msg", side_effect=spy):
            result, exports = await self.run_cluster(self.PORT + 1, workers=1, duration=1.0)
        self.assertIsNotNone(result)
        self.assertNotIn(MSG_PROGRESS, seen)
        self.assertIn("result", seen)
        self.assertEqual(exports, [])


if __name__ == "__main__":
    unittest.main()
