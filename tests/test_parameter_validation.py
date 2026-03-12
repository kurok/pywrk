"""Tests for parameter validation (issue #7)."""

from unittest.mock import patch

import pytest

from pywrkr.main import _build_parser, _parse_and_validate_args
from pywrkr.traffic_profiles import SineProfile, SpikeProfile, StepProfile

# ---------------------------------------------------------------------------
# Traffic profile constructor validation
# ---------------------------------------------------------------------------


class TestSineProfileValidation:
    def test_min_factor_below_zero(self):
        with pytest.raises(ValueError, match="min_factor must be between 0 and 1"):
            SineProfile(min_factor=-0.1)

    def test_min_factor_above_one(self):
        with pytest.raises(ValueError, match="min_factor must be between 0 and 1"):
            SineProfile(min_factor=1.5)

    def test_min_factor_zero_ok(self):
        p = SineProfile(min_factor=0.0)
        assert p.min_factor == 0.0

    def test_min_factor_one_ok(self):
        p = SineProfile(min_factor=1.0)
        assert p.min_factor == 1.0

    def test_min_factor_default_ok(self):
        p = SineProfile()
        assert p.min_factor == 0.1


class TestSpikeProfileValidation:
    def test_interval_zero(self):
        with pytest.raises(ValueError, match="interval must be greater than 0"):
            SpikeProfile(interval=0)

    def test_interval_negative(self):
        with pytest.raises(ValueError, match="interval must be greater than 0"):
            SpikeProfile(interval=-5)

    def test_interval_positive_ok(self):
        p = SpikeProfile(interval=0.1)
        assert p.interval == 0.1


class TestStepProfileValidation:
    def test_negative_level(self):
        with pytest.raises(ValueError, match="levels must be non-negative"):
            StepProfile(levels=[100, -50, 200])

    def test_zero_level_ok(self):
        p = StepProfile(levels=[0, 100, 200])
        assert p.levels == [0, 100, 200]

    def test_all_positive_ok(self):
        p = StepProfile(levels=[100, 500, 1000])
        assert p.levels == [100, 500, 1000]

    def test_empty_levels(self):
        with pytest.raises(ValueError, match="at least one level"):
            StepProfile(levels=[])


# ---------------------------------------------------------------------------
# CLI argument validation (via _parse_and_validate_args)
# ---------------------------------------------------------------------------


def _parse(cli_args: list[str]):
    """Helper: parse CLI args and run validation, returning (config, args)."""
    parser = _build_parser()
    args = parser.parse_args(cli_args)
    return _parse_and_validate_args(parser, args)


class TestRateValidation:
    def test_rate_zero(self):
        with pytest.raises(SystemExit):
            _parse(["--rate", "0", "-d", "10", "http://localhost/"])

    def test_rate_negative(self):
        with pytest.raises(SystemExit):
            _parse(["--rate", "-1", "-d", "10", "http://localhost/"])

    def test_rate_positive_ok(self):
        config, _ = _parse(["--rate", "100", "-d", "10", "http://localhost/"])
        assert config.rate == 100


class TestRampUpValidation:
    def test_ramp_up_exceeds_duration(self):
        with pytest.raises(SystemExit):
            _parse(["-u", "10", "-d", "30", "--ramp-up", "30", "http://localhost/"])

    def test_ramp_up_exceeds_duration_greater(self):
        with pytest.raises(SystemExit):
            _parse(["-u", "10", "-d", "30", "--ramp-up", "60", "http://localhost/"])

    def test_ramp_up_less_than_duration_ok(self):
        config, _ = _parse(["-u", "10", "-d", "30", "--ramp-up", "10", "http://localhost/"])
        assert config.ramp_up == 10


class TestAutofindValidation:
    def test_max_users_less_than_start_users(self):
        with pytest.raises(SystemExit):
            _parse(["--autofind", "--start-users", "100", "--max-users", "50", "http://localhost/"])

    def test_max_users_equal_start_users(self):
        with pytest.raises(SystemExit):
            _parse(
                ["--autofind", "--start-users", "100", "--max-users", "100", "http://localhost/"]
            )

    def test_step_multiplier_one(self):
        with pytest.raises(SystemExit):
            _parse(["--autofind", "--step-multiplier", "1.0", "http://localhost/"])

    def test_step_multiplier_below_one(self):
        with pytest.raises(SystemExit):
            _parse(["--autofind", "--step-multiplier", "0.5", "http://localhost/"])

    def test_valid_autofind_ok(self):
        # Should not raise -- but autofind actually runs, so we just test
        # that validation passes by checking no SystemExit before run_autofind.
        # We mock run_autofind to prevent actual execution.
        with patch("pywrkr.main.run_autofind"):
            config, args = _parse(
                [
                    "--autofind",
                    "--start-users",
                    "10",
                    "--max-users",
                    "100",
                    "--step-multiplier",
                    "2.0",
                    "http://localhost/",
                ]
            )
            assert args.start_users == 10
            assert args.max_users == 100
            assert args.step_multiplier == 2.0
