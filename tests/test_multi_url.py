"""Tests for multi_url.py: URL file parsing and UrlEntry dataclass."""

import os
import tempfile
import unittest

from pywrkr.multi_url import UrlEntry, load_url_file


class TestLoadUrlFile(unittest.TestCase):
    """Tests for load_url_file."""

    def _write_temp(self, content: str) -> str:
        """Write content to a temp file and return the path."""
        fd, path = tempfile.mkstemp(suffix=".txt")
        with os.fdopen(fd, "w") as f:
            f.write(content)
        self.addCleanup(os.unlink, path)
        return path

    def test_simple_urls(self):
        path = self._write_temp("http://example.com/a\nhttp://example.com/b\n")
        entries = load_url_file(path)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0].url, "http://example.com/a")
        self.assertEqual(entries[0].method, "GET")

    def test_method_prefix(self):
        path = self._write_temp("POST http://example.com/api\nDELETE http://example.com/item\n")
        entries = load_url_file(path)
        self.assertEqual(entries[0].method, "POST")
        self.assertEqual(entries[0].url, "http://example.com/api")
        self.assertEqual(entries[1].method, "DELETE")

    def test_comments_and_blanks_ignored(self):
        path = self._write_temp("# comment\n\nhttp://example.com\n\n# another\n")
        entries = load_url_file(path)
        self.assertEqual(len(entries), 1)

    def test_all_methods(self):
        methods = ["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"]
        lines = [f"{m} http://example.com/{m.lower()}" for m in methods]
        path = self._write_temp("\n".join(lines))
        entries = load_url_file(path)
        self.assertEqual(len(entries), len(methods))
        for entry, method in zip(entries, methods):
            self.assertEqual(entry.method, method)

    def test_case_insensitive_method(self):
        path = self._write_temp("post http://example.com/api\n")
        entries = load_url_file(path)
        self.assertEqual(entries[0].method, "POST")

    def test_file_not_found(self):
        with self.assertRaises(FileNotFoundError):
            load_url_file("/nonexistent/path/urls.txt")

    def test_empty_file(self):
        path = self._write_temp("# only comments\n\n")
        with self.assertRaises(ValueError, msg="URL file is empty"):
            load_url_file(path)

    def test_url_without_method_is_get(self):
        path = self._write_temp("http://example.com/test\n")
        entries = load_url_file(path)
        self.assertEqual(entries[0].method, "GET")


class TestUrlEntry(unittest.TestCase):
    """Tests for UrlEntry dataclass."""

    def test_default_method(self):
        entry = UrlEntry(url="http://example.com")
        self.assertEqual(entry.method, "GET")

    def test_custom_method(self):
        entry = UrlEntry(url="http://example.com", method="POST")
        self.assertEqual(entry.method, "POST")


if __name__ == "__main__":
    unittest.main()
