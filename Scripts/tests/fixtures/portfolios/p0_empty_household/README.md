# Fixture portfolio: `p0_empty_household`

<!-- GENERATED FILE. Edit the spec and run `make fixtures`; do not edit by hand. -->
<!-- spec: fixtures/specs/p0_empty_household.json  sha: 741fbb455018473f -->

**Fresh install with no statements**

A portfolio that has been created but never fed a statement. This is the state every new user starts in, and the state in which every literal fallback in the code becomes visible: with no primary equity symbol, no accounts and no Social Security history, anything the dashboard still displays came from a hard-coded default rather than from data.

## At a glance

- **Household**: Nakamura Household (Washington (no state income tax))
- **Pinned `as_of`**: 2026-11-16 (payloads and snapshots are built at this date)
- **Primary equity symbol**: _none configured_
- **Tax rates**: LTCG 0.238, ordinary 0.370, state 0.000
- **Statements**: 0 canonical JSON
- **Scenarios**: 1
- **Priced symbols**: _none_

## Household

| Person | Role | Born | Age at as_of | Social Security |
| --- | --- | --- | --- | --- |
| Ren Nakamura | investor, RMD owner | 1988-09-14 | 38 | none |

## Accounts

| Account | Institution | Type | Tax status | Statements |
| --- | --- | --- | --- | --- |
| _none_ | | | | |

## Positions

| Account | Holding | Kind | Detail |
| --- | --- | --- | --- |
| _none_ | | | |

## Scenarios

- **Default** (protected): base settings

## Privacy settings

- **AI shield mode**: `identity_aliased`
- **Demo mode**: disabled
- **MCP server**: disabled
- **Extra synonyms**: 0

## What this fixture exercises

- `empty_state.render`
- `empty_state.server_start`
- `fallbacks.primary_symbol`
- `fallbacks.social_security`
- `fallbacks.yields`
- `scenarios.default_only`

## Invariants asserted by tests

| Path | Expectation | Why |
| --- | --- | --- |
| `has_portfolio` | equals `True` | A configured-but-empty portfolio still counts as present. |
| `household_name` | equals `'Nakamura Household'` | Page title and header come from config, not from a literal. |
| `sim_asset_count` | equals `0` | No statements means no simulator buckets at all. |
| `account_count` | equals `0` | No accounts are invented from defaults. |

## Notes

This fixture is the default for tests that do not declare a portfolio, so it is also the cheapest root to assemble.

## Using it by hand

```bash
# Open this fixture in the real app (no effect on your own portfolios)
ROOT=$(mktemp -d)
PYTHONPATH=Scripts python3 -m tests.generate --root "$ROOT" --only p0_empty_household
ADVISORBOT_ROOT="$ROOT" python3 Scripts/holdings.py refresh
ADVISORBOT_ROOT="$ROOT" python3 Scripts/advisorbot_server.py --port 8799
```
