# Fixture portfolio: `p3_retiree_annuities`

<!-- GENERATED FILE. Edit the spec and run `make fixtures`; do not edit by hand. -->
<!-- spec: fixtures/specs/p3_retiree_annuities.json  sha: f26c4a1467b786b8 -->

**Retired couple with three annuity types, two-state municipal bonds and a reverse split**

The insurance-and-fixed-income fixture. Three contracts from three invented carriers cover the fixed, indexed and buffered tax models, so no carrier-name branch in the code can match and the asset class has to carry the meaning. Municipal bonds are split across two states, which is the only way to see whether a tax-exemption explanation is state-aware. Both investors have full Social Security earnings records supplied as XML rather than as summary fields, and one fund goes through a reverse split mid-history.

## At a glance

- **Household**: Brennan-Castillo Household (Oregon)
- **Pinned `as_of`**: 2026-11-16 (payloads and snapshots are built at this date)
- **Primary equity symbol**: _none configured_
- **Tax rates**: LTCG 0.288, ordinary 0.469, state 0.099
- **Statements**: 24 canonical JSON, 2 SSA XML
- **Scenarios**: 3
- **Priced symbols**: _none_

## Household

| Person | Role | Born | Age at as_of | Social Security |
| --- | --- | --- | --- | --- |
| Maeve Brennan | investor, RMD owner | 1952-03-09 | 74 | cap_from_2012 1978-2020, claim at 70, from XML |
| Rafael Castillo | investor | 1955-11-27 | 70 | gap_years 1980-2022, claim at 70, from XML |

## Accounts

| Account | Institution | Type | Tax status | Statements |
| --- | --- | --- | --- | --- |
| `CB-5100` | Cedarline Wealth | TRAD_IRA | TRAD_IRA | 2026-03 to 2026-10 |
| `CB-5101` | Cedarline Wealth | BROKERAGE | TAXABLE | 2026-03 to 2026-10 |
| `CB-5102` | Cedarline Wealth | BROKERAGE | TAXABLE | 2026-03 to 2026-10 |

## Positions

| Account | Holding | Kind | Detail |
| --- | --- | --- | --- |
| `CB-5100` | Cedarline Growth Fund Class R | contract_fund | $418,000 |
| `CB-5100` | Cedarline IRA Cash Reserve | cash | opens at $38,500, 3.6% APY |
| `CB-5101` | Solstice Life 5-Year Guaranteed Annuity | annuity | $250,000 premium, issued 2023-06-01, surrender-free 2028-06-01 |
| `CB-5101` | Granite Peak Assurance Indexed Annuity 7 | annuity | $300,000 premium, issued 2022-10-07, surrender-free 2029-10-07 |
| `CB-5101` | Northwind Variable Buffered Annuity 6 | annuity | $200,000 premium, issued 2024-01-31, surrender-free 2030-01-31 |
| `CB-5101` | Cedarline Taxable Cash Sweep | cash | opens at $41,250, 3.6% APY |
| `CB-5102` | Oregon State Transportation Revenue 2031 | muni | $100,000 par, 4.1% coupon, OR issuer |
| `CB-5102` | Portland Metro Water District 2034 | muni | $75,000 par, 3.9% coupon, OR issuer |
| `CB-5102` | Washington State Housing Finance 2032 | muni | $60,000 par, 4.25% coupon, WA issuer |
| `CB-5102` | Spokane County School District 2036 | muni | $45,000 par, 4.4% coupon, WA issuer |

## Scenarios

- **Default** (protected): `simAnnualBurn`, `simIncludeSSN`, `simOrderPreset`
- **Surrender Early**: `simScheduledActions`
- **IRA To Roth**: `simHypotheticalAssets`

## Privacy settings

- **AI shield mode**: `identity_and_values`
- **Demo mode**: disabled, scale 0.5
- **MCP server**: disabled
- **Extra synonyms**: 3

## What this fixture exercises

- `annuities.fixed_myga`
- `annuities.indexed_fia`
- `annuities.buffered_rila_segments`
- `annuities.surrender_dates`
- `annuities.lifo_ordering`
- `munis.state_aware_exemption`
- `ingest.split_adjustment`
- `social_security.from_xml`
- `social_security.two_claim_ages`
- `rmd.in_progress`
- `payload.no_stock_plan`
- `asset_classes.carrier_agnostic`

## Invariants asserted by tests

| Path | Expectation | Why |
| --- | --- | --- |
| `grant_symbols` | equals `[]` | No equity plan: no grant bucket and no placeholder grant (LL-22). |
| `sim_asset_count` | equals `6` | Three annuity contracts, one fund, one muni sleeve, one merged cash-sweep bucket. |

## Notes

The two Washington issues are not exempt from Oregon tax, so a tax-rule sentence that claims blanket state exemption is wrong on this fixture. The Cedarline fund carries a 1-for-4 reverse split effective 2026-05-15, which emits a PRE_SPLIT_SHARES adjustment on that month's statement.

## Using it by hand

```bash
# Open this fixture in the real app (no effect on your own portfolios)
ROOT=$(mktemp -d)
PYTHONPATH=Scripts python3 -m tests.generate --root "$ROOT" --only p3_retiree_annuities
ADVISORBOT_ROOT="$ROOT" python3 Scripts/holdings.py refresh
ADVISORBOT_ROOT="$ROOT" python3 Scripts/advisorbot_server.py --port 8799
```
