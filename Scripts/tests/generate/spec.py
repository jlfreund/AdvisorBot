#!/usr/bin/env python3
"""
Fixture portfolio spec loading and validation.

Specs are JSON (not YAML) so the generator stays stdlib-only, matching the application's
own dependency promise; `fixtures/specs/SCHEMA.md` is the human-facing reference and is
checked against this module by a scan test.

Validation is deliberately strict: an unknown key is an error, because a silently ignored
spec field would produce a fixture that does not exercise what its README claims.
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Any

SPEC_VERSION = 1

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")

TOP_KEYS = {
    "spec_version", "name", "seed", "as_of", "title", "purpose", "exercises",
    "household", "investors", "beneficiaries", "accounts", "market_prices",
    "scenarios", "privacy", "invariants", "plugins", "extra_files", "notes",
}
REQUIRED_TOP = ("name", "seed", "as_of", "title", "household")

HOUSEHOLD_KEYS = {
    "name", "state", "state_label", "tax_rates", "primary_equity_symbol", "symbols",
    "reference_annual_burn", "retirement_annual_burn", "protected_saves",
    "sim_asset_rates", "sim_cash_interest_tax_rate", "sim_ira_ordinary_tax_rate",
    "equity_plan",
    "allow_custom_plugins", "allowed_plugins", "ticker_aliases",
}
TAX_RATE_KEYS = {"ltcg_top", "ordinary_top", "state_rate", "ssn_effective", "ira_ordinary", "cash_interest"}

INVESTOR_KEYS = {"id", "name", "birth", "milestone_age", "rmd_owner", "ssa"}
SSA_KEYS = {"earnings", "claim_age", "fra_age", "emit_xml", "note"}
EARNINGS_KEYS = {"start", "end", "profile"}
EARNINGS_PROFILES = ("cap_from", "mid_rising", "gap_years", "flat", "zero")

BENEFICIARY_KEYS = {"id", "name", "birth", "milestone_age"}

ACCOUNT_KEYS = {
    "id", "institution", "account_name", "account_type", "tax_status", "display_order",
    "periods", "positions", "grants", "statement_subdir", "quirks",
}
PERIOD_KEYS = {"from", "to", "cadence"}

POSITION_KINDS = ("ticker", "cash", "annuity", "muni", "contract_fund")
POSITION_KEYS = {
    "kind", "symbol", "name", "asset_class", "risk_class", "stack_bucket", "lots",
    "value", "yield_pct", "monthly_drift", "credited_rate_pct", "issue_date",
    "maturity_date", "premium", "surrender_schedule", "segments", "par", "coupon_pct",
    "state", "quantity", "cost_basis", "price", "split", "metadata", "quote_symbol",
    "is_scrapeable", "acquisition_date", "expense_ratio_pct",
}
LOT_KEYS = {"qty", "acquired", "cost_per_share", "label"}

GRANTS_KEYS = {
    "symbol", "company", "award_type", "withholding_pct", "grants", "in_kind_transfer",
    "price_plan",
}
GRANT_KEYS = {"id", "grant_date", "total_shares", "cadence", "first_vest", "vests", "cliff_months"}
TRANSFER_KEYS = {"shares", "vest_date", "to_account", "exec_date"}

SPLIT_KEYS = {"effective", "ratio", "notes", "pre_quantity"}

SCENARIO_KEYS = {"name", "protected", "settings", "settings_delta", "notes_md", "active"}
PRIVACY_KEYS = {"ai_shield", "demo_mode", "synonyms", "mcp_server"}
DEMO_KEYS = {"enabled", "scale", "seed", "dob_granularity"}
INVARIANT_KEYS = {"path", "equals", "contains", "absent", "note", "source", "known_gap"}


class SpecError(ValueError):
    """Raised when a spec is malformed; the message names the offending path."""


def _reject_unknown(label: str, raw: dict[str, Any], allowed: set[str]) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise SpecError(f"{label}: unknown key(s): {', '.join(unknown)}")


def _require(label: str, raw: dict[str, Any], keys: tuple[str, ...]) -> None:
    for key in keys:
        if raw.get(key) in (None, ""):
            raise SpecError(f"{label}: `{key}` is required")


def _check_date(label: str, value: Any) -> str:
    text = str(value)
    if not _DATE_RE.match(text):
        raise SpecError(f"{label}: expected YYYY-MM-DD, got {value!r}")
    date.fromisoformat(text)
    return text


def _check_month(label: str, value: Any) -> str:
    text = str(value)
    if not _MONTH_RE.match(text):
        raise SpecError(f"{label}: expected YYYY-MM, got {value!r}")
    return text


def validate(spec: dict[str, Any], *, denylist: list[str] | None = None) -> dict[str, Any]:
    """Validates a spec in place and returns it. Raises `SpecError` with a path on failure."""
    if not isinstance(spec, dict):
        raise SpecError("spec must be a JSON object")
    _reject_unknown("spec", spec, TOP_KEYS)
    _require("spec", spec, REQUIRED_TOP)
    if int(spec.get("spec_version", SPEC_VERSION)) != SPEC_VERSION:
        raise SpecError(f"spec: unsupported spec_version {spec.get('spec_version')!r}")
    name = str(spec["name"])
    if not re.match(r"^p\d+_[a-z0-9_]+$", name):
        raise SpecError(f"spec: `name` must look like p<N>_<slug>, got {name!r}")
    _check_date("spec.as_of", spec["as_of"])

    household = spec["household"]
    if not isinstance(household, dict):
        raise SpecError("spec.household: must be an object")
    _reject_unknown("spec.household", household, HOUSEHOLD_KEYS)
    _require("spec.household", household, ("name", "state"))
    rates = household.get("tax_rates") or {}
    _reject_unknown("spec.household.tax_rates", rates, TAX_RATE_KEYS)

    for idx, inv in enumerate(spec.get("investors") or []):
        label = f"spec.investors[{idx}]"
        _reject_unknown(label, inv, INVESTOR_KEYS)
        _require(label, inv, ("id", "name", "birth"))
        _check_date(f"{label}.birth", inv["birth"])
        ssa = inv.get("ssa") or {}
        _reject_unknown(f"{label}.ssa", ssa, SSA_KEYS)
        earnings = ssa.get("earnings") or {}
        _reject_unknown(f"{label}.ssa.earnings", earnings, EARNINGS_KEYS)
        profile = str(earnings.get("profile") or "flat")
        if earnings and not profile.startswith(EARNINGS_PROFILES):
            raise SpecError(
                f"{label}.ssa.earnings.profile: unknown profile {profile!r} "
                f"(expected one of {EARNINGS_PROFILES}, e.g. 'cap_from_2012')"
            )

    for idx, ben in enumerate(spec.get("beneficiaries") or []):
        label = f"spec.beneficiaries[{idx}]"
        _reject_unknown(label, ben, BENEFICIARY_KEYS)
        _require(label, ben, ("name", "birth"))
        _check_date(f"{label}.birth", ben["birth"])

    seen_accounts: set[str] = set()
    for idx, acct in enumerate(spec.get("accounts") or []):
        label = f"spec.accounts[{idx}]"
        _reject_unknown(label, acct, ACCOUNT_KEYS)
        _require(label, acct, ("id", "institution", "account_type", "tax_status"))
        acct_id = str(acct["id"])
        if acct_id in seen_accounts:
            raise SpecError(f"{label}: duplicate account id {acct_id!r}")
        seen_accounts.add(acct_id)
        periods = acct.get("periods") or {}
        _reject_unknown(f"{label}.periods", periods, PERIOD_KEYS)
        if periods:
            _check_month(f"{label}.periods.from", periods.get("from"))
            _check_month(f"{label}.periods.to", periods.get("to"))
        for pidx, pos in enumerate(acct.get("positions") or []):
            plabel = f"{label}.positions[{pidx}]"
            _reject_unknown(plabel, pos, POSITION_KEYS)
            kind = str(pos.get("kind") or "ticker")
            if kind not in POSITION_KINDS:
                raise SpecError(f"{plabel}.kind: expected one of {POSITION_KINDS}, got {kind!r}")
            _require(plabel, pos, ("name",))
            if pos.get("split") is not None:
                _reject_unknown(f"{plabel}.split", pos["split"], SPLIT_KEYS)
                _require(f"{plabel}.split", pos["split"], ("effective", "ratio"))
                _check_date(f"{plabel}.split.effective", pos["split"]["effective"])
            for lidx, lot in enumerate(pos.get("lots") or []):
                llabel = f"{plabel}.lots[{lidx}]"
                _reject_unknown(llabel, lot, LOT_KEYS)
                _require(llabel, lot, ("qty", "acquired"))
                _check_date(f"{llabel}.acquired", lot["acquired"])
        grants = acct.get("grants")
        if grants is not None:
            glabel = f"{label}.grants"
            _reject_unknown(glabel, grants, GRANTS_KEYS)
            _require(glabel, grants, ("symbol", "company"))
            for gidx, grant in enumerate(grants.get("grants") or []):
                gg = f"{glabel}.grants[{gidx}]"
                _reject_unknown(gg, grant, GRANT_KEYS)
                _require(gg, grant, ("id", "grant_date", "total_shares", "first_vest"))
                _check_date(f"{gg}.grant_date", grant["grant_date"])
                _check_date(f"{gg}.first_vest", grant["first_vest"])
            transfer = grants.get("in_kind_transfer")
            if transfer:
                _reject_unknown(f"{glabel}.in_kind_transfer", transfer, TRANSFER_KEYS)
                _require(f"{glabel}.in_kind_transfer", transfer, ("shares", "vest_date", "to_account"))

    for idx, scenario in enumerate(spec.get("scenarios") or []):
        label = f"spec.scenarios[{idx}]"
        _reject_unknown(label, scenario, SCENARIO_KEYS)
        _require(label, scenario, ("name",))

    privacy = spec.get("privacy") or {}
    _reject_unknown("spec.privacy", privacy, PRIVACY_KEYS)
    _reject_unknown("spec.privacy.demo_mode", privacy.get("demo_mode") or {}, DEMO_KEYS)

    for idx, inv in enumerate(spec.get("invariants") or []):
        label = f"spec.invariants[{idx}]"
        _reject_unknown(label, inv, INVARIANT_KEYS)
        _require(label, inv, ("path",))

    _check_vocabulary(spec, denylist or [])
    return spec


def _iter_values(node: Any) -> Any:
    """Yields every string value in a spec (keys are app schema names, not fixture content)."""
    if isinstance(node, dict):
        for value in node.values():
            yield from _iter_values(value)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_values(item)
    elif isinstance(node, str):
        yield node


def _check_vocabulary(spec: dict[str, Any], denylist: list[str]) -> None:
    """
    Refuses any spec whose *content* reuses the legacy vocabulary.

    A fixture that accidentally named the original employer or custodian would make the
    portability scan pass for the wrong reason, so the generator will not build it. Only
    string values are scanned: dictionary keys are the application's own schema names
    (`gsu_forecast`, `simGoogGrowthPct`), which are themselves a rename candidate rather
    than fixture vocabulary.
    """
    if not denylist:
        return
    from tests.support import scans

    blob = "\n".join(_iter_values(spec))
    hits = scans.find_terms(blob, denylist)
    if hits:
        raise SpecError(
            f"spec {spec.get('name')!r} reuses denylisted legacy vocabulary: {', '.join(hits)}. "
            f"Fixtures must be unlike the original portfolio so leaks are detectable."
        )


def load(path: Path, *, denylist: list[str] | None = None) -> dict[str, Any]:
    spec = json.loads(Path(path).read_text(encoding="utf-8"))
    return validate(spec, denylist=denylist)


def spec_sha(spec: dict[str, Any]) -> str:
    """Stable hash of a spec, stamped into generated artefacts for the drift scan."""
    import hashlib

    blob = json.dumps(spec, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]
