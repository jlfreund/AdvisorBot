#!/usr/bin/env python3
"""
Unit tests for friendly market-index, benchmark, commodity, crypto, and share-class
symbol lookup resolution (`resolve_symbol_alias` in `valuation/market_data.py`).
"""

from __future__ import annotations

import pytest

from valuation.market_data import resolve_symbol_alias


@pytest.mark.feature("payload")
def test_symbol_lookup__resolves_major_indices_and_punctuation_variants():
    """Common market index names and bare index codes resolve to Yahoo Finance `^` tickers."""
    assert resolve_symbol_alias("nasdaq") == "^IXIC"
    assert resolve_symbol_alias("NASDAQ Composite") == "^IXIC"
    assert resolve_symbol_alias("ixic") == "^IXIC"
    assert resolve_symbol_alias("nasdaq-100") == "^NDX"
    assert resolve_symbol_alias("ndx") == "^NDX"
    assert resolve_symbol_alias("S&P 500") == "^GSPC"
    assert resolve_symbol_alias("sp500") == "^GSPC"
    assert resolve_symbol_alias("spx") == "^GSPC"
    assert resolve_symbol_alias("Dow Jones") == "^DJI"
    assert resolve_symbol_alias("djia") == "^DJI"
    assert resolve_symbol_alias("russell 2000") == "^RUT"
    assert resolve_symbol_alias("vix") == "^VIX"
    assert resolve_symbol_alias("10y treasury") == "^TNX"


@pytest.mark.feature("payload")
def test_symbol_lookup__preserves_real_stock_tickers_and_honors_custom_aliases():
    """Real NYSE/NASDAQ stock tickers are never shadowed, and portfolio `ticker_aliases` take precedence."""
    assert resolve_symbol_alias("DOW") == "DOW"
    assert resolve_symbol_alias("GOLD") == "GOLD"
    assert resolve_symbol_alias("COMP") == "COMP"
    assert resolve_symbol_alias("BTC") == "BTC"
    assert resolve_symbol_alias("bitcoin") == "BTC-USD"
    assert resolve_symbol_alias("brk.b") == "BRK-B"
    assert resolve_symbol_alias("MY_TECH_IDX", custom_aliases={"my_tech_idx": "^NDX"}) == "^NDX"
