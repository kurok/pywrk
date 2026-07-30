"""The public Python API.

pywrkr is pure Python, which is the one thing it has that k6, wrk and Gatling
do not: it can run *inside* a test suite, a notebook, or an orchestration
script rather than only as a subprocess.

    import pywrkr

    result = pywrkr.run("https://api.example.com/health", connections=50, duration=30)
    assert result.percentiles.p95 < 0.3

The CLI and this API are two front-ends over one schema: :meth:`Result.to_dict`
returns exactly what ``--json`` writes, because both are produced by the same
builder. Threshold verdicts come back on the result instead of becoming an
``exit()``; the CLI is what maps them to exit codes.

Nothing here prints, exits, or installs signal handlers.
"""

from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping

from pywrkr.config import BenchmarkConfig, Threshold, WorkerStats
from pywrkr.reporting import build_results_dict, evaluate_thresholds, parse_threshold
from pywrkr.workers import LiveStats, run_benchmark, run_user_simulation

__all__ = [
    "Config",
    "Latency",
    "LiveStats",
    "Percentiles",
    "Result",
    "ThresholdVerdict",
    "arun",
    "run",
]

#: The configuration object. Alias rather than a parallel type, so the library
#: and the CLI cannot drift apart in what a run can be asked to do.
Config = BenchmarkConfig

#: Exit code the CLI uses for a breached threshold; mirrored on Result so a
#: caller can reproduce CLI semantics without importing anything else.
EXIT_THRESHOLD_BREACH = 2


@dataclass(frozen=True)
class Latency:
    """Latency summary in seconds."""

    min: float = 0.0
    max: float = 0.0
    mean: float = 0.0
    median: float = 0.0
    stdev: float = 0.0


@dataclass(frozen=True)
class Percentiles:
    """Latency percentiles in seconds.

    Indexable as well as attribute-addressed, so tail percentiles that only
    exist for large samples (``p99.9``) stay reachable::

        result.percentiles.p95
        result.percentiles["p99.9"]
    """

    _values: Mapping[str, float] = field(default_factory=dict)

    def __getitem__(self, key: str) -> float:
        return self._values[key]

    def __contains__(self, key: object) -> bool:
        return key in self._values

    def get(self, key: str, default: "float | None" = None) -> "float | None":
        return self._values.get(key, default)

    def as_dict(self) -> dict[str, float]:
        return dict(self._values)

    @property
    def p50(self) -> float:
        return self._values.get("p50", 0.0)

    @property
    def p75(self) -> float:
        return self._values.get("p75", 0.0)

    @property
    def p90(self) -> float:
        return self._values.get("p90", 0.0)

    @property
    def p95(self) -> float:
        return self._values.get("p95", 0.0)

    @property
    def p99(self) -> float:
        return self._values.get("p99", 0.0)


@dataclass(frozen=True)
class ThresholdVerdict:
    """The outcome of one ``--threshold``-style expression."""

    expression: str
    metric: str
    actual: float
    passed: bool


@dataclass(frozen=True)
class Result:
    """Everything one run produced.

    The canonical form is :meth:`to_dict`, which is byte-for-byte the structure
    the CLI writes with ``--json``. The typed accessors below read from it, so
    the two can never disagree.
    """

    _data: Mapping[str, Any]
    stats: WorkerStats
    thresholds: tuple[ThresholdVerdict, ...] = ()

    # -- canonical forms ---------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Return the results as the CLI's ``--json`` structure."""
        return copy.deepcopy(dict(self._data))

    def to_json(self, indent: "int | None" = 2) -> str:
        """Serialize the results exactly as ``--json`` would write them."""
        return json.dumps(self.to_dict(), indent=indent, allow_nan=False)

    # -- headline numbers --------------------------------------------------

    @property
    def duration(self) -> float:
        """Wall-clock seconds the run actually took."""
        return float(self._data.get("duration_sec", 0.0))

    @property
    def total_requests(self) -> int:
        return int(self._data.get("total_requests", 0))

    @property
    def total_errors(self) -> int:
        return int(self._data.get("total_errors", 0))

    @property
    def requests_per_sec(self) -> float:
        return float(self._data.get("requests_per_sec", 0.0))

    @property
    def error_rate(self) -> float:
        """Errors as a percentage of requests."""
        total = self.total_requests
        return (self.total_errors / total * 100) if total else 0.0

    @property
    def total_bytes(self) -> int:
        return int(self._data.get("total_bytes", 0))

    @property
    def latency(self) -> Latency:
        return Latency(**{k: float(v) for k, v in (self._data.get("latency") or {}).items()})

    @property
    def percentiles(self) -> Percentiles:
        return Percentiles({k: float(v) for k, v in (self._data.get("percentiles") or {}).items()})

    @property
    def status_codes(self) -> dict[int, int]:
        return {int(k): int(v) for k, v in (self._data.get("status_codes") or {}).items()}

    @property
    def error_types(self) -> dict[str, int]:
        return dict(self._data.get("error_types") or {})

    @property
    def http_versions(self) -> dict[str, int]:
        """Negotiated protocol counts, e.g. ``{"2": 1000}``."""
        return dict(self._data.get("http_versions") or {})

    @property
    def rps_timeline(self) -> list[tuple[float, int]]:
        """``(seconds_from_start, requests_in_interval)`` pairs."""
        return [(float(ts), int(count)) for ts, count in self._data.get("rps_timeline") or []]

    @property
    def steps(self) -> dict[str, dict[str, Any]]:
        """Per-step stats for scenario runs, keyed by step name."""
        return copy.deepcopy(dict(self._data.get("step_stats") or {}))

    # -- verdicts ----------------------------------------------------------

    @property
    def passed(self) -> bool:
        """True when every threshold passed (vacuously true with none)."""
        return all(v.passed for v in self.thresholds)

    @property
    def exit_code(self) -> int:
        """The exit code the CLI would use for this result."""
        return 0 if self.passed else EXIT_THRESHOLD_BREACH

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"Result(requests={self.total_requests}, errors={self.total_errors}, "
            f"rps={self.requests_per_sec:.1f}, p95={self.percentiles.p95:.4f}s, "
            f"passed={self.passed})"
        )


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def _coerce_thresholds(raw: "Iterable[str | Threshold] | None") -> list[Threshold]:
    """Accept threshold expressions as strings or already-parsed objects."""
    parsed: list[Threshold] = []
    for item in raw or ():
        parsed.append(item if isinstance(item, Threshold) else parse_threshold(item))
    return parsed


def _build_config(target: "str | BenchmarkConfig", kwargs: dict[str, Any]) -> BenchmarkConfig:
    """Turn a URL plus keywords, or a ready-made Config, into a run config."""
    thresholds = _coerce_thresholds(kwargs.pop("thresholds", None))

    if isinstance(target, BenchmarkConfig):
        if kwargs:
            raise TypeError(
                f"arun(config) takes no other keyword arguments; got {', '.join(sorted(kwargs))}. "
                f"Set them on the Config instead."
            )
        config = copy.copy(target)
        if thresholds:
            config.thresholds = thresholds
    else:
        if not isinstance(target, str) or not target.strip():
            raise TypeError("The first argument must be a target URL or a pywrkr.Config")
        unknown = [k for k in kwargs if not hasattr(BenchmarkConfig, k) and k != "url"]
        if unknown:
            raise TypeError(f"Unknown option(s): {', '.join(sorted(unknown))}")
        config = BenchmarkConfig(url=target, thresholds=thresholds, **kwargs)

    # Library mode: no banner, no result printing, no output files unless the
    # caller asked for them explicitly on the Config.
    config._quiet = True
    return config


async def arun(
    target: "str | BenchmarkConfig",
    *,
    on_tick: "Callable[[LiveStats], None] | None" = None,
    **kwargs: Any,
) -> Result:
    """Run a benchmark and return its :class:`Result`.

    Safe to await from inside an existing event loop — it never calls
    ``asyncio.run``. Nothing is printed, no signal handlers are installed, and
    a breached threshold comes back as a verdict rather than an exit.

    Args:
        target: A URL, or a fully built :class:`Config`.
        on_tick: Called about once a second with a :class:`LiveStats` snapshot.
            An exception from it is logged and the run continues.
        **kwargs: Any :class:`Config` field, when *target* is a URL.
            ``thresholds`` also accepts expression strings such as
            ``"p95 < 300ms"``.

    Returns:
        A :class:`Result` whose ``to_dict()`` matches the CLI's JSON output.

    Raises:
        TypeError: The target or an option is not usable.
    """
    config = _build_config(target, dict(kwargs))
    reported: dict[str, Any] = {}

    def _capture(stats: WorkerStats, duration: float, concurrency: int) -> None:
        reported.update(stats=stats, duration=duration, concurrency=concurrency)

    runner = run_user_simulation if config.users is not None else run_benchmark
    stats, _ = await runner(
        config,
        on_tick=on_tick,
        install_signal_handlers=False,
        on_complete=_capture,
    )

    # The run reports against its measured wall clock, not the configured
    # duration; taking the number it used keeps rps identical to the CLI's.
    return build_result(
        reported.get("stats", stats),
        config,
        float(reported.get("duration", config.duration or 0.0)),
        connections=reported.get("concurrency"),
    )


def run(
    target: "str | BenchmarkConfig",
    *,
    on_tick: "Callable[[LiveStats], None] | None" = None,
    **kwargs: Any,
) -> Result:
    """Blocking wrapper around :func:`arun`, for scripts and test suites.

    Raises:
        RuntimeError: Called from a running event loop, where it would deadlock
            or nest ``asyncio.run``. Await :func:`arun` there instead.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError(
            "pywrkr.run() cannot be called from a running event loop. "
            "Use `await pywrkr.arun(...)` instead."
        )
    return asyncio.run(arun(target, on_tick=on_tick, **kwargs))


def build_result(
    stats: WorkerStats,
    config: BenchmarkConfig,
    duration: float,
    connections: "int | None" = None,
) -> Result:
    """Assemble a :class:`Result` from raw stats.

    Exposed for callers that drive the worker coroutines themselves and still
    want the typed result object.
    """
    concurrency = connections if connections is not None else (config.users or config.connections)
    data = build_results_dict(stats, duration, concurrency, config)
    verdicts = tuple(
        ThresholdVerdict(
            expression=threshold.raw_expr,
            metric=threshold.metric,
            actual=actual,
            passed=passed,
        )
        for threshold, actual, passed in evaluate_thresholds(config.thresholds, stats, duration)
    )
    return Result(_data=data, stats=stats, thresholds=verdicts)
