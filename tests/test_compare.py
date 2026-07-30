#!/usr/bin/env python3
"""Tests for baseline comparison and regression detection."""

import json
import os
import tempfile
import unittest
from io import StringIO
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase

import pywrkr
from pywrkr.compare import (
    EXIT_REGRESSION,
    EXIT_USAGE,
    SCHEMA_VERSION,
    ResultsError,
    average_results,
    compare_results,
    config_differences,
    format_value,
    load_baseline,
    load_results,
    metric_value,
    parse_fail_on,
    render_report,
)
from pywrkr.main import _build_compare_parser, _build_parser, _parse_and_validate_args, _run_compare

# A minimal but complete results doc, shaped exactly like --json output.
BASE = {
    "schema_version": 1,
    "duration_sec": 10.0,
    "connections": 10,
    "total_requests": 10000,
    "total_errors": 10,
    "requests_per_sec": 1000.0,
    "transfer_per_sec_bytes": 51200.0,
    "total_bytes": 512000,
    "config": {
        "mode": "duration",
        "connections": 10,
        "users": None,
        "duration": 10.0,
        "num_requests": None,
        "rate": None,
        "url_host": "api.example.com",
    },
    "latency": {"min": 0.001, "max": 0.5, "mean": 0.02, "median": 0.018, "stdev": 0.01},
    "percentiles": {"p50": 0.018, "p75": 0.025, "p90": 0.04, "p95": 0.05, "p99": 0.12},
    "step_stats": {"checkout": {"count": 100, "mean": 0.03, "median": 0.028}},
}


def variant(**overrides):
    """A deep copy of BASE with dotted-path overrides applied."""
    doc = json.loads(json.dumps(BASE))
    for path, value in overrides.items():
        node = doc
        parts = path.split(".")
        for key in parts[:-1]:
            node = node[key]
        node[parts[-1]] = value
    return doc


def write_json(doc, directory=None):
    handle = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, dir=directory, encoding="utf-8"
    )
    json.dump(doc, handle)
    handle.close()
    return handle.name


# ---------------------------------------------------------------------------
# Expression parsing
# ---------------------------------------------------------------------------


class TestParseFailOn(unittest.TestCase):
    def test_relative_positive(self):
        rule = parse_fail_on("p95 > +10%")
        self.assertEqual(
            (rule.metric, rule.operator, rule.amount, rule.relative), ("p95", ">", 10.0, True)
        )

    def test_relative_negative(self):
        rule = parse_fail_on("rps < -5%")
        self.assertEqual(
            (rule.metric, rule.operator, rule.amount, rule.relative), ("rps", "<", -5.0, True)
        )

    def test_unsigned_is_positive(self):
        self.assertEqual(parse_fail_on("p95 > 10%").amount, 10.0)

    def test_absolute_milliseconds_become_seconds(self):
        rule = parse_fail_on("p99 > +50ms")
        self.assertFalse(rule.relative)
        self.assertAlmostEqual(rule.amount, 0.05)

    def test_absolute_microseconds(self):
        self.assertAlmostEqual(parse_fail_on("p99 > +500us").amount, 0.0005)

    def test_absolute_seconds(self):
        self.assertAlmostEqual(parse_fail_on("p99 > +1.5s").amount, 1.5)

    def test_error_rate_absolute_is_percentage_points(self):
        rule = parse_fail_on("error_rate > +0.5")
        self.assertFalse(rule.relative)
        self.assertEqual(rule.amount, 0.5)

    def test_step_metric(self):
        rule = parse_fail_on("step:checkout.mean > +20ms")
        self.assertEqual(rule.metric, "step:checkout.mean")
        self.assertAlmostEqual(rule.amount, 0.02)

    def test_alias_is_canonicalised(self):
        self.assertEqual(parse_fail_on("mean_latency > +5%").metric, "avg_latency")

    def test_whitespace_tolerated(self):
        self.assertEqual(parse_fail_on("  p95>+10%  ").metric, "p95")

    def test_describe_round_trips(self):
        self.assertEqual(parse_fail_on("p95 > +10%").describe(), "p95 > +10%")
        self.assertEqual(parse_fail_on("rps < -5%").describe(), "rps < -5%")

    def test_malformed(self):
        for expr in ("p95", "p95 >", "> +10%", "p95 = 10%", "p95 >> 10%", ""):
            with self.assertRaises(ValueError, msg=expr):
                parse_fail_on(expr)

    def test_unknown_metric(self):
        with self.assertRaises(ValueError) as ctx:
            parse_fail_on("cpu_usage > +10%")
        self.assertIn("Unknown metric", str(ctx.exception))

    def test_time_unit_on_a_non_latency_metric(self):
        with self.assertRaises(ValueError) as ctx:
            parse_fail_on("rps > +10ms")
        self.assertIn("Invalid unit", str(ctx.exception))

    def test_bare_latency_number_warns(self):
        with self.assertLogs("pywrkr.compare", level="WARNING") as logs:
            rule = parse_fail_on("p95 > +1")
        self.assertAlmostEqual(rule.amount, 1.0)
        self.assertIn("no unit", logs.output[0])


# ---------------------------------------------------------------------------
# Metric access
# ---------------------------------------------------------------------------


class TestMetricValue(unittest.TestCase):
    def test_top_level(self):
        self.assertEqual(metric_value(BASE, "rps"), 1000.0)
        self.assertEqual(metric_value(BASE, "total_requests"), 10000)

    def test_nested(self):
        self.assertEqual(metric_value(BASE, "p95"), 0.05)
        self.assertEqual(metric_value(BASE, "avg_latency"), 0.02)

    def test_derived_error_rate(self):
        self.assertAlmostEqual(metric_value(BASE, "error_rate"), 0.1)

    def test_error_rate_with_no_requests(self):
        self.assertEqual(metric_value(variant(total_requests=0), "error_rate"), 0.0)

    def test_step_metric(self):
        self.assertEqual(metric_value(BASE, "step:checkout.mean"), 0.03)

    def test_absent_is_none_not_zero(self):
        # Treating a missing p99.9 as 0 would report a spectacular improvement.
        self.assertIsNone(metric_value(BASE, "p99.9"))
        self.assertIsNone(metric_value(BASE, "step:nope.mean"))
        self.assertIsNone(metric_value({}, "rps"))

    def test_unknown_metric_is_none(self):
        self.assertIsNone(metric_value(BASE, "not_a_metric"))


# ---------------------------------------------------------------------------
# Delta math
# ---------------------------------------------------------------------------


class TestCompareMath(unittest.TestCase):
    def _delta(self, report, metric):
        return next(d for d in report.metrics if d.metric == metric)

    def test_relative_regression_fires(self):
        report = compare_results(
            BASE, variant(**{"percentiles.p95": 0.06}), [parse_fail_on("p95 > +10%")]
        )
        delta = self._delta(report, "p95")
        self.assertAlmostEqual(delta.delta, 0.01)
        self.assertAlmostEqual(delta.delta_pct, 20.0)
        self.assertTrue(report.regressed)
        self.assertEqual(report.exit_code, EXIT_REGRESSION)

    def test_relative_improvement_does_not_fire(self):
        report = compare_results(
            BASE, variant(**{"percentiles.p95": 0.04}), [parse_fail_on("p95 > +10%")]
        )
        self.assertAlmostEqual(self._delta(report, "p95").delta_pct, -20.0)
        self.assertFalse(report.regressed)
        self.assertEqual(report.exit_code, 0)

    def test_throughput_drop_fires_on_less_than(self):
        report = compare_results(
            BASE, variant(requests_per_sec=900.0), [parse_fail_on("rps < -5%")]
        )
        self.assertAlmostEqual(self._delta(report, "rps").delta_pct, -10.0)
        self.assertTrue(report.regressed)

    def test_throughput_gain_does_not_fire(self):
        report = compare_results(
            BASE, variant(requests_per_sec=1100.0), [parse_fail_on("rps < -5%")]
        )
        self.assertFalse(report.regressed)

    def test_absolute_delta_fires(self):
        report = compare_results(
            BASE, variant(**{"percentiles.p99": 0.2}), [parse_fail_on("p99 > +50ms")]
        )
        self.assertTrue(report.regressed)

    def test_absolute_delta_within_budget(self):
        report = compare_results(
            BASE, variant(**{"percentiles.p99": 0.14}), [parse_fail_on("p99 > +50ms")]
        )
        self.assertFalse(report.regressed)

    def test_error_rate_absolute_points(self):
        # 10 -> 100 errors on 10000 requests is 0.1% -> 1.0%, a +0.9pt move.
        report = compare_results(
            BASE, variant(total_errors=100), [parse_fail_on("error_rate > +0.5")]
        )
        self.assertTrue(report.regressed)
        self.assertAlmostEqual(self._delta(report, "error_rate").delta, 0.9)

    def test_step_metric_rule(self):
        report = compare_results(
            BASE,
            variant(**{"step_stats.checkout.mean": 0.06}),
            [parse_fail_on("step:checkout.mean > +20ms")],
        )
        self.assertTrue(report.regressed)

    def test_zero_baseline_skips_relative_rule(self):
        base = variant(total_errors=0)
        report = compare_results(
            base, variant(total_errors=5), [parse_fail_on("error_rate > +10%")]
        )
        self.assertFalse(report.regressed)
        self.assertIn("undefined", report.verdicts[0].reason)

    def test_missing_metric_skips_rule(self):
        report = compare_results(BASE, BASE, [parse_fail_on("p99.9 > +10%")])
        self.assertFalse(report.regressed)
        self.assertIn("missing", report.verdicts[0].reason)

    def test_identical_runs_produce_zero_deltas(self):
        report = compare_results(BASE, json.loads(json.dumps(BASE)), [parse_fail_on("p95 > +0.1%")])
        self.assertFalse(report.regressed)
        for delta in report.metrics:
            if delta.delta is not None:
                self.assertEqual(delta.delta, 0)

    def test_every_metric_appears_in_the_table(self):
        report = compare_results(BASE, BASE)
        names = {d.metric for d in report.metrics}
        for expected in ("rps", "error_rate", "p50", "p95", "avg_latency", "step:checkout.mean"):
            self.assertIn(expected, names)

    def test_no_rules_never_regresses(self):
        report = compare_results(BASE, variant(**{"percentiles.p95": 10.0}))
        self.assertFalse(report.regressed)
        self.assertEqual(report.exit_code, 0)


# ---------------------------------------------------------------------------
# Loading, averaging, config comparability
# ---------------------------------------------------------------------------


class TestLoading(unittest.TestCase):
    def test_load_results(self):
        path = write_json(BASE)
        self.addCleanup(os.unlink, path)
        self.assertEqual(load_results(path)["total_requests"], 10000)

    def test_missing_file(self):
        with self.assertRaises(ResultsError) as ctx:
            load_results("/nonexistent/results.json")
        self.assertIn("not found", str(ctx.exception))

    def test_invalid_json(self):
        path = write_json(BASE)
        self.addCleanup(os.unlink, path)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{oops")
        with self.assertRaises(ResultsError) as ctx:
            load_results(path)
        self.assertIn("not valid JSON", str(ctx.exception))

    def test_not_an_object(self):
        path = write_json([1, 2, 3])
        self.addCleanup(os.unlink, path)
        with self.assertRaises(ResultsError) as ctx:
            load_results(path)
        self.assertIn("expected a JSON object", str(ctx.exception))

    def test_not_pywrkr_output(self):
        path = write_json({"hello": "world"})
        self.addCleanup(os.unlink, path)
        with self.assertRaises(ResultsError) as ctx:
            load_results(path)
        self.assertIn("does not look like pywrkr", str(ctx.exception))

    def test_newer_schema_is_refused(self):
        path = write_json(variant(schema_version=SCHEMA_VERSION + 1))
        self.addCleanup(os.unlink, path)
        with self.assertRaises(ResultsError) as ctx:
            load_results(path)
        self.assertIn("newer than this pywrkr", str(ctx.exception))

    def test_file_without_schema_version_is_read_as_current(self):
        # Backward compatibility: results written before the key existed have
        # today's shape.
        legacy = json.loads(json.dumps(BASE))
        del legacy["schema_version"]
        path = write_json(legacy)
        self.addCleanup(os.unlink, path)
        self.assertEqual(load_results(path)["total_requests"], 10000)

    def test_glob_averages_three_runs(self):
        directory = tempfile.mkdtemp()
        for rps, p95 in ((900.0, 0.04), (1000.0, 0.05), (1100.0, 0.06)):
            write_json(variant(requests_per_sec=rps, **{"percentiles.p95": p95}), directory)
        baseline, sources = load_baseline(os.path.join(directory, "*.json"))
        self.assertEqual(len(sources), 3)
        self.assertEqual(baseline["baseline_runs"], 3)
        self.assertAlmostEqual(baseline["requests_per_sec"], 1000.0)
        self.assertAlmostEqual(baseline["percentiles"]["p95"], 0.05)
        # Nested step stats are averaged too.
        self.assertAlmostEqual(baseline["step_stats"]["checkout"]["mean"], 0.03)

    def test_glob_matching_nothing(self):
        with self.assertRaises(ResultsError) as ctx:
            load_baseline(os.path.join(tempfile.mkdtemp(), "*.json"))
        self.assertIn("matched no files", str(ctx.exception))

    def test_single_file_is_not_averaged(self):
        path = write_json(BASE)
        self.addCleanup(os.unlink, path)
        baseline, sources = load_baseline(path)
        self.assertEqual(sources, [path])
        self.assertNotIn("baseline_runs", baseline)

    def test_average_of_one(self):
        self.assertEqual(average_results([BASE])["baseline_runs"], 1)

    def test_average_of_none(self):
        with self.assertRaises(ResultsError):
            average_results([])

    def test_average_keeps_non_numeric_fields(self):
        merged = average_results([BASE, variant(requests_per_sec=2000.0)])
        self.assertEqual(merged["config"]["url_host"], "api.example.com")
        self.assertAlmostEqual(merged["requests_per_sec"], 1500.0)


class TestConfigDifferences(unittest.TestCase):
    def test_identical_configs(self):
        self.assertEqual(config_differences(BASE, BASE), [])

    def test_user_count_difference_is_reported(self):
        diffs = config_differences(BASE, variant(**{"config.users": 100}))
        self.assertEqual(len(diffs), 1)
        self.assertIn("users", diffs[0])

    def test_host_difference(self):
        diffs = config_differences(BASE, variant(**{"config.url_host": "other.example.com"}))
        self.assertIn("url_host", diffs[0])

    def test_missing_snapshot_is_not_a_difference(self):
        legacy = json.loads(json.dumps(BASE))
        del legacy["config"]
        self.assertEqual(config_differences(legacy, BASE), [])

    def test_warnings_surface_in_the_report(self):
        report = compare_results(BASE, variant(**{"config.connections": 50}))
        self.assertEqual(len(report.config_warnings), 1)
        self.assertIn("connections", report.config_warnings[0])


# ---------------------------------------------------------------------------
# Rendering (golden output)
# ---------------------------------------------------------------------------


class TestRendering(unittest.TestCase):
    def _render(self, fmt, current=None, rules=("p95 > +10%",)):
        report = compare_results(
            BASE,
            current if current is not None else variant(**{"percentiles.p95": 0.06}),
            [parse_fail_on(r) for r in rules],
            ["baseline.json"],
        )
        out = StringIO()
        render_report(report, fmt, file=out)
        return out.getvalue()

    def test_table_shape(self):
        text = self._render("table")
        self.assertIn("BASELINE COMPARISON", text)
        self.assertIn("Metric", text)
        self.assertIn("Baseline: baseline.json", text)
        self.assertIn("p95", text)
        self.assertIn("50.00ms", text)
        self.assertIn("60.00ms", text)
        self.assertIn("+10.00ms", text)
        self.assertIn("+20.00%", text)
        self.assertIn("[FAIL] p95 > +10%", text)
        self.assertIn("REGRESSION: 1 of 1 rule(s) fired", text)

    def test_table_passing_run(self):
        text = self._render("table", current=json.loads(json.dumps(BASE)))
        self.assertIn("[PASS]", text)
        self.assertIn("OK: all 1 rule(s) passed", text)
        self.assertNotIn("REGRESSION", text)

    def test_table_shows_config_warning(self):
        text = self._render("table", current=variant(**{"config.users": 5}))
        self.assertIn("WARNING: config differs", text)

    def test_markdown_shape(self):
        text = self._render("markdown")
        self.assertIn("### pywrkr baseline comparison", text)
        self.assertIn("| Metric | Baseline | Current | Delta | Change |", text)
        self.assertIn("| --- | ---: | ---: | ---: | ---: |", text)
        self.assertIn("| p95 | 50.00ms | 60.00ms | +10.00ms | +20.00% |", text)
        self.assertIn("❌ `p95 > +10%`", text)
        self.assertIn("**Regression detected.**", text)

    def test_markdown_passing_run(self):
        text = self._render("markdown", current=json.loads(json.dumps(BASE)))
        self.assertIn("✅", text)
        self.assertIn("**No regression detected.**", text)

    def test_json_shape(self):
        payload = json.loads(self._render("json"))
        self.assertEqual(payload["schema_version"], SCHEMA_VERSION)
        self.assertTrue(payload["regressed"])
        self.assertEqual(payload["exit_code"], EXIT_REGRESSION)
        self.assertEqual(payload["baseline_sources"], ["baseline.json"])
        self.assertEqual(payload["baseline_runs"], 1)
        rule = payload["rules"][0]
        self.assertEqual(rule["expression"], "p95 > +10%")
        self.assertTrue(rule["regressed"])
        p95 = next(m for m in payload["metrics"] if m["metric"] == "p95")
        self.assertEqual(p95["unit"], "seconds")
        self.assertEqual(p95["baseline"], 0.05)
        self.assertEqual(p95["current"], 0.06)
        self.assertEqual(p95["delta"], 0.01)
        self.assertEqual(p95["delta_pct"], 20.0)

    def test_json_rounds_away_subtraction_noise(self):
        # 0.3 - 0.1 is 0.19999999999999998 in binary floating point.
        payload = json.loads(self._render("json", current=variant(total_errors=30)))
        error_rate = next(m for m in payload["metrics"] if m["metric"] == "error_rate")
        self.assertEqual(error_rate["delta"], 0.2)

    def test_json_is_valid_with_missing_metrics(self):
        payload = json.loads(self._render("json", rules=("p99.9 > +10%",)))
        self.assertFalse(payload["regressed"])
        self.assertIn("missing", payload["rules"][0]["reason"])


class TestFormatValue(unittest.TestCase):
    def test_seconds_scale(self):
        self.assertEqual(format_value(0.0000005, "seconds"), "0.50us")
        self.assertEqual(format_value(0.05, "seconds"), "50.00ms")
        self.assertEqual(format_value(1.5, "seconds"), "1.500s")

    def test_signed(self):
        self.assertEqual(format_value(0.01, "seconds", signed=True), "+10.00ms")
        self.assertEqual(format_value(-0.01, "seconds", signed=True), "-10.00ms")

    def test_other_units(self):
        self.assertEqual(format_value(1.5, "percent"), "1.50%")
        self.assertEqual(format_value(2048, "bytes"), "2,048B")
        self.assertEqual(format_value(1000.0, "rate"), "1,000.00")
        self.assertEqual(format_value(7, "count"), "7")
        self.assertEqual(format_value(7.5, "count"), "7.50")

    def test_none(self):
        self.assertEqual(format_value(None, "seconds"), "-")


# ---------------------------------------------------------------------------
# `pywrkr compare` subcommand
# ---------------------------------------------------------------------------


class TestCompareSubcommand(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base_path = write_json(BASE, self.tmp.name)

    def _run(self, argv):
        args = _build_compare_parser().parse_args(argv)
        out, err = StringIO(), StringIO()
        with (
            self.assertRaises(SystemExit) as ctx,
            patch("sys.stdout", out),
            patch("sys.stderr", err),
        ):
            _run_compare(args)
        return ctx.exception.code, out.getvalue(), err.getvalue()

    def test_regression_exits_3(self):
        current = write_json(variant(**{"percentiles.p95": 0.06}), self.tmp.name)
        code, out, _ = self._run([self.base_path, current, "--fail-on", "p95 > +10%"])
        self.assertEqual(code, EXIT_REGRESSION)
        self.assertIn("REGRESSION", out)

    def test_pass_exits_0(self):
        current = write_json(BASE, self.tmp.name)
        code, out, _ = self._run([self.base_path, current, "--fail-on", "p95 > +10%"])
        self.assertEqual(code, 0)
        self.assertIn("OK:", out)

    def test_no_rules_exits_0(self):
        current = write_json(variant(**{"percentiles.p95": 1.0}), self.tmp.name)
        code, _, _ = self._run([self.base_path, current])
        self.assertEqual(code, 0)

    def test_missing_file_exits_1(self):
        code, _, err = self._run([self.base_path, "/nonexistent.json"])
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("not found", err)

    def test_bad_expression_exits_1(self):
        current = write_json(BASE, self.tmp.name)
        code, _, err = self._run([self.base_path, current, "--fail-on", "nonsense"])
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("Invalid --fail-on", err)

    def test_config_mismatch_warns_but_passes(self):
        current = write_json(variant(**{"config.users": 100}), self.tmp.name)
        code, out, _ = self._run([self.base_path, current])
        self.assertEqual(code, 0)
        self.assertIn("WARNING: config differs", out)

    def test_strict_config_exits_1(self):
        current = write_json(variant(**{"config.users": 100}), self.tmp.name)
        code, _, err = self._run([self.base_path, current, "--strict-config"])
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("--strict-config", err)

    def test_strict_config_passes_when_configs_match(self):
        current = write_json(BASE, self.tmp.name)
        code, _, _ = self._run([self.base_path, current, "--strict-config"])
        self.assertEqual(code, 0)

    def test_glob_baseline(self):
        directory = tempfile.mkdtemp()
        for rps in (900.0, 1100.0):
            write_json(variant(requests_per_sec=rps), directory)
        current = write_json(variant(requests_per_sec=1000.0), self.tmp.name)
        code, out, _ = self._run(
            [os.path.join(directory, "*.json"), current, "--fail-on", "rps < -5%"]
        )
        self.assertEqual(code, 0)
        self.assertIn("mean of 2 runs", out)

    def test_markdown_format(self):
        current = write_json(variant(**{"percentiles.p95": 0.06}), self.tmp.name)
        code, out, _ = self._run(
            [self.base_path, current, "--fail-on", "p95 > +10%", "--format", "markdown"]
        )
        self.assertEqual(code, EXIT_REGRESSION)
        self.assertIn("### pywrkr baseline comparison", out)

    def test_json_format(self):
        current = write_json(BASE, self.tmp.name)
        code, out, _ = self._run([self.base_path, current, "--format", "json"])
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(out)["regressed"])


# ---------------------------------------------------------------------------
# Main-command flags
# ---------------------------------------------------------------------------


class TestBaselineCliOptions(unittest.TestCase):
    def _parse(self, argv):
        parser = _build_parser()
        return _parse_and_validate_args(parser, parser.parse_args(argv))

    def _expect_error(self, argv):
        with self.assertRaises(SystemExit), patch("sys.stderr", new_callable=StringIO) as err:
            self._parse(argv)
        return err.getvalue()

    def test_flags_reach_the_config(self):
        config, _ = self._parse(
            [
                "http://example.com",
                "--baseline",
                "base.json",
                "--fail-on",
                "p95 > +10%",
                "--save-baseline",
                "out.json",
                "--strict-config",
                "--compare-format",
                "markdown",
            ]
        )
        self.assertEqual(config.baseline, "base.json")
        self.assertEqual(config.save_baseline, "out.json")
        self.assertEqual(len(config.fail_on), 1)
        self.assertEqual(config.fail_on[0].metric, "p95")
        self.assertTrue(config.strict_config)
        self.assertEqual(config.compare_format, "markdown")

    def test_defaults(self):
        config, _ = self._parse(["http://example.com"])
        self.assertIsNone(config.baseline)
        self.assertIsNone(config.save_baseline)
        self.assertEqual(config.fail_on, [])
        self.assertFalse(config.strict_config)
        self.assertEqual(config.compare_format, "table")

    def test_fail_on_without_baseline(self):
        self.assertIn(
            "requires --baseline",
            self._expect_error(["http://example.com", "--fail-on", "p95 > +10%"]),
        )

    def test_strict_config_without_baseline(self):
        self.assertIn(
            "requires --baseline", self._expect_error(["http://example.com", "--strict-config"])
        )

    def test_bad_expression(self):
        self.assertIn(
            "Invalid --fail-on",
            self._expect_error(["http://example.com", "--baseline", "b.json", "--fail-on", "nope"]),
        )

    def test_baseline_without_rules_warns(self):
        with self.assertLogs("pywrkr.main", level="WARNING") as logs:
            self._parse(["http://example.com", "--baseline", "base.json"])
        self.assertIn("cannot fail the build", logs.output[0])


class TestResultsSchema(unittest.TestCase):
    def _results(self, **config_kwargs):
        stats = pywrkr.WorkerStats()
        stats.total_requests = 10
        stats.latencies.append(0.01)
        config = pywrkr.BenchmarkConfig(url="http://api.example.com:8080/x", **config_kwargs)
        return pywrkr.build_results_dict(stats, 1.0, 4, config)

    def test_schema_version_present(self):
        self.assertEqual(self._results()["schema_version"], SCHEMA_VERSION)

    def test_config_snapshot(self):
        snapshot = self._results(connections=4)["config"]
        self.assertEqual(snapshot["mode"], "duration")
        self.assertEqual(snapshot["connections"], 4)
        self.assertEqual(snapshot["url_host"], "api.example.com:8080")

    def test_mode_reflects_users(self):
        self.assertEqual(self._results(users=10)["config"]["mode"], "users")

    def test_mode_reflects_request_count(self):
        self.assertEqual(self._results(num_requests=100)["config"]["mode"], "requests")

    def test_mode_reflects_scenario(self):
        scenario = pywrkr.Scenario(steps=[pywrkr.ScenarioStep(path="/")])
        self.assertEqual(self._results(scenario=scenario)["config"]["mode"], "scenario:duration")

    def test_no_config_snapshot_without_config(self):
        stats = pywrkr.WorkerStats()
        stats.total_requests = 1
        self.assertNotIn("config", pywrkr.build_results_dict(stats, 1.0, 1))

    def test_results_are_comparable_with_themselves(self):
        results = self._results()
        report = compare_results(results, results, [parse_fail_on("p95 > +10%")])
        self.assertFalse(report.regressed)
        self.assertEqual(report.config_warnings, [])


# ---------------------------------------------------------------------------
# End-to-end gate against a real server
# ---------------------------------------------------------------------------


class TestBaselineGateIntegration(AioHTTPTestCase):
    """A latency shift injected between two runs must trip the gate."""

    async def get_application(self):
        self.delay = 0.0
        app = web.Application()
        app.router.add_get("/", self.handle)
        return app

    async def handle(self, request):
        if self.delay:
            import asyncio

            await asyncio.sleep(self.delay)
        return web.Response(text="ok")

    def _config(self, **kwargs):
        defaults = {"connections": 4, "duration": 1.0, "threads": 1, "timeout_sec": 5}
        return pywrkr.BenchmarkConfig(
            url=f"http://127.0.0.1:{self.server.port}/", **{**defaults, **kwargs}
        )

    async def test_save_then_compare_detects_the_shift(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        baseline_path = os.path.join(tmp.name, "baseline.json")

        with patch("sys.stdout", new_callable=StringIO):
            _, code = await pywrkr.run_benchmark(self._config(save_baseline=baseline_path))
        self.assertEqual(code, 0)
        self.assertTrue(os.path.isfile(baseline_path))

        # Same server, now noticeably slower.
        self.delay = 0.02
        out = StringIO()
        with patch("sys.stdout", out):
            _, code = await pywrkr.run_benchmark(
                self._config(baseline=baseline_path, fail_on=[parse_fail_on("p95 > +50%")])
            )
        self.assertEqual(code, EXIT_REGRESSION)
        self.assertIn("REGRESSION", out.getvalue())

    async def test_no_shift_passes(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        baseline_path = os.path.join(tmp.name, "baseline.json")

        with patch("sys.stdout", new_callable=StringIO):
            await pywrkr.run_benchmark(self._config(save_baseline=baseline_path))
        out = StringIO()
        with patch("sys.stdout", out):
            _, code = await pywrkr.run_benchmark(
                # A budget wide enough to absorb loopback jitter.
                self._config(baseline=baseline_path, fail_on=[parse_fail_on("p95 > +5000%")])
            )
        self.assertEqual(code, 0)
        self.assertIn("OK:", out.getvalue())

    async def test_missing_baseline_exits_1(self):
        out = StringIO()
        with patch("sys.stdout", out), patch("sys.stderr", new_callable=StringIO):
            _, code = await pywrkr.run_benchmark(
                self._config(
                    baseline="/nonexistent/base.json", fail_on=[parse_fail_on("p95 > +1%")]
                )
            )
        self.assertEqual(code, EXIT_USAGE)

    async def test_threshold_breach_outranks_a_regression(self):
        # Both gates fire; exit 2 wins because an absolute SLO breach is the
        # stronger statement.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        baseline_path = os.path.join(tmp.name, "baseline.json")

        with patch("sys.stdout", new_callable=StringIO):
            await pywrkr.run_benchmark(self._config(save_baseline=baseline_path))

        self.delay = 0.02
        with patch("sys.stdout", new_callable=StringIO):
            _, code = await pywrkr.run_benchmark(
                self._config(
                    baseline=baseline_path,
                    fail_on=[parse_fail_on("p95 > +1%")],
                    thresholds=[pywrkr.parse_threshold("p95 < 1ms")],
                )
            )
        self.assertEqual(code, 2)

    async def test_sub_runs_do_not_gate(self):
        # A distributed worker or an autofind step sees only part of the load;
        # gating on it (or letting several of them race to overwrite the
        # baseline file) would be meaningless. The master gates the merged result.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        baseline_path = os.path.join(tmp.name, "never-written.json")

        with patch("sys.stdout", new_callable=StringIO):
            _, code = await pywrkr.run_benchmark(
                self._config(
                    save_baseline=baseline_path,
                    baseline="/nonexistent/base.json",
                    fail_on=[parse_fail_on("p95 > +1%")],
                    _quiet=True,
                )
            )
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(baseline_path))

    async def test_strict_config_mismatch_exits_1(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        baseline_path = os.path.join(tmp.name, "baseline.json")

        with patch("sys.stdout", new_callable=StringIO):
            await pywrkr.run_benchmark(self._config(save_baseline=baseline_path))
        with patch("sys.stdout", new_callable=StringIO), patch("sys.stderr", new_callable=StringIO):
            _, code = await pywrkr.run_benchmark(
                self._config(connections=8, baseline=baseline_path, strict_config=True)
            )
        self.assertEqual(code, EXIT_USAGE)


if __name__ == "__main__":
    unittest.main()
