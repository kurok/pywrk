#!/usr/bin/env python3
"""Using pywrkr as a library.

pywrkr is pure Python, so a load test can live inside a test suite, a notebook,
or an orchestration script instead of being shelled out to. Run this directly:

    python examples/library_usage.py https://example.com
"""

import asyncio
import sys

import pywrkr


def simple(url: str) -> None:
    """The one-liner: run, then assert on typed fields."""
    result = pywrkr.run(url, connections=10, duration=5)

    print(f"  requests   {result.total_requests:,} in {result.duration:.1f}s")
    print(f"  throughput {result.requests_per_sec:,.0f} req/s")
    print(
        f"  p50/p95    {result.percentiles.p50 * 1000:.1f}ms / "
        f"{result.percentiles.p95 * 1000:.1f}ms"
    )
    print(f"  errors     {result.total_errors} ({result.error_rate:.2f}%)")

    assert result.error_rate < 5, f"too many errors: {result.error_rate:.2f}%"


def gated(url: str) -> int:
    """Thresholds come back as verdicts, so the caller decides what to do."""
    result = pywrkr.run(
        url,
        connections=20,
        duration=5,
        thresholds=["p95 < 2s", "error_rate < 1%"],
    )
    for verdict in result.thresholds:
        print(
            f"  [{'PASS' if verdict.passed else 'FAIL'}] {verdict.expression} "
            f"(actual: {verdict.actual:.4f})"
        )
    # exit_code mirrors what the CLI would return: 0, or 2 on a breach.
    return result.exit_code


def with_progress(url: str) -> None:
    """Subscribe to live stats while the run is in flight."""

    def show(stats: pywrkr.LiveStats) -> None:
        print(
            f"  ...{stats.elapsed:4.1f}s  {stats.total_requests:>7,} requests  "
            f"{stats.requests_per_sec:>8,.0f} req/s  {stats.total_errors} errors"
        )

    pywrkr.run(url, connections=10, duration=3, on_tick=show)


async def concurrent(url: str) -> None:
    """arun() is async-native, so several targets can be measured at once."""
    results = await asyncio.gather(
        pywrkr.arun(url, connections=5, duration=3),
        pywrkr.arun(url, connections=20, duration=3),
    )
    for config_label, result in zip(("5 connections", "20 connections"), results):
        print(
            f"  {config_label:>15}: {result.requests_per_sec:>9,.0f} req/s, "
            f"p95 {result.percentiles.p95 * 1000:.1f}ms"
        )


def full_control(url: str) -> None:
    """Build a Config for anything the CLI can express — scenarios included."""
    config = pywrkr.Config(
        url=url,
        users=20,
        duration=5,
        ramp_up=2,
        think_time=0.5,
        # scenario=pywrkr.load_scenario("flow.yaml"),
    )
    result = pywrkr.run(config)

    # The dict is exactly what `--json` writes, so it feeds `pywrkr compare`,
    # a dashboard, or a golden file without any translation.
    print(f"  schema_version {result.to_dict()['schema_version']}")
    print(f"  {result.total_requests:,} requests from {config.users} users")


def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else "https://example.com"
    print(f"Target: {url}\n")

    print("1. Simple run")
    simple(url)

    print("\n2. Threshold gate")
    exit_code = gated(url)

    print("\n3. Live progress")
    with_progress(url)

    print("\n4. Two configurations concurrently")
    asyncio.run(concurrent(url))

    print("\n5. Full control")
    full_control(url)

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
