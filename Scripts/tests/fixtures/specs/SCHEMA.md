# Fixture portfolio spec schema

A spec is a JSON file describing one synthetic household. `tests/generate` turns it into a
complete AdvisorBot portfolio folder, its README and its expected snapshots.

JSON rather than YAML, so the generator stays standard-library only, matching the
application's own dependency promise. `tests/generate/spec.py` is the validator and is the
authority; this document is checked against it by
`scans/test_hygiene.py::test_spec_schema_doc__documents_every_validated_key`.

**Validation is strict.** An unknown key is an error, because a silently ignored field
would produce a fixture that does not exercise what its README claims. The generator also
refuses any spec whose *content* reuses the legacy vocabulary in
`fixtures/denylist/legacy_terms.txt`, so a fixture cannot make the portability scan pass
for the wrong reason.

---

## Top level

| Key | Type | Notes |
| --- | --- | --- |
| `spec_version` | int | Must be `1`. |
| `name` | string | Slug, shaped `p<N>_<name>`. Becomes the portfolio folder name. **Required.** |
| `seed` | int | Seeds every price path. Same seed in, identical prices out. **Required.** |
| `as_of` | date | The pinned "today". Payloads and snapshots are built at this date. **Required.** |
| `title` | string | One line, shown at the top of the generated README. **Required.** |
| `purpose` | string | A paragraph explaining what this fixture is for and why it is shaped this way. |
| `exercises` | list | Dotted keys naming the code paths this fixture covers, e.g. `grants.in_kind_transfer`. |
| `household` | object | See below. **Required.** |
| `investors` | list | See below. |
| `beneficiaries` | list | `id`, `name`, `birth`, `milestone_age`. |
| `accounts` | list | See below. |
| `market_prices` | object | `{"SYMBOL": {p0, start, mu, sigma}}`. |
| `scenarios` | list | See below. |
| `privacy` | object | See below. |
| `invariants` | list | See below. |
| `plugins` | list | `{file, content}` written into `Plugins/`. For the opt-in tests. |
| `extra_files` | list | `{path, content}` or `{path, copy_of}`, written verbatim. For adversarial cases. |
| `notes` | string | Anything a reader of the fixture should know. Appended to the README. |

## `household`

| Key | Notes |
| --- | --- |
| `name` | Household name. **Required.** |
| `state` | Two-letter code. Drives the state tax rate and the wording tests. **Required.** |
| `state_label` | Human form for the README, e.g. "Texas (no state income tax)". |
| `tax_rates` | `ltcg_top`, `ordinary_top`, `state_rate`, `ssn_effective`, `ira_ordinary`, `cash_interest`. |
| `primary_equity_symbol` | The concentrated position, or `""` for none. |
| `symbols` | Tracked tickers. Defaults to every priced symbol. |
| `reference_annual_burn`, `retirement_annual_burn` | Spending assumptions. |
| `equity_plan` | Equity-award settings. Emitted as the application's `gsu_forecast` block: `default_net_retention_rate`, `retention_trailing_months`, `target_vested_account_id`, `in_kind_transfer_target_account_id`, `in_kind_transfer_vest_date`, `high_basis_split_threshold`. |
| `sim_asset_rates` | Per-bucket growth overrides, e.g. the cash-sweep APY. |
| `sim_cash_interest_tax_rate`, `sim_ira_ordinary_tax_rate` | Direct overrides. |
| `protected_saves` | Scenario names that must not be overwritten or deleted. |
| `allow_custom_plugins`, `allowed_plugins` | Plugin opt-in, for the SA-15 tests. |
| `ticker_aliases` | Symbol normalisation map. |

## `investors`

`id`, `name`, `birth`, `milestone_age`, `rmd_owner`, and an `ssa` block:

| Key | Notes |
| --- | --- |
| `earnings` | `{start, end, profile}`. Profiles: `mid_rising`, `cap_from_<year>`, `gap_years`, `flat`, `zero`. |
| `claim_age`, `fra_age` | Social Security claiming. |
| `emit_xml` | `true` writes an SSA statement XML and points `external_file` at it; `false` caches the parsed `annual_earnings` map directly. |
| `note` | Optional earnings note override. |

## `accounts`

`id`, `institution`, `account_name`, `account_type`, `tax_status`, `display_order`,
`statement_subdir`, `quirks`, plus:

| Key | Notes |
| --- | --- |
| `periods` | `{from: "YYYY-MM", to: "YYYY-MM", cadence}`. One statement per month, inclusive. |
| `positions` | See below. |
| `grants` | Equity awards held in this account. See below. |

### `positions`

Every position has `kind` and `name`. By kind:

| `kind` | Keys |
| --- | --- |
| `ticker` | `symbol`, `asset_class`, `risk_class`, `stack_bucket`, `lots`, `expense_ratio_pct`, `is_scrapeable`, `quote_symbol`, `split` |
| `cash` | `value`, `yield_pct`, `monthly_drift` |
| `annuity` | `symbol`, `asset_class`, `premium`, `credited_rate_pct`, `issue_date`, `maturity_date`, `surrender_schedule`, `segments` |
| `muni` | `symbol`, `par`, `price`, `coupon_pct`, `state`, `maturity_date` |
| `contract_fund` | `symbol`, `value`, `quantity`, `cost_basis`, `monthly_drift`, `split` |

Shared optional keys: `metadata`, `acquisition_date`, `cost_basis`, `quantity`, `price`.

A `lots` entry is `{qty, acquired, cost_per_share, label}`; cost defaults to the price on
the acquisition date. A `split` is `{effective, ratio, notes, pre_quantity}` and emits a
`PRE_SPLIT_SHARES` adjustment on that month's statement.

### `grants`

| Key | Notes |
| --- | --- |
| `symbol`, `company` | The employer stock. **Required.** |
| `award_type` | `RSU`, `GSU`, … Flows through to every parsed row. |
| `withholding_pct` | Share withholding at each release; sets the retention rate. |
| `grants` | List of `{id, grant_date, total_shares, cadence, first_vest, vests, cliff_months}`. |
| `in_kind_transfer` | `{shares, vest_date, exec_date, to_account}`. Emits a withdrawal and the receiving lot. |
| `price_plan` | Optional price-path override for the grant symbol. |

Vests at or before `as_of` become release history; later ones become unvested tranches.

## `scenarios`

`name`, `protected`, `active`, `notes_md`, and either `settings` (merged over the generated
base) or `settings_delta` (the same thing, named for readability when changing one knob).

## `privacy`

| Key | Notes |
| --- | --- |
| `ai_shield` | `off`, `local_only`, `identity_aliased`, `identity_and_values`. |
| `demo_mode` | `{enabled, scale, seed, dob_granularity}`. |
| `synonyms` | Extra alias inputs, `{"Canonical Name": ["short", "abbrev"]}`. |
| `mcp_server` | `{enabled}`. Off unless a test needs it. |

## `invariants`

What must be true of the built payload. Executed by
`pipeline/test_ingest_canonical.py::test_payload__spec_invariants__hold`.

| Key | Notes |
| --- | --- |
| `path` | Dotted path. **Required.** |
| `source` | `payload` (default) or `payload_summary` (the flat snapshot keys). |
| `equals` / `contains` / `absent` | The expectation. With none of them, the path must simply have a value. |
| `note` | Why this matters. Shown in the README. |
| `known_gap` | A finding id, e.g. `LL-02`. Marks this as behaviour the application *should* have. Reported as an expected failure; if it starts holding, the suite fails and tells you to delete the field. |

---

## Determinism

Every price path is seeded from `(seed, symbol)` with the standard library's Mersenne
Twister, so the same spec produces byte-identical output on any machine. Generated files
carry a `fixture.spec_sha` stamp, and `scans/test_hygiene.py` regenerates every fixture
into a temp directory and diffs it against what is committed. If that fails, either a
generated file was hand-edited or the application changed what the generator records.
