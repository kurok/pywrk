"""Distributed master/worker mode for pywrkr."""

import asyncio
import base64
import json
import logging
import sys
import time

from pywrkr.config import (
    DEFAULT_CONNECTIONS,
    DEFAULT_THINK_TIME_JITTER,
    DEFAULT_THREADS,
    DEFAULT_TIMEOUT,
    BenchmarkConfig,
    LatencyBreakdown,
    SSLConfig,
    Scenario,
    ScenarioStep,
    Threshold,
    WorkerStats,
)
from pywrkr.reporting import (
    evaluate_thresholds,
    print_results,
    print_threshold_results,
)
from pywrkr.workers import run_benchmark, run_user_simulation

logger = logging.getLogger(__name__)


def _serialize_ssl_config(ssl_cfg: SSLConfig) -> dict:
    """Serialize SSLConfig to a JSON-safe dict."""
    return {"verify": ssl_cfg.verify, "ca_bundle": ssl_cfg.ca_bundle}


def _deserialize_ssl_config(data: dict | None) -> SSLConfig:
    """Deserialize a dict back into SSLConfig."""
    if not data:
        return SSLConfig()
    return SSLConfig(verify=data.get("verify", False), ca_bundle=data.get("ca_bundle"))


def _serialize_threshold(th: Threshold) -> dict:
    return {"metric": th.metric, "operator": th.operator, "value": th.value, "raw_expr": th.raw_expr}


def _deserialize_threshold(data: dict) -> Threshold:
    return Threshold(metric=data["metric"], operator=data["operator"],
                     value=data["value"], raw_expr=data["raw_expr"])


def _serialize_scenario_step(step: ScenarioStep) -> dict:
    return {
        "path": step.path, "method": step.method, "body": step.body,
        "headers": dict(step.headers), "assert_status": step.assert_status,
        "assert_body_contains": step.assert_body_contains,
        "think_time": step.think_time, "name": step.name,
    }


def _deserialize_scenario_step(data: dict) -> ScenarioStep:
    return ScenarioStep(
        path=data["path"], method=data.get("method", "GET"), body=data.get("body"),
        headers=data.get("headers", {}), assert_status=data.get("assert_status"),
        assert_body_contains=data.get("assert_body_contains"),
        think_time=data.get("think_time"), name=data.get("name"),
    )


def _serialize_scenario(scenario: Scenario | None) -> dict | None:
    if scenario is None:
        return None
    return {
        "name": scenario.name,
        "think_time": scenario.think_time,
        "steps": [_serialize_scenario_step(s) for s in scenario.steps],
    }


def _deserialize_scenario(data: dict | None) -> Scenario | None:
    if data is None:
        return None
    return Scenario(
        name=data.get("name", "Unnamed Scenario"),
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
        "body": base64.b64encode(config.body).decode() if config.body else None,
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
    body = base64.b64decode(data["body"]) if data.get("body") else None
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
            {"dns": b.dns, "connect": b.connect, "tls": b.tls,
             "ttfb": b.ttfb, "transfer": b.transfer, "is_reused": b.is_reused}
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
        bd_items.append(LatencyBreakdown(
            dns=b.get("dns", 0.0), connect=b.get("connect", 0.0),
            tls=b.get("tls", 0.0), ttfb=b.get("ttfb", 0.0),
            transfer=b.get("transfer", 0.0), is_reused=b.get("is_reused", False),
        ))
    bd_seen = data.get("breakdowns_total_seen", len(bd_items))
    ws.breakdowns = ReservoirSampler.from_list(bd_items, total_seen=bd_seen)
    return ws


async def _send_msg(writer: asyncio.StreamWriter, obj: dict) -> None:
    """Send a length-prefixed JSON message."""
    payload = json.dumps(obj).encode()
    writer.write(len(payload).to_bytes(4, "big") + payload)
    await writer.drain()


async def _recv_msg(reader: asyncio.StreamReader) -> dict:
    """Receive a length-prefixed JSON message."""
    length_bytes = await reader.readexactly(4)
    length = int.from_bytes(length_bytes, "big")
    payload = await reader.readexactly(length)
    return json.loads(payload.decode())


def merge_worker_stats(stats_list: list[WorkerStats]) -> WorkerStats:
    """Merge multiple WorkerStats into one."""
    merged = WorkerStats()
    for ws in stats_list:
        merged.total_requests += ws.total_requests
        merged.total_bytes += ws.total_bytes
        merged.errors += ws.errors
        merged.content_length_errors += ws.content_length_errors
        merged.latencies.extend(ws.latencies)
        merged.rps_timeline.extend(ws.rps_timeline)
        for k, v in ws.error_types.items():
            merged.error_types[k] += v
        for k, v in ws.status_codes.items():
            merged.status_codes[k] += v
        for k, v in ws.step_latencies.items():
            merged.step_latencies[k].extend(v)
        merged.breakdowns.extend(ws.breakdowns)
    return merged


async def run_master(config: BenchmarkConfig, host: str, port: int, expect_workers: int) -> tuple[WorkerStats, int] | None:
    """Run in master mode: wait for workers, distribute config, collect results."""
    logger.info("Master: listening on %s:%s, waiting for %s worker(s)...", host, port, expect_workers)

    worker_connections: list[tuple[asyncio.StreamReader, asyncio.StreamWriter]] = []
    ready_event = asyncio.Event()

    async def handle_worker(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        addr = writer.get_extra_info("peername")
        worker_connections.append((reader, writer))
        logger.info("  Worker connected: %s:%s (%s/%s)",
                    addr[0], addr[1], len(worker_connections), expect_workers)
        if len(worker_connections) >= expect_workers:
            ready_event.set()

    server = await asyncio.start_server(handle_worker, host, port)
    async with server:
        # Wait for all workers with a timeout
        try:
            await asyncio.wait_for(ready_event.wait(), timeout=300)
        except asyncio.TimeoutError:
            logger.error("Master: timed out waiting for workers (%s/%s connected)",
                        len(worker_connections), expect_workers)
            for _, w in worker_connections:
                w.close()
            return

        logger.info("Master: all %s workers connected. Distributing config...", expect_workers)
        config_data = _serialize_config(config)
        for _, writer in worker_connections:
            await _send_msg(writer, {"type": "config", "config": config_data})

        logger.info("Master: benchmark running on all workers...")

        # Collect results
        all_stats: list[WorkerStats] = []
        for i, (reader, writer) in enumerate(worker_connections):
            try:
                msg = await asyncio.wait_for(_recv_msg(reader), timeout=config.duration * 3 + 120 if config.duration else 600)
                if msg.get("type") == "result":
                    ws = _deserialize_stats(msg["stats"])
                    all_stats.append(ws)
                    addr = writer.get_extra_info("peername")
                    logger.info("  Worker %s:%s finished: %s requests, %s errors",
                               addr[0], addr[1], f"{ws.total_requests:,}", ws.errors)
                else:
                    logger.error("  Worker %s: unexpected message type: %s", i, msg.get("type"))
            except (asyncio.TimeoutError, ConnectionError, OSError) as e:
                logger.error("  Worker %s: error receiving results: %s", i, e)
            finally:
                writer.close()

    if not all_stats:
        logger.error("Master: no results received from workers.")
        return

    # Merge and report
    merged = merge_worker_stats(all_stats)
    total_duration = max(
        sum(ws.total_requests for ws in all_stats) / (merged.total_requests / max(merged.latencies) if merged.latencies else 1),
        config.duration or 10.0,
    ) if merged.latencies else (config.duration or 10.0)

    # Use actual duration from config for reporting
    actual_duration = config.duration or 10.0

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
    print_results(merged, actual_duration, report_config.connections,
                  time.monotonic() - actual_duration, report_config)

    # Evaluate SLO thresholds
    exit_code = 0
    if config.thresholds:
        th_results = evaluate_thresholds(config.thresholds, merged, actual_duration)
        print_threshold_results(th_results, file=sys.stdout)
        if any(not passed for _, _, passed in th_results):
            exit_code = 2

    return merged, exit_code


async def run_worker_node(master_host: str, master_port: int) -> None:
    """Run in worker mode: connect to master, receive config, run benchmark, send results."""
    logger.info("Worker: connecting to master at %s:%s...", master_host, master_port)

    reader, writer = await asyncio.open_connection(master_host, master_port)
    logger.info("Worker: connected to master, waiting for config...")

    msg = await _recv_msg(reader)
    if msg.get("type") != "config":
        logger.error("Worker: unexpected message type: %s", msg.get("type"))
        writer.close()
        return

    config = _deserialize_config(msg["config"])
    logger.info("Worker: received config. Target: %s", config.url)
    logger.info("Worker: starting benchmark...")

    # Run the appropriate benchmark
    if config.users is not None:
        stats, _ = await run_user_simulation(config)
    else:
        stats, _ = await run_benchmark(config)

    logger.info("Worker: benchmark complete. %s requests, %s errors",
                f"{stats.total_requests:,}", stats.errors)

    # Send results back to master
    await _send_msg(writer, {"type": "result", "stats": _serialize_stats(stats)})
    writer.close()
    logger.info("Worker: results sent to master. Done.")
