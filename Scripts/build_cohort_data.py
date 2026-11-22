#!/usr/bin/env python3
"""
CodeReview A3 & DA-C05 (Historical Cohort tab — 3-Tier Multi-Asset Specificity Cascade):
Build `Data/historical_cohorts.json` from verified 1928–2025 historical US capital-market data.

Bundled Tier-3 asset-class series (calendar years 1929–2025):
  - `sp_ret_pct`: S&P 500 price index (`^GSPC`) calendar-year return (excluding dividends).
  - `sp_total_ret_pct`: S&P 500 Total Return including reinvested dividends (Shiller / Damodaran NYU Stern).
  - `ust_10y_ret_pct`: 10-Year US Treasury Bond Total Return (coupon + duration price change; Damodaran / FRED).
  - `tbill_3m_pct`: 3-Month US Treasury Bill annual return / secondary-market yield (FRED TB3MS / Damodaran).
  - `gold_ret_pct`: Gold USD annual return (official Mint / London PM Fix / LBMA; World Gold Council / Damodaran).
  - `inflation_pct`: BLS CPI-U annual-average inflation rate (Minneapolis Fed CPI 1913–present table).
  - `single_stock_ret_pct`: Class-benchmark (35.0% vol) log-scaled equity sequence (used only when a single-stock
    holding lacks both Tier-1 trading history in that year and holding-specific realized volatility).

Pass `--offline` to deterministically regenerate `Data/historical_cohorts.json` without network requests.

System Message — `task-6262` check:
"""
from __future__ import annotations

import json
import math
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import HOLDINGS_ROOT  # noqa: E402
from valuation.market_data import _fetch_yahoo_chart  # noqa: E402

ROOT = HOLDINGS_ROOT
OUT = ROOT / "Data" / "historical_cohorts.json"
SINGLE_STOCK_CLASS_VOL = 35.0

# BLS CPI-U, annual average (1928-2025). Source: Minneapolis Fed CPI 1913- table.
CPI_ANNUAL = {
    1928: 17.2, 1929: 17.2, 1930: 16.7, 1931: 15.2, 1932: 13.6, 1933: 12.9, 1934: 13.4, 1935: 13.7, 1936: 13.9, 1937: 14.4,
    1938: 14.1, 1939: 13.9, 1940: 14.0, 1941: 14.7, 1942: 16.3, 1943: 17.3, 1944: 17.6, 1945: 18.0, 1946: 19.5, 1947: 22.3,
    1948: 24.0, 1949: 23.8, 1950: 24.1, 1951: 26.0, 1952: 26.6, 1953: 26.8, 1954: 26.9, 1955: 26.8, 1956: 27.2, 1957: 28.1,
    1958: 28.9, 1959: 29.2, 1960: 29.6, 1961: 29.9, 1962: 30.3, 1963: 30.6, 1964: 31.0, 1965: 31.5, 1966: 32.5, 1967: 33.4,
    1968: 34.8, 1969: 36.7, 1970: 38.8, 1971: 40.5, 1972: 41.8, 1973: 44.4, 1974: 49.3, 1975: 53.8, 1976: 56.9, 1977: 60.6,
    1978: 65.2, 1979: 72.6, 1980: 82.4, 1981: 90.9, 1982: 96.5, 1983: 99.6, 1984: 103.9, 1985: 107.6, 1986: 109.6, 1987: 113.6,
    1988: 118.3, 1989: 124.0, 1990: 130.7, 1991: 136.2, 1992: 140.3, 1993: 144.5, 1994: 148.2, 1995: 152.4, 1996: 156.9, 1997: 160.5,
    1998: 163.0, 1999: 166.6, 2000: 172.2, 2001: 177.1, 2002: 179.9, 2003: 184.0, 2004: 188.9, 2005: 195.3, 2006: 201.6, 2007: 207.3,
    2008: 215.3, 2009: 214.5, 2010: 218.1, 2011: 224.9, 2012: 229.6, 2013: 233.0, 2014: 236.7, 2015: 237.0, 2016: 240.0, 2017: 245.1,
    2018: 251.1, 2019: 255.7, 2020: 258.8, 2021: 271.0, 2022: 292.7, 2023: 304.7, 2024: 313.7, 2025: 321.9,
}

# S&P 500 Annual Dividend Yield % (Shiller / S&P Dow Jones Indices, 1929-2025) used to construct
# exact reinvested total return `((1 + price_ret) * (1 + div_yld / 100) - 1)` aligned with `sp_ret_pct`.
SP500_DIV_YIELD_PCT = {
    1929: 3.53, 1930: 4.65, 1931: 6.15, 1932: 7.43, 1933: 4.41, 1934: 4.11, 1935: 3.88, 1936: 4.35, 1937: 5.42, 1938: 4.32,
    1939: 4.50, 1940: 5.58, 1941: 6.82, 1942: 6.79, 1943: 5.10, 1944: 4.86, 1945: 4.17, 1946: 3.85, 1947: 4.93, 1948: 5.54,
    1949: 6.59, 1950: 6.57, 1951: 6.13, 1952: 5.80, 1953: 5.80, 1954: 4.95, 1955: 4.08, 1956: 4.09, 1957: 4.35, 1958: 3.97,
    1959: 3.23, 1960: 3.47, 1961: 2.98, 1962: 3.37, 1963: 3.17, 1964: 3.01, 1965: 3.00, 1966: 3.40, 1967: 3.20, 1968: 3.07,
    1969: 3.24, 1970: 3.83, 1971: 3.14, 1972: 2.84, 1973: 3.06, 1974: 4.47, 1975: 4.31, 1976: 3.77, 1977: 4.62, 1978: 5.28,
    1979: 5.47, 1980: 5.26, 1981: 5.20, 1982: 5.81, 1983: 4.40, 1984: 4.64, 1985: 4.25, 1986: 3.49, 1987: 3.08, 1988: 3.64,
    1989: 3.45, 1990: 3.61, 1991: 3.24, 1992: 2.99, 1993: 2.78, 1994: 2.82, 1995: 2.56, 1996: 2.19, 1997: 1.77, 1998: 1.49,
    1999: 1.25, 2000: 1.15, 2001: 1.32, 2002: 1.61, 2003: 1.77, 2004: 1.72, 2005: 1.83, 2006: 1.87, 2007: 1.86, 2008: 2.37,
    2009: 2.40, 2010: 1.98, 2011: 2.05, 2012: 2.24, 2013: 2.14, 2014: 1.95, 2015: 2.11, 2016: 2.13, 2017: 1.94, 2018: 1.90,
    2019: 1.93, 2020: 1.78, 2021: 1.38, 2022: 1.57, 2023: 1.54, 2024: 1.32, 2025: 1.26,
}

# 10-Year US Treasury Bond Annual Total Return % (Damodaran NYU Stern / FRED DGS10 constant-maturity total return, 1929-2025)
UST_10Y_TOTAL_RET_PCT = {
    1929: 4.20, 1930: 4.54, 1931: -2.56, 1932: 8.79, 1933: 1.86, 1934: 7.96, 1935: 4.47, 1936: 5.02, 1937: 1.38, 1938: 4.21,
    1939: 4.41, 1940: 5.40, 1941: -2.02, 1942: 2.29, 1943: 2.49, 1944: 2.58, 1945: 3.80, 1946: 3.13, 1947: 0.92, 1948: 1.95,
    1949: 4.66, 1950: 0.43, 1951: -0.30, 1952: 2.27, 1953: 4.14, 1954: 3.29, 1955: -1.34, 1956: -2.26, 1957: 6.80, 1958: -2.10,
    1959: -2.65, 1960: 11.64, 1961: 2.06, 1962: 5.69, 1963: 1.68, 1964: 3.73, 1965: 0.72, 1966: 2.91, 1967: -1.58, 1968: 3.27,
    1969: -5.01, 1970: 16.75, 1971: 9.79, 1972: 2.82, 1973: 3.66, 1974: 1.99, 1975: 3.61, 1976: 15.98, 1977: 1.29, 1978: -0.78,
    1979: 0.67, 1980: -2.99, 1981: 8.20, 1982: 32.81, 1983: 3.20, 1984: 13.73, 1985: 25.71, 1986: 24.28, 1987: -4.96, 1988: 8.22,
    1989: 17.69, 1990: 6.24, 1991: 15.00, 1992: 9.36, 1993: 14.21, 1994: -8.04, 1995: 23.48, 1996: 1.43, 1997: 9.94, 1998: 14.92,
    1999: -8.25, 2000: 16.66, 2001: 5.57, 2002: 15.12, 2003: 0.38, 2004: 4.49, 2005: 2.87, 2006: 1.96, 2007: 10.21, 2008: 20.10,
    2009: -11.12, 2010: 8.46, 2011: 16.04, 2012: 2.97, 2013: -9.10, 2014: 10.75, 2015: 1.28, 2016: 0.69, 2017: 2.80, 2018: -0.02,
    2019: 9.64, 2020: 11.33, 2021: -4.42, 2022: -17.83, 2023: 3.88, 2024: -1.64, 2025: 5.95,
}

# 3-Month US Treasury Bill Annual Return % (FRED TB3MS / Damodaran NYU Stern, 1929-2025)
TBILL_3M_RET_PCT = {
    1929: 4.75, 1930: 2.41, 1931: 1.07, 1932: 0.88, 1933: 0.52, 1934: 0.27, 1935: 0.17, 1936: 0.17, 1937: 0.45, 1938: 0.05,
    1939: 0.02, 1940: 0.01, 1941: 0.10, 1942: 0.32, 1943: 0.37, 1944: 0.38, 1945: 0.38, 1946: 0.38, 1947: 0.59, 1948: 1.04,
    1949: 1.10, 1950: 1.22, 1951: 1.55, 1952: 1.77, 1953: 1.93, 1954: 0.95, 1955: 1.75, 1956: 2.66, 1957: 3.27, 1958: 1.84,
    1959: 3.41, 1960: 2.93, 1961: 2.38, 1962: 2.78, 1963: 3.16, 1964: 3.55, 1965: 3.95, 1966: 4.88, 1967: 4.32, 1968: 5.34,
    1969: 6.68, 1970: 6.46, 1971: 4.35, 1972: 4.07, 1973: 7.04, 1974: 7.89, 1975: 5.84, 1976: 4.99, 1977: 5.27, 1978: 7.22,
    1979: 10.04, 1980: 11.51, 1981: 14.03, 1982: 10.69, 1983: 8.63, 1984: 9.58, 1985: 7.48, 1986: 5.98, 1987: 5.82, 1988: 6.69,
    1989: 8.12, 1990: 7.51, 1991: 5.42, 1992: 3.45, 1993: 3.02, 1994: 4.29, 1995: 5.51, 1996: 5.02, 1997: 5.07, 1998: 4.81,
    1999: 4.66, 2000: 5.85, 2001: 3.45, 2002: 1.62, 2003: 1.02, 2004: 1.38, 2005: 3.16, 2006: 4.73, 2007: 4.41, 2008: 1.48,
    2009: 0.16, 2010: 0.14, 2011: 0.06, 2012: 0.09, 2013: 0.06, 2014: 0.03, 2015: 0.05, 2016: 0.32, 2017: 0.93, 2018: 1.94,
    2019: 2.06, 2020: 0.37, 2021: 0.04, 2022: 2.02, 2023: 5.07, 2024: 4.97, 2025: 4.18,
}

# Gold / Real Asset Annual USD Return % (Official Mint / LBMA PM Fix / World Gold Council, 1929-2025)
GOLD_RET_PCT = {
    1929: 0.00, 1930: 0.00, 1931: 0.00, 1932: 0.00, 1933: 26.70, 1934: 33.40, 1935: 0.00, 1936: 0.00, 1937: 0.00, 1938: 0.00,
    1939: 0.00, 1940: 0.00, 1941: 0.00, 1942: 0.00, 1943: 0.00, 1944: 0.00, 1945: 0.00, 1946: 0.00, 1947: 0.00, 1948: 0.00,
    1949: 0.00, 1950: 0.00, 1951: 0.00, 1952: 0.00, 1953: 0.00, 1954: 0.00, 1955: 0.00, 1956: 0.00, 1957: 0.00, 1958: 0.00,
    1959: 0.00, 1960: 0.00, 1961: 0.00, 1962: 0.00, 1963: 0.00, 1964: 0.00, 1965: 0.00, 1966: 0.00, 1967: 0.00, 1968: 19.86,
    1969: -16.07, 1970: 6.20, 1971: 16.71, 1972: 49.02, 1973: 72.96, 1974: 66.07, 1975: -24.80, 1976: -4.10, 1977: 22.64, 1978: 37.01,
    1979: 126.55, 1980: 15.19, 1981: -32.60, 1982: 14.89, 1983: -16.30, 1984: -19.24, 1985: 5.83, 1986: 18.96, 1987: 24.46, 1988: -15.69,
    1989: -2.23, 1990: -3.69, 1991: -8.56, 1992: -5.73, 1993: 17.68, 1994: -2.17, 1995: 0.98, 1996: -4.55, 1997: -21.74, 1998: -0.83,
    1999: 0.85, 2000: -5.44, 2001: 2.46, 2002: 24.78, 2003: 19.37, 2004: 5.54, 2005: 17.92, 2006: 23.16, 2007: 30.98, 2008: 5.53,
    2009: 24.37, 2010: 29.52, 2011: 10.06, 2012: 7.14, 2013: -28.04, 2014: -1.72, 2015: -10.42, 2016: 8.56, 2017: 13.09, 2018: -1.56,
    2019: 18.31, 2020: 25.12, 2021: -3.64, 2022: -0.28, 2023: 13.10, 2024: 27.22, 2025: 38.40,
}


def _cached_sp_annual_returns() -> dict[int, float]:
    if not OUT.is_file():
        return {}
    raw = json.loads(OUT.read_text(encoding="utf-8"))
    return {
        int(row["year"]): float(row["sp_ret_pct"])
        for row in (raw.get("years") or [])
        if isinstance(row, dict) and "year" in row and "sp_ret_pct" in row
    }


def sp_annual_returns(*, offline: bool = False) -> dict[int, float]:
    """Calendar-year % return of ^GSPC from its last trading day each year (or cached series when offline)."""
    if offline:
        cached = _cached_sp_annual_returns()
        if cached:
            return cached
    try:
        _, rows, _ = _fetch_yahoo_chart("^GSPC", date(1900, 1, 1), date.today(), interval="1d")
    except Exception:
        cached = _cached_sp_annual_returns()
        if cached:
            return cached
        raise
    year_end: dict[int, float] = {}
    for iso, close, _adj in rows:
        y = int(iso[:4])
        year_end[y] = float(close)
    out: dict[int, float] = {}
    for y in sorted(year_end):
        if (y - 1) in year_end and year_end[y - 1] > 0:
            out[y] = (year_end[y] / year_end[y - 1] - 1.0) * 100.0
    return out


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    offline = "--offline" in args
    existing_gen = None
    if offline and OUT.is_file():
        try:
            existing_gen = json.loads(OUT.read_text(encoding="utf-8")).get("generated_at")
        except Exception:
            existing_gen = None

    sp = sp_annual_returns(offline=offline)
    infl: dict[int, float] = {}
    for y in sorted(CPI_ANNUAL):
        if (y - 1) in CPI_ANNUAL and CPI_ANNUAL[y - 1] > 0:
            infl[y] = (CPI_ANNUAL[y] / CPI_ANNUAL[y - 1] - 1.0) * 100.0

    years = sorted(set(sp) & set(infl))
    sp_total: dict[int, float] = {}
    for y in years:
        div_y = SP500_DIV_YIELD_PCT.get(y, 2.0)
        sp_total[y] = ((1.0 + sp[y] / 100.0) * (1.0 + div_y / 100.0) - 1.0) * 100.0

    sp_tot_vals = [sp_total[y] for y in years]
    n = len(sp_tot_vals)
    mean_sp_price = sum(sp[y] for y in years) / n
    mean_sp_tot = sum(sp_tot_vals) / n
    sd_sp_tot = math.sqrt(sum((x - mean_sp_tot) ** 2 for x in sp_tot_vals) / (n - 1))

    # Single-stock fallback proxy: amplify total-return log deviations to single-stock class vol (35.0%)
    k = SINGLE_STOCK_CLASS_VOL / sd_sp_tot if sd_sp_tot > 0 else 1.0
    sp_tot_log = {y: math.log(1.0 + sp_total[y] / 100.0) for y in years}
    mean_log_tot = sum(sp_tot_log.values()) / n

    rows_out = []
    for y in years:
        ss_log = mean_log_tot + k * (sp_tot_log[y] - mean_log_tot)
        ss_ret = (math.exp(ss_log) - 1.0) * 100.0
        rows_out.append({
            "year": y,
            "sp_ret_pct": round(sp[y], 3),
            "sp_total_ret_pct": round(sp_total[y], 3),
            "ust_10y_ret_pct": round(UST_10Y_TOTAL_RET_PCT.get(y, 4.0), 3),
            "tbill_3m_pct": round(TBILL_3M_RET_PCT.get(y, 3.0), 3),
            "gold_ret_pct": round(GOLD_RET_PCT.get(y, 0.0), 3),
            "single_stock_ret_pct": round(ss_ret, 3),
            "inflation_pct": round(infl[y], 3),
        })

    data = {
        "generated_at": existing_gen or date.today().isoformat(),
        "source": (
            "S&P 500 Total Return (price + reinvested dividends, Shiller/Yahoo ^GSPC), "
            "10-Year US Treasury Total Return & 3-Month T-Bill Yield (Damodaran NYU Stern / FRED), "
            "Gold USD Return (LBMA / World Gold Council), and BLS CPI-U annual average (1929–2025)."
        ),
        "note": (
            "3-Tier Historical Specificity Cascade: Tier 1 uses exact ticker / constituent calendar-year adj_close "
            "total return where trading history exists; Tier 2 applies contractual fixed rates (MYGA/CD) and structural "
            "RILA/FIA cap-buffer formulas; Tier 3 maps each risk class to its historical asset-class series "
            "(S&P 500 Total Return, 10Y US Treasury, 3M T-Bill, Gold) with holding-specific realized beta/volatility."
        ),
        "sp_annual_vol_pct": round(sd_sp_tot, 2),
        "sp_annual_mean_pct": round(mean_sp_price, 2),
        "sp_total_annual_mean_pct": round(mean_sp_tot, 2),
        "single_stock_amplification_k": round(k, 3),
        "first_year": years[0],
        "last_year": years[-1],
        "years": rows_out,
    }
    OUT.write_text(json.dumps(data, indent=0) + "\n", encoding="utf-8")
    print(
        f"Wrote {OUT} — {len(rows_out)} years ({years[0]}-{years[-1]}), "
        f"S&P Total Return mean {mean_sp_tot:.2f}% (price {mean_sp_price:.2f}%), vol {sd_sp_tot:.1f}%, k={k:.2f}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
