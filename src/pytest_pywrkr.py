"""pytest integration: performance assertions as ordinary tests.

Performance testing usually lives in its own silo, runs rarely, and rots.
Being pure Python is pywrkr's structural advantage over wrk/k6/Gatling, and
this is what cashes it in: an SLO becomes a test in the suite that already
exists, failing a PR the way a unit test does.

    @pytest.mark.pywrkr(url="/health", connections=20, duration=10,
                        thresholds=["p95 < 200ms"])
    def test_health_meets_slo(pywrkr_result):
        assert pywrkr_result.error_rate < 1.0

Two things shape the design:

* **Benchmarks are real load.** Marked tests skip unless ``--pywrkr-run`` is
  given, so a developer running ``pytest`` does not unknowingly put 50
  connections on a shared staging host. The skip reason says how to opt in.
* **The plugin costs nothing to have installed.** pytest imports every
  registered plugin at startup, in every project that has pywrkr installed.
  So this is a top-level module rather than ``pywrkr.pytest_plugin``: any
  module under ``pywrkr.`` would pull in the package ``__init__`` and its whole
  public API, about 90ms on every pytest run of a project that never
  benchmarks anything. Nothing but pytest is imported at module scope; the
  benchmark runner is imported inside the fixture that needs it.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pywrkr.api import Result

__all__ = [
    "BenchRecord",
    "PywrkrPlugin",
    "format_summary",
    "json_filename",
    "resolve_target",
    "xdist_worker_count",
]

#: ini keys, each the lower-cased name of the Config field it defaults.
_INI_OPTIONS = (
    ("pywrkr_base_url", "Base URL prepended to a relative benchmark target"),
    ("pywrkr_duration", "Default benchmark duration in seconds"),
    ("pywrkr_connections", "Default number of connections"),
)

#: Where a breached-threshold message waits between fixture setup and the call
#: phase that reports it.
_BREACH_KEY: "pytest.StashKey[str]" = pytest.StashKey()

_SKIP_REASON = "pywrkr benchmarks put real load on the target; pass --pywrkr-run to execute them"

#: Characters that are legal in a pytest node id but not in a filename.
_UNSAFE_IN_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass
class BenchRecord:
    """One benchmark a test ran, kept for the terminal summary."""

    nodeid: str
    url: str
    result: "Result"

    @property
    def name(self) -> str:
        """The test's own name, without the file path that prefixes the node id."""
        return self.nodeid.rsplit("::", 1)[-1]

    @property
    def verdict(self) -> str:
        return "PASS" if self.result.passed else "FAIL"

    @property
    def breaches(self) -> list:
        return [v for v in self.result.thresholds if not v.passed]


def resolve_target(url: str, base_url: "str | None") -> str:
    """Join a benchmark target with the configured base URL.

    A relative target is the common case in a test suite -- the host is a
    per-environment detail, the path is what the test is about -- so
    ``pywrkr_base_url`` supplies the former and the test names the latter.
    An absolute URL always wins, so one test can point somewhere else.
    """
    if "://" in url:
        return url
    if not base_url:
        raise pytest.UsageError(
            f"pywrkr: {url!r} is relative and no base URL is configured. "
            "Set `pywrkr_base_url` in your pytest ini file or use an absolute URL."
        )
    return f"{base_url.rstrip('/')}/{url.lstrip('/')}"


def json_filename(nodeid: str) -> str:
    """Turn a pytest node id into a safe, unique-ish JSON filename."""
    stem = _UNSAFE_IN_FILENAME.sub("-", nodeid).strip("-")
    return f"{stem or 'benchmark'}.json"


def format_summary(records: "list[BenchRecord]") -> list[str]:
    """Render the summary table, as a list of lines.

    Separate from the terminal writer so its exact shape can be tested without
    driving pytest's reporting machinery.
    """
    if not records:
        return []
    name_width = max(len("Test"), max(len(r.name) for r in records))
    header = (
        f"{'Test':<{name_width}}  {'Requests':>9}  {'Req/s':>9}  "
        f"{'p50':>9}  {'p95':>9}  {'p99':>9}  {'Errors':>7}  Verdict"
    )
    lines = [header, "-" * len(header)]
    for record in records:
        result = record.result
        pct = result.percentiles
        lines.append(
            f"{record.name:<{name_width}}  "
            f"{result.total_requests:>9,}  "
            f"{result.requests_per_sec:>9,.1f}  "
            f"{_ms(pct.p50):>9}  {_ms(pct.p95):>9}  {_ms(pct.p99):>9}  "
            f"{result.error_rate:>6.2f}%  {record.verdict}"
        )
        for verdict in record.breaches:
            lines.append(f"{'':<{name_width}}    breached: {verdict.expression}")
    return lines


def _format_metric(verdict) -> str:
    """Render a threshold's measured value in the unit its metric is in."""
    from pywrkr.compare import format_value, metric_unit

    return format_value(verdict.actual, metric_unit(verdict.metric))


def _ms(seconds: float) -> str:
    if seconds >= 1:
        return f"{seconds:.3f}s"
    return f"{seconds * 1000:.2f}ms"


class PywrkrPlugin:
    """Collects what every benchmark did, then reports it once at the end."""

    def __init__(self, config: pytest.Config) -> None:
        self.config = config
        self.records: list[BenchRecord] = []

    # -- collection --------------------------------------------------------

    def record(self, nodeid: str, url: str, result: "Result") -> None:
        self.records.append(BenchRecord(nodeid=nodeid, url=url, result=result))
        json_dir = self.config.getoption("pywrkr_json")
        if json_dir:
            self._write_json(json_dir, nodeid, result)

    def _write_json(self, directory: str, nodeid: str, result: "Result") -> None:
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, json_filename(nodeid))
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(result.to_dict(), handle, indent=2)

    # -- reporting ---------------------------------------------------------

    def pytest_terminal_summary(self, terminalreporter) -> None:
        if not self.records:
            return
        terminalreporter.write_sep("=", "pywrkr benchmarks")
        for line in format_summary(self.records):
            terminalreporter.write_line(line)


# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("pywrkr", "performance benchmarking")
    group.addoption(
        "--pywrkr-run",
        action="store_true",
        default=False,
        help="Actually run pywrkr benchmarks. Without it they skip, because "
        "they put real load on the target.",
    )
    group.addoption(
        "--pywrkr-json",
        action="store",
        default=None,
        metavar="DIR",
        help="Write each benchmark's JSON results into DIR, for `pywrkr compare`",
    )
    for name, help_text in _INI_OPTIONS:
        parser.addini(name, help_text, default=None)


def xdist_worker_count(config: pytest.Config) -> int:
    """How many xdist workers this session will use, 0 when xdist is not in play.

    Read defensively: xdist may not be installed, and the option is spelled
    differently on the controller (``numprocesses``) than on a worker (which
    carries ``workerinput`` instead).
    """
    if hasattr(config, "workerinput"):
        return 2  # We are *inside* a worker; the exact count does not matter.
    count = config.getoption("numprocesses", default=None)
    if count in (None, 0, "0", "no"):
        return 0
    if isinstance(count, int):
        return count
    return 2  # "auto" / "logical" -- more than one, which is all that matters.


def pytest_configure(config: pytest.Config) -> None:
    if config.getoption("pywrkr_run", default=False) and xdist_worker_count(config) > 1:
        # Not a plumbing limitation. Load tests running beside each other
        # contend for the same CPU, sockets and target: four workers each
        # opening 50 connections put 200 on the host while each reports 50, so
        # every number produced would be wrong in a way nothing downstream
        # could detect. Refusing is the only honest option.
        raise pytest.UsageError(
            "pywrkr: --pywrkr-run cannot be combined with pytest-xdist. Benchmarks "
            "running in parallel contend for CPU and for the target, so the numbers "
            "they report are not the load they think they applied. Run the "
            "benchmarks in their own non-parallel invocation, e.g. "
            "`pytest -p no:xdist --pywrkr-run -m pywrkr`."
        )
    config.addinivalue_line(
        "markers",
        "pywrkr(url, **options): run a pywrkr benchmark for this test and expose "
        "it as the `pywrkr_result` fixture. Options are Config fields, plus "
        "`thresholds` as a list of expressions.",
    )
    config.pluginmanager.register(PywrkrPlugin(config), "pywrkr-plugin")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item: pytest.Item):
    """Turn a recorded threshold breach into a test failure.

    The benchmark necessarily runs during fixture setup, but a breached SLO is
    a failing test rather than a broken one, and it should read that way in the
    report.

    The breach takes precedence over anything the body raised: it happened
    first, and an assertion that fails after the SLO was already missed is
    usually downstream of it. Reporting the body's complaint instead would bury
    the actual finding.
    """
    outcome = yield
    message = item.stash.get(_BREACH_KEY, None)
    if message is not None:
        outcome.force_exception(pytest.fail.Exception(message))


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip benchmarks unless they were asked for.

    A test suite is run casually and often; a load test is neither. Opting in
    is what keeps `pytest` from quietly becoming a denial of service against
    whatever the base URL points at.
    """
    if config.getoption("pywrkr_run"):
        return
    skip = pytest.mark.skip(reason=_SKIP_REASON)
    for item in items:
        if item.get_closest_marker("pywrkr") or "pywrkr_bench" in getattr(item, "fixturenames", ()):
            item.add_marker(skip)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@dataclass
class _Defaults:
    """ini-file defaults, resolved once per session."""

    base_url: "str | None" = None
    options: dict[str, Any] = field(default_factory=dict)


@pytest.fixture(scope="session")
def _pywrkr_defaults(pytestconfig: pytest.Config) -> _Defaults:
    defaults = _Defaults(base_url=pytestconfig.getini("pywrkr_base_url") or None)
    duration = pytestconfig.getini("pywrkr_duration")
    connections = pytestconfig.getini("pywrkr_connections")
    if duration:
        defaults.options["duration"] = float(duration)
    if connections:
        defaults.options["connections"] = int(connections)
    return defaults


@pytest.fixture
def pywrkr_bench(request: pytest.FixtureRequest, _pywrkr_defaults: _Defaults):
    """Run a benchmark from inside a test and get a :class:`~pywrkr.Result`.

    ::

        def test_health(pywrkr_bench):
            result = pywrkr_bench("/health", connections=20, duration=10)
            assert result.percentiles.p95 < 0.2

    Every call is recorded for the end-of-run summary and, with
    ``--pywrkr-json DIR``, written out for ``pywrkr compare``.
    """
    plugin = request.config.pluginmanager.get_plugin("pywrkr-plugin")
    if plugin is None:  # pragma: no cover - pytest_configure always registers it
        raise pytest.UsageError("pywrkr: the plugin is not registered")

    def bench(url: str, **kwargs: Any) -> "Result":
        # Imported here, not at module scope: the plugin loads in every pytest
        # run of any project that has pywrkr installed, and should cost nothing
        # until a test actually asks for a benchmark.
        from pywrkr.api import run

        target = resolve_target(url, _pywrkr_defaults.base_url)
        options = {**_pywrkr_defaults.options, **kwargs}
        result = run(target, **options)
        plugin.record(request.node.nodeid, target, result)
        return result

    return bench


@pytest.fixture
def pywrkr_result(request: pytest.FixtureRequest, pywrkr_bench) -> "Result":
    """The result of the benchmark declared by ``@pytest.mark.pywrkr``.

    A breached threshold fails the test here, naming the metric, the bound and
    what was actually measured -- so a declarative SLO needs no assert of its
    own, and a failure reads as a performance failure rather than as an
    assertion on an opaque number.
    """
    marker = request.node.get_closest_marker("pywrkr")
    if marker is None:
        raise pytest.UsageError(
            "The `pywrkr_result` fixture requires a @pytest.mark.pywrkr(...) marker "
            "on the test. Use the `pywrkr_bench` fixture to run a benchmark directly."
        )

    options = dict(marker.kwargs)
    url = options.pop("url", None)
    if url is None:
        if not marker.args:
            raise pytest.UsageError("@pytest.mark.pywrkr requires a `url` (positional or keyword)")
        url = marker.args[0]

    result = pywrkr_bench(url, **options)
    if not result.passed:
        # Recorded rather than raised: pytest.fail() inside a fixture is a
        # setup *error*, and a breached SLO is a failing test, not a broken
        # one. pytest_runtest_call below turns this into the failure.
        breaches = "\n".join(
            f"  - {v.expression} (measured {v.metric} = {_format_metric(v)})"
            for v in result.thresholds
            if not v.passed
        )
        request.node.stash[_BREACH_KEY] = f"pywrkr threshold(s) breached for {url}:\n{breaches}"
    return result
