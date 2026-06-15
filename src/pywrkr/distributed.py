"""Distributed master/worker mode for pywrkr."""

import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import logging
import os
import sys
import time

from pywrkr.config import (
    DEFAULT_CONNECTIONS,
    DEFAULT_THINK_TIME_JITTER,
    DEFAULT_THREADS,
    DEFAULT_TIMEOUT,
    BenchmarkConfig,
    LatencyBreakdown,
    Scenario,
    ScenarioStep,
    SSLConfig,
    Threshold,
    WorkerStats,
    merge_stats,
)
from pywrkr.reporting import (
    evaluate_thresholds,
    print_results,
    print_threshold_results,
)
from pywrkr.workers import run_benchmark, run_user_simulation

logger = logging.getLogger(__name__)

# Hard cap on a single distributed-mode message. Length-prefixed framing on its
# own would let a peer claim a 4 GiB payload and exhaust memory before the JSON
# parser ever sees it. Configs and merged stats fit well under this in practice.
_MAX_MESSAGE_BYTES = 256 * 1024 * 1024  # 256 MiB

# Default ceiling for how long a worker should wait for the master to send the
# initial config before assuming the master is dead.
_WORKER_RECV_TIMEOUT_SECONDS = 300.0

# Timeout for the HMAC challenge-response handshake (master and worker sides).
_WORKER_AUTH_TIMEOUT_SECONDS = 5.0


def _serialize_ssl_config(ssl_cfg: SSLConfig) -> dict:
    """Serialize SSLConfig to a JSON-safe dict."""
    return {"verify": ssl_cfg.verify, "ca_bundle": ssl_cfg.ca_bundle}


def _deserialize_ssl_config(data: dict | None) -> SSLConfig:
    """Deserialize a dict back into SSLConfig."""
    if not data:
        return SSLConfig()
    return SSLConfig(verify=data.get("verify", False), ca_bundle=data.get("ca_bundle"))


def _serialize_threshold(th: Threshold) -> dict:
    return {
        "metric": th.metric,
        "operator": th.operator,
        "value": th.value,
        "raw_expr": th.raw_expr,
    }


def _deserialize_threshold(data: dict) -> Threshold:
    return Threshold(
        metric=data["metric"],
        operator=data["operator"],
        value=data["value"],
        raw_expr=data["raw_expr"],
    )


def _serialize_scenario_step(step: ScenarioStep) -> dict:
    return {
        "path": step.path,
        "method": step.method,
        "body": step.body,
        "headers": dict(step.headers),
        "assert_status": step.assert_status,
        "assert_body_contains": step.assert_body_contains,
        "think_time": step.think_time,
        "name": step.name,
    }


def _deserialize_scenario_step(data: dict) -> ScenarioStep:
    return ScenarioStep(
        path=data["path"],
        method=data.get("method", "GET"),
        body=data.get("body"),
        headers=data.get("headers", {}),
        assert_status=data.get("assert_status"),
        assert_body_contains=data.get("assert_body_contains"),
        think_time=data.get("think_time"),
        name=data.get("name"),
    )


def _serialize_scenario(scenario: Scenario | None) -> dict | None:
    if scenario is None:
        return None
    result: dict = {
        "name": scenario.name,
        "think_time": scenario.think_time,
        "steps": [_serialize_scenario_step(s) for s in scenario.steps],
    }
    if scenario.base_url:
        result["base_url"] = scenario.base_url
    return result


def _deserialize_scenario(data: dict | None) -> Scenario | None:
    if data is None:
        return None
    return Scenario(
        name=data.get("name", "Unnamed Scenario"),
        base_url=data.get("base_url"),
        think_time=data.get("think_time", 0.0),
        steps=[_deserialize_scenario_step(s) for s in data.get("steps", [])],
    )


def _serialize_config(config: BenchmarkConfig) -> dict:
    """Serialize a BenchmarkConfig to a JSON-safe dict for network transport."""
    return {
        "url": config.url,
        "connections": config.connections,
        "duration": config.duration,
        "num_requests": config.num_requests,
        "threads": config.threads,
        "method": config.method,
        "headers": dict(config.headers),
        "body": base64.b64encode(config.body).decode() if config.body is not None else None,
        "timeout_sec": config.timeout_sec,
        "keepalive": config.keepalive,
        "basic_auth": config.basic_auth,
        "cookies": list(config.cookies),
        "verify_content_length": config.verify_content_length,
        "verbosity": config.verbosity,
        "random_param": config.random_param,
        "rate": config.rate,
        "rate_ramp": config.rate_ramp,
        "latency_breakdown": config.latency_breakdown,
        "users": config.users,
        "ramp_up": config.ramp_up,
        "think_time": config.think_time,
        "think_time_jitter": config.think_time_jitter,
        # Previously missing fields:
        "ssl_config": _serialize_ssl_config(config.ssl_config),
        "tags": dict(config.tags),
        "otel_endpoint": config.otel_endpoint,
        "prom_remote_write": config.prom_remote_write,
        "thresholds": [_serialize_threshold(t) for t in config.thresholds],
        "scenario": _serialize_scenario(config.scenario),
        "html_report": config.html_report,
        "csv_output": config.csv_output,
        "json_output": config.json_output,
        "html_output": config.html_output,
        "live_dashboard": config.live_dashboard,
    }


def _deserialize_config(data: dict) -> BenchmarkConfig:
    """Deserialize a dict back into a BenchmarkConfig."""
    body = base64.b64decode(data["body"]) if data.get("body") is not None else None
    return BenchmarkConfig(
        url=data["url"],
        connections=data.get("connections", DEFAULT_CONNECTIONS),
        duration=data.get("duration"),
        num_requests=data.get("num_requests"),
        threads=data.get("threads", DEFAULT_THREADS),
        method=data.get("method", "GET"),
        headers=data.get("headers", {}),
        body=body,
        timeout_sec=data.get("timeout_sec", DEFAULT_TIMEOUT),
        keepalive=data.get("keepalive", True),
        basic_auth=data.get("basic_auth"),
        cookies=data.get("cookies", []),
        verify_content_length=data.get("verify_content_length", False),
        verbosity=data.get("verbosity", 0),
        random_param=data.get("random_param", False),
        rate=data.get("rate"),
        rate_ramp=data.get("rate_ramp"),
        latency_breakdown=data.get("latency_breakdown", False),
        users=data.get("users"),
        ramp_up=data.get("ramp_up", 0.0),
        think_time=data.get("think_time", 0.0),
        think_time_jitter=data.get("think_time_jitter", DEFAULT_THINK_TIME_JITTER),
        # Previously missing fields:
        ssl_config=_deserialize_ssl_config(data.get("ssl_config")),
        tags=data.get("tags", {}),
        otel_endpoint=data.get("otel_endpoint"),
        prom_remote_write=data.get("prom_remote_write"),
        thresholds=[_deserialize_threshold(t) for t in data.get("thresholds", [])],
        scenario=_deserialize_scenario(data.get("scenario")),
        html_report=data.get("html_report"),
        csv_output=data.get("csv_output"),
        json_output=data.get("json_output"),
        html_output=data.get("html_output", False),
        live_dashboard=data.get("live_dashboard", False),
        _quiet=True,
    )


def _serialize_stats(stats: WorkerStats) -> dict:
    """Serialize WorkerStats to a JSON-safe dict."""
    return {
        "total_requests": stats.total_requests,
        "total_bytes": stats.total_bytes,
        "errors": stats.errors,
        "content_length_errors": stats.content_length_errors,
        "latencies": list(stats.latencies),
        "latencies_total_seen": getattr(stats.latencies, "total_seen", len(stats.latencies)),
        "error_types": dict(stats.error_types),
        "status_codes": {str(k): v for k, v in stats.status_codes.items()},
        "rps_timeline": stats.rps_timeline,
        # Previously missing:
        "step_latencies": {k: v for k, v in stats.step_latencies.items()},
        "breakdowns": [
            {
                "dns": b.dns,
                "connect": b.connect,
                "tls": b.tls,
                "ttfb": b.ttfb,
                "transfer": b.transfer,
                "is_reused": b.is_reused,
            }
            for b in stats.breakdowns
        ],
        "breakdowns_total_seen": getattr(stats.breakdowns, "total_seen", len(stats.breakdowns)),
    }


def _deserialize_stats(data: dict) -> WorkerStats:
    """Deserialize a dict back into WorkerStats."""
    from pywrkr.config import ReservoirSampler

    ws = WorkerStats()
    ws.total_requests = data.get("total_requests", 0)
    ws.total_bytes = data.get("total_bytes", 0)
    ws.errors = data.get("errors", 0)
    ws.content_length_errors = data.get("content_length_errors", 0)
    lat_items = data.get("latencies", [])
    lat_seen = data.get("latencies_total_seen", len(lat_items))
    ws.latencies = ReservoirSampler.from_list(lat_items, total_seen=lat_seen)
    for k, v in data.get("error_types", {}).items():
        ws.error_types[k] = v
    for k, v in data.get("status_codes", {}).items():
        ws.status_codes[int(k)] = v
    ws.rps_timeline = [tuple(x) for x in data.get("rps_timeline", [])]
    # Previously missing:
    for k, v in data.get("step_latencies", {}).items():
        ws.step_latencies[k] = v
    bd_items = []
    for b in data.get("breakdowns", []):
        bd_items.append(
            LatencyBreakdown(
                dns=b.get("dns", 0.0),
                connect=b.get("connect", 0.0),
                tls=b.get("tls", 0.0),
                ttfb=b.get("ttfb", 0.0),
                transfer=b.get("transfer", 0.0),
                is_reused=b.get("is_reused", False),
            )
        )
    bd_seen = data.get("breakdowns_total_seen", len(bd_items))
    ws.breakdowns = ReservoirSampler.from_list(bd_items, total_seen=bd_seen)
    return ws


async def _send_msg(writer: asyncio.StreamWriter, obj: dict) -> None:
    """Send a length-prefixed JSON message."""
    payload = json.dumps(obj).encode()
    writer.write(len(payload).to_bytes(4, "big") + payload)
    await writer.drain()


async def _recv_msg(reader: asyncio.StreamReader) -> dict:
    """Receive a length-prefixed JSON message.

    Raises:
        ConnectionError: If the remote end closes before the full message
            is received (wraps asyncio.IncompleteReadError), if the peer
            announces a payload larger than the protocol limit, or if the
            message body is not valid UTF-8 JSON (wraps UnicodeDecodeError /
            json.JSONDecodeError).
    """
    try:
        length_bytes = await reader.readexactly(4)
        length = int.from_bytes(length_bytes, "big")
        if length > _MAX_MESSAGE_BYTES:
            raise ConnectionError(
                f"Peer announced message of {length} bytes, exceeds limit of {_MAX_MESSAGE_BYTES}"
            )
        payload = await reader.readexactly(length)
    except asyncio.IncompleteReadError as e:
        raise ConnectionError(
            f"Connection closed before full message received "
            f"(got {len(e.partial)} of {e.expected or '?'} bytes)"
        ) from e
    try:
        return json.loads(payload.decode())
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ConnectionError(f"Malformed message body: {e}") from e


def merge_worker_stats(stats_list: list[WorkerStats]) -> WorkerStats:
    """Merge multiple WorkerStats into one. Delegates to config.merge_stats."""
    return merge_stats(stats_list)


async def run_master(
    config: BenchmarkConfig,
    host: str,
    port: int,
    expect_workers: int,
    worker_secret: str | None = None,
    ready: asyncio.Event | None = None,
) -> tuple[WorkerStats, int] | None:
    """Run in master mode: wait for workers, distribute config, collect results.

    If *worker_secret* is set, each connecting worker must complete an
    HMAC-SHA256 challenge-response before it is admitted.  Workers that fail
    authentication or do not respond within ``_WORKER_AUTH_TIMEOUT_SECONDS``
    are disconnected.

    If *ready* is provided, it is set once the server is listening, so callers
    (and tests) can connect only after the bind completes instead of racing it.
    """
    logger.info(
        "Master: listening on %s:%s, waiting for %s worker(s)...", host, port, expect_workers
    )
    if worker_secret:
        logger.info("Master: worker authentication enabled (HMAC-SHA256).")

    worker_connections: list[tuple[asyncio.StreamReader, asyncio.StreamWriter]] = []
    ready_event = asyncio.Event()

    async def _authenticate_worker(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> bool:
        """Return True if the worker passes the HMAC challenge, False otherwise."""
        if worker_secret is None:
            return False
        nonce = os.urandom(32)
        try:
            await asyncio.wait_for(
                _send_msg(writer, {"type": "challenge", "nonce": nonce.hex()}),
                timeout=_WORKER_AUTH_TIMEOUT_SECONDS,
            )
            msg = await asyncio.wait_for(_recv_msg(reader), timeout=_WORKER_AUTH_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            logger.warning("Master: auth timeout for %s", writer.get_extra_info("peername"))
            return False
        if msg.get("type") != "auth":
            logger.warning(
                "Master: unexpected auth message type %r from %s",
                msg.get("type"),
                writer.get_extra_info("peername"),
            )
            return False
        expected = hmac.new(worker_secret.encode(), nonce, digestmod=hashlib.sha256).hexdigest()
        if not hmac.compare_digest(msg.get("hmac", ""), expected):
            logger.warning(
                "Master: auth failed for %s (wrong secret)",
                writer.get_extra_info("peername"),
            )
            return False
        return True

    async def handle_worker(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        # Reject any connection beyond expect_workers so a late/stale/retrying
        # peer that lands in the race window cannot linger in the connection
        # list and later stall result collection on a recv that never replies.
        if len(worker_connections) >= expect_workers:
            addr = writer.get_extra_info("peername")
            logger.warning("  Rejecting surplus worker connection: %s", addr)
            writer.close()
            return
        if worker_secret:
            if not await _authenticate_worker(reader, writer):
                writer.close()
                return
        addr = writer.get_extra_info("peername")
        worker_connections.append((reader, writer))
        logger.info(
            "  Worker connected: %s:%s (%s/%s)",
            addr[0],
            addr[1],
            len(worker_connections),
            expect_workers,
        )
        if len(worker_connections) >= expect_workers:
            ready_event.set()

    server = await asyncio.start_server(handle_worker, host, port)
    if ready is not None:
        # Signal that the listener is bound and accepting connections.
        ready.set()
    server_closed = False
    try:
        # Wait for all workers with a timeout
        try:
            await asyncio.wait_for(ready_event.wait(), timeout=300)
        except asyncio.TimeoutError:
            logger.error(
                "Master: timed out waiting for workers (%s/%s connected)",
                len(worker_connections),
                expect_workers,
            )
            for _, w in worker_connections:
                w.close()
                await w.wait_closed()
            return None

        # Snapshot the first expect_workers connections and stop accepting more
        # before distributing config, so collection only ever talks to the
        # fixed set of workers we sent a config to. close() synchronously stops
        # accepting new connections; we deliberately do NOT await
        # wait_closed() here because the accepted worker connections are still
        # in use for the run, and in Python 3.12 wait_closed() blocks until
        # those handler connections finish, which would deadlock collection.
        selected = worker_connections[:expect_workers]
        server.close()
        server_closed = True

        logger.info("Master: all %s workers connected. Distributing config...", expect_workers)
        config_data = _serialize_config(config)
        for _, writer in selected:
            await _send_msg(writer, {"type": "config", "config": config_data})

        logger.info("Master: benchmark running on all workers...")

        # Collect results concurrently under a single shared deadline so a slow
        # worker cannot block reads from already-finished workers and the
        # timeout is an overall budget rather than per-worker (N * timeout).
        loop = asyncio.get_event_loop()
        deadline = loop.time() + (config.duration * 3 + 120 if config.duration else 600)
        all_stats: list[WorkerStats] = []
        worker_durations: list[float] = []

        async def _collect_one(
            idx: int, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ):
            try:
                msg = await asyncio.wait_for(
                    _recv_msg(reader), timeout=max(0.0, deadline - loop.time())
                )
                if msg.get("type") == "result":
                    ws = _deserialize_stats(msg["stats"])
                    all_stats.append(ws)
                    reported = msg.get("duration")
                    if isinstance(reported, (int, float)) and reported > 0:
                        worker_durations.append(float(reported))
                    addr = writer.get_extra_info("peername")
                    logger.info(
                        "  Worker %s:%s finished: %s requests, %s errors",
                        addr[0],
                        addr[1],
                        f"{ws.total_requests:,}",
                        ws.errors,
                    )
                else:
                    logger.error("  Worker %s: unexpected message type: %s", idx, msg.get("type"))
            except (asyncio.TimeoutError, ConnectionError, OSError) as e:
                logger.error("  Worker %s: error receiving results: %s", idx, e)
            finally:
                writer.close()
                # A misbehaving peer may have already reset the connection;
                # a failure while closing must not crash the collection.
                with contextlib.suppress(OSError, ConnectionError):
                    await writer.wait_closed()

        await asyncio.gather(
            *(_collect_one(i, reader, writer) for i, (reader, writer) in enumerate(selected))
        )
    finally:
        if not server_closed:
            server.close()

    if not all_stats:
        logger.error("Master: no results received from workers.")
        return None

    # Merge and report
    merged = merge_worker_stats(all_stats)
    # In request-count (-n) mode config.duration is None. Use the real measured
    # wall-clock reported by the workers (max, since they run in parallel)
    # instead of fabricating a fixed window, which would otherwise make the
    # reported Requests/sec and any rps/throughput threshold meaningless.
    if config.duration:
        actual_duration = config.duration
    elif worker_durations:
        actual_duration = max(worker_durations)
    else:
        actual_duration = 10.0

    logger.info("Master: %s worker(s) reported. Merged results:", len(all_stats))
    # Override _quiet for printing
    report_config = BenchmarkConfig(
        url=config.url,
        connections=config.connections * len(all_stats),
        duration=config.duration,
        method=config.method,
        keepalive=config.keepalive,
        users=config.users,
        ramp_up=config.ramp_up,
        think_time=config.think_time,
        think_time_jitter=config.think_time_jitter,
        csv_output=config.csv_output,
        json_output=config.json_output,
        html_output=config.html_output,
        html_report=config.html_report,
        tags=config.tags,
        otel_endpoint=config.otel_endpoint,
        prom_remote_write=config.prom_remote_write,
        thresholds=config.thresholds,
        rate=config.rate,
        rate_ramp=config.rate_ramp,
    )
    # Worker timelines are rebased to per-worker [0, duration) offsets in
    # merge_worker_stats, so the master buckets them from start=0.0 rather than
    # against its own (unrelated) monotonic clock.
    print_results(
        merged,
        actual_duration,
        report_config.connections,
        0.0,
        report_config,
    )

    # Evaluate SLO thresholds
    exit_code = 0
    if config.thresholds:
        th_results = evaluate_thresholds(config.thresholds, merged, actual_duration)
        print_threshold_results(th_results, file=sys.stdout)
        if any(not passed for _, _, passed in th_results):
            exit_code = 2

    return merged, exit_code


async def run_worker_node(
    master_host: str, master_port: int, worker_secret: str | None = None
) -> None:
    """Run in worker mode: connect to master, receive config, run benchmark, send results.

    If *worker_secret* is provided, the worker completes the HMAC-SHA256
    challenge-response expected by a master started with the same secret.
    """
    logger.info("Worker: connecting to master at %s:%s...", master_host, master_port)

    reader, writer = await asyncio.open_connection(master_host, master_port)
    logger.info("Worker: connected to master, waiting for config...")

    try:
        if worker_secret:
            try:
                challenge = await asyncio.wait_for(
                    _recv_msg(reader), timeout=_WORKER_AUTH_TIMEOUT_SECONDS
                )
            except asyncio.TimeoutError:
                logger.error("Worker: timed out waiting for auth challenge from master")
                return
            if challenge.get("type") != "challenge":
                logger.error("Worker: expected auth challenge, got %r", challenge.get("type"))
                return
            nonce = bytes.fromhex(challenge["nonce"])
            response = hmac.new(worker_secret.encode(), nonce, digestmod=hashlib.sha256).hexdigest()
            try:
                await asyncio.wait_for(
                    _send_msg(writer, {"type": "auth", "hmac": response}),
                    timeout=_WORKER_AUTH_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                logger.error("Worker: timed out sending auth response to master")
                return

        try:
            msg = await asyncio.wait_for(_recv_msg(reader), timeout=_WORKER_RECV_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            logger.error(
                "Worker: timed out after %ss waiting for config from master",
                _WORKER_RECV_TIMEOUT_SECONDS,
            )
            return
        except (ConnectionError, OSError) as e:
            logger.error("Worker: failed to receive config from master: %s", e)
            return
        if msg.get("type") != "config":
            logger.error("Worker: unexpected message type: %s", msg.get("type"))
            return

        config = _deserialize_config(msg["config"])
        logger.info("Worker: received config. Target: %s", config.url)
        logger.info("Worker: starting benchmark...")

        # Measure real wall-clock so the master can report throughput against the
        # actual run window in -n (request-count) mode instead of a fixed guess.
        run_start = time.monotonic()
        # Run the appropriate benchmark
        if config.users is not None:
            stats, _ = await run_user_simulation(config)
        else:
            stats, _ = await run_benchmark(config)
        run_duration = time.monotonic() - run_start

        logger.info(
            "Worker: benchmark complete. %s requests, %s errors",
            f"{stats.total_requests:,}",
            stats.errors,
        )

        # Send results back to master, including the measured run duration.
        await _send_msg(
            writer,
            {"type": "result", "stats": _serialize_stats(stats), "duration": run_duration},
        )
        logger.info("Worker: results sent to master. Done.")
    finally:
        writer.close()
        await writer.wait_closed()
