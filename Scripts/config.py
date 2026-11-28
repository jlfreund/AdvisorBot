#!/usr/bin/env python3
"""Central path & declarative project configuration loader for the Holdings pipeline."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date
from functools import wraps
import json
import math
import os
from pathlib import Path
import re
import shutil
import threading
from typing import Any

def _resolve_root() -> Path:
    """
    Resolve the canonical project root directory.
    Priority:
      1. ADVISORBOT_ROOT or HOLDINGS_ROOT environment variables.
      2. If current working directory (cwd) contains project files (Portfolios/, pyproject.toml, or Scripts/),
         use cwd.
      3. If invoked from within the Scripts/ directory, use cwd.parent.
      4. For any new user installation or empty workspace, all filepath construction is local to cwd,
         never referring to any upstream/parent folders.
    """
    env_root = os.environ.get("ADVISORBOT_ROOT") or os.environ.get("HOLDINGS_ROOT")
    if env_root:
        return Path(env_root).resolve()
    cwd = Path.cwd().resolve()
    if (cwd / "pyproject.toml").is_file() or (cwd / "Portfolios").is_dir() or (cwd / "Scripts").is_dir():
        return cwd
    if cwd.name == "Scripts" and ((cwd.parent / "pyproject.toml").is_file() or (cwd.parent / "Portfolios").is_dir()):
        return cwd.parent
    # Pure cwd-local for new installs (never refers to upstream folders)
    return cwd


def _app_install_dir() -> Path:
    """Directory holding the shipped application code and assets (the checkout), independent of the data root."""
    return Path(__file__).resolve().parent.parent


def _app_resource_dir(root: Path, name: str) -> Path:
    """
    Resolves a shipped-resource directory (`Scripts/`, `Assets/`, `Docs/`).

    Normally the data root and the install directory are the same folder, so this is just `root / name`.
    When the data root is a separate directory that does not ship the resource (a test fixture root, or a
    future split install), fall back to the checkout so prompts, badges and templates still resolve.
    """
    candidate = root / name
    if candidate.is_dir():
        return candidate
    fallback = _app_install_dir() / name
    return fallback if fallback.is_dir() else candidate


def _recompute_root_paths() -> None:
    """(Re)computes every root-derived module constant from the current environment."""
    global HOLDINGS_ROOT, ADVISORBOT_ROOT, SCRIPTS_DIR, DOCS_DIR, ASSETS_DIR, PROMPTS_DIR
    global DEFAULT_HTML_REPORT_PATH
    HOLDINGS_ROOT = _resolve_root()
    ADVISORBOT_ROOT = HOLDINGS_ROOT
    SCRIPTS_DIR = _app_resource_dir(HOLDINGS_ROOT, "Scripts")
    DOCS_DIR = _app_resource_dir(HOLDINGS_ROOT, "Docs")
    ASSETS_DIR = _app_resource_dir(HOLDINGS_ROOT, "Assets")
    PROMPTS_DIR = _app_resource_dir(HOLDINGS_ROOT, "Assets/prompts")
    DEFAULT_HTML_REPORT_PATH = HOLDINGS_ROOT / "index.html"


HOLDINGS_ROOT = _resolve_root()
ADVISORBOT_ROOT = HOLDINGS_ROOT
SCRIPTS_DIR = HOLDINGS_ROOT / "Scripts" if (HOLDINGS_ROOT / "Scripts").is_dir() else Path(__file__).resolve().parent
DOCS_DIR = HOLDINGS_ROOT / "Docs"
ASSETS_DIR = HOLDINGS_ROOT / "Assets"
PROMPTS_DIR = ASSETS_DIR / "prompts"
DEFAULT_HTML_REPORT_PATH = HOLDINGS_ROOT / "index.html"
_recompute_root_paths()

# Module-level constants in consumer modules that bind a *copy* of a config path at import time.
# `reload_root()` re-points them so an in-process root switch (used by the test harness and by
# `create_portfolio` on a fresh install) is visible to code that already imported them.
_DEPENDENT_PATH_BINDINGS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("advisorbot_server", ("DATA_DIR", "DEFAULT_CONFIG_PATH", "DEFAULT_DB_PATH", "DEFAULT_HTML_REPORT_PATH",
                           "PORTFOLIO_DIR", "SAVES_DIR")),
    ("holdings", ("DEFAULT_HTML_REPORT_PATH",)),
    ("llm_tools", ("HOLDINGS_ROOT",)),
    ("build_cohort_data", ("HOLDINGS_ROOT",)),
)

# Derived constants in consumer modules that are not simple copies of a config name.
_DEPENDENT_DERIVED: tuple[tuple[str, str, str], ...] = (
    ("advisorbot_server", "PROJECT_ROOT", "html_parent"),
    ("llm_tools", "PROJECT_ROOT", "root"),
)

# Caches in consumer modules that are keyed to the active root/portfolio.
_DEPENDENT_CACHE_HOOKS: tuple[tuple[str, str], ...] = (
    ("llm_tools", "invalidate_payload_cache"),
    ("settings", "invalidate_settings_cache"),
    ("advisorbot_server", "refresh_session_token_for_root"),
)


def _resync_dependent_modules() -> None:
    """Re-points already-imported consumer modules at the current root. Never imports a new module."""
    import sys

    for mod_name, attrs in _DEPENDENT_PATH_BINDINGS:
        mod = sys.modules.get(mod_name)
        if mod is None:
            continue
        for attr in attrs:
            if hasattr(mod, attr):
                setattr(mod, attr, globals()[attr])
    for mod_name, attr, kind in _DEPENDENT_DERIVED:
        mod = sys.modules.get(mod_name)
        if mod is None or not hasattr(mod, attr):
            continue
        value = Path(DEFAULT_HTML_REPORT_PATH).resolve().parent if kind == "html_parent" else HOLDINGS_ROOT
        setattr(mod, attr, value)
    for mod_name, fn_name in _DEPENDENT_CACHE_HOOKS:
        mod = sys.modules.get(mod_name)
        fn = getattr(mod, fn_name, None) if mod is not None else None
        if callable(fn):
            try:
                fn()
            except Exception:
                pass


def reload_root(new_root: Path | str | None = None) -> Path:
    """
    Re-resolves the project root and every path derived from it, in process.

    Pass `new_root` to point the process at a different data root (this sets `ADVISORBOT_ROOT` for
    child processes too); pass nothing to re-read the current environment. Root-derived constants in
    this module, the legacy active-portfolio constants (`refresh_active_paths`), the matching
    constants in already-imported consumer modules, and the root-keyed caches are all refreshed.

    Used by the test harness to run against fixture portfolios without touching `Portfolios/`.
    """
    if new_root is not None:
        os.environ["ADVISORBOT_ROOT"] = str(Path(new_root).resolve())
    _recompute_root_paths()
    refresh_active_paths()
    _resync_dependent_modules()
    return HOLDINGS_ROOT

_PORTFOLIOS_LOCK = threading.RLock()


def is_single_portfolio_override() -> bool:
    return bool(os.environ.get("PROJECT_DIR") or os.environ.get("PORTFOLIO_DIR"))


def get_root() -> Path:
    return _resolve_root()


def get_data_dir() -> Path:
    return Path(os.environ.get("HOLDINGS_DATA_DIR", str(get_root() / "Data"))).resolve()


def get_portfolios_dir() -> Path:
    return Path(os.environ.get("PORTFOLIOS_DIR", str(get_root() / "Portfolios"))).resolve()


def get_llm_dir() -> Path:
    return Path(os.environ.get("LLM_DIR", str(get_data_dir() / "Config" / "LLM"))).resolve()


def get_session_token_file() -> Path:
    return get_data_dir() / ".session_token"


def get_session_secret_file() -> Path:
    return get_data_dir() / ".session_secret"


def get_active_index_path() -> Path:
    return get_portfolios_dir() / "_active.json"


def slugify_portfolio_id(raw_name: str, existing: set[str] | None = None) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "_", str(raw_name or "").strip().lower()).strip("_")
    base = (s or "portfolio")[:48].strip("_") or "portfolio"
    used = {str(x).lower() for x in (existing or set())}
    if base not in used:
        return base
    n = 2
    while f"{base}_{n}" in used:
        n += 1
    return f"{base}_{n}"


def is_valid_portfolio_dir(p: Path) -> bool:
    if not p.is_dir() or p.name.startswith((".", "_")):
        return False
    return (
        (p / "project.json").is_file()
        or (p / "portfolio.sqlite").is_file()
        or (p / "Scenarios").is_dir()
        or (p / "Saves").is_dir()
        or (p / "Statements").is_dir()
    )


def normalize_badge_image_path(badge: str | None, portfolio_id: str | None) -> str:
    raw = str(badge or "").strip()
    if not raw or raw == "Assets/default_badge.svg":
        return "Assets/default_badge.svg"
    if raw.startswith(("data:image/", "http://", "https://", "Portfolios/")):
        return raw
    if portfolio_id and raw.startswith("Project/Assets/"):
        return f"Portfolios/{portfolio_id}/Assets/{raw[len('Project/Assets/'):]}"
    if portfolio_id and raw.startswith("Assets/"):
        return f"Portfolios/{portfolio_id}/{raw}"
    return raw


def normalize_badge_history(
    raw_history: Any,
    portfolio_id: str | None = None,
    current_badge: str | None = None,
    *,
    append_badge: str | None = None,
) -> list[str]:
    """Normalizes `badge_history` to `['Assets/default_badge.svg', ...]` capped at 9 custom entries."""
    default_badge = "Assets/default_badge.svg"
    candidates: list[str] = []
    if isinstance(raw_history, list) and raw_history:
        candidates.extend(str(b) for b in raw_history if b)
    elif current_badge:
        candidates.append(str(current_badge))

    custom: list[str] = []
    for item in candidates:
        norm = normalize_badge_image_path(item, portfolio_id) if portfolio_id is not None else str(item).strip()
        if norm and norm != default_badge and norm not in custom:
            custom.append(norm)

    if append_badge:
        norm_new = (
            normalize_badge_image_path(append_badge, portfolio_id)
            if portfolio_id is not None
            else str(append_badge).strip()
        )
        if norm_new and norm_new != default_badge and norm_new not in custom:
            custom.append(norm_new)

    return [default_badge] + custom[-9:]


def migrate_legacy_project_dir(
    legacy_dir: Path | None = None,
    portfolios_dir: Path | None = None,
    llm_dir: Path | None = None,
    data_dir: Path | None = None,
) -> str | None:
    """
    One-time non-destructive migration of legacy `Project/` into:
      - `Portfolios/<slug>/` for portfolio config, sqlite, Statements, Saves, Assets, Docs, Plugins
      - `Data/Config/LLM/` for LLM provider JSON configs
      - `Data/.session_token` for the sidecar session token
    Returns the migrated portfolio ID if a portfolio was migrated, else None.
    """
    if legacy_dir is None and is_single_portfolio_override():
        return None
    src = (legacy_dir or (HOLDINGS_ROOT / "Project")).resolve()
    if not src.exists() or not src.is_dir():
        return None

    dst_portfolios = (portfolios_dir or get_portfolios_dir()).resolve()
    dst_llm = (llm_dir or get_llm_dir()).resolve()
    dst_data = (data_dir or get_data_dir()).resolve()

    with _PORTFOLIOS_LOCK:
        # 1. Migrate LLM configs -> Data/Config/LLM/
        legacy_llm = src / "LLM"
        if legacy_llm.is_dir():
            dst_llm.mkdir(parents=True, exist_ok=True)
            for item in sorted(legacy_llm.iterdir()):
                if item.is_file():
                    target = dst_llm / item.name
                    if not target.exists():
                        shutil.move(str(item), str(target))
                    else:
                        try:
                            item.unlink()
                        except Exception:
                            pass
            try:
                if not any(legacy_llm.iterdir()):
                    legacy_llm.rmdir()
            except Exception:
                pass

        # 2. Migrate .session_token -> Data/.session_token
        legacy_tok = src / ".session_token"
        if legacy_tok.is_file():
            dst_data.mkdir(parents=True, exist_ok=True)
            tok_dst = dst_data / ".session_token"
            if not tok_dst.exists():
                shutil.move(str(legacy_tok), str(tok_dst))
            else:
                try:
                    legacy_tok.unlink()
                except Exception:
                    pass

        # 3. Check if any portfolio data remains in legacy Project/
        has_portfolio_data = any(
            (src / name).exists()
            for name in ("project.json", "portfolio.sqlite", "Statements", "Saves", "Assets", "Docs", "Plugins")
        )
        if not has_portfolio_data:
            return None

        dst_portfolios.mkdir(parents=True, exist_ok=True)
        existing_ids = {p.name.lower() for p in dst_portfolios.iterdir() if p.is_dir()}
        hh_name = "My Portfolio"
        cfg_file = src / "project.json"
        if cfg_file.is_file():
            try:
                raw_cfg = json.loads(cfg_file.read_text(encoding="utf-8"))
                if isinstance(raw_cfg, dict) and raw_cfg.get("household_name"):
                    hh_name = str(raw_cfg["household_name"]).strip()
            except Exception:
                pass

        slug = slugify_portfolio_id(hh_name, existing=existing_ids)
        target_pdir = dst_portfolios / slug
        target_pdir.mkdir(parents=True, exist_ok=True)

        move_names = (
            "project.json",
            "portfolio.sqlite",
            "portfolio.sqlite-wal",
            "portfolio.sqlite-shm",
            "Statements",
            "Saves",
            "Assets",
            "Docs",
            "Plugins",
            "llm_config.json",
        )
        for nm in move_names:
            sp = src / nm
            dp = target_pdir / nm
            if sp.exists() and not dp.exists():
                shutil.move(str(sp), str(dp))

        # Rewrite Project/ paths inside migrated project.json
        migrated_cfg_path = target_pdir / "project.json"
        if migrated_cfg_path.is_file():
            try:
                cfg_obj = json.loads(migrated_cfg_path.read_text(encoding="utf-8"))
                if isinstance(cfg_obj, dict):
                    cfg_obj["badge_image"] = normalize_badge_image_path(cfg_obj.get("badge_image"), slug)
                    for inv in cfg_obj.get("investors") or []:
                        if isinstance(inv, dict) and isinstance(inv.get("ssn"), dict):
                            ext_f = str(inv["ssn"].get("external_file") or "")
                            if ext_f.startswith("Project/"):
                                inv["ssn"]["external_file"] = f"Portfolios/{slug}/{ext_f[len('Project/'):]}"
                    save_config(cfg_obj, migrated_cfg_path)
            except Exception:
                pass

        # Remove leftover log files and empty Project/ directory if possible
        for log_nm in ("advisorbot_server.log", "advisorbot_server.err", "saves_server.log", "saves_server.err", ".DS_Store"):
            lp = src / log_nm
            if lp.is_file():
                try:
                    lp.unlink()
                except Exception:
                    pass
        try:
            if not any(src.iterdir()):
                src.rmdir()
        except Exception:
            pass

        idx_file = dst_portfolios / "_active.json"
        if not idx_file.exists():
            idx_file.write_text(json.dumps({"active_portfolio_id": slug}, indent=2) + "\n", encoding="utf-8")

        return slug


def _read_active_index_doc() -> dict[str, Any]:
    idx_file = get_active_index_path()
    if not idx_file.is_file():
        return {}
    try:
        raw = json.loads(idx_file.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return raw
    except Exception:
        pass
    return {}


def _read_active_index() -> tuple[bool, str | None]:
    raw = _read_active_index_doc()
    if not raw:
        idx_file = get_active_index_path()
        return (False, None) if not idx_file.is_file() else (False, None)
    aid = raw.get("active_portfolio_id")
    return True, (str(aid).strip() if aid else None)


def _write_active_index(portfolio_id: str | None, *, clear_last: bool = False) -> None:
    pdir = get_portfolios_dir()
    pdir.mkdir(parents=True, exist_ok=True)
    idx_file = get_active_index_path()
    prev_doc = _read_active_index_doc()
    prev_last = str(prev_doc.get("last_active_portfolio_id") or "").strip() or None
    if portfolio_id:
        last_id: str | None = str(portfolio_id).strip()
    elif clear_last:
        last_id = None
    else:
        last_id = prev_last
    payload = {
        "active_portfolio_id": portfolio_id,
        "last_active_portfolio_id": last_id,
    }
    tmp = idx_file.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(idx_file)


def get_last_active_portfolio_id() -> str | None:
    raw = _read_active_index_doc()
    lid = str(raw.get("last_active_portfolio_id") or "").strip()
    return lid or None


def recover_last_active_portfolio(
    preferred_id: str | None = None,
    *,
    allow_single_fallback: bool = True,
) -> str | None:
    """
    Restores `_active.json` to the last opened valid portfolio (or explicit `preferred_id`)
    when `active_portfolio_id` is currently None. Returns the recovered ID or None.
    """
    if is_single_portfolio_override():
        return Path(os.environ.get("PROJECT_DIR") or os.environ["PORTFOLIO_DIR"]).resolve().name
    with _PORTFOLIOS_LOCK:
        cur = get_active_portfolio_id()
        if cur:
            return cur
        pdir = get_portfolios_dir()
        if not pdir.is_dir():
            return None

        def _is_complete_candidate(pid_val: str | None) -> bool:
            if not pid_val:
                return False
            cand_dir = pdir / pid_val
            if not is_valid_portfolio_dir(cand_dir):
                return False
            try:
                cfg = load_config(cand_dir / "project.json", portfolio_id=pid_val)
                return bool(validate_portfolio_config(cfg).get("valid"))
            except Exception:
                return False

        for candidate in (preferred_id, get_last_active_portfolio_id()):
            clean = str(candidate or "").strip()
            if _is_complete_candidate(clean):
                _write_active_index(clean)
                refresh_active_paths()
                return clean

        if allow_single_fallback:
            complete_candidates = sorted(
                p.name for p in pdir.iterdir()
                if is_valid_portfolio_dir(p) and _is_complete_candidate(p.name)
            )
            if complete_candidates:
                chosen = "default" if "default" in complete_candidates else complete_candidates[0]
                _write_active_index(chosen)
                refresh_active_paths()
                return chosen
        return None


_portfolio_context = ContextVar("portfolio_context")


@contextmanager
def portfolio_context(portfolio_id: str | None):
    token = _portfolio_context.set(portfolio_id)
    try:
        yield
    finally:
        _portfolio_context.reset(token)


def with_active_portfolio(func):
    """Resolve all nested data and privacy lookups against the same household."""
    @wraps(func)
    def wrapped(*args, **kwargs):
        with portfolio_context(get_active_portfolio_id()):
            return func(*args, **kwargs)
    return wrapped


def get_active_portfolio_id() -> str | None:
    try:
        return _portfolio_context.get()
    except LookupError:
        pass
    if is_single_portfolio_override():
        return Path(os.environ.get("PROJECT_DIR") or os.environ["PORTFOLIO_DIR"]).resolve().name
    with _PORTFOLIOS_LOCK:
        legacy_dir = HOLDINGS_ROOT / "Project"
        if legacy_dir.is_dir() and not os.environ.get("PORTFOLIOS_DIR"):
            migrate_legacy_project_dir()
        pdir = get_portfolios_dir()
        has_idx, aid = _read_active_index()
        if has_idx:
            if not aid:
                return None
            if is_valid_portfolio_dir(pdir / aid):
                return aid
            # Stored active portfolio vanished; reset index cleanly
            _write_active_index(None)
            return None
        if not pdir.is_dir():
            return None
        candidates = sorted([p.name for p in pdir.iterdir() if is_valid_portfolio_dir(p)])
        if not candidates:
            return None
        complete_candidates = [
            cname
            for cname in candidates
            if validate_portfolio_config(load_config(pdir / cname / "project.json", portfolio_id=cname))["valid"]
        ]
        if not complete_candidates:
            _write_active_index(None)
            return None
        chosen = "default" if "default" in complete_candidates else complete_candidates[0]
        _write_active_index(chosen)
        return chosen


def get_portfolio_dir(portfolio_id: str | None = None) -> Path | None:
    if is_single_portfolio_override():
        return Path(os.environ.get("PROJECT_DIR") or os.environ["PORTFOLIO_DIR"]).resolve()
    pid = portfolio_id if portfolio_id is not None else get_active_portfolio_id()
    if not pid:
        return None
    root = get_portfolios_dir().resolve()
    target = (root / str(pid).strip()).resolve()
    if target.parent != root:
        return None
    return target


def get_statements_dir(portfolio_id: str | None = None) -> Path | None:
    p = get_portfolio_dir(portfolio_id)
    return (p / "Statements") if p is not None else None


def get_scenarios_dir(portfolio_id: str | None = None) -> Path | None:
    p = get_portfolio_dir(portfolio_id)
    if p is None:
        return None
    scenarios = p / "Scenarios"
    saves = p / "Saves"
    if not scenarios.exists() and saves.is_dir():
        try:
            shutil.move(str(saves), str(scenarios))
            return scenarios
        except Exception:
            return saves
    return scenarios if scenarios.is_dir() else (saves if saves.is_dir() else scenarios)


def get_saves_dir(portfolio_id: str | None = None) -> Path | None:
    return get_scenarios_dir(portfolio_id)


def get_snapshots_dir(portfolio_id: str | None = None) -> Path | None:
    s = get_scenarios_dir(portfolio_id)
    return (s / "snapshots") if s is not None else None


def get_plugins_dir(portfolio_id: str | None = None) -> Path | None:
    p = get_portfolio_dir(portfolio_id)
    return (p / "Plugins") if p is not None else None


def get_default_db_path(portfolio_id: str | None = None) -> Path | None:
    p = get_portfolio_dir(portfolio_id)
    return (p / "portfolio.sqlite") if p is not None else None


def get_default_config_path(portfolio_id: str | None = None) -> Path | None:
    p = get_portfolio_dir(portfolio_id)
    return (p / "project.json") if p is not None else None


def refresh_active_paths() -> None:
    """Syncs legacy module constants with the currently active portfolio."""
    global DATA_DIR, PORTFOLIOS_DIR, LLM_DIR, PROJECT_DIR, PORTFOLIO_DIR
    global STATEMENTS_DIR, SAVES_DIR, SCENARIOS_DIR, SNAPSHOTS_DIR, DEFAULT_DB_PATH, DEFAULT_CONFIG_PATH
    DATA_DIR = get_data_dir()
    PORTFOLIOS_DIR = get_portfolios_dir()
    LLM_DIR = get_llm_dir()
    active_dir = get_portfolio_dir()
    base = active_dir if active_dir is not None else PORTFOLIOS_DIR
    PROJECT_DIR = base
    PORTFOLIO_DIR = base
    STATEMENTS_DIR = base / "Statements"
    SCENARIOS_DIR = base / "Scenarios" if (base / "Scenarios").is_dir() else (base / "Saves" if (base / "Saves").is_dir() else base / "Scenarios")
    SAVES_DIR = SCENARIOS_DIR
    SNAPSHOTS_DIR = SCENARIOS_DIR / "snapshots"
    DEFAULT_DB_PATH = base / "portfolio.sqlite"
    DEFAULT_CONFIG_PATH = base / "project.json"


DATA_DIR = get_data_dir()
PORTFOLIOS_DIR = get_portfolios_dir()
LLM_DIR = get_llm_dir()
PROJECT_DIR = PORTFOLIOS_DIR
PORTFOLIO_DIR = PORTFOLIOS_DIR
STATEMENTS_DIR = PORTFOLIOS_DIR / "Statements"
SCENARIOS_DIR = PORTFOLIOS_DIR / "Scenarios"
SAVES_DIR = SCENARIOS_DIR
SNAPSHOTS_DIR = SCENARIOS_DIR / "snapshots"
DEFAULT_DB_PATH = PORTFOLIOS_DIR / "portfolio.sqlite"
DEFAULT_CONFIG_PATH = PORTFOLIOS_DIR / "project.json"

PLANNING_PROMPT_SPECS: tuple[tuple[str, str], ...] = (
    ("monthly_spending", "How much can we spend each month?"),
    ("growth_sensitivity", "Which growth assumption matters most?"),
    ("gift_affordability", "Can we afford a gift or major purchase?"),
)

ADVISOR_PROMPT_FILES: dict[str, str] = {
    "preferred_forecast": "preferred_forecast.md",
    "portfolio_improvements": "portfolio_improvements.md",
    **{key: f"{key}.md" for key, _ in PLANNING_PROMPT_SPECS},
    "model_note_template": "model_note_template.md",
}

DEFAULT_SAVED_PROMPT_SPECS: tuple[tuple[str, str], ...] = (
    ("preferred_forecast", "Preferred Simulation Settings"),
    ("portfolio_improvements", "Recommended Portfolio Improvements"),
    *PLANNING_PROMPT_SPECS,
)


def load_default_advisor_prompts() -> dict[str, str]:
    """Load PII-free generic Advisor prompts & note template from git-tracked `Assets/prompts/`."""
    out: dict[str, str] = {}
    bundled_prompts = Path(__file__).resolve().parent.parent / "Assets" / "prompts"
    for key, fname in ADVISOR_PROMPT_FILES.items():
        fpath = PROMPTS_DIR / fname
        if not fpath.is_file() and bundled_prompts.is_dir():
            fpath = bundled_prompts / fname
        try:
            out[key] = fpath.read_text(encoding="utf-8").strip() if fpath.is_file() else ""
        except Exception:
            out[key] = ""
    return out


def default_saved_prompts() -> list[dict[str, str]]:
    """Return the pre-populated portfolio prompts sourced from `Assets/prompts/`."""
    defaults = load_default_advisor_prompts()
    return [
        {
            "id": key,
            "title": title,
            "prompt": defaults.get(key, ""),
        }
        for key, title in DEFAULT_SAVED_PROMPT_SPECS
    ]


def sanitize_saved_prompts(raw_list: Any) -> list[dict[str, str]]:
    """Sanitize a user-managed list of saved prompt dicts `{id, title, prompt}`."""
    if not isinstance(raw_list, list):
        return []
    out: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for idx, item in enumerate(raw_list):
        if not isinstance(item, dict):
            continue
        title_s = re.sub(r"[\x00-\x1f]+", " ", str(item.get("title") or "")).strip()[:120]
        prompt_s = str(item.get("prompt") or "")
        raw_id = re.sub(r"[^a-z0-9_]+", "_", str(item.get("id") or "").strip().lower()).strip("_")
        if not raw_id:
            base = re.sub(r"[^a-z0-9]+", "_", title_s.lower()).strip("_") or f"prompt_{idx + 1}"
            raw_id = base[:48]
        cand = raw_id
        suffix = 2
        while cand in seen_ids:
            cand = f"{raw_id}_{suffix}"
            suffix += 1
        seen_ids.add(cand)
        out.append(
            {
                "id": cand,
                "title": title_s or f"Prompt {len(out) + 1}",
                "prompt": prompt_s,
            }
        )
    return out


def normalize_saved_prompts(cfg: dict[str, Any] | None = None) -> list[dict[str, str]]:
    """Upgrade older nonempty lists once, preserving custom entries and intentional deletions."""
    if isinstance(cfg, dict) and isinstance(cfg.get("saved_prompts"), list):
        saved = sanitize_saved_prompts(cfg["saved_prompts"])
        if saved and "saved_prompts_version" not in cfg:
            existing_ids = {entry["id"] for entry in saved}
            defaults = load_default_advisor_prompts()
            saved.extend(
                {"id": key, "title": title, "prompt": defaults[key]}
                for key, title in PLANNING_PROMPT_SPECS if key not in existing_ids
            )
        return saved
    defaults = load_default_advisor_prompts()
    legacy = (cfg.get("advisor_prompts") or {}) if isinstance(cfg, dict) and isinstance(cfg.get("advisor_prompts"), dict) else {}
    out: list[dict[str, str]] = []
    for key, title in DEFAULT_SAVED_PROMPT_SPECS:
        ov_val = str(legacy.get(key) or "").strip()
        out.append(
            {
                "id": key,
                "title": title,
                "prompt": ov_val if ov_val else defaults.get(key, ""),
            }
        )
    return out


def resolve_advisor_prompts(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """Merge git-tracked `Assets/prompts/` defaults with `saved_prompts` / legacy `advisor_prompts`."""
    defaults = load_default_advisor_prompts()
    raw_ov = (cfg or {}).get("advisor_prompts")
    saved_map = {p["id"]: p["prompt"] for p in normalize_saved_prompts(cfg) if p.get("id")}
    has_saved_list = isinstance(cfg, dict) and isinstance(cfg.get("saved_prompts"), list)
    overrides: dict[str, str] = {}
    effective: dict[str, str] = {}
    has_override = False
    for key in ADVISOR_PROMPT_FILES:
        if has_saved_list and key in saved_map:
            val_s = str(saved_map.get(key) or "").strip()
            ov_val = val_s if (val_s and val_s != defaults.get(key, "").strip()) else ""
            effective[key] = val_s or defaults.get(key, "")
        else:
            ov_val = str((raw_ov or {}).get(key) or "").strip() if isinstance(raw_ov, dict) else ""
            effective[key] = ov_val if ov_val else defaults.get(key, "")
        overrides[key] = ov_val
        if ov_val:
            has_override = True
    return {
        "defaults": defaults,
        "overrides": overrides,
        "effective": effective,
        "has_override": has_override,
    }


def _make_state_tax_preset(code: str, name: str, state_rate: float) -> dict[str, Any]:
    sr = round(float(state_rate), 6)
    return {
        "code": code,
        "name": name,
        "label": f"{name} ({sr * 100:g}%)" if sr > 0 else f"{name} (0%)",
        "state_rate": sr,
        "rate": sr,
        "fed_ltcg_top": 0.20,
        "niit_rate": 0.038,
        "fed_ordinary_top": 0.37,
        "ltcg_top": round(0.20 + 0.038 + sr, 4),
        "ordinary_top": round(0.37 + 0.038 + sr, 4),
        "ira_ordinary_rate": round(0.37 + sr, 4),
    }


US_STATE_TAX_PRESETS: dict[str, dict[str, Any]] = {
    code: _make_state_tax_preset(code, name, rate)
    for code, name, rate in (
        ("FED", "Federal Only (No State Tax)", 0.0),
        ("NONE", "Federal Only (No State Tax)", 0.0),
        ("AL", "Alabama", 0.05),
        ("AK", "Alaska", 0.0),
        ("AZ", "Arizona", 0.025),
        ("AR", "Arkansas", 0.039),
        ("CA", "California", 0.133),
        ("CO", "Colorado", 0.044),
        ("CT", "Connecticut", 0.0699),
        ("DE", "Delaware", 0.066),
        ("DC", "District of Columbia", 0.1075),
        ("FL", "Florida", 0.0),
        ("GA", "Georgia", 0.0539),
        ("HI", "Hawaii", 0.11),
        ("ID", "Idaho", 0.05695),
        ("IL", "Illinois", 0.0495),
        ("IN", "Indiana", 0.03),
        ("IA", "Iowa", 0.038),
        ("KS", "Kansas", 0.0558),
        ("KY", "Kentucky", 0.04),
        ("LA", "Louisiana", 0.03),
        ("ME", "Maine", 0.0715),
        ("MD", "Maryland", 0.0575),
        ("MA", "Massachusetts", 0.09),
        ("MI", "Michigan", 0.0425),
        ("MN", "Minnesota", 0.0985),
        ("MS", "Mississippi", 0.044),
        ("MO", "Missouri", 0.047),
        ("MT", "Montana", 0.059),
        ("NE", "Nebraska", 0.052),
        ("NV", "Nevada", 0.0),
        ("NH", "New Hampshire", 0.0),
        ("NJ", "New Jersey", 0.1075),
        ("NM", "New Mexico", 0.059),
        ("NY", "New York", 0.109),
        ("NC", "North Carolina", 0.0425),
        ("ND", "North Dakota", 0.025),
        ("OH", "Ohio", 0.035),
        ("OK", "Oklahoma", 0.0475),
        ("OR", "Oregon", 0.099),
        ("PA", "Pennsylvania", 0.0307),
        ("RI", "Rhode Island", 0.0599),
        ("SC", "South Carolina", 0.062),
        ("SD", "South Dakota", 0.0),
        ("TN", "Tennessee", 0.0),
        ("TX", "Texas", 0.0),
        ("UT", "Utah", 0.0455),
        ("VT", "Vermont", 0.0875),
        ("VA", "Virginia", 0.0575),
        ("WA", "Washington", 0.0),
        ("WV", "West Virginia", 0.0482),
        ("WI", "Wisconsin", 0.0765),
        ("WY", "Wyoming", 0.0),
    )
}

_DOB_ISO_RE = re.compile(r"^\d{4}-\d{2}(?:-\d{2})?$")
_SAFE_BUCKET_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{0,80}$")

MAX_WORK_PLAN_ANNUAL_USD = 50_000_000.0
MAX_WORK_PLAN_MATCH_RATE_PCT = 200.0
MAX_WORK_PLAN_CONTRIB_ROWS = 10
VALID_WORK_CONTRIB_PACING = ("monthly", "q1_frontloaded")


def default_work_plan(milestone_age: int = 85, *, is_working: bool = False) -> dict[str, Any]:
    """Return a clean, PII-free default pre-retirement work plan for a family member."""
    ms_clamped = max(18, min(100, int(milestone_age or 85)))
    return {
        "is_working": bool(is_working),
        "default_retirement_age": min(65, ms_clamped),
        "annual_take_home": 0.0,
        "budget_answered": False,
        "covers_full_burn": False,
        "annual_budget_offset": 0.0,
        "contributions": [],
    }


def auto_retire_elapsed_work_plan(
    wp: dict[str, Any],
    birth_date: str | None,
    *,
    today_ym: str | None = None,
) -> dict[str, Any]:
    """
    Transition `wp["is_working"]` to `False` ("Not Working") once the calendar month
    reaches or passes the member's target retirement month (`YYYY-MM`).
    """
    if not isinstance(wp, dict) or not wp.get("is_working"):
        return wp
    b_str = str(birth_date or "").strip()
    if not _DOB_ISO_RE.match(b_str):
        return wp
    try:
        parts = b_str.split("-")
        b_yr = int(parts[0])
        b_mo = max(1, min(12, int(parts[1])))
        ret_age = int(wp.get("default_retirement_age") or 65)
        ret_ym = f"{b_yr + ret_age:04d}-{b_mo:02d}"
        cur_ym = str(today_ym or date.today().isoformat())[:7]
        if len(cur_ym) == 7 and ret_ym <= cur_ym:
            wp["is_working"] = False
    except (TypeError, ValueError, IndexError):
        pass
    return wp


def sanitize_work_plan(
    raw_wp: Any,
    *,
    milestone_age: int = 85,
    clamp_to_milestone: bool = False,
) -> dict[str, Any]:
    """
    Normalize a family member `work_plan` dict into its strict PII-free canonical schema.

    SECURITY INVARIANT (Work-Plan Zero Free-Text PII):
    - Only booleans, bounded ages, bounded non-negative USD floats, and internal simulator
      bucket IDs (`_SAFE_BUCKET_ID_RE`) are retained; all unknown keys (e.g. `employer_name`,
      `job_title`, notes) are unconditionally stripped.
    """
    ms_clamped = max(18, min(100, int(milestone_age or 85)))
    base = default_work_plan(ms_clamped)
    if not isinstance(raw_wp, dict):
        return base

    is_working = bool(raw_wp.get("is_working", False))
    try:
        raw_ret = raw_wp.get("default_retirement_age")
        ret_age = int(round(float(raw_ret))) if raw_ret is not None and not isinstance(raw_ret, bool) else base["default_retirement_age"]
    except (TypeError, ValueError):
        ret_age = base["default_retirement_age"]
    ret_age = max(18, min(100, ret_age))
    if clamp_to_milestone:
        ret_age = min(ret_age, ms_clamped)

    try:
        raw_take_home = float(raw_wp.get("annual_take_home") or 0.0)
        take_home_usd = (
            round(max(0.0, min(MAX_WORK_PLAN_ANNUAL_USD, raw_take_home)), 2)
            if math.isfinite(raw_take_home)
            else 0.0
        )
    except (TypeError, ValueError):
        take_home_usd = 0.0

    covers_full = bool(raw_wp.get("covers_full_burn", False))
    try:
        raw_offset = float(raw_wp.get("annual_budget_offset") or 0.0)
        offset_usd = round(max(0.0, min(MAX_WORK_PLAN_ANNUAL_USD, raw_offset)), 2) if math.isfinite(raw_offset) else 0.0
    except (TypeError, ValueError):
        offset_usd = 0.0
    if clamp_to_milestone and take_home_usd > 0.0 and offset_usd > take_home_usd:
        offset_usd = take_home_usd

    cleaned_contribs: list[dict[str, Any]] = []
    raw_contribs = raw_wp.get("contributions")
    if isinstance(raw_contribs, list):
        for row in raw_contribs[:MAX_WORK_PLAN_CONTRIB_ROWS]:
            if not isinstance(row, dict):
                continue
            target_id = str(row.get("target_asset_id") or "").strip()
            if not _SAFE_BUCKET_ID_RE.match(target_id):
                target_id = ""
            match_id = str(row.get("match_target_asset_id") or "").strip()
            if not _SAFE_BUCKET_ID_RE.match(match_id) or match_id == target_id:
                match_id = ""
            try:
                emp_raw = float(row.get("employee_annual") or 0.0)
                emp_usd = round(max(0.0, min(MAX_WORK_PLAN_ANNUAL_USD, emp_raw)), 2) if math.isfinite(emp_raw) else 0.0
            except (TypeError, ValueError):
                emp_usd = 0.0
            try:
                match_raw = float(row.get("employer_match_annual") or 0.0)
                match_usd = round(max(0.0, min(MAX_WORK_PLAN_ANNUAL_USD, match_raw)), 2) if math.isfinite(match_raw) else 0.0
            except (TypeError, ValueError):
                match_usd = 0.0
            try:
                mr_raw = float(row.get("match_rate_pct") or 0.0)
                mr_pct = round(max(0.0, min(MAX_WORK_PLAN_MATCH_RATE_PCT, mr_raw)), 2) if math.isfinite(mr_raw) else 0.0
            except (TypeError, ValueError):
                mr_pct = 0.0
            raw_pacing = str(row.get("pacing") or "monthly").strip().lower()
            pacing = raw_pacing if raw_pacing in VALID_WORK_CONTRIB_PACING else "monthly"
            catchup = bool(row.get("age_50_catchup", False))
            if not target_id and emp_usd <= 0.0 and match_usd <= 0.0 and not catchup:
                continue
            cleaned_contribs.append({
                "target_asset_id": target_id,
                "employee_annual": emp_usd,
                "employer_match_annual": match_usd,
                "match_rate_pct": mr_pct,
                "match_target_asset_id": match_id,
                "age_50_catchup": catchup,
                "pacing": pacing,
            })

    budget_answered = bool(
        raw_wp.get("budget_answered", False)
        or offset_usd > 0.0
        or covers_full
        or len(cleaned_contribs) > 0
    )

    return {
        "is_working": is_working,
        "default_retirement_age": ret_age,
        "annual_take_home": take_home_usd,
        "budget_answered": budget_answered,
        "covers_full_burn": covers_full,
        "annual_budget_offset": offset_usd,
        "contributions": cleaned_contribs,
    }


def resolve_family_members(
    cfg: dict[str, Any] | None,
    *,
    clamp_work_plan: bool = True,
) -> list[dict[str, Any]]:
    """
    Return the uniform list of household `family_members` from `cfg`.

    DESIGN INTENT (Uniform Family Member Model):
    - Every family member is governed uniformly by two independent flags:
      1. `work_plan.is_working` (`Working` vs `Not Working` — any member, including an adult child,
         may contribute to the household budget or targeted accounts without contributing SSN).
      2. `is_ssn_contributor` (`0..2` members may contribute Social Security retirement benefits).
    - `is_rmd_owner` may be assigned to ANY family member (exactly 1 across the household,
      defaulting to `family_members[0]`), independent of `is_ssn_contributor`.
    """
    if not isinstance(cfg, dict):
        return []
    inv_by_id = {
        str(inv.get("id") or inv.get("name") or "").strip().lower().replace(" ", "_"): inv
        for inv in (cfg.get("investors") or [])
        if isinstance(inv, dict)
    }
    ben_by_id = {
        str(ben.get("id") or ben.get("name") or "").strip().lower().replace(" ", "_"): ben
        for ben in (cfg.get("beneficiaries") or [])
        if isinstance(ben, dict)
    }

    out: list[dict[str, Any]] = []
    raw_fams = cfg.get("family_members")
    if isinstance(raw_fams, list) and len(raw_fams) > 0:
        ssn_count = 0
        for idx, mem in enumerate(raw_fams):
            if not isinstance(mem, dict):
                continue
            q = dict(mem)
            mid = str(q.get("id") or q.get("name") or f"member_{idx + 1}").strip().lower().replace(" ", "_")
            q["id"] = mid
            if not q.get("birth_date") and q.get("birthdate"):
                q["birth_date"] = q.pop("birthdate")
            try:
                ms_age = max(65, min(105, int(round(float(q.get("milestone_age", 85))))))
            except (TypeError, ValueError):
                ms_age = 85
            q["milestone_age"] = ms_age
            src_person = inv_by_id.get(mid) or ben_by_id.get(mid) or {}
            is_ssn = bool(q.get("is_ssn_contributor", mid in inv_by_id))
            if is_ssn:
                ssn_count += 1
                if ssn_count > 2:
                    is_ssn = False
            q["is_ssn_contributor"] = is_ssn
            q["is_rmd_owner"] = bool(q.get("is_rmd_owner", q.get("rmd_owner", False)))
            q.pop("rmd_owner", None)
            if not isinstance(q.get("ssn"), dict):
                q["ssn"] = dict(src_person.get("ssn") or {"fra_age": 67, "default_claim_age": 67, "external_file": ""})
            raw_wp = q.get("work_plan") if isinstance(q.get("work_plan"), dict) else src_person.get("work_plan")
            q["work_plan"] = sanitize_work_plan(raw_wp, milestone_age=ms_age, clamp_to_milestone=clamp_work_plan)
            out.append(q)
    else:
        ssn_count = 0
        for idx, inv in enumerate(cfg.get("investors") or []):
            if not isinstance(inv, dict):
                continue
            q = dict(inv)
            mid = str(q.get("id") or q.get("name") or f"investor_{idx + 1}").strip().lower().replace(" ", "_")
            q["id"] = mid
            if not q.get("birth_date") and q.get("birthdate"):
                q["birth_date"] = q.pop("birthdate")
            try:
                ms_age = max(65, min(105, int(round(float(q.get("milestone_age", 85))))))
            except (TypeError, ValueError):
                ms_age = 85
            q["milestone_age"] = ms_age
            ssn_count += 1
            q["is_ssn_contributor"] = ssn_count <= 2
            q["is_rmd_owner"] = bool(q.get("is_rmd_owner", q.get("rmd_owner", idx == 0)))
            q.pop("rmd_owner", None)
            if not isinstance(q.get("ssn"), dict):
                q["ssn"] = {"fra_age": 67, "default_claim_age": 67, "external_file": ""}
            q["work_plan"] = sanitize_work_plan(q.get("work_plan"), milestone_age=ms_age, clamp_to_milestone=clamp_work_plan)
            out.append(q)
        for idx, ben in enumerate(cfg.get("beneficiaries") or []):
            if not isinstance(ben, dict):
                continue
            q = dict(ben)
            mid = str(q.get("id") or q.get("name") or f"beneficiary_{idx + 1}").strip().lower().replace(" ", "_")
            q["id"] = mid
            if not q.get("birth_date") and q.get("birthdate"):
                q["birth_date"] = q.pop("birthdate")
            try:
                ms_age = max(65, min(105, int(round(float(q.get("milestone_age", 85))))))
            except (TypeError, ValueError):
                ms_age = 85
            q["milestone_age"] = ms_age
            q["is_ssn_contributor"] = False
            q["is_rmd_owner"] = bool(q.get("is_rmd_owner", q.get("rmd_owner", False)))
            q.pop("rmd_owner", None)
            if not isinstance(q.get("ssn"), dict):
                q["ssn"] = {"fra_age": 67, "default_claim_age": 67, "external_file": ""}
            q["work_plan"] = sanitize_work_plan(q.get("work_plan"), milestone_age=ms_age, clamp_to_milestone=clamp_work_plan)
            out.append(q)

    # Auto-transition elapsed target retirement ages to Not Working when resolving active plans,
    # and enforce exactly 1 Designated RMD Owner across the household (independent of SSN contribution)
    cur_ym = date.today().isoformat()[:7]
    rmd_seen = False
    for m in out:
        if clamp_work_plan and isinstance(m.get("work_plan"), dict):
            auto_retire_elapsed_work_plan(m["work_plan"], m.get("birth_date"), today_ym=cur_ym)
        if m.get("is_rmd_owner"):
            if rmd_seen:
                m["is_rmd_owner"] = False
            else:
                rmd_seen = True
    if not rmd_seen and out:
        out[0]["is_rmd_owner"] = True
    return out


def validate_portfolio_config(cfg: dict[str, Any] | None) -> dict[str, Any]:
    """
    Validates that a portfolio configuration specifies all required simulation inputs
    without relying on silent household or state tax assumptions (DP-04 / SA2-21).

    DESIGN INTENT (DP-04):
    - Incomplete draft portfolios MAY be created, saved, listed, and selected for editing in
      Portfolio Settings (displaying an `Incomplete` capsule only on incomplete rows).
    - Incomplete portfolios MUST NOT be selected as the active simulation portfolio
      (`set_active_portfolio`, `/api/portfolios/select`, `holdings.py refresh|render`) until
      `validate_portfolio_config(cfg)["valid"]` is True.
    """
    missing_fields: list[str] = []
    validation_errors: list[str] = []
    if not isinstance(cfg, dict):
        return {
            "valid": False,
            "missing_fields": ["household_name", "reference_annual_burn", "investors", "tax_rates"],
            "validation_errors": ["Portfolio configuration is missing or invalid."],
        }

    hh_name = str(cfg.get("household_name") or "").strip()
    if not hh_name:
        missing_fields.append("household_name")
        validation_errors.append("Household / portfolio name is required.")

    ref_b = cfg.get("reference_annual_burn")
    ret_b = cfg.get("retirement_annual_burn")
    burn_ok = False
    for b_cand in (ref_b, ret_b):
        if isinstance(b_cand, (int, float)) and not isinstance(b_cand, bool) and float(b_cand) > 0:
            burn_ok = True
            break
    if not burn_ok:
        missing_fields.append("reference_annual_burn")
        validation_errors.append("Annual spending burn (reference_annual_burn) must be greater than $0.")

    def _validate_person_work_plan(person: dict[str, Any], label: str) -> bool:
        raw_wp = person.get("work_plan")
        if not isinstance(raw_wp, dict):
            return False
        has_err = False
        try:
            ms_age = int(round(float(person.get("milestone_age", 85))))
        except (TypeError, ValueError):
            ms_age = 85
        raw_ret_age = raw_wp.get("default_retirement_age")
        if raw_ret_age is not None:
            try:
                ret_age_val = int(round(float(raw_ret_age)))
                if ret_age_val < 18 or ret_age_val > 100:
                    has_err = True
                    validation_errors.append(
                        f"{label} default_retirement_age must be between 18 and 100."
                    )
                elif ret_age_val > ms_age:
                    has_err = True
                    validation_errors.append(
                        f"{label} target retirement age ({ret_age_val}) cannot exceed milestone age ({ms_age})."
                    )
            except (TypeError, ValueError):
                has_err = True
                validation_errors.append(f"{label} default_retirement_age must be an integer age.")
        for num_field in ("annual_take_home", "annual_budget_offset"):
            if num_field in raw_wp and raw_wp[num_field] is not None:
                try:
                    fv = float(raw_wp[num_field])
                    if not math.isfinite(fv) or fv < 0.0 or fv > MAX_WORK_PLAN_ANNUAL_USD:
                        raise ValueError("out of bounds")
                except (TypeError, ValueError):
                    has_err = True
                    validation_errors.append(
                        f"{label} work_plan.{num_field} must be between $0 and ${MAX_WORK_PLAN_ANNUAL_USD:,.0f}."
                    )
        if raw_wp.get("is_working"):
            try:
                th_v = float(raw_wp.get("annual_take_home") or 0.0)
                bo_v = float(raw_wp.get("annual_budget_offset") or 0.0)
                if math.isfinite(th_v) and math.isfinite(bo_v) and th_v > 0.0 and bo_v > th_v + 0.01:
                    has_err = True
                    validation_errors.append(
                        f"{label} household budget contribution (${bo_v:,.0f}) cannot exceed annual take-home income (${th_v:,.0f})."
                    )
            except (TypeError, ValueError):
                pass
        raw_contribs = raw_wp.get("contributions")
        if raw_contribs is not None:
            if not isinstance(raw_contribs, list):
                has_err = True
                validation_errors.append(f"{label} work_plan.contributions must be a list.")
            else:
                for c_idx, crow in enumerate(raw_contribs):
                    if not isinstance(crow, dict):
                        continue
                    for c_num in ("employee_annual", "employer_match_annual"):
                        if c_num in crow and crow[c_num] is not None:
                            try:
                                cfv = float(crow[c_num])
                                if not math.isfinite(cfv) or cfv < 0.0 or cfv > MAX_WORK_PLAN_ANNUAL_USD:
                                    raise ValueError("out of bounds")
                            except (TypeError, ValueError):
                                has_err = True
                                validation_errors.append(
                                    f"{label} contribution #{c_idx + 1} {c_num} must be between $0 and ${MAX_WORK_PLAN_ANNUAL_USD:,.0f}."
                                )
                    if "match_rate_pct" in crow and crow["match_rate_pct"] is not None:
                        try:
                            mrv = float(crow["match_rate_pct"])
                            if not math.isfinite(mrv) or mrv < 0.0 or mrv > MAX_WORK_PLAN_MATCH_RATE_PCT:
                                raise ValueError("out of bounds")
                        except (TypeError, ValueError):
                            has_err = True
                            validation_errors.append(
                                f"{label} contribution #{c_idx + 1} match_rate_pct must be between 0% and {MAX_WORK_PLAN_MATCH_RATE_PCT:g}%."
                            )
                    if raw_wp.get("is_working") and (
                        float(crow.get("employee_annual") or 0.0) > 0.0
                        or float(crow.get("employer_match_annual") or 0.0) > 0.0
                        or bool(crow.get("age_50_catchup", False))
                    ):
                        tid = str(crow.get("target_asset_id") or "").strip()
                        if not tid or not _SAFE_BUCKET_ID_RE.match(tid):
                            has_err = True
                            validation_errors.append(
                                f"{label} contribution #{c_idx + 1} requires a valid target holding."
                            )
        return has_err

    raw_fams = cfg.get("family_members")
    raw_invs = cfg.get("investors")
    raw_bens = cfg.get("beneficiaries")
    use_fams = isinstance(raw_fams, list) and len(raw_fams) > 0
    primary_list = raw_fams if use_fams else (raw_invs if isinstance(raw_invs, list) else [])

    if len(primary_list) < 1:
        missing_fields.append("investors")
        validation_errors.append("At least one family member with a name and birth date is required.")
    else:
        fam_err = False
        ssn_true_count = 0
        for idx, mem in enumerate(primary_list):
            if not isinstance(mem, dict):
                fam_err = True
                validation_errors.append(f"Family member #{idx + 1} entry is invalid.")
                continue
            mname = str(mem.get("name") or "").strip()
            mbirth = str(mem.get("birth_date") or mem.get("birthdate") or "").strip()
            lbl = f"Family member {mname or f'#{idx + 1}'}"
            if not mname:
                fam_err = True
                validation_errors.append(f"Family member #{idx + 1} is missing a name.")
            if not mbirth or not _DOB_ISO_RE.match(mbirth):
                fam_err = True
                validation_errors.append(
                    f"{lbl} requires a valid birth_date (YYYY-MM-DD or YYYY-MM)."
                )
            if use_fams and bool(mem.get("is_ssn_contributor")):
                ssn_true_count += 1
            if _validate_person_work_plan(mem, lbl):
                fam_err = True
        if use_fams and ssn_true_count > 2:
            fam_err = True
            validation_errors.append("At most 2 family members may contribute Social Security.")
        if not use_fams and isinstance(raw_bens, list):
            for b_idx, ben in enumerate(raw_bens):
                if isinstance(ben, dict):
                    blbl = f"Family member {str(ben.get('name') or '').strip() or f'beneficiary #{b_idx + 1}'}"
                    if _validate_person_work_plan(ben, blbl):
                        fam_err = True
        if fam_err and "investors" not in missing_fields:
            missing_fields.append("investors")

    tr = cfg.get("tax_rates")
    if not isinstance(tr, dict):
        missing_fields.append("tax_rates")
        validation_errors.append("Tax configuration (filing state, LTCG rate, and ordinary income rate) is required.")
    else:
        state_id = str(tr.get("state_code") or cfg.get("household_state") or "").strip().upper()
        ltcg_val = tr.get("ltcg_top")
        ord_val = tr.get("ordinary_top")
        tr_missing: list[str] = []
        if not state_id:
            tr_missing.append("tax_rates.state_code")
            validation_errors.append("Filing state (or FED/NONE for federal-only) is required in tax_rates.")
        if not (isinstance(ltcg_val, (int, float)) and not isinstance(ltcg_val, bool) and 0.0 <= float(ltcg_val) <= 1.0):
            tr_missing.append("tax_rates.ltcg_top")
            validation_errors.append("Top long-term capital gains tax rate (tax_rates.ltcg_top) is required.")
        if not (isinstance(ord_val, (int, float)) and not isinstance(ord_val, bool) and 0.0 <= float(ord_val) <= 1.0):
            tr_missing.append("tax_rates.ordinary_top")
            validation_errors.append("Top ordinary income tax rate (tax_rates.ordinary_top) is required.")
        if tr_missing:
            missing_fields.append("tax_rates")
            missing_fields.extend(tr_missing)

    return {
        "valid": len(validation_errors) == 0,
        "missing_fields": missing_fields,
        "validation_errors": validation_errors,
    }


def default_config() -> dict[str, Any]:
    """Return a clean, PII-free default project configuration with zero silent household/state assumptions."""
    # PORTABILITY DO (DP-04 / SA2-21): Keep `reference_annual_burn`, `sim_cash_interest_tax_rate`,
    # `sim_ira_ordinary_tax_rate`, and `high_basis_split_threshold` as `None`, `investors` as `[]`,
    # and `tax_rates` as `{}` so unconfigured fields fail validation visibly instead of silently
    # substituting a specific household's spending, California tax rates, or dollar lot thresholds.
    # PORTABILITY DON'T: Never seed state tax rates, ticker symbols, or dollar split thresholds here.
    return {
        "version": 1,
        "household_name": "",
        "badge_image": "Assets/default_badge.svg",
        "reference_annual_burn": None,
        "retirement_annual_burn": None,
        "primary_equity_symbol": "",
        "symbols": [],
        "ticker_aliases": {"BRK/B": "BRK-B"},
        "google_finance_aliases": {"BRK/B": "BRK.B"},
        "investors": [],
        "beneficiaries": [],
        "gsu_forecast": {
            "default_net_retention_rate": 0.50,
            "retention_trailing_months": 24,
            "target_vested_account_id": "",
            "target_vested_security_id": "",
            "in_kind_transfer_target_account_id": "",
            "in_kind_transfer_vest_date": "",
            "high_basis_split_threshold": None,
        },
        "accounts": {},
        "sim_asset_rates": {
            "cash_sweeps": {"rate_model": "fixed", "annual_rate_pct": 4.0},
        },
        "sim_cash_interest_tax_rate": None,
        "sim_ira_ordinary_tax_rate": None,
        "tax_rates": {},
        "historical_defaults": {
            "default_start_date": "2024-01-01",
            "default_forecast_end_date": "2029-12-28",
            "backcast_before_earliest_statement": True,
            "interpolate_between_anchors": True,
        },
        "principal_overrides": {},
        "stack_buckets": [],
        "sim_buckets": {},
        "advisor_prompts": {},
        "saved_prompts_version": 1,
        "saved_prompts": default_saved_prompts(),
    }


def load_config(config_path: Path | None = None, portfolio_id: str | None = None) -> dict[str, Any]:
    if config_path is not None:
        path = config_path
        pid = portfolio_id or get_active_portfolio_id()
    else:
        pid = portfolio_id if portfolio_id is not None else get_active_portfolio_id()
        path = get_default_config_path(pid) if pid else None
    base = default_config()
    if path is None or not path.exists():
        if pid is None and config_path is None:
            base["saved_prompts"] = []
        return base
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        base.update(raw)
        base["saved_prompts"] = normalize_saved_prompts(raw)
    if pid and not is_single_portfolio_override():
        base["badge_image"] = normalize_badge_image_path(base.get("badge_image"), pid)
    return base


_SAVE_CONFIG_LOCK = threading.RLock()


def save_config(cfg: dict[str, Any], config_path: Path | None = None, portfolio_id: str | None = None) -> Path:
    path = config_path or get_default_config_path(portfolio_id)
    if path is None:
        raise RuntimeError("No active portfolio selected; cannot save project.json")
    with _SAVE_CONFIG_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.parent / f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        try:
            tmp_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
            try:
                os.chmod(tmp_path, 0o600)
            except OSError:
                pass
            tmp_path.replace(path)
        finally:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
    return path


def list_portfolios() -> dict[str, Any]:
    """Lists all configured portfolios in `Portfolios/` and identifies the active one."""
    if is_single_portfolio_override():
        pdir = get_portfolio_dir()
        cfg = load_config(pdir / "project.json" if pdir else None)
        pid = pdir.name if pdir else "default"
        val = validate_portfolio_config(cfg)
        return {
            "active_portfolio_id": pid,
            "has_active": True,
            "portfolios_dir": str(pdir or ""),
            "portfolios": [
                {
                    "id": pid,
                    "name": str(cfg.get("household_name") or pid),
                    "badge_image": normalize_badge_image_path(cfg.get("badge_image"), pid),
                    "active": True,
                    "is_complete": val["valid"],
                    "missing_fields": val["missing_fields"],
                    "validation_errors": val["validation_errors"],
                    "investors_count": len(cfg.get("investors") or []),
                    "beneficiaries_count": len(cfg.get("beneficiaries") or []),
                    "symbols": list(cfg.get("symbols") or []),
                    "has_db": bool(pdir and (pdir / "portfolio.sqlite").is_file()),
                    "path": f"Portfolios/{pid}",
                }
            ],
        }

    with _PORTFOLIOS_LOCK:
        active_id = get_active_portfolio_id()
        root = get_portfolios_dir()
        items: list[dict[str, Any]] = []
        if root.is_dir():
            for p in sorted(root.iterdir()):
                if not is_valid_portfolio_dir(p):
                    continue
                pid = p.name
                cfg = load_config(p / "project.json", portfolio_id=pid)
                val = validate_portfolio_config(cfg)
                stmt_dir = p / "Statements"
                stmt_count = 0
                if stmt_dir.is_dir():
                    stmt_count = sum(1 for f in stmt_dir.rglob("*") if f.is_file() and not f.name.startswith("."))
                scenarios_dir = (p / "Scenarios") if (p / "Scenarios").is_dir() else (p / "Saves")
                scenarios_count = 0
                if scenarios_dir.is_dir():
                    scenarios_count = sum(
                        1 for f in scenarios_dir.glob("*.json")
                        if not f.name.startswith("_") and f.name != "global_preferences.json"
                    )
                items.append(
                    {
                        "id": pid,
                        "name": str(cfg.get("household_name") or pid),
                        "badge_image": normalize_badge_image_path(cfg.get("badge_image"), pid),
                        "active": pid == active_id,
                        "is_complete": val["valid"],
                        "missing_fields": val["missing_fields"],
                        "validation_errors": val["validation_errors"],
                        "investors_count": len(cfg.get("investors") or []),
                        "beneficiaries_count": len(cfg.get("beneficiaries") or []),
                        "symbols": list(cfg.get("symbols") or []),
                        "has_db": (p / "portfolio.sqlite").is_file(),
                        "statements_count": stmt_count,
                        "scenarios_count": scenarios_count,
                        "path": f"Portfolios/{pid}",
                    }
                )
        items.sort(key=lambda x: (not x["active"], x["name"].lower(), x["id"]))
        return {
            "active_portfolio_id": active_id,
            "has_active": active_id is not None,
            "portfolios_dir": "Portfolios",
            "portfolios": items,
        }


def set_active_portfolio(portfolio_id: str | None) -> dict[str, Any]:
    """Switches the active portfolio (or clears it when `portfolio_id` is None/empty)."""
    if is_single_portfolio_override():
        return {"ok": False, "error": "Cannot switch portfolios when PORTFOLIO_DIR override is active."}
    with _PORTFOLIOS_LOCK:
        if not portfolio_id:
            _write_active_index(None)
            refresh_active_paths()
            return {"ok": True, "active_portfolio_id": None, **list_portfolios()}
        pid = str(portfolio_id).strip()
        pdir = get_portfolio_dir(pid)
        if pdir is None or not is_valid_portfolio_dir(pdir):
            return {"ok": False, "error": f"Portfolio '{pid}' not found"}
        cfg = load_config(pdir / "project.json", portfolio_id=pid)
        val = validate_portfolio_config(cfg)
        if not val["valid"]:
            return {
                "ok": False,
                "incomplete": True,
                "error": "Cannot activate incomplete portfolio: " + "; ".join(val["validation_errors"]),
                "missing_fields": val["missing_fields"],
                "validation_errors": val["validation_errors"],
                **list_portfolios(),
            }
        _write_active_index(pid)
        refresh_active_paths()
        return {"ok": True, "active_portfolio_id": pid, **list_portfolios()}


def create_portfolio(name: str, activate: bool = True) -> dict[str, Any]:
    """Creates a brand new isolated portfolio directory under `Portfolios/<slug>/`."""
    if is_single_portfolio_override():
        return {"ok": False, "error": "Cannot create portfolios when PORTFOLIO_DIR override is active."}
    clean_name = re.sub(r"[\x00-\x1f]+", " ", str(name or "")).strip()[:80] or "New Portfolio"
    with _PORTFOLIOS_LOCK:
        root = get_portfolios_dir()
        root.mkdir(parents=True, exist_ok=True)
        existing = {p.name.lower() for p in root.iterdir() if p.is_dir()}
        pid = slugify_portfolio_id(clean_name, existing=existing)
        pdir = root / pid
        (pdir / "Statements" / "SSN").mkdir(parents=True, exist_ok=True)
        (pdir / "Scenarios" / "snapshots").mkdir(parents=True, exist_ok=True)
        (pdir / "Assets").mkdir(parents=True, exist_ok=True)

        cfg = default_config()
        cfg["household_name"] = clean_name
        cfg["badge_image"] = "Assets/default_badge.svg"
        save_config(cfg, pdir / "project.json", portfolio_id=pid)

        # Initialize empty SQLite schema so immediate render/simulations succeed cleanly
        from db.database import PortfolioDB

        db_file = pdir / "portfolio.sqlite"
        with PortfolioDB(db_file):
            pass
        for sq_f in pdir.glob("portfolio.sqlite*"):
            if sq_f.is_file():
                try:
                    os.chmod(sq_f, 0o600)
                except OSError:
                    pass

        val = validate_portfolio_config(cfg)
        if not val["valid"]:
            activate = False

        if activate:
            _write_active_index(pid)
            refresh_active_paths()
        elif not _read_active_index()[0]:
            _write_active_index(None)

        return {
            "ok": True,
            "portfolio_id": pid,
            "name": clean_name,
            "active": activate,
            "is_complete": val["valid"],
            "missing_fields": val["missing_fields"],
            "validation_errors": val["validation_errors"],
            **list_portfolios(),
        }


def delete_portfolio(portfolio_id: str) -> dict[str, Any]:
    """Permanently deletes `Portfolios/<portfolio_id>/` and clears active selection if it was active."""
    if is_single_portfolio_override():
        return {"ok": False, "error": "Cannot delete portfolios when PORTFOLIO_DIR override is active."}
    pid = str(portfolio_id or "").strip()
    if not pid:
        return {"ok": False, "error": "Missing portfolio_id"}
    with _PORTFOLIOS_LOCK:
        pdir = get_portfolio_dir(pid)
        if pdir is None or not pdir.is_dir():
            return {"ok": False, "error": f"Portfolio '{pid}' not found"}
        was_active = pid == get_active_portfolio_id()
        last_id = get_last_active_portfolio_id()
        if was_active or last_id == pid:
            _write_active_index(
                None if was_active else get_active_portfolio_id(),
                clear_last=(last_id == pid),
            )
            refresh_active_paths()
        tmp_del = pdir.parent / f".deleting_{pid}_{threading.get_ident()}"
        try:
            pdir.rename(tmp_del)
            shutil.rmtree(tmp_del, ignore_errors=True)
        except OSError:
            shutil.rmtree(pdir, ignore_errors=True)
        refresh_active_paths()
        return {
            "ok": True,
            "deleted_portfolio_id": pid,
            "was_active": was_active,
            **list_portfolios(),
        }


refresh_active_paths()


def yahoo_symbol(raw_ticker: str, cfg: dict[str, Any]) -> str:
    aliases = cfg.get("ticker_aliases", {})
    if raw_ticker in aliases:
        return aliases[raw_ticker]
    return raw_ticker.replace("/", "-")
