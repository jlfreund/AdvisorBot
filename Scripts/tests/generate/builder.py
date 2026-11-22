#!/usr/bin/env python3
"""
Turns a validated fixture spec into a complete AdvisorBot portfolio folder.

Everything a real portfolio has is emitted, so the generated fixture can be opened by the
CLI, the sidecar and the renderer without special-casing:

    project.json            household, investors, accounts, buckets, tax rates
    privacy_registry.json   alias/demo entities derived from the spec
    Statements/             canonical `*.statement.json`, `*.grants.json`, SSA XML
    Scenarios/              `_index.json` + numbered scenario files
    market_prices.json      committed price history (so no test needs the network)
    Plugins/                optional custom plugin files (for the opt-in tests)
    README.md               written by `docs.py` from the same spec

Determinism: all randomness comes from `prices.build_series`, which is seeded per symbol
from `spec.seed`. Running the generator twice produces byte-identical output, which the
fixture-drift scan relies on.
"""

from __future__ import annotations

import json
import shutil
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from . import prices, spec as spec_mod

GENERATED_BY = "tests.generate"

# SSA national average wage index is irrelevant for fixtures; these profiles only need to
# be plausible and deterministic. Earnings are whole dollars so the XML stays readable.
_PROFILE_BASE = {
    "mid_rising": (48_000, 1.045),
    "cap_from": (95_000, 1.06),
    "gap_years": (42_000, 1.035),
    "flat": (60_000, 1.0),
    "zero": (0, 1.0),
}

_OASDI_CAP_2026 = 184_500


# --------------------------------------------------------------------------- helpers


def _month_end(year: int, month: int) -> date:
    if month == 12:
        return date(year, 12, 31)
    return date(year, month + 1, 1) - timedelta(days=1)


def _period_dates(periods: dict[str, Any]) -> list[date]:
    """Statement as-of dates for an account, one per month in `from..to` inclusive."""
    if not periods:
        return []
    start_y, start_m = (int(x) for x in str(periods["from"]).split("-"))
    end_y, end_m = (int(x) for x in str(periods["to"]).split("-"))
    out: list[date] = []
    year, month = start_y, start_m
    while (year, month) <= (end_y, end_m):
        out.append(_month_end(year, month))
        month += 1
        if month > 12:
            year, month = year + 1, 1
    return out


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=False) + "\n", encoding="utf-8")


def _slugify(text: str) -> str:
    out = "".join(ch.lower() if ch.isalnum() else "_" for ch in str(text))
    while "__" in out:
        out = out.replace("__", "_")
    return out.strip("_") or "scenario"


def _lot_cost(lot: dict[str, Any], series: dict[str, float] | None) -> float:
    qty = float(lot["qty"])
    if lot.get("cost_per_share") is not None:
        return qty * float(lot["cost_per_share"])
    if series:
        return qty * prices.price_on(series, date.fromisoformat(str(lot["acquired"])))
    return 0.0


# --------------------------------------------------------------------------- prices


def build_price_book(spec: dict[str, Any]) -> dict[str, dict[str, float]]:
    """Builds the committed price history for every symbol the spec prices."""
    as_of = date.fromisoformat(str(spec["as_of"]))
    plans: dict[str, dict[str, Any]] = dict(spec.get("market_prices") or {})

    # Statement dates must have an exact close so reported values reconcile.
    extra: list[date] = [as_of]
    for acct in spec.get("accounts") or []:
        extra.extend(_period_dates(acct.get("periods") or {}))
    extra = sorted(set(extra))

    book: dict[str, dict[str, float]] = {}
    for symbol in sorted(plans):
        book[symbol.upper()] = prices.build_series(
            symbol, plans[symbol], spec_seed=int(spec["seed"]), as_of=as_of, extra_dates=extra
        )
    return book


# --------------------------------------------------------------------------- positions


def _position_rows(
    account: dict[str, Any],
    as_of: date,
    book: dict[str, dict[str, float]],
    *,
    month_index: int,
) -> list[dict[str, Any]]:
    """Builds the canonical-JSON position rows for one account on one statement date."""
    rows: list[dict[str, Any]] = []
    acct_id = str(account["id"])
    for pos in account.get("positions") or []:
        kind = str(pos.get("kind") or "ticker")
        if kind == "ticker":
            rows.append(_ticker_row(pos, as_of, book))
        elif kind == "cash":
            rows.append(_cash_row(pos, acct_id, as_of, month_index))
        elif kind == "annuity":
            rows.append(_annuity_row(pos, as_of, month_index))
        elif kind == "muni":
            rows.append(_muni_row(pos, as_of))
        elif kind == "contract_fund":
            rows.append(_contract_fund_row(pos, as_of, month_index))
    return [row for row in rows if row is not None]


def _ticker_row(pos: dict[str, Any], as_of: date, book: dict[str, dict[str, float]]) -> dict[str, Any]:
    symbol = str(pos["symbol"]).upper()
    series = book.get(symbol) or {}
    price = prices.price_on(series, as_of) if series else float(pos.get("price") or 0.0)
    lots = pos.get("lots") or []
    held = [lot for lot in lots if date.fromisoformat(str(lot["acquired"])) <= as_of]
    qty = round(sum(float(lot["qty"]) for lot in held), 6)
    split = pos.get("split")
    if split and date.fromisoformat(str(split["effective"])) <= as_of:
        qty = round(qty * float(split["ratio"]), 6)
        price = round(price / float(split["ratio"]), 4)
    cost = round(sum(_lot_cost(lot, series) for lot in held), 2)
    earliest = min((str(lot["acquired"]) for lot in held), default=None)
    row: dict[str, Any] = {
        "security_id": f"TICKER:{symbol}",
        "symbol_or_code": symbol,
        "canonical_name": str(pos["name"]),
        "asset_class": str(pos.get("asset_class") or "EQUITY").upper(),
        "stack_bucket": str(pos.get("stack_bucket") or "stocks"),
        "risk_class": str(pos.get("risk_class") or "broad_equity"),
        "is_scrapeable": bool(pos.get("is_scrapeable", True)),
        "quote_symbol": str(pos.get("quote_symbol") or symbol) if pos.get("is_scrapeable", True) else None,
        "quantity": qty,
        "stmt_price": round(price, 4),
        "market_value": round(qty * price, 2),
        "value_date": as_of.isoformat(),
        "cost_basis": cost,
    }
    if earliest:
        row["acquisition_date"] = earliest
    if pos.get("expense_ratio_pct") is not None:
        row["expense_ratio_pct"] = float(pos["expense_ratio_pct"])
    if pos.get("metadata"):
        row["metadata"] = dict(pos["metadata"])
    return row


def _cash_row(pos: dict[str, Any], acct_id: str, as_of: date, month_index: int) -> dict[str, Any]:
    value = float(pos.get("value") or 0.0) + float(pos.get("monthly_drift") or 0.0) * month_index
    return {
        "security_id": f"CASH:{acct_id}",
        "symbol_or_code": "USD",
        "canonical_name": str(pos["name"]),
        "asset_class": "CASH",
        "stack_bucket": "cash",
        "risk_class": "cash_fixed",
        "is_scrapeable": False,
        "quote_symbol": None,
        "quantity": 1.0,
        "stmt_price": None,
        "market_value": round(value, 2),
        "value_date": as_of.isoformat(),
        "yield_pct": float(pos.get("yield_pct") or 0.0) or None,
    }


def _annuity_row(pos: dict[str, Any], as_of: date, month_index: int) -> dict[str, Any]:
    premium = float(pos.get("premium") or 0.0)
    rate = float(pos.get("credited_rate_pct") or 0.0) / 100.0
    issue = date.fromisoformat(str(pos["issue_date"]))
    years = max(0.0, (as_of - issue).days / 365.25)
    value = premium * ((1.0 + rate) ** years) if rate else premium
    contract = str(pos.get("symbol") or _slugify(pos["name"]).upper())
    asset_class = str(pos.get("asset_class") or "FIXED_ANNUITY").upper()
    risk_default = {
        "FIXED_ANNUITY": "fixed_income",
        "INDEXED_ANNUITY": "hybrid_buffered",
        "RILA_ANNUITY": "hybrid_buffered",
    }.get(asset_class, "fixed_income")
    row: dict[str, Any] = {
        "security_id": f"CONTRACT:{contract}",
        "symbol_or_code": contract,
        "canonical_name": str(pos["name"]),
        "asset_class": asset_class,
        "stack_bucket": str(pos.get("stack_bucket") or "annuity"),
        "risk_class": str(pos.get("risk_class") or risk_default),
        "is_scrapeable": False,
        "quote_symbol": None,
        "quantity": 1.0,
        "stmt_price": None,
        "market_value": round(value, 2),
        "value_date": as_of.isoformat(),
        "cost_basis": premium,
        "initial_premium": premium,
        "issue_date": issue.isoformat(),
        "surrender_value": round(value * 0.93, 2),
    }
    if pos.get("maturity_date"):
        row["maturity_date"] = str(pos["maturity_date"])
    if pos.get("credited_rate_pct") is not None:
        row["credited_rate_pct"] = float(pos["credited_rate_pct"])
    metadata = dict(pos.get("metadata") or {})
    if pos.get("segments"):
        # Sub-segments carry their own value and unit count: the view model renders one
        # child row per segment of a buffered contract.
        segments = []
        for seg in pos["segments"]:
            pct = float(seg.get("pct") or 0.0)
            seg_value = round(value * pct / 100.0, 2)
            units = round(seg_value / 10.0, 3)  # $10 notional unit price
            segments.append(
                {
                    **{k: v for k, v in seg.items() if k != "pct"},
                    "pct": pct,
                    "value": seg_value,
                    "units": units,
                    "maturity_date": str(pos.get("maturity_date") or ""),
                }
            )
        metadata["sub_segments"] = segments
    if pos.get("surrender_schedule"):
        metadata["surrender_schedule"] = pos["surrender_schedule"]
    if metadata:
        row["metadata"] = metadata
    return row


def _muni_row(pos: dict[str, Any], as_of: date) -> dict[str, Any]:
    par = float(pos.get("par") or 0.0)
    price = float(pos.get("price") or 101.5)
    coupon = float(pos.get("coupon_pct") or 4.0)
    code = str(pos.get("symbol") or _slugify(pos["name"]).upper())
    row = {
        "security_id": f"CUSIP:{code}",
        "symbol_or_code": code,
        "canonical_name": str(pos["name"]),
        "asset_class": "MUNI_BOND",
        "stack_bucket": str(pos.get("stack_bucket") or "bonds"),
        "risk_class": str(pos.get("risk_class") or "fixed_income"),
        "is_scrapeable": False,
        "quote_symbol": None,
        "quantity": par,
        "stmt_price": price,
        "market_value": round(par * price / 100.0, 2),
        "value_date": as_of.isoformat(),
        "cost_basis": round(par * 1.0, 2),
        "est_annual_income": round(par * coupon / 100.0, 2),
        "yield_pct": coupon,
        "metadata": {"issuer_state": str(pos.get("state") or ""), **(pos.get("metadata") or {})},
    }
    if pos.get("maturity_date"):
        row["maturity_date"] = str(pos["maturity_date"])
    return row


def _contract_fund_row(pos: dict[str, Any], as_of: date, month_index: int) -> dict[str, Any]:
    value = float(pos.get("value") or 0.0) + float(pos.get("monthly_drift") or 0.0) * month_index
    code = str(pos.get("symbol") or _slugify(pos["name"]).upper())
    quantity = float(pos.get("quantity") or 1.0)
    split = pos.get("split")
    if split and date.fromisoformat(str(split["effective"])) <= as_of:
        # A reverse split changes share count, not market value.
        quantity = round(quantity * float(split["ratio"]), 6)
    return {
        "security_id": f"TICKER:{code}",
        "symbol_or_code": code,
        "canonical_name": str(pos["name"]),
        "asset_class": str(pos.get("asset_class") or "MUTUAL_FUND").upper(),
        "stack_bucket": str(pos.get("stack_bucket") or "stocks"),
        "risk_class": str(pos.get("risk_class") or "broad_equity"),
        "is_scrapeable": False,
        "quote_symbol": None,
        "quantity": quantity,
        "stmt_price": None,
        "market_value": round(value, 2),
        "value_date": as_of.isoformat(),
        "cost_basis": float(pos.get("cost_basis") or value),
    }


# --------------------------------------------------------------------------- statements


def emit_statements(spec: dict[str, Any], out_dir: Path, book: dict[str, dict[str, float]]) -> list[Path]:
    """Writes one canonical statement per account per period."""
    written: list[Path] = []
    sha = spec_mod.spec_sha(spec)
    for acct in spec.get("accounts") or []:
        period_dates = _period_dates(acct.get("periods") or {})
        if not period_dates or not (acct.get("positions") or []):
            continue
        subdir = str(acct.get("statement_subdir") or _slugify(acct["institution"]))
        for idx, as_of in enumerate(period_dates):
            rows = _position_rows(acct, as_of, book, month_index=idx)
            if not rows:
                continue
            payload = {
                "format": "advisorbot.statement/1",
                "doc_type": "CANONICAL_JSON",
                "institution": str(acct["institution"]),
                "account_id": str(acct["id"]),
                "account_name": str(acct.get("account_name") or acct["institution"]),
                "account_type": str(acct["account_type"]).upper(),
                "tax_status": str(acct["tax_status"]).upper(),
                "display_order": int(acct.get("display_order", 100)),
                "period_start": date(as_of.year, as_of.month, 1).isoformat(),
                "as_of_date": as_of.isoformat(),
                "reported_total": round(sum(float(r["market_value"]) for r in rows), 2),
                "positions": rows,
                "fixture": {"generated_by": GENERATED_BY, "spec_sha": sha},
            }
            adjustments = _split_adjustments(acct, as_of)
            if adjustments:
                payload["adjustments"] = adjustments
            path = out_dir / "Statements" / subdir / f"{_slugify(acct['id'])}_{as_of.strftime('%Y-%m')}.statement.json"
            _write_json(path, payload)
            written.append(path)
    return written


def _split_adjustments(account: dict[str, Any], as_of: date) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for pos in account.get("positions") or []:
        split = pos.get("split")
        if not split:
            continue
        effective = date.fromisoformat(str(split["effective"]))
        if effective.year != as_of.year or effective.month != as_of.month:
            continue
        symbol = str(pos.get("symbol") or _slugify(pos["name"]).upper()).upper()
        pre = (
            round(sum(float(lot["qty"]) for lot in pos.get("lots") or []), 6)
            or float(split.get("pre_quantity") or 0.0)
            or float(pos.get("quantity") or 0.0)
        )
        out.append(
            {
                "security_id": f"TICKER:{symbol}",
                "effective_date": effective.isoformat(),
                "adj_type": "PRE_SPLIT_SHARES",
                "pre_quantity": pre,
                "post_quantity": round(pre * float(split["ratio"]), 6),
                "notes": str(split.get("notes") or f"{split['ratio']}-for-1 share adjustment"),
            }
        )
    return out


# --------------------------------------------------------------------------- grants


def _vest_schedule(grant: dict[str, Any]) -> list[date]:
    first = date.fromisoformat(str(grant["first_vest"]))
    count = int(grant.get("vests") or 16)
    cadence = str(grant.get("cadence") or "quarterly")
    step = {"monthly": 1, "quarterly": 3, "annual": 12}.get(cadence, 3)
    out: list[date] = []
    year, month, day = first.year, first.month, first.day
    for _ in range(count):
        out.append(date(year, month, min(day, 28)))
        month += step
        while month > 12:
            month -= 12
            year += 1
    return out


def emit_grants(spec: dict[str, Any], out_dir: Path, book: dict[str, dict[str, float]]) -> list[Path]:
    """Writes `*.grants.json` with vested release history, unvested tranches and transfers."""
    as_of = date.fromisoformat(str(spec["as_of"]))
    written: list[Path] = []
    for acct in spec.get("accounts") or []:
        grants_spec = acct.get("grants")
        if not grants_spec:
            continue
        symbol = str(grants_spec["symbol"]).upper()
        series = book.get(symbol) or {}
        withholding = float(grants_spec.get("withholding_pct") or 38.0) / 100.0
        award_type = str(grants_spec.get("award_type") or "RSU")
        tranches: list[dict[str, Any]] = []
        releases: list[dict[str, Any]] = []
        withdrawals: list[dict[str, Any]] = []
        order = 1000
        for grant in grants_spec.get("grants") or []:
            schedule = _vest_schedule(grant)
            per_vest = round(float(grant["total_shares"]) / max(1, len(schedule)), 3)
            for vest in schedule:
                if vest <= as_of:
                    price = prices.price_on(series, vest) if series else 0.0
                    gross = per_vest
                    net = round(gross * (1.0 - withholding), 3)
                    releases.append(
                        {
                            "order_number": f"ORD-{order}",
                            "vest_date": vest.isoformat(),
                            "release_price": round(price, 4),
                            "gross_shares": gross,
                            "net_shares": net,
                            "tax_method": "Shares Withheld",
                            "grant_id": str(grant["id"]),
                            "symbol": symbol,
                            "company_name": str(grants_spec["company"]),
                            "award_type": award_type,
                            "account_id": str(acct["id"]),
                        }
                    )
                    order += 1
                else:
                    tranches.append(
                        {
                            "grant_id": str(grant["id"]),
                            "grant_date": str(grant["grant_date"]),
                            "vest_date": vest.isoformat(),
                            "orig_qty": per_vest,
                            "deus": 0.0,
                            "total_qty": per_vest,
                            "symbol": symbol,
                            "company_name": str(grants_spec["company"]),
                            "award_type": award_type,
                            "account_id": str(acct["id"]),
                        }
                    )
        transfer = grants_spec.get("in_kind_transfer")
        if transfer:
            exec_date = date.fromisoformat(str(transfer.get("exec_date") or transfer["vest_date"]))
            price = prices.price_on(series, exec_date) if series else 0.0
            withdrawals.append(
                {
                    "order_number": f"XFER-{_slugify(str(transfer['to_account'])).upper()}",
                    "exec_date": exec_date.isoformat(),
                    "plan_name": f"{award_type} Transfer",
                    "tx_type": "Transfer",
                    "price": round(price, 4),
                    "quantity": -abs(float(transfer["shares"])),
                    "net_amount": round(abs(float(transfer["shares"])) * price, 2),
                    "symbol": symbol,
                    "account_id": str(acct["id"]),
                }
            )
        payload = {
            "institution": str(acct["institution"]),
            "account_id": str(acct["id"]),
            "symbol": symbol,
            "company_name": str(grants_spec["company"]),
            "award_type": award_type,
            "as_of_date": as_of.isoformat(),
            "unvested_tranches": tranches,
            "releases_history": releases,
            "withdrawals": withdrawals,
            "fixture": {"generated_by": GENERATED_BY, "spec_sha": spec_mod.spec_sha(spec)},
        }
        path = out_dir / "Statements" / _slugify(acct["institution"]) / f"{symbol.lower()}_awards.grants.json"
        _write_json(path, payload)
        written.append(path)
    return written


# --------------------------------------------------------------------------- SSA


def earnings_table(ssa: dict[str, Any]) -> dict[str, int]:
    """Deterministic year -> earnings map from a named profile."""
    earnings = ssa.get("earnings") or {}
    if not earnings:
        return {}
    start = int(earnings.get("start") or 2000)
    end = int(earnings.get("end") or 2026)
    profile = str(earnings.get("profile") or "flat")
    cap_from = None
    base_key = profile
    if profile.startswith("cap_from"):
        base_key = "cap_from"
        parts = profile.split("_")
        if parts[-1].isdigit():
            cap_from = int(parts[-1])
    base, growth = _PROFILE_BASE.get(base_key, _PROFILE_BASE["flat"])
    gap_years = {start + 3, start + 4} if base_key == "gap_years" else set()
    table: dict[str, int] = {}
    value = float(base)
    for year in range(start, end + 1):
        amount = 0 if year in gap_years else value
        if cap_from is not None and year >= cap_from:
            amount = _OASDI_CAP_2026
        table[str(year)] = int(round(amount / 100.0) * 100)
        value *= growth
    return table


def emit_ssa_xml(spec: dict[str, Any], out_dir: Path) -> list[Path]:
    """Writes SSA statement XML in the schema the application already parses."""
    written: list[Path] = []
    for inv in spec.get("investors") or []:
        ssa = inv.get("ssa") or {}
        if not ssa.get("emit_xml"):
            continue
        table = earnings_table(ssa)
        if not table:
            continue
        rows = "\n".join(
            f'      <osss:Earnings startYear="{year}" endYear="{year}">\n'
            f"        <osss:FicaEarnings>{amount}</osss:FicaEarnings>\n"
            f"        <osss:MedicareEarnings>{amount}</osss:MedicareEarnings>\n"
            f"      </osss:Earnings>"
            for year, amount in sorted(table.items())
        )
        birth = str(inv["birth"])
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<osss:OnlineSocialSecurityStatementData xmlns:osss="http://ssa.gov/osss/schemas/2.0">\n'
            f"  <osss:FileCreationDate>{spec['as_of']}</osss:FileCreationDate>\n"
            "  <osss:UserInformation>\n"
            f"    <osss:Name>{inv['name']}</osss:Name>\n"
            f"    <osss:DateOfBirth>{birth}</osss:DateOfBirth>\n"
            "  </osss:UserInformation>\n"
            "  <osss:EarningsRecord>\n"
            f"{rows}\n"
            "  </osss:EarningsRecord>\n"
            "</osss:OnlineSocialSecurityStatementData>\n"
        )
        path = out_dir / "Statements" / "ssa" / f"{_slugify(inv['id'])}_ssa_statement.xml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(xml, encoding="utf-8")
        written.append(path)
    return written


# --------------------------------------------------------------------------- project.json


def build_project(spec: dict[str, Any], book: dict[str, dict[str, float]]) -> dict[str, Any]:
    """Builds `project.json` from the spec, leaving derived prose to the pipeline."""
    import config

    household = spec["household"]
    rates = household.get("tax_rates") or {}
    cfg = config.default_config()

    cfg["household_name"] = str(household["name"])
    cfg["primary_equity_symbol"] = str(household.get("primary_equity_symbol") or "")
    cfg["symbols"] = list(household.get("symbols") or sorted(book))
    cfg["reference_annual_burn"] = int(household.get("reference_annual_burn") or 60000)
    cfg["retirement_annual_burn"] = int(household.get("retirement_annual_burn") or 60000)
    cfg["tax_rates"] = {
        "state_code": str(household["state"]),
        "ltcg_top": float(rates.get("ltcg_top", 0.238)),
        "ordinary_top": float(rates.get("ordinary_top", 0.37)),
    }
    # Fixture specs also carry the state and Social Security effective rates. The engine
    # does not read these yet (finding LL-10); they are emitted so the fixtures are ready
    # for the fix and so a test can assert the gap explicitly rather than by omission.
    if rates.get("state_rate") is not None:
        cfg["tax_rates"]["state_rate"] = float(rates["state_rate"])
    if rates.get("ssn_effective") is not None:
        cfg["tax_rates"]["ssn_effective"] = float(rates["ssn_effective"])
    cfg["household_state"] = str(household["state"])
    if rates.get("ira_ordinary") is not None:
        cfg["sim_ira_ordinary_tax_rate"] = float(rates["ira_ordinary"])
    if rates.get("cash_interest") is not None:
        cfg["sim_cash_interest_tax_rate"] = float(rates["cash_interest"])
    if household.get("sim_asset_rates"):
        cfg["sim_asset_rates"] = dict(household["sim_asset_rates"])
    if household.get("ticker_aliases"):
        cfg["ticker_aliases"] = dict(household["ticker_aliases"])
    # Spec calls this `equity_plan`; the application's config key is still `gsu_forecast`
    # (a rename candidate, finding LL-26). The generator does the translation so fixture
    # specs never have to use the legacy vocabulary.
    if household.get("equity_plan"):
        cfg["gsu_forecast"] = {**cfg["gsu_forecast"], **dict(household["equity_plan"])}
    if household.get("protected_saves"):
        cfg["protected_saves"] = list(household["protected_saves"])
    if household.get("allow_custom_plugins") is not None:
        cfg["allow_custom_plugins"] = bool(household["allow_custom_plugins"])
    if household.get("allowed_plugins"):
        cfg["allowed_plugins"] = list(household["allowed_plugins"])

    cfg["investors"] = [_investor_entry(inv) for inv in (spec.get("investors") or [])]
    cfg["beneficiaries"] = [
        {
            "id": str(ben.get("id") or _slugify(ben["name"])),
            "name": str(ben["name"]),
            "birth_date": str(ben["birth"]),
            "milestone_age": int(ben.get("milestone_age") or 25),
        }
        for ben in (spec.get("beneficiaries") or [])
    ]
    cfg["accounts"] = {
        str(acct["id"]): {
            "institution": str(acct["institution"]),
            "account_name": str(acct.get("account_name") or acct["institution"]),
            "account_type": str(acct["account_type"]).upper(),
            "tax_status": str(acct["tax_status"]).upper(),
            "display_order": int(acct.get("display_order", 100)),
        }
        for acct in (spec.get("accounts") or [])
    }
    if not cfg["investors"]:
        cfg["investors"] = []
    return cfg


def _investor_entry(inv: dict[str, Any]) -> dict[str, Any]:
    ssa = inv.get("ssa") or {}
    table = earnings_table(ssa)
    years = sorted(int(y) for y in table) if table else []
    entry = {
        "id": str(inv["id"]),
        "name": str(inv["name"]),
        "birth_date": str(inv["birth"]),
        "milestone_age": int(inv.get("milestone_age") or 85),
        "is_rmd_owner": bool(inv.get("rmd_owner", False)),
        "ssn": {
            "fra_age": int(ssa.get("fra_age") or 67),
            "default_claim_age": int(ssa.get("claim_age") or 67),
            "work_span_label": f"{years[0]} - {years[-1]}" if years else "",
            "earnings_note": "",
            "external_file": "",
            "annual_earnings": {str(k): int(v) for k, v in sorted(table.items())} if not ssa.get("emit_xml") else {},
        },
    }
    if ssa.get("emit_xml"):
        entry["ssn"]["external_file"] = f"Statements/ssa/{_slugify(inv['id'])}_ssa_statement.xml"
    return entry


# --------------------------------------------------------------------------- scenarios


_BASE_SETTINGS: dict[str, Any] = {
    "simActiveModel": "simple",
    "simAnnualBurn": 72000,
    "simInflationPct": 2.5,
    "simIncludeSSN": True,
    "simIncludeUnvested": True,
    "simIncludeScheduledActions": True,
    "simScaleMode": "real",
    "simStressScenario": "gfc2008",
    "simCustomShockPct": -30,
    "simOrderPreset": "diversified_first",
    "simAssetOrder": [],
    "simHypotheticalAssets": [],
    "simScheduledActions": [],
    "mcReturnModel": "normal",
    "mcSimulations": 500,
    "mcSingleStockVolPct": 32.0,
    "mcBroadEquityVolPct": 16.0,
    "mcSingleStockCorr": 0.5,
    "taxMode": "net",
    "pastReleaseValMode": "net",
    "cohortStartFrom": 1929,
    "cohortCenterMode": "median",
    "cohortInflationMode": "cpi",
    "holdingForecasts": {},
}


def emit_scenarios(spec: dict[str, Any], out_dir: Path) -> list[Path]:
    """Writes `Scenarios/_index.json` plus one numbered file per scenario."""
    scenarios = spec.get("scenarios") or []
    scen_dir = out_dir / "Scenarios"
    scen_dir.mkdir(parents=True, exist_ok=True)
    stamp = f"{spec['as_of']}T09:00:00+00:00"
    models: list[dict[str, Any]] = []
    written: list[Path] = []
    base_settings = {**_BASE_SETTINGS}
    household = spec.get("household") or {}
    if household.get("retirement_annual_burn"):
        base_settings["simAnnualBurn"] = int(household["retirement_annual_burn"])

    active_id = None
    for idx, scenario in enumerate(scenarios, start=1):
        seq = f"{idx:03d}"
        settings = {**base_settings, **(scenario.get("settings") or {})}
        settings.update(scenario.get("settings_delta") or {})
        file_name = f"{seq}_{_slugify(scenario['name'])}.json"
        body = {
            "id": seq,
            "name": str(scenario["name"]),
            "is_protected": bool(scenario.get("protected", False)),
            "created_at": stamp,
            "updated_at": stamp,
            "author": {"type": "fixture", "model": None, "prompt_type": None},
            "notes_md": str(scenario.get("notes_md") or ""),
            "settings": settings,
            "kpi_summary": {},
            "snapshot": None,
        }
        _write_json(scen_dir / file_name, body)
        written.append(scen_dir / file_name)
        models.append(
            {
                "id": seq,
                "name": str(scenario["name"]),
                "file": file_name,
                "snapshot": None,
                "created_at": stamp,
                "updated_at": stamp,
            }
        )
        if scenario.get("active") or (active_id is None and idx == 1):
            active_id = seq

    _write_json(
        scen_dir / "_index.json",
        {
            "version": 1,
            "active_id": active_id,
            "next_seq": len(scenarios) + 1,
            "models": models,
        },
    )
    written.append(scen_dir / "_index.json")
    return written


# --------------------------------------------------------------------------- privacy


def emit_privacy_registry(spec: dict[str, Any], out_dir: Path) -> Path:
    """
    Writes the privacy settings for the fixture.

    Only the settings block is emitted; the entity table is rebuilt from the portfolio by
    `privacy.registry` on first use, which is the behaviour under test.
    """
    privacy = spec.get("privacy") or {}
    demo = privacy.get("demo_mode") or {}
    body = {
        "version": 1,
        "ai_shield": {
            "mode": str(privacy.get("ai_shield") or "identity_aliased"),
            "block_inapp_agy_cli": True,
            "dob_granularity": "year_month",
            "notes_policy": "translate",
        },
        "demo_mode": {
            "enabled": bool(demo.get("enabled", False)),
            "scale_factor": float(demo.get("scale", 1.0)),
            "seed": str(demo.get("seed") or f"demo-{int(spec['seed']) % 9000 + 1000}"),
            "dob_granularity": str(demo.get("dob_granularity") or "shift_years"),
        },
        "mcp_server": {"enabled": bool((privacy.get("mcp_server") or {}).get("enabled", False))},
        "synonyms": dict(privacy.get("synonyms") or {}),
        "entities": [],
        "fixture": {"generated_by": GENERATED_BY, "spec_sha": spec_mod.spec_sha(spec)},
    }
    path = out_dir / "privacy_registry.json"
    _write_json(path, body)
    return path


# --------------------------------------------------------------------------- extras


def emit_plugins(spec: dict[str, Any], out_dir: Path) -> list[Path]:
    written: list[Path] = []
    for plugin in spec.get("plugins") or []:
        path = out_dir / "Plugins" / str(plugin["file"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(plugin["content"]), encoding="utf-8")
        written.append(path)
    return written


def emit_extra_files(spec: dict[str, Any], out_dir: Path) -> list[Path]:
    """
    Writes raw files verbatim.

    Used by the adversarial fixture for things a generator should not "helpfully" fix:
    invalid JSON, a duplicate statement, an unsupported extension, a future-dated file.
    """
    written: list[Path] = []
    for extra in spec.get("extra_files") or []:
        path = out_dir / str(extra["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        if extra.get("copy_of"):
            shutil.copyfile(out_dir / str(extra["copy_of"]), path)
        else:
            content = extra.get("content")
            text = content if isinstance(content, str) else json.dumps(content, indent=2) + "\n"
            path.write_text(text, encoding="utf-8")
        written.append(path)
    return written


# --------------------------------------------------------------------------- top level


def build_portfolio(spec: dict[str, Any], out_dir: Path) -> dict[str, Any]:
    """
    Generates the complete fixture portfolio folder for `spec`.

    Returns a manifest of what was written, which `docs.py` turns into the README.
    """
    out_dir = Path(out_dir)
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    book = build_price_book(spec)
    _write_json(out_dir / "market_prices.json", {k: book[k] for k in sorted(book)})

    project = build_project(spec, book)
    _write_json(out_dir / "project.json", project)

    statements = emit_statements(spec, out_dir, book)
    grants = emit_grants(spec, out_dir, book)
    ssa = emit_ssa_xml(spec, out_dir)
    scenarios = emit_scenarios(spec, out_dir)
    emit_privacy_registry(spec, out_dir)
    plugins = emit_plugins(spec, out_dir)
    extras = emit_extra_files(spec, out_dir)

    (out_dir / "Statements").mkdir(exist_ok=True)
    (out_dir / "Assets").mkdir(exist_ok=True)

    return {
        "slug": str(spec["name"]),
        "spec_sha": spec_mod.spec_sha(spec),
        "statements": [p.relative_to(out_dir).as_posix() for p in statements],
        "grants": [p.relative_to(out_dir).as_posix() for p in grants],
        "ssa": [p.relative_to(out_dir).as_posix() for p in ssa],
        "scenarios": [p.relative_to(out_dir).as_posix() for p in scenarios],
        "plugins": [p.relative_to(out_dir).as_posix() for p in plugins],
        "extras": [p.relative_to(out_dir).as_posix() for p in extras],
        "symbols": sorted(book),
        "project": project,
    }
