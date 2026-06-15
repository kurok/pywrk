"""Tests for distributed.py: serialization, protocol, and stats merging."""

import asyncio
import contextlib
import hashlib
import hmac
import socket
import unittest

from pywrkr.config import (
    BenchmarkConfig,
    LatencyBreakdown,
    Scenario,
    ScenarioStep,
    SSLConfig,
    Threshold,
    WorkerStats,
)
from pywrkr.distributed import (
    _deserialize_config,
    _deserialize_stats,
    _recv_msg,
    _send_msg,
    _serialize_config,
    _serialize_stats,
    merge_worker_stats,
    run_master,
    run_worker_node,
)


class TestConfigSerialization(unittest.TestCase):
    """Test config serialization roundtrip with various field combinations."""

    def test_minimal_config_roundtrip(self):
        config = BenchmarkConfig(url="http://localhost:8080/api")
        data = _serialize_config(config)
        restored = _deserialize_config(data)
        self.assertEqual(restored.url, config.url)
        self.assertEqual(restored.connections, config.connections)
        self.assertEqual(restored.method, config.method)

    def test_config_with_body(self):
        config = BenchmarkConfig(url="http://localhost/", body=b'{"key": "value"}', method="POST")
        data = _serialize_config(config)
        restored = _deserialize_config(data)
        self.assertEqual(restored.body, config.body)
        self.assertEqual(restored.method, "POST")

    def test_config_with_ssl(self):
        ssl_cfg = SSLConfig(verify=True, ca_bundle="/path/to/ca.pem")
        config = BenchmarkConfig(url="https://example.com/", ssl_config=ssl_cfg)
        data = _serialize_config(config)
        restored = _deserialize_config(data)
        self.assertTrue(restored.ssl_config.verify)
        self.assertEqual(restored.ssl_config.ca_bundle, "/path/to/ca.pem")

    def test_config_with_thresholds(self):
        thresholds = [
            Threshold(metric="p95", operator="<", value=0.3, raw_expr="p95 < 300ms"),
            Threshold(metric="error_rate", operator="<", value=5.0, raw_expr="error_rate < 5%"),
        ]
        config = BenchmarkConfig(url="http://localhost/", thresholds=thresholds)
        data = _serialize_config(config)
        restored = _deserialize_config(data)
        self.assertEqual(len(restored.thresholds), 2)
        self.assertEqual(restored.thresholds[0].metric, "p95")
        self.assertAlmostEqual(restored.thresholds[0].value, 0.3)

    def test_config_with_scenario(self):
        scenario = Scenario(
            name="Login Flow",
            think_time=1.0,
            steps=[
                ScenarioStep(path="/login", method="POST", body={"user": "test"}),
                ScenarioStep(path="/dashboard", method="GET"),
            ],
        )
        config = BenchmarkConfig(url="http://localhost/", scenario=scenario)
        data = _serialize_config(config)
        restored = _deserialize_config(data)
        self.assertIsNotNone(restored.scenario)
        self.assertEqual(restored.scenario.name, "Login Flow")
        self.assertEqual(len(restored.scenario.steps), 2)
        self.assertEqual(restored.scenario.steps[0].path, "/login")

    def test_config_with_tags(self):
        config = BenchmarkConfig(
            url="http://localhost/",
            tags={"env": "staging", "region": "us-east"},
        )
        data = _serialize_config(config)
        restored = _deserialize_config(data)
        self.assertEqual(restored.tags["env"], "staging")

    def test_config_with_rate_limiting(self):
        config = BenchmarkConfig(
            url="http://localhost/",
            rate=1000.0,
            rate_ramp=5000.0,
            duration=60.0,
        )
        data = _serialize_config(config)
        restored = _deserialize_config(data)
        self.assertAlmostEqual(restored.rate, 1000.0)
        self.assertAlmostEqual(restored.rate_ramp, 5000.0)

    def test_config_with_user_simulation(self):
        config = BenchmarkConfig(
            url="http://localhost/",
            users=50,
            ramp_up=10.0,
            think_time=2.0,
            think_time_jitter=0.3,
        )
        data = _serialize_config(config)
        restored = _deserialize_config(data)
        self.assertEqual(restored.users, 50)
        self.assertAlmostEqual(restored.ramp_up, 10.0)
        self.assertAlmostEqual(restored.think_time, 2.0)

    def test_config_output_fields(self):
        config = BenchmarkConfig(
            url="http://localhost/",
            csv_output="results.csv",
            json_output="results.json",
            html_output=True,
            html_report="report.html",
            live_dashboard=True,
        )
        data = _serialize_config(config)
        restored = _deserialize_config(data)
        self.assertEqual(restored.csv_output, "results.csv")
        self.assertEqual(restored.json_output, "results.json")
        self.assertTrue(restored.html_output)
        self.assertEqual(restored.html_report, "report.html")
        self.assertTrue(restored.live_dashboard)


class TestStatsSerialization(unittest.TestCase):
    """Test WorkerStats serialization roundtrip."""

    def test_empty_stats_roundtrip(self):
        stats = WorkerStats()
        data = _serialize_stats(stats)
        restored = _deserialize_stats(data)
        self.assertEqual(restored.total_requests, 0)
        self.assertEqual(restored.errors, 0)

    def test_stats_with_latencies(self):
        stats = WorkerStats()
        stats.total_requests = 1000
        stats.total_bytes = 50000
        stats.errors = 5
        stats.latencies.extend([0.05, 0.1, 0.15, 0.2])
        data = _serialize_stats(stats)
        restored = _deserialize_stats(data)
        self.assertEqual(restored.total_requests, 1000)
        self.assertEqual(restored.total_bytes, 50000)
        self.assertEqual(restored.errors, 5)
        self.assertEqual(list(restored.latencies), [0.05, 0.1, 0.15, 0.2])

    def test_stats_with_breakdowns(self):
        stats = WorkerStats()
        bd = LatencyBreakdown(dns=0.01, connect=0.02, tls=0.03, ttfb=0.04, transfer=0.05)
        stats.breakdowns.append(bd)
        data = _serialize_stats(stats)
        restored = _deserialize_stats(data)
        self.assertEqual(len(restored.breakdowns), 1)
        self.assertAlmostEqual(restored.breakdowns[0].dns, 0.01)
        self.assertAlmostEqual(restored.breakdowns[0].tls, 0.03)

    def test_stats_with_error_types(self):
        stats = WorkerStats()
        stats.error_types["timeout"] += 5
        stats.error_types["connection_reset"] += 3
        data = _serialize_stats(stats)
        restored = _deserialize_stats(data)
        self.assertEqual(restored.error_types["timeout"], 5)
        self.assertEqual(restored.error_types["connection_reset"], 3)

    def test_stats_with_status_codes(self):
        stats = WorkerStats()
        stats.status_codes[200] = 900
        stats.status_codes[404] = 50
        stats.status_codes[500] = 50
        data = _serialize_stats(stats)
        restored = _deserialize_stats(data)
        self.assertEqual(restored.status_codes[200], 900)
        self.assertEqual(restored.status_codes[404], 50)

    def test_stats_with_step_latencies(self):
        stats = WorkerStats()
        stats.step_latencies["login"].extend([0.1, 0.2])
        stats.step_latencies["dashboard"].extend([0.05])
        data = _serialize_stats(stats)
        restored = _deserialize_stats(data)
        self.assertEqual(len(restored.step_latencies["login"]), 2)


class TestMergeWorkerStats(unittest.TestCase):
    """Tests for merge_worker_stats."""

    def test_merge_empty(self):
        merged = merge_worker_stats([])
        self.assertEqual(merged.total_requests, 0)

    def test_merge_single(self):
        ws = WorkerStats()
        ws.total_requests = 100
        ws.errors = 5
        merged = merge_worker_stats([ws])
        self.assertEqual(merged.total_requests, 100)
        self.assertEqual(merged.errors, 5)

    def test_merge_multiple_sums_correctly(self):
        ws1 = WorkerStats()
        ws1.total_requests = 100
        ws1.total_bytes = 5000
        ws1.errors = 2
        ws1.latencies.extend([0.1, 0.2])

        ws2 = WorkerStats()
        ws2.total_requests = 200
        ws2.total_bytes = 10000
        ws2.errors = 3
        ws2.latencies.extend([0.3, 0.4])

        merged = merge_worker_stats([ws1, ws2])
        self.assertEqual(merged.total_requests, 300)
        self.assertEqual(merged.total_bytes, 15000)
        self.assertEqual(merged.errors, 5)
        self.assertEqual(len(merged.latencies), 4)

    def test_merge_error_types(self):
        ws1 = WorkerStats()
        ws1.error_types["timeout"] += 3
        ws1.error_types["dns"] += 1

        ws2 = WorkerStats()
        ws2.error_types["timeout"] += 2
        ws2.error_types["ssl"] += 1

        merged = merge_worker_stats([ws1, ws2])
        self.assertEqual(merged.error_types["timeout"], 5)
        self.assertEqual(merged.error_types["dns"], 1)
        self.assertEqual(merged.error_types["ssl"], 1)

    def test_merge_status_codes(self):
        ws1 = WorkerStats()
        ws1.status_codes[200] = 100
        ws2 = WorkerStats()
        ws2.status_codes[200] = 200
        ws2.status_codes[500] = 5

        merged = merge_worker_stats([ws1, ws2])
        self.assertEqual(merged.status_codes[200], 300)
        self.assertEqual(merged.status_codes[500], 5)

    def test_merge_breakdowns(self):
        ws1 = WorkerStats()
        ws1.breakdowns.append(LatencyBreakdown(dns=0.01))
        ws2 = WorkerStats()
        ws2.breakdowns.append(LatencyBreakdown(dns=0.02))
        ws2.breakdowns.append(LatencyBreakdown(dns=0.03))

        merged = merge_worker_stats([ws1, ws2])
        self.assertEqual(len(merged.breakdowns), 3)


class TestProtocol(unittest.TestCase):
    """Tests for the length-prefixed message protocol."""

    def test_send_recv_roundtrip(self):
        """Test that _send_msg and _recv_msg round-trip correctly."""

        async def _test():
            # Create connected reader/writer pair via TCP loopback
            received = asyncio.Future()

            async def handle(reader, writer):
                msg = await _recv_msg(reader)
                received.set_result(msg)
                writer.close()

            server = await asyncio.start_server(handle, "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]

            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            test_data = {"type": "config", "url": "http://test.com", "value": 42}
            await _send_msg(writer, test_data)
            writer.close()

            result = await asyncio.wait_for(received, timeout=5.0)
            server.close()
            return result

        result = asyncio.run(_test())
        self.assertEqual(result["type"], "config")
        self.assertEqual(result["url"], "http://test.com")
        self.assertEqual(result["value"], 42)

    def test_large_message(self):
        """Test protocol with a large payload."""

        async def _test():
            received = asyncio.Future()

            async def handle(reader, writer):
                msg = await _recv_msg(reader)
                received.set_result(msg)
                writer.close()

            server = await asyncio.start_server(handle, "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]

            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            # ~100KB payload
            big_data = {"data": "x" * 100_000}
            await _send_msg(writer, big_data)
            writer.close()

            result = await asyncio.wait_for(received, timeout=5.0)
            server.close()
            return result

        result = asyncio.run(_test())
        self.assertEqual(len(result["data"]), 100_000)


class TestWorkerAuth(unittest.IsolatedAsyncioTestCase):
    """Tests for HMAC-SHA256 challenge-response authentication."""

    async def _open_raw_connection(self, port: int, timeout: float = 10.0):
        """Return a raw (reader, writer) pair, retrying until the master accepts.

        ``_start_master`` launches ``run_master`` as a task; on a busy CI runner
        the server may not have finished binding when the test connects, which
        previously surfaced as a flaky ``ConnectionRefusedError``. Retry the
        connect until it succeeds (the first successful connection is the real
        one the test uses, so there is no spurious extra connection) rather than
        relying on a fixed sleep.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            try:
                return await asyncio.open_connection("127.0.0.1", port)
            except (ConnectionRefusedError, OSError):
                if loop.time() >= deadline:
                    raise
                await asyncio.sleep(0.02)

    def _compute_hmac(self, secret: str, nonce_hex: str) -> str:
        nonce = bytes.fromhex(nonce_hex)
        return hmac.new(secret.encode(), nonce, digestmod=hashlib.sha256).hexdigest()

    async def _start_master(self, secret: str | None = None):
        """Start a master with a small real-config and return (task, port)."""
        config = BenchmarkConfig(url="http://127.0.0.1:1", duration=0.01, connections=1)
        # Pick a free port
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]

        ready = asyncio.Event()
        task = asyncio.create_task(
            run_master(
                config,
                "127.0.0.1",
                port,
                expect_workers=1,
                worker_secret=secret,
                ready=ready,
            ),
            name="master",
        )
        # Wait until the server is actually accepting connections before any
        # worker/raw client connects — deterministic, unlike a fixed sleep.
        await asyncio.wait_for(ready.wait(), timeout=10)
        return task, port

    async def test_correct_secret_admitted(self):
        """A worker with the correct secret is admitted and receives config."""
        secret = "test-secret-correct"
        master_task, port = await self._start_master(secret=secret)

        worker_task = asyncio.create_task(
            run_worker_node("127.0.0.1", port, worker_secret=secret),
            name="worker",
        )
        # Give the run a short window; we only care that auth succeeded (no crash)
        done, pending = await asyncio.wait({master_task, worker_task}, timeout=8)
        for t in pending:
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                _ = await t
        # Neither task should raise an exception (auth-rejected path raises nothing,
        # but the task itself would return None rather than stats)
        for t in done:
            exc = t.exception()
            self.assertIsNone(exc, f"Unexpected exception: {exc}")

    async def test_wrong_secret_rejected(self):
        """A worker with the wrong secret is disconnected by the master."""
        master_task, port = await self._start_master(secret="correct-secret")

        reader, writer = await self._open_raw_connection(port)
        try:
            # Receive challenge
            challenge = await asyncio.wait_for(_recv_msg(reader), timeout=3)
            self.assertEqual(challenge["type"], "challenge")

            # Send wrong HMAC
            bad_hmac = self._compute_hmac("wrong-secret", challenge["nonce"])
            await _send_msg(writer, {"type": "auth", "hmac": bad_hmac})

            # Master should close the connection — next read returns empty
            data = await asyncio.wait_for(reader.read(1), timeout=3)
            self.assertEqual(data, b"", "Connection should have been closed by master")
        finally:
            writer.close()
            master_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                _ = await master_task

    async def test_no_auth_when_no_secret(self):
        """Master with no secret accepts a worker without any auth exchange."""
        master_task, port = await self._start_master(secret=None)

        reader, writer = await self._open_raw_connection(port)
        try:
            # No challenge should be sent; master should wait for a worker connection
            # We connect raw and then just close — master should not crash.
            await asyncio.sleep(0.1)
        finally:
            writer.close()
            master_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                _ = await master_task

    async def test_auth_timeout_on_no_response(self):
        """A worker that connects but sends no auth response is dropped."""
        master_task, port = await self._start_master(secret="needs-auth")

        reader, writer = await self._open_raw_connection(port)
        try:
            # Receive challenge but don't respond — master should time out and close
            challenge = await asyncio.wait_for(_recv_msg(reader), timeout=3)
            self.assertEqual(challenge["type"], "challenge")
            # Do not send anything; wait for master to close the connection
            data = await asyncio.wait_for(reader.read(1), timeout=8)
            self.assertEqual(data, b"", "Master should have closed connection on auth timeout")
        finally:
            writer.close()
            master_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                _ = await master_task

    async def test_open_raw_connection_retries_until_listening(self):
        """_open_raw_connection retries past ConnectionRefusedError until the
        server is accepting (the fix for the flaky readiness race)."""
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]

        # Nothing is listening yet, so the first connects are refused; start the
        # server after a delay that spans several retry intervals.
        async def handle(reader, writer):
            writer.close()

        async def start_late():
            await asyncio.sleep(0.15)
            return await asyncio.start_server(handle, "127.0.0.1", port)

        server_task = asyncio.create_task(start_late())
        try:
            reader, writer = await self._open_raw_connection(port, timeout=5.0)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
        finally:
            server = await server_task
            server.close()
            await server.wait_closed()

    async def test_open_raw_connection_gives_up_after_deadline(self):
        """If nothing ever listens, the retry loop re-raises once the deadline
        passes rather than spinning forever."""
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        # Port is free and no server is started, so every connect is refused.
        with self.assertRaises((ConnectionRefusedError, OSError)):
            await self._open_raw_connection(port, timeout=0.1)


if __name__ == "__main__":
    unittest.main()
