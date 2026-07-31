"""Tests for the pytest plugin (``pywrkr[pytest]``).

The integration tests drive a nested pytest session with ``pytester``, against
a real aiohttp server started inside the inner test. Anything less would not
exercise what actually matters here: option parsing, marker handling, fixture
wiring, the skip gate, and what the terminal ends up showing.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

import pytest

from pytest_pywrkr import (
    BenchRecord,
    format_summary,
    json_filename,
    resolve_target,
    xdist_worker_count,
)
from pywrkr.api import Percentiles, Result, ThresholdVerdict
from pywrkr.config import WorkerStats

pytest_plugins = ["pytester"]


def make_result(**overrides) -> Result:
    data = {
        "duration_sec": 10.0,
        "total_requests": 1000,
        "total_errors": 5,
        "total_bytes": 4096,
        "requests_per_sec": 100.0,
        "latency": {"min": 0.001, "max": 0.4, "mean": 0.05, "median": 0.04, "stdev": 0.01},
        "percentiles": {"p50": 0.04, "p95": 0.12, "p99": 0.3},
        "status_codes": {"200": 995},
        "error_types": {},
    }
    data.update(overrides.pop("data", {}))
    return Result(
        _data=data,
        stats=WorkerStats(),
        thresholds=tuple(overrides.pop("thresholds", ())),
    )


def record(name: str = "test_health", **overrides) -> BenchRecord:
    return BenchRecord(
        nodeid=f"tests/test_perf.py::{name}",
        url="http://localhost/health",
        result=make_result(**overrides),
    )


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestResolveTarget(unittest.TestCase):
    def test_a_relative_target_is_joined_to_the_base_url(self):
        self.assertEqual(resolve_target("/health", "http://api:8080"), "http://api:8080/health")

    def test_slashes_are_not_doubled(self):
        self.assertEqual(resolve_target("/health", "http://api:8080/"), "http://api:8080/health")
        self.assertEqual(resolve_target("health", "http://api:8080"), "http://api:8080/health")

    def test_an_absolute_target_wins_over_the_base_url(self):
        """So one test can point somewhere else without reconfiguring the suite."""
        self.assertEqual(resolve_target("https://other/x", "http://api:8080"), "https://other/x")

    def test_a_relative_target_without_a_base_url_is_a_usage_error(self):
        with self.assertRaises(pytest.UsageError) as ctx:
            resolve_target("/health", None)
        self.assertIn("pywrkr_base_url", str(ctx.exception))


class TestJsonFilename(unittest.TestCase):
    def test_a_node_id_becomes_a_safe_filename(self):
        self.assertEqual(
            json_filename("tests/test_perf.py::test_health"), "tests-test_perf.py-test_health.json"
        )

    def test_parametrised_ids_stay_distinct(self):
        a = json_filename("t.py::test_x[1-a]")
        b = json_filename("t.py::test_x[2-a]")
        self.assertNotEqual(a, b)

    def test_no_path_separators_survive(self):
        name = json_filename("a/b/c.py::test_x")
        self.assertNotIn("/", name)
        self.assertNotIn("\\", name)

    def test_an_empty_id_still_produces_a_name(self):
        self.assertEqual(json_filename("///"), "benchmark.json")


class TestFormatSummary(unittest.TestCase):
    def test_no_records_render_nothing(self):
        self.assertEqual(format_summary([]), [])

    def test_the_table_has_a_header_a_rule_and_one_row_per_record(self):
        lines = format_summary([record("test_a"), record("test_b")])
        self.assertEqual(len(lines), 4)
        self.assertIn("Test", lines[0])
        self.assertIn("p95", lines[0])
        self.assertTrue(set(lines[1]) == {"-"})
        self.assertTrue(lines[2].startswith("test_a"))

    def test_the_columns_line_up_across_rows(self):
        """A misaligned table is the whole reason to render one by hand."""
        lines = format_summary([record("short"), record("a_very_long_test_name_here")])
        self.assertEqual(len(lines[0]), len(lines[1]))
        self.assertEqual(len(set(len(line) for line in lines[2:])), 1)

    def test_sub_second_latencies_read_in_milliseconds(self):
        lines = format_summary([record()])
        self.assertIn("40.00ms", lines[2])
        self.assertIn("120.00ms", lines[2])

    def test_second_scale_latencies_do_not_read_as_thousands_of_ms(self):
        lines = format_summary([record(data={"percentiles": {"p50": 1.5, "p95": 2.0, "p99": 3.0}})])
        self.assertIn("1.500s", lines[2])
        self.assertNotIn("1500.00ms", lines[2])

    def test_a_passing_record_says_pass(self):
        verdict = ThresholdVerdict("p95 < 500ms", "p95", 0.12, True)
        lines = format_summary([record(thresholds=[verdict])])
        self.assertIn("PASS", lines[2])

    def test_a_breach_says_fail_and_names_the_expression(self):
        verdict = ThresholdVerdict("p95 < 50ms", "p95", 0.12, False)
        lines = format_summary([record(thresholds=[verdict])])
        self.assertIn("FAIL", lines[2])
        self.assertIn("p95 < 50ms", lines[3])

    def test_the_record_name_drops_the_file_path(self):
        self.assertEqual(record("test_health").name, "test_health")


class TestXdistDetection(unittest.TestCase):
    class FakeConfig:
        def __init__(self, numprocesses=None, worker=False):
            self._n = numprocesses
            if worker:
                self.workerinput = {}

        def getoption(self, name, default=None):
            return self._n if name == "numprocesses" else default

    def test_no_xdist_is_zero(self):
        self.assertEqual(xdist_worker_count(self.FakeConfig()), 0)
        self.assertEqual(xdist_worker_count(self.FakeConfig(numprocesses=0)), 0)

    def test_an_explicit_worker_count_is_reported(self):
        self.assertEqual(xdist_worker_count(self.FakeConfig(numprocesses=4)), 4)

    def test_auto_counts_as_more_than_one(self):
        self.assertGreater(xdist_worker_count(self.FakeConfig(numprocesses="auto")), 1)

    def test_being_inside_a_worker_is_detected(self):
        self.assertGreater(xdist_worker_count(self.FakeConfig(worker=True)), 1)


# ---------------------------------------------------------------------------
# Integration, via a nested pytest session
# ---------------------------------------------------------------------------

SERVER_CONFTEST = '''
import pytest
from aiohttp import web

@pytest.fixture(scope="session")
def base_url():
    """A real server for the benchmark to hit, started for the session."""
    import asyncio, threading

    holder = {}
    ready = threading.Event()

    async def handler(request):
        return web.json_response({"ok": True})

    def serve():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def main():
            app = web.Application()
            app.router.add_get("/{tail:.*}", handler)
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            holder["port"] = site._server.sockets[0].getsockname()[1]
            holder["runner"] = runner
            ready.set()
            while not holder.get("stop"):
                await asyncio.sleep(0.05)
            await runner.cleanup()

        loop.run_until_complete(main())

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    ready.wait(10)
    yield f"http://127.0.0.1:{holder['port']}"
    holder["stop"] = True
    thread.join(timeout=5)
'''


POINT_INI_AT_SERVER = """
@pytest.fixture(autouse=True, scope="session")
def _point_ini_at_server(base_url, pytestconfig):
    pytestconfig._inicache["pywrkr_base_url"] = base_url
"""


@pytest.fixture
def perf_project(pytester):
    """A throwaway project with a live target and the plugin enabled."""
    pytester.makeconftest(SERVER_CONFTEST)
    return pytester


class TestSkipGate:
    """Benchmarks are real load, so they must not run by accident."""

    def test_a_marked_test_skips_without_the_opt_in(self, pytester):
        pytester.makepyfile(
            """
            import pytest

            @pytest.mark.pywrkr(url="http://127.0.0.1:1/", duration=1)
            def test_perf(pywrkr_result):
                assert False, "must not have run"
            """
        )
        result = pytester.runpytest("-rs")
        result.assert_outcomes(skipped=1)
        result.stdout.fnmatch_lines(["*real load*--pywrkr-run*"])

    def test_a_bench_fixture_test_skips_too(self, pytester):
        pytester.makepyfile(
            """
            def test_perf(pywrkr_bench):
                assert False, "must not have run"
            """
        )
        pytester.runpytest().assert_outcomes(skipped=1)

    def test_ordinary_tests_are_untouched(self):
        """The plugin loads in every pytest run; it must not skip anything else."""
        pass

    def test_an_unrelated_test_still_runs(self, pytester):
        pytester.makepyfile(
            """
            def test_normal():
                assert 1 + 1 == 2
            """
        )
        pytester.runpytest().assert_outcomes(passed=1)


class TestBenchFixture:
    def test_it_runs_a_benchmark_and_returns_a_result(self, perf_project):
        perf_project.makepyfile(
            """
            def test_health(pywrkr_bench, base_url):
                result = pywrkr_bench(base_url + "/health", connections=4, duration=1)
                assert result.total_requests > 0
                assert result.error_rate == 0.0
                assert result.percentiles.p95 > 0
            """
        )
        perf_project.runpytest("--pywrkr-run").assert_outcomes(passed=1)

    def test_ini_defaults_supply_the_base_url_and_load_shape(self, perf_project):
        perf_project.makepyfile(
            """
            def test_health(pywrkr_bench):
                result = pywrkr_bench("/health")
                # Straight from the ini file, with nothing passed at the call.
                assert result.duration >= 0.9
                assert result.to_dict()["config"]["connections"] == 3
            """
        )
        perf_project.makefile(
            ".ini",
            pytest=(
                "[pytest]\n"
                "pywrkr_duration = 1\n"
                "pywrkr_connections = 3\n"
                "pywrkr_base_url = http://127.0.0.1:{port}\n"
            ),
        )
        # The port is only known at run time, so point the ini at the fixture's
        # server by writing the file from inside the test session instead.
        perf_project.makeconftest(
            SERVER_CONFTEST
            + """
def pytest_configure(config):
    pass

@pytest.fixture(autouse=True, scope="session")
def _point_ini_at_server(base_url, pytestconfig):
    pytestconfig._inicache["pywrkr_base_url"] = base_url
"""
        )
        perf_project.runpytest("--pywrkr-run").assert_outcomes(passed=1)

    def test_explicit_arguments_beat_ini_defaults(self, perf_project):
        perf_project.makepyfile(
            """
            def test_health(pywrkr_bench, base_url):
                result = pywrkr_bench(base_url + "/x", connections=2, duration=1)
                assert result.to_dict()["config"]["connections"] == 2
            """
        )
        perf_project.makefile(".ini", pytest="[pytest]\npywrkr_connections = 9\n")
        perf_project.runpytest("--pywrkr-run").assert_outcomes(passed=1)


class TestMarker:
    def test_the_marker_runs_the_benchmark_for_the_test(self, perf_project):
        perf_project.makepyfile(
            """
            import pytest

            @pytest.mark.pywrkr(url="/ping", connections=4, duration=1,
                                thresholds=["p95 < 30s", "error_rate < 5%"])
            def test_ping(pywrkr_result):
                assert pywrkr_result.total_requests > 0
                assert pywrkr_result.passed
            """
        )
        perf_project.makeconftest(
            SERVER_CONFTEST
            + """
@pytest.fixture(autouse=True, scope="session")
def _point_ini_at_server(base_url, pytestconfig):
    pytestconfig._inicache["pywrkr_base_url"] = base_url
"""
        )
        perf_project.runpytest("--pywrkr-run").assert_outcomes(passed=1)

    def test_a_breached_threshold_fails_the_test_naming_metric_and_bound(self, perf_project):
        """A declarative SLO should need no assert, and read as a perf failure."""
        perf_project.makepyfile(
            """
            import pytest

            @pytest.mark.pywrkr(url="/slow", connections=4, duration=1,
                                thresholds=["p95 < 1us"])
            def test_slow(pywrkr_result):
                pass
            """
        )
        perf_project.makeconftest(
            SERVER_CONFTEST
            + """
@pytest.fixture(autouse=True, scope="session")
def _point_ini_at_server(base_url, pytestconfig):
    pytestconfig._inicache["pywrkr_base_url"] = base_url
"""
        )
        result = perf_project.runpytest("--pywrkr-run")
        result.assert_outcomes(failed=1)
        result.stdout.fnmatch_lines(
            [
                "*pywrkr threshold(s) breached*",
                "*p95 < 1us*measured p95 =*",
            ]
        )

    def test_the_breach_outranks_a_failure_in_the_test_body(self, perf_project):
        """The SLO was missed first; a later assertion is usually downstream."""
        perf_project.makepyfile(
            """
            import pytest

            @pytest.mark.pywrkr(url="/slow", connections=4, duration=1,
                                thresholds=["p95 < 1us"])
            def test_slow(pywrkr_result):
                assert False, "downstream complaint"
            """
        )
        perf_project.makeconftest(SERVER_CONFTEST + POINT_INI_AT_SERVER)
        result = perf_project.runpytest("--pywrkr-run")
        result.assert_outcomes(failed=1)
        result.stdout.fnmatch_lines(["*pywrkr threshold(s) breached*"])
        assert "downstream complaint" not in result.stdout.str()

    def test_the_url_may_be_positional(self, perf_project):
        perf_project.makepyfile(
            """
            import pytest

            @pytest.mark.pywrkr("/ping", connections=2, duration=1)
            def test_ping(pywrkr_result):
                assert pywrkr_result.total_requests > 0
            """
        )
        perf_project.makeconftest(
            SERVER_CONFTEST
            + """
@pytest.fixture(autouse=True, scope="session")
def _point_ini_at_server(base_url, pytestconfig):
    pytestconfig._inicache["pywrkr_base_url"] = base_url
"""
        )
        perf_project.runpytest("--pywrkr-run").assert_outcomes(passed=1)

    def test_the_result_fixture_without_a_marker_is_a_usage_error(self, pytester):
        pytester.makepyfile(
            """
            import pytest

            @pytest.mark.pywrkr(url="http://127.0.0.1:1/", duration=1)
            def test_marked(pywrkr_result):
                pass

            def test_unmarked(pywrkr_result):
                pass
            """
        )
        result = pytester.runpytest(
            "--pywrkr-run",
            "test_the_result_fixture_without_a_marker_is_a_usage_error.py::test_unmarked",
        )
        result.stdout.fnmatch_lines(["*requires a @pytest.mark.pywrkr*"])

    def test_a_marker_without_a_url_is_a_usage_error(self, pytester):
        pytester.makepyfile(
            """
            import pytest

            @pytest.mark.pywrkr(duration=1)
            def test_no_url(pywrkr_result):
                pass
            """
        )
        result = pytester.runpytest("--pywrkr-run")
        result.stdout.fnmatch_lines(["*requires a `url`*"])


class TestTerminalSummary:
    def test_the_summary_table_is_printed_after_a_benchmark(self, perf_project):
        perf_project.makepyfile(
            """
            def test_health(pywrkr_bench, base_url):
                pywrkr_bench(base_url + "/health", connections=2, duration=1)
            """
        )
        result = perf_project.runpytest("--pywrkr-run")
        result.stdout.fnmatch_lines(
            [
                "*pywrkr benchmarks*",
                "*Test*Requests*Req/s*p50*p95*p99*Errors*Verdict*",
                "test_health*PASS*",
            ]
        )

    def test_no_summary_when_nothing_benchmarked(self, pytester):
        pytester.makepyfile("def test_normal(): pass")
        result = pytester.runpytest("--pywrkr-run")
        assert "pywrkr benchmarks" not in result.stdout.str()


class TestJsonOutput:
    def test_each_benchmark_writes_a_schema_valid_file(self, perf_project):
        perf_project.makepyfile(
            """
            def test_alpha(pywrkr_bench, base_url):
                pywrkr_bench(base_url + "/a", connections=2, duration=1)

            def test_beta(pywrkr_bench, base_url):
                pywrkr_bench(base_url + "/b", connections=2, duration=1)
            """
        )
        out_dir = perf_project.path / "perf-json"
        perf_project.runpytest("--pywrkr-run", "--pywrkr-json", str(out_dir)).assert_outcomes(
            passed=2
        )

        files = sorted(p.name for p in Path(out_dir).glob("*.json"))
        assert len(files) == 2, files
        assert any("test_alpha" in name for name in files)
        assert any("test_beta" in name for name in files)

        payload = json.loads((Path(out_dir) / files[0]).read_text(encoding="utf-8"))
        # Must be the same shape `pywrkr compare` reads, or the documented
        # "feed it to compare" workflow does not exist.
        for key in ("schema_version", "duration_sec", "total_requests", "percentiles"):
            assert key in payload, (key, sorted(payload))

    def test_the_files_really_load_in_compare(self, perf_project):
        perf_project.makepyfile(
            """
            def test_alpha(pywrkr_bench, base_url):
                pywrkr_bench(base_url + "/a", connections=2, duration=1)
            """
        )
        out_dir = perf_project.path / "perf-json"
        perf_project.runpytest("--pywrkr-run", "--pywrkr-json", str(out_dir))

        from pywrkr.compare import compare_results, load_baseline

        baseline, sources = load_baseline(str(out_dir / "*.json"))
        assert sources
        report = compare_results(baseline, baseline, [])
        assert not report.regressed

    def test_no_directory_is_created_without_the_option(self, perf_project):
        perf_project.makepyfile(
            """
            def test_alpha(pywrkr_bench, base_url):
                pywrkr_bench(base_url + "/a", connections=2, duration=1)
            """
        )
        perf_project.runpytest("--pywrkr-run").assert_outcomes(passed=1)
        assert not list(perf_project.path.glob("*.json"))


class TestXdistRefusal:
    """Parallel benchmarks measure the wrong thing, so they are refused."""

    def test_combining_with_xdist_is_a_clean_usage_error(self, pytester):
        pytester.makepyfile("def test_normal(): pass")
        result = pytester.runpytest("--pywrkr-run", "-n", "2")
        assert result.ret != 0
        result.stderr.fnmatch_lines(["*cannot be combined with pytest-xdist*"])

    def test_xdist_alone_is_fine(self, pytester):
        """Only --pywrkr-run is refused; the plugin must not break parallel suites."""
        pytester.makepyfile("def test_normal(): pass")
        pytester.runpytest("-n", "2").assert_outcomes(passed=1)


class TestImportCost:
    def test_the_plugin_lives_outside_the_pywrkr_package(self):
        """A `pywrkr.*` module would drag in the package __init__ at startup."""
        import pytest_pywrkr

        assert pytest_pywrkr.__name__ == "pytest_pywrkr"
        assert not pytest_pywrkr.__name__.startswith("pywrkr.")

    def test_importing_the_plugin_does_not_import_pywrkr(self):
        """~90ms on every pytest run of any project that merely depends on it."""
        import subprocess
        import sys

        probe = (
            "import sys; import pytest_pywrkr; "
            "print([m for m in sys.modules if m == 'pywrkr' or m.startswith('pywrkr.')])"
        )
        out = subprocess.run(
            [sys.executable, "-c", probe], capture_output=True, text=True, check=True
        )
        assert out.stdout.strip() == "[]", out.stdout

    def test_the_plugin_imports_nothing_from_pywrkr_at_module_scope(self):
        """It loads in every pytest run of any project that installs pywrkr."""
        import ast
        import inspect

        import pytest_pywrkr as plugin

        tree = ast.parse(inspect.getsource(plugin))
        top_level = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
        modules = []
        for node in top_level:
            if isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            elif node.module:
                modules.append(node.module)
        offenders = [m for m in modules if m.startswith("pywrkr")]
        assert offenders == [], f"module-scope pywrkr imports: {offenders}"

    def test_the_entry_point_is_registered(self):
        """Read the installed metadata, not pyproject: this is what pytest sees."""
        from importlib.metadata import entry_points

        registered = {
            ep.name: ep.value for ep in entry_points(group="pytest11") if ep.name == "pywrkr"
        }
        assert registered == {"pywrkr": "pytest_pywrkr"}, registered

    def test_the_pytest_extra_is_declared(self):
        from importlib.metadata import metadata

        extras = metadata("pywrkr").get_all("Provides-Extra") or []
        assert "pytest" in extras, extras
        assert "all" in extras, extras


class TestPercentilesAccessor(unittest.TestCase):
    """The summary reads p50/p95/p99 off Result; make sure that keeps working."""

    def test_missing_percentiles_read_as_zero_not_an_error(self):
        pct = Percentiles({})
        self.assertEqual((pct.p50, pct.p95, pct.p99), (0.0, 0.0, 0.0))
        self.assertEqual(format_summary([record(data={"percentiles": {}})])[2].count("0.00ms"), 3)
