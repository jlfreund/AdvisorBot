#!/usr/bin/env python3
"""
Documentation freshness.

`CASES.md` is generated from the test docstrings and markers, so it can go stale the
moment a test is added. These checks keep the documentation honest without anyone having
to remember to regenerate it.
"""

from __future__ import annotations

import pytest

from tests.support import roots


@pytest.mark.feature("payload")
def test_cases_catalogue__is_up_to_date():
    """
    `CASES.md` matches the tests on disk.

    Exercises: the catalogue is the suite's index. If it drifts, the documentation claims
    coverage that may not exist.
    """
    from tests import catalog

    expected = catalog.render(catalog.collect())
    actual = catalog.CASES_PATH.read_text(encoding="utf-8") if catalog.CASES_PATH.is_file() else ""
    assert actual == expected, "CASES.md is stale. Run: make fixtures"


@pytest.mark.feature("payload")
def test_cases_catalogue__parses_every_test_file():
    """No test file is skipped by the catalogue because of a syntax error."""
    from tests import catalog

    broken = [c["error"] for c in catalog.collect() if "error" in c]
    assert not broken, "files the catalogue could not parse:\n" + "\n".join(broken)


@pytest.mark.feature("payload")
def test_readme__documents_every_tier_and_marker():
    """
    `README.md` names every tier and cross-cutting marker.

    Exercises: the tier table is how a contributor decides where a new test belongs, so a
    tier that exists in code but not in the README is effectively undiscoverable.
    """
    from tests.support import registry

    readme = (roots.TESTS_DIR / "README.md").read_text(encoding="utf-8")
    missing = [t for t in registry.TIERS if f"`{t}`" not in readme]
    missing += [m for m in registry.CROSS_MARKERS if f"`{m}`" not in readme]
    assert not missing, f"undocumented tiers/markers in README.md: {missing}"


@pytest.mark.feature("payload")
def test_readme__documents_every_fixture_portfolio():
    """Each fixture slug appears in the README, so the set is discoverable from one page."""
    readme = (roots.TESTS_DIR / "README.md").read_text(encoding="utf-8")
    missing = [slug for slug in roots.FIXTURE_SLUGS if slug not in readme]
    assert not missing, f"fixtures missing from README.md: {missing}"
