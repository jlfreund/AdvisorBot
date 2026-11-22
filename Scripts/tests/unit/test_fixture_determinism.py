#!/usr/bin/env python3
"""
Generator determinism.

The whole fixture scheme rests on one property: the same spec produces byte-identical
output on any machine. If that breaks, every snapshot in the suite becomes flaky and the
drift scan becomes noise, so it is pinned here at the unit level where a failure is
unambiguous.
"""

from __future__ import annotations

from datetime import date

import pytest

from tests.generate import prices, spec as spec_mod
from tests.support import roots


@pytest.mark.feature("payload")
def test_price_series__same_seed_and_symbol__is_identical_across_calls():
    """Two independent builds of the same symbol agree to the cent."""
    plan = {"p0": 100.0, "start": "2024-01-05", "mu": 0.08, "sigma": 0.2}
    kwargs = {"spec_seed": 4242, "as_of": date(2026, 11, 16)}
    first = prices.build_series("ABC", plan, **kwargs)
    second = prices.build_series("ABC", plan, **kwargs)
    assert first == second
    assert len(first) > 100, "expected a few years of weekly closes"


@pytest.mark.feature("payload")
def test_price_series__different_symbol__diverges():
    """Per-symbol seeding means two symbols in one spec are not the same path."""
    plan = {"p0": 100.0, "start": "2024-01-05", "mu": 0.08, "sigma": 0.2}
    kwargs = {"spec_seed": 4242, "as_of": date(2026, 11, 16)}
    assert prices.build_series("ABC", plan, **kwargs) != prices.build_series("XYZ", plan, **kwargs)


@pytest.mark.feature("payload")
def test_price_series__zero_volatility__compounds_smoothly():
    """A sigma of zero gives a deterministic compounding path, for engine fixtures."""
    series = prices.build_series(
        "FLAT", {"p0": 100.0, "start": "2025-01-03", "mu": 0.10, "sigma": 0.0},
        spec_seed=1, as_of=date(2026, 1, 2),
    )
    values = [series[k] for k in sorted(series)]
    assert values[0] == pytest.approx(100.0, abs=0.01)
    assert all(b > a for a, b in zip(values, values[1:])), "a drift-only path must be monotonic"
    assert values[-1] == pytest.approx(110.0, rel=0.02), "one year at 10% drift"


@pytest.mark.feature("payload")
def test_price_on__before_first_close__falls_forward_to_the_first_known_price():
    """Lookups never return None, so a lot acquired before the series starts still prices."""
    series = {"2025-01-03": 10.0, "2025-01-10": 11.0}
    assert prices.price_on(series, date(2024, 1, 1)) == 10.0
    assert prices.price_on(series, date(2025, 1, 7)) == 10.0
    assert prices.price_on(series, date(2030, 1, 1)) == 11.0


@pytest.mark.feature("payload")
def test_spec_sha__is_stable_and_order_independent():
    """The spec hash stamped into fixtures ignores key ordering."""
    left = {"name": "p9_x", "seed": 1, "household": {"a": 1, "b": 2}}
    right = {"household": {"b": 2, "a": 1}, "seed": 1, "name": "p9_x"}
    assert spec_mod.spec_sha(left) == spec_mod.spec_sha(right)


@pytest.mark.feature("payload")
def test_every_fixture_slug__has_a_spec_and_a_generated_folder():
    """The registry, the specs on disk and the generated fixtures agree."""
    for slug in roots.FIXTURE_SLUGS:
        assert roots.spec_path(slug).is_file(), f"{slug}: no spec"
        assert (roots.PORTFOLIOS_DIR / slug).is_dir(), f"{slug}: not generated (run `make fixtures`)"
        assert (roots.PORTFOLIOS_DIR / slug / "project.json").is_file(), f"{slug}: no project.json"
        assert (roots.PORTFOLIOS_DIR / slug / "README.md").is_file(), f"{slug}: no README"
