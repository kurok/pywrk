"""A threshold on a metric the run never produced must fail (#213).

`_get_metric_value` used to substitute 0.0 for anything it could not find, so
`p95 < 500ms` -- the common shape of a CI gate -- passed on a run that recorded
no latency at all. A gate that is silent and a gate that is satisfied must not
look the same.
"""

from __future__ import annotations

import io
import unittest

from pywrkr.api import build_result
from pywrkr.ci import evaluate_from_results
from pywrkr.config import AutofindConfig, BenchmarkConfig, WorkerStats
from pywrkr.reporting import (
    build_results_dict,
    evaluate_thresholds,
    format_threshold_actual,
    parse_threshold,
    print_threshold_results,
)
from pywrkr.workers import _extract_step_result, _step_passed

#: Every metric --threshold accepts, and whether it survives an empty run.
LATENCY_METRICS = ("p50", "p75", "p90", "p95", "p99", "avg_latency", "max_latency", "min_latency")


def stats_with(latencies=(), errors=0, total=0, **kwargs) -> WorkerStats:
    stats = WorkerStats(total_requests=total, errors=errors, **kwargs)
    for value in latencies:
        stats.latencies.append(value)
    return stats


def verdict(expr: str, stats: WorkerStats, duration: float = 10.0):
    return evaluate_thresholds([parse_threshold(expr)], stats, duration)[0]


class TestUnmeasuredMetricsFail(unittest.TestCase):
    def test_a_latency_threshold_fails_when_nothing_was_measured(self):
        for metric in LATENCY_METRICS:
            with self.subTest(metric=metric):
                _, actual, passed = verdict(f"{metric} < 500ms", stats_with())
                self.assertIsNone(actual)
                self.assertFalse(passed)

    def test_it_fails_even_when_requests_were_attempted(self):
        """Every request can error before a latency lands; p95 still does not exist."""
        stats = stats_with(latencies=[], errors=100, total=100)
        _, actual, passed = verdict("p95 < 500ms", stats)
        self.assertIsNone(actual)
        self.assertFalse(passed)

    def test_error_rate_over_no_requests_is_undefined_not_zero(self):
        _, actual, passed = verdict("error_rate < 1%", stats_with(total=0))
        self.assertIsNone(actual)
        self.assertFalse(passed)

    def test_rps_over_zero_duration_is_undefined(self):
        _, actual, passed = verdict("rps > 10", stats_with(total=100), duration=0.0)
        self.assertIsNone(actual)
        self.assertFalse(passed)

    def test_the_greater_than_direction_also_reports_not_measured(self):
        """It failed before too, but by luck of the operator rather than on purpose."""
        _, actual, passed = verdict("p99 > 1s", stats_with())
        self.assertIsNone(actual)
        self.assertFalse(passed)

    def test_an_unknown_metric_cannot_pass(self):
        threshold = parse_threshold("p95 < 500ms")
        object.__setattr__(threshold, "metric", "nonexistent") if hasattr(
            threshold, "__setattr__"
        ) else None
        threshold.metric = "nonexistent"
        _, actual, passed = evaluate_thresholds([threshold], stats_with(latencies=[0.1]), 10.0)[0]
        self.assertIsNone(actual)
        self.assertFalse(passed)


class TestGenuineZerosStillPass(unittest.TestCase):
    """The fix must not turn every zero into a failure."""

    def test_a_clean_run_still_passes_an_error_rate_gate(self):
        _, actual, passed = verdict("error_rate < 1%", stats_with(latencies=[0.1], total=100))
        self.assertEqual(actual, 0.0)
        self.assertTrue(passed)

    def test_a_measured_latency_still_passes(self):
        _, actual, passed = verdict("p95 < 500ms", stats_with(latencies=[0.1] * 10, total=10))
        self.assertAlmostEqual(actual, 0.1)
        self.assertTrue(passed)

    def test_a_real_breach_still_fails(self):
        _, actual, passed = verdict("p95 < 50ms", stats_with(latencies=[0.4] * 10, total=10))
        self.assertAlmostEqual(actual, 0.4)
        self.assertFalse(passed)

    def test_a_latency_of_exactly_zero_is_a_measurement(self):
        """0.0 from a real sample is data; 0.0 from nothing is not."""
        _, actual, passed = verdict("p95 < 1ms", stats_with(latencies=[0.0], total=1))
        self.assertEqual(actual, 0.0)
        self.assertTrue(passed)


class TestOutput(unittest.TestCase):
    def test_the_table_says_not_measured_rather_than_a_number(self):
        results = evaluate_thresholds([parse_threshold("p95 < 500ms")], stats_with(), 10.0)
        buf = io.StringIO()
        print_threshold_results(results, file=buf)
        text = buf.getvalue()
        self.assertIn("not measured", text)
        self.assertIn("FAIL", text)
        self.assertNotIn("0.00ms", text)
        self.assertIn("SOME FAILED", text)

    def test_measured_values_still_render_in_their_own_units(self):
        self.assertEqual(format_threshold_actual("p95", 0.1234), "123.40ms")
        self.assertEqual(format_threshold_actual("error_rate", 1.5), "1.50%")
        self.assertEqual(format_threshold_actual("rps", 1234.5), "1234.50")

    def test_none_renders_as_not_measured_for_every_metric_kind(self):
        for metric in ("p95", "error_rate", "rps", "avg_latency"):
            with self.subTest(metric=metric):
                self.assertEqual(format_threshold_actual(metric, None), "not measured")


class TestExitCodeContract(unittest.TestCase):
    def test_the_result_reports_the_breach_and_the_exit_code(self):
        config = BenchmarkConfig(url="http://x/", thresholds=[parse_threshold("p95 < 500ms")])
        result = build_result(stats_with(), config, duration=10.0, connections=1)
        self.assertFalse(result.passed)
        self.assertEqual(result.exit_code, 2)
        self.assertIsNone(result.thresholds[0].actual)
        self.assertFalse(result.thresholds[0].measured)

    def test_a_measured_pass_is_unchanged(self):
        config = BenchmarkConfig(url="http://x/", thresholds=[parse_threshold("p95 < 500ms")])
        result = build_result(
            stats_with(latencies=[0.05] * 10, total=10), config, duration=10.0, connections=1
        )
        self.assertTrue(result.passed)
        self.assertEqual(result.exit_code, 0)
        self.assertTrue(result.thresholds[0].measured)


class TestBothPathsAgree(unittest.TestCase):
    """The live gate and the results-file gate must not diverge again.

    `pywrkr.ci.evaluate_from_results` already refused to pass an unmeasured
    metric; `reporting.evaluate_thresholds` did not. That divergence is the bug.
    """

    def check(self, expr: str, stats: WorkerStats, duration: float):
        config = BenchmarkConfig(url="http://x/")
        live = evaluate_thresholds([parse_threshold(expr)], stats, duration)[0]
        from_file = evaluate_from_results(
            build_results_dict(stats, duration, 1, config), [parse_threshold(expr)]
        )[0]
        return live, from_file

    def test_they_agree_on_an_empty_run(self):
        for expr in ("p95 < 500ms", "error_rate < 1%", "avg_latency < 1s", "max_latency < 1s"):
            with self.subTest(expr=expr):
                live, from_file = self.check(expr, stats_with(), 10.0)
                self.assertEqual(live[2], from_file.passed, expr)
                self.assertFalse(live[2], expr)

    def test_they_agree_on_a_measured_run(self):
        stats = stats_with(latencies=[0.05] * 20, errors=1, total=20)
        for expr in ("p95 < 500ms", "error_rate < 10%", "rps > 1"):
            with self.subTest(expr=expr):
                live, from_file = self.check(expr, stats, 10.0)
                self.assertEqual(live[2], from_file.passed, expr)
                self.assertTrue(live[2], expr)

    def test_they_agree_on_a_real_breach(self):
        stats = stats_with(latencies=[0.9] * 20, errors=0, total=20)
        live, from_file = self.check("p95 < 50ms", stats, 10.0)
        self.assertEqual(live[2], from_file.passed)
        self.assertFalse(live[2])


class TestAutofindGate(unittest.TestCase):
    """--max-p95 had the same substitution: p95 of 0.0 sailed under any limit."""

    def config(self) -> AutofindConfig:
        return AutofindConfig(url="http://x/", max_p95=0.5, max_error_rate=5.0)

    def test_a_step_that_measured_nothing_is_not_sustainable(self):
        step = _extract_step_result(stats_with(), 10.0, 100, self.config())
        self.assertFalse(step.measured)
        self.assertEqual(step.latency_samples, 0)
        self.assertFalse(step.passed)

    def test_a_step_where_every_request_errored_is_not_sustainable(self):
        stats = stats_with(latencies=[], errors=500, total=500)
        step = _extract_step_result(stats, 10.0, 100, self.config())
        self.assertFalse(step.passed)

    def test_a_healthy_step_still_passes(self):
        stats = stats_with(latencies=[0.05] * 50, errors=0, total=50)
        step = _extract_step_result(stats, 10.0, 100, self.config())
        self.assertTrue(step.measured)
        self.assertEqual(step.latency_samples, 50)
        self.assertTrue(step.passed)

    def test_a_slow_step_still_fails(self):
        stats = stats_with(latencies=[0.9] * 50, errors=0, total=50)
        self.assertFalse(_extract_step_result(stats, 10.0, 100, self.config()).passed)

    def test_zero_requests_fails_even_with_latencies_somehow_present(self):
        """Defence in depth: both conditions gate the step independently."""
        from pywrkr.config import StepResult

        step = StepResult(
            users=1,
            rps=0.0,
            p50=0.01,
            p95=0.01,
            p99=0.01,
            error_rate=0.0,
            total_requests=0,
            total_errors=0,
            passed=True,
            measured=True,
            latency_samples=5,
        )
        self.assertFalse(_step_passed(step, self.config()))


if __name__ == "__main__":
    unittest.main()
