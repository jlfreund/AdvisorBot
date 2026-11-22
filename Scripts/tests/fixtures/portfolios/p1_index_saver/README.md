# Fixture portfolio: `p1_index_saver`

<!-- GENERATED FILE. Edit the spec and run `make fixtures`; do not edit by hand. -->
<!-- spec: fixtures/specs/p1_index_saver.json  sha: f3c875157999152f -->

**Single investor, one taxable brokerage, index funds and cash**

The simplest portfolio that still runs the whole pipeline. One investor, one custodian, no employer stock, no annuities, no municipal bonds, and a state with no income tax. Anything the dashboard says about a concentrated position, an equity plan or a state tax clause on this fixture is a legacy literal rather than a fact about the data.

## At a glance

- **Household**: Delacroix Household (Texas (no state income tax))
- **Pinned `as_of`**: 2026-11-16 (payloads and snapshots are built at this date)
- **Primary equity symbol**: _none configured_
- **Tax rates**: LTCG 0.238, ordinary 0.370, state 0.000
- **Statements**: 8 canonical JSON
- **Scenarios**: 3
- **Priced symbols**: `BNDX`, `VTI`

## Household

| Person | Role | Born | Age at as_of | Social Security |
| --- | --- | --- | --- | --- |
| Simone Delacroix | investor, RMD owner | 1990-04-22 | 36 | mid_rising 2012-2026, claim at 67, from summary |

## Accounts

| Account | Institution | Type | Tax status | Statements |
| --- | --- | --- | --- | --- |
| `HB-7001` | Harbor Brokerage | BROKERAGE | TAXABLE | 2026-03 to 2026-10 |

## Positions

| Account | Holding | Kind | Detail |
| --- | --- | --- | --- |
| `HB-7001` | Vanguard Total Stock Market ETF | ticker | 1,250 sh across 3 lot(s), symbol `VTI` |
| `HB-7001` | Vanguard Total International Bond ETF | ticker | 2,000 sh across 1 lot(s), symbol `BNDX` |
| `HB-7001` | Harbor Cash Sweep | cash | opens at $51,450, 3.9% APY |

## Scenarios

- **Default** (protected): `simActiveModel`, `simAnnualBurn`, `simInflationPct`
- **Lean Retirement**: `simAnnualBurn`
- **Early Crash Stress**: `simActiveModel`, `simCustomShockPct`

## Privacy settings

- **AI shield mode**: `identity_aliased`
- **Demo mode**: disabled, scale 0.6
- **MCP server**: disabled
- **Extra synonyms**: 1

## What this fixture exercises

- `ingest.monthly_statements`
- `ingest.anchors`
- `ingest.dedupe`
- `payload.account_labels`
- `wrappers.taxable`
- `cash_interest.ordinary_income`
- `social_security.single_investor`
- `simulator.simple_model`
- `solver.annual_burn`
- `monte_carlo.broad_equity_only`
- `render.no_state_tax_clause`

## Invariants asserted by tests

| Path | Expectation | Why |
| --- | --- | --- |
| `institutions` | equals `['Harbor Brokerage']` | Exactly one custodian exists; a second name would mean a hard-coded institution branch fired (LL-02, fixed). |
| `grant_symbols` | equals `[]` | No equity plan exists, so no grant bucket and no placeholder grant (LL-22). |
| `account_count` | equals `1` | One statement-bearing account. |
| `project.primary_equity_symbol` | equals `''` | No concentrated position is configured, so none should be designated. |

## Notes

Twelve-month statement history would also cover year-boundary logic; eight months is enough for anchors, staleness and dedupe, which is what this fixture is for.

## Using it by hand

```bash
# Open this fixture in the real app (no effect on your own portfolios)
ROOT=$(mktemp -d)
PYTHONPATH=Scripts python3 -m tests.generate --root "$ROOT" --only p1_index_saver
ADVISORBOT_ROOT="$ROOT" python3 Scripts/holdings.py refresh
ADVISORBOT_ROOT="$ROOT" python3 Scripts/advisorbot_server.py --port 8799
```
