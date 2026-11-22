#!/usr/bin/env python3
"""Local HTTP sidecar & canonical Scenario store manager for `Portfolios/<active>/Scenarios/`."""

from __future__ import annotations

import argparse
import base64
from datetime import datetime
import errno
import hmac
from http.client import HTTPConnection, HTTPException
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import logging
import math
import mimetypes
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import threading
import time
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from privacy import (
    PrivacyConfigError,  # noqa: E402
    DemoTranslator,
    apply_demo_projection_to_payload,
    apply_demo_projection_to_saves,
    build_translator,
    load_privacy_config,
    save_privacy_config,
    sync_privacy_registry,
)

SCRIPTS_ROOT = Path(__file__).resolve().parent
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

import config  # noqa: E402
from config import (  # noqa: E402
    ADVISOR_PROMPT_FILES,
    DATA_DIR,
    DEFAULT_CONFIG_PATH,
    DEFAULT_DB_PATH,
    DEFAULT_HTML_REPORT_PATH,
    PORTFOLIO_DIR,
    SAVES_DIR,
    US_STATE_TAX_PRESETS,
    create_portfolio,
    default_config,
    delete_portfolio,
    get_active_portfolio_id,
    get_default_config_path,
    get_default_db_path,
    get_portfolio_dir,
    get_portfolios_dir,
    get_saves_dir,
    get_session_secret_file,
    get_session_token_file,
    list_portfolios,
    load_config,
    load_default_advisor_prompts,
    normalize_badge_history,
    normalize_badge_image_path,
    normalize_saved_prompts,
    refresh_active_paths,
    resolve_advisor_prompts,
    resolve_family_members,
    auto_retire_elapsed_work_plan,
    sanitize_saved_prompts,
    sanitize_work_plan,
    save_config,
    set_active_portfolio,
    validate_portfolio_config,
)

_INITIAL_DATA_DIR = DATA_DIR

_INITIAL_DEFAULT_DB_PATH = DEFAULT_DB_PATH
_INITIAL_DEFAULT_CONFIG_PATH = DEFAULT_CONFIG_PATH
_INITIAL_SAVES_DIR = SAVES_DIR
from settings import (  # noqa: E402,F401
    canonicalize_model_settings,
    default_settings_snapshot,
    invalidate_settings_cache,
)
from db.database import PortfolioDB  # noqa: E402
from ingest.ssa_xml import parse_ssa_xml_path, parse_ssa_xml_text  # noqa: E402
from valuation.market_data import fetch_quote_snapshot, resolve_symbol_alias  # noqa: E402

logging.basicConfig(level=logging.INFO, format="[advisorbot_server] %(levelname)s %(message)s")
log = logging.getLogger("advisorbot_server")

META_FILENAME = "_meta.json"
INDEX_FILENAME = "_index.json"
SNAPSHOTS_SUBDIR = "snapshots"
GLOBAL_PREFERENCES_FILENAME = "global_preferences.json"
DEFAULT_SAVE_NAME = "Default"
DEFAULT_PORT = 8765
_REBUILD_LOCK = threading.Lock()
_MODEL_STORE_LOCK = threading.RLock()
_MIGRATING_DIRS: set[str] = set()
_SNAPSHOT_BACKFILL_ATTEMPTED: set[tuple[str, str]] = set()
_SNAPSHOT_BACKFILL_LOCK = threading.Lock()

# --- Security configuration -------------------------------------------------
TOKEN_FILE = get_session_token_file()
COOKIE_MAX_AGE_SECONDS = 31536000
_URLSAFE_SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{32,}$")


def _resolve_session_secret_file() -> Path:
    if DATA_DIR != _INITIAL_DATA_DIR:
        return Path(DATA_DIR) / ".session_secret"
    return get_session_secret_file()


def _write_0600_secret(path: Path, secret: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp" if path.suffix else ".secret.tmp")
    tmp.write_text(secret, encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    os.chmod(path, 0o600)


def _init_session_token(*, rotate: bool = False) -> str:
    """
    Loads the persistent machine-local 256-bit secret from `0600 Data/.session_secret`
    (or mints and saves a fresh secret when absent, invalid, or `rotate=True`) per DP-01.

    DESIGN INTENT (DP-01 / SA2-01):
    - `0600 Data/.session_secret` persists across server restarts so open browser tabs and
      bookmarks carrying the `HttpOnly; SameSite=Strict` session cookie reconnect seamlessly
      without ever embedding tokens in HTML or opening CORS/unauthed reconnect loopholes.
    - Pass `--rotate-token` on CLI startup to revoke all existing browser cookies.
    - `0600 Data/.session_token` is a separate runtime copy written on startup for local CLI
      tools and unlinked on shutdown.
    """
    secret_path = _resolve_session_secret_file()
    if not rotate and secret_path.is_file():
        try:
            existing = secret_path.read_text(encoding="utf-8").strip()
            if len(existing) >= 32 and _URLSAFE_SECRET_RE.fullmatch(existing):
                try:
                    os.chmod(secret_path, 0o600)
                except OSError:
                    pass
                return existing
        except Exception as exc:
            log.warning("Could not read %s; minting new secret: %s", secret_path, exc)
    fresh = secrets.token_urlsafe(32)
    try:
        _write_0600_secret(secret_path, fresh)
    except Exception as exc:
        log.warning("Could not persist %s: %s", secret_path, exc)
    return fresh


def refresh_session_token_for_root() -> str:
    """Refreshes module root paths and `SESSION_TOKEN` from `Data/.session_secret` after `config.reload_root()`."""
    global DATA_DIR, _INITIAL_DATA_DIR
    global DEFAULT_DB_PATH, _INITIAL_DEFAULT_DB_PATH
    global DEFAULT_CONFIG_PATH, _INITIAL_DEFAULT_CONFIG_PATH
    global SAVES_DIR, _INITIAL_SAVES_DIR
    global DEFAULT_HTML_REPORT_PATH, PROJECT_ROOT
    global SESSION_TOKEN, TOKEN_FILE
    DATA_DIR = config.DATA_DIR
    _INITIAL_DATA_DIR = config.DATA_DIR
    DEFAULT_DB_PATH = config.DEFAULT_DB_PATH
    _INITIAL_DEFAULT_DB_PATH = config.DEFAULT_DB_PATH
    DEFAULT_CONFIG_PATH = config.DEFAULT_CONFIG_PATH
    _INITIAL_DEFAULT_CONFIG_PATH = config.DEFAULT_CONFIG_PATH
    SAVES_DIR = config.SAVES_DIR
    _INITIAL_SAVES_DIR = config.SAVES_DIR
    DEFAULT_HTML_REPORT_PATH = config.DEFAULT_HTML_REPORT_PATH
    PROJECT_ROOT = Path(config.DEFAULT_HTML_REPORT_PATH).resolve().parent
    TOKEN_FILE = get_session_token_file()
    SESSION_TOKEN = _init_session_token(rotate=False)
    return SESSION_TOKEN


SESSION_TOKEN = _init_session_token()
COOKIE_NAME = "portfolio_session"
ALLOWED_HOSTS = {"127.0.0.1", "localhost"}
_ROOT_DOC_FILE_MAP = {
    "readme.md": "README.md",
    "readme": "README.md",
    "disclaimer.md": "DISCLAIMER.md",
    "disclaimer": "DISCLAIMER.md",
    "license": "LICENSE",
    "license.md": "LICENSE",
    "notice": "NOTICE",
    "notice.md": "NOTICE",
}
AUTHED_ROOT_DOC_FILES = {f"/{k}" for k in _ROOT_DOC_FILE_MAP} | {f"/{v}" for v in _ROOT_DOC_FILE_MAP.values()}
PUBLIC_PATHS = {"/api/health", "/favicon.ico"}
PROJECT_ROOT = Path(DEFAULT_HTML_REPORT_PATH).resolve().parent


def _lookup_root_doc(url_path: str) -> str | None:
    rel = unquote(url_path.lstrip("/"))
    if not rel or "\x00" in rel or "/" in rel or "\\" in rel:
        return None
    return _ROOT_DOC_FILE_MAP.get(rel.lower())


def _active_index_html_path() -> Path:
    if str(DEFAULT_HTML_REPORT_PATH) != str(config.DEFAULT_HTML_REPORT_PATH):
        return Path(DEFAULT_HTML_REPORT_PATH)
    if PROJECT_ROOT != Path(config.DEFAULT_HTML_REPORT_PATH).resolve().parent:
        return PROJECT_ROOT / "index.html"
    return Path(DEFAULT_HTML_REPORT_PATH)


STATIC_DIRS = (
    "Docs",
    "Assets",
    "Project/Assets",
    "Project/Docs",
    "Scenarios/snapshots",
    "Saves/snapshots",
    "Project/Scenarios/snapshots",
    "Project/Saves/snapshots",
)
AUTHED_STATIC_DIRS = (
    "Statements",
    "Project/Statements",
)
STATIC_PREFIXES = tuple(f"/{d}/" for d in STATIC_DIRS)
AUTHED_STATIC_PREFIXES = tuple(f"/{d}/" for d in AUTHED_STATIC_DIRS)
_PORTFOLIO_STATIC_RE = re.compile(r"^/Portfolios/[^/]+/(Assets|Docs|Statements|(Scenarios|Saves)/snapshots)/.+$")
MAX_BODY_BYTES = 10 * 1024 * 1024


def _resolve_static_target(url_path: str) -> tuple[Path | None, bool, bool]:
    """Resolves `url_path` to a real file and checks whether it resides inside an authenticated static root (SA2-02)."""
    rel = unquote(url_path.lstrip("/"))
    if not rel or "\x00" in rel:
        return None, False, False
    mapped_root_doc = _lookup_root_doc(url_path)
    if mapped_root_doc:
        for doc_root in (PROJECT_ROOT.resolve(), config._app_install_dir().resolve()):
            candidate = doc_root / mapped_root_doc
            if candidate.is_file() and not candidate.is_symlink():
                return candidate.resolve(), False, True
    active_pdir = get_portfolio_dir()
    target = (PROJECT_ROOT / rel).resolve()
    if not target.is_file() and active_pdir is not None:
        if rel.startswith("Portfolios/"):
            parts = rel.split("/", 2)
            if len(parts) >= 3:
                alt = (active_pdir / parts[2]).resolve()
                if alt.is_file():
                    target = alt
        else:
            sub_rel = rel[len("Project/"):] if rel.startswith("Project/") else rel
            alt = (active_pdir / sub_rel).resolve()
            if alt.is_file():
                target = alt
    if not target.is_file():
        if rel.startswith("Assets/"):
            alt_asset = (config.ASSETS_DIR / rel[len("Assets/"):]).resolve()
            if alt_asset.is_relative_to(config.ASSETS_DIR.resolve()) and alt_asset.is_file():
                return alt_asset, False, True
        if rel.startswith("Docs/"):
            alt_doc = (config.DOCS_DIR / rel[len("Docs/"):]).resolve()
            if alt_doc.is_relative_to(config.DOCS_DIR.resolve()) and alt_doc.is_file():
                return alt_doc, False, True

    # Guard against path traversal out of a static prefix (e.g. /Docs/../Scenarios)
    for d in STATIC_DIRS + AUTHED_STATIC_DIRS:
        if url_path.startswith(f"/{d}/"):
            in_d = (
                target == (PROJECT_ROOT / d).resolve()
                or (PROJECT_ROOT / d).resolve() in target.parents
                or (
                    active_pdir is not None
                    and (
                        target == (active_pdir / d).resolve()
                        or (active_pdir / d).resolve() in target.parents
                    )
                )
            )
            if not in_d:
                return None, False, False
            break

    def _in_dirs(dirs: tuple[str, ...]) -> bool:
        return any(
            target == (PROJECT_ROOT / d).resolve()
            or (PROJECT_ROOT / d).resolve() in target.parents
            or (
                active_pdir is not None
                and (
                    target == (active_pdir / d).resolve()
                    or (active_pdir / d).resolve() in target.parents
                )
            )
            for d in dirs
        )

    auth_ok = _in_dirs(STATIC_DIRS) or _in_dirs(AUTHED_STATIC_DIRS)
    portfolios_root = get_portfolios_dir().resolve()
    try:
        rel_parts = target.relative_to(portfolios_root).parts
    except ValueError:
        rel_parts = ()
    if rel_parts:
        is_active_target = (
            active_pdir is not None
            and len(rel_parts) >= 1
            and (portfolios_root / rel_parts[0]).resolve() == active_pdir.resolve()
        )
        if not is_active_target:
            # SECURITY INVARIANT (SA5-05): Only permit cross-portfolio `Assets/*` (for Portfolio Switcher icons);
            # reject cross-portfolio `Statements`, `Docs`, or `Scenarios/snapshots` with 404.
            assets_root = (portfolios_root / rel_parts[0] / "Assets").resolve()
            if (
                len(rel_parts) >= 3
                and rel_parts[1] == "Assets"
                and not target.is_symlink()
                and assets_root in target.parents
            ):
                return target, False, True
            return None, False, False
        if not auth_ok:
            if (len(rel_parts) >= 3 and rel_parts[1] in ("Assets", "Docs", "Statements")) or (
                len(rel_parts) >= 4 and rel_parts[1] in ("Scenarios", "Saves") and rel_parts[2] == "snapshots"
            ):
                auth_ok = True
    return target, False, auth_ok


def _is_public_static_path(path: str) -> bool:
    """Top-level open-source repo docs (`README.md`, `DISCLAIMER.md`, `LICENSE`, `NOTICE`) are public; all portfolio/snapshot/doc assets require `_pass_gate()` (SA2-02)."""
    if _lookup_root_doc(path):
        target, _, auth_ok = _resolve_static_target(path)
        return bool(target is not None and target.is_file() and auth_ok)
    return False


def _is_authed_static_path(path: str) -> bool:
    if _lookup_root_doc(path):
        target, _, auth_ok = _resolve_static_target(path)
        return bool(target is not None and target.is_file() and auth_ok)
    if not (
        path.startswith(STATIC_PREFIXES)
        or path.startswith(AUTHED_STATIC_PREFIXES)
        or bool(_PORTFOLIO_STATIC_RE.match(path))
    ):
        return False
    target, _, auth_ok = _resolve_static_target(path)
    return bool(target is not None and target.is_file() and auth_ok)


def _norm_reserved_key(raw: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(raw or "").strip().lower())


def _load_reserved_save_names(portfolio_id: str | None = None) -> set[str]:
    """Deprecated: scenario protection is now purely a user-controlled per-scenario flag."""
    return set()


RESERVED_SAVE_NAMES: set[str] = set()
IN_SERVER_PROCESS = False


def _protected_save_names_for_dir(saves_dir: Path) -> set[str]:
    """Reads configured legacy `protected_saves` names from the owning portfolio's `project.json`."""
    cfg_path = saves_dir.parent / "project.json"
    if not cfg_path.is_file():
        return set()
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        return {
            str(n).strip().lower()
            for n in (cfg.get("protected_saves") or [])
            if str(n).strip()
        }
    except Exception:
        return set()


def _is_scenario_doc_protected(doc: dict[str, Any], saves_dir: Path, fallback_name: str = "") -> bool:
    """Checks explicit `is_protected` on a Scenario JSON, falling back to `project.json` when unset."""
    if "is_protected" in doc and doc["is_protected"] is not None:
        return bool(doc["is_protected"])
    name = str(doc.get("name") or fallback_name or "").strip().lower()
    return bool(name and name in _protected_save_names_for_dir(saves_dir))


def is_scenario_protected(name_or_id: str, saves_dir: Path | None = None) -> bool:
    """Returns True if the target scenario is protected via `is_protected` or `project.json` config."""
    raw = str(name_or_id or "").strip()
    if not raw:
        return False
    target_dir = _resolve_saves_dir(saves_dir)
    if target_dir is None:
        return False
    entry = get_model_entry(raw, saves_dir=target_dir)
    if not entry:
        return False
    fpath = target_dir / str(entry.get("file") or "")
    if not fpath.is_file():
        return False
    try:
        doc = json.loads(fpath.read_text(encoding="utf-8"))
        return _is_scenario_doc_protected(doc, target_dir, fallback_name=str(entry.get("name") or raw))
    except Exception:
        return False


is_reserved_save_name = is_scenario_protected


def write_token_file() -> None:
    """Writes the active session token to `0600 Data/.session_token` (and ensures `Data/.session_secret` exists)."""
    try:
        tok_file = get_session_token_file()
        _write_0600_secret(tok_file, SESSION_TOKEN)
        secret_file = _resolve_session_secret_file()
        if not secret_file.is_file() and len(SESSION_TOKEN) >= 32:
            _write_0600_secret(secret_file, SESSION_TOKEN)
    except Exception as exc:
        log.warning("could not write session token file: %s", exc)


def _active_demo_privacy_cfg() -> dict[str, Any] | None:
    """Returns full privacy config if Demo Mode is currently enabled for the active portfolio."""
    pid = get_active_portfolio_id()
    if pid:
        pcfg = load_privacy_config(pid, auto_sync=False)
        if (pcfg.get("demo_mode") or {}).get("enabled"):
            return pcfg
    return None


def _is_demo_write_blocked(path: str) -> bool:
    """
    Default-deny write lock for `POST`/`DELETE` routes while Demo Mode is active (DP-02 / SA2-12 / SA3-01).
    Only exact `/api/privacy` (restricted to toggling `demo_mode.enabled = False`), `/api/ui/poll`, and
    read-only Demo-projected `/api/chat` + `/api/advisor/run` are allowed.
    """
    # SECURITY INVARIANT (SA3-01 / SA2-12 / DP-02):
    # - Keep Demo Mode write-gating strictly default-deny.
    # - NEVER exempt `/api/privacy/*` sub-routes (`/preview`, `/sync`, `/regenerate-demo`), which read or return
    #   unredacted household entities, DOBs, and scale factors! Only exact `/api/privacy` is permitted so the user
    #   can exit Demo Mode (`{"demo_mode": {"enabled": False}}`), and `_dispatch_post` enforces that payload restriction.
    clean_p = path.rstrip("/") or "/"
    if clean_p == "/api/privacy":
        return False
    allowed_prefixes = (
        "/api/ui/poll",
        "/api/chat",
        "/api/advisor/run",
    )
    if any(clean_p == p or clean_p.startswith(p + "/") for p in allowed_prefixes):
        return False
    return True


def _handle_privacy_preview(body: dict[str, Any]) -> dict[str, Any]:
    """Runs live wire serialization & leak check against a sample or custom Prompt 2 snippet."""
    pid = str(body.get("portfolio_id") or "").strip() or get_active_portfolio_id()
    pdir = get_portfolio_dir(pid) if pid else None
    has_reg = bool(pdir and (pdir / "privacy_registry.json").is_file())
    cfg = load_privacy_config(pid, auto_sync=not has_reg)
    tr = build_translator(cfg)
    raw_sample = str(body.get("sample_text") or "").strip()
    if not raw_sample:
        # Build representative sample directly from active portfolio entities so leak check verifies real terms
        ents = cfg.get("entities") or []
        by_cat = {}
        for e in ents:
            by_cat.setdefault(e.get("category"), []).append(e)
        inv_ent = (by_cat.get("investor") or [{}])[0]
        inv = inv_ent.get("canonical", "Investor 1")
        ben = (by_cat.get("beneficiary") or [{}])[0].get("canonical", "Beneficiary 1")
        bank = (by_cat.get("custodian") or [{}])[0].get("canonical", "Brokerage")
        acct = (by_cat.get("account") or [{}])[0].get("canonical", "ACCT-1001")
        eq = (by_cat.get("equity") or [{}])[0].get("canonical", "STOCK")
        inv_dob = ((inv_ent.get("meta") or {}) if isinstance(inv_ent.get("meta"), dict) else {}).get("birth_date", "")
        dob_clause = f" (DOB {inv_dob})" if inv_dob else ""
        raw_sample = (
            f"Prompt 2 Upvote: Consolidate $500,000 from {bank} {acct} {eq} lots to reduce single-stock risk "
            f"while preserving {inv}'s Roth conversion bracket{dob_clause} and {ben}'s age-26 gift."
        )
    sample_payload: dict[str, Any] = {}
    try:
        ov = llm_tools.tool_get_overview()
        sample_payload = {
            "family_birthdays": ov.get("family_birthdays"),
            "simulator_assets": (ov.get("simulator_assets") or [])[:4],
            "models": [
                {"id": m.get("id"), "name": m.get("name"), "notes_md": (m.get("notes_md") or "")[:400]}
                for m in (ov.get("models") or [])[:2]
            ],
        }
    except Exception:
        sample_payload = {}
    res = tr.preview_round_trip(raw_sample, sample_payload)
    res["shield_mode"] = str((cfg.get("ai_shield") or {}).get("mode") or "identity_aliased")
    return res


def render_active_site() -> dict[str, Any]:
    """Fast re-render of `index.html` from the currently active portfolio's DB & config (or empty dashboard)."""
    from datetime import date as _date
    from exporters.html_report import render_html_report
    from exporters.view_model import build_report_payload
    from valuation.simulator import refresh_family_config

    pid = get_active_portfolio_id()
    cfg_path = get_default_config_path(pid) if pid else None
    cfg = load_config(cfg_path, portfolio_id=pid)
    refresh_family_config(cfg)
    db_path = get_default_db_path(pid) if pid else None
    if db_path is not None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        db_ctx = PortfolioDB(db_path)
    else:
        db_ctx = PortfolioDB(":memory:")
    demo_cfg = _active_demo_privacy_cfg()
    with db_ctx as db:
        payload = build_report_payload(db, cfg, _date.today(), bypass_export_masking=(demo_cfg is not None))
    if demo_cfg is not None:
        payload = apply_demo_projection_to_payload(payload, demo_cfg)
    render_html_report(payload, _active_index_html_path())
    return {"ok": True, "active_portfolio_id": pid, "payload": payload}


def run_site_rebuild() -> dict[str, Any]:
    """Runs `python3 holdings.py refresh` to scan statements, sync quotes, and regenerate `index.html`."""
    if not _REBUILD_LOCK.acquire(blocking=False):
        return {
            "ok": False,
            "error": "A site rebuild is already in progress.",
        }
    try:
        holdings_script = SCRIPTS_ROOT / "holdings.py"
        proc = subprocess.run(
            [sys.executable, str(holdings_script), "refresh"],
            cwd=str(SCRIPTS_ROOT),
            capture_output=True,
            text=True,
            timeout=240,
        )
        stdout = (proc.stdout or "").strip()
        stderr = (proc.stderr or "").strip()
        if proc.returncode != 0:
            err_msg = stderr or stdout or f"holdings.py exited with code {proc.returncode}"
            return {
                "ok": False,
                "returncode": proc.returncode,
                "error": err_msg,
                "stdout": stdout,
                "stderr": stderr,
            }
        return {
            "ok": True,
            "returncode": 0,
            "stdout": stdout,
            "stderr": stderr,
        }
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "error": "Site rebuild timed out after 240 seconds.",
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
        }
    finally:
        _REBUILD_LOCK.release()


def _resolve_active_primary_symbol() -> str:
    try:
        cfg = load_config(get_default_config_path())
        sym = str(cfg.get("primary_equity_symbol") or "").strip().upper()
        if not sym and isinstance(cfg.get("symbols"), list) and cfg["symbols"]:
            sym = str(cfg["symbols"][0] or "").strip().upper()
        return sym
    except Exception:
        return ""


def fetch_live_quote(symbol: str = "") -> dict[str, Any]:
    """Fetches the latest live market price and daily change for any ticker or shorthand from Yahoo Finance."""
    sym = str(symbol or "").strip().upper() or _resolve_active_primary_symbol()
    if not sym:
        return {"ok": False, "error": "Primary equity symbol is not configured"}
    custom_aliases = (load_config().get("ticker_aliases") or {})
    return fetch_quote_snapshot(sym, custom_aliases=custom_aliases)


def fetch_live_goog_quote() -> dict[str, Any]:
    return fetch_live_quote(_resolve_active_primary_symbol())


def sanitize_save_name(raw_name: str) -> str:
    """Cleans a user-supplied model display name, stripping control chars and HTML angle brackets (SA2-14)."""
    s = str(raw_name or "").strip()
    if s.lower().endswith(".json"):
        s = s[:-5].strip()
    s = re.sub(r"[\x00-\x1f<>]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip(" .")
    low = s.lower()
    if not s or low in ("_meta", "_index", "global_preferences") or low.startswith("global_"):
        return DEFAULT_SAVE_NAME
    return s[:96]


def slugify_model_name(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", str(name or "").strip().lower()).strip("_")
    return (slug[:48].rstrip("_") or "model")


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _read_index(saves_dir: Path) -> dict[str, Any] | None:
    idx_path = saves_dir / INDEX_FILENAME
    if not idx_path.exists():
        return None
    try:
        raw = json.loads(idx_path.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and isinstance(raw.get("models"), list):
            return raw
    except Exception as exc:
        log.warning("Failed to parse %s: %s", idx_path, exc)
    return None


def _chmod_0600_safe(path: Path) -> None:
    try:
        if path.exists():
            os.chmod(path, 0o600)
    except OSError:
        pass


def _write_index(saves_dir: Path, idx_doc: dict[str, Any]) -> None:
    saves_dir.mkdir(parents=True, exist_ok=True)
    idx_path = saves_dir / INDEX_FILENAME
    text = (json.dumps(idx_doc, indent=2) + "\n").replace("\r\n", "\n")
    idx_path.write_bytes(text.encode("utf-8"))
    _chmod_0600_safe(idx_path)


def _write_json_lf(path: Path, doc: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = (json.dumps(doc, indent=2) + "\n").replace("\r\n", "\n")
    path.write_bytes(text.encode("utf-8"))
    _chmod_0600_safe(path)


def _normalize_author(author: Any) -> dict[str, Any]:
    if isinstance(author, dict):
        raw_t = str(author.get("type") or author.get("kind") or "").strip()
        if raw_t.lower() in ("ai", "ai_advisor"):
            a_type = raw_t.lower()
        elif raw_t:
            a_type = raw_t
        else:
            a_type = "ai_advisor" if author.get("model") else "human"
        return {
            "type": a_type,
            "model": author.get("model"),
            "prompt_type": author.get("prompt_type"),
        }
    if isinstance(author, str) and author.strip().lower() not in ("", "human", "user"):
        s_clean = author.strip()
        if s_clean.lower() in ("ai", "ai_advisor"):
            return {
                "type": s_clean.lower(),
                "model": "Advisor",
                "prompt_type": None,
            }
        return {
            "type": "ai_advisor",
            "model": s_clean,
            "prompt_type": None,
        }
    return {
        "type": "human",
        "model": None,
        "prompt_type": None,
    }


def _default_migrated_notes(name: str, settings: dict[str, Any], kpi_summary: dict[str, Any]) -> str:
    lens_map = {
        "single_path": "Simple Deterministic Path",
        "stress": "Early Crash Stress Test",
        "range": "Monte-Carlo Volatility Band",
        "historical": "Historical Cohort Replay",
    }
    lens = str(kpi_summary.get("active_lens") or settings.get("simActiveModel") or "single_path")
    lens_lbl = lens_map.get(lens, lens)
    burn = float(settings["simAnnualBurn"]) if settings.get("simAnnualBurn") is not None else 400000.0
    inf = float(settings.get("simInflationPct") or 3.0)
    norm_key = _norm_reserved_key(name)
    if norm_key == "zero":
        summary = "Conservative 0% flat-growth floor baseline preserving principal assumptions under inflation."
    elif norm_key == "default":
        summary = "Reference baseline seeded from portfolio historical CAGRs and default household spending."
    elif "default" in norm_key:
        summary = f"Primary household planning baseline ({name}) evaluated under the {lens_lbl} lens."
    else:
        summary = f"Saved retirement simulation model ({name}) evaluated under the {lens_lbl} lens."
    return (
        f"## Executive Summary\n"
        f"{summary}\n\n"
        f"## Key Assumptions\n"
        f"- **Projection Lens:** {lens_lbl}\n"
        f"- **Annual Burn Target:** ${burn:,.0f}/yr (Today's $)\n"
        f"- **Annual Inflation:** {inf:.1f}%\n\n"
        f"## Upvotes & Concurrences\n"
    )


def _fmt_compact_usd(val: float) -> str:
    av = abs(val)
    sign = "-" if val < 0 else ""
    if av >= 1_000_000_000:
        return f"{sign}${av / 1_000_000_000:.2f}B"
    if av >= 1_000_000:
        return f"{sign}${av / 1_000_000:.1f}M"
    if av >= 1_000:
        return f"{sign}${av / 1_000:.0f}K"
    return f"{sign}${av:.0f}"


def _load_pil_font(size: int, bold: bool = False, mono: bool = False) -> Any:
    from PIL import ImageFont

    if mono:
        mono_candidates: list[tuple[str, int]] = (
            [
                ("/System/Library/Fonts/Menlo.ttc", 1),
                ("/System/Library/Fonts/Supplemental/Courier New Bold.ttf", 0),
                ("/System/Library/Fonts/SFNSMono.ttf", 0),
            ]
            if bold
            else [
                ("/System/Library/Fonts/Menlo.ttc", 0),
                ("/System/Library/Fonts/SFNSMono.ttf", 0),
                ("/System/Library/Fonts/Monaco.ttf", 0),
            ]
        )
        for fp, idx in mono_candidates:
            if os.path.exists(fp):
                try:
                    return ImageFont.truetype(fp, size=size, index=idx)
                except Exception:
                    continue

    candidates = (
        [
            "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
            "/System/Library/Fonts/SFNSDisplay.ttf",
            "/System/Library/Fonts/Helvetica.ttc",
        ]
        if bold
        else [
            "/System/Library/Fonts/Supplemental/Arial.ttf",
            "/System/Library/Fonts/SFNSText.ttf",
            "/System/Library/Fonts/Helvetica.ttc",
        ]
    )
    for fp in candidates:
        if os.path.exists(fp):
            try:
                return ImageFont.truetype(fp, size=size)
            except Exception:
                continue
    return ImageFont.load_default()


def _hex_to_rgb(raw_color: Any) -> tuple[int, int, int] | None:
    s = str(raw_color or "").strip()
    if s.startswith("#"):
        s = s[1:]
    if len(s) == 3 and all(c in "0123456789abcdefABCDEF" for c in s):
        return (int(s[0] * 2, 16), int(s[1] * 2, 16), int(s[2] * 2, 16))
    if len(s) == 6 and all(c in "0123456789abcdefABCDEF" for c in s):
        return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
    return None


def _ordinal_age_str(age: int) -> str:
    if 10 <= (age % 100) <= 20:
        suf = "th"
    else:
        suf = {1: "st", 2: "nd", 3: "rd"}.get(age % 10, "th")
    return f"{age}{suf}"


_FALLBACK_BAR_PALETTE: tuple[tuple[int, int, int], ...] = (
    (56, 189, 248),
    (52, 211, 153),
    (245, 158, 11),
    (168, 85, 247),
    (34, 211, 238),
    (20, 184, 166),
    (244, 63, 94),
    (148, 163, 184),
)


def render_model_snapshot_jpg(
    dest_path: Path,
    model_name: str,
    settings: dict[str, Any],
    eval_result: dict[str, Any] | None,
    kpi_summary: dict[str, Any],
    *,
    payload: dict[str, Any] | None = None,
) -> bool:
    """
    Renders a crisp 1160x420 Retirement Window PNG/JPEG thumbnail (2x supersampled 2320x840 LANCZOS)
    with full auto-log scaling, proper RGBA alpha composition matching the Retirement Simulator tab,
    translucent Monte Carlo bands, large legible typography, and on-top milestone & scheduled action pills.
    """
    try:
        from PIL import Image, ImageDraw
    except Exception as exc:
        log.warning("Pillow not available for snapshot generation: %s", exc)
        return False

    W, H = 2320, 840
    base_img = Image.new("RGBA", (W, H), (11, 17, 32, 255))
    draw_base = ImageDraw.Draw(base_img)

    # Font sizes scaled for 2320x840 -> 430px card display (~5.4x downscaling)
    f_axis = _load_pil_font(34, bold=True, mono=True)
    f_pill = _load_pil_font(28, bold=True, mono=True)

    pad_l = 150
    pad_r = 60
    pad_t = 64
    pad_b = 82
    pw = W - pad_l - pad_r
    ph = H - pad_t - pad_b
    axis_y = pad_t + ph

    bars = (eval_result or {}).get("bars") or []
    if not bars:
        out_png = base_img.resize((1160, 420), Image.Resampling.LANCZOS)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        png_early = dest_path.with_suffix(".png")
        out_png.save(png_early, format="PNG", optimize=True)
        _chmod_0600_safe(png_early)
        if dest_path.suffix in (".jpg", ".jpeg"):
            out_png.convert("RGB").save(dest_path, format="JPEG", quality=92, optimize=True)
            _chmod_0600_safe(dest_path)
        return True

    active_lens = str(kpi_summary.get("active_lens") or settings.get("simActiveModel") or "single_path")
    mc_band = ((eval_result or {}).get("monte_carlo") or {}).get("band") or []
    coh_band = ((eval_result or {}).get("historical_cohorts") or {}).get("band") or []
    band = mc_band if (active_lens == "range" and mc_band) else (coh_band if (active_lens == "historical" and coh_band) else [])

    if payload is None:
        try:
            payload = llm_tools.get_cached_payload()
        except Exception:
            payload = {}
    sim_assets = (payload or {}).get("retirement_sim_assets") or []
    sim_assets_sum = sum(float(a.get("current_value") or 0.0) for a in sim_assets if isinstance(a, dict))

    total_months = max(1, len(bars) - 1)
    raw_bar0_val = bars[0].get("totalRealVal")
    start_val = float(raw_bar0_val) if (raw_bar0_val is not None and float(raw_bar0_val) > 0) else sim_assets_sum

    # Determine max value
    max_val = 1_000_000.0
    if band:
        max_val = max(
            [float((b.get("real") or {}).get("p90") or 0.0) for b in band]
            + [float(b.get("totalRealVal") or 0.0) for b in bars]
            + [1_000_000.0]
        )
    else:
        max_val = max([float(b.get("totalRealVal") or 0.0) for b in bars] + [1_000_000.0])

    scale_mode = str(settings.get("simScaleMode") or "auto").lower()
    is_log = (scale_mode == "log") or (scale_mode == "auto" and (max_val > 60e6 or (start_val > 0 and max_val / start_val >= 3.2)))

    y0 = 2_500_000.0
    if not is_log:
        y_max = max_val * 1.08
    else:
        log_milestones = [
            5e6, 10e6, 20e6, 30e6, 50e6, 75e6, 100e6, 150e6, 200e6, 250e6, 300e6, 500e6,
            750e6, 1e9, 1.5e9, 2e9, 3e9, 5e9, 10e9, 20e9, 50e9
        ]
        y_max = next((m for m in log_milestones if m >= max_val * 1.04), math.ceil(max_val / 1e9) * 1e9)

    def val_to_norm(v: float) -> float:
        clamped = max(0.0, float(v or 0.0))
        if not is_log:
            return clamped / y_max if y_max > 0 else 0.0
        return math.log10(1.0 + clamped / y0) / math.log10(1.0 + y_max / y0)

    def y_of(v: float) -> float:
        norm = max(0.0, min(1.0, val_to_norm(v)))
        return axis_y - norm * ph

    def x_of(m_idx: float) -> float:
        return pad_l + (max(0.0, min(float(total_months), float(m_idx))) / float(total_months)) * pw

    # Choose ticks
    if not is_log:
        ticks = [0.0, y_max * 0.25, y_max * 0.5, y_max * 0.75, y_max]
    else:
        log_candidates = [0, 2.5e6, 5e6, 10e6, 25e6, 50e6, 100e6, 250e6, 500e6, 1e9, 2e9, 5e9, 10e9, 20e9]
        ticks = [0.0]
        last_y = y_of(0)
        for c in log_candidates:
            if c <= 0 or c > y_max:
                continue
            cy = y_of(c)
            if last_y - cy >= 75:
                ticks.append(float(c))
                last_y = cy
        if y_of(ticks[-1]) - y_of(y_max) >= 55:
            ticks.append(float(y_max))
        else:
            ticks[-1] = float(y_max)

    # Horizontal gridlines + Y labels on base layer
    for tv in ticks:
        yy = y_of(tv)
        draw_base.line((pad_l, yy, pad_l + pw, yy), fill=(30, 48, 74, 180 if tv > 0 else 255), width=2 if tv > 0 else 3)
        if tv == 0:
            lbl = "$0"
        elif tv >= 1e9:
            lbl = f"${tv/1e9:.1f}B" if (tv % 1e9) != 0 else f"${tv/1e9:.0f}B"
        elif tv >= 1e6:
            lbl = f"${tv/1e6:.1f}M" if (tv % 1e6) != 0 and tv < 100e6 else f"${tv/1e6:.0f}M"
        else:
            lbl = f"${tv/1e3:.0f}K"
        draw_base.text((pad_l - 18, yy - 18), lbl, fill=(148, 163, 184, 255), font=f_axis, anchor="ra")

    # X-axis year ticks (Today, +10y, +20y, +30y, +40y)
    for yr in range(0, 41, 10):
        m_i = yr * 12
        if m_i > total_months:
            break
        xx = x_of(m_i)
        draw_base.line((xx, axis_y, xx, axis_y + 12), fill=(100, 116, 139, 255), width=2)
        xlbl = "Today" if yr == 0 else f"+{yr}y"
        draw_base.text((xx, axis_y + 20), xlbl, fill=(148, 163, 184, 255), font=f_axis, anchor="ma")

    # Outer border on base layer
    draw_base.rounded_rectangle((10, 10, W - 11, H - 11), radius=22, outline=(38, 51, 77, 255), width=3)

    # 1. LAYER 1: Stacked Asset Bars (composition backbone)
    bars_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw_bars = ImageDraw.Draw(bars_layer)

    bw = max(4, int(pw / total_months))
    asset_palette: dict[str, tuple[int, int, int]] = {
        "primary_equity": (56, 189, 248),
        "single_stock": (56, 189, 248),
        "goog": (56, 189, 248),
        "broad_equity": (52, 211, 153),
        "muni_bond": (34, 211, 238),
        "fixed_income": (148, 163, 184),
        "treasury_bills": (148, 163, 184),
        "cash": (203, 213, 225),
        "cash_sweeps": (203, 213, 225),
    }
    for idx_a, a in enumerate(sim_assets):
        if isinstance(a, dict) and a.get("id"):
            aid_s = str(a["id"])
            rgb = _hex_to_rgb(a.get("color"))
            if rgb is not None:
                asset_palette[aid_s] = rgb
            elif aid_s not in asset_palette:
                rc = str(a.get("default_risk_class") or a.get("bucket") or "").lower()
                asset_palette[aid_s] = asset_palette.get(rc, _FALLBACK_BAR_PALETTE[idx_a % len(_FALLBACK_BAR_PALETTE)])
    for idx_h, hyp in enumerate(settings.get("simHypotheticalAssets") or []):
        if isinstance(hyp, dict) and hyp.get("id"):
            hid_s = str(hyp["id"])
            rgb = _hex_to_rgb(hyp.get("color"))
            asset_palette[hid_s] = rgb if rgb is not None else _FALLBACK_BAR_PALETTE[(len(sim_assets) + idx_h) % len(_FALLBACK_BAR_PALETTE)]

    default_order = [str(a["id"]) for a in sim_assets if isinstance(a, dict) and a.get("id")] or [
        "cash", "treasury_bills", "muni_bond", "broad_equity", "primary_equity"
    ]
    raw_order = [str(x) for x in (settings.get("simAssetOrder") or default_order) if x]
    seen_order = set(raw_order)
    for b_probe in bars[:2]:
        for extra_aid in (b_probe.get("assetVals") or {}).keys():
            if extra_aid not in seen_order:
                raw_order.append(str(extra_aid))
                seen_order.add(str(extra_aid))
    order = list(reversed(raw_order))

    step_n = max(1, len(bars) // 480)
    alpha = int(255 * 0.40) if (band or active_lens == "range") else int(255 * 0.92)

    for b in bars[::step_n]:
        m_idx = b["idx"]
        bx = x_of(m_idx)
        defl = float(b.get("deflator") or 1.0)
        av = b.get("assetVals") or {}

        cum_val = 0.0
        if av:
            for idx_col, aid in enumerate(order):
                val = float(av.get(aid) or 0.0) / defl
                if val <= 0.01:
                    continue
                y_bot = y_of(cum_val)
                y_top = y_of(cum_val + val)
                col = asset_palette.get(aid, _FALLBACK_BAR_PALETTE[idx_col % len(_FALLBACK_BAR_PALETTE)])
                draw_bars.rectangle((bx - bw // 2, y_top, bx + bw // 2, y_bot), fill=(*col, alpha))
                cum_val += val
        else:
            ngv = float(b.get("nonGoogVal") or 0.0) / defl
            gv = float(b.get("googVal") or 0.0) / defl
            if ngv > 0.01:
                draw_bars.rectangle((bx - bw // 2, y_of(ngv), bx + bw // 2, y_of(0)), fill=(52, 211, 153, alpha))
            if gv > 0.01:
                draw_bars.rectangle((bx - bw // 2, y_of(ngv + gv), bx + bw // 2, y_of(ngv)), fill=(56, 189, 248, alpha))

    # Alpha-composite bars onto base
    comp_img = Image.alpha_composite(base_img, bars_layer)

    # 2. LAYER 2: Quantile fan bands & P50 median curve (`range` Monte Carlo AND `historical` Cohorts; DA2-10b)
    if active_lens in ("range", "historical") and band:
        fan_rgb = (90, 216, 166) if active_lens == "range" else (56, 189, 248)
        band_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        draw_band = ImageDraw.Draw(band_layer)

        pts_p90 = [(x_of(b["monthIdx"]), y_of(float(b["real"]["p90"]))) for b in band]
        pts_p10 = [(x_of(b["monthIdx"]), y_of(float(b["real"]["p10"]))) for b in reversed(band)]
        draw_band.polygon(pts_p90 + pts_p10, fill=(*fan_rgb, 31))

        pts_p75 = [(x_of(b["monthIdx"]), y_of(float(b["real"]["p75"]))) for b in band]
        pts_p25 = [(x_of(b["monthIdx"]), y_of(float(b["real"]["p25"]))) for b in reversed(band)]
        draw_band.polygon(pts_p75 + pts_p25, fill=(*fan_rgb, 56))

        comp_img = Image.alpha_composite(comp_img, band_layer)

        # Median line and boundary lines
        fg_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        draw_fg = ImageDraw.Draw(fg_layer)

        # Dashed p90 and p10 edges
        for pts, col in [(pts_p90, (*fan_rgb, 140)), ([(x_of(b["monthIdx"]), y_of(float(b["real"]["p10"]))) for b in band], (*fan_rgb, 140))]:
            for i in range(len(pts) - 1):
                if i % 2 == 0:
                    draw_fg.line([pts[i], pts[i+1]], fill=col, width=3)

        # Median P50 curve
        pts_p50 = [(x_of(b["monthIdx"]), y_of(float(b["real"]["p50"]))) for b in band]
        draw_fg.line(pts_p50, fill=(*fan_rgb, 255), width=6)
        comp_img = Image.alpha_composite(comp_img, fg_layer)

    # Stress crash shading (if active_lens == "stress")
    if active_lens == "stress":
        stress_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        draw_stress = ImageDraw.Draw(stress_layer)
        x0 = x_of(0)
        x12 = x_of(12)
        draw_stress.rectangle((x0, pad_t, x12, axis_y), fill=(244, 63, 94, 38))
        comp_img = Image.alpha_composite(comp_img, stress_layer)

    # 3. LAYER 3: Milestones & Scheduled Actions (High-contrast, on top)
    top_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw_top = ImageDraw.Draw(top_layer)

    # Investor milestone lines & capsules (guarded for single-investor portfolios; SA2-22)
    milestones_dict = (eval_result or {}).get("milestones") or {}
    cfg_investors = ((payload or {}).get("project") or {}).get("investors")
    has_second_investor = not (isinstance(cfg_investors, list) and len(cfg_investors) < 2)
    ms1 = milestones_dict.get("investor_1_milestone")
    ms2 = milestones_dict.get("investor_2_milestone") if has_second_investor else None
    if isinstance(ms1, dict) and isinstance(ms2, dict):
        same_id = bool(ms1.get("investor_id") and ms1.get("investor_id") == ms2.get("investor_id"))
        same_name_month = (
            str(ms1.get("investor_name") or "").strip().lower() == str(ms2.get("investor_name") or "").strip().lower()
            and ms1.get("month_idx") == ms2.get("month_idx")
        )
        if same_id or same_name_month:
            ms2 = None

    ms_configs = [
        (ms2, (56, 189, 248, 255), (186, 230, 253, 255)),  # Sky #38bdf8 / #bae6fd
        (ms1, (45, 212, 191, 255), (153, 246, 228, 255)),  # Teal #2dd4bf / #99f6e4
    ]
    placed_top_tags: list[tuple[float, float, int]] = []
    for ms_info, ms_stroke, ms_fg in ms_configs:
        if not isinstance(ms_info, dict):
            continue
        ms_month = int(ms_info.get("month_idx") if ms_info.get("month_idx") is not None else -1)
        if not (0 < ms_month <= total_months):
            continue
        raw_ms_age = ms_info.get("milestone_age")
        if raw_ms_age is None:
            continue
        ms_age = int(raw_ms_age)
        inv_name = str(ms_info.get("investor_name") or "").strip()
        ms_label = f"{inv_name}'s {_ordinal_age_str(ms_age)}" if inv_name else f"Age {ms_age}"
        mx = x_of(ms_month)
        for yy in range(int(pad_t), int(axis_y), 14):
            draw_top.line((mx, yy, mx, min(axis_y, yy + 7)), fill=(ms_stroke[0], ms_stroke[1], ms_stroke[2], 242), width=3)
        bbox = draw_top.textbbox((0, 0), ms_label, font=f_pill)
        txt_w = bbox[2] - bbox[0]
        pill_w = max(140, txt_w + 36)
        pill_h = 40
        pill_x = min(pad_l + pw - pill_w - 6, max(pad_l + 6, mx - pill_w / 2))
        pill_cx = pill_x + pill_w / 2
        row_slot = 0
        while row_slot < 3 and any(
            r == row_slot and abs(cx - pill_cx) < (w / 2 + pill_w / 2 + 12)
            for cx, w, r in placed_top_tags
        ):
            row_slot += 1
        placed_top_tags.append((pill_cx, pill_w, row_slot))
        pill_y = pad_t + 6 + row_slot * (pill_h + 8)
        draw_top.rounded_rectangle(
            (pill_x, pill_y, pill_x + pill_w, pill_y + pill_h),
            radius=8,
            fill=(15, 23, 42, 240),
            outline=ms_stroke,
            width=2,
        )
        draw_top.text((pill_cx, pill_y + pill_h / 2), ms_label, fill=ms_fg, font=f_pill, anchor="mm")

    # Scheduled action markers & capsules (matching app_simulator.js ACT_STYLE)
    act_colors = {
        "withdrawal": {"stroke": (244, 63, 94, 255), "bg": (76, 5, 25, 240), "fg": (254, 205, 211, 255), "sym": "↓"},
        "deposit":    {"stroke": (16, 185, 129, 255), "bg": (6, 78, 59, 240), "fg": (167, 243, 208, 255), "sym": "↑"},
        "transfer":   {"stroke": (34, 211, 238, 255), "bg": (8, 51, 68, 240), "fg": (165, 243, 252, 255), "sym": "⇄"},
    }
    placed_sw_tags: list[tuple[float, float, int]] = []
    action_evs = [(b["idx"], b.get("schedActionEvents") or []) for b in bars if b.get("schedActionEvents")]
    for m_idx, evs in action_evs:
        first_ev = evs[0]
        atype = str(first_ev.get("type") or "withdrawal")
        style = act_colors.get(atype, act_colors["withdrawal"])
        ax = x_of(m_idx)
        for yy in range(int(pad_t), int(axis_y), 16):
            draw_top.line((ax, yy, ax, min(axis_y, yy + 9)), fill=(style["stroke"][0], style["stroke"][1], style["stroke"][2], 242), width=3)

        extra_cnt = len(evs) - 1
        raw_cmt = f"{style['sym']} {str(first_ev.get('comment') or atype or 'Action').strip()}"
        max_chars = 13 if extra_cnt > 0 else 16
        trunc_cmt = (raw_cmt[: max_chars - 1] + "…") if len(raw_cmt) > max_chars else raw_cmt
        if extra_cnt > 0:
            trunc_cmt += f" (+{extra_cnt})"

        bbox = draw_top.textbbox((0, 0), trunc_cmt, font=f_pill)
        txt_w = bbox[2] - bbox[0]
        act_pill_w = max(140, txt_w + 34)
        act_pill_h = 40
        act_pill_x = min(pad_l + pw - act_pill_w - 6, max(pad_l + 6, ax - act_pill_w / 2))
        act_cx = act_pill_x + act_pill_w / 2
        row_slot = 0
        while row_slot < 5 and any(
            r == row_slot and abs(cx - act_cx) < (w / 2 + act_pill_w / 2 + 12)
            for cx, w, r in placed_sw_tags
        ):
            row_slot += 1
        placed_sw_tags.append((act_cx, act_pill_w, row_slot))
        act_pill_y = pad_t + 102 + row_slot * (act_pill_h + 8)
        draw_top.rounded_rectangle(
            (act_pill_x, act_pill_y, act_pill_x + act_pill_w, act_pill_y + act_pill_h),
            radius=8,
            fill=style["bg"],
            outline=style["stroke"],
            width=2,
        )
        draw_top.text(
            (act_cx, act_pill_y + act_pill_h / 2),
            trunc_cmt,
            fill=style["fg"],
            font=f_pill,
            anchor="mm",
        )

    comp_img = Image.alpha_composite(comp_img, top_layer)

    # Downsample to 1160x420 via LANCZOS
    out_img = comp_img.resize((1160, 420), Image.Resampling.LANCZOS)
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    # Save PNG (lossless, full alpha support) and enforce 0600 permissions (SA-08 / SA3-09)
    png_path = dest_path.with_suffix(".png")
    out_img.save(png_path, format="PNG", optimize=True)
    _chmod_0600_safe(png_path)

    # Save JPEG if explicitly requested for legacy compatibility
    if dest_path.suffix in (".jpg", ".jpeg"):
        out_img.convert("RGB").save(dest_path, format="JPEG", quality=92, optimize=True)
        _chmod_0600_safe(dest_path)

    return True


render_model_snapshot_png = render_model_snapshot_jpg


def render_demo_snapshot_for_model(
    saves_dir: Path,
    model_id: str,
    model_name: str,
    settings: dict[str, Any],
    eval_res: dict[str, Any] | None,
    kpi_summary: dict[str, Any] | None,
) -> bool:
    """
    Renders `snapshots/demo_<id>.png` from cached unmasked payload + 1 fast alternate-mode
    simulation via `DemoTranslator` without downloading pricing or refreshing the site.
    """
    try:
        from privacy import DemoTranslator, load_privacy_config

        raw_pl: dict[str, Any] | None = None
        try:
            raw_pl = llm_tools.get_cached_payload(bypass_export_masking=True)
        except Exception:
            raw_pl = None

        if eval_res is None:
            eval_res, computed_kpi, _ = _evaluate_model_for_store(
                settings,
                fast_mode=True,
                preloaded_payload=raw_pl,
            )
            if not kpi_summary:
                kpi_summary = computed_kpi
        if eval_res is None:
            return False

        priv_cfg = load_privacy_config()
        proj = DemoTranslator(priv_cfg)
        demo_eval = proj.project_obj(eval_res)
        demo_kpi = proj.project_obj(kpi_summary or {})
        demo_settings = proj.project_obj({"settings": settings}).get("settings") or settings
        demo_payload: dict[str, Any] | None = None
        if raw_pl:
            demo_payload = {
                "project": proj.project_obj(raw_pl.get("project") or {}),
                "retirement_sim_assets": proj.project_obj(raw_pl.get("retirement_sim_assets") or []),
            }
        demo_name = proj.map_text(model_name)
        demo_dest = saves_dir / SNAPSHOTS_SUBDIR / f"demo_{model_id}.png"
        with _MODEL_STORE_LOCK:
            if not saves_dir.is_dir() or _is_deleted_portfolio_saves_dir(saves_dir):
                return False
            return render_model_snapshot_png(
                demo_dest,
                demo_name,
                demo_settings,
                demo_eval,
                demo_kpi,
                payload=demo_payload,
            )
    except Exception as exc:
        log.warning("Demo snapshot generation skipped for %s: %s", model_id, exc)
        return False


def _fallback_kpi_summary(settings: dict[str, Any]) -> dict[str, Any]:
    from valuation.simulator import build_action_summary

    lens = str(settings.get("simActiveModel") or "single_path")
    return {
        "active_lens": lens,
        "age_85_real": {"has_quantiles": lens in ("range", "historical"), "value": 0, "p25": 0, "p50": 0, "p75": 0},
        "solvency": {"funded_to_85_pct": 100.0, "years_solvent": 100.0},
        "tax_to_85": {"net_kept_pct": 100.0, "eff_tax_pct": 0.0, "cum_tax_real": 0},
        "action_summary": build_action_summary(settings),
    }


def _evaluate_model_for_store(
    settings: dict[str, Any],
    *,
    fast_mode: bool = False,
    preloaded_payload: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any], float | None]:
    """Computes `(eval_result, kpi_summary, goog_price_anchor)` using cached payload (no DB rebuild/quote download)."""
    try:
        from valuation.simulator import evaluate_active_model

        payload = preloaded_payload if preloaded_payload is not None else llm_tools.get_cached_payload(bypass_export_masking=True)
        raw_goog = payload.get("goog_live_price") or ((payload.get("kpis") or {}).get("goog_price"))
        goog_anchor = round(float(raw_goog), 2) if raw_goog and float(raw_goog) > 0 else None
        eff_in = dict(settings)
        if fast_mode and str(eff_in.get("simActiveModel") or "") == "range":
            eff_in["mcSimulations"] = min(int(eff_in.get("mcSimulations") or 200), 120)
        eval_res = evaluate_active_model(payload, eff_in, seed=1)
        return eval_res, eval_res["kpi_summary"], goog_anchor
    except Exception as exc:
        log.warning("Fallback KPI summary during model store evaluation: %s", exc)
        return None, _fallback_kpi_summary(settings), None


def _resolve_saves_dir(saves_dir: Path | None) -> Path | None:
    if saves_dir is not None:
        return saves_dir
    if SAVES_DIR != _INITIAL_SAVES_DIR:
        return SAVES_DIR
    return get_saves_dir()


def _resolve_db_path(portfolio_id: str | None = None) -> Path | None:
    if DEFAULT_DB_PATH != _INITIAL_DEFAULT_DB_PATH:
        return DEFAULT_DB_PATH
    pid = portfolio_id if portfolio_id is not None else get_active_portfolio_id()
    return get_default_db_path(pid) if pid else None


def _resolve_config_path(portfolio_id: str | None = None) -> Path | None:
    if DEFAULT_CONFIG_PATH != _INITIAL_DEFAULT_CONFIG_PATH:
        return DEFAULT_CONFIG_PATH
    pid = portfolio_id if portfolio_id is not None else get_active_portfolio_id()
    return get_default_config_path(pid) if pid else None


def _snapshot_url_for(saves_dir: Path, snap_rel: str, mtime: int) -> str:
    """Returns a portfolio-slug-free relative snapshot URL (`Scenarios/snapshots/...`; SA2-05)."""
    try:
        png_rel = str(Path(snap_rel).with_suffix(".png"))
        if (saves_dir / png_rel).is_file():
            snap_rel = png_rel
            mtime = int((saves_dir / png_rel).stat().st_mtime)
    except Exception:
        pass
    return f"Scenarios/{snap_rel}?v={mtime}"


def _is_deleted_portfolio_saves_dir(saves_dir: Path) -> bool:
    """Returns True when `saves_dir` belongs to a `Portfolios/<id>/` directory that no longer exists."""
    try:
        resolved = saves_dir.resolve()
        p_root = config.get_portfolios_dir().resolve()
        if resolved.parent.parent == p_root and not resolved.parent.is_dir():
            return True
    except Exception:
        pass
    return False


def _backfill_portfolio_snapshots(saves_dir: Path, is_primary_store: bool, missing_snaps: list[dict[str, Any]]) -> None:
    # The destination pins the household even if the browser switches while the worker runs.
    with config.portfolio_context(saves_dir.parent.name):
        _backfill_missing_snapshots(saves_dir, is_primary_store, missing_snaps)


def _backfill_missing_snapshots(
    saves_dir: Path,
    is_primary_store: bool,
    missing_snaps: list[dict[str, Any]],
) -> None:
    """Regenerates missing scenario or demo thumbnails asynchronously outside `_MODEL_STORE_LOCK`."""
    # DESIGN INTENT: Abort immediately if the owning portfolio was deleted while this daemon thread was queued.
    if not saves_dir.is_dir() or _is_deleted_portfolio_saves_dir(saves_dir):
        return
    try:
        shared_payload = llm_tools.get_cached_payload(bypass_export_masking=True)
    except Exception as exc:
        log.warning("Skipping async snapshot backfill (payload unavailable): %s", exc)
        return

    for m in missing_snaps:
        if not saves_dir.is_dir() or _is_deleted_portfolio_saves_dir(saves_dir):
            return
        fpath = saves_dir / str(m.get("file") or "")
        snap_rel = str(m.get("snapshot") or "")
        if not fpath.is_file():
            continue
        try:
            mdoc = json.loads(fpath.read_text(encoding="utf-8"))
            mset = canonicalize_model_settings(mdoc.get("settings") or {})
            mkpi = mdoc.get("kpi_summary") or {}
            eval_res, mkpi2, _ = _evaluate_model_for_store(
                mset,
                fast_mode=not is_primary_store,
                preloaded_payload=shared_payload,
            )
            if snap_rel and not ((saves_dir / snap_rel).is_file() or (saves_dir / snap_rel).with_suffix(".png").is_file()):
                with _MODEL_STORE_LOCK:
                    if not saves_dir.is_dir() or _is_deleted_portfolio_saves_dir(saves_dir):
                        return
                    render_model_snapshot_jpg(
                        saves_dir / snap_rel,
                        str(mdoc.get("name") or m.get("name") or "Scenario"),
                        mset,
                        eval_res,
                        mkpi or mkpi2,
                    )
            mid_str = str(mdoc.get("id") or m.get("id") or "")
            if not saves_dir.is_dir() or _is_deleted_portfolio_saves_dir(saves_dir):
                return
            if mid_str and not (saves_dir / SNAPSHOTS_SUBDIR / f"demo_{mid_str}.png").is_file():
                render_demo_snapshot_for_model(
                    saves_dir,
                    mid_str,
                    str(mdoc.get("name") or m.get("name") or "Scenario"),
                    mset,
                    eval_res,
                    mkpi or mkpi2,
                )
        except Exception as exc:
            log.warning("Snapshot regeneration skipped for %s: %s", fpath, exc)


def ensure_models_migrated(saves_dir: Path | None = None) -> dict[str, Any]:
    """
    Ensures `saves_dir/_index.json` and `saves_dir/snapshots/` exist.
    Performs a one-time atomic migration from legacy `<Name>.json` + `_meta.json` if `_index.json` is absent.
    """
    target_dir = _resolve_saves_dir(saves_dir)
    if target_dir is None:
        return {"version": 1, "active_id": "", "next_seq": 1, "models": []}
    saves_dir = target_dir
    dir_key = str(saves_dir.resolve())
    with _MODEL_STORE_LOCK:
        # DESIGN INTENT: Never resurrect a deleted `Portfolios/<id>/` directory from an in-flight read or thread.
        if _is_deleted_portfolio_saves_dir(saves_dir):
            return {"version": 1, "active_id": "", "next_seq": 1, "models": []}
        saves_dir.mkdir(parents=True, exist_ok=True)
        (saves_dir / SNAPSHOTS_SUBDIR).mkdir(parents=True, exist_ok=True)
        idx = _read_index(saves_dir)
        if dir_key in _MIGRATING_DIRS:
            return idx or {"version": 1, "active_id": "", "next_seq": 1, "models": []}

        _MIGRATING_DIRS.add(dir_key)
        try:
            active_sdir = get_saves_dir()
            is_primary_store = active_sdir is not None and saves_dir.resolve() == active_sdir.resolve()

            if idx is not None:
                if len(idx.get("models") or []) == 0:
                    return idx
                # Ensure every indexed model still exists on disk
                valid_entries: list[dict[str, Any]] = []
                missing_snaps: list[dict[str, Any]] = []
                for m in idx.get("models") or []:
                    fpath = saves_dir / str(m.get("file") or "")
                    if fpath.is_file():
                        valid_entries.append(m)
                        snap_rel = str(m.get("snapshot") or "")
                        mid_str = str(m.get("id") or "")
                        has_snap = (
                            not snap_rel
                            or (saves_dir / snap_rel).is_file()
                            or (saves_dir / snap_rel).with_suffix(".png").is_file()
                        )
                        demo_rel = f"{SNAPSHOTS_SUBDIR}/demo_{mid_str}.png" if mid_str else ""
                        has_demo = (not demo_rel) or (saves_dir / demo_rel).is_file()
                        if IN_SERVER_PROCESS and is_primary_store and (not has_snap or not has_demo):
                            attempt_key = (dir_key, mid_str or str(m.get("file") or ""))
                            with _SNAPSHOT_BACKFILL_LOCK:
                                if attempt_key not in _SNAPSHOT_BACKFILL_ATTEMPTED:
                                    _SNAPSHOT_BACKFILL_ATTEMPTED.add(attempt_key)
                                    missing_snaps.append(dict(m))
                if valid_entries:
                    if len(valid_entries) != len(idx.get("models") or []):
                        idx["models"] = valid_entries
                        if not any(e["id"] == idx.get("active_id") for e in valid_entries):
                            idx["active_id"] = valid_entries[0]["id"]
                        _write_index(saves_dir, idx)
                    if missing_snaps:
                        threading.Thread(
                            target=_backfill_portfolio_snapshots,
                            args=(saves_dir, is_primary_store, missing_snaps),
                            daemon=True,
                        ).start()
                    return idx

            # One-time migration (or fresh empty store initialization)
            legacy_meta = {}
            meta_path = saves_dir / META_FILENAME
            if meta_path.is_file():
                try:
                    legacy_meta = json.loads(meta_path.read_text(encoding="utf-8"))
                except Exception:
                    legacy_meta = {}
            legacy_active_name = str(legacy_meta.get("active_save") or "").strip()

            candidates: list[Path] = []
            for p in saves_dir.glob("*.json"):
                low = p.name.lower()
                if p.name in (META_FILENAME, INDEX_FILENAME, GLOBAL_PREFERENCES_FILENAME):
                    continue
                if low.startswith("_") or low.startswith("global_"):
                    continue
                candidates.append(p)

            def _migration_rank(p: Path) -> tuple[int, str]:
                nk = _norm_reserved_key(p.stem)
                if nk.endswith("sdefault") and nk != "default":
                    return (0, p.name.lower())
                if nk == "default":
                    return (1, p.name.lower())
                if nk == "zero":
                    return (2, p.name.lower())
                return (3, p.name.lower())

            candidates.sort(key=_migration_rank)

            if not candidates:
                # Fresh empty directory -> write 001_default.json + _index.json first, then evaluate & render snapshot
                def_settings = default_settings_snapshot()
                stub_kpi = _fallback_kpi_summary(def_settings)
                now_iso = _now_iso()
                mid = "001"
                slug = slugify_model_name(DEFAULT_SAVE_NAME)
                fname = f"{mid}_{slug}.json"
                snap_rel = f"{SNAPSHOTS_SUBDIR}/{mid}_{slug}.jpg"
                notes = _default_migrated_notes(DEFAULT_SAVE_NAME, def_settings, stub_kpi)
                doc = {
                    "id": mid,
                    "name": DEFAULT_SAVE_NAME,
                    "created_at": now_iso,
                    "updated_at": now_iso,
                    "snapshot": snap_rel,
                    "author": _normalize_author("human"),
                    "goog_price_anchor": None,
                    "kpi_summary": stub_kpi,
                    "settings": def_settings,
                    "notes_md": notes,
                }
                _write_json_lf(saves_dir / fname, doc)
                idx_doc = {
                    "version": 1,
                    "active_id": mid,
                    "next_seq": 2,
                    "models": [
                        {
                            "id": mid,
                            "name": DEFAULT_SAVE_NAME,
                            "file": fname,
                            "snapshot": snap_rel,
                            "created_at": now_iso,
                            "updated_at": now_iso,
                        }
                    ],
                }
                _write_index(saves_dir, idx_doc)
                eval_res, kpi_sum, g_anchor = _evaluate_model_for_store(def_settings, fast_mode=not is_primary_store)
                doc["kpi_summary"] = kpi_sum
                doc["goog_price_anchor"] = g_anchor
                _write_json_lf(saves_dir / fname, doc)
                render_model_snapshot_jpg(saves_dir / snap_rel, DEFAULT_SAVE_NAME, def_settings, eval_res, kpi_sum)
                return idx_doc

            migrated_dir = saves_dir / ".migrated_legacy"
            migrated_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%dT%H%M%S")

            index_models: list[dict[str, Any]] = []
            pending_docs: list[tuple[Path, Path, dict[str, Any], dict[str, Any]]] = []
            active_id = ""
            seq = 1

            # Phase 1: Write canonical JSON files + _index.json FIRST (zero re-entrancy risk)
            for src_path in candidates:
                try:
                    raw = json.loads(src_path.read_text(encoding="utf-8"))
                except Exception as exc:
                    log.warning("Skipping corrupt save file during migration %s: %s", src_path, exc)
                    continue
                stat = src_path.stat()
                mtime_iso = datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec="seconds")
                friendly_name = sanitize_save_name(str(raw.get("name") or src_path.stem))
                raw_settings = raw.get("settings") if isinstance(raw.get("settings"), dict) else raw
                canon_settings = canonicalize_model_settings(raw_settings)

                mid = f"{seq:03d}"
                seq += 1
                slug = slugify_model_name(friendly_name)
                fname = f"{mid}_{slug}.json"
                snap_rel = f"{SNAPSHOTS_SUBDIR}/{mid}_{slug}.jpg"
                stub_kpi = raw.get("kpi_summary") if isinstance(raw.get("kpi_summary"), dict) else _fallback_kpi_summary(canon_settings)

                created_iso = str(raw.get("created_at") or mtime_iso)
                updated_iso = _now_iso() if is_primary_store else mtime_iso
                notes_md = str(raw.get("notes_md") or "").strip() or _default_migrated_notes(
                    friendly_name, canon_settings, stub_kpi
                )

                doc = {
                    "id": mid,
                    "name": friendly_name,
                    "created_at": created_iso,
                    "updated_at": updated_iso,
                    "snapshot": snap_rel,
                    "author": _normalize_author(raw.get("author")),
                    "goog_price_anchor": raw.get("goog_price_anchor"),
                    "kpi_summary": stub_kpi,
                    "settings": canon_settings,
                    "notes_md": notes_md,
                }
                dest_file = saves_dir / fname
                _write_json_lf(dest_file, doc)
                pending_docs.append((dest_file, saves_dir / snap_rel, doc, raw))

                index_models.append(
                    {
                        "id": mid,
                        "name": friendly_name,
                        "file": fname,
                        "snapshot": snap_rel,
                        "created_at": created_iso,
                        "updated_at": updated_iso,
                    }
                )
                if legacy_active_name and (
                    friendly_name.lower() == legacy_active_name.lower()
                    or src_path.stem.lower() == legacy_active_name.lower()
                ):
                    active_id = mid

                if src_path.resolve() != dest_file.resolve() and src_path.exists():
                    src_path.rename(migrated_dir / f"{src_path.stem}.{stamp}.json")

            if meta_path.exists():
                meta_path.rename(migrated_dir / f"_meta.{stamp}.json")

            if not active_id and index_models:
                active_id = index_models[0]["id"]

            idx_doc = {
                "version": 1,
                "active_id": active_id,
                "next_seq": seq,
                "models": index_models,
            }
            _write_index(saves_dir, idx_doc)

            # Phase 2: Load live payload once, populate exact KPI summaries & render JPEG snapshots
            try:
                from valuation.simulator import load_live_payload

                shared_payload = load_live_payload()
            except Exception as exc:
                log.warning("Could not preload payload during migration: %s", exc)
                shared_payload = None

            for dest_file, snap_path, doc, raw in pending_docs:
                canon_settings = doc["settings"]
                eval_res, kpi_sum, g_anchor = _evaluate_model_for_store(
                    canon_settings,
                    fast_mode=not is_primary_store,
                    preloaded_payload=shared_payload,
                )
                doc["kpi_summary"] = kpi_sum
                if not doc.get("goog_price_anchor") and g_anchor:
                    doc["goog_price_anchor"] = g_anchor
                if not str(raw.get("notes_md") or "").strip():
                    doc["notes_md"] = _default_migrated_notes(doc["name"], canon_settings, kpi_sum)
                _write_json_lf(dest_file, doc)
                render_model_snapshot_jpg(snap_path, doc["name"], canon_settings, eval_res, kpi_sum)

            return idx_doc
        finally:
            _MIGRATING_DIRS.discard(dir_key)


def ensure_default_save(saves_dir: Path | None = None) -> None:
    """Ensures `Saves/_index.json` is initialized/migrated and at least one canonical Scenario exists."""
    target_dir = _resolve_saves_dir(saves_dir)
    if target_dir is None:
        return
    ensure_models_migrated(target_dir)


def get_model_entry(id_or_name: str, saves_dir: Path | None = None) -> dict[str, Any] | None:
    """Finds an indexed model entry in `_index.json` by `id` ('001' or '1'), friendly `name`, or `file`."""
    target = str(id_or_name or "").strip()
    if not target:
        return None
    target_dir = _resolve_saves_dir(saves_dir)
    if target_dir is None:
        return None
    idx = ensure_models_migrated(target_dir)
    models = idx.get("models") or []
    t_low = target.lower()
    t_pad = f"{int(target):03d}" if target.isdigit() else ""

    for m in models:
        mid = str(m.get("id") or "")
        if mid.lower() == t_low or (t_pad and mid == t_pad):
            return m
    for m in models:
        if str(m.get("name") or "").strip().lower() == t_low:
            return m
    for m in models:
        f = str(m.get("file") or "")
        if f.lower() == t_low or Path(f).stem.lower() == t_low:
            return m
    for m in models:
        if _norm_reserved_key(str(m.get("name") or "")) == _norm_reserved_key(target):
            return m
    return None


def list_saves_payload(saves_dir: Path | None = None) -> dict[str, Any]:
    """
    Returns all canonical Scenarios indexed by `Scenarios/_index.json`.
    Exposes both `models` (canonical) and `saves` (alias) so all UI and CLI consumers work seamlessly.
    """
    target_dir = _resolve_saves_dir(saves_dir)
    if target_dir is None:
        return {
            "ok": True,
            "saves_dir": "",
            "active_id": "",
            "active_save": "",
            "next_seq": 1,
            "models": [],
            "saves": [],
        }
    saves_dir = target_dir
    with _MODEL_STORE_LOCK:
        idx = ensure_models_migrated(saves_dir)
        active_id = str(idx.get("active_id") or "")
        models_out: list[dict[str, Any]] = []

        for entry in idx.get("models") or []:
            fname = str(entry.get("file") or "")
            fpath = saves_dir / fname
            if not fpath.is_file():
                continue
            try:
                raw = json.loads(fpath.read_text(encoding="utf-8"))
            except Exception:
                continue
            stat = fpath.stat()
            mid = str(raw.get("id") or entry.get("id") or "")
            name = str(raw.get("name") or entry.get("name") or fpath.stem)
            snap_rel = str(raw.get("snapshot") or entry.get("snapshot") or f"{SNAPSHOTS_SUBDIR}/{fpath.stem}.jpg")
            settings = canonicalize_model_settings(
                raw.get("settings") if isinstance(raw.get("settings"), dict) else {}
            )
            created_at = str(raw.get("created_at") or entry.get("created_at") or _now_iso())
            updated_at = str(raw.get("updated_at") or entry.get("updated_at") or created_at)
            snap_file = saves_dir / snap_rel
            has_snapshot = snap_file.is_file() or snap_file.with_suffix(".png").is_file()
            snap_mtime = int(snap_file.stat().st_mtime) if snap_file.is_file() else int(stat.st_mtime)
            demo_rel = f"{SNAPSHOTS_SUBDIR}/demo_{mid}.png" if mid else ""
            demo_file = (saves_dir / demo_rel) if demo_rel else None
            demo_exists = bool(demo_file is not None and demo_file.is_file())
            demo_mtime = int(demo_file.stat().st_mtime) if demo_exists else snap_mtime

            models_out.append(
                {
                    "id": mid,
                    "name": name,
                    "file": fname,
                    "filename": fname,
                    "snapshot": snap_rel if has_snapshot else "",
                    "snapshot_url": _snapshot_url_for(saves_dir, snap_rel, snap_mtime) if has_snapshot else "",
                    "demo_snapshot": demo_rel if demo_exists else "",
                    "demo_snapshot_url": f"Scenarios/{demo_rel}?v={demo_mtime}" if demo_exists else "",
                    "created_at": created_at,
                    "updated_at": updated_at,
                    "updated_ts": round(stat.st_mtime, 3),
                    "is_protected": _is_scenario_doc_protected(raw, saves_dir, fallback_name=name),
                    "author": _normalize_author(raw.get("author")),
                    "goog_price_anchor": raw.get("goog_price_anchor"),
                    "kpi_summary": raw.get("kpi_summary") or {},
                    "settings": settings,
                    "notes_md": str(raw.get("notes_md") or ""),
                }
            )

        ids_map = {m["id"]: m for m in models_out}
        if not active_id or active_id not in ids_map:
            def_m = next((m for m in models_out if m["name"] == DEFAULT_SAVE_NAME), None)
            active_id = def_m["id"] if def_m else (models_out[0]["id"] if models_out else "")
        active_save = ids_map[active_id]["name"] if active_id in ids_map else DEFAULT_SAVE_NAME

        return {
            "ok": True,
            "saves_dir": "",
            "active_id": active_id,
            "active_save": active_save,
            "next_seq": int(idx.get("next_seq") or (len(models_out) + 1)),
            "models": models_out,
            "saves": models_out,
        }


def _decode_snapshot_bytes(image_data: str | bytes | None) -> bytes | None:
    if not image_data:
        return None
    if isinstance(image_data, bytes):
        return image_data
    s = str(image_data).strip()
    if not s:
        return None
    if s.startswith("data:image/"):
        try:
            _, b64 = s.split(",", 1)
            return base64.b64decode(b64)
        except Exception:
            return None
    try:
        return base64.b64decode(s)
    except Exception:
        return None


def _write_jpeg_bytes(dest_path: Path, raw_bytes: bytes) -> bool:
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        from PIL import Image

        with Image.open(io.BytesIO(raw_bytes)) as im:
            if dest_path.suffix.lower() == ".png" and im.format == "PNG" and im.size == (1160, 420):
                dest_path.write_bytes(raw_bytes)
                _chmod_0600_safe(dest_path)
                return True
            rgba = im.convert("RGBA")
            if rgba.size != (1160, 420):
                rgba = rgba.resize((1160, 420), Image.Resampling.LANCZOS)
            png_dest = dest_path.with_suffix(".png")
            rgba.save(png_dest, format="PNG")
            _chmod_0600_safe(png_dest)
            if dest_path.suffix in (".jpg", ".jpeg"):
                rgba.convert("RGB").save(dest_path, format="JPEG", quality=92)
                _chmod_0600_safe(dest_path)
        return True
    except Exception:
        dest_path.write_bytes(raw_bytes)
        _chmod_0600_safe(dest_path)
        return True


def write_save_file(
    name: str,
    settings: dict[str, Any],
    set_active: bool = True,
    saves_dir: Path | None = None,
    *,
    model_id: str | None = None,
    create_new: bool = False,
    author: Any = None,
    notes_md: str | None = None,
    kpi_summary: dict[str, Any] | None = None,
    goog_price_anchor: float | None = None,
    snapshot_data_url: str | bytes | None = None,
    is_protected: bool | None = None,
    allow_protected_overwrite: bool = True,
) -> dict[str, Any]:
    """
    Creates or updates a canonical Scenario JSON + `_index.json` + `1160x420` `.png` snapshot.
    """
    target_dir = _resolve_saves_dir(saves_dir)
    if target_dir is None:
        return {"ok": False, "error": "No active portfolio selected."}
    saves_dir = target_dir
    with _MODEL_STORE_LOCK:
        idx = ensure_models_migrated(saves_dir)
        active_sdir = get_saves_dir()
        is_primary_store = active_sdir is not None and saves_dir.resolve() == active_sdir.resolve()
        clean_name = sanitize_save_name(name)
        canon_settings = canonicalize_model_settings(settings)
        now_iso = _now_iso()

        # DESIGN INTENT (Scenario Protection & SA2-16):
        # - All newly created scenarios start unprotected (`is_protected = False`); never auto-protect
        #   based on reserved words or investor names. Protection is strictly an explicit user toggle.
        # - Match existing scenarios by `model_id` first, then by `clean_name` fallback, and enforce
        #   `allow_protected_overwrite` on the resolved target so a caller cannot bypass protection
        #   by pairing a bogus `id` with a protected scenario's `name`.
        existing_entry: dict[str, Any] | None = None
        if not create_new:
            if model_id:
                existing_entry = get_model_entry(model_id, saves_dir=saves_dir)
            if existing_entry is None:
                existing_entry = next(
                    (
                        m
                        for m in (idx.get("models") or [])
                        if str(m.get("name") or "").strip().lower() == clean_name.lower()
                    ),
                    None,
                )

        existing_doc: dict[str, Any] = {}
        if existing_entry is not None:
            mid = str(existing_entry["id"])
            fname = str(existing_entry["file"])
            snap_rel = str(existing_entry.get("snapshot") or f"{SNAPSHOTS_SUBDIR}/{Path(fname).stem}.jpg")
            created_at = str(existing_entry.get("created_at") or now_iso)
            fpath = saves_dir / fname
            if fpath.is_file():
                try:
                    existing_doc = json.loads(fpath.read_text(encoding="utf-8"))
                except Exception:
                    existing_doc = {}
            if not allow_protected_overwrite and _is_scenario_doc_protected(existing_doc, saves_dir, fallback_name=clean_name):
                return {
                    "ok": False,
                    "protected": True,
                    "error": f"Scenario {clean_name!r} is protected. Confirm overwrite to proceed.",
                }
        else:
            seq = int(idx.get("next_seq") or 1)
            used_ids = {str(m.get("id") or "") for m in (idx.get("models") or [])}
            while f"{seq:03d}" in used_ids:
                seq += 1
            mid = f"{seq:03d}"
            idx["next_seq"] = seq + 1
            slug = slugify_model_name(clean_name)
            fname = f"{mid}_{slug}.json"
            snap_rel = f"{SNAPSHOTS_SUBDIR}/{mid}_{slug}.png"
            created_at = now_iso

        raw_img = _decode_snapshot_bytes(snapshot_data_url)
        eval_res: dict[str, Any] | None = None
        if kpi_summary is None or raw_img is None:
            eval_res, computed_kpi, computed_anchor = _evaluate_model_for_store(
                canon_settings,
                fast_mode=not is_primary_store,
            )
            if kpi_summary is None:
                kpi_summary = computed_kpi
            if goog_price_anchor is None:
                goog_price_anchor = computed_anchor

        eff_author = (
            _normalize_author(author)
            if author is not None
            else _normalize_author(existing_doc.get("author") or "human")
        )
        eff_notes = (
            str(notes_md)
            if notes_md is not None
            else (
                str(existing_doc.get("notes_md") or "")
                or _default_migrated_notes(clean_name, canon_settings, kpi_summary or {})
            )
        )

        eff_protected = bool(is_protected) if is_protected is not None else bool(existing_doc.get("is_protected", False))
        doc = {
            "id": mid,
            "name": clean_name,
            "created_at": created_at,
            "updated_at": now_iso,
            "snapshot": snap_rel,
            "is_protected": eff_protected,
            "author": eff_author,
            "goog_price_anchor": goog_price_anchor if goog_price_anchor is not None else existing_doc.get("goog_price_anchor"),
            "kpi_summary": kpi_summary or {},
            "settings": canon_settings,
            "notes_md": eff_notes,
        }
        _write_json_lf(saves_dir / fname, doc)

        if raw_img:
            _write_jpeg_bytes(saves_dir / snap_rel, raw_img)
        else:
            render_model_snapshot_jpg(saves_dir / snap_rel, clean_name, canon_settings, eval_res, kpi_summary or {})

        if eval_res is not None:
            render_demo_snapshot_for_model(saves_dir, mid, clean_name, canon_settings, eval_res, kpi_summary or {})
        else:
            t = threading.Timer(
                0.25,
                render_demo_snapshot_for_model,
                args=(saves_dir, mid, clean_name, dict(canon_settings), None, dict(kpi_summary or {})),
            )
            t.daemon = True
            t.start()

        if existing_entry is not None:
            existing_entry["name"] = clean_name
            existing_entry["updated_at"] = now_iso
            existing_entry["snapshot"] = snap_rel
        else:
            idx.setdefault("models", []).append(
                {
                    "id": mid,
                    "name": clean_name,
                    "file": fname,
                    "snapshot": snap_rel,
                    "created_at": created_at,
                    "updated_at": now_iso,
                }
            )

        if set_active:
            idx["active_id"] = mid

        _write_index(saves_dir, idx)
        res = list_saves_payload(saves_dir)
        res["saved_id"] = mid
        res["saved_name"] = clean_name
        return res


def set_active_save(id_or_name: str, saves_dir: Path | None = None) -> dict[str, Any]:
    """Updates `active_id` in `Saves/_index.json` by model `id` or friendly `name`."""
    target_dir = _resolve_saves_dir(saves_dir)
    if target_dir is None:
        return list_saves_payload(None)
    saves_dir = target_dir
    with _MODEL_STORE_LOCK:
        idx = ensure_models_migrated(saves_dir)
        entry = get_model_entry(id_or_name, saves_dir=saves_dir)
        if entry is not None:
            idx["active_id"] = str(entry["id"])
            _write_index(saves_dir, idx)
        return list_saves_payload(saves_dir)


def rename_model(id_or_name: str, new_name: str, saves_dir: Path | None = None) -> dict[str, Any]:
    """
    Updates a Scenario's friendly display `name` in `_index.json` and its JSON file
    WITHOUT renaming `<id>_<slug>.json` or `snapshots/<id>_<slug>.jpg` on disk.
    """
    target_dir = _resolve_saves_dir(saves_dir)
    if target_dir is None:
        return {"ok": False, "error": "No active portfolio selected."}
    saves_dir = target_dir
    with _MODEL_STORE_LOCK:
        idx = ensure_models_migrated(saves_dir)
        entry = get_model_entry(id_or_name, saves_dir=saves_dir)
        if entry is None:
            return {"ok": False, "error": f"Scenario '{id_or_name}' not found."}
        clean_new = sanitize_save_name(new_name)
        fpath = saves_dir / str(entry["file"])
        if not fpath.is_file():
            return {"ok": False, "error": f"Scenario file '{entry['file']}' missing."}
        doc = json.loads(fpath.read_text(encoding="utf-8"))
        now_iso = _now_iso()
        doc["name"] = clean_new
        doc["updated_at"] = now_iso
        _write_json_lf(fpath, doc)

        for m in idx.get("models") or []:
            if str(m.get("id")) == str(entry["id"]):
                m["name"] = clean_new
                m["updated_at"] = now_iso
                break
        _write_index(saves_dir, idx)
        res = list_saves_payload(saves_dir)
        res["renamed_id"] = str(entry["id"])
        res["new_name"] = clean_new
        return res


def set_model_protected(id_or_name: str, is_protected: bool, saves_dir: Path | None = None) -> dict[str, Any]:
    """Sets the user-controlled `is_protected` flag on an existing Scenario without modifying its settings."""
    target_dir = _resolve_saves_dir(saves_dir)
    if target_dir is None:
        return {"ok": False, "error": "No active portfolio selected."}
    saves_dir = target_dir
    with _MODEL_STORE_LOCK:
        ensure_models_migrated(saves_dir)
        entry = get_model_entry(id_or_name, saves_dir=saves_dir)
        if entry is None:
            return {"ok": False, "error": f"Scenario '{id_or_name}' not found."}
        fpath = saves_dir / str(entry["file"])
        if not fpath.is_file():
            return {"ok": False, "error": f"Scenario file '{entry['file']}' missing."}
        doc = json.loads(fpath.read_text(encoding="utf-8"))
        doc["is_protected"] = bool(is_protected)
        _write_json_lf(fpath, doc)
        res = list_saves_payload(saves_dir)
        res["protected_id"] = str(entry["id"])
        res["is_protected"] = bool(is_protected)
        return res


def update_model_notes(id_or_name: str, notes_md: str, saves_dir: Path | None = None) -> dict[str, Any]:
    """Updates `notes_md` on an existing Scenario without modifying its `settings` or `active_id`."""
    target_dir = _resolve_saves_dir(saves_dir)
    if target_dir is None:
        return {"ok": False, "error": "No active portfolio selected."}
    saves_dir = target_dir
    with _MODEL_STORE_LOCK:
        idx = ensure_models_migrated(saves_dir)
        entry = get_model_entry(id_or_name, saves_dir=saves_dir)
        if entry is None:
            return {"ok": False, "error": f"Scenario '{id_or_name}' not found."}
        fpath = saves_dir / str(entry["file"])
        if not fpath.is_file():
            return {"ok": False, "error": f"Scenario file '{entry['file']}' missing."}
        doc = json.loads(fpath.read_text(encoding="utf-8"))
        now_iso = _now_iso()
        doc["notes_md"] = str(notes_md or "")
        doc["updated_at"] = now_iso
        _write_json_lf(fpath, doc)
        for m in idx.get("models") or []:
            if str(m.get("id")) == str(entry["id"]):
                m["updated_at"] = now_iso
                break
        _write_index(saves_dir, idx)
        res = list_saves_payload(saves_dir)
        res["updated_id"] = str(entry["id"])
        return res


def append_model_upvote(
    id_or_name: str,
    model_name: str,
    reason: str,
    saves_dir: Path | None = None,
) -> dict[str, Any]:
    """
    Appends a timestamped AI concurrence / upvote signature under `## Upvotes & Concurrences`
    in `notes_md` WITHOUT modifying `settings`, overwriting parameters, or switching `active_id`.
    Permitted on all scenarios including protected baselines.
    """
    target_dir = _resolve_saves_dir(saves_dir)
    if target_dir is None:
        return {"ok": False, "error": "No active portfolio selected."}
    saves_dir = target_dir
    with _MODEL_STORE_LOCK:
        idx = ensure_models_migrated(saves_dir)
        entry = get_model_entry(id_or_name, saves_dir=saves_dir)
        if entry is None:
            return {"ok": False, "error": f"Scenario '{id_or_name}' not found."}
        fpath = saves_dir / str(entry["file"])
        if not fpath.is_file():
            return {"ok": False, "error": f"Scenario file '{entry['file']}' missing."}
        doc = json.loads(fpath.read_text(encoding="utf-8"))
        clean_model = str(model_name or "advisor").strip() or "advisor"
        clean_reason = re.sub(r"\s+", " ", str(reason or "Concurred with existing strategy.").strip())
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        sig_line = f"- **{stamp} • `{clean_model}`:** {clean_reason}"

        cur_notes = str(doc.get("notes_md") or "").rstrip()
        header = "## Upvotes & Concurrences"
        if header in cur_notes:
            updated_notes = f"{cur_notes}\n{sig_line}\n"
        elif cur_notes:
            updated_notes = f"{cur_notes}\n\n{header}\n{sig_line}\n"
        else:
            updated_notes = f"{header}\n{sig_line}\n"

        now_iso = _now_iso()
        doc["notes_md"] = updated_notes
        doc["updated_at"] = now_iso
        _write_json_lf(fpath, doc)

        for m in idx.get("models") or []:
            if str(m.get("id")) == str(entry["id"]):
                m["updated_at"] = now_iso
                break
        _write_index(saves_dir, idx)
        res = list_saves_payload(saves_dir)
        res["upvoted_id"] = str(entry["id"])
        res["upvoted_name"] = str(doc.get("name") or entry.get("name"))
        res["signature"] = sig_line
        return res


def save_model_snapshot(
    id_or_name: str,
    image_data: str | bytes,
    saves_dir: Path | None = None,
) -> dict[str, Any]:
    """Saves a client-uploaded `<canvas>` JPEG thumbnail (`1160x420`) to `saves_dir/<snapshot>`."""
    target_dir = _resolve_saves_dir(saves_dir)
    if target_dir is None:
        return {"ok": False, "error": "No active portfolio selected."}
    saves_dir = target_dir
    with _MODEL_STORE_LOCK:
        ensure_models_migrated(saves_dir)
        entry = get_model_entry(id_or_name, saves_dir=saves_dir)
        if entry is None:
            return {"ok": False, "error": f"Scenario '{id_or_name}' not found."}
        raw_bytes = _decode_snapshot_bytes(image_data)
        if not raw_bytes:
            return {"ok": False, "error": "Invalid or empty snapshot image payload."}
        snap_rel = str(entry.get("snapshot") or f"{SNAPSHOTS_SUBDIR}/{Path(entry['file']).stem}.jpg")
        dest_path = saves_dir / snap_rel
        _write_jpeg_bytes(dest_path, raw_bytes)
        mtime = int(dest_path.stat().st_mtime)
        return {
            "ok": True,
            "id": str(entry["id"]),
            "snapshot": snap_rel,
            "snapshot_url": _snapshot_url_for(saves_dir, snap_rel, mtime),
        }


def _trash_file(p: Path, saves_dir: Path) -> None:
    if not p.exists():
        return
    trash_dir = saves_dir / ".trash"
    trash_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    p.rename(trash_dir / f"{p.stem}.{stamp}{p.suffix}")


def delete_save_file(id_or_name: str, saves_dir: Path | None = None) -> dict[str, Any]:
    """Moves a Scenario's JSON and `.jpg` snapshot to `Saves/.trash/` and removes it from `_index.json`."""
    target_dir = _resolve_saves_dir(saves_dir)
    if target_dir is None:
        return list_saves_payload(None)
    saves_dir = target_dir
    with _MODEL_STORE_LOCK:
        idx = ensure_models_migrated(saves_dir)
        entry = get_model_entry(id_or_name, saves_dir=saves_dir)
        if entry is None:
            return list_saves_payload(saves_dir)

        mid = str(entry["id"])
        fpath = saves_dir / str(entry["file"])
        snap_rel = str(entry.get("snapshot") or "")
        _trash_file(fpath, saves_dir)
        if snap_rel:
            _trash_file(saves_dir / snap_rel, saves_dir)
        _trash_file(saves_dir / SNAPSHOTS_SUBDIR / f"demo_{mid}.png", saves_dir)

        remaining = [m for m in (idx.get("models") or []) if str(m.get("id")) != mid]
        idx["models"] = remaining
        if str(idx.get("active_id") or "") == mid:
            idx["active_id"] = remaining[0]["id"] if remaining else ""
        _write_index(saves_dir, idx)

        if not remaining:
            return {
                "ok": True,
                "saves_dir": "",
                "active_id": "",
                "active_save": "",
                "models": [],
                "saves": [],
            }
        return list_saves_payload(saves_dir)


def load_global_preferences(saves_dir: Path | None = None) -> dict[str, Any]:
    """Loads global user preferences (friendly names, display overrides) from `Scenarios/global_preferences.json` & SQLite."""
    target_dir = _resolve_saves_dir(saves_dir)
    if target_dir is None:
        return {"ok": True, "version": 1, "updated_at": "", "friendly_names": {}}
    saves_dir = target_dir
    saves_dir.mkdir(parents=True, exist_ok=True)
    pref_path = saves_dir / GLOBAL_PREFERENCES_FILENAME
    data: dict[str, Any] = {
        "ok": True,
        "version": 1,
        "updated_at": "",
        "friendly_names": {},
    }
    if pref_path.exists():
        try:
            raw = json.loads(pref_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data.update(raw)
        except Exception:
            pass

    if not isinstance(data.get("friendly_names"), dict):
        data["friendly_names"] = {}

    try:
        db_path = get_default_db_path()
        if db_path is not None and db_path.exists():
            with PortfolioDB(db_path) as db:
                db_prefs = db.get_holding_preferences()
            for key, row in db_prefs.items():
                if key not in data["friendly_names"]:
                    from exporters.view_model import format_investment_type
                    data["friendly_names"][key] = {
                        "holding_key": key,
                        "friendly_name": row.get("friendly_name") or "",
                        "statement_name": row.get("statement_name") or "",
                        "account_id": row.get("account_id"),
                        "security_id": row.get("security_id"),
                        "institution": row.get("institution"),
                        "investment_type": format_investment_type(row.get("investment_type")),
                        "maturity_date": row.get("maturity_date"),
                        "updated_at": row.get("updated_at_iso"),
                    }
                elif not data["friendly_names"][key].get("statement_name") and row.get("statement_name"):
                    data["friendly_names"][key]["statement_name"] = row["statement_name"]
            for _k, _entry in data["friendly_names"].items():
                if isinstance(_entry, dict) and _entry.get("investment_type"):
                    from exporters.view_model import format_investment_type
                    _entry["investment_type"] = format_investment_type(_entry["investment_type"])
    except Exception as exc:
        log.warning("load_global_preferences DB merge failed: %s", exc)

    return data


def save_global_preference_entry(
    holding_key: str,
    friendly_name: str,
    statement_name: str = "",
    account_id: str | None = None,
    security_id: str | None = None,
    institution: str | None = None,
    investment_type: str | None = None,
    maturity_date: str | None = None,
    fee_pct: float | None = None,
    comment: str | None = None,
    notes: str | None = None,
    saves_dir: Path | None = None,
) -> dict[str, Any]:
    """Saves a holding's friendly name, fee_pct, and comment to `Scenarios/global_preferences.json` and SQLite."""
    clean_key = str(holding_key or "").strip()
    if not clean_key:
        return {"ok": False, "error": "Missing holding_key"}
    target_dir = _resolve_saves_dir(saves_dir)
    if target_dir is None:
        return {"ok": False, "error": "No active portfolio selected."}
    saves_dir = target_dir

    clean_fn = str(friendly_name or "").strip()
    clean_sn = str(statement_name or "").strip() or clean_key

    clean_fee = None
    if fee_pct is not None and str(fee_pct).strip() != "":
        try:
            clean_fee = round(float(str(fee_pct).replace("%", "").strip()), 4)
        except (ValueError, TypeError):
            clean_fee = None

    clean_comment = str(comment or "").strip()

    clean_inv_type = None
    if investment_type:
        from exporters.view_model import format_investment_type
        clean_inv_type = format_investment_type(str(investment_type).strip())

    try:
        db_path = get_default_db_path()
        if db_path is not None and db_path.exists():
            with PortfolioDB(db_path) as db:
                db.set_holding_preference(
                    holding_key=clean_key,
                    statement_name=clean_sn,
                    friendly_name=clean_fn,
                    account_id=account_id,
                    security_id=security_id,
                    institution=institution,
                    investment_type=clean_inv_type,
                    maturity_date=maturity_date,
                    fee_pct=clean_fee,
                    comment=clean_comment,
                    notes=notes,
                )
    except Exception as exc:
        log.warning("save_global_preference_entry DB write failed: %s", exc)

    prefs = load_global_preferences(saves_dir)
    now_str = datetime.now().strftime("%b %d, %Y %I:%M %p")
    now_iso = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    entry = {
        "holding_key": clean_key,
        "friendly_name": clean_fn,
        "statement_name": clean_sn,
        "account_id": account_id,
        "security_id": security_id,
        "institution": institution,
        "investment_type": clean_inv_type,
        "maturity_date": maturity_date,
        "fee_pct": clean_fee,
        "comment": clean_comment,
        "notes": notes,
        "updated_at": now_str,
    }
    prefs["friendly_names"][clean_key] = entry
    prefs["updated_at"] = now_str
    prefs["updated_iso"] = now_iso

    saves_dir.mkdir(parents=True, exist_ok=True)
    pref_path = saves_dir / GLOBAL_PREFERENCES_FILENAME
    _write_json_lf(pref_path, prefs)
    return {
        "ok": True,
        "entry": entry,
        "preferences": prefs,
    }


def handle_save_global_preferences(body: dict[str, Any], saves_dir: Path | None = None) -> dict[str, Any]:
    """Handles POST payload for global preferences (single holding or batch)."""
    if not isinstance(body, dict):
        return {"ok": False, "error": "Invalid request body"}
    saves_dir = _resolve_saves_dir(saves_dir)
    if saves_dir is None:
        return {"ok": False, "error": "No active portfolio selected."}

    if "holding_key" in body:
        return save_global_preference_entry(
            holding_key=body["holding_key"],
            friendly_name=body.get("friendly_name", ""),
            statement_name=body.get("statement_name", ""),
            account_id=body.get("account_id"),
            security_id=body.get("security_id"),
            institution=body.get("institution"),
            investment_type=body.get("investment_type"),
            maturity_date=body.get("maturity_date"),
            fee_pct=body.get("fee_pct"),
            comment=body.get("comment"),
            notes=body.get("notes"),
            saves_dir=saves_dir,
        )
    elif "friendly_names" in body and isinstance(body["friendly_names"], dict):
        for k, v in body["friendly_names"].items():
            if isinstance(v, dict):
                save_global_preference_entry(
                    holding_key=k,
                    friendly_name=v.get("friendly_name", ""),
                    statement_name=v.get("statement_name", ""),
                    account_id=v.get("account_id"),
                    security_id=v.get("security_id"),
                    institution=v.get("institution"),
                    investment_type=v.get("investment_type"),
                    maturity_date=v.get("maturity_date"),
                    fee_pct=v.get("fee_pct"),
                    comment=v.get("comment"),
                    notes=v.get("notes"),
                    saves_dir=saves_dir,
                )
            elif isinstance(v, str):
                save_global_preference_entry(
                    holding_key=k,
                    friendly_name=v,
                    saves_dir=saves_dir,
                )
        return {"ok": True, "preferences": load_global_preferences(saves_dir)}
    return {"ok": False, "error": "Missing holding_key or friendly_names in body"}


def get_project_settings(portfolio_id: str | None = None) -> dict[str, Any]:
    """Returns the target (or active) portfolio configuration, resolved prompts, and full portfolios index."""
    bundle = list_portfolios()
    pid = portfolio_id if portfolio_id is not None else bundle.get("active_portfolio_id")
    cfg_path = get_default_config_path(pid) if pid else None
    cfg = load_config(cfg_path, portfolio_id=pid)
    cfg["badge_history"] = normalize_badge_history(cfg.get("badge_history"), pid, cfg.get("badge_image"))
    if "badge_metadata" not in cfg or not isinstance(cfg.get("badge_metadata"), dict):
        cfg["badge_metadata"] = {}
    ref_b = cfg.get("reference_annual_burn")
    ret_b = cfg.get("retirement_annual_burn")
    if ref_b is not None or ret_b is not None:
        try:
            eff_burn = int(round(float(ref_b if ref_b is not None else ret_b)))
            cfg["reference_annual_burn"] = eff_burn
            cfg["retirement_annual_burn"] = eff_burn
        except (ValueError, TypeError):
            pass
    cfg["family_members"] = resolve_family_members(cfg, clamp_work_plan=False)
    for _m in cfg["family_members"]:
        if isinstance(_m, dict) and isinstance(_m.get("work_plan"), dict):
            auto_retire_elapsed_work_plan(_m["work_plan"], _m.get("birth_date"))
    validation = validate_portfolio_config(cfg) if pid else {
        "valid": False,
        "missing_fields": ["household_name", "burn", "investors", "tax_rates"],
        "validation_errors": ["No portfolio selected."],
    }
    return {
        "ok": True,
        "portfolio_id": pid,
        "has_portfolio": bool(pid),
        "project": cfg,
        "validation": validation,
        "state_tax_presets": US_STATE_TAX_PRESETS,
        "saved_prompts": normalize_saved_prompts(cfg) if pid else [],
        "advisor_prompts": resolve_advisor_prompts(cfg),
        "default_advisor_prompts": load_default_advisor_prompts(),
        "portfolios_bundle": bundle,
    }


def list_ssn_statement_files(portfolio_id: str | None = None) -> dict[str, Any]:
    pdir = get_portfolio_dir(portfolio_id)
    if pdir is None:
        return {"ok": True, "files": [], "default_folder": "Portfolios/<portfolio>/Statements/SSN"}
    ssn_dir = pdir / "Statements" / "SSN"
    ssn_dir.mkdir(parents=True, exist_ok=True)
    statements_dir = pdir / "Statements"
    files: list[dict[str, Any]] = []
    seen_paths = set()

    for d in [ssn_dir, statements_dir]:
        if not d.exists() or not d.is_dir():
            continue
        for p in sorted(d.glob("*.xml")):
            rel_p = str(p.relative_to(PROJECT_ROOT)) if PROJECT_ROOT in p.parents else str(p)
            if rel_p in seen_paths:
                continue
            seen_paths.add(rel_p)
            try:
                parsed = parse_ssa_xml_path(p, portfolio_id=portfolio_id)
            except Exception:
                parsed = {"ok": False}
            files.append({
                "name": p.name,
                "path": rel_p,
                "size": p.stat().st_size,
                "mtime": int(p.stat().st_mtime),
                "is_ssa": parsed.get("ok", False) and bool(parsed.get("annual_earnings")),
                "details": parsed if parsed.get("ok") else None,
            })

    return {
        "ok": True,
        "files": files,
        "default_folder": f"Portfolios/{pdir.name}/Statements/SSN",
    }


def handle_parse_ssn_statement(body: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(body, dict):
        return {"ok": False, "error": "Invalid JSON body"}
    file_path = str(body.get("file_path") or "").strip()
    xml_text = str(body.get("xml_text") or "").strip()
    file_name = str(body.get("file_name") or "").strip()
    pid = str(body.get("portfolio_id") or "").strip() or None

    if file_path:
        return parse_ssa_xml_path(file_path, portfolio_id=pid)
    if xml_text:
        return parse_ssa_xml_text(xml_text, file_name=file_name)
    return {"ok": False, "error": "No file_path or xml_text provided"}


def handle_save_ssn_file(body: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(body, dict):
        return {"ok": False, "error": "Invalid JSON body"}
    xml_text = str(body.get("xml_text") or "").strip()
    if not xml_text:
        return {"ok": False, "error": "No xml_text provided"}
    pid = str(body.get("portfolio_id") or "").strip() or None
    pdir = get_portfolio_dir(pid)
    if pdir is None:
        return {"ok": False, "error": "No active portfolio selected."}

    raw_name = str(body.get("file_name") or "social-security-statement.xml").strip()
    clean_name = Path(raw_name).name
    if not clean_name.lower().endswith(".xml"):
        clean_name += ".xml"

    ssn_dir = pdir / "Statements" / "SSN"
    ssn_dir.mkdir(parents=True, exist_ok=True)
    target_path = (ssn_dir / clean_name).resolve()
    if not target_path.is_relative_to(ssn_dir.resolve()):
        return {"ok": False, "error": "Invalid file name"}

    # SECURITY INVARIANT (SA-13 / SA-18 / SA3-09): Validate XML size & XXE constraints BEFORE writing to disk,
    # and enforce `0o600` file permissions on saved SSA statement artifacts.
    rel_p = f"Portfolios/{pdir.name}/Statements/SSN/{clean_name}"
    parsed = parse_ssa_xml_text(xml_text, file_path=rel_p, file_name=clean_name)
    if not parsed.get("ok"):
        return parsed
    try:
        target_path.write_text(xml_text, encoding="utf-8")
        _chmod_0600_safe(target_path)
    except Exception as exc:
        return {"ok": False, "error": f"Failed saving file: {exc}"}

    parsed["saved"] = True
    return parsed


def _sanitize_svg_text(svg_text: str) -> str:
    """Strips active scripting constructs (script/foreignObject/event handlers/javascript URIs) from SVGs (SA-22)."""
    # SECURITY DO: Strip `<script>`, `<foreignObject>`, `on*=` attributes, and `javascript:`/`data:text/html` URIs on import,
    # and keep `_serve_static` pairing `.svg` responses with `Content-Security-Policy: default-src 'none'`.
    # SECURITY DON'T: Never serve unsanitized SVG uploads or relax the SVG CSP header.
    s = str(svg_text or "").strip()
    if not s or "<svg" not in s.lower():
        raise ValueError("Invalid SVG document")
    s = re.sub(r"<script\b[^>]*>.*?</script>", "", s, flags=re.IGNORECASE | re.DOTALL)
    s = re.sub(r"<script\b[^>]*/>", "", s, flags=re.IGNORECASE | re.DOTALL)
    s = re.sub(r"<foreignObject\b[^>]*>.*?</foreignObject>", "", s, flags=re.IGNORECASE | re.DOTALL)
    s = re.sub(r"<foreignObject\b[^>]*/>", "", s, flags=re.IGNORECASE | re.DOTALL)
    s = re.sub(r"\s+on[a-z]+\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+)", "", s, flags=re.IGNORECASE)
    s = re.sub(
        r"((?:href|xlink:href|src)\s*=\s*[\"'])\s*(?:javascript|vbscript|data:text/html)[^\"']*([\"'])",
        r"\1#\2",
        s,
        flags=re.IGNORECASE,
    )
    return s


def handle_upload_badge(pdir: Path, pid: str, raw_badge: str) -> str:
    """Decodes, sanitizes (if SVG), writes `0600` uploaded badge files under `pdir/Assets/`, and returns relative path."""
    header, encoded = raw_badge.split(",", 1)
    ext = ".png"
    if "jpeg" in header or "jpg" in header:
        ext = ".jpg"
    elif "svg" in header:
        ext = ".svg"
    elif "webp" in header:
        ext = ".webp"
    bdata = base64.b64decode(encoded)
    if ext == ".svg":
        clean_svg = _sanitize_svg_text(bdata.decode("utf-8", errors="replace"))
        bdata = clean_svg.encode("utf-8")
    assets_dir = pdir / "Assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    gen_name = f"badge_upload_{ts}{ext}"
    gen_file = assets_dir / gen_name
    gen_file.write_bytes(bdata)
    _chmod_0600_safe(gen_file)
    cur_file = assets_dir / f"household_badge{ext}"
    cur_file.write_bytes(bdata)
    _chmod_0600_safe(cur_file)
    return f"Portfolios/{pid}/Assets/{gen_name}"


def handle_save_project_settings(body: dict[str, Any]) -> dict[str, Any]:
    """
    Updates the target (or active) `Portfolios/<id>/project.json` with household metadata,
    badge icon, investors, beneficiaries, tax rates, bucket overrides, and Advisor Prompts.
    When `rebuild` is True (default), also rebuilds `index.html`.
    """
    if not isinstance(body, dict):
        return {"ok": False, "error": "Invalid JSON body"}

    pid = str(body.get("portfolio_id") or "").strip() or get_active_portfolio_id()
    if not pid:
        return {"ok": False, "error": "No active portfolio selected. Create or select a portfolio first."}
    pdir = get_portfolio_dir(pid)
    cfg_path = get_default_config_path(pid)
    if pdir is None or cfg_path is None:
        return {"ok": False, "error": f"Portfolio '{pid}' not found."}

    if body.get("rebuild_only"):
        cfg_cur = load_config(cfg_path, portfolio_id=pid)
        val_cur = validate_portfolio_config(cfg_cur)
        if not val_cur["valid"]:
            return {
                "ok": False,
                "incomplete": True,
                "error": "Cannot rebuild incomplete portfolio: " + "; ".join(val_cur["validation_errors"]),
                "validation": val_cur,
                "state_tax_presets": US_STATE_TAX_PRESETS,
                "portfolios_bundle": list_portfolios(),
            }
        try:
            render_active_site()
            return {
                "ok": True,
                "rebuilt": True,
                "validation": val_cur,
                "state_tax_presets": US_STATE_TAX_PRESETS,
                "portfolios_bundle": list_portfolios(),
            }
        except Exception as exc:
            return {"ok": False, "error": f"Rebuild failed: {exc}"}

    should_rebuild = bool(body.get("rebuild", True))
    cfg = load_config(cfg_path, portfolio_id=pid)
    cfg["badge_history"] = normalize_badge_history(
        body["badge_history"] if isinstance(body.get("badge_history"), list) else cfg.get("badge_history"),
        pid,
        cfg.get("badge_image"),
    )
    if "badge_metadata" in body and isinstance(body["badge_metadata"], dict):
        hist_set = set(cfg["badge_history"])
        cfg["badge_metadata"] = {k: v for k, v in body["badge_metadata"].items() if k in hist_set or any(h.endswith(k) or k.endswith(h) for h in hist_set)}

    if "household_name" in body and body["household_name"] is not None:
        cfg["household_name"] = str(body["household_name"]).strip()
    if "household_state" in body:
        raw_hs = str(body.get("household_state") or "").strip().upper()
        if raw_hs:
            cfg["household_state"] = raw_hs
        else:
            cfg.pop("household_state", None)
    for burn_key in ("reference_annual_burn", "retirement_annual_burn"):
        if burn_key in body:
            raw_burn = body[burn_key]
            if raw_burn is None or (isinstance(raw_burn, str) and not raw_burn.strip()):
                cfg["reference_annual_burn"] = None
                cfg["retirement_annual_burn"] = None
            else:
                try:
                    val_burn = round(float(raw_burn))
                    cfg["reference_annual_burn"] = val_burn if val_burn > 0 else None
                    cfg["retirement_annual_burn"] = val_burn if val_burn > 0 else None
                except (ValueError, TypeError):
                    pass
            break
    if "badge_image" in body and body["badge_image"] is not None:
        raw_badge = str(body["badge_image"]).strip()
        if raw_badge.startswith("data:image/"):
            try:
                rel_badge = handle_upload_badge(pdir, pid, raw_badge)
                cfg["badge_image"] = rel_badge
                cfg["badge_history"] = normalize_badge_history(cfg.get("badge_history"), pid, append_badge=rel_badge)
            except Exception as exc:
                log.warning("Failed to decode base64 badge image: %s", exc)
        else:
            norm_b = normalize_badge_image_path(raw_badge or "Assets/default_badge.svg", pid)
            cfg["badge_image"] = norm_b
            if norm_b != "Assets/default_badge.svg":
                cfg["badge_history"] = normalize_badge_history(cfg.get("badge_history"), pid, append_badge=norm_b)

    if "symbols" in body and isinstance(body["symbols"], list):
        custom_aliases = (cfg.get("ticker_aliases") or {})
        clean_symbols: list[str] = []
        for s in body["symbols"]:
            raw_sym = str(s.get("symbol") if isinstance(s, dict) else s).strip().upper()
            sym_str = resolve_symbol_alias(raw_sym, custom_aliases=custom_aliases) if raw_sym else ""
            if sym_str and sym_str not in clean_symbols:
                clean_symbols.append(sym_str)
            if len(clean_symbols) >= 10:
                break
        cfg["symbols"] = clean_symbols
        cfg["primary_equity_symbol"] = clean_symbols[0] if clean_symbols else ""
    elif "primary_equity_symbol" in body and body["primary_equity_symbol"] is not None:
        prim = str(body["primary_equity_symbol"]).strip().upper()
        cfg["primary_equity_symbol"] = prim
        if prim:
            existing_syms = cfg.get("symbols") or []
            if prim not in existing_syms:
                cfg["symbols"] = [prim] + [s for s in existing_syms if s != prim][:9]

    def _opt_int(val: Any, fallback: int = 0) -> int:
        if val is None or val == "":
            return fallback
        try:
            return int(float(val))
        except (ValueError, TypeError):
            return fallback

    def _opt_float(val: Any, fallback: float = 0.0) -> float:
        if val is None or val == "":
            return fallback
        try:
            return float(val)
        except (ValueError, TypeError):
            return fallback

    if "investors" in body and isinstance(body["investors"], list):
        existing_by_id = {
            str(inv.get("id") or inv.get("name") or "").strip().lower().replace(" ", "_"): inv
            for inv in (cfg.get("investors") or [])
            if isinstance(inv, dict)
        }
        cleaned_investors: list[dict[str, Any]] = []
        for idx, raw_inv in enumerate(body["investors"]):
            if not isinstance(raw_inv, dict):
                continue
            name = str(raw_inv.get("name") or "").strip()
            inv_id = str(raw_inv.get("id") or name or f"investor_{idx + 1}").strip().lower().replace(" ", "_")
            b_date = str(raw_inv.get("birth_date") or raw_inv.get("birthdate") or "").strip()
            m_age = _opt_int(raw_inv.get("milestone_age"), 85)
            is_rmd = bool(raw_inv.get("is_rmd_owner", raw_inv.get("rmd_owner", idx == 0)))

            prev_inv = existing_by_id.get(inv_id) or {}
            prev_ssn = prev_inv.get("ssn") or {}
            raw_ssn = raw_inv.get("ssn") if isinstance(raw_inv.get("ssn"), dict) else {}

            # DESIGN INTENT (XML-Only Social Security & SA2-17):
            # - Social Security benefits are modeled ONLY when an official `ssa.gov` XML statement has
            #   been uploaded (`annual_earnings` / `external_file`, confined to `portfolio_id=pid`).
            # - Never re-introduce `years_worked` + `average_salary` flat-salary synthesis; clearing a
            #   loaded XML statement (`clear_statement=True`) must leave `annual_earnings` empty (`$0` PIA).
            clear_stmt = bool(raw_ssn.get("clear_statement", False) or raw_ssn.get("use_simple_fields", False))
            if not clear_stmt and "external_file" in raw_ssn and not str(raw_ssn.get("external_file") or "").strip():
                if "annual_earnings" in raw_ssn and not raw_ssn.get("annual_earnings"):
                    clear_stmt = True

            ext_file = (
                ""
                if clear_stmt
                else str(
                    raw_ssn.get("external_file", raw_ssn.get("ssa_data_file", prev_ssn.get("external_file", "")))
                ).strip()
            )
            fra_age = _opt_int(raw_ssn.get("fra_age", prev_ssn.get("fra_age", 67)), 67)
            def_claim = _opt_int(raw_ssn.get("default_claim_age", prev_ssn.get("default_claim_age", 67)), 67)

            work_span = (
                str(raw_ssn.get("work_span_label") or prev_ssn.get("work_span_label") or "")
                if not clear_stmt
                else ""
            )
            if clear_stmt:
                ann_earnings: dict[str, Any] = {}
            elif "annual_earnings" in raw_ssn and isinstance(raw_ssn["annual_earnings"], dict) and len(raw_ssn["annual_earnings"]) > 0:
                ann_earnings = raw_ssn["annual_earnings"]
            elif ext_file:
                parsed_xml = parse_ssa_xml_path(ext_file, portfolio_id=pid)
                if parsed_xml.get("ok") and parsed_xml.get("annual_earnings"):
                    ann_earnings = parsed_xml["annual_earnings"]
                    if not work_span:
                        work_span = parsed_xml.get("work_span_label", "")
                elif prev_ssn.get("annual_earnings"):
                    ann_earnings = prev_ssn["annual_earnings"]
                else:
                    ann_earnings = {}
            elif "annual_earnings" not in raw_ssn and prev_ssn.get("annual_earnings"):
                ann_earnings = prev_ssn["annual_earnings"]
            else:
                ann_earnings = {}

            earn_note = (
                str(raw_ssn.get("earnings_note") or prev_ssn.get("earnings_note") or "")
                if (not clear_stmt and (ann_earnings or ext_file))
                else ""
            )
            if not ann_earnings and not ext_file:
                work_span = ""

            raw_wp = raw_inv["work_plan"] if "work_plan" in raw_inv else prev_inv.get("work_plan")
            inv_entry: dict[str, Any] = {
                "id": inv_id,
                "name": name,
                "birth_date": b_date,
                "milestone_age": m_age,
                "is_rmd_owner": is_rmd,
                "ssn": {
                    "fra_age": fra_age,
                    "default_claim_age": def_claim,
                    "work_span_label": work_span,
                    "earnings_note": earn_note,
                    "external_file": ext_file,
                    "annual_earnings": ann_earnings,
                },
            }
            if isinstance(raw_wp, dict):
                inv_entry["work_plan"] = sanitize_work_plan(raw_wp, milestone_age=m_age, clamp_to_milestone=False)
            cleaned_investors.append(inv_entry)
        cfg["investors"] = cleaned_investors

    if "beneficiaries" in body and isinstance(body["beneficiaries"], list):
        cleaned_bens: list[dict[str, Any]] = []
        for idx, raw_ben in enumerate(body["beneficiaries"]):
            if not isinstance(raw_ben, dict):
                continue
            name = str(raw_ben.get("name") or "").strip()
            if not name and not str(raw_ben.get("birth_date") or raw_ben.get("birthdate") or "").strip():
                continue
            ben_id = str(raw_ben.get("id") or name or f"beneficiary_{idx + 1}").strip().lower().replace(" ", "_")
            b_date = str(raw_ben.get("birth_date") or raw_ben.get("birthdate") or "").strip()
            b_ms = _opt_int(raw_ben.get("milestone_age"), 85)
            ben_entry: dict[str, Any] = {
                "id": ben_id,
                "name": name,
                "birth_date": b_date,
                "milestone_age": b_ms,
                "is_rmd_owner": bool(raw_ben.get("is_rmd_owner", False)),
            }
            if isinstance(raw_ben.get("work_plan"), dict):
                ben_entry["work_plan"] = sanitize_work_plan(raw_ben["work_plan"], milestone_age=b_ms, clamp_to_milestone=False)
            cleaned_bens.append(ben_entry)
        cfg["beneficiaries"] = cleaned_bens

    if "family_members" in body and isinstance(body["family_members"], list):
        cleaned_family: list[dict[str, Any]] = []
        ssn_rank = 0
        rmd_seen = False
        for idx, raw_mem in enumerate(body["family_members"]):
            if not isinstance(raw_mem, dict):
                continue
            name = str(raw_mem.get("name") or "").strip()
            b_date = str(raw_mem.get("birth_date") or raw_mem.get("birthdate") or "").strip()
            if not name and not b_date:
                continue
            mem_id = str(raw_mem.get("id") or name or f"member_{idx + 1}").strip().lower().replace(" ", "_")
            ms_age = _opt_int(raw_mem.get("milestone_age"), 85)
            is_rmd = bool(raw_mem.get("is_rmd_owner", False))
            if is_rmd:
                if rmd_seen:
                    is_rmd = False
                else:
                    rmd_seen = True
            is_ssn = bool(raw_mem.get("is_ssn_contributor", False))
            if is_ssn:
                ssn_rank += 1
                if ssn_rank > 2:
                    is_ssn = False
            raw_ssn = raw_mem.get("ssn") if isinstance(raw_mem.get("ssn"), dict) else {}
            fam_entry: dict[str, Any] = {
                "id": mem_id,
                "name": name,
                "birth_date": b_date,
                "milestone_age": ms_age,
                "is_rmd_owner": is_rmd,
                "is_ssn_contributor": is_ssn,
                "ssn": raw_ssn,
            }
            if isinstance(raw_mem.get("work_plan"), dict):
                fam_entry["work_plan"] = sanitize_work_plan(raw_mem["work_plan"], milestone_age=ms_age, clamp_to_milestone=False)
            cleaned_family.append(fam_entry)
        if not rmd_seen and cleaned_family:
            cleaned_family[0]["is_rmd_owner"] = True
        cfg["family_members"] = cleaned_family
    elif "investors" in body and isinstance(cfg.get("family_members"), list):
        inv_map = {inv["id"]: inv for inv in (cfg.get("investors") or []) if isinstance(inv, dict) and inv.get("id")}
        for mem in cfg["family_members"]:
            if isinstance(mem, dict) and mem.get("is_ssn_contributor"):
                mid = str(mem.get("id") or "").strip()
                if mid in inv_map and "work_plan" in inv_map[mid]:
                    mem["work_plan"] = inv_map[mid]["work_plan"]

    if "tax_rates" in body and isinstance(body["tax_rates"], dict):
        raw_tr = body["tax_rates"]
        existing_tr = cfg.get("tax_rates")
        tr: dict[str, Any] = dict(existing_tr) if isinstance(existing_tr, dict) else {}
        if "state_code" in raw_tr:
            sc = str(raw_tr.get("state_code") or "").strip().upper()
            if sc:
                tr["state_code"] = sc
                if sc in US_STATE_TAX_PRESETS and "state_rate" not in raw_tr and "state_rate" not in tr:
                    tr["state_rate"] = float(US_STATE_TAX_PRESETS[sc]["state_rate"])
            else:
                tr.pop("state_code", None)
        for num_key in (
            "state_rate",
            "ltcg_top",
            "ordinary_top",
            "fed_ltcg_top",
            "niit_rate",
            "fed_ordinary_top",
            "ssn_effective",
        ):
            if num_key in raw_tr:
                raw_v = raw_tr[num_key]
                if raw_v is None or raw_v == "":
                    tr.pop(num_key, None)
                else:
                    try:
                        tr[num_key] = float(raw_v)
                    except (ValueError, TypeError):
                        pass
        # SECURITY INVARIANT (SA3-10b): Explicitly validate/coerce `married_filing_jointly` (bool) and
        # `min_tax_bracket_floor` (enum) instead of blindly copying arbitrary untrusted keys from `body["tax_rates"]`.
        if "married_filing_jointly" in raw_tr:
            mfj_v = raw_tr["married_filing_jointly"]
            if mfj_v is None:
                tr.pop("married_filing_jointly", None)
            elif isinstance(mfj_v, str):
                tr["married_filing_jointly"] = mfj_v.strip().lower() in ("true", "1", "yes", "mfj")
            else:
                tr["married_filing_jointly"] = bool(mfj_v)
        if "min_tax_bracket_floor" in raw_tr:
            fl_v = raw_tr["min_tax_bracket_floor"]
            if fl_v is None or str(fl_v).strip() == "":
                tr.pop("min_tax_bracket_floor", None)
            else:
                fl_s = str(fl_v).strip().lower()
                if fl_s in ("none", "10", "12", "22", "24", "32", "35", "37"):
                    tr["min_tax_bracket_floor"] = fl_s
        cfg["tax_rates"] = tr

    for sim_tax_key in ("sim_ira_ordinary_tax_rate", "sim_cash_interest_tax_rate"):
        if sim_tax_key in body:
            raw_stv = body[sim_tax_key]
            if raw_stv is None or raw_stv == "":
                cfg[sim_tax_key] = None
            else:
                try:
                    cfg[sim_tax_key] = float(raw_stv)
                except (ValueError, TypeError):
                    pass

    for gf_key in ("grant_forecast", "gsu_forecast"):
        if gf_key in body and isinstance(body[gf_key], dict):
            target_gf_key = "grant_forecast" if ("grant_forecast" in cfg or gf_key == "grant_forecast") else "gsu_forecast"
            gf = cfg.setdefault(target_gf_key, {})
            if isinstance(gf, dict):
                gf.update(body[gf_key])

    if "sim_buckets" in body and isinstance(body["sim_buckets"], dict):
        cfg["sim_buckets"] = body["sim_buckets"]
    if "principal_overrides" in body and isinstance(body["principal_overrides"], dict):
        cfg["principal_overrides"] = body["principal_overrides"]

    if "saved_prompts" in body and isinstance(body["saved_prompts"], list):
        cleaned_prompts = sanitize_saved_prompts(body["saved_prompts"])
        cfg["saved_prompts"] = cleaned_prompts
        # Keep legacy advisor_prompts keys aligned while preserving model_note_template
        defaults = load_default_advisor_prompts()
        ap = cfg.setdefault("advisor_prompts", {})
        if not isinstance(ap, dict):
            ap = {}
            cfg["advisor_prompts"] = ap
        prompt_by_id = {p["id"]: p["prompt"] for p in cleaned_prompts}
        for p_key in ("preferred_forecast", "portfolio_improvements"):
            if p_key in prompt_by_id:
                v_s = str(prompt_by_id[p_key] or "").strip()
                ap[p_key] = "" if (not v_s or v_s == defaults.get(p_key, "").strip()) else str(prompt_by_id[p_key])
            elif p_key in ap:
                ap[p_key] = ""
    elif "advisor_prompts" in body and isinstance(body["advisor_prompts"], dict):
        defaults = load_default_advisor_prompts()
        ap = cfg.setdefault("advisor_prompts", {})
        if not isinstance(ap, dict):
            ap = {}
            cfg["advisor_prompts"] = ap
        for p_key in ADVISOR_PROMPT_FILES:
            if p_key in body["advisor_prompts"]:
                raw_val = body["advisor_prompts"][p_key]
                if raw_val is None:
                    ap[p_key] = ""
                else:
                    val_s = str(raw_val).strip()
                    # If identical to generic default or explicitly empty, clear override so default is inherited
                    if not val_s or val_s == defaults.get(p_key, "").strip():
                        ap[p_key] = ""
                    else:
                        ap[p_key] = str(raw_val)

    save_config(cfg, cfg_path, portfolio_id=pid)
    invalidate_settings_cache()
    llm_tools.invalidate_payload_cache()
    global RESERVED_SAVE_NAMES
    RESERVED_SAVE_NAMES = _load_reserved_save_names()

    val = validate_portfolio_config(cfg)
    active_pid = get_active_portfolio_id()
    is_editing_active = (pid == active_pid)
    if should_rebuild and is_editing_active and val["valid"]:
        try:
            rendered = render_active_site()
            payload = rendered.get("payload") or {}
            return {
                "ok": True,
                "portfolio_id": pid,
                "project": cfg,
                "validation": val,
                "state_tax_presets": US_STATE_TAX_PRESETS,
                "saved_prompts": normalize_saved_prompts(cfg),
                "advisor_prompts": resolve_advisor_prompts(cfg),
                "default_advisor_prompts": load_default_advisor_prompts(),
                "payload_project": payload.get("project"),
                "ssn_model": payload.get("ssn_model"),
                "portfolios_bundle": list_portfolios(),
            }
        except Exception as exc:
            log.warning("fast re-render after project save failed: %s", exc)
    elif is_editing_active and val["valid"]:
        from valuation.simulator import refresh_family_config
        refresh_family_config(cfg)

    cfg["family_members"] = resolve_family_members(cfg, clamp_work_plan=False)
    return {
        "ok": True,
        "portfolio_id": pid,
        "project": cfg,
        "validation": val,
        "state_tax_presets": US_STATE_TAX_PRESETS,
        "saved_prompts": normalize_saved_prompts(cfg),
        "advisor_prompts": resolve_advisor_prompts(cfg),
        "default_advisor_prompts": load_default_advisor_prompts(),
        "portfolios_bundle": list_portfolios(),
    }


def handle_list_statements(portfolio_id: str | None = None) -> dict[str, Any]:
    """
    Scans Portfolios/<pid>/Statements/*, checks SQLite ingested_files, and runs parser dry runs
    emitting pass/fail signals to classify files as 'Processed' (Gray), 'Available' (Green), or 'Unknown' (Red).
    """
    from datetime import datetime
    from config import (
        get_active_portfolio_id,
        get_portfolio_dir,
        get_statements_dir,
        get_default_config_path,
        get_default_db_path,
        load_config,
    )
    from db.database import PortfolioDB
    from ingest.scanner import compute_sha256
    from ingest.framework import IngestContext
    from ingest.plugins import build_default_registry
    from ingest.pdf_reader import extract_pdf_pages_batch

    pid = (portfolio_id or get_active_portfolio_id() or "").strip()
    if not pid:
        return {"ok": False, "error": "No portfolio specified"}

    demo_cfg = _active_demo_privacy_cfg()
    orig_pid = pid
    pdir = get_portfolio_dir(pid)
    if demo_cfg is not None and (not pdir or not pdir.exists()):
        pid = get_active_portfolio_id() or pid
        pdir = get_portfolio_dir(pid)

    if not pdir or not pdir.exists():
        return {"ok": False, "error": f"Portfolio directory for '{pid}' not found"}

    sdir = get_statements_dir(pid)
    if not sdir.exists():
        return {
            "ok": True,
            "portfolio_id": pid,
            "statements_dir": str(sdir),
            "files": [],
            "counts": {"processed": 0, "available": 0, "unknown": 0, "total": 0},
            "has_available": False,
        }

    db_path = get_default_db_path(pid)
    known_by_hash: dict[str, dict[str, Any]] = {}
    known_by_path: dict[str, dict[str, Any]] = {}
    if db_path.exists():
        try:
            with PortfolioDB(db_path) as db:
                known_by_hash = db.get_ingested_hashes()
                known_by_path = db.get_ingested_by_path()
        except Exception as exc:
            log.warning("Could not read ingested_files from %s: %s", db_path, exc)

    cfg = load_config(get_default_config_path(pid), portfolio_id=pid)
    plugins_dir = pdir / "Plugins"
    registry = build_default_registry(plugins_dir=plugins_dir if plugins_dir.exists() else None, cfg=cfg)

    sdir_resolved = sdir.resolve()
    all_files = sorted(
        p for p in sdir.rglob("*")
        if not p.is_symlink()
        and p.is_file()
        and p.resolve().is_relative_to(sdir_resolved)
        and not p.name.startswith(".")
    )

    investors = cfg.get("investors") or []
    configured_ssn_files = set()
    for inv in investors:
        ssn_cfg = inv.get("ssn") or {}
        for k in ("statement_file", "external_file"):
            val = str(ssn_cfg.get(k) or "").strip()
            if val:
                configured_ssn_files.add(val)
                configured_ssn_files.add(Path(val).name)
        if ssn_cfg.get("annual_earnings"):
            configured_ssn_files.add("social-security-statement.xml")

    # Pass 1: classify files by content hash / path / configured SSA filename BEFORE extracting PDFs
    pre_entries: list[dict[str, Any]] = []
    unprocessed_pdfs: list[Path] = []
    for p in all_files:
        rel_path = str(p.relative_to(sdir))
        sha256 = compute_sha256(p)
        st = p.stat()
        file_size = st.st_size
        mtime_date = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d")
        rec = known_by_hash.get(sha256) or known_by_path.get(str(p.resolve()))

        is_processed = False
        date_str = None
        doc_type = None
        if rec is not None:
            is_processed = True
            date_str = rec.get("statement_as_of_date") or rec.get("period_start") or mtime_date
            doc_type = rec.get("doc_type")
        elif p.name in configured_ssn_files or rel_path in configured_ssn_files:
            is_processed = True
            doc_type = "SSA_XML"
            try:
                res_ssa = parse_ssa_xml_path(p, portfolio_id=pid)
                if res_ssa.get("ok") and res_ssa.get("max_year"):
                    date_str = f"{res_ssa['max_year']}-12-31"
            except Exception:
                pass
            date_str = date_str or mtime_date

        if not is_processed and p.suffix.lower() == ".pdf":
            unprocessed_pdfs.append(p)
        pre_entries.append({
            "path": p,
            "rel_path": rel_path,
            "sha256": sha256,
            "size": file_size,
            "mtime_date": mtime_date,
            "is_processed": is_processed,
            "date_str": date_str,
            "doc_type": doc_type,
        })

    pages_map = extract_pdf_pages_batch(unprocessed_pdfs) if unprocessed_pdfs else {}

    files_out = []
    for item in pre_entries:
        p = item["path"]
        rel_path = item["rel_path"]
        sha256 = item["sha256"]
        file_size = item["size"]
        mtime_date = item["mtime_date"]

        if item["is_processed"]:
            files_out.append({
                "filename": rel_path,
                "name": p.name,
                "rel_path": rel_path,
                "date": item["date_str"] or mtime_date,
                "status": "Processed",
                "status_color": "gray",
                "doc_type": item["doc_type"] or "STATEMENT",
                "size": file_size,
                "error": None,
            })
            continue

        ctx = IngestContext(
            file_path=p,
            rel_path=rel_path,
            sha256=sha256,
            pages=pages_map.get(p) if p.suffix.lower() == ".pdf" else None,
            cfg=cfg,
        )
        passed, plugin, stmt, err = registry.dry_run_file(ctx)

        if passed and stmt is not None:
            stmt_date = str(stmt.as_of_date or stmt.period_start or mtime_date)
            files_out.append({
                "filename": rel_path,
                "name": p.name,
                "rel_path": rel_path,
                "date": stmt_date,
                "status": "Available",
                "status_color": "green",
                "doc_type": stmt.doc_type,
                "plugin_id": plugin.plugin_id if plugin else None,
                "size": file_size,
                "error": None,
            })
        else:
            files_out.append({
                "filename": rel_path,
                "name": p.name,
                "rel_path": rel_path,
                "date": mtime_date,
                "status": "Unknown",
                "status_color": "red",
                "doc_type": "UNKNOWN",
                "plugin_id": None,
                "size": file_size,
                "error": err or "No registered parser recognized this statement format",
            })

    counts = {
        "processed": sum(1 for f in files_out if f["status"] == "Processed"),
        "available": sum(1 for f in files_out if f["status"] == "Available"),
        "unknown": sum(1 for f in files_out if f["status"] == "Unknown"),
        "total": len(files_out),
    }

    res = {
        "ok": True,
        "portfolio_id": orig_pid if demo_cfg is not None else pid,
        "statements_dir": str(sdir),
        "files": files_out,
        "counts": counts,
        "has_available": counts["available"] > 0,
    }
    if demo_cfg is not None:
        proj = DemoTranslator(demo_cfg)
        res = proj.project_obj(res)
        # Keep non-monetary integer statement counts exact ("total" is in DemoTranslator _USD_KEYS).
        res["counts"] = dict(counts)
        res["portfolio_id"] = orig_pid
        res["statements_dir"] = f"Portfolios/{orig_pid}/Statements"
    return res


def handle_generate_badge(body: dict[str, Any]) -> dict[str, Any]:
    """Generates an SVG site badge for the specified portfolio using the selected LLM or graphic template."""
    import base64
    import re
    import time
    from config import (
        get_active_portfolio_id,
        get_portfolio_dir,
        get_default_config_path,
        load_config,
        save_config,
    )

    if not isinstance(body, dict):
        return {"ok": False, "error": "Request body must be a JSON object"}

    pid = str(body.get("portfolio_id") or get_active_portfolio_id() or "").strip()
    if not pid:
        return {"ok": False, "error": "No portfolio specified"}

    pdir = get_portfolio_dir(pid)
    if not pdir or not pdir.exists():
        return {"ok": False, "error": f"Portfolio directory for '{pid}' not found"}

    pname = str(body.get("portfolio_name") or "").strip()
    if not pname:
        cfg = load_config(get_default_config_path(pid), portfolio_id=pid)
        pname = str(cfg.get("household_name") or pid.replace("_", " ").title())

    user_prompt = str(body.get("prompt") or "").strip()
    model_id = str(body.get("model") or "gemini").strip().lower()
    iterate_current = bool(body.get("iterate_current"))
    base_svg = str(body.get("base_svg") or "").strip()

    if iterate_current and not base_svg:
        cfg = load_config(get_default_config_path(pid), portfolio_id=pid)
        cur_img = cfg.get("badge_image") or "Assets/household_badge.svg"
        if not cur_img.startswith("data:"):
            cand1 = pdir / cur_img
            cand2 = pdir / "Assets" / "household_badge.svg"
            cand3 = Path("Assets") / "default_badge.svg"
            if cand1.exists() and cand1.suffix.lower() == ".svg":
                base_svg = cand1.read_text(encoding="utf-8")
            elif cand2.exists():
                base_svg = cand2.read_text(encoding="utf-8")
            elif cand3.exists():
                base_svg = cand3.read_text(encoding="utf-8")

    initials = "".join([w[0].upper() for w in pname.split()[:2] if w]) or "AB"
    has_reg = (pdir / "privacy_registry.json").is_file()
    pcfg = load_privacy_config(pid, auto_sync=not has_reg)
    demo_on = bool((pcfg.get("demo_mode") or {}).get("enabled"))
    shield_mode = str((pcfg.get("ai_shield") or {}).get("mode") or "identity_aliased").strip().lower()
    shield_active = demo_on or (shield_mode not in ("off", "full_trusted"))
    if demo_on:
        eff_pcfg = json.loads(json.dumps(pcfg))
        demo_k = float((eff_pcfg.get("demo_mode") or {}).get("scale_factor") or 0.25)
        eff_pcfg.setdefault("ai_shield", {})["mode"] = "identity_and_values"
        eff_pcfg["ai_shield"]["scale_factor"] = demo_k
        eff_pcfg["ai_shield"]["value_scale_factor"] = demo_k
        shield_tr = build_translator(eff_pcfg)
    else:
        shield_tr = build_translator(pcfg) if shield_active else None

    llm_pname = pname
    llm_initials = initials
    llm_prompt = user_prompt
    llm_base_svg = base_svg
    if shield_active and shield_tr is not None:
        aliased_name = shield_tr.to_alias_text(pname)
        llm_pname = aliased_name if aliased_name and aliased_name != pname else "Portfolio"
        llm_initials = "AB"
        if user_prompt:
            llm_prompt = shield_tr.to_alias_text(user_prompt)
        if base_svg:
            llm_base_svg = shield_tr.to_alias_text(base_svg)

    if iterate_current and llm_base_svg and "<svg" in llm_base_svg:
        full_system_prompt = f"""You are a master brand designer, visual artist, and SVG coder for prestigious private wealth institutions and luxury digital products.
Task: Redesign and iterate upon the existing 240x64 vector site graphic banner provided below for "{llm_pname}".
Existing SVG to iterate upon:
```xml
{llm_base_svg}
```
User Refinement Guidance: {llm_prompt or 'Enhance the visual polish, contrast, and elegance of the existing site graphic.'}

Requirements:
- Maintain Dimensions: viewBox="0 0 240 64" width="240" height="64" (Exact 15:4 aspect ratio to fit the left navigation sidebar).
- Retain the deep dark background (#0b1120 or similar dark canvas) and overall harmony unless the user requested an overhaul.
- Apply the user's requested refinements, styling tweaks, typography adjustments, or color changes faithfully.
- ID Scoping: Keep all <linearGradient id="..."> and <filter id="..."> unique.
- Conciseness: Keep the SVG code clean, elegant, and under 120 lines (max 4000 characters). Avoid bloated raw path coordinates.
- Output: Return ONLY the updated raw valid <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 240 64" width="240" height="64">...</svg> code. Do NOT include markdown code fences, backticks, or explanatory text."""
    else:
        name_len = len(pname)
        if name_len <= 12:
            font_guidance = 'Use prominent readable typography (e.g. font-size="13" to "16", font-weight="700").'
        elif name_len <= 20:
            font_guidance = 'Use balanced typography (e.g. font-size="11" to "13", font-weight="700").'
        else:
            font_guidance = 'Use compact typography (e.g. font-size="9.5" to "11", font-weight="600") to avoid text overflowing the 240px canvas.'

        full_system_prompt = f"""You are a master brand designer, visual artist, and SVG coder for prestigious private wealth institutions and luxury digital products.
Task: Create a distinctive, high-end vector site graphic banner (240x64) for "{llm_pname}".

Creative Freedom & Composition:
- You have COMPLETE creative freedom over layout, composition, and visual style according to the User Guidance.
- Do NOT feel constrained to a standard "shield badge on the left, text on the right" layout.
- Explore diverse composition directions (choose what best elevates the user's prompt):
  • Elegant typographic logotype with bespoke lettering flourishes, decorative borders, or filigree frame.
  • Integrated insignia, emblem, or brandmark (can be left, right, centered, watermark, or woven into the letterforms).
  • Modern fintech aesthetic with glowing abstract data curves, crystalline facets, architectural line-art, or constellation motifs.
  • Ornate heraldic crest, seal, or medallion if requested by user guidance.
  • Minimalist Swiss / Nordic layout with bold spatial balance and sophisticated contrast.

Brand Elements Available (use as appropriate for your chosen design):
- Brand / Portfolio Name: "{llm_pname}" ({font_guidance}).
- Monogram / Initials: "{llm_initials}" (optional element if fitting for the style).
- Contextual Cues: Optional taglines such as "Wealth & Retirement Simulator", "Private Wealth", or "Asset Tracker" can be included if fitting, or omitted for a cleaner logotype.

Technical Specifications:
- Dimensions: viewBox="0 0 240 64" width="240" height="64" (Exact 15:4 aspect ratio to fit the left navigation sidebar).
- Canvas: Deep dark background (e.g. #090d16, #0b1120, #030712, obsidian, or rich dark navy) with subtle border or glowing edges.
- ID Scoping: Use unique prefixes for all <linearGradient id="..."> and <filter id="..."> (e.g. id="g-...") to avoid DOM collisions.
- Conciseness: Keep the SVG code clean, elegant, and under 120 lines (max 4000 characters). Avoid bloated raw path coordinates.
- Output: Return ONLY the raw valid <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 240 64" width="240" height="64">...</svg> code. Do NOT include markdown code fences, backticks, or explanatory text.

User Guidance:
{llm_prompt or 'Create a gilded, prestigious header graphic for "' + llm_pname + '" with ornate gold filigree details, deep emerald and cyan accents, and luxurious typography against a dark midnight navy canvas.'}"""

    def _extract_svg(txt: str) -> str | None:
        if not txt:
            return None
        cleaned = re.sub(r"^```(?:xml|svg)?\s*", "", txt.strip(), flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
        m = re.search(r"(<svg[\s\S]*?</svg>)", cleaned, re.IGNORECASE)
        if m:
            return m.group(1).strip()
        if "<svg" in cleaned and "</svg>" not in cleaned:
            idx = cleaned.find("<svg")
            partial = cleaned[idx:]
            if len(partial) > 500 and ("</text>" in partial or "</g>" in partial or "</rect>" in partial):
                auto_closed = partial.rstrip() + "\n</svg>"
                m2 = re.search(r"(<svg[\s\S]*?</svg>)", auto_closed, re.IGNORECASE)
                if m2:
                    return m2.group(1).strip()
        return None

    svg_content, llm_label = llm_tools.generate_text_with_model(
        full_system_prompt,
        model_id,
        validator=_extract_svg,
        portfolio_id=pid,
    )
    if svg_content and shield_active and shield_tr is not None:
        svg_content = shield_tr.from_alias_text(svg_content)
        escaped_real = pname.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        if llm_pname == "Portfolio" and pname != "Portfolio":
            svg_content = svg_content.replace(">Portfolio<", f">{escaped_real}<")
        if initials != "AB":
            svg_content = svg_content.replace(">AB<", f">{initials}<")
    is_fallback = False
    model_used = llm_label or model_id

    # Built-in High-End Parametric SVG Generator (Guaranteed fallback)
    if not svg_content:
        is_fallback = True
        model_used = f"Parametric Fallback ({model_id} unavailable)"
        log.warning("Badge generation using parametric fallback for model '%s'", model_id)
        initials = "".join([w[0].upper() for w in pname.split()[:2] if w]) or "AB"
        escaped_name = pname.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        svg_content = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 240 64" width="240" height="64">
  <defs>
    <linearGradient id="bgGrad" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#0b1120"/>
      <stop offset="50%" stop-color="#0f172a"/>
      <stop offset="100%" stop-color="#1e293b"/>
    </linearGradient>
    <linearGradient id="crestGrad" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#38bdf8"/>
      <stop offset="50%" stop-color="#0284c7"/>
      <stop offset="100%" stop-color="#10b981"/>
    </linearGradient>
    <linearGradient id="goldGrad" x1="0%" y1="0%" x2="100%" y2="0%">
      <stop offset="0%" stop-color="#f59e0b"/>
      <stop offset="100%" stop-color="#fbbf24"/>
    </linearGradient>
    <filter id="glow" x="-20%" y="-20%" width="140%" height="140%">
      <feGaussianBlur stdDeviation="2" result="blur" />
      <feComposite in="SourceGraphic" in2="blur" operator="over" />
    </filter>
  </defs>
  <!-- Background Card -->
  <rect x="1" y="1" width="238" height="62" rx="10" fill="url(#bgGrad)" stroke="#334155" stroke-width="1.2"/>
  <rect x="3" y="3" width="234" height="58" rx="8" fill="none" stroke="rgba(56,189,248,0.15)" stroke-width="1"/>

  <!-- Left Emblem Shield & Rising Bars -->
  <g transform="translate(14, 12)">
    <path d="M19 0 C28 0, 36 3, 38 12 C38 27, 26 36, 19 40 C12 36, 0 27, 0 12 C2 3, 10 0, 19 0 Z" fill="rgba(15,23,42,0.85)" stroke="url(#crestGrad)" stroke-width="1.6"/>
    <path d="M8 26 L14 18 L20 22 L28 10" fill="none" stroke="url(#goldGrad)" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" filter="url(#glow)"/>
    <circle cx="28" cy="10" r="2.2" fill="#fbbf24"/>
    <text x="19" y="34" fill="#38bdf8" font-family="system-ui, -apple-system, sans-serif" font-size="8.5" font-weight="800" text-anchor="middle" letter-spacing="0.5">{initials}</text>
  </g>

  <!-- Typography -->
  <g transform="translate(62, 0)">
    <text x="0" y="29" fill="#f8fafc" font-family="system-ui, -apple-system, sans-serif" font-size="14.5" font-weight="700" letter-spacing="0.3">{escaped_name}</text>
    <text x="0" y="45" fill="#94a3b8" font-family="ui-monospace, SFMono-Regular, monospace" font-size="9" font-weight="600" letter-spacing="0.8">WEALTH &amp; RETIREMENT SIMULATOR</text>
  </g>
</svg>"""
        model_used = "Parametric Vector Engine"

    # Sanitize and save to file (SA-22)
    svg_content = _sanitize_svg_text(svg_content)
    assets_dir = pdir / "Assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    gen_file = assets_dir / f"badge_gen_{ts}.svg"
    gen_file.write_text(svg_content, encoding="utf-8")
    _chmod_0600_safe(gen_file)
    badge_file = assets_dir / "household_badge.svg"
    badge_file.write_text(svg_content, encoding="utf-8")
    _chmod_0600_safe(badge_file)

    # Update project.json
    cfg_path = get_default_config_path(pid)
    cfg = load_config(cfg_path, portfolio_id=pid)
    rel_path = f"Portfolios/{pid}/Assets/badge_gen_{ts}.svg"
    cfg["badge_history"] = normalize_badge_history(
        cfg.get("badge_history"),
        pid,
        cfg.get("badge_image"),
        append_badge=rel_path,
    )
    cfg["badge_image"] = rel_path

    # Save badge prompt & preset metadata
    preset_name = str(body.get("preset") or "").strip()
    is_hand_edited = bool(body.get("edited_by_hand"))
    if "badge_metadata" not in cfg or not isinstance(cfg["badge_metadata"], dict):
        cfg["badge_metadata"] = {}
    cfg["badge_metadata"][rel_path] = {
        "prompt": user_prompt,
        "preset": "" if is_hand_edited else preset_name,
        "edited_by_hand": is_hand_edited,
    }
    # Prune keys not in badge_history
    hist_set = set(cfg["badge_history"])
    cfg["badge_metadata"] = {k: v for k, v in cfg["badge_metadata"].items() if k in hist_set or any(h.endswith(k) or k.endswith(h) for h in hist_set)}

    save_config(cfg, cfg_path, portfolio_id=pid)

    b64_svg = base64.b64encode(svg_content.encode("utf-8")).decode("ascii")
    data_url = f"data:image/svg+xml;base64,{b64_svg}"

    return {
        "ok": True,
        "badge_image": f"{rel_path}?_t={ts}",
        "badge_data_url": data_url,
        "badge_history": cfg["badge_history"],
        "badge_metadata": cfg["badge_metadata"],
        "badge_index": len(cfg["badge_history"]),
        "portfolio_id": pid,
        "portfolio_name": pname,
        "model_used": model_used,
        "is_fallback": is_fallback,
    }


handle_generate_portfolio_badge = handle_generate_badge


def handle_ingest_grants(body: dict[str, Any], db_path: Path | None = None, cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """Ingests canonical stock grant schedules / release history / withdrawals via HTTP API."""
    from datetime import date as _date
    from ingest.plugins.grant_json import ingest_grant_payload_dict

    if not isinstance(body, dict):
        return {"ok": False, "error": "Request body must be a JSON object"}

    pid = get_active_portfolio_id()
    if db_path is None:
        db_path = _resolve_db_path(pid)
    if db_path is None:
        return {"ok": False, "error": "No active portfolio selected."}
    if cfg is None:
        cfg_path = _resolve_config_path(pid)
        cfg = load_config(cfg_path, portfolio_id=pid) if cfg_path else default_config()
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with PortfolioDB(db_path) as db:
            res = ingest_grant_payload_dict(
                db=db,
                cfg=cfg,
                payload=body,
                reconcile=bool(body.get("reconcile", True)),
                source_label="http:/api/grants/ingest",
            )
            if body.get("rebuild"):
                from exporters.html_report import render_html_report
                from exporters.view_model import build_report_payload

                payload = build_report_payload(db, cfg, _date.today())
                render_html_report(payload, _active_index_html_path())
                res["rebuilt"] = True
                res["stock_grants"] = {
                    "has_unvested": bool((payload.get("stock_grants") or {}).get("has_unvested")),
                    "companies": [
                        c.get("symbol") for c in ((payload.get("stock_grants") or {}).get("companies") or [])
                    ],
                }
        return res
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        log.exception("Failed ingesting grants payload: %s", exc)
        return {"ok": False, "error": f"Grant ingest failed: {exc}"}


import llm_tools  # noqa: E402


def _on_active_portfolio_changed(
    res: dict[str, Any],
    *,
    ensure_save: bool = False,
    rebuild: bool = True,
    action_label: str = "portfolio change",
) -> None:
    """Invalidates cached settings/payload state, optionally ensures a default save, and re-renders."""
    global RESERVED_SAVE_NAMES
    invalidate_settings_cache()
    llm_tools.invalidate_payload_cache()
    RESERVED_SAVE_NAMES = _load_reserved_save_names()
    if ensure_save:
        ensure_default_save()
    if rebuild:
        try:
            render_active_site()
            res["rebuilt"] = True
        except Exception as exc:
            log.warning("render after %s failed: %s", action_label, exc)


def _render_markdown_html(md_text: str, title: str = "README.md") -> str:
    """Renders basic GitHub-flavored Markdown text to an elegant dark-mode HTML document."""
    import html as py_html
    lines = md_text.splitlines()
    out: list[str] = []
    in_code = False
    code_buf: list[str] = []
    list_stack: list[int] = []
    in_table = False
    table_rows: list[str] = []
    quote_buf: list[str] = []

    _ALERT_META = {
        "NOTE": ("note", "Note"),
        "TIP": ("tip", "Tip"),
        "IMPORTANT": ("important", "Important"),
        "WARNING": ("warning", "Warning"),
        "CAUTION": ("caution", "Caution"),
    }

    def flush_list() -> None:
        nonlocal list_stack
        while list_stack:
            list_stack.pop()
            out.append("</ul>")

    def flush_quote() -> None:
        nonlocal quote_buf
        if not quote_buf:
            return
        qlines = [re.sub(r"^>\s?", "", q) for q in quote_buf]
        quote_buf = []
        first_non_empty = next((i for i, ql in enumerate(qlines) if ql.strip()), None)
        alert_match = (
            re.match(r"^\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\]\s*(.*)$", qlines[first_non_empty].strip(), re.IGNORECASE)
            if first_non_empty is not None
            else None
        )
        if alert_match:
            kind = alert_match.group(1).upper()
            css_kind, title_lbl = _ALERT_META.get(kind, ("note", kind.title()))
            remainder = alert_match.group(2).strip()
            body_lines = ([remainder] if remainder else []) + qlines[first_non_empty + 1:]
            inner = "".join(f"<p>{format_inline(bl.strip())}</p>" for bl in body_lines if bl.strip())
            out.append(f'<div class="callout callout-{css_kind}"><div class="callout-title">{title_lbl}</div>{inner}</div>')
        else:
            inner = "".join(f"<p>{format_inline(ql.strip())}</p>" for ql in qlines if ql.strip())
            out.append(f"<blockquote>{inner}</blockquote>")

    def flush_table() -> None:
        nonlocal in_table, table_rows
        if in_table and table_rows:
            out.append('<div class="table-wrap"><table>')
            for idx, r in enumerate(table_rows):
                cells = [c.strip() for c in r.split("|")[1:-1]]
                if idx == 1 and all(re.match(r"^[:\-]+$", c) for c in cells):
                    continue
                tag = "th" if idx == 0 else "td"
                row_html = "".join(f"<{tag}>{format_inline(c)}</{tag}>" for c in cells)
                out.append(f"<tr>{row_html}</tr>")
            out.append("</table></div>")
            in_table = False
            table_rows = []

    def _safe_md_link(m: re.Match[str]) -> str:
        label_html = m.group(1)
        raw_href = py_html.unescape(m.group(2)).strip()
        compact = re.sub(r"[\x00-\x20]+", "", raw_href).lower()
        allowed = False
        if compact.startswith(("http://", "https://", "mailto:", "#")):
            allowed = True
        elif not compact.startswith("//") and ":" not in compact.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]:
            allowed = True
        if not allowed:
            return label_html
        return f'<a href="{py_html.escape(raw_href, quote=True)}">{label_html}</a>'

    def format_inline(s: str) -> str:
        s = py_html.escape(s)
        s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
        s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
        s = re.sub(r"\*([^*]+)\*", r"<em>\1</em>", s)
        s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", _safe_md_link, s)
        return s

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            if in_code:
                out.append(f'<pre class="code-block"><code>{py_html.escape(chr(10).join(code_buf))}</code></pre>')
                code_buf = []
                in_code = False
            else:
                flush_list()
                flush_quote()
                flush_table()
                in_code = True
            continue
        if in_code:
            code_buf.append(line)
            continue

        if stripped.startswith(">"):
            flush_list()
            if in_table:
                flush_table()
            quote_buf.append(stripped)
            continue
        elif quote_buf:
            flush_quote()

        if stripped.startswith("|") and stripped.endswith("|"):
            flush_list()
            in_table = True
            table_rows.append(stripped)
            continue
        elif in_table:
            flush_table()

        if not stripped:
            flush_list()
            flush_quote()
            continue

        if stripped in ("---", "***", "___"):
            flush_list()
            out.append("<hr />")
            continue

        m_h = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if m_h:
            flush_list()
            level = len(m_h.group(1))
            h_text = format_inline(m_h.group(2))
            out.append(f"<h{level}>{h_text}</h{level}>")
            continue

        m_list = re.match(r"^(\s*)[-*]\s+(.*)$", line)
        if m_list:
            indent = len(m_list.group(1))
            item_text = format_inline(m_list.group(2))
            if not list_stack or indent > list_stack[-1]:
                list_stack.append(indent)
                out.append("<ul>")
            elif indent < list_stack[-1]:
                while list_stack and indent < list_stack[-1]:
                    list_stack.pop()
                    out.append("</ul>")
                if not list_stack or indent > list_stack[-1]:
                    list_stack.append(indent)
                    out.append("<ul>")
            out.append(f"<li>{item_text}</li>")
            continue
        else:
            flush_list()

        out.append(f"<p>{format_inline(stripped)}</p>")

    flush_list()
    flush_quote()
    flush_table()
    if in_code and code_buf:
        out.append(f'<pre class="code-block"><code>{py_html.escape(chr(10).join(code_buf))}</code></pre>')

    body_html = "\n".join(out)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{py_html.escape(title)} — AdvisorBot</title>
  <style>
    :root {{
      --bg: #0b1120;
      --card: #111827;
      --border: #334155;
      --text: #f8fafc;
      --muted: #94a3b8;
      --accent: #38bdf8;
      --code-bg: #1e293b;
    }}
    body {{
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      margin: 0;
      padding: 0;
      line-height: 1.6;
    }}
    .topbar {{
      position: sticky;
      top: 0;
      z-index: 100;
      background: rgba(17, 24, 39, 0.94);
      backdrop-filter: blur(8px);
      border-bottom: 1px solid var(--border);
      padding: 12px 24px;
      display: flex;
      align-items: center;
      justify-content: space-between;
    }}
    .topbar-left {{ display: flex; align-items: center; gap: 12px; font-weight: 700; color: #fff; }}
    .topbar-links {{ display: flex; align-items: center; gap: 14px; font-size: 13px; }}
    .topbar-links a {{ color: var(--accent); text-decoration: none; font-weight: 600; padding: 4px 8px; border-radius: 4px; }}
    .topbar-links a:hover {{ background: rgba(56, 189, 248, 0.12); }}
    .container {{
      max-width: 920px;
      margin: 32px auto;
      padding: 0 24px 64px;
    }}
    h1, h2, h3, h4 {{ color: #ffffff; margin-top: 1.5em; margin-bottom: 0.5em; font-weight: 700; }}
    h1 {{ font-size: 28px; border-bottom: 1px solid var(--border); padding-bottom: 10px; }}
    h2 {{ font-size: 22px; border-bottom: 1px solid rgba(255,255,255,0.06); padding-bottom: 6px; }}
    h3 {{ font-size: 17px; color: var(--accent); }}
    p {{ color: #cbd5e1; font-size: 14.5px; margin: 0.8em 0; }}
    a {{ color: var(--accent); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    code {{
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      background: rgba(255, 255, 255, 0.08);
      padding: 2px 6px;
      border-radius: 4px;
      font-size: 13px;
      color: #7dd3fc;
    }}
    pre.code-block {{
      background: var(--code-bg);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 14px 18px;
      overflow-x: auto;
      font-size: 13px;
      line-height: 1.5;
    }}
    pre.code-block code {{
      background: transparent;
      padding: 0;
      color: #e2e8f0;
    }}
    ul {{ padding-left: 24px; color: #cbd5e1; font-size: 14.5px; }}
    ul ul {{ margin: 4px 0 6px; padding-left: 20px; font-size: 14px; list-style-type: circle; }}
    ul ul ul {{ margin: 3px 0 5px; padding-left: 18px; font-size: 13.5px; list-style-type: square; }}
    li {{ margin: 6px 0; }}
    hr {{ border: none; border-top: 1px solid var(--border); margin: 32px 0; }}
    .table-wrap {{ overflow-x: auto; margin: 16px 0; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 13.5px; }}
    th, td {{ border: 1px solid var(--border); padding: 8px 12px; text-align: left; }}
    th {{ background: #1e293b; color: #fff; }}
    blockquote {{
      margin: 16px 0;
      padding: 12px 16px;
      border-left: 3px solid var(--border);
      background: rgba(30, 41, 59, 0.45);
      border-radius: 0 8px 8px 0;
      color: #cbd5e1;
    }}
    blockquote p {{ margin: 0.4em 0; }}
    .callout {{
      margin: 16px 0;
      padding: 12px 16px;
      border-left: 4px solid var(--accent);
      border-radius: 0 8px 8px 0;
      background: rgba(30, 41, 59, 0.55);
    }}
    .callout-title {{
      font-size: 12px;
      font-weight: 700;
      letter-spacing: 0.04em;
      text-transform: uppercase;
      margin-bottom: 6px;
      color: var(--accent);
    }}
    .callout p {{ margin: 0.35em 0; font-size: 14px; color: #e2e8f0; }}
    .callout-important {{ border-left-color: #a855f7; background: rgba(168, 85, 247, 0.10); }}
    .callout-important .callout-title {{ color: #c084fc; }}
    .callout-warning {{ border-left-color: #f59e0b; background: rgba(245, 158, 11, 0.10); }}
    .callout-warning .callout-title {{ color: #fbbf24; }}
    .callout-caution {{ border-left-color: #ef4444; background: rgba(239, 68, 68, 0.10); }}
    .callout-caution .callout-title {{ color: #f87171; }}
    .callout-tip {{ border-left-color: #10b981; background: rgba(16, 185, 129, 0.10); }}
    .callout-tip .callout-title {{ color: #34d399; }}
  </style>
</head>
<body>
  <header class="topbar">
    <div class="topbar-left">
      <span>AdvisorBot</span>
      <span style="font-weight:400;color:var(--muted);font-size:13px;">• {py_html.escape(title)}</span>
    </div>
    <div class="topbar-links">
      <a href="/">Dashboard &rarr;</a>
      <a href="/Docs/index.html">User Manual &rarr;</a>
      <a href="/{py_html.escape(title)}?raw=1" title="View unrendered markdown">Raw Markdown</a>
    </div>
  </header>
  <main class="container">
    {body_html}
  </main>
</body>
</html>"""


def _project_portfolios_bundle_for_demo(bundle: dict[str, Any], demo_cfg: dict[str, Any]) -> dict[str, Any]:
    """Sanitizes `list_portfolios()` output in Demo Mode to expose only the projected active portfolio (SA2-11)."""
    proj = DemoTranslator(demo_cfg)
    demo_label = str((demo_cfg.get("demo_mode") or {}).get("household_label") or "").strip()
    active_id = bundle.get("active_portfolio_id")
    raw_list = bundle.get("portfolios") or []
    active_entry = next((p for p in raw_list if isinstance(p, dict) and p.get("id") == active_id), None)
    if active_entry is None and raw_list:
        active_entry = raw_list[0] if isinstance(raw_list[0], dict) else None
    if active_entry is None:
        return {
            "portfolios": [],
            "active_portfolio_id": None,
            "has_portfolios": False,
            "single_portfolio_override": bool(bundle.get("single_portfolio_override")),
            "demo_mode": True,
        }
    raw_name = str(active_entry.get("name") or "").strip()
    mapped_name = demo_label or (proj.map_text(raw_name) if raw_name else "Demo Portfolio")
    if raw_name and mapped_name == raw_name and not demo_label:
        mapped_name = "Demo Portfolio"
    demo_entry = {
        "id": "demo_portfolio",
        "name": mapped_name,
        "badge_image": "Assets/default_badge.svg",
        "symbols": [proj.map_text(str(s)) for s in (active_entry.get("symbols") or [])],
        "path": "Portfolios/demo_portfolio",
        "active": True,
        "is_complete": bool(active_entry.get("is_complete", True)),
        "missing_fields": list(active_entry.get("missing_fields") or []),
        "validation_errors": [proj.map_text(str(e)) for e in (active_entry.get("validation_errors") or [])],
    }
    return {
        "portfolios": [demo_entry],
        "active_portfolio_id": "demo_portfolio",
        "has_portfolios": True,
        "single_portfolio_override": bool(bundle.get("single_portfolio_override")),
        "demo_mode": True,
    }


def _redact_privacy_for_demo(pcfg: dict[str, Any]) -> dict[str, Any]:
    """Redacts real canonical/synonym PII and secret `scale_factor` from `GET /api/privacy` in Demo Mode (SA2-11)."""
    import copy

    out = copy.deepcopy(pcfg or {})
    for ent in out.get("entities") or []:
        if isinstance(ent, dict):
            ent["canonical"] = "[Hidden in Demo Mode]"
            ent["synonyms"] = []
            ent["meta"] = {}
    if isinstance(out.get("demo_mode"), dict):
        out["demo_mode"].pop("scale_factor", None)
    if isinstance(out.get("ai_shield"), dict):
        out["ai_shield"].pop("value_scale_factor", None)
        out["ai_shield"].pop("scale_factor", None)
    return out


def _project_project_settings_for_demo(res: dict[str, Any], demo_cfg: dict[str, Any]) -> dict[str, Any]:
    """Projects `GET /api/project` in Demo Mode: masks names, scales burn/salaries, masks DOB days, strips badges (SA2-11)."""
    proj = DemoTranslator(demo_cfg)
    out = proj.project_obj(res)
    out["portfolio_id"] = "demo_portfolio"
    out["demo_mode"] = True
    out["portfolios_bundle"] = _project_portfolios_bundle_for_demo(res.get("portfolios_bundle") or {}, demo_cfg)
    p_cfg = out.get("project")
    if isinstance(p_cfg, dict):
        demo_label = str((demo_cfg.get("demo_mode") or {}).get("household_label") or "").strip()
        if demo_label:
            p_cfg["household_name"] = demo_label
        p_cfg["badge_image"] = "Assets/default_badge.svg"
        p_cfg["badge_history"] = ["Assets/default_badge.svg"]
        p_cfg["badge_metadata"] = {}
        for list_key in ("investors", "beneficiaries", "family_members"):
            for person in p_cfg.get(list_key) or []:
                if not isinstance(person, dict):
                    continue
                for dk in ("birth_date", "birthdate"):
                    if isinstance(person.get(dk), str) and person[dk]:
                        person[dk] = re.sub(r"^(\d{4}-\d{2})-\d{2}$", r"\1-01", person[dk])
                if isinstance(person.get("ssn"), dict):
                    person["ssn"]["annual_earnings"] = {}
                    ext_f = str(person["ssn"].get("external_file") or "").strip()
                    if ext_f:
                        person["ssn"]["external_file"] = proj.map_text(Path(ext_f).name)
    return out


def _project_preferences_for_demo(prefs: dict[str, Any], demo_cfg: dict[str, Any]) -> dict[str, Any]:
    """Projects `GET /api/preferences/global` through `DemoTranslator` and strips statement paths (SA2-11)."""
    proj = DemoTranslator(demo_cfg)
    out = proj.project_obj(prefs)
    if isinstance(out, dict):
        out["demo_mode"] = True
    return out


def _project_quote_for_demo(requested_sym: str, demo_cfg: dict[str, Any]) -> dict[str, Any]:
    """Fetches live quote while projecting `symbol`, `resolved_symbol`, and issuer `name` in Demo Mode (SA2-11)."""
    proj = DemoTranslator(demo_cfg)
    req_up = str(requested_sym or "").strip().upper()
    real_sym = req_up
    demo_sym = ""
    demo_name = ""
    for ent in demo_cfg.get("entities") or []:
        if not isinstance(ent, dict) or ent.get("category") not in ("equity", "fund"):
            continue
        dmeta = ent.get("demo_meta") if isinstance(ent.get("demo_meta"), dict) else {}
        ent_demo_sym = str(dmeta.get("symbol") or ent.get("demo_value") or "").strip().upper()
        canon = str(ent.get("canonical") or "").strip()
        syns_up = {canon.upper()} | {str(s).strip().upper() for s in (ent.get("synonyms") or []) if str(s).strip()}
        if req_up and (req_up == ent_demo_sym or req_up in syns_up):
            real_sym = canon or req_up
            demo_sym = ent_demo_sym or proj.map_text(req_up).upper()
            demo_name = str(dmeta.get("name") or "").strip()
            break
    if not demo_sym:
        mapped = proj.map_text(req_up).strip().upper() if req_up else ""
        demo_sym = mapped if mapped and mapped != req_up else (req_up or "ACME")
    if not demo_name or demo_name.upper() == demo_sym:
        demo_name = f"{demo_sym.title()} Corp."

    res = fetch_live_quote(real_sym)
    out = dict(res)
    if out.get("ok"):
        out["symbol"] = demo_sym
        out["resolved_symbol"] = demo_sym
        out["name"] = demo_name
        if "shortName" in out:
            out["shortName"] = demo_name
        if "longName" in out:
            out["longName"] = demo_name
    elif "error" in out:
        out["error"] = proj.map_text(str(out["error"]))
    return out


class SavesRequestHandler(BaseHTTPRequestHandler):
    """
    Same-origin HTTP handler serving the dashboard, canonical `Scenarios`/`Saves` API,
    and async AI Advisor background jobs on loopback.
    """

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send_common_headers(self) -> None:
        # SECURITY DO (DP-01 / SA2-01 / SA2-15): Keep HTTP responses strictly same-origin on loopback with
        # zero `Access-Control-Allow-*` headers. Offline `file://` snapshots must stay offline.
        # SECURITY DON'T: Never reflect `Origin: null` (trivially forged by `<iframe sandbox="allow-scripts">`
        # on any remote website) or `http://127.0.0.1:<other-port>` with `Access-Control-Allow-Credentials`.
        if getattr(self, "_upgrade_browser_cookie", False):
            self._upgrade_browser_cookie = False
            self.send_header("Set-Cookie", self._browser_cookie())
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")

    def _send_json(self, status: int, data: dict[str, Any]) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self._send_common_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY_BYTES:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def _expected_server_port(self) -> int:
        srv_port = getattr(getattr(self, "server", None), "server_port", None)
        return int(srv_port) if srv_port is not None else DEFAULT_PORT

    def _host_ok(self) -> bool:
        raw_host = (self.headers.get("Host") or "").strip()
        if not raw_host or any(ch.isspace() for ch in raw_host):
            return False
        if any(ch in raw_host for ch in ("@", "/", "\\", "?", "#")) or raw_host.endswith(":"):
            return False
        try:
            parsed_h = urlparse(f"http://{raw_host}")
            port = parsed_h.port
        except ValueError:
            return False
        if (
            parsed_h.username is not None
            or parsed_h.password is not None
            or parsed_h.path
            or parsed_h.params
            or parsed_h.query
            or parsed_h.fragment
        ):
            return False
        hostname = (parsed_h.hostname or "").lower()
        if hostname not in ALLOWED_HOSTS:
            return False
        srv_port = getattr(getattr(self, "server", None), "server_port", None)
        if port is not None and srv_port is not None and int(port) != int(srv_port):
            return False
        return True

    def _origin_same_port_ok(self, raw_origin: str) -> bool:
        val = (raw_origin or "").strip()
        if not val or val.lower() == "null":
            return False
        try:
            po = urlparse(val)
            port = po.port
        except ValueError:
            return False
        if po.scheme.lower() != "http":
            return False
        if po.username is not None or po.password is not None or po.query or po.fragment:
            return False
        if po.path and po.path != "/":
            return False
        if (po.hostname or "").lower() not in ALLOWED_HOSTS:
            return False
        eff_port = int(port) if port is not None else 80
        return eff_port == self._expected_server_port()

    def _csrf_ok(self) -> bool:
        """
        Enforces strict same-origin + same-port + custom-header/JSON CSRF checks on mutating requests (SA2-15).
        CLI/test callers authenticating via `X-Session-Token` bypass browser-cookie CSRF checks.
        """
        # SECURITY DO: Validate same-port `Origin`, `Sec-Fetch-Site`, AND `application/json` or
        # `X-Requested-With: AdvisorBot` on every cookie-authenticated `POST`/`DELETE` request, because
        # browsers share `SameSite=Strict` cookies across all ports on `127.0.0.1` / `localhost`.
        # SECURITY DON'T: Never accept `text/plain` / form-encoded bodies without `X-Requested-With` or `X-Session-Token`.
        hdr_tok = (self.headers.get("X-Session-Token") or "").strip()
        if hdr_tok and secrets.compare_digest(hdr_tok, SESSION_TOKEN):
            return True
        sec_fetch_site = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
        if sec_fetch_site and sec_fetch_site not in ("same-origin", "none"):
            return False
        raw_origin = (self.headers.get("Origin") or "").strip()
        if raw_origin and not self._origin_same_port_ok(raw_origin):
            return False
        ctype = (self.headers.get("Content-Type") or "").strip().lower()
        xrw = (self.headers.get("X-Requested-With") or "").strip()
        if not (ctype.startswith("application/json") or xrw == "AdvisorBot"):
            return False
        return True

    def _session_signature(self, value: str) -> str:
        message = f"browser/{self._expected_server_port()}/{value}".encode()
        return hmac.digest(SESSION_TOKEN.encode(), message, "sha256").hex()

    def _browser_cookie(self) -> str:
        expires = int(time.time()) + COOKIE_MAX_AGE_SECONDS
        value = f"{expires}.{secrets.token_urlsafe(18)}"
        signed = f"{value}.{self._session_signature(value)}"
        return f"{COOKIE_NAME}={signed}; Path=/; Max-Age={COOKIE_MAX_AGE_SECONDS}; HttpOnly; SameSite=Strict"

    def _valid_browser_cookie(self, cookie: str) -> bool:
        fields = cookie.split(".")
        if len(fields) != 3 or not re.fullmatch(r"[0-9]{1,12}", fields[0]):
            return False
        if int(fields[0]) <= time.time():
            return False
        return secrets.compare_digest(fields[2].encode(), self._session_signature(".".join(fields[:2])).encode())

    def _authed(self) -> bool:
        hdr = (self.headers.get("X-Session-Token") or "").strip()
        if hdr and secrets.compare_digest(hdr, SESSION_TOKEN):
            return True
        raw_cookie = self.headers.get("Cookie")
        if raw_cookie:
            try:
                jar = SimpleCookie()
                jar.load(raw_cookie)
                morsel = jar.get(COOKIE_NAME)
                if morsel and self._valid_browser_cookie(morsel.value):
                    return True
                if morsel and secrets.compare_digest(morsel.value.encode(), SESSION_TOKEN.encode()):
                    self._upgrade_browser_cookie = True
                    return True
            except Exception:
                return False
        return False

    def _serve_unauthed_landing(self) -> None:
        # SECURITY DO: Serve a static zero-secret instructions card on unauthenticated `/` navigations (SA2-01).
        # SECURITY DON'T: Never embed `SESSION_TOKEN` or `/?token=...` redirects in unauthenticated HTML responses.
        port_num = self._expected_server_port()
        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>AdvisorBot — Authentication Required</title>
  <style>
    body {{
      background: #0b1120;
      color: #f8fafc;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      display: flex;
      align-items: center;
      justify-content: center;
      min-height: 100vh;
      margin: 0;
      padding: 20px;
      box-sizing: border-box;
    }}
    .box {{
      background: #111827;
      border: 1px solid #334155;
      border-radius: 14px;
      padding: 36px 32px;
      max-width: 480px;
      width: 100%;
      text-align: center;
      box-shadow: 0 20px 25px -5px rgba(0, 0, 0, 0.52);
    }}
    .icon {{
      width: 48px;
      height: 48px;
      margin: 0 auto 16px;
      background: rgba(56, 189, 248, 0.14);
      border: 1px solid rgba(56, 189, 248, 0.35);
      border-radius: 50%;
      display: flex;
      align-items: center;
      justify-content: center;
      color: #38bdf8;
    }}
    h2 {{
      margin: 0 0 10px;
      color: #f8fafc;
      font-size: 20px;
      font-weight: 700;
    }}
    p {{
      color: #94a3b8;
      font-size: 13.5px;
      line-height: 1.55;
      margin: 0 0 18px;
    }}
    .cmd {{
      background: #0f172a;
      border: 1px solid #334155;
      border-radius: 8px;
      padding: 12px 14px;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      font-size: 12.5px;
      color: #7dd3fc;
      text-align: left;
      overflow-x: auto;
    }}
    .sub {{
      margin-top: 22px;
      padding-top: 16px;
      border-top: 1px solid rgba(255, 255, 255, 0.08);
      font-size: 11.5px;
      color: #64748b;
      line-height: 1.5;
    }}
    .mono {{
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      background: rgba(255, 255, 255, 0.06);
      padding: 2px 6px;
      border-radius: 4px;
      font-size: 11px;
      color: #cbd5e1;
    }}
  </style>
</head>
<body>
  <div class="box">
    <div class="icon">
      <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <rect x="3" y="11" width="18" height="11" rx="2" ry="2"></rect>
        <path d="M7 11V7a5 5 0 0 1 10 0v4"></path>
      </svg>
    </div>
    <h2>Session Authentication Required</h2>
    <p>This browser does not have an active AdvisorBot session cookie. Launch or open an authenticated session from Terminal:</p>
    <div class="cmd">python3 Scripts/advisorbot_server.py --open{f" --port {port_num}" if port_num != DEFAULT_PORT else ""}</div>
    <div class="sub">
      Loopback port: <span class="mono">{port_num}</span> &bull; Persistent secret: <span class="mono">Data/.session_secret</span>
    </div>
  </div>
</body>
</html>""".encode("utf-8")
        self.send_response(403)
        self._send_common_headers()
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'")
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html)))
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(html)

    def handle_one_request(self) -> None:
        self._upgrade_browser_cookie = False
        self._demo_scope_already_flipped = False
        try:
            super().handle_one_request()
        except PrivacyConfigError as exc:
            self.close_connection = True
            self._send_json(503, {"ok": False, "error": str(exc)})

    def _pass_gate(self, path: str) -> bool:
        # SECURITY DO: Enforce both loopback Host+Port check and session auth on every private route.
        # SECURITY DON'T: Never expand `PUBLIC_PATHS` to API/portfolio/snapshot routes or skip `_host_ok()`.
        if not self._host_ok():
            self._send_json(403, {"ok": False, "error": "Forbidden host"})
            return False
        if path in PUBLIC_PATHS or _is_public_static_path(path):
            return True
        if self._authed():
            demo = _active_demo_privacy_cfg() is not None
            scope = self.headers.get("X-AdvisorBot-Portfolio")
            if scope is not None:
                if not secrets.compare_digest(scope, self._page_scope(demo)):
                    active_pid = get_active_portfolio_id()
                    if not active_pid and self._try_recover_scope_portfolio(scope):
                        return True
                    if (
                        active_pid
                        and self.command == "POST"
                        and (path.rstrip("/") or "/") == "/api/privacy"
                        and secrets.compare_digest(scope, self._page_scope_for(not demo, active_pid))
                    ):
                        self._demo_scope_already_flipped = True
                        return True
                    self._send_json(409, {"ok": False, "error": "Portfolio or Demo Mode changed. Reload this dashboard."})
                    return False
            elif self.command in ("POST", "DELETE") and get_active_portfolio_id() is not None:
                # SECURITY INVARIANT (SA5-09): Cookie-authenticated browser mutations on portfolio-scoped routes
                # MUST carry `X-AdvisorBot-Portfolio` so an un-scoped or stale tab cannot write blindly.
                hdr_tok = (self.headers.get("X-Session-Token") or "").strip()
                has_token_hdr = bool(hdr_tok and secrets.compare_digest(hdr_tok, SESSION_TOKEN))
                clean_p = path.rstrip("/") or "/"
                is_global_route = (
                    clean_p == "/api/auth"
                    or clean_p.startswith("/api/auth/")
                    or clean_p == "/api/portfolios"
                    or clean_p.startswith("/api/portfolios/")
                    or clean_p == "/api/preferences"
                    or clean_p.startswith("/api/preferences/")
                )
                if not has_token_hdr and not is_global_route:
                    self._send_json(
                        409,
                        {"ok": False, "error": "Missing portfolio scope header. Reload this dashboard."},
                    )
                    return False
            return True
        accept = (self.headers.get("Accept") or "").lower()
        if self.command == "GET" and not path.startswith("/api/") and "text/html" in accept:
            self._serve_unauthed_landing()
            return False
        self._send_json(403, {"ok": False, "error": "Unauthorized (missing session token)"})
        return False

    def _page_scope_for(self, demo: bool, portfolio: str) -> str:
        scope = f"dashboard/{int(demo)}/{portfolio}".encode()
        return hmac.digest(SESSION_TOKEN.encode(), scope, "sha256").hex()

    def _page_scope(self, demo: bool) -> str:
        return self._page_scope_for(demo, get_active_portfolio_id() or "")

    def _try_recover_scope_portfolio(self, scope: str) -> bool:
        """
        If `_active.json` lost its `active_portfolio_id` (`null`), recovers the portfolio whose
        HMAC matches the authenticated dashboard's `X-AdvisorBot-Portfolio` scope header.
        """
        from privacy.registry import PrivacyConfigError

        try:
            pdir = get_portfolios_dir()
            if not pdir.is_dir():
                return False
            candidates: list[str] = []
            last_pid = config.get_last_active_portfolio_id()
            if last_pid:
                candidates.append(last_pid)
            for p in sorted(pdir.iterdir()):
                if config.is_valid_portfolio_dir(p) and p.name not in candidates:
                    candidates.append(p.name)
            for cid in candidates:
                cdir = pdir / cid
                if not config.is_valid_portfolio_dir(cdir):
                    continue
                try:
                    pcfg = load_privacy_config(cid, auto_sync=False)
                    cdemo = bool((pcfg.get("demo_mode") or {}).get("enabled"))
                except PrivacyConfigError:
                    raise
                except Exception:
                    cdemo = False
                if secrets.compare_digest(scope, self._page_scope_for(cdemo, cid)):
                    recovered = config.recover_last_active_portfolio(preferred_id=cid, allow_single_fallback=False)
                    if recovered:
                        refresh_active_paths()
                        invalidate_settings_cache()
                        llm_tools.invalidate_payload_cache()
                        return True
        except PrivacyConfigError:
            raise
        except Exception:
            pass
        return False

    def _serve_index(self) -> None:
        from exporters.html_report import build_html_string

        demo_cfg = _active_demo_privacy_cfg()
        try:
            payload = llm_tools.get_cached_payload(bypass_export_masking=demo_cfg is not None)
            if demo_cfg is not None:
                payload = apply_demo_projection_to_payload(payload, demo_cfg)
            page = build_html_string(payload)
            scope = self._page_scope(demo_cfg is not None)
            meta = f'<meta name="advisorbot-portfolio" content="{scope}">'
            body = page.replace("</head>", meta + "</head>", 1).encode("utf-8")
        except PrivacyConfigError:
            raise
        except Exception:
            logging.getLogger(__name__).exception("Failed to build the dashboard")
            self._send_json(500, {"ok": False, "error": "Unable to build this portfolio dashboard."})
            return
        self.send_response(200)
        self._send_common_headers()
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; frame-ancestors 'none'",
        )
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_static(self, url_path: str) -> None:
        if url_path == "/favicon.ico":
            fav_target = (config.ASSETS_DIR / "default_badge.svg").resolve()
            if not fav_target.is_file():
                self._send_json(404, {"ok": False, "error": "Not found"})
                return
            body = fav_target.read_bytes()
            self.send_response(200)
            self._send_common_headers()
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'")
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        target, _, auth_ok = _resolve_static_target(url_path)
        if target is None or not auth_ok or not target.is_file():
            self._send_json(404, {"ok": False, "error": "Not found"})
            return

        demo_cfg = _active_demo_privacy_cfg()
        if demo_cfg is not None:
            # SECURITY INVARIANT (SA2-02 / SA3-02 — Default-Deny Raw Statements & Portfolio Files in Demo Mode):
            # NEVER serve custodial PDFs, activity XLSXs, or SSA XMLs from `Statements/` while Demo Mode is active,
            # and intercept `snapshots/` and `Assets/` to serve only `demo_<id>.png` and `default_badge.svg`.
            portfolios_root = get_portfolios_dir().resolve()
            active_pdir = get_portfolio_dir()
            in_statements = "Statements" in target.parts and (
                target.is_relative_to(portfolios_root)
                or (active_pdir is not None and target.is_relative_to((active_pdir / "Statements").resolve()))
                or target.is_relative_to((PROJECT_ROOT / "Statements").resolve())
            )
            if in_statements:
                self._send_json(
                    403,
                    {
                        "ok": False,
                        "error": "Raw financial statements are hidden while Demo Mode is active.",
                        "code": "demo_mode_read_only",
                    },
                )
                return

            # SA2-02: Never serve unprojected scenario snapshots while Demo Mode is active.
            if "snapshots" in target.parts and not target.name.startswith("demo_"):
                candidates = [
                    target.with_name(f"demo_{target.name}"),
                    target.with_name(f"demo_{target.stem}.png"),
                ]
                if "_" in target.stem:
                    mid_prefix = target.stem.split("_", 1)[0]
                    candidates.append(target.with_name(f"demo_{mid_prefix}.png"))
                demo_snap = next((c.resolve() for c in candidates if c.is_file()), None)
                if demo_snap is None:
                    self._send_json(404, {"ok": False, "error": "Demo snapshot not found"})
                    return
                target = demo_snap

            # SA2-02: Replace custom household badges inside Portfolios/<pid>/Assets/ with default_badge.svg.
            elif target.name != "default_badge.svg" and "Assets" in target.parts:
                in_portfolio_assets = target.is_relative_to(portfolios_root) or (
                    active_pdir is not None and target.is_relative_to((active_pdir / "Assets").resolve())
                )
                if in_portfolio_assets:
                    default_badge = (config.ASSETS_DIR / "default_badge.svg").resolve()
                    if not default_badge.is_file():
                        self._send_json(404, {"ok": False, "error": "Not found"})
                        return
                    target = default_badge
            elif target.is_relative_to(portfolios_root) and "Docs" not in target.parts and "snapshots" not in target.parts:
                self._send_json(403, {"ok": False, "error": "Portfolio files are hidden in Demo Mode.", "code": "demo_mode_read_only"})
                return

        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        rendered_md_html = False
        if target.suffix.lower() == ".md":
            accept = (self.headers.get("Accept") or "").lower()
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query or "")
            if "text/html" in accept and not (qs.get("raw") or [""])[0]:
                ctype = "text/html; charset=utf-8"
                md_text = target.read_text(encoding="utf-8", errors="replace")
                body = _render_markdown_html(md_text, title=target.name).encode("utf-8")
                rendered_md_html = True
            else:
                ctype = "text/markdown; charset=utf-8"
                body = target.read_bytes()
        else:
            if target.name in ("LICENSE", "NOTICE"):
                ctype = "text/plain; charset=utf-8"
            elif ctype.startswith("text/") or ctype in ("application/json",):
                ctype += "; charset=utf-8"
            body = target.read_bytes()
        self.send_response(200)
        self._send_common_headers()
        if rendered_md_html:
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:")
        elif target.suffix.lower() == ".svg":
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'")
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self._send_common_headers()
        self.end_headers()

    def _handle_silent_reconnect(self) -> None:
        # SECURITY DO (DP-01 / SA2-01): Require an already-authenticated session (`_authed()`) before
        # refreshing the `HttpOnly; SameSite=Strict` session cookie.
        # SECURITY DON'T: Never mint a session cookie to unauthenticated callers based solely on `Host` or `Sec-Fetch-Site`.
        if not self._host_ok():
            self._send_json(403, {"ok": False, "error": "Forbidden host"})
            return
        if not self._authed():
            self._send_json(403, {"ok": False, "error": "Unauthorized (missing or invalid session)"})
            return
        if self.command == "POST" and not self._csrf_ok():
            self._send_json(403, {"ok": False, "error": "CSRF check failed"})
            return
        sec_fetch_site = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
        if sec_fetch_site and sec_fetch_site not in ("same-origin", "none"):
            self._send_json(403, {"ok": False, "error": "Cross-site reconnect blocked"})
            return
        raw_origin = (self.headers.get("Origin") or "").strip()
        if raw_origin and not self._origin_same_port_ok(raw_origin):
            self._send_json(403, {"ok": False, "error": "Untrusted origin for reconnect"})
            return

        cookie = self._browser_cookie()
        self._upgrade_browser_cookie = False
        self.send_response(200)
        self._send_common_headers()
        self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        res_body = b'{"ok": true, "reconnected": true}'
        self.send_header("Content-Length", str(len(res_body)))
        self.end_headers()
        self.wfile.write(res_body)

    @config.with_active_portfolio
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query or "")
        if parsed.path in ("/api/auth/reconnect", "/api/auth/reconnect/"):
            self._handle_silent_reconnect()
            return
        if parsed.path in ("/", "/index.html"):
            qtok = (qs.get("token") or [""])[0].strip()
            if qtok and self._host_ok() and (_consume_browser_ticket(qtok) or secrets.compare_digest(qtok.encode(), SESSION_TOKEN.encode())):
                cookie = self._browser_cookie()
                self.send_response(302)
                self._send_common_headers()
                self.send_header("Set-Cookie", cookie)
                sync_lh = (qs.get("sync_localhost") or [""])[0].strip() == "1"
                req_host = (self.headers.get("Host") or "").split(":", 1)[0].strip().lower()
                port_num = self._expected_server_port()
                if sync_lh and req_host == "127.0.0.1":
                    self.send_header("Location", f"http://localhost:{port_num}/?token={_new_browser_ticket()}")
                else:
                    self.send_header("Location", "/")
                self.end_headers()
                return
        if not self._pass_gate(parsed.path):
            return
        if parsed.path in ("/", "/index.html"):
            self._serve_index()
            return
        if parsed.path == "/favicon.ico" or _is_public_static_path(parsed.path) or _is_authed_static_path(parsed.path):
            self._serve_static(parsed.path)
            return
        demo_cfg = _active_demo_privacy_cfg()
        if parsed.path in ("/api/portfolios", "/api/portfolios/"):
            bundle = list_portfolios()
            if demo_cfg is not None:
                bundle = _project_portfolios_bundle_for_demo(bundle, demo_cfg)
            self._send_json(200, {"ok": True, **bundle})
            return
        if parsed.path in ("/api/saves", "/api/saves/", "/api/models", "/api/models/", "/api/scenarios", "/api/scenarios/"):
            saves_res = list_saves_payload()
            if demo_cfg is not None:
                saves_res = apply_demo_projection_to_saves(saves_res, demo_cfg)
            self._send_json(200, saves_res)
            return
        if parsed.path in ("/api/privacy", "/api/privacy/"):
            pid = get_active_portfolio_id() if demo_cfg is not None else ((qs.get("portfolio_id") or [None])[0] or get_active_portfolio_id())
            pdir = get_portfolio_dir(pid) if pid else None
            has_reg = bool(pdir and (pdir / "privacy_registry.json").is_file())
            pcfg = load_privacy_config(pid, auto_sync=True) if not has_reg else load_privacy_config(pid, auto_sync=False)
            out_pid = pid
            if demo_cfg is not None:
                pcfg = _redact_privacy_for_demo(pcfg)
                out_pid = "demo_portfolio"
            self._send_json(
                200,
                {
                    "ok": True,
                    "portfolio_id": out_pid,
                    "privacy": pcfg,
                    "models": llm_tools.list_available_models(probe=False),
                },
            )
            return
        if parsed.path in ("/api/preferences", "/api/preferences/global"):
            prefs = load_global_preferences()
            if demo_cfg is not None:
                prefs = _project_preferences_for_demo(prefs, demo_cfg)
            self._send_json(200, prefs)
            return
        if parsed.path in ("/api/project", "/api/project/"):
            pid = None if demo_cfg is not None else (qs.get("portfolio_id") or [None])[0]
            proj_res = get_project_settings(portfolio_id=pid)
            if demo_cfg is not None:
                proj_res = _project_project_settings_for_demo(proj_res, demo_cfg)
            self._send_json(200, proj_res)
            return
        if parsed.path in ("/api/ssn/files", "/api/ssn/files/"):
            if demo_cfg is not None:
                self._send_json(200, {"ok": True, "files": [], "default_folder": "Statements/SSN", "demo_mode": True})
                return
            pid = (qs.get("portfolio_id") or [None])[0]
            self._send_json(200, list_ssn_statement_files(portfolio_id=pid))
            return
        if parsed.path in ("/api/portfolio/statements", "/api/statements", "/api/statements/"):
            pid = (qs.get("portfolio_id") or [None])[0]
            res = handle_list_statements(portfolio_id=pid)
            self._send_json(200 if res.get("ok") else 400, res)
            return
        if parsed.path == "/api/advisor/status":
            job_id = (qs.get("job_id") or [None])[0]
            st = llm_tools.get_advisor_job_status(job_id=job_id)
            if demo_cfg is not None:
                st = DemoTranslator(demo_cfg).project_obj(st)
            self._send_json(200, st)
            return
        if parsed.path == "/api/quote":
            sym = (qs.get("symbol") or qs.get("s") or [_resolve_active_primary_symbol()])[0]
            if demo_cfg is not None:
                res = _project_quote_for_demo(sym, demo_cfg)
                self._send_json(200 if res.get("ok") else 400, res)
                return
            res = fetch_live_quote(sym)
            self._send_json(200 if res.get("ok") else 400, res)
            return
        if parsed.path in ("/api/quote/primary", "/api/quote/goog"):
            prim_sym = _resolve_active_primary_symbol()
            if demo_cfg is not None:
                res = _project_quote_for_demo(prim_sym, demo_cfg)
                self._send_json(200 if res.get("ok") else 502, res)
                return
            res = fetch_live_quote(prim_sym)
            self._send_json(200 if res.get("ok") else 502, res)
            return
        if parsed.path == "/api/health":
            self._send_json(200, {"ok": True, "session_active": self._authed()})
            return
        if parsed.path in ("/api/llm/models", "/api/llm/models/"):
            do_probe = (qs.get("probe") or ["0"])[0].lower() in ("1", "true", "yes")
            models_list = llm_tools.list_available_models(probe=do_probe)
            if demo_cfg is not None:
                models_list = DemoTranslator(demo_cfg).project_obj(models_list)
            self._send_json(200, {"ok": True, "models": models_list})
            return
        if parsed.path == "/api/ui/poll":
            since_val = int((qs.get("since") or ["0"])[0] or 0)
            poll_res = llm_tools.poll_ui_actions(since_seq=since_val)
            if demo_cfg is not None:
                poll_res = DemoTranslator(demo_cfg).project_obj(poll_res)
            self._send_json(200, poll_res)
            return
        if parsed.path == "/api/llm/overview":
            ov_res = llm_tools.tool_get_overview()
            if demo_cfg is not None:
                ov_res = DemoTranslator(demo_cfg).project_obj(ov_res)
            self._send_json(200, ov_res)
            return
        if parsed.path == "/api/llm/positions":
            account_id = (qs.get("account_id") or qs.get("account") or [None])[0]
            symbol = (qs.get("symbol") or qs.get("search") or [None])[0]
            has_mat = (qs.get("has_maturity") or [""])[0].lower() in ("1", "true", "yes")
            pos_res = llm_tools.tool_query_positions(
                has_maturity=has_mat if "has_maturity" in qs else None,
                account_id=account_id,
                symbol=symbol,
            )
            if demo_cfg is not None:
                pos_res = DemoTranslator(demo_cfg).project_obj(pos_res)
                if isinstance(pos_res, dict) and isinstance(pos_res.get("positions"), list):
                    pos_res["total_value_usd"] = round(
                        sum(float(p.get("market_value") or 0.0) for p in pos_res["positions"] if isinstance(p, dict)),
                        2,
                    )
            self._send_json(200, pos_res)
            return
        self._send_json(404, {"ok": False, "error": "Not found"})

    def do_POST(self) -> None:  # noqa: N802
        if urlparse(self.path).path.rstrip("/") in {"/api/portfolios", "/api/portfolios/select", "/api/portfolios/delete"}:
            self._handle_post()
        else:
            with config.portfolio_context(get_active_portfolio_id()):
                self._handle_post()

    def _handle_post(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/auth/launch":
            nonce = self.headers.get("X-Launch-Nonce", "")
            proof = self.headers.get("X-Launch-Proof", "")
            port = self._expected_server_port()
            if (not self._host_ok() or not re.fullmatch(r"[0-9a-f]{64}", nonce)
                    or not secrets.compare_digest(proof.encode(), _launch_proof("request", port, nonce).encode())):
                self._send_json(403, {"ok": False, "error": "Unauthorized launcher"})
                return
            reply_nonce = secrets.token_hex(32)
            _new_browser_ticket(_launch_proof("ticket", port, nonce, reply_nonce), nonce)
            self._send_json(200, {"nonce": reply_nonce, "proof": _launch_proof("response", port, nonce, reply_nonce)})
            return
        if parsed.path in ("/api/auth/reconnect", "/api/auth/reconnect/"):
            self._handle_silent_reconnect()
            return
        if not self._pass_gate(parsed.path):
            return
        if not self._csrf_ok():
            self._send_json(403, {"ok": False, "error": "CSRF check failed"})
            return
        if _active_demo_privacy_cfg() is not None and _is_demo_write_blocked(parsed.path):
            self._send_json(
                403,
                {
                    "ok": False,
                    "demo_blocked": True,
                    "error": "Demo Mode is active (read-only view). Disable Demo Mode in Portfolio Settings -> Privacy before saving.",
                },
            )
            return
        body = self._read_json_body()
        try:
            self._dispatch_post(parsed, body)
        except Exception as exc:
            log.exception("Unhandled error processing POST %s: %s", parsed.path, exc)
            self._send_json(500, {"ok": False, "error": f"Server error: {exc}"})

    def _dispatch_post(self, parsed: Any, body: dict[str, Any]) -> None:
        if parsed.path in ("/api/privacy", "/api/privacy/"):
            # SECURITY INVARIANT (SA3-01 — Demo Mode Privacy Write Lock & Response Redaction):
            # 1. While Demo Mode is active, ONLY allow exiting Demo Mode (`demo_mode.enabled == False`), stripping
            #    any `entities`, `ai_shield`, or `mcp_server` fields so Demo-masked placeholders
            #    (`"[Hidden in Demo Mode]"`) can never mutate `privacy_registry.json` and non-exit writes are 403-blocked.
            # 2. Whenever the saved config has `demo_mode.enabled == True` (e.g. turning Demo Mode ON), ALWAYS pass
            #    `res["privacy"]` through `_redact_privacy_for_demo()` before returning JSON to the browser!
            was_demo_active = _active_demo_privacy_cfg() is not None
            if getattr(self, "_demo_scope_already_flipped", False):
                dm_req = body.get("demo_mode") if isinstance(body.get("demo_mode"), dict) else None
                is_pure_idempotent_toggle = (
                    dm_req is not None
                    and set(dm_req.keys()) == {"enabled"}
                    and isinstance(dm_req.get("enabled"), bool)
                    and dm_req.get("enabled") == was_demo_active
                    and set(body.keys()) <= {"demo_mode", "portfolio_id"}
                )
                if not is_pure_idempotent_toggle:
                    self._send_json(409, {"ok": False, "error": "Portfolio or Demo Mode changed. Reload this dashboard."})
                    return
                pid = get_active_portfolio_id()
                save_body = {"demo_mode": {"enabled": was_demo_active}}
            elif was_demo_active:
                dm_req = body.get("demo_mode") if isinstance(body.get("demo_mode"), dict) else None
                if not (dm_req is not None and dm_req.get("enabled") is False):
                    self._send_json(
                        403,
                        {
                            "ok": False,
                            "error": "Demo Mode is read-only. Exit Demo Mode before editing Privacy & AI Shield settings.",
                            "code": "demo_mode_read_only",
                        },
                    )
                    return
                pid = get_active_portfolio_id()
                save_body = {"demo_mode": {"enabled": False}}
            else:
                pid = str(body.get("portfolio_id") or "").strip() or get_active_portfolio_id()
                save_body = body
            res = save_privacy_config(save_body, portfolio_id=pid)
            if res.get("ok"):
                llm_tools.invalidate_payload_cache()
                is_pure_demo_toggle = (
                    set(save_body.keys()) == {"demo_mode"}
                    and isinstance(save_body.get("demo_mode"), dict)
                    and set(save_body["demo_mode"].keys()) == {"enabled"}
                )
                if is_pure_demo_toggle:
                    def _bg_render_after_demo_toggle(target_pid: str | None = pid) -> None:
                        try:
                            with config.portfolio_context(target_pid):
                                render_active_site()
                        except Exception as exc:
                            log.warning("background render_active_site failed after Demo Mode toggle: %s", exc)

                    threading.Thread(target=_bg_render_after_demo_toggle, daemon=True).start()
                else:
                    try:
                        render_active_site()
                    except Exception as exc:
                        log.warning("render_active_site failed after privacy save: %s", exc)
                res["models"] = llm_tools.list_available_models(probe=False)
                if isinstance(res.get("privacy"), dict) and ((res["privacy"].get("demo_mode") or {}).get("enabled")):
                    res["privacy"] = _redact_privacy_for_demo(res["privacy"])
            self._send_json(200 if res.get("ok") else 400, res)
            return
        if parsed.path == "/api/privacy/sync":
            if _active_demo_privacy_cfg() is not None:
                self._send_json(403, {"ok": False, "error": "Demo Mode is read-only.", "code": "demo_mode_read_only"})
                return
            pid = str(body.get("portfolio_id") or "").strip() or get_active_portfolio_id()
            pcfg = sync_privacy_registry(pid, save=True)
            llm_tools.invalidate_payload_cache()
            try:
                render_active_site()
            except Exception as exc:
                log.warning("render_active_site failed after privacy sync: %s", exc)
            if (pcfg.get("demo_mode") or {}).get("enabled"):
                pcfg = _redact_privacy_for_demo(pcfg)
            self._send_json(200, {"ok": True, "portfolio_id": pid, "privacy": pcfg})
            return
        if parsed.path == "/api/privacy/regenerate-demo":
            if _active_demo_privacy_cfg() is not None:
                self._send_json(403, {"ok": False, "error": "Demo Mode is read-only.", "code": "demo_mode_read_only"})
                return
            pid = str(body.get("portfolio_id") or "").strip() or get_active_portfolio_id()
            new_seed = f"demo-{secrets.randbelow(9000) + 1000}"
            res = save_privacy_config({"demo_mode": {"demo_seed": new_seed}}, portfolio_id=pid)
            if res.get("ok"):
                llm_tools.invalidate_payload_cache()
                try:
                    render_active_site()
                except Exception as exc:
                    log.warning("render_active_site failed after regenerate-demo: %s", exc)
                if isinstance(res.get("privacy"), dict) and ((res["privacy"].get("demo_mode") or {}).get("enabled")):
                    res["privacy"] = _redact_privacy_for_demo(res["privacy"])
            self._send_json(200 if res.get("ok") else 400, res)
            return
        if parsed.path == "/api/privacy/preview":
            if _active_demo_privacy_cfg() is not None:
                self._send_json(403, {"ok": False, "error": "Wire preview is disabled while Demo Mode is active.", "code": "demo_mode_read_only"})
                return
            res = _handle_privacy_preview(body)
            self._send_json(200, res)
            return
        if parsed.path in ("/api/portfolios", "/api/portfolios/"):
            name = str(body.get("name") or "").strip()
            if not name:
                self._send_json(400, {"ok": False, "error": "Portfolio name is required."})
                return
            activate = bool(body.get("activate", True))
            res = create_portfolio(name, activate=activate)
            if res.get("ok") and res.get("active"):
                _on_active_portfolio_changed(
                    res,
                    ensure_save=True,
                    rebuild=bool(body.get("rebuild", True)),
                    action_label="create_portfolio",
                )
            self._send_json(200 if res.get("ok") else 400, res)
            return
        if parsed.path in ("/api/portfolios/select", "/api/portfolios/select/"):
            pid = body.get("portfolio_id") if "portfolio_id" in body else body.get("id")
            do_rebuild = bool(body.get("rebuild", True))
            res = set_active_portfolio(str(pid).strip() if pid else None)
            if res.get("ok"):
                _on_active_portfolio_changed(
                    res,
                    ensure_save=bool(res.get("active_portfolio_id")),
                    rebuild=do_rebuild,
                    action_label="select_portfolio",
                )
            self._send_json(200 if res.get("ok") else 400, res)
            return
        if parsed.path in ("/api/portfolios/delete", "/api/portfolios/delete/"):
            pid = str(body.get("portfolio_id") or body.get("id") or "").strip()
            confirm_word = str(body.get("confirm") or "").strip().lower()
            if not pid:
                self._send_json(400, {"ok": False, "error": "Missing portfolio id."})
                return
            if confirm_word != "yes":
                self._send_json(400, {"ok": False, "error": "Please type 'yes' to confirm permanent deletion."})
                return
            with _MODEL_STORE_LOCK:
                res = delete_portfolio(pid)
            if res.get("ok"):
                _on_active_portfolio_changed(
                    res,
                    ensure_save=False,
                    rebuild=True,
                    action_label="delete_portfolio",
                )
            self._send_json(200 if res.get("ok") else 400, res)
            return
        if parsed.path in ("/api/preferences", "/api/preferences/global"):
            res = handle_save_global_preferences(body)
            self._send_json(200 if res.get("ok") else 400, res)
            return
        if parsed.path in ("/api/project", "/api/project/"):
            res = handle_save_project_settings(body)
            self._send_json(200 if res.get("ok") else 400, res)
            return
        if parsed.path in ("/api/portfolio/generate_badge", "/api/badge/generate"):
            res = handle_generate_badge(body)
            self._send_json(200 if res.get("ok") else 400, res)
            return
        if parsed.path in ("/api/grants/ingest", "/api/grants/ingest/"):
            res = handle_ingest_grants(body)
            self._send_json(200 if res.get("ok") else 400, res)
            return
        if parsed.path in ("/api/ssn/parse", "/api/ssn/parse/"):
            res = handle_parse_ssn_statement(body)
            self._send_json(200 if res.get("ok") else 400, res)
            return
        if parsed.path in ("/api/ssn/save_file", "/api/ssn/save_file/"):
            res = handle_save_ssn_file(body)
            self._send_json(200 if res.get("ok") else 400, res)
            return
        if parsed.path in ("/api/saves", "/api/saves/", "/api/models", "/api/models/", "/api/scenarios", "/api/scenarios/"):
            name = str(body.get("name") or "").strip()
            settings = body.get("settings")
            if not name or not isinstance(settings, dict):
                self._send_json(400, {"ok": False, "error": "Missing name or settings"})
                return
            if not get_active_portfolio_id():
                req_pid = str(body.get("portfolio_id") or "").strip() or None
                recovered = config.recover_last_active_portfolio(preferred_id=req_pid, allow_single_fallback=False)
                if recovered:
                    refresh_active_paths()
                    invalidate_settings_cache()
                    llm_tools.invalidate_payload_cache()
            create_new = bool(body.get("create_new", False))
            confirm_overwrite = bool(body.get("overwrite_existing", False) or body.get("confirm_protected", False))
            raw_mid = str(body.get("id") or body.get("model_id") or "").strip()
            clean_name = sanitize_save_name(name)
            if not create_new and not confirm_overwrite and (
                (raw_mid and is_scenario_protected(raw_mid))
                or is_scenario_protected(name)
                or (clean_name and is_scenario_protected(clean_name))
            ):
                self._send_json(
                    409,
                    {"ok": False, "protected": True, "error": f"Scenario {name!r} is protected. Confirm overwrite to proceed."},
                )
                return
            set_act = bool(body.get("set_active", True))
            res = write_save_file(
                name,
                settings,
                set_active=set_act,
                model_id=body.get("id") or body.get("model_id"),
                create_new=create_new,
                author=body.get("author"),
                notes_md=body.get("notes_md"),
                kpi_summary=body.get("kpi_summary") if isinstance(body.get("kpi_summary"), dict) else None,
                goog_price_anchor=body.get("goog_price_anchor"),
                snapshot_data_url=body.get("snapshot_data_url"),
                is_protected=bool(body["is_protected"]) if "is_protected" in body and body["is_protected"] is not None else None,
                allow_protected_overwrite=confirm_overwrite,
            )
            if not res.get("ok") and res.get("protected"):
                self._send_json(409, res)
                return
            self._send_json(200 if res.get("ok", True) else 400, res)
            return
        if parsed.path in ("/api/saves/active", "/api/models/active", "/api/scenarios/active"):
            target = str(body.get("id") or body.get("name") or "").strip()
            if not target:
                self._send_json(400, {"ok": False, "error": "Missing id or name"})
                return
            res = set_active_save(target)
            self._send_json(200, res)
            return
        if parsed.path in ("/api/models/protect", "/api/saves/protect", "/api/scenarios/protect"):
            target = str(body.get("id") or body.get("name") or "").strip()
            if not target or "is_protected" not in body:
                self._send_json(400, {"ok": False, "error": "Missing id/name or is_protected"})
                return
            res = set_model_protected(target, bool(body.get("is_protected")))
            self._send_json(200 if res.get("ok") else 400, res)
            return
        if parsed.path in ("/api/models/rename", "/api/saves/rename", "/api/scenarios/rename"):
            target = str(body.get("id") or body.get("name") or "").strip()
            new_name = str(body.get("new_name") or "").strip()
            if not target or not new_name:
                self._send_json(400, {"ok": False, "error": "Missing id/name or new_name"})
                return
            confirm_protected = bool(body.get("confirm_protected") or body.get("overwrite_existing"))
            if is_scenario_protected(target) and not confirm_protected:
                self._send_json(
                    409,
                    {"ok": False, "protected": True, "error": f"Scenario {target!r} is protected. Confirm rename to proceed."},
                )
                return
            res = rename_model(target, new_name)
            self._send_json(200 if res.get("ok") else 400, res)
            return
        if parsed.path in ("/api/models/notes", "/api/saves/notes", "/api/scenarios/notes"):
            target = str(body.get("id") or body.get("name") or "").strip()
            if not target or "notes_md" not in body:
                self._send_json(400, {"ok": False, "error": "Missing id/name or notes_md"})
                return
            res = update_model_notes(target, str(body.get("notes_md") or ""))
            self._send_json(200 if res.get("ok") else 400, res)
            return
        if parsed.path in ("/api/models/upvote", "/api/saves/upvote", "/api/scenarios/upvote"):
            target = str(body.get("id") or body.get("name") or "").strip()
            if not target:
                self._send_json(400, {"ok": False, "error": "Missing id or name"})
                return
            res = append_model_upvote(
                target,
                model_name=str(body.get("model_name") or body.get("model") or "advisor"),
                reason=str(body.get("reason") or ""),
            )
            self._send_json(200 if res.get("ok") else 400, res)
            return
        if parsed.path in ("/api/models/snapshot", "/api/saves/snapshot", "/api/scenarios/snapshot"):
            target = str(body.get("id") or body.get("name") or "").strip()
            img_data = body.get("image_data") or body.get("snapshot_data_url")
            if not target or not img_data:
                self._send_json(400, {"ok": False, "error": "Missing id/name or image_data"})
                return
            res = save_model_snapshot(target, img_data)
            self._send_json(200 if res.get("ok") else 400, res)
            return
        if parsed.path in ("/api/saves/delete", "/api/models/delete", "/api/scenarios/delete"):
            target = str(body.get("id") or body.get("name") or "").strip()
            if not target:
                self._send_json(400, {"ok": False, "error": "Missing id or name"})
                return
            confirm_protected = bool(body.get("confirm_protected") or body.get("overwrite_existing"))
            if is_scenario_protected(target) and not confirm_protected:
                self._send_json(
                    409,
                    {"ok": False, "protected": True, "error": f"Scenario {target!r} is protected. Confirm delete to proceed."},
                )
                return
            res = delete_save_file(target)
            self._send_json(200, res)
            return
        if parsed.path in ("/api/rebuild", "/api/refresh-site", "/api/portfolio/refresh", "/api/statements/refresh"):
            res = run_site_rebuild()
            self._send_json(200 if res.get("ok") else 500, res)
            return
        if parsed.path == "/api/ui/poll":
            since_val = int(body.get("since") or 0)
            browser_state = body.get("browser_state") if isinstance(body.get("browser_state"), dict) else None
            poll_res = llm_tools.poll_ui_actions(since_seq=since_val, browser_state=browser_state)
            demo_cfg = _active_demo_privacy_cfg()
            if demo_cfg is not None:
                poll_res = DemoTranslator(demo_cfg).project_obj(poll_res)
            self._send_json(200, poll_res)
            return
        if parsed.path in ("/api/llm/probe", "/api/llm/models/probe"):
            target_model = body.get("model") or body.get("id")
            if target_model:
                cfg = llm_tools.load_llm_config(target_model)
                res = llm_tools.probe_llm_config(cfg)
                self._send_json(200, {"ok": True, "model": target_model, "result": res})
            else:
                models = llm_tools.list_available_models(probe=True)
                self._send_json(200, {"ok": True, "models": models})
            return
        if parsed.path == "/api/ui/enqueue":
            if "source_portfolio_id" in body and body["source_portfolio_id"] != get_active_portfolio_id():
                self._send_json(409, {"ok": False, "error": "The active portfolio changed."})
                return
            action = body.get("action") if isinstance(body.get("action"), dict) else body
            ev = llm_tools.enqueue_ui_action(action, forward_to_http=False)
            self._send_json(200, {"ok": True, "event": ev})
            return
        if parsed.path == "/api/ui/control":
            refresh_act = body.get("refresh_action")
            if not refresh_act:
                if body.get("refresh_primary_quote") or body.get("refresh_goog_quote"):
                    refresh_act = "primary_quote"
                elif body.get("reload_page"):
                    refresh_act = "reload_page"
            res = llm_tools.tool_control_live_ui(
                settings_patch=body.get("settings_patch") or body.get("tune_settings"),
                view_patch=body.get("view_patch"),
                switch_screen=body.get("switch_screen") or body.get("switch_tab"),
                refresh_action=refresh_act,
                use_ref_rates=bool(body.get("use_ref_rates", body.get("use_reference_rates", False))),
                reoptimize_order=bool(body.get("reoptimize_order", False)),
            )
            self._send_json(200 if res.get("ok") else 400, res)
            return
        if parsed.path == "/api/llm/positions":
            res = llm_tools.tool_query_positions(
                has_maturity=body.get("has_maturity"),
                account_id=body.get("account_id") or body.get("account"),
                symbol=body.get("symbol") or body.get("search"),
            )
            self._send_json(200, res)
            return
        if parsed.path == "/api/llm/simulate":
            overrides = dict(body.get("overrides") or body.get("settings_overrides") or {})
            if body.get("gifts") is not None:
                overrides["scheduled_actions"] = body.get("gifts")
            if body.get("scheduled_actions") is not None:
                overrides["scheduled_actions"] = body.get("scheduled_actions")
            if body.get("hypothetical_assets") is not None:
                overrides["hypothetical_assets"] = body.get("hypothetical_assets")
            res = llm_tools.tool_run_simulation(
                base_save=body.get("base_save") or body.get("base_save_name"),
                use_ref_rates=bool(body.get("use_ref_rates", body.get("use_reference_rates", False))),
                overrides=overrides or None,
                rollovers=body.get("rollovers") or body.get("hypothetical_rollovers"),
                reoptimize_order=bool(body.get("reoptimize_order", False)),
                compare_with_baseline=bool(body.get("compare_with_baseline", False)),
                apply_non_goog_rate_mode=str(body.get("apply_non_primary_rate_mode") or body.get("apply_non_goog_rate_mode") or "none"),
                uma_rate_mode=str(body.get("uma_rate_mode") or "ytd_statement"),
            )
            self._send_json(200 if res.get("ok") else 400, res)
            return
        if parsed.path in ("/api/llm/non-primary-growth", "/api/llm/non-goog-growth"):
            res = llm_tools.tool_compute_non_primary_growth(
                rollovers=body.get("rollovers") or body.get("hypothetical_rollovers"),
                uma_rate_mode=str(body.get("uma_rate_mode") or "ytd_statement"),
                muni_rate_mode=str(body.get("muni_rate_mode") or "price_cagr"),
                include_cash=bool(body.get("include_cash", False)),
                asset_rate_overrides=body.get("asset_rate_overrides"),
            )
            self._send_json(200 if res.get("ok") else 400, res)
            return
        if parsed.path == "/api/llm/solve":
            overrides = dict(body.get("overrides") or body.get("settings_overrides") or {})
            if body.get("gifts") is not None:
                overrides["scheduled_actions"] = body.get("gifts")
            target_metric = body.get("target_metric")
            if not target_metric and body.get("target_milestone"):
                ms = str(body.get("target_milestone")).lower()
                dm = "nominal" if str(body.get("target_dollar_mode") or "real").lower() == "nominal" else "real"
                target_metric = f"{ms}_{dm}"
            target_val = float(
                body.get("target_value")
                if body.get("target_value") is not None
                else (body.get("target_amount") or 2_000_000.0)
            )
            res = llm_tools.tool_solve_simulation(
                solve_for=str(body.get("solve_for") or "primary_growth"),
                target_metric=str(target_metric) if target_metric else None,
                target_value=target_val,
                base_save=body.get("base_save") or body.get("base_save_name"),
                use_ref_rates=bool(body.get("use_ref_rates", body.get("use_reference_rates", False))),
                overrides=overrides or None,
            )
            self._send_json(200 if res.get("ok") else 400, res)
            return
        if parsed.path == "/api/advisor/apply":
            res = llm_tools.apply_advisor_proposal(str(body.get("job_id") or ""))
            self._send_json(200 if res.get("ok") else 409, res)
            return
        if parsed.path == "/api/advisor/run":
            res = llm_tools.start_advisor_job(
                prompt_type=body.get("prompt_type"),
                custom_prompt=body.get("prompt") or body.get("message"),
                model=body.get("model"),
                ui_state=body.get("browser_state") if isinstance(body.get("browser_state"), dict) else None,
                conversation_id=body.get("conversation_id"),
            )
            self._send_json(200 if res.get("ok") else 400, res)
            return
        if parsed.path == "/api/chat":
            messages = body.get("messages") if isinstance(body.get("messages"), list) else []
            browser_state = body.get("browser_state") if isinstance(body.get("browser_state"), dict) else None
            prompt_text = str(body.get("prompt") or body.get("message") or "")
            if not prompt_text and messages:
                prompt_text = str(messages[-1].get("content") or "")

            chosen_model = str(body.get("model") or "default")
            demo_cfg = _active_demo_privacy_cfg()
            demo_proj = DemoTranslator(demo_cfg) if demo_cfg is not None else None

            def _chat_step(step_info: dict[str, Any]) -> None:
                summary_txt = str(step_info.get("summary") or "Working...")
                status_txt = step_info.get("status_label")
                if demo_proj is not None:
                    summary_txt = demo_proj._scale_inline_usd_text(demo_proj.map_text(summary_txt))
                    if isinstance(status_txt, str):
                        status_txt = demo_proj._scale_inline_usd_text(demo_proj.map_text(status_txt))
                llm_tools.enqueue_ui_action({
                    "type": "chat_tool_step",
                    "tool": step_info.get("tool"),
                    "summary": summary_txt,
                    "progress_pct": step_info.get("progress_pct"),
                    "status_label": status_txt,
                })

            _chat_step({
                "tool": "init",
                "summary": f"Connecting to {chosen_model}...",
                "progress_pct": 5,
                "status_label": f"Model: {chosen_model}",
            })

            res = llm_tools.run_chat_via_agy_cli(
                message=prompt_text,
                conversation_id=body.get("conversation_id"),
                model=chosen_model,
                ui_state=browser_state,
                on_step=_chat_step,
            )
            self._send_json(200 if res.get("ok") else 400, res)
            return
        self._send_json(404, {"ok": False, "error": "Not found"})

    @config.with_active_portfolio
    def do_DELETE(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if not self._pass_gate(parsed.path):
            return
        if not self._csrf_ok():
            self._send_json(403, {"ok": False, "error": "CSRF check failed"})
            return
        if _active_demo_privacy_cfg() is not None and _is_demo_write_blocked(parsed.path):
            self._send_json(
                403,
                {
                    "ok": False,
                    "demo_blocked": True,
                    "error": "Demo Mode is active (read-only view). Disable Demo Mode in Portfolio Settings -> Privacy before deleting.",
                },
            )
            return
        qs = parse_qs(parsed.query or "")
        body = self._read_json_body()
        if parsed.path in ("/api/preferences", "/api/preferences/global"):
            res = handle_save_global_preferences(body)
            self._send_json(200 if res.get("ok") else 400, res)
            return
        if parsed.path in ("/api/saves", "/api/saves/", "/api/models", "/api/models/", "/api/scenarios", "/api/scenarios/"):
            target = str(
                body.get("id")
                or body.get("name")
                or (qs.get("id") or qs.get("name") or [""])[0]
            ).strip()
            if not target:
                self._send_json(400, {"ok": False, "error": "Missing id or name"})
                return
            confirm_q = str((qs.get("confirm_protected") or [""])[0]).strip().lower() in ("1", "true", "yes")
            confirm_protected = bool(confirm_q or body.get("confirm_protected") or body.get("overwrite_existing"))
            if is_scenario_protected(target) and not confirm_protected:
                self._send_json(
                    409,
                    {"ok": False, "protected": True, "error": f"Scenario {target!r} is protected. Confirm delete to proceed."},
                )
                return
            res = delete_save_file(target)
            self._send_json(200, res)
            return
        self._send_json(404, {"ok": False, "error": "Not found"})


def _rotate_server_logs(max_bytes: int = 512 * 1024) -> None:
    """Caps `Data/advisorbot_server.{log,err}` size by rotating oversized logs on startup and enforcing `0o600` (SA-24 / SA2-18)."""
    for fname in ("advisorbot_server.log", "advisorbot_server.err", "saves_server.log", "saves_server.err"):
        lp = DATA_DIR / fname
        try:
            if lp.is_file():
                os.chmod(lp, 0o600)
                if lp.stat().st_size > max_bytes:
                    rot = DATA_DIR / f"{fname}.1"
                    lp.replace(rot)
                    os.chmod(rot, 0o600)
        except OSError:
            pass


def _harden_portfolio_permissions() -> None:
    """Enforces 0o600 permissions on sensitive local portfolio, SQLite, log, and secret artifacts (SA-08 / SA2-18)."""
    try:
        idx_p = _active_index_html_path()
        if idx_p.is_file():
            os.chmod(idx_p, 0o600)
        for data_fp in (
            _resolve_session_secret_file(),
            get_session_token_file(),
            DATA_DIR / "advisorbot_server.log",
            DATA_DIR / "advisorbot_server.err",
            DATA_DIR / "advisorbot_server.log.1",
            DATA_DIR / "advisorbot_server.err.1",
            DATA_DIR / "saves_server.log",
            DATA_DIR / "saves_server.err",
            DATA_DIR / "saves_server.log.1",
            DATA_DIR / "saves_server.err.1",
        ):
            if data_fp.is_file():
                os.chmod(data_fp, 0o600)
        # SA3-09c: Harden legacy `Data/*.sqlite*` files and shared `Data/Config/LLM/*.json` credential configs.
        if DATA_DIR.is_dir():
            for sq_fp in DATA_DIR.glob("*.sqlite*"):
                if sq_fp.is_file():
                    os.chmod(sq_fp, 0o600)
            llm_dir = DATA_DIR / "Config" / "LLM"
            if llm_dir.is_dir():
                for cfg_fp in llm_dir.glob("*.json"):
                    if cfg_fp.is_file():
                        os.chmod(cfg_fp, 0o600)
        p_root = get_portfolios_dir()
        active_idx = p_root / "_active.json"
        if active_idx.is_file():
            os.chmod(active_idx, 0o600)
        candidate_dirs: list[Path] = []
        if p_root.is_dir():
            candidate_dirs.extend(d for d in p_root.iterdir() if d.is_dir() and not d.name.startswith("."))
        active_pdir = get_portfolio_dir()
        if active_pdir is not None and active_pdir.is_dir() and active_pdir not in candidate_dirs:
            candidate_dirs.append(active_pdir)
        for pdir in candidate_dirs:
            for rel in ("project.json", "privacy_registry.json", "account_map.json"):
                fp = pdir / rel
                if fp.is_file():
                    os.chmod(fp, 0o600)
            for fp in pdir.glob("portfolio.sqlite*"):
                if fp.is_file():
                    os.chmod(fp, 0o600)
            db_dir = pdir / "db"
            if db_dir.is_dir():
                for fp in db_dir.glob("portfolio.sqlite*"):
                    if fp.is_file():
                        os.chmod(fp, 0o600)
    except OSError:
        pass


_MAX_BROWSER_TICKETS = 64
_BROWSER_TICKETS: dict[str, str] = {}
_BROWSER_TICKETS_LOCK = threading.Lock()


def _new_browser_ticket(ticket: str | None = None, request_id: str | None = None) -> str:
    request_id = request_id or secrets.token_hex(16)
    ticket = ticket or secrets.token_urlsafe(32)
    with _BROWSER_TICKETS_LOCK:
        if request_id in _BROWSER_TICKETS:
            del _BROWSER_TICKETS[request_id]
        while len(_BROWSER_TICKETS) >= _MAX_BROWSER_TICKETS:
            oldest_key = next(iter(_BROWSER_TICKETS))
            del _BROWSER_TICKETS[oldest_key]
        _BROWSER_TICKETS[request_id] = ticket
    return f"{request_id}.{ticket}"


def _consume_browser_ticket(ticket: str) -> bool:
    request_id, sep, value = ticket.partition(".")
    if not sep or not value:
        return False
    with _BROWSER_TICKETS_LOCK:
        expected = _BROWSER_TICKETS.get(request_id)
        if expected is None or not secrets.compare_digest(value.encode(), expected.encode()):
            return False
        del _BROWSER_TICKETS[request_id]
        return True


def _launch_proof(purpose: str, port: int, nonce: str, reply_nonce: str = "") -> str:
    message = f"advisorbot-launch/{purpose}/{port}/{nonce}/{reply_nonce}".encode("ascii")
    return hmac.digest(SESSION_TOKEN.encode(), message, "sha256").hex()


def _ticket_from_running_server(host: str, port: int) -> str | None:
    """Verify a running instance of this checkout without sending its session secret."""
    nonce = secrets.token_hex(32)
    conn = HTTPConnection(host, port, timeout=2)
    try:
        conn.request("POST", "/api/auth/launch", headers={
            "X-Launch-Nonce": nonce, "X-Launch-Proof": _launch_proof("request", port, nonce),
        })
        response = conn.getresponse()
        if response.status != 200:
            return None
        data = json.loads(response.read(1024))
        if not isinstance(data, dict):
            return None
        reply_nonce = data.get("nonce")
        if not isinstance(reply_nonce, str) or not re.fullmatch(r"[0-9a-f]{64}", reply_nonce):
            return None
        proof = _launch_proof("response", port, nonce, reply_nonce)
        if not secrets.compare_digest(str(data.get("proof") or "").encode(), proof.encode()):
            return None
        ticket = _launch_proof("ticket", port, nonce, reply_nonce)
        return f"{nonce}.{ticket}"
    except (HTTPException, OSError, ValueError):
        return None
    finally:
        conn.close()


def _open_authenticated_browser(host: str, port: int, token: str) -> None:
    """Opens the authenticated dashboard URL in the default browser, seeding both `127.0.0.1` and `localhost` cookies when possible."""
    import socket
    import webbrowser

    if not token:
        webbrowser.open(f"http://{host}:{port}/")
        return
    sync_q = ""
    if host == "127.0.0.1":
        try:
            infos = socket.getaddrinfo("localhost", port, socket.AF_INET, socket.SOCK_STREAM)
            if any(info[4][0] == "127.0.0.1" for info in infos):
                sync_q = "&sync_localhost=1"
        except OSError:
            pass
    webbrowser.open(f"http://{host}:{port}/?token={token}{sync_q}")


def main() -> int:
    global IN_SERVER_PROCESS, SESSION_TOKEN
    # SECURITY DO: Lock process umask to owner-only (`0o077`) at startup so tokens, logs, saves, and DBs aren't world-readable,
    # and bind ThreadingHTTPServer *before* calling `write_token_file()` so a failed second launch cannot clobber a live token.
    # SECURITY DON'T: Never bind to `0.0.0.0` or move `write_token_file()` ahead of socket bind.
    os.umask(0o077)

    parser = argparse.ArgumentParser(description="Portfolio dashboard + Canonical Scenarios local HTTP service")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (loopback only; default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Bind port (default {DEFAULT_PORT})")
    parser.add_argument("--open", action="store_true", help="Open authenticated dashboard URL in default browser")
    parser.add_argument(
        "--rotate-token",
        action="store_true",
        help="Mint a new persistent 256-bit secret in Data/.session_secret, invalidating existing browser cookies",
    )
    args = parser.parse_args()

    if args.host not in ALLOWED_HOSTS:
        print(f"[advisorbot_server] refusing to bind non-loopback host {args.host!r}", flush=True)
        return 2

    ThreadingHTTPServer.allow_reuse_address = True
    try:
        server = ThreadingHTTPServer((args.host, args.port), SavesRequestHandler)
    except OSError as exc:
        if exc.errno in (errno.EADDRINUSE, 48, 98, 10048):
            if args.open and not args.rotate_token:
                ticket = _ticket_from_running_server(args.host, args.port)
                if ticket:
                    _open_authenticated_browser(args.host, args.port, ticket)
                    return 0
            print(f"Port {args.port} is in use. Choose another port for this instance.", file=sys.stderr)
            return 2
        raise

    _rotate_server_logs()
    _harden_portfolio_permissions()
    SESSION_TOKEN = _init_session_token(rotate=bool(args.rotate_token))
    if not get_active_portfolio_id():
        recovered = config.recover_last_active_portfolio(allow_single_fallback=True)
        if recovered:
            refresh_active_paths()
    ensure_default_save()
    write_token_file()
    _harden_portfolio_permissions()
    IN_SERVER_PROCESS = True
    llm_tools.IN_SERVER_PROCESS = True
    # Warm cached unmasked payload in background so first scenario save never rebuilds DB/holdings synchronously
    threading.Thread(
        target=lambda: llm_tools.get_cached_payload(bypass_export_masking=True),
        daemon=True,
    ).start()
    tok_file = get_session_token_file()
    startup_ticket = _new_browser_ticket() if (sys.stdout.isatty() or args.open) else ""
    if sys.stdout.isatty():
        print(
            f"[advisorbot_server] Dashboard: http://{args.host}:{args.port}/?token={startup_ticket}   (loopback, session-token gated)",
            flush=True,
        )
    else:
        print(
            f"[advisorbot_server] Dashboard: http://{args.host}:{args.port}/   (loopback, session-token gated)",
            flush=True,
        )
    print(f"[advisorbot_server] Serving {get_portfolios_dir()}; token at {tok_file}", flush=True)
    if args.open:
        _open_authenticated_browser(args.host, args.port, startup_ticket)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        IN_SERVER_PROCESS = False
        llm_tools.IN_SERVER_PROCESS = False
        server.server_close()
        try:
            tok_file.unlink(missing_ok=True)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
