#!/usr/bin/env python3
"""Tests for data feeders and built-in template functions."""

import json
import os
import tempfile
import unittest
from io import StringIO
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase

import pywrkr
from pywrkr.config import parse_data_spec, validate_scenario_templates
from pywrkr.feeders import (
    CONSUMING_STRATEGIES,
    FEEDER_STRATEGIES,
    DataRuntime,
    Feeder,
    FeederCursor,
    load_feeder,
    shard_rows,
    validate_unique_capacity,
)
from pywrkr.main import _build_parser, _parse_and_validate_args
from pywrkr.templating import (
    TemplateError,
    TemplateFunctions,
    iter_placeholders,
    substitute,
    validate_function_call,
)


def write_file(contents: str, suffix: str) -> str:
    """Write *contents* to a temp file and return its path."""
    handle = tempfile.NamedTemporaryFile(mode="w", suffix=suffix, delete=False, encoding="utf-8")
    handle.write(contents)
    handle.close()
    return handle.name


USERS_CSV = "username,password\nalice,pw-a\nbob,pw-b\ncarol,pw-c\n"


# ---------------------------------------------------------------------------
# Placeholder grammar
# ---------------------------------------------------------------------------


class TestPlaceholderGrammar(unittest.TestCase):
    def _kinds(self, text):
        out = []
        for match in iter_placeholders(text):
            if match.group("func") is not None:
                out.append(("func", match.group("func"), match.group("args")))
            elif match.group("dataset") is not None:
                out.append(("data", match.group("dataset"), match.group("field")))
            else:
                out.append(("var", match.group("var"), None))
        return out

    def test_variable(self):
        self.assertEqual(self._kinds("${token}"), [("var", "token", None)])

    def test_dataset_field(self):
        self.assertEqual(self._kinds("${users.name}"), [("data", "users", "name")])

    def test_dataset_field_allows_awkward_header_names(self):
        # CSV headers are not identifiers; only the characters that would make
        # the placeholder ambiguous are excluded.
        self.assertEqual(self._kinds("${users.user-name}"), [("data", "users", "user-name")])

    def test_function_without_args(self):
        self.assertEqual(self._kinds("${uuid()}"), [("func", "uuid", "")])

    def test_function_with_args(self):
        self.assertEqual(self._kinds("${randint(1, 10)}"), [("func", "randint", "1, 10")])

    def test_mixed(self):
        self.assertEqual(
            self._kinds("${a}/${d.f}/${uuid()}"),
            [("var", "a", None), ("data", "d", "f"), ("func", "uuid", "")],
        )

    def test_non_placeholders_are_ignored(self):
        for text in ("${1bad}", "$VAR", "${a.b.c}", "${a b}", "${}", "${a(}", "money: $5"):
            self.assertEqual(self._kinds(text), [], text)

    def test_no_placeholder_short_circuits(self):
        self.assertEqual(iter_placeholders("/plain/path"), [])


# ---------------------------------------------------------------------------
# Built-in functions
# ---------------------------------------------------------------------------


class TestTemplateFunctions(unittest.TestCase):
    def setUp(self):
        self.fn = TemplateFunctions()

    def test_uuid_is_unique_and_well_formed(self):
        values = {self.fn.call("uuid", "") for _ in range(50)}
        self.assertEqual(len(values), 50)
        for value in values:
            self.assertRegex(value, r"\A[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-")

    def test_randint_stays_in_range(self):
        for _ in range(100):
            self.assertIn(int(self.fn.call("randint", "3,5")), (3, 4, 5))

    def test_randint_single_value_range(self):
        self.assertEqual(self.fn.call("randint", "7,7"), "7")

    def test_randint_negative_bounds(self):
        self.assertIn(int(self.fn.call("randint", "-2,-1")), (-2, -1))

    def test_randstr_length_and_alphabet(self):
        value = self.fn.call("randstr", "12")
        self.assertEqual(len(value), 12)
        self.assertRegex(value, r"\A[A-Za-z0-9]+\Z")

    def test_counter_is_monotonic(self):
        self.assertEqual([self.fn.call("counter", "") for _ in range(4)], ["1", "2", "3", "4"])

    def test_named_counters_are_independent(self):
        self.assertEqual(self.fn.call("counter", "orders"), "1")
        self.assertEqual(self.fn.call("counter", "users"), "1")
        self.assertEqual(self.fn.call("counter", "orders"), "2")
        self.assertEqual(self.fn.call("counter", ""), "1")

    def test_now_is_iso8601(self):
        value = self.fn.call("now", "")
        self.assertRegex(value, r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
        self.assertIn("+00:00", value)

    def test_now_unix_is_an_epoch_int(self):
        self.assertGreater(int(self.fn.call("now", "unix")), 1_600_000_000)

    def test_unknown_function(self):
        with self.assertRaises(TemplateError) as ctx:
            self.fn.call("nope", "")
        self.assertIn("unknown function", str(ctx.exception))


class TestValidateFunctionCall(unittest.TestCase):
    def test_accepts_every_builtin(self):
        for name, raw in (
            ("uuid", ""),
            ("randint", "1,2"),
            ("randstr", "4"),
            ("counter", ""),
            ("counter", "orders"),
            ("now", ""),
            ("now", "unix"),
        ):
            validate_function_call(name, raw)

    def test_unknown_function_lists_the_alternatives(self):
        with self.assertRaises(ValueError) as ctx:
            validate_function_call("nope", "")
        self.assertIn("uuid()", str(ctx.exception))

    def test_wrong_arity(self):
        for name, raw in (("uuid", "1"), ("randint", "1"), ("randstr", ""), ("now", "a,b")):
            with self.assertRaises(ValueError, msg=f"{name}({raw})"):
                validate_function_call(name, raw)

    def test_randint_non_integer(self):
        with self.assertRaises(ValueError) as ctx:
            validate_function_call("randint", "a,b")
        self.assertIn("must be an integer", str(ctx.exception))

    def test_randint_empty_range(self):
        with self.assertRaises(ValueError) as ctx:
            validate_function_call("randint", "9,2")
        self.assertIn("empty range", str(ctx.exception))

    def test_randstr_non_positive(self):
        with self.assertRaises(ValueError) as ctx:
            validate_function_call("randstr", "0")
        self.assertIn("at least 1", str(ctx.exception))

    def test_counter_bad_name(self):
        with self.assertRaises(ValueError) as ctx:
            validate_function_call("counter", "1bad")
        self.assertIn("identifier", str(ctx.exception))

    def test_now_bad_format(self):
        with self.assertRaises(ValueError) as ctx:
            validate_function_call("now", "iso")
        self.assertIn("unix", str(ctx.exception))


class TestSubstituteWithDataAndFunctions(unittest.TestCase):
    def test_dataset_field(self):
        rows = {"users": {"username": "alice"}}
        self.assertEqual(substitute("hi ${users.username}", {}, rows=rows), "hi alice")

    def test_function(self):
        out = substitute("${counter()}", {}, functions=TemplateFunctions())
        self.assertEqual(out, "1")

    def test_all_three_kinds_together(self):
        out = substitute(
            "${tok}/${u.name}/${counter()}",
            {"tok": "T"},
            rows={"u": {"name": "alice"}},
            functions=TemplateFunctions(),
        )
        self.assertEqual(out, "T/alice/1")

    def test_unknown_dataset(self):
        with self.assertRaises(TemplateError) as ctx:
            substitute("${users.name}", {}, rows={})
        self.assertIn("no data set 'users'", str(ctx.exception))

    def test_unknown_field_lists_available(self):
        with self.assertRaises(TemplateError) as ctx:
            substitute("${users.nope}", {}, rows={"users": {"a": "1", "b": "2"}})
        message = str(ctx.exception)
        self.assertIn("has no field 'nope'", message)
        self.assertIn("a, b", message)

    def test_function_without_runtime(self):
        with self.assertRaises(TemplateError) as ctx:
            substitute("${uuid()}", {})
        self.assertIn("no function support", str(ctx.exception))

    def test_bad_function_call(self):
        with self.assertRaises(TemplateError) as ctx:
            substitute("${randint(9,2)}", {}, functions=TemplateFunctions())
        self.assertIn("empty range", str(ctx.exception))

    def test_keep_literal_covers_every_kind(self):
        out = substitute("${v}|${d.f}|${nope()}", {}, keep_literal=True)
        self.assertEqual(out, "${v}|${d.f}|${nope()}")

    def test_problems_are_deduplicated(self):
        with self.assertRaises(TemplateError) as ctx:
            substitute("${a}${a}${b}", {})
        message = str(ctx.exception)
        self.assertEqual(message.count("${a}"), 1)
        self.assertEqual(message.count("${b}"), 1)


# ---------------------------------------------------------------------------
# Loading data files
# ---------------------------------------------------------------------------


class TestLoadFeeder(unittest.TestCase):
    def _csv(self, text=USERS_CSV, **kwargs):
        path = write_file(text, ".csv")
        try:
            return load_feeder(kwargs.pop("name", "users"), path, **kwargs)
        finally:
            os.unlink(path)

    def _json(self, payload, **kwargs):
        path = write_file(json.dumps(payload), ".json")
        try:
            return load_feeder(kwargs.pop("name", "users"), path, **kwargs)
        finally:
            os.unlink(path)

    def test_csv_rows_and_fields(self):
        feeder = self._csv()
        self.assertEqual(feeder.fields, ("username", "password"))
        self.assertEqual(len(feeder.rows), 3)
        self.assertEqual(feeder.rows[0], {"username": "alice", "password": "pw-a"})
        self.assertEqual(feeder.strategy, "loop")

    def test_csv_quoted_values_and_blank_lines(self):
        feeder = self._csv('a,b\n"x,1",2\n\n"y",3\n')
        self.assertEqual(feeder.rows[0], {"a": "x,1", "b": "2"})
        self.assertEqual(len(feeder.rows), 2)

    def test_csv_header_whitespace_is_trimmed(self):
        self.assertEqual(self._csv(" a , b \n1,2\n").fields, ("a", "b"))

    def test_csv_bom_is_stripped(self):
        self.assertEqual(self._csv("﻿a,b\n1,2\n").fields, ("a", "b"))

    def test_json_list_of_objects(self):
        feeder = self._json([{"id": 1, "ok": True}, {"id": 2, "ok": False}])
        self.assertEqual(feeder.rows[0], {"id": "1", "ok": "true"})
        self.assertEqual(feeder.fields, ("id", "ok"))

    def test_json_null_becomes_json_null(self):
        self.assertEqual(self._json([{"a": None}]).rows[0], {"a": "null"})

    def test_missing_file(self):
        with self.assertRaises(ValueError) as ctx:
            load_feeder("users", "/nonexistent/users.csv")
        self.assertIn("file not found", str(ctx.exception))

    def test_empty_file(self):
        with self.assertRaises(ValueError) as ctx:
            self._csv("   \n")
        self.assertIn("file is empty", str(ctx.exception))

    def test_header_only(self):
        with self.assertRaises(ValueError) as ctx:
            self._csv("a,b\n")
        self.assertIn("no data rows", str(ctx.exception))

    def test_unnamed_header_column(self):
        with self.assertRaises(ValueError) as ctx:
            self._csv("a,,c\n1,2,3\n")
        self.assertIn("have no name", str(ctx.exception))

    def test_duplicate_header_columns(self):
        with self.assertRaises(ValueError) as ctx:
            self._csv("a,a\n1,2\n")
        self.assertIn("duplicate header", str(ctx.exception))

    def test_wrong_column_count(self):
        with self.assertRaises(ValueError) as ctx:
            self._csv("a,b\n1,2,3\n")
        self.assertIn("line 2", str(ctx.exception))

    def test_json_not_a_list(self):
        with self.assertRaises(ValueError) as ctx:
            self._json({"a": 1})
        self.assertIn("JSON array of objects", str(ctx.exception))

    def test_json_record_not_an_object(self):
        with self.assertRaises(ValueError) as ctx:
            self._json([1, 2])
        self.assertIn("must be an object", str(ctx.exception))

    def test_json_nested_value_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            self._json([{"a": {"b": 1}}])
        self.assertIn("must be flat", str(ctx.exception))

    def test_json_invalid(self):
        path = write_file("{not json", ".json")
        try:
            with self.assertRaises(ValueError) as ctx:
                load_feeder("users", path)
            self.assertIn("not valid JSON", str(ctx.exception))
        finally:
            os.unlink(path)

    def test_json_empty_record(self):
        with self.assertRaises(ValueError) as ctx:
            self._json([{}])
        self.assertIn("is empty", str(ctx.exception))

    def test_json_inconsistent_fields(self):
        with self.assertRaises(ValueError) as ctx:
            self._json([{"a": 1}, {"b": 2}])
        self.assertIn("different fields", str(ctx.exception))

    def test_unknown_strategy(self):
        with self.assertRaises(ValueError) as ctx:
            self._csv(strategy="sometimes")
        self.assertIn("unknown strategy", str(ctx.exception))

    def test_invalid_data_set_name(self):
        with self.assertRaises(ValueError) as ctx:
            self._csv(name="1bad")
        self.assertIn("identifier", str(ctx.exception))


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------


def make_feeder(strategy="loop", count=3, name="users"):
    rows = tuple({"i": str(i)} for i in range(count))
    return Feeder(name=name, strategy=strategy, rows=rows)


class TestFeederCursor(unittest.TestCase):
    def test_loop_wraps_around(self):
        cursor = FeederCursor(make_feeder("loop"))
        seen = [cursor.next_row()["i"] for _ in range(7)]
        self.assertEqual(seen, ["0", "1", "2", "0", "1", "2", "0"])
        self.assertFalse(cursor.exhausted)

    def test_sequential_stops_when_spent(self):
        cursor = FeederCursor(make_feeder("sequential"))
        self.assertEqual([cursor.next_row()["i"] for _ in range(3)], ["0", "1", "2"])
        self.assertIsNone(cursor.next_row())
        self.assertTrue(cursor.exhausted)

    def test_unique_never_repeats(self):
        cursor = FeederCursor(make_feeder("unique", count=5))
        seen = []
        while (row := cursor.next_row()) is not None:
            seen.append(row["i"])
        self.assertEqual(seen, ["0", "1", "2", "3", "4"])
        self.assertEqual(len(seen), len(set(seen)))
        self.assertTrue(cursor.exhausted)

    def test_random_stays_in_the_set_and_never_runs_dry(self):
        cursor = FeederCursor(make_feeder("random"))
        values = {cursor.next_row()["i"] for _ in range(50)}
        self.assertLessEqual(values, {"0", "1", "2"})
        self.assertFalse(cursor.exhausted)

    def test_empty_feeder_is_immediately_exhausted(self):
        cursor = FeederCursor(Feeder(name="x", strategy="loop", rows=()))
        self.assertIsNone(cursor.next_row())
        self.assertTrue(cursor.exhausted)

    def test_consumes_rows_flag(self):
        for strategy in FEEDER_STRATEGIES:
            expected = strategy in CONSUMING_STRATEGIES
            self.assertEqual(make_feeder(strategy).consumes_rows, expected, strategy)


class TestDataRuntime(unittest.TestCase):
    def test_draws_one_row_per_set(self):
        runtime = DataRuntime.for_feeders(
            {"a": make_feeder("loop", name="a"), "b": make_feeder("loop", name="b")}
        )
        rows = runtime.next_rows()
        self.assertEqual(set(rows), {"a", "b"})

    def test_signals_exhaustion(self):
        runtime = DataRuntime.for_feeders({"a": make_feeder("unique", count=2, name="a")})
        self.assertIsNotNone(runtime.next_rows())
        self.assertIsNotNone(runtime.next_rows())
        self.assertIsNone(runtime.next_rows())
        self.assertEqual(runtime.exhausted_feeders, ("a",))

    def test_no_feeders_yields_empty_rows(self):
        runtime = DataRuntime.for_feeders(None)
        self.assertEqual(runtime.next_rows(), {})
        self.assertEqual(runtime.exhausted_feeders, ())

    def test_functions_are_shared(self):
        runtime = DataRuntime.for_feeders({})
        self.assertEqual(runtime.functions.call("counter", ""), "1")
        self.assertEqual(runtime.functions.call("counter", ""), "2")


class TestValidateUniqueCapacity(unittest.TestCase):
    def test_passes_when_rows_cover_users(self):
        validate_unique_capacity(
            {"u": make_feeder("unique", count=5)}, users=5, num_requests=None, steps=1
        )

    def test_fails_when_users_exceed_rows(self):
        with self.assertRaises(ValueError) as ctx:
            validate_unique_capacity(
                {"u": make_feeder("unique", count=3)}, users=10, num_requests=None, steps=1
            )
        message = str(ctx.exception)
        self.assertIn("3 row(s)", message)
        self.assertIn("at least 10", message)

    def test_request_count_raises_the_bar(self):
        # 20 requests over a 2-step scenario needs 10 iterations, so 10 rows.
        with self.assertRaises(ValueError) as ctx:
            validate_unique_capacity(
                {"u": make_feeder("unique", count=4)}, users=None, num_requests=20, steps=2
            )
        self.assertIn("at least 10", str(ctx.exception))

    def test_nodes_multiply_demand(self):
        with self.assertRaises(ValueError):
            validate_unique_capacity(
                {"u": make_feeder("unique", count=5)},
                users=3,
                num_requests=None,
                steps=1,
                nodes=2,
            )

    def test_other_strategies_are_not_checked(self):
        for strategy in ("loop", "random", "sequential"):
            validate_unique_capacity(
                {"u": make_feeder(strategy, count=1)}, users=100, num_requests=None, steps=1
            )

    def test_no_feeders(self):
        validate_unique_capacity(None, users=10, num_requests=None, steps=1)


class TestShardRows(unittest.TestCase):
    ROWS = tuple({"i": str(i)} for i in range(10))

    def test_single_node_gets_everything(self):
        self.assertEqual(shard_rows(self.ROWS, 0, 1), self.ROWS)

    def test_even_split(self):
        first, second = shard_rows(self.ROWS, 0, 2), shard_rows(self.ROWS, 1, 2)
        self.assertEqual(len(first), 5)
        self.assertEqual(len(second), 5)
        self.assertEqual(list(first) + list(second), list(self.ROWS))

    def test_uneven_split_is_disjoint_and_complete(self):
        shards = [shard_rows(self.ROWS, i, 3) for i in range(3)]
        self.assertEqual([len(s) for s in shards], [4, 3, 3])
        flat = [row["i"] for shard in shards for row in shard]
        self.assertEqual(flat, [row["i"] for row in self.ROWS])
        self.assertEqual(len(flat), len(set(flat)))

    def test_more_nodes_than_rows(self):
        shards = [shard_rows(self.ROWS[:2], i, 4) for i in range(4)]
        self.assertEqual([len(s) for s in shards], [1, 1, 0, 0])


# ---------------------------------------------------------------------------
# Scenario wiring
# ---------------------------------------------------------------------------


class TestScenarioData(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.csv_path = os.path.join(self.tmp.name, "users.csv")
        with open(self.csv_path, "w", encoding="utf-8") as handle:
            handle.write(USERS_CSV)

    def _scenario(self, payload):
        path = os.path.join(self.tmp.name, "scenario.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        return pywrkr.load_scenario(path)

    def test_data_block_is_loaded(self):
        scenario = self._scenario(
            {
                "data": {"users": {"file": "users.csv", "strategy": "unique"}},
                "steps": [{"path": "/${users.username}"}],
            }
        )
        self.assertEqual(scenario.data["users"].strategy, "unique")
        self.assertEqual(len(scenario.data["users"].rows), 3)

    def test_relative_paths_resolve_against_the_scenario_file(self):
        # The scenario names "users.csv" with no directory; it resolves next to
        # the scenario, not next to the process's cwd.
        scenario = self._scenario(
            {"data": {"users": {"file": "users.csv"}}, "steps": [{"path": "/"}]}
        )
        self.assertEqual(scenario.data["users"].source, self.csv_path)

    def test_absolute_paths_are_left_alone(self):
        scenario = self._scenario(
            {"data": {"users": {"file": self.csv_path}}, "steps": [{"path": "/"}]}
        )
        self.assertEqual(scenario.data["users"].source, self.csv_path)

    def test_default_is_no_data(self):
        self.assertEqual(self._scenario({"steps": [{"path": "/"}]}).data, {})

    def test_data_must_be_an_object(self):
        with self.assertRaises(ValueError) as ctx:
            self._scenario({"data": ["users.csv"], "steps": [{"path": "/"}]})
        self.assertIn("'data' must be an object", str(ctx.exception))

    def test_entry_must_be_an_object(self):
        with self.assertRaises(ValueError) as ctx:
            self._scenario({"data": {"users": "users.csv"}, "steps": [{"path": "/"}]})
        self.assertIn("must be an object", str(ctx.exception))

    def test_entry_needs_a_file(self):
        with self.assertRaises(ValueError) as ctx:
            self._scenario({"data": {"users": {"strategy": "loop"}}, "steps": [{"path": "/"}]})
        self.assertIn("needs a 'file'", str(ctx.exception))

    def test_unknown_entry_key(self):
        with self.assertRaises(ValueError) as ctx:
            self._scenario(
                {"data": {"users": {"file": "users.csv", "mode": "x"}}, "steps": [{"path": "/"}]}
            )
        self.assertIn("unknown key", str(ctx.exception))

    def test_missing_data_file_is_a_startup_error(self):
        with self.assertRaises(ValueError) as ctx:
            self._scenario({"data": {"users": {"file": "nope.csv"}}, "steps": [{"path": "/"}]})
        self.assertIn("file not found", str(ctx.exception))

    def test_unknown_function_in_a_step_fails_at_load(self):
        with self.assertRaises(ValueError) as ctx:
            self._scenario({"steps": [{"path": "/${bogus()}"}]})
        self.assertIn("unknown function", str(ctx.exception))

    def test_bad_function_arguments_fail_at_load(self):
        with self.assertRaises(ValueError) as ctx:
            self._scenario({"steps": [{"path": "/", "body": {"n": "${randint(5,1)}"}}]})
        self.assertIn("empty range", str(ctx.exception))

    def test_unknown_field_fails_at_load(self):
        with self.assertRaises(ValueError) as ctx:
            self._scenario(
                {
                    "data": {"users": {"file": "users.csv"}},
                    "steps": [{"path": "/${users.email}"}],
                }
            )
        message = str(ctx.exception)
        self.assertIn("has no field 'email'", message)
        self.assertIn("username, password", message)

    def test_headers_and_bodies_are_validated_too(self):
        with self.assertRaises(ValueError) as ctx:
            self._scenario({"steps": [{"path": "/", "headers": {"X-Id": "${nope()}"}}]})
        self.assertIn("unknown function", str(ctx.exception))

    def test_undeclared_data_set_is_deferred_to_the_cli_merge(self):
        # --data may still supply it, so load_scenario must not reject it here.
        scenario = self._scenario({"steps": [{"path": "/${users.username}"}]})
        self.assertEqual(scenario.data, {})
        with self.assertRaises(ValueError) as ctx:
            validate_scenario_templates(scenario)
        self.assertIn("not declared", str(ctx.exception))

    def test_parse_data_spec_directly(self):
        feeders = parse_data_spec({"users": {"file": "users.csv"}}, self.tmp.name)
        self.assertEqual(feeders["users"].fields, ("username", "password"))
        self.assertEqual(parse_data_spec(None, self.tmp.name), {})


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestDataCliOptions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.csv_path = os.path.join(self.tmp.name, "users.csv")
        with open(self.csv_path, "w", encoding="utf-8") as handle:
            handle.write(USERS_CSV)
        self.scenario_path = os.path.join(self.tmp.name, "scenario.json")

    def _write_scenario(self, payload):
        with open(self.scenario_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)

    def _run(self, argv):
        parser = _build_parser()
        args = parser.parse_args(argv)
        return _parse_and_validate_args(parser, args)

    def _expect_error(self, argv):
        with self.assertRaises(SystemExit), patch("sys.stderr", new_callable=StringIO) as err:
            self._run(argv)
        return err.getvalue()

    def test_data_flag_supplies_a_set_the_file_omits(self):
        self._write_scenario({"steps": [{"path": "/${users.username}"}]})
        config, _ = self._run(
            [
                "http://example.com",
                "--scenario",
                self.scenario_path,
                "--data",
                f"users={self.csv_path}",
            ]
        )
        self.assertEqual(config.scenario.data["users"].fields, ("username", "password"))
        self.assertEqual(config.scenario.data["users"].strategy, "loop")

    def test_data_strategy_flag(self):
        self._write_scenario({"steps": [{"path": "/${users.username}"}]})
        config, _ = self._run(
            [
                "http://example.com",
                "--scenario",
                self.scenario_path,
                "--data",
                f"users={self.csv_path}",
                "--data-strategy",
                "users=random",
            ]
        )
        self.assertEqual(config.scenario.data["users"].strategy, "random")

    def test_strategy_alone_retargets_a_file_declared_set(self):
        self._write_scenario(
            {"data": {"users": {"file": "users.csv"}}, "steps": [{"path": "/${users.username}"}]}
        )
        config, _ = self._run(
            [
                "http://example.com",
                "--scenario",
                self.scenario_path,
                "--data-strategy",
                "users=sequential",
            ]
        )
        self.assertEqual(config.scenario.data["users"].strategy, "sequential")
        self.assertEqual(len(config.scenario.data["users"].rows), 3)

    def test_cli_overrides_the_scenario_file(self):
        other = os.path.join(self.tmp.name, "other.csv")
        with open(other, "w", encoding="utf-8") as handle:
            handle.write("username,password\nzed,pw-z\n")
        self._write_scenario(
            {"data": {"users": {"file": "users.csv"}}, "steps": [{"path": "/${users.username}"}]}
        )
        config, _ = self._run(
            ["http://example.com", "--scenario", self.scenario_path, "--data", f"users={other}"]
        )
        self.assertEqual(len(config.scenario.data["users"].rows), 1)

    def test_data_requires_a_scenario(self):
        message = self._expect_error(["http://example.com", "--data", f"users={self.csv_path}"])
        self.assertIn("require --scenario", message)

    def test_malformed_data_value(self):
        self._write_scenario({"steps": [{"path": "/"}]})
        message = self._expect_error(
            ["http://example.com", "--scenario", self.scenario_path, "--data", "users.csv"]
        )
        self.assertIn("expected 'NAME=VALUE'", message)

    def test_missing_data_file(self):
        self._write_scenario({"steps": [{"path": "/"}]})
        message = self._expect_error(
            [
                "http://example.com",
                "--scenario",
                self.scenario_path,
                "--data",
                "users=/nonexistent.csv",
            ]
        )
        self.assertIn("file not found", message)

    def test_strategy_for_unknown_set(self):
        self._write_scenario({"steps": [{"path": "/"}]})
        message = self._expect_error(
            [
                "http://example.com",
                "--scenario",
                self.scenario_path,
                "--data-strategy",
                "ghosts=loop",
            ]
        )
        self.assertIn("unknown data set", message)

    def test_unknown_strategy(self):
        self._write_scenario({"data": {"users": {"file": "users.csv"}}, "steps": [{"path": "/"}]})
        message = self._expect_error(
            [
                "http://example.com",
                "--scenario",
                self.scenario_path,
                "--data-strategy",
                "users=sometimes",
            ]
        )
        self.assertIn("unknown strategy", message)

    def test_undeclared_reference_is_caught_after_the_merge(self):
        self._write_scenario({"steps": [{"path": "/${ghosts.name}"}]})
        message = self._expect_error(
            [
                "http://example.com",
                "--scenario",
                self.scenario_path,
                "--data",
                f"users={self.csv_path}",
            ]
        )
        self.assertIn("not declared", message)

    def test_unique_capacity_is_checked_against_the_user_count(self):
        self._write_scenario(
            {
                "data": {"users": {"file": "users.csv", "strategy": "unique"}},
                "steps": [{"path": "/${users.username}"}],
            }
        )
        message = self._expect_error(
            ["http://example.com", "--scenario", self.scenario_path, "-u", "10", "-d", "5"]
        )
        self.assertIn("3 row(s)", message)
        self.assertIn("strategy 'unique'", message)

    def test_unique_capacity_passes_when_rows_suffice(self):
        self._write_scenario(
            {
                "data": {"users": {"file": "users.csv", "strategy": "unique"}},
                "steps": [{"path": "/${users.username}"}],
            }
        )
        config, _ = self._run(
            ["http://example.com", "--scenario", self.scenario_path, "-u", "3", "-d", "5"]
        )
        self.assertEqual(config.users, 3)


# ---------------------------------------------------------------------------
# Distributed sharding
# ---------------------------------------------------------------------------


class TestDistributedFeeders(unittest.TestCase):
    def _config(self, strategy="unique", count=10):
        scenario = pywrkr.Scenario(
            steps=[pywrkr.ScenarioStep(path="/${users.i}")],
            data={"users": make_feeder(strategy, count=count)},
        )
        return pywrkr.BenchmarkConfig(url="http://example.com", scenario=scenario)

    def test_round_trip(self):
        from pywrkr.distributed import _deserialize_config, _serialize_config

        payload = json.loads(json.dumps(_serialize_config(self._config())))
        restored = _deserialize_config(payload)
        feeder = restored.scenario.data["users"]
        self.assertEqual(feeder.strategy, "unique")
        self.assertEqual(len(feeder.rows), 10)
        self.assertEqual(feeder.rows[0], {"i": "0"})

    def test_defaults_when_data_absent(self):
        from pywrkr.distributed import _deserialize_config, _serialize_config

        payload = _serialize_config(pywrkr.BenchmarkConfig(url="http://example.com"))
        restored = _deserialize_config(payload)
        self.assertIsNone(restored.scenario)

    def test_unique_stays_globally_unique_across_two_workers(self):
        # The AC's distributed claim, exercised without sockets: shard the
        # serialized config the way the master does, deserialize both halves as
        # two in-process workers, and drain their cursors.
        from pywrkr.distributed import (
            _deserialize_config,
            _serialize_config,
            _shard_config_feeders,
        )

        base = json.loads(json.dumps(_serialize_config(self._config(count=10))))
        drained = []
        for index in range(2):
            worker_config = _deserialize_config(_shard_config_feeders(base, index, 2))
            cursor = FeederCursor(worker_config.scenario.data["users"])
            while (row := cursor.next_row()) is not None:
                drained.append(row["i"])

        self.assertEqual(len(drained), 10)
        self.assertEqual(len(set(drained)), 10, f"a row was handed out twice: {drained}")
        self.assertEqual(sorted(drained, key=int), [str(i) for i in range(10)])

    def test_reusing_strategies_are_not_sharded(self):
        from pywrkr.distributed import _serialize_config, _shard_config_feeders

        base = json.loads(json.dumps(_serialize_config(self._config("loop", count=10))))
        for index in range(2):
            sharded = _shard_config_feeders(base, index, 2)
            self.assertEqual(len(sharded["scenario"]["data"]["users"]["rows"]), 10)

    def test_single_worker_is_left_untouched(self):
        from pywrkr.distributed import _serialize_config, _shard_config_feeders

        base = json.loads(json.dumps(_serialize_config(self._config())))
        self.assertIs(_shard_config_feeders(base, 0, 1), base)


# ---------------------------------------------------------------------------
# Integration
# ---------------------------------------------------------------------------


class TestDataDrivenIntegration(AioHTTPTestCase):
    async def get_application(self):
        self.logins: list[dict] = []
        app = web.Application()
        app.router.add_post("/login", self.handle_login)
        app.router.add_get("/u/{tail:.*}", self.handle_get)
        return app

    async def handle_login(self, request):
        self.logins.append(json.loads(await request.read()))
        return web.json_response({"ok": True})

    async def handle_get(self, request):
        self.logins.append({"path": request.path})
        return web.json_response({"ok": True})

    def _url(self):
        return f"http://127.0.0.1:{self.server.port}"

    async def _run(self, scenario_payload, csv_text=USERS_CSV, users=1, duration=2.0):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with open(os.path.join(tmp.name, "users.csv"), "w", encoding="utf-8") as handle:
            handle.write(csv_text)
        scenario_path = os.path.join(tmp.name, "scenario.json")
        with open(scenario_path, "w", encoding="utf-8") as handle:
            json.dump(scenario_payload, handle)

        scenario = pywrkr.load_scenario(scenario_path)
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

    async def test_ten_users_each_get_their_own_credentials_exactly_once(self):
        rows = "username,password\n" + "".join(f"user{i},pw{i}\n" for i in range(10))
        stats = await self._run(
            {
                "name": "Unique logins",
                "data": {"users": {"file": "users.csv", "strategy": "unique"}},
                "steps": [
                    {
                        "name": "login",
                        "method": "POST",
                        "path": "/login",
                        "body": {"user": "${users.username}", "pass": "${users.password}"},
                    }
                ],
            },
            csv_text=rows,
            users=10,
        )
        # Exactly one request per row, then the run winds down on its own.
        self.assertEqual(stats.total_requests, 10)
        self.assertEqual(stats.errors, 0)
        usernames = [entry["user"] for entry in self.logins]
        self.assertEqual(sorted(usernames), sorted(f"user{i}" for i in range(10)))
        self.assertEqual(len(usernames), len(set(usernames)))
        # Credentials stayed paired with each other.
        for entry in self.logins:
            self.assertEqual(entry["pass"], entry["user"].replace("user", "pw"))

    async def test_loop_wraps_and_keeps_running(self):
        stats = await self._run(
            {
                "name": "Loop",
                "data": {"users": {"file": "users.csv", "strategy": "loop"}},
                "steps": [
                    {
                        "name": "login",
                        "method": "POST",
                        "path": "/login",
                        "body": {"user": "${users.username}"},
                    }
                ],
            },
            users=1,
            duration=1.0,
        )
        self.assertGreater(stats.total_requests, 3)
        usernames = [entry["user"] for entry in self.logins]
        self.assertEqual(set(usernames), {"alice", "bob", "carol"})
        # Wrapped around at least once, in order.
        self.assertEqual(usernames[:4], ["alice", "bob", "carol", "alice"])

    async def test_sequential_stops_the_run(self):
        stats = await self._run(
            {
                "name": "Sequential",
                "data": {"users": {"file": "users.csv", "strategy": "sequential"}},
                "steps": [
                    {
                        "name": "login",
                        "method": "POST",
                        "path": "/login",
                        "body": {"user": "${users.username}"},
                    }
                ],
            },
            users=1,
            duration=5.0,
        )
        self.assertEqual(stats.total_requests, 3)
        self.assertEqual(stats.errors, 0)

    async def test_functions_expand_per_request(self):
        await self._run(
            {
                "name": "Functions",
                "steps": [
                    {
                        "name": "login",
                        "method": "POST",
                        "path": "/login",
                        "body": {
                            "id": "${uuid()}",
                            "seq": "${counter()}",
                            "n": "${randint(1,3)}",
                            "s": "${randstr(5)}",
                            "ts": "${now(unix)}",
                        },
                    }
                ],
            },
            users=2,
            duration=1.0,
        )
        self.assertGreater(len(self.logins), 4)
        ids = [entry["id"] for entry in self.logins]
        self.assertEqual(len(ids), len(set(ids)), "uuid() repeated itself")
        # counter() is shared across users, so the sequence covers 1..N exactly.
        seqs = sorted(int(entry["seq"]) for entry in self.logins)
        self.assertEqual(seqs, list(range(1, len(self.logins) + 1)))
        for entry in self.logins:
            self.assertIn(int(entry["n"]), (1, 2, 3))
            self.assertEqual(len(entry["s"]), 5)
            self.assertRegex(entry["ts"], r"\A\d{10}\Z")

    async def test_data_and_functions_reach_the_path(self):
        await self._run(
            {
                "name": "Path templating",
                "data": {"users": {"file": "users.csv"}},
                "steps": [{"name": "get", "path": "/u/${users.username}/${counter(page)}"}],
            },
            users=1,
            duration=1.0,
        )
        self.assertTrue(self.logins)
        for entry in self.logins:
            self.assertRegex(entry["path"], r"\A/u/(alice|bob|carol)/\d+\Z")


if __name__ == "__main__":
    unittest.main()
