"""``--no-read-body``: skip the response body when nothing looks at it (#217).

Measured before building, and the numbers are why this is opt-in rather than the
default: on a 1.2 MiB payload releasing is 15% *slower*, and the apparent gain on
smaller ones comes from `resp.release()` not being awaited -- so the run stops
timing receipt of the response. The tests below pin the behaviour and the
disclosure, not a speed-up.
"""

from __future__ import annotations

import unittest

from aiohttp import web

from pywrkr.compare import config_differences
from pywrkr.config import BenchmarkConfig, Scenario, ScenarioStep, WorkerStats, parse_extract_spec
from pywrkr.reporting import build_results_dict
from pywrkr.workers import needs_body


def config(**kwargs) -> BenchmarkConfig:
    return BenchmarkConfig(url="http://x/", **kwargs)


class TestNeedsBody(unittest.TestCase):
    def test_reading_is_the_default(self):
        self.assertTrue(needs_body(config()))

    def test_the_flag_turns_it_off(self):
        self.assertFalse(needs_body(config(read_body=False)))

    def test_verify_length_overrides_the_flag(self):
        """It compares declared Content-Length against what arrived."""
        self.assertTrue(needs_body(config(read_body=False, verify_content_length=True)))

    def test_body_logging_verbosity_overrides_the_flag(self):
        self.assertTrue(needs_body(config(read_body=False, verbosity=3)))
        self.assertFalse(needs_body(config(read_body=False, verbosity=2)))

    def test_a_step_with_an_extract_rule_reads_its_body(self):
        step = ScenarioStep(
            path="/x", extract=parse_extract_spec({"t": {"json": "$.token"}}, "step")
        )
        self.assertTrue(needs_body(config(read_body=False), step))

    def step_with(self, **raw) -> ScenarioStep:
        """A step whose assertions are compiled the way a scenario file's are."""
        from pywrkr.assertions import parse_step_assertions

        return ScenarioStep(path="/x", assertions=parse_step_assertions(raw, "step"))

    def test_a_step_asserting_on_the_body_reads_it(self):
        for raw in (
            {"assert_body_contains": "ok"},
            {"assert_body_regex": "o.*k"},
            {"assert_json": {"$.a": 1}},
        ):
            with self.subTest(assertion=next(iter(raw))):
                self.assertTrue(needs_body(config(read_body=False), self.step_with(**raw)))

    def test_a_status_only_step_does_not(self):
        self.assertFalse(needs_body(config(read_body=False), self.step_with(assert_status=200)))

    def test_a_header_assertion_does_not_need_the_body(self):
        step = self.step_with(assert_header={"Content-Type": "application/json"})
        self.assertFalse(needs_body(config(read_body=False), step))

    def test_a_latency_assertion_does_not_need_the_body(self):
        step = self.step_with(assert_max_latency="500ms")
        self.assertFalse(needs_body(config(read_body=False), step))

    def test_the_decision_is_per_step_not_per_run(self):
        """A scenario mixing both kinds must not read all bodies or none."""
        cfg = config(read_body=False)
        needs = ScenarioStep(path="/a", extract=parse_extract_spec({"t": {"json": "$.t"}}, "step"))
        does_not = ScenarioStep(path="/b", assert_status=204)
        self.assertTrue(needs_body(cfg, needs))
        self.assertFalse(needs_body(cfg, does_not))

    def test_with_reading_on_every_step_reads(self):
        cfg = config()
        self.assertTrue(needs_body(cfg, ScenarioStep(path="/b", assert_status=204)))


class TestDisclosure(unittest.TestCase):
    """A zero must never be ambiguous."""

    def results(self, **kwargs) -> dict:
        return build_results_dict(WorkerStats(), 10.0, 1, config(**kwargs))

    def test_the_results_say_whether_bodies_were_read(self):
        self.assertTrue(self.results()["config"]["read_body"])
        self.assertFalse(self.results(read_body=False)["config"]["read_body"])

    def test_compare_warns_when_the_two_runs_differ(self):
        """Otherwise it reports a spectacular transfer-rate collapse as a regression."""
        warnings = config_differences(self.results(), self.results(read_body=False))
        self.assertTrue(any("read_body" in w for w in warnings), warnings)

    def test_compare_is_quiet_when_they_agree(self):
        self.assertEqual(config_differences(self.results(), self.results()), [])

    def test_a_pre_flag_baseline_is_read_as_having_read_bodies(self):
        """Files written before the flag existed carry no key; the default is True."""
        old = self.results()
        del old["config"]["read_body"]
        warnings = config_differences(old, self.results())
        self.assertFalse(any("read_body" in w for w in warnings), warnings)

    def test_it_travels_the_distributed_wire(self):
        from pywrkr.distributed import _deserialize_config, _serialize_config

        for value in (True, False):
            with self.subTest(read_body=value):
                restored = _deserialize_config(_serialize_config(config(read_body=value)))
                self.assertIs(restored.read_body, value)

    def test_an_old_worker_config_defaults_to_reading(self):
        from pywrkr.distributed import _deserialize_config, _serialize_config

        payload = _serialize_config(config())
        del payload["read_body"]
        self.assertTrue(_deserialize_config(payload).read_body)


class TestAgainstAServer(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.body = b'{"payload":"' + b"x" * 4000 + b'"}'
        self.connections: set = set()

        async def handler(request):
            # Which TCP connection served this request, so reuse is observable.
            transport = request.transport
            if transport is not None:
                self.connections.add(id(transport))
            return web.Response(body=self.body, content_type="application/json")

        app = web.Application()
        app.router.add_get("/{t:.*}", handler)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/x"

    async def asyncTearDown(self):
        await self.runner.cleanup()

    async def run_bench(self, **kwargs):
        from pywrkr.api import arun

        return await arun(self.url, connections=2, duration=1.0, **kwargs)

    async def test_reading_counts_the_bytes(self):
        result = await self.run_bench()
        self.assertGreater(result.total_requests, 0)
        self.assertEqual(result.total_bytes, result.total_requests * len(self.body))

    async def test_not_reading_counts_none(self):
        result = await self.run_bench(read_body=False)
        self.assertGreater(result.total_requests, 0)
        self.assertEqual(result.total_bytes, 0)

    async def test_keep_alive_survives_an_unread_response(self):
        """An HTTP/1.1 connection must be drained before it can carry the next
        response. If releasing broke that, every request would need a new
        connection -- which is both slow and a different test than intended.
        """
        result = await self.run_bench(read_body=False)
        self.assertGreater(result.total_requests, 50, "too few requests to judge reuse")
        # Two connections were asked for; a broken release would open one per
        # request instead.
        self.assertLessEqual(len(self.connections), 8, len(self.connections))

    async def test_the_run_still_succeeds_and_reports_statuses(self):
        result = await self.run_bench(read_body=False)
        self.assertEqual(result.total_errors, 0, result.error_types)
        self.assertEqual(result.status_codes.get(200), result.total_requests)

    async def test_a_scenario_step_that_extracts_still_works_with_the_flag(self):
        """The flag must not be able to break a flow that reads response content."""
        from pywrkr.api import arun

        scenario = Scenario(
            name="s",
            steps=[
                ScenarioStep(
                    path="/token",
                    name="get",
                    extract=parse_extract_spec({"p": {"json": "$.payload"}}, "step"),
                ),
                ScenarioStep(path="/use", name="use", assert_status=200),
            ],
        )
        result = await arun(self.url, scenario=scenario, users=2, duration=1.0, read_body=False)
        self.assertEqual(result.to_dict()["extract_failures"], 0)
        self.assertEqual(result.total_errors, 0, result.error_types)
        # The extracting step read its body, so some bytes were counted even
        # though the flag is on.
        self.assertGreater(result.total_bytes, 0)


if __name__ == "__main__":
    unittest.main()
