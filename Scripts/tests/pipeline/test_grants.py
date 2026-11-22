#!/usr/bin/env python3
"""
Equity awards end to end, on a household the grant code was not written against.

The fixture grants RSUs of an unfamiliar ticker through an unfamiliar stock-plan
custodian. That combination is what makes the legacy assumptions visible: a vested-share
account resolved by custodian name finds nothing here (LL-03), an award type defaulted to
the original plan's wording is wrong here (LL-06), and a retention rate taken from a
constant disagrees with this fixture's own release history (LL-09).
"""

from __future__ import annotations

import pytest

from tests.support import payload as P, pipeline as pipeline_support, roots

pytestmark = pytest.mark.portfolio("p2_equity_comp_rsu")

SLUG = "p2_equity_comp_rsu"


@pytest.mark.feature("grants")
def test_grants__symbol_and_company__come_from_the_award_file(root, payload):
    """The dashboard reports the fixture's own ticker and issuer, not a default."""
    grants = payload.get("stock_grants") or {}
    assert grants.get("default_symbol") == "VNTG"
    assert P.grant_symbols(payload) == ["VNTG"]
    entry = (grants.get("by_symbol") or {}).get("VNTG") or {}
    assert entry.get("company_name") == "Vantage Robotics, Inc."
    assert entry.get("award_type") == "RSU"


@pytest.mark.feature("grants")
@pytest.mark.regression("LL-03")
def test_stock_plan_account__is_resolved_from_configuration(root, payload, cfg):
    """
    The vested-award account is the one `project.json` names, at its real custodian.

    Exercises: the fixture's stock plan is custodied somewhere the application has never
    seen, so a custodian-name lookup resolves to nothing and this assertion is the one
    that notices.
    """
    configured = str((cfg.get("gsu_forecast") or {}).get("target_vested_account_id") or "")
    assert configured == "MES-3301"
    assert (cfg.get("accounts") or {})[configured]["institution"] == "Meridian Equity Services"

    entry = ((payload.get("stock_grants") or {}).get("by_symbol") or {}).get("VNTG") or {}
    # Per DP-05, Live View always displays real account names and contract numbers;
    # aliasing occurs dynamically in Demo Mode and AI Chat Privacy Shield.
    assert str(entry.get("account_id")) == configured
    # The vested shares that were transferred out live at the *destination* custodian,
    # which is a different institution again: neither is the one the code assumes.
    assert str(entry.get("available_institution")) == "Harbor Brokerage"


@pytest.mark.feature("grants")
def test_grants__unvested_tranches__become_a_future_release_bucket(root, payload):
    """Future vests roll into one simulator bucket named after the fixture's own ticker."""
    bucket = P.sim_asset(payload, "future_unvested_vntg")
    assert bucket is not None, f"no future-release bucket among {P.sim_asset_ids(payload)}"
    assert "VNTG" in str(bucket.get("title"))
    assert float(bucket.get("current_value") or 0.0) > 0.0


@pytest.mark.feature("grants")
@pytest.mark.regression("LL-09")
def test_net_retention__is_computed_from_this_fixtures_release_history(root, payload):
    """
    Retention comes from the releases in the data, not from a constant.

    Exercises: the fixture withholds 38% at every release, so retention must be 0.62. The
    legacy constant is a different number, which is what makes this a useful assertion.
    """
    assert float(payload.get("net_retention_rate")) == pytest.approx(0.62, abs=0.005)


@pytest.mark.feature("grants")
@pytest.mark.regression("LL-22")
def test_grant_schedule__every_specified_grant_keeps_its_own_id(root, slug):
    """
    All three grants are represented under the ids the award file gave them.

    Exercises: one grant in the fixture is fully vested by the pinned `as_of`, so it shows
    up only in the release history. A test that looked at unvested tranches alone would
    miss that its identity is lost.
    """
    spec = roots.load_spec(SLUG)
    grants_spec = next(a["grants"] for a in spec["accounts"] if a.get("grants"))
    expected_ids = {str(g["id"]) for g in grants_spec["grants"]}

    pipeline_support.ingest_fixture(slug)
    with pipeline_support.open_db(slug) as db:
        seen = {
            str(r["grant_id"])
            for r in db.conn.execute("SELECT DISTINCT grant_id FROM gsu_releases_history")
        } | {
            str(r["grant_id"])
            for r in db.conn.execute("SELECT DISTINCT grant_id FROM gsu_tranches")
        }
    assert expected_ids <= seen, f"missing grants: {sorted(expected_ids - seen)}"


@pytest.mark.feature("grants")
def test_grant_releases__every_vest_is_recorded_even_when_the_grant_id_is_lost(root, slug):
    """
    No release is dropped, whatever id it ends up filed under.

    Exercises: the companion to the strict-xfail above. The identity of a fully vested
    grant is currently lost, but the shares and dates must still all be there, because the
    vested totals and retention rate are computed from them.
    """
    spec = roots.load_spec(SLUG)
    grants_spec = next(a["grants"] for a in spec["accounts"] if a.get("grants"))
    as_of = str(spec["as_of"])
    from tests.generate import builder

    expected_vests = sum(
        1
        for grant in grants_spec["grants"]
        for vest in builder._vest_schedule(grant)
        if vest.isoformat() <= as_of
    )

    pipeline_support.ingest_fixture(slug)
    with pipeline_support.open_db(slug) as db:
        count = db.conn.execute("SELECT COUNT(*) AS n FROM gsu_releases_history").fetchone()["n"]
    assert count == expected_vests


@pytest.mark.feature("grants")
def test_in_kind_transfer__lands_in_the_receiving_account_with_carryover_basis(root, payload):
    """
    The transferred lot appears at the destination custodian with its vest-date basis.

    Exercises: the transfer moves shares from the stock plan to a brokerage account at a
    different institution, with basis carried over rather than reset.
    """
    spec = roots.load_spec(SLUG)
    transfer = next(a["grants"]["in_kind_transfer"] for a in spec["accounts"] if a.get("grants"))
    shares = float(transfer["shares"])

    holdings = [h for h in P.holdings(payload) if str(h.get("symbol") or "").upper() == "VNTG"]
    assert holdings, "the transferred position is missing from the payload"
    assert any(f"{shares:,.0f}" in str(h.get("quantity_str") or "") for h in holdings), (
        f"no VNTG holding reports {shares:,.0f} shares: "
        f"{[h.get('quantity_str') for h in holdings]}"
    )


@pytest.mark.feature("wrappers")
def test_wrappers__each_account_type__gets_its_own_tax_rule(root, payload):
    """
    Taxable, Traditional IRA, Roth and 401(k) buckets each state their own treatment.

    Exercises: `accounts[].tax_status` driving the rule text, which is the layer that
    overrides the generic template.
    """
    wrappers = {
        str(asset.get("id")): str(asset.get("tax_wrapper") or "") for asset in P.sim_assets(payload)
    }
    assert "ROTH_IRA" in wrappers.values(), wrappers
    assert "TRAD_IRA" in wrappers.values(), wrappers
    assert "TAXABLE" in wrappers.values(), wrappers

    for asset in P.sim_assets(payload):
        wrapper = str(asset.get("tax_wrapper") or "")
        title = str(asset.get("tax_rule_title") or "")
        detail = str(asset.get("tax_rule_detail") or "")
        assert title, f"{asset.get('id')}: no tax rule title"
        if wrapper == "ROTH_IRA":
            assert "0%" in title, f"Roth bucket should state a zero rate: {title}"
            assert "tax-free" in detail.lower()
        if wrapper == "TRAD_IRA":
            assert "Ordinary Income" in title or "ordinary income" in detail.lower()


@pytest.mark.feature("rmd")
def test_rmd_owner__is_the_configured_investor(root, payload, cfg):
    """RMDs attach to the investor the config flags, not to the first investor in the list."""
    configured = next(i["id"] for i in cfg["investors"] if i.get("is_rmd_owner"))
    owners = [str(i.get("id")) for i in P.investors(payload) if i.get("rmd_owner")]
    assert owners == [configured]


@pytest.mark.feature("payload")
def test_payload__matches_the_committed_snapshots(root, slug, payload):
    """Account totals and simulator buckets agree with the recorded snapshots."""
    from tests.generate import expected as expected_mod
    from tests.support import snapshots

    spec = roots.load_spec(slug)
    snapshots.assert_flat(expected_mod.payload_summary(spec, payload), slug, "payload_summary")
    snapshots.assert_flat(expected_mod.sim_kpis(spec, payload), slug, "sim_kpis")
