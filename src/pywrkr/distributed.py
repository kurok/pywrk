"""Distributed master/worker mode for pywrkr."""

import asyncio
import base64
import json
import sys
import time

from pywrkr.config import (
    DEFAULT_CONNECTIONS,
    DEFAULT_THINK_TIME_JITTER,
    DEFAULT_THREADS,
    DEFAULT_TIMEOUT,
    BenchmarkConfig,
    WorkerStats,
)
from pywrkr.reporting import (
    evaluate_thresholds,
    print_results,
    print_threshold_results,
)
from pywrkr.workers import run_benchmark, run_user_simulation


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
        _quiet=True,
    )


def _serialize_stats(stats: WorkerStats) -> dict:
    """Serialize WorkerStats to a JSON-safe dict."""
    return {
        "total_requests": stats.total_requests,
        "total_bytes": stats.total_bytes,
        "errors": stats.errors,
        "content_length_errors": stats.content_length_errors,
        "latencies": stats.latencies,
        "error_types": dict(stats.error_types),
        "status_codes": {str(k): v for k, v in stats.status_codes.items()},
        "rps_timeline": stats.rps_timeline,
    }


def _deserialize_stats(data: dict) -> WorkerStats:
    """Deserialize a dict back into WorkerStats."""
    ws = WorkerStats()
    ws.total_requests = data.get("total_requests", 0)
    ws.total_bytes = data.get("total_bytes", 0)
    ws.errors = data.get("errors", 0)
    ws.content_length_errors = data.get("content_length_errors", 0)
    ws.latencies = data.get("latencies", [])
    for k, v in data.get("error_types", {}).items():
        ws.error_types[k] = v
    for k, v in data.get("status_codes", {}).items():
        ws.status_codes[int(k)] = v
    ws.rps_timeline = [tuple(x) for x in data.get("rps_timeline", [])]
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
    print(f"Master: listening on {host}:{port}, waiting for {expect_workers} worker(s)...")

    worker_connections: list[tuple[asyncio.StreamReader, asyncio.StreamWriter]] = []
    ready_event = asyncio.Event()

    async def handle_worker(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        addr = writer.get_extra_info("peername")
        worker_connections.append((reader, writer))
        print(f"  Worker connected: {addr[0]}:{addr[1]} "
              f"({len(worker_connections)}/{expect_workers})")
        if len(worker_connections) >= expect_workers:
            ready_event.set()

    server = await asyncio.start_server(handle_worker, host, port)
    async with server:
        # Wait for all workers with a timeout
        try:
            await asyncio.wait_for(ready_event.wait(), timeout=300)
        except asyncio.TimeoutError:
            print(f"Master: timed out waiting for workers "
                  f"({len(worker_connections)}/{expect_workers} connected)")
            for _, w in worker_connections:
                w.close()
            return

        print(f"\nMaster: all {expect_workers} workers connected. Distributing config...")
        config_data = _serialize_config(config)
        for _, writer in worker_connections:
            await _send_msg(writer, {"type": "config", "config": config_data})

        print("Master: benchmark running on all workers...")

        # Collect results
        all_stats: list[WorkerStats] = []
        for i, (reader, writer) in enumerate(worker_connections):
            try:
                msg = await asyncio.wait_for(_recv_msg(reader), timeout=config.duration * 3 + 120 if config.duration else 600)
                if msg.get("type") == "result":
                    ws = _deserialize_stats(msg["stats"])
                    all_stats.append(ws)
                    addr = writer.get_extra_info("peername")
                    print(f"  Worker {addr[0]}:{addr[1]} finished: "
                          f"{ws.total_requests:,} requests, {ws.errors} errors")
                else:
                    print(f"  Worker {i}: unexpected message type: {msg.get('type')}")
            except Exception as e:
                print(f"  Worker {i}: error receiving results: {e}")
            finally:
                writer.close()

    if not all_stats:
        print("Master: no results received from workers.")
        return

    # Merge and report
    merged = merge_worker_stats(all_stats)
    total_duration = max(
        sum(ws.total_requests for ws in all_stats) / (merged.total_requests / max(merged.latencies) if merged.latencies else 1),
        config.duration or 10.0,
    ) if merged.latencies else (config.duration or 10.0)

    # Use actual duration from config for reporting
    actual_duration = config.duration or 10.0

    print(f"\nMaster: {len(all_stats)} worker(s) reported. Merged results:\n")
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
    print(f"Worker: connecting to master at {master_host}:{master_port}...")

    reader, writer = await asyncio.open_connection(master_host, master_port)
    print("Worker: connected to master, waiting for config...")

    msg = await _recv_msg(reader)
    if msg.get("type") != "config":
        print(f"Worker: unexpected message type: {msg.get('type')}")
        writer.close()
        return

    config = _deserialize_config(msg["config"])
    print(f"Worker: received config. Target: {config.url}")
    print(f"Worker: starting benchmark...")

    # Run the appropriate benchmark
    if config.users is not None:
        stats, _ = await run_user_simulation(config)
    else:
        stats, _ = await run_benchmark(config)

    print(f"Worker: benchmark complete. {stats.total_requests:,} requests, "
          f"{stats.errors} errors")

    # Send results back to master
    await _send_msg(writer, {"type": "result", "stats": _serialize_stats(stats)})
    writer.close()
    print("Worker: results sent to master. Done.")
