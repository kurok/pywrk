"""Variable templating and response extraction for scenario steps.

Scenario correlation has two halves:

* **Extraction** — once a step's response arrives, its ``extract`` rules pull
  values out of the JSON body, a response header, or a regex capture group and
  bind them to variable names.
* **Substitution** — later steps reference those names as ``${name}`` in their
  path, header names/values, and body.

Both halves are deliberately minimal.  ``${name}`` is the entire template
language (no expressions, no logic), and JSONPath support is the dotted subset
(``$.a.b[0].c``) implemented in-house so the package keeps its zero-dependency
runtime.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Mapping

__all__ = [
    "EXTRACT_SOURCES",
    "ON_EXTRACT_FAILURE_CHOICES",
    "ON_TEMPLATE_ERROR_CHOICES",
    "ExtractError",
    "Extractor",
    "TemplateError",
    "apply_extractors",
    "compile_extractor",
    "is_valid_var_name",
    "parse_json_path",
    "resolve_json_path",
    "stringify",
    "substitute",
    "substitute_structure",
]

# A variable name is an identifier: ``${token}``, ``${user_id}``.
VAR_NAME_PATTERN = r"[A-Za-z_][A-Za-z0-9_]*"
_VAR_NAME_RE = re.compile(rf"\A{VAR_NAME_PATTERN}\Z")
_PLACEHOLDER_RE = re.compile(rf"\$\{{({VAR_NAME_PATTERN})\}}")

#: Accepted ``extract`` rule sources; a rule names exactly one of them.
EXTRACT_SOURCES = ("json", "regex", "header")

#: Accepted values for the scenario-level ``on_extract_failure`` option.
ON_EXTRACT_FAILURE_CHOICES = ("abort_iteration", "continue")

#: Accepted values for the scenario-level ``on_template_error`` option.
ON_TEMPLATE_ERROR_CHOICES = ("abort_iteration", "keep_literal")


class TemplateError(Exception):
    """Raised when a ``${var}`` placeholder cannot be resolved."""


class ExtractError(Exception):
    """Raised when an ``extract`` rule cannot produce a value."""


def is_valid_var_name(name: object) -> bool:
    """Return True if *name* can be referenced as ``${name}``."""
    return isinstance(name, str) and _VAR_NAME_RE.match(name) is not None


# ---------------------------------------------------------------------------
# Substitution
# ---------------------------------------------------------------------------


def substitute(template: str, variables: Mapping[str, str], keep_literal: bool = False) -> str:
    """Replace every ``${name}`` in *template* with its value from *variables*.

    Values are inserted verbatim — no URL- or JSON-escaping is applied, so a
    variable destined for a query string should already be in the form the
    target expects.

    Args:
        template: The string to render.
        variables: Variable bindings for the current virtual user/iteration.
        keep_literal: When True, unknown placeholders are left in place
            untouched instead of raising.

    Raises:
        TemplateError: A placeholder names a variable that is not bound and
            *keep_literal* is False.
    """
    # Fast path: the overwhelming majority of steps carry no placeholders, and
    # this helper runs on every path/header/body of every request.
    if "${" not in template:
        return template

    missing: list[str] = []

    def _replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in variables:
            return variables[name]
        missing.append(name)
        return match.group(0)

    rendered = _PLACEHOLDER_RE.sub(_replace, template)
    if missing and not keep_literal:
        unique = list(dict.fromkeys(missing))
        plural = "s" if len(unique) > 1 else ""
        names = ", ".join(f"${{{name}}}" for name in unique)
        raise TemplateError(f"unknown variable{plural} {names}")
    return rendered


def substitute_structure(
    value: Any, variables: Mapping[str, str], keep_literal: bool = False
) -> Any:
    """Recursively render every string inside a JSON-shaped structure.

    Scenario bodies may be given as JSON objects/arrays rather than raw
    strings; this walks them so ``${var}`` works at any depth, in dict keys as
    well as values.  Non-string leaves are returned unchanged.
    """
    if isinstance(value, str):
        return substitute(value, variables, keep_literal)
    if isinstance(value, dict):
        return {
            substitute(k, variables, keep_literal) if isinstance(k, str) else k: (
                substitute_structure(v, variables, keep_literal)
            )
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [substitute_structure(item, variables, keep_literal) for item in value]
    return value


# ---------------------------------------------------------------------------
# JSONPath (dotted subset)
# ---------------------------------------------------------------------------

_PATH_TOKEN_RE = re.compile(
    r"""
      \.(?P<name>[^.\[\]]+)                        # .name
    | \[(?P<index>-?\d+)\]                         # [0] or [-1]
    | \[(?P<quote>["'])(?P<key>.*?)(?P=quote)\]    # ["quoted.key"] / ['key']
    """,
    re.VERBOSE,
)

_SUPPORTED_SYNTAX = 'supported forms: $.name, $.a.b, $.items[0], $["quoted key"]'


def parse_json_path(expr: str) -> tuple[str | int, ...]:
    """Parse a dotted JSONPath expression into a tuple of segments.

    Object keys become strings and array indices become ints, so ``$.a[1].b``
    yields ``("a", 1, "b")``.  A bare ``$`` yields ``()``, meaning "the whole
    document".

    Raises:
        ValueError: The expression uses syntax outside the supported subset
            (filters, wildcards, recursive descent, slices).
    """
    raw = expr.strip()
    if not raw:
        raise ValueError("JSONPath expression is empty")
    if raw.startswith("$"):
        raw = raw[1:]
    # Tolerate the leading dot being omitted: "data.id" == "$.data.id".
    if raw and raw[0] not in ".[":
        raw = "." + raw

    segments: list[str | int] = []
    pos = 0
    while pos < len(raw):
        match = _PATH_TOKEN_RE.match(raw, pos)
        if match is None:
            raise ValueError(
                f"unsupported JSONPath syntax in {expr!r} at offset {pos}; {_SUPPORTED_SYNTAX}"
            )
        if match.group("name") is not None:
            segments.append(match.group("name"))
        elif match.group("index") is not None:
            segments.append(int(match.group("index")))
        else:
            segments.append(match.group("key"))
        pos = match.end()
    return tuple(segments)


def _describe_path(segments: tuple[str | int, ...] | list[str | int]) -> str:
    """Render already-traversed segments back into JSONPath notation."""
    out = "$"
    for seg in segments:
        out += f"[{seg}]" if isinstance(seg, int) else f".{seg}"
    return out


def resolve_json_path(document: Any, segments: tuple[str | int, ...], expr: str = "$") -> Any:
    """Walk *segments* into *document* and return the selected value.

    Raises:
        ExtractError: A segment does not exist, or the document has the wrong
            shape at that point (indexing an object, keying into an array).
    """
    current = document
    for i, seg in enumerate(segments):
        at = _describe_path(segments[:i])
        if isinstance(seg, int):
            if not isinstance(current, (list, tuple)):
                raise ExtractError(
                    f"{expr}: expected an array at {at}, got {type(current).__name__}"
                )
            try:
                current = current[seg]
            except IndexError:
                raise ExtractError(
                    f"{expr}: index {seg} out of range at {at} (length {len(current)})"
                ) from None
        else:
            if not isinstance(current, dict):
                raise ExtractError(
                    f"{expr}: expected an object at {at}, got {type(current).__name__}"
                )
            if seg not in current:
                raise ExtractError(f"{expr}: key {seg!r} not found at {at}")
            current = current[seg]
    return current


def stringify(value: Any) -> str:
    """Render an extracted JSON value as the string a ``${var}`` expands to.

    Scalars keep their JSON spelling (``true``, not ``True``) so a value that
    was extracted from one payload can be substituted into the next without
    surprising the server.  Objects and arrays are re-serialized compactly,
    which makes it possible to carry a whole sub-document between steps.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(value, separators=(",", ":"))


# ---------------------------------------------------------------------------
# Extractors
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Extractor:
    """One compiled ``extract`` rule, bound to a single variable name.

    Regexes are compiled and JSONPaths parsed at scenario-load time so a typo
    surfaces as a validation error before the benchmark starts rather than as a
    per-request failure once it is under way.
    """

    var: str
    source: str  # one of EXTRACT_SOURCES
    expr: str  # raw expression, retained for error messages and round-tripping
    path: tuple[str | int, ...] | None = None  # parsed, when source == "json"
    pattern: re.Pattern[str] | None = None  # compiled, when source == "regex"


def compile_extractor(var: str, source: str, expr: str) -> Extractor:
    """Validate and compile a single extraction rule.

    Raises:
        ValueError: The source is unknown, or the expression is malformed
            (bad regex, no capture group, unsupported JSONPath, empty header).
    """
    if source not in EXTRACT_SOURCES:
        raise ValueError(
            f"unknown extract source {source!r}; expected one of {', '.join(EXTRACT_SOURCES)}"
        )
    if not isinstance(expr, str) or not expr.strip():
        raise ValueError(f"{source} expression must be a non-empty string")

    if source == "json":
        return Extractor(var=var, source=source, expr=expr, path=parse_json_path(expr))
    if source == "regex":
        try:
            pattern = re.compile(expr)
        except re.error as exc:
            raise ValueError(f"invalid regex {expr!r}: {exc}") from None
        if pattern.groups < 1:
            raise ValueError(
                f"regex {expr!r} has no capture group; wrap the part to extract in parentheses"
            )
        return Extractor(var=var, source=source, expr=expr, pattern=pattern)
    return Extractor(var=var, source=source, expr=expr)


_JSON_CACHE_KEY = "json"
_TEXT_CACHE_KEY = "text"


def _response_text(body: bytes | None, cache: dict[str, Any]) -> str:
    if _TEXT_CACHE_KEY not in cache:
        if body is None:
            raise ExtractError("response body was not captured")
        cache[_TEXT_CACHE_KEY] = body.decode("utf-8", errors="replace")
    return cache[_TEXT_CACHE_KEY]


def _response_json(body: bytes | None, cache: dict[str, Any]) -> Any:
    if _JSON_CACHE_KEY not in cache:
        text = _response_text(body, cache)
        try:
            cache[_JSON_CACHE_KEY] = json.loads(text)
        except ValueError as exc:
            raise ExtractError(f"response body is not valid JSON: {exc}") from None
    return cache[_JSON_CACHE_KEY]


def _apply_one(
    rule: Extractor,
    body: bytes | None,
    headers: Mapping[str, str] | None,
    cache: dict[str, Any],
) -> str:
    if rule.source == "header":
        value = headers.get(rule.expr) if headers is not None else None
        if value is None:
            raise ExtractError(f"response has no header {rule.expr!r}")
        return value

    if rule.source == "json":
        selected = resolve_json_path(_response_json(body, cache), rule.path or (), rule.expr)
        if selected is None:
            raise ExtractError(f"{rule.expr} resolved to null")
        return stringify(selected)

    # regex — compile_extractor guarantees a pattern with >= 1 capture group.
    pattern = rule.pattern
    if pattern is None:  # pragma: no cover - unreachable via compile_extractor
        raise ExtractError(f"regex rule {rule.expr!r} was not compiled")
    match = pattern.search(_response_text(body, cache))
    if match is None:
        raise ExtractError(f"regex {rule.expr!r} did not match the response body")
    captured = match.group(1)
    if captured is None:
        raise ExtractError(f"regex {rule.expr!r} matched but capture group 1 is unset")
    return captured


def apply_extractors(
    extractors: Mapping[str, Extractor],
    body: bytes | None,
    headers: Mapping[str, str] | None,
) -> tuple[dict[str, str], list[str]]:
    """Run every rule against one response.

    Rules are independent: a failing rule contributes a message to *failures*
    and the remaining rules still run, so ``on_extract_failure: continue``
    keeps whatever could be extracted instead of discarding the batch.

    Returns:
        ``(values, failures)`` — the variables that resolved, and one
        ``"<var>: <reason>"`` message per rule that did not.
    """
    values: dict[str, str] = {}
    failures: list[str] = []
    # Parsed body shared across rules so a step with five JSONPaths decodes once.
    cache: dict[str, Any] = {}
    for name, rule in extractors.items():
        try:
            values[name] = _apply_one(rule, body, headers, cache)
        except ExtractError as exc:
            failures.append(f"{name}: {exc}")
    return values, failures
