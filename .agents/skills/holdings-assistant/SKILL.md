---
name: holdings-assistant
description: Run retirement simulations, goal-seek rates, query holdings and maturity dates, model tax-aware trade rollovers, manage save configurations, and sync changes in realtime to the Portfolio Tracker & Retirement Simulator web UI.
---

# Holdings Assistant Skill

Use this skill whenever the user asks questions about the Portfolio Tracker & Retirement Simulator, such as:
- Creating or comparing scenario configurations (`Portfolios/<active>/Scenarios/*.json`)
- Scheduling gifts/withdrawals for configured investors or beneficiaries in Today's (`today`) or Nominal (`nominal`) dollars
- Goal-seeking growth rates (`primary_equity`, `broad_equity`, `all_holdings`, or any holding `forecast_key`), annual burn (`simAnnualBurn`), or inflation (`simInflationPct`) to reach a target portfolio value at a milestone (`investor_1_real`, `investor_2_real`, `year_40_real`, or `<investor_id>_<age>_real`)
- Querying positions, accounts, tax basis, yields, or maturity/surrender dates
- Modeling Day-0 what-if position sales and rollovers between asset buckets (`simulator_assets`)
- Tuning settings or switching scenarios/screens in the live `index.html` browser window

## Workflow

1. **Prefer MCP Tools (`holdings-tools`)**:
   - `holdings__get_overview`: Get KPIs, Reference Values, investor/beneficiary birthdays, simulator asset bucket IDs (`simulator_assets`), Social Security summary (`ssn_model`), unvested grants (`unvested_grants`), and scenario list.
   - `holdings__query_positions`: Query holdings (pass `has_maturity=True` to list all holdings with maturity/surrender dates).
   - `holdings__run_simulation`: Run the 40-year monthly + 100-year solvency simulator with optional `base_save`, `use_ref_rates`, `overrides` (including `holdingForecasts[<key>]`, `holdingHalfLives`, `simOptimismMultiplier`, `simScheduledActions`, `simHypotheticalAssets`, `simMarriedFilingJointly`, and `simMinTaxBracketFloor`), and `rollovers`.
   - `holdings__compute_non_primary_growth`: Compute the value-weighted growth rate across diversified non-primary holdings while honoring Privacy Shield.
   - `holdings__solve_simulation`: Goal-seek a parameter (`solve_for`) to hit `target_value` at `target_metric` (`*_real` or `*_nominal`) across Single-Path, Early Crash, Monte Carlo (`P50`), or Historical Cohorts (`P50`).
   - `holdings__manage_saves`: List, get, create, load, upvote (appending a timestamped concurrence under `## Upvotes & Concurrences`), or delete scenario configs in `Portfolios/<active>/Scenarios/`. Each scenario has a user-controlled `is_protected` toggle (`🔒 Protected` / `🔓 Unprotected`); never overwrite or delete protected scenarios unless explicitly asked (`overwrite_existing=True` and `confirmed_by_user=True`).
   - `holdings__control_live_ui`: Push live `settings_patch` adjustments, `use_ref_rates` / `reoptimize_order` resets, `refresh_action` (`"primary_quote"`, `"reload_page"`), or `switch_screen` (`"models"`, `"portfolio"`, `"primary"`, `"retirement"`, `"income"`, `"settings"`) to the open `index.html` page (use `holdings__manage_saves(action="load", ...)` to load a saved scenario).

2. **CLI Fallback (`Scripts/holdings.py`)**:
   If MCP tools are unavailable in the current environment, run:
   ```bash
   python3 Scripts/holdings.py llm-overview
   python3 Scripts/holdings.py llm-positions --has-maturity
   python3 Scripts/holdings.py llm-sim --base-save "Default" ...
   python3 Scripts/holdings.py llm-solve --solve-for primary_equity ...
   python3 Scripts/holdings.py llm-saves list
   python3 Scripts/holdings.py llm-ui --tune '{"simAnnualBurn": 90000}'
   ```
