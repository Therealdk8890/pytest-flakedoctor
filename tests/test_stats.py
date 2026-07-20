from __future__ import annotations

import pytest

from flakedoctor._stats import (
    ALWAYS,
    MIXED,
    NEVER,
    binom_tail_ge,
    category,
    clopper_pearson_lower,
    detection_probability,
    elevation_pvalue,
)


def test_category():
    assert category(5, 5) == ALWAYS
    assert category(0, 5) == NEVER
    assert category(2, 5) == MIXED
    with pytest.raises(ValueError):
        category(0, 0)


def test_binom_tail_edges():
    assert binom_tail_ge(0, 10, 0.3) == 1.0
    assert binom_tail_ge(11, 10, 0.3) == 0.0
    assert binom_tail_ge(10, 10, 0.5) == pytest.approx(0.5**10)


def test_clopper_pearson_all_failures_matches_closed_form():
    # For k == n the bound is alpha ** (1/n): 10/10 at 95% -> ~0.7411.
    assert clopper_pearson_lower(10, 10) == pytest.approx(0.05 ** (1 / 10), abs=1e-9)
    assert clopper_pearson_lower(10, 10) == pytest.approx(0.7411, abs=1e-3)
    assert clopper_pearson_lower(20, 20) == pytest.approx(0.05 ** (1 / 20), abs=1e-9)


def test_clopper_pearson_zero_failures():
    assert clopper_pearson_lower(0, 10) == 0.0


def test_clopper_pearson_partial_is_consistent_with_tail():
    lower = clopper_pearson_lower(9, 10)
    # At the bound, P(X >= k) should equal alpha.
    assert binom_tail_ge(9, 10, lower) == pytest.approx(0.05, abs=1e-6)
    assert 0.0 < lower < clopper_pearson_lower(10, 10)


def test_clopper_pearson_monotone_in_k():
    bounds = [clopper_pearson_lower(k, 10) for k in range(11)]
    assert bounds == sorted(bounds)


def test_clopper_pearson_input_validation():
    with pytest.raises(ValueError):
        clopper_pearson_lower(5, 0)
    with pytest.raises(ValueError):
        clopper_pearson_lower(11, 10)


def test_detection_probability():
    assert detection_probability(0.26, 10) == pytest.approx(0.951, abs=0.005)
    assert detection_probability(0.0, 10) == 0.0


def test_elevation_pvalue_rejects_coinflip_streaks():
    # Baseline 5/10 (a 50/50 flake); verify 5/10 is no elevation at all.
    assert elevation_pvalue(5, 10, 5, 10) > 0.05
    # Even 7/10 is well within what a 50% rate produces by chance.
    assert elevation_pvalue(7, 10, 5, 10) > 0.05


def test_elevation_pvalue_accepts_real_elevation():
    # Baseline 2/10, verify 9/10 — decisive.
    assert elevation_pvalue(9, 10, 2, 10) < 0.001
    assert elevation_pvalue(10, 10, 3, 10) < 0.001


def test_elevation_pvalue_uses_upper_bound_for_clean_baseline():
    # A clean 0/10 baseline does NOT mean the true rate is 0, so a single
    # failure must not be decisive...
    assert elevation_pvalue(1, 2, 0, 10) > 0.05
    # ...but 8/10 against a clean baseline is.
    assert elevation_pvalue(8, 10, 0, 10) < 0.05


def test_elevation_pvalue_degenerate_inputs():
    assert elevation_pvalue(0, 0, 0, 10) == 1.0
    assert elevation_pvalue(5, 10, 0, 0) == 1.0
