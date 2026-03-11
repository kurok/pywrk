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
import os
import sys
from urllib.parse import urlparse

from pywrkr.config import (
    DEFAULT_CONNECTIONS,
    DEFAULT_DURATION,
    DEFAULT_MASTER_PORT,
    DEFAULT_THREADS,
    DEFAULT_TIMEOUT,
    AutofindConfig,
    BenchmarkConfig,
    Threshold,
    load_scenario,
)
from pywrkr.distributed import run_master, run_worker_node
from pywrkr.multi_url import load_url_file, run_multi_url
from pywrkr.reporting import parse_threshold
from pywrkr.traffic_profiles import parse_traffic_profile
from pywrkr.workers import run_autofind, run_benchmark, run_user_simulation


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_header(s: str) -> tuple[str, str]:
    """Parse a 'Name: Value' header string into a (name, value) tuple."""
    if ":" not in s:
        raise argparse.ArgumentTypeError(f"Invalid header format: {s} (expected 'Name: Value')")
    name, value = s.split(":", 1)
    return name.strip(), value.strip()


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
        """,
    )
    parser.add_argument("url", nargs="?", default=None, help="Target URL to benchmark")
    parser.add_argument("-c", "--connections", type=int, default=DEFAULT_CONNECTIONS,
                        help=f"Number of concurrent connections (default: {DEFAULT_CONNECTIONS})")
    parser.add_argument("-d", "--duration", type=float, default=None,
                        help="Duration of test in seconds (default: 10, ignored if -n is set)")
    parser.add_argument("-n", "--num-requests", type=int, default=None,
                        help="Total number of requests to make (ab-style, overrides -d)")
    parser.add_argument("-t", "--threads", type=int, default=DEFAULT_THREADS,
                        help=f"Number of worker groups (default: {DEFAULT_THREADS})")
    parser.add_argument("-m", "--method", default="GET",
                        help="HTTP method (default: GET)")
    parser.add_argument("-H", "--header", action="append", type=parse_header,
                        default=[], dest="headers",
                        help="HTTP header (e.g. -H 'Content-Type: application/json')")
    parser.add_argument("-b", "--body", default=None,
                        help="Request body string")
    parser.add_argument("-p", "--post-file", default=None,
                        help="File containing POST body data (ab-style)")
    parser.add_argument("-A", "--basic-auth", default=None, metavar="user:pass",
                        help="Basic HTTP authentication (ab-style)")
    parser.add_argument("-C", "--cookie", action="append", default=[], dest="cookies",
                        help="Cookie 'name=value' (repeatable, ab-style)")
    parser.add_argument("-k", "--keepalive", action="store_true", default=True,
                        help="Enable keep-alive (default: on)")
    parser.add_argument("--no-keepalive", action="store_true", default=False,
                        help="Disable keep-alive (close connection after each request)")
    parser.add_argument("-l", "--verify-length", action="store_true", default=False,
                        help="Verify response Content-Length consistency (ab-style)")
    parser.add_argument("-v", "--verbosity", type=int, default=0,
                        help="Verbosity level: 2=warnings, 3=status codes, 4=headers+body info")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                        help=f"Request timeout in seconds (default: {DEFAULT_TIMEOUT})")
    parser.add_argument("-e", "--csv", default=None, metavar="FILE",
                        help="Write CSV percentile table to FILE (ab-style)")
    parser.add_argument("-w", "--html", action="store_true", default=False,
                        help="Print results as HTML table (ab-style)")
    parser.add_argument("--json", default=None, metavar="FILE",
                        help="Write JSON results to FILE")
    parser.add_argument("--html-report", default=None, metavar="FILE",
                        help="Generate interactive Gatling-style HTML report to FILE")
    # User simulation mode
    parser.add_argument("-u", "--users", type=int, default=None,
                        help="Number of virtual users (enables user simulation mode)")
    parser.add_argument("--ramp-up", type=float, default=0,
                        help="Ramp-up period in seconds to start all users (default: 0)")
    parser.add_argument("--think-time", type=float, default=1.0,
                        help="Mean think time in seconds between requests per user (default: 1.0)")
    parser.add_argument("--think-jitter", type=float, default=0.5,
                        help="Think time jitter factor 0-1 (default: 0.5, e.g. 1s +/-50%%)")
    parser.add_argument("-R", "--random-param", action="store_true", default=False,
                        help="Append a unique random query parameter (_cb=<uuid>) to each request "
                             "URL to bypass HTTP caching")
    # Rate limiting mode
    parser.add_argument("--rate", type=float, default=None,
                        help="Target requests per second (constant rate mode)")
    parser.add_argument("--rate-ramp", type=float, default=None,
                        help="Linearly ramp rate from --rate to this value over the duration")
    parser.add_argument("--traffic-profile", default=None, metavar="PROFILE",
                        help="Traffic shaping profile. Built-in: sine, step, sawtooth, "
                             "square, spike, business-hours. CSV replay: csv:file.csv. "
                             "Parameters: 'sine:cycles=3,min=0.2', "
                             "'step:levels=100,500,1000', 'spike:interval=10,multiplier=5'. "
                             "Requires --rate (used as base/peak rate)")
    # Scenario mode
    parser.add_argument("--scenario", default=None, metavar="FILE",
                        help="Path to a JSON/YAML scenario file for scripted multi-step requests")
    parser.add_argument("--live", action="store_true", default=False,
                        help="Show a live TUI dashboard during the benchmark "
                             "(requires rich: pip install pywrkr[tui])")
    parser.add_argument("--latency-breakdown", action="store_true", default=False,
                        help="Show detailed latency breakdown per phase "
                             "(DNS, TCP connect, TLS, TTFB, transfer)")
    # Observability export
    parser.add_argument("--tag", action="append", default=[], dest="tags",
                        help="Metadata tag as key=value (repeatable, e.g. --tag environment=prod)")
    parser.add_argument("--otel-endpoint", default=None, metavar="URL",
                        help="Export metrics to an OpenTelemetry collector via OTLP/HTTP")
    parser.add_argument("--prom-remote-write", default=None, metavar="URL",
                        help="Push metrics to a Prometheus Pushgateway-compatible endpoint")
    # Autofind mode
    parser.add_argument("--autofind", action="store_true", default=False,
                        help="Auto-ramp load to find maximum sustainable capacity")
    parser.add_argument("--max-error-rate", type=float, default=1.0,
                        help="Autofind: stop when error rate exceeds this percent (default: 1.0)")
    parser.add_argument("--max-p95", type=float, default=5.0,
                        help="Autofind: stop when p95 latency exceeds this in seconds (default: 5.0)")
    parser.add_argument("--step-duration", type=float, default=30.0,
                        help="Autofind: duration of each step test in seconds (default: 30)")
    parser.add_argument("--start-users", type=int, default=10,
                        help="Autofind: starting number of users (default: 10)")
    parser.add_argument("--max-users", type=int, default=10000,
                        help="Autofind: maximum users to try (default: 10000)")
    parser.add_argument("--step-multiplier", type=float, default=2.0,
                        help="Autofind: multiply users by this each step (default: 2.0)")
    # SLO Thresholds
    parser.add_argument("--threshold", "--th", action="append", default=[], dest="thresholds",
                        help="SLO threshold expression (repeatable, e.g. --threshold 'p95 < 300ms'). "
                             "Exit code 2 if any threshold is breached.")
    # Multi-URL mode
    parser.add_argument("--url-file", default=None, metavar="FILE",
                        help="File with URLs to benchmark (one per line, optional METHOD prefix). "
                             "Runs each URL sequentially with the same settings and prints a comparison.")
    # Distributed mode
    parser.add_argument("--master", action="store_true", default=False,
                        help="Run as master node in distributed mode")
    parser.add_argument("--expect-workers", type=int, default=None, metavar="N",
                        help="Number of workers the master should wait for (required with --master)")
    parser.add_argument("--bind", default="0.0.0.0", metavar="HOST",
                        help="Master bind address (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=DEFAULT_MASTER_PORT, metavar="PORT",
                        help=f"Master/worker port (default: {DEFAULT_MASTER_PORT})")
    parser.add_argument("--worker", default=None, metavar="HOST:PORT",
                        help="Run as worker node, connecting to master at HOST:PORT")
    return parser


def _parse_and_validate_args(parser, args) -> tuple[BenchmarkConfig, argparse.Namespace]:
    """Parse arguments and return validated config with the raw args namespace."""
    # --- Worker mode: connect to master, no URL needed ---
    if args.worker is not None:
        if ":" not in args.worker:
            parser.error("--worker requires HOST:PORT format (e.g. --worker 192.168.1.1:9220)")
        host, port_str = args.worker.rsplit(":", 1)
        try:
            w_port = int(port_str)
        except ValueError:
            parser.error(f"Invalid port in --worker: {port_str}")
        asyncio.run(run_worker_node(host, w_port))
        sys.exit(0)

    # --- Multi-URL mode: --url-file does not require positional url ---
    if args.url_file is not None:
        try:
            url_entries = load_url_file(args.url_file)
        except (FileNotFoundError, ValueError) as e:
            parser.error(str(e))
        for entry in url_entries:
            p = urlparse(entry.url)
            if p.scheme not in ("http", "https"):
                parser.error(f"Invalid URL scheme in url-file: {entry.url}")

    # --- Master mode: requires URL and --expect-workers ---
    if args.master:
        if args.expect_workers is None or args.expect_workers < 1:
            parser.error("--master requires --expect-workers N (N >= 1)")
        if args.url is None:
            parser.error("--master requires a target URL")

    # URL is required for all non-worker, non-url-file modes
    if args.url is None and args.url_file is None:
        parser.error("the following arguments are required: url (or --url-file)")

    if args.url is not None:
        parsed = urlparse(args.url)
        if parsed.scheme not in ("http", "https"):
            parser.error(f"Invalid URL scheme: {parsed.scheme}. Use http:// or https://")

    # Determine mode
    if args.autofind:
        pass
    elif args.users is not None:
        if args.num_requests is not None:
            parser.error("Cannot use -n with -u (user simulation). Use -d for duration.")
        if args.duration is None:
            parser.error("User simulation mode (-u) requires -d (duration).")
    elif args.num_requests is not None and args.duration is not None:
        parser.error("Cannot use both -n (request count) and -d (duration). Pick one.")

    duration = args.duration
    if args.users is None and args.num_requests is None and duration is None:
        duration = DEFAULT_DURATION

    # Body from file or string
    body = None
    if args.post_file:
        if not os.path.isfile(args.post_file):
            parser.error(f"Post file not found: {args.post_file}")
        with open(args.post_file, "rb") as f:
            body = f.read()
    elif args.body:
        body = args.body.encode()

    # Load scenario if specified
    scenario = None
    if hasattr(args, 'scenario') and args.scenario:
        scenario = load_scenario(args.scenario)

    headers = dict(args.headers)
    keepalive = not args.no_keepalive

    # Parse --tag key=value pairs
    tags: dict[str, str] = {}
    for tag_str in args.tags:
        if "=" not in tag_str:
            parser.error(f"Invalid tag format: {tag_str!r} (expected 'key=value')")
        key, value = tag_str.split("=", 1)
        tags[key.strip()] = value.strip()

    # Parse --threshold expressions
    thresholds: list[Threshold] = []
    for expr in args.thresholds:
        try:
            thresholds.append(parse_threshold(expr))
        except ValueError as e:
            parser.error(str(e))

    config = BenchmarkConfig(
        url=args.url or "",
        connections=args.connections,
        duration=duration,
        num_requests=args.num_requests,
        threads=args.threads,
        method=args.method.upper(),
        headers=headers,
        body=body,
        timeout_sec=args.timeout,
        keepalive=keepalive,
        basic_auth=args.basic_auth,
        cookies=args.cookies,
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
        traffic_profile=None,  # parsed below after validation
        scenario=scenario,
        latency_breakdown=args.latency_breakdown,
        tags=tags,
        otel_endpoint=args.otel_endpoint,
        prom_remote_write=args.prom_remote_write,
        thresholds=thresholds,
    )

    # Validate rate options
    if config.rate_ramp is not None and config.rate is None:
        parser.error("--rate-ramp requires --rate")
    if config.rate_ramp is not None and config.duration is None:
        parser.error("--rate-ramp requires -d (duration)")

    # Parse and validate traffic profile
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

    if config.scenario and config.users is None and config.duration is None and config.num_requests is None:
        config.duration = 10.0

    return config, args


def _determine_and_run_mode(config: BenchmarkConfig, args: argparse.Namespace) -> None:
    """Determine which mode to run and execute."""
    if args.url_file is not None:
        url_entries = load_url_file(args.url_file)
        results = asyncio.run(run_multi_url(url_entries, config))
        exit_code = max((r.exit_code for r in results), default=0)
        sys.exit(exit_code)
    elif args.master:
        result = asyncio.run(run_master(config, args.bind, args.port, args.expect_workers))
        if result:
            _, exit_code = result
            sys.exit(exit_code)
        sys.exit(1)
    elif args.autofind:
        keepalive = not args.no_keepalive
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
            keepalive=keepalive,
            json_output=args.json,
        )
        asyncio.run(run_autofind(af_config))
    elif config.users is not None:
        _, exit_code = asyncio.run(run_user_simulation(config))
        sys.exit(exit_code)
    else:
        _, exit_code = asyncio.run(run_benchmark(config))
        sys.exit(exit_code)


def main() -> None:
    """CLI entry point for pywrkr."""
    parser = _build_parser()
    args = parser.parse_args()
    config, args = _parse_and_validate_args(parser, args)
    _determine_and_run_mode(config, args)


if __name__ == "__main__":
    main()
