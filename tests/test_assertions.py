#!/usr/bin/env python3
"""Tests for per-step assertions and per-step reporting."""

import json
import os
import tempfile
import unittest
from io import StringIO
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase

import pywrkr
from pywrkr.assertions import (
    ANY_VALUE,
    AssertionFailure,
    StepAssertions,
    evaluate_assertions,
    parse_duration,
    parse_step_assertions,
)
from pywrkr.reporting import build_step_stats, print_step_table

OK_BODY = json.dumps({"id": 42, "email": "a@b.c", "active": True, "score": 1.5}).encode()
OK_HEADERS = {"Content-Type": "application/json; charset=utf-8", "X-Trace": "abc123"}


def keys(failures):
    return [f.key for f in failures]


# ---------------------------------------------------------------------------
# Duration parsing
# ---------------------------------------------------------------------------


class TestParseDuration(unittest.TestCase):
    def test_units(self):
        self.assertAlmostEqual(parse_duration("500ms", "x"), 0.5)
        self.assertAlmostEqual(parse_duration("1.5s", "x"), 1.5)
        self.assertAlmostEqual(parse_duration("250us", "x"), 0.00025)

    def test_bare_number_is_seconds(self):
        self.assertAlmostEqual(parse_duration("2", "x"), 2.0)
        self.assertAlmostEqual(parse_duration(2, "x"), 2.0)
        self.assertAlmostEqual(parse_duration(0.25, "x"), 0.25)

    def test_whitespace(self):
        self.assertAlmostEqual(parse_duration("  750 ms ", "x"), 0.75)

    def test_rejects_nonsense(self):
        for raw in ("soon", "", "5 minutes", "-1ms", "0", 0, -3, True, None, [1]):
            with self.assertRaises(ValueError, msg=repr(raw)):
                parse_duration(raw, "step 'assert_max_latency'")

    def test_error_names_the_location(self):
        with self.assertRaises(ValueError) as ctx:
            parse_duration("soon", "Step 2 'assert_max_latency'")
        self.assertIn("Step 2 'assert_max_latency'", str(ctx.exception))


# ---------------------------------------------------------------------------
# Load-time parsing / validation
# ---------------------------------------------------------------------------


class TestParseStepAssertions(unittest.TestCase):
    def test_empty_step_asserts_nothing(self):
        rules = parse_step_assertions({}, "Step 0")
        self.assertFalse(rules.any)
        self.assertFalse(rules.needs_body)

    def test_full_step(self):
        rules = parse_step_assertions(
            {
                "assert_status": 200,
                "assert_body_contains": "email",
                "assert_body_regex": r'"id":\s*42',
                "assert_json": {"$.id": 42, "$.email": ANY_VALUE},
                "assert_header": {"Content-Type": "application/json"},
                "assert_max_latency": "500ms",
            },
            "Step 0",
        )
        self.assertTrue(rules.any)
        self.assertTrue(rules.needs_body)
        self.assertEqual(rules.status, 200)
        self.assertEqual(len(rules.json_rules), 2)
        self.assertEqual(len(rules.header_rules), 1)
        self.assertAlmostEqual(rules.max_latency, 0.5)

    def test_needs_body_only_for_body_rules(self):
        header_only = parse_step_assertions(
            {"assert_status": 200, "assert_header": {"X": "y"}, "assert_max_latency": "1s"},
            "Step 0",
        )
        self.assertTrue(header_only.any)
        self.assertFalse(header_only.needs_body)

    def test_bad_status_type(self):
        with self.assertRaises(ValueError) as ctx:
            parse_step_assertions({"assert_status": "200"}, "Step 1")
        self.assertIn("Step 1 'assert_status'", str(ctx.exception))

    def test_bad_body_regex(self):
        with self.assertRaises(ValueError) as ctx:
            parse_step_assertions({"assert_body_regex": "(unclosed"}, "Step 1")
        self.assertIn("invalid regex", str(ctx.exception))

    def test_bad_json_path(self):
        with self.assertRaises(ValueError) as ctx:
            parse_step_assertions({"assert_json": {"$.a[*]": 1}}, "Step 1")
        self.assertIn("Step 1 assert_json", str(ctx.exception))

    def test_json_must_be_an_object(self):
        with self.assertRaises(ValueError):
            parse_step_assertions({"assert_json": ["$.a"]}, "Step 1")

    def test_json_expected_must_be_scalar(self):
        with self.assertRaises(ValueError) as ctx:
            parse_step_assertions({"assert_json": {"$.a": {"nested": 1}}}, "Step 1")
        self.assertIn("must be a scalar", str(ctx.exception))

    def test_header_regex_form(self):
        rules = parse_step_assertions(
            {"assert_header": {"X-Trace": {"regex": "^[a-f0-9]+$"}}}, "Step 0"
        )
        self.assertIsNotNone(rules.header_rules[0].pattern)

    def test_header_bad_regex(self):
        with self.assertRaises(ValueError) as ctx:
            parse_step_assertions({"assert_header": {"X": {"regex": "("}}}, "Step 1")
        self.assertIn("invalid regex", str(ctx.exception))

    def test_header_unknown_object_key(self):
        with self.assertRaises(ValueError) as ctx:
            parse_step_assertions({"assert_header": {"X": {"equals": "y"}}}, "Step 1")
        self.assertIn('{"regex": "..."}', str(ctx.exception))

    def test_header_bad_value_type(self):
        for bad in ([1], True, None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                parse_step_assertions({"assert_header": {"X": bad}}, "Step 1")

    def test_header_must_be_an_object(self):
        with self.assertRaises(ValueError):
            parse_step_assertions({"assert_header": "Content-Type"}, "Step 1")

    def test_bad_max_latency(self):
        with self.assertRaises(ValueError) as ctx:
            parse_step_assertions({"assert_max_latency": "soon"}, "Step 3")
        self.assertIn("Step 3 'assert_max_latency'", str(ctx.exception))


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


class TestEvaluateAssertions(unittest.TestCase):
    def _check(self, spec, status=200, body=OK_BODY, headers=None, latency=0.01):
        rules = parse_step_assertions(spec, "Step 0")
        return evaluate_assertions(
            rules, status, body, OK_HEADERS if headers is None else headers, latency
        )

    def test_everything_passing(self):
        self.assertEqual(
            self._check(
                {
                    "assert_status": 200,
                    "assert_body_contains": "email",
                    "assert_body_regex": r'"id":\s*42',
                    "assert_json": {"$.id": 42, "$.email": ANY_VALUE},
                    "assert_header": {
                        "X-Trace": "abc123",
                        "Content-Type": {"regex": "^application/json"},
                    },
                    "assert_max_latency": "500ms",
                }
            ),
            [],
        )

    def test_status_mismatch(self):
        failures = self._check({"assert_status": 200}, status=503)
        self.assertEqual(keys(failures), ["AssertStatus: expected 200, got 503"])

    def test_body_contains(self):
        self.assertEqual(
            keys(self._check({"assert_body_contains": "nope"})),
            ["AssertBody: 'nope' not found"],
        )

    def test_body_regex(self):
        failures = self._check({"assert_body_regex": r'"id":\s*99'})
        self.assertTrue(failures[0].key.startswith("AssertBodyRegex:"))

    def test_json_value_mismatch(self):
        failures = self._check({"assert_json": {"$.id": 99}})
        self.assertEqual(failures[0].key, "AssertJson: $.id != 99")
        self.assertIn("42", failures[0].detail)

    def test_json_missing_path(self):
        failures = self._check({"assert_json": {"$.nope": ANY_VALUE}})
        self.assertIn("not found", failures[0].key)

    def test_json_any_value_only_needs_existence(self):
        self.assertEqual(self._check({"assert_json": {"$.email": ANY_VALUE}}), [])

    def test_json_numeric_forms_match(self):
        # 1.5 in the file against 1.5 in the payload, and "42" against 42:
        # the user cannot see the payload's Python type, so do not fail on it.
        self.assertEqual(self._check({"assert_json": {"$.score": 1.5, "$.id": "42"}}), [])

    def test_json_booleans_are_not_numbers(self):
        failures = self._check({"assert_json": {"$.active": 1}})
        self.assertEqual(len(failures), 1)

    def test_json_bool_matches_bool(self):
        self.assertEqual(self._check({"assert_json": {"$.active": True}}), [])

    def test_json_invalid_body(self):
        failures = self._check({"assert_json": {"$.id": 1}}, body=b"<html>")
        self.assertIn("not valid JSON", failures[0].key)

    def test_json_missing_body(self):
        failures = self._check({"assert_json": {"$.id": 1}}, body=None)
        self.assertIn("not captured", failures[0].key)

    def test_header_exact_mismatch(self):
        failures = self._check({"assert_header": {"X-Trace": "zzz"}})
        self.assertEqual(failures[0].key, "AssertHeader: X-Trace == 'zzz'")
        self.assertIn("abc123", failures[0].detail)

    def test_header_missing(self):
        self.assertEqual(
            keys(self._check({"assert_header": {"X-Absent": "y"}})),
            ["AssertHeader: X-Absent missing"],
        )

    def test_header_regex(self):
        self.assertEqual(self._check({"assert_header": {"X-Trace": {"regex": "^abc"}}}), [])
        failures = self._check({"assert_header": {"X-Trace": {"regex": "^zzz"}}})
        self.assertIn("~ ^zzz", failures[0].key)

    def test_max_latency(self):
        failures = self._check({"assert_max_latency": "5ms"}, latency=0.05)
        self.assertEqual(failures[0].key, "AssertMaxLatency: over 5.0ms")
        self.assertIn("50.0ms", failures[0].detail)

    def test_max_latency_within_budget(self):
        self.assertEqual(self._check({"assert_max_latency": "500ms"}, latency=0.05), [])

    def test_several_failures_are_reported_separately(self):
        failures = self._check(
            {"assert_status": 201, "assert_body_contains": "nope", "assert_json": {"$.id": 99}},
            status=200,
        )
        self.assertEqual(len(failures), 3)
        self.assertEqual(len(set(keys(failures))), 3)

    def test_body_rules_skipped_when_nothing_needs_the_body(self):
        # A step asserting only on status/headers never decodes the body.
        rules = StepAssertions(status=200)
        self.assertEqual(evaluate_assertions(rules, 200, None, {}, 0.01), [])


class TestErrorKeyCardinality(unittest.TestCase):
    """Keys must come from the rule, never from the observation.

    Folding an observed latency or payload value into the key mints a fresh key
    per request, which blows through the error breakdown's key cap and turns
    the one view that should aggregate into noise.
    """

    def test_max_latency_key_is_stable_across_observations(self):
        rules = parse_step_assertions({"assert_max_latency": "10ms"}, "Step 0")
        observed = {
            evaluate_assertions(rules, 200, None, {}, latency)[0].key
            for latency in (0.011, 0.012, 0.0999, 0.5, 1.234)
        }
        self.assertEqual(len(observed), 1)

    def test_json_mismatch_key_is_stable_across_values(self):
        rules = parse_step_assertions({"assert_json": {"$.id": 1}}, "Step 0")
        # Every observed value differs from the expected 1, so each one fails.
        observed = {
            evaluate_assertions(rules, 200, json.dumps({"id": i}).encode(), {}, 0.01)[0].key
            for i in range(2, 52)
        }
        self.assertEqual(len(observed), 1)

    def test_header_mismatch_key_is_stable_across_values(self):
        rules = parse_step_assertions({"assert_header": {"X-Id": "expected"}}, "Step 0")
        observed = {
            evaluate_assertions(rules, 200, None, {"X-Id": f"req-{i}"}, 0.01)[0].key
            for i in range(50)
        }
        self.assertEqual(len(observed), 1)

    def test_detail_still_carries_the_observation(self):
        rules = parse_step_assertions({"assert_max_latency": "10ms"}, "Step 0")
        failure = evaluate_assertions(rules, 200, None, {}, 0.25)[0]
        self.assertIn("250.0ms", failure.detail)
        self.assertIn("250.0ms", failure.message)

    def test_message_without_detail_is_just_the_key(self):
        self.assertEqual(AssertionFailure("k").message, "k")


# ---------------------------------------------------------------------------
# Scenario loading
# ---------------------------------------------------------------------------


class TestScenarioAssertionLoading(unittest.TestCase):
    def _load(self, data):
        handle = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        json.dump(data, handle)
        handle.close()
        try:
            return pywrkr.load_scenario(handle.name)
        finally:
            os.unlink(handle.name)

    def test_assertions_are_compiled(self):
        scenario = self._load(
            {
                "steps": [
                    {
                        "path": "/",
                        "assert_status": 200,
                        "assert_json": {"$.id": 1},
                        "assert_max_latency": "250ms",
                    }
                ]
            }
        )
        rules = scenario.steps[0].assertions
        self.assertEqual(rules.status, 200)
        self.assertAlmostEqual(rules.max_latency, 0.25)

    def test_bad_assertion_fails_at_load_naming_the_step(self):
        with self.assertRaises(ValueError) as ctx:
            self._load({"steps": [{"path": "/"}, {"path": "/x", "assert_max_latency": "soon"}]})
        self.assertIn("Step 1", str(ctx.exception))

    def test_legacy_fields_still_work(self):
        scenario = self._load(
            {"steps": [{"path": "/", "assert_status": 204, "assert_body_contains": "ok"}]}
        )
        step = scenario.steps[0]
        self.assertEqual(step.assert_status, 204)
        self.assertEqual(step.assertions.status, 204)
        self.assertEqual(step.assertions.body_contains, "ok")

    def test_hand_built_step_still_asserts(self):
        # Constructing a ScenarioStep directly is part of the public API, and
        # its legacy fields must still produce compiled assertions.
        step = pywrkr.ScenarioStep(path="/", assert_status=201, assert_body_contains="hi")
        self.assertEqual(step.assertions.status, 201)
        self.assertEqual(step.assertions.body_contains, "hi")
        self.assertTrue(step.assertions.any)

    def test_hand_built_step_without_assertions(self):
        self.assertFalse(pywrkr.ScenarioStep(path="/").assertions.any)


# ---------------------------------------------------------------------------
# Per-step reporting
# ---------------------------------------------------------------------------


class TestStepStats(unittest.TestCase):
    def _stats(self):
        stats = pywrkr.WorkerStats()
        stats.step_latencies["login"] = [0.01, 0.02, 0.03, 0.04]
        stats.step_latencies["checkout"] = [1.0, 2.0]
        stats.step_errors["checkout"] = 2
        return stats

    def test_blocks_carry_counts_errors_and_percentiles(self):
        blocks = build_step_stats(self._stats(), duration=2.0)
        self.assertEqual(set(blocks), {"login", "checkout"})
        login = blocks["login"]
        self.assertEqual(login["count"], 4)
        self.assertEqual(login["errors"], 0)
        self.assertEqual(login["requests_per_sec"], 2.0)
        self.assertEqual(login["max"], 0.04)
        for key in ("p50", "p95", "p99", "mean", "median", "min", "stdev"):
            self.assertIn(key, login)

    def test_errors_are_attributed_per_step(self):
        blocks = build_step_stats(self._stats(), duration=2.0)
        self.assertEqual(blocks["checkout"]["errors"], 2)
        self.assertEqual(blocks["login"]["errors"], 0)

    def test_a_slow_step_is_visible_separately(self):
        # The whole point: an aggregate p95 would blend these two together.
        blocks = build_step_stats(self._stats(), duration=2.0)
        self.assertLess(blocks["login"]["max"], blocks["checkout"]["max"] / 10)

    def test_empty_and_non_finite_are_skipped(self):
        stats = pywrkr.WorkerStats()
        stats.step_latencies["empty"] = []
        stats.step_latencies["nan"] = [float("inf")]
        self.assertEqual(build_step_stats(stats, 1.0), {})

    def test_single_sample_has_no_stdev(self):
        stats = pywrkr.WorkerStats()
        stats.step_latencies["one"] = [0.5]
        self.assertNotIn("stdev", build_step_stats(stats, 1.0)["one"])

    def test_zero_duration(self):
        stats = pywrkr.WorkerStats()
        stats.step_latencies["a"] = [0.1]
        self.assertEqual(build_step_stats(stats, 0.0)["a"]["requests_per_sec"], 0.0)

    def test_table_renders_every_column(self):
        out = StringIO()
        print_step_table(build_step_stats(self._stats(), 2.0), file=out)
        text = out.getvalue()
        for column in ("Step", "Count", "Errors", "Req/s", "p50", "p95", "p99", "Max"):
            self.assertIn(column, text)
        self.assertIn("login", text)
        self.assertIn("checkout", text)

    def test_table_of_nothing_prints_nothing(self):
        out = StringIO()
        print_step_table({}, file=out)
        self.assertEqual(out.getvalue(), "")

    def test_json_output_keeps_the_step_stats_key(self):
        # Renaming it would break consumers written against earlier releases.
        results = pywrkr.build_results_dict(self._stats(), 2.0, 1)
        self.assertIn("step_stats", results)
        self.assertEqual(results["step_stats"]["checkout"]["errors"], 2)


class TestStepErrorPlumbing(unittest.TestCase):
    def test_merge_sums_step_errors(self):
        a, b = pywrkr.WorkerStats(), pywrkr.WorkerStats()
        a.step_latencies["s"] = [0.1]
        a.step_errors["s"] = 2
        b.step_latencies["s"] = [0.2]
        b.step_errors["s"] = 3
        merged = pywrkr.merge_stats([a, b])
        self.assertEqual(merged.step_errors["s"], 5)
        self.assertEqual(len(merged.step_latencies["s"]), 2)

    def test_distributed_round_trip(self):
        from pywrkr.distributed import _deserialize_stats, _serialize_stats

        stats = pywrkr.WorkerStats()
        stats.step_latencies["s"] = [0.1]
        stats.step_errors["s"] = 4
        restored = _deserialize_stats(json.loads(json.dumps(_serialize_stats(stats))))
        self.assertEqual(restored.step_errors["s"], 4)

    def test_two_workers_merge_per_step_stats(self):
        # What the master does with per-worker results.
        from pywrkr.distributed import _deserialize_stats, _serialize_stats, merge_worker_stats

        payloads = []
        for errors in (1, 2):
            ws = pywrkr.WorkerStats()
            ws.total_requests = 10
            ws.step_latencies["checkout"] = [0.1] * 5
            ws.step_errors["checkout"] = errors
            payloads.append(json.loads(json.dumps(_serialize_stats(ws))))

        merged = merge_worker_stats([_deserialize_stats(p) for p in payloads])
        self.assertEqual(merged.step_errors["checkout"], 3)
        self.assertEqual(len(merged.step_latencies["checkout"]), 10)
        blocks = build_step_stats(merged, 1.0)
        self.assertEqual(blocks["checkout"]["count"], 10)
        self.assertEqual(blocks["checkout"]["errors"], 3)

    def test_step_error_keys_are_capped(self):
        from pywrkr.workers import _record_step_error

        stats = pywrkr.WorkerStats()
        for i in range(600):
            _record_step_error(stats, f"step-{i}")
        self.assertLessEqual(len(stats.step_errors), 501)
        self.assertIn("[other steps]", stats.step_errors)


# ---------------------------------------------------------------------------
# Integration
# ---------------------------------------------------------------------------


class TestAssertionsIntegration(AioHTTPTestCase):
    async def get_application(self):
        app = web.Application()
        app.router.add_get("/user", self.handle_user)
        app.router.add_get("/slow", self.handle_slow)
        app.router.add_get("/plain", self.handle_plain)
        return app

    async def handle_user(self, request):
        return web.json_response({"id": 42, "email": "a@b.c"}, headers={"X-Trace": "abc123"})

    async def handle_slow(self, request):
        import asyncio

        await asyncio.sleep(0.05)
        return web.json_response({"id": 7})

    async def handle_plain(self, request):
        return web.Response(text="plain text")

    def _url(self):
        return f"http://127.0.0.1:{self.server.port}"

    async def _run(self, steps, users=1, duration=1.0, think_time=0.02):
        handle = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        json.dump({"name": "asserts", "base_url": self._url(), "steps": steps}, handle)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        config = pywrkr.BenchmarkConfig(
            url=self._url(),
            users=users,
            duration=duration,
            think_time=think_time,
            think_time_jitter=0.0,
            ramp_up=0.0,
            timeout_sec=5,
            scenario=pywrkr.load_scenario(handle.name),
        )
        out = StringIO()
        with patch("sys.stdout", out):
            stats, code = await pywrkr.run_user_simulation(config)
        return stats, code, out.getvalue()

    async def test_all_assertion_types_pass_together(self):
        stats, _, _ = await self._run(
            [
                {
                    "name": "user",
                    "path": "/user",
                    "assert_status": 200,
                    "assert_body_contains": "email",
                    "assert_body_regex": r'"id":\s*42',
                    "assert_json": {"$.id": 42, "$.email": "*"},
                    "assert_header": {
                        "X-Trace": "abc123",
                        "Content-Type": {"regex": "^application/json"},
                    },
                    "assert_max_latency": "2s",
                }
            ]
        )
        self.assertGreater(stats.total_requests, 0)
        self.assertEqual(stats.errors, 0)
        self.assertEqual(dict(stats.error_types), {})

    async def test_failures_land_in_the_error_breakdown(self):
        stats, _, text = await self._run(
            [{"name": "user", "path": "/user", "assert_json": {"$.id": 99, "$.nope": "*"}}]
        )
        self.assertEqual(stats.errors, stats.total_requests)
        self.assertIn("AssertJson: $.id != 99", stats.error_types)
        self.assertTrue(any("nope" in key for key in stats.error_types))
        self.assertIn("AssertJson", text)

    async def test_max_latency_failure(self):
        stats, _, _ = await self._run(
            [{"name": "slow", "path": "/slow", "assert_max_latency": "5ms"}]
        )
        self.assertEqual(stats.errors, stats.total_requests)
        self.assertIn("AssertMaxLatency: over 5.0ms", stats.error_types)

    async def test_header_failure(self):
        stats, _, _ = await self._run(
            [{"name": "plain", "path": "/plain", "assert_header": {"X-Trace": "abc123"}}]
        )
        self.assertIn("AssertHeader: X-Trace missing", stats.error_types)

    async def test_one_error_per_request_however_many_rules_break(self):
        stats, _, _ = await self._run(
            [
                {
                    "name": "user",
                    "path": "/user",
                    "assert_status": 500,
                    "assert_body_contains": "nope",
                    "assert_json": {"$.id": 99},
                }
            ]
        )
        # Three rules break per request, but the request is one error.
        self.assertEqual(stats.errors, stats.total_requests)
        self.assertEqual(len(stats.error_types), 3)

    async def test_assertion_failures_count_toward_error_rate_thresholds(self):
        handle = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        json.dump(
            {
                "name": "asserts",
                "base_url": self._url(),
                "steps": [{"name": "user", "path": "/user", "assert_status": 500}],
            },
            handle,
        )
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        config = pywrkr.BenchmarkConfig(
            url=self._url(),
            users=1,
            duration=0.5,
            think_time=0.02,
            ramp_up=0.0,
            timeout_sec=5,
            scenario=pywrkr.load_scenario(handle.name),
            thresholds=[pywrkr.parse_threshold("error_rate < 10%")],
        )
        with patch("sys.stdout", new_callable=StringIO):
            _, code = await pywrkr.run_user_simulation(config)
        self.assertEqual(code, 2)

    async def test_per_step_table_attributes_errors_to_the_right_step(self):
        stats, _, text = await self._run(
            [
                {"name": "good", "path": "/user", "assert_status": 200},
                {"name": "bad", "path": "/user", "assert_status": 500},
            ]
        )
        blocks = build_step_stats(stats, 1.0)
        self.assertEqual(blocks["good"]["errors"], 0)
        self.assertEqual(blocks["bad"]["errors"], blocks["bad"]["count"])
        self.assertIn("PER-STEP BREAKDOWN", text)
        self.assertIn("good", text)
        self.assertIn("bad", text)

    async def test_per_step_json_and_html(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        json_path = os.path.join(tmp.name, "r.json")
        html_path = os.path.join(tmp.name, "r.html")

        handle = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        json.dump(
            {
                "name": "asserts",
                "base_url": self._url(),
                "steps": [
                    {"name": "fast", "path": "/user"},
                    {"name": "slow", "path": "/slow"},
                ],
            },
            handle,
        )
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        config = pywrkr.BenchmarkConfig(
            url=self._url(),
            users=1,
            duration=0.6,
            think_time=0.0,
            ramp_up=0.0,
            timeout_sec=5,
            scenario=pywrkr.load_scenario(handle.name),
            json_output=json_path,
            html_report=html_path,
        )
        with patch("sys.stdout", new_callable=StringIO):
            await pywrkr.run_user_simulation(config)

        with open(json_path, encoding="utf-8") as fh:
            results = json.load(fh)
        self.assertEqual(set(results["step_stats"]), {"fast", "slow"})
        self.assertGreater(
            results["step_stats"]["slow"]["p95"], results["step_stats"]["fast"]["p95"]
        )

        with open(html_path, encoding="utf-8") as fh:
            html = fh.read()
        self.assertIn("Per-Step Breakdown", html)
        self.assertIn("fast", html)
        self.assertIn("slow", html)

    async def test_existing_scenarios_behave_identically(self):
        # Only the two legacy assertion keys, exactly as before this change.
        stats, _, _ = await self._run(
            [
                {"name": "ok", "path": "/user", "assert_status": 200},
                {"name": "nope", "path": "/user", "assert_body_contains": "absent"},
            ]
        )
        self.assertIn("AssertBody: 'absent' not found", stats.error_types)
        self.assertEqual(stats.errors, stats.total_requests // 2)


if __name__ == "__main__":
    unittest.main()
