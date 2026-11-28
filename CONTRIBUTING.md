# Contributing to AdvisorBot

Thank you for contributing to **AdvisorBot**! Because AdvisorBot handles sensitive personal financial modeling, all contributions must follow the repository's commit template, privacy/security invariants, and isolated fixture testing workflow.

---

## 1. Commit Message Template (`.gitmessage`)

AdvisorBot uses a standardized commit template stored at [`.gitmessage`](.gitmessage) in the repository root.

### Enable Locally

Run one of the following from the repository root so `git commit` pre-populates the template automatically:

```bash
# Recommended shortcut via Makefile
make commit-template

# Or directly via git config (local to this repository)
git config commit.template .gitmessage
```

> **Note:** Git does not automatically apply `.gitmessage` on `git clone` for security reasons—each contributor opts in once per clone. Lines beginning with `#` are instructional preambles that Git strips automatically when saving your commit. If you squash-merge or commit via the GitHub web UI, please follow the same `(A)`–`(E)` structure in the commit description.

### Required Sections

Every commit message starts with a concise imperative subject line (≤ 72 characters), followed by a blank line and these five sections:

1. **`(A) Classification:`** — Choose one: `Bug` | `Feature` | `Performance` | `Cosmetic` | `Doc/Test` | `Parsing`
2. **`(B) Severity:`** — Choose one: `Low` | `Medium` | `High`
   - `High`: Security or privacy regression, data loss, crash, or incorrect core financial/tax calculation.
   - `Medium`: Partial feature breakage, degraded UX, or notable performance impact.
   - `Low`: Minor polish, documentation update, or non-blocking improvement.
3. **`(C) Description:`** — Explain what changed and why, noting affected modules, parity considerations, or related issues/PRs.
4. **`(D) Privacy and Security Implications:`** — Describe any impact on Privacy Shield (Modes A/B/C), Demo Mode, session/host authentication, `0600` file permissions, PII/wire-leak checks, or LLM/MCP boundaries (write `None` with a brief rationale if none apply).
5. **`(E) Testing notes:`** — Record the exact test commands run and their outcomes (e.g., `make test`, `make test-security`, `make fixtures-check`, or Playwright/UI checks against fixture roots).

### Example Commit Message

```text
Enforce Demo Mode read-only guard on scenario upvote route

(A) Classification: Bug

(B) Severity: High

(C) Description:
Require Demo Mode guard checks on the scenario upvote handler in
Scripts/advisorbot_server.py so read-only demo sessions cannot mutate
saved scenario notes.

(D) Privacy and Security Implications:
Prevents unauthorized state mutation while Demo Mode is active; verified
fail-closed behavior in server security tests.

(E) Testing notes:
Ran `make test-tier T=server` and `make test-security`; all tests passed.
```

---

## 2. Engineering, Privacy & Portability Guardrails

Before submitting a pull request, verify your changes uphold AdvisorBot's non-negotiable invariants (see [Chapter 4.3 of the User & Developer Manual](Docs/ch4-developer-agent.html#adv-guardrails)):

- **Privacy Shield & Wire-Leak Gate**: Route all cloud LLM and MCP outputs through the active Privacy Shield mode (upgraded to Mode C `identity_and_values` in Demo Mode) and `_assert_wire_leak_free()`.
- **No Personal Data or Household Literals**: Never commit real account numbers, statements, `/Users/...` or `/home/...` paths, or household-specific tickers/rates/schedules (`LL-*` portability rules).
- **Safe Subprocesses, Permissions & Auth**: Keep `0o600` file permissions on private state/configs, bind the local server strictly to `127.0.0.1` with host + session token checks, escape dynamic HTML via `escHtml()`, and avoid `shell=True`.
- **Python ↔ JS Engine Parity**: Keep `Scripts/simulator.py` and `Scripts/exporters/templates/app_simulator.js` mathematically identical when editing simulation or tax logic.

---

## 3. Running Tests (Isolated Fixtures Only)

All automated tests run against synthetic fixture portfolios (`p0_empty_household` through `p5_adversarial`) in temporary directories and never read your `Portfolios/` folder or reach external networks. See [Scripts/tests/README.md](Scripts/tests/README.md) for full details.

```bash
# Install dev dependencies (includes pytest)
python -m pip install -e ".[mcp,ingest,ui,dev]"

# Run the default test suite (unit, parsers, pipeline, engine, server, llm, render, scans)
make test PYTHON=.venv/bin/python

# Run security/privacy regressions and fixture drift checks
make test-security PYTHON=.venv/bin/python
make fixtures-check PYTHON=.venv/bin/python

# Run the full suite including Python <-> JS parity and Playwright smoke tests
make test-full PYTHON=.venv/bin/python
```
