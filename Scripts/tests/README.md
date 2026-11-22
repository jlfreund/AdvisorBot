# AdvisorBot test suite

Every test in this suite runs against a **synthetic fixture portfolio** in a throwaway
directory. Nothing reads your own `Portfolios/` folder, and nothing reaches the network.

```bash
make test        # the default set: ~2 min, safe to run any time
make test-full   # + parity, Playwright smoke, slow jobs
make fixtures    # regenerate fixture portfolios, snapshots and CASES.md
```

A bare `pytest` is also safe: real network access and slow jobs are
opt-in (see `[tool.pytest.ini_options]` in `pyproject.toml`).

---

## Why fixtures

The suite this replaced was written against one real household. One test ingested that
household's actual statement folder and asserted its share counts and account values to
the cent, which made the suite both non-portable and a place where position sizes ended up
in version control.

The replacement inverts that. Six generated portfolios cover the product's range, and none
of them resembles the household the application was originally built against. That second
property is what makes them a regression harness rather than just test data: if a rendered
dashboard mentions a custodian, a carrier or a ticker that no fixture contains, the string
came from a literal in the code. The `scans` tier checks exactly that.

---

## Running

| Command | What it does |
| --- | --- |
| `make test` | unit, parsers, pipeline, engine, server, llm, render, scans |
| `make test-full` | the above plus `parity`, the Playwright render smoke and `slow` |
| `make test-one P=p3_retiree_annuities` | every test that declares that fixture |
| `make test-tier T=server` | one tier |
| `make fixtures` | regenerate `fixtures/portfolios/`, `fixtures/expected/`, `CASES.md` |
| `make fixtures-check` | fail if the committed fixtures differ from their specs |
| `pytest --update-snapshots` | rewrite expected snapshots from the current code |
| `pytest --keep-roots` | leave the temporary fixture roots on disk for inspection |

`PYTHON` defaults to `python3.11` (the interpreter that has `pytest` on this machine);
override it if yours differs: `make test PYTHON=python3.12`.

The suite needs only the standard library plus `pytest`. Tiers that need more skip
cleanly: the XLSX parser case needs `openpyxl`, the parity tier and the render smoke need
Node.

---

## Tiers

Every test lives in the directory matching its tier, and is auto-marked with it.

| Marker | Scope | Needs | Runtime | In `make test` |
| --- | --- | --- | --- | --- |
| `unit` | Pure functions: tax models, classification, translator, generator determinism | nothing | seconds | yes |
| `parsers` | Institution parsers against golden page text; the canonical JSON format | nothing | seconds | yes |
| `pipeline` | Ingest → SQLite → view-model on a fixture portfolio | fixture root | < 1 min | yes |
| `engine` | Simulator, solver, Monte Carlo, cohorts | fixture root | < 1 min | yes |
| `server` | Sidecar HTTP: auth, Host checks, writes, demo guard | fixture root, free port | < 1 min | yes |
| `llm` | Shield modes, aliasing, wire-leak gate, with a fake provider | fixture root | seconds | yes |
| `render` | Generated dashboard: structure, self-containment, escaping | fixture root | seconds | structure only |
| `parity` | Python ↔ JS engine equality | Node | 1–2 min | `make test-full` |
| `scans` | Portability, PII, hygiene, fixture drift, documentation freshness | nothing | seconds | yes |

Cross-cutting markers, combined with a tier:

| Marker | Meaning |
| --- | --- |
| `security` | Regression test for a security or privacy finding |
| `slow` | More than a few seconds; excluded by default |
| `network` | Needs real outbound access; excluded by default |
| `legacy` | Ported from the pre-fixture suite; see "Porting notes" |

---

## The six fixture portfolios

Each has a generated `README.md` describing its household, accounts, positions, scenarios
and the invariants tests assert against it.

| Fixture | Purpose |
| --- | --- |
| `p0_empty_household` | Fresh install, no statements. The state every new user starts in, and the one where any unguarded default becomes visible. |
| `p1_index_saver` | One investor, one custodian, ETFs and cash, no-income-tax state. The simplest portfolio that still runs the whole pipeline. |
| `p2_equity_comp_rsu` | Couple with RSUs at an unfamiliar stock-plan custodian, plus Traditional IRA, Roth and 401(k). All grant and wrapper paths. |
| `p3_retiree_annuities` | Retired couple, three annuity types from invented carriers, two-state municipal bonds, a reverse split, Social Security from XML. |
| `p4_trust_household` | Three adults and a minor beneficiary, the same ticker at two custodians, protected scenarios, demo mode, a rich alias table. |
| `p5_adversarial` | Hostile and malformed data: script payloads, a duplicate statement, future and stale dates, negative cash, a plugin that must not load. |

Select them per test:

```python
@pytest.mark.portfolio("p2_equity_comp_rsu")
def test_something(root, payload, cfg):
    ...
```

The first slug becomes the active portfolio; a test may ask for several.

---

## Fixtures available to tests

| Fixture | Gives you |
| --- | --- |
| `root` | A temporary AdvisorBot root with the requested portfolios installed and active |
| `slug` | The active fixture slug |
| `cfg` | That portfolio's `project.json`, as the application loads it |
| `payload` | The view-model payload, built at the spec's pinned `as_of` (cached; deep-copy before mutating) |
| `rendered` | `(path, html)` for the rendered dashboard |
| `db` | An open `PortfolioDB` after ingest |
| `sidecar` | A running sidecar on a free loopback port, plus an HTTP client |

Helpers live in `tests/support/` and are imported, never reimplemented in a test body:

```python
from tests.support import payload as P, pipeline, fake_llm, snapshots, scans
```

---

## Conventions

**File naming** `<tier>/test_<area>_<behaviour>.py`.

**Test naming** `test_<subject>__<condition>__<expectation>`. The double underscore is the
Given / When / Then separator and is what the catalogue splits on.

**Docstrings** The first line is the one-sentence case description and appears in
`CASES.md`; a scan fails any test without one. An optional `Exercises:` paragraph explains
*why* the case exists, which is where the interesting information usually is.

**Traceability** `@pytest.mark.regression("SA-11")` links a test to a finding in
`.dev/SecurityAudit.html`; `@pytest.mark.feature("rmd")` tags the product area. Both are
validated against `tests/support/registry.py`, so a typo fails collection.

**Assertions** Numbers come from `fixtures/expected/` through `support.snapshots`, not from
literals in the test body. A hand-derivable constant (a bracket, a bend point) is fine with
a comment showing the derivation. A scan rejects cents-precise dollar literals in new test
code, because those are almost always pasted from real data.

```python
@pytest.mark.pipeline
@pytest.mark.portfolio("p2_equity_comp_rsu")
@pytest.mark.regression("LL-03")
def test_stock_plan_account__is_resolved_from_configuration(root, payload, cfg):
    """The vested-award account is the one `project.json` names, at its real custodian.

    Exercises: the fixture's stock plan is custodied somewhere the application has never
    seen, so a custodian-name lookup resolves to nothing and this assertion notices.
    """
```

---

## Recording a known gap

Three mechanisms, all of which can only shrink. They exist so the suite stays green while
the open findings stay visible, instead of a growing list of failures everyone learns to
ignore.

| Mechanism | For | Where |
| --- | --- | --- |
| `known_gap: "LL-02"` on a spec invariant | A fixture invariant that should hold but does not yet | `fixtures/specs/*.json` |
| `@pytest.mark.xfail(reason=..., strict=True)` | One test asserting behaviour a finding blocks | the test |
| `expected_leaks.json`, `fallback_allowlist.txt`, `pii_allowlist.txt` | Whole classes of known leaks | `fixtures/denylist/` |

In every case, the moment the behaviour starts working the suite **fails** and tells you to
delete the marker. That is deliberate: closing a finding should be a one-line deletion that
locks the fix in, not an invisible non-event.

---

## Adding a fixture portfolio

1. Write `fixtures/specs/p6_<name>.json`. `fixtures/specs/SCHEMA.md` documents every field.
2. Add the slug to `FIXTURE_SLUGS` in `tests/support/roots.py`.
3. `make fixtures`, then read the generated `fixtures/portfolios/p6_<name>/README.md` and
   check it describes what you intended.
4. Commit the spec, the generated portfolio and its snapshots together.

The generator refuses a spec whose content reuses the legacy vocabulary, so a new fixture
cannot accidentally make the portability scan pass for the wrong reason.

To open a fixture in the real application:

```bash
ROOT=$(mktemp -d)
PYTHONPATH=Scripts python3 -m tests.generate --root "$ROOT" --only p3_retiree_annuities
ADVISORBOT_ROOT="$ROOT" python3 Scripts/holdings.py refresh
ADVISORBOT_ROOT="$ROOT" python3 Scripts/advisorbot_server.py --port 8799
```

---

## Adding a parser case

Fixture statements are written in **canonical statement JSON**
(`*.statement.json`, see `Scripts/ingest/plugins/canonical_json.py`), so no PDF has to be
forged for the pipeline tiers. The institution parsers are covered separately by golden
page text: see `fixtures/parsers/README.md`.

---

## How isolation is enforced

1. `pytest_configure` points `ADVISORBOT_ROOT` at a scratch directory **before** any
   application module is imported.
2. The `root` fixture assembles a per-test root from committed fixtures and switches the
   process to it via `config.reload_root()`, restoring the previous root afterwards.
3. An autouse guard fails any test whose active portfolio is not a known fixture
   slug, so a stray `ADVISORBOT_ROOT` in your shell cannot aim the suite at real data.
4. An autouse fixture blocks outbound HTTP except to loopback; fixture portfolios ship
   their own price history.
5. A scan asserts that no test file refers to `Portfolios/` directly.

### A worked example of why this matters

While this suite was being built, one ad-hoc verification command was run *without* a
fixture root. It called a parser directly, the account-masking layer resolved the active
portfolio, and a test account number was written into the real portfolio's
`account_map.json`. It was removed immediately, but that is exactly the class of accident
the guards above exist to prevent. If you are exploring by hand, set `ADVISORBOT_ROOT`
first.

---

## Porting notes

Thirteen files were carried over from the pre-fixture suite. They keep their coverage but
are marked `legacy` and are not examples of the current conventions: rewrite one in the
tier style when it next needs changing.

Those whose assertions originally encoded the *original* household (its solvency, its
carriers' prose, its account shapes) have all been re-wired against the synthetic fixtures,
so `LEGACY_PENDING` in `conftest.py` is now empty (`{}`). Any future entry parked there
that starts passing fails the audit, which is the prompt to delete its entry.

`test_pipeline.py` was deleted rather than ported: it ingested the real statement folder
and asserted real figures. `pipeline/test_ingest_canonical.py` and
`pipeline/test_grants.py` cover the same ground on fixtures.

---

## Troubleshooting

**`Fixture portfolio '...' has not been generated`** Run `make fixtures`.

**`Committed fixtures differ from their specs`** Someone edited a generated file, or the
application changed what the generator records. Run `make fixtures` and review the diff.

**A snapshot assertion fails after an intended model change** `pytest --update-snapshots`,
then review the diff like code.

**`Refusing to run test against non-fixture root`** `ADVISORBOT_ROOT` is set in
your shell. Unset it.

**`Outbound network access attempted in a test`** Something tried to fetch a quote. Seed
the fixture's `market_prices.json` instead, or mark the test `network`.

**Node-dependent tiers fail to start** `parity` and the render smoke need Node and
Playwright. They are excluded from `make test` for that reason.
