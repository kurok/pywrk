"""Baseline comparison and regression detection.

Absolute SLO gates (``--threshold p95 < 300ms``) rot: loose enough never to
fire, or tight enough to flake on infrastructure noise. What a CI gate usually
wants is relative — "fail if p95 got more than 10% worse than the last known-good
run" — which needs two result files and a delta, not a fixed number.

This module reads pywrkr's own ``--json`` output, computes per-metric deltas, and
evaluates ``--fail-on`` rules against them. It is deliberately arithmetic only:
no statistical significance testing, no trend storage. Averaging several baseline
runs is the one concession to noise.
"""

from __future__ import annotations

import glob
import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence, TextIO

logger = logging.getLogger(__name__)

__all__ = [
    "COMPARE_FORMATS",
    "EXIT_REGRESSION",
    "EXIT_USAGE",
    "SCHEMA_VERSION",
    "ComparisonReport",
    "FailOn",
    "MetricDelta",
    "Verdict",
    "average_results",
    "compare_results",
    "config_differences",
    "load_baseline",
    "load_results",
    "metric_value",
    "parse_fail_on",
    "render_report",
]

#: Bumped when the ``--json`` result shape changes incompatibly. Files without
#: the key predate it and are read as version 1.
SCHEMA_VERSION = 1

#: Exit code for "a --fail-on rule fired", distinct from 2 (absolute threshold).
EXIT_REGRESSION = 3

#: Exit code for a usage or schema problem.
EXIT_USAGE = 1

COMPARE_FORMATS = ("table", "markdown", "json")

# ---------------------------------------------------------------------------
# Metric registry
# ---------------------------------------------------------------------------

# Unit tags drive both display and how a bare --fail-on delta is read.
_SECONDS = "seconds"
_PERCENT = "percent"
_RATE = "rate"
_COUNT = "count"
_BYTES = "bytes"


def _dig(results: dict, *path: str) -> "float | None":
    """Follow a key path through the results dict, returning None if absent."""
    node: Any = results
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return float(node) if isinstance(node, (int, float)) and not isinstance(node, bool) else None


def _error_rate(results: dict) -> "float | None":
    """Errors as a percentage of requests — derived, not stored in the JSON.

    None when no requests were made: a rate over nothing is undefined, not
    zero. Returning 0.0 there let ``error_rate < 1%`` pass on a run that never
    completed a request. See #213.
    """
    total = _dig(results, "total_requests")
    errors = _dig(results, "total_errors")
    if total is None or errors is None or not total:
        return None
    return errors / total * 100


#: metric name -> (accessor, unit, higher_is_better)
_METRICS: dict[str, tuple[Callable[[dict], "float | None"], str, bool]] = {
    "rps": (lambda r: _dig(r, "requests_per_sec"), _RATE, True),
    "error_rate": (_error_rate, _PERCENT, False),
    "total_requests": (lambda r: _dig(r, "total_requests"), _COUNT, True),
    "total_errors": (lambda r: _dig(r, "total_errors"), _COUNT, False),
    "total_bytes": (lambda r: _dig(r, "total_bytes"), _BYTES, True),
    "transfer_rate": (lambda r: _dig(r, "transfer_per_sec_bytes"), _BYTES, True),
    "duration": (lambda r: _dig(r, "duration_sec"), _SECONDS, False),
    "min_latency": (lambda r: _dig(r, "latency", "min"), _SECONDS, False),
    "max_latency": (lambda r: _dig(r, "latency", "max"), _SECONDS, False),
    "avg_latency": (lambda r: _dig(r, "latency", "mean"), _SECONDS, False),
    "median_latency": (lambda r: _dig(r, "latency", "median"), _SECONDS, False),
    "stdev_latency": (lambda r: _dig(r, "latency", "stdev"), _SECONDS, False),
}


def _percentile_getter(key: str) -> Callable[[dict], "float | None"]:
    """Build an accessor for one percentile, closing over its JSON key."""

    def get(results: dict) -> "float | None":
        return _dig(results, "percentiles", key)

    return get


# Percentiles are addressed by their JSON names (p50, p95, p99.9, ...).
_PERCENTILE_KEYS = ("p50", "p75", "p90", "p95", "p99", "p99.9", "p99.99")
for _name in _PERCENTILE_KEYS:
    _METRICS[_name] = (_percentile_getter(_name), _SECONDS, False)

# Aliases for the vocabulary --threshold already uses.
_ALIASES = {
    "mean_latency": "avg_latency",
    "p50_latency": "p50",
    "requests_per_sec": "rps",
}

_STEP_PREFIX = "step:"
_STEP_FIELDS = {
    "count": _COUNT,
    "min": _SECONDS,
    "max": _SECONDS,
    "mean": _SECONDS,
    "median": _SECONDS,
    "stdev": _SECONDS,
}


def _split_step_metric(metric: str) -> "tuple[str, str] | None":
    """Split ``step:checkout.p95``-style names into (step name, field)."""
    if not metric.startswith(_STEP_PREFIX):
        return None
    rest = metric[len(_STEP_PREFIX) :]
    if "." not in rest:
        return None
    name, _, field_name = rest.rpartition(".")
    return (name, field_name) if name else None


def metric_unit(metric: str) -> str:
    """Return the unit tag for *metric*, defaulting to a plain count."""
    metric = _ALIASES.get(metric, metric)
    step = _split_step_metric(metric)
    if step is not None:
        return _STEP_FIELDS.get(step[1], _COUNT)
    entry = _METRICS.get(metric)
    return entry[1] if entry else _COUNT


def metric_value(results: dict, metric: str) -> "float | None":
    """Read one metric out of a results dict, or None when it is absent.

    Absent is not zero: a run without latency samples has no p95, and treating
    that as 0 would report a spectacular improvement.
    """
    metric = _ALIASES.get(metric, metric)
    step = _split_step_metric(metric)
    if step is not None:
        return _dig(results, "step_stats", step[0], step[1])
    entry = _METRICS.get(metric)
    return entry[0](results) if entry else None


def known_metrics(results: dict) -> list[str]:
    """List the metrics present in *results*, top-level ones first."""
    names = [name for name in _METRICS if metric_value(results, name) is not None]
    for step_name, fields in (results.get("step_stats") or {}).items():
        names.extend(f"{_STEP_PREFIX}{step_name}.{f}" for f in fields if f in _STEP_FIELDS)
    return names


def is_known_metric(metric: str) -> bool:
    """True if *metric* is addressable, regardless of any particular run."""
    metric = _ALIASES.get(metric, metric)
    if _split_step_metric(metric) is not None:
        return True
    return metric in _METRICS


# ---------------------------------------------------------------------------
# --fail-on expressions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FailOn:
    """One regression rule: the condition under which the gate fails.

    ``p95 > +10%`` means "fail when p95's delta versus the baseline exceeds
    +10%". The comparison is always against the *delta*, never the raw value —
    that is what ``--threshold`` is for.
    """

    metric: str
    operator: str  # "<" or ">"
    amount: float
    relative: bool
    raw: str

    def describe(self) -> str:
        suffix = "%" if self.relative else ""
        return f"{self.metric} {self.operator} {self.amount:+g}{suffix}"


_FAIL_ON_PATTERN = re.compile(
    r"^\s*(?P<metric>[A-Za-z_][A-Za-z0-9_.:]*)"
    r"\s*(?P<op>[<>])\s*"
    r"(?P<sign>[+-])?(?P<value>[0-9]*\.?[0-9]+)\s*"
    r"(?P<unit>%|ms|us|s)?\s*$"
)


def parse_fail_on(expr: str) -> FailOn:
    """Parse a ``--fail-on`` expression such as ``"p95 > +10%"``.

    A ``%`` suffix makes the rule relative to the baseline value; anything else
    is an absolute delta in the metric's own unit (seconds for latency, with
    ``ms``/``us`` accepted and converted).

    Raises:
        ValueError: The expression is malformed or names an unknown metric.
    """
    match = _FAIL_ON_PATTERN.match(expr)
    if not match:
        raise ValueError(
            f"Invalid --fail-on expression: {expr!r}. "
            f"Expected something like 'p95 > +10%' or 'rps < -5%' or 'p99 > +50ms'"
        )
    metric = _ALIASES.get(match.group("metric"), match.group("metric"))
    if not is_known_metric(metric):
        raise ValueError(
            f"Unknown metric {match.group('metric')!r} in --fail-on {expr!r}. "
            f"Known metrics: {', '.join(sorted(_METRICS))}, or step:<name>.<field>"
        )

    amount = float(match.group("value"))
    if match.group("sign") == "-":
        amount = -amount
    unit = match.group("unit")
    relative = unit == "%"

    if not relative:
        unit_kind = metric_unit(metric)
        if unit in ("ms", "us", "s") and unit_kind != _SECONDS:
            raise ValueError(f"Invalid unit {unit!r} for {metric!r} in --fail-on {expr!r}")
        if unit == "ms":
            amount /= 1000.0
        elif unit == "us":
            amount /= 1_000_000.0
        elif unit is None and unit_kind == _SECONDS:
            logger.warning(
                "--fail-on %r has no unit; reading %s as seconds. "
                "Add 'ms' if that is what you meant.",
                expr.strip(),
                match.group("value"),
            )

    return FailOn(
        metric=metric,
        operator=match.group("op"),
        amount=amount,
        relative=relative,
        raw=expr.strip(),
    )


# ---------------------------------------------------------------------------
# Loading result files
# ---------------------------------------------------------------------------


class ResultsError(Exception):
    """Raised when a results file cannot be read or is the wrong shape."""


def _read_one(path: str) -> dict:
    if not os.path.isfile(path):
        raise ResultsError(f"Results file not found: {path}")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except ValueError as exc:
        raise ResultsError(f"{path}: not valid JSON: {exc}") from None
    if not isinstance(data, dict):
        raise ResultsError(f"{path}: expected a JSON object, got {type(data).__name__}")

    # Files written before schema_version existed have today's shape.
    version = data.get("schema_version", SCHEMA_VERSION)
    if not isinstance(version, int) or version > SCHEMA_VERSION:
        raise ResultsError(
            f"{path}: schema_version {version!r} is newer than this pywrkr understands "
            f"(supported: {SCHEMA_VERSION}). Upgrade pywrkr or regenerate the file."
        )
    if "total_requests" not in data:
        raise ResultsError(
            f"{path}: does not look like pywrkr --json output (no 'total_requests' key)"
        )
    return data


def load_results(spec: str) -> dict:
    """Load a single results file."""
    return _read_one(spec)


def load_baseline(spec: str) -> tuple[dict, list[str]]:
    """Load a baseline, averaging when *spec* is a glob matching several files.

    Averaging several runs is the cheap defence against single-run noise: one
    unlucky baseline otherwise makes every later run look like a regression.

    Returns:
        ``(results, source_paths)`` — the averaged results and the files used.

    Raises:
        ResultsError: The glob matches nothing, or a file is unreadable.
    """
    if any(ch in spec for ch in "*?[") and not os.path.isfile(spec):
        paths = sorted(glob.glob(spec))
        if not paths:
            raise ResultsError(f"Baseline pattern matched no files: {spec}")
    else:
        paths = [spec]

    runs = [_read_one(path) for path in paths]
    if len(runs) == 1:
        return runs[0], paths
    return average_results(runs), paths


#: Top-level keys whose numeric leaves are averaged across baseline runs.
_AVERAGED_KEYS = (
    "duration_sec",
    "requests_per_sec",
    "total_requests",
    "total_errors",
    "total_bytes",
    "transfer_per_sec_bytes",
    "content_length_errors",
    "extract_failures",
    "template_errors",
    "latency",
    "percentiles",
    "step_stats",
)


def _average_node(nodes: Sequence[Any]) -> Any:
    """Average matching numeric leaves, walking nested dicts in step."""
    first = nodes[0]
    if isinstance(first, bool):
        return first
    if isinstance(first, (int, float)):
        numbers = [n for n in nodes if isinstance(n, (int, float)) and not isinstance(n, bool)]
        return sum(numbers) / len(numbers) if numbers else first
    if isinstance(first, dict):
        out = {}
        for key in first:
            present = [n[key] for n in nodes if isinstance(n, dict) and key in n]
            out[key] = _average_node(present) if present else first[key]
        return out
    return first


def average_results(runs: Sequence[dict]) -> dict:
    """Average several result docs into one synthetic baseline.

    Only the numeric metrics comparison can address are averaged; everything
    else (timeline, status codes, config snapshot) is taken from the first run,
    which is enough to describe what the baseline was.
    """
    if not runs:
        raise ResultsError("Cannot average an empty set of baseline runs")
    merged = dict(runs[0])
    for key in _AVERAGED_KEYS:
        present = [run[key] for run in runs if key in run]
        if present:
            merged[key] = _average_node(present)
    merged["baseline_runs"] = len(runs)
    return merged


# ---------------------------------------------------------------------------
# Config comparability
# ---------------------------------------------------------------------------

#: Config-snapshot fields that must match for a comparison to mean anything.
_CONFIG_FIELDS = (
    "mode",
    "connections",
    "users",
    "duration",
    "num_requests",
    "rate",
    "url_host",
    # A run that read bodies and one that did not are not measuring the same
    # thing: total_bytes counts only what was read, and a released response's
    # latency excludes receiving it.
    "read_body",
)


def config_differences(baseline: dict, current: dict) -> list[str]:
    """Describe how the two runs' configurations differ.

    Comparing a 10-user run against a 1000-user baseline produces arithmetic
    that is perfectly correct and completely meaningless, so the difference is
    surfaced rather than left for the reader to notice.
    """
    base_cfg = baseline.get("config")
    cur_cfg = current.get("config")
    if not isinstance(base_cfg, dict) or not isinstance(cur_cfg, dict):
        return []  # a pre-snapshot file; nothing to compare

    differences = []
    for name in _CONFIG_FIELDS:
        # A field absent from either side was not recorded, and an unrecorded
        # value cannot differ from anything. Without this, every field added to
        # the snapshot would make older baselines warn about themselves.
        if name not in base_cfg or name not in cur_cfg:
            continue
        before, after = base_cfg.get(name), cur_cfg.get(name)
        if before != after:
            differences.append(f"{name}: baseline {before!r} vs current {after!r}")
    return differences


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------


@dataclass
class MetricDelta:
    """One metric's before/after and the change between them."""

    metric: str
    unit: str
    baseline: "float | None"
    current: "float | None"
    higher_is_better: bool

    @property
    def delta(self) -> "float | None":
        if self.baseline is None or self.current is None:
            return None
        return self.current - self.baseline

    @property
    def delta_pct(self) -> "float | None":
        """Percentage change, or None when the baseline is zero."""
        delta = self.delta
        if delta is None or self.baseline is None or self.baseline == 0:
            return None
        return delta / abs(self.baseline) * 100


@dataclass
class Verdict:
    """The outcome of one --fail-on rule."""

    rule: FailOn
    delta: MetricDelta
    regressed: bool
    reason: str


@dataclass
class ComparisonReport:
    """Everything a comparison produced: the table, the verdicts, the caveats."""

    metrics: list[MetricDelta] = field(default_factory=list)
    verdicts: list[Verdict] = field(default_factory=list)
    config_warnings: list[str] = field(default_factory=list)
    baseline_sources: list[str] = field(default_factory=list)
    baseline_runs: int = 1

    @property
    def regressed(self) -> bool:
        return any(v.regressed for v in self.verdicts)

    @property
    def exit_code(self) -> int:
        return EXIT_REGRESSION if self.regressed else 0


def _build_delta(baseline: dict, current: dict, metric: str) -> MetricDelta:
    canonical = _ALIASES.get(metric, metric)
    step = _split_step_metric(canonical)
    higher_is_better = False
    if step is None:
        entry = _METRICS.get(canonical)
        higher_is_better = entry[2] if entry else False
    return MetricDelta(
        metric=canonical,
        unit=metric_unit(canonical),
        baseline=metric_value(baseline, canonical),
        current=metric_value(current, canonical),
        higher_is_better=higher_is_better,
    )


def _evaluate(rule: FailOn, delta: MetricDelta) -> Verdict:
    before, after = delta.baseline, delta.current
    if before is None or after is None:
        missing = "baseline" if before is None else "current"
        return Verdict(rule, delta, False, f"skipped: {rule.metric} missing from the {missing} run")

    absolute = after - before
    if rule.relative:
        if before == 0:
            return Verdict(
                rule,
                delta,
                False,
                f"skipped: baseline {rule.metric} is 0, so a relative change is undefined",
            )
        observed = absolute / abs(before) * 100
        rendered = f"{observed:+.2f}%"
    else:
        observed = absolute
        rendered = format_value(observed, delta.unit, signed=True)

    regressed = observed > rule.amount if rule.operator == ">" else observed < rule.amount
    verb = "exceeded" if regressed else "within"
    return Verdict(rule, delta, regressed, f"{rendered} {verb} {rule.describe()}")


def compare_results(
    baseline: dict,
    current: dict,
    rules: "Sequence[FailOn] | None" = None,
    baseline_sources: "Sequence[str] | None" = None,
) -> ComparisonReport:
    """Diff two result docs and evaluate the regression rules against it."""
    names = list(dict.fromkeys(known_metrics(baseline) + known_metrics(current)))
    report = ComparisonReport(
        metrics=[_build_delta(baseline, current, name) for name in names],
        config_warnings=config_differences(baseline, current),
        baseline_sources=list(baseline_sources or []),
        baseline_runs=int(baseline.get("baseline_runs", 1) or 1),
    )
    for rule in rules or []:
        report.verdicts.append(_evaluate(rule, _build_delta(baseline, current, rule.metric)))
    return report


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def format_value(value: "float | None", unit: str, signed: bool = False) -> str:
    """Render a metric value in its own unit, compactly and stably."""
    if value is None:
        return "-"
    sign = "+" if signed and value >= 0 else ""
    if unit == _SECONDS:
        magnitude = abs(value)
        if magnitude < 0.001:
            return f"{sign}{value * 1_000_000:.2f}us"
        if magnitude < 1:
            return f"{sign}{value * 1000:.2f}ms"
        return f"{sign}{value:.3f}s"
    if unit == _PERCENT:
        return f"{sign}{value:.2f}%"
    if unit == _BYTES:
        return f"{sign}{value:,.0f}B"
    if unit == _RATE:
        return f"{sign}{value:,.2f}"
    if float(value).is_integer():
        return f"{sign}{int(value):,}"
    return f"{sign}{value:,.2f}"


def _pct_cell(delta: MetricDelta) -> str:
    pct = delta.delta_pct
    return "-" if pct is None else f"{pct:+.2f}%"


def _rows(report: ComparisonReport) -> list[tuple[str, str, str, str, str]]:
    rows = []
    for delta in report.metrics:
        rows.append(
            (
                delta.metric,
                format_value(delta.baseline, delta.unit),
                format_value(delta.current, delta.unit),
                format_value(delta.delta, delta.unit, signed=True),
                _pct_cell(delta),
            )
        )
    return rows


def _render_table(report: ComparisonReport, file: TextIO) -> None:
    headers = ("Metric", "Baseline", "Current", "Delta", "Change")
    rows = _rows(report)
    widths = [
        max(len(headers[i]), max((len(r[i]) for r in rows), default=0)) for i in range(len(headers))
    ]

    print(f"\n{'=' * 70}", file=file)
    print("  BASELINE COMPARISON", file=file)
    print(f"{'=' * 70}", file=file)
    if report.baseline_sources:
        label = ", ".join(report.baseline_sources)
        if report.baseline_runs > 1:
            label = f"{label} (mean of {report.baseline_runs} runs)"
        print(f"  Baseline: {label}", file=file)
    for warning in report.config_warnings:
        print(f"  WARNING: config differs -- {warning}", file=file)
    print(file=file)

    header_line = "  " + "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    print(header_line, file=file)
    print("  " + "  ".join("-" * w for w in widths), file=file)
    for row in rows:
        print("  " + "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)), file=file)

    if report.verdicts:
        print(file=file)
        for verdict in report.verdicts:
            mark = "FAIL" if verdict.regressed else "PASS"
            print(f"  [{mark}] {verdict.rule.raw}: {verdict.reason}", file=file)
        print(file=file)
        if report.regressed:
            failed = sum(1 for v in report.verdicts if v.regressed)
            print(f"  REGRESSION: {failed} of {len(report.verdicts)} rule(s) fired", file=file)
        else:
            print(f"  OK: all {len(report.verdicts)} rule(s) passed", file=file)


def _render_markdown(report: ComparisonReport, file: TextIO) -> None:
    print("### pywrkr baseline comparison\n", file=file)
    if report.baseline_sources:
        label = ", ".join(f"`{s}`" for s in report.baseline_sources)
        if report.baseline_runs > 1:
            label = f"{label} (mean of {report.baseline_runs} runs)"
        print(f"Baseline: {label}\n", file=file)
    for warning in report.config_warnings:
        print(f"> **Warning:** config differs — {warning}\n", file=file)

    print("| Metric | Baseline | Current | Delta | Change |", file=file)
    print("| --- | ---: | ---: | ---: | ---: |", file=file)
    for row in _rows(report):
        print("| " + " | ".join(row) + " |", file=file)

    if report.verdicts:
        print(file=file)
        for verdict in report.verdicts:
            mark = "❌" if verdict.regressed else "✅"
            print(f"- {mark} `{verdict.rule.raw}` — {verdict.reason}", file=file)
        print(file=file)
        print(
            "**Regression detected.**" if report.regressed else "**No regression detected.**",
            file=file,
        )


def _round(value: "float | None") -> "float | None":
    return None if value is None else round(value, 9)


def _render_json(report: ComparisonReport, file: TextIO) -> None:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "regressed": report.regressed,
        "exit_code": report.exit_code,
        "baseline_sources": report.baseline_sources,
        "baseline_runs": report.baseline_runs,
        "config_warnings": report.config_warnings,
        "metrics": [
            {
                "metric": d.metric,
                "unit": d.unit,
                # Rounded so subtraction noise (0.19999999999999998) does not
                # leak into machine-readable output or golden-file tests. Nine
                # places still resolves nanoseconds.
                "baseline": _round(d.baseline),
                "current": _round(d.current),
                "delta": _round(d.delta),
                "delta_pct": (None if d.delta_pct is None else round(d.delta_pct, 6)),
            }
            for d in report.metrics
        ],
        "rules": [
            {
                "expression": v.rule.raw,
                "metric": v.rule.metric,
                "regressed": v.regressed,
                "reason": v.reason,
            }
            for v in report.verdicts
        ],
    }
    json.dump(payload, file, indent=2, allow_nan=False)
    print(file=file)


def render_report(report: ComparisonReport, fmt: str = "table", file: TextIO | None = None) -> None:
    """Write *report* in one of :data:`COMPARE_FORMATS`."""
    out = file if file is not None else sys.stdout
    if fmt == "markdown":
        _render_markdown(report, out)
    elif fmt == "json":
        _render_json(report, out)
    else:
        _render_table(report, out)
