"""CI summaries: turn a results file into a job summary or PR comment.

The GitHub Action needs three things from a finished run — a markdown table, a
verdict, and a handful of machine-readable outputs. Doing that in inline YAML
bash would put the part most likely to be wrong in the part hardest to test, so
it lives here and the action just calls ``pywrkr summary``.

Thresholds are re-evaluated from the results file rather than re-run, using the
same metric vocabulary :mod:`pywrkr.compare` uses, so a summary and a gate can
never disagree about what ``p95`` means.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from pywrkr.compare import ComparisonReport, format_value, metric_unit, metric_value
from pywrkr.config import Threshold
from pywrkr.reporting import compare_threshold

__all__ = [
    "COMMENT_MARKER",
    "ThresholdOutcome",
    "evaluate_from_results",
    "find_marker_comment",
    "load_results",
    "render_markdown",
    "summary_outputs",
    "upsert_pr_comment",
]

#: Hidden marker that lets the action find and edit its own comment instead of
#: adding a new one on every push. Comment spam is the usual reason a bot like
#: this gets uninstalled.
COMMENT_MARKER = "<!-- pywrkr-performance-report -->"

#: Rows shown in the headline table, as (label, metric name).
_SUMMARY_ROWS = (
    ("Requests", "total_requests"),
    ("Errors", "total_errors"),
    ("Error rate", "error_rate"),
    # The unit rides in the label: format_value renders a rate bare, which
    # reads as ambiguous next to a label that does not name the unit.
    ("Throughput (req/s)", "rps"),
    ("Latency p50", "p50"),
    ("Latency p95", "p95"),
    ("Latency p99", "p99"),
    ("Latency max", "max_latency"),
)


@dataclass(frozen=True)
class ThresholdOutcome:
    """One threshold re-evaluated against a results file."""

    threshold: Threshold
    actual: "float | None"
    passed: bool

    @property
    def expression(self) -> str:
        return self.threshold.raw_expr


def evaluate_from_results(
    results: dict, thresholds: "Iterable[Threshold] | None"
) -> list[ThresholdOutcome]:
    """Check thresholds against an already-written results file.

    A metric the run did not produce counts as a failure rather than a pass.
    This deliberately differs from :func:`pywrkr.reporting.evaluate_thresholds`,
    which substitutes 0.0 for a metric it cannot find -- a run that measured
    nothing would satisfy ``p95 < 500ms``. A gate that silently succeeds
    because its metric is missing is worse than no gate.
    """
    outcomes: list[ThresholdOutcome] = []
    for threshold in thresholds or ():
        actual = metric_value(results, threshold.metric)
        passed = actual is not None and compare_threshold(
            actual, threshold.operator, threshold.value
        )
        outcomes.append(ThresholdOutcome(threshold=threshold, actual=actual, passed=passed))
    return outcomes


def summary_outputs(results: dict, outcomes: "Sequence[ThresholdOutcome]") -> dict[str, str]:
    """Machine-readable values for downstream workflow steps."""

    def _fmt(metric: str, digits: int = 4) -> str:
        value = metric_value(results, metric)
        return "" if value is None else f"{value:.{digits}f}"

    return {
        "p50": _fmt("p50"),
        "p95": _fmt("p95"),
        "p99": _fmt("p99"),
        "rps": _fmt("rps", 2),
        "error_rate": _fmt("error_rate", 4),
        "total_requests": str(int(metric_value(results, "total_requests") or 0)),
        "passed": "true" if all(o.passed for o in outcomes) else "false",
    }


def _row(label: str, results: dict, metric: str) -> "tuple[str, str] | None":
    value = metric_value(results, metric)
    if value is None:
        return None
    return label, format_value(value, metric_unit(metric))


def render_markdown(
    results: dict,
    outcomes: "Sequence[ThresholdOutcome]" = (),
    comparison: "ComparisonReport | None" = None,
    *,
    title: str = "pywrkr performance report",
    target: "str | None" = None,
    include_marker: bool = False,
) -> str:
    """Render the job summary / PR comment body.

    *include_marker* prepends the hidden marker the action looks for when it
    decides whether to edit its previous comment.
    """
    lines: list[str] = []
    if include_marker:
        lines.append(COMMENT_MARKER)
    lines.append(f"## {title}")
    lines.append("")

    config = results.get("config") or {}
    descriptors = [d for d in (target or config.get("url_host"), config.get("mode")) if d]
    if config.get("users"):
        descriptors.append(f"{config['users']} users")
    elif config.get("connections"):
        descriptors.append(f"{config['connections']} connections")
    if config.get("duration"):
        descriptors.append(f"{config['duration']:g}s")
    if descriptors:
        lines.append("`" + "` · `".join(str(d) for d in descriptors) + "`")
        lines.append("")

    lines.append("| Metric | Value |")
    lines.append("| --- | ---: |")
    for label, metric in _SUMMARY_ROWS:
        row = _row(label, results, metric)
        if row is not None:
            lines.append(f"| {row[0]} | {row[1]} |")

    if outcomes:
        lines.append("")
        lines.append("### Thresholds")
        lines.append("")
        for outcome in outcomes:
            mark = "✅" if outcome.passed else "❌"
            unit = metric_unit(outcome.threshold.metric)
            actual = (
                "not measured" if outcome.actual is None else format_value(outcome.actual, unit)
            )
            lines.append(f"- {mark} `{outcome.expression}` — actual {actual}")

    if comparison is not None:
        lines.append("")
        lines.append("### Compared to baseline")
        lines.append("")
        if comparison.baseline_sources:
            label = ", ".join(f"`{s}`" for s in comparison.baseline_sources)
            if comparison.baseline_runs > 1:
                label += f" (mean of {comparison.baseline_runs} runs)"
            lines.append(f"Baseline: {label}")
            lines.append("")
        for warning in comparison.config_warnings:
            lines.append(f"> **Warning:** config differs — {warning}")
            lines.append("")
        lines.append("| Metric | Baseline | Current | Change |")
        lines.append("| --- | ---: | ---: | ---: |")
        for delta in comparison.metrics:
            pct = delta.delta_pct
            lines.append(
                f"| {delta.metric} "
                f"| {format_value(delta.baseline, delta.unit)} "
                f"| {format_value(delta.current, delta.unit)} "
                f"| {'-' if pct is None else f'{pct:+.2f}%'} |"
            )
        if comparison.verdicts:
            lines.append("")
            for verdict in comparison.verdicts:
                mark = "❌" if verdict.regressed else "✅"
                lines.append(f"- {mark} `{verdict.rule.raw}` — {verdict.reason}")

    lines.append("")
    lines.append(_verdict_line(outcomes, comparison))
    lines.append("")
    return "\n".join(lines)


def _verdict_line(
    outcomes: "Sequence[ThresholdOutcome]", comparison: "ComparisonReport | None"
) -> str:
    """The one line a reviewer actually reads."""
    breached = [o for o in outcomes if not o.passed]
    regressed = bool(comparison is not None and comparison.regressed)
    if breached and regressed:
        return f"**Failed:** {len(breached)} threshold(s) breached and a regression was detected."
    if breached:
        return f"**Failed:** {len(breached)} threshold(s) breached."
    if regressed:
        return "**Failed:** performance regressed against the baseline."
    if outcomes or comparison is not None:
        return "**Passed:** all checks green."
    return "_No thresholds or baseline configured — reporting only._"


def load_results(path: str) -> dict:
    """Read a results file written by ``--json``."""
    with open(path, "r", encoding="utf-8") as handle:
        data: Any = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a JSON object, got {type(data).__name__}")
    return data


# -- PR comments -----------------------------------------------------------
#
# Lives here rather than in the action's shell so the part that decides
# "edit or create" is unit-testable. The HTTP call is injected, so the
# decision can be tested without a network or a token.


def find_marker_comment(comments: "Iterable[dict]", marker: str = COMMENT_MARKER) -> "int | None":
    """Return the id of the newest comment carrying *marker*, if any.

    Newest rather than first: if an earlier run's comment was manually
    deleted-and-reposted, or two comments somehow both carry the marker, the
    most recent one is the one a reader is looking at.
    """
    found: "int | None" = None
    for comment in comments:
        body = comment.get("body")
        if isinstance(body, str) and marker in body:
            comment_id = comment.get("id")
            if isinstance(comment_id, int):
                found = comment_id
    return found


def _github_request(
    method: str, url: str, token: str, payload: "dict | None" = None
) -> "list | dict":
    """Minimal GitHub REST call. No third-party dependency on purpose."""
    import urllib.error
    import urllib.request

    if not url.startswith("https://"):
        raise ValueError(f"refusing a non-HTTPS GitHub API URL: {url}")
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    # The https:// scheme is enforced above, so the audit_url_open finding
    # (an attacker-supplied file:// or custom scheme) cannot apply here.
    with urllib.request.urlopen(request, timeout=30) as response:  # nosec B310
        body = response.read().decode("utf-8")
    return json.loads(body) if body else {}


def upsert_pr_comment(
    repo: str,
    issue_number: int,
    body: str,
    *,
    token: str,
    api_url: str = "https://api.github.com",
    marker: str = COMMENT_MARKER,
    request: Any = None,
) -> str:
    """Edit this action's previous comment, or post the first one.

    Returns ``"updated"`` or ``"created"``. Editing in place is not a nicety:
    a bot that appends a fresh comment on every push is the usual reason a
    performance action gets uninstalled.
    """
    call = request if request is not None else _github_request
    base = f"{api_url.rstrip('/')}/repos/{repo}/issues/{issue_number}/comments"
    if marker not in body:
        body = f"{marker}\n{body}"

    existing = call("GET", f"{base}?per_page=100", token, None)
    comment_id = find_marker_comment(existing if isinstance(existing, list) else [], marker)
    if comment_id is not None:
        call(
            "PATCH",
            f"{api_url.rstrip('/')}/repos/{repo}/issues/comments/{comment_id}",
            token,
            {"body": body},
        )
        return "updated"
    call("POST", base, token, {"body": body})
    return "created"
