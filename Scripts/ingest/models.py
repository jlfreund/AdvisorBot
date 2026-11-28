#!/usr/bin/env python3
"""Strongly-typed intermediate data structures produced by statement parsers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ingest.classification import validate_risk_class


@dataclass
class ParsedPosition:
    security_id: str                 # e.g. 'TICKER:AAPL', 'CUSIP:123456789', 'CONTRACT:1000000001', 'CASH:HB-7001'
    symbol_or_code: str              # e.g. 'AAPL', '123456789', '1000000001', 'USD'
    canonical_name: str              # Clean human-readable title
    asset_class: str                 # 'EQUITY', 'ETF', 'MUTUAL_FUND', 'MUNI_BOND', 'BOND', 'FIXED_ANNUITY', 'INDEXED_ANNUITY', 'RILA_ANNUITY', 'CASH', ...
    stack_bucket: str                # Chart rollup key
    is_scrapeable: bool              # True for public Yahoo/Google tickers, False for statement-anchored holdings
    quote_symbol: str | None         # Normalized Yahoo symbol if is_scrapeable else None
    quantity: float                  # Share count, bond par value, or 1.0 for contracts/cash
    stmt_price: float | None         # Per-share or per-100 par price
    market_value: float              # Reported USD market value
    value_date: str                  # Exact 'AS OF' YYYY-MM-DD for this holding (e.g. '2026-10-29' for annuities)
    risk_class: str = ""             # REQUIRED 7-class RiskClass ('cash_fixed'..'crypto_speculative'); validated in __post_init__
    accrued_interest: float = 0.0
    cost_basis: float | None = None
    est_annual_income: float | None = None
    yield_pct: float | None = None
    surrender_value: float | None = None
    issue_date: str | None = None    # YYYY-MM-DD if reported (e.g. annuity issue date or equity acquisition date)
    initial_premium: float | None = None
    # Phase E Forecast Enrichment fields (optional):
    acquisition_date: str | None = None          # Earliest tax-lot or contract purchase date (YYYY-MM-DD)
    maturity_date: str | None = None             # Bond maturity, CD maturity, or annuity surrender-free date (YYYY-MM-DD)
    expense_ratio_pct: float | None = None       # Fund / contract annual fee % if disclosed on statement
    credited_rate_pct: float | None = None       # Contractual fixed APY / MYGA guarantee / CD coupon %
    reported_return_pct: float | None = None     # Annualized IRR / TWR % if printed directly on FI statement
    reported_return_window: str | None = None    # e.g. 'SINCE_INCEPTION', '5Y', '3Y', '1Y'
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.risk_class = validate_risk_class(
            self.asset_class,
            self.risk_class,
            security_id=self.security_id,
        )


@dataclass
class ParsedAdjustment:
    security_id: str
    effective_date: str              # YYYY-MM-DD
    adj_type: str                    # 'PRE_SPLIT_SHARES' | 'HISTORICAL_SHARES'
    pre_quantity: float
    post_quantity: float
    notes: str = ""


@dataclass
class ParsedGSUTranche:
    grant_id: str
    grant_date: str                  # YYYY-MM-DD
    vest_date: str                   # YYYY-MM-DD
    orig_qty: float
    deus: float
    total_qty: float
    symbol: str = ""
    company_name: str = ""
    award_type: str = "RSU"
    account_id: str = ""


@dataclass
class ParsedGSURelease:
    order_number: str
    vest_date: str                   # YYYY-MM-DD
    release_price: float
    gross_shares: float
    net_shares: float
    tax_method: str = ""
    grant_id: str = "UNRESOLVED"
    symbol: str = ""
    company_name: str = ""
    award_type: str = "RSU"
    account_id: str = ""


@dataclass
class ParsedGSUWithdrawal:
    order_number: str
    exec_date: str                   # YYYY-MM-DD
    plan_name: str                   # Plan name or 'Cash'
    tx_type: str                     # 'Sale' | 'Transfer'
    price: float
    quantity: float                  # Negative delta
    net_amount: float | None = None
    symbol: str = ""
    account_id: str = ""


# Generic aliases for multi-company / multi-FI stock grants
ParsedGrantTranche = ParsedGSUTranche
ParsedGrantRelease = ParsedGSURelease
ParsedGrantWithdrawal = ParsedGSUWithdrawal


@dataclass
class ParsedTaxSource:
    """Tax-wrapper source slice inside a multi-source employer retirement plan (e.g. Pre-Tax vs. Roth 401(k))."""

    source_id: str                           # e.g. 'PRETAX', 'ROTH'
    tax_status: str                          # e.g. '401K', 'ROTH_401K', '403B'
    closing_balance: float                   # Reported closing USD balance for this tax source
    opening_balance: float | None = None     # Optional period-opening USD balance
    account_name: str | None = None          # Optional default sub-account display name
    account_type: str | None = None          # Optional default sub-account type label
    cost_basis: float | None = None          # Optional contributed principal / basis for this slice


@dataclass
class ParsedStatement:
    doc_type: str                    # 'WFA_SNAPSHOT', 'MS_QUARTERLY', 'MS_UNVESTED_GSU', 'MS_ACTIVITY_XLSX'
    institution: str
    account_id: str | None
    account_name: str | None
    account_type: str | None
    tax_status: str | None
    display_order: int
    period_start: str | None         # YYYY-MM-DD
    as_of_date: str                  # YYYY-MM-DD
    reported_total: float | None
    positions: list[ParsedPosition] = field(default_factory=list)
    adjustments: list[ParsedAdjustment] = field(default_factory=list)
    gsu_tranches: list[ParsedGSUTranche] = field(default_factory=list)
    gsu_releases: list[ParsedGSURelease] = field(default_factory=list)
    gsu_withdrawals: list[ParsedGSUWithdrawal] = field(default_factory=list)
    tax_sources: list[ParsedTaxSource] = field(default_factory=list)
