"""Variable templating and response extraction for scenario steps.

Scenario correlation has two halves:

* **Extraction** — once a step's response arrives, its ``extract`` rules pull
  values out of the JSON body, a response header, or a regex capture group and
  bind them to variable names.
* **Substitution** — later steps reference those names as ``${name}`` in their
  path, header names/values, and body.

A placeholder takes one of three shapes, and nothing else:

* ``${name}`` — a variable bound by an ``extract`` rule.
* ``${dataset.field}`` — a column of the data row the virtual user holds for
  this iteration (see :mod:`pywrkr.feeders`).
* ``${func(args)}`` — one of the built-in generators listed in
  ``FUNCTION_ARITY``.

There is deliberately no expression language: no arithmetic, no conditionals,
no nesting. JSONPath support is likewise the dotted subset (``$.a.b[0].c``)
implemented in-house so the package keeps its zero-dependency runtime.
"""

from __future__ import annotations

import datetime
import json
import random
import re
import string
import time
import uuid
from dataclasses import dataclass
from typing import Any, Mapping

__all__ = [
    "EXTRACT_SOURCES",
    "FUNCTION_ARITY",
    "ON_EXTRACT_FAILURE_CHOICES",
    "ON_TEMPLATE_ERROR_CHOICES",
    "ExtractError",
    "Extractor",
    "TemplateError",
    "TemplateFunctions",
    "apply_extractors",
    "compile_extractor",
    "compile_header_extractor",
    "compile_json_extractor",
    "compile_regex_extractor",
    "is_valid_var_name",
    "iter_placeholders",
    "parse_json_path",
    "resolve_json_path",
    "stringify",
    "substitute",
    "substitute_structure",
    "validate_function_call",
]

# A variable name is an identifier: ``${token}``, ``${user_id}``.
VAR_NAME_PATTERN = r"[A-Za-z_][A-Za-z0-9_]*"
_VAR_NAME_RE = re.compile(rf"\A{VAR_NAME_PATTERN}\Z")

# A data-set field is whatever a CSV header holds, minus the characters that
# would make the placeholder itself ambiguous.
_FIELD_PATTERN = r"[^\s{}().]+"

# The three placeholder shapes, tried in this order so that the parentheses of a
# function call and the dot of a data reference are recognised before the plain
# variable form. Anything that matches none of them -- ``${1bad}``, ``$VAR``,
# ``${a.b.c}`` -- is not a placeholder at all and is left untouched.
_PLACEHOLDER_RE = re.compile(
    r"\$\{(?:"
    rf"(?P<func>{VAR_NAME_PATTERN})\((?P<args>[^(){{}}]*)\)"
    rf"|(?P<dataset>{VAR_NAME_PATTERN})\.(?P<field>{_FIELD_PATTERN})"
    rf"|(?P<var>{VAR_NAME_PATTERN})"
    r")\}"
)

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
# Built-in generator functions
# ---------------------------------------------------------------------------

#: Built-in ``${func(...)}`` generators mapped to their (min, max) argument count.
FUNCTION_ARITY: dict[str, tuple[int, int]] = {
    "uuid": (0, 0),
    "randint": (2, 2),
    "randstr": (1, 1),
    "counter": (0, 1),
    "now": (0, 1),
}

_NOW_FORMATS = ("unix",)
_RANDSTR_ALPHABET = string.ascii_letters + string.digits
_DEFAULT_COUNTER = "default"


def _split_args(raw: str) -> list[str]:
    """Split a function call's argument text on commas.

    There is no nesting to worry about: the placeholder grammar rejects
    parentheses inside the argument list.
    """
    raw = raw.strip()
    if not raw:
        return []
    return [arg.strip() for arg in raw.split(",")]


def _require_int(value: str, what: str) -> int:
    try:
        return int(value)
    except ValueError:
        raise ValueError(f"{what} must be an integer, got {value!r}") from None


def validate_function_call(name: str, raw_args: str) -> None:
    """Check a ``${func(...)}`` placeholder without evaluating it.

    Called while the scenario file loads so an unknown function or a nonsense
    argument is a startup error naming the step, not a per-request failure.

    Raises:
        ValueError: The function is unknown, the argument count is wrong, or an
            argument is not usable.
    """
    arity = FUNCTION_ARITY.get(name)
    if arity is None:
        raise ValueError(
            f"unknown function {name}(); available: "
            + ", ".join(f"{n}()" for n in sorted(FUNCTION_ARITY))
        )
    low, high = arity
    args = _split_args(raw_args)
    if not low <= len(args) <= high:
        expected = str(low) if low == high else f"{low}-{high}"
        raise ValueError(f"{name}() takes {expected} argument(s), got {len(args)}")

    if name == "randint":
        low_bound = _require_int(args[0], "randint() low bound")
        high_bound = _require_int(args[1], "randint() high bound")
        if low_bound > high_bound:
            raise ValueError(
                f"randint({low_bound},{high_bound}) has an empty range; "
                f"the low bound must not exceed the high bound"
            )
    elif name == "randstr":
        length = _require_int(args[0], "randstr() length")
        if length < 1:
            raise ValueError(f"randstr() length must be at least 1, got {length}")
    elif name == "counter" and args and not is_valid_var_name(args[0]):
        raise ValueError(f"counter() name must be an identifier, got {args[0]!r}")
    elif name == "now" and args and args[0] not in _NOW_FORMATS:
        raise ValueError(
            f"now() takes no argument or one of {', '.join(_NOW_FORMATS)}, got {args[0]!r}"
        )


class TemplateFunctions:
    """Evaluates ``${func(...)}`` placeholders for one benchmark run.

    A single instance is shared by every virtual user, which is what makes
    ``counter()`` strictly monotonic across the run rather than per user. All
    workers live on one event loop, so a plain dict needs no locking.
    """

    __slots__ = ("_counters",)

    def __init__(self) -> None:
        self._counters: dict[str, int] = {}

    def call(self, name: str, raw_args: str) -> str:
        """Evaluate one call, returning its string expansion.

        Raises:
            TemplateError: The function is unknown or its arguments are unusable.
        """
        try:
            validate_function_call(name, raw_args)
        except ValueError as exc:
            raise TemplateError(str(exc)) from None
        args = _split_args(raw_args)

        if name == "uuid":
            return str(uuid.uuid4())
        if name == "randint":
            return str(random.randint(int(args[0]), int(args[1])))
        if name == "randstr":
            return "".join(random.choices(_RANDSTR_ALPHABET, k=int(args[0])))
        if name == "counter":
            key = args[0] if args else _DEFAULT_COUNTER
            nxt = self._counters.get(key, 0) + 1
            self._counters[key] = nxt
            return str(nxt)
        # now
        if args:
            return str(int(time.time()))
        return datetime.datetime.now(datetime.timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Substitution
# ---------------------------------------------------------------------------


def iter_placeholders(template: str) -> "list[re.Match[str]]":
    """Return every placeholder match in *template*, in order.

    Exposed so scenario loading can validate placeholders against the declared
    data sets and built-in functions before the run starts.
    """
    if "${" not in template:
        return []
    return list(_PLACEHOLDER_RE.finditer(template))


def _resolve(
    match: "re.Match[str]",
    variables: Mapping[str, str],
    rows: "Mapping[str, Mapping[str, str]] | None",
    functions: "TemplateFunctions | None",
    problems: list[str],
) -> str:
    """Expand one placeholder, or record why it could not be expanded.

    Returns the original text when unresolvable, so ``keep_literal`` can leave
    it in place; the caller decides whether *problems* is fatal.
    """
    func = match.group("func")
    if func is not None:
        if functions is None:
            problems.append(f"no function support available for ${{{func}()}}")
            return match.group(0)
        try:
            return functions.call(func, match.group("args"))
        except TemplateError as exc:
            problems.append(str(exc))
            return match.group(0)

    dataset = match.group("dataset")
    if dataset is not None:
        field = match.group("field")
        row = (rows or {}).get(dataset)
        if row is None:
            problems.append(f"no data set {dataset!r} for ${{{dataset}.{field}}}")
            return match.group(0)
        if field not in row:
            available = ", ".join(sorted(row)) or "none"
            problems.append(f"data set {dataset!r} has no field {field!r} (available: {available})")
            return match.group(0)
        return row[field]

    name = match.group("var")
    if name in variables:
        return variables[name]
    problems.append(f"unknown variable ${{{name}}}")
    return match.group(0)


def substitute(
    template: str,
    variables: Mapping[str, str],
    keep_literal: bool = False,
    rows: "Mapping[str, Mapping[str, str]] | None" = None,
    functions: "TemplateFunctions | None" = None,
) -> str:
    """Replace every placeholder in *template* with its expansion.

    Values are inserted verbatim — no URL- or JSON-escaping is applied, so a
    value destined for a query string should already be in the form the target
    expects.

    Args:
        template: The string to render.
        variables: Variables bound by ``extract`` for this user/iteration.
        keep_literal: When True, placeholders that cannot be expanded are left
            in place untouched instead of raising.
        rows: The data row this user holds for this iteration, per data set,
            resolving ``${dataset.field}``.
        functions: Generator functions for this run, resolving ``${func(...)}``.

    Raises:
        TemplateError: A placeholder could not be expanded and *keep_literal*
            is False.
    """
    # Fast path: the overwhelming majority of steps carry no placeholders, and
    # this helper runs on every path/header/body of every request.
    if "${" not in template:
        return template

    problems: list[str] = []

    def _replace(match: "re.Match[str]") -> str:
        return _resolve(match, variables, rows, functions, problems)

    rendered = _PLACEHOLDER_RE.sub(_replace, template)
    if problems and not keep_literal:
        raise TemplateError("; ".join(dict.fromkeys(problems)))
    return rendered


def substitute_structure(
    value: Any,
    variables: Mapping[str, str],
    keep_literal: bool = False,
    rows: "Mapping[str, Mapping[str, str]] | None" = None,
    functions: "TemplateFunctions | None" = None,
) -> Any:
    """Recursively render every string inside a JSON-shaped structure.

    Scenario bodies may be given as JSON objects/arrays rather than raw
    strings; this walks them so placeholders work at any depth, in dict keys as
    well as values.  Non-string leaves are returned unchanged.
    """
    if isinstance(value, str):
        return substitute(value, variables, keep_literal, rows, functions)
    if isinstance(value, dict):
        return {
            (
                substitute(k, variables, keep_literal, rows, functions) if isinstance(k, str) else k
            ): substitute_structure(v, variables, keep_literal, rows, functions)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [
            substitute_structure(item, variables, keep_literal, rows, functions) for item in value
        ]
    return value


# ---------------------------------------------------------------------------
# JSONPath (dotted subset)
# ---------------------------------------------------------------------------

# One path token, in alternation order: a dotted name, a bracketed integer index
# (negative allowed), or a bracketed quoted key. The explanatory comments sit out
# here rather than inline: under re.VERBOSE a bracket inside a pattern comment is
# ignored by Python but read as a character class by some regex analysers.
_PATH_TOKEN_RE = re.compile(
    r"""
      \.(?P<name>[^.\[\]]+)
    | \[(?P<index>-?\d+)\]
    | \[(?P<quote>["'])(?P<key>.*?)(?P=quote)\]
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


def _require_expression(source: str, expr: str) -> None:
    if not isinstance(expr, str) or not expr.strip():
        raise ValueError(f"{source} expression must be a non-empty string")


def compile_json_extractor(var: str, expr: str) -> Extractor:
    """Compile a rule that selects a value with a dotted JSONPath.

    Raises:
        ValueError: The expression is empty or uses unsupported JSONPath syntax.
    """
    _require_expression("json", expr)
    return Extractor(var=var, source="json", expr=expr, path=parse_json_path(expr))


def compile_regex_extractor(var: str, expr: str) -> Extractor:
    """Compile a rule that pulls capture group 1 out of the response body.

    Raises:
        ValueError: The expression is empty, is not a valid regex, or has no
            capture group.
    """
    _require_expression("regex", expr)
    try:
        pattern = re.compile(expr)
    except re.error as exc:
        raise ValueError(f"invalid regex {expr!r}: {exc}") from None
    if pattern.groups < 1:
        raise ValueError(
            f"regex {expr!r} has no capture group; wrap the part to extract in parentheses"
        )
    return Extractor(var=var, source="regex", expr=expr, pattern=pattern)


def compile_header_extractor(var: str, expr: str) -> Extractor:
    """Compile a rule that reads a response header by name.

    Raises:
        ValueError: The header name is empty.
    """
    _require_expression("header", expr)
    return Extractor(var=var, source="header", expr=expr)


# One compiler per source. Keeping them separate means an expression only ever
# reaches the parser that matches its source: a JSONPath is never handed to
# re.compile, which a single `expr` parameter switched on `source` would imply.
_COMPILERS = {
    "json": compile_json_extractor,
    "regex": compile_regex_extractor,
    "header": compile_header_extractor,
}


def compile_extractor(var: str, source: str, expr: str) -> Extractor:
    """Validate and compile a single extraction rule, dispatching on *source*.

    Use this when the source comes from data (a scenario file, a distributed
    payload); call the per-source compiler directly when it is known statically.

    Raises:
        ValueError: The source is unknown, or the expression is malformed
            (bad regex, no capture group, unsupported JSONPath, empty header).
    """
    compiler = _COMPILERS.get(source)
    if compiler is None:
        raise ValueError(
            f"unknown extract source {source!r}; expected one of {', '.join(EXTRACT_SOURCES)}"
        )
    return compiler(var, expr)


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
