# Test case catalogue

<!-- GENERATED FILE. Run `make fixtures` (or `python3 -m tests.catalog`) to refresh. -->

**323 tests** across 9 tiers. 80 are tagged as security regressions, 0 are expected failures recording an open finding, and 101 are ported from the pre-fixture suite.

Tagged findings: `DA2-01`, `DA2-04`, `DA2-13`, `DA3-01`, `DA3-03`, `DA3-04`, `DA3-05`, `DA3-06`, `DA4-03`, `DA4-07`, `DA4-08b`, `DA4-09`, `DA4-10`, `DA4-11`, `DA4-13`, `DP-01`, `DP-02`, `DP-03`, `DP-04`, `I4`, `LL-01`, `LL-03`, `LL-06`, `LL-08`, `LL-09`, `LL-13`, `LL-14`, `LL-22`, `LL-23`, `S1`, `SA-02`, `SA-03`, `SA-04`, `SA-05`, `SA-06`, `SA-09`, `SA-11`, `SA-13`, `SA-15`, `SA-18`, `SA-20`, `SA2-01`, `SA2-02`, `SA2-03`, `SA2-04`, `SA2-06`, `SA2-07`, `SA2-08`, `SA2-10`, `SA2-11`, `SA2-12`, `SA2-13`, `SA2-14`, `SA2-15`, `SA2-16`, `SA2-17`, `SA2-21`, `SA2-22`, `SA3-01`, `SA3-02`, `SA3-05`, `SA3-06`, `SA3-08`, `SA4-01`, `SA4-03`, `SA4-04`, `SA4-05`, `SA5-01`, `SA5-02`, `SA5-03`, `SA5-04`, `SA5-05`, `SA5-06`, `SA5-07`, `SA5-08`, `SA5-09`, `SA5-10`.

| Tier | Tests |
| --- | --- |
| `unit` | 18 |
| `parsers` | 28 |
| `pipeline` | 53 |
| `engine` | 41 |
| `parity` | 10 |
| `render` | 18 |
| `server` | 98 |
| `llm` | 35 |
| `scans` | 22 |

## `unit`

### `unit/test_classification.py`

| Case | Portfolio | Covers | Summary |
| --- | --- | --- | --- |
| `test_risk_taxonomy__is_exactly_seven_classes__in_increasing_risk_order` | — | _asset_classes_ | The risk taxonomy is a closed seven-value set, so code can switch on it exhaustively. |
| `test_validate_risk_class__plausible_pairing__is_accepted` | — | _asset_classes_ | Every asset class a fixture portfolio uses pairs cleanly with its risk class. |
| `test_validate_risk_class__missing__raises_rather_than_defaulting` | — | _asset_classes_ | An unclassified holding fails loudly instead of inheriting a silent default. |
| `test_validate_risk_class__conflicting_pair__raises_with_allowed_set` | — | _asset_classes_ | Cash cannot be single-stock; the error names what would have been allowed. |
| `test_annuity_classes__carry_their_own_tax_shape__without_a_carrier_name` | — | `LL-13` _asset_classes_ | Fixed, indexed and buffered annuities are distinguishable by asset class alone. |

### `unit/test_fixture_determinism.py`

| Case | Portfolio | Covers | Summary |
| --- | --- | --- | --- |
| `test_price_series__same_seed_and_symbol__is_identical_across_calls` | — | _payload_ | Two independent builds of the same symbol agree to the cent. |
| `test_price_series__different_symbol__diverges` | — | _payload_ | Per-symbol seeding means two symbols in one spec are not the same path. |
| `test_price_series__zero_volatility__compounds_smoothly` | — | _payload_ | A sigma of zero gives a deterministic compounding path, for engine fixtures. |
| `test_price_on__before_first_close__falls_forward_to_the_first_known_price` | — | _payload_ | Lookups never return None, so a lot acquired before the series starts still prices. |
| `test_spec_sha__is_stable_and_order_independent` | — | _payload_ | The spec hash stamped into fixtures ignores key ordering. |
| `test_every_fixture_slug__has_a_spec_and_a_generated_folder` | — | _payload_ | The registry, the specs on disk and the generated fixtures agree. |

### `unit/test_symbol_lookup.py`

| Case | Portfolio | Covers | Summary |
| --- | --- | --- | --- |
| `test_symbol_lookup__resolves_major_indices_and_punctuation_variants` | — | _payload_ | Common market index names and bare index codes resolve to Yahoo Finance `^` tickers. |
| `test_symbol_lookup__preserves_real_stock_tickers_and_honors_custom_aliases` | — | _payload_ | Real NYSE/NASDAQ stock tickers are never shadowed, and portfolio `ticker_aliases` take precedence. |

### `unit/test_tax_engine.py`

| Case | Portfolio | Covers | Summary |
| --- | --- | --- | --- |
| `SellLotTests::test_underwater_lot_harvests_exact_loss_no_tax` | — | _legacy_ | _(no docstring)_ |
| `SellLotTests::test_gross_up_formula_net_equals_gross_times_factor` | — | _legacy_ | _(no docstring)_ |
| `SellLotTests::test_carryforward_shelters_gain_dollar_for_dollar` | — | _legacy_ | _(no docstring)_ |
| `AnnuityAndLockTests::test_locked_annuity_skipped_before_maturity` | — | _legacy_ | _(no docstring)_ |
| `AnnuityAndLockTests::test_annuity_lifo_earnings_before_principal` | — | _legacy_ | _(no docstring)_ |

## `parsers`

### `parsers/test_canonical_json.py`

| Case | Portfolio | Covers | Summary |
| --- | --- | --- | --- |
| `test_detection__canonical_file__wins_over_every_other_plugin` | — | _ingest_ | A `*.statement.json` file is claimed by the canonical plugin, not by the grants plugin. |
| `test_detection__json_without_the_format_header__is_not_claimed` | — | _ingest_ | An arbitrary JSON file named `.statement.json` is ignored without the format marker. |
| `test_parse__minimal_statement__maps_onto_the_parsed_schema` | — | _ingest_ | Fields land on `ParsedStatement`/`ParsedPosition` without reinterpretation. |
| `test_parse__reported_total__reconciles_against_summed_positions` | — | _ingest_ | The checksum hook sees a consistent statement, so fixtures never warn. |
| `test_parse__account_overrides_in_config__win_over_statement_metadata` | — | _ingest_ | `project.json -> accounts[id]` renames an account without touching the statement. |
| `test_parse__malformed_input__raises_with_a_pointer_to_the_problem` | — | _ingest_ | Every validation failure names the offending field. |
| `test_parse__duplicate_security_ids__are_rejected` | — | _ingest_ | Two rows for one security would double-count silently, so it is an error. |
| `test_parse__embedded_grants__are_accepted_from_one_file` | — | _ingest_ | A single-file fixture can carry positions and awards together. |
| `test_parse__adjustment_rows__carry_split_history` | — | _ingest_ | Share-count adjustments ride along with the statement that reports them. |
| `test_parse__unknown_adjustment_type__is_rejected` | — | _ingest_ | Only the two adjustment types the pipeline understands are accepted. |
| `test_committed_fixture_statements__all_parse` | — | _ingest_ | Every statement the generator has written is parseable right now. |
| `test_payload_helper__is_reusable_without_a_file` | — | _ingest_ | The validator is importable on its own, for callers building statements in memory. |

### `parsers/test_golden_pages.py`

| Case | Portfolio | Covers | Summary |
| --- | --- | --- | --- |
| `test_golden_case__parses_to_the_expected_statement` | — | _ingest_ | Each golden page set parses to its recorded `ParsedStatement`. |
| `test_golden_case__reported_total_reconciles` | — | _ingest_ | A golden case is internally consistent, so the checksum hook stays silent. |
| `test_unrecognised_layout__is_claimed_by_nobody` | — | _ingest_ | Text from an institution with no parser scores zero everywhere. |
| `test_plugin_coverage__every_parser_has_a_case_or_a_recorded_waiver` | — | _ingest_ | No parser is silently untested. |

### `parsers/test_grant_json.py`

| Case | Portfolio | Covers | Summary |
| --- | --- | --- | --- |
| `test_fixture_grants_file__parses_into_tranches_releases_and_withdrawals` | — | _grants_ | The generated award file carries unvested tranches, vested releases and a transfer. |
| `test_award_type_and_symbol__come_from_the_file_not_from_a_default` | — | `LL-06` _grants_ | Every parsed row carries the fixture's own ticker and award type. |
| `test_releases__net_shares_reflect_the_specified_withholding` | — | _grants_ | Net shares are gross minus withholding, which is what retention is computed from. |
| `test_missing_symbol__is_rejected_rather_than_defaulted` | — | `LL-14` _grants_ | A tranche with no symbol fails loudly. |
| `test_empty_payload__is_rejected` | — | _grants_ | A grants file with no rows is a mistake, not an empty success. |

### `parsers/test_ssa_xml.py`

| Case | Portfolio | Covers | Summary |
| --- | --- | --- | --- |
| `test_generated_ssa_xml__parses_with_the_expected_earnings_table` | — | _social_security_ | The fixture's XML round-trips to the earnings table its spec profile describes. |
| `test_ssa_plugin__detects_and_summarises_the_statement` | — | _social_security_ | The plugin claims an SSA document and reports its span as an account row. |
| `test_entity_declaration__does_not_expand_recursively` | — | `SA-18` _social_security_ | An XML document declaring internal entities is handled without amplification. |
| `test_parse_ssa_xml_text__non_xml_input__reports_an_error_rather_than_raising` | — | _social_security_ | Garbage in gives a structured error, which is what the audit table renders. |
| `test_parse_ssa_xml_path__outside_the_portfolio__is_refused` | `p3_retiree_annuities` | `SA-13` _social_security_ | An absolute path outside the active portfolio is refused, not read. |
| `test_parse_ssa_xml_path__missing_file_inside_the_portfolio__reports_not_found` | `p3_retiree_annuities` | _social_security_ | A path that is allowed but absent is reported as missing. |
| `test_parse_ssa_xml_path__relative_fixture_path__resolves_inside_the_portfolio` | `p3_retiree_annuities` | `SA-13` _social_security_ | `investors[].ssn.external_file` resolves relative to the active portfolio folder. |

## `pipeline`

### `pipeline/test_adversarial.py`

| Case | Portfolio | Covers | Summary |
| --- | --- | --- | --- |
| `test_ingest__survives_the_whole_adversarial_folder` | `p5_adversarial` | _ingest_ | The folder ingests without an exception, and reports what it skipped. |
| `test_unsupported_extension__is_not_offered_to_any_parser` | `p5_adversarial` | _ingest_ | A `.rtf` file in the statements folder is ignored by the scanner, not failed over. |
| `test_duplicate_statement__is_ingested_once` | `p5_adversarial` | _dedupe_ | The byte-identical copy under another filename does not double-count. |
| `test_future_dated_statement__does_not_become_the_current_anchor` | `p5_adversarial` | _anchors_ | A statement dated next year does not set today's values. |
| `test_zero_and_negative_values__render_without_error` | `p5_adversarial` | _payload_ | Awkward values do not break the rollup arithmetic. |
| `test_overdrawn_cash__is_carried_into_the_account_total` | `p5_adversarial` | _payload_ | A negative settlement balance reduces the account total rather than vanishing. |
| `test_holding_without_a_cost_basis__is_reported_not_guessed` | `p5_adversarial` | _payload_ | A fund with no reported basis shows an explicit absence rather than a made-up number. |
| `test_ingest__never_imports_a_non_opted_in_plugin` | `p5_adversarial` | `SA-15` _plugins_ | The custom plugin in the fixture's `Plugins/` folder is not executed. |
| `test_broken_scenario_file__does_not_break_the_scenario_list` | `p5_adversarial` | _scenarios_ | An unparseable scenario file is skipped and the valid ones still load. |
| `test_privacy_registry__handles_awkward_names` | `p5_adversarial` | _aliasing_ | An apostrophe, an ampersand, a diacritic and a hyphen all register as entities. |
| `test_malformed_statement__logs_error_and_continues_ingesting` | `p5_adversarial` | _ingest_ | One malformed statement file records a per-file error instead of aborting valid files. |

### `pipeline/test_family_members.py`

| Case | Portfolio | Covers | Summary |
| --- | --- | --- | --- |
| `TestFamilyMembersBackend::test_synthesizes_family_from_investors_and_beneficiaries` | — | _legacy_ | _(no docstring)_ |
| `TestFamilyMembersBackend::test_save_project_config_persists_family_members` | — | _legacy_ | _(no docstring)_ |
| `TestFamilyMembersFrontend::test_html_contains_family_elements` | — | _legacy_ | _(no docstring)_ |
| `TestFamilyMembersFrontend::test_node_family_members_logic` | — | _legacy_ | _(no docstring)_ |

### `pipeline/test_forecast_enrichment.py`

| Case | Portfolio | Covers | Summary |
| --- | --- | --- | --- |
| `PhaseEIngestForecastsTest::test_7_risk_classes_taxonomy_and_validation_gate` | — | _legacy_ | _(no docstring)_ |
| `PhaseEIngestForecastsTest::test_atomic_rollback_on_mid_statement_classification_error` | — | _legacy_ | _(no docstring)_ |
| `PhaseEIngestForecastsTest::test_3_tier_forecast_enricher_waterfall` | — | _legacy_ | _(no docstring)_ |
| `PhaseEIngestForecastsTest::test_sleeve_constituent_bidirectional_rollup_and_realized_vol_override` | — | _legacy_ | _(no docstring)_ |
| `PhaseEIngestForecastsTest::test_7_risk_classes_python_js_parity` | — | _legacy_ | _(no docstring)_ |
| `PhaseEIngestForecastsTest::test_plugin_checksum_custom_discovery_and_cli` | — | _legacy_ | _(no docstring)_ |

### `pipeline/test_grants.py`

| Case | Portfolio | Covers | Summary |
| --- | --- | --- | --- |
| `test_grants__symbol_and_company__come_from_the_award_file` | `p2_equity_comp_rsu` | _grants_ | The dashboard reports the fixture's own ticker and issuer, not a default. |
| `test_stock_plan_account__is_resolved_from_configuration` | `p2_equity_comp_rsu` | `LL-03` _grants_ | The vested-award account is the one `project.json` names, at its real custodian. |
| `test_grants__unvested_tranches__become_a_future_release_bucket` | `p2_equity_comp_rsu` | _grants_ | Future vests roll into one simulator bucket named after the fixture's own ticker. |
| `test_net_retention__is_computed_from_this_fixtures_release_history` | `p2_equity_comp_rsu` | `LL-09` _grants_ | Retention comes from the releases in the data, not from a constant. |
| `test_grant_schedule__every_specified_grant_keeps_its_own_id` | `p2_equity_comp_rsu` | `LL-22` _grants_ | All three grants are represented under the ids the award file gave them. |
| `test_grant_releases__every_vest_is_recorded_even_when_the_grant_id_is_lost` | `p2_equity_comp_rsu` | _grants_ | No release is dropped, whatever id it ends up filed under. |
| `test_in_kind_transfer__lands_in_the_receiving_account_with_carryover_basis` | `p2_equity_comp_rsu` | _grants_ | The transferred lot appears at the destination custodian with its vest-date basis. |
| `test_wrappers__each_account_type__gets_its_own_tax_rule` | `p2_equity_comp_rsu` | _wrappers_ | Taxable, Traditional IRA, Roth and 401(k) buckets each state their own treatment. |
| `test_rmd_owner__is_the_configured_investor` | `p2_equity_comp_rsu` | _rmd_ | RMDs attach to the investor the config flags, not to the first investor in the list. |
| `test_payload__matches_the_committed_snapshots` | `p2_equity_comp_rsu` | _payload_ | Account totals and simulator buckets agree with the recorded snapshots. |

### `pipeline/test_ingest_canonical.py`

| Case | Portfolio | Covers | Summary |
| --- | --- | --- | --- |
| `test_ingest__fixture_statements__all_recognised_and_stored` | `p1_index_saver` | _ingest_ | Every statement in the fixture is claimed by a plugin and ingested. |
| `test_ingest__securities_and_snapshots__land_in_the_database` | `p1_index_saver` | _ingest_ | Positions become securities plus point-in-time snapshots, one set per statement date. |
| `test_ingest__non_scrapeable_cash__gets_a_valuation_anchor` | `p1_index_saver` | _anchors_ | The cash sweep is statement-anchored, so it must produce an anchor row. |
| `test_ingest__events_log__records_one_row_per_ingested_file` | `p1_index_saver` | _ingest_ | `ingest_events` is the audit trail the dashboard's recent-activity panel reads. |
| `test_ingest__identical_statement_under_a_new_name__is_deduplicated` | `p1_index_saver` | _dedupe_ | Re-dropping the same bytes under a different filename ingests nothing new. |
| `test_ingest__rerun_without_changes__is_idempotent` | `p1_index_saver` | _ingest_ | A second refresh over unchanged files changes no positions. |
| `test_payload__matches_the_committed_summary_snapshot` | `p1_index_saver` | _payload_ | The payload agrees with `fixtures/expected/<slug>/payload_summary.json`. |
| `test_payload__simulator_buckets__match_the_committed_snapshot` | `p1_index_saver` | _payload_ | Per-bucket values, wrappers and rule titles agree with the recorded snapshot. |
| `test_payload__spec_invariants__hold` | `p1_index_saver` | _payload_ | The invariants the fixture's spec declares are true of the built payload. |
| `test_cash_sweep__is_a_single_merged_bucket_with_its_configured_rate` | `p1_index_saver` | _cash_interest_ | Cash across accounts rolls into one simulator bucket carrying the configured APY. |

### `pipeline/test_recent_change.py`

| Case | Portfolio | Covers | Summary |
| --- | --- | --- | --- |
| `AdaptiveRecentChangeTest::test_resolve_item_recent_change_equities` | — | _legacy_ | _(no docstring)_ |
| `AdaptiveRecentChangeTest::test_resolve_item_recent_change_annuities` | — | _legacy_ | _(no docstring)_ |
| `AdaptiveRecentChangeTest::test_resolve_item_recent_change_munis` | — | _legacy_ | _(no docstring)_ |
| `AdaptiveRecentChangeTest::test_resolve_item_recent_change_cash` | — | _legacy_ | _(no docstring)_ |
| `AdaptiveRecentChangeTest::test_live_payload_groups_contain_recent_change` | — | _legacy_ | _(no docstring)_ |
| `AdaptiveRecentChangeTest::test_dom_header_and_mode_toggle` | — | _legacy_ | _(no docstring)_ |

### `pipeline/test_stock_grant_math.py`

| Case | Portfolio | Covers | Summary |
| --- | --- | --- | --- |
| `StockGrantsTests::test_legacy_db_migrates_gsu_tranches_composite_pk` | — | _legacy_ | Legacy DB with 3-col PK `(report_date, grant_id, vest_date)` upgrades safely to include `symbol` in PK. |
| `StockGrantsTests::test_n0_no_unvested_hides_nav_and_omits_future_buckets` | — | _legacy_ | D-01/D-06: Historical-only releases with 0 future unvested shares keeps nav hidden (`has_unvested=False`). |
| `StockGrantsTests::test_n1_single_company_grants_json_scanner_and_sim` | — | _legacy_ | D-02/D-07: Dropping `*.grants.json` into Statements/ auto-configures `<ShortName> Stock` & simulator bucket. |
| `StockGrantsTests::test_n2_multi_company_and_custom_plugin_and_cli` | — | _legacy_ | D-03/D-08/D-10: Two companies show `Stock Grants` picker; `Plugins/*.py` auto-loads; CLI `ingest-grants` works. |
| `StockGrantsTests::test_http_handle_ingest_grants_validation_and_success` | — | _legacy_ | D-10: `POST /api/grants/ingest` handler validates schema and ingests custom FI grants. |
| `StockGrantsTests::test_cash_plan_withdrawals_do_not_consume_equity_tranches` | — | _legacy_ | USD cash disbursements (plan_name='Cash') must not subtract dollars as equity shares. |

## `engine`

### `engine/test_monte_carlo.py`

| Case | Portfolio | Covers | Summary |
| --- | --- | --- | --- |
| `MonteCarloTest::test_bear_shield_first_year_uses_only_available_information` | — | _legacy_ | Different seeded paths over the first year produce identical results with the shield on and off. |
| `MonteCarloTest::test_zero_vol_matches_single_path` | — | _legacy_ | vol=0 => degenerate paths => MC median == deterministic single-path engine, to the cent. |
| `MonteCarloTest::test_determinism` | — | _legacy_ | Same seed => byte-identical band + success rates. |
| `MonteCarloTest::test_band_monotonic_and_success_bounds` | — | _legacy_ | _(no docstring)_ |
| `MonteCarloTest::test_historical_model_runs` | — | _legacy_ | _(no docstring)_ |
| `MonteCarloTest::test_empty_ltcg_tranches_fallback_preserves_month0_value` | — | _legacy_ | A vested LTCG_TRANCHES bucket with empty tranches[] must retain its current_value at Month 0. |
| `MonteCarloTest::test_same_ticker_buckets_share_idiosyncratic_shock` | — | _legacy_ | Splitting one single-stock position into 4 equal buckets must produce identical MC percentiles (zero fake diversification). |

### `engine/test_pass3_design_regressions.py`

| Case | Portfolio | Covers | Summary |
| --- | --- | --- | --- |
| `test_hypothetical_ira_rollover__inherits_younger_spouse_owner_and_skips_premature_rmd` | — | _rmd_ | DA4-01/D11: Rolling a younger spouse's 401k into a hypothetical Traditional IRA preserves their birth year and $0 premature RMD. |
| `test_hypothetical_ira__explicit_owner_pools_with_existing_owner_iras_for_january_rmd` | — | _rmd_ | DA4-01/D11: A hypothetical Traditional IRA with explicit owner metadata pools under ('owner', idx, 'IRA') for January RMDs. |
| `test_strategy_rollups__separate_multi_account_sleeves_and_keep_ira_stocks_wrapped` | — | _wrappers_ | DA4-02: Multi-account taxable equity/muni sleeves emit distinct per-account rollups while >5-stock IRAs stay wrapped. |
| `test_scheduled_actions_person_age_normalization__supports_prefix_fractional_fallback_without_crash` | — | _simulator_ | DA4-04: Scheduled action normalization resolves exact and prefix family names, fractional ages, TBD fallbacks, and unknown names safely. |
| `test_optimizer_without_payload__preserves_active_project_context` | — | _simulator_ | DA4-05: Calling optimize_sim_asset_order with payload=None preserves the active project's filing/tax context. |
| `test_pooled_rmd_tax_attribution_and_ordinary_deposit_basis` | — | _tax_models_ | DA4-06/DA4-14: Mandatory RMD funding living burn never leaks RMD tax onto a 0%-tax cash withdrawal, and IRA deposits add $0 basis. |
| `test_intramonth_rsu_vesting__vests_in_calendar_month_and_keeps_exact_lot_date` | — | _grants_ | DA4-06: An RSU vesting on the 25th vests during the 10th-of-month step while preserving exact vestIso for 1-year holding checks. |
| `test_zero_percent_tier1_decays_to_equilibrium_and_ref_rates_reset_half_lives` | — | _simulator_ | DA4-08/D10: A 0.00% Tier-1 forecast decays toward Tier-2/Tier-3 unless Flat or 0x multiplier, and use_ref_rates resets half-lives. |
| `test_view_model_cash_collision_secondary_grant_constituent_keys_and_jan1_fra` | — | _payload_ | DA4-08: Verify Jan-1 SSA FRA rule, constituent ticker parent keys, secondary RSU grant isolation, and rollup_cash_all key stability. |
| `test_privacy_status_meta__reraises_corrupted_privacy_registry_error` | — | _privacy_shield_ | SA5-02b: A corrupted privacy_registry.json raises PrivacyConfigError out of _build_privacy_status_meta (fail-closed). |

### `engine/test_pre_retirement_work.py`

| Case | Portfolio | Covers | Summary |
| --- | --- | --- | --- |
| `PreRetirementWorkTest::test_work_plan_validation_and_zero_pii_sanitization` | — | — | Sanitizer strips free-text employer PII and validator enforces Retirement Age <= Milestone Age. |
| `PreRetirementWorkTest::test_wrapper_cost_basis_and_catchup_invariants` | — | — | Employee Roth adds basis, employer Roth/Pre-Tax adds $0 basis, and Age 50/60-63 catch-up triggers by year. |
| `PreRetirementWorkTest::test_staggered_retirement_bridge_burn_and_gift_independence` | — | — | Staggered retirements draw partial bridge burn during overlap and never suppress Scheduled Gifts. |
| `PreRetirementWorkTest::test_early_crash_stress_anchors_to_drawdown_start_month` | — | — | Early Crash (`stress`) defers Downfall shock until `drawdown_start_month` when paycheck covers burn. |
| `PreRetirementWorkTest::test_privacy_shield_and_demo_mode_work_plan_invariants` | — | — | Mode C and Demo Mode alias composite IDs and scale work USD fields without scaling retirement ages. |
| `PreRetirementWorkTest::test_python_js_parity_with_work_plan_and_scenario_overrides` | — | — | Python and Browser JS engines match 1:1 across Single-Path, Stress, MC, Cohorts, and Auto-Optimize. |

### `engine/test_projection_models.py`

| Case | Portfolio | Covers | Summary |
| --- | --- | --- | --- |
| `test_simulation__runs_and_reports_a_milestone_value` | `p2_equity_comp_rsu` | _simulator_ | A deterministic run produces a milestone wealth figure and a tax total. |
| `test_simulation__is_deterministic_across_runs` | `p2_equity_comp_rsu` | _simulator_ | The same inputs give the same output, to the cent. |
| `test_monte_carlo__with_zero_volatility__collapses_onto_the_single_path` | `p2_equity_comp_rsu` | _monte_carlo_ | With all volatilities zeroed, every percentile of the fan equals the deterministic path. |
| `test_monte_carlo__is_seeded_and_reproducible` | `p2_equity_comp_rsu` | _monte_carlo_ | Two identical Monte Carlo runs agree, so the fan can be snapshot-tested. |
| `test_solver__target_derived_from_the_fixtures_own_outcome__converges` | `p2_equity_comp_rsu` | _solver_ | Goal-seek converges on a target the fixture can actually reach. |
| `test_solver__unreachable_target__reports_the_achievable_range` | `p2_equity_comp_rsu` | _solver_ | An impossible target is reported as unreachable, with the range that is possible. |
| `test_annuity_buckets__are_taxed_under_the_contract_rule_not_the_carrier_name` | `p2_equity_comp_rsu`, `p3_retiree_annuities` | _wrappers_ | All three annuity classes get the ordinary-income-on-gain treatment from their class. |
| `test_historical_cohorts__are_available_and_span_the_published_range` | `p2_equity_comp_rsu`, `p1_index_saver` | _cohorts_ | The cohort table loads and covers the documented start year onward. |
| `test_every_scenario__runs_without_error` | `p2_equity_comp_rsu` | _simulator_ | Each fixture's saved scenarios all execute. |

### `engine/test_release_accuracy.py`

| Case | Portfolio | Covers | Summary |
| --- | --- | --- | --- |
| `test_ordinary_tax_at_every_boundary` | — | `DA3-06` | Tax at the 2026 standard deduction and each ordinary bracket matches the IRS schedule. |
| `test_lot_calendar_anniversary` | — | `DA3-05` | Same-day and anniversary sales are short term, including a leap-day purchase. |
| `test_reopened_schedule_keeps_phase` | — | `DA3-03` | An expired deposit is skipped and a quarterly draw keeps its original payment months. |
| `test_rmd_uses_prior_close_for_each_owner_and_plan` | — | `DA3-01` | Separate IRAs of same-age spouses and separate employer plans each owe their own January draw. |
| `test_pia_uses_eligibility_year_and_later_colas` | — | `DA3-04` | Identical $1,000 AIMEs receive the historical eligibility-year bend points and their subsequent COLAs. |
| `test_competing_withdrawals_report_funded_amounts` | — | — | Two withdrawals requesting $100 each share only $130 after ordinary expenses. |

### `engine/test_solver.py`

| Case | Portfolio | Covers | Summary |
| --- | --- | --- | --- |
| `SolverTests::test_unreachable_target_reports_not_converged` | — | _legacy_ | _(no docstring)_ |
| `SolverTests::test_reachable_target_converges_small_residual` | — | _legacy_ | _(no docstring)_ |
| `SolverTests::test_fewer_iterations_than_fixed_45` | — | _legacy_ | _(no docstring)_ |

## `parity`

### `parity/test_engine_parity.py`

| Case | Portfolio | Covers | Summary |
| --- | --- | --- | --- |
| `ParityTest::test_scenarios_match` | — | _legacy_ | _(no docstring)_ |
| `ParityTest::test_stress_scenarios_match_js` | — | _legacy_ | Market Correction 3-phase (Downfall + Broad/Stock Bounce-Back) matches 1:1 between Python and JS across all presets & custom. |
| `ParityTest::test_hypothetical_asset_and_scheduled_actions_match_js` | — | _legacy_ | Phase D Stage 3: $0-start Hypothetical bucket + Transfer + Recurring Deposit/Withdrawal + On-Vest match 1:1. |
| `ParityTest::test_browser_release_examples` | — | _legacy_ | The rendered JS independently matches January RMDs, tax, lot and calendar examples. |
| `ParityTest::test_optimizer_matches` | — | _legacy_ | O1 + DA-C04: Python and JS Auto-Optimizer return identical liquidation orders across all 4 simulation lenses. |
| `ParityTest::test_saved_equity_half_life_matches_browser` | — | _legacy_ | Compare fast, medium, slow, and Flat (31) two-stage decay across single_path, stress, range (Normal & Historical), and historical cohorts. |
| `ParityTest::test_historical_cohorts_match_js` | — | _legacy_ | A3 Historical tab: deterministic 4-risk-class cohort replay matches 1:1 between Python and JS. |
| `ParityTest::test_monte_carlo_zero_vol_matches_js` | — | _legacy_ | With volatility=0 across risk classes, MC paths are degenerate and match single-path to the cent. |
| `ParityTest::test_monte_carlo_nonzero_vol_matches_js` | — | _legacy_ | Phase D Stage 4: Shared 32-bit mulberry32 + Box-Muller PRNG (`seed=1`) produces 1:1 non-zero-vol MC parity. |
| `ParityTest::test_scheduled_withdrawal_adds_pre_post_event_checkpoints_in_mc_and_cohorts` | — | _legacy_ | Non-monthly scheduled actions add pre-(m-1) and post-(m) checkpoints in MC and Historical cohorts. |

## `render`

### `render/test_dirty_state.py`

| Case | Portfolio | Covers | Summary |
| --- | --- | --- | --- |
| `DirtyStateAuditTest::test_view_vs_calculation_dirty_state` | — | _legacy_ | _(no docstring)_ |
| `DirtyStateAuditTest::test_reload_scenario_revert_and_ui` | — | _legacy_ | Verifies that Ctrl-R / Cmd-R reverts modifications, reloads scenario, and updates left-nav status. |
| `DirtyStateAuditTest::test_historical_mode_dollar_toggle_updates_top_kpis` | — | _legacy_ | Toggling Nominal $ / Today's $ in Historical mode updates all 3 top KPI cards. |
| `DirtyStateAuditTest::test_mc_simulations_forced_500_on_old_saves_and_narrative` | — | _legacy_ | Old saves with non-500 mcSimulations load at 500 paths cleanly, slider is gone, narrative shows 500. |

### `render/test_pass3_ui_regressions.py`

| Case | Portfolio | Covers | Summary |
| --- | --- | --- | --- |
| `test_pass3_ui_regressions_in_rendered_bundle` | `p2_equity_comp_rsu` | `DA4-03` `DA4-07` `DA4-09` `DA4-10` `DA4-11` | Pass-3 UI regressions: reconnect retry, fee growth deduction, past-event badge, dirty-state protection, and AI author badge. |

### `render/test_render_structure.py`

| Case | Portfolio | Covers | Summary |
| --- | --- | --- | --- |
| `test_every_fixture__renders_a_page_with_an_embedded_payload` | — | _render_ | All six fixtures render, and the payload round-trips out of the HTML. |
| `test_rendered_page__loads_no_external_resources` | `p1_index_saver` | `SA-09` _render_ | The page is self-contained: no third-party script, style, font or image. |
| `test_rendered_page__contacts_no_third_party_host_at_runtime` | `p1_index_saver` | `SA-09` _render_ | No fetch target outside the local sidecar and the quote provider survives in the page. |
| `test_page_title__is_the_household_name_without_a_product_suffix` | `p4_trust_household` | `LL-23` _render_ | The browser tab names the household, not a hard-coded product line. |
| `test_hostile_holding_names__are_escaped_in_the_page` | `p5_adversarial` | `SA-11` _render_ | A holding whose name contains markup cannot inject an element or a handler. |
| `test_embedded_payload__cannot_break_out_of_its_script_block` | `p5_adversarial` | `SA-11` _render_ | `</` inside the embedded JSON is escaped, so the script element cannot be closed early. |
| `test_awkward_household_name__survives_round_trip` | `p5_adversarial` | _render_ | An apostrophe, an ampersand and a diacritic all survive into the payload. |
| `test_empty_household__renders_without_inventing_holdings` | `p0_empty_household` | _empty_state_ | A portfolio with no statements renders an empty state, not a fabricated one. |
| `test_rendered_page__payload_is_valid_json_and_matches_the_file` | — | _render_ | The embedded payload parses and carries the fixture's pinned `as_of` date. |
| `test_rendered_page__includes_collapsible_settings_sections` | `p4_trust_household` | _render_ | Portfolio and Family Members settings render uniform collapsible rows and status capsules. |

### `render/test_template_syntax.py`

| Case | Portfolio | Covers | Summary |
| --- | --- | --- | --- |
| `test_js_template__parses` | — | _render_ | Each JavaScript template is syntactically valid on its own. |
| `test_js_templates__are_all_discovered` | — | _render_ | The glob finds the templates, so a passing run is not a vacuous one. |
| `test_rendered_page__script_block_parses` | `p1_index_saver` | _render_ | The concatenated script block in a rendered page parses as a whole. |

## `server`

### `server/test_auth.py`

| Case | Portfolio | Covers | Summary |
| --- | --- | --- | --- |
| `test_health__is_reachable_without_a_token` | `p1_index_saver` | _server_auth_ | The liveness probe stays public so a launcher can wait for the port. |
| `test_dashboard__requires_a_token` | `p1_index_saver` | `SA-06` _server_auth_ | The dashboard itself is gated. |
| `test_privileged_routes__reject_requests_without_a_token` | `p1_index_saver` | `S1` _server_auth_ | Every reading and writing route answers 403 without the session token. |
| `test_privileged_route__accepts_the_session_token_header` | `p1_index_saver` | _server_auth_ | A local tool authenticates with the token from the 0600 file. |
| `test_session_token__is_long_and_random_enough` | `p1_index_saver` | _server_auth_ | The token is a 256-bit URL-safe value, not a predictable string. |
| `test_session_token_file__is_owner_only` | `p1_index_saver` | _server_auth_ | The token file is 0600, because anything that can read it is authenticated. |
| `test_bad_token__is_rejected` | `p1_index_saver` | `SA-20` _server_auth_ | A wrong token is not a valid token, however well formed. |
| `test_host_header__non_loopback__is_refused` | `p1_index_saver` | `S1` _server_auth_ | A request claiming a non-loopback Host is refused before anything else happens. |
| `test_host_header__loopback_names__are_accepted` | `p1_index_saver` | _server_auth_ | Both loopback spellings work, since the browser may use either. |
| `test_responses__carry_no_cors_headers` | `p1_index_saver` | `S1` _server_auth_ | No response grants cross-origin access, even when Origin: null or cross-port localhost is sent. |
| `test_responses__carry_the_hardening_headers` | `p1_index_saver` | _server_auth_ | Sniffing, referrer leakage and caching are all disabled. |
| `test_dashboard_bootstrap__hands_the_browser_a_strict_httponly_cookie` | `p1_index_saver` | _server_auth_ | Opening the dashboard with the launch token sets the session cookie, once. |
| `test_dashboard_bootstrap__wrong_token_in_the_url__sets_no_cookie` | `p1_index_saver` | _server_auth_ | A guessed token in the query string neither authenticates nor seeds a cookie. |
| `test_dashboard__with_a_valid_token_header__serves_the_page` | `p1_index_saver` | _server_auth_ | An authenticated request gets the rendered dashboard, not a redirect. |
| `test_options_preflight__returns_no_cors_allowances` | `p1_index_saver` | _server_auth_ | A preflight gets no permission, even for Origin: null or cross-port localhost (SA2-01). |
| `test_root_docs_and_sync_localhost__work_case_insensitively_and_seed_both_hosts` | `p1_index_saver` | _server_auth_ | Top-level repo docs (README.md, readme.md) are reachable without auth, and sync_localhost hops cleanly. |
| `test_occupied_port_does_not_open_a_browser` | `p1_index_saver` | `SA4-01` | An unrelated occupied port cannot cause browser navigation or publication of a session secret. |
| `test_browser_page_rejects_a_different_active_household` | `p1_index_saver` | `SA4-05` | Switching between two synthetic households rejects an old tab's state and renders a fresh page for the new household. |
| `test_running_instance_issues_a_one_use_link_without_a_restart` | `p1_index_saver` | `SA4-01` | A second launcher can sign in a fresh browser and the resulting cookie works on ordinary requests. |
| `test_legacy_browser_cookie_upgrades_on_a_normal_visit` | `p1_index_saver` | — | A returning browser exchanges its old cookie silently and reuses the signed replacement. |

### `server/test_pass2_audit.py`

| Case | Portfolio | Covers | Summary |
| --- | --- | --- | --- |
| `test_demo_mode__blocks_privacy_subroutes_and_protects_entity_registry` | `p3_retiree_annuities` | `SA3-01` | Demo Mode blocks `/api/privacy/*` subroutes, blocks entity clobbering, and only permits exit. |
| `test_demo_mode__blocks_static_statements_on_http` | `p3_retiree_annuities` | `SA3-02` | Static `/Statements/*` routes return 403 while Demo Mode is active. |
| `test_exported_payload__trims_all_birth_days_to_first_of_month` | `p3_retiree_annuities` | `SA3-05` | Exported `retirement_sim_assets[*].owner_birth_day` and investor DOBs never expose exact calendar days. |
| `test_privacy_translator__roundtrips_cash_and_acct_composite_ids_and_non_monetary_counts` | `p3_retiree_annuities` | `SA3-06` | `PrivacyTranslator` preserves `cash_<slug>` and `acct_<id>` round-trips and leaves cohort/tranche counts unscaled. |
| `test_demo_projector__scales_surrender_value_and_preserves_pct_of_source` | `p3_retiree_annuities` | `SA3-08` | `DemoTranslator` scales `surrender_value` and `*_usd` keys by `k` while keeping `pct_of_source` amounts unscaled. |
| `test_cash_sweep_buckets__preserve_cash_fixed_risk_class_and_sweep_yield` | `p3_retiree_annuities` | `DA2-01` | Retirement cash-sweep buckets (`cash_*`) retain `cash_fixed` risk class rather than equity growth. |
| `test_hypothetical_rollovers__honor_partial_amount_and_wrapper_tax_rules` | `p3_retiree_annuities` | `DA2-04` | Day-0 `hypothetical_rollovers` honor partial `amount`, 0% IRA-to-IRA rollovers, and ordinary Roth conversions. |
| `test_canonical_json_plugin__rejects_mismatched_reported_total` | `p3_retiree_annuities` | `DA2-13` | `CanonicalStatementJsonPlugin` raises `StatementParseError` when `reported_total` disagrees with positions. |

### `server/test_pass6_security.py`

| Case | Portfolio | Covers | Summary |
| --- | --- | --- | --- |
| `test_demo_projector__preserves_statutory_bend_points_and_weights_while_scaling_drawdown_usd` | `p2_equity_comp_rsu` | `SA5-01` `SA5-07` _demo_mode_ | Statutory SSA bend points and fractional constituent weights stay unscaled while new USD keys scale by k. |
| `test_demo_and_shield_translators__alias_fee_halflife_and_principal_override_keys` | `p2_equity_comp_rsu` | `SA5-06` `SA5-08` _privacy_shield_ | Composite keys in fee/half-life/principal dicts alias cleanly and Mode C leaves non-monetary D7 fields unscaled. |
| `test_holdings_cli_and_scope_recovery__fail_closed_on_corrupt_privacy_registry_and_project_demo` | `p2_equity_comp_rsu` | `SA5-02` _demo_mode_ | CLI demo projection honors saved demo_mode and both CLI and scope recovery fail closed on corrupt registries. |
| `test_advisor_jobs__scale_demo_text_isolate_demo_state_and_disambiguate_proposals` | `p2_equity_comp_rsu` | `SA5-03` `DA4-11` `DA4-08b` _scenarios_ | Advisor jobs scale inline USD in Demo Mode, isolate demo_enabled jobs, and disambiguate duplicate names. |
| `test_demo_mode__upgrades_mcp_chat_and_badge_shield_even_when_mode_is_off` | `p2_equity_comp_rsu` | `SA5-04` _privacy_shield_ | When Demo Mode is enabled, MCP, in-app chat, and portfolio-scoped badge calls force Mode C scaling. |
| `test_static_routes__block_cross_portfolio_statements_docs_and_snapshots_but_allow_assets` | `p2_equity_comp_rsu` | `SA5-05` _server_auth_ | Cross-portfolio static reads allow only Assets/* icons and reject Statements, Docs, and snapshots. |
| `test_cookie_mutations_and_demo_control_ui__enforce_portfolio_scope_and_read_only_lock` | `p2_equity_comp_rsu` | `SA5-09` _server_auth_ | Cookie-authenticated writes require X-AdvisorBot-Portfolio and Demo Mode blocks mutating control_live_ui calls. |
| `test_browser_tickets_and_artifact_permissions__fifo_cap_single_mint_and_chmod_0600` | `p2_equity_comp_rsu` | `SA5-10` _server_auth_ | Browser tickets cap at 64 FIFO, main() mints a single startup ticket, and badges/snapshots write 0600. |
| `test_stock_plan_parsers__read_canonical_grant_forecast_config_keys` | `p2_equity_comp_rsu` | `DA4-13` _ingest_ | Stock-plan quarterly PDF, unvested schedule PDF, and activity XLSX parsers honor cfg['grant_forecast']. |

### `server/test_portfolio_management.py`

| Case | Portfolio | Covers | Summary |
| --- | --- | --- | --- |
| `PortfoliosTest::test_legacy_project_migration_to_portfolios_and_data_config` | — | _legacy_ | _(no docstring)_ |
| `PortfoliosTest::test_portfolio_crud_and_http_routes_with_delete_confirmation` | — | _legacy_ | _(no docstring)_ |
| `PortfoliosTest::test_statement_refresh_isolation_between_portfolios` | — | _legacy_ | _(no docstring)_ |
| `PortfoliosTest::test_empty_state_render_and_ui_modal_js_contracts` | — | _legacy_ | _(no docstring)_ |
| `PortfoliosTest::test_reopening_editor_with_no_active_portfolio_keeps_saved_draft` | — | _legacy_ | Verifies the editor loads the edited portfolio's own config, not the empty no-active-portfolio response. |
| `PortfoliosTest::test_badge_generation_and_rotation_server` | — | _legacy_ | Tests that handle_generate_badge saves generated SVG and updates 10-badge rotation window. |
| `PortfoliosTest::test_saved_prompts_lifecycle_and_migration` | — | _legacy_ | Verifies default pre-population, CRUD persistence (including empty []), and legacy migration. |
| `PortfoliosTest::test_prompts_ui_and_agent_dropdown_node_vm` | — | _legacy_ | Verifies Prompts settings UI and Agent dropdown behavior in rendered HTML via Node VM. |
| `PortfoliosTest::test_financial_statements_dry_run_and_status` | — | _legacy_ | Verifies statement parsers do dry runs emitting pass/fail signals to drive status capsules (Processed, Available, Unknown). |
| `PortfoliosTest::test_scenarios_card_keyboard_nav_and_active_cyan_ring` | — | _legacy_ | Verifies removal of 'Open in Simulator' button, active card gold ring + outer teal focus ring, and arrow+Enter navigation. |
| `PortfoliosTest::test_ai_chat_nav_button_selected_state` | — | _legacy_ | Verifies that the leftnav AI Chat button draws active/selected when open, and unselected when closed. |
| `PortfoliosTest::test_fresh_install_cwd_filepath_construction` | — | _legacy_ | Verifies that for a fresh install in any cwd, filepaths never refer to upstream folders. |
| `PortfoliosTest::test_annual_burn_portfolio_setting` | — | _legacy_ | Verifies Annual Burn Rate setting in portfolio settings, defaulting to $50K and overriding. |

### `server/test_round4_audit.py`

| Case | Portfolio | Covers | Summary |
| --- | --- | --- | --- |
| `test_unauthed_landing_and_reconnect__disclose_no_token_and_mint_no_cookie` | `p2_equity_comp_rsu` | `SA2-01` `DP-01` _server_auth_ | Unauthenticated landing HTML contains no session token and `/api/auth/reconnect` refuses unauthed callers. |
| `test_cookie_authenticated_mutations__reject_cross_port_and_plain_text_csrf` | `p2_equity_comp_rsu` | `SA2-15` `DP-01` _server_auth_ | Cookie-authenticated POST and DELETE routes reject cross-port Origins and form-encoded CSRF. |
| `test_snapshots_and_portfolio_assets__require_auth_and_respect_demo_mode` | `p2_equity_comp_rsu` | `SA2-02` _server_auth_ | Scenario snapshots and portfolio badges require auth and never serve raw assets in Demo Mode. |
| `test_translator__aliases_dynamic_investor_keys_sma_equities_and_stateless_composites` | `p2_equity_comp_rsu` | `SA2-03` `SA2-04` `SA2-07` _privacy_shield_ | Dynamic investor dict keys, individual equities, and Turn 2+ composite IDs round-trip cleanly. |
| `test_mode_c_scaling__preserves_share_price_identity_and_statutory_prose` | `p2_equity_comp_rsu` | `SA2-08` `DP-03` _privacy_shield_ | Mode C scales shares while keeping per-share price and statutory prose `$` constants intact. |
| `test_mcp_server__enforces_wire_leak_gate_and_preserves_enabled_on_sync` | `p2_equity_comp_rsu` | `SA2-06` _privacy_shield_ | MCP tool invocation sanitizes exceptions and `sync_privacy_registry` preserves `mcp_server.enabled`. |
| `test_demo_mode__scales_trajectory_bars_projects_gets_and_locks_mutations` | `p2_equity_comp_rsu` | `SA2-10` `SA2-11` `SA2-12` `SA2-13` `DP-02` _demo_mode_ | Demo Mode scales trajectory `totalVal`/`assetVals`, projects GET routes, and blocks writes & chat saves. |
| `test_frontend_tooltip_sanitizer_and_single_investor_guard` | `p2_equity_comp_rsu` | `SA2-14` `SA2-22` _render_ | `sanitizeTooltipHtml` strips XSS vectors in `[data-tip]`, and 1-investor portfolios set `INV1 = null`. |
| `test_protected_scenarios_and_markdown_link_schemes__are_enforced_server_side` | `p2_equity_comp_rsu` | `SA2-16` `SA2-17` _scenarios_ | Protected scenarios reject unconfirmed DELETE / name-matched overwrite, and markdown strips `javascript:` URIs. |
| `test_incomplete_portfolio__can_be_saved_and_edited_but_cannot_become_active` | `p2_equity_comp_rsu` | `SA2-21` `DP-04` _empty_state_ | Draft portfolios without required simulation fields report `is_complete: False` and block activation. |
| `test_ssn_model__requires_xml_statement_and_ignores_legacy_salary_summary_fields` | `p2_equity_comp_rsu`, `p3_retiree_annuities` | `LL-08` _social_security_ | Social Security PIA is `$0` without an uploaded `ssa.gov` XML statement and resolves `external_file` XMLs. |
| `test_demo_chat_response__preserves_configured_shield_mode` | `p2_equity_comp_rsu` | _privacy_shield_ | Demo Mode chat responses do not report the internal Mode C scaling override as `privacy_shield`. |
| `test_demo_mode_exit__succeeds_idempotently_from_demo_scoped_tab` | `p2_equity_comp_rsu` | _privacy_shield_ | Exiting Demo Mode from a Demo-scoped browser tab succeeds even if Demo Mode already flipped off. |

### `server/test_scenario_index.py`

| Case | Portfolio | Covers | Summary |
| --- | --- | --- | --- |
| `ModelsIndexTest::test_legacy_saves_migration_rename_notes_and_upvote` | — | _legacy_ | _(no docstring)_ |
| `ModelsIndexTest::test_protected_saves_guardrails_in_llm_tools` | — | _legacy_ | _(no docstring)_ |
| `ModelsIndexTest::test_scenario_terminology_in_rendered_html` | — | _legacy_ | _(no docstring)_ |
| `ModelsIndexTest::test_scenario_nav_button_status_updates_in_js` | — | _legacy_ | _(no docstring)_ |
| `ModelsIndexTest::test_scenario_detail_holding_overrides_bar_chart` | — | _legacy_ | _(no docstring)_ |
| `ModelsIndexTest::test_scenario_detail_load_jumps_to_retirement_simulator` | — | _legacy_ | Clicking Load in scenario details jumps to Retirement Simulator only when load succeeds. |

### `server/test_security_regressions.py`

| Case | Portfolio | Covers | Summary |
| --- | --- | --- | --- |
| `TestShieldFailClosedAndLeakControls::test_sa02_inapp_shield_and_mcp_fail_closed` | — | _legacy_ | _(no docstring)_ |
| `TestShieldFailClosedAndLeakControls::test_sa03_primary_symbol_scrub_and_tool_names` | — | _legacy_ | _(no docstring)_ |
| `TestShieldFailClosedAndLeakControls::test_sa04_mode_c_scales_all_monetary_keys_and_preserves_rates` | — | _legacy_ | _(no docstring)_ |
| `TestShieldFailClosedAndLeakControls::test_sa05_no_dangerously_skip_permissions` | — | _legacy_ | _(no docstring)_ |
| `TestShieldFailClosedAndLeakControls::test_sa10_generate_text_with_model_respects_shield` | — | _legacy_ | _(no docstring)_ |
| `TestShieldFailClosedAndLeakControls::test_sa12_mcp_honors_privacy_shield_mode` | — | _legacy_ | _(no docstring)_ |
| `TestShieldFailClosedAndLeakControls::test_sa14_destructive_tools_blocked_without_confirmation` | — | _legacy_ | _(no docstring)_ |
| `TestShieldFailClosedAndLeakControls::test_sa16_gemini_uses_header_not_query_param` | — | _legacy_ | _(no docstring)_ |
| `TestShieldFailClosedAndLeakControls::test_sa17_portfolio_llm_precedence_and_endpoint_trust` | — | _legacy_ | _(no docstring)_ |
| `TestShieldFailClosedAndLeakControls::test_sa19_os_user_path_redaction_and_error_sanitization` | — | _legacy_ | _(no docstring)_ |
| `TestStaticAndServerSecurity::test_sa20_persistent_secret_and_explicit_rotation` | — | _legacy_ | _(no docstring)_ |
| `TestStaticAndServerSecurity::test_sa22_svg_sanitizer_strips_xss` | — | _legacy_ | _(no docstring)_ |
| `TestStaticAndServerSecurity::test_sa06_sa21_http_auth_gate_and_traversal` | — | _legacy_ | _(no docstring)_ |
| `TestDataMinimizationAndIngestHardening::test_sa07_demo_projection_masks_dob_paths_and_saves_dir` | — | _legacy_ | _(no docstring)_ |
| `TestDataMinimizationAndIngestHardening::test_sa08_account_masking_and_report_minimization` | — | _legacy_ | _(no docstring)_ |
| `TestDataMinimizationAndIngestHardening::test_sa08_view_model_dob_and_ssn_minimization` | — | _legacy_ | _(no docstring)_ |
| `TestDataMinimizationAndIngestHardening::test_sa13_sa18_xml_xxe_and_path_confinement` | — | _legacy_ | _(no docstring)_ |
| `TestDataMinimizationAndIngestHardening::test_sa18_zip_bomb_and_symlink_guards` | — | _legacy_ | _(no docstring)_ |
| `TestDataMinimizationAndIngestHardening::test_sa15_custom_plugin_opt_in_gate` | — | _legacy_ | _(no docstring)_ |
| `TestDataMinimizationAndIngestHardening::test_sa25_html_report_escaping_and_0600_perms` | — | _legacy_ | _(no docstring)_ |
| `TestDataMinimizationAndIngestHardening::test_sa26_cli_statement_redaction` | — | _legacy_ | _(no docstring)_ |

### `server/test_writes.py`

| Case | Portfolio | Covers | Summary |
| --- | --- | --- | --- |
| `test_scenarios__fixture_set_is_listed` | `p4_trust_household` | _scenarios_ | The generated scenarios are all visible, including the one with an apostrophe. |
| `test_scenario_create__then_appears_in_the_index` | `p4_trust_household` | _scenarios_ | A new scenario is written to disk and indexed. |
| `test_protected_scenario__cannot_be_deleted_through_the_api` | `p4_trust_household` | `I4` _scenarios_ | A protected baseline survives a delete request. |
| `test_scenario_delete__is_reversible` | `p4_trust_household` | `I4` _scenarios_ | An ordinary scenario is moved to the trash folder rather than unlinked. |
| `test_project_save__round_trips_through_the_api` | `p4_trust_household` | _scenarios_ | A settings change posted to `/api/project` is persisted to `project.json`. |
| `test_demo_mode__blocks_mutating_routes` | `p4_trust_household` | _demo_mode_ | With Demo Mode on, writes are refused so a live demonstration cannot corrupt data. |
| `test_ssa_parse__path_outside_the_portfolio__is_refused` | `p4_trust_household` | `SA-13` _privacy_shield_ | An authenticated caller still cannot read an arbitrary file through `file_path`. |
| `test_statement_audit__does_not_execute_a_non_opted_in_plugin` | `p4_trust_household`, `p5_adversarial` | `SA-15` _plugins_ | Listing statements never imports a custom plugin the portfolio has not allowed. |

## `llm`

### `llm/test_privacy_translator.py`

| Case | Portfolio | Covers | Summary |
| --- | --- | --- | --- |
| `PrivacyTranslatorTests::test_longest_match_boundaries_and_short_code_casing` | — | _legacy_ | _(no docstring)_ |
| `PrivacyTranslatorTests::test_d1_d2_composite_ids_dob_and_prompt2_roundtrip` | — | _legacy_ | _(no docstring)_ |
| `PrivacyTranslatorTests::test_dob_age_only_and_notes_strip_policies` | — | _legacy_ | _(no docstring)_ |
| `PrivacyTranslatorTests::test_identity_and_values_mode_scales_usd_and_shifts_birthdays` | — | _legacy_ | _(no docstring)_ |
| `ShieldChatBoundaryTests::test_d3_blocks_inapp_cli_and_remote_under_local_only` | — | _legacy_ | _(no docstring)_ |
| `ShieldChatBoundaryTests::test_no_agy_entries_and_multi_model_catalog_expansion` | — | _legacy_ | _(no docstring)_ |
| `ShieldChatBoundaryTests::test_gemini_api_roundtrip_aliases_egress_and_restores_tool_args_and_reply` | — | _legacy_ | _(no docstring)_ |
| `DemoProjectorTests::test_demo_personas_never_collide_with_real_names_and_hide_k` | — | _legacy_ | _(no docstring)_ |
| `PrivacyServerIntegrationTests::test_privacy_endpoints_and_demo_write_guard` | — | _legacy_ | _(no docstring)_ |
| `PrivacyServerIntegrationTests::test_demo_mode_retirement_goal_and_grant_id_masking` | — | _legacy_ | _(no docstring)_ |

### `llm/test_shield.py`

| Case | Portfolio | Covers | Summary |
| --- | --- | --- | --- |
| `test_fixture_default__is_identity_aliased` | `p4_trust_household` | _privacy_shield_ | A fresh portfolio starts with the shield on, not off. |
| `test_identity_aliased_chat__sends_no_registry_term_on_the_wire` | `p4_trust_household` | `SA-03` _privacy_shield_ | With the shield on, nothing recognisable about the household reaches the provider. |
| `test_wire_payload__carries_no_legacy_vocabulary` | `p4_trust_household` | `SA-03` _privacy_shield_ | No key or tool name on the wire names the original employer or its stock. |
| `test_shield__fails_closed_when_the_privacy_config_cannot_be_read` | `p4_trust_household` | `SA-02` _privacy_shield_ | A broken privacy config blocks the call instead of silently sending raw data. |
| `test_local_only_mode__refuses_a_cloud_endpoint` | `p4_trust_household` | _privacy_shield_ | Local-only means local: a remote base URL is rejected before any request. |
| `test_scaled_mode__multiplies_the_dollar_figures_on_the_wire` | `p4_trust_household` | `SA-04` _privacy_shield_ | In the scaled profile, the money the provider sees is not the household's money. |
| `test_cli_backend__is_blocked_by_default` | `p4_trust_household` | `SA-05` _privacy_shield_ | The workspace CLI agent cannot be driven from the in-app chat while the shield is on. |
| `test_alias_round_trip__restores_real_names_for_the_user` | `p4_trust_household` | _aliasing_ | The reply the user reads is de-aliased, so the membrane is invisible in the UI. |
| `test_model_list__never_includes_an_api_key` | `p4_trust_household` | _privacy_shield_ | The browser's model selector gets status, never credentials. |
| `test_shielded_tool_outputs__alias_dynamic_investor_keys_and_ignore_lowercase_meta` | `p4_trust_household` | _privacy_shield_ | Dict keys in reference_rates/milestones alias investor names and lowercase `_meta` does not trip `META`. |
| `test_mode_c_scaling__preserves_camelcase_ages_and_horizons` | `p4_trust_household` | _privacy_shield_ | Mode C value scaling never distorts camelCase age, year, or horizon setting keys. |
| `test_translator_prose_and_family_birthdays__no_false_code_alias_and_clean_restore` | `p4_trust_household` | _privacy_shield_ | Snake_case identifiers like `ms_month` stay intact while `family_birthdays` restores in all modes. |
| `test_sync_privacy_registry__respects_edited_synonym_deletions` | `p4_trust_household` | _privacy_shield_ | When `synonyms_edited` is set, re-syncing does not re-add manually removed synonyms. |
| `test_http_post_json__retries_transient_503_and_429_then_succeeds` | `p4_trust_household` | _privacy_shield_ | Transient 503/429 responses retry with backoff, while 400 fails immediately without retrying. |
| `test_gemini_chat__falls_back_to_healthy_model_on_timeout_or_503` | `p4_trust_household` | _privacy_shield_ | When the selected Gemini model times out in Round 1, chat automatically falls back to a healthy model. |
| `test_damaged_registry_blocks_egress_and_private_http` | `p4_trust_household` | `SA4-03` | A malformed policy on disk blocks a synthetic provider request and returns 503 on private HTTP routes. |
| `test_mcp_mode_matrix` | `p4_trust_household` | `SA4-04` | MCP requires explicit enablement and rejects Local Only even with the service enabled. |
| `test_switch_during_payload_build_discards_old_household` | `p4_trust_household` | `SA4-05` | A generation change during a pinned build prevents the old household from reaching the next cache read. |
| `test_advisor_review_waits_for_the_user_before_writing` | `p4_trust_household` | — | A completed review offers an optional action and applying the same proposal twice writes only once. |
| `test_overview_exposes_enriched_ssn_grants_and_evidence_under_mode_c` | `p4_trust_household`, `p2_equity_comp_rsu` | `SA-03` `SA-04` _privacy_shield_ | Enriched overview sections alias entities and scale only monetary/share quantities in Mode C. |
| `test_tool_declarations_document_simulation_and_live_ui_controls` | `p4_trust_household` | — | Tool declarations expose full simulation, override, liquidation, and view-patch controls. |
| `test_overview_normal_mode_cleartext_and_demo_mode_projection` | `p4_trust_household`, `p2_equity_comp_rsu` | `SA-03` `SA-04` `SA2-10` _privacy_shield_ | Normal mode returns clear-text unscaled data while Demo Mode projects all names and scales USD/shares. |
| `test_demo_mode_gemini_chat_no_pre_egress_block_unicode_and_dob_ym` | `p4_trust_household`, `p4_trust_household` | `SA-03` _privacy_shield_ | Demo Mode (upgrading Mode B -> Mode C) + Unicode stems & YYYY-MM DOBs pass Gemini pre-egress gate. |
| `test_unicode_person_stems_gemini_demo_mode_wire_clean` | `p4_trust_household`, `p5_adversarial` | `SA-03` _privacy_shield_ | Adversarial Unicode person stems (e.g., Håkon) in camelCase keys do not trip Gemini pre-egress. |
| `test_system_prompt_boilerplate_and_demo_pool_collision_scrubbed` | `p4_trust_household`, `p4_trust_household` | `SA-03` _privacy_shield_ | System prompt boilerplate is scrubbed and assign_demo_personas avoids codes/company/name collisions. |

## `scans`

### `scans/test_catalogue.py`

| Case | Portfolio | Covers | Summary |
| --- | --- | --- | --- |
| `test_cases_catalogue__is_up_to_date` | — | _payload_ | `CASES.md` matches the tests on disk. |
| `test_cases_catalogue__parses_every_test_file` | — | _payload_ | No test file is skipped by the catalogue because of a syntax error. |
| `test_readme__documents_every_tier_and_marker` | — | _payload_ | `README.md` names every tier and cross-cutting marker. |
| `test_readme__documents_every_fixture_portfolio` | — | _payload_ | Each fixture slug appears in the README, so the set is discoverable from one page. |

### `scans/test_fallback_audit.py`

| Case | Portfolio | Covers | Summary |
| --- | --- | --- | --- |
| `test_no_new_literal_fallback__has_been_introduced` | — | `LL-01` _payload_ | Every literal-fallback shape in the source is one the allowlist already records. |
| `test_fallback_allowlist__has_no_stale_entries` | — | _payload_ | Every allowlisted fallback still exists. |
| `test_fallback_patterns__each_compiles_and_matches_its_own_sample` | — | _payload_ | Every pattern in the audit is a working regex, proven against a sample it must match. |
| `test_literal_fallback_family__reports_its_current_state` | — | _payload_ | Prints how many literal fallbacks remain, so the number is visible in a run. |

### `scans/test_hygiene.py`

| Case | Portfolio | Covers | Summary |
| --- | --- | --- | --- |
| `test_test_tree__contains_no_account_number_shapes` | — | _payload_ | No committed test file carries a real-looking account or contract number. |
| `test_test_tree__contains_no_absolute_home_paths` | — | _payload_ | A committed path under /Users or /home names a machine and usually a person. |
| `test_test_code__has_no_cents_precise_dollar_literals` | — | _payload_ | A dollar amount written to the cent in a test body is almost always pasted real data. |
| `test_fixture_tree__matches_its_specs` | — | _payload_ | Regenerating every fixture reproduces exactly what is committed. |
| `test_every_fixture__has_a_generated_readme_that_names_its_spec` | — | _payload_ | Each fixture README is generated and stamped with the spec hash it came from. |
| `test_every_test__has_a_first_line_docstring` | — | _payload_ | Every test documents itself in one sentence. |
| `test_spec_schema_doc__documents_every_validated_key` | — | _payload_ | `fixtures/specs/SCHEMA.md` lists every key the validator accepts. |
| `test_no_test_reads_the_real_portfolios_directory` | — | _payload_ | No test file refers to the user's portfolio folder. |
| `test_application_and_docs__contain_no_pii_or_home_paths` | — | _payload_ | No application source file under `Scripts/` (outside `tests/`) or `Docs/` carries |
| `test_checkout__has_no_index_html_written_by_the_suite` | — | _payload_ | Running the suite leaves no rendered dashboard in the checkout. |

### `scans/test_portability.py`

| Case | Portfolio | Covers | Summary |
| --- | --- | --- | --- |
| `test_rendered_dashboard__leaks_no_new_legacy_term` | — | _render_ | A fixture renders no legacy term that the baseline does not already record. |
| `test_leak_baseline__is_well_formed_and_covers_every_fixture` | — | _render_ | The baseline names every fixture and only denylisted terms, so it cannot drift silently. |
| `test_tax_rule_text__names_no_other_jurisdiction` | — | _payload_ | A tax-rule sentence never names a state the household does not live in. |
| `test_examples_project_template__shares_no_vocabulary_with_the_denylist` | — | _render_ | The shipped example config is a synthetic household, not the original one. |
