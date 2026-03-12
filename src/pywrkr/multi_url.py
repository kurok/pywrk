"""Multi-URL testing mode for pywrkr."""

import logging
import os
import time
from dataclasses import dataclass

from pywrkr.config import BenchmarkConfig, WorkerStats
from pywrkr.reporting import (
    build_multi_url_json,
    print_multi_url_summary,
    write_json_output,
)
from pywrkr.workers import run_benchmark, run_user_simulation

logger = logging.getLogger(__name__)


@dataclass
class UrlEntry:
    """A single entry from a URL file."""

    url: str
    method: str = "GET"


def load_url_file(path: str) -> list[UrlEntry]:
    """Load URLs from a text file.

    Format (one per line):
        http://example.com/api/v1
        POST http://example.com/api/v1/data
        # comments and blank lines are ignored
    """
    if not os.path.isfile(path):
        raise FileNotFoundError(f"URL file not found: {path}")

    entries: list[UrlEntry] = []
    with open(path, "r") as f:
        for line_num, raw_line in enumerate(f, 1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(None, 1)
            if len(parts) == 2 and parts[0].upper() in (
                "GET",
                "POST",
                "PUT",
                "DELETE",
                "PATCH",
                "HEAD",
                "OPTIONS",
            ):
                entries.append(UrlEntry(url=parts[1], method=parts[0].upper()))
            elif len(parts) >= 1:
                entries.append(UrlEntry(url=parts[0]))
            else:
                raise ValueError(f"Invalid line {line_num} in URL file: {raw_line!r}")

    if not entries:
        raise ValueError(f"URL file is empty: {path}")
    return entries


@dataclass
class MultiUrlResult:
    """Result for a single URL in multi-URL mode."""

    url: str
    method: str
    stats: WorkerStats
    duration: float
    exit_code: int


async def run_multi_url(
    url_entries: list[UrlEntry],
    base_config: BenchmarkConfig,
) -> list[MultiUrlResult]:
    """Run benchmarks sequentially for each URL and collect results."""
    results: list[MultiUrlResult] = []

    for i, entry in enumerate(url_entries, 1):
        sep = "\u2500" * 70
        logger.info("\n%s", sep)
        logger.info("  Endpoint %s/%s: %s %s", i, len(url_entries), entry.method, entry.url)
        logger.info("%s\n", sep)

        # Clone config with this URL and method
        config = BenchmarkConfig(
            url=entry.url,
            connections=base_config.connections,
            duration=base_config.duration,
            num_requests=base_config.num_requests,
            threads=base_config.threads,
            method=entry.method,
            headers=dict(base_config.headers),
            body=base_config.body,
            timeout_sec=base_config.timeout_sec,
            keepalive=base_config.keepalive,
            basic_auth=base_config.basic_auth,
            cookies=list(base_config.cookies),
            verify_content_length=base_config.verify_content_length,
            verbosity=base_config.verbosity,
            random_param=base_config.random_param,
            rate=base_config.rate,
            rate_ramp=base_config.rate_ramp,
            latency_breakdown=base_config.latency_breakdown,
            users=base_config.users,
            ramp_up=base_config.ramp_up,
            think_time=base_config.think_time,
            think_time_jitter=base_config.think_time_jitter,
            thresholds=base_config.thresholds,
            tags=base_config.tags,
        )

        start = time.monotonic()
        if config.users is not None:
            stats, exit_code = await run_user_simulation(config)
        else:
            stats, exit_code = await run_benchmark(config)
        duration = time.monotonic() - start

        results.append(
            MultiUrlResult(
                url=entry.url,
                method=entry.method,
                stats=stats,
                duration=duration,
                exit_code=exit_code,
            )
        )

    # Print comparison summary
    print_multi_url_summary(results)

    # JSON output
    if base_config.json_output:
        data = build_multi_url_json(results)
        write_json_output(base_config.json_output, data)
        logger.info("  JSON results written to: %s", base_config.json_output)

    return results
