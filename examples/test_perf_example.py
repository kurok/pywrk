"""Example: performance SLOs as ordinary pytest tests.

Run the suite normally and these skip -- they put real load on the target:

    pytest examples/test_perf_example.py            # skipped
    pytest examples/test_perf_example.py --pywrkr-run

In CI, give them their own job so they do not run in parallel with anything
(including each other -- see the xdist note in the README):

    pytest -m pywrkr --pywrkr-run --pywrkr-json perf-results/

Requires `pip install pywrkr[pytest]` and a target to point at. Set the base
URL once, in pytest.ini / pyproject.toml / setup.cfg:

    [pytest]
    pywrkr_base_url = http://localhost:8080
    pywrkr_duration = 10
    pywrkr_connections = 20
"""

import pytest


@pytest.mark.pywrkr(
    url="/health",
    connections=20,
    duration=10,
    thresholds=["p95 < 200ms", "error_rate < 1%"],
)
def test_health_endpoint_meets_slo(pywrkr_result):
    """Declarative SLOs: a breach fails the test, naming metric and bound.

    The extra assertions below are ordinary Python -- anything the Result
    exposes is fair game.
    """
    assert pywrkr_result.total_requests > 0
    assert 200 in pywrkr_result.status_codes


def test_search_stays_under_budget(pywrkr_bench):
    """The fixture form, for when the assertions are the interesting part."""
    result = pywrkr_bench("/api/search?q=widget", connections=50, duration=15)

    assert result.percentiles.p95 < 0.5, f"p95 was {result.percentiles.p95 * 1000:.0f}ms"
    assert result.error_rate < 1.0
    # Throughput floors catch a regression that latency percentiles miss: a
    # server that gets slower under load often keeps p95 flat and sheds rate.
    assert result.requests_per_sec > 100


def test_write_path_survives_a_burst(pywrkr_bench):
    """Any Config field works, so the load shape is not limited to -c/-d."""
    result = pywrkr_bench(
        "/api/orders",
        method="POST",
        body=b'{"sku": "ABC-123", "qty": 1}',
        headers={"Content-Type": "application/json"},
        users=100,
        ramp_up=10,
        duration=30,
        think_time=1.0,
    )
    assert result.error_rate < 2.0
    assert result.percentiles.p99 < 2.0
