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
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pywrkr.backends import HTTP2_INSTALL_HINT, http2_available
from pywrkr.compare import parse_fail_on
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
    parse_extract_spec,
)
from pywrkr.feeders import (
    CONSUMING_STRATEGIES,
    FEEDER_STRATEGIES,
    Feeder,
    shard_rows,
)
from pywrkr.reporting import (
    evaluate_thresholds,
    print_results,
    print_threshold_results,
    run_baseline_gate,
)
from pywrkr.workers import run_benchmark, run_user_simulation

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pywrkr.streaming import Snapshot

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
        # Without this a per-step threshold would arrive at the worker as an
        # aggregate one, silently gating on a different number.
        "step": th.step,
    }


def _deserialize_threshold(data: dict) -> Threshold:
    return Threshold(
        metric=data["metric"],
        operator=data["operator"],
        value=data["value"],
        raw_expr=data["raw_expr"],
        step=data.get("step"),
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
        # Extractors travel in their scenario-file shape ({"json": "$.token"});
        # the worker recompiles them, so compiled regexes never cross the wire.
        "extract": {name: {rule.source: rule.expr} for name, rule in step.extract.items()},
        # Without these a distributed run would turn a ws: step into an HTTP
        # request against a ws:// URL, which fails obscurely on the worker.
        "ws": step.ws,
        "send": step.send,
        "expect_message_contains": step.expect_message_contains,
        "hold": step.hold,
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
        extract=parse_extract_spec(data.get("extract"), f"Step {data.get('name') or data['path']}"),
        ws=data.get("ws"),
        send=data.get("send"),
        expect_message_contains=data.get("expect_message_contains"),
        hold=data.get("hold", 0.0) or 0.0,
    )


def _serialize_scenario(scenario: Scenario | None) -> dict | None:
    if scenario is None:
        return None
    result: dict = {
        "name": scenario.name,
        "think_time": scenario.think_time,
        "steps": [_serialize_scenario_step(s) for s in scenario.steps],
        "on_extract_failure": scenario.on_extract_failure,
        "on_template_error": scenario.on_template_error,
        "session": scenario.session,
        # Rows travel with the config; see _shard_config_feeders for how they are
        # split so a consuming strategy stays globally unique across nodes.
        "data": {
            name: {
                "strategy": feeder.strategy,
                "source": feeder.source,
                "rows": [dict(row) for row in feeder.rows],
            }
            for name, feeder in scenario.data.items()
        },
    }
    if scenario.base_url:
        result["base_url"] = scenario.base_url
    return result


def _deserialize_scenario(data: dict | None) -> Scenario | None:
    if data is None:
        return None
    defaults = Scenario()
    return Scenario(
        name=data.get("name", "Unnamed Scenario"),
        base_url=data.get("base_url"),
        think_time=data.get("think_time", 0.0),
        steps=[_deserialize_scenario_step(s) for s in data.get("steps", [])],
        on_extract_failure=data.get("on_extract_failure", defaults.on_extract_failure),
        on_template_error=data.get("on_template_error", defaults.on_template_error),
        session=data.get("session", defaults.session),
        data={
            name: Feeder(
                name=name,
                strategy=spec.get("strategy", FEEDER_STRATEGIES[0]),
                rows=tuple(spec.get("rows", [])),
                source=spec.get("source", ""),
            )
            for name, spec in (data.get("data") or {}).items()
        },
    )


def _shard_config_feeders(config_data: dict, index: int, count: int) -> dict:
    """Return *config_data* with this node's slice of the consuming data sets.

    ``unique`` and ``sequential`` promise a row is used at most once for the
    whole run, which a per-node cursor cannot enforce on its own — every node
    would start at row 0. Handing each node a disjoint contiguous range makes the
    promise hold across the cluster. ``loop`` and ``random`` reuse rows by
    design, so they are sent whole.
    """
    scenario = config_data.get("scenario")
    if not scenario or not scenario.get("data") or count <= 1:
        return config_data

    sharded = {}
    for name, spec in scenario["data"].items():
        if spec.get("strategy") not in CONSUMING_STRATEGIES:
            sharded[name] = spec
            continue
        rows = list(shard_rows(tuple(spec.get("rows", [])), index, count))
        sharded[name] = {**spec, "rows": rows}
        logger.debug(
            "Master: data set %r -> worker %d gets %d of %d rows",
            name,
            index,
            len(rows),
            len(spec.get("rows", [])),
        )

    return {**config_data, "scenario": {**scenario, "data": sharded}}


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
        "session_cookies": config.session_cookies,
        "http2": config.http2,
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
        "export_interval": config.export_interval,
        "thresholds": [_serialize_threshold(t) for t in config.thresholds],
        # Carried for a faithful round-trip; a worker never acts on them, because
        # _finalize_run skips the gate for a sub-run (see run_baseline_gate).
        "baseline": config.baseline,
        "save_baseline": config.save_baseline,
        "fail_on": [rule.raw for rule in config.fail_on],
        "strict_config": config.strict_config,
        "compare_format": config.compare_format,
        # Without this a worker would read bodies while the master reported a
        # run that did not, or the reverse.
        "read_body": config.read_body,
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
        session_cookies=data.get("session_cookies", True),
        http2=data.get("http2", False),
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
        export_interval=data.get("export_interval"),
        thresholds=[_deserialize_threshold(t) for t in data.get("thresholds", [])],
        baseline=data.get("baseline"),
        save_baseline=data.get("save_baseline"),
        fail_on=[parse_fail_on(expr) for expr in data.get("fail_on", [])],
        strict_config=data.get("strict_config", False),
        compare_format=data.get("compare_format", "table"),
        read_body=data.get("read_body", True),
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
        "extract_failures": stats.extract_failures,
        "template_errors": stats.template_errors,
        "latencies": list(stats.latencies),
        "latencies_total_seen": getattr(stats.latencies, "total_seen", len(stats.latencies)),
        "error_types": dict(stats.error_types),
        "status_codes": {str(k): v for k, v in stats.status_codes.items()},
        "http_versions": dict(stats.http_versions),
        "rps_timeline": stats.rps_timeline,
        # Previously missing:
        "step_latencies": {k: v for k, v in stats.step_latencies.items()},
        "step_errors": dict(stats.step_errors),
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
    ws.extract_failures = data.get("extract_failures", 0)
    ws.template_errors = data.get("template_errors", 0)
    lat_items = data.get("latencies", [])
    lat_seen = data.get("latencies_total_seen", len(lat_items))
    ws.latencies = ReservoirSampler.from_list(lat_items, total_seen=lat_seen)
    for k, v in data.get("error_types", {}).items():
        ws.error_types[k] = v
    for k, v in data.get("status_codes", {}).items():
        ws.status_codes[int(k)] = v
    for k, v in data.get("http_versions", {}).items():
        ws.http_versions[k] = v
    ws.rps_timeline = [tuple(x) for x in data.get("rps_timeline", [])]
    # Previously missing:
    for k, v in data.get("step_latencies", {}).items():
        ws.step_latencies[k] = v
    for k, v in data.get("step_errors", {}).items():
        ws.step_errors[k] = v
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


# ---------------------------------------------------------------------------
# Progress reporting (worker -> master, during the run)
# ---------------------------------------------------------------------------

#: Config key the master sets to ask workers for periodic progress. Absent
#: means "do not send", so a new worker talking to an old master stays silent
#: instead of provoking an unexpected-message error every interval.
PROGRESS_INTERVAL_KEY = "progress_interval"

#: Message type carrying one interval's numbers. `result` is untouched, so the
#: end-of-run path is identical whether or not progress is in use.
MSG_PROGRESS = "progress"


def progress_interval_for(config: BenchmarkConfig) -> "float | None":
    """The interval to ask workers to report at, or None to ask for nothing.

    Only asked for when the master actually has somewhere to put the data.
    Absent from the config message otherwise -- which is also exactly what an
    older master looks like, so a worker's silence is the correct default in
    both cases and no version negotiation is needed.
    """
    if not config.export_interval:
        return None
    if not (config.otel_endpoint or config.prom_remote_write):
        return None
    return float(config.export_interval)


def requested_progress_interval(config_message: dict) -> "float | None":
    """What the master asked a worker to report at, if anything.

    A bool is not a number here: ``True`` would otherwise be read as a
    one-second interval.
    """
    value = config_message.get(PROGRESS_INTERVAL_KEY)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value > 0 else None


def _serialize_snapshot(snapshot: "Snapshot", seq: int) -> dict:
    """Wire form of one interval.

    Counters are cumulative for the sending worker, so the master must use each
    worker's *latest* message rather than summing every message it has received.
    Raw ``window_samples`` travel because percentiles cannot be merged across
    nodes by averaging: the master pools the samples and computes its own.
    """
    return {
        "type": MSG_PROGRESS,
        "seq": seq,
        "elapsed": round(snapshot.elapsed, 3),
        "total_requests": snapshot.total_requests,
        "total_errors": snapshot.total_errors,
        "total_bytes": snapshot.total_bytes,
        "window_seconds": round(snapshot.window_seconds, 6),
        "window_requests": snapshot.window_requests,
        "window_errors": snapshot.window_errors,
        "window_samples": [round(x, 6) for x in snapshot.window_samples],
        "active_users": snapshot.active_users,
        "final": snapshot.final,
    }


@dataclass
class WorkerProgress:
    """The master's view of one worker's latest interval."""

    seq: int
    elapsed: float
    total_requests: int
    total_errors: int
    total_bytes: int
    window_seconds: float
    window_requests: int
    window_errors: int
    window_samples: list[float] = field(default_factory=list)
    active_users: "int | None" = None
    received_at: float = 0.0


def _deserialize_progress(data: dict, received_at: float) -> WorkerProgress:
    def _num(key: str, default=0):
        value = data.get(key, default)
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) else default

    samples = data.get("window_samples")
    return WorkerProgress(
        seq=int(_num("seq")),
        elapsed=float(_num("elapsed")),
        total_requests=int(_num("total_requests")),
        total_errors=int(_num("total_errors")),
        total_bytes=int(_num("total_bytes")),
        window_seconds=float(_num("window_seconds")),
        window_requests=int(_num("window_requests")),
        window_errors=int(_num("window_errors")),
        window_samples=[float(x) for x in samples if isinstance(x, (int, float))]
        if isinstance(samples, list)
        else [],
        active_users=data.get("active_users")
        if isinstance(data.get("active_users"), int)
        else None,
        received_at=received_at,
    )


class ProgressAggregator:
    """Merges the workers' latest intervals into one cluster-wide snapshot.

    Two things this has to get right:

    * **Counters must not go backwards.** Each worker's numbers are cumulative
      for that worker, so the cluster total is the sum of each worker's *latest*
      report. Summing everything received instead would climb far too fast and
      would not be monotonic when reports arrive out of step.
    * **A worker that stops reporting is not a worker that stopped working, and
      neither is it one still going.** Its last figures stay in the cumulative
      totals -- the requests it sent really happened -- but it is excluded from
      the current window and named as stale, so a partitioned node cannot make
      the cluster look busier than it is.
    """

    def __init__(self, expect_workers: int, interval: float) -> None:
        self.expect_workers = expect_workers
        self.interval = max(interval, 0.001)
        self._latest: dict[int, WorkerProgress] = {}
        self._consumed_seq: dict[int, int] = {}

    def record(self, index: int, progress: WorkerProgress) -> None:
        """Take one worker's report, ignoring one that arrives out of order."""
        previous = self._latest.get(index)
        if previous is not None and progress.seq < previous.seq:
            logger.debug(
                "Master: ignoring out-of-order progress from worker %s (seq %s < %s)",
                index,
                progress.seq,
                previous.seq,
            )
            return
        self._latest[index] = progress

    @property
    def reporting(self) -> int:
        return len(self._latest)

    def stale_workers(self, now: float, max_age: "float | None" = None) -> list[int]:
        """Workers whose last report is too old to count as current."""
        limit = max_age if max_age is not None else self.interval * 3
        return sorted(i for i, p in self._latest.items() if now - p.received_at > limit)

    def build(self, now: float, elapsed: float, final: bool = False) -> "Snapshot | None":
        """One cluster snapshot, or None when no worker has reported yet."""
        from pywrkr.streaming import Snapshot, window_percentiles

        if not self._latest:
            return None
        stale = set(self.stale_workers(now))

        total_requests = sum(p.total_requests for p in self._latest.values())
        total_errors = sum(p.total_errors for p in self._latest.values())
        total_bytes = sum(p.total_bytes for p in self._latest.values())

        samples: list[float] = []
        window_requests = 0
        window_errors = 0
        window_seconds = 0.0
        for index, progress in self._latest.items():
            if index in stale or self._consumed_seq.get(index) == progress.seq:
                # Either the worker has gone quiet, or this is the same report
                # the previous window already counted. Reusing it would invent
                # traffic that did not happen in this interval.
                continue
            self._consumed_seq[index] = progress.seq
            samples.extend(progress.window_samples)
            window_requests += progress.window_requests
            window_errors += progress.window_errors
            window_seconds = max(window_seconds, progress.window_seconds)

        return Snapshot(
            elapsed=elapsed,
            total_requests=total_requests,
            total_errors=total_errors,
            total_bytes=total_bytes,
            window_seconds=window_seconds or self.interval,
            window_requests=window_requests,
            window_errors=window_errors,
            window_percentiles=window_percentiles(samples),
            window_latency=_window_latency_of(samples),
            active_users=_sum_active_users(self._latest, stale),
            final=final,
        )


def _window_latency_of(samples: list[float]) -> dict[str, float]:
    from pywrkr.streaming import _window_latency

    return _window_latency(samples)


def _sum_active_users(latest: dict[int, WorkerProgress], stale: "set[int]") -> "int | None":
    counts = [
        p.active_users for i, p in latest.items() if i not in stale and p.active_users is not None
    ]
    return sum(counts) if counts else None


class _MasterExporter:
    """Exports the cluster-wide snapshot on the master's own interval.

    Sampling is driven by the master's clock rather than by message arrival, so
    one chatty worker cannot pull the export cadence off the interval that was
    asked for.
    """

    def __init__(
        self,
        config: BenchmarkConfig,
        aggregator: ProgressAggregator,
        interval: float,
        start: float,
    ) -> None:
        self._config = config
        self._aggregator = aggregator
        self._interval = interval
        self._start = start
        self._task: "asyncio.Task | None" = None
        self._stop = asyncio.Event()
        self.exported = 0
        self.failed = 0

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        loop = asyncio.get_running_loop()
        while not self._stop.is_set():
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
                return
            self._export(loop.time(), final=False)

    async def aclose(self, now: float) -> None:
        """Emit a final cluster snapshot and stop.

        The final export matters most for a run cut short: the master otherwise
        has nothing at all, even though every worker had numbers the whole time.
        """
        if self._task is None:
            return
        self._stop.set()
        self._export(now, final=True)
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    def _export(self, now: float, final: bool) -> None:
        snapshot = self._aggregator.build(now, elapsed=now - self._start, final=final)
        if snapshot is None:
            return
        stale = self._aggregator.stale_workers(now)
        if stale:
            logger.warning(
                "Master: worker(s) %s have not reported for more than %.1fs; excluded from "
                "this interval",
                ", ".join(str(i) for i in stale),
                self._interval * 3,
            )
        from pywrkr.reporting import export_to_otel, export_to_prometheus

        results = snapshot.to_results_dict()
        tags = dict(self._config.tags)
        tags["export"] = "final" if final else "interval"
        tags["role"] = "master"
        tags["workers_reporting"] = str(self._aggregator.reporting - len(stale))
        ok = True
        if self._config.otel_endpoint:
            ok = export_to_otel(results, self._config.otel_endpoint, tags) and ok
        if self._config.prom_remote_write:
            ok = export_to_prometheus(results, self._config.prom_remote_write, tags) and ok
        if ok:
            self.exported += 1
        else:
            self.failed += 1


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
    # Bound before the try: the cleanup below runs even when the master is
    # cancelled while still waiting for workers, long before these are built.
    aggregator: "ProgressAggregator | None" = None
    master_exporter: "_MasterExporter | None" = None

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
        progress_interval = progress_interval_for(config)
        for shard_index, (_, writer) in enumerate(selected):
            message: dict = {
                "type": "config",
                "config": _shard_config_feeders(config_data, shard_index, expect_workers),
            }
            if progress_interval:
                message[PROGRESS_INTERVAL_KEY] = progress_interval
            await _send_msg(writer, message)

        logger.info("Master: benchmark running on all workers...")

        # Collect results concurrently under a single shared deadline so a slow
        # worker cannot block reads from already-finished workers and the
        # timeout is an overall budget rather than per-worker (N * timeout).
        loop = asyncio.get_event_loop()
        deadline = loop.time() + (config.duration * 3 + 120 if config.duration else 600)
        all_stats: list[WorkerStats] = []
        worker_durations: list[float] = []
        if progress_interval:
            aggregator = ProgressAggregator(expect_workers, progress_interval)
            master_exporter = _MasterExporter(config, aggregator, progress_interval, loop.time())
            await master_exporter.start()

        async def _collect_one(
            idx: int, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ):
            try:
                while True:
                    msg = await asyncio.wait_for(
                        _recv_msg(reader), timeout=max(0.0, deadline - loop.time())
                    )
                    if msg.get("type") != MSG_PROGRESS:
                        break
                    if aggregator is not None:
                        aggregator.record(idx, _deserialize_progress(msg, loop.time()))
                if msg.get("type") == "error":
                    # A worker refused the run outright (e.g. it lacks the
                    # HTTP/2 backend). Say why instead of reporting a run that
                    # quietly had fewer nodes than asked for.
                    logger.error("  Worker %s refused the run: %s", idx, msg.get("error"))
                elif msg.get("type") == "result":
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
        if master_exporter is not None:
            # aclose() is idempotent, and emitting the final cluster snapshot
            # here rather than on the happy path is what makes a run killed
            # mid-flight still leave its last interval behind.
            with contextlib.suppress(asyncio.CancelledError):
                await master_exporter.aclose(asyncio.get_event_loop().time())
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
        # The gate belongs to the merged cluster result, not to any one worker
        # (each of which saw only its own share of the load).
        baseline=config.baseline,
        save_baseline=config.save_baseline,
        fail_on=config.fail_on,
        strict_config=config.strict_config,
        compare_format=config.compare_format,
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

    # Baseline gate on the merged cluster result. As in single-node mode, an
    # absolute threshold breach (2) outranks a relative regression (3).
    baseline_code = run_baseline_gate(
        merged, actual_duration, report_config.connections, report_config
    )
    if baseline_code and exit_code != 2:
        exit_code = baseline_code

    return merged, exit_code


def _current_loop() -> "asyncio.AbstractEventLoop | None":
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


class _ProgressReporter:
    """Forwards a worker's interval snapshots to the master.

    Snapshots are queued and sent by one task so the stream has a single writer:
    interleaving frames from two coroutines would corrupt both messages. A queue
    that fills is drained of its oldest entry rather than blocking the run --
    the newest interval is the one worth having, and the drop is counted.

    :meth:`submit` is called from the exporter's executor thread, not from the
    event loop, so every queue touch is bounced back onto the loop with
    ``call_soon_threadsafe``. ``asyncio.Queue`` is not thread-safe: a
    ``put_nowait`` from another thread can enqueue without waking the getter,
    which leaves the sender parked forever on a queue that is not empty.
    """

    _QUEUE_SIZE = 8

    def __init__(self, writer: asyncio.StreamWriter) -> None:
        self._writer = writer
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=self._QUEUE_SIZE)
        self._task: "asyncio.Task | None" = None
        self._loop_ref: "asyncio.AbstractEventLoop | None" = None
        self._seq = 0
        self.sent = 0
        self.dropped = 0
        self.failed = 0

    async def start(self) -> None:
        self._loop_ref = asyncio.get_running_loop()
        self._task = asyncio.create_task(self._loop())

    def submit(self, snapshot: "Snapshot") -> None:
        """Called from the exporter's thread; must never block the run."""
        self._seq += 1
        payload = _serialize_snapshot(snapshot, self._seq)
        loop = self._loop_ref
        if loop is None or loop is _current_loop():
            self._enqueue(payload)
            return
        loop.call_soon_threadsafe(self._enqueue, payload)

    def _enqueue(self, payload: dict) -> None:
        """Always runs on the event loop that owns the queue."""
        try:
            self._queue.put_nowait(payload)
        except asyncio.QueueFull:
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
                self._queue.task_done()
            self.dropped += 1
            with contextlib.suppress(asyncio.QueueFull):
                self._queue.put_nowait(payload)

    async def _loop(self) -> None:
        while True:
            payload = await self._queue.get()
            try:
                await _send_msg(self._writer, payload)
                self.sent += 1
            except (ConnectionError, OSError) as e:
                # The master went away. The run continues -- it is the thing
                # being measured -- and the result send will report the failure.
                self.failed += 1
                logger.debug("Worker: could not send progress: %s", e)
            finally:
                self._queue.task_done()

    async def aclose(self) -> None:
        if self._task is None:
            return
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._queue.join(), timeout=5.0)
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None
        if self.dropped or self.failed:
            logger.warning(
                "Worker: progress reporting sent %d, dropped %d, failed %d",
                self.sent,
                self.dropped,
                self.failed,
            )


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

        # The master decides the protocol, but each worker needs the backend
        # installed locally. Fail here with the fix, rather than silently
        # contributing HTTP/1.1 load to what is reported as an HTTP/2 run.
        if config.http2 and not http2_available():
            logger.error(
                "Worker: the master requested --http2, but the HTTP/2 backend is not "
                "installed on this node. Install it with: %s",
                HTTP2_INSTALL_HINT,
            )
            with contextlib.suppress(OSError, ConnectionError):
                await _send_msg(
                    writer,
                    {
                        "type": "error",
                        "error": f"worker lacks the HTTP/2 backend ({HTTP2_INSTALL_HINT})",
                    },
                )
            return

        logger.info("Worker: starting benchmark...")

        # The master asks for progress by putting an interval in the config
        # message. Absent -- which is also what an old master looks like -- means
        # stay silent, so this never sends a message the peer cannot read.
        progress_interval = requested_progress_interval(msg)
        reporter: "_ProgressReporter | None" = None
        if progress_interval is not None:
            config.export_interval = float(progress_interval)
            reporter = _ProgressReporter(writer)
            await reporter.start()
            logger.info("Worker: reporting progress to master every %.1fs", progress_interval)

        # Measure real wall-clock so the master can report throughput against the
        # actual run window in -n (request-count) mode instead of a fixed guess.
        run_start = time.monotonic()
        # Run the appropriate benchmark
        try:
            if config.users is not None:
                stats, _ = await run_user_simulation(
                    config, on_snapshot=reporter.submit if reporter else None
                )
            else:
                stats, _ = await run_benchmark(
                    config, on_snapshot=reporter.submit if reporter else None
                )
        finally:
            # Stopped before the result is sent: two writers on one stream would
            # interleave frames and corrupt both messages.
            if reporter is not None:
                await reporter.aclose()
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
