"""Tests for WebSocket benchmarking (``ws://`` targets and scenario ``ws:`` steps).

The integration tests run against a real aiohttp WebSocket server rather than a
mock, because most of what can go wrong here is in the protocol interaction:
whether the close handshake completes, whether a cancelled read loses a frame,
whether a connection the peer drops is distinguishable from one we closed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import unittest
from unittest.mock import patch

import aiohttp
from aiohttp import WSMsgType, web

from pywrkr.config import (
    BenchmarkConfig,
    ScenarioStep,
    WebSocketConfig,
    WorkerStats,
    merge_stats,
)
from pywrkr.main import _build_parser, _parse_and_validate_args
from pywrkr.reporting import build_results_dict, print_results
from pywrkr.websockets import (
    WsStats,
    connection_start_times,
    is_websocket_url,
    merge_ws_stats,
    run_websocket_benchmark,
    ws_results_section,
)

# ---------------------------------------------------------------------------
# Test server
# ---------------------------------------------------------------------------


class WsTestServer:
    """A real WebSocket server, recording what the client actually did.

    ``closes`` is the point of it: the acceptance criterion is that the server
    sees a close frame for every socket, which only a server can attest to.
    """

    def __init__(self) -> None:
        self.closes: list[int | None] = []
        self.connects: list[float] = []
        self.received: list[str] = []
        self.headers: list[dict] = []
        self.queries: list[str] = []
        self.runner: web.AppRunner | None = None
        self.port = 0

    async def start(self) -> str:
        app = web.Application()
        app.router.add_get("/echo", self._echo)
        app.router.add_get("/push", self._push)
        app.router.add_get("/feed", self._feed)
        app.router.add_get("/deny", self._deny)
        app.router.add_get("/hangup", self._hangup)
        app.router.add_get("/slowclose", self._slowclose)
        app.router.add_post("/login", self._login)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]
        return f"127.0.0.1:{self.port}"

    async def stop(self) -> None:
        if self.runner is not None:
            await self.runner.cleanup()

    async def _prepare(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.connects.append(time.monotonic())
        self.headers.append(dict(request.headers))
        self.queries.append(request.query_string)
        return ws

    async def _echo(self, request: web.Request) -> web.WebSocketResponse:
        ws = await self._prepare(request)
        async for msg in ws:
            if msg.type is WSMsgType.TEXT:
                self.received.append(msg.data)
                await ws.send_str(f"reply:{msg.data}")
        self.closes.append(ws.close_code)
        return ws

    async def _push(self, request: web.Request) -> web.WebSocketResponse:
        """Pushes continuously while still reading, so close still works."""
        ws = await self._prepare(request)

        async def pusher():
            # Swallow the reset that racing a close produces. Letting it
            # escape kills the handler before it can answer the close frame,
            # which would make pywrkr look like it leaked the socket.
            with contextlib.suppress(ConnectionResetError, aiohttp.ClientError, RuntimeError):
                while not ws.closed:
                    await ws.send_str("tick")
                    await asyncio.sleep(0.01)

        task = asyncio.create_task(pusher())
        async for _ in ws:
            pass
        task.cancel()
        self.closes.append(ws.close_code)
        return ws

    async def _feed(self, request: web.Request) -> web.WebSocketResponse:
        """Sends a welcome and a heartbeat before the message asked for."""
        ws = await self._prepare(request)
        await ws.send_str(json.dumps({"welcome": True}))
        async for msg in ws:
            if msg.type is WSMsgType.TEXT:
                self.received.append(msg.data)
                payload = json.loads(msg.data)
                await ws.send_str(json.dumps({"heartbeat": 1}))
                await ws.send_str(
                    json.dumps(
                        {
                            "subscribed": payload.get("channel"),
                            "auth": request.query.get("auth"),
                            "sid": "S-42",
                        }
                    )
                )
        self.closes.append(ws.close_code)
        return ws

    async def _slowclose(self, request: web.Request) -> web.WebSocketResponse:
        """Pushes without ever reading, so the close handshake never completes.

        Real services do this. It is the case that separates "the run took N
        seconds" from "the run plus waiting on an unresponsive peer took N".
        """
        ws = await self._prepare(request)
        while not ws.closed:
            await ws.send_str("tick")
            await asyncio.sleep(0.02)
        return ws

    async def _hangup(self, request: web.Request) -> web.WebSocketResponse:
        """Closes the socket from the server side, unprompted."""
        ws = await self._prepare(request)
        await ws.send_str("bye")
        await ws.close(code=1001)
        self.closes.append(ws.close_code)
        return ws

    async def _deny(self, request: web.Request) -> web.Response:
        return web.Response(status=401, text="nope")

    async def _login(self, request: web.Request) -> web.Response:
        return web.json_response({"token": "tok-123"})


class WsServerCase(unittest.IsolatedAsyncioTestCase):
    """Base case that runs a real WebSocket server for each test."""

    async def asyncSetUp(self) -> None:
        self.server = WsTestServer()
        self.netloc = await self.server.start()

    async def asyncTearDown(self) -> None:
        await self.server.stop()

    def ws_url(self, path: str) -> str:
        return f"ws://{self.netloc}{path}"

    def http_url(self, path: str = "/") -> str:
        return f"http://{self.netloc}{path}"

    def config(self, path: str, **kwargs) -> BenchmarkConfig:
        ws_kwargs = {k[3:]: v for k, v in kwargs.items() if k.startswith("ws_")}
        rest = {k: v for k, v in kwargs.items() if not k.startswith("ws_")}
        rest.setdefault("connections", 2)
        rest.setdefault("duration", 1.0)
        return BenchmarkConfig(
            url=self.ws_url(path),
            _quiet=True,
            websocket=WebSocketConfig(**ws_kwargs),
            **rest,
        )


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestUrlDetection(unittest.TestCase):
    def test_ws_and_wss_are_websocket_urls(self):
        self.assertTrue(is_websocket_url("ws://host/path"))
        self.assertTrue(is_websocket_url("wss://host/path"))

    def test_http_urls_are_not(self):
        for url in ("http://host/", "https://host/", "ftp://host/", "host/path"):
            with self.subTest(url=url):
                self.assertFalse(is_websocket_url(url))


class TestConnectionStartTimes(unittest.TestCase):
    def test_no_ramp_starts_everything_at_once(self):
        self.assertEqual(connection_start_times(4, 0.0), [0.0, 0.0, 0.0, 0.0])

    def test_a_ramp_spreads_connections_evenly(self):
        self.assertEqual(connection_start_times(5, 4.0), [0.0, 1.0, 2.0, 3.0, 4.0])

    def test_the_last_socket_starts_at_the_end_of_the_ramp(self):
        """A ramp that finishes early is not the ramp that was asked for."""
        offsets = connection_start_times(10, 9.0)
        self.assertEqual(offsets[-1], 9.0)
        self.assertEqual(len(offsets), 10)

    def test_gaps_are_uniform(self):
        offsets = connection_start_times(8, 7.0)
        gaps = [round(b - a, 6) for a, b in zip(offsets, offsets[1:])]
        self.assertEqual(len(set(gaps)), 1)

    def test_a_single_socket_never_waits(self):
        self.assertEqual(connection_start_times(1, 30.0), [0.0])

    def test_zero_sockets_is_empty(self):
        self.assertEqual(connection_start_times(0, 5.0), [])


class TestMergeWsStats(unittest.TestCase):
    def test_counters_add_up(self):
        a = WsStats(connections_opened=2, messages_sent=10, bytes_received=100)
        b = WsStats(connections_opened=3, messages_sent=5, bytes_received=50)
        merged = merge_ws_stats([a, b])
        self.assertEqual(merged.connections_opened, 5)
        self.assertEqual(merged.messages_sent, 15)
        self.assertEqual(merged.bytes_received, 150)

    def test_peak_concurrency_is_a_maximum_not_a_sum(self):
        """Every socket reads the same shared counter; summing would multiply it."""
        merged = merge_ws_stats([WsStats(peak_concurrent=50), WsStats(peak_concurrent=50)])
        self.assertEqual(merged.peak_concurrent, 50)

    def test_close_codes_are_summed_per_code(self):
        a = WsStats(close_codes={"1000": 2, "1001": 1})
        b = WsStats(close_codes={"1000": 3})
        self.assertEqual(merge_ws_stats([a, b]).close_codes, {"1000": 5, "1001": 1})

    def test_latency_samples_are_pooled(self):
        a, b = WsStats(), WsStats()
        a.rtt_latencies.append(0.1)
        b.rtt_latencies.extend([0.2, 0.3])
        self.assertEqual(sorted(merge_ws_stats([a, b]).rtt_latencies), [0.1, 0.2, 0.3])

    def test_merging_nothing_yields_empty_stats(self):
        self.assertEqual(merge_ws_stats([]).connections_opened, 0)

    def test_merge_stats_carries_the_sidecar(self):
        one, two = WorkerStats(), WorkerStats()
        one.ws = WsStats(connections_opened=1)
        two.ws = WsStats(connections_opened=2)
        self.assertEqual(merge_stats([one, two]).ws.connections_opened, 3)

    def test_merge_stats_leaves_http_runs_alone(self):
        self.assertIsNone(merge_stats([WorkerStats(), WorkerStats()]).ws)


class TestResultsSection(unittest.TestCase):
    def section(self, **kwargs) -> dict:
        return ws_results_section(WsStats(**kwargs), 10.0)

    def test_every_documented_key_is_present(self):
        section = self.section()
        self.assertEqual(
            sorted(section),
            [
                "close",
                "connections",
                "handshake",
                "latency_metric",
                "messages",
                "primary_metric",
                "rtt",
            ],
        )
        self.assertEqual(
            sorted(section["connections"]),
            ["dropped", "failed", "opened", "peak_concurrent", "reconnects"],
        )
        self.assertEqual(sorted(section["close"]), ["codes", "frames_sent", "unacknowledged"])

    def test_rates_are_per_second_of_the_run(self):
        section = self.section(messages_sent=50, messages_received=100)
        self.assertEqual(section["messages"]["sent_per_sec"], 5.0)
        self.assertEqual(section["messages"]["received_per_sec"], 10.0)

    def test_a_zero_length_run_does_not_divide_by_zero(self):
        section = ws_results_section(WsStats(messages_sent=5), 0.0)
        self.assertEqual(section["messages"]["sent_per_sec"], 0.0)

    def test_latency_families_are_reported_separately(self):
        stats = WsStats()
        stats.handshake_latencies.extend([0.01, 0.02])
        stats.rtt_latencies.extend([0.5, 0.6])
        section = ws_results_section(stats, 10.0)
        self.assertEqual(section["handshake"]["count"], 2)
        self.assertEqual(section["rtt"]["count"], 2)
        self.assertNotEqual(section["handshake"]["mean"], section["rtt"]["mean"])

    def test_an_unmeasured_family_is_empty_not_zero(self):
        self.assertEqual(self.section()["rtt"], {})

    def test_the_results_dict_carries_the_section(self):
        stats = WorkerStats(total_requests=10)
        stats.ws = WsStats(connections_opened=2)
        results = build_results_dict(stats, 5.0, 2, BenchmarkConfig(url="ws://h/p"))
        self.assertEqual(results["websocket"]["connections"]["opened"], 2)

    def test_an_http_run_has_no_websocket_section(self):
        results = build_results_dict(WorkerStats(), 5.0, 2, BenchmarkConfig(url="http://h/"))
        self.assertNotIn("websocket", results)


# ---------------------------------------------------------------------------
# CLI validation
# ---------------------------------------------------------------------------


class TestCliValidation(unittest.TestCase):
    def parse(self, *argv):
        parser = _build_parser()
        with patch("sys.argv", ["pywrkr", *argv]):
            return _parse_and_validate_args(parser, parser.parse_args(list(argv)))

    def assert_rejected(self, *argv, containing: str = ""):
        with self.assertRaises(SystemExit) as ctx:
            self.parse(*argv)
        self.assertEqual(ctx.exception.code, 2)
        if containing:
            self.assertIn(containing, self.stderr.getvalue())

    def setUp(self):
        from io import StringIO

        self.stderr = StringIO()
        self._patch = patch("sys.stderr", self.stderr)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def test_a_ws_url_selects_websocket_mode(self):
        config, _ = self.parse("ws://host/feed", "-d", "1")
        self.assertIsNotNone(config.websocket)

    def test_a_wss_url_selects_websocket_mode(self):
        config, _ = self.parse("wss://host/feed", "-d", "1")
        self.assertIsNotNone(config.websocket)

    def test_an_http_url_does_not(self):
        config, _ = self.parse("http://host/", "-d", "1")
        self.assertIsNone(config.websocket)

    def test_an_unknown_scheme_names_every_accepted_one(self):
        self.assert_rejected("ftp://host/", "-d", "1", containing="ws://")

    def test_http_request_flags_are_rejected_not_ignored(self):
        """A -m POST that quietly did nothing is worse than an error."""
        for flag, value in (
            ("-m", "POST"),
            ("-b", "payload"),
            ("-n", "100"),
            ("-u", "5"),
        ):
            with self.subTest(flag=flag):
                self.stderr.truncate(0)
                self.assert_rejected("ws://host/feed", flag, value, containing="do not apply")

    def test_boolean_http_flags_are_rejected(self):
        for flag in ("--http2", "-l", "-R", "--latency-breakdown"):
            with self.subTest(flag=flag):
                self.stderr.truncate(0)
                self.assert_rejected("ws://host/feed", flag, "-d", "1", containing="do not apply")

    def test_output_flags_still_work_on_a_websocket_target(self):
        """-w/--html and --json describe output, not an HTTP request."""
        config, _ = self.parse("ws://host/feed", "-d", "1", "-w")
        self.assertTrue(config.html_output)

    def test_rate_shaping_is_rejected(self):
        self.assert_rejected("ws://host/feed", "--rate", "100", "-d", "1")

    def test_unsupported_modes_are_rejected(self):
        self.assert_rejected("ws://host/feed", "--autofind", containing="not supported")
        self.stderr.truncate(0)
        self.assert_rejected(
            "ws://host/feed", "--master", "--expect-workers", "2", containing="not supported"
        )

    def test_ws_flags_on_an_http_target_are_rejected(self):
        """Silently ignoring them is how a run ends up not testing what was asked."""
        self.assert_rejected(
            "http://host/", "--ws-message", "hi", "-d", "1", containing="only apply to a ws://"
        )

    def test_expect_reply_needs_something_to_reply_to(self):
        self.assert_rejected("ws://host/feed", "--ws-expect-reply", "-d", "1")

    def test_out_of_range_websocket_numbers_are_rejected(self):
        self.assert_rejected("ws://host/f", "--ws-message-interval", "-1", "-d", "1")
        self.stderr.truncate(0)
        self.assert_rejected("ws://host/f", "--ws-close-timeout", "0", "-d", "1")
        self.stderr.truncate(0)
        self.assert_rejected("ws://host/f", "--ws-max-message-size", "0", "-d", "1")

    def test_messages_and_intervals_reach_the_config(self):
        config, _ = self.parse(
            "ws://host/feed",
            "-d",
            "1",
            "--ws-message",
            "a",
            "--ws-message",
            "b",
            "--ws-message-interval",
            "0.25",
            "--ws-expect-reply",
            "--ws-subprotocol",
            "graphql-ws",
        )
        self.assertEqual(config.websocket.messages, ["a", "b"])
        self.assertEqual(config.websocket.message_interval, 0.25)
        self.assertTrue(config.websocket.expect_reply)
        self.assertEqual(config.websocket.subprotocols, ["graphql-ws"])

    def test_reply_timeout_defaults_to_the_request_timeout(self):
        config, _ = self.parse("ws://host/f", "-d", "1", "--ws-message", "x", "--timeout", "7")
        self.assertEqual(config.websocket.reply_timeout, 7)

    def test_ws_options_appear_in_help(self):
        text = _build_parser().format_help()
        self.assertIn("--ws-message", text)
        self.assertIn("--ws-expect-reply", text)


# ---------------------------------------------------------------------------
# Integration: standalone ws:// mode
# ---------------------------------------------------------------------------


class TestWebSocketBenchmark(WsServerCase):
    async def test_a_messaging_run_sends_and_receives(self):
        config = self.config("/echo", ws_messages=["hello"], ws_message_interval=0.01)
        stats, exit_code = await run_websocket_benchmark(config, install_signal_handlers=False)
        self.assertEqual(exit_code, 0)
        self.assertGreater(stats.ws.messages_sent, 0)
        self.assertGreater(stats.ws.messages_received, 0)
        self.assertEqual(stats.ws.connections_opened, 2)
        self.assertEqual(stats.errors, 0)

    async def test_fire_and_forget_still_drains_the_socket(self):
        """Without a reader, incoming frames are invisible and the socket backs up."""
        config = self.config("/echo", ws_messages=["x"], ws_message_interval=0.01)
        stats, _ = await run_websocket_benchmark(config, install_signal_handlers=False)
        self.assertGreater(stats.ws.messages_received, 0)
        self.assertGreater(stats.ws.bytes_received, 0)

    async def test_expect_reply_measures_the_round_trip_not_the_handshake(self):
        config = self.config(
            "/echo", ws_messages=["ping"], ws_message_interval=0.0, ws_expect_reply=True
        )
        stats, _ = await run_websocket_benchmark(config, install_signal_handlers=False)
        self.assertEqual(stats.ws.latency_metric, "rtt")
        self.assertGreater(len(stats.ws.rtt_latencies), 0)
        self.assertGreater(len(stats.ws.handshake_latencies), 0)
        # The run's headline latency is the round trip, and there is one sample
        # per message rather than one per socket.
        self.assertEqual(len(stats.latencies), len(stats.ws.rtt_latencies))

    async def test_without_expect_reply_the_latency_is_the_handshake(self):
        config = self.config("/echo", ws_messages=["x"], ws_message_interval=0.05)
        stats, _ = await run_websocket_benchmark(config, install_signal_handlers=False)
        self.assertEqual(stats.ws.latency_metric, "handshake")
        self.assertEqual(len(stats.latencies), stats.ws.connections_opened)

    async def test_a_listen_only_run_counts_pushed_messages(self):
        config = self.config("/push")
        stats, exit_code = await run_websocket_benchmark(config, install_signal_handlers=False)
        self.assertEqual(exit_code, 0)
        self.assertEqual(stats.ws.messages_sent, 0)
        self.assertGreater(stats.ws.messages_received, 10)
        self.assertEqual(stats.ws.primary_metric, "connections")
        self.assertEqual(stats.total_requests, stats.ws.connections_opened)

    async def test_every_socket_is_closed_with_a_close_frame(self):
        """The acceptance criterion only the server can attest to."""
        config = self.config("/echo", connections=4, ws_messages=["x"], ws_message_interval=0.05)
        stats, _ = await run_websocket_benchmark(config, install_signal_handlers=False)
        await asyncio.sleep(0.2)
        self.assertEqual(len(self.server.closes), 4)
        self.assertEqual(self.server.closes, [1000] * 4)
        self.assertEqual(stats.ws.close_frames_sent, 4)
        self.assertEqual(stats.ws.close_unacked, 0)

    async def test_a_clean_close_is_not_reported_as_an_abnormal_one(self):
        """aiohttp stamps 1006 on any read that is cancelled or times out.

        A polling receive loop therefore makes every clean shutdown look
        abnormal, which is why the reader is ended by closing the socket.
        """
        config = self.config("/push", connections=3)
        stats, _ = await run_websocket_benchmark(config, install_signal_handlers=False)
        # Every socket was closed deliberately, and no close that completed was
        # recorded as abnormal. How many complete inside the timeout is a
        # property of the peer and the runner, so it is asserted server-side by
        # test_every_socket_is_closed_with_a_close_frame rather than here.
        self.assertEqual(stats.ws.close_frames_sent, 3)
        self.assertEqual(stats.ws.close_codes.get("1006", 0), 0)
        self.assertEqual(set(stats.ws.close_codes) - {"1000"}, set(), stats.ws.close_codes)

    async def test_our_own_close_is_not_counted_as_a_dropped_connection(self):
        config = self.config("/push", connections=3)
        stats, _ = await run_websocket_benchmark(config, install_signal_handlers=False)
        self.assertEqual(stats.ws.connections_dropped, 0)
        self.assertEqual(stats.errors, 0)

    async def test_a_server_hangup_is_counted_as_a_drop(self):
        config = self.config("/hangup", connections=2)
        stats, _ = await run_websocket_benchmark(config, install_signal_handlers=False)
        self.assertEqual(stats.ws.connections_dropped, 2)
        self.assertIn("ws_closed_by_peer", stats.error_types)

    async def test_a_refused_upgrade_records_the_http_status(self):
        config = self.config("/deny", connections=3)
        stats, exit_code = await run_websocket_benchmark(config, install_signal_handlers=False)
        self.assertEqual(stats.ws.connections_failed, 3)
        self.assertEqual(stats.ws.connections_opened, 0)
        self.assertEqual(stats.status_codes[401], 3)
        self.assertIn("ws_handshake_401", stats.error_types)
        # A run where nothing connected must never look like a clean pass.
        self.assertEqual(exit_code, 1)

    async def test_ramp_up_staggers_connection_establishment(self):
        """The connection storm has the shape that was asked for."""
        config = self.config("/echo", connections=4, duration=2.0, ramp_up=1.0)
        await run_websocket_benchmark(config, install_signal_handlers=False)
        self.assertEqual(len(self.server.connects), 4)
        span = self.server.connects[-1] - self.server.connects[0]
        # Four sockets over a 1s ramp land at ~0/0.33/0.66/1.0s. Asserting on
        # the total span rather than each gap: CI runners jitter individual
        # intervals, but cannot compress the whole ramp.
        self.assertGreater(span, 0.5)
        self.assertLess(span, 1.6)

    async def test_without_ramp_up_everything_connects_at_once(self):
        config = self.config("/echo", connections=4, duration=1.0, ramp_up=0.0)
        await run_websocket_benchmark(config, install_signal_handlers=False)
        span = self.server.connects[-1] - self.server.connects[0]
        self.assertLess(span, 0.5)

    async def test_teardown_time_is_not_counted_as_load(self):
        """Waiting on a peer that never answers a close is not load.

        Counting it would stretch the reported duration and deflate every rate
        derived from it, against exactly the kind of server most likely to be
        benchmarked over WebSockets.
        """
        config = self.config("/slowclose", connections=2, duration=0.5)
        config.websocket.close_timeout = 0.6
        config._quiet = False
        reported: list[float] = []

        with patch(
            "pywrkr.reporting.print_results",
            side_effect=lambda stats, duration, *a, **kw: reported.append(duration),
        ):
            started = time.monotonic()
            stats, _ = await run_websocket_benchmark(config, install_signal_handlers=False)
            wall = time.monotonic() - started

        # The close really did go unanswered, so there is a gap to get wrong.
        self.assertEqual(stats.ws.close_unacked, 2)
        self.assertGreater(wall, 1.0)
        self.assertLess(reported[0], 0.9, f"teardown leaked into the duration: {reported[0]}")
        self.assertGreater(reported[0], 0.4)

    async def test_a_threshold_on_round_trip_latency_gates_the_run(self):
        from pywrkr.reporting import parse_threshold

        config = self.config(
            "/echo",
            ws_messages=["ping"],
            ws_message_interval=0.0,
            ws_expect_reply=True,
            thresholds=[parse_threshold("p95 < 1us")],
        )
        _, exit_code = await run_websocket_benchmark(config, install_signal_handlers=False)
        self.assertEqual(exit_code, 2)

    async def test_a_satisfied_threshold_leaves_the_run_green(self):
        from pywrkr.reporting import parse_threshold

        config = self.config(
            "/echo",
            ws_messages=["ping"],
            ws_message_interval=0.0,
            ws_expect_reply=True,
            thresholds=[parse_threshold("p95 < 30s")],
        )
        _, exit_code = await run_websocket_benchmark(config, install_signal_handlers=False)
        self.assertEqual(exit_code, 0)

    async def test_messages_cycle_through_every_payload(self):
        config = self.config(
            "/echo", connections=1, ws_messages=["one", "two"], ws_message_interval=0.0
        )
        await run_websocket_benchmark(config, install_signal_handlers=False)
        self.assertIn("one", self.server.received)
        self.assertIn("two", self.server.received)

    async def test_custom_headers_reach_the_handshake(self):
        config = self.config("/echo", headers={"X-Trace": "abc"}, connections=1)
        await run_websocket_benchmark(config, install_signal_handlers=False)
        self.assertEqual(self.server.headers[0].get("X-Trace"), "abc")

    async def test_reconnect_reopens_a_socket_the_server_closed(self):
        config = self.config("/hangup", connections=1, duration=1.0, ws_reconnect=True)
        config.websocket.reconnect_delay = 0.05
        stats, _ = await run_websocket_benchmark(config, install_signal_handlers=False)
        self.assertGreater(stats.ws.reconnects, 0)
        self.assertGreater(stats.ws.connections_opened, 1)

    async def test_without_reconnect_a_dropped_socket_stays_down(self):
        config = self.config("/hangup", connections=1, duration=1.0)
        stats, _ = await run_websocket_benchmark(config, install_signal_handlers=False)
        self.assertEqual(stats.ws.reconnects, 0)
        self.assertEqual(stats.ws.connections_opened, 1)

    async def test_peak_concurrency_reflects_the_fleet(self):
        config = self.config("/push", connections=5, duration=1.0)
        stats, _ = await run_websocket_benchmark(config, install_signal_handlers=False)
        self.assertEqual(stats.ws.peak_concurrent, 5)

    async def test_a_stop_before_any_socket_opens_connects_nothing(self):
        config = self.config("/echo", connections=3, duration=1.0, ramp_up=30.0)
        config.duration = 0.05
        stats, _ = await run_websocket_benchmark(config, install_signal_handlers=False)
        self.assertLessEqual(stats.ws.connections_opened, 1)

    async def test_the_console_report_names_what_the_latency_measures(self):
        from io import StringIO

        config = self.config(
            "/echo", ws_messages=["x"], ws_message_interval=0.0, ws_expect_reply=True
        )
        stats, _ = await run_websocket_benchmark(config, install_signal_handlers=False)
        buf = StringIO()
        print_results(stats, 1.0, 2, 0.0, config, file=buf)
        text = buf.getvalue()
        self.assertIn("WEBSOCKET STATISTICS", text)
        self.assertIn("message round-trip time", text)
        self.assertNotIn("Keep-Alive", text)


class TestTlsOptions(unittest.TestCase):
    def test_wss_shares_the_https_tls_settings(self):
        """--ssl-verify and --ca-bundle must mean the same on wss:// as https://."""
        from pywrkr.backends import ssl_context_from
        from pywrkr.config import SSLConfig
        from pywrkr.websockets import _build_ssl_context

        insecure = SSLConfig(verify=False)
        from_ws = _build_ssl_context(BenchmarkConfig(url="wss://h/p", ssl_config=insecure))
        from_http = ssl_context_from(insecure)
        self.assertFalse(from_ws.check_hostname)
        self.assertEqual(from_ws.verify_mode, from_http.verify_mode)

    def test_ssl_verify_turns_verification_on_for_wss(self):
        """--ssl-verify is opt-in for http(s); wss:// must follow the same rule."""
        from pywrkr.config import SSLConfig
        from pywrkr.websockets import _build_ssl_context

        default = _build_ssl_context(BenchmarkConfig(url="wss://h/p"))
        self.assertFalse(default.check_hostname)
        verified = _build_ssl_context(
            BenchmarkConfig(url="wss://h/p", ssl_config=SSLConfig(verify=True))
        )
        self.assertTrue(verified.check_hostname)


# ---------------------------------------------------------------------------
# Scenario ws: steps
# ---------------------------------------------------------------------------


class TestScenarioStepParsing(unittest.TestCase):
    def load(self, step: dict):
        import json as _json
        import os
        import tempfile

        from pywrkr.config import load_scenario

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            _json.dump({"name": "s", "steps": [step]}, handle)
            path = handle.name
        try:
            return load_scenario(path).steps[0]
        finally:
            os.unlink(path)

    def test_a_ws_step_is_recognised(self):
        step = self.load({"ws": "ws://h/feed", "send": "hi"})
        self.assertTrue(step.is_websocket)
        self.assertEqual(step.ws, "ws://h/feed")
        self.assertEqual(step.send, "hi")

    def test_the_ws_url_also_becomes_the_path(self):
        """So per-step naming, stats and ${var} validation work unchanged."""
        self.assertEqual(self.load({"ws": "ws://h/feed"}).path, "ws://h/feed")

    def test_hold_accepts_a_duration_string(self):
        self.assertEqual(self.load({"ws": "ws://h/f", "hold": "1.5s"}).hold, 1.5)
        self.assertEqual(self.load({"ws": "ws://h/f", "hold": "250ms"}).hold, 0.25)

    def test_hold_accepts_a_bare_number_as_seconds(self):
        self.assertEqual(self.load({"ws": "ws://h/f", "hold": 2}).hold, 2)

    def test_an_http_step_is_not_a_websocket_step(self):
        self.assertFalse(self.load({"path": "/api"}).is_websocket)

    def test_a_step_with_neither_path_nor_ws_is_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            self.load({"send": "hi"})
        self.assertIn("'path' or 'ws'", str(ctx.exception))

    def test_http_only_keys_are_rejected_on_a_ws_step(self):
        for key, value in (
            ("method", "POST"),
            ("body", "x"),
            ("assert_status", 200),
            ("assert_body_contains", "ok"),
        ):
            with self.subTest(key=key):
                with self.assertRaises(ValueError) as ctx:
                    self.load({"ws": "ws://h/f", key: value})
                self.assertIn("does not apply", str(ctx.exception))

    def test_expecting_a_reply_without_sending_anything_is_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            self.load({"ws": "ws://h/f", "expect_message_contains": "ok"})
        self.assertIn("nothing", str(ctx.exception))

    def test_a_negative_hold_is_rejected(self):
        with self.assertRaises(ValueError):
            self.load({"ws": "ws://h/f", "hold": -1})

    def test_a_non_string_ws_url_is_rejected(self):
        with self.assertRaises(ValueError):
            self.load({"ws": 123})

    def test_a_conflicting_path_is_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            self.load({"ws": "ws://h/f", "path": "/other"})
        self.assertIn("both", str(ctx.exception))

    def test_a_hand_built_step_defaults_to_http(self):
        self.assertFalse(ScenarioStep(path="/x").is_websocket)


class TestScenarioWsStep(WsServerCase):
    def scenario_config(self, steps, **kwargs) -> BenchmarkConfig:
        from pywrkr.config import Scenario

        kwargs.setdefault("users", 1)
        kwargs.setdefault("duration", 1.0)
        return BenchmarkConfig(
            url=self.http_url(),
            _quiet=True,
            scenario=Scenario(name="mixed", steps=steps),
            **kwargs,
        )

    async def test_an_http_login_hands_a_token_to_a_ws_step(self):
        """The acceptance criterion: correlation across the protocol boundary."""
        from pywrkr.config import parse_extract_spec
        from pywrkr.workers import run_user_simulation

        steps = [
            ScenarioStep(
                path="/login",
                method="POST",
                name="login",
                extract=parse_extract_spec({"token": {"json": "$.token"}}, "step"),
            ),
            ScenarioStep(
                path=self.ws_url("/feed?auth=${token}"),
                ws=self.ws_url("/feed?auth=${token}"),
                name="subscribe",
                send='{"op":"subscribe","channel":"orders"}',
                expect_message_contains="subscribed",
            ),
        ]
        stats, exit_code = await run_user_simulation(
            self.scenario_config(steps), install_signal_handlers=False
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(stats.errors, 0)
        self.assertGreater(stats.ws.connections_opened, 0)
        # The extracted token really reached the socket's query string.
        self.assertTrue(all(q == "auth=tok-123" for q in self.server.queries))

    async def test_the_send_payload_is_templated_too(self):
        from pywrkr.config import parse_extract_spec
        from pywrkr.workers import run_user_simulation

        steps = [
            ScenarioStep(
                path="/login",
                method="POST",
                name="login",
                extract=parse_extract_spec({"token": {"json": "$.token"}}, "step"),
            ),
            ScenarioStep(
                path=self.ws_url("/feed"),
                ws=self.ws_url("/feed"),
                name="subscribe",
                send='{"channel":"${token}"}',
                expect_message_contains="subscribed",
            ),
        ]
        await run_user_simulation(self.scenario_config(steps), install_signal_handlers=False)
        self.assertTrue(any("tok-123" in msg for msg in self.server.received))

    async def test_a_ws_step_can_extract_from_the_matched_message(self):
        from pywrkr.config import parse_extract_spec
        from pywrkr.workers import run_user_simulation

        steps = [
            ScenarioStep(
                path=self.ws_url("/feed"),
                ws=self.ws_url("/feed"),
                name="subscribe",
                send='{"channel":"orders"}',
                expect_message_contains="subscribed",
                extract=parse_extract_spec({"sid": {"json": "$.sid"}}, "step"),
            ),
        ]
        stats, _ = await run_user_simulation(
            self.scenario_config(steps), install_signal_handlers=False
        )
        self.assertEqual(stats.extract_failures, 0)

    async def test_the_expected_message_is_found_behind_earlier_frames(self):
        """The confirmation routinely arrives behind a welcome and a heartbeat."""
        from pywrkr.workers import run_user_simulation

        steps = [
            ScenarioStep(
                path=self.ws_url("/feed"),
                ws=self.ws_url("/feed"),
                name="subscribe",
                send='{"channel":"orders"}',
                expect_message_contains="subscribed",
            )
        ]
        stats, _ = await run_user_simulation(
            self.scenario_config(steps), install_signal_handlers=False
        )
        self.assertEqual(stats.errors, 0)
        self.assertGreater(stats.ws.unexpected_replies, 0)

    async def test_a_message_that_never_arrives_fails_the_step(self):
        from pywrkr.workers import run_user_simulation

        steps = [
            ScenarioStep(
                path=self.ws_url("/feed"),
                ws=self.ws_url("/feed"),
                name="subscribe",
                send='{"channel":"orders"}',
                expect_message_contains="this-never-arrives",
            )
        ]
        config = self.scenario_config(steps, duration=1.0, timeout_sec=0.3)
        stats, _ = await run_user_simulation(config, install_signal_handlers=False)
        self.assertGreater(stats.errors, 0)
        self.assertGreater(stats.ws.reply_timeouts, 0)

    async def test_hold_keeps_the_socket_open_and_counts_pushed_messages(self):
        from pywrkr.workers import run_user_simulation

        steps = [
            ScenarioStep(
                path=self.ws_url("/push"),
                ws=self.ws_url("/push"),
                name="listen",
                hold=0.3,
            )
        ]
        stats, _ = await run_user_simulation(
            self.scenario_config(steps, duration=1.0), install_signal_handlers=False
        )
        self.assertGreater(stats.ws.messages_received, 5)

    async def test_a_ws_step_closes_its_socket(self):
        from pywrkr.workers import run_user_simulation

        steps = [
            ScenarioStep(
                path=self.ws_url("/feed"),
                ws=self.ws_url("/feed"),
                name="subscribe",
                send='{"channel":"o"}',
                expect_message_contains="subscribed",
            )
        ]
        await run_user_simulation(
            self.scenario_config(steps, duration=0.5), install_signal_handlers=False
        )
        await asyncio.sleep(0.3)
        self.assertTrue(self.server.closes)
        self.assertTrue(all(code == 1000 for code in self.server.closes), self.server.closes)

    async def test_a_refused_upgrade_fails_the_step_rather_than_the_run(self):
        from pywrkr.workers import run_user_simulation

        steps = [
            ScenarioStep(path=self.ws_url("/deny"), ws=self.ws_url("/deny"), name="denied"),
        ]
        stats, _ = await run_user_simulation(
            self.scenario_config(steps, duration=0.5), install_signal_handlers=False
        )
        self.assertGreater(stats.errors, 0)
        self.assertIn("denied", stats.step_errors)

    async def test_a_backend_without_websocket_support_says_so(self):
        from pywrkr.websockets import WsStepOutcome
        from pywrkr.workers import _run_ws_step

        class NoWs:
            def raw_websocket_session(self):
                return None

        outcome = await _run_ws_step(
            ScenarioStep(path="ws://h/f", ws="ws://h/f"),
            "ws://h/f",
            {},
            NoWs(),
            BenchmarkConfig(url="http://h/"),
            WorkerStats(),
            "step",
            0,
            {},
            False,
            None,
            None,
            asyncio.Event(),
        )
        self.assertIsInstance(outcome, WsStepOutcome)
        self.assertFalse(outcome.ok)
        self.assertIn("aiohttp backend", outcome.error)


class TestWebSocketDistributed(unittest.TestCase):
    """Distributed WebSocket mode is not implemented; make that unambiguous."""

    def test_master_mode_rejects_a_websocket_target(self):
        from io import StringIO

        parser = _build_parser()
        argv = ["wss://host/feed", "--master", "--expect-workers", "2"]
        with (
            patch("sys.stderr", new_callable=StringIO) as err,
            patch("sys.argv", ["pywrkr", *argv]),
            self.assertRaises(SystemExit),
        ):
            _parse_and_validate_args(parser, parser.parse_args(argv))
        self.assertIn("not supported", err.getvalue())

    def test_a_scenario_ws_step_survives_the_wire(self):
        """A distributed scenario must not silently become an HTTP request."""
        from pywrkr.distributed import _deserialize_scenario_step, _serialize_scenario_step

        step = ScenarioStep(
            path="ws://h/feed?auth=${token}",
            ws="ws://h/feed?auth=${token}",
            name="subscribe",
            send='{"op":"subscribe"}',
            expect_message_contains="subscribed",
            hold=2.5,
        )
        restored = _deserialize_scenario_step(_serialize_scenario_step(step))
        self.assertTrue(restored.is_websocket)
        self.assertEqual(restored.ws, step.ws)
        self.assertEqual(restored.send, step.send)
        self.assertEqual(restored.expect_message_contains, step.expect_message_contains)
        self.assertEqual(restored.hold, 2.5)

    def test_an_http_step_still_round_trips_as_http(self):
        from pywrkr.distributed import _deserialize_scenario_step, _serialize_scenario_step

        restored = _deserialize_scenario_step(
            _serialize_scenario_step(ScenarioStep(path="/api", method="POST"))
        )
        self.assertFalse(restored.is_websocket)
        self.assertEqual(restored.method, "POST")


class TestRawWebSocketSession(unittest.IsolatedAsyncioTestCase):
    async def test_the_aiohttp_backend_exposes_its_session(self):
        """A ws: step must inherit the cookies an earlier HTTP step set."""
        from pywrkr.backends import create_backend

        backend = create_backend(BenchmarkConfig(url="http://h/"), 4)
        try:
            async with backend.create_session(WorkerStats()) as session:
                self.assertIsInstance(session.raw_websocket_session(), aiohttp.ClientSession)
        finally:
            await backend.aclose()

    def test_the_interface_default_is_no_websocket_support(self):
        from pywrkr.backends import BackendSession

        self.assertIsNone(BackendSession.raw_websocket_session(object()))


if __name__ == "__main__":
    unittest.main()
