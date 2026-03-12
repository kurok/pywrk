"""Tests for reservoir sampling and bounded collections in config.py."""

import random
import statistics
import unittest

from pywrkr.config import (
    DEFAULT_MAX_ERROR_TYPES,
    DEFAULT_RESERVOIR_SIZE,
    CappedErrorDict,
    LatencyBreakdown,
    ReservoirSampler,
    WorkerStats,
)


class TestReservoirSampler(unittest.TestCase):
    """Tests for ReservoirSampler."""

    def test_below_capacity_stores_all(self):
        """When fewer items than capacity are added, all are kept."""
        rs = ReservoirSampler(capacity=100)
        for i in range(50):
            rs.append(i)
        self.assertEqual(len(rs), 50)
        self.assertEqual(rs.total_seen, 50)
        self.assertEqual(list(rs), list(range(50)))

    def test_at_capacity_no_growth(self):
        """Adding items beyond capacity does not grow the list."""
        rs = ReservoirSampler(capacity=100)
        for i in range(10_000):
            rs.append(i)
        self.assertEqual(len(rs), 100)
        self.assertEqual(rs.total_seen, 10_000)

    def test_extend(self):
        """extend() adds all items through the reservoir."""
        rs = ReservoirSampler(capacity=50)
        rs.extend(range(200))
        self.assertEqual(len(rs), 50)
        self.assertEqual(rs.total_seen, 200)

    def test_from_list(self):
        """from_list reconstructs a sampler from serialized data."""
        items = [1.0, 2.0, 3.0]
        rs = ReservoirSampler.from_list(items, capacity=100, total_seen=500)
        self.assertEqual(list(rs), [1.0, 2.0, 3.0])
        self.assertEqual(rs.total_seen, 500)
        self.assertEqual(rs.capacity, 100)

    def test_from_list_truncates(self):
        """from_list truncates items exceeding capacity."""
        items = list(range(200))
        rs = ReservoirSampler.from_list(items, capacity=50, total_seen=200)
        self.assertEqual(len(rs), 50)

    def test_sorted_works(self):
        """sorted() works on reservoir (used by percentile code)."""
        rs = ReservoirSampler(capacity=100)
        rs.extend([3, 1, 2])
        self.assertEqual(sorted(rs), [1, 2, 3])

    def test_indexing(self):
        """Indexing works as with a normal list."""
        rs = ReservoirSampler(capacity=100)
        rs.extend([10, 20, 30])
        self.assertEqual(rs[0], 10)
        self.assertEqual(rs[-1], 30)

    def test_iteration(self):
        """Iteration works as with a normal list."""
        rs = ReservoirSampler(capacity=100)
        rs.extend([1, 2, 3])
        self.assertEqual([x for x in rs], [1, 2, 3])

    def test_percentile_accuracy(self):
        """Reservoir-sampled percentiles are close to true percentiles.

        We generate a known distribution and verify that p50/p95/p99
        from a reservoir sample are within 2% of the true values.
        """
        random.seed(42)
        n = 1_000_000
        capacity = 100_000
        # Generate latencies from an exponential distribution
        true_data = [random.expovariate(1.0 / 0.05) for _ in range(n)]

        rs = ReservoirSampler(capacity=capacity)
        rs.extend(true_data)

        true_sorted = sorted(true_data)
        sample_sorted = sorted(rs)

        for pct in [0.50, 0.95, 0.99]:
            true_idx = int(pct * len(true_sorted))
            sample_idx = int(pct * len(sample_sorted))
            true_val = true_sorted[min(true_idx, len(true_sorted) - 1)]
            sample_val = sample_sorted[min(sample_idx, len(sample_sorted) - 1)]
            # Within 2% relative error
            if true_val > 0:
                rel_err = abs(sample_val - true_val) / true_val
                self.assertLess(
                    rel_err,
                    0.02,
                    f"p{int(pct * 100)}: true={true_val:.6f}, sample={sample_val:.6f}, "
                    f"rel_err={rel_err:.4f}",
                )

    def test_bool_truthiness(self):
        """Empty reservoir is falsy, non-empty is truthy."""
        rs = ReservoirSampler(capacity=10)
        self.assertFalse(rs)
        rs.append(1.0)
        self.assertTrue(rs)

    def test_len_works_with_statistics(self):
        """statistics.mean/median work on reservoir samples."""
        rs = ReservoirSampler(capacity=100)
        rs.extend([1.0, 2.0, 3.0, 4.0, 5.0])
        self.assertAlmostEqual(statistics.mean(rs), 3.0)
        self.assertAlmostEqual(statistics.median(rs), 3.0)


class TestReservoirSamplerWithBreakdowns(unittest.TestCase):
    """Test ReservoirSampler with LatencyBreakdown objects."""

    def test_breakdowns_bounded(self):
        rs = ReservoirSampler(capacity=50)
        for i in range(200):
            rs.append(LatencyBreakdown(dns=i * 0.001))
        self.assertEqual(len(rs), 50)
        self.assertEqual(rs.total_seen, 200)
        # All items are LatencyBreakdown instances
        for item in rs:
            self.assertIsInstance(item, LatencyBreakdown)


class TestCappedErrorDict(unittest.TestCase):
    """Tests for CappedErrorDict."""

    def test_normal_usage_below_cap(self):
        """Under the cap, behaves like a normal defaultdict(int)."""
        d = CappedErrorDict(max_keys=10)
        d["timeout"] += 1
        d["timeout"] += 1
        d["connection_reset"] += 1
        self.assertEqual(d["timeout"], 2)
        self.assertEqual(d["connection_reset"], 1)
        self.assertEqual(len(d), 2)

    def test_cap_redirects_new_keys(self):
        """After hitting the cap, new keys go to overflow bucket."""
        d = CappedErrorDict(max_keys=3)
        d["err1"] += 1
        d["err2"] += 1
        d["err3"] += 1
        # Now at capacity — next new key should overflow
        d["err4"] += 1
        d["err5"] += 1
        # Only 4 keys: err1, err2, err3, [other errors]
        self.assertLessEqual(len(d), 4)
        self.assertEqual(d["err1"], 1)
        self.assertEqual(d["err2"], 1)
        self.assertEqual(d["err3"], 1)
        self.assertEqual(d[CappedErrorDict._OVERFLOW_KEY], 2)

    def test_existing_keys_still_work_after_cap(self):
        """Existing keys can still be incremented after cap is reached."""
        d = CappedErrorDict(max_keys=2)
        d["err1"] += 1
        d["err2"] += 1
        # At capacity
        d["err1"] += 5
        self.assertEqual(d["err1"], 6)
        self.assertEqual(len(d), 2)

    def test_overflow_accumulates(self):
        """Multiple overflow writes accumulate correctly."""
        d = CappedErrorDict(max_keys=1)
        d["err1"] += 1
        # At capacity — all new keys overflow
        for i in range(100):
            d[f"new_err_{i}"] += 1
        self.assertEqual(d[CappedErrorDict._OVERFLOW_KEY], 100)
        self.assertEqual(d["err1"], 1)

    def test_dict_conversion(self):
        """dict(capped) works for serialization."""
        d = CappedErrorDict(max_keys=5)
        d["a"] += 1
        d["b"] += 2
        result = dict(d)
        self.assertEqual(result, {"a": 1, "b": 2})


class TestWorkerStatsDefaults(unittest.TestCase):
    """Verify WorkerStats uses bounded collections by default."""

    def test_latencies_is_reservoir(self):
        stats = WorkerStats()
        self.assertIsInstance(stats.latencies, ReservoirSampler)
        self.assertEqual(stats.latencies.capacity, DEFAULT_RESERVOIR_SIZE)

    def test_breakdowns_is_reservoir(self):
        stats = WorkerStats()
        self.assertIsInstance(stats.breakdowns, ReservoirSampler)

    def test_error_types_is_capped(self):
        stats = WorkerStats()
        self.assertIsInstance(stats.error_types, CappedErrorDict)
        self.assertEqual(stats.error_types.max_keys, DEFAULT_MAX_ERROR_TYPES)

    def test_append_and_extend_work(self):
        """The hot-path append/extend work on WorkerStats fields."""
        stats = WorkerStats()
        stats.latencies.append(0.1)
        stats.latencies.extend([0.2, 0.3])
        self.assertEqual(list(stats.latencies), [0.1, 0.2, 0.3])

        stats.breakdowns.append(LatencyBreakdown(dns=0.001))
        self.assertEqual(len(stats.breakdowns), 1)

        stats.error_types["timeout"] += 1
        self.assertEqual(stats.error_types["timeout"], 1)


if __name__ == "__main__":
    unittest.main()
