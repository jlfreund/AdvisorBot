#!/usr/bin/env python3
"""
Demo Mode Projection Engine (Case A: Screen-Share & Public Export Obfuscation).

Transforms `ReportPayload` and `saves_payload` in memory:
  1. Replaces real personal, custodian, account, equity, fund, manager, carrier,
     contract, and scenario identifiers with deterministic Demo Mode proper names
     from `assign_demo_personas()`, including composite `forecast_key` / `holdingForecasts` keys.
  2. Scales all USD currency fields and asset share counts uniformly by secret `k`
     (plus optional display rounding) while preserving percentages, rates, ages,
     dates, and Monte Carlo return distributions.
  3. Strips custom household crest images back to `Assets/default_badge.svg`.
  4. Never embeds `k` in client-visible `privacy_status` metadata (Decision D4).
"""

from __future__ import annotations

import copy
import hashlib
import re
from pathlib import Path
from typing import Any

from privacy.translator import (
    COMPOSITE_ID_TEXT_RE,
    is_exact_case_synonym,
    mask_dob_year_month,
)

# Currency keys scaled by k (rounded by `round_to` for high-level KPIs/totals or 2 decimals for line items)
# SECURITY INVARIANT (SA2-10 / SA3-03 / SA3-08):
# Keep all payload, LLM overview (`total_value_usd`, `burn_ref_usd`), annuity (`surrender_value`), and
# simulator bar / milestone / drawdown / Monte Carlo USD keys listed here (plus `*_usd` suffix & `ending_real`/`ending_histogram`
# range bounds in `project_obj`) so Demo Mode never leaks 1.0x unscaled household dollar figures.
# SECURITY INVARIANT (SA5-01 / SA5-07):
# NEVER place statutory constants (`bend1`, `bend2`, `oasdi_cap` — whose public IRS/SSA values like `$1,286` / `$7,749`
# in 2026 would create a 1-division secret-`k` oracle if scaled) or `[0, 1]` fractional weights (`weight`, used in
# `constituent_forecasts[*].weight` for `D7` group return curves) inside `_USD_KEYS`.
_USD_KEYS = {
    "total_vested", "vestedTotal", "projRelGross", "wow_change", "live_scraped_val", "anchored_val",
    "goog_available_val", "goog_vested_to_date_net_val", "goog_vested_to_date_gross_val",
    "goog_vested_val", "future_gsu_gross_val", "future_gsu_net_val",
    "totRelNetVal", "totRelGrossVal", "totTodayNetVal", "totTodayGrossVal",
    "relNetVal", "relGrossVal", "todayNetVal", "todayGrossVal",
    "rel_gross_val", "rel_net_val", "today_gross_val", "today_net_val",
    "market_value", "stmt_market_value", "value", "total", "total_value", "total_val", "total_value_usd",
    "current_value", "cost_basis", "basis", "unrealized_gain", "gain",
    "balance", "principal", "amount", "initial_balance", "annual_contribution",
    "simAnnualBurn", "annual_burn", "annualBurn", "burn_ref_usd", "mcSuccessThresholdUsd", "threshold",
    "success_threshold_usd",
    "median_end_real", "end_85_real", "end_85_nom", "end_real", "peak_real",
    "end_yr40_nominal", "end_yr40_real", "start_value",
    "net_worth_at_milestone", "average_salary", "fra_monthly_benefit",
    "annual_benefit", "monthly_benefit", "projected_value", "cash_value",
    "goog", "non_goog", "cash", "vested_val", "unvested_val", "gross_val", "net_val",
    "tax_withheld", "net_proceeds", "rollup_value", "invested_total", "vested_total",
    "non_goog_total", "goog_total",
    "reference_annual_burn", "retirement_annual_burn", "retirement_goal_val",
    "available_val", "future_gross_val", "future_net_val", "est_net_val",
    "vested_to_date_net_val", "vested_to_date_gross_val",
    "vested_rel_val_today", "vested_rel_gross_val_today",
    "val_today_gross", "val_today_net", "val_rel_gross", "val_rel_net",
    "recent_change_val", "principal_val", "cum_tax_real",
    "p10", "p25", "p50", "p75", "p90", "surrender_val", "surrender_value", "accrued_interest",
    "sum_indexed_35", "aime", "tier1_90pct", "tier2_32pct", "tier3_15pct",
    "pia_monthly_2026", "pia_annual_2026",
    "pooledNet", "shortfall", "requested", "funded", "tax_paid", "prior_dec31",
    "totalVal", "totalRealVal", "googVal", "nonGoogVal", "primaryVal", "nonPrimaryVal",
    "burn", "netBurn", "taxPaid", "ssnIncome", "rmdForced",
    "startVal", "endVal", "liquidVal", "illiquidVal",
    "endNominal", "endReal", "nominal", "real",
    "monthlyBurn", "targetMonthlyBurn", "rawMonthlyExpense", "unmetNetBurn",
    "monthlyTax", "monthlyGrossSold", "monthlyYieldTax",
    "ssnNetMo", "ssnTaxMo", "ssnGrossMo", "growthGain",
    "monthlyRmdGross", "monthlyRmdTax",
    "monthlyLossHarvested", "monthlyLossSheltered", "monthlyTaxSaved",
    "cumGross", "cumNet", "cumTax", "cumRealGross", "cumRealNet", "cumRealTax",
    "cumTaxSaved", "cumLossHarvested",
    "annual_take_home", "annual_budget_offset", "employee_annual", "employer_match_annual",
    "monthlyWorkContrib", "monthlyPaycheckOffset", "cumWorkContrib", "cumRealWorkContrib",
    "schedWithdrawNom", "schedWithdrawReal", "schedDepositNom", "schedTransferNom",
    "nomAmt", "todayAmt", "netAmt",
    "nominal_usd", "real_today_usd", "cum_tax_nominal_usd", "cum_tax_real_usd",
    "cum_gross_sold_nominal_usd", "cum_gross_sold_real_usd", "cum_net_spend_nominal_usd",
    "total_gross_sold", "total_tax_paid", "loss_harvested", "tax_saved_by_loss",
}

_QTY_KEYS = {
    "quantity", "shares", "projRelShares",
    "totGrossShares", "totNetShares", "grossShares", "netShares",
    "goog_available_shares", "goog_vested_shares",
    "goog_vested_to_date_net_shares", "goog_vested_to_date_gross_shares",
    "future_gsu_gross_shares", "gross_shares", "net_shares", "vested_shares",
    "unvested_shares", "total_shares", "rem_gross_shares", "rem_net_shares",
    "available_shares", "future_gross_shares", "future_est_net_shares", "est_net_shares",
    "rem_est_net_shares",
    "vested_gross_shares", "vested_net_shares", "total_qty", "est_net_qty",
    "orig_qty", "total_award_shares", "gross_sh", "net_sh", "deus",
}

_SCALED_VAL_DICT_KEYS = {"annual_earnings", "accounts", "buckets", "assets", "assetVals"}

_COMPOSITE_ID_KEYS = {
    "id", "asset_id", "forecast_key", "parent_forecast_keys",
    "simAssetOrder", "holdingForecasts", "holdingHalfLives", "holdingHalfLife", "holdingFeePct",
    "principal_overrides", "account_id", "security_id",
    "target_asset_id", "match_target_asset_id", "source_asset_id", "from_asset_id", "to_asset_id",
    "from_id", "to_id", "source_id", "target_id",
    "holding_key", "group_id", "grant_id", "file", "filename",
}

_FILENAME_EXT_RE = re.compile(r"^(.+)(\.(?:json|png|jpg|jpeg|svg|xml|pdf|csv|xlsx))$", flags=re.IGNORECASE)
_FALLBACK_ACCT_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9])ACCT([-_])(\d{2,3})(?![A-Za-z0-9])", flags=re.IGNORECASE)
_FALLBACK_ANN_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9])ANN([-_])(\d{2,3})(?![A-Za-z0-9])", flags=re.IGNORECASE)


class DemoTranslator:
    """Projects strings & composite simulator keys into realistic Demo Mode personas."""

    def __init__(self, registry_cfg: dict[str, Any]) -> None:
        self.cfg = copy.deepcopy(registry_cfg or {})
        dm = self.cfg.get("demo_mode") or {}
        self.scale_factor = float(dm.get("scale_factor") or 0.25)
        self.round_to = max(1, int(dm.get("round_to") or 1000))

        self._ci_map: dict[str, str] = {}
        self._cs_map: dict[str, str] = {}
        self._seg_map: dict[str, str] = {}
        self._grant_id_cache: dict[str, str] = {}
        self._investor_name_map: dict[str, str] = {}
        self._claim_key_map: dict[str, str] = {}

        exact_pairs: list[tuple[str, str]] = []
        ci_pairs: list[tuple[str, str]] = []

        for ent in self.cfg.get("entities") or []:
            if not isinstance(ent, dict) or not ent.get("enabled", True):
                continue
            cat = str(ent.get("category") or "").lower()
            canon = str(ent.get("canonical") or "").strip()
            demo_val = str(ent.get("demo_value") or "").strip()
            dmeta = ent.get("demo_meta") if isinstance(ent.get("demo_meta"), dict) else {}
            if not canon or not demo_val:
                continue
            if cat == "investor":
                canon_first = canon.split()[0].lower()
                demo_first = str(dmeta.get("first") or demo_val.split()[0])
                self._investor_name_map[canon_first] = demo_first.lower()
                self._claim_key_map[f"ssn{canon_first.title()}ClaimAge"] = f"ssn{demo_first.title()}ClaimAge"
            syns = [str(s).strip() for s in (ent.get("synonyms") or []) if str(s).strip()]
            if canon not in syns:
                syns.insert(0, canon)

            for syn in syns:
                repl = self._pick_contextual_demo_replacement(cat, syn, canon, demo_val, dmeta)
                self._seg_map[syn.lower()] = repl.replace(" ", "_").replace("-", "_") if "_" in syn else repl
                if cat == "account" and "-" in demo_val and "_" in syn:
                    self._seg_map[syn.lower()] = demo_val.replace("-", "_")
                elif cat == "account" and "-" in demo_val and "-" in syn:
                    self._seg_map[syn.lower()] = demo_val
                elif cat == "equity":
                    self._seg_map[syn.lower()] = str(dmeta.get("symbol") or demo_val)
                elif cat == "custodian":
                    codes = dmeta.get("codes") or []
                    self._seg_map[syn.lower()] = str(codes[0] if codes else demo_val).lower()
                elif cat == "grant":
                    self._seg_map[syn.lower()] = demo_val
                elif cat == "carrier":
                    self._seg_map[syn.lower()] = demo_val.split()[0].lower()

                if is_exact_case_synonym(syn, cat):
                    exact_pairs.append((syn, repl))
                    self._cs_map[syn] = repl
                else:
                    ci_pairs.append((syn, repl))
                    self._ci_map[syn.lower()] = repl

        combined = [(s, r, True) for s, r in exact_pairs] + [(s, r, False) for s, r in ci_pairs]
        combined.sort(key=lambda x: (-len(x[0]), x[0].lower()))
        if combined:
            parts = [re.escape(s) if cs else f"(?i:{re.escape(s)})" for s, _r, cs in combined]
            self._re: re.Pattern[str] | None = re.compile(rf"(?<![A-Za-z0-9])(?:{'|'.join(parts)})(?![A-Za-z0-9])")
        else:
            self._re = None
        self._seg_sorted = sorted(self._seg_map.items(), key=lambda kv: (-len(kv[0]), kv[0]))
        cs_lower_to_orig = {k.lower(): k for k in self._cs_map}
        # Precompile segment regexes once rather than compiling on every map_composite_id() call
        self._seg_patterns: list[tuple[re.Pattern[str], re.Pattern[str], str]] = []
        for seg_low, seg_repl in self._seg_sorted:
            if len(seg_low) < 2:
                continue
            ci_pat = re.compile(rf"(?<![A-Za-z0-9]){re.escape(seg_low)}(?![A-Za-z0-9])", flags=re.IGNORECASE)
            cs_orig = cs_lower_to_orig.get(seg_low)
            strict_pat = (
                re.compile(rf"(?<![A-Za-z0-9]){re.escape(cs_orig)}(?![A-Za-z0-9])")
                if (cs_orig is not None and seg_low not in self._ci_map)
                else ci_pat
            )
            self._seg_patterns.append((strict_pat, ci_pat, seg_repl))

    @staticmethod
    def _pick_contextual_demo_replacement(
        cat: str, syn: str, canon: str, demo_val: str, dmeta: dict[str, Any]
    ) -> str:
        s_low = syn.lower()
        if cat == "custodian":
            codes = dmeta.get("codes") or []
            if len(syn) <= 3 and syn.isupper() and codes:
                return str(codes[-1] if syn == "WF" and len(codes) > 1 else codes[0])
            if "work" in s_low or "advisors" in s_low or "bank" in s_low:
                return demo_val
            return str(dmeta.get("short") or demo_val)
        if cat in ("investor", "beneficiary"):
            first = str(dmeta.get("first") or demo_val.split()[0])
            full = str(dmeta.get("full") or demo_val)
            if " " in syn:
                return full
            if syn.endswith("s") and not canon.lower().endswith("s") and syn[:-1].lower() == canon.split()[0].lower():
                return f"{first}'s"
            return first
        if cat == "household":
            surname = str(dmeta.get("surname") or demo_val.split()[0])
            return demo_val if " " in syn else surname
        if cat == "equity":
            if s_low == "gsu" or s_low == "rsu":
                return str(dmeta.get("award_type") or "RSU")
            if "class" in s_low or "inc" in s_low or "corp" in s_low:
                return str(dmeta.get("company") or demo_val)
            if "plan" in s_low:
                return str(dmeta.get("plan") or f"{demo_val} Stock Plan")
            if len(syn) >= 5 and not syn.isupper():
                return str(dmeta.get("short") or demo_val)
            return str(dmeta.get("symbol") or demo_val)
        if cat == "fund":
            if len(syn) == 5 and syn.isupper():
                return str(dmeta.get("symbol") or demo_val)
            return str(dmeta.get("name") or demo_val)
        if cat == "contract":
            return f"CONTRACT:{demo_val}" if syn.upper().startswith("CONTRACT:") else demo_val
        if cat == "scenario":
            return str(dmeta.get("filename") or demo_val) if syn.endswith(".json") else demo_val
        if cat == "grant":
            return demo_val
        if cat == "account" and "_" in syn and "-" in demo_val:
            return demo_val.replace("-", "_")
        return demo_val

    def _replace_match(self, m: re.Match[str]) -> str:
        txt = m.group(0)
        if txt in self._cs_map:
            return self._cs_map[txt]
        return self._ci_map.get(txt.lower(), txt)

    def _replace_fallback_tokens(self, s: str) -> str:
        """Defense-in-depth (`DP-05`): maps any residual `ACCT-01..99` / `ANN-01..99` tokens into realistic Demo strings."""
        seed = str((self.cfg.get("demo_mode") or {}).get("demo_seed") or "demo-4821")

        def _acct_sub(m: re.Match[str]) -> str:
            sep = m.group(1)
            idx = int(m.group(2))
            h = hashlib.sha256(f"{seed}:acct:{idx}".encode("utf-8")).digest()
            p_num = 4810 + (int.from_bytes(h[:2], "big") % 400)
            s_num = 1000 + (int.from_bytes(h[2:4], "big") % 8999)
            out_sep = "_" if sep == "_" else "-"
            return f"{p_num}{out_sep}{s_num}"

        def _ann_sub(m: re.Match[str]) -> str:
            sep = m.group(1)
            idx = int(m.group(2))
            out_sep = "_" if sep == "_" else "-"
            return f"POL{out_sep}{7040 + idx}"

        out = _FALLBACK_ACCT_TOKEN_RE.sub(_acct_sub, s)
        out = _FALLBACK_ANN_TOKEN_RE.sub(_ann_sub, out)
        return out

    def map_composite_id(self, raw_id: str, *, strict_short_codes: bool = False) -> str:
        s = str(raw_id or "")
        if not s:
            return s
        if s.lower() in self._ci_map:
            return self._ci_map[s.lower()]
        m_ext = _FILENAME_EXT_RE.match(s)
        if m_ext:
            stem, ext = m_ext.group(1), m_ext.group(2)
            mapped_stem = self.map_composite_id(stem, strict_short_codes=strict_short_codes)
            mapped_stem = self.map_text(mapped_stem).replace(" ", "_")
            return f"{mapped_stem}{ext}"
        out = s
        has_digit = any(ch.isdigit() for ch in s)
        for strict_pat, ci_pat, seg_repl in self._seg_patterns:
            pat = strict_pat if (strict_short_codes and not has_digit) else ci_pat
            out = pat.sub(seg_repl, out)
        return self._replace_fallback_tokens(out)

    def mask_grant_id(self, gid: str) -> str:
        s = str(gid or "").strip()
        if not s or s.startswith("GR-") or s == "RET-PRIOR":
            return s
        if s in self._grant_id_cache:
            return self._grant_id_cache[s]
        res = self.map_composite_id(s)
        if res != s:
            self._grant_id_cache[s] = res
            return res
        res = self.map_text(s)
        if res != s:
            self._grant_id_cache[s] = res
            return res
        if s.upper().startswith("PRIOR"):
            res = "RET-PRIOR"
        else:
            seed = str((self.cfg.get("demo_mode") or {}).get("demo_seed") or "demo-4821")
            h = hashlib.sha256(f"{seed}:{s}".encode()).digest()
            num = (int.from_bytes(h[:2], "big") % 8999) + 1000
            res = f"GR-{num}"
        self._grant_id_cache[s] = res
        return res

    @staticmethod
    def _mask_dob_string(val: str) -> str:
        return mask_dob_year_month(val)

    def map_text(self, text: str) -> str:
        if not isinstance(text, str) or not text:
            return text
        out = COMPOSITE_ID_TEXT_RE.sub(lambda m: self.map_composite_id(m.group(0), strict_short_codes=True), text)
        if self._re is not None:
            out = self._re.sub(self._replace_match, out)
        out = self._replace_fallback_tokens(out)
        # Strip host OS usernames embedded in local filesystem paths
        out = re.sub(r"/(Users|home)/[^/\s\"':<>]+", r"/\1/demo", out)
        out = re.sub(r"([A-Za-z]:\\Users\\)[^\\\s\"':<>]+", r"\1demo", out)
        return out

    def scale_usd(self, val: Any, *, key_name: str = "") -> Any:
        if not isinstance(val, (int, float)) or isinstance(val, bool):
            return val
        scaled = float(val) * self.scale_factor
        # KPI / summary balances round cleanly to nearest round_to when magnitude >= round_to * 2
        if self.round_to > 1 and abs(scaled) >= self.round_to and key_name in (
            "total_vested", "simAnnualBurn", "mcSuccessThresholdUsd",
            "median_end_real", "end_85_real", "end_85_nom", "end_real", "peak_real",
            "net_worth_at_milestone",
        ):
            r = self.round_to
            return round(scaled / r) * r
        return round(scaled, 2)

    def scale_qty(self, val: Any) -> Any:
        if not isinstance(val, (int, float)) or isinstance(val, bool):
            return val
        return round(float(val) * self.scale_factor, 3)

    def _scale_inline_usd_text(self, s: str) -> str:
        """Scales formatted currency fragments like `$1,234,567` or `Cost Basis: $450k` in pre-rendered display labels."""
        def _sub_dollar(m: re.Match[str]) -> str:
            num_str = m.group(1)
            raw_num = float(num_str.replace(",", ""))
            suffix = (m.group(2) or "").lower()
            mult = 1_000_000.0 if suffix == "m" else (1_000.0 if suffix == "k" else 1.0)
            scaled = raw_num * mult * self.scale_factor
            if suffix == "m" or (not suffix and abs(scaled) >= 1_000_000.0 and "." not in num_str):
                return f"${scaled / 1_000_000.0:,.2f}M"
            if suffix == "k":
                return f"${scaled / 1_000.0:,.0f}K"
            if "." in num_str:
                return f"${scaled:,.2f}"
            return f"${scaled:,.0f}"

        out = re.sub(
            r"\$([0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]+)?|[0-9]+(?:\.[0-9]+)?)(?:\s*([kKmM]))?\b",
            _sub_dollar,
            s,
        )

        def _sub_shares(m: re.Match[str]) -> str:
            raw_s = m.group(1)
            raw_val = float(raw_s.replace(",", ""))
            scaled = raw_val * self.scale_factor
            unit = m.group(2)
            if "." in raw_s:
                decimals = len(raw_s.split(".")[1])
                return f"{scaled:,.{min(decimals, 3)}f} {unit}"
            if scaled.is_integer():
                return f"{scaled:,.0f} {unit}"
            return f"{scaled:,.1f} {unit}"

        # Scale inline share strings like "1,234 shs", "10,607 total sh", "18,608.505 unvested shares"
        out = re.sub(
            r"\b([0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]+)?|[0-9]+(?:\.[0-9]+)?)\s*((?:(?:total|gross|net|est\.?\s*net|unvested|transferred|vested|available)\s+)?(?:shs|shares|sh|units))\b",
            _sub_shares,
            out,
            flags=re.IGNORECASE,
        )
        return self.map_text(out)

    def project_obj(self, obj: Any, *, key_hint: str = "") -> Any:
        if isinstance(obj, dict):
            # Annual earnings dicts / Timeline `accounts`, `buckets`, `assets`, `assetVals` map ID -> numeric dollar balance
            if key_hint in _SCALED_VAL_DICT_KEYS:
                return {
                    (self.map_composite_id(str(dk)) if key_hint in ("accounts", "assets", "assetVals", "buckets") else str(dk)): self.scale_usd(dv)
                    for dk, dv in obj.items()
                }
            # Per-scenario holdingForecasts / half-life / fee dicts map forecast_key -> entry or unscaled numeric value
            if key_hint in ("holdingForecasts", "holdingHalfLives", "holdingHalfLife", "holdingFeePct"):
                return {
                    self.map_composite_id(str(fk)): self.project_obj(fv, key_hint="holdingForecastEntry")
                    for fk, fv in obj.items()
                }
            # Schemas.account / constituent_forecasts map IDs to dicts
            if key_hint in ("account", "constituent_forecasts"):
                return {
                    self.map_composite_id(str(dk)): self.project_obj(dv, key_hint="entry")
                    for dk, dv in obj.items()
                }
            # byGrant maps grant IDs to dicts in release_bars
            if key_hint == "byGrant":
                return {
                    self.mask_grant_id(str(dk)): self.project_obj(dv, key_hint="entry")
                    for dk, dv in obj.items()
                }

            amt_mode = str(obj.get("amount_mode") or "").strip().lower()
            is_pct_amount = amt_mode in ("pct_of_source", "percent", "pct", "percentage")
            is_pre_scaled = bool(obj.get("_demo_scaled"))

            out: dict[str, Any] = {}
            for k, v in obj.items():
                k_str = str(k)
                if k_str == "_demo_scaled":
                    continue
                if k_str in ("hist_pairs", "cohorts", "risk_classes", "forecast_bucket_rates", "hist_annual_returns"):
                    # Keep historical return curves, volatility correlations, and percentage rates intact
                    out[k_str] = self.project_obj(v, key_hint=k_str) if k_str == "forecast_bucket_rates" else copy.deepcopy(v)
                    continue
                if k_str == "amount" and is_pct_amount:
                    out[k_str] = v
                    continue
                if k_str in (
                    "principal_str", "quantity_str", "basis_sub", "growth_str", "growth_sub",
                    "liquidity_sub", "principal_sub", "subtitle", "tax_rule_detail", "tax_rule_title",
                    "recent_change_tip", "notes_md", "earnings_note", "work_span_label", "summary",
                    "principal_tooltip", "growth_tooltip", "source_tooltip", "pct_tooltip",
                    "source_detail", "forecast_source_detail",
                    "reply", "proposal_label", "last_step",
                ) and isinstance(v, str):
                    out[k_str] = self.map_text(v) if (is_pre_scaled and k_str in ("reply", "last_step")) else self._scale_inline_usd_text(v)
                    continue
                if k_str == "principal_overrides" and isinstance(v, dict):
                    out[k_str] = {
                        self.map_composite_id(str(pk)): (
                            self.scale_usd(pv, key_name="principal")
                            if isinstance(pv, (int, float)) and not isinstance(pv, bool)
                            else self.project_obj(pv, key_hint="principal_override_entry")
                        )
                        for pk, pv in v.items()
                    }
                    continue
                if k_str == "friendly_names" and isinstance(v, dict):
                    out[k_str] = {
                        self.map_composite_id(str(fk)): self.project_obj(fv, key_hint="friendly_name_entry")
                        for fk, fv in v.items()
                    }
                    continue
                if k_str == "sim_buckets" and isinstance(v, dict):
                    out[k_str] = {
                        self.map_composite_id(str(bk)): self.project_obj(bv, key_hint="sim_bucket_entry")
                        for bk, bv in v.items()
                    }
                    continue
                if k_str == "ssn_model" and isinstance(v, dict):
                    mapped_ssn: dict[str, Any] = {}
                    for sk, sv in v.items():
                        mapped_key = self._investor_name_map.get(str(sk).lower(), str(sk))
                        mapped_ssn[mapped_key] = self.project_obj(sv, key_hint="ssn_entry")
                    out[k_str] = mapped_ssn
                    continue
                if k_str == "claim_setting_key" and isinstance(v, str):
                    out[k_str] = self._claim_key_map.get(v, v)
                    continue
                # SECURITY INVARIANT (SA2-03 / SA3-03): Alias investor first-name claim-age keys (`ssn<FirstName>ClaimAge`)
                # in BOTH persisted `settings` AND realtime `/api/ui/poll` `settings_patch` events!
                if (k_str in ("settings", "settings_patch") or key_hint in ("settings", "settings_patch")) and isinstance(v, dict):
                    mapped_settings: dict[str, Any] = {}
                    for sk, sv in v.items():
                        new_sk = self._claim_key_map.get(str(sk), str(sk))
                        mapped_settings[new_sk] = self.project_obj(sv, key_hint=new_sk)
                    out[k_str] = mapped_settings
                    continue
                if k_str in ("snapshot", "snapshot_url"):
                    # Clear static JPEG thumbnails so they don't flash unscaled charts or 404 under demo names
                    out[k_str] = ""
                    continue
                if k_str == "badge_image":
                    out[k_str] = "Assets/default_badge.svg"
                    continue
                if k_str == "badge_history":
                    out[k_str] = ["Assets/default_badge.svg"]
                    continue
                if k_str == "badge_metadata":
                    out[k_str] = {}
                    continue
                # SECURITY DO: Strip local directory paths (`saves_dir`, `external_file` -> basename), custom badge prompts,
                # and truncate DOBs to YYYY-MM before serving Demo Mode payloads (`SA-11` / `SA-19`).
                # SECURITY DON'T: Never emit full `YYYY-MM-DD` DOBs or host filesystem paths in demo projections.
                if k_str in ("default_badge_prompt", "saves_dir"):
                    out[k_str] = ""
                    continue
                if k_str == "external_file":
                    bname = Path(str(v)).name if isinstance(v, str) and v.strip() else ""
                    out[k_str] = self.map_composite_id(self.map_text(bname)) if bname else ""
                    continue
                if k_str in ("birth_date", "birthdate", "birth_iso") and isinstance(v, str):
                    out[k_str] = self._mask_dob_string(v)
                    continue
                if (k_str == "birth_day" or k_str.endswith("_birth_day")) and isinstance(v, (int, float)) and not isinstance(v, bool):
                    out[k_str] = 1
                    continue
                if k_str == "family_birthdays":
                    if isinstance(v, dict):
                        out[k_str] = {
                            self.map_text(str(bk)): self._mask_dob_string(str(bv))
                            for bk, bv in v.items()
                        }
                        continue
                    if isinstance(v, list):
                        masked_fb: list[Any] = []
                        for item in v:
                            if isinstance(item, dict):
                                mapped_item = {
                                    mk: (
                                        self.map_text(mv)
                                        if mk == "name"
                                        else (
                                            self._mask_dob_string(mv)
                                            if mk in ("birth_date", "birthdate", "birth_iso") and isinstance(mv, str)
                                            else (1 if mk in ("d", "birth_day") else mv)
                                        )
                                    )
                                    for mk, mv in item.items()
                                }
                                masked_fb.append(mapped_item)
                            else:
                                masked_fb.append(self.project_obj(item))
                        out[k_str] = masked_fb
                        continue
                if k_str == "stock_grants" and isinstance(v, dict):
                    out[k_str] = self._project_stock_grants(v)
                    continue
                if k_str == "google_stock" and isinstance(v, dict):
                    out[k_str] = self._project_grant_symbol_block(v)
                    continue
                if (
                    k_str in _USD_KEYS
                    or k_str.lower().endswith("_usd")
                    or (key_hint == "ending_real" and k_str in ("min", "max"))
                    or (key_hint == "ending_histogram" and k_str in ("lo", "hi"))
                ) and isinstance(v, (int, float)) and not isinstance(v, bool):
                    out[k_str] = self.scale_usd(v, key_name=k_str)
                    continue
                if k_str in _QTY_KEYS and isinstance(v, (int, float)) and not isinstance(v, bool):
                    out[k_str] = self.scale_qty(v)
                    continue
                if k_str in ("primary_symbol", "primary_equity_symbol") and isinstance(v, str):
                    out[k_str] = self.map_text(v)
                    continue
                if k_str == "schemas" and isinstance(v, dict):
                    # Update account schema display labels while keeping bucket keys stable
                    out[k_str] = self.project_obj(v, key_hint="schemas")
                    continue
                out[k_str] = self.project_obj(v, key_hint=k_str)
            return out

        if isinstance(obj, list):
            if key_hint == "symbols":
                return [self.map_text(str(x)) for x in obj]
            return [self.project_obj(item, key_hint=key_hint) for item in obj]

        if isinstance(obj, (int, float)) and not isinstance(obj, bool):
            if key_hint in _USD_KEYS:
                return self.scale_usd(obj, key_name=key_hint)
            if key_hint in _QTY_KEYS:
                return self.scale_qty(obj)
            return obj

        if isinstance(obj, str):
            if key_hint == "grant_id":
                return self.mask_grant_id(obj)
            if key_hint in _COMPOSITE_ID_KEYS:
                return self.map_composite_id(obj)
            return self.map_text(obj)

        return obj

    project_value = project_obj

    def _project_grant_symbol_block(self, blk: dict[str, Any]) -> dict[str, Any]:
        # Deep project while ensuring grant bars/totals scale uniformly by k
        res = self.project_obj(blk, key_hint="grant_symbol_block")
        if isinstance(res, dict):
            # Ensure grant_series IDs and labels are masked
            if isinstance(res.get("grant_series"), list):
                for gs in res["grant_series"]:
                    if isinstance(gs, dict):
                        if "id" in gs:
                            gs["id"] = self.mask_grant_id(str(gs["id"]))
                        if "label" in gs:
                            gs["label"] = self.map_text(str(gs["label"]))
            # Ensure release_bars byGrant dictionary keys are masked
            if isinstance(res.get("release_bars"), list):
                for rb in res["release_bars"]:
                    if isinstance(rb, dict) and isinstance(rb.get("byGrant"), dict):
                        rb["byGrant"] = {
                            self.mask_grant_id(str(gk)): gv
                            for gk, gv in rb["byGrant"].items()
                        }
            # Ensure by_grant items have masked grant_id and label
            if isinstance(res.get("by_grant"), list):
                for bg in res["by_grant"]:
                    if isinstance(bg, dict):
                        if "grant_id" in bg:
                            bg["grant_id"] = self.mask_grant_id(str(bg["grant_id"]))
                        if "label" in bg:
                            bg["label"] = self.map_text(str(bg["label"]))
            # Ensure retired_grant_summary has masked grant_id
            if isinstance(res.get("retired_grant_summary"), dict):
                rgs = res["retired_grant_summary"]
                if "grant_id" in rgs:
                    rgs["grant_id"] = self.mask_grant_id(str(rgs["grant_id"]))
        return res

    def _project_stock_grants(self, sg: dict[str, Any]) -> dict[str, Any]:
        out = copy.deepcopy(sg)
        if out.get("default_symbol"):
            out["default_symbol"] = self.map_text(str(out["default_symbol"]))
        if isinstance(out.get("companies"), list):
            new_comps = []
            for c in out["companies"]:
                nc = self.project_obj(c, key_hint="company")
                if c.get("symbol"):
                    nc["symbol"] = self.map_text(str(c["symbol"]))
                new_comps.append(nc)
            out["companies"] = new_comps
        if isinstance(out.get("by_symbol"), dict):
            new_by: dict[str, Any] = {}
            for sym, sym_blk in out["by_symbol"].items():
                new_sym = self.map_text(str(sym))
                new_by[new_sym] = self._project_grant_symbol_block(sym_blk)
            out["by_symbol"] = new_by
        return out


def apply_demo_projection_to_payload(payload: dict[str, Any], registry_cfg: dict[str, Any]) -> dict[str, Any]:
    """
    Projects an entire `ReportPayload` into Demo Mode (Case A), embedding only
    `privacy_status: {"demo_enabled": true, "shield_mode": ...}` without exposing `k` (D4).
    """
    proj = DemoTranslator(registry_cfg)
    demo_payload = proj.project_obj(payload)
    # Remove/clear real portfolio list names if present in bundles except projected active
    if isinstance(demo_payload.get("portfolios_bundle"), dict):
        pb = demo_payload["portfolios_bundle"]
        for p in pb.get("portfolios") or []:
            if isinstance(p, dict):
                p["badge_image"] = "Assets/default_badge.svg"
    if isinstance(demo_payload.get("gsu_schedule"), dict):
        gsu_by = demo_payload["gsu_schedule"].get("by_grant")
        if isinstance(gsu_by, list):
            for bg in gsu_by:
                if isinstance(bg, dict):
                    if "grant_id" in bg:
                        bg["grant_id"] = proj.mask_grant_id(str(bg["grant_id"]))
                    if "label" in bg:
                        bg["label"] = proj.map_text(str(bg["label"]))
    demo_payload["privacy_status"] = {
        "demo_enabled": True,
        "shield_mode": str((registry_cfg.get("ai_shield") or {}).get("mode") or "identity_aliased"),
    }
    return demo_payload


def apply_demo_projection_to_saves(saves_payload: dict[str, Any], registry_cfg: dict[str, Any]) -> dict[str, Any]:
    """Projects `list_saves_payload()` output into Demo Mode names, keys, and scaled dollar metrics."""
    demo_snaps: dict[str, tuple[str, str]] = {}
    for m in (saves_payload.get("models") or saves_payload.get("saves") or []):
        if isinstance(m, dict) and m.get("id"):
            ds = str(m.get("demo_snapshot") or "")
            du = str(m.get("demo_snapshot_url") or "")
            if ds or du:
                demo_snaps[str(m["id"])] = (ds, du)

    proj = DemoTranslator(registry_cfg)
    out = proj.project_obj(saves_payload)
    out["demo_mode"] = True

    for key in ("models", "saves"):
        for m in (out.get(key) or []):
            if isinstance(m, dict):
                mid = str(m.get("id") or "")
                m.pop("demo_snapshot", None)
                m.pop("demo_snapshot_url", None)
                if mid in demo_snaps:
                    ds, du = demo_snaps[mid]
                    if ds:
                        m["snapshot"] = ds
                    if du:
                        m["snapshot_url"] = du
    return out
