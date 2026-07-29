#!/usr/bin/env python3
"""Tests for per-virtual-user cookie sessions."""

import asyncio
import json
import os
import tempfile
import unittest
from io import StringIO
from unittest.mock import patch

import aiohttp
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase

import pywrkr
from pywrkr.main import _build_parser
from pywrkr.workers import _create_cookie_jar, _target_is_ip_literal

# ---------------------------------------------------------------------------
# Jar construction
# ---------------------------------------------------------------------------


class TestTargetIsIpLiteral(unittest.TestCase):
    def test_ipv4(self):
        self.assertTrue(_target_is_ip_literal("http://127.0.0.1:8080/x"))
        self.assertTrue(_target_is_ip_literal("https://10.0.0.5/"))

    def test_ipv6(self):
        self.assertTrue(_target_is_ip_literal("http://[::1]:8080/"))

    def test_hostname(self):
        self.assertFalse(_target_is_ip_literal("http://localhost:8080/"))
        self.assertFalse(_target_is_ip_literal("https://api.example.com/v1"))

    def test_no_host(self):
        self.assertFalse(_target_is_ip_literal("not-a-url"))
        self.assertFalse(_target_is_ip_literal(""))


class TestCreateCookieJar(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_yields_dummy_jar(self):
        config = pywrkr.BenchmarkConfig(url="http://example.com/", session_cookies=False)
        self.assertIsInstance(_create_cookie_jar(config), aiohttp.DummyCookieJar)

    async def test_hostname_target_uses_safe_jar(self):
        config = pywrkr.BenchmarkConfig(url="http://example.com/")
        jar = _create_cookie_jar(config)
        self.assertIsInstance(jar, aiohttp.CookieJar)
        self.assertFalse(jar._unsafe)

    async def test_ip_target_opens_the_jar(self):
        # aiohttp's default jar drops cookies for IP hosts, which would silently
        # disable sessions against the loopback targets load tests use.
        config = pywrkr.BenchmarkConfig(url="http://127.0.0.1:8080/")
        jar = _create_cookie_jar(config)
        self.assertIsInstance(jar, aiohttp.CookieJar)
        self.assertTrue(jar._unsafe)

    async def test_each_call_returns_a_distinct_jar(self):
        config = pywrkr.BenchmarkConfig(url="http://example.com/")
        self.assertIsNot(_create_cookie_jar(config), _create_cookie_jar(config))


class TestSessionCookiesFlag(unittest.TestCase):
    def test_default_is_on(self):
        args = _build_parser().parse_args(["http://example.com/"])
        self.assertTrue(args.session_cookies)

    def test_flag_turns_it_off(self):
        args = _build_parser().parse_args(["--no-session-cookies", "http://example.com/"])
        self.assertFalse(args.session_cookies)


class TestScenarioSessionOption(unittest.TestCase):
    def _load(self, data):
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        json.dump(data, f)
        f.close()
        try:
            return pywrkr.load_scenario(f.name)
        finally:
            os.unlink(f.name)

    def test_default_is_persistent(self):
        self.assertEqual(self._load({"steps": [{"path": "/"}]}).session, "persistent")

    def test_fresh_per_iteration_accepted(self):
        scenario = self._load({"session": "fresh_per_iteration", "steps": [{"path": "/"}]})
        self.assertEqual(scenario.session, "fresh_per_iteration")

    def test_invalid_value_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            self._load({"session": "sticky", "steps": [{"path": "/"}]})
        self.assertIn("session", str(ctx.exception))

    def test_round_trips_through_distributed_config(self):
        from pywrkr.distributed import _deserialize_config, _serialize_config

        scenario = pywrkr.Scenario(
            session="fresh_per_iteration", steps=[pywrkr.ScenarioStep(path="/")]
        )
        config = pywrkr.BenchmarkConfig(
            url="http://example.com", scenario=scenario, session_cookies=False
        )
        restored = _deserialize_config(json.loads(json.dumps(_serialize_config(config))))
        self.assertEqual(restored.scenario.session, "fresh_per_iteration")
        self.assertFalse(restored.session_cookies)

    def test_distributed_defaults_when_absent(self):
        from pywrkr.distributed import _deserialize_config, _serialize_config

        payload = _serialize_config(pywrkr.BenchmarkConfig(url="http://example.com"))
        del payload["session_cookies"]
        restored = _deserialize_config(payload)
        self.assertTrue(restored.session_cookies)


class TestSessionModeDescription(unittest.TestCase):
    def test_per_user(self):
        config = pywrkr.BenchmarkConfig(url="http://example.com/")
        self.assertEqual(pywrkr.describe_session_mode(config), "cookie jar per user")

    def test_fresh_per_iteration(self):
        config = pywrkr.BenchmarkConfig(
            url="http://example.com/", scenario=pywrkr.Scenario(session="fresh_per_iteration")
        )
        self.assertIn("cleared each iteration", pywrkr.describe_session_mode(config))

    def test_off(self):
        config = pywrkr.BenchmarkConfig(url="http://example.com/", session_cookies=False)
        self.assertIn("off", pywrkr.describe_session_mode(config))

    def test_summary_line_only_in_user_mode(self):
        from pywrkr.reporting import _print_console_results

        stats = pywrkr.WorkerStats()
        stats.total_requests = 1
        for users, expected in ((3, True), (None, False)):
            out = StringIO()
            _print_console_results(
                stats,
                1.0,
                1,
                0.0,
                pywrkr.BenchmarkConfig(url="http://example.com/", users=users),
                None,
                out,
            )
            self.assertEqual("Sessions:" in out.getvalue(), expected, f"users={users}")


# ---------------------------------------------------------------------------
# Integration: a server that issues real cookie sessions
# ---------------------------------------------------------------------------


class _SessionServerMixin:
    """An app that behaves like a cookie-session web application.

    ``/login`` mints a session id and sets it as a cookie; ``/me`` accepts only
    session ids it issued. Every request's inbound ``Cookie`` header is recorded
    so tests can assert on what pywrkr actually sent.
    """

    async def get_application(self):
        self.issued: list[str] = []
        self.cookie_headers: list[str | None] = []
        self.me_sessions: list[str | None] = []
        self.me_unauthenticated = 0
        self.scoped_hits: list[str | None] = []
        self.overlap_users = 0

        app = web.Application()
        app.router.add_post("/login", self.handle_login)
        app.router.add_get("/login", self.handle_login)
        app.router.add_post("/login-overlap", self.handle_login_overlap)
        app.router.add_get("/me", self.handle_me)
        app.router.add_get("/expiring", self.handle_expiring)
        app.router.add_get("/scoped/set", self.handle_scoped_set)
        app.router.add_get("/scoped/read", self.handle_scoped_read)
        app.router.add_get("/elsewhere", self.handle_elsewhere)
        app.router.add_get("/plain", self.handle_plain)
        return app

    def _record(self, request):
        self.cookie_headers.append(request.headers.get("Cookie"))

    async def handle_login(self, request):
        self._record(request)
        sid = f"sess-{len(self.issued)}"
        self.issued.append(sid)
        resp = web.json_response({"session": sid})
        resp.set_cookie("sid", sid, path="/")
        return resp

    async def handle_me(self, request):
        self._record(request)
        sid = request.cookies.get("sid")
        self.me_sessions.append(sid)
        if sid is None or sid not in self.issued:
            self.me_unauthenticated += 1
            return web.json_response({"error": "no session"}, status=401)
        return web.json_response({"session": sid})

    async def handle_login_overlap(self, request):
        """Log in, but hold the response until every user has also logged in.

        Left to themselves, virtual users hitting a loopback server run in
        lock-step: each finishes its whole iteration before the next one's login
        lands, so a single shared jar produces exactly the same observations as
        one jar per user. Forcing the logins to overlap removes that luck — a
        shared jar has to be clobbered here, a per-user jar cannot be.
        """
        resp = await self.handle_login(request)
        deadline = self.overlap_users
        for _ in range(500):
            if len(self.issued) >= deadline:
                break
            await asyncio.sleep(0.01)
        return resp

    async def handle_expiring(self, request):
        self._record(request)
        # max_age=0 tells the jar to drop the cookie immediately.
        resp = web.json_response({"ok": True})
        resp.set_cookie("ephemeral", "gone", max_age=0, path="/")
        return resp

    async def handle_scoped_set(self, request):
        self._record(request)
        resp = web.json_response({"ok": True})
        resp.set_cookie("scoped", "yes", path="/scoped")
        return resp

    async def handle_scoped_read(self, request):
        self.scoped_hits.append(request.cookies.get("scoped"))
        return web.json_response({"ok": True})

    async def handle_elsewhere(self, request):
        self._record(request)
        self.scoped_hits.append(request.cookies.get("scoped"))
        return web.json_response({"ok": True})

    async def handle_plain(self, request):
        self._record(request)
        return web.Response(text="ok")

    def _url(self):
        return f"http://127.0.0.1:{self.server.port}"

    async def _run_scenario(self, scenario_data, users=1, duration=1.0, **config_kwargs):
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
                **config_kwargs,
            )
            with patch("sys.stdout", new_callable=StringIO):
                stats, _ = await pywrkr.run_user_simulation(config)
            return stats
        finally:
            os.unlink(f.name)


_LOGIN_THEN_ME = {
    "name": "Cookie login",
    "steps": [
        {"name": "login", "method": "POST", "path": "/login"},
        {"name": "me", "path": "/me", "assert_status": 200},
    ],
}


class TestCookieSessionIntegration(_SessionServerMixin, AioHTTPTestCase):
    async def test_set_cookie_is_replayed_within_a_vu(self):
        stats = await self._run_scenario(_LOGIN_THEN_ME)
        self.assertGreater(len(self.me_sessions), 0)
        self.assertEqual(self.me_unauthenticated, 0)
        self.assertNotIn(401, stats.status_codes)
        # /me saw a session id, and it was always one /login had issued.
        self.assertTrue(all(sid in self.issued for sid in self.me_sessions))

    async def test_each_vu_authenticates_as_the_session_it_was_given(self):
        await self._run_scenario(_LOGIN_THEN_ME, users=4, duration=1.5)
        self.assertGreater(len(self.me_sessions), 4)
        self.assertEqual(self.me_unauthenticated, 0)
        self.assertEqual(len(self.issued), len(set(self.issued)))
        self.assertGreaterEqual(len(set(self.me_sessions)), 4)

    async def test_overlapping_logins_do_not_overwrite_each_other(self):
        # The AC's "N VUs, N distinct sessions" claim, made non-vacuous: the
        # server holds every login open until all of them have arrived, so the
        # logins genuinely overlap. One shared jar could then only remember the
        # last session id, and the users' /me calls would collide.
        users = 3
        self.overlap_users = users
        await self._run_scenario(
            {
                "name": "Overlapping logins",
                # Think time after the login parks every user at the same point,
                # so all three login responses are processed before any /me is
                # built. Without it the event loop runs each user straight from
                # its response into its next request and a shared jar would never
                # be caught.
                "think_time": 0.05,
                "steps": [
                    {"name": "login", "method": "POST", "path": "/login-overlap"},
                    {"name": "me", "path": "/me", "assert_status": 200},
                ],
            },
            users=users,
            duration=0.5,
        )
        # The barrier only holds the opening round; later iterations run freely,
        # so the assertion looks at exactly the requests that were forced to
        # overlap.
        first_round = self.me_sessions[:users]
        self.assertEqual(len(first_round), users)
        self.assertEqual(len(set(first_round)), users, f"sessions collided: {first_round}")
        self.assertEqual(self.me_unauthenticated, 0)

    async def test_one_jar_is_created_per_virtual_user(self):
        # The invariant itself, asserted directly: the behavioural tests above
        # depend on request interleaving, this one does not.
        jars: list[object] = []
        # Bound at import time, so it still refers to the real factory once the
        # module attribute below is patched.
        real_create = _create_cookie_jar

        def spy(config):
            jar = real_create(config)
            jars.append(jar)
            return jar

        with patch("pywrkr.workers._create_cookie_jar", spy):
            await self._run_scenario(_LOGIN_THEN_ME, users=5, duration=0.5)

        self.assertEqual(len(jars), 5)
        self.assertEqual(len({id(jar) for jar in jars}), 5)

    async def test_session_persists_across_iterations_by_default(self):
        # With persistent sessions the jar still holds the previous iteration's
        # sid when the next /login runs, so that request carries a Cookie header.
        await self._run_scenario(_LOGIN_THEN_ME, duration=1.0)
        login_headers = self.cookie_headers[::2]
        self.assertGreater(len(login_headers), 1)
        self.assertIsNone(login_headers[0])
        self.assertTrue(any(h and "sid=" in h for h in login_headers[1:]))

    async def test_fresh_per_iteration_clears_the_jar(self):
        scenario = dict(_LOGIN_THEN_ME, session="fresh_per_iteration")
        await self._run_scenario(scenario, duration=1.0)
        # Every iteration starts empty, so no /login ever carries a cookie...
        login_headers = self.cookie_headers[::2]
        self.assertGreater(len(login_headers), 1)
        self.assertTrue(all(h is None or "sid=" not in h for h in login_headers))
        # ...yet /me within the same iteration still authenticates.
        self.assertEqual(self.me_unauthenticated, 0)
        self.assertGreater(len(self.me_sessions), 1)

    async def test_no_session_cookies_drops_set_cookie(self):
        stats = await self._run_scenario(_LOGIN_THEN_ME, session_cookies=False)
        # Nothing is stored, so /me never presents a session and always 401s.
        self.assertGreater(len(self.me_sessions), 0)
        self.assertTrue(all(sid is None for sid in self.me_sessions))
        self.assertEqual(self.me_unauthenticated, len(self.me_sessions))
        self.assertGreater(stats.status_codes.get(401, 0), 0)
        self.assertTrue(all(h is None for h in self.cookie_headers))

    async def test_static_cookies_still_sent_with_the_jar_enabled(self):
        await self._run_scenario(
            _LOGIN_THEN_ME, duration=0.6, cookies=["static=1", "flavour=vanilla"]
        )
        self.assertTrue(self.cookie_headers)
        for header in self.cookie_headers:
            self.assertIsNotNone(header)
            self.assertIn("static=1", header)
            self.assertIn("flavour=vanilla", header)
        # The server-set cookie rides along with them.
        self.assertTrue(any("sid=" in h for h in self.cookie_headers))

    async def test_static_cookies_survive_fresh_per_iteration(self):
        # Static -C cookies live in the request header, not the jar, so clearing
        # the jar between iterations must not drop them.
        scenario = dict(_LOGIN_THEN_ME, session="fresh_per_iteration")
        await self._run_scenario(scenario, duration=0.6, cookies=["static=1"])
        self.assertTrue(self.cookie_headers)
        self.assertTrue(all(h and "static=1" in h for h in self.cookie_headers))

    async def test_static_cookies_still_sent_with_sessions_off(self):
        await self._run_scenario(
            _LOGIN_THEN_ME, duration=0.6, session_cookies=False, cookies=["static=1"]
        )
        self.assertTrue(self.cookie_headers)
        self.assertTrue(all(h == "static=1" for h in self.cookie_headers))

    async def test_expired_cookie_is_dropped(self):
        await self._run_scenario(
            {
                "name": "Expiry",
                "steps": [
                    {"name": "expiring", "path": "/expiring"},
                    {"name": "plain", "path": "/plain"},
                ],
            },
            duration=0.6,
        )
        self.assertTrue(self.cookie_headers)
        self.assertTrue(all(h is None or "ephemeral" not in h for h in self.cookie_headers))

    async def test_path_scoped_cookie_is_not_sent_elsewhere(self):
        await self._run_scenario(
            {
                "name": "Path scope",
                "steps": [
                    {"name": "set", "path": "/scoped/set"},
                    {"name": "read", "path": "/scoped/read"},
                    {"name": "elsewhere", "path": "/elsewhere"},
                ],
            },
            duration=0.8,
        )
        # /scoped/read sees the cookie; /elsewhere (outside Path=/scoped) does not.
        in_scope = self.scoped_hits[::2]
        out_of_scope = self.scoped_hits[1::2]
        self.assertTrue(in_scope)
        self.assertTrue(all(v == "yes" for v in in_scope))
        self.assertTrue(out_of_scope)
        self.assertTrue(all(v is None for v in out_of_scope))


class TestUserModeCookieSessions(_SessionServerMixin, AioHTTPTestCase):
    """Plain -u mode (no scenario) also gets a jar per virtual user."""

    async def _run_users(self, users=3, duration=1.0, **config_kwargs):
        config = pywrkr.BenchmarkConfig(
            url=f"{self._url()}/login",
            users=users,
            duration=duration,
            think_time=0.0,
            ramp_up=0.0,
            timeout_sec=5,
            **config_kwargs,
        )
        with patch("sys.stdout", new_callable=StringIO):
            stats, _ = await pywrkr.run_user_simulation(config)
        return stats

    async def test_repeat_requests_carry_the_session_forward(self):
        await self._run_users()
        # First request per VU has no cookie; later ones replay the sid the
        # server set, so at least one request carries one.
        self.assertGreater(len(self.cookie_headers), 3)
        self.assertTrue(any(h and "sid=" in h for h in self.cookie_headers))

    async def test_no_session_cookies_keeps_every_request_anonymous(self):
        await self._run_users(session_cookies=False)
        self.assertGreater(len(self.cookie_headers), 3)
        self.assertTrue(all(h is None for h in self.cookie_headers))


class TestPlainModeUntouched(_SessionServerMixin, AioHTTPTestCase):
    """Plain -c/-d mode keeps aiohttp's default jar unless opted out."""

    async def _run_plain(self, **config_kwargs):
        config = pywrkr.BenchmarkConfig(
            url=f"{self._url()}/login",
            connections=2,
            duration=0.5,
            threads=1,
            timeout_sec=5,
            **config_kwargs,
        )
        with patch("sys.stdout", new_callable=StringIO):
            stats, _ = await pywrkr.run_benchmark(config)
        return stats

    async def test_default_jar_is_left_in_place(self):
        # The default jar refuses cookies for IP hosts, so an IP target behaves
        # exactly as it did before per-VU sessions existed: no cookies replayed.
        await self._run_plain()
        self.assertTrue(self.cookie_headers)
        self.assertTrue(all(h is None for h in self.cookie_headers))

    async def test_opt_out_also_applies(self):
        await self._run_plain(session_cookies=False)
        self.assertTrue(self.cookie_headers)
        self.assertTrue(all(h is None for h in self.cookie_headers))

    async def test_static_cookies_unaffected(self):
        await self._run_plain(cookies=["static=1"])
        self.assertTrue(self.cookie_headers)
        self.assertTrue(all(h == "static=1" for h in self.cookie_headers))


if __name__ == "__main__":
    unittest.main()
