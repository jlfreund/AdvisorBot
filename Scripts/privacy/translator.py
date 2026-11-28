#!/usr/bin/env python3
"""
Bidirectional Alias <-> Canonical Privacy Translator (`to_alias` / `from_alias`).

Protects cloud LLM inference (`Case B`) by compiling longest-match-first, boundary-safe
substitutions from the active portfolio's `privacy_registry.json`, supporting:
  - Case-insensitive matching for full names, multi-word phrases, and >=3-char tickers/codes
  - Case-sensitive matching for 1-2 char ambiguous uppercase tokens (`MS`, `GS`, `C`, `V`) in free text
  - 1-to-1 reversible translation of composite asset keys (`uma_HB_7001`, `single_HB-7001_ACME`)
  - Configurable DOB masking (`year_month`, `full`, `age_only`, `shift_years`)
  - In-place Prompt 2 `notes_md` / upvote translation (`translate` vs `strip`)
"""

from __future__ import annotations

import copy
from datetime import date
import json
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any

_PRODUCT_CATEGORIES = {"equity", "fund", "manager", "carrier"}
_PERSON_CATEGORIES = {"investor", "beneficiary", "person", "family_member"}
_STRUCTURED_SYMBOL_KEYS = {"symbol", "ticker", "symbol_or_code", "quote_symbol"}
_MILESTONE_SCHEMA_KEYS = {
    "ms_month",
    "ms_real",
    "tax_to_ms",
    "success_to_ms_pct",
    "success_to_ms_n",
}

# 3-5 letter uppercase tickers that are also common English or JSON schema words;
# keep exact-case matching for these in free text while aliasing case-insensitively inside composite IDs.
_AMBIGUOUS_ENGLISH_TICKERS = {
    "ALL", "ARE", "AGE", "BEST", "BOND", "CASH", "CAT", "CORE", "COST", "DATA",
    "DAY", "FAST", "FLOW", "FOOD", "FUND", "GOLD", "GRID", "HAS", "HIGH", "KEY",
    "LIFE", "LINE", "LINK", "LIST", "LIVE", "LOAD", "LONG", "LOW", "MAIN", "MARK",
    "META", "MET", "MODE", "NAME", "NET", "NEW", "NEXT", "NOTE", "NOW", "ONE",
    "OPEN", "OUT", "PAGE", "PATH", "PAY", "PEAK", "PETS", "PLAN", "PLUS", "POOL",
    "PORT", "POST", "PURE", "RATE", "READ", "REAL", "REST", "RISK", "ROLE", "RULE",
    "RUN", "SAFE", "SALE", "SAVE", "SEED", "SEE", "SELF", "SET", "SHOW", "SIDE",
    "SIGN", "SITE", "SIX", "SIZE", "SORT", "SPAN", "SPOT", "STEP", "STOP", "SURE",
    "SWAP", "SYNC", "TASK", "TEAM", "TECH", "TEN", "TERM", "TEST", "TEXT", "TIER",
    "TIME", "TOOL", "TOP", "TRUE", "TRY", "TURN", "TWO", "TYPE", "UNIT", "USER",
    "USE", "VERY", "VIEW", "WAIT", "WANT", "WAVE", "WAY", "WEEK", "WELL", "WEST",
    "WHEN", "WIDE", "WILL", "WIN", "WIRE", "WITH", "WORD", "WORK", "YEAR", "YOU",
    "YOUR", "ZERO", "ZONE",
}

# Matches composite identifiers in free text while avoiding plain lowercase snake_case words like `cost_basis` or `ms_month`:
# - `CONTRACT:<alnum_or_dash>`
# - `<ACCT>:TICKER:<SYM>` / `TICKER:<SYM>`
# - `<letters>_...<digit>...` (asset/account IDs containing at least one digit, e.g. `cust_eq_1234_5678`)
# - `NNNN-NNNN` (account number shapes)
# SECURITY INVARIANT (SA2-07 / SA3-06): Include `-` inside the digit lookahead `(?=[A-Za-z0-9_-]*\d)` so composite
# bucket/group IDs containing hyphenated account slugs (e.g. `single_HB-7001_SYM`, `acct_HB-7001`) are matched in free text!
COMPOSITE_ID_TEXT_RE = re.compile(
    r"\b(?:[A-Za-z0-9_-]+:TICKER:[A-Za-z0-9._-]+|TICKER:[A-Za-z0-9._-]+|CONTRACT:[A-Za-z0-9-]+|(?=[A-Za-z0-9_-]*\d)[a-z]{2,}_[A-Za-z0-9_-]+|[0-9]{4}-[0-9]{4})\b"
)

_SINGLE_LETTER_PREFIX_GUARD_RE = re.compile(
    r"\b(?:Class|Cl|Series|Schedule|Type|Tier|Plan|Part|Section|Form|Exhibit|Grade|Option)\s+$",
    flags=re.IGNORECASE,
)

_PRIMARY_SNAKE_KEY_RE = re.compile(r"(?i)(?:(?<=^)|(?<=_))(non_)?goog(?=_|$)")
_PRIMARY_CAMEL_KEY_RE = re.compile(r"(non|Non)?Goog(?=[A-Z]|$)|^goog(?=[A-Z])")


def is_exact_case_synonym(syn: str, cat: str) -> bool:
    """
    1-2 char uppercase tokens (e.g. `MS`, `GS`, `C`, `V`) and ambiguous English/schema words (e.g. `META`, `ALL`, `COST`)
    match case-sensitively in free text; >=3-char non-ambiguous tickers/codes (`ACME`, `VTI`, `RSU`, `PIMCO`) match case-insensitively.
    """
    if not syn or not syn.isupper():
        return False
    if len(syn) <= 2:
        return True
    if cat in _PRODUCT_CATEGORIES and syn.isalpha() and syn in _AMBIGUOUS_ENGLISH_TICKERS:
        return True
    return False


def mask_dob_year_month(dob_str: str) -> str:
    """Truncates a `YYYY-MM-DD` date string to `YYYY-MM-01`."""
    s = str(dob_str or "").strip()
    m = re.match(r"^(\d{4})-(\d{2})-\d{2}$", s)
    return f"{m.group(1)}-{m.group(2)}-01" if m else s


_COMPOSITE_KEYS = {
    "id",
    "asset_id",
    "forecast_key",
    "parent_forecast_keys",
    "simAssetOrder",
    "holdingForecasts",
    "holdingHalfLives",
    "holdingHalfLife",
    "holdingFeePct",
    "liquidation_order",
    "from_id",
    "to_id",
    "from_asset_id",
    "to_asset_id",
    "source_id",
    "target_id",
    "source_asset_id",
    "target_asset_id",
    "match_target_asset_id",
    "holding_key",
    "account_id",
    "security_id",
    "file",
    "filename",
    "base_save",
    "upvoted_id",
    "loaded_id",
}

_COMPOSITE_DICT_PARENTS = {
    "holdingForecasts",
    "holdingHalfLives",
    "holdingHalfLife",
    "holdingFeePct",
    "accounts",
    "sim_buckets",
    "principal_overrides",
    "sim_asset_rates",
    "constituent_forecasts",
    "friendly_names",
}

_CURRENCY_RE = re.compile(r"\$([0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.([0-9]+))?([kKmMbB])?\b")
_SHARES_TEXT_RE = re.compile(
    r"\b([0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]+)?|[0-9]+(?:\.[0-9]+)?)"
    r"(\s+(?:(?:total|gross|net|est\.?\s*net|unvested|transferred|vested|available)\s+)?"
    r"(?:shs|shares|sh|units))\b",
    flags=re.IGNORECASE,
)
_KV_NUMBER_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*([=:])\s*(-?(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]+)?)\b")

# MODE C (`identity_and_values`) INVERTED ALLOWLIST GUARDRAIL:
# Every numeric JSON value is multiplied by scale factor `k` UNLESS its key matches this non-monetary
# allowlist (or `_NON_MONETARY_KEY_SUFFIXES` / `_NON_MONETARY_KEY_SUBSTR`). When adding non-dollar
# simulation fields (e.g. `median_years_solvent`, `min_tax_bracket_floor`, `corr_spy`, `beta_spy`),
# register them here so Mode C never scales years, bracket codes, correlations, or betas.
_RAW_NON_MONETARY_KEY_EXACT = {
    "y", "m", "d", "yr", "mo", "dy", "year", "years", "month", "months", "day", "days",
    "age", "age_years", "birth_year", "birth_month", "birth_day", "claim_age", "start_age", "end_age",
    "retire_age", "retirement_age", "default_retirement_age", "simInvestor1RetirementAge", "simInvestor2RetirementAge",
    "drawdown_start_month", "drawdown_start_year", "retirement_year", "retirement_month",
    "horizon_years", "vest_year", "years_solvent", "median_years_solvent",
    "start_year", "end_year", "target_age", "duration", "month_idx", "year_idx",
    "tax_bracket", "tax_tier", "tier", "min_tax_bracket_floor", "simMinTaxBracketFloor",
    "owner_birth_year", "owner_birth_month", "owner_birth_day",
    "milestone_month", "horizon", "horizon_months", "max_rounds",
    "simCustomShockPct", "simCustomDownMonths", "simCustomBroadBounceMonths", "simCustomStockBounceMonths",
    "down_months", "broad_bounce_months", "stock_bounce_months", "bear_shield_months",
    "epoch", "ts", "timestamp", "seq", "queue_seq", "latest_seq", "page", "pages",
    "total_pages", "mcsimulations", "simulations", "num_paths", "n_paths", "n_cohorts", "ncohorts",
    "historical_weeks", "run_months", "depleted_month", "start_year_from", "start_year_to",
    "first_year", "last_year", "cohortStartFrom", "data_range", "yearsOut",
    "mcSingleStockCorr", "single_stock_corr", "singlestockcorr", "corr", "correlation", "corr_spy", "beta_spy",
    "transfers", "deposits", "withdrawals",
    "round", "port", "status", "code", "error_code", "iterations",
    "progress_pct", "seed", "version", "rank", "default_rank", "order_index", "count",
    # SA3-07: Cohort counts, grant tranche counts, scheduled-action row indices, and statutory SSA constants
    # are non-monetary invariants and must NEVER be multiplied by Mode C `scale_factor` (`DP-03`).
    "tranches", "obs_tranches", "est_tranches", "vested_tranches", "rem_tranches", "total_tranches",
    "rowNum", "rownum", "row_num", "success_to_ms_n", "success_full_n",
    "bend1", "bend2", "oasdi_cap",
    "tax_rate", "effective_tax_rate", "cap_gains_rate", "marginal_tax_rate",
    "siminflationpct", "ssncolapct", "ssninvestor1claimage", "ssninvestor2claimage",
    "simOptimismMultiplier", "pct_portfolio", "default_growth_pct", "ref_growth_pct", "growth_pct",
    "expense_ratio_pct", "fee_pct", "return_pct", "cagr_pct", "yield_pct", "match_rate_pct",
    # SA5-08: Non-monetary D7 decay half-lives, tier selectors, and annual rate vectors must stay unscaled in Mode C
    "half_life", "holdinghalflife", "holding_half_life",
    "selected_tier", "dest_tier", "source_tier", "tiers", "yearly_rates",
    # Per-share / per-unit market prices stay unscaled (DP-03) so (k * shares) * price == k * value
    "price", "current_price", "unit_price", "spot_price", "live_price",
    "goog_live_price", "primary_live_price", "strike_price", "close_price",
    "prev_close", "goog_prev_close", "primary_prev_close", "stmt_price", "release_price",
    "goog_price", "primary_price", "goog_price_anchor", "primary_price_anchor",
    "orig_price", "adj_price",
}
_NON_MONETARY_KEY_EXACT = {
    x.lower() for x in _RAW_NON_MONETARY_KEY_EXACT
} | {
    re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", x).lower() for x in _RAW_NON_MONETARY_KEY_EXACT
}

_MONETARY_OVERRIDE_SUBSTR = (
    "usd", "balance", "amount", "burn", "worth", "basis", "income", "expense",
    "dividend", "deposit", "withdraw", "contribution", "anchor", "cost",
    "value", "market_value", "cost_basis", "total_vested", "total_unvested",
    "shares", "quantity", "qty", "principal", "salary", "benefit", "pia",
    "surrender_value", "death_benefit", "yr_40_real", "yr_40_nominal", "ending_real",
)
_NON_MONETARY_KEY_SUFFIXES = (
    "_pct", "_rate", "_rates", "_pct_yr", "_ratio", "_weight", "_multiplier", "_factor",
    "_tier", "_tiers",
    "_count", "_cnt", "_n", "_tranches", "_num", "_idx", "_index", "_year", "_years", "_month", "_months",
    "_day", "_days", "_age", "_ts", "_timestamp", "_seq", "_id", "_rank",
    "_score", "_prob", "_probability", "_years_left", "_corr", "_price", "_close",
)
_NON_MONETARY_KEY_SUBSTR = (
    "pct", "percent", "percentage", "ratio", "cagr", "weight", "multiplier",
    "volatility", "stdev", "stddev", "std_dev", "sharpe", "sortino",
    "cola", "inflation", "expense_ratio", "tax_rate", "effective_tax", "cap_gains",
    "claim_age", "claim_setting", "mcsimulations", "progress", "month_idx",
)
_SHARE_QTY_SUBSTR = ("share", "qty", "quantity", "unit", "deu")


def _should_scale_numeric_key(key_hint: str) -> bool:
    """
    Allowlist-inverted Mode C scaling rule (`SA-04` / `DP-03`):
    scales EVERY numeric leaf EXCEPT explicit non-monetary invariants (dates, ages, rates/pcts, counts, IDs, per-share prices).
    """
    # SECURITY DO: Default to `True` (scale) for unknown keys so unrecognized dollar/share fields are never leaked unscaled.
    # SECURITY DON'T: Never invert this back to a monetary-only key allowlist.
    k = str(key_hint or "").strip()
    if not k:
        return True
    norm = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", k).lower()
    low = k.lower()
    for cand in (norm, low):
        if cand in _NON_MONETARY_KEY_EXACT:
            return False
        if any(cand.endswith(suf) for suf in _NON_MONETARY_KEY_SUFFIXES):
            return False
        if any(tok in cand for tok in _MONETARY_OVERRIDE_SUBSTR):
            if any(tok in cand for tok in ("_rate", "_pct", "pct_", "cagr", "ratio", "yield", "_price")):
                return False
            return True
        if any(tok in cand for tok in _NON_MONETARY_KEY_SUBSTR):
            return False
    if low in ("rate", "yield", "id", "ok"):
        return False
    return True


def _is_share_qty_key(key_hint: str) -> bool:
    low = str(key_hint or "").strip().lower()
    return any(tok in low for tok in _SHARE_QTY_SUBSTR)


def _format_scaled_currency(val: float, had_cents: bool, suffix: str) -> str:
    if suffix:
        txt = f"{val:,.2f}".rstrip("0").rstrip(".")
        return f"${txt}{suffix}"
    if not had_cents and abs(val - round(val)) < 1e-6:
        return f"${int(round(val)):,}"
    if abs(val - round(val)) < 1e-6:
        return f"${int(round(val)):,}"
    return f"${val:,.2f}"


def _scale_currency_in_text(text: str, factor: float) -> str:
    if not text or abs(factor - 1.0) < 1e-9:
        return text

    def _repl(m: re.Match[str]) -> str:
        # Leave `@ $price` and `$price/sh` per-share quotes unscaled so `(k * shares) * price == k * value`
        before = text[: m.start()].rstrip()
        after = text[m.end() :]
        if before.endswith("@") or re.match(r"^\s*(?:/\s*(?:shs?|shares?|units?)|per\s+(?:sh|share|unit))\b", after, flags=re.IGNORECASE):
            return m.group(0)
        whole = m.group(1).replace(",", "")
        frac = m.group(2)
        suffix = m.group(3) or ""
        try:
            num = float(f"{whole}.{frac}" if frac else whole)
        except ValueError:
            return m.group(0)
        return _format_scaled_currency(num * factor, bool(frac), suffix)

    out = _CURRENCY_RE.sub(_repl, text)

    def _repl_shares(m: re.Match[str]) -> str:
        raw_s = m.group(1)
        unit_part = m.group(2)
        try:
            raw_val = float(raw_s.replace(",", ""))
        except ValueError:
            return m.group(0)
        scaled = raw_val * factor
        if abs(scaled - round(scaled)) < 1e-6:
            if "." in raw_s:
                decimals = min(max(len(raw_s.split(".")[1]), 1), 3)
                return f"{scaled:,.{decimals}f}{unit_part}"
            return f"{int(round(scaled)):,}{unit_part}"
        txt = f"{scaled:,.4f}".rstrip("0").rstrip(".")
        return f"{txt}{unit_part}"

    out = _SHARES_TEXT_RE.sub(_repl_shares, out)

    def _repl_kv(m: re.Match[str]) -> str:
        key_name = m.group(1)
        sep = m.group(2)
        raw_val = m.group(3)
        if not _should_scale_numeric_key(key_name):
            return m.group(0)
        had_dec = "." in raw_val
        try:
            num = float(raw_val.replace(",", ""))
        except ValueError:
            return m.group(0)
        scaled = num * factor
        if not had_dec and abs(scaled - round(scaled)) < 1e-6:
            fmt = str(int(round(scaled)))
        elif abs(scaled - round(scaled)) < 1e-6:
            fmt = str(int(round(scaled)))
        elif _is_share_qty_key(key_name):
            fmt = f"{scaled:.6f}".rstrip("0").rstrip(".")
        else:
            fmt = f"{scaled:.2f}"
        return f"{key_name}{sep}{fmt}"

    return _KV_NUMBER_RE.sub(_repl_kv, out)


class PrivacyTranslator:
    """Compiled bidirectional translator built from a `privacy_registry` config dict."""

    def __init__(self, registry_cfg: dict[str, Any], *, portfolio_id: str | None = None) -> None:
        self.cfg = copy.deepcopy(registry_cfg or {})
        ai = self.cfg.get("ai_shield") or {}
        self.mode = str(ai.get("mode") or "identity_aliased")
        self.dob_granularity = str(ai.get("dob_granularity") or "year_month")
        if self.mode == "identity_and_values":
            self.notes_policy = "strip"
        else:
            self.notes_policy = str(ai.get("notes_policy") or "translate")
        alias_prod_raw = ai.get("alias_products_and_tickers")
        if alias_prod_raw is None:
            alias_prod_raw = ai.get("alias_products", True)
        self.alias_products = bool(alias_prod_raw)
        self.scale_values = self.mode == "identity_and_values"
        try:
            k_raw = float(ai.get("scale_factor") if ai.get("scale_factor") is not None else 0.25)
        except (TypeError, ValueError):
            k_raw = 0.25
        self.value_scale = k_raw if 0.01 <= k_raw <= 100.0 else 0.25
        self._explicit_bday_shift = ai.get("birthday_shift_years") is not None
        try:
            self.birthday_shift_years = int(ai.get("birthday_shift_years") if self._explicit_bday_shift else 3)
        except (TypeError, ValueError):
            self.birthday_shift_years = 3

        self._cache_lock = threading.RLock()
        self._dob_alias_to_real: dict[str, str] = {}
        self._comp_real_to_alias: dict[str, str] = {}
        self._comp_alias_to_real: dict[str, str] = {}
        self._key_alias_to_real: dict[str, str] = {}
        self._scrubbed_key_map: dict[str, str] = {}

        self.token_to_canonical: dict[str, str] = {}
        self.token_to_category: dict[str, str] = {}
        self._person_token_meta: dict[str, dict[str, str]] = {}
        self._syn_to_token_ci: dict[str, str] = {}
        self._syn_to_token_cs: dict[str, str] = {}
        self._structured_sym_to_token: dict[str, str] = {}

        exact_pairs: list[tuple[str, str]] = []
        ci_pairs: list[tuple[str, str]] = []
        seg_map: list[tuple[str, str, str]] = []
        person_camel_stems: list[tuple[str, str]] = []
        person_digit_stems: list[tuple[str, str]] = []

        for ent in self.cfg.get("entities") or []:
            if not isinstance(ent, dict) or not ent.get("enabled", True):
                continue
            tok = str(ent.get("token") or "").strip()
            cat = str(ent.get("category") or "").strip().lower()
            canon = str(ent.get("canonical") or "").strip()
            meta = ent.get("meta") if isinstance(ent.get("meta"), dict) else {}
            if not tok or not canon:
                continue
            if not self.alias_products and cat in _PRODUCT_CATEGORIES:
                continue

            self.token_to_canonical[tok] = canon
            self.token_to_category[tok] = cat
            syns = [str(s).strip() for s in (ent.get("synonyms") or []) if str(s).strip()]
            if canon not in syns:
                syns.insert(0, canon)

            if cat in ("equity", "fund"):
                self._structured_sym_to_token[canon.upper()] = tok

            if cat in _PERSON_CATEGORIES:
                import unicodedata as _ud

                def _fold_ascii(val: str) -> str:
                    rep = val.replace("ø", "o").replace("Ø", "O").replace("æ", "ae").replace("Æ", "AE").replace("ß", "ss")
                    return "".join(ch for ch in _ud.normalize("NFKD", rep) if not _ud.combining(ch))

                first_name = str(meta.get("first") or canon.split()[0]).strip()
                inv_id = str(meta.get("id") or first_name).strip().lower().replace(" ", "_")
                camel_full = canon.strip().title().replace(" ", "")
                camel_first = first_name.strip().title().replace(" ", "")
                ascii_full = _fold_ascii(camel_full)
                ascii_first = _fold_ascii(camel_first)
                self._person_token_meta[tok] = {
                    "canonical": canon,
                    "first": first_name,
                    "id": inv_id,
                    "camel": camel_full or camel_first,
                    "camel_first": camel_first,
                }
                for c_stem in {camel_full, camel_first, ascii_full, ascii_first}:
                    if len(c_stem) >= 3 and c_stem[:1].isupper() and c_stem.isalpha():
                        person_camel_stems.append((c_stem, tok))
                for d_stem in {inv_id, first_name.lower(), camel_full.lower(), ascii_first.lower(), ascii_full.lower()}:
                    if len(d_stem) >= 3 and re.match(r"^[a-z][a-z0-9_]*$", d_stem):
                        person_digit_stems.append((d_stem, tok))

            for syn in syns:
                seg_map.append((syn, tok, cat))
                if len(syn) == 1:
                    # 1-letter English words A / I are aliased only in structured symbol fields & TICKER:<SYM>,
                    # never in free-text prose; other 1-letter uppercase tickers (C, V, T, F) match exact-case.
                    if syn.upper() in ("A", "I"):
                        self._structured_sym_to_token[syn.upper()] = tok
                        continue
                    if syn.isupper() and syn.isalpha():
                        exact_pairs.append((syn, tok))
                        self._syn_to_token_cs[syn] = tok
                    continue

                if is_exact_case_synonym(syn, cat):
                    exact_pairs.append((syn, tok))
                    self._syn_to_token_cs[syn] = tok
                else:
                    ci_pairs.append((syn, tok))
                    self._syn_to_token_ci[syn.lower()] = tok

        exact_pairs.sort(key=lambda x: (-len(x[0]), x[0]))
        ci_pairs.sort(key=lambda x: (-len(x[0]), x[0].lower()))
        self._seg_pairs = sorted(seg_map, key=lambda x: (-len(x[0]), x[0].lower()))
        self._seg_lookup_ci = {s.lower(): t for s, t, _cat in self._seg_pairs}

        # Precompile composite ID segment regexes once rather than per call
        self._seg_patterns: list[tuple[re.Pattern[str], re.Pattern[str], str]] = []
        for syn, tok, cat in self._seg_pairs:
            if len(syn) == 1:
                if cat == "equity" and syn.isalpha():
                    # Match 1-char tickers after `TICKER:` or `_` in structured composite IDs (`ACCT_3:TICKER:C`, `single_HB-7001_V`)
                    one_strict = re.compile(rf"(?:(?<=TICKER:)|(?<=_)){re.escape(syn.upper())}(?![A-Za-z0-9])")
                    one_ci = re.compile(rf"(?:(?<=TICKER:)|(?<=_)){re.escape(syn)}(?![A-Za-z0-9])", flags=re.IGNORECASE)
                    self._seg_patterns.append((one_strict, one_ci, tok))
                continue
            flags = 0 if is_exact_case_synonym(syn, cat) else re.IGNORECASE
            strict_pat = re.compile(rf"(?<![A-Za-z0-9]){re.escape(syn)}(?![A-Za-z0-9])", flags=flags)
            ci_pat = re.compile(rf"(?<![A-Za-z0-9]){re.escape(syn)}(?![A-Za-z0-9])", flags=re.IGNORECASE)
            self._seg_patterns.append((strict_pat, ci_pat, tok))

        # CamelCase & digit-joined person patterns (SA2-03: `ssnJasonClaimAge`, `success_to_jason85_pct`, `yun85_month`)
        person_camel_stems.sort(key=lambda x: (-len(x[0]), x[0]))
        person_digit_stems.sort(key=lambda x: (-len(x[0]), x[0]))
        self._person_camel_map = {stem: tok for stem, tok in person_camel_stems}
        self._person_digit_map = {stem.lower(): tok for stem, tok in person_digit_stems}

        if person_camel_stems:
            c_joined = "|".join(re.escape(st) for st, _ in person_camel_stems)
            self._person_camel_re: re.Pattern[str] | None = re.compile(
                rf"(?:(?<=[a-z0-9])({c_joined})(?=[A-Z0-9]|$)|(?<![^\W_])({c_joined})(?=[A-Z][a-z]))"
            )
        else:
            self._person_camel_re = None

        if person_digit_stems:
            d_joined = "|".join(re.escape(st) for st, _ in person_digit_stems)
            self._person_digit_re: re.Pattern[str] | None = re.compile(
                rf"(?<![^\W_])(?i:({d_joined}))(?=[0-9])"
            )
        else:
            self._person_digit_re = None

        # Combined prose outbound regex
        combined = [(s, t, True) for s, t in exact_pairs] + [(s, t, False) for s, t in ci_pairs]
        combined.sort(key=lambda x: (-len(x[0]), x[0].lower()))
        if combined:
            parts = []
            for syn, _tok, cs in combined:
                esc = re.escape(syn)
                parts.append(esc if cs else f"(?i:{esc})")
            joined = "|".join(parts)
            self._outbound_re: re.Pattern[str] | None = re.compile(
                rf"(?<![A-Za-z0-9])(?:{joined})(?![A-Za-z0-9])"
            )
        else:
            self._outbound_re = None

        self._known_dobs: list[str] = []
        for ent in self.cfg.get("entities") or []:
            meta = ent.get("meta") if isinstance(ent.get("meta"), dict) else {}
            dob = str(meta.get("birth_date") or "").strip()
            if re.match(r"^\d{4}-\d{2}-\d{2}$", dob) and dob not in self._known_dobs:
                self._known_dobs.append(dob)

        self._should_shift_birthdays = (
            self.dob_granularity == "shift_years"
            or (self.scale_values and self.birthday_shift_years != 0 and (self._explicit_bday_shift or bool(self.cfg.get("entities"))))
        )
        if self._should_shift_birthdays:
            for dob in self._known_dobs:
                m_d = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", dob)
                if m_d:
                    shifted_yr = int(m_d.group(1)) + self.birthday_shift_years
                    self._dob_alias_to_real[f"{shifted_yr:04d}-{int(m_d.group(2)):02d}-01"] = dob

        # Inbound token restoration regexes
        toks_sorted = sorted(self.token_to_canonical.keys(), key=lambda t: (-len(t), t))
        if toks_sorted:
            joined_toks = "|".join(re.escape(t) for t in toks_sorted)
            self._inbound_re: re.Pattern[str] | None = re.compile(
                rf"(?<![A-Za-z0-9])({joined_toks})(?![A-Za-z0-9])"
            )
        else:
            self._inbound_re = None

        person_toks_sorted = sorted(self._person_token_meta.keys(), key=lambda t: (-len(t), t))
        if person_toks_sorted:
            p_joined = "|".join(re.escape(t) for t in person_toks_sorted)
            self._inbound_camel_re: re.Pattern[str] | None = re.compile(
                rf"(?:(?<=[a-z])({p_joined})(?=[A-Z]|$)|(?<![A-Za-z0-9])({p_joined})(?=[A-Z][a-z]))"
            )
            self._inbound_digit_re: re.Pattern[str] | None = re.compile(
                rf"(?<![A-Za-z0-9])({p_joined})_(\d+_(?:month|pct|n))\b"
            )
            self._inbound_concat_digit_re: re.Pattern[str] | None = re.compile(
                rf"(?<![A-Za-z0-9])({p_joined})(\d{{2,3}}_(?:month|pct|n))\b"
            )
        else:
            self._inbound_camel_re = None
            self._inbound_digit_re = None
            self._inbound_concat_digit_re = None

        # DESIGN INTENT (SA2-03 / SA2-07):
        # `PrivacyTranslator` is instantiated fresh on every HTTP `/api/chat` turn and every MCP tool call.
        # Pre-seeding person-joined keys (`ssn<Name>ClaimAge`, `success_to_<inv_id>85_pct`) and portfolio/SQLite
        # composite bucket IDs on construction guarantees deterministic Turn 2+ / MCP Call 2+ de-aliasing
        # without requiring sticky server-side session state.
        self._preseed_person_keys()
        self._preseed_portfolio_composite_ids(portfolio_id)

    def _preseed_person_keys(self) -> None:
        for tok, pmeta in self._person_token_meta.items():
            camel = pmeta["camel"]
            camel_first = pmeta["camel_first"]
            inv_id = pmeta["id"]
            first_low = pmeta["first"].lower()
            if camel_first:
                self._key_alias_to_real.setdefault(f"ssn{tok}ClaimAge", f"ssn{camel_first}ClaimAge")
            if camel and camel != camel_first:
                self._key_alias_to_real[f"ssn{tok}ClaimAge"] = f"ssn{camel}ClaimAge"
            if first_low:
                self._key_alias_to_real.setdefault(f"ssn_{tok}_claim_ref_age", f"ssn_{first_low}_claim_ref_age")
            if inv_id:
                for age_n in (80, 85, 90, 95):
                    self._key_alias_to_real.setdefault(f"{tok}_{age_n}_month", f"{inv_id}{age_n}_month")
                    self._key_alias_to_real.setdefault(f"{tok}{age_n}_month", f"{inv_id}{age_n}_month")
                    self._key_alias_to_real.setdefault(f"success_to_{tok}_{age_n}_pct", f"success_to_{inv_id}{age_n}_pct")
                    self._key_alias_to_real.setdefault(f"success_to_{tok}{age_n}_pct", f"success_to_{inv_id}{age_n}_pct")
                    self._key_alias_to_real.setdefault(f"success_to_{tok}_{age_n}_n", f"success_to_{inv_id}{age_n}_n")
                    self._key_alias_to_real.setdefault(f"{tok}_{age_n}th", f"{inv_id}_{age_n}th")
                    self._key_alias_to_real.setdefault(f"{tok}_{age_n}_real", f"{inv_id}_{age_n}_real")
                    self._key_alias_to_real.setdefault(f"{tok}_{age_n}_nominal", f"{inv_id}_{age_n}_nominal")

    def _preseed_portfolio_composite_ids(self, portfolio_id: str | None = None) -> None:
        # 1. Seed structural underscore variants from in-memory entities (works even when no portfolio is on disk)
        in_mem_candidates: list[str] = []
        for ent in self.cfg.get("entities") or []:
            if not isinstance(ent, dict) or not ent.get("enabled", True):
                continue
            cat = str(ent.get("category") or "").strip().lower()
            canon = str(ent.get("canonical") or "").strip()
            if cat == "account" and canon:
                slug = canon.replace("-", "_")
                # CROSS-FILE INVARIANT (SA2-07 / SA3-06 <-> view_model.py `_build_retirement_sim_assets` & `account_rollups`):
                # Every `<prefix>_<acct_slug>` or `<prefix>_<acct_id>` composite ID emitted by `view_model.py`
                # MUST be pre-seeded here so stateless Turn-2+ `PrivacyTranslator` instances restore underscores
                # (`cash_HB_7001`) instead of hyphenated canonicals (`cash_HB-7001`)!
                in_mem_candidates.extend([
                    f"uma_{slug}", f"munis_{slug}", f"rollup_uma_{slug}", f"rollup_munis_{slug}",
                    f"cash_{slug}", f"cash_{canon}", f"acct_{canon}", f"acct_{slug}",
                ])
            elif cat == "contract" and canon:
                in_mem_candidates.append(f"CONTRACT:{canon}")
            elif cat == "scenario":
                meta = ent.get("meta") if isinstance(ent.get("meta"), dict) else {}
                if meta.get("filename"):
                    in_mem_candidates.append(str(meta["filename"]))

        # 2. Seed real composite IDs from target/active portfolio's `project.json`, `Scenarios/*.json`, and `portfolio.sqlite`
        disk_candidates: list[str] = []
        pdir: Path | None = None
        try:
            from config import get_active_portfolio_id, get_portfolio_dir

            eff_pid = portfolio_id or str(self.cfg.get("portfolio_id") or "").strip() or get_active_portfolio_id()
            if eff_pid:
                pdir = get_portfolio_dir(eff_pid)
        except Exception:
            pdir = None

        if pdir is not None and pdir.is_dir():
            proj_fp = pdir / "project.json"
            if proj_fp.is_file():
                try:
                    proj = json.loads(proj_fp.read_text(encoding="utf-8"))
                    if isinstance(proj, dict):
                        for dict_key in ("accounts", "sim_buckets", "principal_overrides", "sim_asset_rates"):
                            sub_d = proj.get(dict_key)
                            if isinstance(sub_d, dict):
                                disk_candidates.extend(str(k) for k in sub_d.keys() if k)
                        gsu_f = proj.get("grant_forecast") or proj.get("gsu_forecast")
                        if isinstance(gsu_f, dict):
                            for gk in ("target_vested_account_id", "target_vested_security_id"):
                                if gsu_f.get(gk):
                                    disk_candidates.append(str(gsu_f[gk]))
                        for inv in (
                            (proj.get("investors") or [])
                            + (proj.get("family_members") or [])
                            + (proj.get("beneficiaries") or [])
                        ):
                            if isinstance(inv, dict):
                                if inv.get("name"):
                                    ck_name = str(inv["name"]).strip().title().replace(" ", "")
                                    disk_candidates.append(f"ssn{ck_name}ClaimAge")
                                wp = inv.get("work_plan")
                                if isinstance(wp, dict):
                                    for crow in wp.get("contributions") or []:
                                        if isinstance(crow, dict):
                                            for wk in ("target_asset_id", "match_target_asset_id"):
                                                if crow.get(wk):
                                                    disk_candidates.append(str(crow[wk]))
                except Exception:
                    pass

            for sub in ("Scenarios", "Saves"):
                sdir = pdir / sub
                if not sdir.is_dir():
                    continue
                for fp in sorted(sdir.glob("*.json")):
                    if fp.name.startswith("_"):
                        continue
                    try:
                        sc_obj = json.loads(fp.read_text(encoding="utf-8"))
                    except Exception:
                        continue
                    if not isinstance(sc_obj, dict):
                        continue
                    if fp.name == "global_preferences.json":
                        for mk in ("friendly_names", "holding_preferences"):
                            fmap = sc_obj.get(mk)
                            if isinstance(fmap, dict):
                                disk_candidates.extend(str(k) for k in fmap.keys() if k)
                        continue
                    disk_candidates.append(fp.name)
                    st = sc_obj.get("settings")
                    if isinstance(st, dict):
                        for field in ("holdingForecasts", "holdingHalfLives"):
                            keyed = st.get(field)
                            if isinstance(keyed, dict):
                                disk_candidates.extend(str(k) for k in keyed if k)
                        for item in st.get("simAssetOrder") or []:
                            if item:
                                disk_candidates.append(str(item))
                        for hyp in st.get("simHypotheticalAssets") or []:
                            if isinstance(hyp, dict) and hyp.get("id"):
                                disk_candidates.append(str(hyp["id"]))
                        for act in st.get("simScheduledActions") or []:
                            if isinstance(act, dict):
                                for ak in ("source_id", "target_id", "from_id", "to_id", "from_asset_id", "to_asset_id"):
                                    if act.get(ak):
                                        disk_candidates.append(str(act[ak]))

            db_fp = pdir / "portfolio.sqlite"
            if db_fp.is_file():
                try:
                    conn = sqlite3.connect(str(db_fp))
                    try:
                        acct_info = [
                            (str(r[0]), str(r[1] or ""))
                            for r in conn.execute("SELECT account_id, institution FROM accounts").fetchall()
                            if r[0]
                        ]
                        sec_rows = [
                            (str(r[0] or ""), str(r[1] or ""))
                            for r in conn.execute("SELECT security_id, symbol_or_code FROM securities").fetchall()
                        ]
                        snap_pairs = [
                            (str(r[0] or ""), str(r[1] or ""))
                            for r in conn.execute("SELECT DISTINCT account_id, security_id FROM position_snapshots").fetchall()
                        ]
                    finally:
                        conn.close()
                    acct_inst_map = {aid: inst for aid, inst in acct_info}
                    sec_sym_map = {sid: sym for sid, sym in sec_rows}
                    for aid, _inst in acct_info:
                        slug = aid.replace("-", "_")
                        disk_candidates.extend([
                            aid, f"uma_{slug}", f"munis_{slug}", f"rollup_uma_{slug}", f"rollup_munis_{slug}",
                            f"cash_{slug}", f"cash_{aid}", f"acct_{aid}", f"acct_{slug}",
                        ])
                    for sid, sym in sec_rows:
                        if sid.startswith("CONTRACT:"):
                            disk_candidates.append(sid)
                        if sym and not sym.startswith(("CASH", "CONTRACT:", "CUSIP:")):
                            sym_low = sym.lower()
                            disk_candidates.extend([
                                f"stock_{sym_low}_high_basis",
                                f"stock_{sym_low}_low_basis",
                                f"future_unvested_{sym_low}",
                            ])
                    for aid, sid in snap_pairs:
                        if aid and sid:
                            disk_candidates.append(f"{aid}:{sid}")
                            sym = sec_sym_map.get(sid) or ""
                            if sym and not sym.startswith("CASH"):
                                aid_slug = aid.replace("-", "_")
                                sym_low = sym.lower()
                                disk_candidates.extend([f"single_{aid}_{sym}", f"{sym_low}_{aid_slug}"])
                                inst_str = acct_inst_map.get(aid) or ""
                                inst_first = re.sub(r"[^a-z0-9]", "", inst_str.split()[0].lower()) if inst_str.strip() else ""
                                if inst_first:
                                    disk_candidates.append(f"{inst_first}_{sym_low}_{aid_slug}")
                except Exception:
                    pass

        # Seed disk IDs first (authoritative), then fill any unmapped in-memory structural templates
        seen_seed: set[str] = set()
        for cid in disk_candidates:
            s_cid = str(cid).strip()
            if not s_cid or s_cid in seen_seed or s_cid in ("auto", "none"):
                continue
            seen_seed.add(s_cid)
            if s_cid.startswith("ssn") and s_cid.endswith("ClaimAge"):
                al_k = self.to_alias_text(s_cid)
                if al_k != s_cid:
                    self._key_alias_to_real[al_k] = s_cid
            else:
                self._alias_composite_id(s_cid)

        for cid in in_mem_candidates:
            s_cid = str(cid).strip()
            if not s_cid or s_cid in seen_seed:
                continue
            seen_seed.add(s_cid)
            # Only register fallback template if its alias isn't already bound to a real portfolio asset ID
            al = self._compute_composite_alias(s_cid, strict_short_codes=False)
            if al != s_cid:
                with self._cache_lock:
                    self._comp_real_to_alias.setdefault(s_cid, al)
                    self._comp_alias_to_real.setdefault(al, s_cid)

    def _scrub_primary_symbol_keys(self, key: str) -> str:
        """Rewrites internal `*goog*` dict keys to generic `primary` / `non_primary` keys (`SA-03` / `SA2-03`)."""
        k = str(key or "")
        if not k or "goog" not in k.lower():
            return k
        # Keep standalone ticker (`GOOG`) and composite asset IDs (`wfa_goog_...`, `single_..._GOOG`) for entity aliasing
        if k == k.upper() and k.isalpha():
            return k
        if any(ch.isdigit() for ch in k) or k.startswith(("single_", "wfa_", "wfb_", "ms_", "ann_", "uma_", "CONTRACT:")):
            return k

        out = _PRIMARY_SNAKE_KEY_RE.sub(lambda m: "non_primary" if m.group(1) else "primary", k)

        def _repl_camel(m: re.Match[str]) -> str:
            prefix = m.group(1)
            if prefix == "non":
                return "nonPrimary"
            if prefix == "Non":
                return "NonPrimary"
            return "primary" if m.group(0).startswith("goog") else "Primary"

        out = _PRIMARY_CAMEL_KEY_RE.sub(_repl_camel, out)
        if out != k:
            with self._cache_lock:
                self._scrubbed_key_map[out] = k
        return out

    def _restore_primary_symbol_keys(self, key: str) -> str:
        """Restores dict keys previously scrubbed by `_scrub_primary_symbol_keys` without mangling native `primary*` keys."""
        k = str(key or "")
        if not k:
            return k
        with self._cache_lock:
            orig = self._scrubbed_key_map.get(k)
        if orig is not None:
            return orig
        if k == "futurePrimaryGrowthPct":
            return "futureGoogGrowthPct"
        if k == "futureNonPrimaryGrowthPct":
            return "futureNonGoogGrowthPct"
        return k

    def _replace_prose_match(self, match: re.Match[str]) -> str:
        txt = match.group(0)
        if len(txt) == 1 and _SINGLE_LETTER_PREFIX_GUARD_RE.search(match.string[: match.start()]):
            return txt
        if txt in self._syn_to_token_cs:
            return self._syn_to_token_cs[txt]
        tok = self._syn_to_token_ci.get(txt.lower())
        if tok:
            return tok
        return self._syn_to_token_cs.get(txt.upper(), txt)

    def _compute_composite_alias(self, s: str, *, strict_short_codes: bool = False) -> str:
        if s in _MILESTONE_SCHEMA_KEYS:
            return s
        out = s
        has_digit = any(ch.isdigit() for ch in s)
        is_delimited_id = ("_" in s or "-" in s or ":" in s) and " " not in s
        for strict_pat, ci_pat, tok in self._seg_patterns:
            pat = strict_pat if (strict_short_codes and not has_digit and not is_delimited_id) else ci_pat
            out = pat.sub(tok, out)
        if self._person_digit_re is not None:
            out = self._person_digit_re.sub(lambda m: f"{self._person_digit_map[m.group(1).lower()]}_", out)
        if self._person_camel_re is not None:
            out = self._person_camel_re.sub(
                lambda m: self._person_camel_map[m.group(1) or m.group(2)],
                out,
            )
        return out

    def _alias_composite_id(self, raw_id: str, *, strict_short_codes: bool = False) -> str:
        s = str(raw_id)
        if not s or s in _MILESTONE_SCHEMA_KEYS:
            return s
        with self._cache_lock:
            cached = self._comp_real_to_alias.get(s)
        if cached is not None:
            return cached

        # Replace known multi-segment account/contract patterns like "HB-7001" or "CONTRACT:CONTRACT-99"
        out = self._compute_composite_alias(s, strict_short_codes=strict_short_codes)

        if out != s:
            with self._cache_lock:
                self._comp_real_to_alias[s] = out
                self._comp_alias_to_real[out] = s
        return out

    def _dealias_composite_id(self, alias_id: str) -> str:
        s = str(alias_id)
        if not s:
            return s
        with self._cache_lock:
            cached = self._comp_alias_to_real.get(s) or self._key_alias_to_real.get(s)
        if cached is not None:
            return cached
        return self.from_alias_text(s)

    def _mask_dob_str(self, dob_str: str) -> str | int:
        txt = str(dob_str or "").strip()
        m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", txt)
        if not m:
            m_ym = re.match(r"^(\d{4})-(\d{2})$", txt)
            if not m_ym:
                return txt
            yr, mo = int(m_ym.group(1)), int(m_ym.group(2))
            if self.dob_granularity == "age_only":
                today = date.today()
                return today.year - yr - (today.month < mo)
            if self._should_shift_birthdays:
                shifted_yr = yr + self.birthday_shift_years
                return f"{shifted_yr:04d}-{mo:02d}"
            return txt
        yr, mo, dy = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if self.dob_granularity == "age_only":
            today = date.today()
            return today.year - yr - ((today.month, today.day) < (mo, dy))
        if self._should_shift_birthdays:
            shifted_yr = yr + self.birthday_shift_years
            masked = f"{shifted_yr:04d}-{mo:02d}-01"
            with self._cache_lock:
                if txt in self._known_dobs:
                    self._dob_alias_to_real[masked] = txt
                else:
                    self._dob_alias_to_real.setdefault(masked, txt)
            return masked
        if self.dob_granularity == "year_month":
            return mask_dob_year_month(txt)
        return txt

    def _alias_person_joined_tokens(self, text: str) -> str:
        out = text
        if self._person_camel_re is not None or self._person_digit_re is not None:
            def _sub_word(m: re.Match[str]) -> str:
                word = m.group(0)
                if word in _MILESTONE_SCHEMA_KEYS:
                    return word
                w_out = word
                if self._person_digit_re is not None:
                    w_out = self._person_digit_re.sub(
                        lambda pm: f"{self._person_digit_map[pm.group(1).lower()]}_",
                        w_out,
                    )
                if self._person_camel_re is not None:
                    w_out = self._person_camel_re.sub(
                        lambda pm: self._person_camel_map[pm.group(1) or pm.group(2)],
                        w_out,
                    )
                if w_out != word:
                    with self._cache_lock:
                        self._key_alias_to_real.setdefault(w_out, word)
                return w_out

            # Match full Unicode word tokens (\w+) so accented person stems
            # (e.g., Zsófia, Håkon) are not split mid-identifier.
            out = re.sub(r"\w+", _sub_word, out)
        return out

    def to_alias_text(self, text: str, *, scale_prose: bool = True) -> str:
        if not isinstance(text, str) or not text:
            return text
        out = text
        if (self.dob_granularity in ("year_month", "age_only", "shift_years") or self._should_shift_birthdays) and self._known_dobs:
            for dob in self._known_dobs:
                masked = self._mask_dob_str(dob)
                repl = f"age {masked}" if self.dob_granularity == "age_only" else str(masked)
                if dob in out:
                    out = out.replace(dob, repl)
                if self._should_shift_birthdays or self.dob_granularity == "age_only":
                    ym_masked = mask_dob_year_month(dob)
                    if ym_masked != dob and ym_masked in out:
                        out = out.replace(ym_masked, repl)
                    ym = dob[:7]
                    if re.search(rf"(?<!\d){re.escape(ym)}(?!\d)", out):
                        masked_ym = self._mask_dob_str(ym)
                        repl_ym = f"age {masked_ym}" if self.dob_granularity == "age_only" else str(masked_ym)
                        out = re.sub(rf"(?<!\d){re.escape(ym)}(?!(?:\d|-\d))", repl_ym, out)
        if self.scale_values and scale_prose:
            out = _scale_currency_in_text(out, self.value_scale)

        # Replace previously cached composite IDs before free-text composite regex
        with self._cache_lock:
            cached_items = sorted(self._comp_real_to_alias.items(), key=lambda kv: (-len(kv[0]), kv[0]))
        for r_key, a_key in cached_items:
            if r_key in out:
                out = re.sub(rf"(?<![A-Za-z0-9]){re.escape(r_key)}(?![A-Za-z0-9])", a_key, out)

        # Protect composite asset IDs embedded in text without touching milestone schema words
        out = COMPOSITE_ID_TEXT_RE.sub(lambda m: self._alias_composite_id(m.group(0), strict_short_codes=True), out)

        if self._outbound_re is not None:
            out = self._outbound_re.sub(self._replace_prose_match, out)

        # Catch camelCase & digit-joined person tokens (`ssnJasonClaimAge`, `success_to_jason85_pct`, `yun85_month`)
        out = self._alias_person_joined_tokens(out)
        return out

    def from_alias_text(self, text: str) -> str:
        if not isinstance(text, str) or not text:
            return text
        out = text
        # DESIGN INTENT (DP-03 / SA2-08 — Mode C `identity_and_values` Invariants):
        # 1. Outbound payloads scale share quantities AND dollar values by `k` while keeping per-share market
        #    `price` UNSCALED so `(k * shares) * price == k * value` holds on the wire.
        # 2. Inbound structured numeric tool arguments are unscaled by `1/k` strictly by key (`from_alias`),
        #    NEVER by regex-multiplying free-form `$` figures in LLM prose or `notes_md` (`from_alias_text`),
        #    because dividing statutory tax constants (e.g. NIIT thresholds or standard deductions) by `k`
        #    corrupts tax commentary. Instead, the chat UI displays a Mode C scaled-values indicator badge.
        with self._cache_lock:
            dob_items = list(self._dob_alias_to_real.items())
            comp_items = sorted(self._comp_alias_to_real.items(), key=lambda x: (-len(x[0]), x[0]))
            key_items = sorted(self._key_alias_to_real.items(), key=lambda x: (-len(x[0]), x[0]))

        for a_dob, r_dob in dob_items:
            if a_dob in out:
                out = out.replace(a_dob, r_dob)

        for a_key, r_key in comp_items:
            if a_key in out:
                out = re.sub(rf"(?<![A-Za-z0-9]){re.escape(a_key)}(?![A-Za-z0-9])", r_key, out)

        for a_key, r_key in key_items:
            if a_key in out:
                out = re.sub(rf"(?<![A-Za-z0-9]){re.escape(a_key)}(?![A-Za-z0-9])", r_key, out)

        # Stateless fallback for joined person tokens (`ssnPERSON_1ClaimAge`, `PERSON_2_85_month`, `PERSON_285_month`)
        if self._inbound_digit_re is not None:
            out = self._inbound_digit_re.sub(
                lambda m: f"{self._person_token_meta[m.group(1)]['id']}{m.group(2)}",
                out,
            )
        if self._inbound_concat_digit_re is not None:
            out = self._inbound_concat_digit_re.sub(
                lambda m: f"{self._person_token_meta[m.group(1)]['id']}{m.group(2)}",
                out,
            )
        if self._inbound_camel_re is not None:
            out = self._inbound_camel_re.sub(
                lambda m: self._person_token_meta[m.group(1) or m.group(2)]["camel"],
                out,
            )

        if self._inbound_re is not None:
            out = self._inbound_re.sub(lambda m: self.token_to_canonical.get(m.group(1), m.group(1)), out)
        return out

    def to_alias(self, obj: Any, *, key_hint: str = "") -> Any:
        if isinstance(obj, dict):
            # Handle family_birthdays {name, y, m, d} dict cleanly
            if {"name", "y", "m"}.issubset(obj.keys()):
                cloned = dict(obj)
                cloned["name"] = self.to_alias_text(str(obj.get("name") or ""), scale_prose=False)
                if self.dob_granularity == "age_only" and "y" in cloned:
                    today = date.today()
                    dy = int(cloned.get("d") or 1)
                    mo = int(cloned.get("m") or 1)
                    yr = int(cloned["y"])
                    cloned["age_years"] = today.year - yr - ((today.month, today.day) < (mo, dy))
                    cloned.pop("y", None)
                    cloned.pop("m", None)
                    cloned.pop("d", None)
                elif self._should_shift_birthdays and "y" in cloned:
                    cloned["y"] = int(cloned["y"]) + self.birthday_shift_years
                    if "d" in cloned:
                        cloned["d"] = 1
                elif self.dob_granularity == "year_month" and "d" in cloned:
                    cloned["d"] = 1
                return cloned

            amt_mode = str(obj.get("amount_mode") or "").strip().lower()
            is_pct_amount = amt_mode in ("pct_of_source", "percent", "pct", "percentage")

            out_d: dict[str, Any] = {}
            for k, v in obj.items():
                k_str = str(k)
                with self._cache_lock:
                    in_comp_real = k_str in self._comp_real_to_alias
                if key_hint in _COMPOSITE_DICT_PARENTS or in_comp_real:
                    new_k = self._alias_composite_id(k_str)
                else:
                    scrubbed_k = self._scrub_primary_symbol_keys(k_str)
                    new_k = self.to_alias_text(scrubbed_k, scale_prose=False)
                if new_k != k_str:
                    with self._cache_lock:
                        self._key_alias_to_real[new_k] = k_str

                if k_str in ("notes_md", "comment") and self.notes_policy == "strip":
                    out_d[new_k] = "[Redacted by Privacy Shield]" if v else v
                    continue
                if k_str in (
                    "birth_date",
                    "birthdate",
                    "birth_iso",
                    "dob",
                    "statement_dob",
                    "owner_birth_date",
                    "spouse_birth_date",
                    "date_of_birth",
                ) and isinstance(v, str):
                    out_d[new_k] = self._mask_dob_str(v)
                    continue
                if k_str in ("birth_year", "retirement_year") and isinstance(v, int) and not isinstance(v, bool):
                    out_d[new_k] = int(v) + self.birthday_shift_years if self._should_shift_birthdays else v
                    continue
                if k_str == "birth_day" and isinstance(v, int) and not isinstance(v, bool):
                    out_d[new_k] = 1 if (self._should_shift_birthdays or self.dob_granularity == "year_month") else v
                    continue
                if key_hint in ("holdingHalfLives", "holdingHalfLife", "holdingFeePct") and type(v) in (int, float):
                    out_d[new_k] = v
                    continue
                if k_str == "amount" and is_pct_amount:
                    out_d[new_k] = v
                    continue
                if k_str in ("transfers", "deposits", "withdrawals") and isinstance(v, (int, float)) and not isinstance(v, bool):
                    if key_hint == "action_summary" or (isinstance(v, int) and abs(v) < 10_000):
                        out_d[new_k] = v
                        continue
                    if self.scale_values:
                        scaled_v = float(v) * self.value_scale
                        out_d[new_k] = int(round(scaled_v)) if isinstance(v, int) and abs(scaled_v - round(scaled_v)) < 1e-6 else round(scaled_v, 2)
                        continue
                out_d[new_k] = self.to_alias(v, key_hint=k_str)
            return out_d

        if isinstance(obj, list):
            return [self.to_alias(item, key_hint=key_hint) for item in obj]

        if isinstance(obj, str):
            if key_hint in _COMPOSITE_KEYS:
                return self._alias_composite_id(obj)
            if key_hint in _STRUCTURED_SYMBOL_KEYS:
                s_up = obj.strip().upper()
                if s_up in self._structured_sym_to_token:
                    return self._structured_sym_to_token[s_up]
            return self.to_alias_text(obj)

        if self.scale_values and isinstance(obj, (int, float)) and not isinstance(obj, bool) and _should_scale_numeric_key(key_hint):
            scaled = float(obj) * self.value_scale
            if abs(scaled - round(scaled)) < 1e-6 and isinstance(obj, int):
                return int(round(scaled))
            if _is_share_qty_key(key_hint):
                r6 = round(scaled, 6)
                return int(round(r6)) if isinstance(obj, int) and abs(r6 - round(r6)) < 1e-6 else r6
            return round(scaled, 2)

        return obj

    def from_alias(self, obj: Any, *, key_hint: str = "") -> Any:
        if isinstance(obj, dict):
            if "name" in obj and ({"y", "m"}.issubset(obj.keys()) or "age_years" in obj or key_hint == "family_birthdays"):
                cloned = dict(obj)
                cloned["name"] = self.from_alias_text(str(obj.get("name") or ""))
                if self._should_shift_birthdays and isinstance(cloned.get("y"), (int, float)) and not isinstance(cloned.get("y"), bool):
                    cloned["y"] = int(cloned["y"]) - self.birthday_shift_years
                return cloned

            amt_mode = str(obj.get("amount_mode") or "").strip().lower()
            is_pct_amount = amt_mode in ("pct_of_source", "percent", "pct", "percentage")

            out_d: dict[str, Any] = {}
            for k, v in obj.items():
                k_str = str(k)
                with self._cache_lock:
                    cached_k = self._comp_alias_to_real.get(k_str) or self._key_alias_to_real.get(k_str)
                    dob_orig = self._dob_alias_to_real.get(v) if isinstance(v, str) else None
                if cached_k is not None:
                    new_k = cached_k
                elif key_hint in _COMPOSITE_DICT_PARENTS:
                    new_k = self._dealias_composite_id(k_str)
                else:
                    new_k = self.from_alias_text(self._restore_primary_symbol_keys(k_str))

                if new_k in ("birth_date", "birthdate", "birth_iso") and dob_orig is not None:
                    out_d[new_k] = dob_orig
                    continue
                if new_k in ("birth_year", "retirement_year") and self._should_shift_birthdays and isinstance(v, (int, float)) and not isinstance(v, bool):
                    out_d[new_k] = int(v) - self.birthday_shift_years
                    continue
                if key_hint in ("holdingHalfLives", "holdingHalfLife", "holdingFeePct") and type(v) in (int, float):
                    out_d[new_k] = v
                    continue
                if new_k == "amount" and is_pct_amount:
                    out_d[new_k] = v
                    continue
                if new_k in ("transfers", "deposits", "withdrawals") and isinstance(v, (int, float)) and not isinstance(v, bool):
                    if key_hint == "action_summary" or (isinstance(v, int) and abs(v) < 10_000):
                        out_d[new_k] = v
                        continue
                    if self.scale_values:
                        unscaled_v = float(v) / self.value_scale
                        out_d[new_k] = int(round(unscaled_v)) if abs(unscaled_v - round(unscaled_v)) < 1e-6 else round(unscaled_v, 2)
                        continue
                out_d[new_k] = self.from_alias(v, key_hint=new_k)
            return out_d

        if isinstance(obj, list):
            if key_hint == "symbols":
                return [self._dealias_composite_id(str(item)) if isinstance(item, str) else self.from_alias(item, key_hint=key_hint) for item in obj]
            return [self.from_alias(item, key_hint=key_hint) for item in obj]

        if isinstance(obj, str):
            if key_hint in _COMPOSITE_KEYS:
                return self._dealias_composite_id(obj)
            return self.from_alias_text(obj)

        if self.scale_values and isinstance(obj, (int, float)) and not isinstance(obj, bool) and _should_scale_numeric_key(key_hint):
            unscaled = float(obj) / self.value_scale
            if abs(unscaled - round(unscaled)) < 1e-6:
                return int(round(unscaled))
            if _is_share_qty_key(key_hint):
                return round(unscaled, 6)
            return round(unscaled, 2)

        return obj

    def find_leaked_terms(self, wire_payload_or_text: Any) -> list[str]:
        """Scans serialized wire JSON/text for any residual enabled entity synonym or unmasked DOB that slipped through."""
        if not isinstance(wire_payload_or_text, str):
            blob = json.dumps(wire_payload_or_text, ensure_ascii=False)
        else:
            blob = wire_payload_or_text

        leaked: list[str] = []
        seen: set[str] = set()

        def _record(term: str) -> None:
            low = term.lower()
            if low not in seen:
                seen.add(low)
                leaked.append(term)

        # Scrub milestone schema keys from a scratch copy so `ms_month` / `ms_real` never false-trigger short code `MS`
        scan_blob = blob
        for ms_k in _MILESTONE_SCHEMA_KEYS:
            if ms_k in scan_blob:
                scan_blob = scan_blob.replace(ms_k, "_milestone_key_")

        for ent in self.cfg.get("entities") or []:
            if not isinstance(ent, dict) or not ent.get("enabled", True):
                continue
            cat = str(ent.get("category") or "").lower()
            if not self.alias_products and cat in _PRODUCT_CATEGORIES:
                continue
            canon = str(ent.get("canonical") or "").strip()
            meta = ent.get("meta") if isinstance(ent.get("meta"), dict) else {}

            for syn in ent.get("synonyms") or []:
                s = str(syn).strip()
                if not s:
                    continue
                if len(s) == 1:
                    if s.upper() in ("A", "I"):
                        if re.search(rf"TICKER:{re.escape(s.upper())}(?![A-Za-z0-9])", scan_blob):
                            _record(s)
                        continue
                    if s.isupper() and s.isalpha():
                        for m1 in re.finditer(rf"(?<![A-Za-z0-9]){re.escape(s)}(?![A-Za-z0-9])", scan_blob):
                            if not _SINGLE_LETTER_PREFIX_GUARD_RE.search(scan_blob[: m1.start()]):
                                _record(s)
                                break
                    continue
                flags = 0 if is_exact_case_synonym(s, cat) else re.IGNORECASE
                if re.search(rf"(?<![A-Za-z0-9]){re.escape(s)}(?![A-Za-z0-9])", scan_blob, flags=flags):
                    _record(s)

            # Check camelCase & digit-joined person tokens (`ssnJasonClaimAge`, `success_to_jason85_pct`, `yun85_month`)
            if cat in _PERSON_CATEGORIES and canon:
                first_name = str(meta.get("first") or canon.split()[0]).strip()
                inv_id = str(meta.get("id") or first_name).strip().lower().replace(" ", "_")
                camel_full = canon.strip().title().replace(" ", "")
                camel_first = first_name.strip().title().replace(" ", "")
                for c_stem in {camel_full, camel_first}:
                    if len(c_stem) >= 3 and c_stem[:1].isupper() and c_stem.isalpha():
                        if re.search(
                            rf"(?:(?<=[a-z0-9]){re.escape(c_stem)}(?=[A-Z0-9]|$)|(?<![A-Za-z0-9]){re.escape(c_stem)}(?=[A-Z][a-z]))",
                            scan_blob,
                        ):
                            _record(c_stem)
                for d_stem in {inv_id, first_name.lower(), camel_full.lower()}:
                    if len(d_stem) >= 3 and re.match(r"^[a-z][a-z0-9_]*$", d_stem):
                        if re.search(rf"(?<![A-Za-z0-9]){re.escape(d_stem)}(?=[0-9])", scan_blob, flags=re.IGNORECASE):
                            _record(d_stem)

        # Scan for unmasked `_known_dobs` (`YYYY-MM-DD` or `YYYY-MM` depending on `dob_granularity`)
        if self._known_dobs:
            for dob in self._known_dobs:
                ym = dob[:7]
                if self._should_shift_birthdays or self.dob_granularity == "age_only":
                    if re.search(rf"(?<!\d){re.escape(dob)}(?!\d)", scan_blob):
                        _record(dob)
                    elif re.search(rf"(?<!\d){re.escape(ym)}(?!\d)", scan_blob):
                        _record(ym)
                elif self.dob_granularity == "year_month":
                    if dob != mask_dob_year_month(dob) and re.search(rf"(?<!\d){re.escape(dob)}(?!\d)", scan_blob):
                        _record(dob)

        return leaked

    def preview_round_trip(self, sample_text: str, sample_payload: Any = None) -> dict[str, Any]:
        wire_text = self.to_alias_text(sample_text)
        restored_text = self.from_alias_text(wire_text)
        wire_payload = self.to_alias(sample_payload) if sample_payload is not None else None
        restored_payload = self.from_alias(wire_payload) if wire_payload is not None else None
        leaks = self.find_leaked_terms(wire_text)
        if wire_payload is not None:
            for lk in self.find_leaked_terms(wire_payload):
                if lk not in leaks:
                    leaks.append(lk)
        return {
            "ok": True,
            "original_text": sample_text,
            "wire_text": wire_text,
            "restored_text": restored_text,
            "wire_payload": wire_payload,
            "restored_payload": restored_payload,
            "leaked_terms": leaks,
            "leak_free": len(leaks) == 0,
        }


def build_translator(registry_cfg: dict[str, Any], *, portfolio_id: str | None = None) -> PrivacyTranslator:
    return PrivacyTranslator(registry_cfg, portfolio_id=portfolio_id)
