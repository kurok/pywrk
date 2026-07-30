#!/usr/bin/env python3
"""
pywrkr - A Python HTTP benchmarking tool inspired by wrk and Apache ab,
with extended statistics.

Usage:
    python pywrkr.py -c 100 -d 10 -t 4 http://localhost:8080/
    python pywrkr.py -n 1000 -c 50 http://localhost:8080/
"""

import argparse
import asyncio
import dataclasses
import json
import logging
import os
import sys
from urllib.parse import urlparse

from pywrkr.backends import HTTP2_INSTALL_HINT, http2_available
from pywrkr.compare import (
    COMPARE_FORMATS,
    EXIT_USAGE,
    ResultsError,
    compare_results,
    load_baseline,
    load_results,
    parse_fail_on,
    render_report,
)
from pywrkr.config import (
    DEFAULT_CONNECTIONS,
    DEFAULT_DURATION,
    DEFAULT_MASTER_PORT,
    DEFAULT_THREADS,
    DEFAULT_TIMEOUT,
    AutofindConfig,
    BenchmarkConfig,
    Scenario,
    SSLConfig,
    Threshold,
    load_scenario,
    validate_scenario_templates,
)
from pywrkr.distributed import run_master, run_worker_node
from pywrkr.feeders import FEEDER_STRATEGIES, load_feeder, validate_unique_capacity
from pywrkr.har_import import HarImportConfig, convert_har
from pywrkr.multi_url import load_url_file, run_multi_url
from pywrkr.reporting import parse_threshold
from pywrkr.traffic_profiles import parse_traffic_profile
from pywrkr.workers import run_autofind, run_benchmark, run_user_simulation

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_header(s: str) -> tuple[str, str]:
    """Parse a 'Name: Value' header string into a (name, value) tuple."""
    if ":" not in s:
        raise argparse.ArgumentTypeError(f"Invalid header format: {s} (expected 'Name: Value')")
    name, value = s.split(":", 1)
    return name.strip(), value.strip()


def _add_core_options(parser: argparse.ArgumentParser) -> None:
    """Add core HTTP and connection options to the parser."""
    parser.add_argument("url", nargs="?", default=None, help="Target URL to benchmark")
    parser.add_argument(
        "-c",
        "--connections",
        type=int,
        default=DEFAULT_CONNECTIONS,
        help=f"Number of concurrent connections (default: {DEFAULT_CONNECTIONS})",
    )
    parser.add_argument(
        "-d",
        "--duration",
        type=float,
        default=None,
        help="Duration of test in seconds (default: 10; mutually exclusive with -n)",
    )
    parser.add_argument(
        "-n",
        "--num-requests",
        type=int,
        default=None,
        help="Total number of requests to make (ab-style; mutually exclusive with -d)",
    )
    parser.add_argument(
        "-t",
        "--threads",
        type=int,
        default=DEFAULT_THREADS,
        help=f"Number of worker groups (default: {DEFAULT_THREADS})",
    )
    parser.add_argument("-m", "--method", default="GET", help="HTTP method (default: GET)")
    parser.add_argument(
        "-H",
        "--header",
        action="append",
        type=parse_header,
        default=[],
        dest="headers",
        help="HTTP header (e.g. -H 'Content-Type: application/json')",
    )
    parser.add_argument("-b", "--body", default=None, help="Request body string")
    parser.add_argument(
        "-p", "--post-file", default=None, help="File containing POST body data (ab-style)"
    )
    parser.add_argument(
        "-A",
        "--basic-auth",
        default=None,
        metavar="user:pass",
        help="Basic HTTP authentication (ab-style)",
    )
    parser.add_argument(
        "-C",
        "--cookie",
        action="append",
        default=[],
        dest="cookies",
        help="Cookie 'name=value' (repeatable, ab-style)",
    )
    parser.add_argument(
        "--no-session-cookies",
        action="store_false",
        dest="session_cookies",
        default=True,
        help="Do not honor Set-Cookie. By default each virtual user keeps its own "
        "cookie jar, so N users look like N sessions; use this to send only the "
        "static -C cookies (e.g. when benchmarking a cache or CDN layer).",
    )
    parser.add_argument(
        "--http2",
        action="store_true",
        default=False,
        help="Use the HTTP/2-capable backend (requires the extra: "
        f"{HTTP2_INSTALL_HINT}). Over https:// the protocol is negotiated by ALPN "
        "and an HTTP/1.1-only server is reported, not silently accepted; over "
        "http:// HTTP/2 is used with prior knowledge (h2c). Note that -c then "
        "bounds concurrent streams rather than connections",
    )
    parser.add_argument(
        "-k",
        "--keepalive",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Enable keep-alive (default: on); use --no-keepalive to disable "
        "(close connection after each request). Last flag wins.",
    )
    parser.add_argument(
        "-l",
        "--verify-length",
        action="store_true",
        default=False,
        help="Verify response Content-Length consistency (ab-style)",
    )
    parser.add_argument(
        "-v",
        "--verbosity",
        type=int,
        default=0,
        help="Verbosity level: 2=warnings, 3=status codes, 4=headers+body info",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help=f"Request timeout in seconds (default: {DEFAULT_TIMEOUT})",
    )
    parser.add_argument(
        "--ssl-verify",
        action="store_true",
        default=False,
        help="Verify SSL certificates (default: off, or set PYWRKR_SSL_VERIFY=1)",
    )
    parser.add_argument(
        "--ca-bundle",
        default=None,
        metavar="FILE",
        help="Path to CA bundle for SSL verification (or set PYWRKR_CA_BUNDLE)",
    )
    parser.add_argument(
        "-R",
        "--random-param",
        action="store_true",
        default=False,
        help="Append a unique random query parameter (_cb=<uuid>) to each request "
        "URL to bypass HTTP caching",
    )


def _add_output_options(parser: argparse.ArgumentParser) -> None:
    """Add output format and reporting options to the parser."""
    parser.add_argument(
        "-e",
        "--csv",
        default=None,
        metavar="FILE",
        help="Write CSV percentile table to FILE (ab-style)",
    )
    parser.add_argument(
        "-w",
        "--html",
        action="store_true",
        default=False,
        help="Print results as HTML table (ab-style)",
    )
    parser.add_argument("--json", default=None, metavar="FILE", help="Write JSON results to FILE")
    parser.add_argument(
        "--html-report",
        default=None,
        metavar="FILE",
        help="Generate interactive Gatling-style HTML report to FILE",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        default=False,
        help="Show a live TUI dashboard during the benchmark "
        "(requires rich: pip install pywrkr[tui])",
    )
    parser.add_argument(
        "--latency-breakdown",
        action="store_true",
        default=False,
        help="Show detailed latency breakdown per phase (DNS, TCP connect, TLS, TTFB, transfer)",
    )
    parser.add_argument(
        "--tag",
        action="append",
        default=[],
        dest="tags",
        help="Metadata tag as key=value (repeatable, e.g. --tag environment=prod)",
    )
    parser.add_argument(
        "--otel-endpoint",
        default=None,
        metavar="URL",
        help="Export metrics to an OpenTelemetry collector via OTLP/HTTP",
    )
    parser.add_argument(
        "--prom-remote-write",
        default=None,
        metavar="URL",
        help="Push metrics to a Prometheus Pushgateway-compatible endpoint",
    )
    parser.add_argument(
        "--baseline",
        default=None,
        metavar="FILE_OR_GLOB",
        help="Compare this run against previous --json results and apply --fail-on "
        "rules. A glob (e.g. 'baselines/*.json') averages the runs it matches",
    )
    parser.add_argument(
        "--save-baseline",
        default=None,
        metavar="FILE",
        help="Write this run's results to FILE for a later --baseline comparison",
    )
    parser.add_argument(
        "--fail-on",
        action="append",
        default=[],
        dest="fail_on",
        metavar="EXPR",
        help="Regression rule against the baseline delta (repeatable), e.g. "
        "--fail-on 'p95 > +10%%' --fail-on 'rps < -5%%'. Exit code 3 when one fires",
    )
    parser.add_argument(
        "--strict-config",
        action="store_true",
        default=False,
        help="Fail (exit 1) instead of warning when the baseline run used a "
        "different load shape (users, connections, duration, host)",
    )
    parser.add_argument(
        "--compare-format",
        choices=list(COMPARE_FORMATS),
        default="table",
        help="Baseline comparison output format (default: table)",
    )
    parser.add_argument(
        "--threshold",
        "--th",
        action="append",
        default=[],
        dest="thresholds",
        help="SLO threshold expression (repeatable, e.g. --threshold 'p95 < 300ms'). "
        "Exit code 2 if any threshold is breached.",
    )


def _add_user_simulation_options(parser: argparse.ArgumentParser) -> None:
    """Add user simulation mode options to the parser."""
    parser.add_argument(
        "-u",
        "--users",
        type=int,
        default=None,
        help="Number of virtual users (enables user simulation mode)",
    )
    parser.add_argument(
        "--ramp-up",
        type=float,
        default=0,
        help="Ramp-up period in seconds to start all users (default: 0)",
    )
    parser.add_argument(
        "--think-time",
        type=float,
        default=1.0,
        help="Mean think time in seconds between requests per user (default: 1.0)",
    )
    parser.add_argument(
        "--think-jitter",
        type=float,
        default=0.5,
        help="Think time jitter factor 0-1 (default: 0.5, e.g. 1s +/-50%%)",
    )


def _add_rate_and_traffic_options(parser: argparse.ArgumentParser) -> None:
    """Add rate limiting and traffic shaping options to the parser."""
    parser.add_argument(
        "--rate", type=float, default=None, help="Target requests per second (constant rate mode)"
    )
    parser.add_argument(
        "--rate-ramp",
        type=float,
        default=None,
        help="Linearly ramp rate from --rate to this value over the duration",
    )
    parser.add_argument(
        "--traffic-profile",
        default=None,
        metavar="PROFILE",
        help="Traffic shaping profile. Built-in: sine, step, sawtooth, "
        "square, spike, business-hours. CSV replay: csv:file.csv. "
        "Parameters: 'sine:cycles=3,min=0.2', "
        "'step:levels=100,500,1000', 'spike:interval=10,multiplier=5'. "
        "Requires --rate (used as base/peak rate)",
    )
    parser.add_argument(
        "--scenario",
        default=None,
        metavar="FILE",
        help="Path to a JSON/YAML scenario file for scripted multi-step requests",
    )
    parser.add_argument(
        "--data",
        action="append",
        default=[],
        dest="data",
        metavar="NAME=FILE",
        help="Attach a CSV/JSON data set to the scenario, referenced as "
        "${NAME.column} (repeatable). Overrides a set of the same name in the "
        "scenario file. Requires --scenario",
    )
    parser.add_argument(
        "--data-strategy",
        action="append",
        default=[],
        dest="data_strategies",
        metavar="NAME=STRATEGY",
        help=f"How rows are handed out for a data set (repeatable): "
        f"{', '.join(FEEDER_STRATEGIES)} (default: {FEEDER_STRATEGIES[0]})",
    )


def _add_autofind_options(parser: argparse.ArgumentParser) -> None:
    """Add autofind capacity-testing options to the parser."""
    parser.add_argument(
        "--autofind",
        action="store_true",
        default=False,
        help="Auto-ramp load to find maximum sustainable capacity",
    )
    parser.add_argument(
        "--max-error-rate",
        type=float,
        default=1.0,
        help="Autofind: stop when error rate exceeds this percent (default: 1.0)",
    )
    parser.add_argument(
        "--max-p95",
        type=float,
        default=5.0,
        help="Autofind: stop when p95 latency exceeds this in seconds (default: 5.0)",
    )
    parser.add_argument(
        "--step-duration",
        type=float,
        default=30.0,
        help="Autofind: duration of each step test in seconds (default: 30)",
    )
    parser.add_argument(
        "--start-users",
        type=int,
        default=10,
        help="Autofind: starting number of users (default: 10)",
    )
    parser.add_argument(
        "--max-users",
        type=int,
        default=10000,
        help="Autofind: maximum users to try (default: 10000)",
    )
    parser.add_argument(
        "--step-multiplier",
        type=float,
        default=2.0,
        help="Autofind: multiply users by this each step (default: 2.0)",
    )


def _add_distributed_options(parser: argparse.ArgumentParser) -> None:
    """Add distributed mode and multi-URL options to the parser."""
    parser.add_argument(
        "--url-file",
        default=None,
        metavar="FILE",
        help="File with URLs to benchmark (one per line, optional METHOD prefix). "
        "Runs each URL sequentially with the same settings and prints a comparison.",
    )
    parser.add_argument(
        "--master",
        action="store_true",
        default=False,
        help="Run as master node in distributed mode",
    )
    parser.add_argument(
        "--expect-workers",
        type=int,
        default=None,
        metavar="N",
        help="Number of workers the master should wait for (required with --master)",
    )
    parser.add_argument(
        "--bind", default="0.0.0.0", metavar="HOST", help="Master bind address (default: 0.0.0.0)"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_MASTER_PORT,
        metavar="PORT",
        help=f"Master/worker port (default: {DEFAULT_MASTER_PORT})",
    )
    parser.add_argument(
        "--worker",
        default=None,
        metavar="HOST:PORT",
        help="Run as worker node, connecting to master at HOST:PORT",
    )
    parser.add_argument(
        "--worker-secret",
        default=None,
        metavar="SECRET",
        help=(
            "Shared secret for authenticating distributed workers (HMAC-SHA256). "
            "Can also be set via the PYWRKR_WORKER_SECRET environment variable. "
            "Master and all workers must use the same secret. "
            "Omitting this leaves the distributed channel unauthenticated."
        ),
    )


def _build_har_import_parser() -> argparse.ArgumentParser:
    """Create the argument parser for the har-import subcommand."""
    parser = argparse.ArgumentParser(
        prog="pywrkr har-import",
        description="Convert a HAR file (browser recording) into a pywrkr scenario or URL file",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Convert HAR to scenario JSON:
  pywrkr har-import recording.har -o scenario.json

  # Convert HAR to URL file for --url-file mode:
  pywrkr har-import recording.har --format url-file -o urls.txt

  # Filter to specific domain, include static assets:
  pywrkr har-import recording.har --domain api.example.com --include-static -o scenario.json

  # Exclude patterns, preserve original headers:
  pywrkr har-import recording.har --exclude '/analytics' --exclude '/tracking' \\
      --preserve-headers -o scenario.json

  # Assert recorded status codes, custom think time multiplier:
  pywrkr har-import recording.har --assert-status --think-time-multiplier 0.5 -o scenario.json
        """,
    )
    parser.add_argument("har_file", help="Path to the HAR file to convert")
    parser.add_argument(
        "-o",
        "--output",
        default=None,
        metavar="FILE",
        help="Output file path (default: print to stdout)",
    )
    parser.add_argument(
        "--format",
        choices=["scenario", "url-file"],
        default="scenario",
        help="Output format (default: scenario)",
    )
    parser.add_argument(
        "--name", default=None, help="Scenario name (default: derived from HAR filename)"
    )
    parser.add_argument(
        "--include-static",
        action="store_true",
        default=False,
        help="Include static assets (CSS, JS, images, fonts)",
    )
    parser.add_argument(
        "--domain",
        action="append",
        default=[],
        dest="domains",
        help="Only include requests to this domain (repeatable)",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        dest="exclude_patterns",
        help=(
            "Exclude URLs matching this regex pattern (repeatable). "
            "WARNING: patterns with catastrophic backtracking (e.g. '(a+)+') "
            "matched against long URLs will hang the process. Use simple, "
            "anchored patterns such as r'^https://example\\.com/api'."
        ),
    )
    parser.add_argument(
        "--include",
        action="append",
        default=[],
        dest="include_patterns",
        help=(
            "Only include URLs matching this regex pattern (repeatable). "
            "WARNING: patterns with catastrophic backtracking (e.g. '(a+)+') "
            "matched against long URLs will hang the process. Use simple, "
            "anchored patterns such as r'^https://example\\.com/api'."
        ),
    )
    parser.add_argument(
        "--preserve-headers",
        action="store_true",
        default=False,
        help="Preserve request headers from the HAR recording "
        "(default: only keep Content-Type for POST/PUT)",
    )
    parser.add_argument(
        "--no-think-time",
        action="store_true",
        default=False,
        help="Don't derive think times from recorded request timing",
    )
    parser.add_argument(
        "--think-time-multiplier",
        type=float,
        default=1.0,
        help="Multiply derived think times by this factor (default: 1.0)",
    )
    parser.add_argument(
        "--assert-status",
        action="store_true",
        default=False,
        help="Add status code assertions from recorded responses",
    )
    return parser


def _build_compare_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the `compare` subcommand."""
    parser = argparse.ArgumentParser(
        prog="pywrkr compare",
        description="Compare two pywrkr --json result files and gate on the deltas",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Delta table only (never fails)
  pywrkr compare baseline.json current.json

  # Gate a PR: fail if p95 got 10%% worse or throughput dropped 5%%
  pywrkr compare baseline.json current.json \\
      --fail-on 'p95 > +10%%' --fail-on 'rps < -5%%'

  # Absolute deltas, and a per-step metric
  pywrkr compare base.json cur.json --fail-on 'p99 > +50ms' \\
      --fail-on 'step:checkout.mean > +20ms'

  # Average several baseline runs to ride out single-run noise
  pywrkr compare 'baselines/*.json' current.json --fail-on 'p95 > +10%%'

  # Ready to paste into a PR comment
  pywrkr compare base.json cur.json --fail-on 'p95 > +10%%' --format markdown

Exit codes: 0 = no regression, 3 = a --fail-on rule fired, 1 = usage/schema error.
""",
    )
    parser.add_argument("baseline", help="Baseline --json file, or a glob to average")
    parser.add_argument("current", help="Current --json file")
    parser.add_argument(
        "--fail-on",
        action="append",
        default=[],
        dest="fail_on",
        metavar="EXPR",
        help="Regression rule (repeatable), e.g. 'p95 > +10%%', 'rps < -5%%', 'p99 > +50ms'",
    )
    parser.add_argument(
        "--format",
        choices=list(COMPARE_FORMATS),
        default="table",
        dest="format",
        help="Output format (default: table)",
    )
    parser.add_argument(
        "--strict-config",
        action="store_true",
        default=False,
        help="Fail (exit 1) instead of warning when the two runs used different load shapes",
    )
    return parser


def _run_compare(args: argparse.Namespace) -> None:
    """Execute the compare subcommand."""
    rules = []
    for expr in args.fail_on:
        try:
            rules.append(parse_fail_on(expr))
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(EXIT_USAGE)

    try:
        baseline, sources = load_baseline(args.baseline)
        current = load_results(args.current)
    except ResultsError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(EXIT_USAGE)

    report = compare_results(baseline, current, rules, sources)
    render_report(report, args.format)

    if report.config_warnings and args.strict_config:
        print(
            "Error: --strict-config is set and the run configurations differ",
            file=sys.stderr,
        )
        sys.exit(EXIT_USAGE)
    sys.exit(report.exit_code)


def _build_parser() -> argparse.ArgumentParser:
    """Create and configure the argument parser."""
    parser = argparse.ArgumentParser(
        description="pywrkr - HTTP benchmarking tool with extended statistics (wrk + ab features)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Duration mode (wrk-style):
  %(prog)s http://localhost:8080/
  %(prog)s -c 200 -d 30 http://localhost:8080/api

  # Request-count mode (ab-style):
  %(prog)s -n 1000 -c 50 http://localhost:8080/

  # POST with auth, cookies, and JSON output:
  %(prog)s -n 500 -c 20 -m POST -b '{"key":"val"}' \\
      -H "Content-Type: application/json" \\
      -A user:pass -C "session=abc123" \\
      --json results.json http://localhost:8080/api

  # User simulation: 1500 users, 5 min, 30s ramp-up, 1s think time:
  %(prog)s -u 1500 -d 300 --ramp-up 30 --think-time 1.0 http://localhost:8080/

  # Cache-busting: append random query param to bypass HTTP caches:
  %(prog)s -R -c 100 -d 10 http://localhost:8080/
  %(prog)s -R -u 300 -d 300 --think-time 1.0 https://example.com/

  # HAR import: convert browser recording to scenario:
  %(prog)s har-import recording.har -o scenario.json
  %(prog)s har-import recording.har --format url-file -o urls.txt
        """,
    )
    _add_core_options(parser)
    _add_output_options(parser)
    _add_user_simulation_options(parser)
    _add_rate_and_traffic_options(parser)
    _add_autofind_options(parser)
    _add_distributed_options(parser)
    return parser


def _require_http_scheme(
    parser: argparse.ArgumentParser,
    url: str,
    context: str,
) -> None:
    """Reject any URL whose scheme is not http(s).

    ``context`` is woven into the error message so the user knows which input
    was rejected (positional URL, url-file entry, or scenario base_url).
    """
    scheme = urlparse(url).scheme
    if scheme not in ("http", "https"):
        parser.error(f"Invalid URL scheme{context}: {url}. Use http:// or https://")


def _validate_mode_conflicts(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
) -> None:
    """Reject incompatible mode-flag combinations instead of silently resolving them.

    Mode dispatch (``_determine_and_run_mode``) picks a single mode by if/elif
    order, which would otherwise drop the other flags without warning. Surface
    those conflicts as clear ``parser.error`` (exit 2) failures.
    """
    # --autofind drives its own user ramp and ignores all load-shape flags.
    if args.autofind:
        ignored = []
        if args.num_requests is not None:
            ignored.append("-n/--num-requests")
        if args.rate is not None:
            ignored.append("--rate")
        if args.rate_ramp is not None:
            ignored.append("--rate-ramp")
        if args.traffic_profile is not None:
            ignored.append("--traffic-profile")
        if args.users is not None:
            ignored.append("-u/--users")
        if args.duration is not None:
            ignored.append("-d/--duration")
        if ignored:
            parser.error(
                "--autofind manages its own user ramp and cannot be combined with "
                f"{', '.join(ignored)}; use --start-users/--max-users/--step-duration instead"
            )
        # --threshold cannot be evaluated in autofind mode (AutofindConfig carries
        # no thresholds), so reject it rather than silently ignoring it. Use
        # --max-p95/--max-error-rate as the autofind pass/fail gate instead.
        if args.thresholds:
            parser.error(
                "--threshold is not supported with --autofind; "
                "use --max-p95/--max-error-rate as the capacity gate"
            )

    # --master and --url-file are standalone modes; reject overlap.
    if args.master and args.autofind:
        parser.error("--master cannot be combined with --autofind")
    if args.master and args.url_file is not None:
        parser.error("--master cannot be combined with --url-file")
    if args.url_file is not None and args.autofind:
        parser.error("--url-file cannot be combined with --autofind")


def _validate_url_and_mode(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
) -> None:
    """Validate URL, worker/master mode, and mutually exclusive options."""
    # Early exit: worker mode connects to a master and needs no URL
    if args.worker is not None:
        if ":" not in args.worker:
            parser.error("--worker requires HOST:PORT format (e.g. --worker 192.168.1.1:9220)")
        host, port_str = args.worker.rsplit(":", 1)
        try:
            w_port = int(port_str)
        except ValueError:
            parser.error(f"Invalid port in --worker: {port_str}")
        worker_secret = args.worker_secret or os.environ.get("PYWRKR_WORKER_SECRET")
        asyncio.run(run_worker_node(host, w_port, worker_secret=worker_secret))
        sys.exit(0)

    _validate_mode_conflicts(parser, args)

    # Multi-URL mode: URLs come from a file. Parse exactly once and reuse the
    # parsed entries for scheme validation and execution (avoids redundant I/O
    # and the TOCTOU window between validation and run).
    if args.url_file is not None:
        try:
            entries = load_url_file(args.url_file)
        except (FileNotFoundError, ValueError) as e:
            parser.error(str(e))  # exits; never returns
        else:
            for entry in entries:
                _require_http_scheme(parser, entry.url, " in url-file")
            args.url_entries = entries

    # Master mode needs both a URL and a worker count
    if args.master:
        if args.expect_workers is None or args.expect_workers < 1:
            parser.error("--master requires --expect-workers N (N >= 1)")
        if args.url is None:
            parser.error("--master requires a target URL")

    # URL is required for all remaining modes
    if args.url is None and args.url_file is None and not args.scenario:
        parser.error("the following arguments are required: url (or --url-file or --scenario)")

    if args.url is not None:
        _require_http_scheme(parser, args.url, "")


def _validate_http2(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Reject --http2 when the optional backend is not installed.

    Failing here, by name, beats an ImportError surfacing from inside a worker
    once the run is already under way.
    """
    if not getattr(args, "http2", False):
        return
    if not http2_available():
        parser.error(
            f"--http2 requires the HTTP/2 backend, which is not installed. "
            f"Install it with: {HTTP2_INSTALL_HINT}"
        )
    if args.latency_breakdown:
        logger.warning(
            "--latency-breakdown with --http2 reports only TTFB and transfer: the "
            "HTTP/2 backend has no hooks for the DNS, TCP and TLS phases, so those "
            "are omitted rather than reported as zero."
        )


def _validate_load_params(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
) -> float | None:
    """Validate load mode options and return effective duration."""
    if args.connections < 1:
        parser.error(f"--connections (-c) must be at least 1, got {args.connections}")
    if args.threads < 1:
        parser.error(f"--threads (-t) must be at least 1, got {args.threads}")
    if args.duration is not None and args.duration <= 0:
        parser.error(f"--duration (-d) must be greater than 0, got {args.duration}")
    if args.num_requests is not None and args.num_requests < 1:
        parser.error(f"--num-requests (-n) must be at least 1, got {args.num_requests}")
    if args.timeout <= 0:
        parser.error(f"--timeout must be greater than 0, got {args.timeout}")
    if args.ramp_up < 0:
        parser.error(f"--ramp-up must be >= 0, got {args.ramp_up}")
    if args.think_time < 0:
        parser.error(f"--think-time must be >= 0, got {args.think_time}")
    if not 0.0 <= args.think_jitter <= 1.0:
        parser.error(f"--think-jitter must be between 0 and 1, got {args.think_jitter}")

    if args.autofind:
        if args.max_users <= args.start_users:
            parser.error(
                f"--max-users ({args.max_users}) must be greater than "
                f"--start-users ({args.start_users})"
            )
        if args.step_multiplier <= 1.0:
            parser.error(f"--step-multiplier ({args.step_multiplier}) must be greater than 1.0")
        # Autofind capacity-tests a plain GET; request-shaping flags would be
        # silently dropped (AutofindConfig carries none of them), producing
        # misleading capacity numbers. Reject the combination explicitly.
        unsupported = []
        if args.method != "GET":
            unsupported.append("-m/--method")
        if args.headers:
            unsupported.append("-H/--header")
        if args.body:
            unsupported.append("-b/--body")
        if args.post_file:
            unsupported.append("-p/--post-file")
        if args.basic_auth:
            unsupported.append("-A/--basic-auth")
        if args.cookies:
            unsupported.append("-C/--cookie")
        if args.verify_length:
            unsupported.append("-l/--verify-length")
        if unsupported:
            parser.error(
                "--autofind only supports plain unauthenticated GET load; "
                f"{', '.join(unsupported)} are not supported in autofind mode"
            )
    elif args.users is not None:
        if args.users < 1:
            parser.error(f"--users (-u) must be at least 1, got {args.users}")
        if args.num_requests is not None:
            parser.error("Cannot use -n with -u (user simulation). Use -d for duration.")
        if args.duration is None:
            parser.error("User simulation mode (-u) requires -d (duration).")
    elif args.num_requests is not None and args.duration is not None:
        parser.error("Cannot use both -n (request count) and -d (duration). Pick one.")

    if args.rate is not None and args.rate <= 0:
        parser.error("--rate must be greater than 0")

    duration = args.duration
    if args.users is None and args.num_requests is None and duration is None:
        duration = DEFAULT_DURATION

    if args.ramp_up and duration is not None and args.ramp_up >= duration:
        parser.error(f"--ramp-up ({args.ramp_up}s) must be less than duration ({duration}s)")

    return duration


def _validate_rate_and_traffic(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
    config: BenchmarkConfig,
) -> None:
    """Validate rate limiting and traffic profile cross-field constraints."""
    if config.rate_ramp is not None and config.rate is None:
        parser.error("--rate-ramp requires --rate")
    if config.rate_ramp is not None and config.duration is None:
        parser.error("--rate-ramp requires -d (duration)")
    if config.rate_ramp is not None and config.rate_ramp <= 0:
        parser.error(f"--rate-ramp must be greater than 0, got {config.rate_ramp}")

    if args.traffic_profile is not None:
        if config.rate is None:
            parser.error("--traffic-profile requires --rate (used as base/peak rate)")
        if config.duration is None:
            parser.error("--traffic-profile requires -d (duration)")
        if config.rate_ramp is not None:
            parser.error("--traffic-profile cannot be combined with --rate-ramp")
        try:
            config.traffic_profile = parse_traffic_profile(args.traffic_profile)
        except (ValueError, FileNotFoundError, OSError) as e:
            parser.error(f"Invalid --traffic-profile: {e}")


def _resolve_body(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
) -> bytes | None:
    """Resolve request body from --post-file or --body."""
    if args.post_file:
        if not os.path.isfile(args.post_file):
            parser.error(f"Post file not found: {args.post_file}")
        with open(args.post_file, "rb") as f:
            return f.read()
    elif args.body:
        return args.body.encode()
    return None


def _parse_tags_and_thresholds(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
) -> tuple[dict[str, str], list[Threshold]]:
    """Parse and validate --tag and --threshold CLI values."""
    tags: dict[str, str] = {}
    for tag_str in args.tags:
        if "=" not in tag_str:
            parser.error(f"Invalid tag format: {tag_str!r} (expected 'key=value')")
        key, value = tag_str.split("=", 1)
        tags[key.strip()] = value.strip()

    thresholds: list[Threshold] = []
    for expr in args.thresholds:
        try:
            thresholds.append(parse_threshold(expr))
        except ValueError as e:
            parser.error(str(e))

    return tags, thresholds


def _parse_baseline_options(parser: argparse.ArgumentParser, args: argparse.Namespace) -> list:
    """Validate the baseline gate flags and compile the --fail-on rules."""
    rules = []
    for expr in getattr(args, "fail_on", []) or []:
        try:
            rules.append(parse_fail_on(expr))
        except ValueError as e:
            parser.error(str(e))

    if rules and not args.baseline:
        parser.error("--fail-on requires --baseline (there is nothing to compare against)")
    if args.strict_config and not args.baseline:
        parser.error("--strict-config requires --baseline")
    if args.baseline and not rules:
        # Still useful: the delta table is printed, the gate just never fails.
        logger.warning(
            "--baseline given without --fail-on: the comparison is reported but "
            "cannot fail the build. Add e.g. --fail-on 'p95 > +10%%'."
        )
    return rules


def _split_named_option(parser: argparse.ArgumentParser, raw: str, flag: str) -> tuple[str, str]:
    """Split a ``NAME=VALUE`` CLI option, erroring out on a malformed one."""
    if "=" not in raw:
        parser.error(f"Invalid {flag} value {raw!r} (expected 'NAME=VALUE')")
    name, value = raw.split("=", 1)
    name, value = name.strip(), value.strip()
    if not name or not value:
        parser.error(f"Invalid {flag} value {raw!r} (expected 'NAME=VALUE')")
    return name, value


def _apply_data_options(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
    scenario: "Scenario | None",
) -> None:
    """Merge ``--data`` / ``--data-strategy`` into the loaded scenario.

    CLI data sets override same-named ones from the scenario file, and a
    strategy may be given for a set the file already declares. Everything is
    resolved here so a bad path or an unknown strategy is a startup error.
    """
    data_args = getattr(args, "data", []) or []
    strategy_args = getattr(args, "data_strategies", []) or []
    if not data_args and not strategy_args:
        return
    if scenario is None:
        parser.error(
            "--data/--data-strategy require --scenario: data sets feed "
            "${name.column} placeholders in scenario steps"
        )

    strategies: dict[str, str] = {}
    for raw in strategy_args:
        name, strategy = _split_named_option(parser, raw, "--data-strategy")
        strategies[name] = strategy

    for raw in data_args:
        name, path = _split_named_option(parser, raw, "--data")
        try:
            scenario.data[name] = load_feeder(
                name, path, strategies.pop(name, FEEDER_STRATEGIES[0])
            )
        except ValueError as e:
            parser.error(f"Invalid --data: {e}")

    # Leftover strategies must name a set the scenario file declared, otherwise
    # the flag silently does nothing.
    for name, strategy in strategies.items():
        feeder = scenario.data.get(name)
        if feeder is None:
            known = ", ".join(sorted(scenario.data)) or "none"
            parser.error(
                f"--data-strategy names unknown data set {name!r} "
                f"(declared: {known}); add it with --data {name}=FILE"
            )
        if strategy not in FEEDER_STRATEGIES:
            parser.error(
                f"--data-strategy {name}={strategy!r}: unknown strategy; "
                f"expected one of {', '.join(FEEDER_STRATEGIES)}"
            )
        # Only the strategy changes; the rows are already loaded.
        scenario.data[name] = dataclasses.replace(feeder, strategy=strategy)

    try:
        validate_scenario_templates(scenario)
    except ValueError as e:
        parser.error(str(e))


def _parse_and_validate_args(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
) -> tuple[BenchmarkConfig, argparse.Namespace]:
    """Validate parsed CLI arguments and build a BenchmarkConfig.

    Delegates to focused sub-validators, then constructs the config object.
    Returns both the config and the raw namespace (needed by
    _determine_and_run_mode for mode-specific fields).
    """
    _validate_url_and_mode(parser, args)
    _validate_http2(parser, args)

    duration = _validate_load_params(parser, args)
    body = _resolve_body(parser, args)
    tags, thresholds = _parse_tags_and_thresholds(parser, args)

    # Load scenario file (JSON or YAML) if provided
    scenario = None
    if hasattr(args, "scenario") and args.scenario:
        try:
            scenario = load_scenario(args.scenario)
        except (FileNotFoundError, ValueError, json.JSONDecodeError, ImportError) as e:
            parser.error(f"Invalid --scenario: {e}")

    _apply_data_options(parser, args, scenario)

    # Scenario mode: use scenario's base_url as fallback when no positional URL given
    if scenario and args.url is None:
        if scenario.base_url:
            args.url = scenario.base_url
            # base_url is assigned after _validate_url_and_mode ran (args.url was
            # None then), so re-validate the scheme here to avoid bypass.
            _require_http_scheme(parser, args.url, " in scenario base_url")
        else:
            parser.error(
                "--scenario requires a target base URL because scenario steps contain "
                "relative paths. Either pass a URL argument "
                "(e.g. pywrkr http://host --scenario file.json) or include "
                "'base_url' in the scenario file."
            )

    # Build SSL config from CLI args + environment
    env_ssl = SSLConfig.from_env()
    ssl_config = SSLConfig(
        verify=args.ssl_verify or env_ssl.verify,
        ca_bundle=args.ca_bundle or env_ssl.ca_bundle,
    )

    credentials_in_use = bool(args.basic_auth or args.cookies)
    url_is_https = (args.url or "").startswith("https://")
    if credentials_in_use and url_is_https and not ssl_config.verify:
        print(
            "WARNING: SSL verification is disabled while credentials are in use. "
            "Your credentials may be exposed to a man-in-the-middle attack. "
            "Use --ssl-verify to enable certificate validation.",
            file=sys.stderr,
        )

    config = BenchmarkConfig(
        url=args.url or "",
        connections=args.connections,
        duration=duration,
        num_requests=args.num_requests,
        threads=args.threads,
        method=args.method.upper(),
        headers=dict(args.headers),
        body=body,
        timeout_sec=args.timeout,
        keepalive=args.keepalive,
        ssl_config=ssl_config,
        basic_auth=args.basic_auth,
        cookies=args.cookies,
        session_cookies=args.session_cookies,
        http2=args.http2,
        verify_content_length=args.verify_length,
        verbosity=args.verbosity,
        csv_output=args.csv,
        html_output=args.html,
        json_output=args.json,
        html_report=args.html_report,
        users=args.users,
        ramp_up=args.ramp_up,
        think_time=args.think_time,
        think_time_jitter=args.think_jitter,
        random_param=args.random_param,
        live_dashboard=args.live,
        rate=args.rate,
        rate_ramp=args.rate_ramp,
        traffic_profile=None,
        scenario=scenario,
        latency_breakdown=args.latency_breakdown,
        tags=tags,
        otel_endpoint=args.otel_endpoint,
        prom_remote_write=args.prom_remote_write,
        thresholds=thresholds,
        baseline=args.baseline,
        save_baseline=args.save_baseline,
        fail_on=_parse_baseline_options(parser, args),
        strict_config=args.strict_config,
        compare_format=args.compare_format,
    )

    _validate_rate_and_traffic(parser, args, config)

    # Scenario mode defaults to 10s if no duration/count is specified
    if (
        config.scenario
        and config.users is None
        and config.duration is None
        and config.num_requests is None
    ):
        config.duration = 10.0

    if config.scenario is not None and config.scenario.data:
        # Checked here rather than at scenario load: capacity depends on the
        # user count, request budget, and worker count, none of which the
        # scenario file knows.
        try:
            validate_unique_capacity(
                config.scenario.data,
                config.users,
                config.num_requests,
                len(config.scenario.steps),
                nodes=max(1, getattr(args, "expect_workers", 1) or 1) if args.master else 1,
            )
        except ValueError as e:
            parser.error(str(e))

    return config, args


def _determine_and_run_mode(config: BenchmarkConfig, args: argparse.Namespace) -> None:
    """Determine which mode to run and execute."""
    if args.url_file is not None:
        # Reuse the entries parsed during validation (see _validate_url_and_mode)
        # to avoid a redundant re-parse and the TOCTOU window. Fall back to a
        # fresh parse only if validation did not run (e.g. direct unit calls).
        url_entries = getattr(args, "url_entries", None)
        if url_entries is None:
            url_entries = load_url_file(args.url_file)
        results = asyncio.run(run_multi_url(url_entries, config))
        exit_code = max((r.exit_code for r in results), default=0)
        sys.exit(exit_code)
    elif args.master:
        worker_secret = args.worker_secret or os.environ.get("PYWRKR_WORKER_SECRET")
        result = asyncio.run(
            run_master(
                config, args.bind, args.port, args.expect_workers, worker_secret=worker_secret
            )
        )
        if result:
            _, exit_code = result
            sys.exit(exit_code)
        sys.exit(1)
    elif args.autofind:
        af_config = AutofindConfig(
            url=args.url,
            max_error_rate=args.max_error_rate,
            max_p95=args.max_p95,
            step_duration=args.step_duration,
            start_users=args.start_users,
            max_users=args.max_users,
            step_multiplier=args.step_multiplier,
            think_time=args.think_time,
            think_time_jitter=args.think_jitter,
            random_param=args.random_param,
            timeout_sec=args.timeout,
            keepalive=config.keepalive,
            ssl_config=config.ssl_config,
            json_output=args.json,
        )
        steps = asyncio.run(run_autofind(af_config))
        # Autofind must be usable as a CI gate: exit non-zero when no sustainable
        # load was found (no step passed the --max-p95/--max-error-rate gate).
        sustainable = any(step.passed for step in steps)
        sys.exit(0 if sustainable else 2)
    elif config.users is not None:
        _, exit_code = asyncio.run(run_user_simulation(config))
        sys.exit(exit_code)
    else:
        _, exit_code = asyncio.run(run_benchmark(config))
        sys.exit(exit_code)


def _run_har_import(args: argparse.Namespace) -> None:
    """Execute the har-import subcommand."""
    config = HarImportConfig(
        include_static=args.include_static,
        exclude_patterns=args.exclude_patterns,
        include_patterns=args.include_patterns,
        allowed_domains=args.domains,
        preserve_headers=args.preserve_headers,
        add_think_time=not args.no_think_time,
        think_time_multiplier=args.think_time_multiplier,
        assert_status=args.assert_status,
    )
    try:
        content = convert_har(
            har_path=args.har_file,
            output_path=args.output,
            output_format=args.format,
            config=config,
            name=args.name,
        )
    except (FileNotFoundError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if args.output:
        entries_word = "scenario" if args.format == "scenario" else "URL file"
        print(f"Wrote {entries_word} to {args.output}")
    else:
        print(content, end="")


def main() -> None:
    """CLI entry point for pywrkr."""
    logging.basicConfig(
        level=logging.WARNING,
        format="%(levelname)s [%(name)s] %(message)s",
    )
    # Intercept subcommands before the main parser (which has a positional
    # `url` argument that would swallow the subcommand name).
    if len(sys.argv) > 1 and sys.argv[1] == "har-import":
        parser = _build_har_import_parser()
        args = parser.parse_args(sys.argv[2:])
        _run_har_import(args)
        return

    if len(sys.argv) > 1 and sys.argv[1] == "compare":
        _run_compare(_build_compare_parser().parse_args(sys.argv[2:]))
        return

    parser = _build_parser()
    args = parser.parse_args()
    config, args = _parse_and_validate_args(parser, args)
    _determine_and_run_mode(config, args)


if __name__ == "__main__":
    main()
