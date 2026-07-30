"""Pluggable HTTP client backends.

pywrkr was HTTP/1.1-only because aiohttp does not speak HTTP/2, yet most
production edges (CDNs, ALBs, nginx, Envoy) serve HTTP/2 to clients — and their
behaviour under load is qualitatively different, because h2 multiplexes streams
over one connection instead of holding a connection per in-flight request.

Two implementations sit behind one interface:

* :class:`AiohttpBackend` — the default. Same behaviour and dependency set as
  before, including the trace hooks that produce the DNS/TCP/TLS phase breakdown.
* :class:`HttpxBackend` — used for ``--http2``, from the optional
  ``pywrkr[http2]`` extra.

The split is two levels deep on purpose, mirroring what a load test needs: a
:class:`Backend` per run owning the shared connection pool, and a
:class:`BackendSession` per virtual user owning that user's cookies. An eventual
HTTP/3 backend slots in here without touching the worker loop.
"""

from __future__ import annotations

import asyncio
import ssl
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Mapping
from urllib.parse import urlparse

import aiohttp

from pywrkr.config import LatencyBreakdown, WorkerStats

if TYPE_CHECKING:
    from pywrkr.config import BenchmarkConfig, SSLConfig

#: Backend identifiers accepted internally.
BACKEND_AIOHTTP = "aiohttp"
BACKEND_HTTPX = "httpx"

#: Shown whenever --http2 is requested without the extra installed.
HTTP2_INSTALL_HINT = "pip install 'pywrkr[http2]'"

#: Normalised protocol labels.
HTTP_1_1 = "1.1"
HTTP_2 = "2"
HTTP_UNKNOWN = "unknown"

#: Every phase the aiohttp trace hooks can measure.
ALL_PHASES = ("dns", "connect", "tls", "ttfb", "transfer", "total")

#: What the httpx backend can measure: it has no hooks for the connection
#: phases, so those are reported as unavailable rather than as zero.
HTTPX_PHASES = ("ttfb", "transfer", "total")


class BackendUnavailableError(RuntimeError):
    """Raised when a requested backend's dependencies are not installed."""


@dataclass(slots=True)
class BackendResponse:
    """One response, reduced to what the worker loop and stats actually use.

    Slotted because one of these is allocated per request on the hot path.
    """

    status: int
    body: bytes
    headers: Mapping[str, str]
    http_version: str


# ---------------------------------------------------------------------------
# Interface
# ---------------------------------------------------------------------------


class BackendSession(ABC):
    """One virtual user's client: its own cookies, a shared connection pool."""

    @abstractmethod
    async def __aenter__(self) -> "BackendSession": ...

    @abstractmethod
    async def __aexit__(self, *exc_info) -> None: ...

    @abstractmethod
    async def send(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: "bytes | None",
        timeout_sec: float,
        trace_ctx: "dict | None" = None,
    ) -> BackendResponse:
        """Send one request and return the full response.

        *trace_ctx* being non-None means the caller wants a latency breakdown;
        a backend that cannot produce one ignores it.
        """

    @abstractmethod
    def clear_cookies(self) -> None:
        """Drop every stored cookie (scenario ``session: fresh_per_iteration``)."""

    def raw_websocket_session(self) -> "aiohttp.ClientSession | None":
        """The client a scenario ``ws:`` step can open a socket on, or None.

        Reusing this virtual user's own session is what makes the socket
        inherit the cookies an earlier HTTP login step set -- the whole point
        of a mixed HTTP/WebSocket flow. A backend that cannot speak WebSocket
        returns None and the step reports that rather than failing obscurely.
        """
        return None


class Backend(ABC):
    """A run's transport: owns the shared pool, hands out per-user sessions."""

    name: str = BACKEND_AIOHTTP
    #: Phases this backend can actually measure.
    phases: tuple[str, ...] = ALL_PHASES
    #: Exception types the worker loop should treat as request failures.
    transport_errors: tuple[type[BaseException], ...] = ()

    @abstractmethod
    def create_session(self, stats: WorkerStats, isolate_cookies: bool = True) -> BackendSession:
        """Build a session for one virtual user."""

    @abstractmethod
    async def aclose(self) -> None:
        """Release the shared pool."""

    @property
    def describe(self) -> str:
        """Human label for banners and reports."""
        return self.name


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def target_is_ip_literal(url: str) -> bool:
    """True when *url*'s host is a bare IP address rather than a name.

    Cookie jars refuse to store cookies for IP hosts, which silently breaks
    sessions against the loopback targets load tests point at.
    """
    import ipaddress

    host = urlparse(url).hostname
    if not host:
        return False
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def create_cookie_jar(config: "BenchmarkConfig", isolate_cookies: bool = True):
    """Build the aiohttp cookie jar for one virtual user's session.

    ``--no-session-cookies`` yields a ``DummyCookieJar``, which discards every
    ``Set-Cookie`` so requests carry only the static ``-C`` header — the
    behaviour to keep when benchmarking a cache or CDN layer.

    *isolate_cookies* is False for plain connection mode, which has no per-user
    identity to isolate and keeps the client library's own default jar.
    """
    if not config.session_cookies:
        return aiohttp.DummyCookieJar()
    if not isolate_cookies:
        return None
    return aiohttp.CookieJar(unsafe=target_is_ip_literal(config.url))


def build_ssl_context(config: "BenchmarkConfig") -> "ssl.SSLContext | None":
    """Create an SSL context for HTTPS targets, or None for plain HTTP."""
    if urlparse(config.url).scheme != "https":
        return None
    return ssl_context_from(config.ssl_config)


def ssl_context_from(ssl_config: "SSLConfig") -> "ssl.SSLContext":
    """Build a context from --ssl-verify / --ca-bundle, with no scheme opinion.

    Split out from :func:`build_ssl_context` so ``wss://`` gets byte-identical
    TLS behaviour to ``https://`` rather than a second implementation that
    drifts from it.
    """
    ssl_ctx = ssl.create_default_context()
    if not ssl_config.verify:
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE
    elif ssl_config.ca_bundle:
        ssl_ctx.load_verify_locations(ssl_config.ca_bundle)
    return ssl_ctx


def normalize_http_version(raw: object) -> str:
    """Map a backend's protocol marker onto ``1.1`` / ``2`` / ``unknown``.

    Accepts the spellings the ecosystem actually uses: ``HTTP/2`` from httpx,
    a bare ``2`` from an ASGI scope, and the ``h2`` ALPN token.
    """
    text = str(raw or "").upper().replace("HTTP/", "").strip()
    if text.startswith("H") and text[1:2].isdigit():
        text = text[1:]  # ALPN tokens: h2, h3
    if text.startswith("2"):
        return HTTP_2
    if text.startswith("1"):
        return HTTP_1_1
    if text.startswith("3"):
        return "3"
    return HTTP_UNKNOWN


# ---------------------------------------------------------------------------
# aiohttp backend (default)
# ---------------------------------------------------------------------------


class AiohttpSession(BackendSession):
    """aiohttp ClientSession wrapper."""

    __slots__ = ("_session", "_ssl", "_jar", "_timeout_sec", "_timeout")

    def __init__(self, session: aiohttp.ClientSession, ssl_verify: bool, jar) -> None:
        self._session = session
        self._ssl = ssl_verify
        self._jar = jar
        # ClientTimeout is immutable and rebuilt on every request otherwise; the
        # value only changes as a duration run winds down, so cache it.
        self._timeout_sec: float = -1.0
        self._timeout: "aiohttp.ClientTimeout | None" = None

    def _client_timeout(self, timeout_sec: float) -> aiohttp.ClientTimeout:
        if timeout_sec != self._timeout_sec or self._timeout is None:
            self._timeout_sec = timeout_sec
            self._timeout = aiohttp.ClientTimeout(total=timeout_sec)
        return self._timeout

    def raw_websocket_session(self) -> "aiohttp.ClientSession | None":
        return self._session

    async def __aenter__(self) -> "AiohttpSession":
        await self._session.__aenter__()
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self._session.__aexit__(*exc_info)

    async def send(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: "bytes | None",
        timeout_sec: float,
        trace_ctx: "dict | None" = None,
    ) -> BackendResponse:
        async with self._session.request(
            method,
            url,
            headers=headers,
            data=body,
            ssl=self._ssl,
            timeout=self._client_timeout(timeout_sec),
            trace_request_ctx=trace_ctx,
        ) as resp:
            data = await resp.read()
            version = resp.version
            return BackendResponse(
                status=resp.status,
                body=data,
                # Handed over as-is: it is already a case-insensitive mapping,
                # and copying it into a dict on every request is pure overhead
                # for the callers that only ever read one header.
                headers=resp.headers,
                http_version=(
                    HTTP_1_1
                    if version is not None and version.major == 1 and version.minor == 1
                    else normalize_http_version(
                        f"{version.major}.{version.minor}" if version else None
                    )
                ),
            )

    def clear_cookies(self) -> None:
        if self._jar is not None:
            self._jar.clear()


class AiohttpBackend(Backend):
    """The default backend: HTTP/1.1 with full phase-level tracing."""

    name = BACKEND_AIOHTTP
    phases = ALL_PHASES
    transport_errors = (aiohttp.ClientError, asyncio.TimeoutError, OSError)

    def __init__(self, config: "BenchmarkConfig", pool_limit: int) -> None:
        self._config = config
        self._ssl_verify = config.ssl_config.verify
        ssl_ctx = build_ssl_context(config)
        self._connector = aiohttp.TCPConnector(
            limit=pool_limit,
            # True is aiohttp's own default ("verify normally"); it is only ever
            # reached for http:// targets, where the setting is unused anyway.
            ssl=ssl_ctx if ssl_ctx is not None else True,
            force_close=not config.keepalive,
            enable_cleanup_closed=True,
        )

    @property
    def connector(self) -> aiohttp.TCPConnector:
        """The shared pool, exposed for tests and for legacy call sites."""
        return self._connector

    def create_session(self, stats: WorkerStats, isolate_cookies: bool = True) -> BackendSession:
        from pywrkr.workers import create_trace_config

        jar = create_cookie_jar(self._config, isolate_cookies)
        kwargs: dict = {"connector": self._connector, "connector_owner": False}
        if self._config.latency_breakdown:
            kwargs["trace_configs"] = [create_trace_config(stats)]
        if jar is not None:
            kwargs["cookie_jar"] = jar
        return AiohttpSession(aiohttp.ClientSession(**kwargs), self._ssl_verify, jar)

    async def aclose(self) -> None:
        await self._connector.close()

    @property
    def describe(self) -> str:
        return "aiohttp (HTTP/1.1)"


# ---------------------------------------------------------------------------
# httpx backend (--http2)
# ---------------------------------------------------------------------------


def _import_httpx():
    """Import httpx with h2 support, or explain how to get it."""
    try:
        import httpx
    except ImportError:
        raise BackendUnavailableError(
            f"--http2 requires the httpx backend, which is not installed. "
            f"Install it with: {HTTP2_INSTALL_HINT}"
        ) from None
    try:
        import h2  # noqa: F401
    except ImportError:
        raise BackendUnavailableError(
            f"--http2 requires HTTP/2 support in httpx (the 'h2' package), which is "
            f"not installed. Install it with: {HTTP2_INSTALL_HINT}"
        ) from None
    return httpx


class HttpxSession(BackendSession):
    """httpx AsyncClient wrapper.

    Two send paths: the plain one, and a streaming one used only when a latency
    breakdown was requested, because measuring time-to-first-byte means not
    buffering the whole body up front.
    """

    __slots__ = ("_client", "_httpx", "_stats", "_collect_phases", "_drop_cookies")

    def __init__(
        self, client, httpx_mod, stats: WorkerStats, collect_phases: bool, drop_cookies: bool
    ) -> None:
        self._client = client
        self._httpx = httpx_mod
        self._stats = stats
        self._collect_phases = collect_phases
        self._drop_cookies = drop_cookies

    async def __aenter__(self) -> "HttpxSession":
        await self._client.__aenter__()
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self._client.__aexit__(*exc_info)

    async def send(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: "bytes | None",
        timeout_sec: float,
        trace_ctx: "dict | None" = None,
    ) -> BackendResponse:
        timeout = self._httpx.Timeout(timeout_sec)
        request = self._client.build_request(
            method, url, headers=headers, content=body, timeout=timeout
        )
        if self._collect_phases and trace_ctx is not None:
            response = await self._send_streaming(request)
        else:
            resp = await self._client.send(request)
            response = BackendResponse(
                status=resp.status_code,
                body=resp.content,
                headers=dict(resp.headers),
                http_version=normalize_http_version(resp.http_version),
            )
        if self._drop_cookies:
            # No DummyCookieJar equivalent in httpx, so emulate one.
            self._client.cookies.clear()
        return response

    async def _send_streaming(self, request) -> BackendResponse:
        """Send with the body streamed so TTFB can be measured."""
        start = time.monotonic()
        resp = await self._client.send(request, stream=True)
        first_byte: "float | None" = None
        chunks: list[bytes] = []
        try:
            async for chunk in resp.aiter_bytes():
                if first_byte is None:
                    first_byte = time.monotonic()
                chunks.append(chunk)
        finally:
            await resp.aclose()
        end = time.monotonic()
        if first_byte is None:  # empty body: first byte is the end of the response
            first_byte = end

        self._stats.breakdowns.append(
            LatencyBreakdown(
                ttfb=max(first_byte - start, 0.0),
                transfer=max(end - first_byte, 0.0),
                # DNS/TCP/TLS are not observable here; say so rather than
                # recording zeros that would drag the phase averages down.
                available=HTTPX_PHASES,
            )
        )
        return BackendResponse(
            status=resp.status_code,
            body=b"".join(chunks),
            headers=dict(resp.headers),
            http_version=normalize_http_version(resp.http_version),
        )

    def clear_cookies(self) -> None:
        self._client.cookies.clear()


class HttpxBackend(Backend):
    """HTTP/2-capable backend built on httpx.

    Over TLS, ALPN decides: a server that only offers HTTP/1.1 is used as such
    and reported, never silently counted as h2. Over cleartext there is nothing
    to negotiate, so h2 is attempted with prior knowledge (h2c) — which is the
    only way ``--http2`` can mean anything against an ``http://`` target.
    """

    name = BACKEND_HTTPX

    def __init__(self, config: "BenchmarkConfig", pool_limit: int) -> None:
        self._httpx = _import_httpx()
        self._config = config
        self._cleartext = urlparse(config.url).scheme != "https"
        self.phases = HTTPX_PHASES
        self.transport_errors = (self._httpx.HTTPError, asyncio.TimeoutError, OSError)
        self._limits = self._httpx.Limits(
            max_connections=max(1, pool_limit),
            max_keepalive_connections=0 if not config.keepalive else max(1, pool_limit),
        )

    def _verify(self):
        if self._cleartext:
            return True  # unused for http://
        if not self._config.ssl_config.verify:
            return False
        return self._config.ssl_config.ca_bundle or True

    def create_session(self, stats: WorkerStats, isolate_cookies: bool = True) -> BackendSession:
        client = self._httpx.AsyncClient(
            http2=True,
            # Cleartext h2 has no ALPN handshake, so HTTP/1.1 has to be off for
            # the client to use HTTP/2 prior knowledge. Over TLS both stay on so
            # ALPN can legitimately fall back to h1.
            http1=not self._cleartext,
            limits=self._limits,
            verify=self._verify(),
            follow_redirects=False,
            timeout=self._httpx.Timeout(self._config.timeout_sec),
        )
        return HttpxSession(
            client,
            self._httpx,
            stats,
            collect_phases=self._config.latency_breakdown,
            drop_cookies=not self._config.session_cookies,
        )

    async def aclose(self) -> None:
        return None  # each AsyncClient owns and closes its own pool

    @property
    def describe(self) -> str:
        mode = "h2c prior knowledge" if self._cleartext else "ALPN, h1 fallback"
        return f"httpx (HTTP/2, {mode})"


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_backend(config: "BenchmarkConfig", pool_limit: int) -> Backend:
    """Build the backend a config asks for.

    Raises:
        BackendUnavailableError: ``--http2`` was requested but httpx/h2 are not
            installed.
    """
    if getattr(config, "http2", False):
        return HttpxBackend(config, pool_limit)
    return AiohttpBackend(config, pool_limit)


def http2_available() -> bool:
    """True when the optional HTTP/2 dependencies are importable."""
    try:
        _import_httpx()
    except BackendUnavailableError:
        return False
    return True
