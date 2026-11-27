    const PAYLOAD = __REPORT_PAYLOAD_JSON__;
    const TIMELINE = PAYLOAD.timeline || [];
    const STOCK_GRANTS = PAYLOAD.stock_grants || {
      has_unvested: Boolean((PAYLOAD.google_stock && PAYLOAD.google_stock.by_grant && PAYLOAD.google_stock.by_grant.length) || false),
      default_symbol: PAYLOAD.primary_symbol || "",
      companies: [],
      by_symbol: {}
    };

    function getGrantCompanies() {
      return Array.isArray(STOCK_GRANTS.companies) ? STOCK_GRANTS.companies : [];
    }

    let activeGrantSymbol = (() => {
      const comps = getGrantCompanies();
      try {
        const saved = (localStorage.getItem("active_grant_symbol") || "").toUpperCase();
        if (saved && comps.some(c => c.symbol === saved)) return saved;
      } catch (_e) {}
      if (STOCK_GRANTS.default_symbol) return STOCK_GRANTS.default_symbol;
      return comps.length > 0 ? comps[0].symbol : (PAYLOAD.primary_symbol || "");
    })();

    let GSTOCK = (STOCK_GRANTS.by_symbol && STOCK_GRANTS.by_symbol[activeGrantSymbol])
      || PAYLOAD.google_stock
      || { grant_series: [], release_bars: [], by_year: [], by_grant: [] };
    let REL_BARS = GSTOCK.release_bars || [];
    let GRANT_SERIES = GSTOCK.grant_series || [];
    let GRANT_META_MAP = Object.fromEntries(GRANT_SERIES.map(g => [g.id, g]));

    // PORTABILITY DO: Read `PRIMARY_SYMBOL`, `NET_RATE`, and volatility presets from `PAYLOAD` (`LL-01`, `LL-08`, `LL-09`, `LL-32`).
    // PORTABILITY DON'T: Fall back to a hardcoded ticker, withholding rate literal, or single-stock volatility constant.
    const TODAY_ISO = PAYLOAD.today_iso;
    const NET_RATE = Number(PAYLOAD.net_retention_rate) || 0.50;
    const PRIMARY_SYMBOL = PAYLOAD.primary_symbol || (PAYLOAD.project && PAYLOAD.project.primary_equity_symbol) || "";

    // Minimum distance between mouse pointer and nearest edge of any hover tooltip (>= 52px ≈ 1.38cm at 96 DPI)
    const MIN_POINTER_GAP_PX = 52;
    // SECURITY DO (DP-01 / SA2-01 / SA2-15): Keep `SAVES_API_BASE = ""` so all HTTP calls stay same-origin,
    // and treat `file://` opens (`isOfflineFileSnapshot()`) strictly as offline snapshots with zero network I/O.
    // SECURITY DON'T: Never point `SAVES_API_BASE` at `http://127.0.0.1:8765` from `file://` or re-enable CORS.
    const SAVES_API_BASE = "";

    function isOfflineFileSnapshot() {
      return typeof window !== "undefined" && Boolean(window.location && window.location.protocol === "file:");
    }

    const ICONS = {
      pencil: `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 3a2.828 2.828 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z"></path></svg>`,
      trash: `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path><line x1="10" y1="11" x2="10" y2="17"></line><line x1="14" y1="11" x2="14" y2="17"></line></svg>`,
    };

    // AdvisorBot Service Status Management
    const SERVICE_STATES = {
      CONNECTED: {
        dotClass: "dot-green",
        text: "AdvisorBot Service: Connected",
        showRetry: false,
      },
      RECONNECTING: {
        dotClass: "dot-amber",
        text: "AdvisorBot Service: Reconnecting...",
        showRetry: false,
      },
      RESTART_SYNC: {
        dotClass: "dot-amber",
        text: "AdvisorBot Service: Session updated — Reconnecting...",
        showRetry: false,
      },
      TEMPORARILY_OFFLINE: {
        dotClass: "dot-red",
        text: "AdvisorBot Service: Temporarily offline — Reconnecting automatically...",
        showRetry: true,
      },
      OFFLINE: {
        dotClass: "dot-red",
        text: "AdvisorBot Service: Connection lost — Reconnecting...",
        showRetry: true,
      },
      STALE_SCOPE: {
        dotClass: "dot-amber",
        text: "Active portfolio switched in another tab — press ⌘R / Ctrl+R to reload",
        showRetry: false,
      },
    };

    let _currentServiceState = "CONNECTED";
    let _serviceHeartbeatTimer = null;
    let _reconnectPromise = null;

    function updateServiceStatus(stateKey, customMsg = null) {
      _currentServiceState = stateKey;
      const cfg = SERVICE_STATES[stateKey] || SERVICE_STATES.CONNECTED;
      if (typeof document !== "undefined") {
        const dotEl = document.getElementById("svc-status-dot");
        const txtEl = document.getElementById("svc-status-text");
        const retryBtn = document.getElementById("svc-retry-btn");
        if (dotEl) {
          dotEl.className = `svc-status-dot ${cfg.dotClass}`;
        }
        if (txtEl) {
          txtEl.textContent = customMsg || cfg.text;
          if (txtEl.classList) txtEl.classList.toggle("stale-scope", stateKey === "STALE_SCOPE");
        }
        if (retryBtn) {
          retryBtn.style.display = cfg.showRetry ? "inline-block" : "none";
        }
      }
    }

    async function retryServiceConnection() {
      if (isOfflineFileSnapshot()) {
        updateServiceStatus("TEMPORARILY_OFFLINE", "Offline file:// snapshot");
        return;
      }
      updateServiceStatus("RECONNECTING");
      const reconRes = await silentReconnectSession();
      const success = Boolean(reconRes && (reconRes === true || reconRes.ok));
      if (success) {
        updateServiceStatus("CONNECTED");
        if (typeof window !== "undefined" && typeof window.refreshActiveSiteState === "function") {
          window.refreshActiveSiteState();
        }
      } else {
        updateServiceStatus("TEMPORARILY_OFFLINE");
      }
    }

    async function silentReconnectSession() {
      if (isOfflineFileSnapshot()) {
        return { ok: false, status: 0, data: null, error: "Offline file:// snapshot" };
      }
      if (_reconnectPromise) return _reconnectPromise;
      _reconnectPromise = (async () => {
        try {
          updateServiceStatus("RECONNECTING");
          const res = await fetch(`${SAVES_API_BASE}/api/auth/reconnect`, {
            method: "POST",
            cache: "no-store",
            credentials: "include",
            headers: {
              "Content-Type": "application/json",
              "X-Requested-With": "AdvisorBot",
            }
          });
          const data = await res.json().catch(() => null);
          const ok = Boolean(res.ok && data && data.ok);
          if (ok) {
            updateServiceStatus("CONNECTED");
          } else {
            updateServiceStatus("TEMPORARILY_OFFLINE");
          }
          return ok;
        } catch (_e) {
          updateServiceStatus("TEMPORARILY_OFFLINE");
          return false;
        } finally {
          _reconnectPromise = null;
        }
      })();
      return _reconnectPromise;
    }

    /**
     * Fetch a JSON endpoint on the local saves server.
     * Supports optional `body` (JSON-serialized) and `timeoutMs`.
     * Automatically attempts silent background reconnection and transparent retry.
     */
    async function apiJson(path, opts = {}) {
      if (isOfflineFileSnapshot()) {
        return { ok: false, status: 0, data: null, error: "Offline file:// snapshot" };
      }
      const { body, timeoutMs, headers, onProgress, ...rest } = opts;
      const emitProgress = typeof onProgress === "function" ? onProgress : null;
      const ctrl = timeoutMs ? new AbortController() : null;
      const timer = ctrl ? setTimeout(() => ctrl.abort(), timeoutMs) : null;
      try {
        const fetchOpts = { ...rest };
        if (ctrl) fetchOpts.signal = ctrl.signal;
        const methodUpper = String(fetchOpts.method || (body !== undefined ? "POST" : "GET")).toUpperCase();
        fetchOpts.method = methodUpper;
        const mergedHeaders = {
          "X-Requested-With": "AdvisorBot",
          ...(headers || {}),
        };
        const portfolioScope = document.querySelector('meta[name="advisorbot-portfolio"]')?.content;
        if (portfolioScope) mergedHeaders["X-AdvisorBot-Portfolio"] = portfolioScope;
        if (body !== undefined || methodUpper === "POST" || methodUpper === "DELETE" || methodUpper === "PUT" || methodUpper === "PATCH") {
          if (!mergedHeaders["Content-Type"]) {
            mergedHeaders["Content-Type"] = "application/json";
          }
        }
        if (body !== undefined) {
          fetchOpts.body = typeof body === "string" ? body : JSON.stringify(body);
        }
        fetchOpts.headers = mergedHeaders;
        if (methodUpper === "GET" && !fetchOpts.cache) {
          fetchOpts.cache = "no-store";
        }
        fetchOpts.credentials = fetchOpts.credentials || "include";

        let resp = null;
        try {
          resp = await fetch(`${SAVES_API_BASE}${path}`, fetchOpts);
        } catch (_netErr) {
          if (_netErr && _netErr.name === "AbortError") {
            return { ok: false, status: 408, aborted: true, data: null, error: "Request timed out while waiting for the server." };
          }
          // Momentary network drop or server restarting
          updateServiceStatus("TEMPORARILY_OFFLINE");
          if (emitProgress) await emitProgress("Connection interrupted — reconnecting to server...", "#fbbf24");
          const reconRes = await silentReconnectSession();
          const reconnected = Boolean(reconRes && (reconRes === true || reconRes.ok));
          if (reconnected) {
            if (emitProgress) await emitProgress("Reconnected — retrying write on server...");
            try {
              resp = await fetch(`${SAVES_API_BASE}${path}`, fetchOpts);
            } catch (_retryErr) {
              if (_retryErr && _retryErr.name === "AbortError") {
                return { ok: false, status: 408, aborted: true, data: null, error: "Request timed out while waiting for the server." };
              }
              updateServiceStatus("TEMPORARILY_OFFLINE");
              return { ok: false, status: 0, data: null, error: "Service unavailable" };
            }
          } else {
            return { ok: false, status: 0, data: null, error: "Service unavailable" };
          }
        }

        if (resp.status === 403 && !path.startsWith("/api/auth/")) {
          updateServiceStatus("RESTART_SYNC");
          if (emitProgress) await emitProgress("Session expired — reconnecting to server...", "#fbbf24");
          const reauthRes = await silentReconnectSession();
          const reauthed = Boolean(reauthRes && (reauthRes === true || reauthRes.ok));
          if (reauthed) {
            if (emitProgress) await emitProgress("Session restored — writing scenario file to server...");
            try {
              resp = await fetch(`${SAVES_API_BASE}${path}`, fetchOpts);
            } catch (_e) {
              if (_e && _e.name === "AbortError") {
                return { ok: false, status: 408, aborted: true, data: null, error: "Request timed out while waiting for the server." };
              }
              updateServiceStatus("TEMPORARILY_OFFLINE");
              return { ok: false, status: 0, data: null, error: "Service unavailable" };
            }
          }
        }

        if (resp.status === 409) {
          if (typeof window !== "undefined") window._pageScopeStale = true;
          const isMac = typeof navigator !== "undefined" && /Mac|iPhone|iPod|iPad/i.test(navigator.userAgent || "");
          const rKey = isMac ? "\u2318R" : "Ctrl+R";
          const staleMsg = `Active portfolio switched — press ${rKey} to reload this tab`;
          updateServiceStatus("STALE_SCOPE", staleMsg);
          if (typeof document !== "undefined") {
            const pillEl = document.getElementById("svc-status-pill");
            if (pillEl) {
              if (!document.getElementById("svc-status-text")) pillEl.textContent = staleMsg;
              if (pillEl.style) pillEl.style.color = "#fbbf24";
            }
            const subEl = document.getElementById("save-status-sub");
            if (subEl) {
              subEl.textContent = `\u25CF Portfolio switched \u2022 Press ${rKey} to reload page`;
              if (subEl.style) subEl.style.color = "#fbbf24";
              if (subEl.classList) subEl.classList.add("dirty-text");
            }
          }
        } else if (resp.ok) {
          if (_currentServiceState !== "CONNECTED" && !(typeof window !== "undefined" && window._pageScopeStale)) {
            updateServiceStatus("CONNECTED");
          }
        } else if (resp.status >= 500) {
          updateServiceStatus("TEMPORARILY_OFFLINE");
        }

        const data = await resp.json().catch(() => null);
        return { ok: resp.ok, status: resp.status, data };
      } finally {
        if (timer) clearTimeout(timer);
      }
    }

    function startServiceHealthMonitor() {
      if (typeof window === "undefined") return;
      if (_serviceHeartbeatTimer) clearInterval(_serviceHeartbeatTimer);
      if (isOfflineFileSnapshot()) {
        updateServiceStatus("TEMPORARILY_OFFLINE", "Offline file:// snapshot");
        return;
      }

      const checkHealth = async () => {
        try {
          const resp = await fetch(`${SAVES_API_BASE}/api/health`, {
            cache: "no-store",
            credentials: "include",
            headers: { "X-Requested-With": "AdvisorBot" },
          });
          if (resp.ok) {
            const data = await resp.json().catch(() => null);
            if (data && data.session_active === false) {
              updateServiceStatus("RESTART_SYNC");
              await silentReconnectSession();
            } else if (_currentServiceState !== "CONNECTED" && !(typeof window !== "undefined" && window._pageScopeStale)) {
              updateServiceStatus("CONNECTED");
            }
          } else {
            updateServiceStatus("TEMPORARILY_OFFLINE");
          }
        } catch (_err) {
          if (_currentServiceState === "CONNECTED") {
            updateServiceStatus("TEMPORARILY_OFFLINE");
          }
        }
      };

      setTimeout(checkHealth, 1200);
      _serviceHeartbeatTimer = setInterval(checkHealth, 12000);
    }

    let currentScreen = "models"; // 'models' | 'portfolio' | 'google' | 'retirement' | 'income'

    let TODAY_IDX = TIMELINE.findIndex(w => w.isToday);
    if (TODAY_IDX < 0) {
      TODAY_IDX = TIMELINE.reduce((acc, w, i) => (w.iso <= TODAY_ISO ? i : acc), 0);
    }

    // Last observed index in REL_BARS (<= TODAY_ISO)
    let REL_LAST_OBS_IDX = REL_BARS.reduce((acc, b, i) => (b.iso <= TODAY_ISO ? i : acc), -1);
    if (REL_LAST_OBS_IDX < 0 && REL_BARS.length > 0) REL_LAST_OBS_IDX = 0;

    function activeGrantLivePrice() {
      if (GSTOCK && Number.isFinite(Number(GSTOCK.live_price)) && Number(GSTOCK.live_price) > 0) {
        return Number(GSTOCK.live_price);
      }
      if (PRIMARY_SYMBOL && activeGrantSymbol === PRIMARY_SYMBOL) {
        return Number(PAYLOAD.goog_live_price) || 0;
      }
      return 0;
    }

    function loadActiveGrantCompany(sym) {
      const comps = getGrantCompanies();
      const target = (sym || "").toUpperCase();
      if (target && STOCK_GRANTS.by_symbol && STOCK_GRANTS.by_symbol[target]) {
        activeGrantSymbol = target;
      } else if (comps.length > 0) {
        activeGrantSymbol = comps[0].symbol;
      }
      GSTOCK = (STOCK_GRANTS.by_symbol && STOCK_GRANTS.by_symbol[activeGrantSymbol])
        || PAYLOAD.google_stock
        || { grant_series: [], release_bars: [], by_year: [], by_grant: [] };
      REL_BARS = GSTOCK.release_bars || [];
      GRANT_SERIES = GSTOCK.grant_series || [];
      GRANT_META_MAP = Object.fromEntries(GRANT_SERIES.map(g => [g.id, g]));
      REL_LAST_OBS_IDX = REL_BARS.reduce((acc, b, i) => (b.iso <= TODAY_ISO ? i : acc), -1);
      if (REL_LAST_OBS_IDX < 0 && REL_BARS.length > 0) REL_LAST_OBS_IDX = 0;
      if (typeof updateGrantScreenKPIs === "function") {
        updateGrantScreenKPIs();
      }
    }

    // Portfolio Timeline state
    let activePreset = PAYLOAD.initial_preset || "all";
    let activeYearJump = null;
    let isExactPresetWindow = true;
    let stackMode = "bucket"; // 'bucket' | 'account' | 'asset'
    let taxMode = PAYLOAD.default_tax_mode || "net";
    let futureGoogGrowthPct = 0.0; // legacy alias synced from holdingForecasts[PRIMARY_SYMBOL]
    let futureNonGoogGrowthPct = 0.0; // legacy alias synced from weighted non-primary holdings
    let tableGrouping = "strategy"; // 'strategy' | 'account'
    let holdingsViewMode = "table"; // 'table' | 'pie'
    let currentGoogPrice = Number(PAYLOAD.goog_live_price) || 0;
    let currentGoogQuoteTs = PAYLOAD.generated_at || "";
    const expandedGroups = new Set();

    const MIN_VISIBLE_BARS = 4;
    const RETIREMENT_GOAL_YEARS = Number(PAYLOAD.retirement_goal_years) || 30;
    let RETIREMENT_ANNUAL_BURN = Number(PAYLOAD.reference_annual_burn || PAYLOAD.retirement_annual_burn) || 50000;
    const DYN_INFLATION_REF = (function() {
      const FALLBACK_PCT = 3.1;
      const FALLBACK_RANGE = "2016–2025";
      try {
        const cohorts = (window.PAYLOAD && window.PAYLOAD.cohorts) || null;
        if (cohorts && Array.isArray(cohorts.years)) {
          const valid = cohorts.years
            .filter(y => y && typeof y.year === "number" && typeof y.inflation_pct === "number")
            .sort((a, b) => a.year - b.year);
          // DA-B05: Take exact 10 annual CPI-U observations (`slice(-10)` -> e.g. 2016–2025 = 10 yrs);
          // DO NOT filter by `year >= latest - 10` (which produces an 11-year inclusive span).
          const sample = valid.slice(-10);
          if (sample.length >= 8) {
            let prod = 1.0;
            for (const y of sample) {
              prod *= (1.0 + y.inflation_pct / 100.0);
            }
            const cagr = (Math.pow(prod, 1.0 / sample.length) - 1.0) * 100.0;
            const rounded = Math.round(cagr * 10) / 10;
            const rangeStr = `${sample[0].year}–${sample[sample.length - 1].year}`;
            return { pct: rounded, range: rangeStr };
          }
        }
      } catch (e) {
        console.warn("Failed dynamic 10yr inflation calculation, falling back to payload reference:", e);
      }
      const plRate = window.PAYLOAD && typeof window.PAYLOAD.reference_inflation_pct === "number"
        ? window.PAYLOAD.reference_inflation_pct
        : FALLBACK_PCT;
      const plRange = (window.PAYLOAD && window.PAYLOAD.reference_inflation_range) || FALLBACK_RANGE;
      return { pct: plRate, range: plRange };
    })();

    const RETIREMENT_INFLATION_REF = DYN_INFLATION_REF.pct;
    const RETIREMENT_INFLATION_RANGE = DYN_INFLATION_REF.range;
    const RETIREMENT_GOAL_VAL = Number(PAYLOAD.retirement_goal_val) || (RETIREMENT_ANNUAL_BURN * RETIREMENT_GOAL_YEARS);
    let viewStart = 0;
    let viewEnd = Math.max(0, TIMELINE.length - 1);

    function formatBurnCompact(n) {
      const val = Number(n) || 0;
      const abs = Math.abs(val);
      if (abs < 0.5) return "$0";
      const sign = val < 0 ? "-$" : "$";
      if (abs >= 1e6) {
        const m = abs / 1e6;
        return `${sign}${m % 1 === 0 ? m.toFixed(0) : m.toFixed(1)}M`;
      }
      const k = abs / 1e3;
      return `${sign}${k % 1 === 0 ? k.toFixed(0) : k.toFixed(1)}K`;
    }

    function formatMonthlySpend(annualUsd) {
      const val = Math.max(0, Number(annualUsd) || 0);
      return `$${Math.round(val / 12).toLocaleString("en-US")}/mo`;
    }

    // Stock Releases chart state
    let pastReleaseValMode = "release"; // 'release' | 'today'
    let relActivePreset = "all";
    let relActiveYearJump = null;
    const MIN_REL_VISIBLE_BARS = 2;
    let relViewStart = 0;
    let relViewEnd = Math.max(0, REL_BARS.length - 1);

    // Retirement Simulator chart state (480 future months = 40 years from Today)
    const SIM_TOTAL_MONTHS = 480;
    const MIN_SIM_VISIBLE_BARS = 6;
    const PROJECT_CFG = PAYLOAD.project || {};
    const SSN_MODEL = PAYLOAD.ssn_model || {};
    const SSN_INVESTORS = (Array.isArray(SSN_MODEL.investors) && SSN_MODEL.investors.length > 0)
      ? SSN_MODEL.investors
      : [
          { id: "investor_1", name: "Investor 1", birth_year: 1975, birth_month: 1, birth_day: 1, milestone_age: 85, rmd_owner: true, claim_setting_key: "ssnInvestor1ClaimAge", default_claim_age: 67, pia_monthly_2026: 0 }
        ];
    // PORTABILITY DO (SA2-22): Keep `INV1 = null` and `HAS_SECOND_INVESTOR = false` when `SSN_INVESTORS.length < 2`
    // so single-investor portfolios never clone Investor 0 into a phantom second spouse on charts, KPIs, or snapshots.
    const INV0 = SSN_INVESTORS[0];
    const INV1 = SSN_INVESTORS.length > 1 ? SSN_INVESTORS[1] : null;
    const HAS_SECOND_INVESTOR = Boolean(INV1);
    const _FAM_RMD_OWNER = (Array.isArray(PROJECT_CFG.family_members) ? PROJECT_CFG.family_members : [])
      .find(x => x && (x.is_rmd_owner || x.rmd_owner));
    const RMD_OWNER = _FAM_RMD_OWNER || SSN_INVESTORS.find(x => x.rmd_owner) || INV0;
    // SECURE 2.0 Required Minimum Distributions: age 75 for birth years >= 1960, age 73 for <= 1959.
    const RMD_BIRTH_YEAR = Number(RMD_OWNER.birth_year || String(RMD_OWNER.birth_date || "").slice(0, 4)) || 1975;
    const RMD_AGE = RMD_BIRTH_YEAR >= 1960 ? 75 : 73;
    const ULT_DIVISORS = {
      73:26.5,74:25.5,75:24.6,76:23.7,77:22.9,78:22.0,79:21.1,80:20.2,81:19.4,82:18.5,
      83:17.7,84:16.8,85:16.0,86:15.2,87:14.4,88:13.7,89:12.9,90:12.2,91:11.5,92:10.8,
      93:10.1,94:9.5,95:8.9,96:8.4,97:7.8,98:7.3,99:6.8,100:6.4,101:6.0,102:5.6,103:5.2,
      104:4.9,105:4.6,106:4.3,107:4.1,108:3.9,109:3.7,110:3.5,111:3.4,112:3.3,113:3.1,
      114:3.0,115:2.9,116:2.8,117:2.7,118:2.5,119:2.3,120:2.0,
    };
    const BASE_SIM_ASSETS = PAYLOAD.retirement_sim_assets || [];
    const fixedForecastKeys = new Set(BASE_SIM_ASSETS.filter(a => a.rate_model === "fixed")
      .flatMap(a => [a.id, a.forecast_key]).filter(Boolean));
    let SIM_ASSETS = [...BASE_SIM_ASSETS];
    let holdingForecasts = {};
    let holdingHalfLives = {};
    let holdingFeePct = (function() {
      const m = {};
      (BASE_SIM_ASSETS || []).forEach(a => {
        const fk = a && (a.forecast_key || a.id);
        if (fk && a.fee_pct != null && Number(a.fee_pct) > 0) m[fk] = Number(a.fee_pct);
      });
      (PAYLOAD.holdings_by_strategy || []).forEach(g => {
        const fk = g && (g.forecast_key || g.holding_key || g.group_id);
        if (fk && g.fee_pct != null && Number(g.fee_pct) > 0) m[fk] = Number(g.fee_pct);
        (g.children || []).forEach(ch => {
          const cfk = ch && (ch.forecast_key || ch.holding_key);
          if (cfk && ch.fee_pct != null && Number(ch.fee_pct) > 0) m[cfk] = Number(ch.fee_pct);
        });
      });
      return m;
    })();
    let simMarriedFilingJointly = null;
    let simMinTaxBracketFloor = null;
    let simInvestor1RetirementAge = null;
    let simInvestor2RetirementAge = null;
    let simMemberRetirementAges = {};
    let simDrawdownStartMonth = 1;
    let simHypotheticalAssets = [];
    let simScheduledActions = [];
    let simIncludeScheduledActions = true;
    let mcSingleStockVolPct = 37.5;
    let mcBroadEquityVolPct = 13.9;
    let mcSingleStockCorr = 0.51;

    const RISK_CLASSES_META = {
      cash_fixed: {
        label: "Fixed / Cash",
        vol: 0.0,
        corr: 0.0,
        gfcShock: 0,
        dotcomShock: 0,
        covidShock: 0,
        summary: "Contractual or near-zero-volatility rate (money market sweeps, bank deposits, CDs, fixed MYGAs)."
      },
      fixed_income: {
        label: "Bonds / Fixed Inc",
        vol: 4.5,
        corr: 0.20,
        gfcShock: 5,
        dotcomShock: 24,
        covidShock: 3,
        rates2022Shock: -13,
        summary: "Investment-grade municipal, Treasury, corporate bond ladders, bond ETFs, and principal-protected FIAs."
      },
      hybrid_buffered: {
        label: "Balanced / Buffered",
        vol: 8.5,
        corr: 0.75,
        gfcShock: -25,
        dotcomShock: -20,
        covidShock: -16,
        rates2022Shock: -16,
        summary: "Multi-asset 60/40, target-date funds, buffered ETFs, and downside-buffered RILA annuities."
      },
      broad_equity: {
        label: "Broad Equity",
        vol: 14.5,
        corr: 1.00,
        gfcShock: -50,
        dotcomShock: -45,
        covidShock: -34,
        rates2022Shock: -24,
        summary: "Diversified equity index ETFs, mutual funds, REITs, and multi-stock direct-indexing sleeves."
      },
      real_diversifier: {
        label: "Real Assets / Gold",
        vol: 15.0,
        corr: 0.05,
        gfcShock: 5,
        dotcomShock: 4,
        covidShock: 8,
        rates2022Shock: 1,
        summary: "Gold, precious metals, and broad commodity trusts with near-zero equity beta and crisis ballast."
      },
      single_stock: {
        label: "Single Stock",
        vol: 36.2,
        corr: 0.52,
        gfcShock: -60,
        dotcomShock: -75,
        covidShock: -34,
        rates2022Shock: -38,
        summary: "Individual corporate equities & RSU awards with idiosyncratic company risk and deep crash drawdowns."
      },
      crypto_speculative: {
        label: "Crypto / Speculative",
        vol: 68.0,
        corr: 0.45,
        gfcShock: -72,
        dotcomShock: -80,
        covidShock: -48,
        rates2022Shock: -64,
        summary: "Spot Bitcoin/Ether ETFs, leveraged 2x/3x funds, options, and venture/speculative positions."
      },
    };

    const _PRIM_SYM_UPPER = String(PRIMARY_SYMBOL || "").trim().toUpperCase();
    const GOOG_PRIMARY_KEYS = new Set([
      ...(_PRIM_SYM_UPPER ? [_PRIM_SYM_UPPER] : []),
      ...((BASE_SIM_ASSETS || []).filter(a => a.is_goog || a.is_goog_priced || (_PRIM_SYM_UPPER && a.grant_symbol === _PRIM_SYM_UPPER) || (a.bucket === "goog" && !a.grant_symbol)).map(a => a.id))
    ]);

    function _resolveForecastByKey(key, parentKeys = [], fallbackGrowth = 0, fallbackRisk = "broad_equity") {
      const hf = (holdingForecasts && typeof holdingForecasts === "object") ? holdingForecasts : {};
      const candidates = [key, ...(Array.isArray(parentKeys) ? parentKeys : [])];
      const fbRc = RISK_CLASSES_META[fallbackRisk] ? fallbackRisk : "broad_equity";
      for (const cand of candidates) {
        if (!cand || !(cand in hf)) continue;
        const entry = hf[cand];
        if (entry?.use_reference && cand !== key) continue;
        if (typeof entry === "number" && Number.isFinite(entry)) {
          return {
            growth_pct: Math.max(-20, Math.min(25, entry)),
            risk_class: fbRc,
            inherited: cand !== key,
            inherited_from: cand === key ? null : cand,
            is_override: true,
            risk_overridden: false
          };
        }
        if (entry && typeof entry === "object") {
          const rawGp = entry.use_reference || entry.growth_pct == null ? fallbackGrowth : Number(entry.growth_pct);
          const gp = Number.isFinite(rawGp) ? Math.max(-20, Math.min(25, rawGp)) : Number(fallbackGrowth || 0);
          const rcRaw = String(entry.risk_class || fbRc).trim().toLowerCase();
          const rc = RISK_CLASSES_META[rcRaw] ? rcRaw : fbRc;
          const rawVol = entry.vol_pct != null ? Number(entry.vol_pct) : null;
          const volPct = (rawVol != null && Number.isFinite(rawVol)) ? Math.max(0, Math.min(150, rawVol)) : null;
          const riskOverridden = Boolean(entry.risk_overridden) || (Boolean(entry.risk_class) && rc !== fbRc);
          return {
            growth_pct: gp,
            risk_class: rc,
            vol_pct: volPct,
            inherited: cand !== key,
            inherited_from: cand === key ? null : cand,
            is_override: true,
            risk_overridden: riskOverridden
          };
        }
      }
      return {
        growth_pct: Number(fallbackGrowth) || 0,
        risk_class: fbRc,
        vol_pct: null,
        inherited: false,
        inherited_from: null,
        is_override: false,
        risk_overridden: false
      };
    }

    function _applyConstituentRollup(item, baseResolved, defRc) {
      const constituents = item && Array.isArray(item.constituent_forecasts) ? item.constituent_forecasts : null;
      const hf = (holdingForecasts && typeof holdingForecasts === "object") ? holdingForecasts : {};
      if (!constituents?.length || hf[item.forecast_key || item.id]?.use_reference) return baseResolved;
      const anyChildOverridden = constituents.some(c => c && c.forecast_key && (c.forecast_key in hf));
      if (!anyChildOverridden) return baseResolved;

      const parentHasOverride = Boolean(baseResolved.is_override);
      const parentGp = Number(baseResolved.growth_pct) || 0;
      let wTotal = 0.0, wGpSum = 0.0;
      constituents.forEach(c => {
        const cw = Number(c && c.weight) || 0.0;
        if (cw <= 0) return;
        wTotal += cw;
        const ck = String(c.forecast_key || "");
        let cgp;
        if (ck && (ck in hf)) {
          cgp = _resolveForecastByKey(ck, [], Number(c.default_growth_pct) || 0, c.default_risk_class || defRc).growth_pct;
        } else if (parentHasOverride) {
          cgp = parentGp;
        } else {
          cgp = Number(c.default_growth_pct) || 0;
        }
        wGpSum += cw * cgp;
      });
      if (wTotal > 0) {
        return {
          ...baseResolved,
          constituent_override: true,
          growth_pct: Math.max(-20, Math.min(25, wGpSum / wTotal))
        };
      }
      return baseResolved;
    }

    function defaultHoldingForecasts(googDefault = 0, nonGoogDefault = 0, useRefRates = false) {
      const forecasts = {};
      const gVal = Number(googDefault) || 0;
      const ngVal = Number(nonGoogDefault) || 0;
      const prim = String(PRIMARY_SYMBOL || "").trim().toUpperCase();

      const primRef = useRefRates ? getGoogReferenceRate() : gVal;
      if (prim) {
        const primEntry = {
          growth_pct: Math.round(primRef * 100) / 100,
          risk_class: "single_stock"
        };
        if (useRefRates) {
          const primHold = (BASE_SIM_ASSETS || []).find(a => a && (a.symbol === prim || a.id === prim || a.forecast_key === prim) && Number(a.realized_vol_pct) > 0)
            || (PAYLOAD.holdings_by_strategy || []).find(g => g && (g.symbol === prim || g.forecast_key === prim) && Number(g.realized_vol_pct) > 0);
          if (primHold && Number.isFinite(Number(primHold.realized_vol_pct))) {
            primEntry.vol_pct = Math.round(Number(primHold.realized_vol_pct) * 100) / 100;
          }
        }
        forecasts[prim] = primEntry;
      }

      (BASE_SIM_ASSETS || []).forEach(a => {
        if (!a || !a.id) return;
        const rm = String(a.rate_model || "").toLowerCase();
        const isSingle = a.bucket === "goog" || a.is_goog || (prim && a.symbol === prim) || a.default_risk_class === "single_stock";
        const rc = RISK_CLASSES_META[a.default_risk_class]
          ? a.default_risk_class
          : (RISK_CLASSES_META[a.risk_class]
              ? a.risk_class
              : (rm === "fixed"
                  ? (a.tax_model === "MUNI" || a.asset_class === "MUNI_BOND" ? "fixed_income" : "cash_fixed")
                  : (isSingle ? "single_stock" : "broad_equity")));
        let gp;
        if (useRefRates && a.ref_growth_pct != null) {
          gp = Number(a.ref_growth_pct);
        } else if (rm === "fixed") {
          gp = a.annual_rate_pct != null ? Number(a.annual_rate_pct) : 4.0;
        } else if (rc === "single_stock") {
          gp = gVal;
        } else {
          gp = ngVal;
        }
        const entry = {
          growth_pct: Math.round(gp * 100) / 100,
          risk_class: rc
        };
        if (useRefRates && rc !== "cash_fixed" && a.realized_vol_pct != null && Number.isFinite(Number(a.realized_vol_pct)) && Number(a.realized_vol_pct) > 0) {
          entry.vol_pct = Math.round(Number(a.realized_vol_pct) * 100) / 100;
        }
        const fk = a.forecast_key || a.id;
        forecasts[fk] = entry;
        if (a.id !== fk) forecasts[a.id] = { ...entry };
      });

      if (useRefRates) {
        (PAYLOAD.holdings_by_strategy || []).forEach(g => {
          const fk = g.forecast_key || g.holding_key || g.group_id;
          if (!fk) return;
          const isSingle = (prim && g.symbol === prim) || g.is_goog || g.default_risk_class === "single_stock";
          const rc = RISK_CLASSES_META[g.default_risk_class]
            ? g.default_risk_class
            : (isSingle ? "single_stock" : "broad_equity");
          const gp = g.ref_growth_pct != null ? Number(g.ref_growth_pct) : (isSingle ? primRef : getNonGoogReferenceRate());
          const rVol = (rc !== "cash_fixed" && g.realized_vol_pct != null && Number.isFinite(Number(g.realized_vol_pct)) && Number(g.realized_vol_pct) > 0)
            ? Math.round(Number(g.realized_vol_pct) * 100) / 100
            : null;
          if (!forecasts[fk]) {
            const gEntry = { growth_pct: Math.round(gp * 100) / 100, risk_class: rc };
            if (rVol != null) gEntry.vol_pct = rVol;
            forecasts[fk] = gEntry;
          } else if (rVol != null && forecasts[fk].vol_pct == null) {
            forecasts[fk].vol_pct = rVol;
          }
        });
      }

      return forecasts;
    }

    function resolveHoldingForecast(itemOrKey, parentKeys = [], fallbackGrowth = 0, fallbackRisk = "broad_equity") {
      if (!itemOrKey) {
        return { growth_pct: 0, risk_class: "broad_equity", inherited: false, inherited_from: null, is_override: false, risk_overridden: false };
      }
      if (typeof itemOrKey === "string") {
        return _resolveForecastByKey(itemOrKey, parentKeys, fallbackGrowth, fallbackRisk);
      }
      const item = itemOrKey;
      const key = item.forecast_key || item.holding_key || item.group_id || (item.account_id && item.security_id ? (item.account_id + ":" + item.security_id) : (item.id || ""));
      const pKeys = Array.isArray(item.parent_forecast_keys) ? item.parent_forecast_keys : (item.parent_key ? [item.parent_key] : []);
      const rm = String(item.rate_model || "").toLowerCase();
      const isSingle = item.bucket === "goog" || item.is_goog || (PRIMARY_SYMBOL && item.symbol === PRIMARY_SYMBOL) || item.default_risk_class === "single_stock";
      const defRc = item.default_risk_class || item.risk_class || (rm === "fixed" ? (item.tax_model === "MUNI" || item.asset_class === "MUNI_BOND" ? "fixed_income" : "cash_fixed") : (isSingle ? "single_stock" : "broad_equity"));
      const defGp = item.default_growth_pct != null
        ? Number(item.default_growth_pct)
        : (rm === "fixed" && item.annual_rate_pct != null
            ? Number(item.annual_rate_pct)
            : (isSingle ? futureGoogGrowthPct : futureNonGoogGrowthPct));
      const baseRes = _resolveForecastByKey(key, pKeys, defGp, defRc);
      return _applyConstituentRollup(item, baseRes, defRc);
    }

    function _escJsAttrStr(s) {
      return String(s ?? "").replace(/\\/g, "\\\\").replace(/'/g, "\\'");
    }

    function holdingGrowthEvidence(item) {
      const entries = (item.growth_evidence || []).filter(e =>
        [1, 2, 3].includes(e.tier) && Number.isFinite(e.projected_return_pct));
      if (item.is_hypothetical || item.id === "rmd_cash_sweep" || (!entries.length && item.risk_class)) {
        const rc = item.risk_class || item.default_risk_class || "broad_equity";
        if (item.id === "rmd_cash_sweep") entries.push({tier: 3, projected_return_pct: item.ref_growth_pct,
          source_detail: `Built-in cash sweep interest rate assumed as of ${TODAY_ISO}`});
        const rate = PAYLOAD.risk_classes?.[rc]?.benchmark_return_pct;
        if (rate != null) entries.push({tier: 3, projected_return_pct: rate,
          source_detail: `Long-run historical average for ${(RISK_CLASSES_META[rc]?.label || rc.replace(/_/g, " ")).toLowerCase()} holdings as of ${TODAY_ISO}`});
      }
      return entries.sort((a, b) => a.tier - b.tier);
    }

    function holdingReferenceGrowth(item) {
      return holdingGrowthEvidence(item)[0]?.projected_return_pct ?? item.ref_growth_pct ?? item.default_growth_pct;
    }

    const FLAT_HALF_LIFE_YEARS = 31;

    function formatHalfLifeLabel(halfLife) {
      const n = Math.round(Number(halfLife) || 5);
      if (n >= FLAT_HALF_LIFE_YEARS) return "Flat (no decay)";
      return n === 1 ? "1 year" : `${n} years`;
    }

    function normalizeHoldingHalfLives(raw) {
      return Object.fromEntries(Object.entries(raw || {}).filter(([, n]) => Number.isFinite(n))
        .map(([k, n]) => [k, Math.max(1, Math.min(FLAT_HALF_LIFE_YEARS, Math.round(n)))]));
    }

    function holdingHalfLife(item, parentsOnly = false, ybk = holdingHalfLives) {
      const map = ybk || {};
      const key = item.forecast_key || item.holding_key || item.id;
      const keys = [...(parentsOnly ? [] : [key]), ...(item.parent_forecast_keys || [])];
      for (const k of keys) if (map[k] != null) return map[k];
      return 5;
    }

    const growthTierNames = ["", "Holding or contract rate", "Peer market index", "Long-run asset-class average"];

    function _utcDaysBetween(startIso, endIso) {
      const a = String(startIso || "").slice(0, 10).split("-").map(Number);
      const b = String(endIso || "").slice(0, 10).split("-").map(Number);
      if (a.length !== 3 || b.length !== 3 || a.some(Number.isNaN) || b.some(Number.isNaN)) return 0;
      return Math.round((Date.UTC(b[0], b[1] - 1, b[2]) - Date.UTC(a[0], a[1] - 1, a[2])) / 86400000);
    }

    function contractualLockYears(item, todayIso = TODAY_ISO) {
      const matRaw = String((item && (item.maturity_iso || item.maturity_date)) || "").slice(0, 10);
      const tStr = String(todayIso || "").slice(0, 10);
      if (!matRaw || !tStr || matRaw <= tStr) return 0;
      const rm = String((item && item.rate_model) || "");
      const src = String((item && item.return_source) || "");
      const key = item && (item.forecast_key || item.id);
      const isFixedKey = Boolean(key && fixedForecastKeys.has(key));
      if (rm === "fixed" || isFixedKey || src === "CONTRACT_CREDITED_RATE" || src === "STATEMENT_YTM" || src === "STATEMENT_COUPON_YIELD") {
        const days = _utcDaysBetween(tStr, matRaw);
        if (days > 0) return Math.max(1, Math.ceil(days / 365.2425));
      }
      return 0;
    }

    function hasMaturingConstituents(item, todayIso = TODAY_ISO) {
      const tStr = String(todayIso || "").slice(0, 10);
      if (!tStr || !item || !Array.isArray(item.constituent_forecasts)) return false;
      return item.constituent_forecasts.some(c => contractualLockYears(c, tStr) > 0);
    }

    function _holdingEvidenceByTier(evidence) {
      const byTier = {};
      for (const row of evidence || []) {
        if ([1, 2, 3].includes(row.tier) && Number.isFinite(row.projected_return_pct) && byTier[row.tier] == null) {
          byTier[row.tier] = row;
        }
      }
      return byTier;
    }

    function nextForecastTier(item, evidence = holdingGrowthEvidence(item)) {
      if (!item || item.is_hypothetical || !evidence.length) return null;
      const byTier = _holdingEvidenceByTier(evidence);
      const selTier = evidence[0].tier;
      const lowerTiers = [2, 3].filter(t => t > selTier && byTier[t] != null);
      if (!lowerTiers.length) return null;
      const rc = String(item.default_risk_class || item.risk_class || "");
      const rm = String(item.rate_model || "");
      const src = String(item.return_source || "");
      const isFixedKey = fixedForecastKeys.has(item.forecast_key) || item.parent_forecast_keys?.some(k => fixedForecastKeys.has(k));
      const lockYrs = contractualLockYears(item);
      const hasMatChildren = hasMaturingConstituents(item);
      if (!hasMatChildren && lockYrs === 0 && (rc === "cash_fixed" || rm === "fixed" || isFixedKey || src === "CONTRACT_CREDITED_RATE")) {
        return null;
      }
      return byTier[lowerTiers[lowerTiers.length - 1]] || null;
    }

    function holdingFadeTarget(item) {
      return nextForecastTier(item)?.projected_return_pct ?? null;
    }

    function _singleHoldingCurveAt(item, start, halfLife, year, todayIso = TODAY_ISO) {
      if (halfLife >= FLAT_HALF_LIFE_YEARS) return Number(start);
      const evidence = holdingGrowthEvidence(item);
      if (!evidence.length) return Number(start);
      const byTier = _holdingEvidenceByTier(evidence);
      const selTier = evidence[0].tier;
      const lowerTiers = [2, 3].filter(t => t > selTier && byTier[t] != null);
      if (!lowerTiers.length) return Number(start);
      const h = Math.max(1, Math.min(30, Number(halfLife) || 5));
      const lockYrs = contractualLockYears(item, todayIso);
      if (lockYrs > 0) {
        if (year < lockYrs) return Number(start);
        const u = year - lockYrs;
        if (byTier[2] != null && byTier[3] != null) {
          const t2 = byTier[2].projected_return_pct;
          const t3 = byTier[3].projected_return_pct;
          return t3 + (t2 - t3) * Math.pow(0.5, u / (2 * h));
        }
        return byTier[lowerTiers[lowerTiers.length - 1]].projected_return_pct;
      }
      if (selTier === 1 && byTier[2] != null && byTier[3] != null) {
        const t2 = byTier[2].projected_return_pct;
        const t3 = byTier[3].projected_return_pct;
        return t3 + (t2 - t3) * Math.pow(0.5, year / (2 * h)) + (Number(start) - t2) * Math.pow(0.5, year / h);
      }
      const dest = byTier[lowerTiers[0]].projected_return_pct;
      return dest + (Number(start) - dest) * Math.pow(0.5, year / h);
    }
    const singleHoldingCurveAt = _singleHoldingCurveAt;

    function _lookupConstituentHalfLife(c, ybk) {
      const map = ybk || {};
      const ck = String((c && c.forecast_key) || "");
      const keys = [ck, ...(Array.isArray(c && c.parent_forecast_keys) ? c.parent_forecast_keys : [])].filter(Boolean);
      for (const k of keys) {
        if (map[k] != null) return map[k];
      }
      return null;
    }

    function holdingReturnCurve(item, start, halfLife = null, ybk = holdingHalfLives) {
      const effHalfLife = halfLife != null ? halfLife : holdingHalfLife(item, false, ybk);
      const target = holdingFadeTarget(item);
      // A Flat (no decay) setting or single-tier holding remains constant at every horizon.
      if (target == null || effHalfLife >= FLAT_HALF_LIFE_YEARS) return () => Number(start);

      const parentKey = item.forecast_key || item.holding_key || item.id || "";
      if (hasMaturingConstituents(item)) {
        const specs = [];
        let wSum = 0;
        let baseWStart = 0;
        for (const c of item.constituent_forecasts || []) {
          const cw = Number(c.weight) || 0;
          if (cw <= 0) continue;
          const child = resolveHoldingForecast(c.forecast_key, parentKey ? [parentKey] : [], c.default_growth_pct, c.default_risk_class || "fixed_income");
          const cStart = Number(child.growth_pct) || 0;
          const childYears = _lookupConstituentHalfLife(c, ybk);
          const childH = childYears != null ? childYears : effHalfLife;
          wSum += cw;
          baseWStart += cw * cStart;
          specs.push({cw, c, cStart, childH});
        }
        if (wSum > 0 && specs.length) {
          const shift = Number(start) - (baseWStart / wSum);
          return elapsed => {
            let tot = 0;
            for (const sp of specs) {
              tot += sp.cw * _singleHoldingCurveAt(sp.c, sp.cStart + shift, sp.childH, elapsed);
            }
            return tot / wSum;
          };
        }
      }

      const childDeltas = [];
      for (const c of item.constituent_forecasts || []) {
        const years = _lookupConstituentHalfLife(c, ybk);
        if (years == null || years === effHalfLife) continue;
        if (holdingFadeTarget(c) == null) continue;
        const cw = Number(c.weight) || 0;
        if (cw <= 0) continue;
        const child = resolveHoldingForecast(c.forecast_key, parentKey ? [parentKey] : [], c.default_growth_pct, c.default_risk_class);
        childDeltas.push({cw, c, cStart: Number(child.growth_pct) || 0, childH: years});
      }
      return elapsed => {
        let rate = _singleHoldingCurveAt(item, start, effHalfLife, elapsed);
        for (const d of childDeltas) {
          rate += d.cw * (
            _singleHoldingCurveAt(d.c, d.cStart, d.childH, elapsed)
            - _singleHoldingCurveAt(d.c, d.cStart, effHalfLife, elapsed)
          );
        }
        return rate;
      };
    }

    function describeHoldingReturnPath(item, start, halfLife, ybk = holdingHalfLives) {
      const evidence = holdingGrowthEvidence(item);
      const byTier = _holdingEvidenceByTier(evidence);
      const selected = evidence[0];
      const destination = nextForecastTier(item, evidence);
      const tierLabel = e => `Tier ${e.tier} (${growthTierNames[e.tier]}, ${e.projected_return_pct.toFixed(2)}%)`;
      const reference = selected ? `Hist. Ref uses ${tierLabel(selected)}. ` : "";
      const yearOne = `Year 1 starts at ${Number(start).toFixed(2)}%. `;
      if (!destination) {
        let reason = "No broader market tier is available for gradual mean reversion.";
        if (selected && evidence.some(e => e.tier > selected.tier)) reason = "This holding stays at its fixed or account-specific rate.";
        return {eligible: false, text: reference + yearOne + reason};
      }
      const custom = start !== selected.projected_return_pct ? yearOne : "";
      if (halfLife >= FLAT_HALF_LIFE_YEARS) {
        return {
          eligible: true,
          text: reference + custom + `Flat mode holds ${Number(start).toFixed(2)}% steady across Years 1–40 without drifting toward broader market averages (shown before any simulation multiplier or fees).`,
        };
      }
      const annual = holdingReturnCurve(item, start, halfLife, ybk);
      const h = Math.max(1, Math.min(30, Math.round(Number(halfLife) || 5)));
      const lockYrs = contractualLockYears(item);
      const matStr = String((item && (item.maturity_iso || item.maturity_date)) || "").slice(0, 10);
      let transitionDesc;
      if (lockYrs > 0) {
        const rollSummary = (byTier[2] && byTier[3])
          ? `reinvests at ${tierLabel(byTier[2])} and gradually trends toward ${tierLabel(byTier[3])} (closing half that gap every ${2 * h} years)`
          : `reinvests at ${tierLabel(destination)}`;
        transitionDesc = `Earns its fixed ${Number(start).toFixed(2)}% rate through ${matStr} (${lockYrs === 1 ? "Year 1" : `Years 1–${lockYrs}`}), then ${rollSummary}. `;
      } else if (hasMaturingConstituents(item) && byTier[2] && byTier[3]) {
        transitionDesc = `Each bond earns its fixed yield until maturity, then reinvests at ${tierLabel(byTier[2])} and gradually trends toward ${tierLabel(byTier[3])} (closing half that gap every ${2 * h} years). `;
      } else if (selected.tier === 1 && byTier[2] && byTier[3]) {
        transitionDesc = `The gap vs. ${tierLabel(byTier[2])} narrows by half every ${h === 1 ? "year" : h + " years"}, while the peer index gradually trends toward ${tierLabel(byTier[3])} (closing half that gap every ${2 * h} years). `;
      } else {
        transitionDesc = `Gradually trends toward ${tierLabel(destination)}, closing half the gap every ${h === 1 ? "year" : h + " years"}. `;
      }
      return {
        eligible: true,
        text: reference + custom + transitionDesc +
          `Year ${1 + h} · ${annual(h).toFixed(2)}%. Year 40 · ${annual(39).toFixed(2)}%. ` +
          "Shown before any simulation multiplier or fees.",
      };
    }

    function growthTierTableHtml(item) {
      const pct = n => Number(n).toFixed(2) + "%";
      const evidence = holdingGrowthEvidence(item);
      const selected = evidence[0];
      const reference = selected?.projected_return_pct ?? item.ref_growth_pct ?? item.default_growth_pct;
      const rows = growthTierNames.slice(1).map((method, index) => {
        const tier = index + 1;
        const [primary, ...others] = evidence.filter(entry => entry.tier === tier);
        const used = primary && primary === selected;
        const details = primary ? escHtml(primary.source_detail) : "Unavailable";
        const alternatives = others.map(entry =>
          `<div class="growth-alternative">Also available · <b>${pct(entry.projected_return_pct)}</b><br>${escHtml(entry.source_detail)}</div>`
        ).join("");
        return `<tr${used ? ' class="growth-tier-used"' : ""}><th>${tier}</th>` +
          `<td><b>${method}</b><div>${details}</div>${alternatives}</td>` +
          `<td class="growth-rate">${primary ? pct(primary.projected_return_pct) : "—"}` +
          `${used ? "<small>Used</small>" : ""}</td></tr>`;
      }).join("");
      return `<table class="growth-tiers"><caption>Recommended baseline${reference != null ? ` · Ref ${pct(reference)}` : ""}${item.confidence ? " · " + escHtml(item.confidence.toLowerCase()) + " source confidence" : ""}</caption>` +
        `<thead><tr><th>Tier</th><th>Methodology</th><th class="growth-rate num">Annual rate</th></tr></thead><tbody>${rows}</tbody></table>`;
    }

    function projectedGrowthTooltip(item, forecast, label = "Holding projected growth", ybk = holdingHalfLives) {
      const pct = n => Number(n).toFixed(2) + "%";
      const base = forecast.base_growth_pct ?? forecast.growth_pct;
      const lines = [`<strong>${escHtml(label)} · ${pct(base)} / year</strong>`];
      const name = item.friendly_name || item.title || item.label || item.symbol;
      if (name) lines.unshift(`<strong>${escHtml(name)}</strong>`);
      const effHalfLife = forecast.halfLife != null ? forecast.halfLife : holdingHalfLife(item, false, ybk);
      lines.push(escHtml(describeHoldingReturnPath(item, Number(base), effHalfLife, ybk).text));
      const evidence = holdingGrowthEvidence(item);
      const selected = evidence[0];
      const reference = selected?.projected_return_pct ?? item.ref_growth_pct ?? item.default_growth_pct;
      if (forecast.constituent_override) lines.push("The group rate includes value-weighted scenario settings for individual holdings.");
      if (forecast.inherited_from) lines.push(`Inherited scenario setting from ${escHtml(forecast.inherited_from)}.`);
      else if (forecast.is_override) lines.push(`Saved scenario setting: ${pct(base)}.`);
      if (forecast.growth_multiplier != null && forecast.growth_multiplier !== 1) {
        lines.push(`Simulation only · ${pct(base)} × ${Number(forecast.growth_multiplier).toFixed(2)} = ${pct(forecast.growth_pct)} / year.`);
      }
      if (forecast.expense_ratio_pct > 0) lines.push(`The simulator deducts fees of ${pct(forecast.expense_ratio_pct)} separately.`);
      if (item.is_hypothetical) lines.push("Hypothetical holding. Its editable scenario return has no observed market or statement history.");
      if (item.shared_reference) lines.push("Security-wide reference. Any statement comparison comes from a holding of the same security.");
      if (!selected) {
        if (reference != null) lines.push(`Reference assumption: ${pct(reference)}.`);
        lines.push(escHtml(item.source_detail || (item.id === "rmd_cash_sweep"
          ? "Built-in interest assumption for the simulated cash sweep."
          : "Detailed source history is unavailable for this holding.")));
      }
      lines.push(growthTierTableHtml(item));
      if (item.is_sub_segment) lines.push("This segment uses its parent contract’s baseline.");
      return lines.map(line => `<div>${line}</div>`).join("");
    }

    function renderForecastCapsuleHtml(item, editKey, isHypothetical = false) {
      const isSim = isHypothetical || (item && (item.is_hypothetical || item.bucket != null || item.rate_model != null || item.cost_basis != null));
      const f = (isSim && typeof resolveSimAssetForecast === "function")
        ? resolveSimAssetForecast(item)
        : resolveHoldingForecast(item);
      const gp = Number(f.base_growth_pct ?? f.growth_pct) || 0;
      const sign = gp > 0 ? "+" : "";
      const tip = projectedGrowthTooltip(item, f) + "<div>Click to edit this scenario forecast.</div>";
      const keyToUse = editKey || (item && (item.holding_key || item.forecast_key || item.group_id || item.id)) || "";
      const fillPct = Math.max(0, Math.min(100, (Math.max(0, gp) / 25.0) * 100));
      const isHyp = isHypothetical || String(keyToUse).startsWith("hyp_");
      return `<span class="mdl-in-capsule has-tip" data-tip="${escHtml(tip)}" data-sa-action="${isHyp ? "open-hyp" : "open-friendly"}" data-sa-key="${escHtml(keyToUse)}" data-sa-stop="1">` +
        `<span class="mdl-in-capsule-fill" style="width:${fillPct.toFixed(1)}%;"></span>` +
        `<span class="mdl-in-capsule-txt">${sign}${gp.toFixed(2)}%</span></span>`;
    }

    function computeWeightedPortfolioForecastPct() {
      const groups = PAYLOAD.holdings_by_strategy || PAYLOAD.holdings_by_account || [];
      let totalVal = 0;
      let weightedSum = 0;
      groups.forEach(g => {
        const items = (Array.isArray(g.children) && g.children.length > 0) ? g.children : [g];
        items.forEach(c => {
          const val = Number(c.current_value) || 0;
          if (val > 0) {
            const f = resolveHoldingForecast(c);
            totalVal += val;
            weightedSum += val * (Number(f.growth_pct) || 0);
          }
        });
      });
      return totalVal > 0 ? (weightedSum / totalVal) : 0;
    }

    function updatePortfolioForecastPresetUI() {
      const wGrowth = computeWeightedPortfolioForecastPct();
      const wgSign = wGrowth > 0.005 ? "+" : "";
      const ptWp = document.getElementById("pt-weighted-growth-pill");
      if (ptWp) {
        ptWp.textContent = `${wgSign}${wGrowth.toFixed(1)}% / yr wt.`;
      }
      const ptZeroBtn = document.getElementById("pt-all-zero-btn");
      const tblZeroBtn = document.getElementById("tbl-all-zero-btn");
      const simZeroBtn = document.getElementById("sim-all-zero-btn");
      const hasAnyOverride = Boolean(holdingForecasts && Object.keys(holdingForecasts).length > 0);
      const isFlatZero = hasAnyOverride &&
        Math.abs(futureGoogGrowthPct) < 0.05 &&
        Math.abs(futureNonGoogGrowthPct) < 0.05;
      [ptZeroBtn, tblZeroBtn, simZeroBtn].forEach(b => { if (b) b.classList.toggle("active", isFlatZero); });

      const isReset = (!holdingForecasts || Object.keys(holdingForecasts).length === 0);
      const ptResetBtn = document.getElementById("pt-all-reset-btn");
      const tblResetBtn = document.getElementById("tbl-all-reset-btn");
      const simResetBtn = document.getElementById("sim-all-reset-btn");
      [ptResetBtn, tblResetBtn, simResetBtn].forEach(b => { if (b) b.classList.toggle("active", isReset && !isFlatZero); });
    }

    function syncLegacyGrowthAliasesFromForecasts() {
      if (!holdingForecasts || typeof holdingForecasts !== "object") return;
      const primSimAsset = (BASE_SIM_ASSETS || []).find(a =>
        a.bucket === "goog" && (a.is_goog || a.grant_symbol === _PRIM_SYM_UPPER || !a.grant_symbol)
      );
      const primDefGp = (primSimAsset && primSimAsset.default_growth_pct != null)
        ? Number(primSimAsset.default_growth_pct)
        : futureGoogGrowthPct;
      const primRate = primSimAsset
        ? resolveSimAssetForecast(primSimAsset).base_growth_pct
        : (PRIMARY_SYMBOL ? resolveHoldingForecast(PRIMARY_SYMBOL, [], primDefGp, "single_stock").growth_pct : primDefGp);
      futureGoogGrowthPct = Math.round(primRate * 10) / 10;
      let wSum = 0, vSum = 0;
      (BASE_SIM_ASSETS || []).forEach(a => {
        if (a.bucket === "goog" || a.rate_model === "fixed" || a.is_unvested_goog) return;
        const v = Math.max(0, Number(a.current_value != null ? a.current_value : a.initial_value) || 0);
        if (v > 0) {
          wSum += v * resolveSimAssetForecast(a).base_growth_pct;
          vSum += v;
        }
      });
      if (vSum > 0) {
        futureNonGoogGrowthPct = Math.round((wSum / vSum) * 10) / 10;
      }
    }

    function applyFlatZeroForecasts() {
      // DA-B09: Set market-variable holdings to 0.0% while preserving contractual fixed rates (MYGAs, CDs, fixed annuities).
      holdingForecasts = defaultHoldingForecasts(0, 0, false);
      futureGoogGrowthPct = 0.0;
      futureNonGoogGrowthPct = 0.0;
      syncSharedGrowthControls();
      SIM_ASSETS = materializeSimAssets();
      updateFutureKPI();
      recomputeSimBars();
      renderChartAndNav();
      renderHoldingsTable();
      renderHoldingsPie();
      updateSimSummaryDOM();
      renderSimChartAndNav();
      checkSaveDirtyState();
    }

    function applyHistoricalRefForecasts() {
      holdingHalfLives = {};
      holdingForecasts = defaultHoldingForecasts(getGoogReferenceRate(), getNonGoogReferenceRate(), true);
      syncLegacyGrowthAliasesFromForecasts();
      syncSharedGrowthControls();
      SIM_ASSETS = materializeSimAssets();
      updateFutureKPI();
      recomputeSimBars();
      renderChartAndNav();
      renderHoldingsTable();
      renderHoldingsPie();
      updateSimSummaryDOM();
      renderSimChartAndNav();
      checkSaveDirtyState();
    }

    function resetAllHoldingForecastsToIngested() {
      holdingHalfLives = {};
      holdingForecasts = {};
      simOptimismMultiplier = 1.0;
      const optSl = document.getElementById("sim-optimism-slider");
      if (optSl) optSl.value = "1";
      const optPill = document.getElementById("sim-optimism-pill");
      if (optPill) optPill.textContent = "1.00×";
      if (typeof updateSimOptimismDOM === "function") {
        updateSimOptimismDOM();
      }
      simHypotheticalAssets = simHypotheticalAssets.map(h => ({
        ...h, growth_pct: holdingReferenceGrowth({...h, is_hypothetical: true}) ?? h.growth_pct
      }));
      futureGoogGrowthPct = getGoogReferenceRate();
      futureNonGoogGrowthPct = getNonGoogReferenceRate();
      syncLegacyGrowthAliasesFromForecasts();
      syncSharedGrowthControls();
      SIM_ASSETS = materializeSimAssets();
      updateFutureKPI();
      recomputeSimBars();
      renderChartAndNav();
      renderHoldingsTable();
      renderHoldingsPie();
      updateSimSummaryDOM();
      renderSimChartAndNav();
      if (typeof renderSimLiquidationTable === "function") {
        renderSimLiquidationTable();
      }
      checkSaveDirtyState();
      if (typeof updatePortfolioForecastPresetUI === "function") {
        updatePortfolioForecastPresetUI();
      }
    }
    window.resetAllHoldingForecastsToIngested = resetAllHoldingForecastsToIngested;
    window.applyFlatZeroForecasts = applyFlatZeroForecasts;
    window.applyHistoricalRefForecasts = applyHistoricalRefForecasts;

    let simAnnualBurn = RETIREMENT_ANNUAL_BURN; // annual burn slider value
    let simInflationPct = RETIREMENT_INFLATION_REF; // trailing 10-yr historical CPI avg, default Ref
    let simIncludeUnvested = true;
    let simIncludeSSN = true; // Feed after-tax Social Security into Retirement Simulator
    let simDollarMode = "nominal"; // 'nominal' | 'real' (Today's purchasing power)
    let simScaleMode = "auto"; // 'auto' (adaptive log when growth spans tens of millions/billions) | 'log' | 'linear'
    let simOrderPreset = "tax_optimal"; // 'tax_optimal' | 'auto_optimal' | 'heir_legacy' | 'goog_first' | 'custom'
    let simBearShield = true; // Dynamic bear-market routing through Cash -> Munis -> Fixed during downturns
    let simAssetOrder = SIM_ASSETS.map(a => a.id);
    let simAssetStats = {};
    let simCumGrossSold = 0;
    let simCumNetProceeds = 0;
    let simCumTaxPaid = 0;
    let simCumLossHarvested = 0;
    let simCumLossUsed = 0;
    let simCumTaxSaved = 0;
    let simCumSsnGross = 0;
    let simCumSsnNet = 0;
    let simSolvencyDepleted = null;
    let _draggedSimAssetId = null;
    let simActivePreset = "40y";
    let SIM_BARS = [];
    let simViewStart = 0;
    let simViewEnd = SIM_TOTAL_MONTHS;
    let simScheduledWithdrawals = []; // [{ ym: "YYYY-MM", amount: number, dollar_mode: "today"|"nominal", comment: string }]
    let simIncludeScheduledWithdrawals = true; // Whether scheduled withdrawals are included in the simulator graph & cashflows
    // CodeReview A3: Projection-model tabs (Single path | Stress | Range).
    // Common controls (growth sliders, burn, window, SSN, withdrawals, order) are shared across all
    // tabs and stored ONCE in the save; only these model-specific inputs are per-tab extras.
    let simActiveModel = "single_path"; // 'single_path' | 'stress' | 'range'
    let simOptimismMultiplier = 1.0;    // Optimism Slider [-2.0..2.0] (Ref: 1.0), multiplier for all proj growth settings
    const STRESS_NO_BOUNCE_MONTHS = 75;
    function formatStressMonths(m, isBounce = false) {
      const n = Math.max(0, Math.round(Number(m) || 0));
      if (isBounce && n >= STRESS_NO_BOUNCE_MONTHS) return "No Bounce";
      if (n === 0) return "0 mo";
      if (n < 12) return `${n} mo`;
      if (n % 6 === 0) {
        const yrs = n / 12;
        return `${yrs % 1 === 0 ? yrs.toFixed(0) : yrs.toFixed(1)} yr`;
      }
      return `${n} mo`;
    }
    let simStressScenario = "none";     // Market Correction: 'none' | 'gfc2008' | 'dotcom2000' | 'covid2020' | 'rates2022' | 'custom'
    let simCustomShockPct = -30;        // Market Correction: custom peak-to-trough single-stock shock (%)
    let simCustomDownMonths = 12;       // Market Correction: custom downfall duration (months, 3..36)
    let simCustomBroadBounceMonths = 24;// Market Correction: custom broad-equity bounce duration (3..75; 75 = No Bounce)
    let simCustomStockBounceMonths = 36;// Market Correction: custom single-stock bounce duration (3..75; 75 = No Bounce)
    let mcGoogVolPct = 37.5;            // Range: single-stock annualized volatility % (Ref from weekly history)
    let mcNonGoogVolPct = 13.9;         // Range: broad-equity annualized volatility % (Ref from weekly history)
    let mcReturnModel = "normal";       // 'normal' (correlated Gaussian) | 'historical' (52-week block bootstrap)
    let mcGoogRestCorr = 0.51;          // single-stock<->rest correlation (Ref 0.51 from weekly history)
    let mcSimulations = 500;            // fixed number of Monte Carlo paths
    let mcSuccessThresholdUsd = 0;      // Probability tab: a path "succeeds" if ending real >= this ($, Today's dollars)
    // Historical (cohort / retire-in-year) tab controls:
    let cohortCenterMode = "anchor";    // 'anchor' (re-center returns to Expected sliders) | 'raw'
    let cohortInflationMode = "real_cpi"; // 'real_cpi' (each cohort's actual CPI) | 'flat' (inflation slider)
    let cohortStartFrom = -1;           // earliest start year to replay; -1 = earliest the data allows
    const cohortSpotlight = []; // spotlight start years (empty; best & worst are auto-highlighted)
    let simWithdrawalsDollarMode = "today"; // 'today' | 'nominal' (view/input currency for Scheduled Withdrawals section)
    let selectedSimWithdrawalIdx = -1;
    let simSwDialogMode = "create"; // 'create' | 'edit' | 'delete'
    let simSwDialogDollarMode = "today";

    // Income & Expense (Social Security OASDI) chart & feeder state
    // CodeReview A3: aligned weekly [single_stock, benchmark] return pairs for the Historical Monte Carlo
    // bootstrap. Empty => the Historical model gracefully falls back to Normal.
    window.MC_HIST_PAIRS = (PAYLOAD.montecarlo && PAYLOAD.montecarlo.hist_pairs) || [];
    // CodeReview A3 (Historical Cohort tab): real long-history annual series (S&P + single-stock proxy + CPI).
    window.COHORT_DATA = PAYLOAD.cohorts || { years: [], first_year: null, last_year: null };
    // Data-driven Monte Carlo slider Ref values (realized from cached weekly price history).
    const _mcRef = PAYLOAD.montecarlo || {};
    const MC_REF_GOOG_VOL = Number(_mcRef.ref_single_stock_vol_pct ?? _mcRef.ref_goog_vol_pct) || 35.0;      // single-stock annualized vol
    const MC_REF_NONGOOG_VOL = Number(_mcRef.ref_broad_equity_vol_pct ?? _mcRef.ref_nongoog_vol_pct) || 13.9; // SPY annualized vol, full history
    const MC_REF_CORR = (_mcRef.ref_corr != null ? Number(_mcRef.ref_corr) : 0.52); // aligned correlation
    // Seed the factory defaults from the live Refs (a loaded save overrides these per-field).
    mcGoogVolPct = MC_REF_GOOG_VOL;
    mcNonGoogVolPct = MC_REF_NONGOOG_VOL;
    mcGoogRestCorr = MC_REF_CORR;
    mcSingleStockVolPct = MC_REF_GOOG_VOL;
    mcBroadEquityVolPct = MC_REF_NONGOOG_VOL;
    mcSingleStockCorr = MC_REF_CORR;
    holdingForecasts = {};
    syncLegacyGrowthAliasesFromForecasts();
    const SSN_INV0_PIA_2026 = Number(INV0.pia_monthly_2026) || 0;
    const SSN_INV1_PIA_2026 = HAS_SECOND_INVESTOR ? (Number(INV1.pia_monthly_2026) || 0) : 0;
    const SSN_EFF_TAX_RATE = Number(SSN_MODEL.effective_ssn_tax_rate ?? 0.3145);
    const SSN_NET_FACTOR = 1.0 - SSN_EFF_TAX_RATE;
    let ssnInv0ClaimAge = Number(INV0.default_claim_age) || 67;
    let ssnInv1ClaimAge = HAS_SECOND_INVESTOR ? (Number(INV1.default_claim_age) || 67) : 67;
    const SSN_COLA_REF = Number(SSN_MODEL.default_cola_pct) || 2.8;
    const SSN_COLA_10Y_AVG = Number(SSN_MODEL.cola_10y_avg_pct) || 2.8;
    const SSN_COLA_SPAN = String(SSN_MODEL.cola_span_label || "2016–2026");
    let ssnColaPct = SSN_COLA_REF;
    let ssnDollarMode = "nominal"; // 'nominal' | 'real' (Today's 2026 purchasing power)
    let ssnActivePreset = "40y";
    let SSN_BARS = [];
    let ssnViewStart = 0;
    let ssnViewEnd = SIM_TOTAL_MONTHS;

    const fmtUSD0 = v => (v < 0 ? "-$" : "$") + Math.round(Math.abs(v)).toLocaleString("en-US");
    const fmtUSD = fmtUSD0;
    const fmtUSD2 = v => (v < 0 ? "-$" : "$") + Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    const fmtSignedUSD0 = v => (v >= 0 ? "+" : "-") + "$" + Math.round(Math.abs(v)).toLocaleString("en-US");
    const fmtSignedUSD2 = v => (v >= 0 ? "+" : "-") + "$" + Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    const fmtSimCompact = v => {
      const n = Number(v) || 0;
      const abs = Math.abs(n);
      const sign = n < 0 ? "-$" : "$";
      if (abs >= 1e9) return sign + (abs / 1e9).toFixed(1) + "B";
      if (abs >= 1e6) return sign + (abs / 1e6).toFixed(1) + "M";
      if (abs >= 1e3) return sign + (abs / 1e3).toFixed(0) + "K";
      return sign + Math.round(abs).toLocaleString("en-US");
    };

    const INVESTMENT_TYPE_LABELS = {
      "RILA_ANNUITY": "Registered Index-Linked Annuity (RILA)",
      "INDEXED_ANNUITY": "Fixed Indexed Annuity (FIA)",
      "FIXED_ANNUITY": "Fixed Annuity (MYGA)",
      "MYGA": "Fixed Annuity (MYGA)",
      "EQUITY": "Public Equity / Common Stock",
      "MUTUAL_FUND": "Mutual Fund",
      "ETF": "Exchange-Traded Fund (ETF)",
      "MUNI_BOND": "Tax-Exempt Municipal Bond",
      "BOND": "Fixed Income / Bond",
      "TREASURY": "US Treasury Security",
      "TIPS": "Treasury Inflation-Protected Security (TIPS)",
      "CASH": "Cash & Deposit Sweep",
      "MONEY_MARKET": "Money Market Fund",
      "CD": "Certificate of Deposit (CD)",
      "RSU": "Restricted Stock Unit (RSU)",
      "BUFFER_ETF": "Buffered / Defined-Outcome ETF",
      "BALANCED_FUND": "Balanced / Allocation Fund",
      "TARGET_DATE_FUND": "Target Date Fund",
      "HIGH_YIELD": "High-Yield Bond / Credit",
      "COMMODITY": "Commodity",
      "GOLD": "Precious Metals / Gold",
      "PRECIOUS_METAL": "Precious Metals",
      "REIT": "Real Estate Investment Trust (REIT)",
      "CIT": "Collective Investment Trust (CIT)",
      "CRYPTO": "Cryptocurrency / Digital Asset",
      "LEVERAGED_ETF": "Leveraged ETF",
      "OPTION": "Option Contract",
    };

    function formatInvestmentType(val) {
      if (!val) return "—";
      const s = String(val).trim();
      if (INVESTMENT_TYPE_LABELS[s]) return INVESTMENT_TYPE_LABELS[s];
      const upper = s.toUpperCase();
      if (INVESTMENT_TYPE_LABELS[upper]) return INVESTMENT_TYPE_LABELS[upper];
      if (s.includes(" ") || /[a-z]/.test(s)) return s;
      return s.split("_").map(w => w.charAt(0).toUpperCase() + w.slice(1).toLowerCase()).join(" ");
    }
    window.formatInvestmentType = formatInvestmentType;

    // SECURITY DO: Escape every user/model/config string via `escHtml()` before innerHTML interpolation (`SA-13`).
    // SECURITY DON'T: Never build raw template-literal HTML strings with unescaped portfolio names, save titles, or paths.
    function escHtml(s) {
      return String(s ?? "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;")
        .replace(/`/g, "&#96;")
        .replace(/\\/g, "&#92;");
    }

    // SECURITY DO: Wire dynamic table/dialog buttons through `data-sa-*` attributes handled here (`SA-13`).
    // SECURITY DON'T: Never interpolate variables into inline `onclick="..."` HTML attribute handlers.
    (function bindDelegatedActions() {
      if (typeof document === "undefined" || window.__saDelegatedBound) return;
      window.__saDelegatedBound = true;
      function _runSaAction(el, evt, action) {
        if (!el || !action) return;
        if (el.getAttribute("data-sa-stop") === "1" && evt && typeof evt.stopPropagation === "function") {
          evt.stopPropagation();
        }
        const key = el.getAttribute("data-sa-key") || "";
        const id = el.getAttribute("data-sa-id") || "";
        const name = el.getAttribute("data-sa-name") || "";
        switch (action) {
          case "open-hyp":
            if (typeof openHypotheticalAssetDialog === "function") openHypotheticalAssetDialog(key || id, false);
            break;
          case "open-friendly":
            if (typeof openFriendlyNameDialog === "function") openFriendlyNameDialog(key || id, evt);
            break;
          case "jump-rel-year":
            if (typeof jumpRelToYear === "function") jumpRelToYear(el.getAttribute("data-sa-year") || "");
            break;
          case "toggle-group":
            if (typeof toggleGroup === "function") toggleGroup(id);
            break;
          case "select-tracked-symbol":
            if (typeof selectTrackedSymbol === "function") selectTrackedSymbol(el.getAttribute("data-sa-sym") || "");
            break;
          case "select-grant-company":
            if (typeof selectGrantCompany === "function") selectGrantCompany(el.getAttribute("data-sa-sym") || "");
            break;
          case "move-sim-asset":
            if (typeof moveSimAssetRow === "function") moveSimAssetRow(id, Number(el.getAttribute("data-sa-dir") || 0));
            break;
          case "open-model-detail":
            if (typeof openModelDetail === "function") openModelDetail(id);
            break;
          case "load-model":
            if (typeof loadModelById === "function") {
              loadModelById(id, el.getAttribute("data-sa-open-sim") === "1");
            }
            break;
          case "toggle-model-protect":
            if (typeof toggleModelProtection === "function") toggleModelProtection(id, evt);
            break;
          case "rename-model":
            if (typeof renameDetailModel === "function") renameDetailModel(id);
            break;
          case "dup-model":
            if (typeof duplicateDetailModel === "function") duplicateDetailModel(id);
            break;
          case "del-model":
            if (typeof deleteDetailModel === "function") deleteDetailModel(id);
            break;
          case "save-detail-notes":
            if (typeof saveDetailNotes === "function") saveDetailNotes(id);
            break;
          case "select-dialog-save":
            if (typeof selectDialogSave === "function") selectDialogSave(name);
            break;
          case "select-portfolio":
            if (typeof selectPortfolioRow === "function") selectPortfolioRow(id);
            break;
          case "edit-portfolio":
            if (typeof editPortfolioRow === "function") editPortfolioRow(id);
            break;
          case "del-portfolio":
            if (typeof openPortfolioDeleteConfirm === "function") openPortfolioDeleteConfirm(id);
            break;
          case "load-ssa-path":
            if (typeof loadSsaFileByPath === "function") loadSsaFileByPath(el.getAttribute("data-sa-path") || "");
            break;
          case "sync-dob":
            if (typeof syncInvestorDobFromXml === "function") syncInvestorDobFromXml(el.getAttribute("data-sa-dob") || "");
            break;
        }
      }
      document.addEventListener("click", evt => {
        const el = evt.target && evt.target.closest ? evt.target.closest("[data-sa-action]") : null;
        if (!el) return;
        _runSaAction(el, evt, el.getAttribute("data-sa-action"));
      });
      document.addEventListener("dblclick", evt => {
        const el = evt.target && evt.target.closest ? evt.target.closest("[data-sa-dbl]") : null;
        if (!el) return;
        if (el.getAttribute("data-sa-dbl") === "load-dialog-save") {
          if (typeof selectDialogSave === "function") selectDialogSave(el.getAttribute("data-sa-name") || "");
          if (typeof loadSelectedDialogSave === "function") loadSelectedDialogSave();
        }
      });
      document.addEventListener("change", evt => {
        const el = evt.target && evt.target.closest ? evt.target.closest("[data-sa-change]") : null;
        if (!el) return;
        const chg = el.getAttribute("data-sa-change");
        const tok = el.getAttribute("data-sa-tok") || "";
        if (chg === "priv-enabled" && typeof onPrivacyEntityFieldChange === "function") {
          onPrivacyEntityFieldChange(tok, "enabled", Boolean(el.checked));
        } else if (chg === "priv-synonyms" && typeof onPrivacyEntityFieldChange === "function") {
          onPrivacyEntityFieldChange(tok, "synonyms", String(el.value || ""));
        }
      });
      document.addEventListener("keydown", evt => {
        const el = evt.target && evt.target.closest ? evt.target.closest("[data-sa-keydown='model-card']") : null;
        if (!el || typeof onModelCardKeyDown !== "function") return;
        onModelCardKeyDown(evt, el.getAttribute("data-sa-id") || "");
      });
    })();

    function switchScreen(screen) {
      const normalized = screen === "scenarios" ? "models" : screen;
      // DESIGN INTENT (`DA2-07b`): Support `"settings"` / `"cfg"` tab-switch actions by opening Portfolio Settings.
      if (normalized === "settings" || normalized === "cfg") {
        if (typeof openProjectSettingsDialog === "function") openProjectSettingsDialog("info");
        return;
      }
      const valid = ["models", "portfolio", "google", "retirement", "income"];
      let nextScreen = valid.includes(normalized) ? normalized : "models";
      const comps = getGrantCompanies();
      if (nextScreen === "google" && comps.length === 0) {
        nextScreen = "models";
      }
      currentScreen = nextScreen;
      const mdlPane = document.getElementById("screen-models");
      if (mdlPane) mdlPane.classList.toggle("active", currentScreen === "models");
      document.getElementById("screen-portfolio").classList.toggle("active", currentScreen === "portfolio");
      document.getElementById("screen-google").classList.toggle("active", currentScreen === "google");
      document.getElementById("screen-retirement").classList.toggle("active", currentScreen === "retirement");
      const incPane = document.getElementById("screen-income");
      if (incPane) incPane.classList.toggle("active", currentScreen === "income");

      const mdlBtn = document.getElementById("nav-btn-models");
      if (mdlBtn) mdlBtn.classList.toggle("active", currentScreen === "models");
      document.getElementById("nav-btn-portfolio").classList.toggle("active", currentScreen === "portfolio");
      document.getElementById("nav-btn-google").classList.toggle("active", currentScreen === "google");
      document.getElementById("nav-btn-retirement").classList.toggle("active", currentScreen === "retirement");
      const incBtn = document.getElementById("nav-btn-income");
      if (incBtn) incBtn.classList.toggle("active", currentScreen === "income");

      if (currentScreen === "models") {
        document.getElementById("hdr-title").textContent = "Saved Scenarios";
      } else if (currentScreen === "google") {
        const curC = comps.find(c => c.symbol === activeGrantSymbol) || comps[0];
        const shortNm = curC ? (curC.short_name || curC.symbol) : "Equity";
        const awardTp = curC ? (curC.award_type || "RSU") : "RSU";
        document.getElementById("hdr-title").textContent = comps.length >= 2
          ? `Stock Grants — ${shortNm} (${curC.symbol})`
          : `${shortNm} Stock & ${awardTp} Releases`;
      } else if (currentScreen === "retirement") {
        document.getElementById("hdr-title").textContent = "Retirement Simulator";
      } else if (currentScreen === "income") {
        document.getElementById("hdr-title").textContent = "SSN Visualizer (Social Security & Feeders)";
      } else {
        document.getElementById("hdr-title").textContent = "Positions & Timeline";
      }

      hideTooltip();
      document.getElementById("ui-tooltip").style.display = "none";

      const targetHash = currentScreen === "models"
        ? "#scenarios"
        : (currentScreen === "google"
            ? "#equity-grants"
            : (currentScreen === "retirement" ? "#retirement" : (currentScreen === "income" ? "#income-expense" : "#portfolio")));
      if (window.location.hash !== targetHash) {
        history.replaceState(null, "", targetHash);
      }

      syncSharedGrowthControls();

      if (currentScreen === "models") {
        if (typeof renderModelsScreen === "function") renderModelsScreen();
      } else if (currentScreen === "google") {
        renderRelChartAndNav();
        renderGoogleStockTables();
      } else if (currentScreen === "retirement") {
        if (typeof updateSimBaselineSummary === "function") updateSimBaselineSummary();
        renderSimChartAndNav();
      } else if (currentScreen === "income") {
        renderSsnChartAndNav();
        renderSsnTables();
      } else {
        renderChartAndNav();
      }
    }

    // Universal floating tooltip positioner enforcing >= 52px (> 1cm) gap from mouse pointer
    function placeFloatingBoxAwayFromPointer(el, clientX, clientY) {
      el.style.visibility = "hidden";
      el.style.display = "block";
      const w = el.offsetWidth;
      const h = el.offsetHeight;
      const vw = window.innerWidth;
      const vh = window.innerHeight;
      const margin = 10;

      let left;
      let top = Math.min(Math.max(margin, clientY - h * 0.35), Math.max(margin, vh - h - margin));

      if (clientX + MIN_POINTER_GAP_PX + w <= vw - margin) {
        left = clientX + MIN_POINTER_GAP_PX;
      } else if (clientX - MIN_POINTER_GAP_PX - w >= margin) {
        left = clientX - MIN_POINTER_GAP_PX - w;
      } else {
        left = Math.min(Math.max(margin, clientX - w / 2), Math.max(margin, vw - w - margin));
        if (clientY - MIN_POINTER_GAP_PX - h >= margin) {
          top = clientY - MIN_POINTER_GAP_PX - h;
        } else {
          top = Math.min(vh - h - margin, clientY + MIN_POINTER_GAP_PX);
        }
      }

      el.style.left = Math.round(left) + "px";
      el.style.top = Math.round(top) + "px";
      el.style.visibility = "visible";
    }

    // DESIGN INTENT (`DA2-13a` — coupled with `resolveSimAssetForecast` in `app_simulator.js`):
    // Screen 1 & Screen 2 forecast bars MUST resolve primary equity and diversified holdings through
    // `resolveSimAssetForecast` (honoring `simOptimismMultiplier` and `holdingFeePct`) and read
    // `a.current_value ?? a.initial_value` (`BASE_SIM_ASSETS` carries `current_value` from `view_model.py`).
    function timelineGrowthFactor(asset, yearsAhead) {
      const fc = resolveSimAssetForecast(asset);
      const feePct = Number(fc.expense_ratio_pct) || 0;
      const netGp = fc.net_growth_pct != null ? Number(fc.net_growth_pct) : ((Number(fc.growth_pct) || 0) - feePct);
      if (yearsAhead <= 1 || holdingFadeTarget(asset) == null)
        return (1 + netGp / 100) ** yearsAhead;
      const curve = holdingReturnCurve(asset, fc.base_growth_pct);
      let factor = 1;
      for (let year = 0; year < yearsAhead; year++) {
        factor *= (1 + (curve(year) * fc.growth_multiplier - feePct) / 100) ** Math.min(1, yearsAhead - year);
      }
      return factor;
    }

    function googFutureMultiplierForIso(isoStr) {
      if (!isoStr || isoStr <= TODAY_ISO) return 1.0;
      const primSimA = (BASE_SIM_ASSETS || []).find(
        x => x && (x.is_goog_priced || x.symbol === PRIMARY_SYMBOL || (x.forecast_key || x.id) === PRIMARY_SYMBOL)
      );
      const yearsAhead = (Date.parse(isoStr) - Date.parse(TODAY_ISO)) / (365.25 * 86400000);
      if (primSimA) return timelineGrowthFactor(primSimA, yearsAhead);
      const gp = resolveHoldingForecast(PRIMARY_SYMBOL, [], futureGoogGrowthPct, "single_stock").growth_pct;
      return (1 + gp / 100) ** yearsAhead;
    }

    function nonGoogFutureMultiplierForIso(isoStr) {
      if (!isoStr || isoStr <= TODAY_ISO) return 1;
      const yearsAhead = (Date.parse(isoStr) - Date.parse(TODAY_ISO)) / (365.25 * 86400000);
      let sum = 0, value = 0;
      for (const a of BASE_SIM_ASSETS) {
        if (a.bucket === "goog" || a.is_unvested_goog) continue;
        const v = Math.max(0, Number(a.current_value ?? a.initial_value) || 0);
        if (v > 0) { sum += v * timelineGrowthFactor(a, yearsAhead); value += v; }
      }
      return value ? sum / value : (1 + futureNonGoogGrowthPct / 100) ** yearsAhead;
    }

    function googFutureMultiplier(w) {
      if (!w || w.iso <= TODAY_ISO || w.kind !== "FORECAST") return 1.0;
      return googFutureMultiplierForIso(w.iso);
    }

    function nonGoogFutureMultiplier(w) {
      if (!w || w.iso <= TODAY_ISO || w.kind !== "FORECAST") return 1.0;
      return nonGoogFutureMultiplierForIso(w.iso);
    }

    function baseGoogVestedForBar(w) {
      return (w && w.buckets && w.buckets.goog_combined) ? w.buckets.goog_combined : 0;
    }

    // Grow each timeline stack bucket at its resolved `holdingForecasts` rate on FORECAST bars.
    function bucketForecastMultiplier(bucketId, w) {
      if (!w || w.iso <= TODAY_ISO || w.kind !== "FORECAST") return 1.0;
      const yrs = (Date.parse(w.iso + "T00:00:00Z") - Date.parse(TODAY_ISO + "T00:00:00Z")) / (365.25 * 86400000);
      if (yrs <= 0) return 1.0;
      const spec = (PAYLOAD.forecast_bucket_rates || {})[bucketId];
      if (spec && Array.isArray(spec.forecast_keys) && spec.forecast_keys.length > 0) {
        let wSum = 0, vSum = 0;
        spec.forecast_keys.forEach(entry => {
          const k = typeof entry === "string" ? entry : (entry && entry.key ? entry.key : "");
          if (!k) return;
          const wt = (entry && typeof entry === "object" && Number(entry.weight) > 0) ? Number(entry.weight) : 1.0;
          const fbRate = (entry && typeof entry === "object" && entry.fallback_rate != null) ? Number(entry.fallback_rate) : (spec.rate || 0);
          const simA = (BASE_SIM_ASSETS || []).find(
            a => a && (a.id === k || a.forecast_key === k || (k === PRIMARY_SYMBOL && (a.is_goog_priced || a.symbol === PRIMARY_SYMBOL)))
          );
          const factor = simA ? timelineGrowthFactor(simA, yrs) :
            (1 + resolveHoldingForecast(k, [], fbRate, k === PRIMARY_SYMBOL ? "single_stock" : "broad_equity").growth_pct / 100) ** yrs;
          wSum += wt * factor;
          vSum += wt;
        });
        if (vSum > 0) {
          return wSum / vSum;
        }
      }
      if (spec && spec.model === "fixed") {
        return Math.pow(1.0 + (spec.rate || 0) / 100.0, yrs);
      }
      if (bucketId === "goog_combined") return googFutureMultiplier(w);
      return nonGoogFutureMultiplier(w);
    }

    function vestedBarValue(w) {
      if (!w) return 0;
      if (w.kind !== "FORECAST") {
        return Math.max(0, w.vestedTotal || 0);
      }
      const buckets = w.buckets || {};
      const defs = (PAYLOAD.schemas && PAYLOAD.schemas.bucket) || [];
      let tot = 0;
      for (const d of defs) {
        const raw = buckets[d.id] || 0;
        tot += raw * bucketForecastMultiplier(d.id, w);
      }
      return Math.max(0, tot);
    }

    function projectedReleaseValue(w) {
      if (!w || w.iso <= TODAY_ISO || w.kind !== "FORECAST") return 0;
      const gross = w.projRelGross || 0;
      const base = taxMode === "net" ? gross * NET_RATE : gross;
      return base * googFutureMultiplier(w);
    }

    function totalBarValue(w) {
      return vestedBarValue(w) + projectedReleaseValue(w);
    }

    function getGoogReferenceRate() {
      const g = PAYLOAD.bucket_summary && PAYLOAD.bucket_summary.goog;
      if (!g) return 0;
      const raw = g.five_yr_cagr_pct != null ? g.five_yr_cagr_pct : g.annual_appreciation_pct;
      return Math.max(-20, Math.min(25, Math.round(raw * 10) / 10));
    }

    function getNonGoogReferenceRate() {
      const ng = PAYLOAD.bucket_summary && PAYLOAD.bucket_summary.non_goog;
      if (!ng) return 0;
      return Math.max(-20, Math.min(25, Math.round(ng.annual_appreciation_pct * 10) / 10));
    }

    function updateSliderReferenceButtons() {
      // Legacy top-level growth sliders were replaced by per-holding forecast capsules.
    }

    function syncSharedGrowthControls() {
      futureGoogGrowthPct = Math.max(-20, Math.min(25, Math.round((Number(futureGoogGrowthPct) || 0) * 10) / 10));
      futureNonGoogGrowthPct = Math.max(-20, Math.min(25, Math.round((Number(futureNonGoogGrowthPct) || 0) * 10) / 10));
      updateSliderReferenceButtons();
      updatePortfolioForecastPresetUI();
    }

    // U5: coalesce rapid slider input into one recompute+render per animation frame.
    let _simRecomputeRaf = 0;
    function scheduleSimRecompute() {
      if (_simRecomputeRaf) return;
      const raf = (typeof requestAnimationFrame === "function") ? requestAnimationFrame : (cb => setTimeout(cb, 16));
      _simRecomputeRaf = raf(() => {
        _simRecomputeRaf = 0;
        recomputeSimBars();
        // CROSS-FILE INVARIANT (`DA2-02`): Keep Screen 4 CPI-deflated "Today's $" bars & matrix synced with `simInflationPct`.
        if (typeof recomputeSsnBars === "function") recomputeSsnBars();
        renderChartAndNav();
        renderHoldingsTable();
        renderSimChartAndNav();
        if (currentScreen === "ssn") {
          if (typeof renderSsnChartAndNav === "function") renderSsnChartAndNav();
          if (typeof renderSsnTables === "function") renderSsnTables();
        }
        checkSaveDirtyState();
      });
    }

    function onGoogGrowthInput(val) {
      futureGoogGrowthPct = Math.max(-20, Math.min(25, parseFloat(val) || 0));
      if (!holdingForecasts || typeof holdingForecasts !== "object") {
        holdingForecasts = {};
      }
      GOOG_PRIMARY_KEYS.forEach(k => {
        if (holdingForecasts[k]) {
          holdingForecasts[k].growth_pct = futureGoogGrowthPct;
        } else {
          holdingForecasts[k] = { growth_pct: futureGoogGrowthPct, risk_class: "single_stock" };
        }
      });
      syncSharedGrowthControls();
      scheduleSimRecompute();
    }

    function onNonGoogGrowthInput(val) {
      futureNonGoogGrowthPct = Math.max(-20, Math.min(25, parseFloat(val) || 0));
      if (!holdingForecasts || typeof holdingForecasts !== "object") {
        holdingForecasts = {};
      }
      (BASE_SIM_ASSETS || []).forEach(a => {
        if (!a || !a.id || a.bucket === "goog" || a.rate_model === "fixed") return;
        const rc = a.default_risk_class || "broad_equity";
        if (rc === "single_stock" || rc === "cash_fixed") return;
        const fk = a.forecast_key || a.id;
        holdingForecasts[fk] = { growth_pct: futureNonGoogGrowthPct, risk_class: holdingForecasts[fk]?.risk_class || rc };
        if (a.id !== fk) {
          holdingForecasts[a.id] = { ...holdingForecasts[fk] };
        }
      });
      syncSharedGrowthControls();
      scheduleSimRecompute();
    }

    function _setSimRefTip(btnOrId, spec) {
      const el = typeof btnOrId === "string" ? document.getElementById(btnOrId) : btnOrId;
      if (!el || !spec) return;
      if (typeof el.removeAttribute === "function") el.removeAttribute("title");
      const l1 = escHtml(spec.label1 || "What it means:");
      const l2 = escHtml(spec.label2 || "How it's calculated:");
      const html =
        `<div style="min-width:260px;max-width:365px;line-height:1.45;">` +
        `<div style="font-weight:700;font-size:12px;color:#c4b5fd;margin-bottom:4px;">${escHtml(spec.title || "")}</div>` +
        `<div style="font-size:11px;color:#e2e8f0;margin-bottom:5px;"><strong>${l1}</strong> ${spec.meaningHtml || escHtml(spec.meaning || "")}</div>` +
        `<div style="font-size:10.8px;color:#cbd5e1;margin-bottom:5px;"><strong>${l2}</strong> ${spec.calcHtml || escHtml(spec.calculation || "")}</div>` +
        `<div style="font-size:10px;color:#94a3b8;border-top:1px solid rgba(255,255,255,0.08);padding-top:4px;">${escHtml(spec.clickHint || "Click to reset this control to its reference value.")}</div>` +
        `</div>`;
      if (typeof el.setAttribute === "function") el.setAttribute("data-tip", html);
    }

    function syncSimBurnControls() {
      const burnCompact = formatBurnCompact(RETIREMENT_ANNUAL_BURN);
      const moBurn = Math.round(RETIREMENT_ANNUAL_BURN / 12).toLocaleString("en-US");

      const refBtn = document.getElementById("sim-burn-ref-btn");
      if (refBtn) {
        refBtn.textContent = `Ref: ${burnCompact}`;
        refBtn.onclick = () => onSimBurnInput(RETIREMENT_ANNUAL_BURN);
        refBtn.classList.toggle("active", Math.abs(simAnnualBurn - RETIREMENT_ANNUAL_BURN) < 500);
        _setSimRefTip(refBtn, {
          title: `Reference Annual Burn Rate (${burnCompact}/yr)`,
          meaning: `Your baseline annual after-tax household spending budget in today's dollars ($${moBurn}/mo), automatically adjusted upward each month for inflation.`,
          calculation: `Uses the baseline annual spending target configured in Portfolio Settings (${fmtUSD0(RETIREMENT_ANNUAL_BURN)}/yr; defaults to $50,000/yr if unset).`,
          clickHint: `Click to reset Annual Burn Rate to ${burnCompact}/yr.`,
        });
      }

      const currentBurn = (simAnnualBurn != null && Number.isFinite(Number(simAnnualBurn)))
        ? Math.max(0, Number(simAnnualBurn))
        : RETIREMENT_ANNUAL_BURN;
      const minVal = 0;
      const maxVal = Math.max(100000, Math.round(Math.max(1000000, RETIREMENT_ANNUAL_BURN * 2.0, currentBurn * 1.5) / 1000) * 1000);
      const stepVal = Math.max(500, Math.round(RETIREMENT_ANNUAL_BURN * 0.01 / 500) * 500);

      const sl = document.getElementById("sim-burn-slider");
      if (sl) {
        sl.min = String(minVal);
        sl.max = String(maxVal);
        sl.step = String(stepVal);
        if (parseFloat(sl.value) !== currentBurn) {
          sl.value = String(currentBurn);
        }
      }
      const minLbl = document.getElementById("sim-burn-min-lbl");
      if (minLbl) minLbl.textContent = formatBurnCompact(minVal);
      const maxLbl = document.getElementById("sim-burn-max-lbl");
      if (maxLbl) maxLbl.textContent = formatBurnCompact(maxVal);

      const pill = document.getElementById("sim-burn-pill");
      if (pill) {
        pill.textContent = `${formatBurnCompact(currentBurn)} / yr`;
        pill.title = `Initial Annual Burn Rate in today's dollars (click to reset to reference ${burnCompact}/yr)`;
        pill.onclick = () => onSimBurnInput(RETIREMENT_ANNUAL_BURN);
      }
      if (typeof updateSimMonthlySpendingDOM === "function") updateSimMonthlySpendingDOM();
      if (typeof updateSimBaselineSummary === "function") updateSimBaselineSummary();
      if (typeof updateSimOverridesSummary === "function") updateSimOverridesSummary();
    }

    function onSimBurnInput(val) {
      if (val === undefined || val === null || val === "") {
        simAnnualBurn = RETIREMENT_ANNUAL_BURN;
      } else {
        const parsed = parseFloat(val);
        simAnnualBurn = Number.isFinite(parsed) ? Math.max(0, Math.round(parsed)) : RETIREMENT_ANNUAL_BURN;
      }
      syncSimBurnControls();
      if (typeof setActiveSimComputingControl === "function") setActiveSimComputingControl("sim-burn-pill");
      scheduleSimRecompute();
    }

    function syncSimInflationControls() {
      const sl = document.getElementById("sim-inf-slider");
      if (sl && parseFloat(sl.value) !== simInflationPct) {
        sl.value = String(simInflationPct);
      }
      const pill = document.getElementById("sim-inf-pill");
      if (pill) {
        const keepComputing = pill.classList && pill.classList.contains("sim-computing");
        pill.textContent = `${simInflationPct.toFixed(1)}% / yr`;
        pill.className = "mdl-in-pill mono" + (keepComputing ? " sim-computing" : "");
        pill.title = `Annual Inflation Rate (click to reset to 10-yr reference: ${RETIREMENT_INFLATION_REF.toFixed(1)}%/yr, based on ${RETIREMENT_INFLATION_RANGE} average)`;
        pill.onclick = () => onSimInflationInput(RETIREMENT_INFLATION_REF);
      }
      const zeroBtn = document.getElementById("sim-inf-zero-btn");
      if (zeroBtn) zeroBtn.classList.toggle("active", Math.abs(simInflationPct) < 0.05);
      const refBtn = document.getElementById("sim-inf-ref-btn");
      if (refBtn) {
        refBtn.textContent = `Ref: ${RETIREMENT_INFLATION_REF.toFixed(1)}%`;
        refBtn.onclick = () => onSimInflationInput(RETIREMENT_INFLATION_REF);
        refBtn.classList.toggle("active", Math.abs(simInflationPct - RETIREMENT_INFLATION_REF) < 0.05);
        _setSimRefTip(refBtn, {
          title: `Reference Annual Inflation (${RETIREMENT_INFLATION_REF.toFixed(1)}%/yr)`,
          meaning: "The annual inflation rate compounded monthly to escalate future living expenses and convert projected balances back into Today's $.",
          calculation: `The 10-year compound annual average of U.S. consumer inflation (CPI) over ${RETIREMENT_INFLATION_RANGE}: ${RETIREMENT_INFLATION_REF.toFixed(1)}%/yr.`,
          clickHint: `Click to reset Annual Inflation Rate to ${RETIREMENT_INFLATION_REF.toFixed(1)}%/yr.`,
        });
      }
      if (typeof updateSimBaselineSummary === "function") updateSimBaselineSummary();
      if (typeof updateSimOverridesSummary === "function") updateSimOverridesSummary();
    }

    function onSimInflationInput(val) {
      if (val === undefined || val === null || val === "") {
        val = RETIREMENT_INFLATION_REF;
      }
      simInflationPct = Math.max(0, Math.min(6, Math.round((parseFloat(val) || 0) * 10) / 10));
      syncSimInflationControls();
      if (typeof setActiveSimComputingControl === "function") setActiveSimComputingControl("sim-inf-pill");
      scheduleSimRecompute();
    }

    let simBaselineExpanded = false;

    function toggleSimBaselineDrawer() {
      setSimBaselineDrawerExpanded(!simBaselineExpanded);
    }

    function setSimBaselineDrawerExpanded(expanded) {
      simBaselineExpanded = Boolean(expanded);
      const body = document.getElementById("sim-baseline-body");
      const caret = document.getElementById("sim-baseline-caret");
      const btn = document.getElementById("sim-baseline-toggle-btn");
      if (body) {
        body.style.display = simBaselineExpanded ? "block" : "none";
      }
      if (caret) {
        caret.style.transform = simBaselineExpanded ? "rotate(90deg)" : "rotate(0deg)";
        caret.style.color = simBaselineExpanded ? "#38bdf8" : "#94a3b8";
      }
      if (btn) {
        btn.setAttribute("aria-expanded", simBaselineExpanded ? "true" : "false");
        btn.title = simBaselineExpanded ? "Click to collapse Inclusions" : "Click expansion arrow to adjust Inclusions";
      }
    }
    window.toggleSimBaselineDrawer = toggleSimBaselineDrawer;
    window.setSimBaselineDrawerExpanded = setSimBaselineDrawerExpanded;

    function simHasUnvested() {
      if (typeof PAYLOAD === "undefined" || !PAYLOAD) return false;
      const k = PAYLOAD.kpis || {};
      if ((Number(k.future_gsu_gross_shares) || 0) > 0 || (Number(k.future_gsu_net_val) || 0) > 0) return true;
      if (typeof getGrantCompanies === "function") {
        const comps = getGrantCompanies();
        if (Array.isArray(comps) && comps.some(c => (Number(c.future_est_net_shares) || 0) > 0 || (Number(c.future_gross_shares) || 0) > 0)) return true;
      }
      if (typeof SIM_ASSETS !== "undefined" && Array.isArray(SIM_ASSETS)) {
        if (SIM_ASSETS.some(a => a.is_unvested_goog && ((Number(a.current_value) || 0) > 0 || (Array.isArray(a.unvested_tranches) && a.unvested_tranches.length > 0)))) return true;
      }
      return false;
    }

    function simHasSSN() {
      const model = (typeof PAYLOAD !== "undefined" && PAYLOAD) ? PAYLOAD.ssn_model : null;
      if (!model || !Array.isArray(model.investors) || model.investors.length === 0) return false;
      return model.investors.some(inv => ((Number(inv.pia_annual_2026) || 0) > 0 || (Number(inv.pia_monthly_2026) || 0) > 0));
    }

    function simHasScheduledEvents() {
      return typeof simScheduledActions !== "undefined" && Array.isArray(simScheduledActions) && simScheduledActions.length > 0;
    }

    function syncSimInclusionsUI() {
      const hasUnv = simHasUnvested();
      const hasSsn = simHasSSN();
      const hasEv = simHasScheduledEvents();

      const unvGroup = document.getElementById("sim-unvested-toggle-group");
      if (unvGroup) unvGroup.style.display = hasUnv ? "inline-flex" : "none";

      const ssnGroup = document.getElementById("sim-ssn-toggle-group");
      if (ssnGroup) ssnGroup.style.display = hasSsn ? "inline-flex" : "none";

      const evGroup = document.getElementById("sim-events-toggle-group");
      if (evGroup) evGroup.style.display = hasEv ? "inline-flex" : "none";

      const anyAvail = hasUnv || hasSsn || hasEv;
      const bar = document.getElementById("sim-baseline-bar-container");
      if (bar) bar.style.display = anyAvail ? "" : "none";

      const box = document.getElementById("sim-inclusions-box");
      if (box) box.style.display = anyAvail ? "flex" : "none";

      const titleEl = document.getElementById("sim-baseline-header-label");
      if (titleEl) {
        titleEl.textContent = "Inclusions:";
      }
    }
    window.simHasUnvested = simHasUnvested;
    window.simHasSSN = simHasSSN;
    window.simHasScheduledEvents = simHasScheduledEvents;
    window.syncSimInclusionsUI = syncSimInclusionsUI;

    function updateSimBaselineSummary() {
      const el = document.getElementById("sim-baseline-summary-subtext");
      if (!el) return;

      syncSimInclusionsUI();

      const hasUnv = simHasUnvested();
      const hasSsn = simHasSSN();
      const hasEv = simHasScheduledEvents();

      const fmtInc = (label, on) => on
        ? `<span style="color:#38bdf8;font-weight:600;">${label}</span>`
        : `<span style="color:#64748b;text-decoration:line-through;">${label}</span>`;

      const incList = [];
      if (hasUnv) incList.push(fmtInc("Unvested Equity", typeof simIncludeUnvested !== "undefined" ? simIncludeUnvested : true));
      if (hasSsn) incList.push(fmtInc("SSN Income", typeof simIncludeSSN !== "undefined" ? simIncludeSSN : true));
      if (hasEv) incList.push(fmtInc("Scheduled Events", typeof simIncludeScheduledActions !== "undefined" ? simIncludeScheduledActions : true));

      el.innerHTML = incList.length > 0 ? incList.join(" &bull; ") : `<span class="muted">None configured</span>`;
    }
    window.updateSimBaselineSummary = updateSimBaselineSummary;

    function seriesForCurrentStack() {
      const defs = PAYLOAD.schemas[stackMode] || [];
      const mapField = stackMode === "bucket" ? "buckets" : (stackMode === "account" ? "accounts" : "assets");
      return defs.map(d => ({
        id: d.id,
        label: d.label,
        color: d.color,
        valFn: w => {
          const raw = (w[mapField] && w[mapField][d.id]) ? w[mapField][d.id] : 0;
          if (!w || w.kind !== "FORECAST") return raw;
          if (stackMode === "bucket") {
            return Math.max(0, raw * bucketForecastMultiplier(d.id, w));
          }
          const gMult = googFutureMultiplier(w);
          const ngMult = nonGoogFutureMultiplier(w);
          if (stackMode === "asset") {
            const nonEqBuckets = new Set(["cash_sweeps", "muni_bonds", "fixed_annuities", "indexed_annuities", "rila_annuities", "mutual_funds"]);
            const computeAssetUnscaled = acId => {
              const r = (w.assets && w.assets[acId]) ? w.assets[acId] : 0;
              if (r <= 0) return 0;
              if (acId === "CASH") return r * bucketForecastMultiplier("cash_sweeps", w);
              if (acId === "MUNI_BOND" || acId === "MUNI_BONDS") return r * bucketForecastMultiplier("muni_bonds", w);
              if (acId === "FIXED_ANNUITY") return r * bucketForecastMultiplier("fixed_annuities", w);
              if (acId === "INDEXED_ANNUITY") return r * bucketForecastMultiplier("indexed_annuities", w);
              if (acId === "RILA_ANNUITY") return r * bucketForecastMultiplier("rila_annuities", w);
              if (acId === "MUTUAL_FUND") return r * bucketForecastMultiplier("mutual_funds", w);
              if (acId === "EQUITY") {
                const bDefs = (PAYLOAD.schemas && PAYLOAD.schemas.bucket) || [];
                const bMap = w.buckets || {};
                let eqBaseSum = 0;
                let eqGrownSum = 0;
                for (const bd of bDefs) {
                  if (nonEqBuckets.has(bd.id)) continue;
                  const bv = Number(bMap[bd.id]) || 0;
                  if (bv > 0) {
                    eqBaseSum += bv;
                    eqGrownSum += bv * bucketForecastMultiplier(bd.id, w);
                  }
                }
                const remEq = r - eqBaseSum;
                if (remEq > 0.005) {
                  eqGrownSum += remEq * bucketForecastMultiplier("mutual_funds", w);
                } else if (eqBaseSum > 0 && Math.abs(remEq) > 0.005) {
                  eqGrownSum *= (r / eqBaseSum);
                }
                return eqGrownSum > 0 ? eqGrownSum : r * ngMult;
              }
              return r * ngMult;
            };
            const unscaled = computeAssetUnscaled(d.id);
            const totUnscaled = defs.reduce((s, item) => s + computeAssetUnscaled(item.id), 0);
            if (totUnscaled > 0) {
              return Math.max(0, unscaled * (vestedBarValue(w) / totUnscaled));
            }
            return Math.max(0, unscaled);
          }
          // Account mode: scale proportionally to vestedBarValue / w.vestedTotal
          const baseTot = Number(w.vestedTotal) || 0;
          if (baseTot > 0) {
            return Math.max(0, raw * (vestedBarValue(w) / baseTot));
          }
          return raw;
        }
      }));
    }

    // U1: keep the unvested-toggle labels in sync with the live payload instead of stale literals.
    function updateUnvestedToggleLabels() {
      const k = PAYLOAD.kpis || {};
      const vestedM = (k.total_vested || 0) / 1e6;
      const comps = getGrantCompanies();
      let netSh = 0;
      if (comps.length > 0) {
        netSh = Math.round(comps.reduce((s, c) => s + (Number(c.future_est_net_shares) || 0), 0));
      } else {
        const goog = PAYLOAD.goog_live_price || 0;
        netSh = goog > 0 ? Math.round((k.future_gsu_net_val || 0) / goog) : 0;
      }
      const offBtn = document.getElementById("sim-unvested-off-btn");
      if (offBtn) offBtn.title = `Simulate using only currently vested holdings ($${vestedM.toFixed(2)}M)`;
      const onBtn = document.getElementById("sim-unvested-on-btn");
      if (onBtn) {
        onBtn.textContent = "Included";
        onBtn.title = `Include scheduled unvested stock releases (${netSh.toLocaleString("en-US")} est. after-tax net shares; each tranche vests with 100% cost basis on its vest date)`;
      }
    }

    function updateGrantsNavButton() {
      const btn = document.getElementById("nav-btn-google");
      const lbl = document.getElementById("nav-grants-lbl");
      const sub = document.getElementById("nav-grants-sub");
      const chev = document.getElementById("nav-grants-chevron");
      if (!btn || !lbl || !sub) return;
      const comps = getGrantCompanies();
      if (comps.length === 0) {
        btn.style.display = "none";
        if (chev) chev.style.display = "none";
        if (typeof closeGrantsPopup === "function") closeGrantsPopup();
        return;
      }
      btn.style.display = "flex";
      if (comps.length === 1) {
        const c = comps[0];
        lbl.textContent = `${c.short_name || c.symbol} Vesting`;
        sub.textContent = `Releases & ${c.award_type || "RSU"} Grants`;
        if (chev) chev.style.display = "none";
        if (typeof closeGrantsPopup === "function") closeGrantsPopup();
      } else {
        lbl.textContent = "Stock Vesting";
        const totSh = comps.reduce((s, c) => s + (Number(c.future_gross_shares) || 0), 0);
        sub.textContent = `${comps.length} Companies • ${Math.round(totSh).toLocaleString("en-US")} Unvested Sh`;
        if (chev) chev.style.display = "inline-flex";
      }
    }

    function rebuildRelYearJumpButtons() {
      const relYears = Array.from(new Set(REL_BARS.map(b => b.yearLbl))).sort();
      const minRelYr = relYears.length ? relYears[0] : (TODAY_ISO ? TODAY_ISO.slice(0, 4) : "2023");
      const maxRelYr = relYears.length ? relYears[relYears.length - 1] : minRelYr;
      const curYr = TODAY_ISO ? TODAY_ISO.slice(0, 4) : minRelYr;

      const relYtdBtn = document.querySelector('[data-rel-preset="ytd"]');
      if (relYtdBtn) relYtdBtn.textContent = `${curYr} YTD`;

      const relHistBtn = document.querySelector('[data-rel-preset="historical"]');
      if (relHistBtn) relHistBtn.textContent = `Historical (${minRelYr}–Today)`;

      const relAllBtn = document.querySelector('[data-rel-preset="all"]');
      if (relAllBtn) relAllBtn.textContent = minRelYr === maxRelYr ? `Full ${minRelYr}` : `Full ${minRelYr}–${maxRelYr}`;

      const ryWrap = document.getElementById("rel-year-btns");
      if (!ryWrap) return;
      ryWrap.querySelectorAll("[data-rel-year]").forEach(el => el.remove());
      relYears.forEach(yr => {
        const b = document.createElement("button");
        b.className = "btn btn-year";
        b.dataset.relYear = yr;
        b.textContent = yr;
        b.onclick = () => jumpRelToYear(yr);
        ryWrap.appendChild(b);
      });
    }

    function initHeaderAndKPIs() {
      updateUnvestedToggleLabels();
      const hasPortfolio = Boolean(PAYLOAD.has_portfolio);
      const activePortfolioName = hasPortfolio
        ? (PAYLOAD.household_name || (PAYLOAD.project && PAYLOAD.project.household_name) || "Portfolio Settings")
        : "Portfolio Settings";
      const navBadge = document.getElementById("nav-household-badge");
      if (navBadge) {
        navBadge.alt = activePortfolioName;
        navBadge.title = hasPortfolio
          ? `${activePortfolioName} • Click to open Portfolio Settings`
          : "Portfolio Settings • Click to create or select a Portfolio";
        if (PAYLOAD.badge_image) navBadge.src = PAYLOAD.badge_image;
      }
      const navSettingsLbl = document.getElementById("nav-settings-lbl");
      if (navSettingsLbl) {
        navSettingsLbl.textContent = hasPortfolio ? activePortfolioName : "Portfolio Settings";
        navSettingsLbl.title = hasPortfolio
          ? `${activePortfolioName} • Manage Portfolios, Investors, Beneficiaries, Symbols, and Saved Prompts`
          : "Portfolio Settings • Create or select a Portfolio";
      }
      const navSettingsSub = document.getElementById("nav-settings-sub");
      if (navSettingsSub) {
        navSettingsSub.textContent = hasPortfolio ? "Portfolio Settings" : "Select or create portfolio";
      }
      const ms0Init = Number(INV0 && INV0.milestone_age) || 85;
      const simInhEyebrow = document.getElementById("sim-kpi-inheritance-lbl");
      if (simInhEyebrow) simInhEyebrow.textContent = `\u2605 Age ${ms0Init} Wealth (Today's $)`;
      const simSolvEyebrow = document.getElementById("sim-kpi-solvency-lbl");
      if (simSolvEyebrow) simSolvEyebrow.textContent = `\u2605 Funded / Solvency (To Age ${ms0Init})`;
      const simTaxEyebrow = document.getElementById("sim-kpi-taxdrag-lbl");
      if (simTaxEyebrow) simTaxEyebrow.textContent = `\u2605 Tax Efficiency (Through Age ${ms0Init})`;

      const inv0DrawLbl = document.getElementById("ssn-inv0-draw-lbl");
      if (inv0DrawLbl) inv0DrawLbl.textContent = `${INV0.name} First Draw Age:`;
      const kpi0Lbl = document.getElementById("ssn-kpi-inv0-lbl");
      if (kpi0Lbl) kpi0Lbl.textContent = `${INV0.name} Total Income (Age ${ssnInv0ClaimAge}–${ms0Init})`;
      const kpiCombLbl = document.getElementById("ssn-kpi-comb-lbl");
      if (kpiCombLbl) kpiCombLbl.textContent = HAS_SECOND_INVESTOR ? "Total Household Income" : "After-Tax Net Household Income";
      const mat0Mo = document.getElementById("ssn-mat-inv0-mo-hdr");
      if (mat0Mo) mat0Mo.textContent = `${INV0.name} / Mo`;
      const mat0Tot = document.getElementById("ssn-mat-inv0-tot-hdr");
      if (mat0Tot) mat0Tot.textContent = `${INV0.name} Total (By ${ms0Init})`;
      const matCombHdr = document.getElementById("ssn-mat-comb-hdr");
      if (matCombHdr) matCombHdr.textContent = HAS_SECOND_INVESTOR ? `Combined (By ${ms0Init})` : `Total (By ${ms0Init})`;
      const hist0 = document.getElementById("ssn-hist-inv0-hdr");
      if (hist0) hist0.innerHTML = `${escHtml(INV0.name)} Capped &rarr; Indexed`;

      const kpi1Card = document.getElementById("ssn-kpi-inv1-card");
      const inv1SliderBox = document.getElementById("ssn-inv1-slider-box");
      const drawSlidersGrid = document.getElementById("ssn-draw-sliders-grid");
      const mat1Mo = document.getElementById("ssn-mat-inv1-mo-hdr");
      const mat1Tot = document.getElementById("ssn-mat-inv1-tot-hdr");
      const hist1 = document.getElementById("ssn-hist-inv1-hdr");
      if (HAS_SECOND_INVESTOR && INV1) {
        const ms1Init = Number(INV1.milestone_age) || 85;
        if (kpi1Card) kpi1Card.style.display = "";
        if (inv1SliderBox) inv1SliderBox.style.display = "";
        if (drawSlidersGrid) drawSlidersGrid.style.gridTemplateColumns = "repeat(2,minmax(0,1fr))";
        const inv1DrawLbl = document.getElementById("ssn-inv1-draw-lbl");
        if (inv1DrawLbl) inv1DrawLbl.textContent = `${INV1.name} First Draw Age:`;
        const kpi1Lbl = document.getElementById("ssn-kpi-inv1-lbl");
        if (kpi1Lbl) kpi1Lbl.textContent = `${INV1.name} Total Income (Age ${ssnInv1ClaimAge}–${ms1Init})`;
        if (mat1Mo) { mat1Mo.style.display = ""; mat1Mo.textContent = `${INV1.name} / Mo`; }
        if (mat1Tot) { mat1Tot.style.display = ""; mat1Tot.textContent = `${INV1.name} Total (By ${ms1Init})`; }
        if (hist1) { hist1.style.display = ""; hist1.innerHTML = `${escHtml(INV1.name)} Capped &rarr; Indexed`; }
      } else {
        if (kpi1Card) kpi1Card.style.display = "none";
        if (inv1SliderBox) inv1SliderBox.style.display = "none";
        if (drawSlidersGrid) drawSlidersGrid.style.gridTemplateColumns = "1fr";
        if (mat1Mo) mat1Mo.style.display = "none";
        if (mat1Tot) mat1Tot.style.display = "none";
        if (hist1) hist1.style.display = "none";
      }
      document.getElementById("hdr-title").textContent = currentScreen === "models" ? "Saved Scenarios" : "Positions & Timeline";
      const tsEl = document.getElementById("hdr-goog-ts");
      if (tsEl) tsEl.textContent = currentGoogQuoteTs || PAYLOAD.generated_at || "";
      updateHeaderGoogDisplay();

      const k = PAYLOAD.kpis;
      document.getElementById("kpi-total").textContent = fmtUSD2(k.total_vested);
      const acctCount = (PAYLOAD.holdings_by_account ? PAYLOAD.holdings_by_account.length : 0)
        || (PAYLOAD.schemas?.account ? PAYLOAD.schemas.account.length : 6);
      const totalSubEl = document.getElementById("kpi-total-sub");
      if (totalSubEl) {
        totalSubEl.textContent = `${acctCount} accounts (excludes unvested future equity)`;
      }

      const wowEl = document.getElementById("kpi-wow");
      wowEl.textContent = `${fmtSignedUSD0(k.wow_change)} (${k.wow_pct >= 0 ? "+" : ""}${k.wow_pct.toFixed(2)}%)`;
      wowEl.className = "kpi-val mono " + (k.wow_change > 0 ? "pos" : (k.wow_change < 0 ? "neg" : ""));
      const priorTotal = k.total_vested - k.wow_change;
      document.getElementById("kpi-wow-sub").textContent = `From ${PAYLOAD.prior_bar_iso} (${fmtUSD0(priorTotal)}) to Today`;

      document.getElementById("kpi-cov").textContent = `${k.live_scraped_pct.toFixed(1)}% Live`;
      // T3: show how stale the statement-anchored values are.
      const anchorNote = (k.anchored_as_of)
        ? ` (anchors as of ${k.anchored_as_of}${k.anchored_stale_days != null ? `, ${k.anchored_stale_days}d` : ""})`
        : "";
      document.getElementById("kpi-cov-sub").textContent =
        `${fmtUSD0(k.live_scraped_val)} live quotes • ${fmtUSD0(k.anchored_val)} (${k.anchored_pct.toFixed(1)}%) statement-anchored${anchorNote}`;

      updateGrantsNavButton();
      updateGrantScreenKPIs();

      // Populate Portfolio Timeline year quick-jump buttons and dynamic preset labels
      const years = Array.from(new Set(TIMELINE.map(w => w.yearLbl))).sort();
      const minYr = years.length ? years[0] : (TODAY_ISO ? TODAY_ISO.slice(0, 4) : "2026");
      const maxYr = years.length ? years[years.length - 1] : minYr;
      const curYr = TODAY_ISO ? TODAY_ISO.slice(0, 4) : minYr;

      const ytdBtn = document.querySelector('[data-preset="ytd"]');
      if (ytdBtn) ytdBtn.textContent = `${curYr} YTD`;

      const stackBucketBtn = document.querySelector('[data-stack="bucket"]');
      if (stackBucketBtn && PAYLOAD.schemas && Array.isArray(PAYLOAD.schemas.bucket)) {
        stackBucketBtn.textContent = `Holding Bucket (${PAYLOAD.schemas.bucket.length})`;
      }
      const stackAssetBtn = document.querySelector('[data-stack="asset"]');
      if (stackAssetBtn && PAYLOAD.schemas && Array.isArray(PAYLOAD.schemas.asset)) {
        stackAssetBtn.textContent = `Asset Class (${PAYLOAD.schemas.asset.length})`;
      }
      const ssnRealBtn = document.getElementById("ssn-dlr-real-btn");
      if (ssnRealBtn) {
        ssnRealBtn.title = `Deflate future Social Security checks by the CPI inflation rate into Today's ${curYr} purchasing power (Today's $)`;
      }

      const histBtn = document.querySelector('[data-preset="historical"]');
      if (histBtn) histBtn.textContent = `Historical (${minYr}–Today)`;

      const allBtn = document.querySelector('[data-preset="all"]');
      if (allBtn) allBtn.textContent = minYr === maxYr ? `Full ${minYr}` : `Full ${minYr}–${maxYr}`;

      const titleEl = document.querySelector("#screen-portfolio .panel-title");
      if (titleEl) {
        titleEl.textContent = minYr === maxYr
          ? `Weekly Portfolio Value & Projected Stock Releases (${minYr})`
          : `Weekly Portfolio Value & Projected Stock Releases (${minYr} – ${maxYr})`;
      }

      const yWrap = document.getElementById("year-btns");
      if (yWrap && !yWrap.querySelector("[data-year]")) {
        years.forEach(yr => {
          const b = document.createElement("button");
          b.className = "btn btn-year";
          b.dataset.year = yr;
          b.textContent = yr;
          b.onclick = () => jumpToYear(yr);
          yWrap.appendChild(b);
        });
      }

      // Populate Stock Grants Releases year quick-jump buttons for active company
      rebuildRelYearJumpButtons();
      updatePortfolioForecastPresetUI();
    }

    function updateGrantScreenKPIs() {
      const comps = getGrantCompanies();
      const curC = comps.find(c => c.symbol === activeGrantSymbol) || (STOCK_GRANTS.by_symbol && STOCK_GRANTS.by_symbol[activeGrantSymbol]) || comps[0];
      const sym = curC ? curC.symbol : (activeGrantSymbol || "EQUITY");

      const availLblEl = document.getElementById("kpi-grant-avail-lbl");
      const availValEl = document.getElementById("kpi-goog");
      const availVestedEl = document.getElementById("kpi-goog-vested");
      const availSubEl = document.getElementById("kpi-goog-sub");

      const futLblEl = document.getElementById("kpi-future-lbl");
      const futValEl = document.getElementById("kpi-future");
      const futPreEl = document.getElementById("kpi-future-pretax");
      const futSubEl = document.getElementById("kpi-future-sub");

      if (!availValEl || !futValEl) return;

      if (!curC || comps.length === 0) {
        if (availLblEl) availLblEl.textContent = "Available Shares";
        availValEl.textContent = "$0";
        if (availVestedEl) availVestedEl.textContent = "No equity shares found";
        if (availSubEl) availSubEl.textContent = "0 sh in account (0.0% of portfolio)";

        if (futLblEl) futLblEl.textContent = "Future Stock Releases";
        futValEl.textContent = "$0";
        if (futPreEl) futPreEl.textContent = "No unvested equity grants scheduled";
        if (futSubEl) futSubEl.innerHTML = `<span class="muted">Future grants automatically appear once ingested</span>`;
        return;
      }

      // --- CARD 1: Available Shares ---
      if (availLblEl) availLblEl.textContent = `Available ${sym}`;

      const px = Number(curC.live_price) || (sym === PRIMARY_SYMBOL ? Number(PAYLOAD.goog_live_price) : 0) || 0;
      const rate = Number(curC.net_retention_rate) || NET_RATE;

      let availSh = curC.available_shares != null ? Number(curC.available_shares) : null;
      let availVal = curC.available_val != null ? Number(curC.available_val) : null;
      let availPct = curC.available_pct != null ? Number(curC.available_pct) : null;

      // Fallback if not directly provided on curC
      if (availSh == null || isNaN(availSh)) {
        if (PRIMARY_SYMBOL && sym === PRIMARY_SYMBOL) {
          const k = PAYLOAD.kpis || {};
          availVal = k.goog_available_val != null ? k.goog_available_val : k.goog_vested_val;
          availSh = k.goog_available_shares != null ? k.goog_available_shares : k.goog_vested_shares;
          availPct = k.goog_available_pct != null ? k.goog_available_pct : k.goog_vested_pct;
        } else {
          let foundSh = 0;
          let foundVal = 0;
          (PAYLOAD.holdings_by_account || []).forEach(acc => {
            (acc.children || []).forEach(h => {
              if ((h.symbol || "").toUpperCase() === sym) {
                const q = parseFloat((h.quantity_str || "0").replace(/[^0-9.-]/g, "")) || 0;
                const v = parseFloat((h.principal_str || "0").replace(/[^0-9.-]/g, "")) || (q * px);
                foundSh += q;
                foundVal += v;
              }
            });
          });
          availSh = foundSh;
          availVal = foundVal;
          const totNow = PAYLOAD.kpis?.total_vested || 1;
          availPct = (foundVal / totNow) * 100.0;
        }
      }

      availSh = availSh || 0;
      availVal = availVal != null ? availVal : (availSh * px);
      availPct = availPct || 0;

      const g = (STOCK_GRANTS.by_symbol && STOCK_GRANTS.by_symbol[sym]) || GSTOCK || {};
      const vestedNetSh = curC.vested_net_shares != null
        ? Number(curC.vested_net_shares)
        : (g.vested_net_shares != null
            ? Number(g.vested_net_shares)
            : (sym === PRIMARY_SYMBOL ? (PAYLOAD.kpis?.goog_vested_to_date_net_shares || availSh) : availSh));
      const vestedNetVal = curC.vested_to_date_net_val != null
        ? Number(curC.vested_to_date_net_val)
        : (g.vested_rel_val_today != null
            ? Number(g.vested_rel_val_today)
            : (vestedNetSh * px));

      const instName = curC.institution ? `${curC.institution} account` : (curC.account_id ? `account ${curC.account_id}` : "brokerage account");

      availValEl.textContent = fmtUSD0(availVal);
      if (availVestedEl) {
        availVestedEl.textContent = `Vested ${sym}: ${fmtUSD0(vestedNetVal)} (${vestedNetSh.toLocaleString("en-US", { maximumFractionDigits: 3 })} net sh vested to date)`;
      }
      if (availSubEl) {
        availSubEl.textContent = `${availSh.toLocaleString("en-US", { maximumFractionDigits: 3 })} sh in ${instName} (${availPct.toFixed(1)}% of portfolio)`;
      }

      // --- CARD 2: Future Releases ---
      const curYr = TODAY_ISO ? TODAY_ISO.slice(0, 4) : "2026";
      const symEstYears = [];
      (g.release_bars || []).forEach(b => {
        if (b.kind !== "OBSERVED" && b.yearLbl) symEstYears.push(b.yearLbl);
      });
      const minYr = symEstYears.length ? symEstYears.reduce((a, b) => (a < b ? a : b), symEstYears[0]) : curYr;
      const maxYr = symEstYears.length ? symEstYears.reduce((a, b) => (a > b ? a : b), symEstYears[0]) : curYr;
      const yrSpan = minYr === maxYr ? minYr : `${minYr}–${maxYr}`;

      if (futLblEl) {
        futLblEl.textContent = `Future ${sym} Releases (${yrSpan})`;
      }

      const futGrossSh = curC.future_gross_shares != null ? Number(curC.future_gross_shares) : (g.future_gross_shares != null ? Number(g.future_gross_shares) : 0);
      const futNetSh = curC.future_est_net_shares != null ? Number(curC.future_est_net_shares) : (g.future_est_net_shares != null ? Number(g.future_est_net_shares) : (futGrossSh * rate));
      const futGrossVal = curC.future_gross_val != null ? Number(curC.future_gross_val) : (futGrossSh * px);
      const futNetVal = curC.future_net_val != null ? Number(curC.future_net_val) : (futNetSh * px);

      // Title text: Gross (before tax) number
      futValEl.textContent = "+" + fmtUSD0(futGrossVal);

      const pxHtml = `<strong class="mono" style="font-size:13px;font-weight:800;color:#38bdf8;">${fmtUSD2(px)}</strong>`;
      // Subtext: Net (after tax) number and retention details
      if (futPreEl) {
        futPreEl.textContent = `Est. after taxes: +${fmtUSD0(futNetVal)} (${futNetSh.toLocaleString("en-US", { maximumFractionDigits: 1 })} net sh)`;
      }

      if (futSubEl) {
        futSubEl.innerHTML = `${futGrossSh.toLocaleString("en-US", { maximumFractionDigits: 1 })} gross sh @ ${pxHtml} (${(rate * 100).toFixed(1)}% est. retention)`;
      }
    }

    function updateFutureKPI() {
      updateGrantScreenKPIs();
    }

    // Updates Row 1 Box 3 ("+N <unit> Change") from first bar in window to MIN(last bar in window, Today)
    function updateWindowToTodayKPI(kpiStartIdx, kpiEndIdx) {
      const lblEl = document.getElementById("kpi-win-lbl");
      const valEl = document.getElementById("kpi-win-val");
      const subEl = document.getElementById("kpi-win-sub");
      const firstBar = TIMELINE[kpiStartIdx];
      const lastBar = TIMELINE[kpiEndIdx];
      const todayBar = TIMELINE[TODAY_IDX];
      if (!firstBar || !lastBar || !todayBar) return;

      const msPerDay = 86400000;

      if (kpiStartIdx <= TODAY_IDX) {
        const endIdx = Math.min(kpiEndIdx, TODAY_IDX);
        const endBar = TIMELINE[endIdx];
        const days = Math.max(0, Math.round((Date.parse(endBar.iso + "T00:00:00Z") - Date.parse(firstBar.iso + "T00:00:00Z")) / msPerDay));
        const startVal = firstBar.vestedTotal;
        const endVal = endBar.vestedTotal;
        const diff = endVal - startVal;
        const pct = startVal > 0 ? (diff / startVal) * 100 : 0;

        // Check if current [kpiStartIdx .. endIdx] matches the full bar span of a past calendar year
        let matchedFullPastYear = null;
        if (firstBar.yearLbl === endBar.yearLbl && firstBar.yearLbl < TODAY_ISO.slice(0, 4)) {
          const yrFirst = TIMELINE.findIndex(w => w.yearLbl === firstBar.yearLbl);
          let yrLast = -1;
          for (let i = TIMELINE.length - 1; i >= 0; i--) {
            if (TIMELINE[i].yearLbl === firstBar.yearLbl) { yrLast = i; break; }
          }
          if (kpiStartIdx === yrFirst && endIdx === yrLast) {
            matchedFullPastYear = firstBar.yearLbl;
          }
        }

        let spanLabel;
        if (activeYearJump) {
          spanLabel = activeYearJump === TODAY_ISO.slice(0, 4)
            ? `${activeYearJump} Change (YTD)`
            : `${activeYearJump} Change`;
        } else if (matchedFullPastYear) {
          spanLabel = `${matchedFullPastYear} Change`;
        } else if (isExactPresetWindow && activePreset === "ytd") {
          const mos = Math.max(1, Math.round(days / 30.4375));
          spanLabel = `${TODAY_ISO.slice(0, 4)} YTD Change (+${mos} Mo)`;
        } else if (isExactPresetWindow && activePreset === "past_month") {
          spanLabel = "+1-Month Change";
        } else if (isExactPresetWindow && activePreset === "past_year") {
          spanLabel = "+1-Year Change";
        } else if (days === 0) {
          spanLabel = "0-Day Change";
        } else if (days >= 26 && days <= 35) {
          spanLabel = "+1-Month Change";
        } else if (days >= 355 && days <= 375) {
          spanLabel = "+1-Year Change";
        } else if (days < 95) {
          const wks = Math.max(1, Math.round(days / 7));
          spanLabel = `+${wks}-Week Change`;
        } else if (days < 355) {
          const mos = Math.max(2, Math.round(days / 30.4375));
          spanLabel = `+${mos}-Month Change`;
        } else {
          const yrs = days / 365.25;
          const yrStr = Math.abs(yrs - Math.round(yrs)) < 0.08 ? String(Math.round(yrs)) : yrs.toFixed(1);
          spanLabel = `+${yrStr}-Year Change`;
        }

        lblEl.textContent = spanLabel;
        valEl.textContent = `${fmtSignedUSD0(diff)} (${pct >= 0 ? "+" : ""}${pct.toFixed(2)}%)`;
        valEl.className = "kpi-val mono " + (diff > 0 ? "pos" : (diff < 0 ? "neg" : ""));
        subEl.textContent = `From ${firstBar.iso} (${fmtUSD0(startVal)}) to ${endBar.iso} (${fmtUSD0(endVal)})`;
      } else {
        // Entire displayed window is in the future (start date > Today)
        const startTot = totalBarValue(firstBar);
        const endTot = totalBarValue(lastBar);
        const diff = endTot - startTot;
        const pct = startTot > 0 ? (diff / startTot) * 100 : 0;
        lblEl.textContent = activeYearJump ? `${activeYearJump} Projected Change` : "Projected Window Change";
        valEl.textContent = `${fmtSignedUSD0(diff)} (${pct >= 0 ? "+" : ""}${pct.toFixed(2)}%)`;
        valEl.className = "kpi-val mono " + (diff > 0 ? "pos" : (diff < 0 ? "neg" : ""));
        subEl.textContent = `From ${firstBar.iso} (${fmtUSD0(startTot)}) to ${lastBar.iso} (${fmtUSD0(endTot)})`;
      }
    }

    function isoMinusDays(baseIso, deltaDays) {
      const d = new Date(Date.parse(baseIso + "T00:00:00Z") - deltaDays * 86400000);
      return d.toISOString().slice(0, 10);
    }

    function clampViewportGeneric(s, e, totalLen, minBars) {
      const maxIdx = Math.max(0, totalLen - 1);
      if (maxIdx === 0) return [0, 0];
      const minSpan = Math.min(maxIdx, Math.max(0, minBars - 1));
      let span = Math.max(minSpan, Math.min(maxIdx, e - s));
      let ns = s;
      let ne = s + span;
      if (ns < 0) { ns = 0; ne = span; }
      if (ne > maxIdx) { ne = maxIdx; ns = Math.max(0, maxIdx - span); }
      return [ns, ne];
    }

    function clampViewport(s, e) {
      return clampViewportGeneric(s, e, TIMELINE.length, MIN_VISIBLE_BARS);
    }

    function clampRelViewport(s, e) {
      return clampViewportGeneric(s, e, REL_BARS.length, MIN_REL_VISIBLE_BARS);
    }

    function presetBounds(preset) {
      const last = Math.max(0, TIMELINE.length - 1);
      if (preset === "past_month") {
        const target = isoMinusDays(TODAY_ISO, 31);
        const idx = TIMELINE.findIndex(w => w.iso >= target);
        const startIdx = idx >= 0 ? Math.min(idx, Math.max(0, TODAY_IDX - (MIN_VISIBLE_BARS - 1))) : Math.max(0, TODAY_IDX - 4);
        return [startIdx, TODAY_IDX];
      }
      if (preset === "recent") return [Math.max(0, TODAY_IDX - 12), TODAY_IDX];
      if (preset === "ytd") {
        const curYr = TODAY_ISO.slice(0, 4);
        const idx = TIMELINE.findIndex(w => w.iso.startsWith(curYr));
        return [idx >= 0 ? idx : 0, TODAY_IDX];
      }
      if (preset === "past_year") {
        const target = isoMinusDays(TODAY_ISO, 365);
        const idx = TIMELINE.findIndex(w => w.iso >= target);
        return [idx >= 0 ? idx : 0, TODAY_IDX];
      }
      if (preset === "historical") return [0, TODAY_IDX];
      if (preset === "future") return [TODAY_IDX, last];
      return [0, last];
    }

    function applyPreset(preset) {
      activePreset = preset;
      activeYearJump = null;
      isExactPresetWindow = true;
      document.querySelectorAll("[data-preset]").forEach(b => b.classList.toggle("active", b.dataset.preset === preset));
      document.querySelectorAll("[data-year]").forEach(b => b.classList.remove("active"));
      const [s, e] = presetBounds(preset);
      [viewStart, viewEnd] = clampViewport(s, e);
      renderChartAndNav();
    }

    function jumpToYear(yr) {
      activePreset = null;
      activeYearJump = yr;
      isExactPresetWindow = false;
      document.querySelectorAll("[data-preset]").forEach(b => b.classList.remove("active"));
      document.querySelectorAll("[data-year]").forEach(b => b.classList.toggle("active", b.dataset.year === yr));
      const first = TIMELINE.findIndex(w => w.yearLbl === yr);
      let last = -1;
      for (let i = TIMELINE.length - 1; i >= 0; i--) {
        if (TIMELINE[i].yearLbl === yr) { last = i; break; }
      }
      if (first >= 0 && last >= first) {
        [viewStart, viewEnd] = clampViewport(first, last);
        renderChartAndNav();
      }
    }

    function resetZoom() { applyPreset(activePreset || "all"); }

    function setStackMode(mode) {
      stackMode = mode;
      document.querySelectorAll("[data-stack]").forEach(b => b.classList.toggle("active", b.dataset.stack === mode));
      renderChartAndNav();
    }

    function setTaxMode(mode) {
      taxMode = mode;
      document.querySelectorAll("[data-tax]").forEach(b => b.classList.toggle("active", b.dataset.tax === mode));
      updateFutureKPI();
      renderChartAndNav();
      checkSaveDirtyState();
    }

    const CHART_GEOM = { W: 1160, H: 340, padL: 76, padR: 22, padT: 20, padB: 42 };
    const NAV_GEOM = { W: 1160, H: 64, padL: 76, padR: 22, padT: 5, padB: 20 };

    function niceAxisMax(rawMax, steps = 5) {
      if (rawMax <= 0) return 100000;
      const padded = rawMax * 1.08;
      const roughStep = padded / steps;
      const mag = Math.pow(10, Math.floor(Math.log10(roughStep)));
      const norm = roughStep / mag;
      let niceNorm = 10;
      if (norm <= 1) niceNorm = 1;
      else if (norm <= 2) niceNorm = 2;
      else if (norm <= 2.5) niceNorm = 2.5;
      else if (norm <= 4) niceNorm = 4;
      else if (norm <= 5) niceNorm = 5;
      return niceNorm * mag * steps;
    }

    function fmtAxisUSD(v) {
      if (v === 0) return "$0";
      if (v >= 1000000) {
        const m = v / 1e6;
        return "$" + (Math.abs(m * 10 - Math.round(m * 10)) < 1e-4 ? m.toFixed(1) : m.toFixed(2)) + "M";
      }
      return "$" + Math.round(v / 1000).toLocaleString("en-US") + "k";
    }

    function renderChartAndNav() {
      const svg = document.getElementById("weekly-svg");
      if (!TIMELINE.length) {
        svg.innerHTML = `<text x="580" y="170" fill="#94a3b8" text-anchor="middle" font-size="14">No timeline bars in database.</text>`;
        return;
      }

      const iStart = Math.max(0, Math.floor(viewStart));
      const iEnd = Math.min(TIMELINE.length - 1, Math.ceil(viewEnd));
      const visible = TIMELINE.slice(iStart, iEnd + 1);
      const n = visible.length;
      const spanSlots = Math.max(1, (viewEnd - viewStart) + 1);
      const approxN = Math.round(spanSlots);

      const { W, H, padL, padR, padT, padB } = CHART_GEOM;
      const plotW = W - padL - padR;
      const plotH = H - padT - padB;
      const axisY = padT + plotH;

      const series = seriesForCurrentStack();
      window._currentSeries = series;
      window._barGeom = {};

      const activeBurnToday = (typeof simAnnualBurn === "number" && Number.isFinite(simAnnualBurn)) ? simAnnualBurn : RETIREMENT_ANNUAL_BURN;
      const activeInfPct = (typeof simInflationPct === "number" && Number.isFinite(simInflationPct)) ? simInflationPct : RETIREMENT_INFLATION_REF;
      const goalValForBar = w => {
        const baseGoal = activeBurnToday * RETIREMENT_GOAL_YEARS;
        if (!w || !w.iso || w.iso <= TODAY_ISO) return baseGoal;
        const yrsAhead = (Date.parse(w.iso + "T00:00:00Z") - Date.parse(TODAY_ISO + "T00:00:00Z")) / (365.25 * 86400000);
        return yrsAhead > 0 ? baseGoal * Math.pow(1.0 + activeInfPct / 100.0, yrsAhead) : baseGoal;
      };

      let maxVal = activeBurnToday * RETIREMENT_GOAL_YEARS;
      visible.forEach(w => {
        const t = Math.max(totalBarValue(w), goalValForBar(w));
        if (t > maxVal) maxVal = t;
      });
      const yMax = Math.ceil((maxVal * 1.08) / 2000000) * 2000000;

      const kpiStartIdx = Math.min(TIMELINE.length - 1, Math.max(0, Math.round(viewStart)));
      const kpiEndIdx = Math.min(TIMELINE.length - 1, Math.max(kpiStartIdx, Math.round(viewEnd)));
      updateWindowToTodayKPI(kpiStartIdx, kpiEndIdx);

      let parts = [];
      parts.push(`<defs>
        <pattern id="gsu-hatch" width="7" height="7" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
          <rect width="7" height="7" fill="#065f46"></rect>
          <line x1="0" y1="0" x2="0" y2="7" stroke="#34d399" stroke-width="3"></line>
        </pattern>
        <clipPath id="weekly-plot-clip">
          <rect x="${padL}" y="0" width="${plotW}" height="${H}"></rect>
        </clipPath>
      </defs>`);

      const ySteps = 5;
      for (let s = 0; s <= ySteps; s++) {
        const v = (yMax / ySteps) * s;
        const y = padT + plotH - (v / yMax) * plotH;
        parts.push(`<line x1="${padL}" y1="${y.toFixed(1)}" x2="${W - padR}" y2="${y.toFixed(1)}" stroke="${s === 0 ? '#334155' : '#1e293b'}" stroke-width="1"/>`);
        parts.push(`<text x="${padL - 8}" y="${(y + 4).toFixed(1)}" text-anchor="end" fill="#cbd5e1" font-size="11.5" font-family="ui-monospace, monospace">$${(v / 1e6).toFixed(1)}M</text>`);
      }

      const slotW = plotW / spanSlots;
      const barW = Math.max(1.4, Math.min(34, slotW * (approxN > 140 ? 0.86 : 0.76)));
      const labelStride = Math.max(1, Math.ceil(approxN / 13));

      let clipped = [];
      clipped.push(`<rect id="focus-slot-bg" x="0" y="${padT}" width="0" height="${plotH}" fill="rgba(56, 189, 248, 0.14)" display="none" pointer-events="none"/>`);

      let todayX = null;
      let hitRects = [];

      visible.forEach((w, localIdx) => {
        const globalIdx = iStart + localIdx;
        const isAfterToday = (TODAY_IDX >= 0 ? globalIdx > TODAY_IDX : w.kind === "FORECAST") && !w.isToday;
        const barOpacity = isAfterToday ? "0.60" : "0.95";
        const slotX = padL + (globalIdx - viewStart) * slotW;
        const cx = slotX + 0.5 * slotW;
        const bx = cx - barW / 2;
        let curY = axisY;

        series.forEach(s => {
          const val = s.valFn(w);
          if (val <= 0) return;
          const h = (val / yMax) * plotH;
          curY -= h;
          clipped.push(`<rect x="${bx.toFixed(2)}" y="${curY.toFixed(2)}" width="${barW.toFixed(2)}" height="${Math.max(0.6, h).toFixed(2)}" fill="${s.color}" opacity="${barOpacity}"/>`);
        });

        const relVal = projectedReleaseValue(w);
        if (relVal > 0) {
          const rh = (relVal / yMax) * plotH;
          curY -= rh;
          clipped.push(`<rect x="${bx.toFixed(2)}" y="${curY.toFixed(2)}" width="${barW.toFixed(2)}" height="${Math.max(0.8, rh).toFixed(2)}" fill="url(#gsu-hatch)" stroke="#34d399" stroke-width="${barW > 5 ? 0.7 : 0.3}"${isAfterToday ? ' opacity="0.60"' : ''}/>`);
        }

        const barH = Math.max(1, axisY - curY);
        window._barGeom[globalIdx] = { slotX, slotW, bx, topY: curY, barW, barH, cx };

        if (w.isToday) {
          todayX = cx;
          clipped.push(`<rect x="${(bx - 1).toFixed(2)}" y="${curY.toFixed(2)}" width="${(barW + 2).toFixed(2)}" height="${barH.toFixed(2)}" fill="none" stroke="#f8fafc" stroke-width="1.3"/>`);
        }

        if (globalIdx % labelStride === 0 && cx >= padL + 22 && cx <= W - padR - 22) {
          const lbl = approxN > 70 ? w.monthLbl : w.shortLbl;
          clipped.push(`<text x="${cx.toFixed(1)}" y="${H - 14}" text-anchor="middle" fill="#cbd5e1" font-weight="500" font-size="11.5" font-family="ui-monospace, monospace">${escHtml(lbl)}</text>`);
        }

        hitRects.push(`<rect x="${slotX.toFixed(2)}" y="${padT}" width="${slotW.toFixed(2)}" height="${plotH}" fill="transparent" onmousemove="showTooltip(event, ${globalIdx})" onmouseleave="hideTooltip()"/>`);
      });

      // Unlabeled Dec 31 year-boundary tick marks (moving continuously as you pan & zoom)
      for (let k = Math.max(0, iStart - 1); k < Math.min(TIMELINE.length - 1, iEnd + 1); k++) {
        if (TIMELINE[k].yearLbl !== TIMELINE[k + 1].yearLbl) {
          const tickX = padL + ((k + 1) - viewStart) * slotW;
          if (tickX >= padL - 1 && tickX <= W - padR + 1) {
            clipped.push(`<line x1="${tickX.toFixed(2)}" y1="${(axisY - 7).toFixed(1)}" x2="${tickX.toFixed(2)}" y2="${(axisY + 10).toFixed(1)}" stroke="#e2e8f0" stroke-width="2" stroke-linecap="round" pointer-events="none"/>`);
          }
        }
      }

      if (todayX !== null && todayX >= padL && todayX <= W - padR) {
        clipped.push(`<line x1="${todayX.toFixed(1)}" y1="${padT}" x2="${todayX.toFixed(1)}" y2="${axisY}" stroke="#38bdf8" stroke-width="1.4" stroke-dasharray="4,3" pointer-events="none"/>`);
        const tagX = Math.min(W - padR - 52, Math.max(padL + 2, todayX - 26));
        clipped.push(`<rect x="${tagX.toFixed(1)}" y="${padT + 2}" width="52" height="17" rx="4" fill="#0f172a" stroke="#38bdf8" stroke-width="1" pointer-events="none"/>`);
        clipped.push(`<text x="${(tagX + 26).toFixed(1)}" y="${padT + 14}" text-anchor="middle" fill="#7dd3fc" font-size="10" font-weight="700" pointer-events="none">TODAY</text>`);
      }

      // 30-Year Retirement Goal Curve (Today's $ up to TODAY, inflation-indexed in Nominal $ across future forecast bars — DA-A06)
      const goalPts = [];
      visible.forEach((w, localIdx) => {
        const globalIdx = iStart + localIdx;
        const cx = padL + (globalIdx - viewStart + 0.5) * slotW;
        const gv = goalValForBar(w);
        const gy = padT + plotH - (gv / yMax) * plotH;
        goalPts.push({ x: cx, y: gy, v: gv });
      });
      if (goalPts.length > 0) {
        const firstY = goalPts[0].y;
        const lastY = goalPts[goalPts.length - 1].y;
        const polyPts = [`${padL.toFixed(1)},${firstY.toFixed(1)}`, ...goalPts.map(p => `${p.x.toFixed(1)},${p.y.toFixed(1)}`), `${(W - padR).toFixed(1)},${lastY.toFixed(1)}`].join(" ");
        if (lastY >= padT && firstY <= axisY) {
          clipped.push(`<polyline points="${polyPts}" fill="none" stroke="#0f172a" stroke-width="4" opacity="0.65" pointer-events="none"/>`);
          clipped.push(`<polyline points="${polyPts}" fill="none" stroke="#2dd4bf" stroke-width="2" stroke-dasharray="7,4" pointer-events="none"/>`);
          const goalMillions = (activeBurnToday * RETIREMENT_GOAL_YEARS) / 1e6;
          const goalStr = "$" + (Math.abs(goalMillions * 10 - Math.round(goalMillions * 10)) < 1e-4 ? goalMillions.toFixed(1) : goalMillions.toFixed(2)) + "M";
          const goalLabel = `${RETIREMENT_GOAL_YEARS}-Yr Goal: ${goalStr} (Today's $)`;
          const gTagW = Math.max(156, Math.ceil(goalLabel.length * 6.4 + 18));
          const gTagX = Math.max(padL + 4, W - padR - gTagW - 6);
          const gTagY = Math.max(padT + 2, lastY - 19);
          clipped.push(`<rect x="${gTagX.toFixed(1)}" y="${gTagY.toFixed(1)}" width="${gTagW}" height="17" rx="4" fill="rgba(15, 23, 42, 0.92)" stroke="#2dd4bf" stroke-width="1" pointer-events="none"/>`);
          clipped.push(`<text x="${(gTagX + gTagW / 2).toFixed(1)}" y="${(gTagY + 12).toFixed(1)}" text-anchor="middle" fill="#5eead4" font-size="10" font-weight="700" font-family="ui-monospace, monospace" pointer-events="none">${goalLabel}</text>`);
        }
      }

      clipped.push(`<rect id="focus-bar-ring" x="0" y="0" width="0" height="0" rx="1.5" fill="rgba(255, 255, 255, 0.14)" stroke="#ffffff" stroke-width="2" display="none" pointer-events="none"/>`);
      clipped.push(`<circle id="focus-bar-dot" cx="0" cy="0" r="3.5" fill="#38bdf8" stroke="#ffffff" stroke-width="1.4" display="none" pointer-events="none"/>`);
      clipped.push(...hitRects);

      parts.push(`<g clip-path="url(#weekly-plot-clip)">${clipped.join("")}</g>`);

      svg.innerHTML = parts.join("");
      renderNavigator();
      renderLegend(series);
    }

    function renderNavigator() {
      const nav = document.getElementById("nav-svg");
      const totalN = TIMELINE.length;
      if (!totalN) return;

      const { W, H, padL, padR, padT, padB } = NAV_GEOM;
      const plotW = W - padL - padR;
      const plotH = H - padT - padB;

      let maxAll = 1;
      TIMELINE.forEach(w => {
        const t = totalBarValue(w);
        if (t > maxAll) maxAll = t;
      });

      let vestedPts = [`${padL},${padT + plotH}`];
      let totalPts = [`${padL},${padT + plotH}`];

      TIMELINE.forEach((w, i) => {
        const x = padL + (i / Math.max(1, totalN - 1)) * plotW;
        const yV = padT + plotH - (vestedBarValue(w) / (maxAll * 1.05)) * plotH;
        const yT = padT + plotH - (totalBarValue(w) / (maxAll * 1.05)) * plotH;
        vestedPts.push(`${x.toFixed(1)},${yV.toFixed(1)}`);
        totalPts.push(`${x.toFixed(1)},${yT.toFixed(1)}`);
      });
      vestedPts.push(`${padL + plotW},${padT + plotH}`);
      totalPts.push(`${padL + plotW},${padT + plotH}`);

      const xFromIdx = idx => padL + (idx / Math.max(1, totalN - 1)) * plotW;
      const winX1 = xFromIdx(viewStart);
      const winX2 = xFromIdx(viewEnd);
      const winW = Math.max(8, winX2 - winX1);
      const todayNavX = xFromIdx(TODAY_IDX);

      let out = [];
      out.push(`<rect x="${padL}" y="${padT}" width="${plotW}" height="${plotH}" rx="4" fill="#0b1324" stroke="#1e293b"/>`);
      out.push(`<polygon points="${totalPts.join(' ')}" fill="rgba(16, 185, 129, 0.36)"/>`);
      out.push(`<polygon points="${vestedPts.join(' ')}" fill="rgba(56, 189, 248, 0.34)"/>`);

      let prevYr = "";
      TIMELINE.forEach((w, i) => {
        if (w.yearLbl !== prevYr) {
          prevYr = w.yearLbl;
          const x = xFromIdx(i);
          out.push(`<line x1="${x.toFixed(1)}" y1="${padT}" x2="${x.toFixed(1)}" y2="${padT + plotH}" stroke="rgba(148,163,184,0.45)" stroke-width="1.2"/>`);
          out.push(`<text x="${(x + 5).toFixed(1)}" y="${H - 5}" fill="#f1f5f9" font-size="12" font-weight="700" font-family="ui-monospace, monospace">${w.yearLbl}</text>`);
        }
      });

      out.push(`<line x1="${todayNavX.toFixed(1)}" y1="${padT}" x2="${todayNavX.toFixed(1)}" y2="${padT + plotH}" stroke="#38bdf8" stroke-width="1.8" stroke-dasharray="2,2"/>`);

      out.push(`<rect x="${padL}" y="${padT}" width="${Math.max(0, winX1 - padL).toFixed(1)}" height="${plotH}" fill="rgba(2, 6, 23, 0.58)" style="cursor:pointer;" data-nav-action="jump"/>`);
      out.push(`<rect x="${(winX1 + winW).toFixed(1)}" y="${padT}" width="${Math.max(0, padL + plotW - (winX1 + winW)).toFixed(1)}" height="${plotH}" fill="rgba(2, 6, 23, 0.58)" style="cursor:pointer;" data-nav-action="jump"/>`);

      out.push(`<rect x="${winX1.toFixed(1)}" y="${padT}" width="${winW.toFixed(1)}" height="${plotH}" fill="rgba(56, 189, 248, 0.16)" stroke="#38bdf8" stroke-width="1.4" style="cursor:grab;" data-nav-action="move"/>`);
      out.push(`<rect x="${(winX1 - 4).toFixed(1)}" y="${padT + 6}" width="8" height="${plotH - 12}" rx="3" fill="#7dd3fc" style="cursor:ew-resize;" data-nav-action="left"/>`);
      out.push(`<rect x="${(winX1 + winW - 4).toFixed(1)}" y="${padT + 6}" width="8" height="${plotH - 12}" rx="3" fill="#7dd3fc" style="cursor:ew-resize;" data-nav-action="right"/>`);

      nav.innerHTML = out.join("");

      const sIdx = Math.max(0, Math.floor(viewStart));
      const eIdx = Math.min(totalN - 1, Math.ceil(viewEnd));
      document.getElementById("nav-range-text").textContent =
        `${TIMELINE[sIdx].iso} → ${TIMELINE[eIdx].iso}`;
    }

    function renderLegend(series) {
      const leg = document.getElementById("chart-legend");
      const items = series.map(s =>
        `<span class="legend-item"><span class="swatch" style="background:${s.color};"></span>${escHtml(s.label)}</span>`
      );
      const relSymLbl = PRIMARY_SYMBOL || "Equity";
      const relLabel = taxMode === "net"
        ? `Projected ${relSymLbl} Releases (Future Only • Est. Net ${(NET_RATE * 100).toFixed(1)}%)`
        : `Projected ${relSymLbl} Releases (Future Only • Gross 100%)`;
      items.push(`<span class="legend-item"><span class="swatch swatch-hatch"></span><strong>${escHtml(relLabel)}</strong></span>`);
      const goalMillions = RETIREMENT_GOAL_VAL / 1e6;
      const goalStr = "$" + (Math.abs(goalMillions * 10 - Math.round(goalMillions * 10)) < 1e-4 ? goalMillions.toFixed(1) : goalMillions.toFixed(2)) + "M";
      const burnValK = RETIREMENT_ANNUAL_BURN / 1000;
      const burnKStr = (burnValK % 1 === 0 ? burnValK.toFixed(0) : burnValK.toFixed(1));
      const burnK = "$" + burnKStr + "k/yr";
      items.push(`<span class="legend-item"><span style="display:inline-block;width:16px;height:0;border-top:2px dashed #2dd4bf;vertical-align:middle;margin-right:5px;"></span><strong style="color:#5eead4;">${RETIREMENT_GOAL_YEARS}-Yr Retirement Goal (${goalStr} @ ${burnK})</strong></span>`);
      leg.innerHTML = items.join("");
    }

    function highlightBarInSvg(geomMap, idx, prefix = "") {
      const g = geomMap && geomMap[idx];
      const slotEl = document.getElementById(prefix + "focus-slot-bg");
      const ringEl = document.getElementById(prefix + "focus-bar-ring");
      const dotEl = document.getElementById(prefix + "focus-bar-dot");
      if (!g || !slotEl || !ringEl || !dotEl) return;

      slotEl.setAttribute("x", g.slotX.toFixed(2));
      slotEl.setAttribute("width", g.slotW.toFixed(2));
      slotEl.setAttribute("display", "inline");

      const pad = Math.min(1.5, g.barW * 0.18);
      ringEl.setAttribute("x", (g.bx - pad).toFixed(2));
      ringEl.setAttribute("y", g.topY.toFixed(2));
      ringEl.setAttribute("width", (g.barW + pad * 2).toFixed(2));
      ringEl.setAttribute("height", g.barH.toFixed(2));
      ringEl.setAttribute("display", "inline");

      dotEl.setAttribute("cx", g.cx.toFixed(2));
      dotEl.setAttribute("cy", Math.max(6, g.topY - 5).toFixed(2));
      dotEl.setAttribute("display", "inline");
    }

    function showTooltip(evt, idx) {
      const w = TIMELINE[idx];
      if (!w) return;
      highlightBarInSvg(window._barGeom, idx, "");

      const tip = document.getElementById("chart-tooltip");
      const tot = totalBarValue(w);
      const prev = idx > 0 ? totalBarValue(TIMELINE[idx - 1]) : null;
      const deltaStr = prev !== null && prev > 0
        ? `<div class="muted" style="margin-bottom:6px;">1W Change: <strong class="${tot >= prev ? 'pos' : 'neg'}">${fmtSignedUSD0(tot - prev)} (${(((tot - prev) / prev) * 100).toFixed(2)}%)</strong></div>`
        : `<div class="muted" style="margin-bottom:6px;">Initial timeline bar</div>`;

      const badgeCls = w.kind === "FORECAST" ? "pill-green" : (w.kind === "TODAY" || w.kind === "OBSERVED" ? "pill-blue" : "pill-cyan");
      const primFc = resolveHoldingForecast(PRIMARY_SYMBOL, [], futureGoogGrowthPct, "single_stock").growth_pct;
      const wtdFc = computeWeightedPortfolioForecastPct();
      const projGoogNote = (w.kind === "FORECAST" && Math.abs(primFc) > 1e-6)
        ? `<div class="mono" style="font-size:11.2px;color:#7dd3fc;margin-bottom:2px;">Proj. ${escHtml(PRIMARY_SYMBOL)}: ${fmtUSD2(PAYLOAD.goog_live_price * googFutureMultiplier(w))}/sh (${primFc > 0 ? "+" : ""}${primFc.toFixed(1)}%/yr)</div>`
        : "";
      const projNonGoogNote = (w.kind === "FORECAST" && Math.abs(wtdFc) > 1e-6)
        ? `<div class="mono" style="font-size:11.2px;color:#a7f3d0;margin-bottom:4px;">Weighted Portfolio Forecast: ${wtdFc > 0 ? "+" : ""}${wtdFc.toFixed(1)}%/yr</div>`
        : "";
      const series = window._currentSeries || [];
      let rows = [];

      const relVal = projectedReleaseValue(w);
      if (relVal > 0) {
        const shShown = taxMode === "net" ? w.projRelShares * NET_RATE : w.projRelShares;
        const pct = ((relVal / tot) * 100).toFixed(1);
        rows.push(`<div style="display:flex;justify-content:space-between;gap:12px;margin:2px 0;color:#6ee7b7;">
          <span><span class="swatch swatch-hatch" style="width:9px;height:9px;margin-right:5px;"></span>${escHtml(PRIMARY_SYMBOL || "Equity")} Released (+${shShown.toFixed(1)} sh)</span>
          <span class="mono">${fmtUSD0(relVal)} (${pct}%)</span>
        </div>`);
      }

      series.slice().reverse().forEach(s => {
        const v = s.valFn(w);
        if (v <= 0) return;
        const pct = ((v / tot) * 100).toFixed(1);
        rows.push(`<div style="display:flex;justify-content:space-between;gap:12px;margin:2px 0;">
          <span><span class="swatch" style="background:${s.color};width:9px;height:9px;margin-right:5px;"></span>${escHtml(s.label)}</span>
          <span class="mono">${fmtUSD0(v)} <span class="muted">(${pct}%)</span></span>
        </div>`);
      });

      tip.innerHTML = `
        <div style="display:flex;justify-content:space-between;align-items:center;gap:10px;">
          <strong class="mono">${escHtml(w.iso)}</strong>
          <span class="pill ${badgeCls}">${escHtml(w.kind)}</span>
        </div>
        <div class="mono" style="font-size:16px;font-weight:700;margin:3px 0;">${fmtUSD2(tot)}</div>
        ${projGoogNote}
        ${projNonGoogNote}
        ${deltaStr}
        <div style="border-top:1px solid #26334d;padding-top:5px;">${rows.join("")}</div>
      `;

      placeFloatingBoxAwayFromPointer(tip, evt.clientX, evt.clientY);
    }

    function hideTooltip() {
      const tipEl = document.getElementById("chart-tooltip");
      if (tipEl) tipEl.style.display = "none";
      ["", "rel-", "sim-", "ssn-"].forEach(prefix => {
        const slotEl = document.getElementById(prefix + "focus-slot-bg");
        const ringEl = document.getElementById(prefix + "focus-bar-ring");
        const dotEl = document.getElementById(prefix + "focus-bar-dot");
        if (slotEl) slotEl.setAttribute("display", "none");
        if (ringEl) ringEl.setAttribute("display", "none");
        if (dotEl) dotEl.setAttribute("display", "none");
      });
    }

    // =========================================================================
    // STOCK VESTING SCREEN: RELEASES CHART & TABLES
    // =========================================================================

    function setPastReleaseValMode(mode) {
      pastReleaseValMode = mode === "today" ? "today" : "release";
      document.querySelectorAll("[data-past-val]").forEach(b =>
        b.classList.toggle("active", b.dataset.pastVal === pastReleaseValMode)
      );
      renderRelChartAndNav();
      renderGoogleStockTables();
      checkSaveDirtyState();
    }

    function relGrantNetVal(bar, grantId) {
      const gb = bar.byGrant && bar.byGrant[grantId];
      if (!gb) return 0;
      if (bar.kind === "OBSERVED") {
        return pastReleaseValMode === "today" ? gb.val_today_net : gb.val_rel_net;
      }
      return gb.val_today_net;
    }

    function relBarTotalNetVal(bar) {
      if (!bar) return 0;
      if (bar.kind === "OBSERVED") {
        return pastReleaseValMode === "today" ? bar.totTodayNetVal : bar.totRelNetVal;
      }
      return bar.totTodayNetVal;
    }

    function relBarTotalGrossVal(bar) {
      if (!bar) return 0;
      if (bar.kind === "OBSERVED") {
        return pastReleaseValMode === "today" ? bar.totTodayGrossVal : bar.totRelGrossVal;
      }
      return bar.totTodayGrossVal;
    }

    function relPresetBounds(preset) {
      const last = Math.max(0, REL_BARS.length - 1);
      const endObs = Math.max(0, REL_LAST_OBS_IDX);
      if (preset === "past_month") {
        const target = isoMinusDays(TODAY_ISO, 45);
        const idx = REL_BARS.findIndex(b => b.iso >= target && b.iso <= TODAY_ISO);
        const s = idx >= 0 ? Math.min(idx, Math.max(0, endObs - (MIN_REL_VISIBLE_BARS - 1))) : Math.max(0, endObs - 1);
        return [s, endObs];
      }
      if (preset === "ytd") {
        const curYr = TODAY_ISO.slice(0, 4);
        const idx = REL_BARS.findIndex(b => b.iso.startsWith(curYr));
        return [idx >= 0 ? idx : 0, endObs];
      }
      if (preset === "past_year") {
        const target = isoMinusDays(TODAY_ISO, 370);
        const idx = REL_BARS.findIndex(b => b.iso >= target);
        return [idx >= 0 ? idx : 0, endObs];
      }
      if (preset === "historical") return [0, endObs];
      return [0, last];
    }

    function applyRelPreset(preset) {
      relActivePreset = preset;
      relActiveYearJump = null;
      document.querySelectorAll("[data-rel-preset]").forEach(b =>
        b.classList.toggle("active", b.dataset.relPreset === preset)
      );
      document.querySelectorAll("[data-rel-year]").forEach(b => b.classList.remove("active"));
      const [s, e] = relPresetBounds(preset);
      [relViewStart, relViewEnd] = clampRelViewport(s, e);
      renderRelChartAndNav();
    }

    function jumpRelToYear(yr) {
      relActivePreset = null;
      relActiveYearJump = yr;
      document.querySelectorAll("[data-rel-preset]").forEach(b => b.classList.remove("active"));
      document.querySelectorAll("[data-rel-year]").forEach(b =>
        b.classList.toggle("active", b.dataset.relYear === yr)
      );
      const first = REL_BARS.findIndex(b => b.yearLbl === yr);
      let last = -1;
      for (let i = REL_BARS.length - 1; i >= 0; i--) {
        if (REL_BARS[i].yearLbl === yr) { last = i; break; }
      }
      if (first >= 0 && last >= first) {
        [relViewStart, relViewEnd] = clampRelViewport(first, last);
        renderRelChartAndNav();
      }
    }

    function renderRelChartAndNav() {
      const svg = document.getElementById("releases-svg");
      if (!svg) return;
      if (!REL_BARS.length) {
        svg.innerHTML = `<text x="580" y="170" fill="#94a3b8" text-anchor="middle" font-size="14">No grant release records in database.</text>`;
        return;
      }

      const iStart = Math.max(0, Math.floor(relViewStart));
      const iEnd = Math.min(REL_BARS.length - 1, Math.ceil(relViewEnd));
      const visible = REL_BARS.slice(iStart, iEnd + 1);
      const spanSlots = Math.max(1, (relViewEnd - relViewStart) + 1);
      const approxN = Math.round(spanSlots);

      const { W, H, padL, padR, padT, padB } = CHART_GEOM;
      const plotW = W - padL - padR;
      const plotH = H - padT - padB;
      const axisY = padT + plotH;

      window._relBarGeom = {};

      let maxVal = 1;
      visible.forEach(b => {
        const t = relBarTotalNetVal(b);
        if (t > maxVal) maxVal = t;
      });
      const ySteps = 5;
      const yMax = niceAxisMax(maxVal, ySteps);

      let parts = [];
      parts.push(`<defs>
        <clipPath id="rel-plot-clip">
          <rect x="${padL}" y="0" width="${plotW}" height="${H}"></rect>
        </clipPath>
      </defs>`);

      for (let s = 0; s <= ySteps; s++) {
        const v = (yMax / ySteps) * s;
        const y = padT + plotH - (v / yMax) * plotH;
        parts.push(`<line x1="${padL}" y1="${y.toFixed(1)}" x2="${W - padR}" y2="${y.toFixed(1)}" stroke="${s === 0 ? '#334155' : '#1e293b'}" stroke-width="1"/>`);
        parts.push(`<text x="${padL - 8}" y="${(y + 4).toFixed(1)}" text-anchor="end" fill="#94a3b8" font-size="11" font-family="ui-monospace, monospace">${fmtAxisUSD(v)}</text>`);
      }

      const slotW = plotW / spanSlots;
      const barW = Math.max(2.2, Math.min(44, slotW * 0.74));
      const labelStride = Math.max(1, Math.ceil(approxN / 13));

      let clipped = [];
      clipped.push(`<rect id="rel-focus-slot-bg" x="0" y="${padT}" width="0" height="${plotH}" fill="rgba(56, 189, 248, 0.14)" display="none" pointer-events="none"/>`);

      let todaySplitX = null;
      let hitRects = [];

      visible.forEach((b, localIdx) => {
        const globalIdx = iStart + localIdx;
        const slotX = padL + (globalIdx - relViewStart) * slotW;
        const cx = slotX + 0.5 * slotW;
        const bx = cx - barW / 2;
        let curY = axisY;

        const isEst = b.kind === "ESTIMATED";
        GRANT_SERIES.forEach(gs => {
          const val = relGrantNetVal(b, gs.id);
          if (val <= 0) return;
          const h = (val / yMax) * plotH;
          curY -= h;
          clipped.push(`<rect x="${bx.toFixed(2)}" y="${curY.toFixed(2)}" width="${barW.toFixed(2)}" height="${Math.max(0.7, h).toFixed(2)}" fill="${gs.color}" opacity="${isEst ? '0.82' : '0.96'}"/>`);
        });

        const barH = Math.max(1, axisY - curY);
        if (isEst && barH > 2) {
          clipped.push(`<rect x="${bx.toFixed(2)}" y="${curY.toFixed(2)}" width="${barW.toFixed(2)}" height="${barH.toFixed(2)}" fill="none" stroke="rgba(226,232,240,0.42)" stroke-width="0.8" stroke-dasharray="2,2" pointer-events="none"/>`);
        }

        window._relBarGeom[globalIdx] = { slotX, slotW, bx, topY: curY, barW, barH, cx };

        if (globalIdx === REL_LAST_OBS_IDX && globalIdx < REL_BARS.length - 1) {
          todaySplitX = slotX + slotW;
        }

        if (globalIdx % labelStride === 0 && cx >= padL + 22 && cx <= W - padR - 22) {
          const lbl = approxN > 28 ? b.monthLbl : b.shortLbl;
          const col = isEst ? "#6ee7b7" : "#94a3b8";
          clipped.push(`<text x="${cx.toFixed(1)}" y="${H - 15}" text-anchor="middle" fill="${col}" font-size="10.8" font-family="ui-monospace, monospace">${escHtml(lbl)}</text>`);
        }

        hitRects.push(`<rect x="${slotX.toFixed(2)}" y="${padT}" width="${slotW.toFixed(2)}" height="${plotH}" fill="transparent" onmousemove="showReleaseTooltip(event, ${globalIdx})" onmouseleave="hideTooltip()"/>`);
      });

      // Unlabeled Dec 31 year-boundary tick marks (moving continuously as you pan & zoom)
      for (let k = Math.max(0, iStart - 1); k < Math.min(REL_BARS.length - 1, iEnd + 1); k++) {
        if (REL_BARS[k].yearLbl !== REL_BARS[k + 1].yearLbl) {
          const tickX = padL + ((k + 1) - relViewStart) * slotW;
          if (tickX >= padL - 1 && tickX <= W - padR + 1) {
            clipped.push(`<line x1="${tickX.toFixed(2)}" y1="${(axisY - 7).toFixed(1)}" x2="${tickX.toFixed(2)}" y2="${(axisY + 10).toFixed(1)}" stroke="#e2e8f0" stroke-width="2" stroke-linecap="round" pointer-events="none"/>`);
          }
        }
      }

      if (todaySplitX !== null && todaySplitX >= padL && todaySplitX <= W - padR) {
        clipped.push(`<line x1="${todaySplitX.toFixed(1)}" y1="${padT}" x2="${todaySplitX.toFixed(1)}" y2="${axisY}" stroke="#38bdf8" stroke-width="1.5" stroke-dasharray="4,3" pointer-events="none"/>`);
        const tagX = Math.min(W - padR - 56, Math.max(padL + 2, todaySplitX - 28));
        clipped.push(`<rect x="${tagX.toFixed(1)}" y="${padT + 2}" width="56" height="17" rx="4" fill="#0f172a" stroke="#38bdf8" stroke-width="1" pointer-events="none"/>`);
        clipped.push(`<text x="${(tagX + 28).toFixed(1)}" y="${padT + 14}" text-anchor="middle" fill="#7dd3fc" font-size="10" font-weight="700" pointer-events="none">TODAY</text>`);
      }

      clipped.push(`<rect id="rel-focus-bar-ring" x="0" y="0" width="0" height="0" rx="1.5" fill="rgba(255, 255, 255, 0.14)" stroke="#ffffff" stroke-width="2" display="none" pointer-events="none"/>`);
      clipped.push(`<circle id="rel-focus-bar-dot" cx="0" cy="0" r="3.5" fill="#38bdf8" stroke="#ffffff" stroke-width="1.4" display="none" pointer-events="none"/>`);
      clipped.push(...hitRects);

      parts.push(`<g clip-path="url(#rel-plot-clip)">${clipped.join("")}</g>`);

      svg.innerHTML = parts.join("");
      renderRelNavigator();
      renderRelLegend();
    }

    function renderRelNavigator() {
      const nav = document.getElementById("rel-nav-svg");
      const totalN = REL_BARS.length;
      if (!nav || !totalN) return;

      const { W, H, padL, padR, padT, padB } = NAV_GEOM;
      const plotW = W - padL - padR;
      const plotH = H - padT - padB;

      let maxAll = 1;
      REL_BARS.forEach(b => {
        const t = relBarTotalNetVal(b);
        if (t > maxAll) maxAll = t;
      });

      const xFromIdx = idx => padL + (idx / Math.max(1, totalN - 1)) * plotW;
      let obsPts = [`${padL},${padT + plotH}`];
      let lastObsX = padL;
      let estPts = [];

      REL_BARS.forEach((b, i) => {
        const x = xFromIdx(i);
        const y = padT + plotH - (relBarTotalNetVal(b) / (maxAll * 1.08)) * plotH;
        if (b.kind === "OBSERVED") {
          obsPts.push(`${x.toFixed(1)},${y.toFixed(1)}`);
          lastObsX = x;
        } else {
          if (estPts.length === 0) {
            estPts.push(`${x.toFixed(1)},${padT + plotH}`);
          }
          estPts.push(`${x.toFixed(1)},${y.toFixed(1)}`);
        }
      });
      obsPts.push(`${lastObsX.toFixed(1)},${padT + plotH}`);
      if (estPts.length > 0) {
        estPts.push(`${padL + plotW},${padT + plotH}`);
      }

      const winX1 = xFromIdx(relViewStart);
      const winX2 = xFromIdx(relViewEnd);
      const winW = Math.max(8, winX2 - winX1);
      const splitNavX = xFromIdx(Math.min(totalN - 1, REL_LAST_OBS_IDX + 0.5));

      let out = [];
      out.push(`<rect x="${padL}" y="${padT}" width="${plotW}" height="${plotH}" rx="4" fill="#0b1324" stroke="#1e293b"/>`);
      if (obsPts.length > 2) {
        out.push(`<polygon points="${obsPts.join(' ')}" fill="rgba(56, 189, 248, 0.36)"/>`);
      }
      if (estPts.length > 2) {
        out.push(`<polygon points="${estPts.join(' ')}" fill="rgba(16, 185, 129, 0.36)"/>`);
      }

      let prevYr = "";
      REL_BARS.forEach((b, i) => {
        if (b.yearLbl !== prevYr) {
          prevYr = b.yearLbl;
          const x = xFromIdx(i);
          out.push(`<line x1="${x.toFixed(1)}" y1="${padT}" x2="${x.toFixed(1)}" y2="${padT + plotH}" stroke="rgba(148,163,184,0.45)" stroke-width="1.2"/>`);
          out.push(`<text x="${(x + 5).toFixed(1)}" y="${H - 5}" fill="#f1f5f9" font-size="12" font-weight="700" font-family="ui-monospace, monospace">${b.yearLbl}</text>`);
        }
      });

      out.push(`<line x1="${splitNavX.toFixed(1)}" y1="${padT}" x2="${splitNavX.toFixed(1)}" y2="${padT + plotH}" stroke="#38bdf8" stroke-width="1.8" stroke-dasharray="2,2"/>`);

      out.push(`<rect x="${padL}" y="${padT}" width="${Math.max(0, winX1 - padL).toFixed(1)}" height="${plotH}" fill="rgba(2, 6, 23, 0.58)" style="cursor:pointer;" data-rel-nav-action="jump"/>`);
      out.push(`<rect x="${(winX1 + winW).toFixed(1)}" y="${padT}" width="${Math.max(0, padL + plotW - (winX1 + winW)).toFixed(1)}" height="${plotH}" fill="rgba(2, 6, 23, 0.58)" style="cursor:pointer;" data-rel-nav-action="jump"/>`);

      out.push(`<rect x="${winX1.toFixed(1)}" y="${padT}" width="${winW.toFixed(1)}" height="${plotH}" fill="rgba(56, 189, 248, 0.16)" stroke="#38bdf8" stroke-width="1.4" style="cursor:grab;" data-rel-nav-action="move"/>`);
      out.push(`<rect x="${(winX1 - 4).toFixed(1)}" y="${padT + 6}" width="8" height="${plotH - 12}" rx="3" fill="#7dd3fc" style="cursor:ew-resize;" data-rel-nav-action="left"/>`);
      out.push(`<rect x="${(winX1 + winW - 4).toFixed(1)}" y="${padT + 6}" width="8" height="${plotH - 12}" rx="3" fill="#7dd3fc" style="cursor:ew-resize;" data-rel-nav-action="right"/>`);

      nav.innerHTML = out.join("");

      const sIdx = Math.max(0, Math.floor(relViewStart));
      const eIdx = Math.min(totalN - 1, Math.ceil(relViewEnd));
      document.getElementById("rel-nav-range-text").textContent =
        `${REL_BARS[sIdx].iso} → ${REL_BARS[eIdx].iso}`;
    }

    function renderRelLegend() {
      const leg = document.getElementById("releases-legend");
      if (!leg) return;
      const items = GRANT_SERIES.map(gs => {
        const pxTxt = gs.grant_price ? ` • Grant Px $${gs.grant_price.toFixed(2)}` : "";
        const tip = gs.status === "RETIRED"
          ? `Prior-History Grant (${gs.id}): Retired ${gs.last_vest}. Vested ${gs.vested_tranches} releases (${gs.vested_gross_shares.toLocaleString("en-US")} gross / ${gs.vested_net_shares.toLocaleString("en-US")} net sh).`
          : `Grant ${gs.id} (Granted ${gs.grant_date}${pxTxt}): ${gs.vested_tranches} vested releases (${gs.vested_net_shares.toLocaleString("en-US")} net sh) + ${gs.rem_tranches} remaining (${gs.rem_gross_shares.toLocaleString("en-US")} gross / ${gs.rem_est_net_shares.toLocaleString("en-US")} est. net sh through ${gs.last_vest}).`;
        return `<span class="legend-item has-tip" data-tip="${escHtml(tip)}"><span class="swatch" style="background:${gs.color};"></span>${escHtml(gs.label)}</span>`;
      });
      items.push(`<span class="legend-item muted" style="margin-left:auto;font-size:11.3px;">Dashed outline = Estimated Future Release (${(NET_RATE * 100).toFixed(1)}% net retention)</span>`);
      leg.innerHTML = items.join("");
    }

    function showReleaseTooltip(evt, idx) {
      const b = REL_BARS[idx];
      if (!b) return;
      highlightBarInSvg(window._relBarGeom, idx, "rel-");

      const tip = document.getElementById("chart-tooltip");
      const totNetVal = relBarTotalNetVal(b);
      const totGrossVal = relBarTotalGrossVal(b);
      const isEst = b.kind === "ESTIMATED";
      const badgeCls = isEst ? "pill-green" : "pill-blue";
      const badgeTxt = isEst ? "ESTIMATED" : "OBSERVED";

      const curSymPx = activeGrantLivePrice();
      let pxLine = "";
      if (!isEst) {
        if (pastReleaseValMode === "release") {
          pxLine = `<div class="mono muted" style="font-size:11.3px;margin-bottom:4px;">Valued @ Release FMV: <strong>${fmtUSD2(b.releasePrice)}/sh</strong></div>`;
        } else {
          pxLine = `<div class="mono" style="font-size:11.3px;color:#7dd3fc;margin-bottom:4px;">Valued @ Today: <strong>${fmtUSD2(curSymPx)}/sh</strong> <span class="muted">(Release FMV: ${fmtUSD2(b.releasePrice)})</span></div>`;
        }
      } else {
        pxLine = `<div class="mono" style="font-size:11.3px;color:#6ee7b7;margin-bottom:4px;">Valued @ Today: <strong>${fmtUSD2(curSymPx)}/sh</strong></div>`;
      }

      let grantRows = [];
      GRANT_SERIES.slice().reverse().forEach(gs => {
        const gb = b.byGrant && b.byGrant[gs.id];
        if (!gb) return;
        const v = relGrantNetVal(b, gs.id);
        if (v <= 0) return;
        grantRows.push(`
          <div style="display:flex;justify-content:space-between;align-items:baseline;gap:12px;margin:3px 0;">
            <span>
              <span class="swatch" style="background:${gs.color};width:9px;height:9px;margin-right:5px;"></span>
              <strong class="mono">${escHtml(gs.id)}</strong>
              <span class="mono muted" style="font-size:11px;margin-left:4px;">${gb.net_sh.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 3 })} sh</span>
            </span>
            <span class="mono"><strong>${fmtUSD0(v)}</strong> <span class="muted" style="font-size:10.8px;">(${gb.gross_sh.toLocaleString("en-US", { maximumFractionDigits: 1 })} gr)</span></span>
          </div>
        `);
      });

      tip.innerHTML = `
        <div style="display:flex;justify-content:space-between;align-items:center;gap:10px;">
          <strong class="mono">${escHtml(b.iso)}</strong>
          <span class="pill ${badgeCls}">${badgeTxt}</span>
        </div>
        <div class="mono" style="font-size:16px;font-weight:700;margin:3px 0;">${fmtUSD2(totNetVal)} <span class="muted" style="font-size:11.5px;font-weight:500;">net</span></div>
        <div class="mono muted" style="font-size:11.3px;margin-bottom:3px;">
          ${b.totNetShares.toLocaleString("en-US", { minimumFractionDigits: 3, maximumFractionDigits: 3 })} net sh &bull; Pre-tax: ${fmtUSD0(totGrossVal)} (${b.totGrossShares.toLocaleString("en-US", { maximumFractionDigits: 1 })} sh)
        </div>
        ${pxLine}
        <div style="border-top:1px solid #26334d;padding-top:5px;margin-top:4px;">
          <div class="muted" style="font-size:10.5px;text-transform:uppercase;letter-spacing:0.04em;margin-bottom:3px;">Contributing Grants (${grantRows.length})</div>
          ${grantRows.join("")}
        </div>
      `;

      placeFloatingBoxAwayFromPointer(tip, evt.clientX, evt.clientY);
    }

    function renderGoogleStockTables() {
      const gsu = GSTOCK || { release_bars: [], by_year: [], by_grant: [] };
      const sym = gsu.symbol || activeGrantSymbol || PRIMARY_SYMBOL || "";
      const compFull = gsu.company_name || sym;
      const compShort = gsu.short_name || sym;
      const awardTp = gsu.award_type || "RSU";
      const livePx = activeGrantLivePrice();

      const yrRows = gsu.by_year || [];
      const minYr = yrRows.length ? yrRows[0].year : TODAY_ISO.slice(0, 4);
      const maxYr = yrRows.length ? yrRows[yrRows.length - 1].year : minYr;
      const yrRangeTxt = minYr === maxYr ? String(minYr) : `${minYr} – ${maxYr}`;
      const obsBars = REL_BARS.filter(b => b.kind === "OBSERVED");
      const estBars = REL_BARS.filter(b => b.kind === "ESTIMATED");

      const relSubEl = document.getElementById("gstock-rel-sub");
      if (relSubEl) {
        const spanLbl = REL_BARS.length ? ` (${REL_BARS[0].shortLbl} – ${REL_BARS[REL_BARS.length - 1].shortLbl})` : "";
        relSubEl.innerHTML = `${escHtml(compFull)} (<span class="mono">${escHtml(sym)}</span>) ${escHtml(awardTp)} Releases Stacked by Grant ID${escHtml(spanLbl)} &bull; Drag chart or overview bar to pan/zoom`;
      }
      const yrTitleEl = document.getElementById("gstock-yr-title");
      if (yrTitleEl) yrTitleEl.textContent = `Yearly ${compShort} Stock Releases (${yrRangeTxt})`;
      const yrSubEl = document.getElementById("gstock-yr-sub");
      if (yrSubEl) {
        const obsSpan = obsBars.length ? `${obsBars[0].monthLbl}–${obsBars[obsBars.length - 1].monthLbl}` : "none";
        const estSpan = estBars.length ? `${estBars[0].monthLbl}–${estBars[estBars.length - 1].monthLbl}` : "none";
        yrSubEl.textContent = `Observed releases (${obsSpan}) + scheduled unvested tranches (${estSpan})`;
      }
      const grTitleEl = document.getElementById("gstock-grant-title");
      if (grTitleEl) grTitleEl.textContent = `${compFull} ${awardTp} Grants`;
      const grPxTipEl = document.getElementById("gstock-grant-price-tip");
      if (grPxTipEl) grPxTipEl.dataset.tip = `Closing market price of ${compFull} (${sym}) on the grant date.`;
      const allBtn = document.querySelector('[data-rel-preset="all"]');
      if (allBtn) allBtn.textContent = `Full ${yrRangeTxt}`;
      const histBtn = document.querySelector('[data-rel-preset="historical"]');
      if (histBtn) histBtn.textContent = obsBars.length ? `Historical (${obsBars[0].yearLbl}–Today)` : "Historical";
      const ytdBtn = document.querySelector('[data-rel-preset="ytd"]');
      if (ytdBtn && TODAY_ISO) ytdBtn.textContent = `${TODAY_ISO.slice(0, 4)} YTD`;

      const totUnvSh = gsu.future_gross_shares != null ? gsu.future_gross_shares : (gsu.by_grant || []).reduce((s, g) => s + (g.total_qty || 0), 0);
      const grants = gsu.by_grant || [];
      const drawerPill = document.getElementById("gsu-drawer-pill");
      if (drawerPill) {
        drawerPill.textContent =
          `As of ${gsu.report_date || TODAY_ISO} • ${grants.length} Active Grants • ${totUnvSh.toLocaleString("en-US", { maximumFractionDigits: 1 })} Rem. Sh`;
      }

      const basisPill = document.getElementById("gsu-year-basis-pill");
      if (basisPill) {
        const pastTxt = pastReleaseValMode === "today" ? "Past @ Today's Quote" : "Past @ Release FMV";
        basisPill.textContent = `${pastTxt} • Future @ Today's Quote`;
      }

      // Dynamically aggregate each Calendar Year from REL_BARS so both controls update values seamlessly
      const dynByYear = {};
      REL_BARS.forEach(b => {
        const y = b.yearLbl;
        if (!dynByYear[y]) dynByYear[y] = { grossVal: 0, netVal: 0 };
        dynByYear[y].grossVal += relBarTotalGrossVal(b);
        dynByYear[y].netVal += relBarTotalNetVal(b);
      });

      let sumTr = 0, sumGrossSh = 0, sumNetSh = 0, sumGrossVal = 0, sumNetVal = 0;

      document.getElementById("gsu-year-tbody").innerHTML = yrRows.map(y => {
        const pillCls = y.status === "SCHEDULED" ? "pill-green" : (y.status === "CURRENT_SPLIT" ? "pill-blue" : "pill-cyan");
        const badgeHtml = y.badge ? `<span class="pill ${pillCls} has-tip" style="margin-left:6px;" data-tip="${escHtml(y.tooltip || '')}">${escHtml(y.badge)}</span>` : "";
        const subHtml = y.subnote ? `<div class="row-sub">${escHtml(y.subnote)}</div>` : "";
        const gVal = dynByYear[y.year] ? dynByYear[y.year].grossVal : y.gross_val;
        const nVal = dynByYear[y.year] ? dynByYear[y.year].netVal : y.net_val;

        sumTr += y.tranches;
        sumGrossSh += y.shares;
        sumNetSh += y.net_shares;
        sumGrossVal += gVal;
        sumNetVal += nVal;

        return `<tr style="cursor:pointer;" title="Click to zoom Releases graph to ${escHtml(y.year)}" data-sa-action="jump-rel-year" data-sa-year="${escHtml(y.year)}">
          <td><div class="mono"><strong>${escHtml(y.year)}</strong>${badgeHtml}</div>${subHtml}</td>
          <td class="num mono">${y.tranches}</td>
          <td class="num mono">${y.shares.toLocaleString("en-US", { minimumFractionDigits: 3, maximumFractionDigits: 3 })}</td>
          <td class="num mono">${y.net_shares.toLocaleString("en-US", { minimumFractionDigits: 3, maximumFractionDigits: 3 })}</td>
          <td class="num mono">${fmtUSD0(gVal)}</td>
          <td class="num mono pos"><strong>${fmtUSD0(nVal)}</strong></td>
        </tr>`;
      }).join("");

      document.getElementById("gsu-year-tfoot").innerHTML = `<tr>
        <td>Total (${escHtml(yrRangeTxt)})</td>
        <td class="num mono">${sumTr}</td>
        <td class="num mono">${sumGrossSh.toLocaleString("en-US", { minimumFractionDigits: 3, maximumFractionDigits: 3 })}</td>
        <td class="num mono">${sumNetSh.toLocaleString("en-US", { minimumFractionDigits: 3, maximumFractionDigits: 3 })}</td>
        <td class="num mono">${fmtUSD0(sumGrossVal)}</td>
        <td class="num mono pos">${fmtUSD0(sumNetVal)}</td>
      </tr>`;

      // Dynamically sum future projected values per active grant across estimated REL_BARS
      const dynFutGrant = {};
      REL_BARS.forEach(b => {
        if (b.kind !== "ESTIMATED") return;
        Object.entries(b.byGrant || {}).forEach(([gid, gb]) => {
          if (!dynFutGrant[gid]) dynFutGrant[gid] = { gross: 0, net: 0 };
          dynFutGrant[gid].gross += gb.val_today_gross;
          dynFutGrant[gid].net += gb.val_today_net;
        });
      });

      let totRemTr = 0, totRemSh = 0, totRemGrossVal = 0, totRemNetVal = 0;

      document.getElementById("gsu-grant-tbody").innerHTML = grants.map(g => {
        const gGross = dynFutGrant[g.grant_id] ? dynFutGrant[g.grant_id].gross : g.gross_val;
        const gNet = dynFutGrant[g.grant_id] ? dynFutGrant[g.grant_id].net : g.net_val;
        totRemTr += g.tranches;
        totRemSh += g.total_qty;
        totRemGrossVal += gGross;
        totRemNetVal += gNet;

        const gPxStr = g.grant_price != null ? fmtUSD2(g.grant_price) : "—";
        const chgVsGrant = (g.grant_price && livePx > 0) ? (((livePx - g.grant_price) / g.grant_price) * 100) : null;
        const gPxTip = g.grant_price
          ? `${sym} close on grant date ${g.grant_date}: ${fmtUSD2(g.grant_price)}/sh${chgVsGrant != null ? ` (${chgVsGrant >= 0 ? "+" : ""}${chgVsGrant.toFixed(1)}% vs live ${fmtUSD2(livePx)}/sh)` : ""}.`
          : "Grant price unavailable.";
        const spanTip = `Grant ${g.grant_id}: Granted ${g.grant_date} @ ${gPxStr}/sh • First vest: ${g.first_vest_ever} • Next unvested: ${g.first_vest} • Final vest: ${g.last_vest}. Schedule: ${g.cadence_note}. Total award (vested + remaining): ${g.total_award_shares.toLocaleString("en-US", { maximumFractionDigits: 3 })} sh.`;

        const fmtShortIso = iso => iso && iso.length === 10 ? `${iso.slice(5, 7)}/${iso.slice(8, 10)}/${iso.slice(2, 4)}` : iso;
        const grantTip = `Grant ${g.grant_id}: ${g.vested_tranches} releases vested to date (${g.vested_gross_shares.toLocaleString("en-US", { maximumFractionDigits: 3 })} gross / ${g.vested_net_shares.toLocaleString("en-US", { maximumFractionDigits: 3 })} actual net sh).`;

        return `<tr>
          <td>
            <div class="mono has-tip" style="display:inline-block;" data-tip="${escHtml(grantTip)}"><span class="swatch" style="background:${g.color};width:9px;height:9px;margin-right:5px;"></span><strong>${escHtml(g.grant_id)}</strong></div>
            <div class="row-sub mono">${g.vested_tranches} vst (${Math.round(g.vested_net_shares).toLocaleString("en-US")} sh)</div>
          </td>
          <td class="num mono"><span class="has-tip" data-tip="${escHtml(gPxTip)}">${gPxStr}</span></td>
          <td>
            <div class="mono has-tip" style="display:inline-block;" data-tip="${escHtml(spanTip)}">${escHtml(fmtShortIso(g.grant_date))} &rarr; <strong>${escHtml(fmtShortIso(g.last_vest))}</strong></div>
            <div class="row-sub mono">Next: ${escHtml(fmtShortIso(g.first_vest))}</div>
          </td>
          <td class="num mono">${g.tranches}</td>
          <td class="num">
            <div class="mono">${g.total_qty.toLocaleString("en-US", { minimumFractionDigits: 3, maximumFractionDigits: 3 })}</div>
            <div class="row-sub mono">~${g.est_net_qty.toLocaleString("en-US", { maximumFractionDigits: 1 })} net</div>
          </td>
          <td class="num mono">${fmtUSD0(gGross)}</td>
          <td class="num mono pos"><strong>${fmtUSD0(gNet)}</strong></td>
        </tr>`;
      }).join("");

      document.getElementById("gsu-grant-tfoot").innerHTML = `<tr>
        <td colspan="3">Total Remaining Unvested</td>
        <td class="num mono">${totRemTr}</td>
        <td class="num mono">${totRemSh.toLocaleString("en-US", { minimumFractionDigits: 3, maximumFractionDigits: 3 })}</td>
        <td class="num mono">${fmtUSD0(totRemGrossVal)}</td>
        <td class="num mono pos">${fmtUSD0(totRemNetVal)}</td>
      </tr>`;
    }

