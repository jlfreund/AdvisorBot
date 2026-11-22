#!/usr/bin/env python3
"""
Portability scan: legacy vocabulary leaking out of the code.

No fixture portfolio uses the names the application was originally built around, so any
occurrence of one in a rendered dashboard came from a literal in the code rather than from
the data. That is what turns six synthetic portfolios into a regression harness for the
legacy-logic findings (`LL-*`).

`fixtures/denylist/expected_leaks.json` records what still leaks today, per fixture. The
baseline may only shrink: a new term fails the scan as a regression, and a term that stops
appearing fails it as a stale entry to delete. Emptying the file closes the LL backlog.
"""

from __future__ import annotations

import json

import pytest

from tests.support import env, pipeline as pipeline_support, roots, scans

BASELINE_PATH = roots.DENYLIST_DIR / "expected_leaks.json"


def _baseline() -> dict[str, list[str]]:
    data = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    return {slug: list(terms) for slug, terms in (data.get("portfolios") or {}).items()}


@pytest.mark.parametrize("slug", list(roots.FIXTURE_SLUGS))
@pytest.mark.feature("render")
def test_rendered_dashboard__leaks_no_new_legacy_term(slug, tmp_path_factory):
    """
    A fixture renders no legacy term that the baseline does not already record.

    Exercises: every hard-coded label, tooltip, bucket id and payload key in one pass. A
    new failure here means a literal was added; a stale baseline entry means one was fixed.
    """
    target = tmp_path_factory.mktemp(f"scan-{slug}")
    roots.assemble_root(target, [slug])
    with env.pointed_at(target):
        pipeline_support.clear_payload_cache()
        html = pipeline_support.rendered_html(slug)
        pipeline_support.clear_payload_cache()

    found = set(scans.scan_text_for_legacy(html))
    allowed = set(_baseline().get(slug) or [])

    new_leaks = sorted(found - allowed)
    assert not new_leaks, (
        f"{slug}: new legacy term(s) in the rendered dashboard: {new_leaks}\n"
        f"No fixture uses these words, so they came from a literal in the code.\n"
        f"Fix the literal, or (if deliberate) add it to {BASELINE_PATH.name} with a reason."
    )

    fixed = sorted(allowed - found)
    assert not fixed, (
        f"{slug}: these legacy term(s) no longer appear: {fixed}\n"
        f"Delete them from {BASELINE_PATH.name} to lock the fix in."
    )


@pytest.mark.feature("render")
def test_leak_baseline__is_well_formed_and_covers_every_fixture():
    """The baseline names every fixture and only denylisted terms, so it cannot drift silently."""
    baseline = _baseline()
    assert set(baseline) == set(roots.FIXTURE_SLUGS), sorted(set(roots.FIXTURE_SLUGS) ^ set(baseline))
    known = set(roots.denylist_terms())
    for slug, terms in baseline.items():
        unknown = sorted(set(terms) - known)
        assert not unknown, f"{slug}: baseline lists terms that are not on the denylist: {unknown}"


@pytest.mark.feature("payload")
@pytest.mark.parametrize("slug", ["p1_index_saver", "p3_retiree_annuities"])
def test_tax_rule_text__names_no_other_jurisdiction(slug, tmp_path_factory):
    """
    A tax-rule sentence never names a state the household does not live in.

    Exercises: finding LL-10. The fixtures are in Texas and Oregon; a rule that mentions
    California is quoting a literal rather than describing the household.
    """
    from tests.support import payload as P

    target = tmp_path_factory.mktemp(f"tax-{slug}")
    roots.assemble_root(target, [slug])
    with env.pointed_at(target):
        pipeline_support.clear_payload_cache()
        payload = pipeline_support.build_payload(slug)
        rules = P.tax_rule_texts(payload)
        pipeline_support.clear_payload_cache()

    spec = roots.load_spec(slug)
    home_state = str(spec["household"]["state"])
    offenders = [
        text for text in rules
        if "CA" in text.replace("CASH", "").replace("Capital", "") and home_state != "CA"
    ]
    assert not offenders, (
        "LL-10: the state portion of the tax rules must reflect the configured state rate. "
        f"{home_state} household shown: {offenders[:2]}"
    )


@pytest.mark.feature("render")
def test_examples_project_template__shares_no_vocabulary_with_the_denylist():
    """
    The shipped example config is a synthetic household, not the original one.

    Exercises: finding LL-24. New users are told to copy this file, so anything legacy in
    it propagates to every fresh portfolio.
    """
    path = env.app_checkout_dir() / "Examples" / "project.example.json"
    if not path.is_file():
        pytest.skip("no example project template in this checkout")
    hits = scans.scan_text_for_legacy(path.read_text(encoding="utf-8"))
    assert not hits, f"LL-24: the example config carries legacy vocabulary: {hits}"
