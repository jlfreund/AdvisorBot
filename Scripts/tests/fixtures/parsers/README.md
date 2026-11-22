# Golden parser fixtures

Each institution parser is covered by **golden page text**: a synthetic statement in the
institution's own layout, plus the `ParsedStatement` the parser must return.

```
fixtures/parsers/<institution>/
    <case>.pages.txt        pages separated by a form-feed line (\f)
    <case>.expected.json    the expected ParsedStatement
```

Tests build an `IngestContext(pages=[...])` and call `detect()` then `parse()`, so **no PDF
is read** and the suite needs no PDF toolchain. `parsers/test_golden_pages.py` runs every
case it finds, so adding a folder is all that is required.

## Why this directory is exempt from the portability scan

A statement's page text necessarily contains the institution's own name and an
account-number-shaped string, because that is precisely what the parser detects on. This
is the one directory excluded from the legacy-vocabulary and account-number scans. The
account numbers here are invented (`1111-2222`), and nothing else about the page is real.

## Authoring a case

1. Open a real statement and extract its text (`python3 Scripts/holdings.py
   inspect-statement <file>` prints what the parser sees).
2. Replace **every** identifier and figure: account number, holder name, security names,
   quantities, prices, totals. Keep the column layout, the header wording and the row
   order, since those are what the regexes match on.
3. Cut to the minimum number of pages that still parses. One page of cash plus one page of
   holdings is usually enough.
4. Make the arithmetic consistent: the closing value should equal the sum of the positions,
   so `verify_checksum` stays silent.
5. Record the expected statement in `<case>.expected.json`. Only the fields you list are
   compared, so start with the ones that matter and add more as the case earns it.
6. Run the tier and read the diff: `make test-tier T=parsers`.

Statement account numbers are preserved in local storage and Live View (and aliased
dynamically by `DemoTranslator` in Demo Mode and `PrivacyTranslator` in AI Chat Mode B/C).

## Coverage

`test_plugin_coverage__every_parser_has_a_case_or_a_recorded_waiver` asserts that the set
of parsers without a golden case exactly equals the recorded waiver list in that file.
Registering a new parser without a case therefore fails with its plugin id, which is the
prompt to author one.

Current cases:

| Institution | Case | Covers |
| --- | --- | --- |
| `wells_fargo` | `brokerage_basic` | Cash sweep plus one managed-sleeve equity, UMA style header, statement period and closing-value reconciliation |

Waived (with reasons in `parsers/test_golden_pages.py`): the three Morgan Stanley formats,
SSA XML, canonical JSON and generic grant JSON. The last three are covered directly by
their own test modules.
