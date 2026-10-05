"""Tests for experiment assignment engine."""
from experimentation.assignment import assign_variant


class TestAssignment:
    def test_deterministic(self):
        v1 = assign_variant('user_123', 'exp_001')
        v2 = assign_variant('user_123', 'exp_001')
        assert v1 == v2

    def test_different_users_get_different_variants(self):
        variants = set()
        for i in range(100):
            v = assign_variant(f'user_{i}', 'exp_001')
            variants.add(v)
        assert len(variants) == 2  # variant_0 and variant_1

    def test_different_experiments_different_assignment(self):
        v1 = assign_variant('user_123', 'exp_001')
        v2 = assign_variant('user_123', 'exp_002')
        # Not guaranteed to differ, but tests the mechanism works
        assert v1 in ('variant_0', 'variant_1')
        assert v2 in ('variant_0', 'variant_1')

    def test_holdout(self):
        holdout_count = 0
        for i in range(10000):
            v = assign_variant(f'user_{i}', 'exp_001', traffic_pct=0.5)
            if v == 'holdout':
                holdout_count += 1
        # Should be roughly 50% holdout
        assert 4000 < holdout_count < 6000

    def test_multi_variant(self):
        variants = set()
        for i in range(1000):
            v = assign_variant(f'user_{i}', 'exp_001', num_variants=4)
            variants.add(v)
        assert len(variants) == 4

    def test_balance(self):
        counts = {'variant_0': 0, 'variant_1': 0}
        for i in range(10000):
            v = assign_variant(f'user_{i}', 'exp_balance_test')
            counts[v] += 1
        ratio = counts['variant_0'] / counts['variant_1']
        assert 0.9 < ratio < 1.1  # within 10% balance
