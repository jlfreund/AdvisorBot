#!/usr/bin/env python3
"""
Canonical 7-class `RiskClass` validation, conflict detection, and public-ticker classification
helpers for built-in and agent-authored FI statement parsers (`IngestPipeline.html` Section 3).
"""

from __future__ import annotations

import re

from ingest.errors import InvalidHoldingClassificationError

VALID_RISK_CLASSES: tuple[str, ...] = (
    "cash_fixed",
    "fixed_income",
    "hybrid_buffered",
    "broad_equity",
    "real_diversifier",
    "single_stock",
    "crypto_speculative",
)

# Explicit conflict matrix: disallow nonsensical `(asset_class, risk_class)` pairs so buggy
# scrapers fail loudly at parse/insert time instead of silently corrupting simulation behavior.
ALLOWED_RISK_BY_ASSET_CLASS: dict[str, set[str]] = {
    "CASH": {"cash_fixed"},
    "MONEY_MARKET": {"cash_fixed"},
    "CD": {"cash_fixed", "fixed_income"},
    "FIXED_ANNUITY": {"cash_fixed", "fixed_income"},
    "MYGA": {"cash_fixed", "fixed_income"},
    "MUNI_BOND": {"fixed_income", "cash_fixed", "hybrid_buffered"},
    "BOND": {"fixed_income", "cash_fixed", "hybrid_buffered"},
    "TREASURY": {"fixed_income", "cash_fixed"},
    "TIPS": {"fixed_income", "real_diversifier"},
    "INDEXED_ANNUITY": {"fixed_income", "hybrid_buffered"},
    "RILA_ANNUITY": {"hybrid_buffered", "broad_equity"},
    "BALANCED_FUND": {"hybrid_buffered", "broad_equity", "fixed_income"},
    "TARGET_DATE_FUND": {"hybrid_buffered", "broad_equity", "fixed_income"},
    "BUFFER_ETF": {"hybrid_buffered", "broad_equity"},
    "HIGH_YIELD": {"hybrid_buffered", "fixed_income"},
    "MUTUAL_FUND": {
        "cash_fixed",
        "fixed_income",
        "hybrid_buffered",
        "broad_equity",
        "real_diversifier",
        "crypto_speculative",
    },
    "ETF": {
        "cash_fixed",
        "fixed_income",
        "hybrid_buffered",
        "broad_equity",
        "real_diversifier",
        "crypto_speculative",
    },
    "CIT": {"fixed_income", "hybrid_buffered", "broad_equity", "real_diversifier"},
    "REIT": {"broad_equity", "single_stock", "real_diversifier", "hybrid_buffered"},
    "COMMODITY": {"real_diversifier", "crypto_speculative"},
    "GOLD": {"real_diversifier"},
    "PRECIOUS_METAL": {"real_diversifier"},
    "EQUITY": {"single_stock", "crypto_speculative"},
    "RSU": {"single_stock", "crypto_speculative"},
    "GSU": {"single_stock"},
    "CRYPTO": {"crypto_speculative"},
    "LEVERAGED_ETF": {"crypto_speculative"},
    "OPTION": {"crypto_speculative", "single_stock"},
}


# Deterministic ticker catalogs for common ETFs / mutual funds encountered across brokerages
_CASH_TICKERS: frozenset[str] = frozenset({
    "SPAXX", "FDRXX", "FZFXX", "SPRXX", "FTEXX", "VMFXX", "VMMXX", "VUSXX",
    "SWVXX", "SNSXX", "SNOXX", "TTTXX", "TMCXX", "GVMXX", "WMPXX",
    "SGOV", "BIL", "SHV", "USFR", "TFLO", "TBIL", "GBIL", "CLIP", "BOXX",
})

_FIXED_INCOME_TICKERS: frozenset[str] = frozenset({
    "BND", "AGG", "SCHZ", "IUSB", "BNDX", "IAGG",
    "MUB", "VTEB", "TFI", "CMF", "NYF", "SUB", "VWIUX", "VWITX", "VCAIX",
    "VCIT", "VCSH", "VCLT", "LQD", "IGIB", "IGSB",
    "VGIT", "VGSH", "VGLT", "IEF", "IEI", "SHY", "TLT", "TLH", "GOVT", "SCHR", "SCHO", "SCHQ",
    "TIP", "SCHP", "VTIP", "STIP", "VBTLX", "FXNAX", "SWAGX", "DODIX", "PTTRX", "PIMIX",
})

_HYBRID_BUFFERED_TICKERS: frozenset[str] = frozenset({
    "VBIAX", "VBINX", "VWELX", "VWENX", "FPURX", "FBALX", "PRWCX",
    "AOR", "AOM", "AOK", "AOA",
    "VFIFX", "VFORX", "VTTHX", "VTTVX", "VTWNX", "VLXVX", "FFNOX",
    "BUFR", "BJAN", "BAPR", "BJUL", "BOCT", "PJAN", "PAPR", "PJUL", "POCT", "HEQT", "JEPI", "JEPQ",
    "HYG", "JNK", "USHY", "SHYG", "BKLN", "SRLN", "PFF", "PGX", "FPE",
})

_REAL_DIVERSIFIER_TICKERS: frozenset[str] = frozenset({
    "GLD", "GLDM", "IAU", "SGOL", "BAR", "PHYS",
    "SLV", "SIVR", "PSLV", "PPLT", "PALL",
    "DBC", "PDBC", "GSG", "COMT", "BCI", "FTGC", "USO", "UNG",
    "DBMF", "KMLM", "CTA", "WTMF",
})

_CRYPTO_SPECULATIVE_TICKERS: frozenset[str] = frozenset({
    "IBIT", "FBTC", "GBTC", "ARKB", "BITB", "HODL", "BTCO", "BRRR", "EZBC", "BTC",
    "ETHA", "FETH", "ETHE", "ETH", "BITO", "BITX",
    "TQQQ", "SQQQ", "UPRO", "SPXU", "SSO", "SDS", "UDOW", "URTY",
    "SOXL", "SOXS", "TECL", "TECS", "LABU", "LABD", "FNGU", "TMF", "TMV", "NVDL", "TSLL", "MSTU", "CONL",
})

_KNOWN_BROAD_EQUITY_ETFS: frozenset[str] = frozenset({
    "SPY", "VOO", "IVV", "SPLG", "VTI", "ITOT", "SCHB", "IWV", "VT",
    "QQQ", "QQQM", "VUG", "IWF", "SCHG", "MGK", "VOOG",
    "VTV", "IWD", "SCHV", "VIG", "SCHD", "DGRO", "VYM", "HDV", "DVY", "RSP", "QUAL", "MTUM", "USMV",
    "VO", "IJH", "MDY", "SCHM", "VB", "IJR", "IWM", "SCHA", "AVUV", "DFSV",
    "VXUS", "IXUS", "VEA", "IEFA", "EFA", "SCHF", "VWO", "IEMG", "EEM", "SCHE", "AVDV",
    "XLK", "XLF", "XLV", "XLE", "XLI", "XLY", "XLP", "XLU", "XLB", "XLRE", "XLC",
    "VGT", "SMH", "SOXX", "VNQ", "SCHH", "IYR",
    "VFIAX", "VTSAX", "VTIAX", "FXAIX", "FSKAX", "FTIHX", "SWPPX", "SWTSX", "SWISX",
})


def validate_risk_class(
    asset_class: str,
    risk_class: str | None,
    *,
    security_id: str | None = None,
    plugin_id: str | None = None,
    file_path: object = None,
) -> str:
    """
    Validates that `risk_class` is non-empty, belongs to the 7-class canonical taxonomy,
    and does not violate `ALLOWED_RISK_BY_ASSET_CLASS` for `asset_class`.
    Raises `InvalidHoldingClassificationError` on any violation.
    """
    sec_tag = f" for '{security_id}'" if security_id else ""
    ctx_parts = []
    if plugin_id:
        ctx_parts.append(f"plugin={plugin_id}")
    if file_path:
        ctx_parts.append(f"file={file_path}")
    ctx_tag = f" [{', '.join(ctx_parts)}]" if ctx_parts else ""
    rc = str(risk_class or "").strip().lower()
    if not rc:
        raise InvalidHoldingClassificationError(
            f"Missing required `risk_class`{sec_tag} (asset_class='{asset_class}'){ctx_tag}. "
            f"Must be one of {VALID_RISK_CLASSES}.",
            file_path=str(file_path) if file_path else None,
        )
    if rc not in VALID_RISK_CLASSES:
        raise InvalidHoldingClassificationError(
            f"Invalid `risk_class='{risk_class}'`{sec_tag}{ctx_tag}. "
            f"Must be one of {VALID_RISK_CLASSES}.",
            file_path=str(file_path) if file_path else None,
        )
    ac = str(asset_class or "").strip().upper()
    allowed = ALLOWED_RISK_BY_ASSET_CLASS.get(ac)
    if allowed is not None and rc not in allowed:
        raise InvalidHoldingClassificationError(
            f"Conflicting classification{sec_tag}{ctx_tag}: asset_class='{ac}' cannot use "
            f"risk_class='{rc}' (allowed for {ac}: {sorted(allowed)}).",
            file_path=str(file_path) if file_path else None,
        )
    return rc


def default_risk_class_for_asset_class(asset_class: str) -> str:
    """Deterministic migration backfill mapper for legacy `securities` rows."""
    ac = str(asset_class or "").strip().upper()
    if ac in ("CASH", "MONEY_MARKET", "CD", "FIXED_ANNUITY", "MYGA"):
        return "cash_fixed"
    if ac in ("MUNI_BOND", "BOND", "TREASURY", "TIPS", "INDEXED_ANNUITY"):
        return "fixed_income"
    if ac in ("RILA_ANNUITY", "BALANCED_FUND", "TARGET_DATE_FUND", "BUFFER_ETF", "HIGH_YIELD"):
        return "hybrid_buffered"
    if ac in ("COMMODITY", "GOLD", "PRECIOUS_METAL"):
        return "real_diversifier"
    if ac in ("CRYPTO", "LEVERAGED_ETF", "OPTION"):
        return "crypto_speculative"
    if ac in ("MUTUAL_FUND", "ETF", "CIT", "REIT"):
        return "broad_equity"
    return "single_stock"


def classify_public_security(
    symbol: str,
    canonical_name: str = "",
    asset_class: str = "EQUITY",
    quote_type: str | None = None,
) -> tuple[str, str]:
    """
    Deterministic classifier for public tickers (`symbol`, `canonical_name`, `asset_class`).
    Returns `(normalized_asset_class, risk_class)` so both built-in and agent-authored
    statement plugins classify ETFs, mutual funds, money markets, gold/commodities,
    crypto/leveraged products, and individual equities accurately without guesswork.
    """
    sym = str(symbol or "").strip().upper()
    name_up = str(canonical_name or "").strip().upper()
    ac = str(asset_class or "EQUITY").strip().upper()
    qt = str(quote_type or "").strip().upper()

    # 1. Exact ticker catalog matches
    if sym in _CASH_TICKERS:
        out_ac = "MUTUAL_FUND" if (sym.endswith("XX") or ac == "MUTUAL_FUND") else ("ETF" if ac in ("EQUITY", "ETF") else ac)
        return out_ac, "cash_fixed"
    if sym in _FIXED_INCOME_TICKERS:
        out_ac = "MUTUAL_FUND" if ac == "MUTUAL_FUND" or (len(sym) == 5 and sym.endswith("X")) else "ETF"
        return out_ac, "fixed_income"
    if sym in _HYBRID_BUFFERED_TICKERS:
        out_ac = "MUTUAL_FUND" if ac == "MUTUAL_FUND" or (len(sym) == 5 and sym.endswith("X")) else "ETF"
        return out_ac, "hybrid_buffered"
    if sym in _REAL_DIVERSIFIER_TICKERS:
        return ("ETF" if ac in ("EQUITY", "ETF") else ac), "real_diversifier"
    if sym in _CRYPTO_SPECULATIVE_TICKERS:
        return ("ETF" if ac in ("EQUITY", "ETF") else ac), "crypto_speculative"
    if sym in _KNOWN_BROAD_EQUITY_ETFS:
        out_ac = "MUTUAL_FUND" if ac == "MUTUAL_FUND" or (len(sym) == 5 and sym.endswith("X")) else "ETF"
        return out_ac, "broad_equity"

    # 2. Money market / 5-letter 'XX' sweep tickers
    if (len(sym) == 5 and sym.endswith("XX")) or "MONEY MARKET" in name_up or "TREASURY BILL ETF" in name_up:
        out_ac = "MUTUAL_FUND" if len(sym) == 5 and sym.endswith("XX") else ("ETF" if "ETF" in name_up else "CASH")
        return out_ac, "cash_fixed"

    # 3. Detect ETFs / Mutual Funds / CITs from quote_type, asset_class, or unambiguous fund naming
    is_fund = (
        ac in ("ETF", "MUTUAL_FUND", "CIT", "TARGET_DATE_FUND", "BALANCED_FUND", "BUFFER_ETF")
        or qt in ("ETF", "MUTUALFUND")
        or (len(sym) == 5 and sym.endswith("X") and "CORP" not in name_up and "INC" not in name_up)
        or bool(re.search(r"\b(?:ETF|ETFS|INDEX FD|INDEX FUND|MUTUAL FUND|TARGET RETIREMENT|TARGET DATE)\b", name_up))
    )

    if is_fund:
        norm_ac = ac if ac in ("MUTUAL_FUND", "ETF", "CIT", "TARGET_DATE_FUND", "BALANCED_FUND", "BUFFER_ETF") else (
            "MUTUAL_FUND" if (len(sym) == 5 and sym.endswith("X")) or "FUND" in name_up and "ETF" not in name_up else "ETF"
        )
        if re.search(r"\b(?:2X|3X|ULTRA|ULTRAPRO|DIREXION DAILY|LEVERAGED|BITCOIN|ETHEREUM|ETHER|CRYPTO)\b", name_up):
            return norm_ac, "crypto_speculative"
        if re.search(r"\b(?:GOLD|SILVER|BULLION|PRECIOUS METAL|COMMODIT|MANAGED FUTURES)\b", name_up):
            return norm_ac, "real_diversifier"
        if re.search(r"\b(?:TARGET DATE|TARGET RETIREMENT|RETIREMENT 20\d\d|BALANCED|ALLOCATION|BUFFER|DEFINED OUTCOME|HIGH YIELD|PREFERRED)\b", name_up):
            return norm_ac, "hybrid_buffered"
        if re.search(r"\b(?:MUNI|MUNICIPAL|TREASURY|BOND|FIXED INCOME|AGGREGATE|TIPS|INFLATION PROTECTED)\b", name_up):
            return norm_ac, "fixed_income"
        return norm_ac, "broad_equity"

    # 4. Individual equities / crypto tokens
    if ac in ("CRYPTO", "LEVERAGED_ETF", "OPTION"):
        return ac, "crypto_speculative"
    if ac in ("GOLD", "PRECIOUS_METAL", "COMMODITY"):
        return ac, "real_diversifier"
    if ac in ("CASH", "FIXED_ANNUITY", "MYGA", "CD"):
        return ac, "cash_fixed"
    if ac in ("MUNI_BOND", "BOND", "TREASURY", "TIPS", "INDEXED_ANNUITY"):
        return ac, "fixed_income"
    if ac in ("RILA_ANNUITY",):
        return ac, "hybrid_buffered"

    return ac, "single_stock"
