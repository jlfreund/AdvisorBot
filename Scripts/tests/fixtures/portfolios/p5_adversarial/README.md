# Fixture portfolio: `p5_adversarial`

<!-- GENERATED FILE. Edit the spec and run `make fixtures`; do not edit by hand. -->
<!-- spec: fixtures/specs/p5_adversarial.json  sha: a7359e63b0b0e876 -->

**Hostile and malformed data**

Everything a portfolio folder should survive but no well-behaved fixture contains: script payloads in holding and scenario names, a duplicate statement, a future-dated statement, a stale statement, zero and negative values, a missing cost basis, an XML entity declaration, a grants file with a missing symbol, an unsupported file extension, a broken scenario index and a custom plugin that must not be executed. Nothing here should raise; everything here should be reported.

## At a glance

- **Household**: O'Neil & Søren-Quist Household (New York)
- **Pinned `as_of`**: 2026-11-16 (payloads and snapshots are built at this date)
- **Primary equity symbol**: `QX`
- **Tax rates**: LTCG 0.304, ordinary 0.449, state 0.069
- **Statements**: 5 canonical JSON, 7 raw extra file(s)
- **Scenarios**: 3
- **Priced symbols**: `QX`, `ZQZ`

## Household

| Person | Role | Born | Age at as_of | Social Security |
| --- | --- | --- | --- | --- |
| Fiadh O'Neil | investor, RMD owner | 1974-06-17 | 52 | none |
| Håkon Søren-Quist | investor | 1972-02-29 | 54 | none |
| <b>Bold</b> Child | beneficiary | 2010-10-10 | 16 | milestone age 25 |

## Accounts

| Account | Institution | Type | Tax status | Statements |
| --- | --- | --- | --- | --- |
| `QQ-0001` | Quill & Quire Securities | BROKERAGE | TAXABLE | 2026-08 to 2026-10 |
| `QQ-0002` | Quill & Quire Securities | TRAD_IRA | TRAD_IRA | 2026-09 to 2026-10 |

## Positions

| Account | Holding | Kind | Detail |
| --- | --- | --- | --- |
| `QQ-0001` | Quixote Industries');alert(1)// | ticker | 1,500 sh across 1 lot(s), symbol `QX` |
| `QQ-0001` | Zero Quantity Holding <script>alert(2)</script> | ticker | 0 sh across 1 lot(s), symbol `ZQZ` |
| `QQ-0001` | Fund With No Reported Basis | contract_fund | $64,000 |
| `QQ-0001` | Overdrawn Settlement Cash | cash | opens at $-4,200, 0.0% APY |
| `QQ-0002` | Quixote Industries | ticker | 400 sh across 1 lot(s), symbol `QX` |

## Scenarios

- **Default** (protected): `simAnnualBurn`
- **Quote "Injection" And \Backslash**: `simInflationPct`
- **Emoji Scenario 🚀 With A Very Long Name That Goes On And On Past Any Reasonable Column Width To Check Truncation Behaviour In Tables And Dialogs**: `simInflationPct`

## Privacy settings

- **AI shield mode**: `identity_aliased`
- **Demo mode**: disabled
- **MCP server**: disabled
- **Extra synonyms**: 1

## What this fixture exercises

- `ingest.dedupe`
- `ingest.future_dated`
- `ingest.stale_statement`
- `ingest.unsupported_extension`
- `parsers.malformed_grants`
- `parsers.xml_entity_declaration`
- `plugins.opt_in_enforced`
- `render.escaping`
- `scenarios.index_repair`
- `payload.zero_and_negative`
- `aliasing.awkward_names`

## Invariants asserted by tests

| Path | Expectation | Why |
| --- | --- | --- |
| `household_name` | equals `"O'Neil & Søren-Quist Household"` | Apostrophes, ampersands and diacritics survive the pipeline and the renderer. |

## Notes

Files under `Malformed/` sit deliberately outside `Statements/` because one unparseable statement currently aborts the whole ingest run (`ingest.pipeline` re-raises `IngestError`). The parser tier feeds them to the plugins directly, which is the right level for malformed-input coverage, and keeps this fixture ingestable end to end. `Scenarios/_index.json` does not list `900_broken.json`, so the index-repair path sees an orphan file. The plugin marker file is asserted absent by the plugin opt-in test.

## Using it by hand

```bash
# Open this fixture in the real app (no effect on your own portfolios)
ROOT=$(mktemp -d)
PYTHONPATH=Scripts python3 -m tests.generate --root "$ROOT" --only p5_adversarial
ADVISORBOT_ROOT="$ROOT" python3 Scripts/holdings.py refresh
ADVISORBOT_ROOT="$ROOT" python3 Scripts/advisorbot_server.py --port 8799
```
