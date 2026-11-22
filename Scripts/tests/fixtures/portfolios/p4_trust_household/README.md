# Fixture portfolio: `p4_trust_household`

<!-- GENERATED FILE. Edit the spec and run `make fixtures`; do not edit by hand. -->
<!-- spec: fixtures/specs/p4_trust_household.json  sha: fd752403b6d831a3 -->

**Three-adult trust household with a minor beneficiary, two custodians and a rich alias table**

The scenario, privacy and demo-mode fixture. The same concentrated ticker is held at two different custodians, so any account label that comes from a two-custodian branch rather than from configuration is visible immediately. Three adults plus a minor beneficiary exercise milestone resolution and scheduled gifts, and a deliberately awkward set of names and scenario titles (apostrophes, diacritics, a hyphen) exercises the alias registry and the escaping path.

## At a glance

- **Household**: Varga Family Trust (Massachusetts (flat 5%))
- **Pinned `as_of`**: 2026-11-16 (payloads and snapshots are built at this date)
- **Primary equity symbol**: `KLRN`
- **Tax rates**: LTCG 0.288, ordinary 0.420, state 0.050
- **Statements**: 50 canonical JSON
- **Scenarios**: 5
- **Priced symbols**: `AGG`, `IEFA`, `ITOT`, `KLRN`

## Household

| Person | Role | Born | Age at as_of | Social Security |
| --- | --- | --- | --- | --- |
| Ilona Varga | investor, RMD owner | 1968-12-02 | 57 | cap_from_2010 1992-2026, claim at 67, from summary |
| Petros Varga | investor | 1967-05-19 | 59 | mid_rising 1990-2024, claim at 70, from summary |
| Zsófia Varga | investor | 1996-08-08 | 30 | mid_rising 2019-2026, claim at 67, from summary |
| Noa Varga | beneficiary | 2015-01-30 | 11 | milestone age 21 |

## Accounts

| Account | Institution | Type | Tax status | Statements |
| --- | --- | --- | --- | --- |
| `CB-6200` | Cedarline Wealth | BROKERAGE | TAXABLE | 2026-01 to 2026-10 |
| `HB-8800` | Harbor Brokerage | BROKERAGE | TAXABLE | 2026-01 to 2026-10 |
| `HB-8801` | Harbor Brokerage | ROTH_IRA | ROTH_IRA | 2026-01 to 2026-10 |
| `CB-6201` | Cedarline Wealth | TRAD_IRA | TRAD_IRA | 2026-01 to 2026-10 |
| `HB-8900` | Harbor Brokerage | BROKERAGE | TAXABLE | 2026-01 to 2026-10 |

## Positions

| Account | Holding | Kind | Detail |
| --- | --- | --- | --- |
| `CB-6200` | Kelerin Therapeutics, Inc. | ticker | 12,000 sh across 2 lot(s), symbol `KLRN` |
| `CB-6200` | iShares Core S&P Total U.S. Stock Market ETF | ticker | 1,800 sh across 1 lot(s), symbol `ITOT` |
| `CB-6200` | Cedarline Trust Cash Sweep | cash | opens at $96,000, 3.7% APY |
| `HB-8800` | Kelerin Therapeutics, Inc. | ticker | 4,200 sh across 1 lot(s), symbol `KLRN` |
| `HB-8800` | iShares Core MSCI EAFE ETF | ticker | 2,500 sh across 1 lot(s), symbol `IEFA` |
| `HB-8801` | iShares Core S&P Total U.S. Stock Market ETF | ticker | 700 sh across 1 lot(s), symbol `ITOT` |
| `CB-6201` | iShares Core U.S. Aggregate Bond ETF | ticker | 4,000 sh across 1 lot(s), symbol `AGG` |
| `HB-8900` | iShares Core S&P Total U.S. Stock Market ETF | ticker | 180 sh across 1 lot(s), symbol `ITOT` |

## Scenarios

- **Default** (protected): `simAnnualBurn`
- **Ilona's Default** (protected): `simAnnualBurn`
- **Gift To Noa At 21**: `simScheduledActions`
- **Sell Concentrated Position 2028**: `simScheduledActions`
- **Compare Baseline**: `simInflationPct`

## Privacy settings

- **AI shield mode**: `identity_aliased`
- **Demo mode**: disabled, scale 0.4
- **MCP server**: disabled
- **Extra synonyms**: 7

## What this fixture exercises

- `scenarios.crud`
- `scenarios.protected_names`
- `scenarios.compare`
- `demo_mode.projection`
- `aliasing.round_trip`
- `aliasing.synonyms`
- `privacy_shield.wire_gate`
- `payload.two_custodians_same_ticker`
- `social_security.three_adults`
- `beneficiary.scheduled_gift`
- `server_auth.scenario_writes`

## Invariants asserted by tests

| Path | Expectation | Why |
| --- | --- | --- |
| `institutions` | equals `['Cedarline Wealth', 'Harbor Brokerage']` | Two custodians, both read from config. A third name means a hard-coded branch fired (LL-02). |
| `project.primary_equity_symbol` | equals `'KLRN'` | The concentrated ticker is configured, not assumed. |

## Notes

Scenario names here deliberately include an apostrophe and the household includes a diacritic, which is what makes this the right fixture for the escaping and alias-boundary tests.

## Using it by hand

```bash
# Open this fixture in the real app (no effect on your own portfolios)
ROOT=$(mktemp -d)
PYTHONPATH=Scripts python3 -m tests.generate --root "$ROOT" --only p4_trust_household
ADVISORBOT_ROOT="$ROOT" python3 Scripts/holdings.py refresh
ADVISORBOT_ROOT="$ROOT" python3 Scripts/advisorbot_server.py --port 8799
```
