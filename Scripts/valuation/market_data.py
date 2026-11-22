#!/usr/bin/env python3
"""Unauthenticated Yahoo Finance historical & live quote fetcher with SQLite caching."""

from __future__ import annotations

import concurrent.futures
import json
import urllib.parse
import urllib.request
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from db.database import PortfolioDB, utc_now_iso

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"


def to_friday(d: date) -> date:
    """Maps any date in a calendar week (Sun..Sat or Mon..Sun) to that week's Friday."""
    # Yahoo 1wk timestamps typically land on Sunday/Monday at start of week; map Mon..Sun week to its Friday
    # Specifically, if weekday() == 6 (Sunday), next Friday is +5 days; Mon(0)->+4, Tue(1)->+3, Fri(4)->0, Sat(5)->-1
    if d.weekday() == 6:
        return d + timedelta(days=5)
    return d + timedelta(days=(4 - d.weekday()))


def _fetch_yahoo_chart(
    quote_symbol: str,
    start_date: date,
    end_date: date,
    interval: str = "1wk",
) -> tuple[str, list[tuple[str, float, float | None]], float | None]:
    """
    Fetches split-adjusted closes from Yahoo Finance for `[start_date .. end_date]`.
    Returns `(quote_symbol, [(friday_iso, close_price, adj_close), ...], regular_market_price)`.
    """
    p1 = int(datetime.combine(start_date - timedelta(days=7), time.min, tzinfo=timezone.utc).timestamp())
    p2 = int(datetime.combine(end_date + timedelta(days=2), time.max, tzinfo=timezone.utc).timestamp())
    enc_sym = urllib.parse.quote(quote_symbol)
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{enc_sym}?interval={interval}&period1={p1}&period2={p2}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})

    with urllib.request.urlopen(req, timeout=12) as resp:
        payload = json.loads(resp.read().decode("utf-8"))

    result = payload["chart"]["result"][0]
    meta = result.get("meta", {})
    live_price = meta.get("regularMarketPrice")

    timestamps = result.get("timestamp") or []
    quotes = (result.get("indicators", {}).get("quote") or [{}])[0]
    closes = quotes.get("close") or []
    adj_list = ((result.get("indicators", {}).get("adjclose") or [{}])[0]).get("adjclose") or []

    bars: dict[str, tuple[float, float | None]] = {}
    for idx, ts in enumerate(timestamps):
        cl = closes[idx] if idx < len(closes) else None
        if cl is None:
            continue
        adj = adj_list[idx] if idx < len(adj_list) else None
        raw_dt = datetime.fromtimestamp(ts, tz=timezone.utc).date()
        fri_dt = to_friday(raw_dt) if interval == "1wk" else raw_dt
        # Do not map an incomplete current-week candle into a future Friday beyond `end_date`
        if fri_dt > end_date:
            fri_dt = end_date
        bars[fri_dt.isoformat()] = (float(cl), float(adj) if adj is not None else None)

    rows = [(d_iso, vals[0], vals[1]) for d_iso, vals in sorted(bars.items())]
    return quote_symbol, rows, (float(live_price) if live_price is not None else None)


def sync_market_prices(
    db: PortfolioDB,
    symbols: list[str],
    start_date: date,
    end_date: date,
    force_full: bool = False,
    max_workers: int = 20,
) -> dict[str, Any]:
    """
    Ensures `market_prices` in SQLite is populated for all `symbols` (+ benchmark `SPY`)
    across `[start_date .. end_date]`, plus records live price at `end_date`.
    """
    unique_syms = sorted(set(symbols) | {"SPY"})

    # Determine which symbols need full historical range vs incremental refresh
    to_fetch: list[tuple[str, date]] = []
    for sym in unique_syms:
        if force_full:
            to_fetch.append((sym, start_date))
            continue
        row = db.conn.execute(
            "SELECT MIN(price_date) AS min_d, MAX(price_date) AS max_d FROM market_prices WHERE quote_symbol = ?",
            (sym,),
        ).fetchone()
        min_d = row["min_d"] if row else None
        max_d = row["max_d"] if row else None
        if min_d is None or max_d is None:
            to_fetch.append((sym, start_date))
        else:
            # If requested start_date is earlier than cached min_d (by >10 days), backfill from start_date
            if (date.fromisoformat(min_d) - start_date).days > 10:
                to_fetch.append((sym, start_date))
            elif max_d < end_date.isoformat():
                # Incremental catch-up from last cached date minus 7 days
                inc_start = date.fromisoformat(max_d) - timedelta(days=7)
                to_fetch.append((sym, inc_start))

    if not to_fetch:
        return {"symbols_queried": 0, "bars_upserted": 0, "errors": []}

    now_iso = utc_now_iso()
    bars_upserted = 0
    errors: list[str] = []

    def worker(item: tuple[str, date]):
        sym, s_dt = item
        return _fetch_yahoo_chart(sym, s_dt, end_date, interval="1wk")

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(worker, item): item[0] for item in to_fetch}
        for fut in concurrent.futures.as_completed(futures):
            sym = futures[fut]
            try:
                qsym, rows, live_p = fut.result()
                for d_iso, cl, adj in rows:
                    db.conn.execute(
                        """
                        INSERT INTO market_prices (quote_symbol, price_date, close_price, adj_close, fetched_at_iso)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(quote_symbol, price_date) DO UPDATE SET
                            close_price = excluded.close_price,
                            adj_close = COALESCE(excluded.adj_close, market_prices.adj_close),
                            fetched_at_iso = excluded.fetched_at_iso
                        """,
                        (qsym, d_iso, cl, adj, now_iso),
                    )
                    bars_upserted += 1
                if live_p is not None:
                    db.conn.execute(
                        """
                        INSERT INTO market_prices (quote_symbol, price_date, close_price, adj_close, fetched_at_iso)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(quote_symbol, price_date) DO UPDATE SET
                            close_price = excluded.close_price,
                            fetched_at_iso = excluded.fetched_at_iso
                        """,
                        (qsym, end_date.isoformat(), live_p, live_p, now_iso),
                    )
                    bars_upserted += 1
            except Exception as exc:
                errors.append(f"{sym}: {exc}")

    db.conn.commit()
    return {
        "symbols_queried": len(to_fetch),
        "bars_upserted": bars_upserted,
        "errors": errors,
    }


def sync_exact_dates_for_symbol(
    db: PortfolioDB,
    quote_symbol: str,
    iso_dates: list[str],
) -> None:
    """Fetches daily split-adjusted closes around specific dates (e.g. GSU grant dates) if missing."""
    missing = []
    for d_iso in sorted(set(iso_dates)):
        if not d_iso:
            continue
        d_dt = date.fromisoformat(d_iso)
        w_start = (d_dt - timedelta(days=5)).isoformat()
        row = db.conn.execute(
            "SELECT 1 FROM market_prices WHERE quote_symbol = ? AND price_date BETWEEN ? AND ? LIMIT 1",
            (quote_symbol, w_start, d_iso),
        ).fetchone()
        if row is None:
            missing.append(d_dt)

    if not missing:
        return

    now_iso = utc_now_iso()
    for target_dt in missing:
        try:
            _sym, rows, _live = _fetch_yahoo_chart(
                quote_symbol,
                target_dt - timedelta(days=5),
                target_dt + timedelta(days=2),
                interval="1d",
            )
            for d_iso, cl, adj in rows:
                db.conn.execute(
                    """
                    INSERT INTO market_prices (quote_symbol, price_date, close_price, adj_close, fetched_at_iso)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(quote_symbol, price_date) DO UPDATE SET
                        close_price = excluded.close_price,
                        adj_close = COALESCE(excluded.adj_close, market_prices.adj_close),
                        fetched_at_iso = excluded.fetched_at_iso
                    """,
                    (quote_symbol, d_iso, cl, adj, now_iso),
                )
        except Exception:
            pass
    db.conn.commit()


# Built-in shorthand lookup dictionary mapping human-friendly index, benchmark, commodity,
# crypto, and share-class names to canonical Yahoo Finance symbols.
# DESIGN & PORTABILITY INVARIANTS:
#   1. Never shadow active NYSE/NASDAQ stock or ETF tickers (e.g. keep `DOW`, `GOLD`, `COMP`,
#      `BTC`, `ETH`, `SOL` unmapped so real listed securities resolve to themselves).
#   2. Keep completely household-neutral (`LL-01`..`LL-33` — zero household-specific terms).
COMMON_SYMBOL_LOOKUP: dict[str, str] = {
    # US Market Indices
    "NASDAQ": "^IXIC",
    "NASDAQ COMPOSITE": "^IXIC",
    "NASDAQ INDEX": "^IXIC",
    "IXIC": "^IXIC",
    "NASDAQ 100": "^NDX",
    "NASDAQ100": "^NDX",
    "NAS100": "^NDX",
    "NDX": "^NDX",
    "S&P": "^GSPC",
    "S&P 500": "^GSPC",
    "S&P500": "^GSPC",
    "SP 500": "^GSPC",
    "SP500": "^GSPC",
    "SPX": "^GSPC",
    "GSPC": "^GSPC",
    "DOW JONES": "^DJI",
    "DOW 30": "^DJI",
    "DJIA": "^DJI",
    "DJI": "^DJI",
    "INDU": "^DJI",
    "RUSSELL 2000": "^RUT",
    "RUSSELL2000": "^RUT",
    "RUS2000": "^RUT",
    "RUT": "^RUT",
    "RUSSELL 1000": "^RUI",
    "RUSSELL 3000": "^RUA",
    "WILSHIRE 5000": "^W5000",
    "NYSE COMPOSITE": "^NYA",
    "VIX": "^VIX",
    "VOLATILITY INDEX": "^VIX",
    "PHLX SEMICONDUCTOR": "^SOX",
    # International Indices
    "FTSE": "^FTSE",
    "FTSE 100": "^FTSE",
    "UK100": "^FTSE",
    "DAX": "^GDAXI",
    "GDAXI": "^GDAXI",
    "GER40": "^GDAXI",
    "CAC 40": "^FCHI",
    "FCHI": "^FCHI",
    "EURO STOXX": "^STOXX50E",
    "EURO STOXX 50": "^STOXX50E",
    "STOXX50": "^STOXX50E",
    "STOXX50E": "^STOXX50E",
    "NIKKEI": "^N225",
    "NIKKEI 225": "^N225",
    "N225": "^N225",
    "HANG SENG": "^HSI",
    "SHANGHAI COMPOSITE": "000001.SS",
    "S&P TSX": "^GSPTSE",
    "GSPTSE": "^GSPTSE",
    "ASX 200": "^AXJO",
    "AXJO": "^AXJO",
    "KOSPI": "^KS11",
    "SENSEX": "^BSESN",
    "NIFTY 50": "^NSEI",
    # US Treasury Yields & Currency Benchmarks
    "13W TREASURY": "^IRX",
    "3M TREASURY": "^IRX",
    "IRX": "^IRX",
    "5Y TREASURY": "^FVX",
    "US5Y": "^FVX",
    "FVX": "^FVX",
    "10Y TREASURY": "^TNX",
    "10YR TREASURY": "^TNX",
    "US10Y": "^TNX",
    "TNX": "^TNX",
    "30Y TREASURY": "^TYX",
    "30YR TREASURY": "^TYX",
    "US30Y": "^TYX",
    "TYX": "^TYX",
    "DXY": "DX-Y.NYB",
    "DOLLAR INDEX": "DX-Y.NYB",
    "USD INDEX": "DX-Y.NYB",
    # Commodities (Futures)
    "GOLD FUTURES": "GC=F",
    "GOLD SPOT": "GC=F",
    "XAUUSD": "GC=F",
    "SILVER FUTURES": "SI=F",
    "SILVER SPOT": "SI=F",
    "XAGUSD": "SI=F",
    "CRUDE OIL": "CL=F",
    "WTI": "CL=F",
    "WTI OIL": "CL=F",
    "BRENT OIL": "BZ=F",
    "NATURAL GAS": "NG=F",
    "NATGAS": "NG=F",
    # Cryptocurrencies (Yahoo USD pairs)
    "BITCOIN": "BTC-USD",
    "BTCUSD": "BTC-USD",
    "BTC/USD": "BTC-USD",
    "ETHEREUM": "ETH-USD",
    "ETHER": "ETH-USD",
    "ETHUSD": "ETH-USD",
    "ETH/USD": "ETH-USD",
    "SOLANA": "SOL-USD",
    "SOLUSD": "SOL-USD",
    "SOL/USD": "SOL-USD",
    "DOGECOIN": "DOGE-USD",
    "DOGEUSD": "DOGE-USD",
    "RIPPLE": "XRP-USD",
    "XRPUSD": "XRP-USD",
    "CARDANO": "ADA-USD",
    # Share-class & common issuer name shorthands
    "BERKSHIRE": "BRK-B",
    "BERKSHIRE HATHAWAY": "BRK-B",
    "BRK/B": "BRK-B",
    "BRK.B": "BRK-B",
    "BRKB": "BRK-B",
    "BRK/A": "BRK-A",
    "BRK.A": "BRK-A",
    "BRKA": "BRK-A",
    "APPLE": "AAPL",
    "MICROSOFT": "MSFT",
    "NVIDIA": "NVDA",
    "AMAZON": "AMZN",
    "META PLATFORMS": "META",
    "TESLA": "TSLA",
    "BROADCOM": "AVGO",
    "NETFLIX": "NFLX",
    "COSTCO": "COST",
    "WALMART": "WMT",
    "ELI LILLY": "LLY",
    "MASTERCARD": "MA",
    "VISA": "V",
}


def _normalize_symbol_lookup_key(raw: str) -> str:
    """Collapses repeated whitespace, underscores, and hyphens between words while preserving `&`, `/`, `.`, `^`, `=`."""
    import re as _re

    s = (raw or "").strip().upper()
    if not s:
        return ""
    s = _re.sub(r"[\s_\-]+", " ", s).strip()
    return s


def resolve_symbol_alias(symbol: str, custom_aliases: dict[str, str] | None = None) -> str:
    """Resolves user-entered index/company shorthands or portfolio `ticker_aliases` to Yahoo Finance tickers."""
    raw = (symbol or "").strip().upper()
    if not raw:
        return ""
    norm = _normalize_symbol_lookup_key(raw)
    no_amp = norm.replace("&", "").replace("  ", " ").strip()

    if isinstance(custom_aliases, dict) and custom_aliases:
        up_custom = {_normalize_symbol_lookup_key(str(k)): str(v).strip().upper() for k, v in custom_aliases.items() if k and v}
        raw_custom = {str(k).strip().upper(): str(v).strip().upper() for k, v in custom_aliases.items() if k and v}
        if raw in raw_custom:
            return raw_custom[raw]
        if norm in up_custom:
            return up_custom[norm]

    for key_cand in (raw, norm, no_amp, no_amp.replace(" ", "")):
        if key_cand in COMMON_SYMBOL_LOOKUP:
            return COMMON_SYMBOL_LOOKUP[key_cand]
    return raw


def fetch_quote_snapshot(symbol: str = "", *, custom_aliases: dict[str, str] | None = None) -> dict[str, Any]:
    """Fetches the latest live market price and daily change for a ticker from Yahoo Finance.

    Supports:
      - Friendly index/asset shorthands via `COMMON_SYMBOL_LOOKUP` (e.g. `NASDAQ` -> `^IXIC`, `SP500` -> `^GSPC`)
      - Standard US equities/ETFs (e.g. AAPL, NVDA, SPY, VOO, QQQ)
      - Mutual funds (e.g. VFIAX, FXAIX, VTSAX)
      - Bond ETFs & funds (e.g. BND, TLT, AGG, BNDX)
      - US share classes (e.g. BRK.B or BRK-B)
      - Foreign exchanges using Yahoo's exchange suffixes (e.g. VWRA.L, SAP.DE, 7203.T, RY.TO)
      - Market indices and crypto (e.g. ^IXIC, ^GSPC, ^DJI, BTC-USD)
    """
    sym = (symbol or "").strip().upper()
    if not sym:
        return {"ok": False, "error": "No symbol specified"}
    canonical = resolve_symbol_alias(sym, custom_aliases=custom_aliases)
    candidates: list[str] = []
    for base_s in (canonical, sym):
        if base_s and base_s not in candidates:
            candidates.append(base_s)
        if "." in base_s:
            dash_variant = base_s.replace(".", "-")
            if dash_variant not in candidates:
                parts = base_s.split(".")
                # If 1-letter after dot on a letter-ticker (e.g. BRK.B), try hyphenated first; otherwise try dot first
                if len(parts) == 2 and len(parts[1]) == 1 and parts[1].isalpha() and not parts[0].isdigit():
                    idx_pos = candidates.index(base_s)
                    candidates.insert(idx_pos, dash_variant)
                else:
                    candidates.append(dash_variant)
    # If user typed a bare index code without `^` (e.g. `HSI`, `AXJO`) that isn't a listed stock, try `^<sym>` last
    if sym.isalnum() and 2 <= len(sym) <= 8 and f"^{sym}" not in candidates:
        candidates.append(f"^{sym}")

    last_error = f"Symbol '{sym}' not found on Yahoo Finance"
    for cand in candidates:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(cand)}?interval=1m&range=1d"
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            res_list = payload.get("chart", {}).get("result") or []
            if not res_list:
                continue
            meta = res_list[0].get("meta", {})
            price = float(meta.get("regularMarketPrice") or 0.0)
            prev_close = float(meta.get("chartPreviousClose") or meta.get("previousClose") or 0.0)
            short_name = str(meta.get("shortName") or meta.get("symbol") or cand)
            mkt_time = meta.get("regularMarketTime")
            if price <= 0.001:
                continue
            change = (price - prev_close) if prev_close > 0 else 0.0
            change_pct = ((price - prev_close) / prev_close * 100.0) if prev_close > 0 else 0.0
            dt = datetime.fromtimestamp(int(mkt_time)) if mkt_time else datetime.now()
            ts_str = dt.strftime("%b %d, %Y %I:%M %p").replace(" 0", " ")
            return {
                "ok": True,
                "symbol": cand,
                "input_symbol": sym,
                "resolved_symbol": cand,
                "name": short_name,
                "price": round(price, 2),
                "prev_close": round(prev_close, 2) if prev_close > 0 else None,
                "change": round(change, 2),
                "change_pct": round(change_pct, 2),
                "market_time": mkt_time,
                "timestamp_str": ts_str,
            }
        except Exception as exc:
            last_error = str(exc)
            continue
    return {"ok": False, "error": last_error}
