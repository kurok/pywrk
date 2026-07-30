"""Per-step response assertions for scenarios.

``assert_status`` alone lets a load test pass while the API returns
well-formed garbage — a classic false green. These rules check the things that
actually say the response was correct: payload shape and values, headers, body
patterns, and a per-request latency bound.

Everything is compiled when the scenario file loads, so a bad regex, an
unsupported JSONPath, or a nonsense duration is a startup error naming the
step rather than a failure repeated once per request. The JSONPath subset is
the one :mod:`pywrkr.templating` already implements, so extraction and
assertions never disagree about what ``$.a.b[0]`` means.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Mapping

from pywrkr.templating import ExtractError, parse_json_path, resolve_json_path, stringify

__all__ = [
    "ANY_VALUE",
    "HeaderAssertion",
    "JsonAssertion",
    "StepAssertions",
    "AssertionFailure",
    "evaluate_assertions",
    "parse_duration",
    "parse_step_assertions",
]

#: ``assert_json`` value meaning "this path must exist, any value will do".
ANY_VALUE = "*"

_DURATION_PATTERN = re.compile(r"\A\s*([0-9]*\.?[0-9]+)\s*(ms|us|s)?\s*\Z")


def parse_duration(raw: object, where: str) -> float:
    """Parse ``500ms`` / ``1.5s`` / ``250us`` into seconds.

    A bare number is read as seconds, matching ``--threshold``, but warns
    through the caller's error message if it looks like a mistake is likely.

    Raises:
        ValueError: The value is not a positive duration.
    """
    if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
        raise ValueError(f"{where} must be a duration like '500ms', got {type(raw).__name__}")
    if isinstance(raw, (int, float)):
        value, unit = float(raw), "s"
    else:
        match = _DURATION_PATTERN.match(raw)
        if not match:
            raise ValueError(
                f"{where} must be a duration like '500ms', '1.5s' or '250us', got {raw!r}"
            )
        value, unit = float(match.group(1)), (match.group(2) or "s")
    if value <= 0:
        raise ValueError(f"{where} must be greater than zero, got {raw!r}")
    if unit == "ms":
        return value / 1000.0
    if unit == "us":
        return value / 1_000_000.0
    return value


@dataclass(frozen=True)
class JsonAssertion:
    """One ``assert_json`` rule: a path plus what is expected there."""

    expr: str
    path: tuple
    expected: Any  # ANY_VALUE, or the value the path must equal


@dataclass(frozen=True)
class HeaderAssertion:
    """One ``assert_header`` rule: exact match, or a regex."""

    name: str
    expected: "str | None" = None
    pattern: "re.Pattern[str] | None" = None

    def describe(self) -> str:
        return f"~ {self.pattern.pattern}" if self.pattern else f"== {self.expected!r}"


@dataclass
class StepAssertions:
    """Every assertion attached to one scenario step."""

    status: "int | None" = None
    body_contains: "str | None" = None
    body_regex: "re.Pattern[str] | None" = None
    json_rules: tuple[JsonAssertion, ...] = ()
    header_rules: tuple[HeaderAssertion, ...] = ()
    max_latency: "float | None" = None

    @property
    def needs_body(self) -> bool:
        """True when a rule has to look at the response body."""
        return bool(self.body_contains or self.body_regex or self.json_rules)

    @property
    def any(self) -> bool:
        """True when the step asserts anything at all."""
        return bool(
            self.status is not None
            or self.needs_body
            or self.header_rules
            or self.max_latency is not None
        )


# ---------------------------------------------------------------------------
# Parsing / validation
# ---------------------------------------------------------------------------


def _parse_json_rules(raw: object, where: str) -> tuple[JsonAssertion, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, dict):
        raise ValueError(f"{where} 'assert_json' must be an object, got {type(raw).__name__}")
    rules = []
    for expr, expected in raw.items():
        if not isinstance(expr, str):
            raise ValueError(f"{where} 'assert_json' keys must be JSONPath strings")
        if isinstance(expected, (dict, list)):
            raise ValueError(
                f"{where} assert_json {expr!r}: expected value must be a scalar or "
                f'"{ANY_VALUE}", got {type(expected).__name__}'
            )
        try:
            path = parse_json_path(expr)
        except ValueError as exc:
            raise ValueError(f"{where} assert_json {expr!r}: {exc}") from None
        rules.append(JsonAssertion(expr=expr, path=path, expected=expected))
    return tuple(rules)


def _parse_header_rules(raw: object, where: str) -> tuple[HeaderAssertion, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, dict):
        raise ValueError(f"{where} 'assert_header' must be an object, got {type(raw).__name__}")
    rules = []
    for name, expected in raw.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"{where} 'assert_header' keys must be header names")
        if isinstance(expected, dict):
            unknown = [k for k in expected if k != "regex"]
            if unknown or "regex" not in expected:
                raise ValueError(
                    f"{where} assert_header {name!r}: an object form must be "
                    f'{{"regex": "..."}}, got keys {sorted(expected)}'
                )
            pattern_text = expected["regex"]
            if not isinstance(pattern_text, str):
                raise ValueError(f"{where} assert_header {name!r}: 'regex' must be a string")
            try:
                pattern = re.compile(pattern_text)
            except re.error as exc:
                raise ValueError(
                    f"{where} assert_header {name!r}: invalid regex {pattern_text!r}: {exc}"
                ) from None
            rules.append(HeaderAssertion(name=name, pattern=pattern))
            continue
        if isinstance(expected, (list, bool)) or expected is None:
            raise ValueError(
                f"{where} assert_header {name!r}: expected a string or "
                f'{{"regex": "..."}}, got {type(expected).__name__}'
            )
        rules.append(HeaderAssertion(name=name, expected=str(expected)))
    return tuple(rules)


def parse_step_assertions(step_data: Mapping[str, Any], where: str) -> StepAssertions:
    """Compile every assertion on one scenario step.

    Raises:
        ValueError: Any rule is malformed. Reported here, at load time.
    """
    status = step_data.get("assert_status")
    if status is not None and (isinstance(status, bool) or not isinstance(status, int)):
        raise ValueError(f"{where} 'assert_status' must be an integer, got {type(status).__name__}")

    contains = step_data.get("assert_body_contains")
    if contains is not None and not isinstance(contains, str):
        raise ValueError(
            f"{where} 'assert_body_contains' must be a string, got {type(contains).__name__}"
        )

    body_regex = None
    raw_regex = step_data.get("assert_body_regex")
    if raw_regex is not None:
        if not isinstance(raw_regex, str):
            raise ValueError(
                f"{where} 'assert_body_regex' must be a string, got {type(raw_regex).__name__}"
            )
        try:
            body_regex = re.compile(raw_regex)
        except re.error as exc:
            raise ValueError(
                f"{where} 'assert_body_regex': invalid regex {raw_regex!r}: {exc}"
            ) from None

    max_latency = None
    if step_data.get("assert_max_latency") is not None:
        max_latency = parse_duration(
            step_data["assert_max_latency"], f"{where} 'assert_max_latency'"
        )

    return StepAssertions(
        status=status,
        body_contains=contains,
        body_regex=body_regex,
        json_rules=_parse_json_rules(step_data.get("assert_json"), where),
        header_rules=_parse_header_rules(step_data.get("assert_header"), where),
        max_latency=max_latency,
    )


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def _json_matches(actual: Any, expected: Any) -> bool:
    """Compare an extracted JSON value against the expected one.

    Numbers compare numerically (``42`` matches ``42.0``); everything else
    falls back to comparing JSON spellings, so ``"42"`` in the file also
    matches a numeric ``42`` in the payload rather than failing on a type the
    user could not see.
    """
    # Before anything else: in Python ``True == 1``, so a boolean payload value
    # would silently satisfy an expected 1 (and vice versa). JSON treats them as
    # different types and so should the assertion.
    if isinstance(actual, bool) != isinstance(expected, bool):
        return False
    if actual == expected:
        return True
    if isinstance(actual, (int, float)) and isinstance(expected, (int, float)):
        return float(actual) == float(expected)
    return stringify(actual) == stringify(expected)


@dataclass(frozen=True)
class AssertionFailure:
    """One broken rule: a stable key for aggregation, plus the observed detail.

    The key is derived only from the rule, never from what came back. Folding an
    observed latency or payload value into it would mint a fresh key on every
    request and blow through the error-breakdown's key cap within seconds,
    turning the one view that should aggregate into noise. The observation goes
    in *detail*, which is logged rather than counted.
    """

    key: str
    detail: str = ""

    @property
    def message(self) -> str:
        return f"{self.key} ({self.detail})" if self.detail else self.key


def evaluate_assertions(
    rules: StepAssertions,
    status: int,
    body: "bytes | None",
    headers: "Mapping[str, str] | None",
    latency: float,
) -> list[AssertionFailure]:
    """Check one response, returning one entry per failed assertion."""
    failures: list[AssertionFailure] = []

    if rules.status is not None and status != rules.status:
        # Status codes are a small bounded set, so the observed one is safe in
        # the key and genuinely useful there.
        failures.append(AssertionFailure(f"AssertStatus: expected {rules.status}, got {status}"))

    if rules.max_latency is not None and latency > rules.max_latency:
        failures.append(
            AssertionFailure(
                f"AssertMaxLatency: over {rules.max_latency * 1000:.1f}ms",
                f"took {latency * 1000:.1f}ms",
            )
        )

    for header_rule in rules.header_rules:
        value = headers.get(header_rule.name) if headers is not None else None
        if value is None:
            failures.append(AssertionFailure(f"AssertHeader: {header_rule.name} missing"))
        elif (
            (header_rule.pattern.search(value) is None)
            if header_rule.pattern
            else (value != header_rule.expected)
        ):
            failures.append(
                AssertionFailure(
                    f"AssertHeader: {header_rule.name} {header_rule.describe()}",
                    f"was {value!r}",
                )
            )

    if not rules.needs_body:
        return failures

    text: "str | None" = None
    if body is not None:
        text = body.decode("utf-8", errors="replace")

    if rules.body_contains is not None:
        if text is None or rules.body_contains not in text:
            failures.append(AssertionFailure(f"AssertBody: '{rules.body_contains}' not found"))

    if rules.body_regex is not None:
        if text is None or not rules.body_regex.search(text):
            failures.append(
                AssertionFailure(f"AssertBodyRegex: {rules.body_regex.pattern!r} did not match")
            )

    if rules.json_rules:
        document: Any = None
        parse_error: "str | None" = None
        if text is None:
            parse_error = "response body was not captured"
        else:
            try:
                document = json.loads(text)
            except ValueError:
                parse_error = "response body is not valid JSON"
        for json_rule in rules.json_rules:
            if parse_error is not None:
                failures.append(AssertionFailure(f"AssertJson: {json_rule.expr}: {parse_error}"))
                continue
            try:
                actual = resolve_json_path(document, json_rule.path, json_rule.expr)
            except ExtractError as exc:
                failures.append(AssertionFailure(f"AssertJson: {exc}"))
                continue
            if json_rule.expected == ANY_VALUE:
                continue
            if not _json_matches(actual, json_rule.expected):
                failures.append(
                    AssertionFailure(
                        f"AssertJson: {json_rule.expr} != {json_rule.expected!r}",
                        f"was {actual!r}",
                    )
                )

    return failures
