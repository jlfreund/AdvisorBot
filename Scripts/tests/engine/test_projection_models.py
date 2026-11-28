#!/usr/bin/env python3
"""
Simulator, solver and projection models, driven by fixture portfolios.

These are the portable replacements for the ported engine tests that encoded the original
household's solvency and absolute dollar targets. Everything here is either a structural
property (a zero-volatility Monte Carlo collapses to the single path) or a target derived
from the fixture's own output, so it holds for any portfolio.
"""

from __future__ import annotations

import pytest

from tests.support import pipeline as pipeline_support, roots

pytestmark = pytest.mark.portfolio("p2_equity_comp_rsu")


def _settings(payload: dict) -> dict:
    from valuation.simulator import build_effective_settings

    return build_effective_settings(payload)


@pytest.mark.feature("simulator")
def test_simulation__runs_and_reports_a_milestone_value(root, payload):
    """A deterministic run produces a milestone wealth figure and a tax total."""
    from valuation.simulator import run_simulation

    result = run_simulation(payload, settings=_settings(payload))
    assert result.get("bars"), "the simulation produced no monthly path"
    milestones = result.get("milestones") or {}
    assert float((milestones.get("start_today") or {}).get("nominal_usd") or 0) > 0
    # At least one milestone key is derived from the fixture's own investor ids. (The
    # generic `investor_1_milestone` / `year_40` aliases sit alongside them by design.)
    derived = [k for k in milestones if k.startswith("inv_")]
    assert derived, f"no investor-derived milestone keys in {sorted(milestones)}"
    assert any("amara" in k for k in derived), sorted(milestones)
    assert isinstance((result.get("solvency") or {}).get("sustains_40_years"), bool)


@pytest.mark.feature("simulator")
def test_simulation__is_deterministic_across_runs(root, payload):
    """
    The same inputs give the same output, to the cent.

    Exercises: determinism is what makes the snapshot assertions meaningful. A simulator
    that drifted between runs would make every expectation in the suite flaky.
    """
    from valuation.simulator import run_simulation

    settings = _settings(payload)
    first = run_simulation(payload, settings=settings)
    second = run_simulation(payload, settings=settings)
    assert first.get("milestones") == second.get("milestones")
    assert first.get("solvency") == second.get("solvency")


@pytest.mark.feature("monte_carlo")
def test_monte_carlo__with_zero_volatility__collapses_onto_the_single_path(root, payload):
    """
    With all volatilities zeroed, every percentile of the fan equals the deterministic path.

    Exercises: the structural identity between the Monte Carlo engine and the simple model.
    This is the portable half of the ported zero-volatility test, which also asserted a
    solvency percentage that only held for the original portfolio.
    """
    from valuation.simulator import run_monte_carlo, run_simulation

    settings = {
        **_settings(payload),
        "mcSingleStockVolPct": 0.0,
        "mcBroadEquityVolPct": 0.0,
        "mcSingleStockCorr": 0.0,
        "mcSimulations": 24,
    }
    single = run_simulation(payload, settings=settings)
    fan = run_monte_carlo(payload, settings=settings)

    band = fan.get("band") or []
    assert band, "monte carlo returned no band"
    last = band[-1]
    percentiles = last.get("nominal") or last
    values = [percentiles[key] for key in ("p10", "p50", "p90") if key in percentiles]
    assert len(values) == 3, percentiles
    assert max(values) - min(values) < 1.0, f"zero-volatility fan is not degenerate: {values}"

    single_end = float((single.get("bars") or [{}])[-1].get("totalVal") or 0.0)
    assert abs(float(percentiles["p50"]) - single_end) < 1.0


@pytest.mark.feature("monte_carlo")
def test_monte_carlo__is_seeded_and_reproducible(root, payload):
    """Two identical Monte Carlo runs agree, so the fan can be snapshot-tested."""
    from valuation.simulator import run_monte_carlo

    settings = {**_settings(payload), "mcSimulations": 32}
    first = run_monte_carlo(payload, settings=settings)
    second = run_monte_carlo(payload, settings=settings)
    assert (first.get("band") or [])[-1] == (second.get("band") or [])[-1]


@pytest.mark.feature("solver")
def test_solver__target_derived_from_the_fixtures_own_outcome__converges(root, payload):
    """
    Goal-seek converges on a target the fixture can actually reach.

    Exercises: the bisection solver, with a target taken from the portfolio's own baseline
    rather than an absolute dollar figure. The ported test asked for $25M, which no
    synthetic fixture reaches, so it reported "not converged" and told us nothing.
    """
    from valuation.simulator import run_simulation, solve_simulation

    settings = _settings(payload)
    baseline = run_simulation(payload, settings=settings)
    milestones = baseline.get("milestones") or {}
    primary = next((v for k, v in milestones.items() if k != "start_today"), None)
    if not primary or float(primary.get("real_today_usd") or 0) <= 0:
        pytest.skip("this fixture has no positive milestone value to solve against")

    # No `target_metric`: the solver derives it from the fixture's own primary investor and
    # milestone age, which is the portable way to ask the question. Which *parameter* can
    # reach a given target depends on the portfolio, so the test requires that at least one
    # of them converges rather than naming one up front.
    target = float(primary["real_today_usd"]) * 1.10
    attempts = {}
    for knob in ("futureGoogGrowthPct", "futureNonGoogGrowthPct", "simInflationPct", "simAnnualBurn"):
        result = solve_simulation(
            payload, solve_for=knob, target_value=target, settings=settings
        )
        attempts[knob] = result
        if result.get("converged"):
            assert abs(float(result.get("residual_pct") or 99)) < 1.0, result
            assert len(result.get("achievable_range_usd") or []) == 2
            return
    pytest.fail(
        "no solver knob reached a target 10% above the fixture's own baseline:\n"
        + "\n".join(f"  {k}: {v.get('note')}" for k, v in attempts.items())
    )


@pytest.mark.feature("solver")
def test_solver__unreachable_target__reports_the_achievable_range(root, payload):
    """An impossible target is reported as unreachable, with the range that is possible."""
    from valuation.simulator import solve_simulation

    result = solve_simulation(
        payload,
        solve_for="simAnnualBurn",
        target_value=500_000_000_000,
        settings=_settings(payload),
    )
    assert result.get("converged") is False
    assert result.get("note")
    assert len(result.get("achievable_range_usd") or []) == 2


@pytest.mark.feature("wrappers")
@pytest.mark.portfolio("p3_retiree_annuities")
def test_annuity_buckets__are_taxed_under_the_contract_rule_not_the_carrier_name(root, payload):
    """
    All three annuity classes get the ordinary-income-on-gain treatment from their class.

    Exercises: finding LL-13. The fixture's carriers are invented, so a carrier-name branch
    cannot fire and the asset class has to carry the tax shape.
    """
    from tests.support import payload as P

    contracts = [a for a in P.sim_assets(payload) if str(a.get("id")).startswith("ann_")]
    assert len(contracts) == 3, [a.get("id") for a in P.sim_assets(payload)]
    for contract in contracts:
        title = str(contract.get("tax_rule_title") or "")
        assert "72" in title or "Ord" in title, f"{contract['id']}: {title}"
        assert contract.get("maturity_iso"), f"{contract['id']}: no surrender-free date"


@pytest.mark.feature("cohorts")
@pytest.mark.portfolio("p1_index_saver")
def test_historical_cohorts__are_available_and_span_the_published_range(root, payload):
    """The cohort table loads and covers the documented start year onward."""
    cohorts = payload.get("cohorts") or {}
    if not cohorts.get("years"):
        pytest.skip("no cohort data bundled in this checkout")
    assert int(cohorts["first_year"]) <= 1930
    assert int(cohorts["last_year"]) >= 2020


@pytest.mark.feature("simulator")
@pytest.mark.parametrize("slug", ["p1_index_saver", "p2_equity_comp_rsu", "p3_retiree_annuities"])
def test_every_scenario__runs_without_error(slug, tmp_path_factory):
    """
    Each fixture's saved scenarios all execute.

    Exercises: scheduled gifts, hypothetical rollovers, stress and the simple model, as
    they are actually configured on disk rather than as hand-built settings dicts.
    """
    from tests.support import env
    from valuation.simulator import build_effective_settings, run_simulation

    target = tmp_path_factory.mktemp(f"scen-{slug}")
    roots.assemble_root(target, [slug])
    with env.pointed_at(target):
        pipeline_support.clear_payload_cache()
        payload = pipeline_support.build_payload(slug)
        saves = (payload.get("saves_bundle") or {}).get("models") or []
        assert saves, f"{slug}: no scenarios in the payload"
        for save in saves:
            settings = build_effective_settings(payload, save.get("settings") or {})
            result = run_simulation(payload, settings=settings)
            assert result.get("bars"), f"{slug}: scenario {save.get('name')!r} produced no path"
        pipeline_support.clear_payload_cache()


@pytest.mark.feature("solver")
@pytest.mark.portfolio("p1_index_saver")
def test_solver__initial_portfolio_value_for_solvency_pct(root, payload):
    """Solving for initial_portfolio_value targeting solvency_pct scales vested balances monotonically."""
    from valuation.simulator import build_effective_settings, solve_simulation

    settings = build_effective_settings(payload)
    settings["simActiveModel"] = "range"
    settings["mcSimulations"] = 30
    res = solve_simulation(
        payload,
        solve_for="initial_portfolio_value",
        target_metric="solvency_pct",
        target_value=50.0,
        settings=settings,
    )
    assert res["solve_for"] == "initial_portfolio_value"
    assert res["target_metric"] == "solvency_pct"
    assert "baseline_portfolio_value_usd" in res
    assert "portfolio_scale_factor" in res
    assert res["achievable_range_pct"][0] <= res["achievable_range_pct"][1]
    assert res["solved_value_exact"] > 0
