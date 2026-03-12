"""HAR (HTTP Archive) import for pywrkr.

Converts HAR files (recorded browser traffic) into pywrkr scenario files
or URL lists, dramatically reducing test-authoring time.

HAR spec: http://www.softwareishard.com/blog/har-12-spec/
"""

import json
import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse


@dataclass
class HarEntry:
    """A single request extracted from a HAR file."""

    url: str
    method: str = "GET"
    headers: dict[str, str] = field(default_factory=dict)
    body: str | None = None
    content_type: str | None = None
    status: int = 0
    time_ms: float = 0.0


@dataclass
class HarImportConfig:
    """Options controlling HAR-to-pywrkr conversion."""

    include_static: bool = False
    exclude_patterns: list[str] = field(default_factory=list)
    include_patterns: list[str] = field(default_factory=list)
    allowed_domains: list[str] = field(default_factory=list)
    preserve_headers: bool = False
    skip_headers: list[str] = field(
        default_factory=lambda: [
            "accept-encoding",
            "connection",
            "host",
            "user-agent",
            "content-length",
            "sec-ch-ua",
            "sec-ch-ua-mobile",
            "sec-ch-ua-platform",
            "sec-fetch-dest",
            "sec-fetch-mode",
            "sec-fetch-site",
            "sec-fetch-user",
            "upgrade-insecure-requests",
            "referer",
            "origin",
            "cookie",
        ]
    )
    add_think_time: bool = True
    think_time_multiplier: float = 1.0
    assert_status: bool = False


# File extensions considered "static" (images, fonts, stylesheets, scripts)
_STATIC_EXTENSIONS = frozenset(
    {
        ".css",
        ".js",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".svg",
        ".ico",
        ".woff",
        ".woff2",
        ".ttf",
        ".eot",
        ".otf",
        ".map",
        ".webp",
        ".avif",
        ".mp4",
        ".webm",
        ".mp3",
        ".ogg",
    }
)


def _is_static(url: str) -> bool:
    """Return True if the URL looks like a static asset."""
    path = urlparse(url).path.lower()
    _, ext = os.path.splitext(path)
    return ext in _STATIC_EXTENSIONS


def _matches_patterns(url: str, patterns: list[str]) -> bool:
    """Return True if the URL matches any of the regex patterns."""
    for pattern in patterns:
        if re.search(pattern, url):
            return True
    return False


def parse_har(path: str) -> list[HarEntry]:
    """Parse a HAR file and return a list of HarEntry objects.

    Raises FileNotFoundError if the file doesn't exist, ValueError if the
    file is not valid HAR JSON.
    """
    if not os.path.isfile(path):
        raise FileNotFoundError(f"HAR file not found: {path}")

    with open(path, "r", encoding="utf-8") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON in HAR file: {e}")

    if not isinstance(data, dict) or "log" not in data:
        raise ValueError("Invalid HAR file: missing 'log' key")

    log = data["log"]
    if not isinstance(log, dict) or "entries" not in log:
        raise ValueError("Invalid HAR file: missing 'log.entries' key")

    entries = []
    for entry in log["entries"]:
        request = entry.get("request", {})
        response = entry.get("response", {})

        url = request.get("url", "")
        if not url:
            continue

        method = request.get("method", "GET").upper()

        # Extract headers
        headers = {}
        for h in request.get("headers", []):
            name = h.get("name", "")
            value = h.get("value", "")
            if name:
                headers[name.lower()] = value

        # Extract body
        body = None
        content_type = None
        post_data = request.get("postData")
        if post_data:
            body = post_data.get("text", "")
            content_type = post_data.get("mimeType", "")

        # Response status
        status = response.get("status", 0)

        # Timing
        time_ms = entry.get("time", 0.0)

        entries.append(
            HarEntry(
                url=url,
                method=method,
                headers=headers,
                body=body if body else None,
                content_type=content_type,
                status=status,
                time_ms=time_ms,
            )
        )

    return entries


def filter_entries(
    entries: list[HarEntry],
    config: HarImportConfig,
) -> list[HarEntry]:
    """Apply filters to HAR entries based on HarImportConfig."""
    result = []
    for entry in entries:
        # Filter static assets
        if not config.include_static and _is_static(entry.url):
            continue

        # Domain filtering
        if config.allowed_domains:
            domain = urlparse(entry.url).hostname or ""
            if not any(domain == d or domain.endswith("." + d) for d in config.allowed_domains):
                continue

        # Exclude patterns
        if config.exclude_patterns and _matches_patterns(entry.url, config.exclude_patterns):
            continue

        # Include patterns (if specified, only matching URLs pass)
        if config.include_patterns and not _matches_patterns(entry.url, config.include_patterns):
            continue

        result.append(entry)

    return result


def _build_step_headers(
    entry: HarEntry,
    config: HarImportConfig,
) -> dict[str, str]:
    """Build the headers dict for a scenario step."""
    if not config.preserve_headers:
        # Only keep content-type for requests with a body
        if entry.content_type and entry.body:
            return {"Content-Type": entry.content_type}
        return {}

    skip = set(config.skip_headers)
    return {name: value for name, value in entry.headers.items() if name.lower() not in skip}


def _compute_think_times(
    entries: list[HarEntry],
    multiplier: float,
) -> list[float]:
    """Compute inter-request think times from HAR timing data.

    Returns a list of think times (one per entry). The first entry has 0.
    Think times are derived from the gap between consecutive entries.
    """
    if len(entries) <= 1:
        return [0.0] * len(entries)

    think_times = [0.0]
    # Use cumulative time to estimate gaps
    for i in range(1, len(entries)):
        prev_time = entries[i - 1].time_ms
        gap = max(0.0, prev_time)  # milliseconds
        think_sec = (gap / 1000.0) * multiplier
        # Cap think time to something reasonable (max 30s)
        think_sec = min(think_sec, 30.0)
        # Round to 2 decimal places
        think_sec = round(think_sec, 2)
        think_times.append(think_sec)

    return think_times


def har_to_scenario(
    entries: list[HarEntry],
    config: HarImportConfig,
    name: str = "HAR Import",
) -> dict:
    """Convert filtered HAR entries to a pywrkr scenario dict.

    The returned dict can be serialized to JSON/YAML and used with
    ``pywrkr --scenario``.
    """
    if not entries:
        raise ValueError("No HAR entries to convert (all filtered out?)")

    # Compute think times from recorded timing
    think_times = (
        _compute_think_times(entries, config.think_time_multiplier)
        if config.add_think_time
        else [0.0] * len(entries)
    )

    steps = []
    for i, entry in enumerate(entries):
        parsed = urlparse(entry.url)
        # Use path + query as the step path (scenario is relative to base URL)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query

        step: dict = {
            "name": f"{entry.method} {path}",
            "path": path,
            "method": entry.method,
        }

        # Headers
        headers = _build_step_headers(entry, config)
        if headers:
            step["headers"] = headers

        # Body
        if entry.body:
            # Try to parse as JSON for cleaner output
            try:
                step["body"] = json.loads(entry.body)
            except (json.JSONDecodeError, TypeError):
                step["body"] = entry.body

        # Status assertion
        if config.assert_status and entry.status and 200 <= entry.status < 400:
            step["assert_status"] = entry.status

        # Think time
        if think_times[i] > 0:
            step["think_time"] = think_times[i]

        steps.append(step)

    return {
        "name": name,
        "think_time": 0.0,
        "steps": steps,
    }


def har_to_url_file(
    entries: list[HarEntry],
) -> str:
    """Convert filtered HAR entries to pywrkr URL-file format.

    Returns a string with one ``METHOD URL`` per line (suitable for
    ``pywrkr --url-file``). Only unique URLs are included.
    """
    if not entries:
        raise ValueError("No HAR entries to convert (all filtered out?)")

    seen = set()
    lines = []
    for entry in entries:
        key = (entry.method, entry.url)
        if key in seen:
            continue
        seen.add(key)
        if entry.method == "GET":
            lines.append(entry.url)
        else:
            lines.append(f"{entry.method} {entry.url}")

    return "\n".join(lines) + "\n"


def convert_har(
    har_path: str,
    output_path: str | None = None,
    output_format: str = "scenario",
    config: HarImportConfig | None = None,
    name: str | None = None,
) -> str:
    """High-level HAR conversion: parse, filter, convert, and optionally write.

    Args:
        har_path: Path to the .har file.
        output_path: Where to write output. If None, returns the content
            as a string without writing to disk.
        output_format: "scenario" (JSON scenario) or "url-file" (URL list).
        config: Import options. Uses defaults if None.
        name: Scenario name (used with format="scenario").

    Returns:
        The generated content as a string.

    Raises:
        FileNotFoundError: HAR file not found.
        ValueError: Invalid HAR or no entries after filtering.
    """
    if config is None:
        config = HarImportConfig()

    entries = parse_har(har_path)
    filtered = filter_entries(entries, config)

    if not filtered:
        raise ValueError(
            f"No requests remained after filtering {len(entries)} HAR entries. "
            f"Try --include-static or adjusting --domain / --exclude filters."
        )

    scenario_name = name or os.path.splitext(os.path.basename(har_path))[0]

    if output_format == "scenario":
        scenario = har_to_scenario(filtered, config, name=scenario_name)
        content = json.dumps(scenario, indent=2) + "\n"
    elif output_format == "url-file":
        content = har_to_url_file(filtered)
    else:
        raise ValueError(
            f"Unknown output format: {output_format!r} (expected 'scenario' or 'url-file')"
        )

    if output_path:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(content)

    return content
