"""Per-step thresholds: ``--threshold "step:checkout p95 < 800ms"`` (#216).

For a scenario the aggregate p95 is a blend of every step, so a flow whose login
is 1ms and whose payment call is 250ms sits comfortably under an aggregate
budget while the step that matters is five times over it -- and adding fast
steps *improves* the number. The step-scoped form is the one that expresses the
real SLO.
"""

from __future__ import annotations

import io
import unittest
from unittest.mock import patch

from pywrkr.config import BenchmarkConfig, Scenario, ScenarioStep, WorkerStats
from pywrkr.main import _build_parser, _parse_and_validate_args
from pywrkr.reporting import evaluate_thresholds, parse_threshold, print_threshold_results


def stats_with_steps(steps: dict, errors: dict | None = None) -> WorkerStats:
    stats = WorkerStats()
    for name, samples in steps.items():
        stats.step_latencies[name].extend(samples)
        stats.total_requests += len(samples)
        for value in samples:
            stats.latencies.append(value)
    for name, count in (errors or {}).items():
        stats.step_errors[name] += count
        stats.errors += count
        stats.total_requests += count
    return stats


def verdict(expr: str, stats: WorkerStats, duration: float = 10.0):
    return evaluate_thresholds([parse_threshold(expr)], stats, duration)[0]


class TestParsing(unittest.TestCase):
    def test_the_aggregate_form_is_unchanged(self):
        threshold = parse_threshold("p95 < 300ms")
        self.assertIsNone(threshold.step)
        self.assertEqual(threshold.metric, "p95")
        self.assertAlmostEqual(threshold.value, 0.3)

    def test_a_step_scoped_threshold_parses(self):
        threshold = parse_threshold("step:checkout p95 < 800ms")
        self.assertEqual(threshold.step, "checkout")
        self.assertEqual(threshold.metric, "p95")
        self.assertAlmostEqual(threshold.value, 0.8)

    def test_a_step_name_containing_spaces_parses(self):
        """The default naming is `METHOD /path`, so spaces are the common case."""
        threshold = parse_threshold("step:GET /api/users p99 < 1s")
        self.assertEqual(threshold.step, "GET /api/users")
        self.assertEqual(threshold.metric, "p99")

    def test_a_step_name_containing_a_colon_parses(self):
        threshold = parse_threshold("step:Step 1: POST /login p95 < 200ms")
        self.assertEqual(threshold.step, "Step 1: POST /login")

    def test_a_step_name_that_looks_like_a_metric_still_resolves_the_last_one(self):
        """The metric alternation anchors the end of the name, not the start."""
        threshold = parse_threshold("step:p95 handler p95 < 1s")
        self.assertEqual(threshold.step, "p95 handler")
        self.assertEqual(threshold.metric, "p95")

    def test_every_metric_works_per_step(self):
        for metric in (
            "p50",
            "p75",
            "p90",
            "p95",
            "p99",
            "avg_latency",
            "max_latency",
            "min_latency",
            "error_rate",
            "rps",
        ):
            with self.subTest(metric=metric):
                threshold = parse_threshold(f"step:x {metric} < 1")
                self.assertEqual(threshold.metric, metric)
                self.assertEqual(threshold.step, "x")

    def test_units_convert_the_same_as_the_aggregate_form(self):
        self.assertAlmostEqual(parse_threshold("step:x p95 < 250us").value, 0.00025)
        self.assertAlmostEqual(parse_threshold("step:x p95 < 1.5s").value, 1.5)
        self.assertAlmostEqual(parse_threshold("step:x error_rate < 2.5%").value, 2.5)

    def test_an_empty_step_name_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_threshold("step: p95 < 1s")

    def test_a_step_form_without_a_metric_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_threshold("step:checkout < 800ms")

    def test_nonsense_is_still_rejected(self):
        for expr in ("step:x nonsense < 1s", "step:x p95 !! 1s", "not a threshold"):
            with self.subTest(expr=expr):
                with self.assertRaises(ValueError):
                    parse_threshold(expr)

    def test_the_raw_expression_is_kept_for_display(self):
        self.assertEqual(
            parse_threshold("  step:checkout p95 < 800ms ").raw_expr, "step:checkout p95 < 800ms"
        )


class TestEvaluation(unittest.TestCase):
    def stats(self) -> WorkerStats:
        return stats_with_steps(
            {"login": [0.001] * 20, "payment": [0.25] * 20},
            errors={"payment": 2},
        )

    def test_a_step_is_judged_on_its_own_samples(self):
        stats = self.stats()
        self.assertTrue(verdict("step:login p95 < 50ms", stats)[2])
        self.assertFalse(verdict("step:payment p95 < 50ms", stats)[2])

    def test_the_aggregate_can_pass_while_a_step_fails(self):
        """The whole reason the feature exists."""
        stats = self.stats()
        self.assertTrue(verdict("p95 < 500ms", stats)[2])
        self.assertFalse(verdict("step:payment p95 < 50ms", stats)[2])

    def test_adding_fast_steps_does_not_improve_a_step_threshold(self):
        slow_only = stats_with_steps({"payment": [0.25] * 10})
        with_fast = stats_with_steps({"payment": [0.25] * 10, "ping": [0.0001] * 10_000})
        self.assertEqual(
            verdict("step:payment p95 < 50ms", slow_only)[1],
            verdict("step:payment p95 < 50ms", with_fast)[1],
        )
        # ...whereas the aggregate is diluted into passing.
        self.assertFalse(verdict("p95 < 50ms", slow_only)[2])
        self.assertTrue(verdict("p95 < 50ms", with_fast)[2])

    def test_latency_metrics_read_from_the_step(self):
        stats = stats_with_steps({"x": [0.1, 0.2, 0.3]})
        self.assertAlmostEqual(verdict("step:x min_latency < 1s", stats)[1], 0.1)
        self.assertAlmostEqual(verdict("step:x max_latency < 1s", stats)[1], 0.3)
        self.assertAlmostEqual(verdict("step:x avg_latency < 1s", stats)[1], 0.2)

    def test_step_error_rate_counts_that_step_s_failures(self):
        stats = stats_with_steps({"pay": [0.01] * 90}, errors={"pay": 10})
        self.assertAlmostEqual(verdict("step:pay error_rate < 20%", stats)[1], 10.0)
        self.assertTrue(verdict("step:pay error_rate < 20%", stats)[2])
        self.assertFalse(verdict("step:pay error_rate < 5%", stats)[2])

    def test_a_step_with_no_failures_has_a_real_zero_error_rate(self):
        stats = stats_with_steps({"ok": [0.01] * 10})
        self.assertEqual(verdict("step:ok error_rate < 1%", stats)[1], 0.0)
        self.assertTrue(verdict("step:ok error_rate < 1%", stats)[2])

    def test_step_rps_is_that_step_s_throughput(self):
        stats = stats_with_steps({"x": [0.01] * 100})
        self.assertAlmostEqual(verdict("step:x rps > 5", stats, duration=10.0)[1], 10.0)

    def test_a_step_that_never_ran_fails_rather_than_passing(self):
        """A never-executed step must not read as a satisfied SLO (#213)."""
        stats = self.stats()
        _, actual, passed = verdict("step:refund p95 < 500ms", stats)
        self.assertIsNone(actual)
        self.assertFalse(passed)

    def test_a_step_that_never_ran_has_no_error_rate_either(self):
        """0 errors over 0 attempts is undefined, not a clean 0% (#213)."""
        _, actual, passed = verdict("step:refund error_rate < 1%", self.stats())
        self.assertIsNone(actual)
        self.assertFalse(passed)

    def test_a_step_that_never_ran_has_no_rps(self):
        _, actual, passed = verdict("step:refund rps > 1", self.stats())
        self.assertIsNone(actual)
        self.assertFalse(passed)

    def test_a_step_that_only_errored_still_reports_its_error_rate(self):
        stats = stats_with_steps({}, errors={"pay": 5})
        self.assertEqual(verdict("step:pay error_rate < 1%", stats)[1], 100.0)
        self.assertFalse(verdict("step:pay error_rate < 1%", stats)[2])

    def test_a_step_that_only_errored_has_no_latency(self):
        stats = stats_with_steps({}, errors={"pay": 5})
        _, actual, passed = verdict("step:pay p95 < 1s", stats)
        self.assertIsNone(actual)
        self.assertFalse(passed)

    def test_a_folded_step_name_cannot_silently_pass(self):
        """Distributed merges cap unique step names into `[other steps]`.

        A threshold naming a step that got folded away finds nothing, so it
        fails rather than reporting a satisfied gate about data it cannot see.
        """
        stats = stats_with_steps({"[other steps]": [0.01] * 10})
        _, actual, passed = verdict("step:checkout p95 < 1s", stats)
        self.assertIsNone(actual)
        self.assertFalse(passed)

    def test_step_and_aggregate_thresholds_coexist(self):
        stats = self.stats()
        results = evaluate_thresholds(
            [
                parse_threshold("p95 < 500ms"),
                parse_threshold("step:login p95 < 50ms"),
                parse_threshold("step:payment p95 < 50ms"),
            ],
            stats,
            10.0,
        )
        self.assertEqual([r[2] for r in results], [True, True, False])

    def test_a_per_step_metric_means_the_same_as_the_aggregate_one(self):
        """One step's p95 equals the aggregate p95 of a run containing only it."""
        samples = [0.01 * i for i in range(1, 101)]
        one_step = stats_with_steps({"only": samples})
        self.assertAlmostEqual(
            verdict("step:only p95 < 10s", one_step)[1],
            verdict("p95 < 10s", one_step)[1],
        )


class TestReporting(unittest.TestCase):
    def test_the_table_names_the_step(self):
        stats = stats_with_steps({"payment": [0.25] * 10})
        results = evaluate_thresholds([parse_threshold("step:payment p95 < 50ms")], stats, 10.0)
        buf = io.StringIO()
        print_threshold_results(results, file=buf)
        text = buf.getvalue()
        self.assertIn("step:payment p95 < 50ms", text)
        self.assertIn("FAIL", text)

    def test_a_long_expression_is_not_truncated(self):
        stats = stats_with_steps({"a very long step name indeed": [0.01]})
        results = evaluate_thresholds(
            [parse_threshold("step:a very long step name indeed p95 < 50ms")], stats, 10.0
        )
        buf = io.StringIO()
        print_threshold_results(results, file=buf)
        self.assertIn("step:a very long step name indeed p95 < 50ms", buf.getvalue())

    def test_an_unmeasured_step_reads_as_not_measured(self):
        results = evaluate_thresholds(
            [parse_threshold("step:missing p95 < 50ms")], WorkerStats(), 10.0
        )
        buf = io.StringIO()
        print_threshold_results(results, file=buf)
        self.assertIn("not measured", buf.getvalue())


class TestStartupValidation(unittest.TestCase):
    """Both failures are caught before any load is applied."""

    def setUp(self):
        self.stderr = io.StringIO()
        patcher = patch("sys.stderr", self.stderr)
        patcher.start()
        self.addCleanup(patcher.stop)

    def parse(self, *argv):
        parser = _build_parser()
        with patch("sys.argv", ["pywrkr", *argv]):
            return _parse_and_validate_args(parser, parser.parse_args(list(argv)))

    def scenario_file(self) -> str:
        import json
        import os
        import tempfile

        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(
            {
                "name": "flow",
                "steps": [
                    {"name": "login", "path": "/login", "method": "POST"},
                    {"name": "payment", "path": "/pay", "method": "POST"},
                ],
            },
            handle,
        )
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def test_a_step_threshold_without_a_scenario_is_rejected(self):
        with self.assertRaises(SystemExit):
            self.parse("http://x/", "-d", "1", "--threshold", "step:checkout p95 < 1s")
        self.assertIn("needs --scenario", self.stderr.getvalue())

    def test_an_unknown_step_name_is_rejected_and_the_known_ones_listed(self):
        """A typo and a step that never ran need different fixes, so they are
        different messages -- and a typo is knowable before the run starts.
        """
        with self.assertRaises(SystemExit):
            self.parse(
                "http://x/",
                "-d",
                "1",
                "--scenario",
                self.scenario_file(),
                "--threshold",
                "step:checkut p95 < 1s",
            )
        message = self.stderr.getvalue()
        self.assertIn("does not define", message)
        self.assertIn("'checkut'", message)
        self.assertIn("'login'", message)
        self.assertIn("'payment'", message)

    def test_a_known_step_name_is_accepted(self):
        config, _ = self.parse(
            "http://x/",
            "-d",
            "1",
            "--scenario",
            self.scenario_file(),
            "--threshold",
            "step:payment p95 < 1s",
        )
        self.assertEqual(config.thresholds[0].step, "payment")

    def test_an_aggregate_threshold_needs_no_scenario(self):
        config, _ = self.parse("http://x/", "-d", "1", "--threshold", "p95 < 1s")
        self.assertIsNone(config.thresholds[0].step)

    def test_a_default_named_step_is_matched_by_its_generated_name(self):
        import json
        import os
        import tempfile

        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump({"name": "s", "steps": [{"path": "/api"}]}, handle)
        handle.close()
        self.addCleanup(os.unlink, handle.name)

        from pywrkr.config import load_scenario

        generated = load_scenario(handle.name).steps[0].name
        config, _ = self.parse(
            "http://x/",
            "-d",
            "1",
            "--scenario",
            handle.name,
            "--threshold",
            f"step:{generated} p95 < 1s",
        )
        self.assertEqual(config.thresholds[0].step, generated)


class TestDistributedRoundTrip(unittest.TestCase):
    """Without `step` on the wire a per-step threshold would arrive at the worker
    as an aggregate one, gating on a different number without saying so.
    """

    def test_a_step_threshold_survives_the_wire(self):
        from pywrkr.distributed import _deserialize_config, _serialize_config

        config = BenchmarkConfig(
            url="http://x/",
            thresholds=[parse_threshold("step:checkout p95 < 800ms")],
            scenario=Scenario(name="s", steps=[ScenarioStep(path="/c", name="checkout")]),
        )
        restored = _deserialize_config(_serialize_config(config))
        self.assertEqual(restored.thresholds[0].step, "checkout")
        self.assertEqual(restored.thresholds[0].metric, "p95")
        self.assertAlmostEqual(restored.thresholds[0].value, 0.8)

    def test_an_aggregate_threshold_stays_aggregate(self):
        from pywrkr.distributed import _deserialize_config, _serialize_config

        config = BenchmarkConfig(url="http://x/", thresholds=[parse_threshold("p95 < 1s")])
        restored = _deserialize_config(_serialize_config(config))
        self.assertIsNone(restored.thresholds[0].step)

    def test_every_threshold_field_is_on_the_wire(self):
        """Guards the next field added to Threshold, like the config test does."""
        from dataclasses import fields

        from pywrkr.distributed import _serialize_threshold

        serialized = set(_serialize_threshold(parse_threshold("p95 < 1s")))
        self.assertEqual(
            {
                f.name
                for f in fields(
                    BenchmarkConfig.__annotations__
                    and __import__("pywrkr.config", fromlist=["Threshold"]).Threshold
                )
            }
            - serialized,
            set(),
        )


if __name__ == "__main__":
    unittest.main()
