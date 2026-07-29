#!/usr/bin/env python3
"""Tests for scenario variable extraction and ${var} templating."""

import json
import os
import tempfile
import unittest
from io import StringIO
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase

import pywrkr
from pywrkr.config import parse_extract_spec
from pywrkr.templating import (
    ExtractError,
    TemplateError,
    apply_extractors,
    compile_extractor,
    is_valid_var_name,
    parse_json_path,
    resolve_json_path,
    stringify,
    substitute,
    substitute_structure,
)
from pywrkr.workers import _render_step

# ---------------------------------------------------------------------------
# ${var} substitution
# ---------------------------------------------------------------------------


class TestSubstitute(unittest.TestCase):
    def test_no_placeholder_returns_input(self):
        self.assertEqual(substitute("/api/items", {"token": "x"}), "/api/items")

    def test_single_placeholder(self):
        self.assertEqual(substitute("Bearer ${token}", {"token": "abc"}), "Bearer abc")

    def test_multiple_and_repeated_placeholders(self):
        out = substitute("/u/${id}/p/${pid}/${id}", {"id": "7", "pid": "9"})
        self.assertEqual(out, "/u/7/p/9/7")

    def test_unknown_variable_raises(self):
        with self.assertRaises(TemplateError) as ctx:
            substitute("Bearer ${token}", {})
        self.assertIn("${token}", str(ctx.exception))

    def test_unknown_variables_listed_once_each(self):
        with self.assertRaises(TemplateError) as ctx:
            substitute("${a}${b}${a}", {})
        msg = str(ctx.exception)
        self.assertIn("${a}", msg)
        self.assertIn("${b}", msg)
        self.assertEqual(msg.count("${a}"), 1)

    def test_keep_literal_leaves_unknown_in_place(self):
        out = substitute("Bearer ${token}", {}, keep_literal=True)
        self.assertEqual(out, "Bearer ${token}")

    def test_keep_literal_still_substitutes_known(self):
        out = substitute("${a}-${b}", {"a": "1"}, keep_literal=True)
        self.assertEqual(out, "1-${b}")

    def test_non_identifier_placeholder_is_not_a_placeholder(self):
        # ${1bad} and shell-style $VAR are left untouched rather than erroring:
        # only ${identifier} is template syntax.
        self.assertEqual(substitute("${1bad} $VAR", {}), "${1bad} $VAR")

    def test_empty_value_substitutes(self):
        self.assertEqual(substitute("x=${v}", {"v": ""}), "x=")


class TestSubstituteStructure(unittest.TestCase):
    def test_nested_dict_and_list(self):
        body = {"auth": {"token": "${token}"}, "ids": ["${id}", 5, None]}
        out = substitute_structure(body, {"token": "T", "id": "3"})
        self.assertEqual(out, {"auth": {"token": "T"}, "ids": ["3", 5, None]})

    def test_dict_keys_are_substituted(self):
        out = substitute_structure({"${k}": "v"}, {"k": "name"})
        self.assertEqual(out, {"name": "v"})

    def test_non_string_leaves_untouched(self):
        self.assertEqual(substitute_structure(42, {}), 42)
        self.assertIsNone(substitute_structure(None, {}))

    def test_unknown_variable_propagates(self):
        with self.assertRaises(TemplateError):
            substitute_structure({"a": ["${missing}"]}, {})


class TestIsValidVarName(unittest.TestCase):
    def test_accepts_identifiers(self):
        for name in ("token", "_x", "a1", "user_id"):
            self.assertTrue(is_valid_var_name(name), name)

    def test_rejects_others(self):
        for name in ("1abc", "a-b", "", "a b", None, 7):
            self.assertFalse(is_valid_var_name(name), repr(name))


# ---------------------------------------------------------------------------
# JSONPath subset
# ---------------------------------------------------------------------------


class TestParseJsonPath(unittest.TestCase):
    def test_dotted(self):
        self.assertEqual(parse_json_path("$.a.b"), ("a", "b"))

    def test_index(self):
        self.assertEqual(parse_json_path("$.items[0].id"), ("items", 0, "id"))

    def test_negative_index(self):
        self.assertEqual(parse_json_path("$.items[-1]"), ("items", -1))

    def test_quoted_key(self):
        self.assertEqual(parse_json_path('$["a.b"].c'), ("a.b", "c"))
        self.assertEqual(parse_json_path("$['x y']"), ("x y",))

    def test_dollar_alone_is_whole_document(self):
        self.assertEqual(parse_json_path("$"), ())

    def test_leading_dollar_optional(self):
        self.assertEqual(parse_json_path("data.id"), ("data", "id"))

    def test_whitespace_tolerated(self):
        self.assertEqual(parse_json_path("  $.a  "), ("a",))

    def test_empty_rejected(self):
        with self.assertRaises(ValueError):
            parse_json_path("   ")

    def test_unsupported_syntax_rejected(self):
        for expr in ("$.a[*]", "$.a[1:2]", "$..a", "$.a[", "$.a]"):
            with self.assertRaises(ValueError, msg=expr):
                parse_json_path(expr)


class TestResolveJsonPath(unittest.TestCase):
    DOC = {"a": {"b": [{"id": 1}, {"id": 2}]}, "flag": False, "nil": None}

    def test_nested(self):
        self.assertEqual(resolve_json_path(self.DOC, ("a", "b", 1, "id")), 2)

    def test_negative_index(self):
        self.assertEqual(resolve_json_path(self.DOC, ("a", "b", -1, "id")), 2)

    def test_empty_segments_returns_document(self):
        self.assertIs(resolve_json_path(self.DOC, ()), self.DOC)

    def test_missing_key(self):
        with self.assertRaises(ExtractError) as ctx:
            resolve_json_path(self.DOC, ("nope",), "$.nope")
        self.assertIn("not found", str(ctx.exception))

    def test_index_into_object(self):
        with self.assertRaises(ExtractError) as ctx:
            resolve_json_path(self.DOC, ("a", 0), "$.a[0]")
        self.assertIn("expected an array", str(ctx.exception))

    def test_key_into_array(self):
        with self.assertRaises(ExtractError) as ctx:
            resolve_json_path(self.DOC, ("a", "b", "id"), "$.a.b.id")
        self.assertIn("expected an object", str(ctx.exception))

    def test_index_out_of_range(self):
        with self.assertRaises(ExtractError) as ctx:
            resolve_json_path(self.DOC, ("a", "b", 9), "$.a.b[9]")
        self.assertIn("out of range", str(ctx.exception))

    def test_error_message_names_the_position(self):
        with self.assertRaises(ExtractError) as ctx:
            resolve_json_path(self.DOC, ("a", "b", 0, "nope"), "$.a.b[0].nope")
        self.assertIn("$.a.b[0]", str(ctx.exception))


class TestStringify(unittest.TestCase):
    def test_str_passthrough(self):
        self.assertEqual(stringify("x"), "x")

    def test_bool_uses_json_spelling(self):
        self.assertEqual(stringify(True), "true")
        self.assertEqual(stringify(False), "false")

    def test_numbers(self):
        self.assertEqual(stringify(7), "7")
        self.assertEqual(stringify(1.5), "1.5")

    def test_containers_are_compact_json(self):
        self.assertEqual(stringify({"a": 1}), '{"a":1}')
        self.assertEqual(stringify([1, 2]), "[1,2]")

    def test_none_becomes_json_null(self):
        self.assertEqual(stringify(None), "null")


# ---------------------------------------------------------------------------
# Extractor compilation
# ---------------------------------------------------------------------------


class TestCompileExtractor(unittest.TestCase):
    def test_json_parses_path(self):
        rule = compile_extractor("t", "json", "$.access_token")
        self.assertEqual(rule.path, ("access_token",))
        self.assertIsNone(rule.pattern)

    def test_regex_compiles(self):
        rule = compile_extractor("c", "regex", r'value="([^"]+)"')
        self.assertIsNotNone(rule.pattern)
        self.assertIsNone(rule.path)

    def test_header_needs_no_compilation(self):
        rule = compile_extractor("s", "header", "X-Session-Id")
        self.assertIsNone(rule.pattern)
        self.assertIsNone(rule.path)

    def test_unknown_source(self):
        with self.assertRaises(ValueError) as ctx:
            compile_extractor("v", "xpath", "//a")
        self.assertIn("unknown extract source", str(ctx.exception))

    def test_empty_expression(self):
        with self.assertRaises(ValueError):
            compile_extractor("v", "header", "  ")

    def test_non_string_expression(self):
        with self.assertRaises(ValueError):
            compile_extractor("v", "json", 5)

    def test_invalid_regex(self):
        with self.assertRaises(ValueError) as ctx:
            compile_extractor("v", "regex", "(unclosed")
        self.assertIn("invalid regex", str(ctx.exception))

    def test_regex_without_capture_group(self):
        with self.assertRaises(ValueError) as ctx:
            compile_extractor("v", "regex", "token=.+")
        self.assertIn("no capture group", str(ctx.exception))

    def test_invalid_json_path(self):
        with self.assertRaises(ValueError):
            compile_extractor("v", "json", "$.a[*]")


# ---------------------------------------------------------------------------
# Applying extractors to a response
# ---------------------------------------------------------------------------


class TestApplyExtractors(unittest.TestCase):
    def test_json_header_and_regex_together(self):
        rules = {
            "token": compile_extractor("token", "json", "$.access_token"),
            "session": compile_extractor("session", "header", "X-Session-Id"),
        }
        body = json.dumps({"access_token": "abc"}).encode()
        values, failures = apply_extractors(rules, body, {"X-Session-Id": "s-1"})
        self.assertEqual(values, {"token": "abc", "session": "s-1"})
        self.assertEqual(failures, [])

    def test_regex_first_capture_group(self):
        rules = {"csrf": compile_extractor("csrf", "regex", r'name="csrf" value="([^"]+)"')}
        body = b'<input name="csrf" value="tok-42">'
        values, failures = apply_extractors(rules, body, {})
        self.assertEqual(values, {"csrf": "tok-42"})
        self.assertEqual(failures, [])

    def test_regex_no_match(self):
        rules = {"csrf": compile_extractor("csrf", "regex", r"value=\"([^\"]+)\"")}
        values, failures = apply_extractors(rules, b"nothing here", {})
        self.assertEqual(values, {})
        self.assertEqual(len(failures), 1)
        self.assertIn("did not match", failures[0])

    def test_regex_optional_group_unset(self):
        rules = {"v": compile_extractor("v", "regex", r"a(x)?b")}
        values, failures = apply_extractors(rules, b"ab", {})
        self.assertEqual(values, {})
        self.assertIn("capture group 1 is unset", failures[0])

    def test_missing_header(self):
        rules = {"s": compile_extractor("s", "header", "X-Absent")}
        values, failures = apply_extractors(rules, b"", {})
        self.assertEqual(values, {})
        self.assertIn("no header", failures[0])

    def test_header_without_any_headers(self):
        rules = {"s": compile_extractor("s", "header", "X-Absent")}
        _, failures = apply_extractors(rules, b"", None)
        self.assertIn("no header", failures[0])

    def test_body_not_json(self):
        rules = {"t": compile_extractor("t", "json", "$.t")}
        _, failures = apply_extractors(rules, b"<html>", {})
        self.assertIn("not valid JSON", failures[0])

    def test_body_not_captured(self):
        rules = {"t": compile_extractor("t", "json", "$.t")}
        _, failures = apply_extractors(rules, None, {})
        self.assertIn("not captured", failures[0])

    def test_null_value_is_a_failure(self):
        rules = {"t": compile_extractor("t", "json", "$.t")}
        _, failures = apply_extractors(rules, b'{"t": null}', {})
        self.assertIn("resolved to null", failures[0])

    def test_rules_are_independent(self):
        rules = {
            "ok": compile_extractor("ok", "json", "$.a"),
            "bad": compile_extractor("bad", "json", "$.missing"),
        }
        values, failures = apply_extractors(rules, b'{"a": "1"}', {})
        self.assertEqual(values, {"ok": "1"})
        self.assertEqual(len(failures), 1)
        self.assertTrue(failures[0].startswith("bad: "))

    def test_nested_and_scalar_conversion(self):
        rules = {
            "num": compile_extractor("num", "json", "$.data.count"),
            "obj": compile_extractor("obj", "json", "$.data"),
            "flag": compile_extractor("flag", "json", "$.data.ok"),
        }
        body = json.dumps({"data": {"count": 3, "ok": True}}).encode()
        values, failures = apply_extractors(rules, body, {})
        self.assertEqual(failures, [])
        self.assertEqual(values["num"], "3")
        self.assertEqual(values["flag"], "true")
        self.assertEqual(json.loads(values["obj"]), {"count": 3, "ok": True})


# ---------------------------------------------------------------------------
# Scenario-file validation
# ---------------------------------------------------------------------------


class TestParseExtractSpec(unittest.TestCase):
    def test_none_yields_empty(self):
        self.assertEqual(parse_extract_spec(None, "Step 0"), {})

    def test_valid_spec(self):
        out = parse_extract_spec({"token": {"json": "$.t"}}, "Step 0")
        self.assertEqual(out["token"].source, "json")

    def test_not_a_dict(self):
        with self.assertRaises(ValueError) as ctx:
            parse_extract_spec(["token"], "Step 3")
        self.assertIn("Step 3 'extract' must be an object", str(ctx.exception))

    def test_bad_variable_name(self):
        with self.assertRaises(ValueError) as ctx:
            parse_extract_spec({"1bad": {"json": "$.t"}}, "Step 0")
        self.assertIn("not a valid ${name} identifier", str(ctx.exception))

    def test_rule_not_a_dict(self):
        with self.assertRaises(ValueError) as ctx:
            parse_extract_spec({"t": "$.t"}, "Step 0")
        self.assertIn("must be an object", str(ctx.exception))

    def test_unknown_rule_key(self):
        with self.assertRaises(ValueError) as ctx:
            parse_extract_spec({"t": {"jsonpath": "$.t"}}, "Step 0")
        self.assertIn("unknown key", str(ctx.exception))

    def test_no_source(self):
        with self.assertRaises(ValueError) as ctx:
            parse_extract_spec({"t": {}}, "Step 0")
        self.assertIn("exactly one of", str(ctx.exception))

    def test_two_sources(self):
        with self.assertRaises(ValueError) as ctx:
            parse_extract_spec({"t": {"json": "$.t", "header": "X"}}, "Step 0")
        self.assertIn("exactly one of", str(ctx.exception))

    def test_compile_error_is_prefixed(self):
        with self.assertRaises(ValueError) as ctx:
            parse_extract_spec({"t": {"regex": "(unclosed"}}, "Step 2")
        self.assertIn("Step 2 extract 't'", str(ctx.exception))


class TestScenarioLoadingWithExtract(unittest.TestCase):
    def _load(self, data):
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        json.dump(data, f)
        f.close()
        try:
            return pywrkr.load_scenario(f.name)
        finally:
            os.unlink(f.name)

    def test_defaults_are_backward_compatible(self):
        scenario = self._load({"steps": [{"path": "/"}]})
        self.assertEqual(scenario.steps[0].extract, {})
        self.assertEqual(scenario.on_extract_failure, "abort_iteration")
        self.assertEqual(scenario.on_template_error, "abort_iteration")

    def test_extract_is_compiled_at_load_time(self):
        scenario = self._load(
            {
                "steps": [
                    {
                        "path": "/login",
                        "method": "POST",
                        "extract": {
                            "token": {"json": "$.access_token"},
                            "sid": {"header": "X-Session-Id"},
                            "csrf": {"regex": 'value="([^"]+)"'},
                        },
                    }
                ]
            }
        )
        extract = scenario.steps[0].extract
        self.assertEqual(list(extract), ["token", "sid", "csrf"])
        self.assertEqual(extract["token"].path, ("access_token",))
        self.assertIsNotNone(extract["csrf"].pattern)

    def test_scenario_level_options(self):
        scenario = self._load(
            {
                "on_extract_failure": "continue",
                "on_template_error": "keep_literal",
                "steps": [{"path": "/"}],
            }
        )
        self.assertEqual(scenario.on_extract_failure, "continue")
        self.assertEqual(scenario.on_template_error, "keep_literal")

    def test_invalid_on_extract_failure(self):
        with self.assertRaises(ValueError) as ctx:
            self._load({"on_extract_failure": "explode", "steps": [{"path": "/"}]})
        self.assertIn("on_extract_failure", str(ctx.exception))

    def test_invalid_on_template_error(self):
        with self.assertRaises(ValueError) as ctx:
            self._load({"on_template_error": "shrug", "steps": [{"path": "/"}]})
        self.assertIn("on_template_error", str(ctx.exception))

    def test_bad_extract_reported_with_step_index(self):
        with self.assertRaises(ValueError) as ctx:
            self._load(
                {"steps": [{"path": "/"}, {"path": "/x", "extract": {"t": {"regex": "no-group"}}}]}
            )
        self.assertIn("Step 1", str(ctx.exception))


# ---------------------------------------------------------------------------
# Step rendering
# ---------------------------------------------------------------------------


class TestRenderStep(unittest.TestCase):
    def test_path_headers_and_body(self):
        step = pywrkr.ScenarioStep(
            path="/users/${id}",
            method="POST",
            headers={"Authorization": "Bearer ${token}", "X-${hname}": "1"},
            body={"who": "${id}"},
        )
        variables = {"id": "42", "token": "T", "hname": "Trace"}
        path, headers, body = _render_step(step, {"User-Agent": "pywrkr"}, variables, False)
        self.assertEqual(path, "/users/42")
        self.assertEqual(headers["Authorization"], "Bearer T")
        self.assertEqual(headers["X-Trace"], "1")
        self.assertEqual(headers["User-Agent"], "pywrkr")
        self.assertEqual(json.loads(body), {"who": "42"})
        # dict bodies still get a JSON Content-Type
        self.assertEqual(headers["Content-Type"], "application/json")

    def test_string_body_substituted(self):
        step = pywrkr.ScenarioStep(path="/", body='{"t": "${token}"}')
        _, _, body = _render_step(step, {}, {"token": "T"}, False)
        self.assertEqual(body, b'{"t": "T"}')

    def test_none_body_stays_none(self):
        step = pywrkr.ScenarioStep(path="/")
        _, _, body = _render_step(step, {}, {}, False)
        self.assertIsNone(body)

    def test_base_headers_are_not_mutated(self):
        base = {"User-Agent": "pywrkr"}
        step = pywrkr.ScenarioStep(path="/", body={"a": 1}, headers={"X": "1"})
        _render_step(step, base, {}, False)
        self.assertEqual(base, {"User-Agent": "pywrkr"})

    def test_unknown_variable_raises(self):
        step = pywrkr.ScenarioStep(path="/${missing}")
        with self.assertRaises(TemplateError):
            _render_step(step, {}, {}, False)

    def test_keep_literal(self):
        step = pywrkr.ScenarioStep(path="/${missing}")
        path, _, _ = _render_step(step, {}, {}, True)
        self.assertEqual(path, "/${missing}")


# ---------------------------------------------------------------------------
# Distributed round-trip
# ---------------------------------------------------------------------------


class TestCorrelationSerialization(unittest.TestCase):
    def test_scenario_round_trip(self):
        from pywrkr.distributed import _deserialize_config, _serialize_config

        scenario = pywrkr.Scenario(
            name="Login Flow",
            on_extract_failure="continue",
            on_template_error="keep_literal",
            steps=[
                pywrkr.ScenarioStep(
                    path="/login",
                    method="POST",
                    extract=parse_extract_spec(
                        {"token": {"json": "$.access_token"}, "sid": {"header": "X-Sid"}},
                        "Step 0",
                    ),
                ),
                pywrkr.ScenarioStep(path="/me", headers={"Authorization": "Bearer ${token}"}),
            ],
        )
        config = pywrkr.BenchmarkConfig(url="http://example.com", scenario=scenario)
        restored = _deserialize_config(json.loads(json.dumps(_serialize_config(config))))
        self.assertIsNotNone(restored.scenario)
        self.assertEqual(restored.scenario.on_extract_failure, "continue")
        self.assertEqual(restored.scenario.on_template_error, "keep_literal")
        extract = restored.scenario.steps[0].extract
        self.assertEqual(extract["token"].path, ("access_token",))
        self.assertEqual(extract["sid"].source, "header")

    def test_stats_round_trip(self):
        from pywrkr.distributed import _deserialize_stats, _serialize_stats

        ws = pywrkr.WorkerStats()
        ws.extract_failures = 4
        ws.template_errors = 2
        restored = _deserialize_stats(json.loads(json.dumps(_serialize_stats(ws))))
        self.assertEqual(restored.extract_failures, 4)
        self.assertEqual(restored.template_errors, 2)

    def test_merge_sums_counters(self):
        a, b = pywrkr.WorkerStats(), pywrkr.WorkerStats()
        a.extract_failures, a.template_errors = 1, 2
        b.extract_failures, b.template_errors = 3, 4
        merged = pywrkr.merge_stats([a, b])
        self.assertEqual(merged.extract_failures, 4)
        self.assertEqual(merged.template_errors, 6)


class TestCorrelationReporting(unittest.TestCase):
    def test_json_always_carries_the_counters(self):
        results = pywrkr.build_results_dict(pywrkr.WorkerStats(), 1.0, 1)
        self.assertEqual(results["extract_failures"], 0)
        self.assertEqual(results["template_errors"], 0)

    def _console_output(self, stats):
        from pywrkr.reporting import _print_console_results

        out = StringIO()
        _print_console_results(
            stats, 1.0, 1, 0.0, pywrkr.BenchmarkConfig(url="http://example.com"), None, out
        )
        return out.getvalue()

    def test_terminal_output_shows_counters_when_nonzero(self):
        stats = pywrkr.WorkerStats()
        stats.total_requests = 10
        stats.extract_failures = 3
        stats.template_errors = 1
        text = self._console_output(stats)
        self.assertIn("Extract Failures:  3", text)
        self.assertIn("Template Errors:   1", text)

    def test_terminal_output_omits_counters_when_zero(self):
        stats = pywrkr.WorkerStats()
        stats.total_requests = 10
        text = self._console_output(stats)
        self.assertNotIn("Extract Failures:", text)
        self.assertNotIn("Template Errors:", text)


# ---------------------------------------------------------------------------
# Integration: a stateful server that issues real tokens
# ---------------------------------------------------------------------------


class _CorrelationServerMixin:
    """An aiohttp app that behaves like a real authenticated API.

    ``/login`` mints a unique token per call, ``/me`` accepts only tokens it
    actually issued, and both record what they saw so tests can assert on
    per-user isolation rather than just on status codes.
    """

    async def get_application(self):
        self.issued: list[str] = []
        self.presented: list[str] = []
        self.me_calls = 0
        self.paths_seen: list[str] = []
        self.bodies_seen: list[str] = []
        self.token_calls = 0

        app = web.Application()
        app.router.add_post("/login", self.handle_login)
        app.router.add_get("/me", self.handle_me)
        app.router.add_get("/form", self.handle_form)
        app.router.add_get("/token-once", self.handle_token_once)
        app.router.add_get("/echo/{tail:.*}", self.handle_echo)
        app.router.add_post("/echo-body", self.handle_echo_body)
        return app

    async def handle_login(self, request):
        token = f"tok-{len(self.issued)}"
        self.issued.append(token)
        return web.json_response(
            {"access_token": token, "user": {"id": len(self.issued)}},
            headers={"X-Session-Id": f"sid-{len(self.issued)}"},
        )

    async def handle_me(self, request):
        self.me_calls += 1
        auth = request.headers.get("Authorization", "")
        token = auth[len("Bearer ") :] if auth.startswith("Bearer ") else ""
        self.presented.append(token)
        if token not in self.issued:
            return web.json_response({"error": "bad token"}, status=401)
        return web.json_response({"token": token})

    async def handle_form(self, request):
        return web.Response(
            text='<form><input name="csrf" value="csrf-9"></form>', content_type="text/html"
        )

    async def handle_token_once(self, request):
        # Only the first call hands out a token; later calls omit the key so
        # extraction fails and the scenario's failure policy is exercised.
        self.token_calls += 1
        if self.token_calls == 1:
            return web.json_response({"token": "once-1"})
        return web.json_response({})

    async def handle_echo(self, request):
        self.paths_seen.append(request.path)
        return web.json_response({"path": request.path})

    async def handle_echo_body(self, request):
        self.bodies_seen.append((await request.read()).decode())
        return web.json_response({"ok": True})

    def _url(self):
        return f"http://localhost:{self.server.port}"

    async def _run(self, scenario_data, users=1, duration=1.0):
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        json.dump(scenario_data, f)
        f.close()
        try:
            scenario = pywrkr.load_scenario(f.name)
            config = pywrkr.BenchmarkConfig(
                url=self._url(),
                users=users,
                duration=duration,
                think_time=0.0,
                ramp_up=0.0,
                timeout_sec=5,
                scenario=scenario,
            )
            with patch("sys.stdout", new_callable=StringIO):
                stats, _ = await pywrkr.run_user_simulation(config)
            return stats
        finally:
            os.unlink(f.name)


class TestCorrelationIntegration(_CorrelationServerMixin, AioHTTPTestCase):
    async def test_login_token_flows_into_authenticated_request(self):
        stats = await self._run(
            {
                "name": "Login Flow",
                "steps": [
                    {
                        "name": "login",
                        "method": "POST",
                        "path": "/login",
                        "body": {"user": "demo"},
                        "extract": {"token": {"json": "$.access_token"}},
                    },
                    {
                        "name": "me",
                        "path": "/me",
                        "headers": {"Authorization": "Bearer ${token}"},
                        "assert_status": 200,
                    },
                ],
            }
        )
        self.assertGreater(self.me_calls, 0)
        self.assertEqual(stats.extract_failures, 0)
        self.assertEqual(stats.template_errors, 0)
        # Every token presented to /me was one the server actually issued.
        self.assertTrue(self.presented)
        self.assertTrue(set(self.presented) <= set(self.issued))
        self.assertNotIn(401, stats.status_codes)

    async def test_header_and_regex_sources(self):
        stats = await self._run(
            {
                "name": "Header + Regex",
                "steps": [
                    {
                        "name": "login",
                        "method": "POST",
                        "path": "/login",
                        "extract": {"sid": {"header": "X-Session-Id"}},
                    },
                    {
                        "name": "form",
                        "path": "/form",
                        "extract": {"csrf": {"regex": 'name="csrf" value="([^"]+)"'}},
                    },
                    {"name": "echo", "path": "/echo/${sid}/${csrf}"},
                ],
            }
        )
        self.assertEqual(stats.extract_failures, 0)
        self.assertEqual(stats.template_errors, 0)
        self.assertTrue(self.paths_seen)
        self.assertTrue(all(p.startswith("/echo/sid-") for p in self.paths_seen))
        self.assertTrue(all(p.endswith("/csrf-9") for p in self.paths_seen))

    async def test_extracted_value_substituted_into_body(self):
        stats = await self._run(
            {
                "name": "Body Templating",
                "steps": [
                    {
                        "name": "login",
                        "method": "POST",
                        "path": "/login",
                        "extract": {
                            "token": {"json": "$.access_token"},
                            "uid": {"json": "$.user.id"},
                        },
                    },
                    {
                        "name": "post-body",
                        "method": "POST",
                        "path": "/echo-body",
                        "body": {"auth": {"token": "${token}"}, "user": "${uid}"},
                    },
                ],
            }
        )
        self.assertEqual(stats.extract_failures, 0)
        self.assertEqual(stats.template_errors, 0)
        self.assertTrue(self.bodies_seen)
        for raw in self.bodies_seen:
            sent = json.loads(raw)
            # A numeric JSON value arrives as its JSON spelling, not "1.0"
            # or Python's repr.
            self.assertTrue(sent["auth"]["token"].startswith("tok-"))
            self.assertTrue(sent["user"].isdigit())

    async def test_variables_are_isolated_per_user(self):
        # Each virtual user must own its token: if the scopes were shared, one
        # user would overwrite another's token before it reached /me and the
        # same token would show up twice.
        await self._run(
            {
                "name": "Isolation",
                "steps": [
                    {
                        "name": "login",
                        "method": "POST",
                        "path": "/login",
                        "extract": {"token": {"json": "$.access_token"}},
                    },
                    {
                        "name": "me",
                        "path": "/me",
                        "headers": {"Authorization": "Bearer ${token}"},
                    },
                ],
            },
            users=4,
            duration=1.5,
        )
        self.assertGreater(len(self.presented), 4)
        self.assertEqual(len(self.presented), len(set(self.presented)))
        self.assertTrue(set(self.presented) <= set(self.issued))

    async def test_variables_reset_between_iterations(self):
        # /token-once only mints a token on its first call. With
        # on_extract_failure=continue the second step still runs, and because
        # the scope is cleared per iteration its ${token} is unbound -> a
        # template error rather than a stale value from iteration 1.
        stats = await self._run(
            {
                "name": "Reset",
                "on_extract_failure": "continue",
                "steps": [
                    {
                        "name": "token",
                        "path": "/token-once",
                        "extract": {"token": {"json": "$.token"}},
                    },
                    {"name": "echo", "path": "/echo/${token}"},
                ],
            }
        )
        self.assertGreater(stats.extract_failures, 0)
        self.assertGreater(stats.template_errors, 0)
        self.assertEqual(self.paths_seen.count("/echo/once-1"), 1)
        self.assertTrue(any("ExtractFailure" in k for k in stats.error_types))
        self.assertTrue(any("TemplateError" in k for k in stats.error_types))

    async def test_abort_iteration_skips_remaining_steps(self):
        stats = await self._run(
            {
                "name": "Abort",
                "steps": [
                    {
                        "name": "login",
                        "method": "POST",
                        "path": "/login",
                        "extract": {"token": {"json": "$.nope"}},
                    },
                    {
                        "name": "me",
                        "path": "/me",
                        "headers": {"Authorization": "Bearer ${token}"},
                    },
                ],
            }
        )
        self.assertGreater(stats.extract_failures, 0)
        self.assertEqual(self.me_calls, 0)
        self.assertEqual(stats.template_errors, 0)
        self.assertTrue(any("ExtractFailure" in k for k in stats.error_types))

    async def test_continue_runs_remaining_steps(self):
        stats = await self._run(
            {
                "name": "Continue",
                "on_extract_failure": "continue",
                "on_template_error": "keep_literal",
                "steps": [
                    {
                        "name": "login",
                        "method": "POST",
                        "path": "/login",
                        "extract": {"token": {"json": "$.nope"}},
                    },
                    {
                        "name": "me",
                        "path": "/me",
                        "headers": {"Authorization": "Bearer ${token}"},
                    },
                ],
            }
        )
        self.assertGreater(stats.extract_failures, 0)
        self.assertGreater(self.me_calls, 0)
        self.assertEqual(stats.template_errors, 0)
        # keep_literal sends the placeholder verbatim, so the server rejects it.
        self.assertIn("${token}", self.presented)

    async def test_template_error_aborts_and_is_counted(self):
        # think_time paces the retry: no request is sent, so without it the
        # iteration would just spin for the whole duration.
        stats = await self._run(
            {
                "name": "Template Error",
                "think_time": 0.02,
                "steps": [
                    {"name": "echo", "path": "/echo/${never_set}"},
                    {"name": "me", "path": "/me"},
                ],
            },
            duration=0.5,
        )
        self.assertGreater(stats.template_errors, 0)
        self.assertEqual(self.me_calls, 0)
        self.assertEqual(self.paths_seen, [])
        self.assertTrue(any("TemplateError" in k for k in stats.error_types))

    async def test_extract_failure_does_not_double_count_http_error(self):
        # /me without a token answers 401, which is already counted as an error;
        # the failed extraction on that same response must not add a second one.
        stats = await self._run(
            {
                "name": "No Double Count",
                "on_extract_failure": "continue",
                "steps": [
                    {
                        "name": "me",
                        "path": "/me",
                        "extract": {"token": {"json": "$.access_token"}},
                    },
                ],
            }
        )
        self.assertGreater(stats.extract_failures, 0)
        self.assertEqual(stats.errors, stats.total_requests)
        self.assertGreater(stats.status_codes.get(401, 0), 0)

    async def test_error_total_charges_an_iteration_once(self):
        # login succeeds (200) but its extraction fails, so the following
        # ${token} cannot resolve. Both are symptoms of one broken iteration:
        # the dedicated counters see both, Total Errors sees one.
        stats = await self._run(
            {
                "name": "Single Charge",
                "on_extract_failure": "continue",
                "steps": [
                    {
                        "name": "login",
                        "method": "POST",
                        "path": "/login",
                        "extract": {"token": {"json": "$.nope"}},
                    },
                    {
                        "name": "me",
                        "path": "/me",
                        "headers": {"Authorization": "Bearer ${token}"},
                    },
                ],
            }
        )
        self.assertGreater(stats.extract_failures, 0)
        self.assertGreater(stats.template_errors, 0)
        # One iteration == one login request == one error.
        self.assertEqual(stats.errors, stats.total_requests)
        self.assertLess(stats.errors, stats.extract_failures + stats.template_errors)
        self.assertEqual(self.me_calls, 0)


class TestPlainStepsSkipResponseCapture(_CorrelationServerMixin, AioHTTPTestCase):
    """A step without extract rules must not retain the response body.

    The body has always been read (``_execute_request`` calls ``resp.read()``
    unconditionally); this pins the fact that steps without extraction hold on
    to nothing extra, so plain benchmarks keep their current memory profile.
    """

    async def test_capture_is_opt_in(self):
        from pywrkr.workers import _execute_request

        captured: list[object] = []
        real_execute = _execute_request

        async def spy(*args, **kwargs):
            result = await real_execute(*args, **kwargs)
            captured.append((kwargs.get("capture_response", False), result.body, result.headers))
            return result

        with patch("pywrkr.workers._execute_request", spy):
            await self._run(
                {
                    "name": "Mixed",
                    "steps": [
                        {"name": "plain", "path": "/form"},
                        {
                            "name": "capturing",
                            "path": "/form",
                            "extract": {"csrf": {"regex": 'value="([^"]+)"'}},
                        },
                    ],
                },
                duration=0.5,
            )

        self.assertTrue(captured)
        plain = [c for c in captured if c[0] is False]
        capturing = [c for c in captured if c[0] is True]
        self.assertTrue(plain)
        self.assertTrue(capturing)
        self.assertTrue(all(body is None and headers is None for _, body, headers in plain))
        self.assertTrue(all(body is not None for _, body, _ in capturing))


if __name__ == "__main__":
    unittest.main()
