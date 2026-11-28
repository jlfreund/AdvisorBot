-- Canonical SQLite schema for AdvisorBot (Data/portfolio.sqlite)

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS update_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    mode TEXT NOT NULL,                      -- 'REFRESH', 'HISTORICAL', 'FUTURE', 'INGEST'
    started_at REAL NOT NULL,                -- Unix epoch seconds (high-res watermark for file scan)
    started_iso TEXT NOT NULL,
    completed_iso TEXT,
    files_scanned INTEGER DEFAULT 0,
    files_ingested INTEGER DEFAULT 0,
    positions_changed INTEGER DEFAULT 0,
    anchors_updated INTEGER DEFAULT 0,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS accounts (
    account_id TEXT PRIMARY KEY,             -- e.g. 'ACCT-01', 'ACCT-02'
    institution TEXT NOT NULL,               -- e.g. 'Brokerage Custodian' | 'Stock Plan Administrator'
    account_name TEXT NOT NULL,
    account_type TEXT NOT NULL,
    tax_status TEXT NOT NULL,                -- 'TAXABLE', 'TRAD_IRA', 'ROTH_IRA', '401K', 'ROTH_401K'
    display_order INTEGER DEFAULT 100,
    parent_account_id TEXT                   -- Optional parent employer plan ID when split across tax sources
);

CREATE TABLE IF NOT EXISTS ingested_files (
    sha256_hash TEXT PRIMARY KEY,
    file_path TEXT NOT NULL,
    rel_path TEXT NOT NULL,
    file_size INTEGER NOT NULL,
    file_birthtime REAL NOT NULL,
    file_mtime REAL NOT NULL,
    file_ctime REAL NOT NULL,
    doc_type TEXT NOT NULL,                  -- Statement plugin document type identifier
    account_id TEXT,
    period_start TEXT,
    statement_as_of_date TEXT NOT NULL,
    reported_total REAL,
    ingested_at_iso TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ingested_files_path ON ingested_files(file_path);
CREATE INDEX IF NOT EXISTS idx_ingested_files_asof ON ingested_files(statement_as_of_date);

CREATE TABLE IF NOT EXISTS securities (
    security_id TEXT PRIMARY KEY,            -- 'TICKER:AAPL', 'CUSIP:000000000', 'CONTRACT:001', 'CASH:ACCT-01'
    symbol_or_code TEXT NOT NULL,
    canonical_name TEXT NOT NULL,
    asset_class TEXT NOT NULL,               -- 'EQUITY', 'ETF', 'MUTUAL_FUND', 'MUNI_BOND', 'FIXED_ANNUITY', 'INDEXED_ANNUITY', 'RILA_ANNUITY', 'CASH'
    risk_class TEXT NOT NULL CHECK (risk_class IN ('cash_fixed','fixed_income','hybrid_buffered','broad_equity','real_diversifier','single_stock','crypto_speculative')),
    stack_bucket TEXT NOT NULL,              -- Rollup key for stacked bar chart
    is_scrapeable INTEGER NOT NULL,          -- 1 = public Yahoo/Google ticker, 0 = statement-anchored
    quote_symbol TEXT,                       -- Yahoo Finance symbol if is_scrapeable = 1
    valuation_model TEXT NOT NULL,           -- 'LIVE_TICKER' | 'STATEMENT_ANCHOR'
    metadata_json TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS position_snapshots (
    account_id TEXT NOT NULL,
    as_of_date TEXT NOT NULL,                -- Statement closing date YYYY-MM-DD
    security_id TEXT NOT NULL,
    quantity REAL NOT NULL,                  -- Shares, par face value, or 1.0 for contracts/cash
    stmt_price REAL,
    stmt_market_value REAL NOT NULL,
    accrued_interest REAL DEFAULT 0.0,
    cost_basis REAL,
    est_annual_income REAL,
    yield_pct REAL,
    status TEXT NOT NULL DEFAULT 'OPEN',     -- 'OPEN' | 'CLOSED'
    source_hash TEXT NOT NULL,
    details_json TEXT DEFAULT '{}',
    PRIMARY KEY (account_id, as_of_date, security_id),
    FOREIGN KEY (account_id) REFERENCES accounts(account_id),
    FOREIGN KEY (security_id) REFERENCES securities(security_id)
);

CREATE INDEX IF NOT EXISTS idx_pos_snap_lookup ON position_snapshots(account_id, as_of_date);

CREATE TABLE IF NOT EXISTS position_adjustments (
    adjustment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL,
    security_id TEXT NOT NULL,
    effective_date TEXT NOT NULL,            -- Effective date YYYY-MM-DD for split or opening share adjustment
    adj_type TEXT NOT NULL,                  -- 'PRE_SPLIT_SHARES' | 'OPENING_SHARES'
    pre_quantity REAL NOT NULL,
    post_quantity REAL NOT NULL,
    source_hash TEXT,
    notes TEXT,
    UNIQUE(account_id, security_id, effective_date, adj_type)
);

CREATE TABLE IF NOT EXISTS valuation_anchors (
    security_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    value_date TEXT NOT NULL,                -- Exact 'AS OF' valuation date YYYY-MM-DD reported on statement
    market_value REAL NOT NULL,              -- Copied non-scrapeable value
    surrender_value REAL,
    accrued_interest REAL DEFAULT 0.0,
    issue_date TEXT,                         -- YYYY-MM-DD if reported (e.g. annuity contract issue date)
    initial_premium REAL,                    -- Initial premium if reported on contract
    source_hash TEXT NOT NULL,
    metadata_json TEXT DEFAULT '{}',
    PRIMARY KEY (security_id, account_id, value_date),
    FOREIGN KEY (security_id) REFERENCES securities(security_id),
    FOREIGN KEY (account_id) REFERENCES accounts(account_id)
);

CREATE INDEX IF NOT EXISTS idx_anchors_lookup ON valuation_anchors(account_id, security_id, value_date);

-- PORTABILITY DO: Require parsers to supply `symbol` explicitly (`symbol TEXT NOT NULL` with no default ticker) (`LL-14`, `LL-30`).
-- PORTABILITY DON'T: Add a `DEFAULT` ticker literal to `gsu_tranches` or `gsu_releases_history`.
CREATE TABLE IF NOT EXISTS gsu_tranches (
    report_date TEXT NOT NULL,               -- Report run date YYYY-MM-DD
    symbol TEXT NOT NULL,                    -- Equity ticker symbol (supplied by parser)
    company_name TEXT DEFAULT '',            -- Issuer company name
    award_type TEXT NOT NULL DEFAULT 'RSU',  -- Award type ('RSU', 'GSU', 'PSU', etc.)
    account_id TEXT DEFAULT '',              -- Optional target account ID
    institution TEXT DEFAULT '',             -- Institution / broker name
    grant_id TEXT NOT NULL,
    vest_date TEXT NOT NULL,                 -- YYYY-MM-DD
    grant_date TEXT NOT NULL,
    orig_qty REAL NOT NULL,
    deus REAL NOT NULL,
    total_qty REAL NOT NULL,
    source_hash TEXT NOT NULL,
    PRIMARY KEY (report_date, symbol, grant_id, vest_date)
);

CREATE INDEX IF NOT EXISTS idx_gsu_vest_date ON gsu_tranches(report_date, vest_date);

CREATE TABLE IF NOT EXISTS gsu_releases_history (
    order_number TEXT PRIMARY KEY,           -- Release order identifier from Stock Plan Activity Report
    symbol TEXT NOT NULL,                    -- Equity ticker symbol (supplied by parser)
    company_name TEXT DEFAULT '',            -- Issuer company name
    award_type TEXT NOT NULL DEFAULT 'RSU',  -- Award type
    account_id TEXT DEFAULT '',              -- Optional account ID
    institution TEXT DEFAULT '',             -- Institution / broker name
    vest_date TEXT NOT NULL,                 -- YYYY-MM-DD
    grant_id TEXT NOT NULL,                  -- Resolved Grant ID
    release_price REAL NOT NULL,             -- FMV price at release
    gross_shares REAL NOT NULL,
    net_shares REAL NOT NULL,
    tax_method TEXT,
    source_hash TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_gsu_rel_hist_date ON gsu_releases_history(vest_date, grant_id);

CREATE TABLE IF NOT EXISTS gsu_withdrawals (
    order_number TEXT PRIMARY KEY,           -- Withdrawal order identifier
    symbol TEXT NOT NULL,                    -- Equity ticker symbol (supplied by parser)
    account_id TEXT DEFAULT '',              -- Optional account ID
    institution TEXT DEFAULT '',             -- Institution / broker name
    exec_date TEXT NOT NULL,                 -- YYYY-MM-DD
    plan_name TEXT NOT NULL,                 -- Plan name or 'Cash'
    tx_type TEXT NOT NULL,                   -- 'Sale' | 'Transfer'
    price REAL NOT NULL,
    quantity REAL NOT NULL,                  -- Negative share/cash delta
    net_amount REAL,
    source_hash TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_gsu_wdr_date ON gsu_withdrawals(exec_date);

CREATE TABLE IF NOT EXISTS ingest_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER,
    occurred_at_iso TEXT NOT NULL,
    event_type TEXT NOT NULL,                -- 'FILE_INGESTED', 'NEW_POSITION', 'QTY_CHANGED', 'POSITION_CLOSED', 'ANCHOR_UPDATED'
    account_id TEXT,
    security_id TEXT,
    as_of_date TEXT,
    old_value REAL,
    new_value REAL,
    summary TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS market_prices (
    quote_symbol TEXT NOT NULL,
    price_date TEXT NOT NULL,                -- YYYY-MM-DD (Friday date for weekly bars, or exact date for live)
    close_price REAL NOT NULL,               -- Split-adjusted market close
    adj_close REAL,
    fetched_at_iso TEXT NOT NULL,
    PRIMARY KEY (quote_symbol, price_date)
);

CREATE INDEX IF NOT EXISTS idx_market_prices_sym_date ON market_prices(quote_symbol, price_date);

CREATE TABLE IF NOT EXISTS timeline_bars (
    bar_date TEXT PRIMARY KEY,               -- YYYY-MM-DD (Friday close date, Today, or Future Friday)
    bar_kind TEXT NOT NULL,                  -- 'OBSERVED' | 'BACKCAST' | 'TODAY' | 'FORECAST'
    vested_total REAL NOT NULL,              -- Sum of all owned/anchored holdings on bar_date (excludes future unvested releases)
    projected_release_gross REAL NOT NULL DEFAULT 0.0,   -- Strictly 0 for bar_date <= today; >0 on future bars as vest_date <= bar_date
    projected_release_shares REAL NOT NULL DEFAULT 0.0,  -- Cumulative gross shares released in (today, bar_date]
    bucket_totals_json TEXT NOT NULL,        -- JSON map of stack_bucket -> USD value
    account_totals_json TEXT NOT NULL,       -- JSON map of account_id -> USD value
    asset_totals_json TEXT NOT NULL,         -- JSON map of asset_class -> USD value
    computed_at_iso TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS holding_preferences (
    holding_key TEXT PRIMARY KEY,            -- e.g. 'single_ACCT-01_CONTRACT001' or 'ACCT-01:CONTRACT:001'
    account_id TEXT,                         -- e.g. 'ACCT-01'
    security_id TEXT,                        -- e.g. 'CONTRACT:001'
    statement_name TEXT NOT NULL,            -- e.g. 'Fixed Annuity'
    friendly_name TEXT NOT NULL DEFAULT '',  -- User-specified short friendly name
    institution TEXT,                        -- Custodian / financial institution name
    investment_type TEXT,                    -- e.g. 'Fixed Annuity (Tax-Deferred MYGA)'
    maturity_date TEXT,                      -- e.g. '2027-12-23'
    fee_pct REAL,                            -- Annual fee percentage (e.g. 0.8 for 0.8%)
    comment TEXT,                            -- User comment / descriptive notes
    notes TEXT,                              -- Additional system notes / tags
    updated_at_iso TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_holding_pref_acct_sec ON holding_preferences(account_id, security_id);

CREATE TABLE IF NOT EXISTS holding_forecasts_baseline (
    account_id TEXT NOT NULL DEFAULT '',     -- Account ID or '' for global security fallback
    security_id TEXT NOT NULL,
    risk_class TEXT NOT NULL CHECK (risk_class IN ('cash_fixed','fixed_income','hybrid_buffered','broad_equity','real_diversifier','single_stock','crypto_speculative')),
    projected_return_pct REAL NOT NULL,
    realized_vol_pct REAL,                   -- 10Y annualized weekly log-return sigma % (public tickers)
    beta_spy REAL,                           -- 10Y weekly beta vs SPY (public tickers)
    corr_spy REAL,                           -- 10Y weekly Pearson correlation vs SPY
    lookback_years REAL,                     -- Actual observation window in years
    return_source TEXT NOT NULL,             -- 'PUBLIC_10Y_TOTAL_RETURN', 'STATEMENT_YTM', 'CONTRACT_CREDITED_RATE', 'STATEMENT_INCEPTION_CAGR', 'STATEMENT_COST_BASIS_CAGR', 'STATEMENT_REPORTED_RETURN', 'RISK_CLASS_BENCHMARK'
    confidence TEXT NOT NULL CHECK (confidence IN ('HIGH','MEDIUM','LOW')),
    source_detail TEXT NOT NULL DEFAULT '',
    growth_evidence_json TEXT,
    computed_at_iso TEXT NOT NULL,
    PRIMARY KEY (account_id, security_id),
    FOREIGN KEY (security_id) REFERENCES securities(security_id)
);

CREATE INDEX IF NOT EXISTS idx_holding_forecasts_sec ON holding_forecasts_baseline(security_id);

