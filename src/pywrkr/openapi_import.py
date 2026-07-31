"""Generate a load-test scenario from an OpenAPI document.

``har-import`` covers "I can click through the app in a browser". For an
API-first service there is often no browser flow to record, but an OpenAPI
document already exists -- FastAPI and most modern frameworks publish one for
free. This is the API-first twin: point pywrkr at a spec and get a runnable
scenario skeleton.

The design principle throughout is **be honest about what cannot be inferred**.
A spec says an endpoint takes a ``user_id``; it rarely says which user ids
exist. Guessing produces a scenario that benchmarks a 404 handler, which is
worse than useless because it looks like it worked. So every value that cannot
be read out of the schema becomes a ``${placeholder}`` and is listed in a
"needs input" report, and credentials are never invented.

Two more choices follow from the same principle:

* **Only safe methods by default.** Generating a scenario that DELETEs its way
  through an API because the spec documented the endpoint is not a helpful
  default. Mutating methods require naming them.
* **A bounded in-house reader**, not ``openapi-core``. What is needed here is
  example extraction and single-document ``$ref`` resolution, and a validating
  spec library would add a heavy dependency to a load-testing tool for the
  parts of the spec this never looks at.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

__all__ = [
    "MUTATING_METHODS",
    "OpenApiImportConfig",
    "Operation",
    "SAFE_METHODS",
    "convert_openapi",
    "load_spec",
    "openapi_to_scenario",
    "openapi_to_url_file",
    "select_operations",
]

#: Methods generated unless the user asks for more. Benchmarking a DELETE
#: endpoint has to be a conscious choice, not something a spec walk decides.
SAFE_METHODS = ("GET", "HEAD")

MUTATING_METHODS = ("POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE")

_ALL_METHODS = tuple(SAFE_METHODS) + MUTATING_METHODS

#: How deep to follow $ref chains before calling it a cycle.
_MAX_REF_DEPTH = 32

#: Stand-in values by JSON Schema type, used only when the schema offers no
#: example, default or enum of its own.
_TYPE_STUBS: dict[str, Any] = {
    "string": "string",
    "integer": 1,
    "number": 1.0,
    "boolean": True,
    "array": [],
    "object": {},
}

#: Formats worth a better stub than the bare type gives.
_FORMAT_STUBS: dict[str, str] = {
    "date": "2024-01-01",
    "date-time": "2024-01-01T00:00:00Z",
    "uuid": "${uuid()}",
    "email": "user@example.com",
    "uri": "https://example.com",
    "hostname": "example.com",
    "ipv4": "192.0.2.1",
}


class SpecError(ValueError):
    """A spec that cannot be read, with a pointer to where it went wrong."""


@dataclass
class OpenApiImportConfig:
    """What to select from the spec and how to render it."""

    methods: tuple[str, ...] = SAFE_METHODS
    include_patterns: list[str] = field(default_factory=list)
    exclude_patterns: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    base_url: "str | None" = None
    assert_status: bool = False
    think_time: float = 0.0


@dataclass
class Operation:
    """One selected spec operation, flattened into what a step needs."""

    method: str
    path: str
    operation_id: "str | None" = None
    summary: "str | None" = None
    tags: tuple[str, ...] = ()
    parameters: list[dict] = field(default_factory=list)
    body: Any = None
    body_content_type: "str | None" = None
    success_status: "int | None" = None

    @property
    def name(self) -> str:
        return self.operation_id or f"{self.method} {self.path}"


@dataclass
class Placeholder:
    """A value the spec did not supply, surfaced rather than invented."""

    step: str
    location: str  # "path", "query", "header", "body" or "auth"
    name: str
    hint: str = ""

    def __str__(self) -> str:
        suffix = f" -- {self.hint}" if self.hint else ""
        return f"{self.step}: {self.location} `{self.name}`{suffix}"


@dataclass
class ImportReport:
    """Everything the caller needs to know about what was and was not inferred."""

    scenario: dict
    placeholders: list[Placeholder] = field(default_factory=list)
    auth_notes: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    def summary_lines(self) -> list[str]:
        """The end-of-run report: what still needs a human."""
        lines: list[str] = []
        if self.auth_notes:
            lines.append("Authentication (no credentials were invented):")
            lines.extend(f"  - {note}" for note in self.auth_notes)
        if self.placeholders:
            lines.append("")
            lines.append(
                f"Needs input -- {len(self.placeholders)} value(s) the spec did not supply. "
                "Each is a ${placeholder}; bind them with --data or an earlier extract step:"
            )
            lines.extend(f"  - {p}" for p in self.placeholders)
        if self.skipped:
            lines.append("")
            lines.append("Skipped:")
            lines.extend(f"  - {reason}" for reason in self.skipped)
        return lines


# ---------------------------------------------------------------------------
# Reading the document
# ---------------------------------------------------------------------------


def _parse_document(text: str, origin: str) -> dict:
    """Parse JSON, falling back to YAML."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as json_error:
        try:
            import yaml
        except ImportError:
            raise SpecError(
                f"{origin}: not valid JSON ({json_error.msg} at line {json_error.lineno}), "
                "and pyyaml is not installed to try YAML. Install with: pip install pyyaml"
            ) from None
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as yaml_error:
            raise SpecError(f"{origin}: not valid JSON or YAML: {yaml_error}") from None
    if not isinstance(data, dict):
        raise SpecError(
            f"{origin}: expected an OpenAPI object at the document root, got {type(data).__name__}"
        )
    return data


def _fetch_remote(url: str, ssl_config=None, timeout: float = 30.0) -> str:
    """Download a spec, honouring --ssl-verify / --ca-bundle."""
    import ssl as ssl_module
    import urllib.error
    import urllib.request

    context: "ssl_module.SSLContext | None" = None
    if urlparse(url).scheme == "https":
        if ssl_config is not None:
            from pywrkr.backends import ssl_context_from

            context = ssl_context_from(ssl_config)
        else:
            context = ssl_module.create_default_context()
    request = urllib.request.Request(url, headers={"Accept": "application/json, text/yaml, */*"})
    try:
        # Scheme is validated by the caller; only http(s) reaches this point.
        with urllib.request.urlopen(  # nosec B310
            request, timeout=timeout, context=context
        ) as response:
            return response.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        raise SpecError(f"{url}: server returned {e.code} {e.reason}") from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise SpecError(f"{url}: could not fetch the spec: {e}") from None


def load_spec(source: str, ssl_config=None, timeout: float = 30.0) -> dict:
    """Load an OpenAPI 3.x document from a path or an http(s) URL.

    Blocking, including the remote fetch. Call it from a thread if you are
    already inside an event loop -- fetching a spec from an app running in the
    same loop would otherwise deadlock until the timeout.

    Swagger 2.0 is rejected rather than half-converted: the parameter model is
    different enough (``body`` parameters, no ``requestBody``, no
    ``components``) that a partial translation would silently drop request
    bodies. Converting properly is a separate job, and public converters
    already do it well.
    """
    scheme = urlparse(source).scheme
    if scheme in ("http", "https"):
        text = _fetch_remote(source, ssl_config, timeout)
    elif scheme and len(scheme) > 1:
        raise SpecError(f"Unsupported spec location scheme {scheme!r}: use a path, http or https")
    else:
        if not os.path.isfile(source):
            raise SpecError(f"Spec file not found: {source}")
        with open(source, "r", encoding="utf-8") as handle:
            text = handle.read()

    spec = _parse_document(text, source)

    if "swagger" in spec and "openapi" not in spec:
        raise SpecError(
            f"{source}: this is Swagger {spec['swagger']}, which pywrkr does not read. "
            "Convert it to OpenAPI 3 first (e.g. with swagger2openapi or the "
            "editor.swagger.io converter) and import the result."
        )
    version = str(spec.get("openapi", ""))
    if not version:
        raise SpecError(
            f"{source}: no `openapi` version field. This does not look like an OpenAPI document."
        )
    if not version.startswith("3."):
        raise SpecError(f"{source}: OpenAPI {version} is not supported; pywrkr reads 3.0 and 3.1")
    if not isinstance(spec.get("paths"), dict):
        raise SpecError(f"{source}: no `paths` object, so there is nothing to generate")
    return spec


def _resolve(node: Any, spec: dict, depth: int = 0) -> Any:
    """Follow a single-document ``$ref``.

    Cross-document refs are out of scope; they are reported where they are
    found rather than resolved to nothing and silently dropped.
    """
    if not isinstance(node, dict) or "$ref" not in node:
        return node
    if depth >= _MAX_REF_DEPTH:
        raise SpecError(f"$ref chain longer than {_MAX_REF_DEPTH} hops; the document has a cycle")
    ref = node["$ref"]
    if not isinstance(ref, str) or not ref.startswith("#/"):
        raise SpecError(
            f"Unsupported $ref {ref!r}: pywrkr resolves single-document refs only "
            "(bundle the spec first, e.g. with redocly bundle)"
        )
    target: Any = spec
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(target, dict) or part not in target:
            raise SpecError(f"$ref {ref!r} does not resolve: no {part!r} at that level")
        target = target[part]
    return _resolve(target, spec, depth + 1)


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def _compile(patterns: "Sequence[str]") -> list[re.Pattern[str]]:
    compiled = []
    for pattern in patterns:
        try:
            compiled.append(re.compile(pattern))
        except re.error as e:
            raise SpecError(f"Invalid pattern {pattern!r}: {e}") from None
    return compiled


def select_operations(spec: dict, config: OpenApiImportConfig) -> tuple[list[Operation], list[str]]:
    """Walk the spec and return the operations to generate, plus what was skipped."""
    include = _compile(config.include_patterns)
    exclude = _compile(config.exclude_patterns)
    wanted = {m.upper() for m in config.methods}
    wanted_tags = {t.lower() for t in config.tags}

    operations: list[Operation] = []
    skipped: list[str] = []
    mutating_seen = 0

    for path, path_item in sorted(spec["paths"].items()):
        if not isinstance(path_item, dict):
            continue
        path_item = _resolve(path_item, spec)
        shared_params = path_item.get("parameters", [])

        for method in _ALL_METHODS:
            raw = path_item.get(method.lower())
            if not isinstance(raw, dict):
                continue
            if method not in wanted:
                if method in MUTATING_METHODS:
                    mutating_seen += 1
                continue
            if exclude and any(p.search(path) for p in exclude):
                continue
            if include and not any(p.search(path) for p in include):
                continue
            tags = tuple(str(t) for t in raw.get("tags", []) if isinstance(t, str))
            if wanted_tags and not ({t.lower() for t in tags} & wanted_tags):
                continue
            operations.append(_build_operation(spec, method, path, raw, shared_params, skipped))

    if mutating_seen and not (wanted - set(SAFE_METHODS)):
        skipped.append(
            f"{mutating_seen} mutating operation(s) (POST/PUT/PATCH/DELETE). "
            "Only safe methods are generated by default; add --method POST to include them."
        )
    return operations, skipped


def _build_operation(
    spec: dict,
    method: str,
    path: str,
    raw: dict,
    shared_params: list,
    skipped: list[str],
) -> Operation:
    parameters: list[dict] = []
    for param in list(shared_params) + list(raw.get("parameters", [])):
        try:
            resolved = _resolve(param, spec)
        except SpecError as e:
            skipped.append(f"{method} {path}: parameter skipped ({e})")
            continue
        if isinstance(resolved, dict) and resolved.get("name"):
            parameters.append(resolved)

    body, content_type = _select_body(spec, raw, f"{method} {path}", skipped)
    return Operation(
        method=method,
        path=path,
        operation_id=raw.get("operationId"),
        summary=raw.get("summary"),
        tags=tuple(str(t) for t in raw.get("tags", []) if isinstance(t, str)),
        parameters=parameters,
        body=body,
        body_content_type=content_type,
        success_status=_success_status(raw),
    )


def _select_body(spec: dict, raw: dict, where: str, skipped: list[str]):
    """Pick the JSON request body schema, if the operation has one."""
    request_body = raw.get("requestBody")
    if not request_body:
        return None, None
    try:
        request_body = _resolve(request_body, spec)
    except SpecError as e:
        skipped.append(f"{where}: request body skipped ({e})")
        return None, None
    content = request_body.get("content")
    if not isinstance(content, dict):
        return None, None
    for media_type, media in content.items():
        if "json" not in media_type:
            continue
        if not isinstance(media, dict):
            continue
        return media, media_type
    types = ", ".join(sorted(content)) or "none"
    skipped.append(f"{where}: request body is {types}, and only JSON bodies are generated")
    return None, None


def _success_status(raw: dict) -> "int | None":
    """The documented 2xx the operation is expected to return."""
    responses = raw.get("responses")
    if not isinstance(responses, dict):
        return None
    codes = []
    for key in responses:
        try:
            code = int(key)
        except (TypeError, ValueError):
            continue
        if 200 <= code < 300:
            codes.append(code)
    return min(codes) if codes else None


# ---------------------------------------------------------------------------
# Value generation
# ---------------------------------------------------------------------------


def _example_from_schema(schema: Any, spec: dict, depth: int = 0) -> tuple[Any, bool]:
    """Best value the schema itself offers, and whether one was found.

    Order is ``example`` > ``default`` > first ``enum`` > a type stub. The flag
    matters more than the value: a type stub is a guess, and a caller needs to
    know it is looking at one.
    """
    if depth > 8 or schema is None:
        return None, False
    try:
        schema = _resolve(schema, spec, depth)
    except SpecError:
        return None, False
    if not isinstance(schema, dict):
        return None, False

    if "example" in schema:
        return schema["example"], True
    if "examples" in schema and isinstance(schema["examples"], list) and schema["examples"]:
        return schema["examples"][0], True
    if "default" in schema:
        return schema["default"], True
    if isinstance(schema.get("enum"), list) and schema["enum"]:
        return schema["enum"][0], True
    if isinstance(schema.get("const"), (str, int, float, bool)):
        return schema["const"], True

    for key in ("allOf", "anyOf", "oneOf"):
        options = schema.get(key)
        if isinstance(options, list) and options:
            return _example_from_schema(options[0], spec, depth + 1)

    declared = schema.get("type")
    if isinstance(declared, list):  # OpenAPI 3.1 allows a type array
        declared = next((t for t in declared if t != "null"), None)

    if declared == "object" or "properties" in schema:
        return _object_stub(schema, spec, depth), False
    if declared == "array":
        item, _ = _example_from_schema(schema.get("items"), spec, depth + 1)
        return ([item] if item is not None else []), False
    fmt = schema.get("format")
    if isinstance(fmt, str) and fmt in _FORMAT_STUBS:
        return _FORMAT_STUBS[fmt], False
    if isinstance(declared, str) and declared in _TYPE_STUBS:
        return _TYPE_STUBS[declared], False
    return None, False


def _object_stub(schema: dict, spec: dict, depth: int) -> dict:
    """Build an object from its properties, required fields first."""
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return {}
    required = set(schema.get("required", []) if isinstance(schema.get("required"), list) else [])
    out: dict[str, Any] = {}
    for name, subschema in properties.items():
        value, _ = _example_from_schema(subschema, spec, depth + 1)
        if value is None and name not in required:
            continue
        out[name] = value if value is not None else f"${{{name}}}"
    return out


def _placeholder_name(raw: str) -> str:
    """A ``${...}`` name the templating engine will accept."""
    cleaned = re.sub(r"[^A-Za-z0-9_]", "_", raw).strip("_")
    if not cleaned or cleaned[0].isdigit():
        cleaned = f"v_{cleaned}" if cleaned else "value"
    return cleaned


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


def _server_base_url(spec: dict) -> "str | None":
    servers = spec.get("servers")
    if isinstance(servers, list):
        for entry in servers:
            if isinstance(entry, dict) and isinstance(entry.get("url"), str):
                url = entry["url"].strip()
                if url and "{" not in url:
                    return url.rstrip("/")
    return None


def _auth_requirements(spec: dict, operations: "Iterable[Operation]") -> tuple[dict, list[str]]:
    """Templated auth headers plus guidance. Credentials are never invented."""
    schemes = ((spec.get("components") or {}).get("securitySchemes")) or {}
    if not isinstance(schemes, dict) or not schemes:
        return {}, []

    required = spec.get("security")
    names: list[str] = []
    if isinstance(required, list):
        for requirement in required:
            if isinstance(requirement, dict):
                names.extend(requirement)
    if not names:
        names = list(schemes)

    headers: dict[str, str] = {}
    notes: list[str] = []
    for name in dict.fromkeys(names):
        scheme = schemes.get(name)
        if not isinstance(scheme, dict):
            continue
        kind = str(scheme.get("type", "")).lower()
        if kind == "http":
            style = str(scheme.get("scheme", "")).lower()
            label = "Bearer" if style == "bearer" else style.title() or "Bearer"
            headers["Authorization"] = f"{label} ${{token}}"
            notes.append(
                f"`{name}` is HTTP {style or 'bearer'}: the Authorization header is templated as "
                "`${token}`. Supply it with --data or extract it from a login step."
            )
        elif kind == "apikey":
            location = str(scheme.get("in", "header")).lower()
            key = str(scheme.get("name", "X-API-Key"))
            variable = _placeholder_name(key)
            if location == "header":
                headers[key] = f"${{{variable}}}"
            notes.append(
                f"`{name}` is an apiKey in the {location} named `{key}`: templated as "
                f"`${{{variable}}}`."
                + ("" if location == "header" else " Add it to the query string yourself.")
            )
        else:
            notes.append(
                f"`{name}` is `{kind or 'unknown'}`, which pywrkr does not template. "
                "Add the credentials to the scenario by hand."
            )
    return headers, notes


def openapi_to_scenario(
    spec: dict,
    config: "OpenApiImportConfig | None" = None,
    name: "str | None" = None,
) -> ImportReport:
    """Turn a loaded spec into a scenario dict plus a report of what it could not infer."""
    config = config or OpenApiImportConfig()
    operations, skipped = select_operations(spec, config)
    auth_headers, auth_notes = _auth_requirements(spec, operations)

    steps: list[dict] = []
    placeholders: list[Placeholder] = []
    for operation in operations:
        steps.append(_build_step(operation, spec, config, auth_headers, placeholders))

    if not steps:
        skipped.append("No operations matched. Check --method, --include/--exclude and --tag.")

    raw_info = spec.get("info")
    info: dict = raw_info if isinstance(raw_info, dict) else {}
    scenario: dict = {
        "name": name or str(info.get("title") or "OpenAPI scenario"),
        "steps": steps,
    }
    base_url = config.base_url or _server_base_url(spec)
    if base_url:
        scenario["base_url"] = base_url
    if config.think_time:
        scenario["think_time"] = config.think_time

    return ImportReport(
        scenario=scenario, placeholders=placeholders, auth_notes=auth_notes, skipped=skipped
    )


def _build_step(
    operation: Operation,
    spec: dict,
    config: OpenApiImportConfig,
    auth_headers: dict[str, str],
    placeholders: list[Placeholder],
) -> dict:
    path, query, headers = _render_parameters(operation, spec, placeholders)
    step: dict = {"name": operation.name, "path": path + query, "method": operation.method}

    merged = {**auth_headers, **headers}
    if merged:
        step["headers"] = merged

    if isinstance(operation.body, dict):
        value, exact = _example_from_schema(operation.body.get("schema"), spec)
        if "example" in operation.body:
            value, exact = operation.body["example"], True
        if value is not None:
            step["body"] = value
            for unresolved in _find_placeholders(value):
                placeholders.append(
                    Placeholder(operation.name, "body", unresolved, "no example in the schema")
                )
            if not exact and not isinstance(value, (str, int, float, bool)):
                logger.debug("Body for %s built from type stubs", operation.name)

    if config.assert_status and operation.success_status:
        step["assert_status"] = operation.success_status
    return step


def _render_parameters(
    operation: Operation, spec: dict, placeholders: list[Placeholder]
) -> tuple[str, str, dict[str, str]]:
    """Fill path/query/header parameters, recording every one that was guessed."""
    path = operation.path
    query_parts: list[str] = []
    headers: dict[str, str] = {}

    for param in operation.parameters:
        name = str(param["name"])
        location = str(param.get("in", "query")).lower()
        required = bool(param.get("required")) or location == "path"
        schema = param.get("schema", param)
        value, exact = _example_from_schema(schema, spec)
        if "example" in param:
            value, exact = param["example"], True

        if not exact and (location == "path" or required):
            variable = _placeholder_name(name)
            rendered = f"${{{variable}}}"
            placeholders.append(
                Placeholder(
                    operation.name,
                    location,
                    variable,
                    "required, and the spec gives no example/default/enum",
                )
            )
        elif not exact or value is None:
            # An optional parameter the spec says nothing about is omitted, not
            # stubbed. Omitting it is a request the spec explicitly allows;
            # sending `?q=string` is a different request that may take a
            # different code path, so a stub here would quietly change what is
            # being benchmarked. Bodies are the opposite case -- see
            # _object_stub -- because the shape has to match for the request to
            # be accepted at all.
            continue
        else:
            rendered = _stringify(value)

        if location == "path":
            path = path.replace(f"{{{name}}}", rendered)
        elif location == "query":
            query_parts.append(f"{name}={rendered}")
        elif location == "header":
            headers[name] = rendered

    # A path template the spec never declared a parameter for still has to go
    # somewhere a human will see it, rather than being sent as a literal brace.
    # The lookbehind matters: a `${name}` just written above is itself a braced
    # group, and matching it again would produce `$${name}` and a duplicate
    # entry in the report.
    for leftover in re.findall(r"(?<!\$)\{([^{}]+)\}", path):
        variable = _placeholder_name(leftover)
        path = path.replace(f"{{{leftover}}}", f"${{{variable}}}")
        placeholders.append(
            Placeholder(operation.name, "path", variable, "no matching parameter in the spec")
        )

    query = f"?{'&'.join(query_parts)}" if query_parts else ""
    return path, query, headers


def _stringify(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, dict)):
        return json.dumps(value)
    return str(value)


#: A `${...}` in a generated body: either a bare name that needs binding, or a
#: generator call such as `${uuid()}` that resolves itself at run time.
_BODY_PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(\([^{}]*\))?\}")


def _find_placeholders(value: Any) -> list[str]:
    """Every ``${name}`` in a generated body that still needs a value.

    Generator calls are excluded: ``${uuid()}`` resolves itself on every
    request, so listing it under "needs input" would send the reader looking
    for something to bind that does not need binding.
    """
    found: list[str] = []
    if isinstance(value, str):
        found.extend(name for name, call in _BODY_PLACEHOLDER.findall(value) if not call)
    elif isinstance(value, dict):
        for item in value.values():
            found.extend(_find_placeholders(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_find_placeholders(item))
    return found


def openapi_to_url_file(spec: dict, config: "OpenApiImportConfig | None" = None) -> ImportReport:
    """Render the selection as a url-file instead of a scenario."""
    report = openapi_to_scenario(spec, config)
    base = report.scenario.get("base_url", "")
    lines = []
    for step in report.scenario["steps"]:
        url = f"{base.rstrip('/')}{step['path']}" if base else step["path"]
        lines.append(f"{step['method']} {url}" if step["method"] != "GET" else url)
    report.scenario = {"_url_file": "\n".join(lines) + ("\n" if lines else "")}
    return report


def convert_openapi(
    spec_source: str,
    output_path: "str | None" = None,
    output_format: str = "scenario",
    config: "OpenApiImportConfig | None" = None,
    name: "str | None" = None,
    ssl_config=None,
    timeout: float = 30.0,
) -> tuple[str, ImportReport]:
    """Load a spec and render it, returning the text and the report."""
    spec = load_spec(spec_source, ssl_config, timeout)
    if output_format == "url-file":
        report = openapi_to_url_file(spec, config)
        content = report.scenario["_url_file"]
    else:
        report = openapi_to_scenario(spec, config, name)
        content = json.dumps(report.scenario, indent=2) + "\n"

    if output_path:
        with open(output_path, "w", encoding="utf-8") as handle:
            handle.write(content)
    return content, report
