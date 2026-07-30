"""WebSocket benchmarking: ``ws://`` and ``wss://`` targets.

A request/response benchmarker cannot see what matters about a WebSocket
service. The interesting load characteristics are a connection storm (a
thousand sockets opening at once), the number of sockets a server can hold
open, and how long a message takes to come back on a socket that is already
established — none of which is a request rate.

So the shape of the run is different: sockets are long-lived, opened once and
held for the duration, and the work happens *on* them. What is deliberately not
different is everything downstream. WebSocket runs produce an ordinary
:class:`~pywrkr.config.WorkerStats`, so percentiles, ``--threshold``, the exit
code contract, ``--json``, the HTML report, ``pywrkr compare`` and the
OTel/Prometheus exporters all work without knowing this module exists.

Two mappings make that work, and both are stated in the results file rather
than left for the reader to infer (see ``websocket.latency_metric`` and
``websocket.primary_metric``):

* **Latency** is the message round-trip time when ``--ws-expect-reply`` is set,
  and the handshake time otherwise. That is the number a ``p95 < 100ms``
  threshold is about in each case.
* **A "request"** is a message sent when there are messages to send, and a
  completed handshake otherwise — so ``requests_per_sec`` is message
  throughput for a messaging run and connection throughput for a
  connection-storm run.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import ssl
import sys
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Sequence
from urllib.parse import urlparse

import aiohttp

from pywrkr.config import (
    BenchmarkConfig,
    WorkerStats,
    WsStats,
    merge_stats,
    merge_ws_stats,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pywrkr.config import WebSocketConfig

logger = logging.getLogger(__name__)

__all__ = [
    "WS_SCHEMES",
    "WsStats",
    "connection_start_times",
    "is_websocket_url",
    "merge_ws_stats",
    "WsStepOutcome",
    "execute_ws_step",
    "run_websocket_benchmark",
    "ws_results_section",
]

#: URL schemes that select WebSocket mode.
WS_SCHEMES = ("ws", "wss")

#: Handshake status recorded on a successful upgrade, so ``status_codes`` in the
#: results says something true rather than being empty.
_SWITCHING_PROTOCOLS = 101

#: How long to wait for the peer's close frame before dropping the transport.
DEFAULT_CLOSE_TIMEOUT = 5.0


def is_websocket_url(url: str) -> bool:
    """True when *url* should be benchmarked as a WebSocket."""
    return urlparse(url).scheme in WS_SCHEMES


def _latency_summary(samples: "Sequence[float]") -> dict:
    """Min/mean/max plus percentiles for one WebSocket latency family."""
    import math
    import statistics

    from pywrkr.reporting import compute_percentiles

    finite = [x for x in samples if math.isfinite(x)]
    if not finite:
        return {}
    pct_pairs = compute_percentiles(finite)
    return {
        "count": len(finite),
        "min": round(min(finite), 6),
        "max": round(max(finite), 6),
        "mean": round(statistics.mean(finite), 6),
        "percentiles": {f"p{p}": round(v, 6) for p, v in pct_pairs},
    }


def ws_results_section(ws: WsStats, duration: float) -> dict:
    """The ``websocket`` block of the results file.

    Documented schema (all keys always present so a CI consumer can rely on
    them; ``handshake``/``rtt`` are ``{}`` when nothing was measured)::

        websocket:
          latency_metric: "rtt" | "handshake"   # which metric percentiles/ describe
          primary_metric: "messages" | "connections"  # what total_requests counts
          connections: {opened, failed, dropped, reconnects, peak_concurrent}
          messages: {sent, received, sent_per_sec, received_per_sec,
                     bytes_sent, bytes_received, reply_timeouts,
                     unexpected_replies}
          handshake: {count, min, max, mean, percentiles: {...}}
          rtt:       {count, min, max, mean, percentiles: {...}}
          close: {frames_sent, unacknowledged, codes: {"1000": n, ...}}
    """
    per_sec = (lambda n: round(n / duration, 2)) if duration > 0 else (lambda n: 0.0)
    return {
        "latency_metric": ws.latency_metric,
        "primary_metric": ws.primary_metric,
        "connections": {
            "opened": ws.connections_opened,
            "failed": ws.connections_failed,
            "dropped": ws.connections_dropped,
            "reconnects": ws.reconnects,
            "peak_concurrent": ws.peak_concurrent,
        },
        "messages": {
            "sent": ws.messages_sent,
            "received": ws.messages_received,
            "sent_per_sec": per_sec(ws.messages_sent),
            "received_per_sec": per_sec(ws.messages_received),
            "bytes_sent": ws.bytes_sent,
            "bytes_received": ws.bytes_received,
            "reply_timeouts": ws.reply_timeouts,
            "unexpected_replies": ws.unexpected_replies,
        },
        "handshake": _latency_summary(ws.handshake_latencies),
        "rtt": _latency_summary(ws.rtt_latencies),
        "close": {
            "frames_sent": ws.close_frames_sent,
            "unacknowledged": ws.close_unacked,
            "codes": dict(ws.close_codes),
        },
    }


def connection_start_times(count: int, ramp_up: float) -> list[float]:
    """Offsets, in seconds from start, at which each socket should connect.

    With ``--ramp-up`` this is what turns "open 500 sockets" into a connection
    storm of a stated shape rather than an instantaneous thundering herd. The
    last socket starts at exactly *ramp_up*, so the ramp takes the time asked
    for instead of finishing one slot early.
    """
    if count <= 0:
        return []
    if ramp_up <= 0 or count == 1:
        return [0.0] * count
    step = ramp_up / (count - 1)
    return [i * step for i in range(count)]


class _Fleet:
    """Shared state across the sockets of one run."""

    def __init__(self) -> None:
        self.active = 0
        self.peak = 0

    def opened(self) -> None:
        self.active += 1
        self.peak = max(self.peak, self.active)

    def closed(self) -> None:
        self.active = max(self.active - 1, 0)


def _build_ssl_context(config: BenchmarkConfig) -> "ssl.SSLContext | None":
    """TLS settings for ``wss://``, honouring --ssl-verify and --ca-bundle.

    Shares :func:`pywrkr.backends.ssl_context_from` with the HTTPS path so a
    ``wss://`` run trusts and verifies exactly what an ``https://`` run does.
    """
    from pywrkr.backends import ssl_context_from

    return ssl_context_from(config.ssl_config)


async def _drain_reply(
    ws: "aiohttp.ClientWebSocketResponse[Any]",
    stats: WorkerStats,
    ws_stats: WsStats,
    timeout: float,
) -> "float | None":
    """Wait for one message, returning the seconds it took, or None on failure."""
    started = time.monotonic()
    try:
        msg = await asyncio.wait_for(ws.receive(), timeout=timeout)
    except asyncio.TimeoutError:
        ws_stats.reply_timeouts += 1
        stats.errors += 1
        stats.error_types["ws_reply_timeout"] += 1
        return None
    elapsed = time.monotonic() - started
    if not _account_message(msg, stats, ws_stats):
        return None
    return elapsed


def _account_message(
    msg: aiohttp.WSMessage,
    stats: WorkerStats,
    ws_stats: WsStats,
    expected_close: bool = False,
) -> bool:
    """Count one received frame. Returns False when the socket has ended.

    *expected_close* is set once this side has decided to shut down. Without
    it the CLOSE frame that our own ``close()`` produces would be counted as a
    dropped connection and an error -- every clean run would end by reporting
    one failure per socket.
    """
    if msg.type in (aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY):
        payload = msg.data
        size = len(payload.encode() if isinstance(payload, str) else payload)
        ws_stats.messages_received += 1
        ws_stats.bytes_received += size
        stats.total_bytes += size
        return True
    if msg.type is aiohttp.WSMsgType.ERROR:
        stats.errors += 1
        stats.error_types["ws_transport_error"] += 1
        return False
    # CLOSE / CLOSED / CLOSING.
    if expected_close:
        return False
    # The peer went away on its own, which is a dropped connection.
    ws_stats.connections_dropped += 1
    ws_stats.record_close(getattr(msg, "data", None))
    stats.errors += 1
    stats.error_types["ws_closed_by_peer"] += 1
    return False


async def _close_socket(
    ws: "aiohttp.ClientWebSocketResponse[Any]", ws_stats: WsStats, timeout: float
) -> None:
    """Close with a close frame, not by dropping the transport.

    A benchmark that walks away from a thousand sockets leaves the server
    holding a thousand half-open connections, which poisons whatever is
    measured next against the same host.
    """
    if ws.closed:
        return
    # close() writes the frame before it waits for the answer, so the frame is
    # sent whether or not the peer ever acknowledges it. Counting it here and
    # the missing acknowledgement separately is the difference between "we
    # leaked sockets" and "that server never answers a close".
    ws_stats.close_frames_sent += 1
    try:
        await asyncio.wait_for(ws.close(), timeout=timeout)
        ws_stats.record_close(ws.close_code)
    except (asyncio.TimeoutError, aiohttp.ClientError, OSError) as exc:
        ws_stats.close_unacked += 1
        logger.debug("Close handshake did not complete: %s", exc)


def _record_handshake_failure(exc: BaseException, stats: WorkerStats, ws_stats: WsStats) -> None:
    ws_stats.connections_failed += 1
    stats.errors += 1
    if isinstance(exc, aiohttp.WSServerHandshakeError):
        # An auth failure or a wrong path shows up as the HTTP status the
        # server refused the upgrade with, which is what makes it diagnosable.
        stats.status_codes[exc.status] += 1
        stats.error_types[f"ws_handshake_{exc.status}"] += 1
    elif isinstance(exc, asyncio.TimeoutError):
        stats.error_types["ws_handshake_timeout"] += 1
    else:
        stats.error_types[f"ws_{type(exc).__name__}"] += 1


async def _socket_worker(
    session: aiohttp.ClientSession,
    url: str,
    config: BenchmarkConfig,
    ws_config: "WebSocketConfig",
    stats: WorkerStats,
    ws_stats: WsStats,
    fleet: _Fleet,
    stop: asyncio.Event,
    start_delay: float,
    ssl_ctx: "ssl.SSLContext | bool | None",
) -> None:
    """Hold one socket open for the run, sending and counting messages."""
    ws_stats.latency_metric = "rtt" if ws_config.expect_reply else "handshake"
    ws_stats.primary_metric = "messages" if ws_config.messages else "connections"

    if start_delay > 0:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=start_delay)
        if stop.is_set():
            return

    attempts = 0
    while not stop.is_set():
        attempts += 1
        if attempts > 1:
            ws_stats.reconnects += 1
        connected = await _run_one_connection(
            session, url, config, ws_config, stats, ws_stats, fleet, stop, ssl_ctx
        )
        if stop.is_set() or not ws_config.reconnect:
            return
        if not connected:
            # A target that refuses the upgrade will refuse it again in a
            # microsecond-tight loop; back off rather than spinning.
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=ws_config.reconnect_delay)


async def _run_one_connection(
    session: aiohttp.ClientSession,
    url: str,
    config: BenchmarkConfig,
    ws_config: "WebSocketConfig",
    stats: WorkerStats,
    ws_stats: WsStats,
    fleet: _Fleet,
    stop: asyncio.Event,
    ssl_ctx: "ssl.SSLContext | bool | None",
) -> bool:
    """One connect/use/close cycle. Returns True if the handshake succeeded."""
    kwargs: dict[str, Any] = {
        "headers": dict(config.headers) or None,
        "timeout": aiohttp.ClientWSTimeout(ws_close=ws_config.close_timeout),
        "autoping": True,
        "max_msg_size": ws_config.max_message_size,
    }
    if ws_config.subprotocols:
        kwargs["protocols"] = tuple(ws_config.subprotocols)
    if ws_config.ping_interval:
        kwargs["heartbeat"] = ws_config.ping_interval
    if ssl_ctx is not None:
        kwargs["ssl"] = ssl_ctx

    started = time.monotonic()
    try:
        ws = await asyncio.wait_for(session.ws_connect(url, **kwargs), timeout=config.timeout_sec)
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError) as exc:
        _record_handshake_failure(exc, stats, ws_stats)
        return False

    handshake = time.monotonic() - started
    ws_stats.connections_opened += 1
    ws_stats.handshake_latencies.append(handshake)
    stats.status_codes[_SWITCHING_PROTOCOLS] += 1
    fleet.opened()
    if ws_stats.primary_metric == "connections":
        stats.total_requests += 1
    if ws_stats.latency_metric == "handshake":
        _record_latency(stats, handshake)

    # A socket that sends and waits for each reply reads inline; anything else
    # gets a dedicated reader so incoming frames are counted (and the socket
    # drained) even when nothing is being sent. See _receive_loop for why that
    # reader is never cancelled.
    reader: "asyncio.Task | None" = None
    shutdown = asyncio.Event()
    try:
        if ws_config.expect_reply:
            await _send_loop(ws, ws_config, stats, ws_stats, stop)
        else:
            reader = asyncio.create_task(_receive_loop(ws, stats, ws_stats, shutdown))
            if ws_config.messages:
                await _send_loop(ws, ws_config, stats, ws_stats, stop)
            else:
                # Wait for the run to end *or* the socket to die. Waiting only
                # on the stop event would leave a slot parked and idle for the
                # rest of the run after the peer hung up, and --ws-reconnect
                # would never fire.
                await _first_of(stop.wait(), reader)
    finally:
        shutdown.set()
        await _close_socket(ws, ws_stats, ws_config.close_timeout)
        await _drain_reader(reader, ws_config.close_timeout)
        fleet.closed()
    return True


def _record_latency(stats: WorkerStats, value: float) -> None:
    stats.latencies.append(value)
    if stats.window_latencies is not None:
        stats.window_latencies.append(value)


async def _first_of(waiter, task: "asyncio.Task") -> None:
    """Return when either the awaitable or the already-running task finishes."""
    pending_waiter = asyncio.ensure_future(waiter)
    try:
        await asyncio.wait({pending_waiter, task}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        if not pending_waiter.done():
            pending_waiter.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pending_waiter


async def _drain_reader(reader: "asyncio.Task | None", timeout: float) -> None:
    """Await a reader task that a socket close should have already ended."""
    if reader is None:
        return
    try:
        await asyncio.wait_for(reader, timeout=timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        reader.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reader


async def _send_loop(
    ws: "aiohttp.ClientWebSocketResponse[Any]",
    ws_config: "WebSocketConfig",
    stats: WorkerStats,
    ws_stats: WsStats,
    stop: asyncio.Event,
) -> None:
    """Send payloads on a schedule, optionally timing each reply."""
    index = 0
    while not stop.is_set():
        payload = ws_config.messages[index % len(ws_config.messages)]
        index += 1
        try:
            await ws.send_str(payload)
        except (aiohttp.ClientError, ConnectionResetError, RuntimeError) as exc:
            ws_stats.connections_dropped += 1
            stats.errors += 1
            stats.error_types[f"ws_send_{type(exc).__name__}"] += 1
            return
        ws_stats.messages_sent += 1
        ws_stats.bytes_sent += len(payload.encode())
        stats.total_requests += 1

        if ws_config.expect_reply:
            rtt = await _drain_reply(ws, stats, ws_stats, ws_config.reply_timeout)
            if rtt is None:
                if ws.closed:
                    return
            else:
                ws_stats.rtt_latencies.append(rtt)
                _record_latency(stats, rtt)

        if ws_config.message_interval <= 0:
            # Flat-out send loop: yield so the receive path and the stop event
            # get a turn instead of starving inside a tight coroutine.
            await asyncio.sleep(0)
            continue
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=ws_config.message_interval)


async def _receive_loop(
    ws: "aiohttp.ClientWebSocketResponse[Any]",
    stats: WorkerStats,
    ws_stats: WsStats,
    shutdown: "asyncio.Event | None" = None,
) -> None:
    """Read until the socket ends, counting everything that arrives.

    Deliberately unbounded: it is stopped by *closing* the socket, never by
    cancelling or timing out the read. aiohttp stamps ``ABNORMAL_CLOSURE``
    (1006) on any ``receive()`` that is cancelled or times out, so a polling
    loop would report every clean shutdown as an abnormal one -- and, worse,
    can lose a partially-read frame. ``close()`` wakes a pending ``receive()``
    through aiohttp's own close-wait, which is the supported way out.
    """
    while True:
        msg = await ws.receive()
        expected = shutdown is not None and shutdown.is_set()
        if not _account_message(msg, stats, ws_stats, expected_close=expected):
            return


async def run_websocket_benchmark(
    config: BenchmarkConfig,
    *,
    install_signal_handlers: bool = True,
    quiet: "bool | None" = None,
) -> tuple[WorkerStats, int]:
    """Run a WebSocket benchmark and return merged stats with an exit code.

    Mirrors :func:`pywrkr.workers.run_benchmark`: same return contract, same
    threshold/baseline/export gates, same exit codes.
    """
    from pywrkr.reporting import (
        evaluate_thresholds,
        print_results,
        print_threshold_results,
        run_baseline_gate,
        run_observability_exports,
    )
    from pywrkr.workers import _setup_signal_handlers

    ws_config = config.websocket
    if ws_config is None:  # pragma: no cover - the CLI always supplies one
        from pywrkr.config import WebSocketConfig

        ws_config = WebSocketConfig()
    quiet = config._quiet if quiet is None else quiet

    stop = asyncio.Event()
    if install_signal_handlers:
        _setup_signal_handlers(stop)

    count = max(config.connections, 1)
    all_stats = [WorkerStats() for _ in range(count)]
    all_ws_stats = [WsStats() for _ in range(count)]
    fleet = _Fleet()
    delays = connection_start_times(count, config.ramp_up)
    ssl_ctx = _build_ssl_context(config) if urlparse(config.url).scheme == "wss" else None

    connector = aiohttp.TCPConnector(limit=0, ssl=ssl_ctx if ssl_ctx is not None else True)
    session = aiohttp.ClientSession(connector=connector)
    stop_stamp: list[float] = []
    start_time = time.monotonic()
    try:
        tasks = [
            asyncio.create_task(
                _socket_worker(
                    session,
                    config.url,
                    config,
                    ws_config,
                    all_stats[i],
                    all_ws_stats[i],
                    fleet,
                    stop,
                    delays[i],
                    ssl_ctx,
                )
            )
            for i in range(count)
        ]
        timer = asyncio.create_task(_run_timer(config, stop))
        stamper = asyncio.create_task(_stamp_when_stopped(stop, stop_stamp))
        results = await asyncio.gather(*tasks, return_exceptions=True)
        end_time = time.monotonic()
        stop.set()
        for task in (timer, stamper):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    finally:
        stop.set()
        await session.close()

    worker_crashed = False
    for i, result in enumerate(results):
        if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
            worker_crashed = True
            logger.error("Socket %d crashed: %s: %s", i, type(result).__name__, result)

    # Load ended when the stop event fired; anything after that is teardown.
    # Falls back to the gather time for a run that ended because every socket
    # finished on its own (a target that refuses every handshake, say).
    duration = (stop_stamp[0] if stop_stamp else end_time) - start_time
    merged = merge_stats(all_stats)
    merged_ws = merge_ws_stats(all_ws_stats)
    merged_ws.peak_concurrent = fleet.peak
    merged.ws = merged_ws

    if not quiet:
        print_results(merged, duration, count, start_time, config)

    exit_code = 0
    if config.thresholds:
        th_results = evaluate_thresholds(config.thresholds, merged, duration)
        if not quiet:
            print_threshold_results(th_results, file=sys.stdout)
        if any(not passed for _, _, passed in th_results):
            exit_code = 2

    baseline_code = run_baseline_gate(merged, duration, count, config, None)
    if baseline_code and exit_code != 2:
        exit_code = baseline_code

    if worker_crashed:
        exit_code = max(exit_code, 1)

    # Every socket failing its handshake is a failed run, not a clean one that
    # happens to report zeroes.
    if merged_ws.connections_opened == 0 and merged_ws.connections_failed > 0:
        exit_code = max(exit_code, 1)

    if not run_observability_exports(merged, duration, count, config, None):
        exit_code = max(exit_code, 1)

    return merged, exit_code


async def _run_timer(config: BenchmarkConfig, stop: asyncio.Event) -> None:
    """Stop the run once the requested duration has elapsed."""
    duration = config.duration
    if duration is None or duration <= 0:
        return
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=duration)
    stop.set()


async def _stamp_when_stopped(stop: asyncio.Event, stamp: list[float]) -> None:
    """Record when load stopped, which is not when the run finished.

    Closing a socket properly means waiting for the peer's close frame, and a
    server that is busy pushing may take seconds to send one. That teardown is
    not load: counting it would stretch the reported duration and deflate every
    rate derived from it.
    """
    await stop.wait()
    stamp.append(time.monotonic())


# ---------------------------------------------------------------------------
# Scenario `ws:` steps
# ---------------------------------------------------------------------------


@dataclass
class WsStepOutcome:
    """What one scenario ``ws:`` step did."""

    ok: bool
    latency: float = 0.0
    error: "str | None" = None
    #: Text of the message that satisfied ``expect_message_contains``, so the
    #: step's ``extract`` rules have something to run against.
    body: bytes = b""


async def execute_ws_step(
    session: "aiohttp.ClientSession",
    url: str,
    headers: dict[str, str],
    *,
    send: "str | None",
    expect_contains: "str | None",
    hold: float,
    timeout: float,
    stats: WorkerStats,
    ws_stats: WsStats,
    stop: "asyncio.Event | None" = None,
) -> WsStepOutcome:
    """Run one WebSocket step of a scenario and report what happened.

    The step's latency is the whole thing -- handshake, send, and the wait for
    the expected reply -- because that is what the user of a "subscribe and get
    the first update" flow actually waits for. ``hold`` afterwards is passive
    listening and is deliberately not counted in it.
    """
    started = time.monotonic()
    reader: "asyncio.Task | None" = None
    shutdown = asyncio.Event()
    ws_kwargs: dict[str, Any] = {"headers": headers or None, "autoping": True}
    try:
        ws = await asyncio.wait_for(session.ws_connect(url, **ws_kwargs), timeout=timeout)
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError) as exc:
        _record_handshake_failure(exc, stats, ws_stats)
        return WsStepOutcome(ok=False, error=_error_name(exc))

    handshake = time.monotonic() - started
    ws_stats.connections_opened += 1
    ws_stats.handshake_latencies.append(handshake)
    stats.status_codes[_SWITCHING_PROTOCOLS] += 1

    body = b""
    try:
        if send is not None:
            await ws.send_str(send)
            ws_stats.messages_sent += 1
            ws_stats.bytes_sent += len(send.encode())

        if expect_contains is not None:
            matched, body = await _await_matching(ws, expect_contains, timeout, stats, ws_stats)
            if not matched:
                return WsStepOutcome(
                    ok=False,
                    latency=time.monotonic() - started,
                    error=f"WsExpect: no message containing {expect_contains!r}",
                )

        latency = time.monotonic() - started
        if hold > 0:
            reader = await _hold_open(ws, hold, stats, ws_stats, stop, shutdown)
        return WsStepOutcome(ok=True, latency=latency, body=body)
    except (aiohttp.ClientError, ConnectionResetError, RuntimeError) as exc:
        ws_stats.connections_dropped += 1
        return WsStepOutcome(ok=False, latency=time.monotonic() - started, error=_error_name(exc))
    finally:
        shutdown.set()
        await _close_socket(ws, ws_stats, DEFAULT_CLOSE_TIMEOUT)
        await _drain_reader(reader, DEFAULT_CLOSE_TIMEOUT)


def _error_name(exc: BaseException) -> str:
    if isinstance(exc, aiohttp.WSServerHandshakeError):
        return f"WSHandshake {exc.status}"
    if isinstance(exc, asyncio.TimeoutError):
        return "WSTimeout"
    return type(exc).__name__


async def _await_matching(
    ws: "aiohttp.ClientWebSocketResponse[Any]",
    needle: str,
    timeout: float,
    stats: WorkerStats,
    ws_stats: WsStats,
) -> tuple[bool, bytes]:
    """Read until a message contains *needle*, or the budget runs out.

    Scans every arriving message rather than only the first: a subscription
    confirmation routinely arrives behind a welcome frame or a heartbeat, and
    a step that only ever looked at message one would fail on a correct server.
    """
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            ws_stats.reply_timeouts += 1
            return False, b""
        try:
            msg = await asyncio.wait_for(ws.receive(), timeout=remaining)
        except asyncio.TimeoutError:
            ws_stats.reply_timeouts += 1
            return False, b""
        if not _account_message(msg, stats, ws_stats):
            return False, b""
        text = msg.data if isinstance(msg.data, str) else msg.data.decode("utf-8", "replace")
        if needle in text:
            return True, text.encode()
        ws_stats.unexpected_replies += 1


async def _hold_open(
    ws: "aiohttp.ClientWebSocketResponse[Any]",
    hold: float,
    stats: WorkerStats,
    ws_stats: WsStats,
    stop: "asyncio.Event | None",
    shutdown: asyncio.Event,
) -> "asyncio.Task":
    """Keep the socket open for *hold* seconds, counting what arrives.

    Returns the reader task; the caller closes the socket, which ends it. As in
    :func:`_receive_loop`, the read is never cancelled or timed out, because
    aiohttp would then record the clean close as an abnormal one.
    """
    reader = asyncio.create_task(_receive_loop(ws, stats, ws_stats, shutdown))
    if stop is None:
        await asyncio.sleep(hold)
        return reader
    # Ends early when the run is stopping, so SIGINT during a long `hold`
    # does not leave the socket parked for the rest of it.
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=hold)
    return reader
