#!/usr/bin/env python3
"""
Static audit of literal-fallback shapes in the application source.

Findings LL-01, LL-08, LL-09, LL-14, LL-17 and LL-18 all have the same shape: a
configuration field exists, but when it is empty a hardcoded ticker, date, yield, or
tax-rate literal wins (`or "GOOG"`, `or 0.25`, `or "2025-01-15"`). The fixtures catch
these at runtime only when a code path happens to execute; this scan catches them in
the source.

`fixtures/denylist/fallback_allowlist.txt` lists the ones that are still open. The list can
only shrink: a new hit fails as a regression, and an entry that no longer matches fails as
stale. Emptying the file closes the family.
"""

from __future__ import annotations

import pytest

from tests.support import env, roots, scans

ALLOWLIST_PATH = roots.DENYLIST_DIR / "fallback_allowlist.txt"


@pytest.mark.feature("payload")
@pytest.mark.regression("LL-01")
def test_no_new_literal_fallback__has_been_introduced():
    """
    Every literal-fallback shape in the source is one the allowlist already records.

    Exercises: the LL-01 family at the source level. A hit that is not on the list is a
    new hard-coded default; the failure names the file, the line and the pattern.
    """
    hits = set(scans.scan_fallbacks(env.app_checkout_dir()))
    allowed = scans.fallback_allowlist()

    new_hits = sorted(hits - allowed)
    assert not new_hits, (
        "new literal fallback(s) introduced:\n"
        + "\n".join(f"  {h}" for h in new_hits[:30])
        + f"\n\nUse the configuration field instead, or record the line in "
        f"{ALLOWLIST_PATH.name} with a reason."
    )


@pytest.mark.feature("payload")
def test_fallback_allowlist__has_no_stale_entries():
    """
    Every allowlisted fallback still exists.

    Exercises: the shrink-only property. When a fix lands, its line stops matching and this
    test says so, which is the prompt to delete the entry and close the finding.
    """
    hits = set(scans.scan_fallbacks(env.app_checkout_dir()))
    allowed = scans.fallback_allowlist()

    stale = sorted(allowed - hits)
    assert not stale, (
        "these allowlisted fallbacks no longer match (the line moved, or the fix landed):\n"
        + "\n".join(f"  {s}" for s in stale[:30])
        + f"\n\nDelete them from {ALLOWLIST_PATH.name}."
    )


# One line each pattern must match, so the guard is self-verifying. Once the codebase is
# clean a pattern matches nothing in the source, which is the goal state, not a reason to
# stop checking: these samples prove the regex still works and would catch a relapse.
PATTERN_SAMPLES: dict[str, str] = {
    "primary_symbol_or_py": 'sym = cfg.get("primary_equity_symbol") or "GOOG"',
    "primary_symbol_or_js": 'const sym = PAYLOAD.primary_symbol || "GOOG";',
    "primary_symbol_default_arg": 'cfg.get("primary_equity_symbol", "GOOG")',
    "institution_fallback": 'inst = acct.get("institution") or "Wells Fargo Advisors"',
    "institution_branch": 'short = "MS" if "Morgan" in inst else "Other"',
    "carrier_branch": 'is_rila = "allianz" in asset_id',
    "ssn_effective_literal": 'rate = model.get("effective_ssn_tax_rate") or 0.25',
    "pia_literal": 'pia = inv.get("pia_monthly_2026") or 2500.0',
    "transfer_date_literal": 'xfer = gsu_cfg.get("in_kind_transfer_vest_date") or "2025-01-15"',
    "retention_literal": "const NET_RATE = PAYLOAD.net_retention_rate || 0.555;",
    "vol_preset_literal": "const vol = REF.single_stock_vol || 30.0;",
    "muni_yield_literal": '{"munis": 3.50}',
    "annuity_yield_literal": '{"fixed_annuity": 4.00}',
    "ltcg_js_literal": 'const ltcgPct = rates.ltcg ?? "25.0";',
    "ordinary_js_literal": 'const ordPct = rates.ordinary ?? "40.0";',
    "ltcg_num_fallback": 'ltcg = tr.get("ltcg_top", 0.25)',
    "ordinary_num_fallback": 'ord_r = tr.get("ordinary_top") or 0.40',
    "ira_num_fallback": 'ira_r = cfg.get("sim_ira_ordinary_tax_rate", 0.35)',
    "split_threshold_fallback": '{"high_basis_split_threshold": 100.0}',
    "cash_apy_fallback": 'apy = item.get("apy") or 0.04',
    "state_tax_delta": "const treasuryRate = ltcgTop - 0.10;",
    "grant_symbol_db_default": "symbol TEXT NOT NULL DEFAULT 'GOOG',",
}


@pytest.mark.feature("payload")
def test_fallback_patterns__each_compiles_and_matches_its_own_sample():
    """
    Every pattern in the audit is a working regex, proven against a sample it must match.

    Exercises: a pattern that matches nothing in the source is the goal state, so "does it
    find anything" is not a usable health check. Each pattern instead carries one line it
    must match, which keeps the guard honest once the codebase is clean.
    """
    import re

    declared = {}
    for raw in scans.fallback_patterns():
        pattern_id, _, regex = raw.partition("=")
        pattern_id = pattern_id.strip()
        assert regex, f"malformed pattern line: {raw!r}"
        declared[pattern_id] = re.compile(regex.strip())

    missing_samples = sorted(set(declared) - set(PATTERN_SAMPLES))
    assert not missing_samples, (
        f"patterns with no sample line in this test: {missing_samples}. "
        "Add one so the regex stays verifiable after the fix lands."
    )
    stale_samples = sorted(set(PATTERN_SAMPLES) - set(declared))
    assert not stale_samples, f"samples for patterns that no longer exist: {stale_samples}"

    for pattern_id, regex in sorted(declared.items()):
        sample = PATTERN_SAMPLES[pattern_id]
        assert regex.search(sample), f"{pattern_id} no longer matches its own sample: {sample!r}"


@pytest.mark.feature("payload")
def test_literal_fallback_family__reports_its_current_state(capsys):
    """
    Prints how many literal fallbacks remain, so the number is visible in a run.

    Exercises: nothing on its own. It exists so `-s` shows the size of the remaining LL-01
    family without anyone having to open the allowlist.
    """
    hits = sorted(scans.scan_fallbacks(env.app_checkout_dir()))
    with capsys.disabled():
        print(f"\n  literal fallbacks remaining: {len(hits)}")
        for hit in hits[:10]:
            print(f"    {hit}")
    assert isinstance(hits, list)
