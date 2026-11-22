# AdvisorBot — AI Workspace Guide (`GEMINI.md`)

Welcome to the **AdvisorBot** workspace.
When `agy` is started from this directory (or invoked by the in-page AI Assistant drawer), use the **`holdings-tools`** MCP server (or `python3 Scripts/holdings.py llm-*` CLI commands) to answer portfolio questions, run simulations, goal-seek rates, manage save configurations, and update the live web UI in realtime.

## 1. Authoritative Tools (`holdings-tools` MCP Server)

Always prefer calling the `holdings-tools` MCP tools (or the equivalent `Scripts/holdings.py` CLI subcommands) rather than manually parsing SQLite or HTML:

1. **`holdings__get_overview`** (`python3 Scripts/holdings.py llm-overview`)
   - Returns current portfolio KPIs, live equity quote, Reference Values, configured investors & beneficiaries (`family_birthdays` from `Portfolios/<active>/project.json`), all retirement simulator asset buckets (`simulator_assets`), Social Security summary (`ssn_model`), unvested grants (`unvested_grants`), and saved configurations in `Portfolios/<active>/Scenarios/`.

2. **`holdings__query_positions`** (`python3 Scripts/holdings.py llm-positions [--has-maturity] [--account ...] [--category ...] [--search ...]`)
   - Queries individual positions across all accounts and annuity contracts.
   - Pass `has_maturity=True` (`--has-maturity`) to list all holdings with maturity / surrender-free dates sorted chronologically.

3. **`holdings__run_simulation`** (`python3 Scripts/holdings.py llm-sim ...`)
   - Runs the exact 480-month (40-year) + 100-year solvency Retirement Simulator matching `index.html`.
   - Supports:
     - `base_save`: e.g. `"Default"`, `"001"`, or any scenario name/ID from `holdings__get_overview`.
     - `use_ref_rates=True`: Seeds per-holding `holdingForecasts` (`{<forecast_key>: {growth_pct, risk_class, vol_pct, fee_pct}}`), resets `holdingHalfLives` (`{}`) and `simOptimismMultiplier` (`1.0`), and initializes `simAnnualBurn` and `simInflationPct` with the portfolio's Reference Values.
     - `overrides`: Dict of scenario overrides (`simActiveModel`, `simStressScenario`, `holdingForecasts`, `holdingHalfLives`, `simOptimismMultiplier`, `simAnnualBurn`, `simInflationPct`, `simMarriedFilingJointly`, `simMinTaxBracketFloor`, `simScheduledActions`, `simHypotheticalAssets`, etc.).
     - `rollovers`: Day-0 trade/rollover what-if scenarios, e.g. `[{"from_asset_id": "<src_bucket_id>", "to_asset_id": "<dst_bucket_id>", "sell_all": true, "pay_tax_from_proceeds": true}]`. Automatically computes realized capital gains, deducts LTCG tax from proceeds, and adds net proceeds with stepped-up cost basis to the destination bucket.

4. **`holdings__solve_simulation`** (`python3 Scripts/holdings.py llm-solve ...`)
   - Goal-seek bisection solver (~40 iterations, `<0.01` precision).
   - Solves for `primary_equity`, `broad_equity`, `all_holdings`, any `<forecast_key>`, `simAnnualBurn`, or `simInflationPct` to hit `target_value` at `target_metric` (`"investor_1_real"`, `"investor_2_real"`, `"year_40_real"`, or `"<investor_id>_<age>_real"` / `"_nominal"`).

5. **`holdings__manage_saves`** (`python3 Scripts/holdings.py llm-saves ...`)
   - Lists, inspects, creates, loads, upvotes, or deletes scenario files in `Portfolios/<active>/Scenarios/*.json`.
   - When `apply_to_live_ui=True` (default), newly created or loaded scenarios are immediately broadcast to any open `index.html` browser window.
   - `action="upvote"` appends a timestamped concurrence signature under `## Upvotes & Concurrences` in `notes_md` without overwriting settings.
   - **IMPORTANT RULE**: Each scenario carries a user-controlled `is_protected` toggle (`🔒 Protected` / `🔓 Unprotected`). Never overwrite or delete scenarios unless `overwrite_existing=True` and `confirmed_by_user=True` are explicitly confirmed by the user (`overwrite_existing=False` by default).

6. **`holdings__control_live_ui`** (`python3 Scripts/holdings.py llm-ui ...`)
   - Pushes realtime actions to the open `index.html` browser page via the local sidecar (`http://127.0.0.1:8765/api/ui/poll`):
     - `settings_patch`: Live-adjusts simulator settings, `holdingForecasts`, `holdingHalfLives`, `simOptimismMultiplier`, or `simScheduledActions` and redraws the charts immediately (disabled in read-only Demo Mode).
     - `use_ref_rates=True` / `reoptimize_order=True`: Resets active scenario inputs to Reference Values or re-runs liquidation order optimization (use `holdings__manage_saves(action="load", id=...)` to switch the active scenario).
     - `switch_screen`: Switches between `"models"`, `"portfolio"`, `"primary"`, `"retirement"`, `"income"`, or `"settings"`.
     - `refresh_action`: Triggers `"primary_quote"`, `"reload_page"`, or confirmed `"rebuild_site"`.

## 2. Portfolio Configuration & Private Data (`Portfolios/` & `Data/Config/`)

All private portfolio data lives in the gitignored `Portfolios/` directory (`Portfolios/_active.json` tracks the selected portfolio), while shared LLM provider configs live in the gitignored `Data/Config/LLM/` directory:
- `Portfolios/<active>/project.json`: Household/portfolio name, badge icon, investors (with birthdays, milestone ages, and Social Security `ssn` blocks), beneficiaries, account metadata, tax rates, `grant_forecast`, and retirement simulator bucket definitions (`sim_buckets`).
- `Portfolios/<active>/Statements/`: Raw financial institution PDF/XLSX/JSON statements (`Statements/SSN/` holds uploaded `ssa.gov` XML statements).
- `Portfolios/<active>/Scenarios/`: Saved simulation scenario configurations (`*.json`, `_index.json`, and `0600` `snapshots/*.png`).
- `Portfolios/<active>/portfolio.sqlite`: Owner-only (`0600`) local SQLite database of ingested statements, positions, anchors, and market prices storing real account and contract IDs for Live View (`DP-05`), paired with `0600` `privacy_registry.json` to project realistic personas in **Demo Mode** and `ACCT_N` / `CONTRACT_N` tokens under **AI Privacy Shield** (Modes B & C).
- `Data/Config/LLM/*.json` (and portfolio overrides in `Portfolios/<active>/LLM/*.json`): LLM provider API keys (`gemini.json`, `claude.json`, `openai.json`).
- `holdings__compute_non_primary_growth` (`python3 Scripts/holdings.py llm-non-primary-growth ...`): Analyzes value-weighted growth across diversified non-primary holdings while honoring the active Privacy Shield mode.

Always call `holdings__get_overview` at the start of a session to inspect the active portfolio's investors, beneficiaries, asset bucket IDs (`simulator_assets`), and saved scenarios.

## 3. Security & Privacy Coding Guardrails (Do's & Don'ts)

When editing Python or frontend code in this repository, preserve all security & privacy invariants:

- **Fail-Closed Privacy Shield & Leak Assert**: Route every cloud LLM call and MCP tool invocation through `_resolve_inapp_privacy_shield()` / `_check_mcp_access()` (automatically upgrading to `identity_and_values` scaled by `demo_mode.scale_factor` whenever Demo Mode is enabled, even if `ai_shield.mode == "off"`) and verify outbound payloads via `_assert_wire_leak_free()`. Never skip translation on error or pass raw account numbers/names/tickers in tool arguments or error text.
- **Mode C Allowlist Scaling**: In Mode C (`identity_and_values`), scale every numeric field *unless* its key is explicitly in the non-monetary allowlist (`_RAW_NON_MONETARY_KEY_EXACT`, `_NON_MONETARY_KEY_SUFFIXES`, etc.) — do not switch back to a monetary-only key allowlist, and never scale statutory SSA bend points (`bend1`, `bend2`) or `[0, 1]` constituent weights in `DemoTranslator`.
- **Credentials & File Permissions**: Pass Gemini API keys via `x-goog-api-key` headers (never URL `?key=` params). Keep `os.umask(0o077)` in `advisorbot_server.main()` and `mcp_server.py` and `0o600` permissions on tokens, LLM configs, SQLite DBs, `privacy_registry.json`, badges, scenario files/snapshots, and HTML exports.
- **Subprocess & Server Hygiene**: Never invoke `agy` or other CLI tools with `--dangerously-skip-permissions` or `shell=True`; pass untrusted prompts via `stdin` with `--disable-slash-commands`. Bind `ThreadingHTTPServer` to `127.0.0.1` *before* writing `Data/.session_token` (backed by persistent `0600` `Data/.session_secret`), require host + token/cookie + `X-AdvisorBot-Portfolio` scope headers on mutating routes, and confine cross-portfolio static reads strictly to `Portfolios/<id>/Assets/*`.
- **Frontend & Parser Hardening**: Escape dynamic HTML with `escHtml()`, use `data-*` delegated listeners instead of inline `onclick` attributes, never call third-party CORS proxies for market quotes, keep `DP-05` dynamic Demo Mode / Privacy Shield aliasing + `YYYY-MM` DOB trimming intact, and preserve XML/Zip/PDF size limits.
- **Portability & Zero Household Literals (`LL-01`–`LL-33`)**: Never hardcode household-specific tickers, employer award names, custodian/carrier branches, dated transfer or cashflow schedules, dollar basis literals, PIA amounts, or state tax rates as fallbacks in `Scripts/`. All portfolio-specific metadata must live in `Portfolios/<active>/project.json` or ingested statement tables, and both `pytest Scripts/tests/scans/test_fallback_audit.py` and `pytest Scripts/tests/scans/test_portability.py` must pass with zero unallowlisted hits.
- **Verify Against Isolated Fixtures**: Run `make test` (or `pytest -q`, which isolates `ADVISORBOT_ROOT` to synthetic fixture portfolios `p0_empty_household`–`p5_adversarial` and blocks external network calls); when running `render_check.mjs`, always point `ADVISORBOT_ROOT` at an isolated fixture root (`ADVISORBOT_ROOT=<fixture_root> node Scripts/tests/render_check.mjs <fixture_url>`) rather than a personal portfolio.
