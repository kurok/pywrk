"""Traffic shaping profiles and rate limiter for pywrkr."""

from __future__ import annotations

import asyncio
import csv
import math
import time

# ---------------------------------------------------------------------------
# Traffic profiles -- advanced traffic shaping
# ---------------------------------------------------------------------------


class TrafficProfile:
    """Base class for traffic shaping profiles.

    A profile maps (elapsed_seconds, total_duration, base_rate) -> target_rate.
    """

    name: str = "custom"

    def rate_at(self, elapsed: float, duration: float, base_rate: float) -> float:
        """Return the target RPS at *elapsed* seconds into a test of *duration*."""
        raise NotImplementedError

    def describe(self) -> str:
        """Return a human-readable description of this profile."""
        return self.name


class SineProfile(TrafficProfile):
    """Sinusoidal oscillation between *min_factor* and 1.0 of base_rate.

    Parameters:
        cycles  -- number of full sine cycles over the test duration (default 2)
        min     -- minimum rate as a fraction of base_rate (default 0.1)
    """

    name = "sine"

    def __init__(self, cycles: float = 2.0, min_factor: float = 0.1):
        if not (0 <= min_factor <= 1):
            raise ValueError(f"SineProfile min_factor must be between 0 and 1, got {min_factor}")
        self.cycles = cycles
        self.min_factor = min_factor

    def rate_at(self, elapsed: float, duration: float, base_rate: float) -> float:
        if duration <= 0:
            return base_rate
        progress = elapsed / duration
        # oscillate between min_factor and 1.0
        amplitude = (1.0 - self.min_factor) / 2.0
        mid = self.min_factor + amplitude
        factor = mid + amplitude * math.sin(2 * math.pi * self.cycles * progress)
        return base_rate * factor

    def describe(self) -> str:
        return f"sine (cycles={self.cycles}, min={self.min_factor:.0%})"


class StepProfile(TrafficProfile):
    """Step function -- hold each level for an equal fraction of duration.

    Parameters:
        levels -- comma-separated RPS values (e.g. "100,500,1000,200")
    """

    name = "step"

    def __init__(self, levels: list[float]):
        if not levels:
            raise ValueError("step profile requires at least one level")
        for i, level in enumerate(levels):
            if level < 0:
                raise ValueError(
                    f"StepProfile levels must be non-negative, got {level} at index {i}"
                )
        self.levels = levels

    def rate_at(self, elapsed: float, duration: float, base_rate: float) -> float:
        if duration <= 0:
            return self.levels[0]
        n = len(self.levels)
        idx = min(int(elapsed / duration * n), n - 1)
        return self.levels[idx]

    def describe(self) -> str:
        return f"step (levels={','.join(f'{lv:.0f}' for lv in self.levels)})"


class SawtoothProfile(TrafficProfile):
    """Repeating linear ramp from *min_factor* to 1.0, then reset.

    Parameters:
        cycles -- number of sawtooth cycles (default 3)
        min    -- minimum rate fraction (default 0.1)
    """

    name = "sawtooth"

    def __init__(self, cycles: float = 3.0, min_factor: float = 0.1):
        self.cycles = cycles
        self.min_factor = min_factor

    def rate_at(self, elapsed: float, duration: float, base_rate: float) -> float:
        if duration <= 0:
            return base_rate
        cycle_pos = (elapsed / duration * self.cycles) % 1.0
        factor = self.min_factor + (1.0 - self.min_factor) * cycle_pos
        return base_rate * factor

    def describe(self) -> str:
        return f"sawtooth (cycles={self.cycles}, min={self.min_factor:.0%})"


class SquareProfile(TrafficProfile):
    """Alternates between base_rate and base_rate * *low_factor*.

    Parameters:
        cycles -- number of on/off cycles (default 3)
        low    -- low-phase rate fraction (default 0.2)
    """

    name = "square"

    def __init__(self, cycles: float = 3.0, low_factor: float = 0.2):
        self.cycles = cycles
        self.low_factor = low_factor

    def rate_at(self, elapsed: float, duration: float, base_rate: float) -> float:
        if duration <= 0:
            return base_rate
        cycle_pos = (elapsed / duration * self.cycles) % 1.0
        return base_rate if cycle_pos < 0.5 else base_rate * self.low_factor

    def describe(self) -> str:
        return f"square (cycles={self.cycles}, low={self.low_factor:.0%})"


class SpikeProfile(TrafficProfile):
    """Baseline rate with periodic sharp spikes.

    Parameters:
        interval  -- seconds between spike starts (default 10)
        spike_dur -- duration of each spike in seconds (default 2)
        multiplier -- spike rate = base_rate * multiplier (default 5)
        baseline   -- baseline rate fraction between spikes (default 0.2)
    """

    name = "spike"

    def __init__(
        self,
        interval: float = 10.0,
        spike_dur: float = 2.0,
        multiplier: float = 5.0,
        baseline: float = 0.2,
    ):
        if interval <= 0:
            raise ValueError(f"SpikeProfile interval must be greater than 0, got {interval}")
        self.interval = interval
        self.spike_dur = spike_dur
        self.multiplier = multiplier
        self.baseline = baseline

    def rate_at(self, elapsed: float, duration: float, base_rate: float) -> float:
        if self.interval <= 0:
            return base_rate * self.baseline
        phase = elapsed % self.interval
        if phase < self.spike_dur:
            return base_rate * self.multiplier
        return base_rate * self.baseline

    def describe(self) -> str:
        return (
            f"spike (interval={self.interval}s, dur={self.spike_dur}s, "
            f"x{self.multiplier}, baseline={self.baseline:.0%})"
        )


class BusinessHoursProfile(TrafficProfile):
    """Simulates a 24-hour business day traffic curve compressed into test duration.

    The curve peaks at "midday" (50% through) and has low traffic at
    "night" (start and end).
    """

    name = "business-hours"

    def rate_at(self, elapsed: float, duration: float, base_rate: float) -> float:
        if duration <= 0:
            return base_rate
        # Map elapsed to 0..24 "hours"
        hour = (elapsed / duration) * 24.0
        # Piecewise model: ramp up 6-9, peak 9-17, ramp down 17-21, low 21-6
        if hour < 6:
            factor = 0.05
        elif hour < 9:
            factor = 0.05 + 0.95 * ((hour - 6) / 3.0)
        elif hour < 17:
            # Slight midday dip
            mid_progress = (hour - 9) / 8.0
            factor = 1.0 - 0.15 * math.sin(math.pi * mid_progress)
        elif hour < 21:
            factor = 1.0 - 0.95 * ((hour - 17) / 4.0)
        else:
            factor = 0.05
        return base_rate * factor

    def describe(self) -> str:
        return "business-hours (24h curve compressed to test duration)"


class CsvProfile(TrafficProfile):
    """Replay a traffic curve from a CSV file.

    The CSV must have at least two columns. The first row is treated as a header
    if the first cell is non-numeric.

    Supported column layouts (auto-detected):
        time_sec, rate        -- absolute RPS at each time offset
        time_sec, multiplier  -- factor applied to base_rate (if header says
                                 "multiplier" or "factor")

    Between data points the rate is linearly interpolated.  Before the first
    point and after the last, the nearest value is held.
    """

    name = "csv"

    def __init__(self, filepath: str):
        self.filepath = filepath
        self._times: list[float] = []
        self._values: list[float] = []
        self._is_multiplier: bool = False
        self._load(filepath)

    def _load(self, filepath: str) -> None:
        """Load and parse CSV data points from file."""
        with open(filepath, newline="") as f:
            reader = csv.reader(f)
            rows = list(reader)
        if not rows:
            raise ValueError(f"CSV traffic profile is empty: {filepath}")

        # Detect header
        start = 0
        try:
            float(rows[0][0])
        except (ValueError, IndexError):
            header = [c.strip().lower() for c in rows[0]]
            self._is_multiplier = any(h in ("multiplier", "factor") for h in header)
            start = 1

        for row in rows[start:]:
            if len(row) < 2:
                continue
            t, v = float(row[0]), float(row[1])
            self._times.append(t)
            self._values.append(v)

        if not self._times:
            raise ValueError(f"No data points in CSV traffic profile: {filepath}")

    def rate_at(self, elapsed: float, duration: float, base_rate: float) -> float:
        times, values = self._times, self._values
        # Clamp to range
        if elapsed <= times[0]:
            raw = values[0]
        elif elapsed >= times[-1]:
            raw = values[-1]
        else:
            # Binary search for surrounding points
            lo, hi = 0, len(times) - 1
            while lo < hi - 1:
                mid = (lo + hi) // 2
                if times[mid] <= elapsed:
                    lo = mid
                else:
                    hi = mid
            # Linear interpolation
            t0, t1 = times[lo], times[hi]
            v0, v1 = values[lo], values[hi]
            frac = (elapsed - t0) / (t1 - t0) if t1 != t0 else 0
            raw = v0 + (v1 - v0) * frac

        return base_rate * raw if self._is_multiplier else raw

    def describe(self) -> str:
        mode = "multiplier" if self._is_multiplier else "absolute RPS"
        return f"csv ({self.filepath}, {len(self._times)} points, {mode})"


# Built-in profile registry
_BUILTIN_PROFILES: dict[str, type[TrafficProfile]] = {
    "sine": SineProfile,
    "step": StepProfile,
    "sawtooth": SawtoothProfile,
    "square": SquareProfile,
    "spike": SpikeProfile,
    "business-hours": BusinessHoursProfile,
}


def parse_traffic_profile(spec: str) -> TrafficProfile:
    """Parse a --traffic-profile specification string.

    Formats:
        sine                           -- built-in with defaults
        sine:cycles=4,min=0.2          -- built-in with parameters
        csv:path/to/file.csv           -- CSV replay
        step:levels=100,500,1000,200   -- step with explicit RPS levels
    """
    # Split name:params
    if ":" in spec:
        name, params_str = spec.split(":", 1)
    else:
        name, params_str = spec, ""

    name = name.strip().lower()

    # CSV is special: everything after "csv:" is the filepath
    if name == "csv":
        if not params_str:
            raise ValueError("csv profile requires a file path: csv:path/to/file.csv")
        return CsvProfile(params_str.strip())

    if name not in _BUILTIN_PROFILES:
        available = ", ".join(sorted(list(_BUILTIN_PROFILES.keys()) + ["csv"]))
        raise ValueError(f"Unknown traffic profile: {name!r}. Available: {available}")

    # Parse key=value params
    kwargs: dict[str, str] = {}
    if params_str:
        for part in params_str.split(","):
            if "=" in part:
                k, v = part.split("=", 1)
                kwargs[k.strip()] = v.strip()
            else:
                kwargs[part.strip()] = ""

    cls = _BUILTIN_PROFILES[name]

    if name == "step":
        # Step profile expects levels=100,500,... but levels values are
        # already split by commas, so we handle it specially.
        levels_str = params_str  # the whole params string is the levels list
        # But if it starts with "levels=", strip that prefix
        if levels_str.lower().startswith("levels="):
            levels_str = levels_str[7:]
        levels = [float(x.strip()) for x in levels_str.split(",") if x.strip()]
        return StepProfile(levels=levels)

    # Map string params to constructor types
    typed_kwargs: dict[str, float] = {}
    param_aliases = {"min": "min_factor", "low": "low_factor"}
    for k, v in kwargs.items():
        key = param_aliases.get(k, k)
        try:
            typed_kwargs[key] = float(v)
        except ValueError:
            raise ValueError(f"Invalid parameter for {name} profile: {k}={v}")

    return cls(**typed_kwargs)


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------


class RateLimiter:
    """Token-bucket-style rate limiter that distributes requests evenly over time.

    Supports a fixed rate, a linear ramp from *start_rate* to *end_rate*
    over *ramp_duration* seconds, or an arbitrary *traffic_profile*.
    """

    def __init__(
        self,
        rate: float,
        end_rate: float | None = None,
        ramp_duration: float | None = None,
        traffic_profile: TrafficProfile | None = None,
        duration: float | None = None,
    ):
        self.start_rate = rate
        self.end_rate = end_rate
        self.ramp_duration = ramp_duration
        self.traffic_profile = traffic_profile
        self.duration = duration or 0.0
        self._start_time: float | None = None
        self._last_time: float = 0.0
        self.waits: int = 0  # how many times we actually slept

    def _current_rate(self, now: float) -> float:
        """Return the target RPS at the given monotonic time.

        Priority: traffic_profile > linear ramp > fixed start_rate.
        """
        # Traffic profile takes precedence
        if self.traffic_profile is not None:
            if self._start_time is None:
                return self.start_rate
            elapsed = now - self._start_time
            return self.traffic_profile.rate_at(elapsed, self.duration, self.start_rate)
        # Linear ramp fallback
        if self.end_rate is not None and self.ramp_duration and self.ramp_duration > 0:
            if self._start_time is None:
                return self.start_rate
            elapsed = now - self._start_time
            progress = min(elapsed / self.ramp_duration, 1.0)
            return self.start_rate + (self.end_rate - self.start_rate) * progress
        return self.start_rate

    async def acquire(self) -> None:
        """Wait until the next request is allowed under the rate limit.

        Uses a token bucket approach: calculates the required interval between
        requests based on the current rate, and sleeps if the next request
        would arrive too early.

        Since asyncio runs in a single thread, state updates between await
        points are atomic -- no lock is needed.  The scheduled send time
        (``_last_time``) is advanced *before* sleeping so that concurrent
        coroutines each claim their own time slot without contention.
        """
        now = time.monotonic()
        if self._start_time is None:
            self._start_time = now
            self._last_time = now
            return

        rate = self._current_rate(now)
        if rate <= 0:
            return
        interval = 1.0 / rate
        target = self._last_time + interval
        # Reserve our slot immediately so the next caller gets the
        # slot after ours -- this is safe without a lock because
        # there is no await between the read and the write.
        self._last_time = max(target, now)
        wait = target - now
        if wait > 0:
            self.waits += 1
            await asyncio.sleep(wait)
