"""Tests for `pywrkr openapi-import`.

The generator's whole value rests on one property: it must not produce
plausible-looking garbage. A scenario that benchmarks a 404 handler because a
path parameter was invented is worse than no scenario, so most of what follows
is about which values are taken from the spec and which are surfaced as
placeholders instead.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from aiohttp import web

from pywrkr.config import load_scenario
from pywrkr.main import _build_openapi_import_parser, _run_openapi_import
from pywrkr.openapi_import import (
    SAFE_METHODS,
    OpenApiImportConfig,
    SpecError,
    convert_openapi,
    load_spec,
    openapi_to_scenario,
    openapi_to_url_file,
    select_operations,
)

# ---------------------------------------------------------------------------
# Fixture specs
# ---------------------------------------------------------------------------

WIDGET_SPEC: dict = {
    "openapi": "3.0.3",
    "info": {"title": "Widget API", "version": "1.0.0"},
    "servers": [{"url": "https://api.example.com/v1"}],
    "components": {
        "securitySchemes": {"bearerAuth": {"type": "http", "scheme": "bearer"}},
        "schemas": {
            "Widget": {
                "type": "object",
                "required": ["sku", "qty"],
                "properties": {
                    "sku": {"type": "string", "example": "ABC-123"},
                    "qty": {"type": "integer", "default": 1},
                    "colour": {"type": "string", "enum": ["red", "blue"]},
                },
            }
        },
    },
    "security": [{"bearerAuth": []}],
    "paths": {
        "/widgets": {
            "get": {
                "operationId": "listWidgets",
                "tags": ["public"],
                "parameters": [
                    {"name": "limit", "in": "query", "schema": {"type": "integer", "default": 25}},
                    {
                        "name": "cursor",
                        "in": "query",
                        "required": True,
                        "schema": {"type": "string"},
                    },
                ],
                "responses": {"200": {"description": "ok"}},
            },
            "post": {
                "operationId": "createWidget",
                "tags": ["public"],
                "requestBody": {
                    "content": {
                        "application/json": {"schema": {"$ref": "#/components/schemas/Widget"}}
                    }
                },
                "responses": {"201": {"description": "created"}},
            },
        },
        "/widgets/{widgetId}": {
            "get": {
                "operationId": "getWidget",
                "tags": ["public"],
                "parameters": [
                    {
                        "name": "widgetId",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                    }
                ],
                "responses": {"200": {"description": "ok"}},
            },
            "delete": {"operationId": "deleteWidget", "responses": {"204": {"description": ""}}},
        },
        "/admin/flush": {
            "get": {
                "operationId": "flush",
                "tags": ["admin"],
                "responses": {"200": {"description": "ok"}},
            }
        },
    },
}


def write_spec(spec, suffix=".json") -> str:
    """Write a spec to a temp file. A str is written verbatim, a dict as JSON."""
    handle = tempfile.NamedTemporaryFile("w", suffix=suffix, delete=False)
    if isinstance(spec, str):
        handle.write(spec)
    else:
        json.dump(spec, handle)
    handle.close()
    return handle.name


def steps_of(spec=None, **config_kwargs) -> list[dict]:
    report = openapi_to_scenario(spec or WIDGET_SPEC, OpenApiImportConfig(**config_kwargs))
    return report.scenario["steps"]


def step_named(steps, name) -> dict:
    return next(s for s in steps if s["name"] == name)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


class TestLoadSpec(unittest.TestCase):
    def load_text(self, text, suffix=".json"):
        path = write_spec(text, suffix)
        self.addCleanup(os.unlink, path)
        return load_spec(path)

    def test_a_json_spec_loads(self):
        path = write_spec(WIDGET_SPEC)
        self.addCleanup(os.unlink, path)
        self.assertEqual(load_spec(path)["info"]["title"], "Widget API")

    def test_a_yaml_spec_loads(self):
        yaml_text = (
            "openapi: 3.0.3\n"
            "info:\n  title: YAML API\n  version: '1'\n"
            "paths:\n  /ping:\n    get:\n      responses:\n        '200':\n"
            "          description: ok\n"
        )
        self.assertEqual(self.load_text(yaml_text, ".yaml")["info"]["title"], "YAML API")

    def test_a_missing_file_names_the_file(self):
        with self.assertRaises(SpecError) as ctx:
            load_spec("/nonexistent/spec.json")
        self.assertIn("/nonexistent/spec.json", str(ctx.exception))

    def test_swagger_2_is_rejected_with_a_route_forward(self):
        """Half-converting it would silently drop every request body."""
        with self.assertRaises(SpecError) as ctx:
            self.load_text({"swagger": "2.0", "paths": {}})
        message = str(ctx.exception)
        self.assertIn("Swagger 2.0", message)
        self.assertIn("swagger2openapi", message)

    def test_a_non_openapi_document_is_rejected(self):
        with self.assertRaises(SpecError) as ctx:
            self.load_text({"hello": "world"})
        self.assertIn("openapi", str(ctx.exception))

    def test_a_future_major_version_is_rejected(self):
        with self.assertRaises(SpecError) as ctx:
            self.load_text({"openapi": "4.0.0", "paths": {}})
        self.assertIn("4.0.0", str(ctx.exception))

    def test_openapi_31_is_accepted(self):
        self.assertEqual(self.load_text({"openapi": "3.1.0", "paths": {}})["openapi"], "3.1.0")

    def test_a_spec_without_paths_is_rejected(self):
        with self.assertRaises(SpecError) as ctx:
            self.load_text({"openapi": "3.0.0"})
        self.assertIn("paths", str(ctx.exception))

    def test_a_root_level_array_is_rejected_by_type(self):
        path = write_spec("[1, 2, 3]", ".json")
        self.addCleanup(os.unlink, path)
        with self.assertRaises(SpecError) as ctx:
            load_spec(path)
        self.assertIn("got list", str(ctx.exception))

    def test_malformed_json_reports_where(self):
        path = write_spec("{not json at all", ".json")
        self.addCleanup(os.unlink, path)
        with self.assertRaises(SpecError) as ctx:
            load_spec(path)
        self.assertIn(path, str(ctx.exception))

    def test_an_unsupported_location_scheme_is_rejected(self):
        with self.assertRaises(SpecError) as ctx:
            load_spec("ftp://example.com/spec.json")
        self.assertIn("ftp", str(ctx.exception))


class TestRemoteFetch(unittest.TestCase):
    def test_an_http_error_is_reported_with_the_status(self):
        import urllib.error

        error = urllib.error.HTTPError("http://x/spec.json", 404, "Not Found", {}, None)
        with patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(SpecError) as ctx:
                load_spec("http://x/spec.json")
        self.assertIn("404", str(ctx.exception))

    def test_a_connection_failure_is_reported(self):
        import urllib.error

        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
            with self.assertRaises(SpecError) as ctx:
                load_spec("http://x/spec.json")
        self.assertIn("could not fetch", str(ctx.exception))

    def test_the_timeout_is_passed_through(self):
        captured = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps(WIDGET_SPEC).encode()

        def fake_urlopen(request, timeout=None, context=None):
            captured["timeout"] = timeout
            captured["context"] = context
            return FakeResponse()

        with patch("urllib.request.urlopen", fake_urlopen):
            load_spec("http://x/spec.json", timeout=7.5)
        self.assertEqual(captured["timeout"], 7.5)
        # Plain http needs no TLS context; https is covered below.
        self.assertIsNone(captured["context"])

    def test_https_honours_the_ssl_config(self):
        from pywrkr.config import SSLConfig

        captured = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps(WIDGET_SPEC).encode()

        def fake_urlopen(request, timeout=None, context=None):
            captured["context"] = context
            return FakeResponse()

        with patch("urllib.request.urlopen", fake_urlopen):
            load_spec("https://x/spec.json", ssl_config=SSLConfig(verify=True))
        self.assertTrue(captured["context"].check_hostname)

        with patch("urllib.request.urlopen", fake_urlopen):
            load_spec("https://x/spec.json", ssl_config=SSLConfig(verify=False))
        self.assertFalse(captured["context"].check_hostname)


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


class TestSelection(unittest.TestCase):
    def test_only_safe_methods_by_default(self):
        """A scenario that DELETEs its way through an API is not a default."""
        steps = steps_of()
        self.assertEqual({s["method"] for s in steps}, {"GET"})
        self.assertEqual(SAFE_METHODS, ("GET", "HEAD"))

    def test_the_skipped_mutating_operations_are_reported_not_silent(self):
        report = openapi_to_scenario(WIDGET_SPEC)
        joined = " ".join(report.skipped)
        self.assertIn("2 mutating operation(s)", joined)
        self.assertIn("--method POST", joined)

    def test_mutating_methods_are_included_when_named(self):
        steps = steps_of(methods=("GET", "POST"))
        self.assertIn("POST", {s["method"] for s in steps})

    def test_include_and_exclude_filter_by_path(self):
        self.assertEqual(
            [s["path"] for s in steps_of(exclude_patterns=["/admin"])],
            ["/widgets?limit=25&cursor=${cursor}", "/widgets/${widgetId}"],
        )
        self.assertEqual([s["name"] for s in steps_of(include_patterns=["/admin"])], ["flush"])

    def test_exclude_wins_over_include(self):
        self.assertEqual(steps_of(include_patterns=["/widgets"], exclude_patterns=["/widgets"]), [])

    def test_tags_filter_operations(self):
        self.assertEqual([s["name"] for s in steps_of(tags=["admin"])], ["flush"])
        self.assertNotIn("flush", [s["name"] for s in steps_of(tags=["public"])])

    def test_tag_matching_is_case_insensitive(self):
        self.assertEqual([s["name"] for s in steps_of(tags=["ADMIN"])], ["flush"])

    def test_an_invalid_pattern_is_a_spec_error_not_a_traceback(self):
        with self.assertRaises(SpecError) as ctx:
            steps_of(include_patterns=["[unclosed"])
        self.assertIn("Invalid pattern", str(ctx.exception))

    def test_selecting_nothing_says_so(self):
        report = openapi_to_scenario(WIDGET_SPEC, OpenApiImportConfig(tags=["nope"]))
        self.assertEqual(report.scenario["steps"], [])
        self.assertTrue(any("No operations matched" in s for s in report.skipped))

    def test_path_level_parameters_are_inherited_by_each_operation(self):
        spec = {
            "openapi": "3.0.0",
            "paths": {
                "/x/{id}": {
                    "parameters": [
                        {
                            "name": "id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string", "example": "abc"},
                        }
                    ],
                    "get": {"operationId": "getX", "responses": {"200": {"description": ""}}},
                }
            },
        }
        self.assertEqual(steps_of(spec)[0]["path"], "/x/abc")

    def test_operations_are_emitted_in_a_stable_order(self):
        first = [s["name"] for s in steps_of()]
        second = [s["name"] for s in steps_of()]
        self.assertEqual(first, second)
        self.assertEqual(
            first, sorted(first, key=lambda n: [s["name"] for s in steps_of()].index(n))
        )

    def test_select_operations_returns_operation_objects(self):
        operations, _ = select_operations(WIDGET_SPEC, OpenApiImportConfig())
        self.assertTrue(all(op.method in SAFE_METHODS for op in operations))
        self.assertEqual({op.name for op in operations}, {"flush", "listWidgets", "getWidget"})


# ---------------------------------------------------------------------------
# Value generation — the part that must not invent data
# ---------------------------------------------------------------------------


class TestValueGeneration(unittest.TestCase):
    def test_a_defaulted_query_parameter_uses_its_default(self):
        self.assertIn("limit=25", step_named(steps_of(), "listWidgets")["path"])

    def test_a_required_parameter_with_no_example_becomes_a_placeholder(self):
        """Inventing one produces a scenario that benchmarks a 404 handler."""
        self.assertIn("cursor=${cursor}", step_named(steps_of(), "listWidgets")["path"])

    def test_a_path_parameter_with_no_example_becomes_a_placeholder(self):
        self.assertEqual(step_named(steps_of(), "getWidget")["path"], "/widgets/${widgetId}")

    def test_a_placeholder_is_written_once_not_nested(self):
        """`${name}` is itself a braced group; rescanning yields `$${name}`."""
        path = step_named(steps_of(), "getWidget")["path"]
        self.assertNotIn("$$", path)

    def test_every_placeholder_is_listed_in_the_report(self):
        report = openapi_to_scenario(WIDGET_SPEC)
        names = [p.name for p in report.placeholders]
        self.assertEqual(sorted(names), ["cursor", "widgetId"])
        self.assertTrue(all(p.hint for p in report.placeholders))

    def test_the_report_names_the_step_and_the_location(self):
        report = openapi_to_scenario(WIDGET_SPEC)
        rendered = [str(p) for p in report.placeholders]
        self.assertTrue(any("listWidgets: query `cursor`" in line for line in rendered))
        self.assertTrue(any("getWidget: path `widgetId`" in line for line in rendered))

    def test_an_example_beats_a_default(self):
        spec = _one_get(
            {
                "name": "q",
                "in": "query",
                "example": "shown",
                "schema": {"type": "string", "default": "hidden"},
            }
        )
        self.assertIn("q=shown", steps_of(spec)[0]["path"])

    def test_a_default_beats_an_enum(self):
        spec = _one_get(
            {
                "name": "q",
                "in": "query",
                "schema": {"type": "string", "default": "d", "enum": ["e1", "e2"]},
            }
        )
        self.assertIn("q=d", steps_of(spec)[0]["path"])

    def test_an_enum_supplies_its_first_value(self):
        spec = _one_get(
            {
                "name": "q",
                "in": "query",
                "required": True,
                "schema": {"type": "string", "enum": ["first", "second"]},
            }
        )
        self.assertIn("q=first", steps_of(spec)[0]["path"])

    def test_an_optional_parameter_with_nothing_to_go_on_is_dropped_not_guessed(self):
        """A stub query value would change what is being benchmarked."""
        spec = _one_get({"name": "q", "in": "query", "schema": {"type": "string"}})
        step = steps_of(spec)[0]
        self.assertNotIn("q=", step["path"])
        self.assertEqual(openapi_to_scenario(spec).placeholders, [])

    def test_a_header_parameter_lands_in_headers(self):
        spec = _one_get({"name": "X-Trace", "in": "header", "example": "abc"})
        self.assertEqual(steps_of(spec)[0]["headers"]["X-Trace"], "abc")

    def test_a_boolean_is_rendered_as_json_not_python(self):
        spec = _one_get(
            {"name": "flag", "in": "query", "schema": {"type": "boolean", "default": True}}
        )
        self.assertIn("flag=true", steps_of(spec)[0]["path"])
        self.assertNotIn("flag=True", steps_of(spec)[0]["path"])

    def test_an_undeclared_path_template_still_becomes_a_placeholder(self):
        """A literal `{id}` in the URL would just 404."""
        spec = {
            "openapi": "3.0.0",
            "paths": {
                "/x/{id}": {"get": {"operationId": "g", "responses": {"200": {"description": ""}}}}
            },
        }
        report = openapi_to_scenario(spec)
        self.assertEqual(report.scenario["steps"][0]["path"], "/x/${id}")
        self.assertEqual([p.name for p in report.placeholders], ["id"])

    def test_a_parameter_name_that_is_not_a_valid_variable_is_sanitised(self):
        spec = {
            "openapi": "3.0.0",
            "paths": {
                "/x/{user-id}": {
                    "get": {"operationId": "g", "responses": {"200": {"description": ""}}}
                }
            },
        }
        self.assertEqual(openapi_to_scenario(spec).scenario["steps"][0]["path"], "/x/${user_id}")


class TestBodyGeneration(unittest.TestCase):
    def body(self, spec=None) -> dict:
        return step_named(steps_of(spec, methods=("GET", "POST")), "createWidget")["body"]

    def test_a_ref_is_resolved_and_the_body_generated(self):
        self.assertEqual(self.body(), {"sku": "ABC-123", "qty": 1, "colour": "red"})

    def test_schema_examples_defaults_and_enums_are_all_honoured(self):
        body = self.body()
        self.assertEqual(body["sku"], "ABC-123")  # example
        self.assertEqual(body["qty"], 1)  # default
        self.assertEqual(body["colour"], "red")  # first enum

    def test_a_media_type_example_beats_the_schema(self):
        spec = json.loads(json.dumps(WIDGET_SPEC))
        media = spec["paths"]["/widgets"]["post"]["requestBody"]["content"]["application/json"]
        media["example"] = {"sku": "OVERRIDE"}
        self.assertEqual(self.body(spec), {"sku": "OVERRIDE"})

    def test_a_required_field_with_no_example_becomes_a_placeholder_in_the_body(self):
        spec = json.loads(json.dumps(WIDGET_SPEC))
        schema = spec["components"]["schemas"]["Widget"]
        schema["properties"]["owner"] = {}
        schema["required"].append("owner")
        report = openapi_to_scenario(spec, OpenApiImportConfig(methods=("POST",)))
        body = report.scenario["steps"][0]["body"]
        self.assertEqual(body["owner"], "${owner}")
        self.assertIn("owner", [p.name for p in report.placeholders])
        self.assertIn("body", [p.location for p in report.placeholders])

    def test_a_non_json_body_is_skipped_and_reported(self):
        spec = json.loads(json.dumps(WIDGET_SPEC))
        spec["paths"]["/widgets"]["post"]["requestBody"]["content"] = {
            "application/xml": {"schema": {"type": "string"}}
        }
        report = openapi_to_scenario(spec, OpenApiImportConfig(methods=("POST",)))
        self.assertNotIn("body", report.scenario["steps"][0])
        self.assertTrue(any("only JSON bodies" in s for s in report.skipped))

    def test_a_uuid_format_uses_the_generator_not_a_fixed_value(self):
        """A fixed uuid in a create body makes every virtual user collide."""
        spec = {
            "openapi": "3.0.0",
            "paths": {
                "/x": {
                    "post": {
                        "operationId": "p",
                        "requestBody": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "required": ["id"],
                                        "properties": {"id": {"type": "string", "format": "uuid"}},
                                    }
                                }
                            }
                        },
                        "responses": {"201": {"description": ""}},
                    }
                }
            },
        }
        self.assertEqual(steps_of(spec, methods=("POST",))[0]["body"], {"id": "${uuid()}"})

    def test_a_generator_placeholder_is_not_reported_as_needing_input(self):
        spec = {
            "openapi": "3.0.0",
            "paths": {
                "/x": {
                    "post": {
                        "operationId": "p",
                        "requestBody": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "required": ["id"],
                                        "properties": {"id": {"type": "string", "format": "uuid"}},
                                    }
                                }
                            }
                        },
                        "responses": {"201": {"description": ""}},
                    }
                }
            },
        }
        report = openapi_to_scenario(spec, OpenApiImportConfig(methods=("POST",)))
        # The body really does carry a `${...}`, so there is something to get
        # wrong -- it is just one that resolves itself.
        self.assertEqual(report.scenario["steps"][0]["body"], {"id": "${uuid()}"})
        self.assertEqual(report.placeholders, [])

    def test_a_bare_body_placeholder_is_still_reported(self):
        """The generator-call exemption must not swallow real placeholders."""
        from pywrkr.openapi_import import _find_placeholders

        self.assertEqual(_find_placeholders({"a": "${token}"}), ["token"])
        self.assertEqual(_find_placeholders({"a": "${uuid()}"}), [])
        self.assertEqual(
            sorted(_find_placeholders(["${x}", {"y": "${randint(1,9)}"}, "${z}"])), ["x", "z"]
        )

    def test_a_nested_object_is_built_recursively(self):
        spec = {
            "openapi": "3.0.0",
            "paths": {
                "/x": {
                    "post": {
                        "operationId": "p",
                        "requestBody": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "inner": {
                                                "type": "object",
                                                "properties": {
                                                    "n": {"type": "integer", "default": 7}
                                                },
                                            }
                                        },
                                    }
                                }
                            }
                        },
                        "responses": {"201": {"description": ""}},
                    }
                }
            },
        }
        self.assertEqual(steps_of(spec, methods=("POST",))[0]["body"], {"inner": {"n": 7}})


class TestRefResolution(unittest.TestCase):
    def test_a_cross_document_ref_is_refused_with_guidance(self):
        spec = {
            "openapi": "3.0.0",
            "paths": {
                "/x": {
                    "get": {
                        "operationId": "g",
                        "parameters": [{"$ref": "other.yaml#/components/parameters/Id"}],
                        "responses": {"200": {"description": ""}},
                    }
                }
            },
        }
        report = openapi_to_scenario(spec)
        self.assertTrue(any("single-document refs only" in s for s in report.skipped))

    def test_a_dangling_ref_names_what_is_missing(self):
        spec = {
            "openapi": "3.0.0",
            "paths": {
                "/x": {
                    "get": {
                        "operationId": "g",
                        "parameters": [{"$ref": "#/components/parameters/Missing"}],
                        "responses": {"200": {"description": ""}},
                    }
                }
            },
        }
        report = openapi_to_scenario(spec)
        self.assertTrue(any("does not resolve" in s for s in report.skipped))

    def test_a_ref_cycle_is_bounded_rather_than_hanging(self):
        spec = {
            "openapi": "3.0.0",
            "components": {
                "schemas": {
                    "A": {"$ref": "#/components/schemas/B"},
                    "B": {"$ref": "#/components/schemas/A"},
                }
            },
            "paths": {
                "/x": {
                    "post": {
                        "operationId": "p",
                        "requestBody": {
                            "content": {
                                "application/json": {"schema": {"$ref": "#/components/schemas/A"}}
                            }
                        },
                        "responses": {"201": {"description": ""}},
                    }
                }
            },
        }
        # Must terminate; the body is simply not generated.
        report = openapi_to_scenario(spec, OpenApiImportConfig(methods=("POST",)))
        self.assertNotIn("body", report.scenario["steps"][0])


class TestAuth(unittest.TestCase):
    def test_bearer_auth_is_templated_and_explained(self):
        report = openapi_to_scenario(WIDGET_SPEC)
        self.assertEqual(report.scenario["steps"][0]["headers"]["Authorization"], "Bearer ${token}")
        self.assertTrue(any("${token}" in note for note in report.auth_notes))

    def test_no_credentials_are_invented(self):
        """The one thing a generator must never do."""
        rendered = json.dumps(openapi_to_scenario(WIDGET_SPEC).scenario)
        for leaked in ("password", "secret", "hunter2", "changeme"):
            self.assertNotIn(leaked, rendered.lower())

    def test_an_apikey_header_is_templated(self):
        spec = json.loads(json.dumps(WIDGET_SPEC))
        spec["components"]["securitySchemes"] = {
            "key": {"type": "apiKey", "in": "header", "name": "X-API-Key"}
        }
        spec["security"] = [{"key": []}]
        report = openapi_to_scenario(spec)
        self.assertEqual(report.scenario["steps"][0]["headers"]["X-API-Key"], "${X_API_Key}")

    def test_an_apikey_in_the_query_is_reported_rather_than_silently_dropped(self):
        spec = json.loads(json.dumps(WIDGET_SPEC))
        spec["components"]["securitySchemes"] = {
            "key": {"type": "apiKey", "in": "query", "name": "api_key"}
        }
        spec["security"] = [{"key": []}]
        report = openapi_to_scenario(spec)
        self.assertTrue(any("query" in note for note in report.auth_notes))

    def test_an_unsupported_scheme_says_so(self):
        spec = json.loads(json.dumps(WIDGET_SPEC))
        spec["components"]["securitySchemes"] = {"oauth": {"type": "oauth2", "flows": {}}}
        spec["security"] = [{"oauth": []}]
        report = openapi_to_scenario(spec)
        self.assertTrue(any("by hand" in note for note in report.auth_notes))

    def test_no_security_schemes_means_no_auth_headers(self):
        spec = json.loads(json.dumps(WIDGET_SPEC))
        del spec["components"]["securitySchemes"]
        del spec["security"]
        report = openapi_to_scenario(spec)
        self.assertNotIn("headers", report.scenario["steps"][0])
        self.assertEqual(report.auth_notes, [])


class TestScenarioShape(unittest.TestCase):
    def test_the_base_url_comes_from_the_spec_servers_entry(self):
        self.assertEqual(
            openapi_to_scenario(WIDGET_SPEC).scenario["base_url"], "https://api.example.com/v1"
        )

    def test_an_explicit_base_url_overrides_the_spec(self):
        report = openapi_to_scenario(
            WIDGET_SPEC, OpenApiImportConfig(base_url="https://staging.example.com")
        )
        self.assertEqual(report.scenario["base_url"], "https://staging.example.com")

    def test_a_templated_server_url_is_not_used(self):
        """`https://{region}.api.com` is not a URL anything can connect to."""
        spec = json.loads(json.dumps(WIDGET_SPEC))
        spec["servers"] = [{"url": "https://{region}.api.example.com"}]
        self.assertNotIn("base_url", openapi_to_scenario(spec).scenario)

    def test_the_name_defaults_to_the_spec_title(self):
        self.assertEqual(openapi_to_scenario(WIDGET_SPEC).scenario["name"], "Widget API")

    def test_the_name_can_be_overridden(self):
        self.assertEqual(openapi_to_scenario(WIDGET_SPEC, name="Mine").scenario["name"], "Mine")

    def test_assert_status_uses_the_documented_success_code(self):
        steps = steps_of(methods=("GET", "POST"), assert_status=True)
        self.assertEqual(step_named(steps, "listWidgets")["assert_status"], 200)
        self.assertEqual(step_named(steps, "createWidget")["assert_status"], 201)

    def test_assert_status_is_off_by_default(self):
        self.assertNotIn("assert_status", steps_of()[0])

    def test_think_time_is_applied_when_asked(self):
        self.assertEqual(
            openapi_to_scenario(WIDGET_SPEC, OpenApiImportConfig(think_time=1.5)).scenario[
                "think_time"
            ],
            1.5,
        )

    def test_the_step_name_falls_back_to_method_and_path(self):
        spec = {
            "openapi": "3.0.0",
            "paths": {"/x": {"get": {"responses": {"200": {"description": ""}}}}},
        }
        self.assertEqual(steps_of(spec)[0]["name"], "GET /x")


class TestGeneratedScenarioIsValid(unittest.TestCase):
    """The output has to survive the loader it is written for."""

    def test_the_generated_scenario_loads_via_load_scenario(self):
        report = openapi_to_scenario(WIDGET_SPEC, OpenApiImportConfig(methods=("GET", "POST")))
        path = write_spec(report.scenario)
        self.addCleanup(os.unlink, path)
        scenario = load_scenario(path)
        self.assertEqual(len(scenario.steps), len(report.scenario["steps"]))
        self.assertEqual(scenario.name, "Widget API")

    def test_placeholders_survive_as_template_variables(self):
        report = openapi_to_scenario(WIDGET_SPEC)
        path = write_spec(report.scenario)
        self.addCleanup(os.unlink, path)
        scenario = load_scenario(path)
        widget_step = next(s for s in scenario.steps if s.name == "getWidget")
        self.assertIn("${widgetId}", widget_step.path)

    def test_a_scenario_with_assert_status_loads_too(self):
        report = openapi_to_scenario(
            WIDGET_SPEC, OpenApiImportConfig(methods=("GET", "POST"), assert_status=True)
        )
        path = write_spec(report.scenario)
        self.addCleanup(os.unlink, path)
        self.assertEqual(load_scenario(path).steps[0].assert_status, 200)


class TestUrlFileFormat(unittest.TestCase):
    def test_get_urls_are_bare_and_others_carry_the_method(self):
        report = openapi_to_url_file(WIDGET_SPEC, OpenApiImportConfig(methods=("GET", "POST")))
        lines = report.scenario["_url_file"].strip().splitlines()
        self.assertIn("https://api.example.com/v1/admin/flush", lines)
        self.assertIn("POST https://api.example.com/v1/widgets", lines)

    def test_the_report_still_lists_placeholders(self):
        report = openapi_to_url_file(WIDGET_SPEC)
        self.assertTrue(report.placeholders)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestCli(unittest.TestCase):
    def setUp(self):
        self.spec_path = write_spec(WIDGET_SPEC)
        self.addCleanup(os.unlink, self.spec_path)
        self.dir = tempfile.mkdtemp()

    def run_cli(self, *argv):
        args = _build_openapi_import_parser().parse_args([self.spec_path, *argv])
        out, err = io.StringIO(), io.StringIO()
        with patch("sys.stdout", out), patch("sys.stderr", err):
            _run_openapi_import(args)
        return out.getvalue(), err.getvalue()

    def test_the_scenario_goes_to_stdout_by_default(self):
        out, _ = self.run_cli()
        self.assertEqual(json.loads(out)["name"], "Widget API")

    def test_the_needs_input_report_goes_to_stderr(self):
        """So `openapi-import spec.json > scenario.json` still yields valid JSON."""
        out, err = self.run_cli()
        json.loads(out)  # must not be polluted by the report
        self.assertIn("Needs input", err)
        self.assertIn("widgetId", err)

    def test_output_to_a_file(self):
        path = os.path.join(self.dir, "scenario.json")
        out, _ = self.run_cli("-o", path)
        self.assertIn("Wrote scenario to", out)
        self.assertEqual(json.loads(open(path).read())["name"], "Widget API")

    def test_the_url_file_format(self):
        out, _ = self.run_cli("--format", "url-file")
        self.assertIn("https://api.example.com/v1/widgets", out)

    def test_flags_reach_the_config(self):
        out, _ = self.run_cli(
            "--method",
            "GET",
            "--method",
            "POST",
            "--exclude",
            "/admin",
            "--base-url",
            "http://localhost:9",
            "--assert-status",
            "--name",
            "Custom",
            "--think-time",
            "2",
        )
        scenario = json.loads(out)
        self.assertEqual(scenario["name"], "Custom")
        self.assertEqual(scenario["base_url"], "http://localhost:9")
        self.assertEqual(scenario["think_time"], 2)
        self.assertIn("POST", {s["method"] for s in scenario["steps"]})
        self.assertNotIn("flush", {s["name"] for s in scenario["steps"]})
        self.assertTrue(all("assert_status" in s for s in scenario["steps"]))

    def test_a_bad_spec_exits_one_with_an_actionable_message(self):
        args = _build_openapi_import_parser().parse_args(["/nope/spec.json"])
        with patch("sys.stderr", new_callable=io.StringIO) as err:
            with self.assertRaises(SystemExit) as ctx:
                _run_openapi_import(args)
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("/nope/spec.json", err.getvalue())

    def test_the_subcommand_is_reachable_from_main(self):
        from pywrkr.main import main

        path = os.path.join(self.dir, "out.json")
        argv = ["pywrkr", "openapi-import", self.spec_path, "-o", path]
        with (
            patch("sys.argv", argv),
            patch("sys.stdout", new_callable=io.StringIO),
            patch("sys.stderr", new_callable=io.StringIO),
        ):
            main()
        self.assertTrue(os.path.exists(path))


# ---------------------------------------------------------------------------
# Integration: generate from a live app's own spec, then run it
# ---------------------------------------------------------------------------


class TestAgainstLiveApp(unittest.IsolatedAsyncioTestCase):
    """The end-to-end claim: a spec from a running app produces a scenario that runs."""

    async def asyncSetUp(self):
        self.hits: list[str] = []

        async def openapi(request):
            return web.json_response(
                {
                    "openapi": "3.0.3",
                    "info": {"title": "Live API", "version": "1"},
                    "paths": {
                        "/items": {
                            "get": {
                                "operationId": "listItems",
                                "parameters": [
                                    {
                                        "name": "limit",
                                        "in": "query",
                                        "schema": {"type": "integer", "default": 5},
                                    }
                                ],
                                "responses": {"200": {"description": "ok"}},
                            }
                        },
                        "/items/{itemId}": {
                            "get": {
                                "operationId": "getItem",
                                "parameters": [
                                    {
                                        "name": "itemId",
                                        "in": "path",
                                        "required": True,
                                        "schema": {"type": "string", "example": "abc"},
                                    }
                                ],
                                "responses": {"200": {"description": "ok"}},
                            }
                        },
                    },
                }
            )

        async def anything(request):
            self.hits.append(str(request.rel_url))
            return web.json_response({"ok": True})

        app = web.Application()
        app.router.add_get("/openapi.json", openapi)
        app.router.add_get("/items", anything)
        app.router.add_get("/items/{tail}", anything)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.base = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"

    async def asyncTearDown(self):
        await self.runner.cleanup()

    async def test_spec_from_the_app_becomes_a_scenario_that_runs_against_it(self):
        # In a thread: load_spec blocks, and the app serving the spec runs in
        # this very loop, so a direct call would deadlock until the timeout.
        content, report = await asyncio.to_thread(
            convert_openapi,
            f"{self.base}/openapi.json",
            config=OpenApiImportConfig(base_url=self.base, assert_status=True),
        )
        self.assertEqual(report.placeholders, [], report.summary_lines())

        path = os.path.join(tempfile.mkdtemp(), "scenario.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)

        from pywrkr.api import arun

        result = await arun(self.base, scenario=load_scenario(path), users=2, duration=1.0)
        self.assertGreater(result.total_requests, 0)
        self.assertEqual(result.total_errors, 0, result.error_types)
        # Both generated steps really were exercised against the live app.
        self.assertTrue(any(hit.startswith("/items?") for hit in self.hits), self.hits)
        self.assertTrue(any(hit.startswith("/items/abc") for hit in self.hits), self.hits)


def _one_get(parameter: dict) -> dict:
    """A minimal spec with a single GET carrying one parameter."""
    return {
        "openapi": "3.0.0",
        "paths": {
            "/x": {
                "get": {
                    "operationId": "g",
                    "parameters": [parameter],
                    "responses": {"200": {"description": ""}},
                }
            }
        },
    }


if __name__ == "__main__":
    unittest.main()
