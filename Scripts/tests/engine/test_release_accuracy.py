"""Independent cash, tax and calendar examples for the V1 review."""
import pytest

from valuation import simulator as sim


@pytest.mark.regression("DA3-06")
@pytest.mark.parametrize("joint,deduction,tops", [
    (True, 32200, (24800, 100800, 211400, 403550, 512450, 768700)),
    (False, 16100, (12400, 50400, 105700, 201775, 256225, 640600)),
])
def test_ordinary_tax_at_every_boundary(joint, deduction, tops):
    """Tax at the 2026 standard deduction and each ordinary bracket matches the IRS schedule."""
    cfg = sim.build_tax_schedule_cfg({"project": {"investors": [{}, {}], "tax_rates": {"state_rate": 0}}}, {"simMarriedFilingJointly": joint})
    tax = 0
    prev = 0
    for bound, rate in zip((0, *tops), (0, .10, .12, .22, .24, .32, .35)):
        tax += (bound - prev) * rate
        actual = sim._tax_ordinary_increment({"_taxCfg": cfg}, deduction + bound, iso="2026-01-01", dfl=1, apply_niit=False)
        assert actual["tax"] == pytest.approx(tax)
        prev = bound


@pytest.mark.regression("DA3-05")
def test_lot_calendar_anniversary():
    """Same-day and anniversary sales are short term, including a leap-day purchase."""
    for buy, anniversary, after in [("2025-01-15", "2026-01-15", "2026-01-16"), ("2024-02-29", "2025-02-28", "2025-03-01")]:
        lot = {"vestIso": buy}
        assert sim._is_short_term_lot(lot, buy)
        assert sim._is_short_term_lot(lot, anniversary)
        assert not sim._is_short_term_lot(lot, after)


@pytest.mark.regression("DA3-03")
def test_reopened_schedule_keeps_phase():
    """An expired deposit is skipped and a quarterly draw keeps its original payment months."""
    once, quarterly = sim.normalize_scheduled_actions([
        {"type": "deposit", "ym": "2023-01", "to_id": "cash", "amount": 10000},
        {"cadence": "quarterly", "start_ym": "2023-02", "amount": 100},
        {"amount": 200},
    ], "2026-11-18")
    assert once["ym"] == "2023-01"
    assert not sim._action_triggers_in_ym(once, "2026-12")
    assert not sim._action_triggers_in_ym(quarterly, "2026-12")
    assert sim._action_triggers_in_ym(quarterly, "2027-02")


@pytest.mark.regression("DA3-01")
def test_rmd_uses_prior_close_for_each_owner_and_plan():
    """Separate IRAs of same-age spouses and separate employer plans each owe their own January draw."""
    assets = [dict(id=key, account_id=key, rmd_owner_index=owner, tax_wrapper=wrapper, tax_model="ORDINARY_FULL",
                   owner_birth_year=1953, current_value=26500, cost_basis=0)
              for key, owner, wrapper in [("a", 0, "TRAD_IRA"), ("b", 1, "TRAD_IRA"), ("c", 0, "401K"), ("d", 0, "401K")]]
    order = [a["id"] for a in assets]
    state = sim._clone_sim_asset_state(assets, "2025-12-31", True)
    step = sim._step_sim_month(assets, state, "2026-01-31", "Jan", .1, .1, 4000, True, order,
                               asset_monthly_rates={key: .1 for key in order})
    assert step["monthlyRmdGross"] == pytest.approx(4000)
    for key in order:
        assert state[key]["val"] == pytest.approx(26500 * 1.1 - 1000)


@pytest.mark.regression("DA3-04")
def test_pia_uses_eligibility_year_and_later_colas():
    """Identical $1,000 AIMEs receive the historical eligibility-year bend points and their subsequent COLAs."""
    from exporters.view_model import _build_ssn_model

    investors = [dict(id=str(y), birth_date=f"{y}-06-15", ssn={"annual_earnings": {str(yr): 140000 for yr in (2023, 2024, 2025)}}) for y in (1962, 1963)]
    profiles = _build_ssn_model({"investors": investors})["investors"]
    for profile, expected in zip(profiles, [(1174, 7078, 948.3), (1226, 7391, 925.2)]):
        assert profile["aime"] == 1000
        assert (profile["bend1"], profile["bend2"], profile["pia_monthly_2026"]) == expected


def test_competing_withdrawals_report_funded_amounts():
    """Two withdrawals requesting $100 each share only $130 after ordinary expenses."""
    assets = [dict(id="cash", tax_model="CASH", tax_rate=0, current_value=150, cost_basis=150)]
    actions = sim.normalize_scheduled_actions([dict(id=key, amount=100, ym="2026-02") for key in ("a", "b")], "2026-01-15")
    state = sim._clone_sim_asset_state(assets, "2026-01-15", True)
    result = sim._step_sim_month(assets, state, "2026-02-15", "Feb", 0, 0, 20, True, ["cash"],
                                 scheduled_actions=actions, include_scheduled_actions=True)
    assert [(e["netAmt"], e["shortfall"]) for e in result["schedActionEvents"]] == [(100, 0), (30, 70)]
    assert result["unmetNetBurn"] == 70
