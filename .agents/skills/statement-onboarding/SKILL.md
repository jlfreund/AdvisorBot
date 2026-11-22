---
name: statement-onboarding
description: Inspect unfamiliar Financial Institution (FI) PDF/XLSX/CSV/JSON statements, write custom StatementPlugin parsers with mandatory 7-class RiskClass classification, verify accounting checksums and forecast enrichment in an isolated in-memory dry-run, and onboard new statements into the Portfolio Tracker.
---

# Statement Onboarding Skill (Phase E)

Use this skill whenever onboarding a new Financial Institution (FI) statement format (e.g., Fidelity, Charles Schwab, Vanguard, Interactive Brokers, Merrill, TIAA, or custom annuity/advisory PDFs) into `Portfolios/<active>/Statements/`.

## Zero-PII Rule

- Write household-specific or account-specific custom parsers into the gitignored directory **`Portfolios/<active>/Plugins/<fi>_plugin.py`** and opt them in by adding `"allowed_plugins": ["<fi>_plugin.py"]` (or `"allow_custom_plugins": true`) in `Portfolios/<active>/project.json` (`SA-15` requires explicit opt-in before executing custom `.py` plugins; once opted in, custom plugins take higher tie-break priority than built-in plugins in `Scripts/ingest/plugins/`).
- Never hardcode personal names, account numbers, or balances into git-tracked files under `Scripts/`.

---

## 4-Step Agent Onboarding Loop

### Step 1: Inspect the Raw Statement
Run `inspect-statement` to extract page-numbered text, candidate table headers, candidate tickers/CUSIPs, dollar amounts, and existing plugin detection scores:
```bash
python3 Scripts/holdings.py inspect-statement "Portfolios/<active>/Statements/<file>.pdf"
# Optional structured JSON output:
python3 Scripts/holdings.py inspect-statement "Portfolios/<active>/Statements/<file>.pdf" --json
```

### Step 2: Implement a `StatementPlugin` Subclass
Create `Portfolios/<active>/Plugins/<fi>_plugin.py` subclassing `StatementPlugin` (`Scripts/ingest/framework.py`):

```python
from __future__ import annotations
from ingest.classification import classify_public_security
from ingest.errors import StatementParseError
from ingest.framework import IngestContext, StatementPlugin
from ingest.models import ParsedPosition, ParsedStatement


class CustomBrokeragePlugin(StatementPlugin):
    plugin_id = "custom_brokerage_pdf"
    institution_name = "Custom Brokerage"
    supported_extensions = (".pdf",)

    def detect(self, ctx: IngestContext) -> float:
        text = "\n".join(ctx.pages or [])
        if "Custom Brokerage" in text and "Account Summary" in text:
            return 0.95
        return 0.0

    def parse(self, ctx: IngestContext) -> ParsedStatement:
        text = "\n".join(ctx.pages or [])
        if not text.strip():
            raise StatementParseError("Empty PDF text", plugin_id=self.plugin_id, file_path=ctx.file_path)

        acct_id = "CUSTOM_ACCT_1"
        name, acct_type, tax_status, order = ctx.resolve_account_metadata(
            acct_id,
            default_name="Custom Brokerage Account",
            default_type="BROKERAGE",
            default_tax_status="TAXABLE",  # TAXABLE | ROTH_IRA | TRAD_IRA | 401K | ROTH_401K | HSA
            default_display_order=50,
        )

        # Classify public tickers/ETFs/funds deterministically, or set explicit risk_class:
        asset_cls, risk_cls = classify_public_security("VTI", "Vanguard Total Stock Market ETF")
        positions = [
            ParsedPosition(
                account_id=acct_id,
                security_id="VTI",
                symbol="VTI",
                cusip=None,
                name="Vanguard Total Stock Market ETF",
                asset_class=asset_cls,       # e.g. 'ETF'
                risk_class=risk_cls,         # REQUIRED: one of the 7 canonical RiskClass values
                category="Broad Equity ETF",
                quantity=100.0,
                unit_price=280.00,
                market_value=28000.00,
                cost_basis=20000.00,
                unrealized_gain=8000.00,
                yield_pct=1.35,              # Statement YTM / SEC yield (%) when reported
                coupon_pct=None,
                is_scrapeable=True,          # True for public Yahoo Finance tickers
                acquisition_date=None,       # Optional 'YYYY-MM-DD' for inception CAGR on non-public assets
                maturity_date=None,          # Optional 'YYYY-MM-DD' for bonds / CDs / annuity surrender end
                credited_rate_pct=None,      # Optional contractual APY (%) for cash / CDs / MYGAs
                reported_return_pct=None,    # Optional statement-printed annualized return (%)
            )
        ]

        return ParsedStatement(
            doc_type="CUSTOM_BROKERAGE",
            institution=self.institution_name,
            account_id=acct_id,
            account_name=name,
            account_type=acct_type,
            tax_status=tax_status,
            display_order=order,
            period_start=None,
            as_of_date="2026-03-31",
            reported_total=28000.00,         # Reconciled within ±$0.05 against sum(positions)
            positions=positions,
        )
```

---

## Mandatory 7-Class `RiskClass` Taxonomy

Every `ParsedPosition` **must** set `risk_class` to one of the 7 canonical values (omitting it or passing a conflicting `(asset_class, risk_class)` pair raises `InvalidHoldingClassificationError` at construction and DB upsert time):

| `risk_class` | Preset $\sigma$ | Typical Holdings & Valid `asset_class` Pairings |
|---|---|---|
| `cash_fixed` | `0.0%` | Bank sweeps, money market funds (`VMFXX`, `SPAXX`), T-bills, CDs, fixed MYGAs (`CASH`, `MONEY_MARKET`, `CD`, `MYGA`, `FIXED_ANNUITY`, `ETF`, `MUTUAL_FUND`) |
| `fixed_income` | `4.5%` | Individual municipal/Treasury/corporate bonds, bond ETFs/funds (`BND`, `AGG`), principal-protected FIAs (`MUNI_BOND`, `TREASURY_BOND`, `CORP_BOND`, `BOND_ETF`, `INDEXED_ANNUITY`, `ETF`, `MUTUAL_FUND`) |
| `hybrid_buffered` | `8.5%` | Balanced 60/40 funds, target-date funds, buffered outcome ETFs, downside-buffered RILAs (`BALANCED_FUND`, `RILA_ANNUITY`, `ETF`, `MUTUAL_FUND`) |
| `broad_equity` | `14.5%` | Diversified index/sector/international ETFs (`VTI`, `SPY`, `VXUS`, `VNQ`), equity mutual funds (`ETF`, `MUTUAL_FUND`, `REIT`) |
| `real_diversifier` | `15.0%` | Gold/precious-metal & commodity trusts (`GLD`, `IAU`, `DBC`) (`COMMODITY_ETF`, `REAL_ASSET`, `ETF`, `MUTUAL_FUND`) |
| `single_stock` | `36.2%` | Individual corporate stocks (`AAPL`, `GOOG`, `NVDA`) and vested/unvested RSU/GSU awards (`EQUITY`) |
| `crypto_speculative` | `68.0%` | Spot Bitcoin/Ether ETFs (`IBIT`, `FBTC`), 2x/3x leveraged ETFs (`TQQQ`, `SOXL`), options, crypto (`CRYPTO`, `LEVERAGED_ETF`, `SPECULATIVE`, `OPTION`, `ETF`) |

> **Tip**: For public tickers, call `classify_public_security(symbol, description, reported_asset_category)` from `Scripts/ingest/classification.py` to deterministically map ETFs, MMFs, bond funds, target-date/buffer funds, gold trusts, leveraged/crypto funds, and single stocks.

---

## Enrichment Fields for Accurate Projected Returns

Post-ingest, `Scripts/valuation/forecast_enricher.py` automatically populates `holding_forecasts_baseline`:
1. **Public Tickers (`is_scrapeable=True`)**: Fetches 10-Year Yahoo Finance `adj_close` (dividend-reinvested total return) CAGR + 10Y realized annualized volatility ($\sigma$) & $\beta_{\text{SPY}}$.
2. **Contractual Cash, MYGAs & Bonds (`DP-4`)**: Uses `credited_rate_pct` (cash/CD/MYGA APY) or `yield_pct` (nominal statement YTM/YTW) first, falling back to `coupon_pct`.
3. **Structured Annuities & Non-Public Funds**: Uses `reported_return_pct`, or inception CAGR from `(cost_basis, market_value, acquisition_date)` when holding window $\ge 180\text{d}$ (`<180d` falls back to the class benchmark with `confidence="LOW"` + CLI warning).

---

### Step 3: Dry-Run Verification (`test-plugin`)
Always verify your plugin in an isolated `:memory:` SQLite database before touching the live portfolio:
```bash
python3 Scripts/holdings.py test-plugin "Portfolios/<active>/Statements/<file>.pdf"
```
Confirm:
- Detection score $> 0.80$
- Zero `InvalidHoldingClassificationError` exceptions
- Strict checksum (`reported_total` vs. summed `positions`) within `±$0.05`
- Sensible `PROJ_RET`, `CONF`, and `SOURCE` across every holding

### Step 4: Live Refresh & Render
Once `test-plugin` passes cleanly, run the full refresh to ingest into `Portfolios/<active>/portfolio.sqlite`, enrich forecasts, and render `index.html`:
```bash
python3 Scripts/holdings.py refresh
```
