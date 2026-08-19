"""Streaming metrics export while a run is in flight.

Exporting only at the end leaves the operator blind for exactly the runs where
observability matters most: a 30-minute soak, an autofind ramp, a traffic
profile. There is no live Grafana view to line up against server-side
dashboards, no way to correlate a latency spike with a deploy as it happens,
and a killed run exports nothing at all.

Two properties shape the design:

* **Percentiles are windowed.** A spike 25 minutes ago must not still be
  dragging the current p95 around. Counters stay cumulative and monotonic so
  ``rate()`` works; percentiles describe only the interval just ended.
* **A slow collector must not slow the run.** Sampling and sending are separate
  tasks joined by a bounded queue: if the sender is stuck on an unreachable
  endpoint the queue fills and snapshots are dropped, counted, and reported at
  the end — never blocking a request worker, never silently pretending it all
  went out.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import statistics
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Sequence

from pywrkr.config import WorkerStats

if TYPE_CHECKING:
    from pywrkr.config import ActiveUsers, BenchmarkConfig
    from pywrkr.traffic_profiles import RateLimiter

logger = logging.getLogger(__name__)

__all__ = [
    "MIN_EXPORT_INTERVAL",
    "Snapshot",
    "StreamingExporter",
    "window_percentiles",
]

#: Anything faster than this is more load on the collector than signal.
MIN_EXPORT_INTERVAL = 1.0

#: Snapshots held for a stuck sender before new ones are dropped. Small on
#: purpose: stale snapshots are worth less than a truthful drop count.
_QUEUE_SIZE = 8

#: Percentiles reported per window.
_WINDOW_PERCENTILES = (50, 95, 99)

#: Cap on raw samples carried with one snapshot. Enough for a stable p99 while
#: keeping a distributed interval's payload bounded regardless of throughput.
_MAX_WIRE_SAMPLES = 1000


def window_percentiles(samples: Sequence[float]) -> dict[str, float]:
    """Nearest-rank p50/p95/p99 over one window's samples.

    Deliberately independent of the run-cumulative percentile machinery: this
    describes the interval just ended, which is the whole point of streaming.
    """
    finite = sorted(x for x in samples if math.isfinite(x))
    if not finite:
        return {}
    n = len(finite)
    out: dict[str, float] = {}
    for pct in _WINDOW_PERCENTILES:
        index = min(int(math.ceil(pct / 100 * n)) - 1, n - 1)
        out[f"p{pct}"] = finite[max(index, 0)]
    return out


@dataclass(frozen=True)
class Snapshot:
    """One point-in-time view of a run, ready to export."""

    elapsed: float
    total_requests: int
    total_errors: int
    total_bytes: int
    window_seconds: float
    window_requests: int
    window_errors: int
    window_percentiles: dict[str, float] = field(default_factory=dict)
    window_latency: dict[str, float] = field(default_factory=dict)
    active_users: "int | None" = None
    target_rate: "float | None" = None
    final: bool = False
    #: The interval's raw latency samples, retained only when a sink asks for
    #: them. A distributed master needs them: percentiles cannot be merged
    #: across nodes by averaging, so it has to pool the samples and compute its
    #: own. Off by default -- a single-node run would pay for a copy it never
    #: reads.
    window_samples: tuple[float, ...] = ()

    @property
    def requests_per_sec(self) -> float:
        """Throughput over this window, not since the run started."""
        return self.window_requests / self.window_seconds if self.window_seconds > 0 else 0.0

    def to_results_dict(self) -> dict:
        """Shape the snapshot like ``build_results_dict`` output.

        The exporters already know how to walk that structure, so streaming
        reuses them instead of growing a second metric mapping that could drift.
        """
        results: dict = {
            "duration_sec": round(self.elapsed, 3),
            "total_requests": self.total_requests,
            "total_errors": self.total_errors,
            "total_bytes": self.total_bytes,
            "requests_per_sec": round(self.requests_per_sec, 2),
            "transfer_per_sec_bytes": 0.0,
            "status_codes": {},
            "error_types": {},
        }
        if self.window_latency:
            results["latency"] = dict(self.window_latency)
        if self.window_percentiles:
            results["percentiles"] = dict(self.window_percentiles)
        return results


class StreamingExporter:
    """Pushes periodic snapshots to the configured OTel/Prometheus endpoints.

    Start it with :meth:`start` once the workers are running and close it with
    :meth:`aclose`, which emits one final snapshot — so an aborted soak still
    leaves its last state in the TSDB.
    """

    def __init__(
        self,
        config: "BenchmarkConfig",
        all_stats: list[WorkerStats],
        interval: float,
        *,
        active_users: ActiveUsers | None = None,
        rate_limiter: "RateLimiter | None" = None,
        start_time: "float | None" = None,
        queue_size: int = _QUEUE_SIZE,
        sinks: "Sequence[Callable[[Snapshot], bool]] | None" = None,
        keep_samples: bool = False,
        max_samples: int = _MAX_WIRE_SAMPLES,
    ) -> None:
        self._config = config
        self._all_stats = all_stats
        self._interval = max(interval, MIN_EXPORT_INTERVAL)
        self._active_users = active_users
        self._rate_limiter = rate_limiter
        self._start = start_time if start_time is not None else time.monotonic()
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=queue_size)
        self._tasks: list[asyncio.Task] = []
        self._stop = asyncio.Event()
        # Where snapshots go. The default is the OTel/Prometheus push, present
        # only when an endpoint is actually configured; a distributed worker
        # adds one that forwards to the master, so the sampling, the bounded
        # queue and the drop accounting are shared rather than reimplemented
        # alongside it.
        # A flag rather than `self._push` in the list: binding the method here
        # would freeze it, so patching `_push` (which tests and subclasses do)
        # would silently stop taking effect.
        self._push_to_endpoints = bool(config.otel_endpoint or config.prom_remote_write)
        self._sinks: list[Callable[[Snapshot], bool]] = list(sinks) if sinks else []
        if sinks is not None:
            self._push_to_endpoints = False
        self._keep_samples = keep_samples
        self._max_samples = max_samples

        self._last_mark = self._start
        self._last_requests = 0
        self._last_errors = 0

        self.queued = 0
        self.dropped = 0
        self.sent = 0
        self.failed = 0

    # -- lifecycle ---------------------------------------------------------

    def enable_window_capture(self, stats: WorkerStats) -> None:
        """Turn on per-window latency capture for one worker's stats.

        Off by default: the capture costs an append per request, which a run
        without streaming export should not pay.
        """
        if stats.window_latencies is None:
            stats.window_latencies = []

    async def start(self) -> None:
        """Spawn the sampler and sender tasks."""
        for stats in self._all_stats:
            self.enable_window_capture(stats)
        self._tasks = [
            asyncio.create_task(self._sample_loop()),
            asyncio.create_task(self._send_loop()),
        ]

    async def aclose(self) -> None:
        """Emit a final snapshot, drain the queue, and stop."""
        self._stop.set()
        try:
            self._queue.put_nowait(self._build_snapshot(final=True))
            self.queued += 1
        except asyncio.QueueFull:
            self.dropped += 1
        # Give the sender a bounded chance to flush what is queued.
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._queue.join(), timeout=self._interval * 2 + 5)
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                _ = await task
        self._tasks = []

    # -- sampling ----------------------------------------------------------

    def _drain_window(self) -> list[float]:
        """Take and clear every worker's window samples."""
        samples: list[float] = []
        for stats in self._all_stats:
            buffer = stats.window_latencies
            if buffer is None:
                # A user that ramped up after the exporter started.
                self.enable_window_capture(stats)
                continue
            if buffer:
                samples.extend(buffer)
                buffer.clear()
        return samples

    def _build_snapshot(self, final: bool = False) -> Snapshot:
        now = time.monotonic()
        total_requests = sum(s.total_requests for s in self._all_stats)
        total_errors = sum(s.errors for s in self._all_stats)
        total_bytes = sum(s.total_bytes for s in self._all_stats)
        samples = self._drain_window()

        window_seconds = max(now - self._last_mark, 1e-9)
        snapshot = Snapshot(
            elapsed=now - self._start,
            total_requests=total_requests,
            total_errors=total_errors,
            total_bytes=total_bytes,
            window_seconds=window_seconds,
            window_requests=max(total_requests - self._last_requests, 0),
            window_errors=max(total_errors - self._last_errors, 0),
            window_percentiles=window_percentiles(samples),
            window_latency=_window_latency(samples),
            window_samples=self._wire_samples(samples),
            active_users=self._active_users.count if self._active_users is not None else None,
            target_rate=_current_target_rate(self._rate_limiter),
            final=final,
        )
        self._last_mark = now
        self._last_requests = total_requests
        self._last_errors = total_errors
        return snapshot

    async def _sample_loop(self) -> None:
        while not self._stop.is_set():
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
                return
            try:
                self._queue.put_nowait(self._build_snapshot())
                self.queued += 1
            except asyncio.QueueFull:
                # The sender is behind, most likely on an unreachable
                # collector. Dropping is the right call -- blocking here would
                # push back onto the run itself -- but it is counted and
                # reported rather than swallowed.
                self.dropped += 1
                logger.debug("Export queue full; dropped a snapshot (%d so far)", self.dropped)

    # -- sending -----------------------------------------------------------

    def add_sink(self, sink: "Callable[[Snapshot], bool]") -> None:
        """Send snapshots here as well as anywhere already configured.

        The sink is called **from an executor thread**, not from the event loop:
        the built-in one does blocking HTTP, which is why the send path runs
        there at all. A sink that touches loop-owned state must bounce it back
        with ``call_soon_threadsafe`` -- an ``asyncio.Queue.put_nowait`` from
        another thread can enqueue without waking the getter.
        """
        self._sinks.append(sink)

    def _wire_samples(self, samples: list[float]) -> tuple[float, ...]:
        """Samples to carry with the snapshot, capped so one interval cannot
        put an unbounded payload on the wire."""
        if not self._keep_samples or not samples:
            return ()
        if len(samples) <= self._max_samples:
            return tuple(samples)
        # Even stride rather than the first N: the first N would be whatever the
        # earliest workers happened to record, which is not the interval.
        step = len(samples) / self._max_samples
        return tuple(samples[int(i * step)] for i in range(self._max_samples))

    async def _send_loop(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            snapshot = await self._queue.get()
            try:
                ok = await loop.run_in_executor(None, self._deliver, snapshot)
                if ok:
                    self.sent += 1
                else:
                    self.failed += 1
            except Exception:  # pragma: no cover - defensive
                self.failed += 1
                logger.debug("Streaming export raised", exc_info=True)
            finally:
                self._queue.task_done()

    def _deliver(self, snapshot: Snapshot) -> bool:
        """Hand the snapshot to every sink. One failing does not skip the rest."""
        ok = True
        if self._push_to_endpoints:
            ok = self._push(snapshot) and ok
        for sink in self._sinks:
            try:
                ok = sink(snapshot) and ok
            except Exception:  # pragma: no cover - a sink must not kill the run
                logger.debug("Streaming sink raised", exc_info=True)
                ok = False
        return ok

    def _push(self, snapshot: Snapshot) -> bool:
        """Blocking push, always called in an executor thread."""
        from pywrkr.reporting import export_to_otel, export_to_prometheus

        results = snapshot.to_results_dict()
        tags = dict(self._config.tags)
        tags["export"] = "final" if snapshot.final else "interval"
        ok = True
        if self._config.otel_endpoint:
            ok = export_to_otel(results, self._config.otel_endpoint, tags) and ok
        if self._config.prom_remote_write:
            ok = export_to_prometheus(results, self._config.prom_remote_write, tags) and ok
        return ok

    # -- reporting ---------------------------------------------------------

    @property
    def undelivered(self) -> int:
        """Snapshots queued that never reached the collector either way.

        Counted separately from failures: a push still hanging on an
        unreachable endpoint when the run ends has neither succeeded nor
        reported an error, and reporting nothing at all would be the silent
        success this whole path is supposed to avoid.
        """
        return max(self.queued - self.sent - self.failed, 0)

    @property
    def all_delivered(self) -> bool:
        return not (self.failed or self.dropped or self.undelivered)

    def summary(self) -> "str | None":
        """One line describing what happened, or None when nothing was streamed."""
        if not (self.queued or self.dropped):
            return None
        parts = [f"{self.sent} snapshot(s) exported"]
        if self.failed:
            parts.append(f"{self.failed} failed")
        if self.undelivered:
            parts.append(f"{self.undelivered} never delivered (collector unresponsive)")
        if self.dropped:
            parts.append(f"{self.dropped} dropped (collector too slow)")
        return "Streaming export: " + ", ".join(parts)


def _window_latency(samples: Sequence[float]) -> dict[str, float]:
    """Min/max/mean/median over one window, mirroring the results schema."""
    finite = [x for x in samples if math.isfinite(x)]
    if not finite:
        return {}
    return {
        "min": min(finite),
        "max": max(finite),
        "mean": statistics.mean(finite),
        "median": statistics.median(finite),
        "stdev": statistics.stdev(finite) if len(finite) > 1 else 0.0,
    }


def _current_target_rate(rate_limiter: "RateLimiter | None") -> "float | None":
    """The rate the limiter is aiming for right now.

    Under a ramp or a traffic profile this moves during the run, which is
    exactly what makes it worth graphing next to the achieved rate: the gap
    between the two is where the target stops being met.
    """
    if rate_limiter is None:
        return None
    live = getattr(rate_limiter, "_current_rate", None)
    if callable(live):
        try:
            return float(live(time.monotonic()))
        except Exception:  # pragma: no cover - falls back to the base rate
            logger.debug("Could not read the live target rate", exc_info=True)
    base = getattr(rate_limiter, "start_rate", None)
    return float(base) if isinstance(base, (int, float)) else None
