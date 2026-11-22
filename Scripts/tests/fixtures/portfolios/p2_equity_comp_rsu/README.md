# Fixture portfolio: `p2_equity_comp_rsu`

<!-- GENERATED FILE. Edit the spec and run `make fixtures`; do not edit by hand. -->
<!-- spec: fixtures/specs/p2_equity_comp_rsu.json  sha: 22d3d59ce37a565f -->

**Couple with RSUs at an unfamiliar stock-plan custodian, plus Traditional and Roth IRAs**

The equity-compensation fixture, built so that nothing about it matches the household the grant code was written against: the awards are plain RSUs rather than the award type the grant code assumes, the plan is custodied at a third custodian, the employer ticker is not the original one, and the household is in a flat-tax state. Every grant, wrapper and RMD code path runs here.

## At a glance

- **Household**: Okafor-Lindqvist Household (Colorado (flat 4.4%))
- **Pinned `as_of`**: 2026-11-16 (payloads and snapshots are built at this date)
- **Primary equity symbol**: `VNTG`
- **Tax rates**: LTCG 0.278, ordinary 0.414, state 0.044
- **Statements**: 32 canonical JSON, 1 grant file(s)
- **Scenarios**: 3
- **Priced symbols**: `VBTLX`, `VNTG`, `VOO`, `VTSAX`

## Household

| Person | Role | Born | Age at as_of | Social Security |
| --- | --- | --- | --- | --- |
| Amara Okafor | investor, RMD owner | 1979-02-11 | 47 | cap_from_2012 2001-2026, claim at 67, from summary |
| Elias Lindqvist | investor | 1981-07-30 | 45 | mid_rising 2004-2026, claim at 70, from summary |
| Tobias Okafor | beneficiary | 2012-05-03 | 14 | milestone age 25 |

## Accounts

| Account | Institution | Type | Tax status | Statements |
| --- | --- | --- | --- | --- |
| `MES-3301` | Meridian Equity Services | STOCK_PLAN | TAXABLE | none |
| `HB-7101` | Harbor Brokerage | BROKERAGE | TAXABLE | 2026-03 to 2026-10 |
| `HB-7102` | Harbor Brokerage | TRAD_IRA | TRAD_IRA | 2026-03 to 2026-10 |
| `HB-7103` | Harbor Brokerage | ROTH_IRA | ROTH_IRA | 2026-03 to 2026-10 |
| `NB-2210` | Northbank Retirement | 401K | TRAD_IRA | 2026-03 to 2026-10 |

## Positions

| Account | Holding | Kind | Detail |
| --- | --- | --- | --- |
| `HB-7101` | Vantage Robotics, Inc. Class A | ticker | 1,000 sh across 1 lot(s), symbol `VNTG` |
| `HB-7101` | Vanguard S&P 500 ETF | ticker | 400 sh across 1 lot(s), symbol `VOO` |
| `HB-7101` | Harbor Cash Sweep | cash | opens at $84,200, 3.9% APY |
| `HB-7102` | Vanguard Total Bond Market Index Admiral | ticker | 3,000 sh across 1 lot(s), symbol `VBTLX` |
| `HB-7103` | Vanguard Total Stock Market Index Admiral | ticker | 900 sh across 1 lot(s), symbol `VTSAX` |
| `NB-2210` | Northbank Target Date 2045 Fund | contract_fund | $312,000 |

### Equity awards at `MES-3301` (Meridian Equity Services)

- **Symbol / company**: `VNTG` (Vantage Robotics, Inc.), award type **RSU**
- **Withholding at release**: 38.0%
- **`G-2022-A`**: 4,800 shares granted 2022-08-15, quarterly from 2022-11-15 (16 vests, last 2026-08-15)
- **`G-2024-B`**: 3,200 shares granted 2024-05-15, quarterly from 2025-05-15 (12 vests, last 2028-02-15)
- **`G-2026-C`**: 2,400 shares granted 2026-03-15, monthly from 2026-04-15 (36 vests, last 2029-03-15)
- **In-kind transfer**: 1,000 shares to `HB-7101` on 2024-11-15 (carryover basis)

## Scenarios

- **Default** (protected): `pastReleaseValMode`, `simAnnualBurn`, `simIncludeUnvested`
- **Diversify At Vest**: `simScheduledActions`
- **Roth Conversion**: `simHypotheticalAssets`

## Privacy settings

- **AI shield mode**: `identity_aliased`
- **Demo mode**: disabled, scale 0.35
- **MCP server**: disabled
- **Extra synonyms**: 2

## What this fixture exercises

- `grants.rsu_award_type`
- `grants.vest_schedule`
- `grants.releases_history`
- `grants.net_retention_from_history`
- `grants.basis_split`
- `grants.in_kind_transfer`
- `grants.non_ms_custodian`
- `wrappers.trad_ira`
- `wrappers.roth`
- `wrappers.401k`
- `rmd.onset`
- `primary_symbol.non_default`
- `solver.primary_growth`
- `scenarios.hypothetical_rollover`
- `beneficiary.milestone`

## Invariants asserted by tests

| Path | Expectation | Why |
| --- | --- | --- |
| `grant_symbols` | equals `['VNTG']` | The grant ticker comes from the grants file, not from a default symbol (LL-01). |
| `stock_grants.default_symbol` | equals `'VNTG'` | Primary symbol is honoured end to end. |
| `project.primary_equity_symbol` | equals `'VNTG'` | Config value survives into the payload. |

## Notes

The in-kind transfer deliberately lands in a different custodian from the stock plan, so any code that resolves the vested-share account by custodian name (finding LL-03) resolves to nothing here.

## Using it by hand

```bash
# Open this fixture in the real app (no effect on your own portfolios)
ROOT=$(mktemp -d)
PYTHONPATH=Scripts python3 -m tests.generate --root "$ROOT" --only p2_equity_comp_rsu
ADVISORBOT_ROOT="$ROOT" python3 Scripts/holdings.py refresh
ADVISORBOT_ROOT="$ROOT" python3 Scripts/advisorbot_server.py --port 8799
```
