#!/usr/bin/env python3
"""Unit tests for pywrkr HAR import feature."""

import json
import os
import tempfile
import unittest

from pywrkr.har_import import (
    HarEntry,
    HarImportConfig,
    _compute_think_times,
    convert_har,
    filter_entries,
    har_to_scenario,
    har_to_url_file,
    parse_har,
)


def _make_har(entries):
    """Build a minimal HAR dict from a list of entry dicts."""
    return {"log": {"version": "1.2", "entries": entries}}


def _make_entry(
    url="https://api.example.com/test",
    method="GET",
    status=200,
    time_ms=100.0,
    headers=None,
    post_data=None,
    started_datetime=None,
):
    """Build a single HAR entry dict."""
    entry = {
        "time": time_ms,
        "request": {
            "method": method,
            "url": url,
            "headers": headers or [],
        },
        "response": {
            "status": status,
            "headers": [],
            "content": {"size": 100, "mimeType": "application/json"},
        },
    }
    if started_datetime:
        entry["startedDateTime"] = started_datetime
    if post_data:
        entry["request"]["postData"] = post_data
    return entry


def _write_har(har_dict):
    """Write a HAR dict to a temp file and return its path."""
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".har", delete=False, encoding="utf-8")
    json.dump(har_dict, f)
    f.close()
    return f.name


class TestParseHar(unittest.TestCase):
    """Tests for parse_har()."""

    def test_parse_basic_har(self):
        har = _make_har(
            [
                _make_entry(url="https://example.com/a", method="GET", status=200, time_ms=50),
                _make_entry(
                    url="https://example.com/b",
                    method="POST",
                    status=201,
                    time_ms=100,
                    post_data={"mimeType": "application/json", "text": '{"key": "val"}'},
                ),
            ]
        )
        path = _write_har(har)
        try:
            entries = parse_har(path)
            self.assertEqual(len(entries), 2)
            self.assertEqual(entries[0].url, "https://example.com/a")
            self.assertEqual(entries[0].method, "GET")
            self.assertEqual(entries[0].status, 200)
            self.assertEqual(entries[1].method, "POST")
            self.assertEqual(entries[1].body, '{"key": "val"}')
            self.assertEqual(entries[1].content_type, "application/json")
        finally:
            os.unlink(path)

    def test_parse_file_not_found(self):
        with self.assertRaises(FileNotFoundError):
            parse_har("/nonexistent/file.har")

    def test_parse_invalid_json(self):
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".har", delete=False)
        f.write("not json {{{")
        f.close()
        try:
            with self.assertRaises(ValueError):
                parse_har(f.name)
        finally:
            os.unlink(f.name)

    def test_parse_missing_log_key(self):
        path = _write_har({"not_log": {}})
        try:
            with self.assertRaises(ValueError):
                parse_har(path)
        finally:
            os.unlink(path)

    def test_parse_missing_entries(self):
        path = _write_har({"log": {"version": "1.2"}})
        try:
            with self.assertRaises(ValueError):
                parse_har(path)
        finally:
            os.unlink(path)

    def test_parse_empty_entries(self):
        path = _write_har(_make_har([]))
        try:
            entries = parse_har(path)
            self.assertEqual(len(entries), 0)
        finally:
            os.unlink(path)

    def test_parse_entry_without_url_skipped(self):
        har = _make_har([{"request": {"method": "GET", "url": ""}, "response": {}, "time": 0}])
        path = _write_har(har)
        try:
            entries = parse_har(path)
            self.assertEqual(len(entries), 0)
        finally:
            os.unlink(path)

    def test_parse_extracts_started_datetime(self):
        har = _make_har(
            [
                _make_entry(
                    url="https://example.com/a", started_datetime="2025-01-15T10:00:00.000Z"
                ),
            ]
        )
        path = _write_har(har)
        try:
            entries = parse_har(path)
            self.assertEqual(entries[0].started_datetime, "2025-01-15T10:00:00.000Z")
        finally:
            os.unlink(path)

    def test_parse_extracts_headers(self):
        har = _make_har(
            [
                _make_entry(
                    headers=[
                        {"name": "Authorization", "value": "Bearer token123"},
                        {"name": "Content-Type", "value": "application/json"},
                    ],
                ),
            ]
        )
        path = _write_har(har)
        try:
            entries = parse_har(path)
            self.assertEqual(entries[0].headers["authorization"], "Bearer token123")
            self.assertEqual(entries[0].headers["content-type"], "application/json")
        finally:
            os.unlink(path)


class TestFilterEntries(unittest.TestCase):
    """Tests for filter_entries()."""

    def _entries(self):
        return [
            HarEntry(url="https://api.example.com/users", method="GET", status=200, time_ms=100),
            HarEntry(url="https://api.example.com/orders", method="POST", status=201, time_ms=150),
            HarEntry(url="https://cdn.example.com/style.css", method="GET", status=200, time_ms=50),
            HarEntry(url="https://cdn.example.com/logo.png", method="GET", status=200, time_ms=30),
            HarEntry(
                url="https://analytics.tracker.com/pixel", method="GET", status=200, time_ms=20
            ),
        ]

    def test_default_filters_static(self):
        config = HarImportConfig()
        result = filter_entries(self._entries(), config)
        urls = [e.url for e in result]
        self.assertIn("https://api.example.com/users", urls)
        self.assertIn("https://api.example.com/orders", urls)
        self.assertIn("https://analytics.tracker.com/pixel", urls)
        self.assertNotIn("https://cdn.example.com/style.css", urls)
        self.assertNotIn("https://cdn.example.com/logo.png", urls)

    def test_include_static(self):
        config = HarImportConfig(include_static=True)
        result = filter_entries(self._entries(), config)
        self.assertEqual(len(result), 5)

    def test_domain_filter(self):
        config = HarImportConfig(allowed_domains=["api.example.com"])
        result = filter_entries(self._entries(), config)
        self.assertEqual(len(result), 2)
        from urllib.parse import urlparse

        self.assertTrue(all(urlparse(e.url).hostname == "api.example.com" for e in result))

    def test_exclude_pattern(self):
        config = HarImportConfig(exclude_patterns=[r"/orders"])
        result = filter_entries(self._entries(), config)
        self.assertFalse(any("orders" in e.url for e in result))

    def test_include_pattern(self):
        config = HarImportConfig(include_patterns=[r"/users"])
        result = filter_entries(self._entries(), config)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].url, "https://api.example.com/users")

    def test_combined_filters(self):
        config = HarImportConfig(
            allowed_domains=["api.example.com"],
            exclude_patterns=[r"/orders"],
        )
        result = filter_entries(self._entries(), config)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].url, "https://api.example.com/users")


class TestHarToScenario(unittest.TestCase):
    """Tests for har_to_scenario()."""

    def test_basic_scenario(self):
        entries = [
            HarEntry(url="https://api.example.com/users", method="GET", time_ms=100),
            HarEntry(url="https://api.example.com/users/1", method="GET", time_ms=80),
        ]
        config = HarImportConfig()
        scenario = har_to_scenario(entries, config)
        self.assertEqual(scenario["name"], "HAR Import")
        self.assertEqual(len(scenario["steps"]), 2)
        self.assertEqual(scenario["steps"][0]["path"], "/users")
        self.assertEqual(scenario["steps"][0]["method"], "GET")
        self.assertEqual(scenario["steps"][1]["path"], "/users/1")

    def test_post_with_body(self):
        entries = [
            HarEntry(
                url="https://api.example.com/users",
                method="POST",
                body='{"name": "Alice"}',
                content_type="application/json",
                time_ms=100,
            ),
        ]
        config = HarImportConfig()
        scenario = har_to_scenario(entries, config)
        step = scenario["steps"][0]
        self.assertEqual(step["method"], "POST")
        self.assertEqual(step["body"], {"name": "Alice"})
        self.assertEqual(step["headers"]["Content-Type"], "application/json")

    def test_non_json_body(self):
        entries = [
            HarEntry(
                url="https://api.example.com/upload",
                method="POST",
                body="plain text body",
                content_type="text/plain",
                time_ms=100,
            ),
        ]
        config = HarImportConfig()
        scenario = har_to_scenario(entries, config)
        self.assertEqual(scenario["steps"][0]["body"], "plain text body")

    def test_assert_status(self):
        entries = [
            HarEntry(url="https://api.example.com/users", method="GET", status=200, time_ms=100),
            HarEntry(url="https://api.example.com/fail", method="GET", status=500, time_ms=50),
        ]
        config = HarImportConfig(assert_status=True)
        scenario = har_to_scenario(entries, config)
        self.assertEqual(scenario["steps"][0]["assert_status"], 200)
        # 500 status should not get assertion (only 2xx/3xx)
        self.assertNotIn("assert_status", scenario["steps"][1])

    def test_think_times_with_timestamps(self):
        # Entry 1 starts at T=0, takes 200ms, Entry 2 starts at T=500ms
        # Think time = 500ms - (0ms + 200ms) = 300ms = 0.3s
        entries = [
            HarEntry(
                url="https://api.example.com/a",
                method="GET",
                time_ms=200,
                started_datetime="2025-01-15T10:00:00.000Z",
            ),
            HarEntry(
                url="https://api.example.com/b",
                method="GET",
                time_ms=100,
                started_datetime="2025-01-15T10:00:00.500Z",
            ),
        ]
        config = HarImportConfig()
        scenario = har_to_scenario(entries, config)
        self.assertNotIn("think_time", scenario["steps"][0])
        self.assertEqual(scenario["steps"][1]["think_time"], 0.3)

    def test_think_times_fallback_without_timestamps(self):
        # Without timestamps, falls back to using previous entry duration
        entries = [
            HarEntry(url="https://api.example.com/a", method="GET", time_ms=200),
            HarEntry(url="https://api.example.com/b", method="GET", time_ms=100),
        ]
        config = HarImportConfig()
        scenario = har_to_scenario(entries, config)
        self.assertNotIn("think_time", scenario["steps"][0])
        # Fallback: uses previous entry duration (200ms = 0.2s)
        self.assertEqual(scenario["steps"][1]["think_time"], 0.2)

    def test_no_think_time(self):
        entries = [
            HarEntry(url="https://api.example.com/a", method="GET", time_ms=200),
            HarEntry(url="https://api.example.com/b", method="GET", time_ms=100),
        ]
        config = HarImportConfig(add_think_time=False)
        scenario = har_to_scenario(entries, config)
        self.assertNotIn("think_time", scenario["steps"][0])
        self.assertNotIn("think_time", scenario["steps"][1])

    def test_think_time_multiplier(self):
        # With timestamps: gap = 2000ms - (0 + 1000ms) = 1000ms, * 0.5 = 500ms
        entries = [
            HarEntry(
                url="https://api.example.com/a",
                method="GET",
                time_ms=1000,
                started_datetime="2025-01-15T10:00:00.000Z",
            ),
            HarEntry(
                url="https://api.example.com/b",
                method="GET",
                time_ms=100,
                started_datetime="2025-01-15T10:00:02.000Z",
            ),
        ]
        config = HarImportConfig(think_time_multiplier=0.5)
        scenario = har_to_scenario(entries, config)
        self.assertEqual(scenario["steps"][1]["think_time"], 0.5)

    def test_think_time_multiplier_fallback(self):
        # Without timestamps: fallback uses duration 1000ms * 0.5 = 0.5s
        entries = [
            HarEntry(url="https://api.example.com/a", method="GET", time_ms=1000),
            HarEntry(url="https://api.example.com/b", method="GET", time_ms=100),
        ]
        config = HarImportConfig(think_time_multiplier=0.5)
        scenario = har_to_scenario(entries, config)
        self.assertEqual(scenario["steps"][1]["think_time"], 0.5)

    def test_empty_entries_raises(self):
        config = HarImportConfig()
        with self.assertRaises(ValueError):
            har_to_scenario([], config)

    def test_query_string_preserved(self):
        entries = [
            HarEntry(url="https://api.example.com/search?q=test&page=1", method="GET", time_ms=100),
        ]
        config = HarImportConfig()
        scenario = har_to_scenario(entries, config)
        self.assertEqual(scenario["steps"][0]["path"], "/search?q=test&page=1")

    def test_custom_name(self):
        entries = [HarEntry(url="https://example.com/", method="GET", time_ms=50)]
        config = HarImportConfig()
        scenario = har_to_scenario(entries, config, name="My Test")
        self.assertEqual(scenario["name"], "My Test")

    def test_preserve_headers(self):
        entries = [
            HarEntry(
                url="https://api.example.com/",
                method="GET",
                headers={
                    "authorization": "Bearer abc",
                    "x-custom": "value",
                    "user-agent": "Mozilla/5.0",
                    "host": "api.example.com",
                },
                time_ms=100,
            ),
        ]
        config = HarImportConfig(preserve_headers=True)
        scenario = har_to_scenario(entries, config)
        h = scenario["steps"][0]["headers"]
        self.assertEqual(h["authorization"], "Bearer abc")
        self.assertEqual(h["x-custom"], "value")
        # Skipped headers should not be present
        self.assertNotIn("user-agent", h)
        self.assertNotIn("host", h)


class TestComputeThinkTimes(unittest.TestCase):
    """Tests for _compute_think_times() with timestamp-based calculation."""

    def test_single_entry(self):
        entries = [
            HarEntry(url="https://a.com/", time_ms=100, started_datetime="2025-01-15T10:00:00.000Z")
        ]
        result = _compute_think_times(entries, 1.0)
        self.assertEqual(result, [0.0])

    def test_empty(self):
        self.assertEqual(_compute_think_times([], 1.0), [])

    def test_overlapping_requests_clamps_to_zero(self):
        # Entry 2 starts before entry 1 finishes -> think time should be 0
        entries = [
            HarEntry(
                url="https://a.com/1", time_ms=500, started_datetime="2025-01-15T10:00:00.000Z"
            ),
            HarEntry(
                url="https://a.com/2", time_ms=100, started_datetime="2025-01-15T10:00:00.200Z"
            ),
        ]
        result = _compute_think_times(entries, 1.0)
        self.assertEqual(result[0], 0.0)
        self.assertEqual(result[1], 0.0)  # clamped: 200ms - 500ms < 0

    def test_correct_gap_calculation(self):
        # Entry 1: starts T=0, duration=100ms -> ends T=100ms
        # Entry 2: starts T=300ms -> gap = 200ms
        # Entry 3: starts T=500ms, entry 2 duration=50ms -> ends T=350ms -> gap = 150ms
        entries = [
            HarEntry(
                url="https://a.com/1", time_ms=100, started_datetime="2025-01-15T10:00:00.000Z"
            ),
            HarEntry(
                url="https://a.com/2", time_ms=50, started_datetime="2025-01-15T10:00:00.300Z"
            ),
            HarEntry(
                url="https://a.com/3", time_ms=80, started_datetime="2025-01-15T10:00:00.500Z"
            ),
        ]
        result = _compute_think_times(entries, 1.0)
        self.assertEqual(result[0], 0.0)
        self.assertEqual(result[1], 0.2)
        self.assertEqual(result[2], 0.15)

    def test_think_time_capped_at_30s(self):
        entries = [
            HarEntry(
                url="https://a.com/1", time_ms=100, started_datetime="2025-01-15T10:00:00.000Z"
            ),
            HarEntry(
                url="https://a.com/2", time_ms=100, started_datetime="2025-01-15T10:01:00.000Z"
            ),  # 60s gap
        ]
        result = _compute_think_times(entries, 1.0)
        self.assertEqual(result[1], 30.0)

    def test_multiplier_applied(self):
        entries = [
            HarEntry(
                url="https://a.com/1", time_ms=100, started_datetime="2025-01-15T10:00:00.000Z"
            ),
            HarEntry(
                url="https://a.com/2", time_ms=100, started_datetime="2025-01-15T10:00:01.100Z"
            ),  # gap = 1000ms
        ]
        result = _compute_think_times(entries, 0.5)
        self.assertEqual(result[1], 0.5)  # 1.0s * 0.5 = 0.5s


class TestHarToUrlFile(unittest.TestCase):
    """Tests for har_to_url_file()."""

    def test_basic_url_file(self):
        entries = [
            HarEntry(url="https://example.com/a", method="GET"),
            HarEntry(url="https://example.com/b", method="POST"),
        ]
        result = har_to_url_file(entries)
        lines = result.strip().split("\n")
        self.assertEqual(lines[0], "https://example.com/a")
        self.assertEqual(lines[1], "POST https://example.com/b")

    def test_deduplication(self):
        entries = [
            HarEntry(url="https://example.com/a", method="GET"),
            HarEntry(url="https://example.com/a", method="GET"),
            HarEntry(url="https://example.com/b", method="GET"),
        ]
        result = har_to_url_file(entries)
        lines = result.strip().split("\n")
        self.assertEqual(len(lines), 2)

    def test_empty_raises(self):
        with self.assertRaises(ValueError):
            har_to_url_file([])


class TestConvertHar(unittest.TestCase):
    """Tests for the high-level convert_har() function."""

    def setUp(self):
        self.har = _make_har(
            [
                _make_entry(url="https://api.example.com/users", method="GET", time_ms=100),
                _make_entry(url="https://api.example.com/users/1", method="GET", time_ms=80),
                _make_entry(url="https://cdn.example.com/style.css", method="GET", time_ms=50),
            ]
        )
        self.path = _write_har(self.har)

    def tearDown(self):
        os.unlink(self.path)

    def test_convert_to_scenario(self):
        content = convert_har(self.path, output_format="scenario")
        data = json.loads(content)
        self.assertIn("steps", data)
        self.assertEqual(len(data["steps"]), 2)  # CSS filtered out

    def test_convert_to_url_file(self):
        content = convert_har(self.path, output_format="url-file")
        lines = content.strip().split("\n")
        self.assertEqual(len(lines), 2)

    def test_convert_with_output_file(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            out_path = f.name
        try:
            convert_har(self.path, output_path=out_path, output_format="scenario")
            with open(out_path) as f:
                data = json.loads(f.read())
            self.assertIn("steps", data)
        finally:
            os.unlink(out_path)

    def test_convert_include_static(self):
        config = HarImportConfig(include_static=True)
        content = convert_har(self.path, output_format="scenario", config=config)
        data = json.loads(content)
        self.assertEqual(len(data["steps"]), 3)

    def test_convert_all_filtered_out(self):
        config = HarImportConfig(allowed_domains=["nonexistent.example.com"])
        with self.assertRaises(ValueError) as ctx:
            convert_har(self.path, config=config)
        self.assertIn("No requests remained", str(ctx.exception))

    def test_convert_unknown_format(self):
        with self.assertRaises(ValueError):
            convert_har(self.path, output_format="xml")

    def test_convert_custom_name(self):
        content = convert_har(self.path, output_format="scenario", name="Custom Name")
        data = json.loads(content)
        self.assertEqual(data["name"], "Custom Name")


class TestHarImportCLI(unittest.TestCase):
    """Tests for the har-import CLI subcommand."""

    def test_cli_har_import_to_stdout(self):
        from pywrkr.main import _build_har_import_parser

        parser = _build_har_import_parser()
        har_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "examples",
            "sample-recording.har",
        )
        args = parser.parse_args([har_path])
        self.assertEqual(args.har_file, har_path)
        self.assertEqual(args.format, "scenario")
        self.assertIsNone(args.output)

    def test_cli_har_import_with_options(self):
        from pywrkr.main import _build_har_import_parser

        parser = _build_har_import_parser()
        args = parser.parse_args(
            [
                "test.har",
                "-o",
                "output.json",
                "--format",
                "url-file",
                "--include-static",
                "--domain",
                "api.example.com",
                "--domain",
                "web.example.com",
                "--exclude",
                r"/analytics",
                "--preserve-headers",
                "--no-think-time",
                "--think-time-multiplier",
                "0.5",
                "--assert-status",
                "--name",
                "My Test",
            ]
        )
        self.assertEqual(args.format, "url-file")
        self.assertEqual(args.output, "output.json")
        self.assertTrue(args.include_static)
        self.assertEqual(args.domains, ["api.example.com", "web.example.com"])
        self.assertEqual(args.exclude_patterns, [r"/analytics"])
        self.assertTrue(args.preserve_headers)
        self.assertTrue(args.no_think_time)
        self.assertEqual(args.think_time_multiplier, 0.5)
        self.assertTrue(args.assert_status)
        self.assertEqual(args.name, "My Test")


class TestSampleHarFile(unittest.TestCase):
    """Test against the example HAR file shipped with the project."""

    def _sample_har_path(self):
        return os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "examples",
            "sample-recording.har",
        )

    def test_parse_sample_har(self):
        entries = parse_har(self._sample_har_path())
        self.assertEqual(len(entries), 7)
        methods = [e.method for e in entries]
        self.assertIn("GET", methods)
        self.assertIn("POST", methods)
        self.assertIn("PUT", methods)
        self.assertIn("DELETE", methods)

    def test_default_filter_excludes_static(self):
        entries = parse_har(self._sample_har_path())
        filtered = filter_entries(entries, HarImportConfig())
        # CSS and PNG should be filtered out
        self.assertEqual(len(filtered), 5)

    def test_scenario_from_sample(self):
        content = convert_har(self._sample_har_path(), output_format="scenario")
        data = json.loads(content)
        self.assertEqual(data["name"], "sample-recording")
        self.assertEqual(len(data["steps"]), 5)
        # POST step should have body
        post_step = [s for s in data["steps"] if s["method"] == "POST"][0]
        self.assertIn("body", post_step)
        self.assertEqual(post_step["body"]["name"], "Alice")

    def test_url_file_from_sample(self):
        content = convert_har(self._sample_har_path(), output_format="url-file")
        lines = content.strip().split("\n")
        self.assertEqual(len(lines), 5)
        # GET requests don't have method prefix
        self.assertTrue(lines[0].startswith("https://"))
        # POST has method prefix
        self.assertTrue(any(line.startswith("POST ") for line in lines))


if __name__ == "__main__":
    unittest.main()
