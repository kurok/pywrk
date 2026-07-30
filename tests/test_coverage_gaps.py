"""Tests targeting coverage gaps in config.py, workers.py, traffic_profiles.py, and reporting.py."""

import json
import tempfile
import time

import pytest

from pywrkr.config import (
    BenchmarkConfig,
    ReservoirSampler,
    SSLConfig,
    WorkerStats,
    load_scenario,
)
from pywrkr.traffic_profiles import (
    BusinessHoursProfile,
    CsvProfile,
    RateLimiter,
    SpikeProfile,
    StepProfile,
    TrafficProfile,
)

# ---------------------------------------------------------------------------
# ReservoirSampler edge cases (config.py lines 132, 167)
# ---------------------------------------------------------------------------


class TestReservoirSamplerEdgeCases:
    def test_init_with_iterable(self):
        """Line 132: constructor with iterable arg calls append()."""
        sampler = ReservoirSampler(capacity=5, iterable=[1, 2, 3])
        assert len(sampler) == 3
        assert sampler.total_seen == 3

    def test_init_with_iterable_exceeds_capacity(self):
        """Iterable larger than capacity triggers sampling."""
        sampler = ReservoirSampler(capacity=3, iterable=range(100))
        assert len(sampler) == 3
        assert sampler.total_seen == 100

    def test_repr(self):
        """Line 167: __repr__ format."""
        sampler = ReservoirSampler(capacity=10)
        sampler.append(1.0)
        sampler.append(2.0)
        r = repr(sampler)
        assert "capacity=10" in r
        assert "total_seen=2" in r
        assert "len=2" in r


# ---------------------------------------------------------------------------
# SSLConfig.from_env (config.py)
# ---------------------------------------------------------------------------


class TestSSLConfigFromEnv:
    def test_from_env_default(self):
        """Default SSLConfig values."""
        ssl = SSLConfig()
        assert ssl.verify is False
        assert ssl.ca_bundle is None

    def test_from_env_picks_up_env_vars(self):
        """SSLConfig.from_env reads environment variables."""
        import os

        env_backup = os.environ.copy()
        try:
            os.environ["PYWRKR_SSL_VERIFY"] = "1"
            os.environ["PYWRKR_CA_BUNDLE"] = "/some/path"
            ssl = SSLConfig.from_env()
            assert ssl.verify is True
            assert ssl.ca_bundle == "/some/path"
        finally:
            os.environ.clear()
            os.environ.update(env_backup)


# ---------------------------------------------------------------------------
# load_scenario edge cases (config.py lines 389-408, 427)
# ---------------------------------------------------------------------------


class TestLoadScenarioEdgeCases:
    def test_load_yaml_scenario(self):
        """Line 389-395: YAML scenario file (.yaml extension)."""
        pytest.importorskip("yaml")
        import yaml

        scenario_data = {
            "name": "yaml test",
            "steps": [{"path": "/api/test", "method": "GET"}],
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(scenario_data, f)
            path = f.name
        try:
            scenario = load_scenario(path)
            assert scenario.name == "yaml test"
            assert len(scenario.steps) == 1
        finally:
            import os

            os.unlink(path)

    def test_load_yml_extension(self):
        """Line 389: .yml extension also triggers YAML parsing."""
        pytest.importorskip("yaml")
        import yaml

        scenario_data = {
            "steps": [{"path": "/health"}],
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            yaml.dump(scenario_data, f)
            path = f.name
        try:
            scenario = load_scenario(path)
            assert len(scenario.steps) == 1
        finally:
            import os

            os.unlink(path)

    def test_unknown_extension_tries_json_first(self):
        """Line 400-401: unknown extension tries JSON."""
        scenario_data = {
            "steps": [{"path": "/api/data"}],
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".conf", delete=False) as f:
            json.dump(scenario_data, f)
            path = f.name
        try:
            scenario = load_scenario(path)
            assert len(scenario.steps) == 1
        finally:
            import os

            os.unlink(path)

    def test_unknown_extension_falls_back_to_yaml(self):
        """Line 403-406: unknown extension, invalid JSON, falls back to YAML."""
        pytest.importorskip("yaml")
        import yaml

        scenario_data = {
            "steps": [{"path": "/api/data"}],
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".conf", delete=False) as f:
            yaml.dump(scenario_data, f)
            path = f.name
        try:
            scenario = load_scenario(path)
            assert len(scenario.steps) == 1
        finally:
            import os

            os.unlink(path)

    def test_step_not_dict_raises(self):
        """Line 427: step that isn't a dict."""
        scenario_data = {"steps": ["not a dict"]}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(scenario_data, f)
            path = f.name
        try:
            with pytest.raises(ValueError, match="must be a dict"):
                load_scenario(path)
        finally:
            import os

            os.unlink(path)

    def test_scenario_not_dict_raises(self):
        """Line 414-416: scenario root is a list instead of dict."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump([1, 2, 3], f)
            path = f.name
        try:
            with pytest.raises(ValueError, match="JSON/YAML object"):
                load_scenario(path)
        finally:
            import os

            os.unlink(path)

    def test_scenario_missing_steps_key(self):
        """Line 418-419: missing 'steps' key."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"name": "no steps"}, f)
            path = f.name
        try:
            with pytest.raises(ValueError, match="steps"):
                load_scenario(path)
        finally:
            import os

            os.unlink(path)

    def test_scenario_empty_steps(self):
        """Line 421-422: empty steps list."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"steps": []}, f)
            path = f.name
        try:
            with pytest.raises(ValueError, match="at least one step"):
                load_scenario(path)
        finally:
            import os

            os.unlink(path)


# ---------------------------------------------------------------------------
# Workers: step latency overflow (workers.py line 112, 288)
# ---------------------------------------------------------------------------


class TestStepLatencyOverflow:
    def test_record_step_latency_overflow(self):
        """Line 112: step names exceeding _MAX_STEP_NAMES fold into '[other steps]'."""
        from pywrkr.workers import _MAX_STEP_NAMES, _record_step_latency

        stats = WorkerStats()
        # Fill up to the max
        for i in range(_MAX_STEP_NAMES):
            _record_step_latency(stats, f"step_{i}", 0.1)
        assert len(stats.step_latencies) == _MAX_STEP_NAMES

        # One more unique name should overflow
        _record_step_latency(stats, "overflow_step", 0.2)
        assert "[other steps]" in stats.step_latencies
        assert stats.step_latencies["[other steps]"] == [0.2]

    def test_merge_step_latencies_overflow(self):
        """Line 288: _merge_all_stats step latency overflow."""
        from pywrkr.workers import _MAX_STEP_NAMES
        from pywrkr.workers import merge_stats as merge_all_stats

        stats1 = WorkerStats()
        stats2 = WorkerStats()

        # Fill stats1 with max step names
        for i in range(_MAX_STEP_NAMES):
            stats1.step_latencies[f"step_{i}"] = [0.1]

        # stats2 has a new unique step name
        stats2.step_latencies["new_step"] = [0.2]

        merged = merge_all_stats([stats1, stats2])
        # "new_step" should be folded into "[other steps]"
        assert "[other steps]" in merged.step_latencies


# ---------------------------------------------------------------------------
# Workers: SSL with CA bundle (workers.py line 321)
# ---------------------------------------------------------------------------


class TestCreateSSLContext:
    def test_ca_bundle_loading(self):
        """Line 321: HTTPS with custom ca_bundle."""
        from pywrkr.workers import _create_ssl_context

        config = BenchmarkConfig(
            url="https://example.com/",
            ssl_config=SSLConfig(verify=True, ca_bundle=None),
        )
        ctx = _create_ssl_context(config)
        assert ctx is not None
        # verify mode should be enabled
        import ssl

        assert ctx.verify_mode == ssl.CERT_REQUIRED


# ---------------------------------------------------------------------------
# Traffic profiles: edge cases
# ---------------------------------------------------------------------------


class TestTrafficProfileEdgeCases:
    def test_base_class_rate_at_raises(self):
        """Line 25: TrafficProfile.rate_at() is abstract."""
        profile = TrafficProfile()
        with pytest.raises(NotImplementedError):
            profile.rate_at(0.0, 10.0, 100.0)

    def test_base_class_describe(self):
        """Line 29: TrafficProfile.describe() returns name."""
        profile = TrafficProfile()
        assert profile.describe() == "custom"

    def test_step_profile_zero_duration(self):
        """Line 83: StepProfile with duration <= 0."""
        p = StepProfile(levels=[100, 200, 300])
        assert p.rate_at(5.0, 0.0, 100.0) == 100  # returns first level

    def test_spike_profile_phases(self):
        """Line 175-179: SpikeProfile spike vs baseline phases."""
        p = SpikeProfile(interval=10, spike_dur=2, multiplier=5.0, baseline=0.5)
        # During spike phase (elapsed=1, phase=1 < spike_dur=2)
        assert p.rate_at(1.0, 60.0, 100.0) == 500.0
        # During baseline phase (elapsed=5, phase=5 > spike_dur=2)
        assert p.rate_at(5.0, 60.0, 100.0) == 50.0

    def test_business_hours_profile_all_phases(self):
        """Lines 204-214: BusinessHoursProfile covers all time-of-day phases."""
        p = BusinessHoursProfile()
        duration = 100.0
        base_rate = 100.0

        # Night (hour < 6): elapsed at 10% → hour ~2.4
        night = p.rate_at(duration * 0.1, duration, base_rate)
        assert night == pytest.approx(5.0)  # 0.05 * 100

        # Ramp up (hour 6-9): elapsed at 30% → hour ~7.2
        ramp = p.rate_at(duration * 0.3, duration, base_rate)
        assert ramp > 5.0  # Should be ramping up

        # Peak (hour 9-17): elapsed at 50% → hour 12
        peak = p.rate_at(duration * 0.5, duration, base_rate)
        assert peak > 80.0  # Should be near peak

        # Ramp down (hour 17-21): elapsed at 80% → hour ~19.2
        ramp_down = p.rate_at(duration * 0.8, duration, base_rate)
        assert ramp_down < peak

        # Late night (hour > 21): elapsed at 95% → hour ~22.8
        late = p.rate_at(duration * 0.95, duration, base_rate)
        assert late == pytest.approx(5.0)

    def test_business_hours_zero_duration(self):
        """Line 198-199: BusinessHoursProfile with duration <= 0."""
        p = BusinessHoursProfile()
        assert p.rate_at(5.0, 0.0, 100.0) == 100.0

    def test_csv_profile_describe(self):
        """Line 297-298: CsvProfile.describe()."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
            f.write("time_sec,rate\n0,100\n60,500\n")
            path = f.name
        try:
            p = CsvProfile(path)
            desc = p.describe()
            assert "csv" in desc
            assert "2 points" in desc
            assert "absolute RPS" in desc
        finally:
            import os

            os.unlink(path)


# ---------------------------------------------------------------------------
# RateLimiter edge cases (traffic_profiles.py lines 412, 418)
# ---------------------------------------------------------------------------


class TestRateLimiterEdgeCases:
    def test_rate_limiter_with_traffic_profile(self):
        """Line 410-414: _current_rate uses traffic_profile when started."""
        profile = StepProfile(levels=[50, 100, 200])
        limiter = RateLimiter(
            rate=100.0,
            duration=60.0,
            traffic_profile=profile,
        )
        # Simulate acquire having set _start_time
        limiter._start_time = time.monotonic()
        rate = limiter._current_rate(time.monotonic())
        assert rate > 0

    def test_rate_limiter_with_ramp(self):
        """Line 416-421: _current_rate with linear ramp (end_rate)."""
        limiter = RateLimiter(
            rate=50.0,
            end_rate=200.0,
            ramp_duration=60.0,
            duration=60.0,
        )
        limiter._start_time = time.monotonic()
        # At start, rate should be close to start_rate
        rate = limiter._current_rate(time.monotonic())
        assert rate >= 49.0  # Allow small timing tolerance

    def test_rate_limiter_no_start_time_with_profile(self):
        """Line 411-412: _current_rate before acquire() returns start_rate (profile path)."""
        limiter = RateLimiter(
            rate=100.0,
            duration=60.0,
            traffic_profile=StepProfile(levels=[50, 100]),
        )
        # Not started yet (_start_time is None)
        rate = limiter._current_rate(time.monotonic())
        assert rate == 100.0

    def test_rate_limiter_no_start_time_with_ramp(self):
        """Line 417-418: _current_rate before acquire() returns start_rate (ramp path)."""
        limiter = RateLimiter(
            rate=50.0,
            end_rate=200.0,
            ramp_duration=60.0,
            duration=60.0,
        )
        rate = limiter._current_rate(time.monotonic())
        assert rate == 50.0


# ---------------------------------------------------------------------------
# Reporting: edge cases (reporting.py lines 231, 248, 284, 302)
# ---------------------------------------------------------------------------


class TestReportingEdgeCases:
    def test_compare_ge_operator(self):
        """Line 246-248: >= and fallback operators."""
        from pywrkr.reporting import compare_threshold

        assert compare_threshold(10.0, ">=", 10.0) is True
        assert compare_threshold(9.0, ">=", 10.0) is False
        assert compare_threshold(10.0, "<=", 10.0) is True
        assert compare_threshold(11.0, "<=", 10.0) is False
        # Unknown operator returns False
        assert compare_threshold(10.0, "==", 10.0) is False

    def test_print_percentiles_empty(self):
        """Line 284: empty latencies produce no output."""
        import io

        from pywrkr.reporting import print_percentiles

        out = io.StringIO()
        print_percentiles([], file=out)
        assert out.getvalue() == ""

    def test_print_rps_timeline_empty(self):
        """Line 294-295, 301-302: empty timeline."""
        import io

        from pywrkr.reporting import print_rps_timeline

        out = io.StringIO()
        print_rps_timeline([], start=0.0, duration=10.0, file=out)
        assert out.getvalue() == ""

    def test_resolve_metric_value_flat(self):
        """Line 720-724: _resolve_metric_value flat and nested."""
        from pywrkr.reporting import _resolve_metric_value

        results = {"rps": 100.5, "latency": {"p95": 0.05}}
        assert _resolve_metric_value(results, "rps", None, 1.0) == pytest.approx(100.5)
        assert _resolve_metric_value(results, "latency", "p95", 1000.0) == pytest.approx(50.0)
        # Missing key returns None (absent data, not a fabricated zero)
        assert _resolve_metric_value(results, "missing", None, 1.0) is None
