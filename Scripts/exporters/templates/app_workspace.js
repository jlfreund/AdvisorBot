    // =========================================================================
    // CANONICAL MODEL REGISTRY, OFFSCREEN 1160x420 SNAPSHOTTER & #screen-models
    // =========================================================================

    const SAVES_LOCAL_KEY = "holdings_models_v2";
    let currentModelId = "001";
    let currentSaveName = "Default";
    let loadedSettingsSnapshot = null;
    let isSaveDirty = false;
    let _suppressDirtyCheck = true;
    let savesRegistry = {}; // name -> canonical model entry
    let modelsList = [];    // ordered array of canonical model entries
    let selectedDialogSaveName = null;
    let selectedModelDetailId = null;
    let isEditingDetailNotes = false;
    let _allowNotesEditorRender = false;
    let savesServerOnline = false;

    function roundSnapNum(val, decimals = 4) {
      const n = Number(val);
      if (!Number.isFinite(n)) return 0;
      const f = Math.pow(10, decimals);
      return Math.round(n * f) / f;
    }

    function findModelEntry(idOrName) {
      const q = String(idOrName || "").trim();
      if (!q) return null;
      const qLow = q.toLowerCase();
      const byId = modelsList.find(m => String(m.id || "").toLowerCase() === qLow);
      if (byId) return byId;
      const byExact = savesRegistry[q];
      if (byExact) return byExact;
      return modelsList.find(m => String(m.name || "").toLowerCase() === qLow) || null;
    }

    function getCanonicalHoldingForecastsSnapshot() {
      const out = {};
      const src = (holdingForecasts && typeof holdingForecasts === "object") ? holdingForecasts : {};
      Object.keys(src).sort().forEach(fk => {
        const entry = src[fk] || {};
        const rc = RISK_CLASSES_META[entry.risk_class] ? entry.risk_class : "broad_equity";
        const item = {
          growth_pct: roundSnapNum(entry.growth_pct, 2),
          risk_class: rc,
        };
        if (entry.use_reference) item.use_reference = true;
        if (entry.risk_overridden) {
          item.risk_overridden = true;
        }
        // DA-B02: Persist custom per-holding volatility (`vol_pct`) across Scenario save/load so
        // Monte Carlo & Cohort overrides in the Holding Forecast modal are never silently dropped.
        if (entry.vol_pct != null && Number.isFinite(Number(entry.vol_pct))) {
          item.vol_pct = Math.max(0, Math.min(150, roundSnapNum(entry.vol_pct, 2)));
        }
        out[fk] = item;
      });
      return out;
    }

    function getCurrentSettingsSnapshot() {
      const snap = {
        holdingForecasts: getCanonicalHoldingForecastsSnapshot(),
        holdingHalfLives: (function() {
          const out = {};
          const norm = normalizeHoldingHalfLives(holdingHalfLives);
          Object.keys(norm).sort().forEach(k => { out[k] = norm[k]; });
          return out;
        })(),
        taxMode: taxMode || "net",
        pastReleaseValMode: pastReleaseValMode || "release",
        simAnnualBurn: Math.round((simAnnualBurn != null && Number.isFinite(Number(simAnnualBurn))) ? Math.max(0, Number(simAnnualBurn)) : RETIREMENT_ANNUAL_BURN),
        simInflationPct: roundSnapNum(simInflationPct, 2),
        simOptimismMultiplier: roundSnapNum(simOptimismMultiplier, 2),
        simIncludeUnvested: Boolean(simIncludeUnvested),
        simIncludeSSN: Boolean(simIncludeSSN),
        simIncludeScheduledActions: Boolean(simIncludeScheduledActions),
        simBearShield: Boolean(simBearShield),
        simOrderPreset: simOrderPreset || "tax_optimal",
        simAssetOrder: Array.isArray(simAssetOrder) ? [...simAssetOrder] : [],
        simScheduledActions: normalizeSimScheduledActions(simScheduledActions),
        simHypotheticalAssets: normalizeHypotheticalAssets(simHypotheticalAssets),
        simActiveModel: simActiveModel || "single_path",
        simStressScenario: simStressScenario || "none",
        simCustomShockPct: roundSnapNum(simCustomShockPct, 1),
        simCustomDownMonths: Math.max(3, Math.min(36, Math.round(Number(simCustomDownMonths) || 12))),
        simCustomBroadBounceMonths: Math.max(3, Math.min(STRESS_NO_BOUNCE_MONTHS, Math.round(Number(simCustomBroadBounceMonths) || 24))),
        simCustomStockBounceMonths: Math.max(3, Math.min(STRESS_NO_BOUNCE_MONTHS, Math.round(Number(simCustomStockBounceMonths) || 36))),
        mcSingleStockVolPct: roundSnapNum(mcSingleStockVolPct, 2),
        mcBroadEquityVolPct: roundSnapNum(mcBroadEquityVolPct, 2),
        mcReturnModel: mcReturnModel || "normal",
        mcSingleStockCorr: roundSnapNum(mcSingleStockCorr, 3),
        mcSimulations: 500,
        mcSuccessThresholdUsd: Math.round(Number(mcSuccessThresholdUsd) || 0),
        cohortCenterMode: cohortCenterMode || "anchor",
        cohortInflationMode: "real_cpi",
        cohortStartFrom: Math.round(Number(cohortStartFrom)),
        simMarriedFilingJointly: null,
        simMinTaxBracketFloor: null,
        simInvestor1RetirementAge: simInvestor1RetirementAge != null && Number.isFinite(Number(simInvestor1RetirementAge))
          ? Math.round(Number(simInvestor1RetirementAge))
          : null,
        simInvestor2RetirementAge: simInvestor2RetirementAge != null && Number.isFinite(Number(simInvestor2RetirementAge))
          ? Math.round(Number(simInvestor2RetirementAge))
          : null,
        simMemberRetirementAges: (function() {
          const out = {};
          const src = (typeof simMemberRetirementAges === "object" && simMemberRetirementAges) ? simMemberRetirementAges : {};
          Object.keys(src).sort().forEach(k => {
            const v = src[k];
            if (v != null && typeof v !== "boolean" && Number.isFinite(Number(v))) {
              out[k] = Math.round(Number(v));
            }
          });
          return out;
        })(),
        [INV0.claim_setting_key || "ssnInvestor1ClaimAge"]: Math.round(Number(ssnInv0ClaimAge) || 67),
        ssnColaPct: roundSnapNum(ssnColaPct, 2),
      };
      if (HAS_SECOND_INVESTOR && INV1) {
        snap[INV1.claim_setting_key || "ssnInvestor2ClaimAge"] = Math.round(Number(ssnInv1ClaimAge) || 67);
      }
      return snap;
    }

    function snapshotsEqual(a, b) {
      if (!a || !b) return false;
      return JSON.stringify(a) === JSON.stringify(b);
    }

    function isDemoModeActive() {
      return Boolean(
        (_privacyConfig && _privacyConfig.demo_mode && _privacyConfig.demo_mode.enabled) ??
        (PAYLOAD && PAYLOAD.privacy_status && PAYLOAD.privacy_status.demo_enabled)
      );
    }

    function showDemoBlockedAlert() {
      const bd = document.getElementById("demo-blocked-backdrop");
      if (bd) bd.classList.add("open");
    }

    function closeDemoBlockedAlert() {
      const bd = document.getElementById("demo-blocked-backdrop");
      if (bd) bd.classList.remove("open");
    }
    window.closeDemoBlockedAlert = closeDemoBlockedAlert;

    function updateSaveStatusUI(flashSavedMessage = false) {
      const dotEl = document.getElementById("save-status-dot");
      const diskEl = document.getElementById("nav-disk-btn");
      const reloadBtn = document.getElementById("nav-reload-btn");
      const nameEl = document.getElementById("save-current-name");
      const subEl = document.getElementById("save-status-sub");
      const btnEl = document.getElementById("nav-btn-models");
      const rawName = String(currentSaveName || "").trim();
      const hasLoadedScenario = Boolean(rawName);
      const dispTitle = hasLoadedScenario ? rawName : "Scenarios";
      if (nameEl) {
        nameEl.textContent = dispTitle;
        nameEl.title = hasLoadedScenario ? rawName : "Scenarios";
      }
      const isMac = typeof navigator !== "undefined" && /Mac|iPhone|iPod|iPad/i.test(navigator.userAgent || "");
      const sKey = isMac ? "\u2318S" : "Ctrl+S";
      const rKey = isMac ? "\u2318R" : "Ctrl+R";

      const isDemoActive = isDemoModeActive();

      if (btnEl) {
        btnEl.classList.toggle("dirty-scenario", isSaveDirty);
        const statusSummary = isDemoActive
          ? `Locked in Demo Mode (Read-Only &mdash; press ${rKey} to revert)`
          : (hasLoadedScenario
              ? (isSaveDirty ? `Unsaved <b style="color:#c4b5fd;">&#9670;</b> changes &mdash; press <b>${sKey}</b> to save or <b>${rKey}</b> to revert` : `All <b style="color:#c4b5fd;">&#9670;</b> changes saved (press <b>${sKey}</b> to quick-save)`)
              : (isSaveDirty ? `Unsaved <b style="color:#c4b5fd;">&#9670;</b> changes &mdash; press <b>${sKey}</b> to save` : `Click to view Saved Scenarios or press <b>${sKey}</b> to save`));
        if (typeof btnEl.setAttribute === "function") {
          if (typeof btnEl.removeAttribute === "function") btnEl.removeAttribute("title");
          btnEl.setAttribute("data-tip",
            `<div><strong style="color:#f8fafc;">Active Scenario: ${escHtml(rawName || "Scenarios")}</strong><br>` +
            `<span style="font-size:11px;color:#cbd5e1;">${statusSummary}.</span><br>` +
            `<small style="color:#94a3b8;display:block;margin-top:4px;">Purple <b style="color:#c4b5fd;">&#9670;</b> boxes &amp; capsules save to this Scenario and drive the gold <b style="color:#fcd34d;">&#9733;</b> Milestone KPIs, without affecting other scenarios.</small></div>`
          );
        } else {
          btnEl.title = hasLoadedScenario ? `Active Scenario: ${rawName}` : "Saved Scenarios";
        }
      }
      if (diskEl) {
        const diskAction = isDemoActive
          ? "Demo Mode Active (Read-Only)"
          : (!hasLoadedScenario
              ? `Save current purple &#9670; settings as a Scenario (${sKey})`
              : (isSaveDirty ? `Save unsaved purple &#9670; edits to &ldquo;${escHtml(rawName)}&rdquo; (${sKey})` : `Quick-save &ldquo;${escHtml(rawName)}&rdquo; (${sKey})`));
        if (typeof diskEl.setAttribute === "function") {
          if (typeof diskEl.removeAttribute === "function") diskEl.removeAttribute("title");
          diskEl.setAttribute("data-tip", `<div><strong>${diskAction}</strong></div>`);
        } else {
          diskEl.title = hasLoadedScenario ? `Quick-Save "${rawName}" (${sKey})` : `Save Scenario (${sKey})`;
        }
      }
      if (reloadBtn) {
        reloadBtn.style.display = isSaveDirty ? "inline-flex" : "none";
        reloadBtn.title = `Reload "${rawName || 'Scenario'}" & revert unsaved changes (${rKey})`;
      }
      if (dotEl) {
        dotEl.classList.toggle("saved", !isSaveDirty);
        dotEl.classList.toggle("dirty", isSaveDirty);
        dotEl.title = isDemoActive
          ? (isSaveDirty ? `Unsaved Scenario changes in Demo Mode — Reload (${rKey})` : `Locked in Demo Mode (Read-Only)`)
          : (isSaveDirty
              ? `Unsaved Scenario changes — Save (${sKey}) • Reload (${rKey})`
              : (hasLoadedScenario ? "Saved" : "No unsaved changes"));
      }
      if (subEl) {
        const demoLockedText = isSaveDirty
          ? `\u25CF Modified \u2022 \uD83D\uDD12 Demo Mode \u2022 Reload (${rKey})`
          : `\uD83D\uDD12 Demo Mode (Locked)`;
        const steadyText = isDemoActive
          ? demoLockedText
          : (!hasLoadedScenario
              ? (isSaveDirty ? `\u25CF Modified \u2022 Save (${sKey}) \u2022 Reload (${rKey})` : `Choose or save (${sKey})`)
              : (isSaveDirty ? `\u25CF Modified \u2022 Save (${sKey}) \u2022 Reload (${rKey})` : `Saved \u2022 ${sKey} to save`));
        if (flashSavedMessage === "reloaded" || flashSavedMessage === "reverted") {
          subEl.textContent = `Reloaded \u2713 (${rKey})`;
          subEl.classList.remove("dirty-text");
          subEl.classList.add("saved-flash");
          subEl.style.color = "#38bdf8";
          setTimeout(() => {
            if (subEl) subEl.classList.remove("saved-flash");
            if (!isSaveDirty && subEl) {
              subEl.textContent = steadyText;
              subEl.style.color = "";
            }
          }, 1800);
        } else if (flashSavedMessage) {
          subEl.textContent = `Saved \u2713 (${sKey})`;
          subEl.classList.remove("dirty-text");
          subEl.classList.add("saved-flash");
          subEl.style.color = "#34d399";
          setTimeout(() => {
            if (subEl) subEl.classList.remove("saved-flash");
            if (!isSaveDirty && subEl) {
              subEl.textContent = steadyText;
              subEl.style.color = "";
            }
          }, 1800);
        } else if (isSaveDirty) {
          subEl.textContent = steadyText;
          subEl.style.color = "";
          subEl.classList.remove("saved-flash");
          subEl.classList.add("dirty-text");
        } else {
          subEl.textContent = steadyText;
          subEl.style.color = "";
          subEl.classList.remove("saved-flash");
          subEl.classList.remove("dirty-text");
        }
      }
      if (currentScreen === "models" && !selectedModelDetailId) {
        renderModelsScreen();
      }
    }

    function checkSaveDirtyState() {
      if (_suppressDirtyCheck || !loadedSettingsSnapshot) return;
      const cur = getCurrentSettingsSnapshot();
      const dirtyNow = !snapshotsEqual(cur, loadedSettingsSnapshot);
      if (dirtyNow !== isSaveDirty) {
        isSaveDirty = dirtyNow;
        updateSaveStatusUI(false);
      }
    }

    function applySettingsSnapshot(snap) {
      if (!snap || typeof snap !== "object") return;
      if (typeof cancelSimProgressiveJob === "function") cancelSimProgressiveJob(true);
      const prevSuppress = _suppressDirtyCheck;
      _suppressDirtyCheck = true;
      try {
        // 1. Per-holding forecasts (sparse explicit overrides; untouched holdings inherit ingested baselines)
        holdingHalfLives = normalizeHoldingHalfLives(snap.holdingHalfLives);
        const hasLegacyGoog = snap.futureGoogGrowthPct != null || snap.simGoogGrowthPct != null;
        const hasLegacyNonGoog = snap.futureNonGoogGrowthPct != null || snap.simNonGoogGrowthPct != null;
        if (snap.holdingForecasts && typeof snap.holdingForecasts === "object") {
          const nextHf = {};
          Object.entries(snap.holdingForecasts).forEach(([fk, entry]) => {
            if (typeof entry === "number" && Number.isFinite(entry)) {
              const simA = (BASE_SIM_ASSETS || []).find(a => a.id === fk || a.forecast_key === fk);
              const defRc = (simA && RISK_CLASSES_META[simA.default_risk_class]) ? simA.default_risk_class : "broad_equity";
              nextHf[fk] = { growth_pct: Math.max(-20, Math.min(25, entry)), risk_class: defRc };
            } else if (entry && typeof entry === "object") {
              const g = Math.max(-20, Math.min(25, Number(entry.growth_pct) || 0));
              const simA = (BASE_SIM_ASSETS || []).find(a => a.id === fk || a.forecast_key === fk);
              const defRc = (simA && RISK_CLASSES_META[simA.default_risk_class]) ? simA.default_risk_class : "broad_equity";
              const rc = RISK_CLASSES_META[entry.risk_class] ? entry.risk_class : defRc;
              const outEntry = { growth_pct: g, risk_class: rc };
              if (entry.use_reference) outEntry.use_reference = true;
              if (entry.risk_overridden) {
                outEntry.risk_overridden = true;
              }
              // DA-B02: Persist custom per-holding volatility (`vol_pct`) across Scenario save/load so
        // Monte Carlo & Cohort overrides in the Holding Forecast modal are never silently dropped.
        if (entry.vol_pct != null && Number.isFinite(Number(entry.vol_pct))) {
                outEntry.vol_pct = Math.max(0, Math.min(150, Math.round(Number(entry.vol_pct) * 100) / 100));
              }
              nextHf[fk] = outEntry;
            }
          });
          holdingForecasts = nextHf;
        } else if (hasLegacyGoog || hasLegacyNonGoog) {
          const rawGoog = Number(snap.futureGoogGrowthPct ?? snap.simGoogGrowthPct ?? 0) || 0;
          const rawNonGoog = Number(snap.futureNonGoogGrowthPct ?? snap.simNonGoogGrowthPct ?? 0) || 0;
          holdingForecasts = defaultHoldingForecasts(rawGoog, rawNonGoog);
        } else {
          holdingForecasts = {};
        }
        if (snap.holdingFeePct && typeof snap.holdingFeePct === "object" && !Array.isArray(snap.holdingFeePct)) {
          if (!holdingFeePct || typeof holdingFeePct !== "object") holdingFeePct = {};
          Object.entries(snap.holdingFeePct).forEach(([fk, rawFee]) => {
            if (rawFee === null) {
              delete holdingFeePct[fk];
            } else if (rawFee !== undefined && Number.isFinite(Number(rawFee))) {
              holdingFeePct[fk] = Math.max(0, Number(rawFee));
            }
          });
        }
        syncLegacyGrowthAliasesFromForecasts();
        syncSharedGrowthControls();

        // 2. Scheduled actions & hypothetical accounts materialize BEFORE simAssetOrder resolution
        const rawActionsPre = Array.isArray(snap.simScheduledActions)
          ? snap.simScheduledActions
          : (Array.isArray(snap.simScheduledWithdrawals) ? snap.simScheduledWithdrawals : []);
        simScheduledActions = normalizeSimScheduledActions(rawActionsPre);
        simScheduledWithdrawals = simScheduledActions;
        selectedSimWithdrawalIdx = simScheduledActions.length > 0 ? 0 : -1;
        simHypotheticalAssets = normalizeHypotheticalAssets(snap.simHypotheticalAssets || []);
        SIM_ASSETS = materializeSimAssets();

        // 3. Screen 1 & 2 valuation settings
        taxMode = snap.taxMode || "net";
        document.querySelectorAll("[data-tax]").forEach(b => b.classList.toggle("active", b.dataset.tax === taxMode));
        updateFutureKPI();

        pastReleaseValMode = snap.pastReleaseValMode === "today" ? "today" : "release";
        document.querySelectorAll("[data-past-val]").forEach(b =>
          b.classList.toggle("active", b.dataset.pastVal === pastReleaseValMode)
        );

        // 4. Screen 4: Social Security settings (recomputed before Sim since SSN feeds Sim)
        const _k0 = INV0.claim_setting_key || "ssnInvestor1ClaimAge";
        ssnInv0ClaimAge = Math.max(62, Math.min(70, Math.round(Number(snap[_k0] ?? snap.ssnInvestor1ClaimAge ?? INV0.default_claim_age) || 67)));
        const jSl = document.getElementById("ssn-inv0-age-slider");
        if (jSl) jSl.value = String(ssnInv0ClaimAge);

        if (HAS_SECOND_INVESTOR && INV1) {
          const _k1 = INV1.claim_setting_key || "ssnInvestor2ClaimAge";
          ssnInv1ClaimAge = Math.max(62, Math.min(70, Math.round(Number(snap[_k1] ?? snap.ssnInvestor2ClaimAge ?? INV1.default_claim_age) || 67)));
          const ySl = document.getElementById("ssn-inv1-age-slider");
          if (ySl) ySl.value = String(ssnInv1ClaimAge);
        }

        ssnColaPct = Math.max(0, Math.min(6, Math.round((Number(snap.ssnColaPct ?? SSN_COLA_REF)) * 10) / 10));
        const cSl = document.getElementById("ssn-cola-slider");
        if (cSl) cSl.value = String(ssnColaPct);
        const cPill = document.getElementById("ssn-cola-pill");
        if (cPill) {
          cPill.textContent = `${ssnColaPct.toFixed(1)}% / yr`;
          cPill.className = "mdl-in-pill mono";
          cPill.title = `Average Annual COLA (click to reset to reference ${SSN_COLA_REF.toFixed(1)}%/yr)`;
          cPill.onclick = () => onSsnColaInput(SSN_COLA_REF);
        }
        const cZeroBtn = document.getElementById("ssn-cola-zero-btn");
        if (cZeroBtn) cZeroBtn.classList.toggle("active", Math.abs(ssnColaPct) < 0.05);
        const cSub = document.getElementById("ssn-cola-subtext");
        if (cSub && typeof SSN_COLA_SPAN !== "undefined") {
          cSub.textContent = `Trailing 10-year historical SSA COLA average (${SSN_COLA_SPAN})`;
        }
        const cRefBtn = document.getElementById("ssn-cola-ref-btn");
        if (cRefBtn) {
          cRefBtn.textContent = `Ref: ${SSN_COLA_REF.toFixed(1)}%`;
          cRefBtn.classList.toggle("active", Math.abs(ssnColaPct - SSN_COLA_REF) < 0.05);
          cRefBtn.title = `Set to ${SSN_COLA_REF.toFixed(1)}%/yr reference rate: average Social Security COLA for ${SSN_COLA_SPAN}.`;
          cRefBtn.onclick = () => onSsnColaInput(SSN_COLA_REF);
        }

        recomputeSsnBars();

        // 5. Screen 3: Retirement Simulator settings
        simAnnualBurn = (snap.simAnnualBurn != null && Number.isFinite(Number(snap.simAnnualBurn)))
          ? Math.max(0, Math.round(Number(snap.simAnnualBurn)))
          : RETIREMENT_ANNUAL_BURN;
        syncSimBurnControls();

        simInflationPct = Math.max(0, Math.min(6, Math.round((Number(snap.simInflationPct ?? RETIREMENT_INFLATION_REF)) * 10) / 10));
        syncSimInflationControls();

        simOptimismMultiplier = snap.simOptimismMultiplier != null ? Math.max(-2.0, Math.min(2.0, Number(snap.simOptimismMultiplier))) : 1.0;
        if (typeof updateSimOptimismDOM === "function") updateSimOptimismDOM();

        simMarriedFilingJointly = null;
        simMinTaxBracketFloor = null;
        const _ms0 = Math.max(18, Math.min(100, Math.round(Number((INV0 && INV0.milestone_age) || 85))));
        const _ms1 = Math.max(18, Math.min(100, Math.round(Number((INV1 && INV1.milestone_age) || 85))));
        simInvestor1RetirementAge = (snap.simInvestor1RetirementAge != null && typeof snap.simInvestor1RetirementAge !== "boolean" && Number.isFinite(Number(snap.simInvestor1RetirementAge)))
          ? Math.max(18, Math.min(_ms0, Math.round(Number(snap.simInvestor1RetirementAge))))
          : null;
        simInvestor2RetirementAge = (snap.simInvestor2RetirementAge != null && typeof snap.simInvestor2RetirementAge !== "boolean" && Number.isFinite(Number(snap.simInvestor2RetirementAge)))
          ? Math.max(18, Math.min(_ms1, Math.round(Number(snap.simInvestor2RetirementAge))))
          : null;
        simMemberRetirementAges = {};
        if (snap.simMemberRetirementAges && typeof snap.simMemberRetirementAges === "object") {
          const _famsSnap = Array.isArray(PROJECT_CFG.family_members) ? PROJECT_CFG.family_members : [];
          Object.entries(snap.simMemberRetirementAges).forEach(([rawK, rawV]) => {
            if (rawV == null || typeof rawV === "boolean" || !Number.isFinite(Number(rawV))) return;
            const kStr = String(rawK || "").trim();
            const mMatch = kStr.match(/^(?:member_)?(\d+)(?:_retirement_age)?$/);
            if (!mMatch) return;
            const fIdx = parseInt(mMatch[1], 10);
            if (!Number.isInteger(fIdx) || fIdx < 0 || fIdx > 25) return;
            const msCap = _famsSnap[fIdx] ? Math.max(18, Math.min(100, Math.round(Number(_famsSnap[fIdx].milestone_age || 85)))) : 100;
            simMemberRetirementAges[`member_${fIdx}_retirement_age`] = Math.max(18, Math.min(msCap, Math.round(Number(rawV))));
          });
        }
        if (typeof syncSimRetirementAgeControls === "function") syncSimRetirementAgeControls();
        if (typeof syncSimTaxControls === "function") syncSimTaxControls();

        simIncludeUnvested = snap.simIncludeUnvested != null ? Boolean(snap.simIncludeUnvested) : true;
        const unvOffBtn = document.getElementById("sim-unvested-off-btn");
        const unvOnBtn = document.getElementById("sim-unvested-on-btn");
        if (unvOffBtn) unvOffBtn.classList.toggle("active", !simIncludeUnvested);
        if (unvOnBtn) unvOnBtn.classList.toggle("active", simIncludeUnvested);

        simIncludeSSN = snap.simIncludeSSN != null ? Boolean(snap.simIncludeSSN) : true;
        ["sim-ssn-off-btn", "ssn-feed-off-btn"].forEach(id => {
          const el = document.getElementById(id);
          if (el) el.classList.toggle("active", !simIncludeSSN);
        });
        ["sim-ssn-on-btn", "ssn-feed-on-btn"].forEach(id => {
          const el = document.getElementById(id);
          if (el) el.classList.toggle("active", simIncludeSSN);
        });

        const rawIncAct = snap.simIncludeScheduledActions != null
          ? snap.simIncludeScheduledActions
          : snap.simIncludeScheduledWithdrawals;
        simIncludeScheduledActions = rawIncAct != null ? Boolean(rawIncAct) : true;
        simIncludeScheduledWithdrawals = simIncludeScheduledActions;
        const swOffBtn = document.getElementById("sim-sw-off-btn");
        const swOnBtn = document.getElementById("sim-sw-on-btn");
        if (swOffBtn) swOffBtn.classList.toggle("active", !simIncludeScheduledActions);
        if (swOnBtn) swOnBtn.classList.toggle("active", simIncludeScheduledActions);

        simBearShield = snap.simBearShield != null ? Boolean(snap.simBearShield) : true;
        simOrderPreset = snap.simOrderPreset || "tax_optimal";
        const validIds = new Set(SIM_ASSETS.map(a => a.id));
        if (Array.isArray(snap.simAssetOrder) && snap.simAssetOrder.length > 0) {
          const filtered = snap.simAssetOrder.filter(id => validIds.has(id));
          SIM_ASSETS.forEach(a => {
            if (!filtered.includes(a.id)) filtered.push(a.id);
          });
          simAssetOrder = filtered;
        } else {
          simAssetOrder = [...SIM_ASSETS].sort((a, b) => a.default_rank - b.default_rank).map(a => a.id);
        }
        updateSimOrderPresetButtons();

        const rawActions = Array.isArray(snap.simScheduledActions)
          ? snap.simScheduledActions
          : (Array.isArray(snap.simScheduledWithdrawals) ? snap.simScheduledWithdrawals : []);
        simScheduledActions = normalizeSimScheduledActions(rawActions);
        simScheduledWithdrawals = simScheduledActions;
        selectedSimWithdrawalIdx = simScheduledActions.length > 0 ? 0 : -1;

        // 5b. Projection-model lens & risk parameters
        {
          if (snap.simActiveModel) {
            const validModels = ["single_path", "stress", "range", "historical"];
            simActiveModel = validModels.includes(snap.simActiveModel) ? snap.simActiveModel
              : (snap.simActiveModel === "probability" ? "range" : "single_path");
          } else {
            simActiveModel = "single_path";
          }
          const validStress = ["none", "gfc2008", "dotcom2000", "covid2020", "rates2022", "custom"];
          simStressScenario = (simActiveModel !== "single_path" && validStress.includes(snap.simStressScenario))
            ? snap.simStressScenario
            : "none";
          simCustomShockPct = Math.max(-90, Math.min(0, Number(snap.simCustomShockPct ?? -30)));
          simCustomDownMonths = Math.max(3, Math.min(36, Math.round(Number(snap.simCustomDownMonths ?? 12))));
          simCustomBroadBounceMonths = Math.max(3, Math.min(STRESS_NO_BOUNCE_MONTHS, Math.round(Number(snap.simCustomBroadBounceMonths ?? 24))));
          simCustomStockBounceMonths = Math.max(3, Math.min(STRESS_NO_BOUNCE_MONTHS, Math.round(Number(snap.simCustomStockBounceMonths ?? 36))));
          mcSingleStockVolPct = Math.max(0, Math.min(100, Number(snap.mcSingleStockVolPct ?? snap.mcGoogVolPct ?? MC_REF_GOOG_VOL)));
          mcBroadEquityVolPct = Math.max(0, Math.min(100, Number(snap.mcBroadEquityVolPct ?? snap.mcNonGoogVolPct ?? MC_REF_NONGOOG_VOL)));
          mcGoogVolPct = mcSingleStockVolPct;
          mcNonGoogVolPct = mcBroadEquityVolPct;
          mcReturnModel = snap.mcReturnModel === "historical" ? "historical" : "normal";
          mcSingleStockCorr = Math.max(-0.99, Math.min(0.99, Number(snap.mcSingleStockCorr ?? snap.mcGoogRestCorr ?? MC_REF_CORR)));
          mcGoogRestCorr = mcSingleStockCorr;
          mcSimulations = 500;
          mcSuccessThresholdUsd = Math.max(0, Math.round(Number(snap.mcSuccessThresholdUsd ?? 0)));
          cohortCenterMode = snap.cohortCenterMode === "raw" ? "raw" : "anchor";
          cohortInflationMode = "real_cpi";
          cohortStartFrom = snap.cohortStartFrom != null && Number(snap.cohortStartFrom) >= 0 ? Math.round(Number(snap.cohortStartFrom)) : -1;
          if (typeof syncSimModelControls === "function") syncSimModelControls();
        }

        recomputeSimBars();

        // 6. Re-render all screens so every page reflects the loaded Scenario
        renderChartAndNav();
        renderHoldingsTable();
        renderHoldingsPie();
        renderRelChartAndNav();
        renderGoogleStockTables();
        updateSimSummaryDOM();
        renderSimChartAndNav();
        renderSimLiquidationTable();
        updateSsnSummaryDOM();
        renderSsnChartAndNav();
        renderSsnTables();
      } finally {
        _suppressDirtyCheck = prevSuppress;
      }
      loadedSettingsSnapshot = getCurrentSettingsSnapshot();
      isSaveDirty = false;
      updateSaveStatusUI(false);
    }

    function captureSimSnapshotDataUrl(modelTitle = "") {
      try {
        const W = 1160;
        const H = 420;
        const canvas = document.createElement("canvas");
        if (!canvas || typeof canvas.getContext !== "function") return "";
        canvas.width = W;
        canvas.height = H;
        const ctx = canvas.getContext("2d");
        if (!ctx || typeof canvas.toDataURL !== "function") return "";

        ctx.fillStyle = "#0b1120";
        ctx.fillRect(0, 0, W, H);

        // Subtle outer border
        ctx.strokeStyle = "#26334d";
        ctx.lineWidth = 1.5;
        if (typeof ctx.roundRect === "function") {
          ctx.beginPath();
          ctx.roundRect(5, 5, W - 10, H - 10, 11);
          ctx.stroke();
        } else {
          ctx.strokeRect(5, 5, W - 10, H - 10);
        }

        const padL = 75, padR = 30, padT = 32, padB = 41;
        const plotW = W - padL - padR;
        const plotH = H - padT - padB;
        const axisY = padT + plotH;
        const bars = Array.isArray(SIM_BARS) ? SIM_BARS : [];
        if (bars.length === 0) return "";

        const deflatorAt = idx => Math.pow(1 + (Number(simInflationPct) || 0) / 100, idx / 12);

        // Determine Y max in Today's $
        // DESIGN INTENT (`DA2-10b` — coupled with `render_model_snapshot_jpg` in `advisorbot_server.py`):
        // Ensure `HIST_LAST_RESULT` is hydrated on demand when `simActiveModel === "historical"` so both
        // Monte Carlo (`range`) and Historical Cohorts (`historical`) snapshots render quantile fan bands + P50!
        if (simActiveModel === "historical" && !HIST_LAST_RESULT && typeof getCohortResult === "function") {
          try { getCohortResult(); } catch (_e) {}
        }
        let maxVal = 1_000_000;
        if (simActiveModel === "range" && MC_LAST_RESULT && Array.isArray(MC_LAST_RESULT.p90)) {
          MC_LAST_RESULT.p90.forEach((v, i) => {
            const rv = v / deflatorAt(i);
            if (rv > maxVal) maxVal = rv;
          });
        } else if (simActiveModel === "historical" && HIST_LAST_RESULT && Array.isArray(HIST_LAST_RESULT.p90)) {
          HIST_LAST_RESULT.p90.forEach((v, i) => {
            const rv = v / deflatorAt(i);
            if (rv > maxVal) maxVal = rv;
          });
        } else {
          bars.forEach((b, i) => {
            const rv = (Number(b.totalVal || b.total) || 0) / deflatorAt(i);
            if (rv > maxVal) maxVal = rv;
          });
        }

        const startVal = (bars[0] ? Number(bars[0].totalVal || bars[0].total) : 15.5e6) / deflatorAt(0);
        const isLog = simScaleMode === "log" || (simScaleMode === "auto" && (maxVal > 60e6 || (startVal > 0 && maxVal / startVal >= 3.2)));
        const y0 = 2.5e6;
        const logMilestones = [
          5e6, 10e6, 20e6, 30e6, 50e6, 75e6, 100e6, 150e6, 200e6, 250e6, 300e6, 500e6,
          750e6, 1e9, 1.5e9, 2e9, 3e9, 5e9, 10e9, 20e9, 50e9
        ];
        const yMax = isLog ? (logMilestones.find(m => m >= maxVal * 1.04) || (Math.ceil(maxVal / 1e9) * 1e9)) : maxVal * 1.08;

        const valToNorm = v => {
          const cl = Math.max(0, v || 0);
          return isLog ? Math.log10(1 + cl / y0) / Math.log10(1 + yMax / y0) : (yMax > 0 ? cl / yMax : 0);
        };
        const yAt = v => axisY - Math.min(1.0, Math.max(0, valToNorm(v))) * plotH;
        const xAt = i => padL + (i / Math.max(1, bars.length - 1)) * plotW;

        // Ticks
        let ticks = [];
        if (!isLog) {
          ticks = [0, yMax * 0.25, yMax * 0.5, yMax * 0.75, yMax];
        } else {
          const candidates = [0, 5e6, 10e6, 25e6, 50e6, 100e6, 250e6, 500e6, 1e9, 2e9, 5e9, 10e9, 20e9];
          ticks = [0];
          let lastY = yAt(0);
          for (const c of candidates) {
            if (c <= 0 || c > yMax) continue;
            const cy = yAt(c);
            if (lastY - cy >= 38) {
              ticks.push(c);
              lastY = cy;
            }
          }
          if (yAt(ticks[ticks.length - 1]) - yAt(yMax) >= 28) {
            ticks.push(yMax);
          } else {
            ticks[ticks.length - 1] = yMax;
          }
        }

        // Horizontal gridlines + Y labels
        ctx.font = "bold 19px ui-monospace, SFMono-Regular, Menlo, monospace";
        ctx.textAlign = "right";
        ctx.textBaseline = "middle";
        ticks.forEach(tv => {
          const yy = yAt(tv);
          ctx.strokeStyle = tv === 0 ? "rgba(45, 60, 90, 0.95)" : "rgba(30, 48, 74, 0.7)";
          ctx.lineWidth = tv === 0 ? 1.5 : 1;
          ctx.beginPath();
          ctx.moveTo(padL, yy);
          ctx.lineTo(padL + plotW, yy);
          ctx.stroke();

          let lbl = "$0";
          if (tv >= 1e9) lbl = (tv % 1e9) !== 0 ? `$${(tv / 1e9).toFixed(1)}B` : `$${Math.round(tv / 1e9)}B`;
          else if (tv >= 1e6) lbl = ((tv % 1e6) !== 0 && tv < 100e6) ? `$${(tv / 1e6).toFixed(1)}M` : `$${Math.round(tv / 1e6)}M`;
          else if (tv > 0) lbl = `$${Math.round(tv / 1e3)}K`;

          ctx.fillStyle = "#94a3b8";
          ctx.fillText(lbl, padL - 10, yy);
        });

        // X-axis year ticks (Today, +10y, +20y, +30y, +40y)
        ctx.textAlign = "center";
        ctx.textBaseline = "top";
        ctx.font = "bold 19px ui-monospace, SFMono-Regular, Menlo, monospace";
        for (let yr = 0; yr <= 40; yr += 10) {
          const mIdx = yr * 12;
          if (mIdx > bars.length - 1) break;
          const xx = xAt(mIdx);
          ctx.strokeStyle = "#64748b";
          ctx.lineWidth = 1;
          ctx.beginPath();
          ctx.moveTo(xx, axisY);
          ctx.lineTo(xx, axisY + 6);
          ctx.stroke();
          const lbl = yr === 0 ? "Today" : `+${yr}y`;
          ctx.fillStyle = "#94a3b8";
          ctx.fillText(lbl, xx, axisY + 10);
        }

        // Stacked Asset Bars
        const assetMap = Object.fromEntries((SIM_ASSETS || []).map(a => [a.id, a]));
        const rawOrder = (Array.isArray(simAssetOrder) && simAssetOrder.length > 0)
          ? simAssetOrder
          : (SIM_ASSETS || []).map(a => a.id);
        const order = rawOrder.slice().reverse();
        const bw = Math.max(2, plotW / bars.length);
        const isFan = (simActiveModel === "range" && MC_LAST_RESULT) || (simActiveModel === "historical" && HIST_LAST_RESULT);

        ctx.save();
        ctx.globalAlpha = isFan ? 0.40 : 0.92;
        for (let i = 0; i < bars.length; i++) {
          const b = bars[i];
          const defl = deflatorAt(i);
          const x = padL + (i / Math.max(1, bars.length - 1)) * plotW - bw / 2;
          const aVals = b.assetVals || b.balances || {};
          let cumVal = 0;

          if (Object.keys(aVals).length > 0) {
            for (const aid of order) {
              const a = assetMap[aid];
              if (!a || (a.is_unvested_goog && !simIncludeUnvested)) continue;
              const val = (Number(aVals[aid]) || 0) / defl;
              if (val <= 0.01) continue;
              const yBot = yAt(cumVal);
              const yTop = yAt(cumVal + val);
              ctx.fillStyle = a.color || "#38bdf8";
              ctx.fillRect(x, yTop, bw, Math.max(0.6, yBot - yTop));
              cumVal += val;
            }
          } else {
            const tot = (Number(b.totalVal || b.total) || 0) / defl;
            if (tot > 0.01) {
              const yBot = yAt(0);
              const yTop = yAt(tot);
              ctx.fillStyle = "#38bdf8";
              ctx.fillRect(x, yTop, bw, Math.max(0.6, yBot - yTop));
            }
          }
        }
        ctx.restore();

        // Quantile fan bands + P50 median curve (`DA2-10b`: rendered for BOTH Monte Carlo `"range"` and Historical Cohorts `"historical"`)
        const activeFan = (simActiveModel === "range" && MC_LAST_RESULT && Array.isArray(MC_LAST_RESULT.p90))
          ? MC_LAST_RESULT
          : ((simActiveModel === "historical" && HIST_LAST_RESULT && Array.isArray(HIST_LAST_RESULT.p90)) ? HIST_LAST_RESULT : null);
        if (activeFan) {
          const fan = activeFan;
          const isHist = simActiveModel === "historical";
          const drawBand = (hiArr, loArr, fill) => {
            ctx.fillStyle = fill;
            ctx.beginPath();
            for (let i = 0; i < bars.length; i++) {
              const x = xAt(i);
              const y = yAt((hiArr[i] || 0) / deflatorAt(i));
              if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
            }
            for (let i = bars.length - 1; i >= 0; i--) {
              const x = xAt(i);
              const y = yAt((loArr[i] || 0) / deflatorAt(i));
              ctx.lineTo(x, y);
            }
            ctx.closePath();
            ctx.fill();
          };
          drawBand(fan.p90, fan.p10, isHist ? "rgba(251, 191, 36, 0.16)" : "rgba(90, 216, 166, 0.15)");
          drawBand(fan.p75, fan.p25, isHist ? "rgba(251, 191, 36, 0.28)" : "rgba(90, 216, 166, 0.28)");
          ctx.strokeStyle = isHist ? "#FBBF24" : "#5AD8A6";
          ctx.lineWidth = 2.5;
          ctx.beginPath();
          for (let i = 0; i < bars.length; i++) {
            const x = xAt(i);
            const y = yAt((fan.p50[i] || 0) / deflatorAt(i));
            if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
          }
          ctx.stroke();
        }

        // Stress scenario year 1 crash shading
        if (simActiveModel === "stress") {
          const x0 = xAt(0);
          const x12 = xAt(Math.min(bars.length - 1, 12));
          ctx.fillStyle = "rgba(244, 63, 94, 0.15)";
          ctx.fillRect(x0, padT, x12 - x0, plotH);
        }

        // Investor Milestone Age vertical markers & capsules (matching app_simulator.js)
        const bdayMilestones = [];
        if (HAS_SECOND_INVESTOR && INV1) {
          bdayMilestones.push({
            label: `${INV1.name}'s ${INV1.milestone_age || 85}th`,
            stroke: "#38bdf8",
            textFill: "#bae6fd",
            idx: bars.findIndex((b, idx) => ageOnIso(INV1.birth_year, INV1.birth_month, INV1.birth_day, b.iso, idx) >= (INV1.milestone_age || 85)),
          });
        }
        bdayMilestones.push({
          label: `${INV0.name}'s ${INV0.milestone_age || 85}th`,
          stroke: "#2dd4bf",
          textFill: "#99f6e4",
          idx: bars.findIndex((b, idx) => ageOnIso(INV0.birth_year, INV0.birth_month, INV0.birth_day, b.iso, idx) >= (INV0.milestone_age || 85)),
        });
        const placedTopTags = [];
        const pillFont = "700 14px ui-monospace, 'SF Mono', Menlo, monospace";
        bdayMilestones.forEach(bm => {
          if (bm.idx < 0 || bm.idx >= bars.length) return;
          const mx = xAt(bm.idx);
          ctx.save();
          ctx.strokeStyle = bm.stroke;
          ctx.setLineDash([4, 3]);
          ctx.lineWidth = 1.6;
          ctx.beginPath();
          ctx.moveTo(mx, padT);
          ctx.lineTo(mx, axisY);
          ctx.stroke();
          ctx.restore();

          ctx.font = pillFont;
          const pillW = Math.max(76, Math.round(ctx.measureText(bm.label).width + 18));
          const pillH = 20;
          const pillX = Math.min(padL + plotW - pillW - 3, Math.max(padL + 3, mx - pillW / 2));
          const pillCx = pillX + pillW / 2;
          let rowSlot = 0;
          while (rowSlot < 3 && placedTopTags.some(p => p.rowSlot === rowSlot && Math.abs(p.cx - pillCx) < (p.w / 2 + pillW / 2 + 6))) {
            rowSlot++;
          }
          placedTopTags.push({ cx: pillCx, w: pillW, rowSlot });
          const pillY = padT + 3 + rowSlot * (pillH + 4);

          ctx.fillStyle = "rgba(15, 23, 42, 0.94)";
          ctx.strokeStyle = bm.stroke;
          ctx.lineWidth = 1.1;
          if (typeof ctx.roundRect === "function") {
            ctx.beginPath();
            ctx.roundRect(pillX, pillY, pillW, pillH, 4);
            ctx.fill();
            ctx.stroke();
          } else {
            ctx.fillRect(pillX, pillY, pillW, pillH);
            ctx.strokeRect(pillX, pillY, pillW, pillH);
          }
          ctx.fillStyle = bm.textFill;
          ctx.textAlign = "center";
          ctx.textBaseline = "middle";
          ctx.fillText(bm.label, pillCx, pillY + pillH / 2);
        });

        // Scheduled Action markers & capsules (matching app_simulator.js ACT_STYLE)
        if (simIncludeScheduledActions) {
          const ACT_STYLE = {
            withdrawal: { stroke: "#f43f5e", bg: "rgba(76, 5, 25, 0.94)", fg: "#fecdd3", sym: "\u2193" },
            deposit:    { stroke: "#10b981", bg: "rgba(6, 78, 59, 0.94)", fg: "#a7f3d0", sym: "\u2191" },
            transfer:   { stroke: "#22d3ee", bg: "rgba(8, 51, 68, 0.94)", fg: "#a5f3fc", sym: "\u21C4" },
          };
          const placedSwTags = [];
          for (let i = 0; i < bars.length; i++) {
            const evs = (bars[i].schedActionEvents || bars[i].schedWithdrawEvents || []).filter(ev => (ev.cadence || "one_time") === "one_time");
            if (evs.length === 0) continue;
            const firstEv = evs[0];
            const st = ACT_STYLE[firstEv.type] || ACT_STYLE.withdrawal;
            const ax = xAt(i);

            ctx.save();
            ctx.strokeStyle = st.stroke;
            ctx.setLineDash([4, 3]);
            ctx.lineWidth = 1.6;
            ctx.beginPath();
            ctx.moveTo(ax, padT);
            ctx.lineTo(ax, axisY);
            ctx.stroke();
            ctx.restore();

            const extraCnt = evs.length - 1;
            const rawCmt = `${st.sym} ${firstEv.comment || firstEv.type || "Action"}`;
            const maxChars = extraCnt > 0 ? 13 : 16;
            const truncCmt = (rawCmt.length > maxChars ? rawCmt.slice(0, maxChars - 1) + "\u2026" : rawCmt) + (extraCnt > 0 ? ` (+${extraCnt})` : "");

            ctx.font = pillFont;
            const pillW = Math.max(70, Math.round(ctx.measureText(truncCmt).width + 18));
            const pillH = 20;
            const pillX = Math.min(padL + plotW - pillW - 3, Math.max(padL + 3, ax - pillW / 2));
            const pillCx = pillX + pillW / 2;
            let rowSlot = 0;
            while (rowSlot < 5 && placedSwTags.some(p => p.rowSlot === rowSlot && Math.abs(p.cx - pillCx) < (p.w / 2 + pillW / 2 + 6))) {
              rowSlot++;
            }
            placedSwTags.push({ cx: pillCx, w: pillW, rowSlot });
            const pillY = padT + 51 + rowSlot * (pillH + 4);

            ctx.fillStyle = st.bg;
            ctx.strokeStyle = st.stroke;
            ctx.lineWidth = 1.1;
            if (typeof ctx.roundRect === "function") {
              ctx.beginPath();
              ctx.roundRect(pillX, pillY, pillW, pillH, 4);
              ctx.fill();
              ctx.stroke();
            } else {
              ctx.fillRect(pillX, pillY, pillW, pillH);
              ctx.strokeRect(pillX, pillY, pillW, pillH);
            }
            ctx.fillStyle = st.fg;
            ctx.textAlign = "center";
            ctx.textBaseline = "middle";
            ctx.fillText(truncCmt, pillCx, pillY + pillH / 2);
          }
        }

        const out = canvas.toDataURL("image/png");
        return (typeof out === "string" && out.startsWith("data:image/")) ? out : "";
      } catch (_err) {
        return "";
      }
    }

    function normalizeModelEntry(raw) {
      if (!raw || typeof raw !== "object" || !raw.name) return null;
      const id = String(raw.id || "").trim() || "001";
      const file = raw.file || raw.filename || `${raw.name}.json`;
      const snapRel = raw.snapshot || "";
      const savesPrefix = PAYLOAD.active_portfolio_id ? `Portfolios/${PAYLOAD.active_portfolio_id}/Scenarios` : "Scenarios";
      const snapUrl = raw.snapshot_url || (snapRel ? `${savesPrefix}/${snapRel}` : "");
      return {
        id,
        name: String(raw.name).trim(),
        file,
        filename: file,
        snapshot: snapRel,
        snapshot_url: snapUrl,
        created_at: raw.created_at || raw.updated_at || "",
        updated_at: raw.updated_at || "",
        is_protected: Boolean(raw.is_protected),
        author: (raw.author && typeof raw.author === "object") ? raw.author : { type: "human", model: null },
        goog_price_anchor: raw.goog_price_anchor != null ? Number(raw.goog_price_anchor) : (Number(currentGoogPrice) || null),
        kpi_summary: (raw.kpi_summary && typeof raw.kpi_summary === "object") ? raw.kpi_summary : null,
        settings: (raw.settings && typeof raw.settings === "object") ? raw.settings : null,
        notes_md: String(raw.notes_md || ""),
      };
    }

    function resolveModelSnapshotUrl(entry) {
      if (!entry) return "";
      if (entry.snapshot_url) return entry.snapshot_url;
      const savesPrefix = PAYLOAD.active_portfolio_id ? `Portfolios/${PAYLOAD.active_portfolio_id}/Scenarios` : "Scenarios";
      let snapPath = entry.snapshot || "";
      if (snapPath.endsWith(".jpg")) snapPath = snapPath.replace(/\.jpg$/, ".png");
      const rawUrl = snapPath ? `${savesPrefix}/${snapPath}` : "";
      if (!rawUrl) return "";
      if (rawUrl.startsWith("data:")) return rawUrl;
      const cleanUrl = rawUrl.split("?")[0];
      const stamp = entry.updated_at || entry.created_at || "";
      return stamp ? `${cleanUrl}?v=${encodeURIComponent(stamp)}` : cleanUrl;
    }

    function ingestSavesListPayload(data, retainSelection = false) {
      if (!data || typeof data !== "object") return;
      const rawItems = Array.isArray(data.models) ? data.models : (Array.isArray(data.saves) ? data.saves : []);
      const nextList = [];
      const nextReg = {};
      rawItems.forEach(item => {
        const norm = normalizeModelEntry(item);
        if (norm) {
          nextList.push(norm);
          nextReg[norm.name] = norm;
        }
      });
      if (nextList.length > 0) {
        modelsList = nextList;
        savesRegistry = nextReg;
      }
      if (retainSelection) return;
      if (data.active_id) {
        const m = findModelEntry(data.active_id);
        if (m) {
          currentModelId = m.id;
          currentSaveName = m.name;
        }
      } else if (data.active_save && savesRegistry[data.active_save]) {
        currentSaveName = data.active_save;
        currentModelId = savesRegistry[data.active_save].id || currentModelId;
      }
    }

    function persistLocalSavesRegistry() {
      try {
        // SECURITY DO: Persist IDs and names metadata only (`SA-23` localStorage minimization).
        // SECURITY DON'T: Never store `kpi_summary`, `notes_md`, settings, or portfolio holdings in `localStorage`.
        const minimalModels = modelsList.map(m => ({
          id: m.id,
          name: m.name,
          updated_at: m.updated_at || "",
        }));
        localStorage.setItem(SAVES_LOCAL_KEY, JSON.stringify({
          active_id: currentModelId,
          active_save: currentSaveName,
          models: minimalModels,
        }));
      } catch (_e) {
        // Ignore storage quota errors
      }
    }

    function loadInitialSavesRegistry() {
      savesRegistry = {};
      modelsList = [];
      // Seed from embedded PAYLOAD.saves_bundle first (authoritative disk state at render time)
      const bundle = PAYLOAD.saves_bundle || {};
      ingestSavesListPayload(bundle);

      // Merge any offline localStorage cache only when bundle was empty
      if (modelsList.length === 0) {
        try {
          const raw = localStorage.getItem(SAVES_LOCAL_KEY);
          if (raw) {
            const parsed = JSON.parse(raw);
            ingestSavesListPayload(parsed);
          }
        } catch (_e) {
          // Ignore localStorage parse errors
        }
      }

      if (!savesRegistry[currentSaveName]) {
        const first = modelsList[0];
        if (first) {
          currentModelId = first.id;
          currentSaveName = first.name;
        } else {
          currentModelId = "";
          currentSaveName = "";
        }
      }
    }

    async function refreshSavesFromServer() {
      try {
        const { ok, status, data } = await apiJson("/api/models");
        if (!ok || !data) throw new Error("HTTP " + status);
        if (data && data.ok && (Array.isArray(data.models) || Array.isArray(data.saves))) {
          savesServerOnline = true;
          ingestSavesListPayload(data, isSaveDirty);
          persistLocalSavesRegistry();
          if (currentScreen === "models" && !(selectedModelDetailId && isEditingDetailNotes)) {
            renderModelsScreen();
          }
          return data;
        }
      } catch (_err) {
        savesServerOnline = false;
      }
      return null;
    }

    async function initSavesSystem() {
      _suppressDirtyCheck = true;
      loadInitialSavesRegistry();

      const initialEntry = findModelEntry(currentModelId) || (currentSaveName ? savesRegistry[currentSaveName] : null);
      if (initialEntry && initialEntry.settings) {
        currentModelId = initialEntry.id || currentModelId;
        currentSaveName = initialEntry.name;
        applySettingsSnapshot(initialEntry.settings);
      } else {
        currentModelId = "";
        currentSaveName = "";
        if (typeof syncSimTaxControls === "function") syncSimTaxControls();
        loadedSettingsSnapshot = getCurrentSettingsSnapshot();
      }
      _suppressDirtyCheck = false;
      isSaveDirty = false;
      updateSaveStatusUI(false);
      renderModelsScreen();

      let serverData = await refreshSavesFromServer();
      if (!isOfflineFileSnapshot() && PAYLOAD && PAYLOAD.active_portfolio_id && serverData && !serverData.active_save && (!Array.isArray(serverData.models) || serverData.models.length === 0)) {
        try {
          const selRes = await apiJson("/api/portfolios/select", {
            body: { portfolio_id: PAYLOAD.active_portfolio_id, rebuild: false },
          });
          if (selRes && selRes.ok) {
            serverData = (await refreshSavesFromServer()) || serverData;
          }
        } catch (_e) {}
      }
      checkSaveDirtyState();
      if (!isSaveDirty && serverData && (serverData.active_id || serverData.active_save)) {
        const srvEntry = findModelEntry(serverData.active_id || serverData.active_save);
        if (srvEntry && srvEntry.settings) {
          currentModelId = srvEntry.id;
          currentSaveName = srvEntry.name;
          applySettingsSnapshot(srvEntry.settings);
        }
      }
      updateSaveStatusUI(false);
      renderModelsScreen();
    }

    function yieldForUiPaint() {
      return new Promise(resolve => {
        if (typeof requestAnimationFrame === "function") {
          requestAnimationFrame(() => setTimeout(resolve, 0));
        } else {
          setTimeout(resolve, 0);
        }
      });
    }

    async function persistSaveToBackend(saveName, settingsObj, setActive = true, opts = {}) {
      const reportProgress = typeof opts.onProgress === "function" ? opts.onProgress : null;
      const nowStr = new Date().toISOString().replace("T", " ").slice(0, 19);
      const existing = opts.createNew ? null : (findModelEntry(opts.id) || savesRegistry[saveName]);
      let kpiSummary = opts.kpi_summary || null;
      if (!kpiSummary) {
        if (reportProgress) await reportProgress("Computing scenario KPIs & forecast metrics...");
        try {
          kpiSummary = computeCurrentKpiSummary();
        } catch (err) {
          console.warn("computeCurrentKpiSummary fallback:", err);
          kpiSummary = null;
        }
      }
      let snapshotDataUrl = opts.snapshot_data_url || "";
      if (!snapshotDataUrl) {
        if (reportProgress) await reportProgress("Capturing simulator chart snapshot...");
        snapshotDataUrl = captureSimSnapshotDataUrl(saveName);
      }
      const googAnchor = Number(currentGoogPrice) || null;

      if (isOfflineFileSnapshot()) {
        if (reportProgress) await reportProgress("Writing scenario to local browser storage...");
        const optimistic = normalizeModelEntry({
          ...(existing || {}),
          id: existing ? existing.id : (opts.id || String(modelsList.length + 1).padStart(3, "0")),
          name: saveName,
          updated_at: nowStr,
          created_at: existing ? existing.created_at : nowStr,
          author: opts.author || (existing ? existing.author : { type: "human", model: null }),
          goog_price_anchor: googAnchor,
          kpi_summary: kpiSummary,
          settings: settingsObj,
          notes_md: opts.notes_md != null ? opts.notes_md : (existing ? existing.notes_md : ""),
          snapshot_url: snapshotDataUrl || (existing ? existing.snapshot_url : ""),
        });
        if (optimistic) {
          savesRegistry[saveName] = optimistic;
          const idx = modelsList.findIndex(m => m.id === optimistic.id || m.name === saveName);
          if (idx >= 0) modelsList[idx] = optimistic;
          else modelsList.push(optimistic);
          if (setActive) {
            currentModelId = optimistic.id;
            currentSaveName = optimistic.name;
          }
        }
        persistLocalSavesRegistry();
        renderModelsScreen();
        return { ok: true, status: 0, offline: true, data: null };
      }

      try {
        if (reportProgress) await reportProgress(`Writing scenario file "${saveName}" on server...`);
        const { ok, status, data, error } = await apiJson("/api/models", {
          timeoutMs: 60000,
          onProgress: reportProgress,
          body: {
            portfolio_id: (PAYLOAD && PAYLOAD.active_portfolio_id) || undefined,
            id: existing ? existing.id : undefined,
            name: saveName,
            settings: settingsObj,
            set_active: setActive,
            create_new: Boolean(opts.createNew),
            overwrite_existing: Boolean(opts.overwriteExisting),
            author: opts.author || (existing ? existing.author : { type: "human", model: null }),
            notes_md: opts.notes_md != null ? opts.notes_md : undefined,
            kpi_summary: kpiSummary,
            goog_price_anchor: googAnchor,
            snapshot_data_url: snapshotDataUrl || undefined,
          },
        });
        if (ok && data && data.ok !== false) {
          if (reportProgress) await reportProgress("Updating scenarios index & cards...");
          savesServerOnline = true;
          ingestSavesListPayload(data);
          if (data.saved_id) {
            const savedEntry = findModelEntry(data.saved_id);
            if (savedEntry && setActive) {
              currentModelId = savedEntry.id;
              currentSaveName = savedEntry.name;
            }
          }
          persistLocalSavesRegistry();
          renderModelsScreen();
          return { ok: true, status: status || 200, data };
        }
        if (status === 0 || status >= 500) {
          savesServerOnline = false;
        }
        let errMsg = (data && data.error) || error || "";
        if (!errMsg) {
          if (status === 403) {
            errMsg = "Open AdvisorBot with --open in the same browser profile, then retry Save in this tab.";
          } else if (status === 0) {
            errMsg = "Could not reach the AdvisorBot server to save.";
          } else {
            errMsg = `Save failed (HTTP ${status || "error"}).`;
          }
        }
        return { ok: false, status: status || 0, error: errMsg, data };
      } catch (err) {
        savesServerOnline = false;
        const msg = err && err.name === "AbortError"
          ? "Save request timed out while waiting for the server."
          : ((err && err.message) || "Save request failed.");
        return { ok: false, status: 0, error: msg, data: null };
      }
    }

    async function saveToCurrentFile() {
      if (isDemoModeActive()) {
        showDemoBlockedAlert();
        return;
      }
      const entry = findModelEntry(currentModelId) || (currentSaveName ? savesRegistry[currentSaveName] : null);
      if (!entry && !String(currentSaveName || "").trim()) {
        await promptSaveCurrentAsNewModel();
        return;
      }
      const targetName = (entry ? entry.name : (currentSaveName || "Default")).trim() || "Default";
      let overwriteExisting = false;
      if (entry && entry.is_protected) {
        if (typeof window.confirm === "function") {
          const ok = window.confirm(
            `"${targetName}" is a Protected Scenario.\n\nClick OK to overwrite "${targetName}", or Cancel to save your changes as a new Scenario.`
          );
          if (!ok) {
            await promptSaveCurrentAsNewModel();
            return;
          }
        }
        overwriteExisting = true;
      }
      const snap = getCurrentSettingsSnapshot();
      const saveRes = await persistSaveToBackend(targetName, snap, true, {
        id: entry ? entry.id : undefined,
        overwriteExisting,
      });
      const backdrop = document.getElementById("save-dialog-backdrop");
      if (!saveRes || !saveRes.ok) {
        if (backdrop && backdrop.classList.contains("open")) {
          showSaveDialogMsg((saveRes && saveRes.error) || "Failed to save Scenario.", "#fda4af");
        } else {
          const status = document.getElementById("save-status-sub");
          if (status) {
            status.textContent = "Save failed · Retry Save";
            status.title = (saveRes && saveRes.error) || "Your edits are still open.";
          }
        }
        return;
      }
      loadedSettingsSnapshot = snap;
      isSaveDirty = !snapshotsEqual(getCurrentSettingsSnapshot(), snap);
      updateSaveStatusUI(!isSaveDirty);
      if (backdrop && backdrop.classList.contains("open")) {
        selectedDialogSaveName = currentSaveName;
        renderSaveDialogList();
        showSaveDialogMsg(`Saved current settings & 1160\u00D7420 snapshot to "${currentSaveName}".`, "#34d399");
      }
    }

    async function reloadCurrentScenario() {
      if (window._isReloadingScenario) return;
      window._isReloadingScenario = true;

      const btnEl = document.getElementById("nav-btn-models");
      const reloadBtn = document.getElementById("nav-reload-btn");
      const subEl = document.getElementById("save-status-sub");
      const dotEl = document.getElementById("save-status-dot");
      const rawName = String(currentSaveName || "").trim();

      if (btnEl) btnEl.classList.add("is-reloading");
      if (reloadBtn) {
        reloadBtn.style.display = "inline-flex";
        reloadBtn.classList.add("spinning");
      }
      if (subEl) {
        subEl.textContent = "Reloading scenario...";
        subEl.classList.remove("dirty-text");
        subEl.style.color = "#38bdf8";
      }
      if (dotEl) {
        dotEl.classList.add("reloading");
      }

      // Yield main thread so the browser paints the start of the animation immediately
      await new Promise(resolve => setTimeout(resolve, 50));

      const startTime = Date.now();
      try {
        if (savesServerOnline) {
          try {
            await refreshSavesFromServer();
          } catch (_e) {}
        }
        const entry = findModelEntry(currentModelId) || (currentSaveName ? savesRegistry[currentSaveName] : null);
        let targetSnap = null;
        if (entry && entry.settings) {
          targetSnap = entry.settings;
        } else if (loadedSettingsSnapshot) {
          targetSnap = loadedSettingsSnapshot;
        }
        if (targetSnap) {
          applySettingsSnapshot(targetSnap);
          loadedSettingsSnapshot = getCurrentSettingsSnapshot();
          isSaveDirty = false;
        }
      } catch (err) {
        console.error("Failed to reload scenario:", err);
      } finally {
        // Guarantee the user sees a smooth visual confirmation for at least 500ms
        const elapsed = Date.now() - startTime;
        const minAnimMs = 500;
        if (elapsed < minAnimMs) {
          await new Promise(resolve => setTimeout(resolve, minAnimMs - elapsed));
        }

        if (btnEl) btnEl.classList.remove("is-reloading");
        if (reloadBtn) {
          reloadBtn.classList.remove("spinning");
        }
        if (dotEl) {
          dotEl.classList.remove("reloading");
        }

        updateSaveStatusUI("reloaded");

        const backdrop = document.getElementById("save-dialog-backdrop");
        if (backdrop && backdrop.classList.contains("open")) {
          renderSaveDialogList();
          showSaveDialogMsg(`Reverted modifications to "${rawName || 'Scenario'}".`, "#38bdf8");
        }
        window._isReloadingScenario = false;
      }
    }

    async function promptSaveCurrentAsNewModel() {
      if (isDemoModeActive()) {
        showDemoBlockedAlert();
        return;
      }
      const base = (currentSaveName || "Scenario").replace(/\s*\(Copy\)$/i, "");
      await openSaveDialog("save", { prefillName: `${base} (Custom)` });
    }

    function countModelActionSummary(entry) {
      if (entry && entry.kpi_summary && entry.kpi_summary.action_summary) {
        const a = entry.kpi_summary.action_summary;
        return {
          hypothetical_count: Number(a.hypothetical_count) || 0,
          transfers: Number(a.transfers) || 0,
          deposits: Number(a.deposits) || 0,
          withdrawals: Number(a.withdrawals) || 0,
        };
      }
      const s = (entry && entry.settings) || {};
      const hyps = Array.isArray(s.simHypotheticalAssets) ? s.simHypotheticalAssets : [];
      const acts = Array.isArray(s.simScheduledActions)
        ? s.simScheduledActions
        : (Array.isArray(s.simScheduledWithdrawals) ? s.simScheduledWithdrawals : []);
      let transfers = 0, deposits = 0, withdrawals = 0;
      acts.forEach(act => {
        const t = String(act?.type || "withdrawal").toLowerCase();
        if (t === "transfer") transfers++;
        else if (t === "deposit") deposits++;
        else withdrawals++;
      });
      return { hypothetical_count: hyps.length, transfers, deposits, withdrawals };
    }

    function countModelUpvotes(notesMd) {
      if (!notesMd) return 0;
      const matches = String(notesMd).match(/(?:Concurred|Upvoted)\s*(?:&|and)?\s*(?:Upvoted)?|###\s*\u25B2\s*Concurrence/gi);
      return matches ? matches.length : 0;
    }

    function formatModelLensLabel(lens) {
      const map = {
        single_path: "Single-Path",
        stress: "Early Crash",
        range: "Monte-Carlo",
        historical: "Historical",
      };
      return map[lens] || "Single-Path";
    }

    function renderMarkdownNotes(mdText) {
      const raw = String(mdText || "").trim();
      if (!raw) {
        return `<div class="muted" style="font-style:italic;">No thesis or rationale notes recorded for this Scenario yet. Click <strong>Edit Notes</strong> above to document assumptions and trade rationale.</div>`;
      }
      const lines = raw.split(/\r?\n/);
      const out = [];
      let inList = false;
      let inTable = false;

      const fmtInline = txt => escHtml(txt)
        .replace(/`([^`]+)`/g, '<code class="mono" style="background:rgba(15,23,42,0.85);padding:1px 5px;border-radius:4px;border:1px solid #334155;color:#7dd3fc;">$1</code>')
        .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
        .replace(/\*([^*]+)\*/g, "<em>$1</em>");

      const closeBlocks = () => {
        if (inList) { out.push("</ul>"); inList = false; }
        if (inTable) { out.push("</tbody></table>"); inTable = false; }
      };

      for (let i = 0; i < lines.length; i++) {
        const line = lines[i];
        const trimmed = line.trim();
        if (!trimmed) {
          closeBlocks();
          continue;
        }
        if (trimmed.startsWith("|") && trimmed.endsWith("|")) {
          if (/^\|[\s\-:|]+\|$/.test(trimmed)) continue;
          const cells = trimmed.slice(1, -1).split("|").map(c => fmtInline(c.trim()));
          if (!inTable) {
            if (inList) { out.push("</ul>"); inList = false; }
            out.push(`<table><thead><tr>${cells.map(c => `<th>${c}</th>`).join("")}</tr></thead><tbody>`);
            inTable = true;
          } else {
            out.push(`<tr>${cells.map(c => `<td>${c}</td>`).join("")}</tr>`);
          }
          continue;
        }
        if (inTable) { out.push("</tbody></table>"); inTable = false; }

        if (/^###\s+/.test(trimmed)) {
          closeBlocks();
          out.push(`<h3>${fmtInline(trimmed.replace(/^###\s+/, ""))}</h3>`);
        } else if (/^##\s+/.test(trimmed)) {
          closeBlocks();
          out.push(`<h2>${fmtInline(trimmed.replace(/^##\s+/, ""))}</h2>`);
        } else if (/^#\s+/.test(trimmed)) {
          closeBlocks();
          out.push(`<h1>${fmtInline(trimmed.replace(/^#\s+/, ""))}</h1>`);
        } else if (/^[-*]\s+/.test(trimmed)) {
          if (!inList) { out.push("<ul>"); inList = true; }
          out.push(`<li>${fmtInline(trimmed.replace(/^[-*]\s+/, ""))}</li>`);
        } else if (/^>\s*/.test(trimmed)) {
          closeBlocks();
          out.push(`<blockquote>${fmtInline(trimmed.replace(/^>\s*/, ""))}</blockquote>`);
        } else if (/^---+$/.test(trimmed)) {
          closeBlocks();
          out.push(`<hr style="border:none;border-top:1px solid #1e293b;margin:10px 0;" />`);
        } else {
          closeBlocks();
          out.push(`<p>${fmtInline(trimmed)}</p>`);
        }
      }
      closeBlocks();
      return out.join("\n");
    }

    function formatModelKpiCells(entry) {
      const k = (entry && entry.kpi_summary) || {};
      const lens = k.active_lens || (entry?.settings?.simActiveModel) || "single_path";
      const isBand = lens === "range" || lens === "historical";

      let worthVal = "\u2014";
      let worthSub = `Today's $ at ${INV0.name} ${INV0.milestone_age}`;
      const a85 = k.age_85_real || {};
      if ((isBand || a85.has_quantiles) && a85.p50 != null) {
        worthVal = `${fmtSimCompact(a85.p50)} P50`;
        const qLo = a85.p25 != null ? `P25 ${fmtSimCompact(a85.p25)}` : `P10 ${fmtSimCompact(a85.p10 || 0)}`;
        const qHi = a85.p75 != null ? `P75 ${fmtSimCompact(a85.p75)}` : `P90 ${fmtSimCompact(a85.p90 || 0)}`;
        worthSub = `${qLo} \u2013 ${qHi}`;
      } else if (a85.value != null) {
        worthVal = fmtSimCompact(a85.value);
      } else if (a85.point != null) {
        worthVal = fmtSimCompact(a85.point);
      } else if (a85.p50 != null) {
        worthVal = fmtSimCompact(a85.p50);
      }

      let solvVal = "\u2014";
      let solvSub = "100-Yr Burn Coverage";
      const slv = k.solvency || {};
      const rawPct = slv.funded_to_85_pct != null ? slv.funded_to_85_pct : slv.pct;
      const rawYrs = slv.years_solvent != null ? slv.years_solvent : slv.years;
      if (rawPct != null) {
        const p = Math.round(Number(rawPct));
        const yrs = rawYrs != null ? Number(rawYrs) : 100;
        if (isBand) {
          solvVal = `${p}% Funded`;
          solvSub = `Paths solvent to ${INV0.name} ${INV0.milestone_age}`;
        } else if (yrs >= 99.9 && p >= 100) {
          solvVal = `100% (100+ yrs)`;
          solvSub = "Sustains full 100-yr horizon";
        } else {
          solvVal = p >= 100 ? `100% (${yrs.toFixed(1)} yrs)` : `Depletes Yr ${yrs.toFixed(1)}`;
          solvSub = slv.depletion_ym ? `Depletes ${slv.depletion_ym}` : `Burn covered ${yrs.toFixed(1)} yrs`;
        }
      }

      let taxVal = "\u2014";
      let taxSub = `To ${INV0.name} Age ${INV0.milestone_age}`;
      const tx = k.tax_to_85 || {};
      const keptPct = tx.net_kept_pct != null ? tx.net_kept_pct : tx.efficiency_pct;
      const paidReal = tx.cum_tax_real != null ? tx.cum_tax_real : tx.real_tax_paid;
      if (keptPct != null) {
        const eff = Number(keptPct).toFixed(1);
        const paid = paidReal != null ? fmtSimCompact(paidReal) : "$0";
        taxVal = `${eff}% Kept`;
        taxSub = `${paid} real tax paid`;
      }

      return { worthVal, worthSub, solvVal, solvSub, taxVal, taxSub, lens };
    }

    function _isAiAuthorType(authorType) {
      const t = String(authorType || "").trim().toLowerCase();
      return t === "ai" || t === "ai_advisor";
    }

    function hasOpenModal() {
      if (typeof document === "undefined") return false;
      if (typeof document.querySelector === "function") {
        const openEl = document.querySelector(
          ".modal-backdrop.open, #project-settings-backdrop.open, #save-dialog-backdrop.open, #portfolio-delete-backdrop.open, #sim-sw-dialog-backdrop.open, #sim-hyp-dialog-backdrop.open, #friendly-name-dialog-backdrop.open, #demo-blocked-backdrop.open, #rebuild-backdrop.open, #protect-confirm-backdrop.open"
        );
        if (openEl && openEl.classList && typeof openEl.classList.contains === "function" && openEl.classList.contains("open") === true) {
          return true;
        }
      }
      if (typeof document.getElementById === "function") {
        const modalIds = [
          "project-settings-backdrop",
          "save-dialog-backdrop",
          "portfolio-delete-backdrop",
          "sim-sw-dialog-backdrop",
          "sim-hyp-dialog-backdrop",
          "friendly-name-dialog-backdrop",
          "demo-blocked-backdrop",
          "rebuild-backdrop",
          "protect-confirm-backdrop",
        ];
        for (const id of modalIds) {
          const el = document.getElementById(id);
          if (!el) continue;
          if (el.classList && typeof el.classList.contains === "function" && el.classList.contains("open") === true) {
            return true;
          }
          if (id === "protect-confirm-backdrop" && el.style && el.style.display === "flex") {
            return true;
          }
        }
      }
      return false;
    }
    if (typeof window !== "undefined") {
      window.hasOpenModal = hasOpenModal;
    }

    function _shouldSuppressModelCardAutoFocus() {
      if (hasOpenModal()) return true;
      const ae = typeof document !== "undefined" ? document.activeElement : null;
      if (!ae) return false;
      const tag = String(ae.tagName || "").toUpperCase();
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return true;
      if (ae.isContentEditable === true) return true;
      if (typeof ae.getAttribute === "function") {
        const ce = ae.getAttribute("contenteditable");
        if (ce === "" || ce === "true" || ce === "plaintext-only") return true;
      }
      if (ae.id === "ai-chat-pane") return true;
      if (typeof ae.closest === "function") {
        const inChat = ae.closest("#ai-chat-pane");
        if (inChat && (inChat.id === "ai-chat-pane" || inChat === true)) return true;
      }
      if (typeof document.getElementById === "function") {
        const chatPane = document.getElementById("ai-chat-pane");
        if (chatPane && typeof chatPane.contains === "function" && chatPane.contains(ae) === true) return true;
      }
      return false;
    }

    function renderModelsScreen() {
      const gridView = document.getElementById("models-grid-view");
      const detailView = document.getElementById("models-detail-view");
      const countPill = document.getElementById("models-count-pill");
      const cardsEl = document.getElementById("models-cards-container");
      if (!cardsEl) return;

      const entries = modelsList.length > 0 ? modelsList : Object.values(savesRegistry);
      if (countPill) {
        countPill.textContent = `${entries.length} ${entries.length === 1 ? "Scenario" : "Scenarios"}`;
      }

      if (selectedModelDetailId) {
        const detailEntry = findModelEntry(selectedModelDetailId);
        if (detailEntry) {
          if (gridView) gridView.style.display = "none";
          if (detailView) detailView.style.display = "block";
          if (isEditingDetailNotes && !_allowNotesEditorRender) {
            const panelEl = document.getElementById("models-detail-panel");
            const existingEditor = document.getElementById("model-detail-notes-editor");
            if (panelEl && panelEl.innerHTML && String(panelEl.innerHTML).includes("model-detail-notes-editor")) {
              return;
            }
            if (existingEditor && existingEditor.tagName === "TEXTAREA") {
              return;
            }
          }
          renderModelDetailPanel(detailEntry);
          return;
        }
        selectedModelDetailId = null;
      }

      if (gridView) gridView.style.display = "block";
      if (detailView) detailView.style.display = "none";

      if (entries.length === 0) {
        cardsEl.innerHTML = `<div class="panel muted" style="padding:24px;text-align:center;">No saved Scenarios found in <code>Scenarios/</code>. Click <strong>&#9670; Save As New Scenario</strong> above.</div>`;
        return;
      }

      cardsEl.innerHTML = entries.map(entry => {
        const isActive = (entry.id && entry.id === currentModelId) || entry.name === currentSaveName;
        const acts = countModelActionSummary(entry);
        const kpi = formatModelKpiCells(entry);
        const authorType = entry.author?.type || "human";
        const authorModel = entry.author?.model || "";
        const lensBadge = `<span class="mdl-in-pill mono" style="font-size:10.5px;">\u25C6 ${escHtml(formatModelLensLabel(kpi.lens))}</span>`;
        const activeBadge = isActive
          ? `<span class="pill ${isSaveDirty ? "pill-rose" : "pill-green"} mono" style="font-size:10.5px;">${isSaveDirty ? "\u25CF Active (Modified)" : "\u2713 Active"}</span>`
          : "";
        const lockBadge = entry.is_protected
          ? `<button type="button" class="pill pill-slate mono" data-sa-action="toggle-model-protect" data-sa-id="${escHtml(entry.id)}" data-sa-stop="1" title="Protected Scenario — click to manage protection" style="font-size:10.5px;cursor:pointer;border:1px solid #475569;line-height:inherit;">\uD83D\uDD12 Protected</button>`
          : `<button type="button" class="pill pill-slate mono" data-sa-action="toggle-model-protect" data-sa-id="${escHtml(entry.id)}" data-sa-stop="1" title="Unprotected Scenario — click to enable protection" style="font-size:10.5px;cursor:pointer;opacity:0.8;border:1px dashed #334155;line-height:inherit;">\uD83D\uDD13 Unprotected</button>`;
        const authorBadge = _isAiAuthorType(authorType)
          ? `<span class="pill pill-cyan mono" style="font-size:10.5px;">\uD83E\uDD16 AI${authorModel ? " \u2022 " + escHtml(authorModel) : ""}</span>`
          : "";
        const hypBadge = acts.hypothetical_count > 0
          ? `<span class="pill pill-teal mono" title="Contains ${acts.hypothetical_count} synthesized $0-start Hypothetical Account(s)" style="font-size:10.5px;font-weight:700;">\u2726 ${acts.hypothetical_count} Hypothetical</span>`
          : "";

        const capsules = [lensBadge];
        if (activeBadge) capsules.push(activeBadge);
        capsules.push(lockBadge);
        if (authorBadge) capsules.push(authorBadge);
        if (hypBadge) capsules.push(hypBadge);
        if (acts.transfers > 0) capsules.push(`<span class="pill pill-cyan mono" style="font-size:10.5px;">\u21C4 ${acts.transfers} Transfer${acts.transfers === 1 ? "" : "s"}</span>`);
        if (acts.deposits > 0) capsules.push(`<span class="pill pill-green mono" style="font-size:10.5px;">\u2191 ${acts.deposits} Deposit${acts.deposits === 1 ? "" : "s"}</span>`);
        if (acts.withdrawals > 0) capsules.push(`<span class="pill pill-rose mono" style="font-size:10.5px;">\u2193 ${acts.withdrawals} Withdrawal${acts.withdrawals === 1 ? "" : "s"}</span>`);

        const thumbSrc = resolveModelSnapshotUrl(entry);

        // DESIGN INTENT: Scenario Card top-to-bottom hierarchy:
        // 1. Top row: ID# + Scenario Name (larger font) with action buttons right-aligned
        // 2. Timestamp subtext
        // 3. Unified status/metadata/action capsules row (no "Human" or upvote capsules)
        // 4. 3 Gold Milestone KPI boxes
        // 5. Separator line + Trajectory chart at the bottom of the card
        return `
          <div class="model-card ${isActive ? "active-model active-loaded" : ""}"
               id="model-card-${escHtml(entry.id)}"
               data-model-id="${escHtml(entry.id)}"
               tabindex="0"
               role="group"
               aria-label="${escHtml(entry.name)} Scenario${isActive ? " (Currently Loaded)" : ""}"
               data-sa-action="open-model-detail"
               data-sa-id="${escHtml(entry.id)}"
               data-sa-keydown="model-card">
            <div class="model-card-body">
              <div>
                <div style="display:flex;justify-content:space-between;align-items:center;gap:12px;">
                  <div style="min-width:0;flex:1;display:flex;align-items:baseline;gap:8px;flex-wrap:wrap;line-height:1.25;">
                    <span class="mono" style="color:#94a3b8;font-size:13px;font-weight:600;">#${escHtml(entry.id)}</span>
                    <span style="font-size:19px;font-weight:700;color:#f8fafc;letter-spacing:-0.01em;">${escHtml(entry.name)}</span>
                  </div>
                  <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;justify-content:flex-end;flex-shrink:0;">
                    <button type="button" class="btn ${isActive ? "" : "btn-primary"}" style="font-size:11.5px;padding:4px 12px;" data-sa-action="load-model" data-sa-id="${escHtml(entry.id)}" data-sa-stop="1">
                      ${isActive ? "\u2713 Loaded" : "Activate Scenario"}
                    </button>
                    <button type="button" class="btn" style="font-size:11.5px;padding:4px 10px;" data-sa-action="open-model-detail" data-sa-id="${escHtml(entry.id)}" data-sa-stop="1">
                      Details &amp; Notes &rarr;
                    </button>
                  </div>
                </div>
                <div class="muted mono" style="font-size:11px;margin-top:4px;">
                  Updated ${escHtml(entry.updated_at || "\u2014")}${entry.goog_price_anchor ? ` \u2022 ${escHtml(PRIMARY_SYMBOL)} $${Number(entry.goog_price_anchor).toFixed(2)}` : ""}
                </div>
              </div>

              <!-- Unified Status & Scheduled Actions Capsules Strip -->
              <div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center;">
                ${capsules.join("")}
              </div>

              <!-- 3 Champagne Gold-Crested Milestone Primary Evaluation KPIs -->
              <div class="model-kpi-strip">
                <div class="model-kpi-cell has-tip" style="cursor:help;" data-tip="&lt;strong style='color:#fcd34d;'&gt;&#9733; Scenario Outcome: Age ${INV0.milestone_age} Worth&lt;/strong&gt;&lt;br&gt;Inflation-adjusted Today's $ portfolio balance at milestone age, computed from this Scenario's purple &lt;b&gt;&#9670;&lt;/b&gt; inputs.">
                  <div class="model-kpi-lbl">\u2605 Age ${INV0.milestone_age} Worth</div>
                  <div class="model-kpi-val mono">${escHtml(kpi.worthVal)}</div>
                  <div class="muted mono" style="font-size:9.8px;">${escHtml(kpi.worthSub)}</div>
                </div>
                <div class="model-kpi-cell has-tip" style="cursor:help;" data-tip="&lt;strong style='color:#fcd34d;'&gt;&#9733; Scenario Outcome: Solvency&lt;/strong&gt;&lt;br&gt;Years your portfolio funds projected spending (up to 100+ years) or survival rate to milestone age under this Scenario's purple &lt;b&gt;&#9670;&lt;/b&gt; inputs.">
                  <div class="model-kpi-lbl">\u2605 Solvency</div>
                  <div class="model-kpi-val mono">${escHtml(kpi.solvVal)}</div>
                  <div class="muted mono" style="font-size:9.8px;">${escHtml(kpi.solvSub)}</div>
                </div>
                <div class="model-kpi-cell has-tip" style="cursor:help;" data-tip="&lt;strong style='color:#fcd34d;'&gt;&#9733; Scenario Outcome: Tax Efficiency&lt;/strong&gt;&lt;br&gt;After-tax share kept and cumulative tax drag through Age ${INV0.milestone_age} under this Scenario's purple &lt;b&gt;&#9670;&lt;/b&gt; inputs.">
                  <div class="model-kpi-lbl">\u2605 Tax Eff. (to ${INV0.milestone_age})</div>
                  <div class="model-kpi-val mono">${escHtml(kpi.taxVal)}</div>
                  <div class="muted mono" style="font-size:9.8px;">${escHtml(kpi.taxSub)}</div>
                </div>
              </div>
            </div>

            <div class="model-thumb">
              ${thumbSrc
                ? `<img class="model-thumb-img" src="${escHtml(thumbSrc)}" alt="${escHtml(entry.name)} trajectory" loading="lazy" onerror="if(this.src&&this.src.includes('.png')){this.src=this.src.replace('.png','.jpg');}else if(this.src&&this.src.includes('?')){this.src=this.src.split('?')[0];}else{this.style.display='none';if(this.nextElementSibling)this.nextElementSibling.style.display='block';}" /><div class="muted mono model-thumb-placeholder" style="display:none;padding:44px 16px;text-align:center;font-size:11.5px;">Retirement Trajectory Snapshot</div>`
                : `<div class="muted mono model-thumb-placeholder" style="padding:44px 16px;text-align:center;font-size:11.5px;">Retirement Trajectory Snapshot</div>`}
            </div>
          </div>
        `;
      }).join("");

      // Auto-focus loaded scenario card so both inner gold ring and outer teal focus ring
      // are immediately visible when opening the Scenarios page with >= 2 cards.
      // If only 1 scenario exists, focus is suppressed so only the single loaded gold ring appears.
      const cards = Array.from(cardsEl.querySelectorAll(".model-card"));
      if (cards.length >= 2 && currentScreen === "models" && !selectedModelDetailId && !_shouldSuppressModelCardAutoFocus()) {
        const previouslyFocusedId = (document.activeElement && typeof document.activeElement.getAttribute === "function")
          ? document.activeElement.getAttribute("data-model-id")
          : null;
        let cardToFocus = null;
        if (previouslyFocusedId) {
          cardToFocus = cards.find(c => c.getAttribute("data-model-id") === previouslyFocusedId);
        }
        if (!cardToFocus) {
          cardToFocus = cards.find(c => c.classList.contains("active-loaded") || c.classList.contains("active-model")) || cards[0];
        }
        if (cardToFocus && typeof cardToFocus.focus === "function") {
          cardToFocus.focus({ preventScroll: true });
        }
      }
    }

    function navigateModelCards(direction) {
      const container = document.getElementById("models-cards-container");
      if (!container) return;
      const cards = Array.from(container.querySelectorAll(".model-card"));
      if (cards.length <= 1) return;

      const activeEl = document.activeElement;
      let curIdx = cards.indexOf(activeEl);

      if (curIdx < 0) {
        curIdx = cards.findIndex(c => c.classList.contains("active-loaded") || c.classList.contains("active-model"));
        if (curIdx < 0) curIdx = 0;
      }

      let cols = 1;
      if (cards.length >= 2) {
        const top0 = cards[0].offsetTop || 0;
        if (cards[1].offsetTop !== undefined && Math.abs(cards[1].offsetTop - top0) < 15) {
          cols = 2;
          for (let i = 2; i < cards.length; i++) {
            if (cards[i].offsetTop !== undefined && Math.abs(cards[i].offsetTop - top0) < 15) {
              cols++;
            } else {
              break;
            }
          }
        }
      }

      let nextIdx = curIdx;
      if (direction === "right") {
        nextIdx = Math.min(cards.length - 1, curIdx + 1);
      } else if (direction === "left") {
        nextIdx = Math.max(0, curIdx - 1);
      } else if (direction === "down") {
        nextIdx = Math.min(cards.length - 1, curIdx + cols);
      } else if (direction === "up") {
        nextIdx = Math.max(0, curIdx - cols);
      }

      if (cards[nextIdx]) {
        cards[nextIdx].focus();
        if (typeof cards[nextIdx].scrollIntoView === "function") {
          cards[nextIdx].scrollIntoView({ block: "nearest", behavior: "smooth" });
        }
      }
    }

    function onModelCardKeyDown(e, id) {
      if (e.key === "Enter" || e.key === " ") {
        const isChildButton = e.target && e.target.tagName === "BUTTON";
        if (!isChildButton) {
          e.preventDefault();
          e.stopPropagation();
          loadModelById(id, false);
        }
      } else if (e.key === "ArrowRight") {
        e.preventDefault();
        e.stopPropagation();
        navigateModelCards("right");
      } else if (e.key === "ArrowLeft") {
        e.preventDefault();
        e.stopPropagation();
        navigateModelCards("left");
      } else if (e.key === "ArrowDown") {
        e.preventDefault();
        e.stopPropagation();
        navigateModelCards("down");
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        e.stopPropagation();
        navigateModelCards("up");
      }
    }

    async function loadModelById(idOrName, openSim = false) {
      const entry = findModelEntry(idOrName);
      if (!entry || !entry.settings || typeof entry.settings !== "object") return false;
      const prevModelId = currentModelId;
      const prevSaveName = currentSaveName;
      currentModelId = entry.id;
      currentSaveName = entry.name;
      try {
        applySettingsSnapshot(entry.settings);
      } catch (err) {
        currentModelId = prevModelId;
        currentSaveName = prevSaveName;
        console.error("Failed to load scenario settings:", err);
        return false;
      }
      persistLocalSavesRegistry();
      let apiRes = null;
      try {
        apiRes = await apiJson("/api/models/active", {
          body: { id: entry.id, name: entry.name },
        });
      } catch (_e) {
        // Handled transparently by apiJson
      }
      if (apiRes && apiRes.status === 409) {
        return false;
      }
      updateSaveStatusUI(false);
      selectedModelDetailId = null;
      isEditingDetailNotes = false;
      if (openSim) {
        switchScreen("retirement");
      } else {
        renderModelsScreen();
        const activeCard = document.getElementById("model-card-" + entry.id);
        if (activeCard) {
          activeCard.focus();
          if (typeof activeCard.scrollIntoView === "function") {
            activeCard.scrollIntoView({ behavior: "smooth", block: "nearest" });
          }
        }
      }
      return true;
    }

    function openModelDetail(idOrName) {
      const entry = findModelEntry(idOrName);
      if (!entry) return;
      selectedModelDetailId = entry.id;
      isEditingDetailNotes = false;
      renderModelsScreen();
    }

    function closeModelDetail() {
      selectedModelDetailId = null;
      isEditingDetailNotes = false;
      renderModelsScreen();
    }

    function renderModelDetailPanel(entry) {
      const panelEl = document.getElementById("models-detail-panel");
      if (!panelEl || !entry) return;
      const isActive = (entry.id && entry.id === currentModelId) || entry.name === currentSaveName;
      const kpi = formatModelKpiCells(entry);
      const actsSummary = countModelActionSummary(entry);
      const s = entry.settings || {};
      const hyps = Array.isArray(s.simHypotheticalAssets) ? s.simHypotheticalAssets : [];
      const actions = Array.isArray(s.simScheduledActions)
        ? s.simScheduledActions
        : (Array.isArray(s.simScheduledWithdrawals) ? s.simScheduledWithdrawals : []);

      const thumbSrc = resolveModelSnapshotUrl(entry);

      const assetLabelMap = {};
      BASE_SIM_ASSETS.forEach(a => { assetLabelMap[a.id] = a.label; });
      hyps.forEach(h => { assetLabelMap[h.id] = `\u2726 ${h.label}`; });
      assetLabelMap.outside_external = "Outside / External Source";

      const _fmtRcLbl = rc => (RISK_CLASSES_META[rc]?.label || rc || "Broad Equity");
      // DESIGN INTENT (`DA2-18`): Map every wrapper alias emitted by `view_model.py`, `app_simulator.js`, and saved scenarios.
      const _fmtTwLbl = tw => ({
        taxable: "Taxable", taxable_stock: "Taxable Equity", taxable_ltcg: "Taxable LTCG",
        taxable_yield: "Taxable Bond / Coupon", taxable_bond: "Taxable Bond / Coupon",
        cash_sweep: "Cash / Money Market", cash: "Liquid Cash",
        state_tax_exempt: "US Treasuries (0% State)", treasury: "US Treasuries (0% State)", us_treasury: "US Treasuries (0% State)",
        muni_exempt: "Tax-Exempt Muni", muni: "Tax-Exempt Muni", dual_tax_exempt: "Tax-Exempt Muni",
        trad_ira: "Traditional IRA", ordinary_full: "Traditional IRA (Ordinary)",
        roth_ira: "Roth IRA", roth: "Roth / Tax-Free", tax_free: "Roth / Tax-Free",
        annuity_lifo: "Non-Qual Annuity (LIFO)",
      }[String(tw || "taxable").toLowerCase()] || tw || "Taxable");
      const _fmtPresetLbl = p => ({
        tax_optimal: "Tax-Optimal", auto_optimal: "Auto-Optimize", heir_legacy: "Heir Legacy (§1014)",
        goog_first: "Concentrated First", custom: "Custom Order",
      }[String(p || "tax_optimal").toLowerCase()] || p || "Tax-Optimal");
      const _fmtStressLbl = sc => ({
        none: "None", gfc2008: "2008 Global Financial Crisis", dotcom2000: "2000 Dot-com Crash",
        covid2020: "2020 COVID Shock", rates2022: "2022 Rate Shock", custom: "Custom Correction",
      }[String(sc || "none").toLowerCase()] || sc || "None");

      const hypRowsHtml = hyps.length === 0
        ? `<div class="muted" style="font-size:11.8px;">No synthesized hypothetical accounts in this Scenario (uses current real-world statements only).</div>`
        : `<div class="table-wrap" style="margin-top:6px;"><table><thead><tr><th>Hypothetical Account</th><th>Custodian / Product</th><th class="num">Growth</th><th>Risk / Wrapper</th></tr></thead><tbody>${
            hyps.map(h => `
              <tr>
                <td><span class="pill pill-teal mono" style="font-size:10px;margin-right:5px;">\u2726 HYP</span><strong>${escHtml(h.label || h.id)}</strong></td>
                <td class="muted" style="font-size:11.5px;">${escHtml(h.institution || "")}${h.real_product_ref ? ` \u2022 ${escHtml(h.real_product_ref)}` : ""}</td>
                <td class="num mono" data-tip="${escHtml(projectedGrowthTooltip({...h, is_hypothetical: true}, {growth_pct: h.growth_pct || 0}, "Saved scenario growth", s.holdingHalfLives || {}))}">${Number(h.growth_pct || 0).toFixed(1)}% <span class="muted">(-${Number(h.expense_ratio_pct || 0).toFixed(2)}% ER)</span></td>
                <td class="mono" style="font-size:11px;">${escHtml(_fmtRcLbl(h.risk_class || "broad_equity"))} \u2022 ${escHtml(_fmtTwLbl(h.tax_wrapper || "taxable"))}</td>
              </tr>
            `).join("")
          }</tbody></table></div>`;

      const actRowsHtml = actions.length === 0
        ? `<div class="muted" style="font-size:11.8px;margin-top:8px;">No scheduled transfers, deposits, or withdrawals configured.</div>`
        : `<div class="table-wrap" style="margin-top:8px;"><table><thead><tr><th>Type</th><th>Cadence / Timing</th><th>Route</th><th class="num">Amount</th><th>Purpose</th></tr></thead><tbody>${
            actions.map(act => {
              const t = String(act.type || "withdrawal").toLowerCase();
              const badge = t === "transfer"
                ? `<span class="pill pill-cyan mono" style="font-size:10px;">\u21C4 Transfer</span>`
                : (t === "deposit"
                    ? `<span class="pill pill-green mono" style="font-size:10px;">\u2191 Deposit</span>`
                    : `<span class="pill pill-rose mono" style="font-size:10px;">\u2193 Withdraw</span>`);
              const cad = act.cadence || "one_time";
              const rawYm = act.ym || act.date || "next_month";
              const niceYm = rawYm === "next_month" ? "Next Month" : rawYm;
              const cadLbl = cad === "on_vest" ? "On Stock Vest" : (cad === "annual" ? "Annual" : (cad === "quarterly" ? "Quarterly" : (cad === "monthly" ? "Monthly" : cad)));
              const timing = cad === "one_time"
                ? (act.person && act.age ? `${act.person} Age ${act.age}` : niceYm)
                : `${cadLbl}${act.start_ym ? ` (${act.start_ym}\u2192${act.end_ym || "end"})` : ""}`;
              const route = t === "transfer"
                ? `${assetLabelMap[act.from_asset_id || act.source_asset_id] || act.from_asset_id || act.source_asset_id || "Auto Priority"} \u2192 ${assetLabelMap[act.to_asset_id || act.target_asset_id] || act.to_asset_id || act.target_asset_id || "?"}`
                : (t === "deposit"
                    ? `\u2192 ${assetLabelMap[act.to_asset_id || act.target_asset_id] || act.to_asset_id || act.target_asset_id || "Liquid Cash"}`
                    : `${(act.from_asset_id || act.source_asset_id) && (act.from_asset_id || act.source_asset_id) !== "waterfall" && (act.from_asset_id || act.source_asset_id) !== "auto" ? (assetLabelMap[act.from_asset_id || act.source_asset_id] || act.from_asset_id || act.source_asset_id) : "Auto Priority"} \u2192 Spend/Gift`);
              const amtTxt = act.amount_mode === "pct_of_source"
                ? `${Number(act.amount || 0).toFixed(1)}% of source`
                : `${fmtUSD(Number(act.amount) || 0)} (${act.dollar_mode === "nominal" ? "Nominal $" : "Today's $"})`;
              return `
                <tr>
                  <td>${badge}</td>
                  <td class="mono" style="font-size:11.2px;">${escHtml(timing)}</td>
                  <td style="font-size:11.5px;">${escHtml(route)}</td>
                  <td class="num mono">${escHtml(amtTxt)}</td>
                  <td class="muted" style="font-size:11.3px;">${escHtml(act.purpose || act.comment || "")}</td>
                </tr>
              `;
            }).join("")
          }</tbody></table></div>`;

      // DA-D06 + DA4-09: Include ALL saved `holdingForecasts` keys AND `holdingHalfLives` keys (even when
      // `holdingForecasts` is empty!), display custom half-life badges, and evaluate `projectedGrowthTooltip`
      // strictly against `s.holdingHalfLives` rather than live global `holdingHalfLives`.
      const hf = s.holdingForecasts || {};
      const hhl = normalizeHoldingHalfLives(s.holdingHalfLives || {});
      const _resolveSavedHoldingMeta = fk => {
        const rawKey = String(fk).replace(/^strat:|^pos:/, "");
        const hyp = hyps.find(a => a.id === fk);
        const h = hyp ? {...hyp, title: hyp.label, is_hypothetical: true}
          : (typeof findHoldingByKey === "function" ? (findHoldingByKey(fk) || findHoldingByKey(rawKey)) : null) || {forecast_key: fk, risk_class: hf[fk]?.risk_class};
        const hasHf = Object.prototype.hasOwnProperty.call(hf, fk) && hf[fk] != null;
        const g = hasHf
          ? Number((hf[fk]?.use_reference ? holdingReferenceGrowth(h) : null) ?? hf[fk]?.growth_pct ?? (typeof hf[fk] === "number" ? hf[fk] : 0))
          : Number(holdingReferenceGrowth(h) ?? h.default_growth_pct ?? h.annual_rate_pct ?? 0);
        return { rawKey, h, hasHf, g: Number.isFinite(g) ? g : 0 };
      };
      const nonZeroHf = Array.from(new Set([...Object.keys(hf), ...Object.keys(hhl)])).sort();
      const metaByFk = Object.fromEntries(nonZeroHf.map(fk => [fk, _resolveSavedHoldingMeta(fk)]));
      nonZeroHf.sort((a, b) => (metaByFk[b].g - metaByFk[a].g) || a.localeCompare(b));

      let holdingOverridesHtml = "";
      if (nonZeroHf.length === 0) {
        holdingOverridesHtml = `<div class="muted" style="font-size:11.8px;padding:6px 0;">No per-holding growth overrides configured in this Scenario (all holdings inherit ingested baseline rates).</div>`;
      } else {
        const vals = nonZeroHf.map(k => metaByFk[k].g);
        const minVal = Math.min(...vals);
        const maxVal = Math.max(...vals);
        let axisMin = Math.min(0, minVal);
        let axisMax = Math.max(0, maxVal);
        if (axisMax === axisMin) {
          axisMax = axisMin + 10;
        }
        const range = axisMax - axisMin;
        const zeroPct = ((0 - axisMin) / range) * 100;

        const rowsHtml = nonZeroHf.map(fk => {
          const { rawKey, h, hasHf, g } = metaByFk[fk];
          const prefs = (typeof globalPreferences === "object" && globalPreferences && globalPreferences.friendly_names) ? globalPreferences.friendly_names : {};
          const prefEntry = prefs[fk] || prefs[rawKey] || {};
          const friendlyName = (prefEntry && prefEntry.friendly_name) || h.friendly_name || "";
          const actualName = h.statement_name || h.title || h.name || rawKey || fk;
          const displayName = (friendlyName && String(friendlyName).trim()) ? String(friendlyName).trim() : actualName;
          const vOverride = (hf[fk] && hf[fk].vol_pct != null && Number.isFinite(Number(hf[fk].vol_pct))) ? Number(hf[fk].vol_pct) : null;
          const hlOverride = Object.prototype.hasOwnProperty.call(hhl, fk) ? hhl[fk] : null;
          const hlBadgeTxt = hlOverride != null
            ? (hlOverride >= FLAT_HALF_LIFE_YEARS ? "Half-life: Flat" : `Half-life: ${hlOverride} yr`)
            : "";
          const resolvedHlForTip = hlOverride != null ? hlOverride : holdingHalfLife(h, false, hhl);
          const widthPct = range > 0 ? (Math.abs(g) / range) * 100 : 0;
          const isPos = g >= 0;

          const barStyle = isPos
            ? `left:${zeroPct.toFixed(2)}%;width:${widthPct.toFixed(2)}%;min-width:38px;max-width:${(100 - zeroPct).toFixed(2)}%;background:linear-gradient(90deg, #059669, #10b981);`
            : `right:${(100 - zeroPct).toFixed(2)}%;width:${widthPct.toFixed(2)}%;min-width:38px;max-width:${zeroPct.toFixed(2)}%;background:linear-gradient(90deg, #e11d48, #f43f5e);`;

          return `
            <tr>
              <td style="width:42%;max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${escHtml(displayName)}${friendlyName && friendlyName !== actualName ? ` (${escHtml(actualName)})` : ''}">
                <span style="font-weight:600;color:#f8fafc;">${escHtml(displayName)}</span>
                ${friendlyName && friendlyName !== actualName ? `<span class="muted mono" style="font-size:10px;margin-left:5px;">(${escHtml(actualName)})</span>` : ''}
                ${vOverride != null ? `<span class="pill pill-slate mono" style="font-size:9.5px;padding:1px 4px;margin-left:5px;" title="Custom Monte Carlo volatility override">&sigma; ${vOverride.toFixed(1)}%</span>` : ''}
                ${hlBadgeTxt ? `<span class="pill pill-slate mono" style="font-size:9.5px;padding:1px 4px;margin-left:5px;" title="Custom forecast mean-reversion half-life">${escHtml(hlBadgeTxt)}</span>` : ''}
              </td>
              <td style="width:58%;">
                <div style="position:relative;width:100%;height:22px;background:rgba(15,23,42,0.6);border:1px solid #1e293b;border-radius:4px;overflow:hidden;display:flex;align-items:center;" data-tip="${escHtml(projectedGrowthTooltip(h, {growth_pct: g, is_override: hasHf, halfLife: resolvedHlForTip}, "Saved scenario growth", hhl))}">
                  ${zeroPct > 0 && zeroPct < 100 ? `<div style="position:absolute;left:${zeroPct.toFixed(2)}%;top:0;bottom:0;width:1px;background:#475569;z-index:2;"></div>` : ''}
                  <div style="position:absolute;top:1px;bottom:1px;border-radius:3px;display:flex;align-items:center;justify-content:center;padding:0 4px;z-index:1;box-shadow:0 1px 2px rgba(0,0,0,0.3);${barStyle}">
                    <span class="mono" style="font-size:10px;font-weight:700;color:#ffffff;text-shadow:0 1px 2px rgba(0,0,0,0.9);white-space:nowrap;">${g >= 0 ? '+' : ''}${g.toFixed(1)}%</span>
                  </div>
                </div>
              </td>
            </tr>
          `;
        }).join("");

        holdingOverridesHtml = `
          <div class="table-wrap">
            <table>
              <thead>
                <tr>
                  <th style="width:42%;">Holding</th>
                  <th style="width:58%;">
                    <div style="display:flex;justify-content:space-between;align-items:center;font-size:11px;font-weight:600;">
                      <span class="mono">${minVal >= 0 ? "+" : ""}${minVal.toFixed(1)}% (Min)</span>
                      <span style="font-size:10px;text-transform:uppercase;color:#64748b;letter-spacing:0.5px;">Growth Range</span>
                      <span class="mono">${maxVal >= 0 ? "+" : ""}${maxVal.toFixed(1)}% (Max)</span>
                    </div>
                  </th>
                </tr>
              </thead>
              <tbody>
                ${rowsHtml}
              </tbody>
            </table>
          </div>
        `;
      }

      const _k0 = INV0.claim_setting_key || "ssnInvestor1ClaimAge";
      const _k1 = (HAS_SECOND_INVESTOR && INV1 && INV1.claim_setting_key) || "ssnInvestor2ClaimAge";
      const _inv0HasSsn = INV0 && INV0.is_ssn_contributor !== false;
      const ssnClaimRowHtml = !_inv0HasSsn
        ? `<tr><td class="muted">Social Security</td><td class="num mono muted">None Contributing</td></tr>`
        : (HAS_SECOND_INVESTOR && INV1
            ? `<tr><td class="muted">SSN Claim Ages (${escHtml(INV0.name)} / ${escHtml(INV1.name)})</td><td class="num mono">Age ${s[_k0] || 67} / Age ${s[_k1] || 67}</td></tr>`
            : `<tr><td class="muted">SSN Claim Age (${escHtml(INV0.name)})</td><td class="num mono">Age ${s[_k0] || 67}</td></tr>`);
      const _pFamsDet = Array.isArray(PROJECT_CFG.family_members) && PROJECT_CFG.family_members.length > 0
        ? PROJECT_CFG.family_members
        : [...(Array.isArray(PROJECT_CFG.investors) ? PROJECT_CFG.investors.map(x => Object.assign({}, x, { is_ssn_contributor: true })) : []),
           ...(Array.isArray(PROJECT_CFG.beneficiaries) ? PROJECT_CFG.beneficiaries.map(x => Object.assign({}, x, { is_ssn_contributor: false })) : [])];
      const _anySsnDet = _pFamsDet.some(m => Boolean(m && m.is_ssn_contributor));
      const _memAgesDet = (s.simMemberRetirementAges && typeof s.simMemberRetirementAges === "object") ? s.simMemberRetirementAges : {};
      let _ssnRankDet = 0;
      const _retPartsDet = [];
      _pFamsDet.forEach((m, fIdx) => {
        if (!m || typeof m !== "object") return;
        const isSsnM = Boolean(m.is_ssn_contributor);
        const wpM = sanitizeWorkPlanJs(m.work_plan, m.milestone_age || 85);
        let ovrM = _memAgesDet[`member_${fIdx}_retirement_age`];
        if (ovrM == null) ovrM = _memAgesDet[String(fIdx)];
        if (ovrM == null) {
          if (isSsnM && _ssnRankDet < 2) ovrM = _ssnRankDet === 0 ? s.simInvestor1RetirementAge : s.simInvestor2RetirementAge;
          else if (!_anySsnDet && fIdx < 2) ovrM = fIdx === 0 ? s.simInvestor1RetirementAge : s.simInvestor2RetirementAge;
        }
        if (isSsnM) _ssnRankDet++;
        if (wpM.is_working || ovrM != null) {
          const ageTxt = ovrM != null ? `Age ${Math.round(Number(ovrM))}` : `Age ${wpM.default_retirement_age} (Ref)`;
          _retPartsDet.push({ name: m.name || `Member ${fIdx + 1}`, ageTxt });
        }
      });
      const retAgeRowHtml = _retPartsDet.length === 0
        ? ""
        : `<tr><td class="muted">Stop-Working / Retirement (${_retPartsDet.map(x => escHtml(x.name)).join(" / ")})</td><td class="num mono">${_retPartsDet.map(x => escHtml(x.ageTxt)).join(" / ")}</td></tr>`;

      panelEl.innerHTML = `
        <div style="display:flex;flex-wrap:wrap;justify-content:space-between;align-items:center;gap:10px;margin-bottom:14px;padding-bottom:12px;border-bottom:1px solid #1e293b;">
          <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;">
            <button type="button" class="btn" onclick="closeModelDetail()">&larr; All Scenarios</button>
            <div>
              <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
                <span class="mono muted" style="font-size:12px;">#${escHtml(entry.id)}</span>
                <span style="font-size:18px;font-weight:700;color:#f8fafc;">${escHtml(entry.name)}</span>
                <span class="mdl-in-pill mono">\u25C6 ${escHtml(formatModelLensLabel(kpi.lens))}</span>
                ${isActive ? `<span class="pill ${isSaveDirty ? "pill-rose" : "pill-green"} mono">${isSaveDirty ? "\u25CF Active (Modified)" : "\u2713 Active"}</span>` : ""}
                ${actsSummary.hypothetical_count > 0 ? `<span class="pill pill-teal mono">\u2726 ${actsSummary.hypothetical_count} Hypothetical</span>` : ""}
                ${entry.is_protected
                  ? `<button type="button" class="pill pill-slate mono" data-sa-action="toggle-model-protect" data-sa-id="${escHtml(entry.id)}" title="Protected Scenario — click to manage protection" style="cursor:pointer;border:1px solid #475569;">\uD83D\uDD12 Protected</button>`
                  : `<button type="button" class="pill pill-slate mono" data-sa-action="toggle-model-protect" data-sa-id="${escHtml(entry.id)}" title="Unprotected Scenario — click to enable protection" style="cursor:pointer;opacity:0.78;border:1px dashed #334155;">\uD83D\uDD13 Unprotected</button>`}
              </div>
              <div class="muted mono" style="font-size:11px;margin-top:2px;">
                Updated ${escHtml(entry.updated_at || "\u2014")} &bull; Author: ${_isAiAuthorType(entry.author?.type) ? `AI (${escHtml(entry.author?.model || "Advisor")})` : "Human"}
              </div>
            </div>
          </div>

          <div style="display:flex;gap:6px;flex-wrap:wrap;">
            <button type="button" class="btn btn-primary" data-sa-action="load-model" data-sa-id="${escHtml(entry.id)}" data-sa-open-sim="1">Load</button>
            <button type="button" class="btn" data-sa-action="rename-model" data-sa-id="${escHtml(entry.id)}">Rename</button>
            <button type="button" class="btn" data-sa-action="dup-model" data-sa-id="${escHtml(entry.id)}">Duplicate</button>
            <button type="button" class="btn btn-danger" data-sa-action="del-model" data-sa-id="${escHtml(entry.id)}">Delete</button>
          </div>
        </div>

        ${thumbSrc ? `
          <div style="border:1px solid #1e293b;border-radius:10px;overflow:hidden;margin-bottom:14px;background:#070d1a;">
            <img src="${escHtml(thumbSrc)}" alt="${escHtml(entry.name)} trajectory" style="width:100%;max-height:360px;object-fit:contain;display:block;" onerror="if(this.src&&this.src.includes('?')){this.src=this.src.split('?')[0];}else{this.style.display='none';}" />
          </div>
        ` : ""}

        <div class="kpi-row" style="margin-bottom:16px;">
          <div class="kpi-card mdl-kpi-card has-tip" style="cursor:help;" data-tip="&lt;strong style='color:#fcd34d;'&gt;&#9733; Primary Scenario Outcome&lt;/strong&gt;&lt;br&gt;Gold &lt;b&gt;&#9733;&lt;/b&gt; KPI cards report simulated results driven by this Scenario's saved purple &lt;b&gt;&#9670;&lt;/b&gt; inputs.">
            <div class="kpi-lbl mdl-kpi-eyebrow"><span>\u2605 Age ${INV0.milestone_age} Real Worth (${escHtml(formatModelLensLabel(kpi.lens))})</span></div>
            <div class="kpi-val mono">${escHtml(kpi.worthVal)}</div>
            <div class="kpi-sub mono">${escHtml(kpi.worthSub)}</div>
          </div>
          <div class="kpi-card mdl-kpi-card has-tip" style="cursor:help;" data-tip="&lt;strong style='color:#fcd34d;'&gt;&#9733; Primary Scenario Outcome&lt;/strong&gt;&lt;br&gt;Gold &lt;b&gt;&#9733;&lt;/b&gt; KPI cards report simulated results driven by this Scenario's saved purple &lt;b&gt;&#9670;&lt;/b&gt; inputs.">
            <div class="kpi-lbl mdl-kpi-eyebrow"><span>\u2605 100-Yr Solvency</span></div>
            <div class="kpi-val mono">${escHtml(kpi.solvVal)}</div>
            <div class="kpi-sub mono">${escHtml(kpi.solvSub)}</div>
          </div>
          <div class="kpi-card mdl-kpi-card has-tip" style="cursor:help;" data-tip="&lt;strong style='color:#fcd34d;'&gt;&#9733; Primary Scenario Outcome&lt;/strong&gt;&lt;br&gt;Gold &lt;b&gt;&#9733;&lt;/b&gt; KPI cards report simulated results driven by this Scenario's saved purple &lt;b&gt;&#9670;&lt;/b&gt; inputs.">
            <div class="kpi-lbl mdl-kpi-eyebrow"><span>\u2605 Tax Efficiency (to Age ${INV0.milestone_age})</span></div>
            <div class="kpi-val mono">${escHtml(kpi.taxVal)}</div>
            <div class="kpi-sub mono">${escHtml(kpi.taxSub)}</div>
          </div>
        </div>

        <div class="model-detail-grid">
          <div style="display:flex;flex-direction:column;gap:14px;">
            <div class="mdl-in-box" style="padding:12px 14px;">
              <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;">
                <span class="mdl-in-lbl" style="font-size:12.5px;"><span class="mdl-in-glyph">&#9670;</span> Hypothetical Accounts &amp; Scheduled Actions</span>
                <span class="mono muted" style="font-size:11px;">${hyps.length} Hypothetical &bull; ${actions.length} Action${actions.length === 1 ? "" : "s"}</span>
              </div>
              ${hypRowsHtml}
              ${actRowsHtml}
            </div>

            <div class="mdl-in-box" style="padding:12px 14px;">
              <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
                <span class="mdl-in-lbl" style="font-size:12.5px;"><span class="mdl-in-glyph">&#9670;</span> Saved Simulation Parameters</span>
                <span class="mdl-in-pill mono">Global Sim Settings</span>
              </div>
              <div class="table-wrap">
                <table>
                  <tbody>
                    <tr><td class="muted">Active Prediction Lens</td><td class="num mono">${escHtml(formatModelLensLabel(s.simActiveModel || "single_path"))}</td></tr>
                    <tr><td class="muted">Annual Retirement Burn (Today's $)</td><td class="num mono">${fmtUSD((s.simAnnualBurn != null && Number.isFinite(Number(s.simAnnualBurn))) ? Math.max(0, Number(s.simAnnualBurn)) : RETIREMENT_ANNUAL_BURN)} / yr</td></tr>
                    <tr><td class="muted">Annual Inflation / SSN COLA</td><td class="num mono">${Number(s.simInflationPct ?? RETIREMENT_INFLATION_REF).toFixed(1)}% / ${Number(s.ssnColaPct ?? 2.8).toFixed(1)}%</td></tr>
                    ${retAgeRowHtml}
                    ${ssnClaimRowHtml}
                    <tr><td class="muted">Liquidation Strategy / Tax View</td><td class="num mono">${escHtml(_fmtPresetLbl(s.simOrderPreset))}${s.simBearShield !== false ? " + Bear Shield" : ""} &bull; ${escHtml(s.taxMode === "gross" ? "Pre-Tax" : "After-Tax Net")}</td></tr>
                    <tr><td class="muted">Include Unvested / SSN / Actions</td><td class="num mono">${s.simIncludeUnvested !== false ? "Yes" : "No"} / ${s.simIncludeSSN !== false ? "Yes" : "No"} / ${s.simIncludeScheduledActions !== false ? "Yes" : "No"}</td></tr>
                    <tr><td class="muted">MC Model / Paths</td><td class="num mono">${escHtml(s.mcReturnModel === "historical" ? "Historical Bootstrap" : "Normal (Multi-Asset)")} &bull; 500 paths</td></tr>
                    <tr><td class="muted">Market Correction</td><td class="num mono">${escHtml(_fmtStressLbl(s.simStressScenario))}${s.simStressScenario === "custom" ? ` (${s.simCustomShockPct}% / ${formatStressMonths(s.simCustomDownMonths ?? 12)} down)` : ""}</td></tr>
                  </tbody>
                </table>
              </div>
            </div>

            <div class="mdl-in-box" style="padding:12px 14px;">
              <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
                <span class="mdl-in-lbl" style="font-size:12.5px;"><span class="mdl-in-glyph">&#9670;</span> Per-Holding Annual Growth Overrides</span>
                <span class="mdl-in-pill mono">${nonZeroHf.length} Override${nonZeroHf.length === 1 ? "" : "s"}</span>
              </div>
              ${holdingOverridesHtml}
            </div>
          </div>

          <div class="panel" style="margin:0;padding:14px 16px;background:rgba(15,23,42,0.72);">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;padding-bottom:8px;border-bottom:1px solid #1e293b;">
              <span style="font-size:13.5px;font-weight:700;color:#f8fafc;">Scenario Thesis, Trade Rationale &amp; Audit Notes</span>
              <div style="display:flex;gap:6px;">
                ${isEditingDetailNotes ? `
                  <button type="button" class="btn" style="font-size:11.5px;padding:3px 9px;" onclick="toggleDetailNotesEdit(false)">Cancel</button>
                  <button type="button" class="btn btn-green" style="font-size:11.5px;padding:3px 10px;" data-sa-action="save-detail-notes" data-sa-id="${escHtml(entry.id)}">Save Notes</button>
                ` : `
                  <button type="button" class="btn" style="font-size:11.5px;padding:3px 10px;" onclick="toggleDetailNotesEdit(true)">&#9998; Edit Notes</button>
                `}
              </div>
            </div>
            ${isEditingDetailNotes ? `
              <textarea aria-label="Scenario Notes" id="model-detail-notes-editor" class="save-name-input mono" rows="18" style="width:100%;box-sizing:border-box;resize:vertical;line-height:1.5;font-size:12px;">${escHtml(entry.notes_md || "")}</textarea>
            ` : `
              <div class="md-render">${renderMarkdownNotes(entry.notes_md)}</div>
            `}
          </div>
        </div>
      `;
    }

    function toggleDetailNotesEdit(editing) {
      isEditingDetailNotes = Boolean(editing);
      _allowNotesEditorRender = true;
      try {
        renderModelsScreen();
      } finally {
        _allowNotesEditorRender = false;
      }
    }

    async function saveDetailNotes(id) {
      if (typeof isDemoModeActive === "function" && isDemoModeActive()) {
        showDemoBlockedAlert();
        return;
      }
      const entry = findModelEntry(id);
      const ed = document.getElementById("model-detail-notes-editor");
      if (!entry || !ed) return;
      const nextNotes = ed.value;
      entry.notes_md = nextNotes;
      isEditingDetailNotes = false;
      persistLocalSavesRegistry();
      try {
        const { ok, data } = await apiJson("/api/models/notes", { body: { id: entry.id, notes_md: nextNotes } });
        if (ok && data) {
          ingestSavesListPayload(data, isSaveDirty);
        }
      } catch (_e) {
        // Offline fallback already updated in memory
      }
      renderModelsScreen();
    }

    let _protectConfirmResolve = null;

    function openProtectConfirmDialog(questionHtml) {
      return new Promise(resolve => {
        const backdrop = document.getElementById("protect-confirm-backdrop");
        const qEl = document.getElementById("protect-dlg-question");
        if (!backdrop || !qEl) {
          resolve(typeof window.confirm === "function" ? window.confirm(questionHtml.replace(/<[^>]+>/g, "")) : false);
          return;
        }
        qEl.innerHTML = questionHtml;
        _protectConfirmResolve = resolve;
        if (backdrop.classList) backdrop.classList.add("open");
        backdrop.style.display = "flex";
      });
    }

    function closeProtectConfirmDialog(confirmed) {
      const backdrop = document.getElementById("protect-confirm-backdrop");
      if (backdrop) {
        if (backdrop.classList) backdrop.classList.remove("open");
        backdrop.style.display = "none";
      }
      const fn = _protectConfirmResolve;
      _protectConfirmResolve = null;
      if (typeof fn === "function") fn(Boolean(confirmed));
    }

    // DESIGN INTENT (Scenario Protection & SA2-13):
    // - All saved scenarios start unprotected; protection is purely a user decision toggled via confirmation
    //   dialog (never auto-derived from reserved words). Users can still overwrite/rename/delete their own
    //   protected scenarios after explicit confirmation (`confirm_protected=1`).
    // - Block protection toggles client-side when Demo Mode is active (`SA2-13`).
    async function toggleModelProtection(id, evt) {
      if (evt && typeof evt.stopPropagation === "function") evt.stopPropagation();
      if (typeof isDemoModeActive === "function" && isDemoModeActive()) {
        showDemoBlockedAlert();
        return;
      }
      const entry = findModelEntry(id);
      if (!entry) return;
      const nextProtected = !Boolean(entry.is_protected);
      const qHtml = nextProtected
        ? `Enable protection for <strong>${escHtml(entry.name)}</strong> (#${escHtml(entry.id)})?`
        : `Remove protection from <strong>${escHtml(entry.name)}</strong> (#${escHtml(entry.id)})?`;
      const confirmed = await openProtectConfirmDialog(qHtml);
      if (!confirmed) return;

      entry.is_protected = nextProtected;
      if (savesRegistry[entry.name]) savesRegistry[entry.name].is_protected = nextProtected;
      try {
        const { ok, data } = await apiJson("/api/models/protect", {
          body: { id: entry.id, name: entry.name, is_protected: nextProtected },
        });
        if (ok && data) {
          ingestSavesListPayload(data, isSaveDirty);
        }
      } catch (_e) {
        // Offline fallback already updated in memory
      }
      persistLocalSavesRegistry();
      renderModelsScreen();
      if (typeof renderSaveDialogList === "function") {
        const backdrop = document.getElementById("save-dialog-backdrop");
        if (backdrop && backdrop.classList.contains("open")) renderSaveDialogList();
      }
    }

    async function renameDetailModel(id) {
      if (typeof isDemoModeActive === "function" && isDemoModeActive()) {
        showDemoBlockedAlert();
        return;
      }
      const entry = findModelEntry(id);
      if (!entry) return;
      if (entry.is_protected && typeof window.confirm === "function") {
        const ok = window.confirm(`"${entry.name}" is a Protected Scenario.\n\nAre you sure you want to rename it?`);
        if (!ok) return;
      }
      const nextRaw = window.prompt("Enter new friendly display name for this Scenario:", entry.name);
      if (!nextRaw) return;
      const clean = sanitizeSaveNameClient(nextRaw);
      if (!clean || clean === entry.name) return;
      try {
        const { ok, data } = await apiJson("/api/models/rename", {
          body: { id: entry.id, new_name: clean, confirm_protected: Boolean(entry.is_protected) },
        });
        if (ok && data) {
          ingestSavesListPayload(data, isSaveDirty);
          if (currentModelId === entry.id) currentSaveName = clean;
          updateSaveStatusUI(false);
          renderModelsScreen();
        }
      } catch (_e) {
        // Ignore offline error
      }
    }

    async function duplicateDetailModel(id) {
      if (typeof isDemoModeActive === "function" && isDemoModeActive()) {
        showDemoBlockedAlert();
        return;
      }
      const entry = findModelEntry(id);
      if (!entry || !entry.settings) return;
      const rawName = window.prompt("Name for duplicated Scenario:", `${entry.name} (Copy)`);
      if (!rawName) return;
      const clean = sanitizeSaveNameClient(rawName);
      if (!clean) return;
      await persistSaveToBackend(clean, entry.settings, true, {
        createNew: true,
        notes_md: entry.notes_md || "",
        kpi_summary: entry.kpi_summary || undefined,
      });
      const created = savesRegistry[clean];
      if (created) {
        selectedModelDetailId = created.id;
        applySettingsSnapshot(created.settings);
      }
      renderModelsScreen();
    }

    async function deleteDetailModel(id) {
      if (typeof isDemoModeActive === "function" && isDemoModeActive()) {
        showDemoBlockedAlert();
        return;
      }
      const entry = findModelEntry(id);
      if (!entry) return;
      const msg = entry.is_protected
        ? `"${entry.name}" (#${entry.id}) is a Protected Scenario.\n\nAre you sure you want to permanently delete it?`
        : `Permanently delete Scenario "${entry.name}" (#${entry.id})?`;
      if (typeof window.confirm === "function" && !window.confirm(msg)) return;
      try {
        const delPath = entry.is_protected
          ? `/api/models/delete?id=${encodeURIComponent(entry.id)}&confirm_protected=1`
          : `/api/models/delete?id=${encodeURIComponent(entry.id)}`;
        const { ok, data } = await apiJson(delPath, {
          body: { id: entry.id, name: entry.name, confirm_protected: Boolean(entry.is_protected) },
        });
        if (ok && data) {
          delete savesRegistry[entry.name];
          modelsList = modelsList.filter(m => m.id !== entry.id);
          ingestSavesListPayload(data);
          selectedModelDetailId = null;
          const activeNow = findModelEntry(currentModelId) || modelsList[0];
          if (activeNow && activeNow.settings) {
            currentModelId = activeNow.id;
            currentSaveName = activeNow.name;
            applySettingsSnapshot(activeNow.settings);
          } else {
            currentModelId = "";
            currentSaveName = "";
            updateSaveStatusUI(false);
          }
          renderModelsScreen();
        }
      } catch (_e) {
        // Ignore offline
      }
    }

    function showSaveDialogMsg(msg, color = "var(--muted)") {
      const el = document.getElementById("save-dialog-msg");
      if (!el) return;
      el.textContent = msg;
      el.style.color = color;
    }

    let _saveBackdropMouseDown = false;

    function onSaveBackdropMouseDown(evt) {
      _saveBackdropMouseDown = Boolean(evt && evt.target && evt.target.id === "save-dialog-backdrop");
    }

    async function openSaveDialog(mode = "save", opts = {}) {
      window._saveDialogMode = mode === "load" ? "load" : "save";
      _saveBackdropMouseDown = false;
      const backdrop = document.getElementById("save-dialog-backdrop");
      if (!backdrop) return;
      backdrop.classList.add("open");

      const titleTextEl = document.getElementById("save-dlg-title-text");
      if (titleTextEl) {
        titleTextEl.textContent = window._saveDialogMode === "load" ? "Load Scenario" : "Save Scenario";
      }

      const primaryBtn = document.getElementById("dlg-primary-btn");
      if (primaryBtn) {
        primaryBtn.textContent = window._saveDialogMode === "load" ? "Load" : "Save";
      }

      const overwriteBtn = document.getElementById("dlg-overwrite-btn");
      if (overwriteBtn) {
        overwriteBtn.style.display = window._saveDialogMode === "load" ? "none" : "inline-flex";
      }

      const inputBox = document.getElementById("save-dlg-input-box");
      if (inputBox) {
        inputBox.style.display = window._saveDialogMode === "load" ? "none" : "flex";
      }

      const shortcutHint = document.getElementById("save-dlg-shortcut-hint");
      if (shortcutHint) {
        shortcutHint.style.display = window._saveDialogMode === "load" ? "none" : "inline";
      }

      selectedDialogSaveName = currentSaveName;
      const inputEl = document.getElementById("save-name-input");
      const prefill = (opts && typeof opts.prefillName === "string") ? opts.prefillName : "";
      if (inputEl) {
        inputEl.value = prefill;
        inputEl.dataset.autoPrefill = prefill ? "1" : "";
        if (prefill) onSaveNameInputChange(prefill, true);
      }
      renderSaveDialogList();

      const msgEl = document.getElementById("save-dialog-msg");
      if (msgEl) msgEl.textContent = "";

      if (window._saveDialogMode === "save" && inputEl) {
        setTimeout(() => {
          inputEl.focus();
          if (prefill) inputEl.select();
        }, 30);
      }

      await refreshSavesFromServer();
      if (!selectedDialogSaveName || !savesRegistry[selectedDialogSaveName]) {
        selectedDialogSaveName = currentSaveName;
      }
      renderSaveDialogList();
    }

    function closeSaveDialog(evt) {
      if (evt) {
        const fromBackdrop = Boolean(evt.target && evt.target.id === "save-dialog-backdrop");
        const startedOnBackdrop = _saveBackdropMouseDown;
        _saveBackdropMouseDown = false;
        if (!fromBackdrop || !startedOnBackdrop) return;
      } else {
        _saveBackdropMouseDown = false;
      }
      const backdrop = document.getElementById("save-dialog-backdrop");
      if (backdrop) backdrop.classList.remove("open");
    }

    function summarizeSaveSettings(s) {
      if (!s) return "Default configuration";
      const lens = formatModelLensLabel(s.simActiveModel || "single_path");
      const burn = Math.round(((s.simAnnualBurn != null && Number.isFinite(Number(s.simAnnualBurn))) ? Math.max(0, Number(s.simAnnualBurn)) : RETIREMENT_ANNUAL_BURN) / 1000);
      const inf = Number(s.simInflationPct ?? RETIREMENT_INFLATION_REF);
      const _k0 = INV0.claim_setting_key || "ssnInvestor1ClaimAge";
      const jAge = s[_k0] || s.ssnInvestor1ClaimAge || 67;
      let ssnSummaryStr = `SSN ${jAge}`;
      if (HAS_SECOND_INVESTOR && INV1) {
        const _k1 = INV1.claim_setting_key || "ssnInvestor2ClaimAge";
        const yAge = s[_k1] || s.ssnInvestor2ClaimAge || 67;
        ssnSummaryStr = `SSN ${jAge}/${yAge}`;
      }
      const acts = Array.isArray(s.simScheduledActions) ? s.simScheduledActions : (s.simScheduledWithdrawals || []);
      const hyps = Array.isArray(s.simHypotheticalAssets) ? s.simHypotheticalAssets : [];
      const suffix = [
        hyps.length > 0 ? `${hyps.length} Hyp` : "",
        acts.length > 0 ? `${acts.length} Action${acts.length === 1 ? "" : "s"}` : "",
      ].filter(Boolean).join(" \u2022 ");
      return `${lens} \u2022 Burn $${burn}K (${inf.toFixed(1)}% inf) \u2022 ${ssnSummaryStr}${suffix ? " \u2022 " + suffix : ""}`;
    }

    function renderSaveDialogList() {
      const listEl = document.getElementById("save-files-list");
      const countEl = document.getElementById("save-count-pill");
      const activeNameEl = document.getElementById("save-dlg-active-name");
      const primaryBtn = document.getElementById("dlg-primary-btn");
      const loadBtn = document.getElementById("dlg-load-btn");
      const overwriteBtn = document.getElementById("dlg-overwrite-btn");
      const delBtn = document.getElementById("dlg-delete-btn");
      const writeBtn = document.getElementById("dlg-save-btn");
      const inputEl = document.getElementById("save-name-input");
      if (activeNameEl) activeNameEl.textContent = currentSaveName || "Default";
      if (!listEl) return;

      const entries = modelsList.length > 0 ? [...modelsList] : Object.values(savesRegistry);
      if (countEl) {
        countEl.textContent = `${entries.length} ${entries.length === 1 ? "Scenario" : "Scenarios"}`;
      }

      if (entries.length === 0) {
        listEl.innerHTML = `<div class="muted" style="padding:18px;text-align:center;font-size:12.5px;">No Scenarios in Scenarios/ yet. Type a name below and click Save.</div>`;
      } else {
        listEl.innerHTML = entries.map(item => {
          const isSel = item.name === selectedDialogSaveName;
          const isCur = item.name === currentSaveName;
          const activeBadge = isCur
            ? `<span class="pill ${isSaveDirty ? 'pill-rose' : 'pill-green'} mono" style="font-size:10px;padding:1px 6px;">${isSaveDirty ? "Active \u2022 Modified" : "Active"}</span>`
            : "";
          const lockBadge = item.is_protected
            ? `<button type="button" class="pill pill-slate mono" data-sa-action="toggle-model-protect" data-sa-id="${escHtml(item.id)}" data-sa-stop="1" title="Protected Scenario — click to manage protection" style="font-size:9.5px;padding:1px 6px;cursor:pointer;border:1px solid #475569;">\uD83D\uDD12 Protected</button>`
            : `<button type="button" class="pill pill-slate mono" data-sa-action="toggle-model-protect" data-sa-id="${escHtml(item.id)}" data-sa-stop="1" title="Unprotected Scenario — click to enable protection" style="font-size:9.5px;padding:1px 6px;cursor:pointer;opacity:0.75;border:1px dashed #334155;">\uD83D\uDD13 Unprotected</button>`;
          const summary = summarizeSaveSettings(item.settings);
          return `
            <div class="save-row ${isSel ? 'selected' : ''}"
                 data-sa-action="select-dialog-save"
                 data-sa-dbl="load-dialog-save"
                 data-sa-name="${escHtml(item.name)}"
                 title="Click to select '${escHtml(item.name)}'">
              <div class="save-row-left">
                <div class="save-row-name">
                  <span class="mono">${escHtml(item.name)}</span>
                  ${lockBadge}
                  ${activeBadge}
                </div>
                <div class="save-row-meta mono">${escHtml(summary)}</div>
              </div>
              <div class="save-row-right">
                <span class="mono muted" style="font-size:10.8px;">${escHtml(item.updated_at || "")}</span>
              </div>
            </div>
          `;
        }).join("");
      }

      const selEntry = selectedDialogSaveName ? savesRegistry[selectedDialogSaveName] : null;
      const hasSel = Boolean(selEntry);

      if (primaryBtn) {
        if (window._saveDialogMode === "load") {
          primaryBtn.disabled = !hasSel;
          primaryBtn.textContent = "Load";
        } else {
          primaryBtn.disabled = false;
          primaryBtn.textContent = "Save";
        }
      }

      if (loadBtn) {
        loadBtn.disabled = !hasSel;
        loadBtn.textContent = hasSel ? `Load "${selectedDialogSaveName}"` : "Load Selected";
      }
      if (overwriteBtn) {
        overwriteBtn.disabled = !hasSel;
        overwriteBtn.textContent = hasSel ? `Overwrite "${selectedDialogSaveName}"` : "Overwrite Selected";
      }
      if (delBtn) {
        delBtn.disabled = !hasSel;
        delBtn.textContent = hasSel ? `Delete "${selectedDialogSaveName}"` : "Delete Selected";
      }

      const typedName = inputEl ? sanitizeSaveNameClient(inputEl.value) : "";
      if (writeBtn) {
        if (typedName && savesRegistry[typedName]) {
          writeBtn.textContent = `Overwrite "${typedName}"`;
        } else if (typedName) {
          writeBtn.textContent = `Create "${typedName}"`;
        } else {
          writeBtn.textContent = "Create New Scenario";
        }
      }
    }

    function selectDialogSave(name) {
      selectedDialogSaveName = name;
      const inputEl = document.getElementById("save-name-input");
      if (inputEl && window._saveDialogMode !== "load" && inputEl.dataset.autoPrefill === "1") {
        // Clear an unedited auto-prefill ("... (Custom)") when picking an existing row so Save and Overwrite agree
        inputEl.value = "";
        inputEl.dataset.autoPrefill = "";
      }
      renderSaveDialogList();
      const msgEl = document.getElementById("save-dialog-msg");
      if (msgEl) msgEl.textContent = "";
    }

    function onSaveNameInputChange(val, _isAutoPrefill = false) {
      const inputEl = document.getElementById("save-name-input");
      if (inputEl && !_isAutoPrefill) {
        inputEl.dataset.autoPrefill = "";
      }
      const trimmed = sanitizeSaveNameClient(val || "");
      if (trimmed) {
        const match = savesRegistry[trimmed]
          || modelsList.find(m => String((m && m.name) || "").trim().toLowerCase() === trimmed.toLowerCase());
        if (match) selectedDialogSaveName = match.name;
      }
      renderSaveDialogList();
    }

    function onSaveNameKeyDown(evt) {
      if (evt.key === "Enter" && !evt.isComposing) {
        evt.preventDefault();
        onDialogPrimaryAction();
      }
    }

    async function onDialogPrimaryAction() {
      if (window._saveDialogMode === "load") {
        await loadSelectedDialogSave();
      } else {
        await saveFromDialog();
      }
    }

    function sanitizeSaveNameClient(raw) {
      // Match server `sanitize_save_name`: strip control chars & HTML angle brackets while keeping apostrophes/punctuation
      return String(raw || "")
        .trim()
        .replace(/\.json$/i, "")
        .replace(/[\x00-\x1f<>]+/g, " ")
        .replace(/\s+/g, " ")
        .replace(/^[ .]+|[ .]+$/g, "")
        .slice(0, 96);
    }

    let _saveDialogSubmitting = false;

    async function reportSaveDialogStep(msg, color = "#94a3b8") {
      showSaveDialogMsg(msg, color);
      await yieldForUiPaint();
    }

    async function overwriteSelectedDialogSave() {
      if (_saveDialogSubmitting) return;
      if (isDemoModeActive()) {
        showSaveDialogMsg("Saving is not permitted while Demo Mode is active.", "#fda4af");
        showDemoBlockedAlert();
        return;
      }
      const targetName = selectedDialogSaveName;
      const existing = targetName
        ? (savesRegistry[targetName]
            || modelsList.find(m => String((m && m.name) || "").trim().toLowerCase() === String(targetName).trim().toLowerCase())
            || null)
        : null;
      if (!existing) {
        showSaveDialogMsg("Please select a Scenario row in the list to overwrite.", "#fda4af");
        return;
      }
      const resolvedName = existing.name || targetName;
      if (existing.is_protected && typeof window.confirm === "function") {
        const ok = window.confirm(
          `"${resolvedName}" is a Protected Scenario.\n\nClick OK to overwrite "${resolvedName}", or Cancel.`
        );
        if (!ok) return;
      }
      const primaryBtn = document.getElementById("dlg-primary-btn");
      const overwriteBtn = document.getElementById("dlg-overwrite-btn");
      _saveDialogSubmitting = true;
      if (primaryBtn) {
        primaryBtn.disabled = true;
        primaryBtn.textContent = "Saving...";
      }
      if (overwriteBtn) {
        overwriteBtn.disabled = true;
        overwriteBtn.textContent = "Saving...";
      }
      try {
        await reportSaveDialogStep("Collecting current scenario settings...");
        const snap = getCurrentSettingsSnapshot();
        const saveRes = await persistSaveToBackend(resolvedName, snap, true, {
          id: existing.id,
          overwriteExisting: Boolean(existing.is_protected),
          onProgress: reportSaveDialogStep,
        });
        if (!saveRes || !saveRes.ok) {
          showSaveDialogMsg((saveRes && saveRes.error) || "Failed to overwrite Scenario.", "#fda4af");
          return;
        }
        loadedSettingsSnapshot = snap;
        isSaveDirty = false;
        selectedDialogSaveName = resolvedName;
        const inputEl = document.getElementById("save-name-input");
        if (inputEl) {
          inputEl.value = "";
          inputEl.dataset.autoPrefill = "";
        }
        updateSaveStatusUI(true);
        renderSaveDialogList();
        showSaveDialogMsg(`Overwrote "${resolvedName}" with current settings & fresh snapshot.`, "#34d399");
      } catch (err) {
        console.error("overwriteSelectedDialogSave failed:", err);
        showSaveDialogMsg((err && err.message) || "Unexpected error while overwriting Scenario.", "#fda4af");
      } finally {
        _saveDialogSubmitting = false;
        if (primaryBtn) {
          primaryBtn.disabled = false;
          primaryBtn.textContent = window._saveDialogMode === "load" ? "Load" : "Save";
        }
        renderSaveDialogList();
      }
    }

    async function saveFromDialog() {
      if (_saveDialogSubmitting) return;
      if (isDemoModeActive()) {
        showSaveDialogMsg("Saving is not permitted while Demo Mode is active.", "#fda4af");
        showDemoBlockedAlert();
        return;
      }
      const inputEl = document.getElementById("save-name-input");
      const rawName = inputEl ? inputEl.value.trim() : "";
      let cleanName = sanitizeSaveNameClient(rawName);
      if (!cleanName && selectedDialogSaveName) {
        cleanName = sanitizeSaveNameClient(selectedDialogSaveName);
      }
      if (!cleanName) {
        showSaveDialogMsg("Please enter a Scenario name or select a Scenario to save.", "#fda4af");
        if (inputEl) inputEl.focus();
        return;
      }
      const cleanLow = cleanName.toLowerCase();
      const existing = savesRegistry[cleanName]
        || modelsList.find(m => String(m && m.name || "").trim().toLowerCase() === cleanLow)
        || null;
      const targetName = existing ? existing.name : cleanName;
      if (existing && existing.is_protected && typeof window.confirm === "function") {
        const ok = window.confirm(
          `"${targetName}" is a Protected Scenario.\n\nClick OK to overwrite "${targetName}", or Cancel to choose a different name.`
        );
        if (!ok) {
          if (inputEl) {
            inputEl.value = `${targetName} (Custom)`;
            inputEl.focus();
            inputEl.select();
          }
          return;
        }
      }

      const primaryBtn = document.getElementById("dlg-primary-btn");
      const overwriteBtn = document.getElementById("dlg-overwrite-btn");
      _saveDialogSubmitting = true;
      if (primaryBtn) {
        primaryBtn.disabled = true;
        primaryBtn.textContent = "Saving...";
      }
      if (overwriteBtn) {
        overwriteBtn.disabled = true;
      }
      try {
        await reportSaveDialogStep("Collecting current scenario settings...");
        const snap = getCurrentSettingsSnapshot();
        const saveRes = await persistSaveToBackend(targetName, snap, true, {
          id: existing ? existing.id : undefined,
          createNew: !existing,
          overwriteExisting: Boolean(existing && existing.is_protected),
          onProgress: reportSaveDialogStep,
        });
        if (!saveRes || !saveRes.ok) {
          showSaveDialogMsg((saveRes && saveRes.error) || "Failed to save Scenario.", "#fda4af");
          return;
        }
        loadedSettingsSnapshot = snap;
        isSaveDirty = false;
        selectedDialogSaveName = currentSaveName || targetName;
        if (inputEl) {
          inputEl.value = "";
          inputEl.dataset.autoPrefill = "";
        }
        updateSaveStatusUI(true);
        await reportSaveDialogStep(`Saved "${selectedDialogSaveName}"!`, "#34d399");
        closeSaveDialog();
      } catch (err) {
        console.error("saveFromDialog failed:", err);
        showSaveDialogMsg((err && err.message) || "Unexpected error while saving Scenario.", "#fda4af");
      } finally {
        _saveDialogSubmitting = false;
        if (primaryBtn) {
          primaryBtn.disabled = false;
          primaryBtn.textContent = window._saveDialogMode === "load" ? "Load" : "Save";
        }
        renderSaveDialogList();
      }
    }

    async function loadSelectedDialogSave() {
      const targetName = selectedDialogSaveName;
      const entry = savesRegistry[targetName];
      if (!entry || !entry.settings) {
        showSaveDialogMsg("Please select a Scenario row in the list to load.", "#fda4af");
        return;
      }
      await loadModelById(entry.id || entry.name, false);
      closeSaveDialog();
    }

    async function deleteSelectedDialogSave() {
      if (typeof isDemoModeActive === "function" && isDemoModeActive()) {
        showSaveDialogMsg("Deleting is not permitted while Demo Mode is active.", "#fda4af");
        showDemoBlockedAlert();
        return;
      }
      const targetName = selectedDialogSaveName;
      const entry = targetName ? savesRegistry[targetName] : null;
      if (!entry) {
        showSaveDialogMsg("Select a Scenario row in the list to delete.", "#fda4af");
        return;
      }
      const msg = entry.is_protected
        ? `"${targetName}" is a Protected Scenario.\n\nAre you sure you want to permanently delete it?`
        : `Permanently delete Scenario "${targetName}"?`;
      if (typeof window.confirm === "function" && !window.confirm(msg)) {
        return;
      }
      try {
        const delPath = entry.is_protected
          ? `/api/models/delete?id=${encodeURIComponent(entry.id)}&confirm_protected=1`
          : `/api/models/delete?id=${encodeURIComponent(entry.id)}`;
        const { ok, data } = await apiJson(delPath, {
          body: { id: entry.id, name: targetName, confirm_protected: Boolean(entry.is_protected) },
        });
        if (ok && data) {
          delete savesRegistry[targetName];
          modelsList = modelsList.filter(m => m.id !== entry.id);
          ingestSavesListPayload(data);
          persistLocalSavesRegistry();
        }
      } catch (_e) {
        // Handled transparently by apiJson
      }
      const nextEntry = findModelEntry(currentModelId) || modelsList[0];
      if (nextEntry && nextEntry.settings) {
        currentModelId = nextEntry.id;
        currentSaveName = nextEntry.name;
        applySettingsSnapshot(nextEntry.settings);
      } else {
        currentModelId = "";
        currentSaveName = "";
      }
      selectedDialogSaveName = currentSaveName;
      updateSaveStatusUI(false);
      renderSaveDialogList();
      renderModelsScreen();
      showSaveDialogMsg(`Deleted Scenario "${targetName}".`, "#7dd3fc");
    }

    let isRebuildingSite = false;
    let rebuildNeedsReload = false;

    function formatRebuildLogsHtml(rawText, forceError = false) {
      const text = String(rawText || "").trim();
      if (!text) {
        return `<ul class="rebuild-log-list"><li class="rebuild-log-row log-info"><span class="rebuild-log-bullet">&bull;</span><span>Rebuild completed with no warnings or errors.</span></li></ul>`;
      }
      const lines = text.split(/\r?\n/).map(l => l.trim()).filter(Boolean);
      const items = lines.map(line => {
        let rest = line;
        let rowCls = forceError ? "log-err" : "log-info";
        let bullet = "&bull;";
        let prefixHtml = "";

        if (/^\[ADD\]\s*/i.test(rest)) {
          rowCls = "log-add";
          bullet = "&#10010;";
          rest = rest.replace(/^\[ADD\]\s*/i, "");
        } else if (/^\[CHANGE\]\s*/i.test(rest)) {
          rowCls = "log-warn";
          bullet = "&#9888;";
          rest = rest.replace(/^\[CHANGE\]\s*/i, "");
          prefixHtml = `<strong>WARNING:</strong> `;
        } else if (/^\[DELETE\]\s*/i.test(rest)) {
          rowCls = "log-err";
          bullet = "&#10006;";
          rest = rest.replace(/^\[DELETE\]\s*/i, "");
        } else if (/^WARNING:\s*/i.test(rest)) {
          rowCls = "log-warn";
          bullet = "&#9888;";
          rest = rest.replace(/^WARNING:\s*/i, "");
          prefixHtml = `<strong>WARNING:</strong> `;
        } else if (/^(ERROR:|Traceback)/i.test(rest)) {
          rowCls = "log-err";
          bullet = "&#10006;";
        } else if (/\b(deleted|removed)\b/i.test(rest)) {
          rowCls = "log-err";
          bullet = "&#10006;";
        } else if (/\b(warning|skipped|Closed\/redeemed|Quantity changed|Anchor updated|renamed|transferred)\b/i.test(rest)) {
          rowCls = "log-warn";
          bullet = "&#9888;";
          prefixHtml = `<strong>WARNING:</strong> `;
        } else if (/\b(Added|Ingested|New position|Loaded \d+)\b/i.test(rest)) {
          rowCls = "log-add";
          bullet = "&#10010;";
        }

        let stageBadge = "";
        const stageMatch = rest.match(/^\[([a-zA-Z0-9_-]+)\]\s*(.*)$/);
        if (stageMatch) {
          stageBadge = `<span class="rebuild-log-stage">${escHtml(stageMatch[1])}</span>`;
          rest = stageMatch[2];
        }
        rest = rest.replace(/^WARNING:\s*/i, "");

        const bodyHtml = escHtml(rest)
          .replace(/(\$[0-9,]+(?:\.[0-9]+)?)/g, "<strong>$1</strong>")
          .replace(/(\b\d[0-9,]*\s+(?:statement file\(s\)|symbol\(s\)|cached price record\(s\)|new price record\(s\)|existing price record\(s\)|price record\(s\)|weekly timeline bar\(s\)|bytes)\b)/g, "<strong>$1</strong>");

        return `<li class="rebuild-log-row ${rowCls}"><span class="rebuild-log-bullet">${bullet}</span><div style="min-width:0;flex:1;">${stageBadge}${prefixHtml}${bodyHtml}</div></li>`;
      });
      return `<ul class="rebuild-log-list">${items.join("")}</ul>`;
    }

    function closeRebuildBackdrop() {
      if (isRebuildingSite) return;
      const bd = document.getElementById("rebuild-backdrop");
      if (bd) bd.classList.remove("open");
      if (rebuildNeedsReload) {
        rebuildNeedsReload = false;
        window.location.reload();
      }
    }

    async function triggerSiteRebuild() {
      if (isRebuildingSite) return;
      isRebuildingSite = true;
      rebuildNeedsReload = false;

      const barEl = document.getElementById("nav-refresh-bar");
      const bdEl = document.getElementById("rebuild-backdrop");
      const ringEl = document.getElementById("rebuild-spinner-ring");
      const fillEl = document.getElementById("rebuild-bar-fill");
      const titleEl = document.getElementById("rebuild-title");
      const msgEl = document.getElementById("rebuild-sub");
      const logEl = document.getElementById("rebuild-log");
      const ftrEl = document.getElementById("rebuild-ftr");

      if (barEl) barEl.classList.add("running");

      if (ringEl) ringEl.className = "rebuild-spinner-ring";
      if (fillEl) fillEl.className = "rebuild-bar-fill";
      if (titleEl) titleEl.textContent = "Refreshing Portfolio...";
      if (msgEl) msgEl.textContent = "Scanning statements, syncing live market quotes, and updating portfolio views...";
      if (logEl) {
        logEl.style.display = "block";
        logEl.innerHTML = `<ul class="rebuild-log-list"><li class="rebuild-log-row log-info"><span class="rebuild-log-bullet">&bull;</span><div><span class="rebuild-log-stage">status</span>Scanning statement files, fetching latest market quotes, and updating weekly timeline bars...</div></li></ul>`;
      }
      if (ftrEl) ftrEl.style.display = "none";
      if (bdEl) bdEl.classList.add("open");

      try {
        const { ok, status, data } = await apiJson("/api/rebuild", { body: {} });
        if (ok && data && data.ok) {
          rebuildNeedsReload = true;
          if (ringEl) ringEl.className = "rebuild-spinner-ring done";
          if (fillEl) fillEl.className = "rebuild-bar-fill done";
          if (titleEl) titleEl.textContent = "Portfolio Refresh Complete";
          if (msgEl) msgEl.textContent = "Statements reconciled and market quotes updated — click OK to reload dashboard.";
          if (logEl) {
            logEl.style.display = "block";
            logEl.innerHTML = formatRebuildLogsHtml(data.stdout, false);
          }
          if (ftrEl) ftrEl.style.display = "flex";
          const okBtn = document.getElementById("rebuild-ok-btn");
          if (okBtn) okBtn.focus();
          return;
        }

        const errText = (data && (data.error || data.stderr || data.stdout)) || `Server returned HTTP ${status}`;
        if (ringEl) ringEl.className = "rebuild-spinner-ring error";
        if (fillEl) fillEl.className = "rebuild-bar-fill error";
        if (titleEl) titleEl.textContent = "Portfolio Refresh Failed";
        if (msgEl) msgEl.textContent = "Refresh encountered an error — existing portfolio data and views were not modified.";
        if (logEl) {
          logEl.style.display = "block";
          logEl.innerHTML = formatRebuildLogsHtml(errText, true);
        }
        if (ftrEl) ftrEl.style.display = "flex";
      } catch (_err) {
        if (ringEl) ringEl.className = "rebuild-spinner-ring error";
        if (fillEl) fillEl.className = "rebuild-bar-fill error";
        if (titleEl) titleEl.textContent = "AdvisorBot Service Offline";
        if (msgEl) msgEl.innerHTML = "Could not reach the local AdvisorBot service. Please ensure the service is running.";
        if (logEl) {
          logEl.style.display = "block";
          logEl.innerHTML = formatRebuildLogsHtml("ERROR: Could not connect to local AdvisorBot rebuild service.", true);
        }
        if (ftrEl) ftrEl.style.display = "flex";
      } finally {
        isRebuildingSite = false;
        if (barEl) barEl.classList.remove("running");
      }
    }

    // =========================================================================
    // COLLAPSIBLE RIGHT-PANE AI CHAT ASSISTANT & REALTIME UI SYNC
    // =========================================================================
    const AI_MODEL_STORAGE = "portfolio_tracker_ai_model_v1";
    const AI_PANE_OPEN_STORAGE = "portfolio_tracker_ai_pane_open_v1";

    let aiModel = "default";
    let aiConversationId = null;
    let aiMessages = [];
    let aiIsSending = false;
    let lastUiEventSeq = 0;
    try {
      lastUiEventSeq = parseInt(sessionStorage.getItem("portfolio_last_ui_seq") || "0", 10) || 0;
    } catch (_e) {}
    let uiPollTimer = null;

    let availableAiModels = [];

    async function loadAiModels(probe = false) {
      const probeBtn = document.getElementById("ai-probe-models-btn");
      if (probeBtn && probe) {
        probeBtn.disabled = true;
        probeBtn.style.opacity = "0.5";
        probeBtn.style.cursor = "wait";
      }
      try {
        const { ok, data } = await apiJson(`/api/llm/models${probe ? "?probe=1" : ""}`);
        if (!ok || !data) throw new Error("Could not fetch models");
        if (data && data.ok && Array.isArray(data.models)) {
          availableAiModels = data.models.filter(m => m && m.type !== "cli" && m.provider !== "agy");
          renderAiModelUI();
        }
      } catch (err) {
        console.warn("[ai-models] Could not fetch /api/llm/models:", err);
      } finally {
        if (probeBtn) {
          probeBtn.disabled = false;
          probeBtn.style.opacity = "1";
          probeBtn.style.cursor = "pointer";
        }
      }
    }

    function _getModelStatusMeta(m) {
      const isCli = m && m.type === "cli";
      if (isCli) {
        const ready = m.status === "ready";
        return {
          dot: ready ? "🟢" : "⚪",
          suffix: ready ? "" : " (unavailable)",
          pillClass: ready ? "pill-green" : "pill-slate",
          dotColor: ready ? "#10b981" : "#94a3b8",
          pillLabel: ready ? "Ready" : "Unavailable",
        };
      }
      const missingKey = !m.has_key || m.status === "missing_key";
      if (missingKey) {
        return {
          dot: "⚪",
          suffix: " (needs key)",
          pillClass: "pill-slate",
          dotColor: "#94a3b8",
          pillLabel: "Needs Key",
        };
      }
      if (m.status === "error" || m.status === "quota") {
        const errStr = String(m.error || "");
        const isQuota = m.status === "quota" || /quota|rate.?limit|429/i.test(errStr);
        const isCredits = /credit|billing/i.test(errStr);
        return {
          dot: "🔴",
          suffix: isQuota ? " (quota/429)" : (isCredits ? " (no credits)" : " (error)"),
          pillClass: "pill-red",
          dotColor: "#f43f5e",
          pillLabel: isQuota ? "Quota Exceeded" : (isCredits ? "No Credits" : "Error"),
        };
      }
      if (m.status === "working") {
        return {
          dot: "🟢",
          suffix: m.latency_ms ? ` (${m.latency_ms}ms)` : "",
          pillClass: "pill-green",
          dotColor: "#10b981",
          pillLabel: m.latency_ms ? `Working (${m.latency_ms}ms)` : "Working",
        };
      }
      return {
        dot: "🟢",
        suffix: "",
        pillClass: "pill-blue",
        dotColor: "#38bdf8",
        pillLabel: "Ready",
      };
    }

    function _getModelTierKeyword(m) {
      const t = String((m && m.tier) || "").toLowerCase().trim();
      const mid = String((m && (m.model || m.id)) || "").toLowerCase().trim();
      if (t === "reasoning" || /\b(o1|o3|o4)\b/i.test(mid)) {
        return "Deep Reasoning";
      }
      if (t === "flagship" || t === "pro" || /opus|carbon|argon|\bpro\b/i.test(mid)) {
        return "Deep Thinking";
      }
      if (/haiku|lite|nano/i.test(mid) || t === "lite") {
        return "Fastest / Quick Tasks";
      }
      if (t === "creative" || /fable/i.test(mid)) {
        return "Creative / Nuanced";
      }
      if (t === "balanced" || /sonnet/i.test(mid)) {
        return "Everyday Usage";
      }
      if (t === "fast" || /flash|hydrogen|terra|[-_]mini\b/i.test(mid)) {
        return "Fast Workhorse";
      }
      return "Everyday Usage";
    }

    function _renderModelOptionHtml(m, optVal) {
      const meta = _getModelStatusMeta(m);
      const label = m.display_name || m.model || m.id;
      const kw = _getModelTierKeyword(m);
      const kwSuffix = kw ? ` · ${kw}` : "";
      const blockedSuffix = m.shield_blocked ? " (Blocked by Shield)" : "";
      const tipText = m.shield_blocked ? (m.shield_reason || "Blocked by Privacy Shield") : (m.error || m.description || "");
      const tipAttr = tipText ? ` title="${escHtml(String(tipText))}"` : "";
      const disAttr = m.shield_blocked ? " disabled" : "";
      return `<option value="${escHtml(optVal)}"${tipAttr}${disAttr}>${meta.dot} ${escHtml(label)}${escHtml(kwSuffix)}${escHtml(meta.suffix)}${escHtml(blockedSuffix)}</option>`;
    }

    function _providerGroupLabel(m) {
      const p = String((m && m.provider) || "").toLowerCase();
      if (p === "gemini") return "Gemini (Direct API)";
      if (p === "claude" || p === "anthropic") return "Anthropic Claude (Direct API)";
      if (p === "openai") return "OpenAI (Direct API)";
      return "Configured LLMs (Direct API)";
    }

    function _buildGroupedApiModelOptionsHtml(models) {
      const groups = new Map();
      for (const m of models) {
        if (!m || m.type === "cli" || m.provider === "agy") continue;
        const lbl = _providerGroupLabel(m);
        if (!groups.has(lbl)) groups.set(lbl, []);
        groups.get(lbl).push(m);
      }
      let html = "";
      for (const [lbl, items] of groups.entries()) {
        html += `<optgroup label="${escHtml(lbl)}">`;
        for (const m of items) {
          html += _renderModelOptionHtml(m, m.id);
        }
        html += `</optgroup>`;
      }
      return html;
    }

    function renderAiModelUI() {
      const sel = document.getElementById("ai-model-select");
      if (!sel) return;

      const savedPref = localStorage.getItem(AI_MODEL_STORAGE) || aiModel;
      const apiModels = (availableAiModels || []).filter(m => m && m.type !== "cli" && m.provider !== "agy");

      let html = _buildGroupedApiModelOptionsHtml(apiModels);
      if (!html) {
        html = `<option value="gemini" disabled>⚪ No Direct API LLMs Configured</option>`;
      }
      sel.innerHTML = html;

      // Select active model (prefer unblocked working/default API model, then unblocked keyed model)
      const isAllowed = m => Boolean(m && !m.shield_blocked);
      const isAvailable = m => Boolean(m && m.status !== "error" && m.status !== "quota" && m.status !== "missing_key");
      let activeMatch = apiModels.find(m => (m.id === savedPref || m.model === savedPref || m.raw_id === savedPref) && isAllowed(m) && isAvailable(m));
      if (!activeMatch) {
        activeMatch = apiModels.find(m => (m.id === savedPref || m.model === savedPref || m.raw_id === savedPref) && isAllowed(m))
          || apiModels.find(m => isAllowed(m) && m.is_default && m.status === "working")
          || apiModels.find(m => isAllowed(m) && m.status === "working")
          || apiModels.find(m => isAllowed(m) && m.is_default && m.has_key && isAvailable(m))
          || apiModels.find(m => isAllowed(m) && m.has_key && isAvailable(m))
          || apiModels.find(m => isAllowed(m))
          || apiModels[0];
      }
      aiModel = activeMatch ? activeMatch.id : "";
      if (aiModel) sel.value = aiModel;
      try {
        localStorage.setItem(AI_MODEL_STORAGE, aiModel);
      } catch (_e) {}

      populateBadgeModelDropdown();
    }

    async function probeAiModels() {
      await loadAiModels(true);
    }

    function toggleAiChatPane(forceOpen) {
      const pane = document.getElementById("ai-chat-pane");
      const edgeTab = document.getElementById("ai-chat-edge-tab");
      const aiNavBtn = document.getElementById("nav-ai-bar");
      if (!pane) return;
      const isCurrentlyCollapsed = pane.classList.contains("collapsed");
      const nextOpen = typeof forceOpen === "boolean" ? forceOpen : isCurrentlyCollapsed;
      pane.classList.toggle("collapsed", !nextOpen);
      if (edgeTab) edgeTab.classList.toggle("hidden", nextOpen);
      if (aiNavBtn) {
        aiNavBtn.classList.toggle("active", nextOpen);
        aiNavBtn.classList.toggle("selected", nextOpen);
        aiNavBtn.setAttribute("aria-expanded", nextOpen ? "true" : "false");
      }
      try {
        localStorage.setItem(AI_PANE_OPEN_STORAGE, nextOpen ? "1" : "0");
      } catch (_e) {}
      if (nextOpen && availableAiModels.length === 0) {
        loadAiModels(true);
      }
      if (nextOpen) {
        setTimeout(() => {
          const inp = document.getElementById("ai-chat-input");
          if (inp) inp.focus();
          renderActiveScreenCharts();
        }, 210);
      } else {
        setTimeout(renderActiveScreenCharts, 210);
      }
    }

    function renderActiveScreenCharts() {
      if (currentScreen === "models") {
        if (typeof renderModelsScreen === "function") renderModelsScreen();
      } else if (currentScreen === "google") {
        renderRelChartAndNav();
      } else if (currentScreen === "retirement") {
        renderSimChartAndNav();
      } else if (currentScreen === "income") {
        renderSsnChartAndNav();
      } else {
        renderChartAndNav();
      }
    }

    function onAiModelChange(val) {
      aiModel = val || "default";
      try {
        localStorage.setItem(AI_MODEL_STORAGE, aiModel);
      } catch (_e) {}
      renderAiModelUI();
    }

    function resetAiChatSession() {
      aiConversationId = null;
      aiMessages = [];
      const box = document.getElementById("ai-chat-messages");
      if (box) box.replaceChildren();
      const st = document.getElementById("ai-chat-status");
      if (st) st.textContent = "New session ready";
    }

    let activeAdvisorJob = null;
    let _dismissedAdvisorJobId = null;
    const _applyingAdvisorJobIds = new Set();
    let _lastAdvisorBannerSig = "";

    function dismissAdvisorJobBanner() {
      if (activeAdvisorJob && activeAdvisorJob.job_id) {
        _dismissedAdvisorJobId = activeAdvisorJob.job_id;
      }
      _lastAdvisorBannerSig = "";
      const banner = document.getElementById("ai-advisor-job-banner");
      if (banner) banner.classList.remove("open");
    }

    function updateAdvisorJobBanner(job) {
      activeAdvisorJob = job || null;
      const banner = document.getElementById("ai-advisor-job-banner");
      const isRunning = Boolean(job && job.status === "running");
      if (!banner) return;

      if (!job || (job.status !== "running" && job.job_id === _dismissedAdvisorJobId)) {
        banner.classList.remove("open");
        banner.innerHTML = "";
        _lastAdvisorBannerSig = "";
        return;
      }

      banner.classList.add("open");
      const elapsed = job.elapsed_sec != null ? `${Number(job.elapsed_sec).toFixed(1)}s` : "";
      const stepCnt = Number(job.step_count || 0);
      const isError = Boolean(job && job.status === "error");
      const lastStep = job.last_step || (isRunning ? "Running Advisor..." : "Completed");
      const progressPct = Math.max(0, Math.min(100, Math.round(Number(job.progress_pct != null ? job.progress_pct : (isRunning ? 8 : 100)))));
      const defaultStatus = isRunning ? "Phase 1/5: Auditing Positions & Tax Basis" : (isError ? "Execution Failed" : "Complete");
      const statusLabel = job.status_label || defaultStatus;
      const isApplying = Boolean((job.job_id && _applyingAdvisorJobIds.has(job.job_id)) || job.applying);
      const renderSig = JSON.stringify([
        job.job_id || "",
        job.status || "",
        job.title || "",
        progressPct,
        statusLabel,
        lastStep,
        job.error || "",
        job.proposal_label || "",
        isApplying,
      ]);

      if (renderSig === _lastAdvisorBannerSig && banner.innerHTML) {
        const elapEl = typeof banner.querySelector === "function" ? banner.querySelector("[data-advisor-elapsed]") : null;
        if (elapEl) {
          elapEl.textContent = isRunning
            ? `${elapsed}${stepCnt > 0 ? ` • ${stepCnt} steps` : ""}`
            : (elapsed ? `(${elapsed})` : "");
        }
        const existingApply = typeof banner.querySelector === "function" ? banner.querySelector("[data-advisor-apply]") : null;
        if (existingApply && job.proposal_label) {
          existingApply.disabled = isApplying;
          existingApply.textContent = isApplying ? "Applying..." : job.proposal_label;
        }
        return;
      }
      _lastAdvisorBannerSig = renderSig;

      if (isRunning) {
        banner.style.borderColor = "rgba(45, 212, 191, 0.5)";
        banner.style.background = "rgba(13, 148, 136, 0.16)";
        banner.innerHTML = `
          <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;">
            <span style="display:inline-flex;align-items:center;gap:6px;font-weight:700;color:#5eead4;">
              <span class="mc-spin"></span>
              <span>${escHtml(job.title || "AI Advisor Running")}</span>
            </span>
            <span class="mono muted" data-advisor-elapsed style="font-size:10px;">${escHtml(elapsed)}${stepCnt > 0 ? ` • ${stepCnt} steps` : ""}</span>
          </div>
          <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;margin-top:4px;font-size:10.5px;">
            <span style="color:#5eead4;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${escHtml(statusLabel)}</span>
            <span class="mono" style="font-weight:700;color:#2dd4bf;margin-left:auto;">${progressPct}%</span>
          </div>
          <div style="height:3px;background:rgba(255,255,255,0.12);border-radius:2px;overflow:hidden;margin-top:3px;">
            <div style="width:${progressPct}%;height:100%;background:#2dd4bf;border-radius:2px;transition:width 0.4s ease;"></div>
          </div>
          <div class="mono" style="font-size:10.4px;color:#cbd5e1;margin-top:4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">
            ${escHtml(lastStep)}
          </div>
          <div class="muted" style="font-size:10px;margin-top:2px;">Running asynchronously — browse Scenarios &amp; charts freely.</div>
        `;
      } else if (isError) {
        banner.style.borderColor = "rgba(244, 63, 94, 0.5)";
        banner.style.background = "rgba(136, 19, 55, 0.25)";
        banner.innerHTML = `
          <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;">
            <span style="font-weight:700;color:#fda4af;">&#10006; ${escHtml(job.title || "Advisor Error")}</span>
            <button type="button" class="cap-btn mono" style="padding:0 5px;font-size:10px;" onclick="dismissAdvisorJobBanner()">&#10005;</button>
          </div>
          <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;margin-top:4px;font-size:10.5px;">
            <span style="color:#fda4af;font-weight:600;">Status: Failed</span>
            <span class="mono" style="font-weight:700;color:#f43f5e;margin-left:auto;">${progressPct}%</span>
          </div>
          <div style="height:3px;background:rgba(255,255,255,0.12);border-radius:2px;overflow:hidden;margin-top:3px;">
            <div style="width:${progressPct}%;height:100%;background:#f43f5e;border-radius:2px;"></div>
          </div>
          <div class="mono" style="font-size:10.3px;color:#fecdd3;margin-top:4px;">${escHtml(job.error || lastStep)}</div>
        `;
      } else {
        banner.style.borderColor = "rgba(16, 185, 129, 0.5)";
        banner.style.background = "rgba(6, 78, 59, 0.25)";
        banner.innerHTML = `
          <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;">
            <span style="font-weight:700;color:#6ee7b7;">&#10003; ${escHtml(job.title || "Advisor Complete")} <span data-advisor-elapsed>${elapsed ? `(${escHtml(elapsed)})` : ""}</span></span>
            <button type="button" class="cap-btn mono" style="padding:0 5px;font-size:10px;" onclick="dismissAdvisorJobBanner()">&#10005;</button>
          </div>
          <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;margin-top:4px;font-size:10.5px;">
            <span style="color:#6ee7b7;font-weight:600;">Status: Complete</span>
            <span class="mono" style="font-weight:700;color:#10b981;margin-left:auto;">100%</span>
          </div>
          <div style="height:3px;background:rgba(255,255,255,0.12);border-radius:2px;overflow:hidden;margin-top:3px;">
            <div style="width:100%;height:100%;background:#10b981;border-radius:2px;"></div>
          </div>
          <div class="mono" style="font-size:10.3px;color:#a7f3d0;margin-top:4px;">${escHtml(lastStep)}</div>
          ${job.proposal_label ? `<button type="button" class="cap-btn" data-advisor-apply${isApplying ? " disabled" : ""}></button>` : ''}
        `;
        const apply = job.proposal_label && typeof banner.querySelector === "function" && banner.querySelector("[data-advisor-apply]");
        if (apply) {
          apply.disabled = isApplying;
          apply.textContent = isApplying ? "Applying..." : job.proposal_label;
          apply.onclick = async () => {
            if ((job.job_id && _applyingAdvisorJobIds.has(job.job_id)) || apply.disabled) return;
            if (job.job_id) _applyingAdvisorJobIds.add(job.job_id);
            apply.disabled = true;
            apply.textContent = "Applying...";
            _lastAdvisorBannerSig = JSON.stringify([
              job.job_id || "",
              job.status || "",
              job.title || "",
              progressPct,
              statusLabel,
              lastStep,
              job.error || "",
              job.proposal_label || "",
              true,
            ]);
            try {
              const { ok, data } = await apiJson("/api/advisor/apply", { body: { job_id: job.job_id } });
              if (job.job_id) _applyingAdvisorJobIds.delete(job.job_id);
              if (ok) {
                delete job.proposal_label;
                job.applying = false;
                _lastAdvisorBannerSig = "";
                updateAdvisorJobBanner(job);
                await refreshSavesFromServer();
              } else {
                job.applying = false;
                _lastAdvisorBannerSig = "";
                appendAiMessage("assistant", data?.error || "Could not apply the proposal. Your edits remain available.");
                updateAdvisorJobBanner(job);
              }
            } catch (_err) {
              if (job.job_id) _applyingAdvisorJobIds.delete(job.job_id);
              job.applying = false;
              _lastAdvisorBannerSig = "";
              appendAiMessage("assistant", "Could not apply the proposal. Your edits remain available.");
              updateAdvisorJobBanner(job);
            }
          };
        }

      }
    }

    async function runAdvisorWorkflow(promptType, promptTitle = null, promptBody = null) {
      toggleAiChatPane(true);
      _dismissedAdvisorJobId = null;
      const defaultTitle = promptType === "preferred_forecast"
        ? "Preferred Simulation Settings"
        : "Recommended Portfolio Improvements";
      const label = (promptTitle && String(promptTitle).trim()) || defaultTitle;
      appendAiMessage("user", `Run Advisor Workflow: ${label}`);
      updateAdvisorJobBanner({
        job_id: "pending",
        prompt_type: promptType,
        title: label,
        model: aiModel,
        status: "running",
        elapsed_sec: 0,
        step_count: 1,
        progress_pct: 8,
        status_label: "Phase 1/5: Auditing Positions & Tax Basis",
        last_step: `Starting ${aiModel} background Advisor...`,
      });
      try {
        const reqBody = {
          prompt_type: promptType,
          model: aiModel,
          conversation_id: aiConversationId,
          browser_state: {
            currentScreen,
            currentSaveName,
            currentModelId,
            isSaveDirty,
            settings: getCurrentSettingsSnapshot(),
          },
        };
        if (promptBody && String(promptBody).trim()) {
          reqBody.prompt = String(promptBody);
        }
        const { ok, status, data } = await apiJson("/api/advisor/run", { body: reqBody });
        if (!ok || !data || !data.ok) {
          const errMsg = (data && data.error) || `HTTP ${status}`;
          updateAdvisorJobBanner({
            job_id: "err",
            title: label,
            status: "error",
            error: errMsg,
          });
          appendAiMessage("assistant", `**Advisor Launch Error:** ${errMsg}`);
          return;
        }
        if (data.job) {
          updateAdvisorJobBanner(data.job);
        }
      } catch (err) {
        updateAdvisorJobBanner({
          job_id: "offline",
          title: label,
          status: "error",
          error: "AdvisorBot Service is currently offline.",
        });
        appendAiMessage("assistant", "**Service Temporarily Offline:** Could not communicate with the local service. Reconnecting automatically...");
      }
    }

    function formatAiMarkdown(raw) {
      const text = String(raw || "");
      const lines = text.split("\n");
      let html = "";
      let inCode = false;
      let codeBuf = [];
      let inList = false;
      let inTable = false;
      let tableRows = [];

      const flushList = () => {
        if (inList) {
          html += "</ul>";
          inList = false;
        }
      };

      const flushTable = () => {
        if (!inTable || tableRows.length === 0) return;
        html += "<table>";
        tableRows.forEach((r, idx) => {
          const cells = r.split("|").slice(1, -1).map(c => c.trim());
          if (idx === 1 && cells.every(c => /^[:\-]+$/.test(c))) return;
          const tag = idx === 0 ? "th" : "td";
          html += "<tr>" + cells.map(c => `<${tag}>${formatInlineMd(c)}</${tag}>`).join("") + "</tr>";
        });
        html += "</table>";
        inTable = false;
        tableRows = [];
      };

      const formatInlineMd = s => {
        let out = escHtml(s);
        out = out.replace(/`([^`]+)`/g, "<code>$1</code>");
        out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
        return out;
      };

      for (let i = 0; i < lines.length; i++) {
        const line = lines[i];
        if (line.trim().startsWith("```")) {
          flushList();
          flushTable();
          if (!inCode) {
            inCode = true;
            codeBuf = [];
          } else {
            html += `<pre><code>${escHtml(codeBuf.join("\n"))}</code></pre>`;
            inCode = false;
          }
          continue;
        }
        if (inCode) {
          codeBuf.push(line);
          continue;
        }
        if (line.trim().startsWith("|") && line.trim().endsWith("|")) {
          flushList();
          inTable = true;
          tableRows.push(line.trim());
          continue;
        } else if (inTable) {
          flushTable();
        }
        const trimmed = line.trim();
        if (!trimmed) {
          flushList();
          continue;
        }
        if (/^[-*]\s+/.test(trimmed)) {
          if (!inList) {
            html += "<ul>";
            inList = true;
          }
          html += `<li>${formatInlineMd(trimmed.replace(/^[-*]\s+/, ""))}</li>`;
        } else if (/^#{1,4}\s+/.test(trimmed)) {
          flushList();
          html += `<p><strong>${formatInlineMd(trimmed.replace(/^#{1,4}\s+/, ""))}</strong></p>`;
        } else {
          flushList();
          html += `<p>${formatInlineMd(trimmed)}</p>`;
        }
      }
      flushList();
      flushTable();
      if (inCode && codeBuf.length > 0) {
        html += `<pre><code>${escHtml(codeBuf.join("\n"))}</code></pre>`;
      }
      return html || `<p>${escHtml(text)}</p>`;
    }

    let _aiPendingBubbleEl = null;
    let _aiPendingTimer = null;
    let _aiPendingStartTs = 0;

    function createPendingAiBubble(modelId) {
      removePendingAiBubble();
      const box = document.getElementById("ai-chat-messages");
      if (!box) return null;

      _aiPendingStartTs = Date.now();
      const wrap = document.createElement("div");
      wrap.id = "ai-pending-msg-wrap";
      wrap.className = "ai-msg assistant";

      const matched = (availableAiModels || []).find(m => m.id === modelId || m.model === modelId);
      const modelLabel = (matched && (matched.display_name || matched.label || matched.name)) || modelId || "Agent";

      wrap.innerHTML = `
        <div class="ai-msg-bubble ai-thinking-bubble">
          <div class="ai-thinking-header">
            <div class="ai-thinking-spinner"></div>
            <span>Combobulating...</span>
            <span class="ai-thinking-timer mono" id="ai-pending-timer">0s</span>
          </div>
          <div class="ai-thinking-shimmer">
            <div class="ai-thinking-shimmer-bar"></div>
          </div>
          <div class="ai-thinking-status mono" id="ai-pending-status"><span style="color:#38bdf8;font-size:11px;">&#9881;</span> <span class="status-text">Consulting ${escHtml(modelLabel)} &bull; Analyzing prompt...</span></div>
        </div>
      `;
      box.appendChild(wrap);
      box.scrollTop = box.scrollHeight;
      _aiPendingBubbleEl = wrap;

      _aiPendingTimer = setInterval(() => {
        const timerEl = document.getElementById("ai-pending-timer");
        if (timerEl && _aiPendingStartTs > 0) {
          const sec = Math.floor((Date.now() - _aiPendingStartTs) / 1000);
          timerEl.textContent = `${sec}s`;
        }
      }, 500);

      return wrap;
    }

    function updatePendingAiStatus(text, label) {
      const statusEl = document.getElementById("ai-pending-status");
      if (statusEl) {
        let cleanText = String(text || "").trim();
        let cleanLabel = String(label || "").trim();
        let display = "";
        if (cleanLabel && cleanText && cleanLabel !== cleanText) {
          display = `${cleanLabel} • ${cleanText}`;
        } else {
          display = cleanText || cleanLabel || "Working...";
        }
        statusEl.innerHTML = `<span style="color:#38bdf8;font-size:11px;">&#9881;</span> <span class="status-text">${escHtml(display)}</span>`;
        const box = document.getElementById("ai-chat-messages");
        if (box) box.scrollTop = box.scrollHeight;
      }
    }

    function removePendingAiBubble() {
      if (_aiPendingTimer) {
        clearInterval(_aiPendingTimer);
        _aiPendingTimer = null;
      }
      _aiPendingStartTs = 0;
      const el = document.getElementById("ai-pending-msg-wrap");
      if (el) {
        if (typeof el.remove === "function") {
          el.remove();
        } else if (el.parentNode && typeof el.parentNode.removeChild === "function") {
          el.parentNode.removeChild(el);
        }
      }
      _aiPendingBubbleEl = null;
    }

    function _activeAiShieldMode() {
      return String(
        (_privacyConfig && _privacyConfig.ai_shield && _privacyConfig.ai_shield.mode) ||
        (PAYLOAD && PAYLOAD.privacy_status && PAYLOAD.privacy_status.shield_mode) ||
        "identity_aliased"
      );
    }

    function appendAiMessage(role, content, toolCalls = [], uiEvents = [], meta = null) {
      if (role === "assistant") {
        removePendingAiBubble();
      }
      const box = document.getElementById("ai-chat-messages");
      if (!box) return;
      const wrap = document.createElement("div");
      wrap.className = `ai-msg ${role}`;

      let pillsHtml = "";
      const hasTools = Array.isArray(toolCalls) && toolCalls.length > 0;
      const hasUi = Array.isArray(uiEvents) && uiEvents.length > 0;
      if (hasTools || hasUi) {
        const totalCount = (hasTools ? toolCalls.length : 0) + (hasUi ? uiEvents.length : 0);
        let itemsHtml = "";
        if (hasTools) {
          itemsHtml += `<div class="ai-tool-pills">` + toolCalls.map(tc => {
            const label = tc.summary || tc.name || tc.tool || "tool";
            return `<span class="ai-tool-pill mono" title="Tool Executed">&#128736; ${escHtml(label)}</span>`;
          }).join("") + `</div>`;
        }
        if (hasUi) {
          itemsHtml += `<div class="ai-tool-pills" style="margin-top:4px;">` + uiEvents.map(ev => {
            const act = (ev && ev.action) ? ev.action : ev;
            return `<span class="ai-ui-pill mono" title="Realtime UI Action Applied">&#9889; UI: ${escHtml((act && act.type) || "sync")}</span>`;
          }).join("") + `</div>`;
        }
        pillsHtml = `
          <details class="ai-tool-details">
            <summary class="mono">&#128736; ${totalCount} tool call${totalCount === 1 ? '' : 's'} executed ▾</summary>
            ${itemsHtml}
          </details>
        `;
      }

      const shieldMode = (meta && meta.privacy_shield) ? String(meta.privacy_shield) : _activeAiShieldMode();
      const demoActiveNow = Boolean((meta && meta.demo_mode) || isDemoModeActive());
      const modeCMetaHtml = (!demoActiveNow && role === "assistant" && shieldMode === "identity_and_values")
        ? `<div class="ai-reply-shield-meta mono" title="Mode C Privacy Shield active: dollar figures in chat prose are obfuscated while statutory tax constants remain exact.">&#128737; Mode C &bull; Dollar figures obfuscated by Privacy Shield (statutory tax constants exact)</div>`
        : "";

      const bodyHtml = role === "user"
        ? `<div class="ai-msg-bubble"><p>${escHtml(content)}</p></div>`
        : `${pillsHtml}<div class="ai-msg-bubble">${formatAiMarkdown(content)}</div>${modeCMetaHtml}`;
      wrap.innerHTML = bodyHtml;
      box.appendChild(wrap);
      box.scrollTop = box.scrollHeight;
    }

    function sendQuickAiPrompt(text) {
      const inp = document.getElementById("ai-chat-input");
      if (inp) inp.value = text;
      sendAiChatMessage();
    }

    function _getActiveSavedPrompts() {
      const hasActive = Boolean(_portfoliosBundle && _portfoliosBundle.has_active && _portfoliosBundle.active_portfolio_id);
      if (!hasActive) return [];
      return Array.isArray(_activeSavedPrompts) ? _activeSavedPrompts : [];
    }

    function renderAiPromptsDropdown() {
      const sel = document.getElementById("ai-prompts-select");
      if (!sel) return;
      const prompts = _getActiveSavedPrompts();
      let html = `<option value="">Run a Saved Prompt...</option>`;
      if (prompts.length === 0) {
        html += `<option value="__create_saved_prompt__">Create Saved Prompt</option>`;
      } else {
        prompts.forEach((p, idx) => {
          const title = (p && p.title ? String(p.title) : "Untitled Prompt").trim() || "Untitled Prompt";
          html += `<option value="${idx}">${escHtml(title)}</option>`;
        });
      }
      sel.innerHTML = html;
      sel.value = "";
    }

    function onAiPromptDropdownChange(sel) {
      const val = sel ? String(sel.value || "") : "";
      if (sel) sel.value = "";
      if (!val) return;
      if (val === "__create_saved_prompt__") {
        openProjectSettingsDialog("prompts");
        return;
      }
      const idx = parseInt(val, 10);
      const prompts = _getActiveSavedPrompts();
      const item = (!isNaN(idx) && idx >= 0 && idx < prompts.length) ? prompts[idx] : null;
      if (!item) return;
      const pid = String(item.id || "").trim();
      const title = String(item.title || "Saved Prompt").trim() || "Saved Prompt";
      const body = String(item.prompt || "").trim();
      if (pid === "preferred_forecast" || pid === "portfolio_improvements") {
        runAdvisorWorkflow(pid, title, body || null);
      } else if (body) {
        toggleAiChatPane(true);
        sendQuickAiPrompt(body);
      } else {
        openProjectSettingsDialog("prompts");
      }
    }

    function onAiChatInputKeyDown(evt) {
      if (evt.key === "Enter" && !evt.shiftKey) {
        evt.preventDefault();
        sendAiChatMessage();
      }
    }

    async function sendAiChatMessage() {
      if (aiIsSending) return;
      const inp = document.getElementById("ai-chat-input");
      const btn = document.getElementById("ai-chat-send-btn");
      const statusEl = document.getElementById("ai-chat-status");
      if (!inp) return;
      const text = inp.value.trim();
      if (!text) return;

      inp.value = "";
      appendAiMessage("user", text);
      aiMessages.push({ role: "user", content: text });

      aiIsSending = true;
      if (btn) {
        btn.disabled = true;
        btn.textContent = "Thinking...";
      }
      if (statusEl) {
        statusEl.textContent = "Analyzing portfolio scenario...";
      }

      createPendingAiBubble(aiModel);
      pollLiveUiEvents();
      const fastPollTimer = setInterval(pollLiveUiEvents, 300);

      try {
        const { ok, status, data } = await apiJson("/api/chat", {
          body: {
            model: aiModel,
            conversation_id: aiConversationId,
            prompt: text,
            messages: aiMessages,
            browser_state: {
              currentScreen,
              currentSaveName,
              isSaveDirty,
              settings: getCurrentSettingsSnapshot(),
            },
          },
        });
        removePendingAiBubble();
        if (!ok || !data || !data.ok) {
          if (data && data.privacy_shield) {
            updatePrivacyChromeBadges({
              demo_enabled: Boolean((data && data.demo_mode) || isDemoModeActive()),
              shield_mode: data.privacy_shield,
            });
          }
          let errMsg = (data && data.error) || `HTTP ${status}`;
          if (status === 403 || errMsg.includes("missing session token")) {
            errMsg = "AdvisorBot Service is currently re-authenticating. Please try your prompt again.";
          }
          appendAiMessage("assistant", `**Notice:** ${errMsg}`);
          if (statusEl) statusEl.textContent = "Service temporarily unavailable";
        } else {
          if (data.privacy_shield) {
            updatePrivacyChromeBadges({
              demo_enabled: Boolean(data.demo_mode || isDemoModeActive()),
              shield_mode: data.privacy_shield,
            });
          }
          if (data.conversation_id) {
            aiConversationId = data.conversation_id;
          }
          const reply = data.reply || "(No response)";
          const tCalls = data.tool_calls || [];
          const uEvents = data.ui_events || [];
          for (const ev of uEvents) {
            await applyLiveUiEvent(ev);
          }
          appendAiMessage("assistant", reply, tCalls, uEvents, {
            privacy_shield: data.privacy_shield,
            demo_mode: Boolean(data.demo_mode || isDemoModeActive()),
          });
          aiMessages.push({ role: "assistant", content: reply });
          if (statusEl) {
            statusEl.textContent = data.conversation_id
              ? "Advisor session ready • Enter to send"
              : "Ready • Enter to send";
          }
        }
      } catch (err) {
        removePendingAiBubble();
        appendAiMessage(
          "assistant",
          `**AdvisorBot Service Temporarily Offline:** Could not communicate with the local service. The service is attempting to reconnect automatically.`
        );
        if (statusEl) statusEl.textContent = "Service reconnecting...";
      } finally {
        if (fastPollTimer) clearInterval(fastPollTimer);
        removePendingAiBubble();
        aiIsSending = false;
        if (btn) {
          btn.disabled = false;
          btn.textContent = "Send →";
        }
      }
    }

    function _mergeSettingsPatch(cur, patch) {
      const merged = Object.assign({}, cur, patch);
      const nestedMapKeys = ["holdingForecasts", "holdingHalfLives", "holdingFeePct", "simMemberRetirementAges"];
      for (const mk of nestedMapKeys) {
        if (patch && Object.prototype.hasOwnProperty.call(patch, mk) && patch[mk] && typeof patch[mk] === "object" && !Array.isArray(patch[mk])) {
          const baseMap = (cur && cur[mk] && typeof cur[mk] === "object" && !Array.isArray(cur[mk]))
            ? cur[mk]
            : (mk === "holdingFeePct" && typeof holdingFeePct === "object" && holdingFeePct ? holdingFeePct : {});
          const nextMap = Object.assign({}, baseMap);
          Object.entries(patch[mk]).forEach(([k, v]) => {
            if (v === null) {
              delete nextMap[k];
            } else if (mk === "holdingForecasts" && v && typeof v === "object" && baseMap[k] && typeof baseMap[k] === "object") {
              nextMap[k] = Object.assign({}, baseMap[k], v);
            } else {
              nextMap[k] = v;
            }
          });
          merged[mk] = nextMap;
        }
      }
      return merged;
    }

    async function applyLiveUiEvent(ev) {
      if (!ev || typeof ev !== "object") return;
      if (ev.seq && ev.seq > lastUiEventSeq) {
        lastUiEventSeq = ev.seq;
        try { sessionStorage.setItem("portfolio_last_ui_seq", String(lastUiEventSeq)); } catch (_e) {}
      }
      const act = (ev.action && typeof ev.action === "object") ? ev.action : ev;
      const evType = act.type || "";
      if (evType === "chat_tool_step") {
        updatePendingAiStatus(act.summary, act.status_label);
      } else if (evType === "saves_updated" || evType === "sync_saves" || evType === "models_updated") {
        await refreshSavesFromServer();
        const loadTarget = act.load_save_name || act.name || act.id;
        if (loadTarget && !isSaveDirty) {
          const matched = findModelEntry(loadTarget);
          if (matched && matched.settings) {
            currentModelId = matched.id || currentModelId;
            currentSaveName = matched.name;
            applySettingsSnapshot(matched.settings);
            updateSaveStatusUI(false);
          }
        }
        renderSaveDialogList();
        if (typeof renderModelsScreen === "function" && !(selectedModelDetailId && isEditingDetailNotes)) {
          renderModelsScreen();
        }
      } else if ((evType === "load_save" || evType === "load_model") && (act.save_name || act.name || act.id)) {
        await refreshSavesFromServer();
        if (!isSaveDirty) {
          const wanted = String(act.id || act.save_name || act.name);
          const matched = findModelEntry(wanted);
          if (matched && matched.settings) {
            currentModelId = matched.id || currentModelId;
            currentSaveName = matched.name;
            applySettingsSnapshot(matched.settings);
            updateSaveStatusUI(false);
            renderSaveDialogList();
            if (typeof renderModelsScreen === "function" && !(selectedModelDetailId && isEditingDetailNotes)) {
              renderModelsScreen();
            }
          } else if (act.settings && typeof act.settings === "object") {
            currentSaveName = act.name || act.save_name;
            applySettingsSnapshot(act.settings);
            updateSaveStatusUI(false);
          }
        }
      } else if (evType === "advisor_job_started") {
        _dismissedAdvisorJobId = null;
        updateAdvisorJobBanner({
          job_id: act.job_id || "running",
          prompt_type: act.prompt_type,
          title: act.title || "AI Advisor Running",
          model: act.model || aiModel,
          status: "running",
          elapsed_sec: 0,
          step_count: 1,
          progress_pct: act.progress_pct != null ? Number(act.progress_pct) : 8,
          status_label: act.status_label || "Phase 1/5: Auditing Positions & Tax Basis",
          last_step: `Started ${act.title || "Advisor"} (${act.model || aiModel})`,
        });
      } else if (evType === "advisor_tool_step" || evType === "advisor_job_step") {
        if (activeAdvisorJob && activeAdvisorJob.status === "running") {
          activeAdvisorJob.step_count = act.step_count != null ? Number(act.step_count) : (Number(activeAdvisorJob.step_count || 0) + 1);
          activeAdvisorJob.last_step = act.last_step || act.summary || act.tool || "Executing tool...";
          if (act.progress_pct != null) activeAdvisorJob.progress_pct = Number(act.progress_pct);
          if (act.status_label) activeAdvisorJob.status_label = act.status_label;
          updateAdvisorJobBanner(activeAdvisorJob);
        }
        updatePendingAiStatus(act.last_step || act.summary, act.status_label);
      } else if (evType === "advisor_job_done") {
        if (act.conversation_id) aiConversationId = act.conversation_id;
        if (act.ok) {
          updateAdvisorJobBanner({
            job_id: act.job_id || "done",
            title: act.title || "Advisor Run Complete",
            status: "completed",
            progress_pct: 100,
            status_label: "Complete",
            elapsed_sec: activeAdvisorJob ? activeAdvisorJob.elapsed_sec : 0,
            last_step: "Analysis ready",
            proposal_label: act.proposal_label,
          });
          if (act.reply) {
            appendAiMessage("assistant", act.reply);
          }
        } else {
          updateAdvisorJobBanner({
            job_id: act.job_id || "err",
            title: act.title || "Advisor Error",
            status: "error",
            progress_pct: act.progress_pct || (activeAdvisorJob ? activeAdvisorJob.progress_pct : 0),
            status_label: "Execution Failed",
            error: act.error || "Advisor run failed.",
            last_step: act.error || "Advisor run failed.",
          });
          appendAiMessage("assistant", `**Advisor Error:** ${act.error || "Advisor run failed."}`);
        }
      } else if (evType === "control_ui") {
        if (act.settings_patch && typeof act.settings_patch === "object" && Object.keys(act.settings_patch).length > 0) {
          const diskBaseline = loadedSettingsSnapshot;
          const cur = getCurrentSettingsSnapshot();
          const merged = _mergeSettingsPatch(cur, act.settings_patch);
          const feeChanged = Boolean(act.settings_patch.holdingFeePct && typeof act.settings_patch.holdingFeePct === "object");
          if (!snapshotsEqual(cur, merged) || feeChanged) {
            applySettingsSnapshot(merged);
            loadedSettingsSnapshot = diskBaseline;
            isSaveDirty = !snapshotsEqual(getCurrentSettingsSnapshot(), loadedSettingsSnapshot);
            updateSaveStatusUI(false);
          }
        }
        if (act.reoptimize_order && typeof optimizeSimAssetOrder === "function") {
          optimizeSimAssetOrder();
        }
        if (act.view_patch && typeof act.view_patch === "object") {
          if (act.view_patch.simDollarMode && typeof setSimDollarMode === "function") {
            setSimDollarMode(act.view_patch.simDollarMode);
          }
          if (act.view_patch.simScaleMode && typeof setSimScaleMode === "function") {
            setSimScaleMode(act.view_patch.simScaleMode);
          }
          if ((act.view_patch.simActiveModel || act.view_patch.simModel) && typeof setSimModel === "function") {
            setSimModel(act.view_patch.simActiveModel || act.view_patch.simModel);
          }
          if (act.view_patch.simPreset && typeof applySimPreset === "function") {
            applySimPreset(act.view_patch.simPreset);
          }
          if (act.view_patch.stackMode && typeof setStackMode === "function") {
            setStackMode(act.view_patch.stackMode);
          }
        }
        // CROSS-FILE INVARIANT (`DA2-07b` — coupled with `llm_control_live_ui` in `Scripts/llm_tools.py`):
        // Accept every tab/screen alias emitted by MCP & in-app AI tools (`scenarios`, `main`, `grants`, `primary`, `settings`, `cfg`, `primary_quote`).
        if (act.switch_screen) {
          const t = String(act.switch_screen).toLowerCase();
          if (t === "models" || t === "saves" || t === "scenarios") switchScreen("models");
          else if (t === "sim" || t === "retirement") switchScreen("retirement");
          else if (t === "gsu" || t === "google" || t === "grants" || t === "primary" || t === "vesting" || t === "equity") switchScreen("google");
          else if (t === "income" || t === "ssn") switchScreen("income");
          else if (t === "settings" || t === "cfg") switchScreen("settings");
          else switchScreen("portfolio");
        }
        if (act.refresh_action === "goog_quote" || act.refresh_action === "primary_quote") {
          fetchLiveGoogQuote(true);
        } else if (act.refresh_action === "reload_page") {
          if (isSaveDirty) return;
          let lastR = 0;
          try { lastR = parseInt(sessionStorage.getItem("portfolio_last_reload_ts") || "0", 10) || 0; } catch (_e) {}
          const now = Date.now();
          if (now - lastR < 15000) {
            console.warn("Suppressing rapid reload loop from live UI event");
            return;
          }
          try { sessionStorage.setItem("portfolio_last_reload_ts", String(now)); } catch (_e) {}
          window.location.reload();
        } else if (act.refresh_action === "rebuild_site") {
          triggerSiteRebuild();
        }
      } else if (evType === "tune_settings" && act.settings && typeof act.settings === "object") {
        const diskBaseline = loadedSettingsSnapshot;
        const cur = getCurrentSettingsSnapshot();
        const merged = _mergeSettingsPatch(cur, act.settings);
        const feeChanged = Boolean(act.settings.holdingFeePct && typeof act.settings.holdingFeePct === "object");
        if (!snapshotsEqual(cur, merged) || feeChanged) {
          applySettingsSnapshot(merged);
          loadedSettingsSnapshot = diskBaseline;
          isSaveDirty = !snapshotsEqual(getCurrentSettingsSnapshot(), loadedSettingsSnapshot);
          updateSaveStatusUI(false);
        }
      } else if (evType === "switch_tab" && act.tab) {
        const t = String(act.tab).toLowerCase();
        if (t === "models" || t === "saves" || t === "scenarios") switchScreen("models");
        else if (t === "sim" || t === "retirement") switchScreen("retirement");
        else if (t === "gsu" || t === "google" || t === "grants" || t === "primary" || t === "vesting" || t === "equity") switchScreen("google");
        else if (t === "income" || t === "ssn") switchScreen("income");
        else if (t === "settings" || t === "cfg") switchScreen("settings");
        else switchScreen("portfolio");
      } else if (evType === "refresh_goog_quote" || evType === "refresh_primary_quote") {
        fetchLiveGoogQuote(true);
      } else if (evType === "reload_page") {
        if (isSaveDirty) return;
        let lastR = 0;
        try { lastR = parseInt(sessionStorage.getItem("portfolio_last_reload_ts") || "0", 10) || 0; } catch (_e) {}
        const now = Date.now();
        if (now - lastR < 15000) {
          console.warn("Suppressing rapid reload loop from live UI event");
          return;
        }
        try { sessionStorage.setItem("portfolio_last_reload_ts", String(now)); } catch (_e) {}
        window.location.reload();
      }
    }

    async function pollLiveUiEvents() {
      try {
        const { ok, data } = await apiJson("/api/ui/poll", {
          body: {
            since: lastUiEventSeq,
            browser_state: {
              currentScreen,
              currentSaveName,
              currentModelId,
              isSaveDirty,
              settings: getCurrentSettingsSnapshot(),
            },
          },
        });
        if (!ok || !data) return;
        if (data && data.advisor_job) {
          updateAdvisorJobBanner(data.advisor_job);
        }
        if (data && Array.isArray(data.events) && data.events.length > 0) {
          for (const ev of data.events) {
            await applyLiveUiEvent(ev);
          }
        }
        if (data && typeof data.latest_seq === "number") {
          lastUiEventSeq = data.latest_seq;
          try { sessionStorage.setItem("portfolio_last_ui_seq", String(lastUiEventSeq)); } catch (_e) {}
        }
      } catch (_e) {
        // Silently retry on next scheduled interval
      }
    }

    function initAiChatAssistant() {
      try {
        loadAiModels(true);
        const savedOpen = localStorage.getItem(AI_PANE_OPEN_STORAGE);
        if (savedOpen === "1") {
          toggleAiChatPane(true);
        }
      } catch (_e) {}

      pollLiveUiEvents();
      if (uiPollTimer) clearInterval(uiPollTimer);
      uiPollTimer = setInterval(pollLiveUiEvents, 1500);
    }

    // =========================================================================
    // PORTFOLIO & SETTINGS MODAL
    // =========================================================================
    let _projDraft = null;
    let _draggedSymbolIdx = null;
    let _selectedPromptIdx = 0;
    let _defaultAdvisorPrompts = Object.assign(
      {},
      (PAYLOAD.advisor_prompts && PAYLOAD.advisor_prompts.defaults) || PAYLOAD.advisor_prompts || {}
    );
    let _resolvedAdvisorPrompts = Object.assign(
      {},
      (PAYLOAD.advisor_prompts && PAYLOAD.advisor_prompts.effective) || PAYLOAD.advisor_prompts || {}
    );
    let _activeSavedPrompts = Array.isArray(PAYLOAD.saved_prompts)
      ? JSON.parse(JSON.stringify(PAYLOAD.saved_prompts))
      : (Array.isArray(PROJECT_CFG.saved_prompts) ? JSON.parse(JSON.stringify(PROJECT_CFG.saved_prompts)) : []);
    let _portfoliosBundle = Object.assign(
      {
        active_portfolio_id: PAYLOAD.active_portfolio_id || null,
        has_active: Boolean(PAYLOAD.has_portfolio),
        portfolios: [],
      },
      PAYLOAD.portfolios_bundle || {}
    );
    let _selectedPortfolioId = _portfoliosBundle.active_portfolio_id || null;
    let _editingPortfolioId = _portfoliosBundle.active_portfolio_id || null;
    let _loadedDraftPortfolioId = _portfoliosBundle.active_portfolio_id || null;
    let _projNeedsReloadOnClose = false;
    let _projAutoSaveTimer = null;
    let _projAutoSavePromise = null;
    let _projLastSavedPayload = "";
    let _deleteTargetPortfolioId = null;

    function _getActivePortfolioEntry() {
      const aid = _portfoliosBundle && _portfoliosBundle.active_portfolio_id;
      if (!aid || !Array.isArray(_portfoliosBundle.portfolios)) return null;
      return _portfoliosBundle.portfolios.find(p => p.id === aid) || null;
    }

    function updateProjectSettingsHeaderUI() {
      const activeObj = _getActivePortfolioEntry();
      const hasActive = Boolean(activeObj);
      const titleText = hasActive ? (activeObj.name || "Portfolio Settings") : "Portfolio Settings";
      const pathText = hasActive ? "Portfolio Settings • Active" : "Portfolio Settings";

      const titleEl = document.getElementById("proj-dlg-title-text");
      if (titleEl) titleEl.textContent = titleText;
      const pillEl = document.getElementById("proj-dlg-path-pill");
      if (pillEl) pillEl.textContent = pathText;

      const navLbl = document.getElementById("nav-settings-lbl");
      const navSub = document.getElementById("nav-settings-sub");
      const navBadge = document.getElementById("nav-household-badge");
      const navBtnLabel = hasActive ? (activeObj.name || "Portfolio Settings") : "Portfolio Settings";
      if (navLbl) {
        navLbl.textContent = navBtnLabel;
        navLbl.title = hasActive
          ? `${navBtnLabel} • Manage Portfolios, Investors, Beneficiaries, Symbols, and Prompts`
          : "Portfolio Settings • Create or select a Portfolio";
      }
      if (navSub) {
        navSub.textContent = hasActive ? "Portfolio Settings" : "Select or create portfolio";
      }
      if (navBadge && hasActive && activeObj.badge_image) {
        navBadge.alt = activeObj.name || "Portfolio Settings";
        navBadge.title = `${activeObj.name} • Click to open Portfolio Settings`;
      } else if (navBadge && !hasActive) {
        navBadge.alt = "Portfolio Settings";
        navBadge.title = "Portfolio Settings • Click to create or select a Portfolio";
      }
    }

    function _setProjSettingsMsg(text, color = "#94a3b8") {
      const msgEl = document.getElementById("proj-settings-msg");
      if (!msgEl) return;
      msgEl.textContent = text;
      msgEl.style.color = color;
    }

    function switchProjectSettingsTab(tabName, syncDomFirst = true) {
      if (syncDomFirst) _syncProjDraftFromDOM();
      const validTabs = ["info", "family", "investors", "beneficiaries", "symbols", "prompts", "privacy"];
      let target = validTabs.includes(tabName) ? tabName : "info";
      if (target === "investors" || target === "beneficiaries") {
        target = "family";
      }
      validTabs.forEach(t => {
        const btn = document.getElementById(`proj-tab-btn-${t}`);
        const pane = document.getElementById(`proj-pane-${t}`);
        const isActive = (t === target) || ((target === "family") && (t === "family" || t === "investors" || t === "beneficiaries"));
        if (btn) btn.classList.toggle("active", isActive);
        if (pane) pane.classList.toggle("active", isActive);
      });

      _updateSelectPortfolioButtonState(target);

      if (target === "info") {
        renderPortfoliosTab();
      } else if (target === "family") {
        renderProjectFamilyMembers();
      } else if (target === "symbols") {
        renderProjectSymbolsList();
      } else if (target === "prompts") {
        renderProjectPromptsPane();
      } else if (target === "privacy") {
        loadAndRenderPrivacyTab();
      }
    }

    const US_STATE_TAX_PRESETS = {
      FED: { label: "Federal Only (0% State)", rate: 0 },
      AL: { label: "Alabama (5.0%)", rate: 0.05 },
      AK: { label: "Alaska (0%)", rate: 0 },
      AZ: { label: "Arizona (2.5%)", rate: 0.025 },
      AR: { label: "Arkansas (3.9%)", rate: 0.039 },
      CA: { label: "California (13.3%)", rate: 0.133 },
      CO: { label: "Colorado (4.4%)", rate: 0.044 },
      CT: { label: "Connecticut (6.99%)", rate: 0.0699 },
      DE: { label: "Delaware (6.6%)", rate: 0.066 },
      DC: { label: "District of Columbia (10.75%)", rate: 0.1075 },
      FL: { label: "Florida (0%)", rate: 0 },
      GA: { label: "Georgia (5.39%)", rate: 0.0539 },
      HI: { label: "Hawaii (11.0%)", rate: 0.11 },
      ID: { label: "Idaho (5.695%)", rate: 0.05695 },
      IL: { label: "Illinois (4.95%)", rate: 0.0495 },
      IN: { label: "Indiana (3.0%)", rate: 0.03 },
      IA: { label: "Iowa (3.8%)", rate: 0.038 },
      KS: { label: "Kansas (5.58%)", rate: 0.0558 },
      KY: { label: "Kentucky (4.0%)", rate: 0.04 },
      LA: { label: "Louisiana (3.0%)", rate: 0.03 },
      ME: { label: "Maine (7.15%)", rate: 0.0715 },
      MD: { label: "Maryland (5.75%)", rate: 0.0575 },
      MA: { label: "Massachusetts (9.0%)", rate: 0.09 },
      MI: { label: "Michigan (4.25%)", rate: 0.0425 },
      MN: { label: "Minnesota (9.85%)", rate: 0.0985 },
      MS: { label: "Mississippi (4.4%)", rate: 0.044 },
      MO: { label: "Missouri (4.7%)", rate: 0.047 },
      MT: { label: "Montana (5.9%)", rate: 0.059 },
      NE: { label: "Nebraska (5.2%)", rate: 0.052 },
      NV: { label: "Nevada (0%)", rate: 0 },
      NH: { label: "New Hampshire (0%)", rate: 0 },
      NJ: { label: "New Jersey (10.75%)", rate: 0.1075 },
      NM: { label: "New Mexico (5.9%)", rate: 0.059 },
      NY: { label: "New York (10.9%)", rate: 0.109 },
      NC: { label: "North Carolina (4.25%)", rate: 0.0425 },
      ND: { label: "North Dakota (2.5%)", rate: 0.025 },
      OH: { label: "Ohio (3.5%)", rate: 0.035 },
      OK: { label: "Oklahoma (4.75%)", rate: 0.0475 },
      OR: { label: "Oregon (9.9%)", rate: 0.099 },
      PA: { label: "Pennsylvania (3.07%)", rate: 0.0307 },
      RI: { label: "Rhode Island (5.99%)", rate: 0.0599 },
      SC: { label: "South Carolina (6.2%)", rate: 0.062 },
      SD: { label: "South Dakota (0%)", rate: 0 },
      TN: { label: "Tennessee (0%)", rate: 0 },
      TX: { label: "Texas (0%)", rate: 0 },
      UT: { label: "Utah (4.55%)", rate: 0.0455 },
      VT: { label: "Vermont (8.75%)", rate: 0.0875 },
      VA: { label: "Virginia (5.75%)", rate: 0.0575 },
      WA: { label: "Washington (0%)", rate: 0 },
      WV: { label: "West Virginia (4.82%)", rate: 0.0482 },
      WI: { label: "Wisconsin (7.65%)", rate: 0.0765 },
      WY: { label: "Wyoming (0%)", rate: 0 },
    };
    let _serverStateTaxPresets = null;
    let _projValidation = null;

    function _ingestServerStateTaxPresets(rawPresets) {
      if (!rawPresets) return;
      const out = {};
      if (Array.isArray(rawPresets)) {
        rawPresets.forEach(item => {
          if (!item || typeof item !== "object") return;
          const code = String(item.code || item.state_code || "").trim().toUpperCase();
          if (!code) return;
          const rate = Number(item.rate ?? item.state_rate ?? 0) || 0;
          const label = String(item.label || item.name || `${code} (${(rate * 100).toFixed(2).replace(/\.?0+$/, "")}%)`);
          out[code] = { label, rate };
        });
      } else if (typeof rawPresets === "object") {
        Object.entries(rawPresets).forEach(([k, val]) => {
          const code = String(k || "").trim().toUpperCase();
          if (!code) return;
          if (typeof val === "number") {
            const pctStr = (val * 100).toFixed(2).replace(/\.?0+$/, "");
            const fallbackLbl = US_STATE_TAX_PRESETS[code]?.label || `${code} (${pctStr}%)`;
            out[code] = { label: fallbackLbl, rate: val };
          } else if (val && typeof val === "object") {
            const rate = Number(val.rate ?? val.state_rate ?? US_STATE_TAX_PRESETS[code]?.rate ?? 0) || 0;
            const pctStr = (rate * 100).toFixed(2).replace(/\.?0+$/, "");
            const baseName = val.label || val.name;
            const label = baseName
              ? (String(baseName).includes("%") ? String(baseName) : `${baseName} (${pctStr}%)`)
              : (US_STATE_TAX_PRESETS[code]?.label || `${code} (${pctStr}%)`);
            out[code] = { label, rate };
          }
        });
      }
      if (Object.keys(out).length > 0) {
        _serverStateTaxPresets = out;
      }
    }
    if (typeof PAYLOAD !== "undefined" && PAYLOAD && PAYLOAD.us_state_tax_presets) {
      _ingestServerStateTaxPresets(PAYLOAD.us_state_tax_presets);
    }

    function _draftFamilyStats(draftObj) {
      const d = draftObj || _projDraft || PROJECT_CFG || {};
      const curYr = parseInt(String(TODAY_ISO || "2026").slice(0, 4), 10) || 2026;
      const fam = Array.isArray(d.family_members) && d.family_members.length > 0
        ? d.family_members
        : [...(Array.isArray(d.investors) ? d.investors : []), ...(Array.isArray(d.beneficiaries) ? d.beneficiaries : [])];
      let total = fam.length;
      let adults = 0;
      fam.forEach(p => {
        const bStr = String((p && (p.birth_date || p.birthdate)) || "").slice(0, 4);
        const by = /^\d{4}$/.test(bStr) ? parseInt(bStr, 10) : (curYr - 30);
        if ((curYr - by) >= 18) adults++;
      });
      if (total === 0) {
        total = (typeof SSN_INVESTORS !== "undefined" && SSN_INVESTORS.length) ? SSN_INVESTORS.length : 1;
        adults = total;
      }
      // DA-C03 / Topic A Filing Status Gate:
      // Married Filing Jointly is allowed ONLY when >1 family members exist (`mfjEligible`),
      // and defaults to checked ONLY when >=2 adult family members (age >= 18) exist (`defaultMfj`).
      // Also keep `mfjChk.parentElement && mfjChk.parentElement.style` guarded below for headless Node VM tests.
      return { total, adults, mfjEligible: total > 1, defaultMfj: total > 1 && adults >= 2 };
    }

    function describeTaxBracketFloor(floorKey, isMfj, stateRateDec) {
      const fk = String(floorKey || "none").toLowerCase().replace(/%/g, "");
      const stPct = Math.max(0, Number(stateRateDec || 0) * 100);
      const stSuffix = stPct > 0.01 ? ` + ${stPct.toFixed(1)}% state` : " (0% state)";
      const deduction = FED_STD_DED_2026[isMfj];
      const floors = isMfj ? FLOOR_TAXABLE_STARTS_MFJ : FLOOR_TAXABLE_STARTS_SINGLE;
      const gains = isMfj ? FED_LTCG_TOPS_MFJ : FED_LTCG_TOPS_SINGLE;
      if (!(fk in floors) || fk === "none") {
        return `Portfolio income uses the $${deduction.toLocaleString()} standard deduction. The federal 0% capital gains band ends at $${gains[0].toLocaleString()} of taxable income.`;
      }
      const income = deduction + floors[fk];
      const gainsRate = floors[fk] < gains[0] ? 0 : floors[fk] < gains[1] ? 15 : 20;
      return `Assumes $${income.toLocaleString()} in outside ordinary income. Additional ordinary income starts at ${fk}% federal${stSuffix}. Additional long-term gains start at ${gainsRate}% federal before net investment income tax.`;
    }

    function syncSimTaxControls() {
      simMarriedFilingJointly = null;
      simMinTaxBracketFloor = null;
      if (typeof updateSimBaselineSummary === "function") updateSimBaselineSummary();
    }
    window.syncSimTaxControls = syncSimTaxControls;

    function _getMergedStateTaxPresets() {
      return Object.assign({}, US_STATE_TAX_PRESETS, _serverStateTaxPresets || {});
    }

    function _populateTaxStateDropdown() {
      const sel = document.getElementById("proj-tax-state");
      if (!sel) return;
      const presets = _getMergedStateTaxPresets();
      const curVal = sel.value || "";
      const codes = Object.keys(presets);
      const ordered = [];
      if (presets.FED) ordered.push("FED");
      codes.filter(c => c !== "FED").sort((a, b) => {
        const la = String(presets[a]?.label || a);
        const lb = String(presets[b]?.label || b);
        return la.localeCompare(lb);
      }).forEach(c => ordered.push(c));

      let html = `<option value="">Select state...</option>`;
      ordered.forEach(code => {
        const p = presets[code];
        html += `<option value="${escHtml(code)}">${escHtml(p.label || code)}</option>`;
      });
      sel.innerHTML = html;
      if (curVal && presets[curVal]) {
        sel.value = curVal;
      } else {
        sel.value = "";
      }
    }

    function _fmtRatePctInput(decVal) {
      if (decVal == null || decVal === "" || !Number.isFinite(Number(decVal))) return "";
      const pct = Math.round(Number(decVal) * 10000) / 100;
      return String(pct);
    }

    function _syncBurnControlsFromDraft() {
      const rawBurn = _projDraft ? (_projDraft.reference_annual_burn ?? _projDraft.retirement_annual_burn) : null;
      const hasBurn = rawBurn != null && rawBurn !== "" && Number(rawBurn) > 0;
      const burnVal = hasBurn ? Math.round(Number(rawBurn)) : null;
      const burnInput = document.getElementById("proj-burn-input");
      if (burnInput && document.activeElement !== burnInput) {
        burnInput.value = burnVal != null ? ("$" + burnVal.toLocaleString("en-US")) : "";
      }
      const burnSlider = document.getElementById("proj-burn-slider");
      if (burnSlider) {
        burnSlider.value = String(burnVal != null ? Math.min(1000000, Math.max(10000, burnVal)) : 10000);
        burnSlider.style.opacity = burnVal != null ? "1" : "0.45";
      }
    }

    function _syncTaxControlsFromDraft() {
      _populateTaxStateDropdown();
      const tr = (_projDraft && _projDraft.tax_rates) || {};
      const stateSel = document.getElementById("proj-tax-state");
      const ltcgInp = document.getElementById("proj-tax-ltcg");
      const ordInp = document.getElementById("proj-tax-ordinary");
      const iraInp = document.getElementById("proj-tax-ira");
      const sumPill = document.getElementById("proj-tax-summary-pill");

      const stCode = String(tr.state_code || "").trim().toUpperCase();
      if (stateSel && document.activeElement !== stateSel) {
        const presets = _getMergedStateTaxPresets();
        stateSel.value = (stCode && presets[stCode]) ? stCode : "";
      }
      if (ltcgInp && document.activeElement !== ltcgInp) {
        ltcgInp.value = _fmtRatePctInput(tr.ltcg_top);
      }
      if (ordInp && document.activeElement !== ordInp) {
        ordInp.value = _fmtRatePctInput(tr.ordinary_top);
      }
      const iraDec = _projDraft && _projDraft.sim_ira_ordinary_tax_rate != null
        ? _projDraft.sim_ira_ordinary_tax_rate
        : null;
      if (iraInp && document.activeElement !== iraInp) {
        iraInp.value = _fmtRatePctInput(iraDec);
      }

      const famStats = _draftFamilyStats(_projDraft);
      const mfjChk = document.getElementById("proj-tax-mfj-check");
      const mfjNote = document.getElementById("proj-tax-mfj-note");
      const floorSel = document.getElementById("proj-tax-floor-select");
      const floorDesc = document.getElementById("proj-tax-floor-desc");

      let effMfj = false;
      if (!famStats.mfjEligible) {
        effMfj = false;
        if (tr.married_filing_jointly != null) tr.married_filing_jointly = false;
      } else if (typeof tr.married_filing_jointly === "boolean") {
        effMfj = tr.married_filing_jointly;
      } else {
        effMfj = famStats.defaultMfj;
      }
      if (mfjChk) {
        mfjChk.disabled = !famStats.mfjEligible;
        mfjChk.checked = effMfj;
        if (mfjChk.parentElement && mfjChk.parentElement.style) {
          mfjChk.parentElement.style.opacity = famStats.mfjEligible ? "1" : "0.55";
          mfjChk.parentElement.style.cursor = famStats.mfjEligible ? "pointer" : "not-allowed";
        }
      }
      if (mfjNote) {
        mfjNote.textContent = famStats.mfjEligible
          ? `Enabled (${famStats.total} family members, ${famStats.adults} adult${famStats.adults === 1 ? "" : "s"} 18+). Default is ${famStats.defaultMfj ? "checked (2+ adults)" : "unchecked (<2 adults)"}.`
          : "Single filing status (add a 2nd family member in the Family tab to enable Married Filing Jointly).";
      }
      const effFloor = String(tr.min_tax_bracket_floor || "none").toLowerCase();
      if (floorSel && document.activeElement !== floorSel) {
        floorSel.value = ["none", "10", "12", "22", "24", "32", "35", "37"].includes(effFloor) ? effFloor : "none";
      }
      const effStateRate = tr.state_rate != null
        ? Number(tr.state_rate)
        : (tr.ltcg_top != null ? Math.max(0, Number(tr.ltcg_top) - 0.238) : 0);
      if (floorDesc) {
        floorDesc.textContent = describeTaxBracketFloor(effFloor, effMfj, effStateRate);
      }

      if (sumPill) {
        const hasCore = Boolean(stCode && tr.ltcg_top != null && tr.ordinary_top != null);
        const hasAll = hasCore && iraDec != null;
        if (hasAll) {
          sumPill.textContent = "✓ Completed";
          sumPill.className = "pill pill-green mono proj-collapse-status-pill has-tip";
        } else if (hasCore) {
          sumPill.textContent = "Needs Review";
          sumPill.className = "pill pill-amber mono proj-collapse-status-pill has-tip";
        } else {
          sumPill.textContent = "Incomplete";
          sumPill.className = "pill pill-rose mono proj-collapse-status-pill has-tip";
        }
      }
      if (typeof syncPortfolioCollapsibleHeaders === "function") {
        syncPortfolioCollapsibleHeaders(false);
      }
    }

    const _projSectionExplicitOpen = new Map();
    const _projSectionAutoInitialized = new Set();
    let _lastStatementsState = null;
    let _projCollapseDelegatedEl = null;

    function _resetProjCollapsibleSessionState() {
      _projSectionExplicitOpen.clear();
      _projSectionAutoInitialized.clear();
    }

    function _statusCapsuleSpec(status) {
      if (status === "incomplete") {
        return { label: "Incomplete", pillCls: "pill pill-rose mono proj-collapse-status-pill has-tip", rowCls: "status-incomplete" };
      }
      if (status === "needs_review") {
        return { label: "Needs Review", pillCls: "pill pill-amber mono proj-collapse-status-pill has-tip", rowCls: "status-needs-review" };
      }
      return { label: "\u2713 Completed", pillCls: "pill pill-green mono proj-collapse-status-pill has-tip", rowCls: "status-completed" };
    }

    function _resolveProjRowExpanded(fullKey, status, deferAutoInit = false) {
      if (_projSectionExplicitOpen.has(fullKey)) {
        return Boolean(_projSectionExplicitOpen.get(fullKey));
      }
      if (_projSectionAutoInitialized.has(fullKey)) {
        return null;
      }
      const autoExpand = (status === "incomplete" || status === "needs_review");
      if (!deferAutoInit) {
        _projSectionAutoInitialized.add(fullKey);
      }
      return autoExpand;
    }

    function _applyProjRowDOM(rowEl, summaryEl, pillEl, fullKey, status, summaryText, tipHtml, forceReset = false, deferAutoInit = false) {
      if (!rowEl) return;
      const spec = _statusCapsuleSpec(status);
      rowEl.classList.remove("status-incomplete", "status-needs-review", "status-completed");
      rowEl.classList.add(spec.rowCls);
      if (summaryEl) summaryEl.textContent = summaryText || "";
      if (pillEl) {
        pillEl.textContent = spec.label;
        pillEl.className = spec.pillCls;
        if (tipHtml) pillEl.setAttribute("data-tip", tipHtml);
      }
      if (forceReset && !_projSectionExplicitOpen.has(fullKey)) {
        _projSectionAutoInitialized.delete(fullKey);
      }
      const nextExp = _resolveProjRowExpanded(fullKey, status, deferAutoInit);
      if (nextExp !== null) {
        rowEl.classList.toggle("expanded", nextExp);
      }
    }

    function _ensureProjCollapsibleDelegation() {
      const bd = document.getElementById("project-settings-backdrop");
      if (!bd || _projCollapseDelegatedEl === bd || typeof bd.addEventListener !== "function") return;
      _projCollapseDelegatedEl = bd;
      bd.addEventListener("click", evt => {
        const hdr = evt.target && evt.target.closest ? evt.target.closest("[data-proj-collapse-toggle]") : null;
        if (!hdr) return;
        if (evt.target && evt.target.closest && evt.target.closest("input, select, textarea, button, a, label")) return;
        const rowEl = hdr.closest(".proj-collapsible-row");
        if (!rowEl) return;
        const rawKey = hdr.getAttribute("data-proj-collapse-toggle") || "";
        const pid = _editingPortfolioId || (_portfoliosBundle && _portfoliosBundle.active_portfolio_id) || "default";
        let fullKey = `${rawKey}:${pid}`;
        if (rawKey.startsWith("family:")) {
          const curM = (_projDraft && Array.isArray(_projDraft.family_members)) ? _projDraft.family_members[_selectedFamilyIdx] : null;
          const mId = (curM && curM.id) || `idx_${_selectedFamilyIdx}`;
          fullKey = `${rawKey}:${pid}:${mId}`;
        }
        const nowExp = !rowEl.classList.contains("expanded");
        rowEl.classList.toggle("expanded", nowExp);
        _projSectionExplicitOpen.set(fullKey, nowExp);
      });
    }

    function _setDraftBadgePreviewSrc(src) {
      const resolved = src || "Assets/default_badge.svg";
      const previewEl = document.getElementById("proj-badge-preview");
      if (previewEl) previewEl.src = resolved;
      const thumbEl = document.getElementById("proj-badge-hdr-thumb");
      if (thumbEl) thumbEl.src = resolved;
    }

    function syncPortfolioCollapsibleHeaders(forceReset = false) {
      _ensureProjCollapsibleDelegation();
      if (!_projDraft) return;
      const pid = _editingPortfolioId || (_portfoliosBundle && _portfoliosBundle.active_portfolio_id) || "portfolio";

      // 1. Identity
      const hhName = String(_projDraft.household_name || "").trim();
      const idStatus = hhName ? "completed" : "incomplete";
      const idSummary = hhName ? `${hhName} \u2022 Portfolios/${pid}` : "Portfolio name required";
      const idTip = hhName ? "Portfolio display name is configured." : "Enter a household or portfolio display name.";
      _applyProjRowDOM(
        document.getElementById("proj-sec-identity"),
        document.getElementById("proj-sec-identity-summary"),
        document.getElementById("proj-sec-identity-pill"),
        `portfolio:identity:${pid}`,
        idStatus,
        idSummary,
        idTip,
        forceReset
      );

      // 2. Annual Burn Rate
      const rawBurn = _projDraft.reference_annual_burn ?? _projDraft.retirement_annual_burn;
      const burnVal = (rawBurn != null && rawBurn !== "" && Number(rawBurn) > 0) ? Math.round(Number(rawBurn)) : null;
      const burnStatus = burnVal ? "completed" : "incomplete";
      const burnSummary = burnVal
        ? `$${burnVal.toLocaleString("en-US")}/yr ($${Math.round(burnVal / 12).toLocaleString("en-US")}/mo after-tax, Today's $)`
        : "Not set \u2014 required for Retirement Simulator";
      const burnTip = burnVal
        ? "Reference annual after-tax household spending is configured."
        : "Enter your reference annual household spending (> $0).";
      _applyProjRowDOM(
        document.getElementById("proj-burn-box"),
        document.getElementById("proj-sec-burn-summary"),
        document.getElementById("proj-sec-burn-pill"),
        `portfolio:burn:${pid}`,
        burnStatus,
        burnSummary,
        burnTip,
        forceReset
      );

      // 3. Tax Jurisdiction & Filing
      const tr = _projDraft.tax_rates || {};
      const stCode = String(tr.state_code || "").trim().toUpperCase();
      const hasCoreTax = Boolean(stCode && tr.ltcg_top != null && tr.ordinary_top != null);
      const hasIraTax = _projDraft.sim_ira_ordinary_tax_rate != null;
      const famStats = _draftFamilyStats(_projDraft);
      const effMfj = !famStats.mfjEligible ? false : (typeof tr.married_filing_jointly === "boolean" ? tr.married_filing_jointly : famStats.defaultMfj);
      const effFloor = String(tr.min_tax_bracket_floor || "none").toLowerCase();
      let taxStatus = "incomplete";
      let taxSummary = "Filing state & tax ceilings required";
      let taxTip = "Select a filing state (or FED for 0% state tax) and verify LTCG / Ordinary tax ceilings.";
      if (hasCoreTax && hasIraTax) {
        taxStatus = "completed";
        const flStr = (effFloor && effFloor !== "none") ? ` \u2022 ${effFloor}% Floor` : "";
        taxSummary = `${stCode} \u2022 ${effMfj ? "MFJ" : "Single"}${flStr} \u2022 LTCG ${(Number(tr.ltcg_top) * 100).toFixed(1)}% / Ord ${(Number(tr.ordinary_top) * 100).toFixed(1)}%`;
        taxTip = "Tax jurisdiction, filing status, and ceiling rates are configured.";
      } else if (hasCoreTax) {
        taxStatus = "needs_review";
        taxSummary = `${stCode} \u2022 ${effMfj ? "MFJ" : "Single"} \u2022 LTCG ${(Number(tr.ltcg_top) * 100).toFixed(1)}% / Ord ${(Number(tr.ordinary_top) * 100).toFixed(1)}% (IRA ceiling unset)`;
        taxTip = "Filing state and LTCG/Ordinary ceilings are set; review Top IRA Ceiling % to complete.";
      }
      _applyProjRowDOM(
        document.getElementById("proj-tax-box"),
        document.getElementById("proj-sec-tax-summary"),
        document.getElementById("proj-tax-summary-pill"),
        `portfolio:tax:${pid}`,
        taxStatus,
        taxSummary,
        taxTip,
        forceReset
      );

      // 4. Site Graphic (nested inside Portfolio Identity)
      const rot = typeof getBadgeRotation === "function" ? getBadgeRotation() : ["Assets/default_badge.svg"];
      const curBadge = String(_projDraft.badge_image || "Assets/default_badge.svg");
      _setDraftBadgePreviewSrc(curBadge);
      const isDefBadge = curBadge.includes("default_badge.svg");
      const graphSummary = `${isDefBadge ? "Default graphic" : "Custom graphic"} (${rot.length} / 10 slots used)`;
      _applyProjRowDOM(
        document.getElementById("proj-sec-graphic"),
        document.getElementById("proj-sec-graphic-summary"),
        document.getElementById("proj-sec-graphic-pill"),
        `portfolio:graphic:${pid}`,
        "completed",
        graphSummary,
        "Site graphic is configured (upload an image or generate custom vector graphics anytime).",
        forceReset
      );

      // 5. Financial Statements
      const stLoaded = Boolean(_lastStatementsState && _lastStatementsState.pid === pid && _lastStatementsState.loaded);
      let stmtStatus = "needs_review";
      let stmtSummary = "Scanning statements\u2026";
      let stmtTip = "Scanning Statements/ directory for financial statements.";
      if (stLoaded) {
        const c = _lastStatementsState.counts || { processed: 0, available: 0, unknown: 0, total: 0 };
        const nProc = Number(c.processed) || 0;
        const nAvail = Number(c.available) || 0;
        const nUnk = Number(c.unknown) || 0;
        const nTotal = Math.max(Number(c.total) || 0, nProc + nAvail + nUnk);
        if (nTotal === 0) {
          stmtStatus = "needs_review";
          stmtSummary = "0 files in Statements/ \u2014 drop PDF/XLSX/CSV files to ingest";
          stmtTip = "No statement files found yet in Statements/. Drop statements in and run Portfolio Refresh.";
        } else if (nAvail > 0 || nUnk > 0) {
          stmtStatus = "needs_review";
          stmtSummary = `${nProc} Processed \u2022 ${nAvail} Available \u2022 ${nUnk} Unknown`;
          stmtTip = nAvail > 0
            ? `${nAvail} statement(s) validated and ready \u2014 click Portfolio Refresh to ingest.`
            : `${nUnk} file(s) were not recognized by any statement parser.`;
        } else {
          stmtStatus = "completed";
          stmtSummary = `${nProc} Processed \u2022 All statements ingested`;
          stmtTip = "All statement files in Statements/ have been ingested into your portfolio.";
        }
      }
      _applyProjRowDOM(
        document.getElementById("proj-statements-section"),
        document.getElementById("proj-sec-statements-summary"),
        document.getElementById("proj-sec-statements-pill"),
        `portfolio:statements:${pid}`,
        stmtStatus,
        stmtSummary,
        stmtTip,
        forceReset,
        !stLoaded
      );
    }

    function _roundTaxDec(v) {
      return Math.round(Number(v) * 1000000) / 1000000;
    }

    function onProjectTaxStateChange(stateCode) {
      if (!_projDraft) return;
      const code = String(stateCode || "").trim().toUpperCase();
      if (!_projDraft.tax_rates || typeof _projDraft.tax_rates !== "object") {
        _projDraft.tax_rates = {};
      }
      if (!code) {
        _projDraft.tax_rates.state_code = "";
        _projDraft.tax_rates.state_rate = null;
        _syncTaxControlsFromDraft();
        _scheduleProjectAutoSave(350);
        return;
      }
      const presets = _getMergedStateTaxPresets();
      const preset = presets[code];
      const stRate = _roundTaxDec(preset ? Number(preset.rate || 0) : 0);
      const fedLtcg = 0.20;
      const niit = 0.038;
      const fedOrd = 0.37;
      const ltcgTop = _roundTaxDec(0.238 + stRate);
      const ordTop = _roundTaxDec(0.408 + stRate);
      const iraTop = _roundTaxDec(0.37 + stRate);
      const prevMfj = _projDraft.tax_rates ? _projDraft.tax_rates.married_filing_jointly : undefined;
      const prevFloor = _projDraft.tax_rates ? _projDraft.tax_rates.min_tax_bracket_floor : undefined;
      _projDraft.tax_rates = {
        state_code: code,
        state_rate: stRate,
        fed_ltcg_top: fedLtcg,
        niit_rate: niit,
        fed_ordinary_top: fedOrd,
        ltcg_top: ltcgTop,
        ordinary_top: ordTop,
        ssn_effective: 0.3145,
        married_filing_jointly: prevMfj,
        min_tax_bracket_floor: prevFloor || "none",
      };
      _projDraft.sim_ira_ordinary_tax_rate = iraTop;
      _projDraft.sim_cash_interest_tax_rate = ordTop;
      _syncTaxControlsFromDraft();
      _scheduleProjectAutoSave(300);
    }

    function onProjectTaxFilingChange() {
      if (!_projDraft) return;
      if (!_projDraft.tax_rates || typeof _projDraft.tax_rates !== "object") {
        _projDraft.tax_rates = {};
      }
      const famStats = _draftFamilyStats(_projDraft);
      const mfjChk = document.getElementById("proj-tax-mfj-check");
      const floorSel = document.getElementById("proj-tax-floor-select");
      if (famStats.mfjEligible && mfjChk) {
        _projDraft.tax_rates.married_filing_jointly = Boolean(mfjChk.checked);
      } else {
        _projDraft.tax_rates.married_filing_jointly = false;
      }
      if (floorSel) {
        _projDraft.tax_rates.min_tax_bracket_floor = String(floorSel.value || "none").toLowerCase();
      }
      if (_portfoliosBundle && _editingPortfolioId === _portfoliosBundle.active_portfolio_id && PROJECT_CFG && PROJECT_CFG.tax_rates) {
        PROJECT_CFG.tax_rates.married_filing_jointly = _projDraft.tax_rates.married_filing_jointly;
        PROJECT_CFG.tax_rates.min_tax_bracket_floor = _projDraft.tax_rates.min_tax_bracket_floor;
        syncSimTaxControls();
        recomputeSimBars();
        renderSimChartAndNav();
        updateSimSummaryDOM();
      }
      _syncTaxControlsFromDraft();
      _scheduleProjectAutoSave(300);
    }
    window.onProjectTaxFilingChange = onProjectTaxFilingChange;

    function onProjectTaxRateInput() {
      if (!_projDraft) return;
      if (!_projDraft.tax_rates || typeof _projDraft.tax_rates !== "object") {
        _projDraft.tax_rates = {};
      }
      const stateSel = document.getElementById("proj-tax-state");
      const ltcgInp = document.getElementById("proj-tax-ltcg");
      const ordInp = document.getElementById("proj-tax-ordinary");
      const iraInp = document.getElementById("proj-tax-ira");

      const parsePctField = (el) => {
        if (!el) return undefined;
        const raw = String(el.value || "").trim();
        if (raw === "") return null;
        const num = parseFloat(raw);
        if (!Number.isFinite(num)) return null;
        return _roundTaxDec(Math.max(0, Math.min(99.9, num)) / 100.0);
      };

      if (stateSel) {
        _projDraft.tax_rates.state_code = String(stateSel.value || "").trim().toUpperCase();
      }
      const ltcgVal = parsePctField(ltcgInp);
      const ordVal = parsePctField(ordInp);
      const iraVal = parsePctField(iraInp);
      if (ltcgVal !== undefined) _projDraft.tax_rates.ltcg_top = ltcgVal;
      if (ordVal !== undefined) {
        _projDraft.tax_rates.ordinary_top = ordVal;
        _projDraft.sim_cash_interest_tax_rate = ordVal;
      }
      if (iraVal !== undefined) _projDraft.sim_ira_ordinary_tax_rate = iraVal;
      if (_projDraft.tax_rates.fed_ltcg_top == null) _projDraft.tax_rates.fed_ltcg_top = 0.20;
      if (_projDraft.tax_rates.niit_rate == null) _projDraft.tax_rates.niit_rate = 0.038;
      if (_projDraft.tax_rates.fed_ordinary_top == null) _projDraft.tax_rates.fed_ordinary_top = 0.37;
      if (_projDraft.tax_rates.ssn_effective == null) _projDraft.tax_rates.ssn_effective = 0.3145;

      syncPortfolioCollapsibleHeaders(false);
      _scheduleProjectAutoSave(400);
    }

    function _getPortfolioIncompleteMessages(entry, validationOverride = null) {
      const errs = [];
      const srcVal = validationOverride && typeof validationOverride === "object" ? validationOverride : null;
      const vErrs = (srcVal && Array.isArray(srcVal.validation_errors))
        ? srcVal.validation_errors
        : (entry && Array.isArray(entry.validation_errors) ? entry.validation_errors : []);
      const mFields = (srcVal && Array.isArray(srcVal.missing_fields))
        ? srcVal.missing_fields
        : (entry && Array.isArray(entry.missing_fields) ? entry.missing_fields : []);
      vErrs.forEach(msg => {
        const clean = String(msg || "").trim();
        if (clean && !errs.includes(clean)) errs.push(clean);
      });
      if (errs.length === 0 && mFields.length > 0) {
        errs.push(`Missing required fields: ${mFields.join(", ")}`);
      }
      if (errs.length === 0) {
        errs.push("Complete Annual Burn Rate, Tax Jurisdiction & Rates, and Investor Birthdate in Portfolio Settings.");
      }
      return errs;
    }

    function _updateSelectPortfolioButtonState(activeTabOverride = null) {
      const selBtn = document.getElementById("proj-save-btn");
      if (!selBtn) return;
      const infoTabActive = activeTabOverride
        ? (activeTabOverride === "info")
        : Boolean(document.getElementById("proj-tab-btn-info")?.classList.contains("active"));
      selBtn.style.display = infoTabActive ? "" : "none";

      const list = Array.isArray(_portfoliosBundle && _portfoliosBundle.portfolios) ? _portfoliosBundle.portfolios : [];
      const activeId = (_portfoliosBundle && _portfoliosBundle.active_portfolio_id) || null;
      const hasActive = Boolean(_portfoliosBundle && _portfoliosBundle.has_active && activeId);
      const selEntry = _selectedPortfolioId ? list.find(p => p.id === _selectedPortfolioId) : null;

      if (!selEntry) {
        selBtn.disabled = true;
        selBtn.title = "Select or create a portfolio first";
        return;
      }
      if (selEntry.is_complete === false) {
        const msgs = _getPortfolioIncompleteMessages(selEntry);
        selBtn.disabled = true;
        selBtn.title = `Cannot activate incomplete portfolio — ${msgs.join(" • ")}`;
        return;
      }
      if (hasActive && selEntry.id === activeId && list.length < 2) {
        selBtn.disabled = true;
        selBtn.title = "This portfolio is already active in the dashboard";
        return;
      }
      selBtn.disabled = false;
      selBtn.title = "Activate and load the selected portfolio into the dashboard";
    }

    function _makeDefaultSavedPromptsList() {
      return (PAYLOAD.default_saved_prompt_specs || []).map(([id, title]) => ({
        id, title, prompt: _defaultAdvisorPrompts[id] || "",
      }));
    }

    function _buildDefaultDraftForPortfolio(name = "My Portfolio") {
      // PORTABILITY DO: Seed new portfolios with empty/unconfigured required fields so users enter real household values (`DP-04`).
      // PORTABILITY DON'T: Silently pre-fill "1975-01-01", $150,000 salary, $50,000 burn, or California 37.1%/54.1% tax rates.
      const defaultMember = {
        id: "member_1",
        name: "Family Member 1",
        birth_date: "",
        milestone_age: 85,
        is_rmd_owner: true,
        is_ssn_contributor: false,
        ssn: { default_claim_age: 67, external_file: "" },
        work_plan: sanitizeWorkPlanJs({ is_working: true }, 85),
      };
      return {
        household_name: name || "My Portfolio",
        badge_image: "Assets/default_badge.svg",
        badge_history: ["Assets/default_badge.svg"],
        badge_metadata: {},
        reference_annual_burn: null,
        retirement_annual_burn: null,
        tax_rates: {
          state_code: "",
          state_rate: null,
          ltcg_top: null,
          ordinary_top: null,
          fed_ltcg_top: null,
          niit_rate: null,
          fed_ordinary_top: null,
          ssn_effective: null,
          married_filing_jointly: null,
          min_tax_bracket_floor: "none",
        },
        sim_ira_ordinary_tax_rate: null,
        sim_cash_interest_tax_rate: null,
        primary_equity_symbol: "",
        symbols: [],
        family_members: [JSON.parse(JSON.stringify(defaultMember))],
        investors: [],
        beneficiaries: [JSON.parse(JSON.stringify(defaultMember))],
        advisor_prompts: {},
        saved_prompts: _makeDefaultSavedPromptsList(),
      };
    }

    function _normalizeProjectDraftObject(projObj, fallbackName = "My Portfolio") {
      const p = (projObj && typeof projObj === "object") ? projObj : {};
      const base = _buildDefaultDraftForPortfolio(p.household_name || fallbackName);
      const rawBurn = p.reference_annual_burn ?? p.retirement_annual_burn;
      const parsedBurn = (rawBurn != null && rawBurn !== "" && Number.isFinite(Number(rawBurn)) && Number(rawBurn) > 0)
        ? Math.round(Number(rawBurn))
        : null;
      const rawTr = (p.tax_rates && typeof p.tax_rates === "object") ? p.tax_rates : {};
      const numOrNull = (v) => (v != null && v !== "" && Number.isFinite(Number(v))) ? Number(v) : null;
      const loadedSavedPrompts = Array.isArray(p.saved_prompts)
        ? p.saved_prompts
        : base.saved_prompts;

      return {
        household_name: (p.household_name !== undefined && p.household_name !== null)
          ? String(p.household_name)
          : (fallbackName != null ? String(fallbackName) : ""),
        badge_image: p.badge_image || "Assets/default_badge.svg",
        badge_history: Array.isArray(p.badge_history) && p.badge_history.length > 0
          ? p.badge_history
          : ["Assets/default_badge.svg"],
        badge_metadata: (p.badge_metadata && typeof p.badge_metadata === "object")
          ? Object.assign({}, p.badge_metadata)
          : {},
        reference_annual_burn: parsedBurn,
        retirement_annual_burn: parsedBurn,
        tax_rates: {
          state_code: String(rawTr.state_code || "").trim().toUpperCase(),
          state_rate: numOrNull(rawTr.state_rate),
          ltcg_top: numOrNull(rawTr.ltcg_top),
          ordinary_top: numOrNull(rawTr.ordinary_top),
          fed_ltcg_top: numOrNull(rawTr.fed_ltcg_top),
          niit_rate: numOrNull(rawTr.niit_rate),
          fed_ordinary_top: numOrNull(rawTr.fed_ordinary_top),
          ssn_effective: numOrNull(rawTr.ssn_effective),
          state_ssn_rate: numOrNull(rawTr.state_ssn_rate),
          ssn_fed_taxable_portion: numOrNull(rawTr.ssn_fed_taxable_portion),
          married_filing_jointly: typeof rawTr.married_filing_jointly === "boolean" ? rawTr.married_filing_jointly : null,
          min_tax_bracket_floor: rawTr.min_tax_bracket_floor ? String(rawTr.min_tax_bracket_floor).toLowerCase() : "none",
        },
        sim_ira_ordinary_tax_rate: numOrNull(p.sim_ira_ordinary_tax_rate),
        sim_cash_interest_tax_rate: numOrNull(p.sim_cash_interest_tax_rate),
        primary_equity_symbol: p.primary_equity_symbol || "",
        symbols: Array.isArray(p.symbols) && p.symbols.length > 0
          ? p.symbols
          : (p.primary_equity_symbol ? [p.primary_equity_symbol] : []),
        family_members: Array.isArray(p.family_members) && p.family_members.length > 0
          ? JSON.parse(JSON.stringify(p.family_members))
          : null,
        investors: Array.isArray(p.investors)
          ? JSON.parse(JSON.stringify(p.investors))
          : [],
        beneficiaries: Array.isArray(p.beneficiaries) ? JSON.parse(JSON.stringify(p.beneficiaries)) : [],
        advisor_prompts: Object.assign({}, p.advisor_prompts || {}),
        saved_prompts: JSON.parse(JSON.stringify(loadedSavedPrompts)),
      };
    }

    async function _loadPortfolioDraftIntoEditor(pid) {
      _selectedPromptIdx = 0;
      if (!pid) {
        _projDraft = _buildDefaultDraftForPortfolio("My Portfolio");
        _projValidation = null;
        _loadedDraftPortfolioId = null;
        renderProjectSettingsForm();
        return;
      }
      try {
        const { ok, data } = await apiJson(`/api/project?portfolio_id=${encodeURIComponent(pid)}`);
        if (ok && data && data.ok && data.project) {
            if (data.portfolios_bundle) _portfoliosBundle = data.portfolios_bundle;
            if (data.state_tax_presets) _ingestServerStateTaxPresets(data.state_tax_presets);
            _projValidation = data.validation || null;
            if (data.default_advisor_prompts) _defaultAdvisorPrompts = Object.assign({}, data.default_advisor_prompts);
            if (data.advisor_prompts) _resolvedAdvisorPrompts = Object.assign({}, data.advisor_prompts);
            const projSource = Object.assign({}, data.project);
            if (!Array.isArray(projSource.saved_prompts) && Array.isArray(data.saved_prompts)) {
              projSource.saved_prompts = data.saved_prompts;
            }
            _projDraft = _normalizeProjectDraftObject(projSource, "");
            _initFamilyMembersFromDraft();
            _loadedDraftPortfolioId = pid;
            _projLastSavedPayload = JSON.stringify(_projDraft);
            if (_portfoliosBundle && pid === _portfoliosBundle.active_portfolio_id) {
              _activeSavedPrompts = JSON.parse(JSON.stringify(_projDraft.saved_prompts));
              renderAiPromptsDropdown();
            }
            fetchSsnStatementFiles(pid);
            renderProjectSettingsForm();
            updateProjectSettingsHeaderUI();
          }
      } catch (_e) {}
    }

    function renderPortfoliosTab() {
      const listEl = document.getElementById("proj-portfolios-list");
      const detailBox = document.getElementById("proj-portfolio-detail-box");
      const detailTitle = document.getElementById("proj-portfolio-detail-title");
      const detailPill = document.getElementById("proj-portfolio-detail-pill");
      const valBanner = document.getElementById("proj-validation-banner");
      if (!listEl) return;

      const list = Array.isArray(_portfoliosBundle && _portfoliosBundle.portfolios) ? _portfoliosBundle.portfolios : [];
      const activeId = (_portfoliosBundle && _portfoliosBundle.active_portfolio_id) || null;
      if (_selectedPortfolioId && !list.some(p => p.id === _selectedPortfolioId)) {
        _selectedPortfolioId = activeId || (list[0] ? list[0].id : null);
      }
      if (_editingPortfolioId && !list.some(p => p.id === _editingPortfolioId)) {
        _editingPortfolioId = activeId || null;
      }

      _updateSelectPortfolioButtonState();

      if (list.length === 0) {
        listEl.innerHTML = `
          <div style="padding:18px;text-align:center;color:#94a3b8;font-size:12.5px;border:1px dashed #334155;border-radius:8px;">
            No portfolios have been created yet. Click <strong>+ New Portfolio</strong> above to create your first portfolio.
          </div>
        `;
        if (detailBox) detailBox.style.display = "none";
        return;
      }

      listEl.innerHTML = list.map(p => {
        const isActive = p.id === activeId;
        const isSelected = p.id === _selectedPortfolioId;
        const isEditing = p.id === _editingPortfolioId;
        const isIncomplete = p.is_complete === false;
        const badgeUrl = p.badge_image || "Assets/default_badge.svg";
        const metaParts = [];
        metaParts.push(`${p.investors_count || 0} investor${(p.investors_count || 0) === 1 ? "" : "s"}`);
        if (p.scenarios_count != null) metaParts.push(`${p.scenarios_count} scenario${p.scenarios_count === 1 ? "" : "s"}`);

        // DESIGN INTENT (DP-04): Display the amber `Incomplete` capsule ONLY on incomplete draft rows
        // (`p.is_complete === false`). Incomplete portfolios may be saved, listed, and opened for editing,
        // but cannot be selected to become the active simulation portfolio until all required fields validate.
        let incompletePillHtml = "";
        if (isIncomplete) {
          const errs = _getPortfolioIncompleteMessages(p);
          const tipHtml = `<div style="font-weight:700;color:#fde047;margin-bottom:4px;">Incomplete Portfolio — Cannot Activate Yet</div>` +
            `<ul style="margin:0;padding-left:16px;font-size:11px;line-height:1.45;color:#e2e8f0;">` +
            errs.map(e => `<li>${escHtml(e)}</li>`).join("") +
            `</ul>`;
          incompletePillHtml = `<span class="pill pill-amber mono has-tip" style="font-size:10px;padding:1px 7px;cursor:help;" data-tip="${escHtml(tipHtml)}">Incomplete</span>`;
        }

        return `
          <div class="proj-symbol-row" style="cursor:pointer;display:flex;align-items:center;justify-content:space-between;padding:10px 14px;${isSelected || isEditing ? "background:rgba(56,189,248,0.12);border-color:#38bdf8;" : ""}" data-sa-action="select-portfolio" data-sa-id="${escHtml(p.id)}">
            <div style="display:flex;align-items:center;gap:12px;min-width:0;flex:1;">
              <img src="${escHtml(badgeUrl)}" alt="" style="width:44px;height:24px;object-fit:cover;border-radius:5px;border:1px solid #334155;background:#020617;flex-shrink:0;" />
              <div style="min-width:0;flex:1;">
                <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
                  <span style="font-weight:700;font-size:14px;color:#f8fafc;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${escHtml(p.name || p.id)}</span>
                  ${isActive ? `<span class="pill pill-green mono" style="font-size:10px;padding:1px 7px;">Loaded</span>` : ""}
                  ${incompletePillHtml}
                </div>
                <div class="muted mono" style="font-size:11px;">Portfolios/${escHtml(p.id)} &bull; ${escHtml(metaParts.join(" • "))}</div>
              </div>
            </div>
            <div style="display:flex;align-items:center;gap:8px;flex-shrink:0;">
              <button type="button" class="btn ${isEditing ? "btn-primary" : ""}" style="font-size:11.5px;padding:4px 10px;display:flex;align-items:center;gap:5px;" title="Edit ${escHtml(p.name || p.id)}" aria-label="Edit ${escHtml(p.name || p.id)}" data-sa-action="edit-portfolio" data-sa-id="${escHtml(p.id)}" data-sa-stop="1">
                ${ICONS.pencil}
                <span>${isEditing ? "Editing" : "Edit"}</span>
              </button>
              <button type="button" class="btn-delete-symbol" title="Delete ${escHtml(p.name || p.id)}" aria-label="Delete ${escHtml(p.name || p.id)}" data-sa-action="del-portfolio" data-sa-id="${escHtml(p.id)}" data-sa-stop="1">
                ${ICONS.trash}
              </button>
            </div>
          </div>
        `;
      }).join("");

      const editEntry = _editingPortfolioId ? list.find(p => p.id === _editingPortfolioId) : null;
      if (!editEntry) {
        if (detailBox) detailBox.style.display = "none";
        return;
      }
      if (detailBox) detailBox.style.display = "block";
      if (detailTitle) detailTitle.textContent = `Edit Portfolio: ${(_projDraft && _projDraft.household_name) || editEntry.name}`;
      if (detailPill) detailPill.textContent = `Portfolios/${editEntry.id}`;

      if (valBanner) {
        const useValOverride = (_loadedDraftPortfolioId === editEntry.id && _projValidation) ? _projValidation : null;
        const editIncomplete = useValOverride ? (useValOverride.valid === false) : (editEntry.is_complete === false);
        if (editIncomplete) {
          const errs = _getPortfolioIncompleteMessages(editEntry, useValOverride);
          valBanner.style.display = "block";
          valBanner.innerHTML =
            `<div style="font-weight:700;color:#fde047;margin-bottom:3px;">&#9888; Complete required settings before activating this portfolio:</div>` +
            `<ul style="margin:0;padding-left:18px;">${errs.map(e => `<li>${escHtml(e)}</li>`).join("")}</ul>`;
        } else {
          valBanner.style.display = "none";
          valBanner.innerHTML = "";
        }
      }

      const hEl = document.getElementById("proj-household-name");
      if (hEl && document.activeElement !== hEl && _projDraft) {
        hEl.value = _projDraft.household_name !== undefined ? _projDraft.household_name : (editEntry.name || "");
      }
      _syncBurnControlsFromDraft();
      _syncTaxControlsFromDraft();

      if (_projDraft) {
        _setDraftBadgePreviewSrc(_projDraft.badge_image || editEntry.badge_image || "Assets/default_badge.svg");
      }
      populateBadgeModelDropdown();
      updateDefaultBadgePrompt(false);
      syncPortfolioCollapsibleHeaders(false);
      fetchPortfolioStatements(_editingPortfolioId || editEntry.id);
    }

    function selectPortfolioRow(pid) {
      _selectedPortfolioId = pid;
      const list = Array.isArray(_portfoliosBundle && _portfoliosBundle.portfolios) ? _portfoliosBundle.portfolios : [];
      const row = list.find(p => p.id === pid);
      if (row && row.is_complete === false) {
        const errs = _getPortfolioIncompleteMessages(row);
        _setProjSettingsMsg(`"${row.name || row.id}" is incomplete — complete required settings (${errs.join("; ")}) before activating.`, "#fbbf24");
        editPortfolioRow(pid);
        return;
      }
      renderPortfoliosTab();
    }

    async function editPortfolioRow(pid) {
      await _flushProjectAutoSave();
      const switchedPortfolio = (_editingPortfolioId !== pid);
      _selectedPortfolioId = pid;
      _editingPortfolioId = pid;
      if (switchedPortfolio) {
        _lastStatementsState = { pid: null, loaded: false, processed: 0, available: 0, unknown: 0, total: 0 };
      }
      if (_loadedDraftPortfolioId !== pid) {
        await _loadPortfolioDraftIntoEditor(pid);
      }
      renderPortfoliosTab();
    }

    function toggleNewPortfolioBox(show) {
      const box = document.getElementById("proj-new-portfolio-box");
      const inp = document.getElementById("proj-new-portfolio-name");
      if (!box) return;
      box.style.display = show ? "flex" : "none";
      if (show && inp) {
        inp.value = "";
        setTimeout(() => inp.focus(), 20);
      }
    }

    async function confirmCreateNewPortfolio() {
      const inp = document.getElementById("proj-new-portfolio-name");
      const btn = document.getElementById("proj-new-portfolio-create-btn");
      const name = (inp ? inp.value : "").trim();
      if (!name) {
        _setProjSettingsMsg("Please enter a portfolio name first.", "#fda4af");
        if (inp) inp.focus();
        return;
      }
      if (btn) btn.disabled = true;
      _setProjSettingsMsg(`Creating "${name}"...`, "#7dd3fc");
      try {
        await _flushProjectAutoSave();
        const prevActiveId = (_portfoliosBundle && _portfoliosBundle.active_portfolio_id) || null;
        const { ok, data } = await apiJson("/api/portfolios", { body: { name, activate: false, rebuild: false } });
        if (!ok || !data || !data.ok) {
          _setProjSettingsMsg((data && data.error) || "Failed to create portfolio.", "#fda4af");
          return;
        }
        toggleNewPortfolioBox(false);
        if (data.portfolios_bundle) {
          _portfoliosBundle = data.portfolios_bundle;
        } else {
          const resolvedActive = data.active_portfolio_id !== undefined ? data.active_portfolio_id : prevActiveId;
          _portfoliosBundle = {
            active_portfolio_id: resolvedActive || null,
            has_active: Boolean(data.has_active != null ? data.has_active : resolvedActive),
            portfolios: data.portfolios || [],
          };
        }
        _selectedPortfolioId = data.portfolio_id;
        _editingPortfolioId = data.portfolio_id;
        if (_portfoliosBundle.active_portfolio_id && _portfoliosBundle.active_portfolio_id !== prevActiveId) {
          _projNeedsReloadOnClose = true;
        }
        await _loadPortfolioDraftIntoEditor(data.portfolio_id);
        renderPortfoliosTab();
        updateProjectSettingsHeaderUI();
        _setProjSettingsMsg(`Created "${name}". Complete required settings (burn rate, filing state/tax rates, investor birthdate) before activating.`, "#fbbf24");
      } catch (_e) {
        _setProjSettingsMsg("Local server offline. Cannot create portfolio.", "#fda4af");
      } finally {
        if (btn) btn.disabled = false;
      }
    }

    function openPortfolioDeleteConfirm(pid) {
      const list = (_portfoliosBundle && _portfoliosBundle.portfolios) || [];
      const target = list.find(p => p.id === pid);
      if (!target) return;
      _deleteTargetPortfolioId = pid;
      const nameEl = document.getElementById("portfolio-delete-name");
      const pathEl = document.getElementById("portfolio-delete-path");
      const inp = document.getElementById("portfolio-delete-confirm-input");
      const errEl = document.getElementById("portfolio-delete-err");
      const delBtn = document.getElementById("portfolio-delete-confirm-btn");
      if (nameEl) nameEl.textContent = target.name || target.id;
      if (pathEl) pathEl.textContent = `Portfolios/${target.id}/`;
      if (inp) inp.value = "";
      if (errEl) errEl.textContent = "";
      if (delBtn) delBtn.disabled = true;
      const bd = document.getElementById("portfolio-delete-backdrop");
      if (bd) {
        bd.classList.add("open");
        if (inp) setTimeout(() => inp.focus(), 20);
      }
    }

    function closePortfolioDeleteConfirm() {
      _deleteTargetPortfolioId = null;
      const bd = document.getElementById("portfolio-delete-backdrop");
      if (bd) bd.classList.remove("open");
    }

    function onPortfolioDeleteInput() {
      const inp = document.getElementById("portfolio-delete-confirm-input");
      const delBtn = document.getElementById("portfolio-delete-confirm-btn");
      const errEl = document.getElementById("portfolio-delete-err");
      if (!inp || !delBtn) return;
      const typed = inp.value.trim().toLowerCase();
      delBtn.disabled = typed !== "yes";
      if (errEl) errEl.textContent = "";
    }

    async function executePortfolioDelete() {
      const pid = _deleteTargetPortfolioId;
      const inp = document.getElementById("portfolio-delete-confirm-input");
      const errEl = document.getElementById("portfolio-delete-err");
      const delBtn = document.getElementById("portfolio-delete-confirm-btn");
      if (!pid || !inp) return;
      if (inp.value.trim().toLowerCase() !== "yes") {
        if (errEl) errEl.textContent = "Please type 'yes' to confirm permanent deletion.";
        return;
      }
      if (delBtn) delBtn.disabled = true;
      try {
        const { ok, data } = await apiJson("/api/portfolios/delete", { body: { portfolio_id: pid, confirm: "yes" } });
        if (!ok || !data || !data.ok) {
          if (errEl) errEl.textContent = (data && data.error) || "Failed deleting portfolio.";
          if (delBtn) delBtn.disabled = false;
          return;
        }
        closePortfolioDeleteConfirm();
        _portfoliosBundle = {
          active_portfolio_id: data.active_portfolio_id || null,
          has_active: Boolean(data.has_active),
          portfolios: data.portfolios || [],
        };
        if (_selectedPortfolioId === pid) {
          _selectedPortfolioId = _portfoliosBundle.active_portfolio_id || ((_portfoliosBundle.portfolios[0] && _portfoliosBundle.portfolios[0].id) || null);
        }
        if (_editingPortfolioId === pid) {
          _editingPortfolioId = _portfoliosBundle.active_portfolio_id || null;
          if (_editingPortfolioId) {
            await _loadPortfolioDraftIntoEditor(_editingPortfolioId);
          } else {
            _projDraft = _buildDefaultDraftForPortfolio("My Portfolio");
            _loadedDraftPortfolioId = null;
            renderProjectSettingsForm();
          }
        }
        if (data.was_active) {
          _projNeedsReloadOnClose = true;
        }
        if (!(_portfoliosBundle && _portfoliosBundle.has_active && _portfoliosBundle.active_portfolio_id)) {
          _activeSavedPrompts = [];
          renderAiPromptsDropdown();
        } else if (data.was_active && _editingPortfolioId !== _portfoliosBundle.active_portfolio_id) {
          apiJson(`/api/project?portfolio_id=${encodeURIComponent(_portfoliosBundle.active_portfolio_id)}`)
            .then(({ ok: pOk, data: d }) => {
              if (!pOk) return;
              if (d && d.ok && d.project && Array.isArray(d.project.saved_prompts)) {
                _activeSavedPrompts = JSON.parse(JSON.stringify(d.project.saved_prompts));
                renderAiPromptsDropdown();
              }
            }).catch(() => {});
        }
        renderPortfoliosTab();
        updateProjectSettingsHeaderUI();
        _setProjSettingsMsg(`Permanently deleted "${pid}".`, "#fda4af");
      } catch (_e) {
        if (errEl) errEl.textContent = "Local server offline. Cannot delete portfolio.";
        if (delBtn) delBtn.disabled = false;
      }
    }

    function onPortfolioIdentityChanged() {
      if (!_projDraft) return;
      const hEl = document.getElementById("proj-household-name");
      if (hEl) {
        _projDraft.household_name = hEl.value;
      }
      const list = (_portfoliosBundle && _portfoliosBundle.portfolios) || [];
      const rowObj = _editingPortfolioId ? list.find(p => p.id === _editingPortfolioId) : null;
      if (rowObj) {
        rowObj.name = _projDraft.household_name.trim() || rowObj.id;
        const dt = document.getElementById("proj-portfolio-detail-title");
        if (dt) dt.textContent = `Edit Portfolio: ${rowObj.name}`;
        updateProjectSettingsHeaderUI();
        renderPortfoliosTab();
      }
      updateDefaultBadgePrompt(false);
      syncPortfolioCollapsibleHeaders(false);
      _scheduleProjectAutoSave(400);
    }

    function onProjectBurnSliderInput(val) {
      const num = Math.max(1000, Math.round(Number(val) || 10000));
      if (!_projDraft) return;
      _projDraft.reference_annual_burn = num;
      _projDraft.retirement_annual_burn = num;
      const slider = document.getElementById("proj-burn-slider");
      if (slider) slider.style.opacity = "1";
      const inp = document.getElementById("proj-burn-input");
      if (inp) inp.value = "$" + num.toLocaleString("en-US");
      syncPortfolioCollapsibleHeaders(false);
      _scheduleProjectAutoSave(400);
    }

    function onProjectBurnTextInput(val) {
      if (!_projDraft) return;
      const clean = String(val || "").replace(/[^0-9]/g, "");
      const num = clean ? parseInt(clean, 10) : null;
      _projDraft.reference_annual_burn = num;
      _projDraft.retirement_annual_burn = num;
      const slider = document.getElementById("proj-burn-slider");
      if (slider) {
        if (num != null && num > 0) {
          slider.value = String(Math.min(1000000, Math.max(10000, num)));
          slider.style.opacity = "1";
        } else {
          slider.value = "10000";
          slider.style.opacity = "0.45";
        }
      }
      syncPortfolioCollapsibleHeaders(false);
      _scheduleProjectAutoSave(400);
    }

    function onProjectBurnTextFocus(el) {
      if (el) setTimeout(() => el.select(), 10);
    }

    function onProjectBurnTextBlur(el) {
      if (!_projDraft) return;
      const clean = String(el ? el.value : "").replace(/[^0-9]/g, "");
      if (!clean) {
        _projDraft.reference_annual_burn = null;
        _projDraft.retirement_annual_burn = null;
        if (el) el.value = "";
        syncPortfolioCollapsibleHeaders(false);
        _scheduleProjectAutoSave(0);
        return;
      }
      const num = Math.max(1000, parseInt(clean, 10));
      _projDraft.reference_annual_burn = num;
      _projDraft.retirement_annual_burn = num;
      if (el) el.value = "$" + num.toLocaleString("en-US");
      syncPortfolioCollapsibleHeaders(false);
      const slider = document.getElementById("proj-burn-slider");
      if (slider) {
        slider.value = String(Math.min(1000000, Math.max(10000, num)));
      }
      _scheduleProjectAutoSave(0);
    }

    async function _saveProjectDraftNow() {
      if (!_editingPortfolioId || !_projDraft) return;
      _syncProjDraftFromDOM();
      const serialized = JSON.stringify(_projDraft);
      if (serialized === _projLastSavedPayload) return;
      _setProjSettingsMsg("Portfolio Settings • Saving...", "#7dd3fc");
      try {
        const payloadBody = Object.assign({}, _projDraft, {
          portfolio_id: _editingPortfolioId,
          rebuild: false,
          household_name: (_projDraft.household_name || "").trim(),
        });
        const { ok, data } = await apiJson("/api/project", { body: payloadBody });
        if (ok && data && data.ok) {
          _projLastSavedPayload = serialized;
          if (data.validation) {
            _projValidation = data.validation;
          }
          if (data.portfolios_bundle) {
            _portfoliosBundle = data.portfolios_bundle;
          } else if (data.validation && _portfoliosBundle && Array.isArray(_portfoliosBundle.portfolios)) {
            const pRow = _portfoliosBundle.portfolios.find(p => p.id === _editingPortfolioId);
            if (pRow) {
              pRow.is_complete = Boolean(data.validation.valid);
              pRow.missing_fields = Array.isArray(data.validation.missing_fields) ? data.validation.missing_fields : [];
              pRow.validation_errors = Array.isArray(data.validation.validation_errors) ? data.validation.validation_errors : [];
            }
          }
          if (_editingPortfolioId === _portfoliosBundle.active_portfolio_id) {
            _projNeedsReloadOnClose = true;
            if (_projDraft.reference_annual_burn) {
              if (typeof PAYLOAD !== "undefined") {
                PAYLOAD.reference_annual_burn = _projDraft.reference_annual_burn;
                PAYLOAD.retirement_annual_burn = _projDraft.reference_annual_burn;
              }
              if (typeof PROJECT_CFG === "object" && PROJECT_CFG) {
                PROJECT_CFG.reference_annual_burn = _projDraft.reference_annual_burn;
                PROJECT_CFG.retirement_annual_burn = _projDraft.reference_annual_burn;
              }
              if (typeof RETIREMENT_ANNUAL_BURN !== "undefined") {
                RETIREMENT_ANNUAL_BURN = _projDraft.reference_annual_burn;
              }
              if (typeof syncSimBurnControls === "function") {
                syncSimBurnControls();
              }
            }
            if (typeof PROJECT_CFG === "object" && PROJECT_CFG) {
              const nextInvs = (data.project && Array.isArray(data.project.investors)) ? data.project.investors : (_projDraft.investors || []);
              const nextBens = (data.project && Array.isArray(data.project.beneficiaries)) ? data.project.beneficiaries : (_projDraft.beneficiaries || []);
              const nextFams = (data.project && Array.isArray(data.project.family_members)) ? data.project.family_members : (_projDraft.family_members || []);
              PROJECT_CFG.investors = JSON.parse(JSON.stringify(nextInvs));
              PROJECT_CFG.beneficiaries = JSON.parse(JSON.stringify(nextBens));
              PROJECT_CFG.family_members = JSON.parse(JSON.stringify(nextFams));
              if (typeof SSN_INVESTORS !== "undefined" && Array.isArray(SSN_INVESTORS)) {
                SSN_INVESTORS.forEach((sInv, sIdx) => {
                  if (PROJECT_CFG.investors[sIdx] && PROJECT_CFG.investors[sIdx].work_plan) {
                    sInv.work_plan = JSON.parse(JSON.stringify(PROJECT_CFG.investors[sIdx].work_plan));
                  }
                });
              }
              if (typeof syncSimRetirementAgeControls === "function") syncSimRetirementAgeControls();
              if (typeof recomputeSimBars === "function") recomputeSimBars();
              if (typeof renderSimChartAndNav === "function") renderSimChartAndNav();
            }
            _activeSavedPrompts = JSON.parse(JSON.stringify(
              (data.project && Array.isArray(data.project.saved_prompts))
                ? data.project.saved_prompts
                : (_projDraft.saved_prompts || [])
            ));
            renderAiPromptsDropdown();
          }
          updateProjectSettingsHeaderUI();
          _updateSelectPortfolioButtonState();
          const infoPane = document.getElementById("proj-pane-info");
          if (infoPane && infoPane.classList.contains("active")) {
            renderPortfoliosTab();
          }
          _setProjSettingsMsg("Portfolio Settings • Saved ✓", "#34d399");
        } else {
          _setProjSettingsMsg((data && data.error) || "Auto-save failed.", "#fda4af");
        }
      } catch (_e) {
        _setProjSettingsMsg("Local server offline; changes not persisted.", "#fda4af");
      }
    }

    function _scheduleProjectAutoSave(delayMs = 350) {
      if (!_editingPortfolioId || !_projDraft) return;
      if (_projAutoSaveTimer) clearTimeout(_projAutoSaveTimer);
      if (delayMs <= 0) {
        _projAutoSaveTimer = null;
        _projAutoSavePromise = _saveProjectDraftNow();
        return;
      }
      _projAutoSaveTimer = setTimeout(() => {
        _projAutoSaveTimer = null;
        _projAutoSavePromise = _saveProjectDraftNow();
      }, delayMs);
    }

    async function _flushProjectAutoSave() {
      if (_projAutoSaveTimer) {
        clearTimeout(_projAutoSaveTimer);
        _projAutoSaveTimer = null;
        await _saveProjectDraftNow();
      } else if (_projAutoSavePromise) {
        await _projAutoSavePromise;
      }
    }

    function _getProjSavedPrompts() {
      if (!_projDraft) return [];
      if (!Array.isArray(_projDraft.saved_prompts)) {
        _projDraft.saved_prompts = _makeDefaultSavedPromptsList();
      }
      return _projDraft.saved_prompts;
    }

    function _makeUniquePromptId(list) {
      const used = new Set((list || []).map(p => String((p && p.id) || "")));
      let n = 1;
      while (used.has(`custom_prompt_${n}`)) n++;
      return `custom_prompt_${n}`;
    }

    function renderProjectPromptsPane() {
      if (!_projDraft) return;
      const list = _getProjSavedPrompts();
      const listEl = document.getElementById("proj-prompts-list");
      const detailBox = document.getElementById("proj-prompt-detail-box");
      const titleInput = document.getElementById("proj-prompt-title-input");
      const detailInput = document.getElementById("proj-prompt-detail-input");

      if (_selectedPromptIdx >= list.length) _selectedPromptIdx = Math.max(0, list.length - 1);
      if (_selectedPromptIdx < 0) _selectedPromptIdx = 0;

      if (listEl) {
        if (list.length === 0) {
          listEl.innerHTML = `<div style="padding:14px;border:1px dashed #334155;border-radius:8px;font-size:12px;color:#94a3b8;text-align:center;">No saved prompts for this portfolio yet. Click <strong>+Create Prompt</strong> above or select <strong>Create Saved Prompt</strong> from the Agent dropdown.</div>`;
        } else {
          listEl.innerHTML = list.map((p, idx) => {
            const isSelected = idx === _selectedPromptIdx;
            const rowTitle = (p && p.title ? String(p.title) : "Untitled Prompt").trim() || "Untitled Prompt";
            const charCount = String((p && p.prompt) || "").trim().length;
            return `
              <div class="proj-symbol-row ${isSelected ? 'active-investor-row' : ''}" style="cursor:pointer;display:flex;align-items:center;justify-content:space-between;padding:10px 14px;${isSelected ? 'background:rgba(56,189,248,0.12);border-color:#38bdf8;' : ''}" onclick="selectProjectPromptRow(${idx})">
                <div style="display:flex;align-items:center;gap:12px;min-width:0;flex:1;">
                  <span id="proj-prompt-row-title-${idx}" style="font-weight:700;font-size:13.5px;color:#f8fafc;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${escHtml(rowTitle)}</span>
                  <span id="proj-prompt-row-meta-${idx}" class="muted mono" style="font-size:11.5px;white-space:nowrap;flex-shrink:0;">${charCount} chars</span>
                </div>
                <div style="display:flex;align-items:center;gap:8px;flex-shrink:0;">
                  <button type="button" class="btn ${isSelected ? 'btn-primary' : ''}" style="font-size:11.5px;padding:4px 12px;display:flex;align-items:center;gap:5px;" onclick="event.stopPropagation();selectProjectPromptRow(${idx})">
                    ${ICONS.pencil}
                    <span>${isSelected ? 'Editing' : 'Edit'}</span>
                  </button>
                  <button type="button" class="btn-delete-symbol" title="Delete ${escHtml(rowTitle)}" aria-label="Delete ${escHtml(rowTitle)}" onclick="event.stopPropagation();removeProjectPromptRow(${idx})">
                    ${ICONS.trash}
                  </button>
                </div>
              </div>
            `;
          }).join("");
        }
      }

      if (list.length === 0) {
        if (detailBox) detailBox.style.display = "none";
        if (titleInput) titleInput.value = "";
        if (detailInput) detailInput.value = "";
        return;
      }

      const cur = list[_selectedPromptIdx];
      if (detailBox) detailBox.style.display = "block";
      if (titleInput && titleInput.value !== (cur.title || "")) {
        titleInput.value = cur.title || "";
      }
      if (detailInput && detailInput.value !== (cur.prompt || "")) {
        detailInput.value = cur.prompt || "";
      }
      const headerEl = document.getElementById("proj-prompt-detail-header");
      if (headerEl) {
        const curTitle = (cur && cur.title ? String(cur.title) : "Prompt Details").trim() || "Prompt Details";
        headerEl.textContent = `Prompt Details — ${curTitle}`;
      }
    }

    function selectProjectPromptRow(idx) {
      _syncCurrentPromptFromDOM(false);
      _selectedPromptIdx = idx;
      renderProjectPromptsPane();
    }

    function addProjectPromptRow() {
      if (!_projDraft) return;
      _syncCurrentPromptFromDOM(false);
      const list = _getProjSavedPrompts();
      list.push({
        id: _makeUniquePromptId(list),
        title: "New Prompt",
        prompt: "",
      });
      _selectedPromptIdx = list.length - 1;
      renderProjectPromptsPane();
      _scheduleProjectAutoSave(0);
      const titleInput = document.getElementById("proj-prompt-title-input");
      if (titleInput) {
        titleInput.focus();
        titleInput.select();
      }
    }

    function removeProjectPromptRow(idx) {
      if (!_projDraft) return;
      _syncCurrentPromptFromDOM(false);
      const list = _getProjSavedPrompts();
      if (idx < 0 || idx >= list.length) return;
      list.splice(idx, 1);
      if (_selectedPromptIdx >= list.length) {
        _selectedPromptIdx = Math.max(0, list.length - 1);
      } else if (_selectedPromptIdx > idx) {
        _selectedPromptIdx -= 1;
      }
      renderProjectPromptsPane();
      _scheduleProjectAutoSave(0);
    }

    function _syncCurrentPromptFromDOM(fromUser = false) {
      if (!_projDraft) return;
      const list = Array.isArray(_projDraft.saved_prompts) ? _projDraft.saved_prompts : null;
      if (!list || _selectedPromptIdx < 0 || _selectedPromptIdx >= list.length) return;
      const titleInput = document.getElementById("proj-prompt-title-input");
      const detailInput = document.getElementById("proj-prompt-detail-input");
      if (!titleInput && !detailInput) return;
      const cur = list[_selectedPromptIdx];
      if (!cur) return;
      if (titleInput) cur.title = titleInput.value;
      if (detailInput) cur.prompt = detailInput.value;

      if (cur.id === "preferred_forecast" || cur.id === "portfolio_improvements") {
        if (!_projDraft.advisor_prompts || typeof _projDraft.advisor_prompts !== "object") {
          _projDraft.advisor_prompts = {};
        }
        const defVal = _defaultAdvisorPrompts[cur.id] || "";
        const val = String(cur.prompt || "");
        _projDraft.advisor_prompts[cur.id] = (!val.trim() || val.trim() === defVal.trim()) ? "" : val;
      }

      if (fromUser) {
        const label = (cur.title ? String(cur.title) : "Untitled Prompt").trim() || "Untitled Prompt";
        const rowTitleEl = document.getElementById(`proj-prompt-row-title-${_selectedPromptIdx}`);
        if (rowTitleEl) rowTitleEl.textContent = label;
        const rowMetaEl = document.getElementById(`proj-prompt-row-meta-${_selectedPromptIdx}`);
        if (rowMetaEl) rowMetaEl.textContent = `${String(cur.prompt || "").trim().length} chars`;
        const headerEl = document.getElementById("proj-prompt-detail-header");
        if (headerEl) headerEl.textContent = `Prompt Details — ${label}`;
      }
    }

    function onProjectPromptFieldInput(fromUser = true) {
      if (!_projDraft) return;
      _syncCurrentPromptFromDOM(fromUser);
      if (fromUser) _scheduleProjectAutoSave(450);
    }

    let _availableSsnFiles = [];

    async function fetchSsnStatementFiles(portfolioId = null) {
      const pid = portfolioId || _editingPortfolioId || (_portfoliosBundle && _portfoliosBundle.active_portfolio_id) || "";
      const qs = pid ? `?portfolio_id=${encodeURIComponent(pid)}` : "";
      try {
        const { ok, data } = await apiJson(`/api/ssn/files${qs}`);
        if (ok && data && data.ok && Array.isArray(data.files)) {
            _availableSsnFiles = data.files;
            const famTab = document.getElementById("proj-pane-family") || document.getElementById("proj-pane-investors");
            if (famTab && famTab.classList.contains("active")) {
              renderProjectFamilyMembers();
            }
          }
      } catch (_e) {}
    }

    function parseSsaXmlText(xmlText, fileName = "", filePath = "") {
      try {
        const parser = new DOMParser();
        const xmlDoc = parser.parseFromString(xmlText, "text/xml");
        const parseError = xmlDoc.querySelector("parsererror");
        if (parseError) throw new Error(parseError.textContent);

        function getFirstTagText(doc, tagName) {
          const el = doc.getElementsByTagName(tagName)[0] || doc.getElementsByTagNameNS("*", tagName)[0];
          return el && el.textContent ? el.textContent.trim() : "";
        }

        const name = getFirstTagText(xmlDoc, "Name");
        const dob = getFirstTagText(xmlDoc, "DateOfBirth");

        let fraAge = 67;
        let fraEstimate = 0;
        let earlyEstimate = 0;
        let delayedEstimate = 0;

        const fullEstEl = xmlDoc.getElementsByTagName("FullRetirementEstimate")[0] ||
                          xmlDoc.getElementsByTagNameNS("*", "FullRetirementEstimate")[0];
        if (fullEstEl) {
          const estEl = fullEstEl.getElementsByTagName("Estimate")[0] ||
                        fullEstEl.getElementsByTagNameNS("*", "Estimate")[0];
          if (estEl) fraEstimate = parseFloat(estEl.textContent) || 0;
          const yrsEl = fullEstEl.getElementsByTagName("Years")[0] ||
                        fullEstEl.getElementsByTagNameNS("*", "Years")[0];
          if (yrsEl) fraAge = parseInt(yrsEl.textContent, 10) || 67;
        }

        const earlyEstEl = xmlDoc.getElementsByTagName("EarlyRetirementEstimate")[0] ||
                           xmlDoc.getElementsByTagNameNS("*", "EarlyRetirementEstimate")[0];
        if (earlyEstEl) {
          const estEl = earlyEstEl.getElementsByTagName("Estimate")[0] ||
                        earlyEstEl.getElementsByTagNameNS("*", "Estimate")[0];
          if (estEl) earlyEstimate = parseFloat(estEl.textContent) || 0;
        }

        const delayedEstEl = xmlDoc.getElementsByTagName("DelayedRetirementEstimate")[0] ||
                             xmlDoc.getElementsByTagNameNS("*", "DelayedRetirementEstimate")[0];
        if (delayedEstEl) {
          const estEl = delayedEstEl.getElementsByTagName("Estimate")[0] ||
                        delayedEstEl.getElementsByTagNameNS("*", "Estimate")[0];
          if (estEl) delayedEstimate = parseFloat(estEl.textContent) || 0;
        }

        const earnings = {};
        const earningsNodes = xmlDoc.getElementsByTagName("Earnings");
        for (let i = 0; i < earningsNodes.length; i++) {
          const node = earningsNodes[i];
          const startYr = node.getAttribute("startYear");
          const ficaEl = node.getElementsByTagName("FicaEarnings")[0] ||
                         node.getElementsByTagNameNS("*", "FicaEarnings")[0];
          if (startYr && ficaEl) {
            const val = parseFloat(ficaEl.textContent);
            if (val > 0) earnings[startYr] = val;
          }
        }

        const years = Object.keys(earnings).map(y => parseInt(y, 10)).sort((a, b) => a - b);
        const minYr = years.length > 0 ? years[0] : 0;
        const maxYr = years.length > 0 ? years[years.length - 1] : 0;
        const totEarn = Object.values(earnings).reduce((a, b) => a + b, 0);
        const avgSal = years.length > 0 ? Math.round(totEarn / years.length) : 0;

        const fn = fileName || (filePath ? filePath.split("/").pop() : "social-security-statement.xml");
        const activePid = _editingPortfolioId || (_portfoliosBundle && _portfoliosBundle.active_portfolio_id) || "portfolio";
        const fp = filePath || `Portfolios/${activePid}/Statements/SSN/${fn}`;

        return {
          ok: true,
          name: name,
          birth_date: dob,
          fra_age: fraAge,
          fra_estimate: fraEstimate,
          early_estimate: earlyEstimate,
          delayed_estimate: delayedEstimate,
          annual_earnings: earnings,
          years_worked: years.length,
          average_salary: avgSal,
          min_year: minYr,
          max_year: maxYr,
          work_span_label: years.length > 0 ? `${minYr} - ${maxYr}` : "",
          file_name: fn,
          file_path: fp
        };
      } catch (err) {
        return { ok: false, error: String(err) };
      }
    }

    function toggleSsnFilePickerMenu(evt) {
      if (evt) evt.stopPropagation();
      const menu = document.getElementById("proj-ssn-file-menu");
      if (!menu) return;

      if (_availableSsnFiles.length === 0) {
        triggerSsnFileInput();
        return;
      }

      const isOpen = menu.style.display === "flex";
      if (isOpen) {
        menu.style.display = "none";
        return;
      }

      menu.innerHTML = `
        <div style="padding:6px 10px;font-size:11px;font-weight:700;color:#94a3b8;border-bottom:1px solid #1e293b;">
          Statements/SSN/ Files:
        </div>
        ${_availableSsnFiles.map(f => `
          <div class="nav-symbol-item" data-sa-action="load-ssa-path" data-sa-path="${escHtml(f.path)}" style="display:flex;flex-direction:column;gap:2px;align-items:flex-start;padding:6px 10px;">
            <div style="display:flex;align-items:center;justify-content:space-between;width:100%;">
              <span style="font-weight:600;color:#f8fafc;font-size:12px;">📄 ${escHtml(f.name)}</span>
              <span class="muted mono" style="font-size:10px;">${(f.size/1024).toFixed(1)} KB</span>
            </div>
            ${f.details && f.details.name ? `<span class="muted" style="font-size:10.5px;">${escHtml(f.details.name)}${f.details.years_worked ? ` • ${f.details.years_worked} yrs` : ''}${f.details.fra_estimate ? ` • FRA $${Math.round(f.details.fra_estimate).toLocaleString()}` : ''}</span>` : ''}
          </div>
        `).join("")}
        <div style="border-top:1px solid #1e293b;padding-top:4px;margin-top:2px;">
          <div class="nav-symbol-item" onclick="triggerSsnFileInput()" style="font-weight:600;color:#38bdf8;padding:6px 10px;font-size:11.5px;">
            📁 Browse other XML file from disk...
          </div>
        </div>
      `;
      menu.style.display = "flex";

      function closeMenu(e) {
        if (!menu.contains(e.target) && e.target !== document.getElementById("proj-ssn-filepicker-btn")) {
          menu.style.display = "none";
          document.removeEventListener("click", closeMenu);
        }
      }
      setTimeout(() => document.addEventListener("click", closeMenu), 10);
    }

    async function loadSsaFileByPath(filePath) {
      const menu = document.getElementById("proj-ssn-file-menu");
      if (menu) menu.style.display = "none";

      const mem = _getSelectedFamilyMember();
      if (!mem) return;

      try {
        const pid = _editingPortfolioId || (_portfoliosBundle && _portfoliosBundle.active_portfolio_id) || "";
        const { ok, data } = await apiJson("/api/ssn/parse", {
          body: { file_path: filePath, portfolio_id: pid || undefined },
        });
        if (ok && data && data.ok) {
          applyParsedSsaToInvestor(mem, data);
          _syncInvestorsAndBeneficiariesFromFamily();
          renderProjectFamilyMembers();
          _scheduleProjectAutoSave(0);
          return;
        }
      } catch (_e) {}

      const found = _availableSsnFiles.find(f => f.path === filePath);
      if (found && found.details && found.details.ok) {
        applyParsedSsaToInvestor(mem, found.details);
        _syncInvestorsAndBeneficiariesFromFamily();
        renderProjectFamilyMembers();
        _scheduleProjectAutoSave(0);
      }
    }

    function applyParsedSsaToInvestor(inv, data) {
      if (!inv.ssn) inv.ssn = {};
      const activePid = _editingPortfolioId || (_portfoliosBundle && _portfoliosBundle.active_portfolio_id) || "portfolio";
      inv.ssn.external_file = data.file_path || `Portfolios/${activePid}/Statements/SSN/${data.file_name}`;
      inv.ssn.annual_earnings = data.annual_earnings || {};
      inv.ssn.work_span_label = data.work_span_label || "";
      inv.ssn.fra_estimate = data.fra_estimate || 0;
      inv.ssn.statement_name = data.name || "";
      inv.ssn.statement_dob = data.birth_date || "";
      inv.ssn.statement_file = data.file_name || "social-security-statement.xml";
      delete inv.ssn.years_worked;
      delete inv.ssn.average_salary;
      delete inv.ssn.use_simple_fields;
      delete inv.ssn.clear_statement;
    }

    function triggerSsnFileInput() {
      const menu = document.getElementById("proj-ssn-file-menu");
      if (menu) menu.style.display = "none";
      const input = document.getElementById("proj-ssn-file-input");
      if (input) {
        input.value = "";
        input.click();
      }
    }

    function handleSsaFilePicked(evt) {
      const file = evt.target && evt.target.files && evt.target.files[0];
      if (!file) return;

      const reader = new FileReader();
      reader.onload = async function(e) {
        const xmlText = e.target.result;
        const parsed = parseSsaXmlText(xmlText, file.name);
        if (!parsed || !parsed.ok) {
          alert("Failed to parse Social Security statement XML. Please ensure it is an official statement file from ssa.gov.");
          return;
        }

        const mem = _getSelectedFamilyMember();
        if (!mem) return;

        applyParsedSsaToInvestor(mem, parsed);

        try {
          const pid = _editingPortfolioId || (_portfoliosBundle && _portfoliosBundle.active_portfolio_id) || "";
          const { ok, data: sData } = await apiJson("/api/ssn/save_file", {
            body: { file_name: file.name, xml_text: xmlText, portfolio_id: pid || undefined },
          });
          if (ok && sData && sData.ok) {
            applyParsedSsaToInvestor(mem, sData);
            fetchSsnStatementFiles(pid);
          }
        } catch (_e) {}

        _syncInvestorsAndBeneficiariesFromFamily();
        renderProjectFamilyMembers();
        _scheduleProjectAutoSave(0);
      };
      reader.readAsText(file);
    }

    // DESIGN INTENT (XML-Only Social Security):
    // Social Security benefits are supported strictly via uploaded official `ssa.gov` XML statements.
    // Clearing a loaded XML statement sets `clear_statement = true` and wipes all earnings fields (`$0` PIA);
    // never fall back to `years_worked` / `average_salary` summary fields.
    function clearLoadedSsaXml() {
      const mem = _getSelectedFamilyMember();
      if (!mem || !mem.ssn) return;
      mem.ssn.external_file = "";
      mem.ssn.annual_earnings = {};
      mem.ssn.work_span_label = "";
      mem.ssn.earnings_note = "";
      mem.ssn.statement_name = "";
      mem.ssn.statement_dob = "";
      mem.ssn.statement_file = "";
      mem.ssn.fra_estimate = 0;
      delete mem.ssn.years_worked;
      delete mem.ssn.average_salary;
      delete mem.ssn.use_simple_fields;
      mem.ssn.clear_statement = true;
      _syncInvestorsAndBeneficiariesFromFamily();
      renderProjectFamilyMembers();
      _scheduleProjectAutoSave(0);
    }

    function syncInvestorDobFromXml(dobStr) {
      if (!dobStr) return;
      const mem = _getSelectedFamilyMember();
      if (!mem) return;
      mem.birth_date = dobStr;
      delete mem.birthdate;
      _syncCurrentFamilyMemberFromDOM(true);
      renderProjectFamilyMembers();
      _scheduleProjectAutoSave(0);
    }

    async function openProjectSettingsDialog(targetTab = "info") {
      const bd = document.getElementById("project-settings-backdrop");
      if (!bd) return;
      _selectedFamilyIdx = 0;
      _selectedInvestorIdx = 0;
      _projNeedsReloadOnClose = false;
      _resetProjCollapsibleSessionState();
      const curSymbols = getTrackedSymbols();
      const fallbackPayloadBurn = (typeof PAYLOAD === "object" && PAYLOAD && Number(PAYLOAD.reference_annual_burn || PAYLOAD.retirement_annual_burn) > 0)
        ? Math.round(Number(PAYLOAD.reference_annual_burn || PAYLOAD.retirement_annual_burn))
        : null;
      const resolvedInitialBurn = PROJECT_CFG.reference_annual_burn ?? PROJECT_CFG.retirement_annual_burn ?? fallbackPayloadBurn;
      const initialProjSource = Object.assign({}, PROJECT_CFG || {}, {
        household_name: PROJECT_CFG.household_name !== undefined
          ? PROJECT_CFG.household_name
          : (PAYLOAD.household_name !== undefined ? PAYLOAD.household_name : ""),
        badge_image: PROJECT_CFG.badge_image || PAYLOAD.badge_image || "Assets/default_badge.svg",
        badge_history: PROJECT_CFG.badge_history || PAYLOAD.badge_history || ["Assets/default_badge.svg"],
        badge_metadata: (PROJECT_CFG && typeof PROJECT_CFG.badge_metadata === "object" && PROJECT_CFG.badge_metadata !== null)
          ? PROJECT_CFG.badge_metadata
          : ((PAYLOAD && typeof PAYLOAD.badge_metadata === "object" && PAYLOAD.badge_metadata !== null) ? PAYLOAD.badge_metadata : {}),
        reference_annual_burn: resolvedInitialBurn,
        retirement_annual_burn: resolvedInitialBurn,
        tax_rates: PROJECT_CFG.tax_rates || PAYLOAD.tax_rates || {},
        sim_ira_ordinary_tax_rate: PROJECT_CFG.sim_ira_ordinary_tax_rate ?? null,
        sim_cash_interest_tax_rate: PROJECT_CFG.sim_cash_interest_tax_rate ?? null,
        primary_equity_symbol: PROJECT_CFG.primary_equity_symbol || (curSymbols[0] || ""),
        symbols: curSymbols.length > 0 ? curSymbols : (PROJECT_CFG.primary_equity_symbol ? [PROJECT_CFG.primary_equity_symbol] : []),
        saved_prompts: Array.isArray(PROJECT_CFG.saved_prompts)
          ? PROJECT_CFG.saved_prompts
          : (Array.isArray(PAYLOAD.saved_prompts) ? PAYLOAD.saved_prompts : _makeDefaultSavedPromptsList()),
      });
      _projDraft = _normalizeProjectDraftObject(initialProjSource, initialProjSource.household_name);
      _initFamilyMembersFromDraft();
      _selectedPromptIdx = 0;
      _projLastSavedPayload = JSON.stringify(_projDraft);
      updateProjectSettingsHeaderUI();
      renderProjectSettingsForm({ fetchStatements: false });
      switchProjectSettingsTab(targetTab || "info", false);
      _setProjSettingsMsg(
        _editingPortfolioId
          ? "Portfolio Settings • Saved"
          : "Create or select a portfolio to begin",
        "#94a3b8"
      );
      bd.classList.add("open");

      try {
        const { ok, data } = await apiJson("/api/project");
        if (ok && data && data.ok) {
            if (data.state_tax_presets) _ingestServerStateTaxPresets(data.state_tax_presets);
            _projValidation = data.validation || null;
            if (data.portfolios_bundle) {
              _portfoliosBundle = data.portfolios_bundle;
              _selectedPortfolioId = _portfoliosBundle.active_portfolio_id || ((_portfoliosBundle.portfolios && _portfoliosBundle.portfolios[0]) ? _portfoliosBundle.portfolios[0].id : null);
              _editingPortfolioId = _portfoliosBundle.active_portfolio_id || _selectedPortfolioId || null;
              _loadedDraftPortfolioId = _editingPortfolioId;
            }
            if (data.default_advisor_prompts) {
              _defaultAdvisorPrompts = Object.assign({}, data.default_advisor_prompts);
            }
            if (data.advisor_prompts) {
              _resolvedAdvisorPrompts = Object.assign({}, data.advisor_prompts);
            }
            // DESIGN INTENT (Draft Must Match Its Target): this `/api/project` call carries no
            // `portfolio_id`, so the server answers for the ACTIVE portfolio — and when nothing
            // is active it answers with the empty default config while still returning
            // `ok: true` and a truthy `data.project`. `_editingPortfolioId` was just resolved
            // above and can point at a merely *selected* (inactive) portfolio, so adopting this
            // response unconditionally hands the editor a blank draft aimed at a real file; the
            // next auto-save then serializes those blanks over it and destroys the saved
            // household name, tax rates and symbols. Only adopt the response when it is
            // genuinely the record for the portfolio being edited; otherwise fetch that
            // portfolio's own config by id.
            const _draftMatchesTarget = Boolean(data.project) && data.portfolio_id === _editingPortfolioId;
            if (_editingPortfolioId && _draftMatchesTarget) {
              const projSrc = Object.assign({}, data.project);
              if (!Array.isArray(projSrc.saved_prompts) && Array.isArray(data.saved_prompts)) {
                projSrc.saved_prompts = data.saved_prompts;
              }
              _projDraft = _normalizeProjectDraftObject(projSrc, "");
              _initFamilyMembersFromDraft();
              if (_portfoliosBundle && _editingPortfolioId === _portfoliosBundle.active_portfolio_id) {
                _activeSavedPrompts = JSON.parse(JSON.stringify(_projDraft.saved_prompts));
              }
              renderAiPromptsDropdown();
              _projLastSavedPayload = JSON.stringify(_projDraft);
              fetchSsnStatementFiles(_editingPortfolioId);
            } else if (_editingPortfolioId) {
              await _loadPortfolioDraftIntoEditor(_editingPortfolioId);
            } else {
              renderAiPromptsDropdown();
            }
            updateProjectSettingsHeaderUI();
            renderProjectSettingsForm();
            switchProjectSettingsTab(targetTab || "info", false);
            _setProjSettingsMsg(
              _editingPortfolioId
                ? "Portfolio Settings • Saved"
                : "Create or select a portfolio to begin",
              "#94a3b8"
            );
          }
      } catch (_e) {
        fetchPortfolioStatements(_editingPortfolioId || (_portfoliosBundle && _portfoliosBundle.active_portfolio_id));
      }
    }

    async function closeProjectSettingsDialog() {
      await _flushProjectAutoSave();
      const bd = document.getElementById("project-settings-backdrop");
      if (bd) bd.classList.remove("open");
      if (_projNeedsReloadOnClose) {
        _projNeedsReloadOnClose = false;
        const isDemoOn = Boolean((_privacyConfig && _privacyConfig.demo_mode && _privacyConfig.demo_mode.enabled) || (PAYLOAD && PAYLOAD.privacy_status && PAYLOAD.privacy_status.demo_enabled));
        if (!isDemoOn) {
          try {
            await apiJson("/api/project", {
              body: {
                portfolio_id: (_portfoliosBundle && _portfoliosBundle.active_portfolio_id) || undefined,
                rebuild_only: true,
              },
            });
          } catch (_e) {}
        }
        window.location.reload();
      }
    }

    function triggerBadgePicker() {
      const input = document.getElementById("proj-badge-file-input");
      if (input) {
        input.value = "";
        input.click();
      }
    }

    function handleBadgeFileSelected(evt) {
      const file = evt.target && evt.target.files && evt.target.files[0];
      if (!file) return;
      const msgEl = document.getElementById("proj-badge-msg");
      const previewEl = document.getElementById("proj-badge-preview");

      if (file.size > 2 * 1024 * 1024) {
        if (msgEl) {
          msgEl.textContent = `❌ File size (${(file.size / (1024 * 1024)).toFixed(2)} MB) exceeds 2MB limit.`;
          msgEl.style.color = "#fda4af";
        }
        return;
      }

      const reader = new FileReader();
      reader.onload = function(e) {
        const dataUrl = e.target.result;
        const img = new Image();
        img.onload = function() {
          const w = img.naturalWidth || img.width;
          const h = img.naturalHeight || img.height;

          if (w < 48 || h < 16) {
            if (msgEl) {
              msgEl.textContent = `❌ Image too small (${w}×${h}px). Minimum required is 48×16 px.`;
              msgEl.style.color = "#fda4af";
            }
            return;
          }

          if (w > 2400 || h > 1600) {
            if (msgEl) {
              msgEl.textContent = `❌ Image too large (${w}×${h}px). Maximum allowed is 2400×1600 px.`;
              msgEl.style.color = "#fda4af";
            }
            return;
          }

          _projDraft.badge_image = dataUrl;
          _setDraftBadgePreviewSrc(dataUrl);
          const rot = getBadgeRotation();
          const custom = rot.filter(b => b && b !== "Assets/default_badge.svg");
          custom.push(dataUrl);
          _projDraft.badge_history = ["Assets/default_badge.svg", ...custom.slice(-9)];
          const list = (_portfoliosBundle && _portfoliosBundle.portfolios) || [];
          const rowObj = _editingPortfolioId ? list.find(p => p.id === _editingPortfolioId) : null;
          if (rowObj) {
            rowObj.badge_image = dataUrl;
            rowObj.badge_history = _projDraft.badge_history;
          }
          renderPortfoliosTab();
          syncBadgeSliderUI();
          if (msgEl) {
            msgEl.textContent = `✓ Valid badge (${w}×${h}px, ${(file.size / 1024).toFixed(1)} KB) selected.`;
            msgEl.style.color = "#34d399";
          }
          _scheduleProjectAutoSave(0);
        };
        img.onerror = function() {
          if (msgEl) {
            msgEl.textContent = "❌ Failed to parse image. Please select a valid PNG, JPG, or SVG file.";
            msgEl.style.color = "#fda4af";
          }
        };
        img.src = dataUrl;
      };
      reader.readAsDataURL(file);
    }

    function getBadgeRotation() {
      if (!_projDraft) return ["Assets/default_badge.svg"];
      let hist = Array.isArray(_projDraft.badge_history) ? _projDraft.badge_history.slice() : [];
      const custom = hist.filter(b => b && b !== "Assets/default_badge.svg").slice(-9);
      hist = ["Assets/default_badge.svg", ...custom];
      _projDraft.badge_history = hist;
      return hist;
    }

    function syncBadgeSliderUI() {
      const slider = document.getElementById("proj-badge-slider");
      const leftLbl = document.getElementById("proj-badge-slider-left-lbl");
      const rightLbl = document.getElementById("proj-badge-slider-right-lbl");
      const delBtn = document.getElementById("proj-badge-delete-btn");
      if (!slider || !_projDraft) return;
      const rotation = getBadgeRotation();
      const total = rotation.length;
      slider.min = "1";
      slider.max = String(Math.max(1, total));
      slider.disabled = (total <= 1);

      const curBadge = _projDraft.badge_image || "Assets/default_badge.svg";
      let idx = rotation.indexOf(curBadge);
      if (idx < 0) {
        idx = rotation.findIndex(b => b && b.split("?")[0] === curBadge.split("?")[0]);
      }
      if (idx < 0) idx = 0;
      slider.value = String(idx + 1);
      slider.title = `Badge slot ${idx + 1} of ${total} (${total} of 10 slots used)`;

      if (leftLbl) leftLbl.textContent = "1";
      if (rightLbl) rightLbl.textContent = `${total} / 10 used`;

      if (delBtn) {
        if (total <= 1) {
          delBtn.disabled = true;
          delBtn.title = "Default badge is locked and cannot be deleted";
        } else if (idx === 0) {
          delBtn.disabled = false;
          delBtn.title = `Delete all ${total - 1} custom badge(s) (keep locked default badge)`;
        } else {
          delBtn.disabled = false;
          delBtn.title = `Delete this badge (Shift+click to delete all ${total - 1} custom badges)`;
        }
      }

      syncPortfolioCollapsibleHeaders(false);
      syncBadgePromptAndPresetUI(idx, curBadge);
    }

    function onBadgeSliderChange(val) {
      if (!_projDraft) return;
      const slider = document.getElementById("proj-badge-slider");
      const leftLbl = document.getElementById("proj-badge-slider-left-lbl");
      const rightLbl = document.getElementById("proj-badge-slider-right-lbl");
      const delBtn = document.getElementById("proj-badge-delete-btn");
      const rotation = getBadgeRotation();
      const total = rotation.length;
      const idx = Math.max(0, Math.min(rotation.length - 1, parseInt(val, 10) - 1));
      const selectedBadge = rotation[idx] || "Assets/default_badge.svg";
      _projDraft.badge_image = selectedBadge;

      _setDraftBadgePreviewSrc(selectedBadge);

      const list = (_portfoliosBundle && _portfoliosBundle.portfolios) || [];
      const rowObj = _editingPortfolioId ? list.find(p => p.id === _editingPortfolioId) : null;
      if (rowObj) {
        rowObj.badge_image = selectedBadge;
        rowObj.badge_history = rotation;
      }
      renderPortfoliosTab();

      // If currently editing active portfolio, update left nav badge immediately
      const activePid = _portfoliosBundle && _portfoliosBundle.active_portfolio_id;
      if (!_editingPortfolioId || _editingPortfolioId === activePid) {
        const navBadge = document.getElementById("nav-household-badge");
        if (navBadge) navBadge.src = selectedBadge;
      }

      if (slider) slider.title = `Badge slot ${idx + 1} of ${total} (${total} of 10 slots used)`;
      if (leftLbl) leftLbl.textContent = "1";
      if (rightLbl) rightLbl.textContent = `${total} / 10 used`;

      if (delBtn) {
        if (total <= 1) {
          delBtn.disabled = true;
          delBtn.title = "Default badge is locked and cannot be deleted";
        } else if (idx === 0) {
          delBtn.disabled = false;
          delBtn.title = `Delete all ${total - 1} custom badge(s) (keep locked default badge)`;
        } else {
          delBtn.disabled = false;
          delBtn.title = `Delete this badge (Shift+click to delete all ${total - 1} custom badges)`;
        }
      }

      syncBadgePromptAndPresetUI(idx, selectedBadge);

      const msgEl = document.getElementById("proj-badge-msg");
      if (msgEl) {
        msgEl.textContent = idx === 0 ? "Default badge selected." : `Badge ${idx + 1} of ${rotation.length} selected.`;
        msgEl.style.color = "#94a3b8";
      }
      _scheduleProjectAutoSave(0);
    }

    function deleteCurrentOrAllCustomBadges(evt) {
      if (!_projDraft) return;
      const rotation = getBadgeRotation();
      const total = rotation.length;
      if (total <= 1) return; // Locked default badge cannot be deleted

      const curBadge = _projDraft.badge_image || "Assets/default_badge.svg";
      let idx = rotation.indexOf(curBadge);
      if (idx < 0) {
        idx = rotation.findIndex(b => b && b.split("?")[0] === curBadge.split("?")[0]);
      }
      if (idx < 0) idx = 0;

      const isShift = evt && (evt.shiftKey || evt.altKey);
      const isDeleteAll = isShift || idx === 0;

      if (isDeleteAll) {
        // Delete all custom badges, keeping only the locked default badge
        _projDraft.badge_history = ["Assets/default_badge.svg"];
        _projDraft.badge_image = "Assets/default_badge.svg";
        _projDraft.badge_metadata = {};
      } else {
        // Delete specific custom badge at idx (idx >= 1)
        const deletedBadge = rotation[idx];
        if (deletedBadge && _projDraft.badge_metadata) {
          delete _projDraft.badge_metadata[deletedBadge];
          delete _projDraft.badge_metadata[deletedBadge.split("?")[0]];
        }
        const updated = rotation.filter((_, i) => i !== idx);
        _projDraft.badge_history = updated;
        const nextIdx = Math.max(0, idx - 1);
        _projDraft.badge_image = updated[nextIdx] || "Assets/default_badge.svg";
      }

      _setDraftBadgePreviewSrc(_projDraft.badge_image);

      const list = (_portfoliosBundle && _portfoliosBundle.portfolios) || [];
      const rowObj = _editingPortfolioId ? list.find(p => p.id === _editingPortfolioId) : null;
      if (rowObj) {
        rowObj.badge_image = _projDraft.badge_image;
        rowObj.badge_history = _projDraft.badge_history;
        if (_projDraft.badge_metadata) rowObj.badge_metadata = _projDraft.badge_metadata;
      }
      renderPortfoliosTab();

      // If currently editing active portfolio, update left nav badge immediately
      const activePid = _portfoliosBundle && _portfoliosBundle.active_portfolio_id;
      if (!_editingPortfolioId || _editingPortfolioId === activePid) {
        const navBadge = document.getElementById("nav-household-badge");
        if (navBadge) navBadge.src = _projDraft.badge_image;
      }

      syncBadgeSliderUI();

      const msgEl = document.getElementById("proj-badge-msg");
      if (msgEl) {
        if (isDeleteAll) {
          msgEl.textContent = `✓ All custom badges deleted. Locked default badge restored.`;
        } else {
          msgEl.textContent = `✓ Deleted badge slot ${idx + 1}. Locked default badge preserved.`;
        }
        msgEl.style.color = "#34d399";
      }
      _scheduleProjectAutoSave(0);
    }

    function onBadgePromptInput(el) {
      if (!el) return;
      el.dataset.isDefault = "false";
      el.dataset.isEditedByHand = "true";
      el.dataset.stylePreset = "";
      document.querySelectorAll(".badge-style-chip").forEach(chip => {
        chip.classList.remove("active");
      });
    }

    function syncBadgePromptAndPresetUI(idx, badgePath) {
      const promptEl = document.getElementById("proj-badge-prompt");
      if (!promptEl) return;
      const chips = document.querySelectorAll(".badge-style-chip");

      if (idx === 0) {
        // Slot 1 is the locked default badge loaded from disk (not generated)
        promptEl.value = "";
        promptEl.dataset.isDefault = "false";
        promptEl.dataset.isEditedByHand = "false";
        promptEl.dataset.stylePreset = "";
        chips.forEach(c => c.classList.remove("active"));
        return;
      }

      const metaMap = (_projDraft && _projDraft.badge_metadata) || {};
      let meta = metaMap[badgePath];
      if (!meta && badgePath) {
        const clean = badgePath.split("?")[0].replace(/^\.?\//, "");
        for (const [k, v] of Object.entries(metaMap)) {
          const kClean = k.split("?")[0].replace(/^\.?\//, "");
          if (k === badgePath || kClean === clean || clean.endsWith(kClean) || kClean.endsWith(clean)) {
            meta = v;
            break;
          }
        }
      }

      if (meta && meta.prompt) {
        promptEl.value = meta.prompt;
        promptEl.dataset.isDefault = "false";
        const isHand = Boolean(meta.edited_by_hand || !meta.preset);
        promptEl.dataset.isEditedByHand = isHand ? "true" : "false";
        promptEl.dataset.stylePreset = meta.preset || "";
        chips.forEach(c => {
          c.classList.toggle("active", !isHand && c.id === `chip-badge-${meta.preset}`);
        });
      } else {
        // Loaded from disk (not generated) or no saved prompt
        promptEl.value = "";
        promptEl.dataset.isDefault = "false";
        promptEl.dataset.isEditedByHand = "false";
        promptEl.dataset.stylePreset = "";
        chips.forEach(c => c.classList.remove("active"));
      }
    }

    function populateBadgeModelDropdown() {
      const sel = document.getElementById("proj-badge-model-select");
      if (!sel) return;

      const curVal = sel.value;
      const apiModels = (availableAiModels || []).filter(m => m && m.type !== "cli" && m.provider !== "agy");

      let html = _buildGroupedApiModelOptionsHtml(apiModels);
      if (!html) {
        html += `<option value="gemini">🟢 Gemini 2.5 Flash</option>`;
        html += `<option value="claude">🟢 Claude Sonnet 5.5</option>`;
        html += `<option value="openai">🟢 OpenAI GPT-4.1</option>`;
      }

      sel.innerHTML = html;
      const optionsArr = (sel && sel.options) ? Array.from(sel.options) : [];
      if (curVal && optionsArr.some(o => o.value === curVal && !o.disabled)) {
        sel.value = curVal;
      } else {
        const defaultOpt = apiModels.find(m => m.is_default && !m.shield_blocked && m.has_key);
        const firstReady = optionsArr.find(o => !o.disabled && (o.text || "").includes("🟢"));
        if (defaultOpt && optionsArr.some(o => o.value === defaultOpt.id && !o.disabled)) {
          sel.value = defaultOpt.id;
        } else if (firstReady) {
          sel.value = firstReady.value;
        }
      }
    }

    const BADGE_STYLE_PRESETS = {
      gilded: (name) => `Create an opulent, gilded typographic header graphic for "${name}". Feature an ornate gold filigree border framing the title, rich champagne and polished gold accents, emerald jewel highlights, and bespoke luxury serif typography on midnight navy.`,
      crest: (name) => `Create a prestigious heraldic crest and brand banner for "${name}". Feature an intricate gilded seal, rich royal navy and emerald gradient accents, and regal high-contrast typography against dark obsidian.`,
      fintech: (name) => `Create an ultra-sleek, modern FinTech brand graphic for "${name}". Feature dynamic luminous neon-cyan and electric-indigo gradient line-art, glowing market pulse geometry, and razor-sharp modern sans-serif typography against dark obsidian.`,
      prism: (name) => `Create a luxury faceted crystal header graphic for "${name}". Feature 3D geometric prism facets refracting emerald, cyan, and champagne gold light, with sharp high-contrast modern typography on deep midnight navy.`,
      swiss: (name) => `Create an authoritative Swiss International Style brand banner for "${name}". Feature bold clean typography, minimalist asymmetric geometric layout, subtle platinum and matte gold accent rules, and stark high-contrast elegance on dark graphite.`,
      nordic: (name) => `Create an understated Nordic minimalist wealth graphic for "${name}". Feature organic continuous-line growth curves, warm champagne gold and muted sage accents, and clean humanist typography on deep charcoal.`,
      artdeco: (name) => `Create a striking 1920s Art Deco luxury header graphic for "${name}". Feature geometric fan motifs, streamlined chevron patterns, lustrous metallic brass and polished gold borders, with elegant vintage typography on obsidian black.`,
      flora: (name) => `Create a sophisticated botanical wealth brand banner for "${name}". Feature delicate golden laurel branches, organic leaf filigree, deep forest emerald gradients, and timeless humanist serif lettering on dark midnight slate.`,
      quantum: (name) => `Create a cutting-edge quantum algorithmic finance graphic for "${name}". Feature intricate luminous vector circuit lattices, glowing deep-violet and electric-cyan energy nodes, and ultra-crisp futuristic monospace typography on dark void navy.`,
      monogram: (name) => `Create an elite bespoke monogram site banner for "${name}". Feature an intertwined calligraphic initial crest, regal platinum and rose-gold filigree accents, with stately high-contrast Roman serif typography on dark royal onyx.`
    };

    function applyBadgeStylePreset(styleKey) {
      const promptEl = document.getElementById("proj-badge-prompt");
      const hEl = document.getElementById("proj-household-name");
      const pName = (hEl && hEl.value.trim()) || (_projDraft && _projDraft.household_name) || "Household Portfolio";

      const fn = BADGE_STYLE_PRESETS[styleKey] || BADGE_STYLE_PRESETS.gilded || BADGE_STYLE_PRESETS.crest;
      if (promptEl) {
        promptEl.value = fn(pName);
        promptEl.dataset.isDefault = "false";
        promptEl.dataset.isEditedByHand = "false";
        promptEl.dataset.stylePreset = styleKey;
      }

      document.querySelectorAll(".badge-style-chip").forEach(chip => {
        chip.classList.toggle("active", chip.id === `chip-badge-${styleKey}`);
      });
      const iterateCheck = document.getElementById("proj-badge-iterate-check");
      if (iterateCheck) iterateCheck.checked = false;
    }

    function onBadgeIterateToggle(checked) {
      const promptEl = document.getElementById("proj-badge-prompt");
      if (!promptEl) return;
      if (checked) {
        promptEl.dataset.prevPrompt = promptEl.value;
        promptEl.placeholder = "Describe refinements to current graphic (e.g. switch accents to electric cyan, adjust typography, refine layout)...";
        promptEl.value = "Keep the overall styling and colors, but refine the typography and add subtle luminous accent rules.";
      } else {
        if (promptEl.dataset.prevPrompt) {
          promptEl.value = promptEl.dataset.prevPrompt;
        } else {
          updateDefaultBadgePrompt(true);
        }
        promptEl.placeholder = "Prompt for site graphic generation...";
      }
    }

    function updateDefaultBadgePrompt(force = false) {
      const promptEl = document.getElementById("proj-badge-prompt");
      if (!promptEl) return;
      const iterateCheck = document.getElementById("proj-badge-iterate-check");
      if (iterateCheck && iterateCheck.checked) return;
      if (!force) {
        const curBadge = (_projDraft && _projDraft.badge_image) || "Assets/default_badge.svg";
        const rot = getBadgeRotation();
        const idx = rot.indexOf(curBadge);
        if (idx <= 0) return; // slot 1 (default badge) stays empty
        if (promptEl.dataset.isEditedByHand === "true") return;
      }
      const hEl = document.getElementById("proj-household-name");
      const pName = (hEl && hEl.value.trim()) || (_projDraft && _projDraft.household_name) || "Household Portfolio";
      const presetKey = promptEl.dataset.stylePreset;
      if (!presetKey) return;
      const fn = BADGE_STYLE_PRESETS[presetKey] || BADGE_STYLE_PRESETS.gilded;
      promptEl.value = fn(pName);
    }

    async function generatePortfolioBadge() {
      const btn = document.getElementById("proj-badge-generate-btn");
      const btnLbl = document.getElementById("proj-badge-btn-lbl");
      const msgEl = document.getElementById("proj-badge-msg");
      const previewEl = document.getElementById("proj-badge-preview");
      const modelSel = document.getElementById("proj-badge-model-select");
      const promptEl = document.getElementById("proj-badge-prompt");
      const iterateCheck = document.getElementById("proj-badge-iterate-check");

      const pid = _editingPortfolioId || (_portfoliosBundle && _portfoliosBundle.active_portfolio_id) || "";
      const hEl = document.getElementById("proj-household-name");
      const pName = (hEl && hEl.value.trim()) || (_projDraft && _projDraft.household_name) || "Household Portfolio";
      const userPrompt = (promptEl && promptEl.value.trim()) || "";
      const selectedModel = (modelSel && modelSel.value) || "gemini";
      const isIterate = Boolean(iterateCheck && iterateCheck.checked);

      const activeChip = document.querySelector(".badge-style-chip.active");
      const activePreset = activeChip ? activeChip.id.replace("chip-badge-", "") : (promptEl ? promptEl.dataset.stylePreset || "" : "");
      const isEditedByHand = Boolean((promptEl && promptEl.dataset.isEditedByHand === "true") || !activePreset);

      if (btn) {
        btn.disabled = true;
        btn.classList.add("btn-generating");
        btn.style.pointerEvents = "none";
        btn.style.cursor = "wait";
      }
      if (btnLbl) {
        btnLbl.innerHTML = `<span style="display:inline-block;animation:badge-spin 1s linear infinite;">⚙</span> Generating...`;
      }
      if (msgEl) {
        msgEl.textContent = "";
      }

      try {
        const { ok, data } = await apiJson("/api/portfolio/generate_badge", {
          body: {
            portfolio_id: pid,
            portfolio_name: pName,
            prompt: userPrompt,
            model: selectedModel,
            iterate_current: isIterate,
            preset: activePreset,
            edited_by_hand: isEditedByHand,
          },
        });
        if (ok && data && data.ok) {
          const badgeUrl = data.badge_data_url || data.badge_image;
          if (_projDraft) {
            _projDraft.badge_image = data.badge_image || badgeUrl;
            if (Array.isArray(data.badge_history)) {
              _projDraft.badge_history = data.badge_history;
            } else {
              const rot = getBadgeRotation();
              const custom = rot.filter(b => b && b !== "Assets/default_badge.svg");
              custom.push(data.badge_image || badgeUrl);
              _projDraft.badge_history = ["Assets/default_badge.svg", ...custom.slice(-9)];
            }
            if (data.badge_metadata && typeof data.badge_metadata === "object") {
              _projDraft.badge_metadata = data.badge_metadata;
            }
          }
          _setDraftBadgePreviewSrc(badgeUrl);

          // If currently editing active portfolio, update left nav badge immediately
          const activePid = _portfoliosBundle && _portfoliosBundle.active_portfolio_id;
          if (!pid || pid === activePid) {
            const navBadge = document.getElementById("nav-household-badge");
            if (navBadge) navBadge.src = badgeUrl;
          }

          // Update portfolio bundle entry
          const list = (_portfoliosBundle && _portfoliosBundle.portfolios) || [];
          const rowObj = pid ? list.find(p => p.id === pid) : null;
          if (rowObj) {
            rowObj.badge_image = data.badge_image || badgeUrl;
            if (_projDraft && _projDraft.badge_history) rowObj.badge_history = _projDraft.badge_history;
            if (_projDraft && _projDraft.badge_metadata) rowObj.badge_metadata = _projDraft.badge_metadata;
          }
          renderPortfoliosTab();
          syncBadgeSliderUI();

          if (msgEl) {
            const modelUsed = data.model_used || selectedModel;
            if (data.is_fallback) {
              msgEl.textContent = `⚠️ AI model unavailable (${modelUsed}); used parametric fallback graphic.`;
              msgEl.style.color = "#f59e0b";
            } else {
              msgEl.textContent = `✓ Graphic generated (${modelUsed}) and saved!`;
              msgEl.style.color = "#34d399";
            }
          }
        } else {
          const errMsg = (data && data.error) || `HTTP ${resp.status}`;
          if (msgEl) {
            msgEl.textContent = `❌ Generation failed: ${errMsg}`;
            msgEl.style.color = "#fda4af";
          }
        }
      } catch (err) {
        if (msgEl) {
          msgEl.textContent = `❌ Network error: ${err.message || err}`;
          msgEl.style.color = "#fda4af";
        }
      } finally {
        if (btn) {
          btn.disabled = false;
          btn.classList.remove("btn-generating");
          btn.style.pointerEvents = "";
          btn.style.cursor = "";
        }
        if (btnLbl) btnLbl.innerHTML = `&#10024; Generate`;
      }
    }

    let _statementsFetchSeq = 0;

    async function fetchPortfolioStatements(pid) {
      const tableBody = document.getElementById("proj-statements-table-body");
      const countPill = document.getElementById("proj-statements-count-pill");
      const dirLabel = document.getElementById("proj-statements-dir-label");
      const summaryEl = document.getElementById("proj-statements-status-summary");
      const refreshBtn = document.getElementById("proj-portfolio-refresh-btn");
      const targetPid = pid || _editingPortfolioId || (_portfoliosBundle && _portfoliosBundle.active_portfolio_id);

      if (!targetPid) return;
      const reqSeq = ++_statementsFetchSeq;
      if (dirLabel) dirLabel.textContent = `Portfolios/${targetPid}/Statements/`;

      try {
        const { ok, status, data } = await apiJson(`/api/portfolio/statements?portfolio_id=${encodeURIComponent(targetPid)}`);
        if (reqSeq !== _statementsFetchSeq) return;
        const activeEditingPid = _editingPortfolioId || (_portfoliosBundle && _portfoliosBundle.active_portfolio_id);
        if (activeEditingPid && targetPid !== activeEditingPid) return;
        if (!ok) throw new Error(`HTTP ${status}`);
        if (!data || !data.ok) throw new Error((data && data.error) || "Failed to load statements");

        const files = Array.isArray(data.files) ? data.files : [];
        const rawCounts = data.counts || {};
        const nProc = rawCounts.processed != null ? Number(rawCounts.processed) : files.filter(f => f && f.status === "Processed").length;
        const nAvail = rawCounts.available != null ? Number(rawCounts.available) : files.filter(f => f && f.status === "Available").length;
        const nUnk = rawCounts.unknown != null ? Number(rawCounts.unknown) : files.filter(f => f && f.status === "Unknown").length;
        const counts = {
          processed: nProc,
          available: nAvail,
          unknown: nUnk,
          total: Math.max(Number(rawCounts.total) || 0, nProc + nAvail + nUnk, files.length),
        };
        _lastStatementsState = { pid: activeEditingPid || targetPid, loaded: true, counts };
        syncPortfolioCollapsibleHeaders(false);

        if (countPill) {
          countPill.textContent = `${files.length} File${files.length === 1 ? "" : "s"}`;
        }
        if (summaryEl) {
          summaryEl.innerHTML = `
            <span style="color:#94a3b8;">${counts.processed} Processed</span> &bull; 
            <span style="color:${counts.available > 0 ? '#34d399' : '#64748b'};font-weight:${counts.available > 0 ? '700' : '400'};">${counts.available} Available</span> &bull; 
            <span style="color:${counts.unknown > 0 ? '#f87171' : '#64748b'};">${counts.unknown} Unknown</span>
          `;
        }

        // Manage Portfolio Refresh button state (unghosted when Available statements exist)
        if (refreshBtn) {
          if (counts.available > 0) {
            refreshBtn.disabled = false;
            refreshBtn.style.opacity = "1";
            refreshBtn.style.cursor = "pointer";
            refreshBtn.title = `Run ingest pipeline for the currently active portfolio (${counts.available} Available statement${counts.available === 1 ? "" : "s"})`;
            refreshBtn.classList.remove("ghosted");
          } else {
            refreshBtn.disabled = true;
            refreshBtn.style.opacity = "0.45";
            refreshBtn.style.cursor = "not-allowed";
            refreshBtn.title = "No available statements ready for ingestion";
            refreshBtn.classList.add("ghosted");
          }
        }

        if (!tableBody) return;
        if (files.length === 0) {
          tableBody.innerHTML = `
            <tr>
              <td colspan="3" style="padding:18px;text-align:center;color:#64748b;font-size:12px;">
                No statement files found in <code class="mono" style="color:#38bdf8;">Portfolios/${escHtml(targetPid)}/Statements/</code>.
              </td>
            </tr>
          `;
          return;
        }

        tableBody.innerHTML = files.map((f, idx) => {
          let capsuleHtml = "";
          if (f.status === "Processed") {
            capsuleHtml = `<span class="pill mono" style="background:#334155;color:#cbd5e1;border:1px solid #475569;font-size:10.5px;padding:2px 8px;border-radius:12px;font-weight:600;display:inline-block;" title="Statement already ingested into your local portfolio store">Processed</span>`;
          } else if (f.status === "Available") {
            capsuleHtml = `<span class="pill mono" style="background:rgba(16,185,129,0.22);color:#34d399;border:1px solid rgba(16,185,129,0.55);font-size:10.5px;padding:2px 8px;border-radius:12px;font-weight:600;display:inline-block;" title="Parser validated statement format via dry run (ready to ingest)">Available</span>`;
          } else {
            capsuleHtml = `<span class="pill mono" style="background:rgba(239,68,68,0.18);color:#f87171;border:1px solid rgba(239,68,68,0.45);font-size:10.5px;padding:2px 8px;border-radius:12px;font-weight:600;display:inline-block;" title="${escHtml(f.error || 'No registered parser recognized this statement format')}">Unknown</span>`;
          }

          const rowBg = idx % 2 === 1 ? "background:rgba(15,23,42,0.4);" : "";
          const icon = f.filename.endsWith(".pdf") ? "📄" : (f.filename.endsWith(".xlsx") ? "📊" : (f.filename.endsWith(".xml") ? "📜" : "📋"));
          
          return `
            <tr style="border-bottom:1px solid #1e293b;${rowBg}">
              <td style="padding:8px 12px;color:#f1f5f9;">
                <div style="display:flex;align-items:center;gap:7px;min-width:0;">
                  <span style="font-size:13px;flex-shrink:0;">${icon}</span>
                  <span class="mono" style="font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;" title="${escHtml(f.filename)}">${escHtml(f.filename)}</span>
                </div>
              </td>
              <td style="padding:8px 12px;color:#94a3b8;white-space:nowrap;" class="mono">
                ${escHtml(f.date || "—")}
              </td>
              <td style="padding:8px 12px;text-align:center;white-space:nowrap;">
                ${capsuleHtml}
              </td>
            </tr>
          `;
        }).join("");
      } catch (err) {
        if (tableBody) {
          tableBody.innerHTML = `
            <tr>
              <td colspan="3" style="padding:16px;text-align:center;color:#fda4af;font-size:12px;">
                Could not load statements: ${escHtml(err.message || String(err))}
              </td>
            </tr>
          `;
        }
      }
    }

    async function rescanPortfolioStatements() {
      const pid = _editingPortfolioId || (_portfoliosBundle && _portfoliosBundle.active_portfolio_id);
      const rescanBtn = document.getElementById("proj-statements-rescan-btn");
      const rescanLbl = document.getElementById("proj-statements-rescan-lbl");
      if (rescanBtn) rescanBtn.disabled = true;
      if (rescanLbl) rescanLbl.innerHTML = `&#8987; Rescanning...`;
      await fetchPortfolioStatements(pid);
      if (rescanBtn) rescanBtn.disabled = false;
      if (rescanLbl) rescanLbl.innerHTML = `&#8635; Rescan`;
    }

    async function triggerPortfolioRefresh() {
      const btn = document.getElementById("proj-portfolio-refresh-btn");
      const lbl = document.getElementById("proj-portfolio-refresh-lbl");
      const msg = document.getElementById("proj-statements-action-msg");
      if (btn) {
        btn.disabled = true;
        btn.style.opacity = "0.6";
      }
      if (lbl) lbl.innerHTML = `&#8987; Ingesting...`;
      if (msg) {
        msg.textContent = "Running ingest pipeline for active portfolio...";
        msg.style.color = "#38bdf8";
      }

      try {
        const { ok, data } = await apiJson("/api/portfolio/refresh", {
          body: {
            portfolio_id: (_portfoliosBundle && _portfoliosBundle.active_portfolio_id) || undefined,
          },
        });
        if (ok && data && data.ok) {
          if (msg) {
            msg.textContent = "✓ Ingest pipeline completed successfully! Rescanning statements...";
            msg.style.color = "#34d399";
          }
          _projNeedsReloadOnClose = true;
          await fetchPortfolioStatements(_editingPortfolioId || (_portfoliosBundle && _portfoliosBundle.active_portfolio_id));
        } else {
          if (msg) {
            msg.textContent = `❌ Refresh failed: ${(data && data.error) || 'Ingest error'}`;
            msg.style.color = "#fda4af";
          }
        }
      } catch (err) {
        if (msg) {
          msg.textContent = `❌ Network error: ${err.message || err}`;
          msg.style.color = "#fda4af";
        }
      } finally {
        if (lbl) lbl.innerHTML = `&#9881; Portfolio Refresh`;
      }
    }

    function renderProjectSymbolsList() {
      const container = document.getElementById("proj-symbols-list");
      const countBadge = document.getElementById("proj-symbols-count-badge");
      if (!container || !_projDraft) return;

      if (!Array.isArray(_projDraft.symbols)) {
        _projDraft.symbols = _projDraft.primary_equity_symbol ? [_projDraft.primary_equity_symbol] : [];
      }
      const symbols = _projDraft.symbols;
      if (countBadge) {
        countBadge.textContent = `${symbols.length} / 10`;
        countBadge.className = symbols.length >= 10 ? "pill pill-cyan mono" : "pill pill-blue mono";
      }

      if (symbols.length === 0) {
        container.innerHTML = `
          <div style="padding:16px;text-align:center;color:#94a3b8;font-size:12px;border:1px dashed #334155;border-radius:8px;">
            No symbols configured. Type a ticker above and click <strong>+ Verify &amp; Add</strong>.
          </div>
        `;
        return;
      }

      container.innerHTML = symbols.map((sym, idx) => `
        <div class="proj-symbol-row" draggable="true"
             ondragstart="onSymbolDragStart(event, ${idx})"
             ondragover="onSymbolDragOver(event)"
             ondrop="onSymbolDrop(event, ${idx})"
             ondragend="onSymbolDragEnd(event)">
          <span class="symbol-drag-handle" title="Drag to reorder">&#8942;&#8942;</span>
          <div style="display:flex;flex-direction:column;gap:1px;">
            <button type="button" class="symbol-order-btn" title="Move Up" ${idx === 0 ? "disabled" : ""} onclick="moveProjectSymbol(${idx}, -1)">&blacktriangle;</button>
            <button type="button" class="symbol-order-btn" title="Move Down" ${idx === symbols.length - 1 ? "disabled" : ""} onclick="moveProjectSymbol(${idx}, 1)">&blacktriangledown;</button>
          </div>
          <div style="display:flex;align-items:center;gap:8px;flex:1;min-width:0;">
            <span class="mono" style="font-size:13.5px;font-weight:700;color:#f8fafc;">${escHtml(sym)}</span>
            ${(typeof SYMBOL_DISPLAY_LABELS === "object" && SYMBOL_DISPLAY_LABELS[sym]) ? `<span class="muted" style="font-size:11.5px;">${escHtml(SYMBOL_DISPLAY_LABELS[sym])}</span>` : ""}
            ${idx === 0 ? '<span class="pill pill-blue mono" style="font-size:10px;">Primary Equity</span>' : `<span class="muted mono" style="font-size:11px;">#${idx + 1}</span>`}
            <span class="pill pill-green mono" style="font-size:10px;margin-left:auto;">&#10003; Verified</span>
          </div>
          <button type="button" class="btn-delete-symbol" title="Delete ${escHtml(sym)}" onclick="removeProjectSymbol(${idx})">
            ${ICONS.trash}
          </button>
        </div>
      `).join("");
    }

    function onSymbolDragStart(evt, idx) {
      _draggedSymbolIdx = idx;
      evt.dataTransfer.effectAllowed = "move";
      if (evt.target) evt.target.classList.add("dragging");
    }

    function onSymbolDragOver(evt) {
      evt.preventDefault();
      evt.dataTransfer.dropEffect = "move";
    }

    function onSymbolDrop(evt, targetIdx) {
      evt.preventDefault();
      if (_draggedSymbolIdx === null || _draggedSymbolIdx === targetIdx) return;
      _syncProjDraftFromDOM();
      const item = _projDraft.symbols.splice(_draggedSymbolIdx, 1)[0];
      _projDraft.symbols.splice(targetIdx, 0, item);
      _projDraft.primary_equity_symbol = _projDraft.symbols[0] || "";
      _draggedSymbolIdx = null;
      renderProjectSymbolsList();
      _scheduleProjectAutoSave(0);
    }

    function onSymbolDragEnd(evt) {
      _draggedSymbolIdx = null;
      document.querySelectorAll(".proj-symbol-row.dragging").forEach(el => el.classList.remove("dragging"));
    }

    function moveProjectSymbol(idx, delta) {
      _syncProjDraftFromDOM();
      const symbols = _projDraft.symbols;
      const targetIdx = idx + delta;
      if (targetIdx < 0 || targetIdx >= symbols.length) return;
      const temp = symbols[idx];
      symbols[idx] = symbols[targetIdx];
      symbols[targetIdx] = temp;
      _projDraft.primary_equity_symbol = symbols[0] || "";
      renderProjectSymbolsList();
      _scheduleProjectAutoSave(0);
    }

    function removeProjectSymbol(idx) {
      _syncProjDraftFromDOM();
      if (Array.isArray(_projDraft.symbols)) {
        _projDraft.symbols.splice(idx, 1);
        _projDraft.primary_equity_symbol = _projDraft.symbols[0] || "";
      }
      renderProjectSymbolsList();
      _scheduleProjectAutoSave(0);
    }

    async function addProjectSymbol() {
      _syncProjDraftFromDOM();
      const inputEl = document.getElementById("proj-new-symbol-input");
      const msgEl = document.getElementById("proj-symbol-verify-msg");
      const btnEl = document.getElementById("proj-add-symbol-btn");
      if (!inputEl) return;
      const rawSym = inputEl.value.trim().toUpperCase();
      if (!rawSym) {
        if (msgEl) { msgEl.textContent = "Please enter a ticker or index name (e.g. AAPL, NASDAQ, SP500)."; msgEl.style.color = "#fda4af"; }
        return;
      }
      const sym = (typeof resolveSymbolLookup === "function") ? resolveSymbolLookup(rawSym) : rawSym;
      if (!Array.isArray(_projDraft.symbols)) _projDraft.symbols = [];
      if (_projDraft.symbols.length >= 10) {
        if (msgEl) { msgEl.textContent = "Maximum limit of 10 symbols reached."; msgEl.style.color = "#fda4af"; }
        return;
      }
      if (_projDraft.symbols.includes(sym)) {
        const lbl = (typeof SYMBOL_DISPLAY_LABELS === "object" && SYMBOL_DISPLAY_LABELS[sym]) ? ` (${SYMBOL_DISPLAY_LABELS[sym]})` : "";
        if (msgEl) { msgEl.textContent = `${sym}${lbl} is already in the tracked list.`; msgEl.style.color = "#fda4af"; }
        return;
      }

      if (btnEl) btnEl.disabled = true;
      const aliasHint = sym !== rawSym ? ` (resolved from "${rawSym}")` : "";
      if (msgEl) { msgEl.textContent = `Verifying ${sym}${aliasHint} with Yahoo Finance...`; msgEl.style.color = "#7dd3fc"; }

      try {
        const { ok, status, data } = await apiJson(`/api/quote?symbol=${encodeURIComponent(sym)}`);
        if (ok && data && data.ok) {
          const finalSym = String(data.resolved_symbol || data.symbol || sym).trim().toUpperCase();
          if (_projDraft.symbols.includes(finalSym)) {
            if (msgEl) { msgEl.textContent = `${finalSym} is already in the tracked list.`; msgEl.style.color = "#fda4af"; }
            return;
          }
          _projDraft.symbols.push(finalSym);
          _projDraft.primary_equity_symbol = _projDraft.symbols[0] || finalSym;
          inputEl.value = "";
          if (msgEl) {
            const pxFmt = (typeof formatSymbolQuotePrice === "function")
              ? formatSymbolQuotePrice(finalSym, Number(data.price))
              : `$${Number(data.price).toFixed(2)}`;
            const fromNote = finalSym !== rawSym ? ` [from ${rawSym}]` : "";
            msgEl.textContent = `✓ Added ${finalSym}${fromNote} (${data.name || ""}) - ${pxFmt}`;
            msgEl.style.color = "#34d399";
          }
          renderProjectSymbolsList();
          _scheduleProjectAutoSave(0);
          return;
        }
        const err = (data && data.error) || `HTTP ${status}`;
        if (msgEl) {
          msgEl.textContent = `❌ Symbol "${rawSym}" not found: ${err}`;
          msgEl.style.color = "#fda4af";
        }
      } catch (e) {
        if (msgEl) {
          msgEl.textContent = `❌ Could not verify "${rawSym}": service temporarily offline.`;
          msgEl.style.color = "#fda4af";
        }
      } finally {
        if (btnEl) btnEl.disabled = false;
      }
    }

    let _selectedFamilyIdx = 0;
    let _selectedInvestorIdx = 0; // backward compatibility alias
    let _famSsnExpanded = false;
    const _wpRetSavingsOpen = new Map();

    function _autoRetireElapsedWorkPlanJs(wp, birthDateStr) {
      if (!wp || typeof wp !== "object" || !wp.is_working) return wp;
      const bStr = String(birthDateStr || "").trim();
      if (!/^\d{4}-\d{2}(?:-\d{2})?$/.test(bStr)) return wp;
      const parts = bStr.split("-");
      const bYr = parseInt(parts[0], 10);
      const bMo = Math.max(1, Math.min(12, parseInt(parts[1], 10) || 1));
      const retAge = parseInt(wp.default_retirement_age, 10) || 65;
      const retYm = `${String(bYr + retAge).padStart(4, "0")}-${String(bMo).padStart(2, "0")}`;
      const curYm = String(TODAY_ISO || "2026-01-01").slice(0, 7);
      if (curYm.length === 7 && retYm <= curYm) {
        wp.is_working = false;
      }
      return wp;
    }

    function _initFamilyMembersFromDraft() {
      if (!_projDraft) return;
      if (Array.isArray(_projDraft.family_members) && _projDraft.family_members.length > 0) {
        const draftPersonsById = {};
        [...(Array.isArray(_projDraft.investors) ? _projDraft.investors : []),
         ...(Array.isArray(_projDraft.beneficiaries) ? _projDraft.beneficiaries : [])].forEach(p => {
          if (p && typeof p === "object") {
            const k = String(p.id || p.name || "").trim().toLowerCase().replace(/\s+/g, "_");
            if (k) draftPersonsById[k] = p;
          }
        });
        _projDraft.family_members.forEach((m, idx) => {
          if (!m.id) m.id = `member_${idx + 1}`;
          if (!m.birth_date && m.birthdate) {
            m.birth_date = m.birthdate;
            delete m.birthdate;
          }
          if (m.milestone_age == null) m.milestone_age = 85;
          if (m.is_ssn_contributor == null) {
            m.is_ssn_contributor = false;
          }
          if (m.is_rmd_owner == null && m.rmd_owner != null) {
            m.is_rmd_owner = Boolean(m.rmd_owner);
            delete m.rmd_owner;
          }
          const mk = String(m.id || m.name || "").trim().toLowerCase().replace(/\s+/g, "_");
          const srcPerson = draftPersonsById[mk] || {};
          const rawWp = (m.work_plan && typeof m.work_plan === "object") ? m.work_plan : srcPerson.work_plan;
          m.work_plan = _autoRetireElapsedWorkPlanJs(
            sanitizeWorkPlanJs(rawWp, m.milestone_age || 85),
            m.birth_date
          );
        });
      } else {
        const fam = [];
        const invs = Array.isArray(_projDraft.investors) ? _projDraft.investors : [];
        const bens = Array.isArray(_projDraft.beneficiaries) ? _projDraft.beneficiaries : [];
        invs.forEach((inv, idx) => {
          const msAge = inv.milestone_age || 85;
          const bDateInv = inv.birth_date || inv.birthdate || "";
          fam.push({
            id: inv.id || `investor_${idx + 1}`,
            name: inv.name || `Investor ${idx + 1}`,
            birth_date: bDateInv,
            milestone_age: msAge,
            is_rmd_owner: Boolean(inv.is_rmd_owner != null ? inv.is_rmd_owner : (inv.rmd_owner != null ? inv.rmd_owner : idx === 0)),
            is_ssn_contributor: idx < 2,
            ssn: inv.ssn ? JSON.parse(JSON.stringify(inv.ssn)) : { default_claim_age: 67, external_file: "" },
            work_plan: _autoRetireElapsedWorkPlanJs(sanitizeWorkPlanJs(inv.work_plan, msAge), bDateInv),
          });
        });
        bens.forEach((ben, idx) => {
          const msAge = ben.milestone_age || 85;
          const bDateBen = ben.birth_date || ben.birthdate || "";
          fam.push({
            id: ben.id || `beneficiary_${idx + 1}`,
            name: ben.name || `Beneficiary ${idx + 1}`,
            birth_date: bDateBen,
            milestone_age: msAge,
            is_rmd_owner: Boolean(ben.is_rmd_owner != null ? ben.is_rmd_owner : false),
            is_ssn_contributor: false,
            ssn: { default_claim_age: 67, external_file: "" },
            work_plan: _autoRetireElapsedWorkPlanJs(sanitizeWorkPlanJs(ben.work_plan, msAge), bDateBen),
          });
        });
        if (fam.length === 0) {
          fam.push({
            id: "member_1",
            name: "Family Member 1",
            birth_date: "",
            milestone_age: 85,
            is_rmd_owner: true,
            is_ssn_contributor: false,
            ssn: { default_claim_age: 67, external_file: "" },
            work_plan: sanitizeWorkPlanJs({ is_working: true }, 85),
          });
        }
        _projDraft.family_members = fam;
      }
      _syncInvestorsAndBeneficiariesFromFamily();
    }

    function _syncInvestorsAndBeneficiariesFromFamily() {
      if (!_projDraft || !Array.isArray(_projDraft.family_members)) return;
      const fam = _projDraft.family_members;

      // Enforce 0..2 SSN contributors (0 is allowed; never force SSN onto Member #1)
      let ssnCount = 0;
      fam.forEach(m => {
        if (m.is_ssn_contributor) {
          ssnCount++;
          if (ssnCount > 2) {
            m.is_ssn_contributor = false;
          }
        }
      });

      // Enforce exactly 1 Designated RMD Owner across ANY family member (independent of SSN)
      let rmdFound = false;
      fam.forEach(m => {
        if (m.is_rmd_owner) {
          if (rmdFound) {
            m.is_rmd_owner = false;
          } else {
            rmdFound = true;
          }
        }
      });
      if (!rmdFound && fam.length > 0) {
        fam[0].is_rmd_owner = true;
      }

      // Sanitize work_plan uniformly on every family member
      fam.forEach(m => {
        const msAge = m.milestone_age || 85;
        m.work_plan = sanitizeWorkPlanJs(m.work_plan, msAge);
      });

      // Populate investors (SSN contributors, 0..2)
      const contributors = fam.filter(m => m.is_ssn_contributor).slice(0, 2);
      _projDraft.investors = contributors.map((m, idx) => {
        const msAge = m.milestone_age || 85;
        return {
          id: m.id || `investor_${idx + 1}`,
          name: m.name || `Investor ${idx + 1}`,
          birth_date: m.birth_date || "",
          milestone_age: msAge,
          is_rmd_owner: Boolean(m.is_rmd_owner),
          ssn: m.ssn ? Object.assign({}, m.ssn) : {
            default_claim_age: 67,
            external_file: ""
          },
          work_plan: JSON.parse(JSON.stringify(m.work_plan)),
        };
      });

      // Populate non-SSN members (preserving milestone_age, is_rmd_owner, and work_plan)
      const nonContributors = fam.filter(m => !m.is_ssn_contributor);
      _projDraft.beneficiaries = nonContributors.map((m, idx) => ({
        id: m.id || (m.name ? m.name.toLowerCase().replace(/\s+/g, "_") : `member_${idx + 1}`),
        name: m.name || "",
        birth_date: m.birth_date || "",
        milestone_age: m.milestone_age || 85,
        is_rmd_owner: Boolean(m.is_rmd_owner),
        work_plan: JSON.parse(JSON.stringify(m.work_plan)),
      }));
      _syncTaxControlsFromDraft();
    }

    function _getSelectedFamilyMember() {
      if (!_projDraft) return null;
      if (!Array.isArray(_projDraft.family_members) || _projDraft.family_members.length === 0) {
        _initFamilyMembersFromDraft();
      }
      if (!_projDraft.family_members || _projDraft.family_members.length === 0) return null;
      if (_selectedFamilyIdx >= _projDraft.family_members.length) {
        _selectedFamilyIdx = Math.max(0, _projDraft.family_members.length - 1);
      }
      _selectedInvestorIdx = _selectedFamilyIdx;
      return _projDraft.family_members[_selectedFamilyIdx];
    }

    function _syncCurrentFamilyMemberFromDOM(updateTopList = false) {
      if (!_projDraft || !Array.isArray(_projDraft.family_members)) return;
      const mem = _projDraft.family_members[_selectedFamilyIdx];
      if (!mem) return;

      const nameEl = document.getElementById("proj-cur-fam-name");
      const bdayEl = document.getElementById("proj-cur-fam-bday");
      const msEl = document.getElementById("proj-cur-fam-ms");
      const claimEl = document.getElementById("proj-cur-fam-claim");

      if (nameEl) mem.name = nameEl.value.trim();
      if (bdayEl) {
        mem.birth_date = bdayEl.value.trim();
        delete mem.birthdate;
        if (mem.work_plan && mem.work_plan.is_working && /^\d{4}-\d{2}/.test(mem.birth_date)) {
          const bp = mem.birth_date.split("-");
          const bYr = parseInt(bp[0], 10);
          const bMo = Math.max(1, Math.min(12, parseInt(bp[1], 10) || 1));
          const msNow = Math.max(65, Math.min(105, parseInt(msEl ? msEl.value : mem.milestone_age, 10) || 85));
          const curAgeNow = Math.max(18, ageOnIso(bYr, bMo, 1, TODAY_ISO, 0));
          if (mem.work_plan.default_retirement_age <= curAgeNow) {
            mem.work_plan.default_retirement_age = Math.min(msNow, Math.max(65, curAgeNow + 1));
            const retInp = document.getElementById("proj-cur-fam-ret-age");
            if (retInp) {
              retInp.min = String(Math.min(msNow, curAgeNow + 1));
              retInp.value = String(mem.work_plan.default_retirement_age);
            }
          }
        }
      }
      if (msEl) {
        mem.milestone_age = Math.max(65, Math.min(105, parseInt(msEl.value, 10) || 85));
        if (mem.work_plan && typeof mem.work_plan === "object" && mem.work_plan.default_retirement_age > mem.milestone_age) {
          mem.work_plan.default_retirement_age = mem.milestone_age;
          const retInp = document.getElementById("proj-cur-fam-ret-age");
          if (retInp) {
            retInp.max = String(mem.milestone_age);
            retInp.value = String(mem.milestone_age);
          }
        }
      }

      if (!mem.ssn) mem.ssn = {};
      if (claimEl) {
        mem.ssn.default_claim_age = Math.max(62, Math.min(70, parseInt(claimEl.value, 10) || 67));
      }

      _syncInvestorsAndBeneficiariesFromFamily();
      if (typeof syncFamilyCollapsibleHeaders === "function") syncFamilyCollapsibleHeaders();

      if (updateTopList) {
        const dt = document.getElementById("proj-family-detail-title");
        if (dt) dt.textContent = `Edit Details: ${mem.name || `Family Member #${_selectedFamilyIdx + 1}`}`;
        renderProjectFamilyListOnly();
        _scheduleProjectAutoSave(400);
      }
    }

    function _syncCurrentInvestorFromDOM(updateTopList = false) {
      _syncCurrentFamilyMemberFromDOM(updateTopList);
    }

    function _syncProjDraftFromDOM() {
      if (!_projDraft) return;
      const hEl = document.getElementById("proj-household-name");
      if (hEl) _projDraft.household_name = hEl.value.trim();

      const burnInp = document.getElementById("proj-burn-input");
      if (burnInp) {
        const clean = String(burnInp.value || "").replace(/[^0-9]/g, "");
        if (clean) {
          const num = parseInt(clean, 10);
          _projDraft.reference_annual_burn = num;
          _projDraft.retirement_annual_burn = num;
        } else {
          _projDraft.reference_annual_burn = null;
          _projDraft.retirement_annual_burn = null;
        }
      }

      const stateSel = document.getElementById("proj-tax-state");
      const ltcgInp = document.getElementById("proj-tax-ltcg");
      const ordInp = document.getElementById("proj-tax-ordinary");
      const iraInp = document.getElementById("proj-tax-ira");
      if (stateSel || ltcgInp || ordInp || iraInp) {
        if (!_projDraft.tax_rates || typeof _projDraft.tax_rates !== "object") {
          _projDraft.tax_rates = {};
        }
        const parsePctDom = (el) => {
          if (!el) return undefined;
          const raw = String(el.value || "").trim();
          if (raw === "") return null;
          const num = parseFloat(raw);
          return Number.isFinite(num) ? _roundTaxDec(Math.max(0, Math.min(99.9, num)) / 100.0) : null;
        };
        if (stateSel) _projDraft.tax_rates.state_code = String(stateSel.value || "").trim().toUpperCase();
        const lv = parsePctDom(ltcgInp);
        const ov = parsePctDom(ordInp);
        const iv = parsePctDom(iraInp);
        if (lv !== undefined) _projDraft.tax_rates.ltcg_top = lv;
        if (ov !== undefined) {
          _projDraft.tax_rates.ordinary_top = ov;
          _projDraft.sim_cash_interest_tax_rate = ov;
        }
        if (iv !== undefined) _projDraft.sim_ira_ordinary_tax_rate = iv;
        const mfjChk = document.getElementById("proj-tax-mfj-check");
        const floorSel = document.getElementById("proj-tax-floor-select");
        const famStats = _draftFamilyStats(_projDraft);
        if (!famStats.mfjEligible) {
          _projDraft.tax_rates.married_filing_jointly = false;
        } else if (mfjChk) {
          _projDraft.tax_rates.married_filing_jointly = Boolean(mfjChk.checked);
        }
        if (floorSel) {
          _projDraft.tax_rates.min_tax_bracket_floor = String(floorSel.value || "none").toLowerCase();
        }
      }

      if (Array.isArray(_projDraft.symbols) && _projDraft.symbols.length > 0) {
        _projDraft.primary_equity_symbol = _projDraft.symbols[0];
      }

      _syncCurrentFamilyMemberFromDOM(false);
      onProjectPromptFieldInput(false);
    }

    function selectProjectFamilyMember(idx) {
      _syncCurrentFamilyMemberFromDOM(false);
      _selectedFamilyIdx = idx;
      _selectedInvestorIdx = idx;
      const mem = _projDraft && _projDraft.family_members ? _projDraft.family_members[idx] : null;
      _famSsnExpanded = Boolean(mem && mem.is_ssn_contributor);
      renderProjectFamilyMembers();
    }

    function selectProjectInvestor(idx) {
      selectProjectFamilyMember(idx);
    }

    function toggleFamilySsnAccordion() {
      _famSsnExpanded = !_famSsnExpanded;
      const bodyEl = document.getElementById("proj-fam-ssn-body");
      const chevronEl = document.getElementById("proj-fam-ssn-chevron");
      const summaryStateEl = document.getElementById("proj-fam-ssn-summary-state");
      if (bodyEl) bodyEl.style.display = _famSsnExpanded ? "block" : "none";
      if (chevronEl) chevronEl.style.transform = _famSsnExpanded ? "rotate(180deg)" : "rotate(0deg)";
      if (summaryStateEl) summaryStateEl.textContent = _famSsnExpanded ? "Hide" : "Show";
    }

    function onToggleFamilySsnContribution(checked) {
      if (!_projDraft || !Array.isArray(_projDraft.family_members)) return;
      const mem = _projDraft.family_members[_selectedFamilyIdx];
      if (!mem) return;

      if (checked) {
        const existingCount = _projDraft.family_members.filter((m, idx) => idx !== _selectedFamilyIdx && m.is_ssn_contributor).length;
        if (existingCount >= 2) {
          alert("A maximum of 2 family members can contribute Social Security. Please disable SSN contribution on another member first.");
          const cb = document.getElementById("proj-cur-fam-ssn-enabled");
          if (cb) cb.checked = false;
          return;
        }
        mem.is_ssn_contributor = true;
        _famSsnExpanded = true;
        const pid = _editingPortfolioId || (_portfoliosBundle && _portfoliosBundle.active_portfolio_id) || "default";
        const mId = mem.id || `idx_${_selectedFamilyIdx}`;
        _projSectionExplicitOpen.set(`family:ssn:${pid}:${mId}`, true);
      } else {
        mem.is_ssn_contributor = false;
      }

      _syncInvestorsAndBeneficiariesFromFamily();
      renderProjectFamilyMembers();
      _scheduleProjectAutoSave(0);
    }

    function onToggleFamilyRmdOwner(checked) {
      if (!_projDraft || !Array.isArray(_projDraft.family_members)) return;
      const mem = _projDraft.family_members[_selectedFamilyIdx];
      if (!mem) return;

      if (checked) {
        mem.is_rmd_owner = true;
        _projDraft.family_members.forEach((otherMem, oIdx) => {
          if (oIdx !== _selectedFamilyIdx) {
            otherMem.is_rmd_owner = false;
            delete otherMem.rmd_owner;
          }
        });
      } else {
        const otherMem = _projDraft.family_members.find((_m, idx) => idx !== _selectedFamilyIdx);
        if (otherMem) {
          mem.is_rmd_owner = false;
          otherMem.is_rmd_owner = true;
        } else {
          mem.is_rmd_owner = true;
          const cb = document.getElementById("proj-cur-fam-rmd");
          if (cb) cb.checked = true;
          return;
        }
      }

      _syncInvestorsAndBeneficiariesFromFamily();
      renderProjectFamilyMembers();
      _scheduleProjectAutoSave(0);
    }

    function _evaluateFamilyMemberSections(mem, isSsnContributor, contribNum) {
      const mName = String((mem && mem.name) || "").trim();
      const bDate = String((mem && (mem.birth_date || mem.birthdate)) || "").trim();
      const msAge = Math.max(65, Math.min(105, parseInt(mem && mem.milestone_age, 10) || 85));
      const validDob = /^\d{4}-\d{2}(?:-\d{2})?$/.test(bDate);
      const bYrNum = /^\d{4}/.test(bDate) ? parseInt(bDate.slice(0, 4), 10) : 1975;
      const rmdAgeNum = bYrNum >= 1960 ? 75 : 73;
      const isRmd = Boolean(mem && (mem.is_rmd_owner != null ? mem.is_rmd_owner : mem.rmd_owner));

      // 1. Identity & Milestone
      const idStatus = (mName && validDob) ? "completed" : "incomplete";
      const idSummary = `${mName || "Name required"} \u2022 ${validDob ? `Born ${bDate}` : "Birthdate required"} \u2022 Milestone Age ${msAge}`;
      const idTip = (mName && validDob)
        ? "Member name, birthdate, and milestone age are configured."
        : "Enter both a member name and a valid birthdate.";

      // 2. Work & Pre-Retirement Plan (uniform across ALL family members)
      const wp = sanitizeWorkPlanJs(mem && mem.work_plan, msAge);
      const eligIds = new Set(_getEligibleWorkPlanAssets().map(a => a.id));
      const rows = Array.isArray(wp.contributions) ? wp.contributions : [];
      const missingTarget = wp.is_working && rows.some(
        r => (Number(r.employee_annual || 0) > 0 || Number(r.employer_match_annual || 0) > 0) && !String(r.target_asset_id || "").trim()
      );
      const staleTarget = wp.is_working && rows.some(
        r => String(r.target_asset_id || "").trim() && !eligIds.has(String(r.target_asset_id).trim())
      );
      const bd = _computeWorkPlanPaycheckBreakdown(wp, _projDraft);
      const totContrib = bd.totalGrossAccountDeposits;
      const hasBudgetImpact = Boolean(wp.covers_full_burn || bd.houseContrib > 0);
      const rawRetAge = (mem && mem.work_plan && mem.work_plan.default_retirement_age != null)
        ? Number(mem.work_plan.default_retirement_age)
        : wp.default_retirement_age;
      let workEval;
      if (!wp.is_working) {
        workEval = {
          status: "completed",
          summary: "Not Working",
          tip: "Member is not currently contributing paycheck income or ongoing account deposits.",
        };
      } else if (missingTarget || rawRetAge > msAge || rawRetAge < 18) {
        workEval = {
          status: "incomplete",
          summary: `Working \u2192 Age ${wp.default_retirement_age} \u2022 Target holding required`,
          tip: "Select an ingested target holding for every non-zero contribution row (and keep Target retirement age <= Milestone Age).",
        };
      } else if (staleTarget) {
        workEval = {
          status: "needs_review",
          summary: `Working \u2192 Age ${wp.default_retirement_age} \u2022 Missing holding target`,
          tip: "One or more targeted holdings are missing from current statements.",
        };
      } else if (bd.overAllocated || (!bd.q1Answered && bd.hasConfiguredContribs)) {
        workEval = {
          status: "needs_review",
          summary: `Working \u2192 Age ${wp.default_retirement_age} \u2022 ${!bd.q1Answered ? "Enter take-home income" : "Allocations exceed paycheck"}`,
          tip: !bd.q1Answered
            ? "Enter annual take-home (after-tax) income in Step 1 to reconcile configured retirement contributions."
            : `Household contribution (${fmtUSD0(bd.houseContrib)}) + after-tax employee retirement contributions (${fmtUSD0(Math.round(bd.empAfterTaxTotal))}) exceed annual take-home pay (${fmtUSD0(bd.totalPaycheck)}).`,
        };
      } else if (!bd.q1Answered && !wp.covers_full_burn && bd.houseContrib <= 0) {
        workEval = {
          status: "needs_review",
          summary: `Working \u2192 Age ${wp.default_retirement_age} \u2022 Enter annual take-home income`,
          tip: "Characterize annual take-home (after-tax) income and household budget contribution, or switch to Not Working.",
        };
      } else if (bd.q1Answered && !bd.q2Answered) {
        workEval = {
          status: "needs_review",
          summary: `Working \u2192 Age ${wp.default_retirement_age} \u2022 Take-home ${fmtUSD0(bd.totalPaycheck)}/yr \u2022 Set budget contribution`,
          tip: "Specify how much of this paycheck contributes toward the household's monthly budget in Step 2.",
        };
      } else if (!hasBudgetImpact && totContrib <= 0) {
        workEval = {
          status: "needs_review",
          summary: `Working \u2192 Age ${wp.default_retirement_age} \u2022 $0 budget & $0 retirement savings`,
          tip: "Working is enabled with $0 household budget contribution and $0 retirement savings \u2014 allocate paycheck or switch to Not Working.",
        };
      } else {
        const burnPart = (wp.covers_full_burn && !bd.q1Answered && Number(wp.annual_budget_offset || 0) <= 0)
          ? "100% burn covered"
          : (bd.houseContrib > 0 ? `${fmtUSD0(bd.houseContrib)}/yr house budget` : "$0 house budget");
        const contribPart = totContrib > 0 ? ` \u2022 ${fmtUSD0(totContrib)}/yr retirement savings` : "";
        workEval = {
          status: "completed",
          summary: `Working \u2192 Age ${wp.default_retirement_age} \u2022 ${burnPart}${contribPart}`,
          tip: "Target retirement age, take-home paycheck, household budget contribution, and retirement savings are configured.",
        };
      }

      // 3. Social Security & RMD (independent of Working)
      const ssn = (mem && mem.ssn) || {};
      const claimAge = ssn.default_claim_age || 67;
      const hasExplicitEarnings = ssn.annual_earnings && Object.keys(ssn.annual_earnings).length > 0;
      const ssaFile = ssn.external_file || ssn.ssa_data_file || "";
      const hasXml = Boolean(ssaFile || hasExplicitEarnings);
      const xmlFileName = ssaFile ? ssaFile.split("/").pop() : (hasXml ? (ssn.statement_file || "social-security-statement.xml") : "");
      const xmlDob = ssn.statement_dob || "";
      const showSyncDob = Boolean(hasXml && xmlDob && bDate && xmlDob !== bDate);

      let ssnEval;
      if (!isSsnContributor) {
        ssnEval = {
          status: "completed",
          summary: `Not contributing SSN${isRmd ? ` \u2022 RMD Owner (${rmdAgeNum}+)` : ""}`,
          tip: "Social Security contribution is disabled for this family member.",
        };
      } else if (!hasXml || showSyncDob) {
        ssnEval = {
          status: "needs_review",
          summary: `Claim Age ${claimAge}${isRmd ? ` \u2022 RMD Owner (${rmdAgeNum}+)` : ""} \u2022 ${!hasXml ? "No ssa.gov XML ($0 PIA)" : "Birthdate differs from XML"}`,
          tip: !hasXml
            ? "No official ssa.gov XML statement uploaded yet ($0 Social Security benefit currently modeled)."
            : "Profile birthdate differs from ssa.gov XML statement birthdate \u2014 expand to review or sync.",
        };
      } else {
        ssnEval = {
          status: "completed",
          summary: `Claim Age ${claimAge}${isRmd ? ` \u2022 RMD Owner (${rmdAgeNum}+)` : ""} \u2022 XML: ${xmlFileName}`,
          tip: "Official ssa.gov XML statement and claiming defaults are configured.",
        };
      }

      const allStatuses = [idStatus, workEval.status, ssnEval.status];
      let overall = "completed";
      if (allStatuses.includes("incomplete")) overall = "incomplete";
      else if (allStatuses.includes("needs_review")) overall = "needs_review";

      return {
        identity: { status: idStatus, summary: idSummary, tip: idTip },
        work: workEval,
        ssn: ssnEval,
        overall,
      };
    }

    function syncFamilyCollapsibleHeaders() {
      if (!_projDraft || !Array.isArray(_projDraft.family_members)) return;
      const curMem = _projDraft.family_members[_selectedFamilyIdx];
      if (!curMem) return;
      let contribCount = 0;
      let curContribNum = null;
      _projDraft.family_members.forEach((m, idx) => {
        if (m.is_ssn_contributor && contribCount < 2) {
          contribCount++;
          if (idx === _selectedFamilyIdx) curContribNum = contribCount;
        }
      });
      const isSsn = Boolean(curMem.is_ssn_contributor);
      const pid = _editingPortfolioId || (_portfoliosBundle && _portfoliosBundle.active_portfolio_id) || "default";
      const mId = curMem.id || `idx_${_selectedFamilyIdx}`;
      const ev = _evaluateFamilyMemberSections(curMem, isSsn, curContribNum);

      _applyProjRowDOM(
        document.getElementById("proj-fam-sec-identity"),
        document.getElementById("proj-fam-sec-identity-summary"),
        document.getElementById("proj-fam-sec-identity-pill"),
        `family:identity:${pid}:${mId}`,
        ev.identity.status,
        ev.identity.summary,
        ev.identity.tip
      );
      if (ev.work) {
        _applyProjRowDOM(
          document.getElementById("proj-fam-sec-work"),
          document.getElementById("proj-fam-sec-work-summary"),
          document.getElementById("proj-fam-sec-work-pill"),
          `family:work:${pid}:${mId}`,
          ev.work.status,
          ev.work.summary,
          ev.work.tip
        );
      }
      _applyProjRowDOM(
        document.getElementById("proj-fam-sec-ssn"),
        document.getElementById("proj-fam-sec-ssn-summary"),
        document.getElementById("proj-fam-sec-ssn-pill"),
        `family:ssn:${pid}:${mId}`,
        ev.ssn.status,
        ev.ssn.summary,
        ev.ssn.tip
      );
    }

    function _milestoneAgeTooltipHtml() {
      return (
        `<div style="font-weight:700;color:#38bdf8;margin-bottom:4px;">Milestone Age — How It Drives the Simulation</div>` +
        `<div style="line-height:1.45;margin-bottom:6px;">Target planning horizon age (65–105, default 85). Used in four places:</div>` +
        `<ul style="margin:0;padding-left:16px;font-size:11.2px;line-height:1.45;color:#cbd5e1;">` +
          `<li><b style="color:#fcd34d;">&#9733; Headline KPIs &amp; Optimization:</b> For the primary horizon member (first SSN contributor, or RMD Owner when no one contributes SSN), sets the evaluation age for <b>Milestone Wealth</b>, <b>Funded / Solvency %</b>, <b>Tax Efficiency</b>, <b>Auto-Optimize</b>, and <b>Goal-Seek</b>.</li>` +
          `<li><b style="color:#f8fafc;">Chart Markers:</b> Draws vertical milestone marker lines and timeline capsules on Retirement Simulator, SSN Visualizer, and Saved Scenario charts.</li>` +
          `<li><b style="color:#7dd3fc;">Stop-Working Ceiling:</b> Caps this member's <b>Target retirement age</b> and per-Scenario Stop-Working slider (<code>Target Retirement Age &le; Milestone Age</code>).</li>` +
          `<li><b style="color:#94a3b8;">Checkpoint, Not Cutoff:</b> Does <i>not</i> truncate the simulation — the engine always runs the full 40-year trajectory and 100-year solvency check.</li>` +
        `</ul>`
      );
    }

    function _targetRetirementAgeTooltipHtml() {
      return (
        `<div style="font-weight:700;color:#38bdf8;margin-bottom:4px;">Target Retirement Age</div>` +
        `<div style="line-height:1.45;margin-bottom:6px;">Monthly paycheck income modeling &mdash; both household budget contributions and ongoing retirement/investment account deposits &mdash; stops once this member reaches their target retirement age.</div>` +
        `<div style="line-height:1.45;color:#cbd5e1;font-size:11.2px;">When they reach their target retirement age, their working status will automatically change to <b>Not Working</b>. Capped at <b>Milestone Age</b>, and adjustable per Scenario in Simulator Overrides.</div>`
      );
    }

    function _retirementSavingsTooltipHtml(ordRateDec) {
      const pct = Math.round(Math.max(0, Math.min(1, Number(ordRateDec || 0))) * 1000) / 10;
      return (
        `<div style="font-weight:700;color:#38bdf8;margin-bottom:4px;">After-Tax Paycheck &amp; Retirement Savings Breakdown</div>` +
        `<ul style="margin:0;padding-left:16px;font-size:11.2px;line-height:1.45;color:#cbd5e1;">` +
          `<li><b>Total Paycheck:</b> Current annual after-tax take-home income (Step 1).</li>` +
          `<li><b>House Contribution:</b> Paycheck amount funding the household's monthly spending budget (Step 2).</li>` +
          `<li><b>Retirement Contributions:</b> After-tax paycheck cost of Employee contributions &mdash; Pre-Tax 401(k)/Traditional IRA contributions are tax-adjusted using your portfolio's ordinary tax rate (<code>&times; (1 &minus; ${pct}%)</code>), while Roth &amp; Taxable contributions deduct 1:1. <b>Employer Match</b> is added to the holding without reducing remaining take-home pay.</li>` +
          `<li><b>Remaining:</b> Unallocated after-tax paycheck (<code>Total Paycheck &minus; House Contribution &minus; Retirement Contributions</code>).</li>` +
        `</ul>`
      );
    }

    function _getDraftIraOrdinaryTaxRate(draftCfg, assetObj) {
      const d = draftCfg || _projDraft || PROJECT_CFG || {};
      if (d.sim_ira_ordinary_tax_rate != null && Number.isFinite(Number(d.sim_ira_ordinary_tax_rate))) {
        return Math.max(0, Math.min(0.99, Number(d.sim_ira_ordinary_tax_rate)));
      }
      const tr = (d && d.tax_rates) || (PROJECT_CFG && PROJECT_CFG.tax_rates) || {};
      if (tr.ordinary_top != null && Number.isFinite(Number(tr.ordinary_top))) {
        const niit = tr.niit_rate != null && Number.isFinite(Number(tr.niit_rate)) ? Number(tr.niit_rate) : 0.038;
        return Math.max(0, Math.min(0.99, Number(tr.ordinary_top) - niit));
      }
      if (PROJECT_CFG && PROJECT_CFG.sim_ira_ordinary_tax_rate != null && Number.isFinite(Number(PROJECT_CFG.sim_ira_ordinary_tax_rate))) {
        return Math.max(0, Math.min(0.99, Number(PROJECT_CFG.sim_ira_ordinary_tax_rate)));
      }
      if (assetObj && assetObj.tax_rate != null && Number.isFinite(Number(assetObj.tax_rate))) {
        return Math.max(0, Math.min(0.99, Number(assetObj.tax_rate)));
      }
      return 0;
    }

    function _computeWorkPlanPaycheckBreakdown(wp, draftCfg) {
      const d = draftCfg || _projDraft || PROJECT_CFG || {};
      const refRaw = d.reference_annual_burn ?? d.retirement_annual_burn ?? (PROJECT_CFG && (PROJECT_CFG.reference_annual_burn ?? PROJECT_CFG.retirement_annual_burn));
      const refBurn = Number.isFinite(Number(refRaw)) && Number(refRaw) > 0 ? Math.round(Number(refRaw)) : 0;
      const totalPaycheck = Math.max(0, Number((wp && wp.annual_take_home) || 0));
      const rawOffset = Math.max(0, Number((wp && wp.annual_budget_offset) || 0));
      const houseContrib = Boolean(wp && wp.covers_full_burn && totalPaycheck <= 0 && rawOffset <= 0)
        ? refBurn
        : rawOffset;
      const rows = (wp && Array.isArray(wp.contributions)) ? wp.contributions : [];
      const eligMap = new Map(_getEligibleWorkPlanAssets().map(a => [a.id, a]));
      const baseOrdRate = _getDraftIraOrdinaryTaxRate(d, null);

      let empGrossTotal = 0;
      let empAfterTaxTotal = 0;
      let employerMatchTotal = 0;
      let hasPreTaxEmployee = false;
      const rowDetails = rows.map(r => {
        const tid = String((r && r.target_asset_id) || "").trim();
        const asset = eligMap.get(tid) || null;
        const isPreTax = Boolean(asset && String(asset.tax_model || "") === "ORDINARY_FULL");
        const rowOrdRate = isPreTax ? _getDraftIraOrdinaryTaxRate(d, asset) : 0;
        const empGross = Math.max(0, Number((r && r.employee_annual) || 0));
        const matchGross = Math.max(0, Number((r && r.employer_match_annual) || 0));
        const empAfterTax = isPreTax ? (empGross * Math.max(0, Math.min(1, 1 - rowOrdRate))) : empGross;
        if (isPreTax && empGross > 0) hasPreTaxEmployee = true;
        empGrossTotal += empGross;
        empAfterTaxTotal += empAfterTax;
        employerMatchTotal += matchGross;
        return { isPreTax, rowOrdRate, empGross, empAfterTax, matchGross };
      });

      const totalGrossAccountDeposits = empGrossTotal + employerMatchTotal;
      const hasConfiguredContribs = Boolean(rows.length > 0 || totalGrossAccountDeposits > 0);
      const q1Answered = totalPaycheck > 0;
      const q2Answered = Boolean(
        q1Answered && (Boolean(wp && wp.budget_answered) || rawOffset > 0 || hasConfiguredContribs)
      );
      const remainingBeforeRetirement = totalPaycheck - houseContrib;
      const remainingAfterTax = totalPaycheck - houseContrib - empAfterTaxTotal;
      const overAllocated = Boolean(q1Answered && (houseContrib > totalPaycheck + 0.5 || remainingAfterTax < -0.5));
      const showRetirementSavingsBar = Boolean(
        hasConfiguredContribs || (q1Answered && q2Answered && remainingBeforeRetirement > 0)
      );

      return {
        refBurn,
        totalPaycheck,
        houseContrib,
        q1Answered,
        q2Answered,
        remainingBeforeRetirement,
        baseOrdRate,
        empGrossTotal,
        empAfterTaxTotal,
        employerMatchTotal,
        totalGrossAccountDeposits,
        hasPreTaxEmployee,
        hasConfiguredContribs,
        remainingAfterTax,
        overAllocated,
        showRetirementSavingsBar,
        rowDetails,
      };
    }

    /** Re-render only the top family selection list from `_projDraft.family_members`. */
    function renderProjectFamilyListOnly() {
      const listEl = document.getElementById("proj-family-list");
      if (!listEl || !_projDraft || !Array.isArray(_projDraft.family_members)) return;
      const famArr = _projDraft.family_members;

      // Assign SSN Contributor numbers 1 and 2
      let contribCounter = 0;
      const contribNumbers = {};
      famArr.forEach((m, idx) => {
        if (m.is_ssn_contributor && contribCounter < 2) {
          contribCounter++;
          contribNumbers[idx] = contribCounter;
        }
      });

      listEl.innerHTML = famArr.map((mem, idx) => {
        const isSelected = idx === _selectedFamilyIdx;
        const isRmd = Boolean(mem.is_rmd_owner != null ? mem.is_rmd_owner : mem.rmd_owner);
        const isSsn = Boolean(mem.is_ssn_contributor);
        const contribNum = contribNumbers[idx];
        const bDate = mem.birth_date || mem.birthdate || "";
        const bYrNum = /^\d{4}/.test(String(bDate)) ? parseInt(String(bDate).slice(0, 4), 10) : 1975;
        const rmdAgeNum = bYrNum >= 1960 ? 75 : 73;
        const ssn = mem.ssn || {};
        const claimAge = ssn.default_claim_age || 67;
        const birthSpanHtml = bDate
          ? `<span class="muted mono" style="font-size:12px;">Birth: ${escHtml(bDate)}</span>`
          : `<span class="pill pill-rose mono" style="font-size:9.5px;">Birthdate required</span>`;
        const memEval = _evaluateFamilyMemberSections(mem, isSsn, contribNum);
        const overallSpec = _statusCapsuleSpec(memEval.overall);

        return `
          <div class="proj-symbol-row ${isSelected ? 'active-investor-row' : ''}" style="cursor:pointer;display:flex;align-items:center;justify-content:space-between;padding:10px 14px;${isSelected ? 'background:rgba(56,189,248,0.12);border-color:#38bdf8;' : ''}" onclick="selectProjectFamilyMember(${idx})">
            <div style="display:flex;align-items:center;gap:12px;min-width:0;flex:1;flex-wrap:wrap;">
              <span class="mono" style="font-weight:700;font-size:14px;color:#f8fafc;">${escHtml(mem.name || `Member ${idx + 1}`)}</span>
              ${birthSpanHtml}
              <span class="muted mono has-tip" style="font-size:12px;cursor:help;" data-tip="${escHtml(_milestoneAgeTooltipHtml())}">Milestone: Age ${mem.milestone_age || 85} <span class="info-dot">i</span></span>
              ${(mem.work_plan && mem.work_plan.is_working) ? `<span class="pill pill-cyan mono" style="font-size:10px;">Working &rarr; Age ${mem.work_plan.default_retirement_age || 65}</span>` : ''}
              ${isSsn ? `<span class="muted mono" style="font-size:12px;">Claim SSN: Age ${claimAge}</span>` : ''}
              ${(isSsn && contribNum) ? `<span class="pill ${contribNum === 1 ? 'pill-blue' : 'pill-green'} mono" style="font-size:10px;">SSN Contributor #${contribNum}</span>` : ''}
              ${isRmd ? `<span class="pill pill-teal mono has-tip" data-tip="&lt;div style='font-weight:700;color:#2dd4bf;margin-bottom:3px;'&gt;Designated RMD Owner&lt;/div&gt;&lt;div&gt;Mandatory annual Traditional IRA distributions begin at age ${rmdAgeNum} based on this member's birth year (IRS SECURE 2.0: 73 for ≤1959, 75 for ≥1960).&lt;/div&gt;" style="font-size:9.5px;cursor:help;">RMD Owner (${rmdAgeNum}+)</span>` : ''}
            </div>
            <div style="display:flex;align-items:center;gap:8px;flex-shrink:0;">
              <span class="${overallSpec.pillCls}" style="font-size:10px;padding:2px 8px;">${overallSpec.label}</span>
              <button type="button" class="btn ${isSelected ? 'btn-primary' : ''}" style="font-size:11.5px;padding:4px 12px;display:flex;align-items:center;gap:5px;" onclick="event.stopPropagation();selectProjectFamilyMember(${idx})">
                ${ICONS.pencil}
                <span>${isSelected ? 'Editing' : 'Edit'}</span>
              </button>
              <button type="button" class="btn-delete-symbol" title="${famArr.length <= 1 ? 'Portfolio must have at least 1 family member' : 'Delete ' + escHtml(mem.name || 'Member')}" ${famArr.length <= 1 ? 'disabled style="opacity:0.25;cursor:not-allowed;"' : ''} onclick="event.stopPropagation();removeProjectFamilyMemberRow(${idx})">
                ${ICONS.trash}
              </button>
            </div>
          </div>
        `;
      }).join("");
    }

    function renderProjectInvestorsListOnly() {
      renderProjectFamilyListOnly();
    }

    function _getEligibleWorkPlanAssets() {
      const todayS = String(TODAY_ISO || "2026-01-01");
      return (BASE_SIM_ASSETS || []).filter(a => {
        if (!a || !a.id || a.is_unvested_goog) return false;
        if (a.tax_model === "ANNUITY_LIFO" && a.maturity_iso && String(a.maturity_iso) > todayS) return false;
        return true;
      });
    }

    function _renderWorkPlanAssetOptions(selectedId, allowEmptyLabel = null) {
      const elig = _getEligibleWorkPlanAssets();
      const sel = String(selectedId || "").trim();
      const hasSel = !sel || elig.some(a => a.id === sel);
      const groups = { pretax: [], roth: [], taxable: [] };
      elig.forEach(a => {
        const tm = String(a.tax_model || "");
        const lbl = a.short_title || a.label || a.title || a.id;
        const optHtml = `<option value="${escHtml(a.id)}" ${a.id === sel ? "selected" : ""}>${escHtml(lbl)} (${fmtUSD0(Number(a.current_value) || 0)})</option>`;
        if (tm === "ORDINARY_FULL") groups.pretax.push(optHtml);
        else if (tm === "TAX_FREE") groups.roth.push(optHtml);
        else groups.taxable.push(optHtml);
      });
      let out = "";
      if (allowEmptyLabel !== null) {
        out += `<option value="" ${!sel ? "selected" : ""}>${escHtml(allowEmptyLabel)}</option>`;
      } else {
        out += `<option value="" ${!sel ? "selected" : ""}>Select ingested holding...</option>`;
      }
      if (!hasSel && sel) {
        out += `<option value="${escHtml(sel)}" selected>&#9888; Missing holding (${escHtml(sel)})</option>`;
      }
      if (groups.pretax.length) out += `<optgroup label="Pre-Tax 401(k) / Traditional IRA">${groups.pretax.join("")}</optgroup>`;
      if (groups.roth.length) out += `<optgroup label="Roth 401(k) / Roth IRA (Tax-Free)">${groups.roth.join("")}</optgroup>`;
      if (groups.taxable.length) out += `<optgroup label="Taxable Brokerage / Cash">${groups.taxable.join("")}</optgroup>`;
      return { html: out, missingSelected: Boolean(!hasSel && sel), count: elig.length };
    }

    function _renderRetSavingsBreakdownHtml(bd) {
      const remRounded = Math.round(bd.remainingAfterTax);
      const isNeg = bd.remainingAfterTax < -0.5;
      const remStr = isNeg
        ? `-${fmtUSD0(Math.abs(remRounded))}`
        : fmtUSD0(Math.max(0, remRounded));
      const remColor = isNeg ? "#fda4af" : (remRounded > 0 ? "#34d399" : "#94a3b8");
      const retAfterTaxRounded = Math.round(bd.empAfterTaxTotal);
      const matchBadge = bd.employerMatchTotal > 0
        ? ` <span style="color:#2dd4bf;font-size:10.5px;" title="Employer match added on top of paycheck (+${escHtml(fmtUSD0(bd.employerMatchTotal))}/yr)">(+${escHtml(fmtUSD0(bd.employerMatchTotal))} match)</span>`
        : "";
      const preTaxHint = bd.hasPreTaxEmployee
        ? ` <span class="muted" style="font-size:10px;">(after-tax)</span>`
        : "";
      return (
        `<span style="white-space:nowrap;"><span class="muted">Total Paycheck:</span> <b style="color:#f8fafc;">${escHtml(fmtUSD0(bd.totalPaycheck))}</b></span>` +
        `<span class="proj-collapse-sep">&bull;</span>` +
        `<span style="white-space:nowrap;"><span class="muted">House Contribution:</span> <b style="color:#7dd3fc;">&minus;${escHtml(fmtUSD0(bd.houseContrib))}</b></span>` +
        `<span class="proj-collapse-sep">&bull;</span>` +
        `<span style="white-space:nowrap;"><span class="muted">Retirement Contributions:</span> <b style="color:#c4b5fd;">&minus;${escHtml(fmtUSD0(retAfterTaxRounded))}</b>${preTaxHint}${matchBadge}</span>` +
        `<span class="proj-collapse-sep">&bull;</span>` +
        `<span style="white-space:nowrap;"><span class="muted">Remaining:</span> <b style="color:${remColor};">${escHtml(remStr)}</b></span>`
      );
    }

    function _renderWorkPlanEmpImpactNote(detail) {
      if (!detail || detail.empGross <= 0) {
        return `<span class="muted">Enter $/yr employee contribution</span>`;
      }
      if (detail.isPreTax) {
        const pct = Math.round(detail.rowOrdRate * 1000) / 10;
        return `<span style="color:#c4b5fd;">After-tax paycheck cost: ~${escHtml(fmtUSD0(Math.round(detail.empAfterTax)))}/yr</span> <span class="muted">(${pct}% pre-tax shelter)</span>`;
      }
      return `<span class="muted">After-tax paycheck cost: ${escHtml(fmtUSD0(Math.round(detail.empAfterTax)))}/yr (1:1)</span>`;
    }

    function _formatWorkPlanQ1Sub(bd) {
      if (!bd.q1Answered) {
        return "Characterize current annual after-tax take-home pay ($1 to $1M+ slider, or type higher).";
      }
      return `~${fmtUSD0(Math.round(bd.totalPaycheck / 12))}/mo after-tax take-home income in today's dollars.`;
    }

    function _formatWorkPlanQ2Sub(bd, wp) {
      if (!bd.q1Answered && !(wp && wp.covers_full_burn)) {
        return "Enter Step 1 annual take-home income first.";
      }
      if (wp && wp.covers_full_burn && !bd.q1Answered) {
        return "Legacy 100% burn coverage active — enter Step 1 take-home income to see paycheck breakdown.";
      }
      if (!bd.q2Answered) {
        return "Drag slider, type $/yr, or pick a preset below to allocate toward the household budget.";
      }
      const moStr = `${fmtUSD0(Math.round(bd.houseContrib / 12))}/mo toward household budget`;
      if (bd.refBurn > 0) {
        if (bd.houseContrib >= bd.refBurn) {
          return `${moStr} \u2022 Covers 100% of ${fmtUSD0(bd.refBurn)}/yr reference burn (Scheduled Gifts still execute).`;
        }
        const pct = Math.min(99, Math.round((bd.houseContrib / bd.refBurn) * 100));
        const gap = Math.max(0, bd.refBurn - bd.houseContrib);
        return `${moStr} \u2022 Covers ${pct}% of ${fmtUSD0(bd.refBurn)}/yr burn (portfolio bridges ${fmtUSD0(gap)}/yr).`;
      }
      return `${moStr} (portfolio bridges any remaining monthly spending shortfall).`;
    }

    function _syncWorkPlanLiveDOM(mem) {
      if (!mem || !mem.work_plan) return;
      const msAge = Math.max(18, Math.min(100, parseInt(mem.milestone_age, 10) || 85));
      const wp = sanitizeWorkPlanJs(mem.work_plan, msAge);
      mem.work_plan = wp;
      const bd = _computeWorkPlanPaycheckBreakdown(wp, _projDraft);

      const bDate = String(mem.birth_date || mem.birthdate || "");
      const bParts = bDate.split("-");
      const bYr = /^\d{4}$/.test(bParts[0] || "") ? parseInt(bParts[0], 10) : 1975;
      const bMo = /^\d{1,2}$/.test(bParts[1] || "") ? Math.max(1, Math.min(12, parseInt(bParts[1], 10))) : 1;
      const retYm = `${bYr + wp.default_retirement_age}-${String(bMo).padStart(2, "0")}`;

      const retNoteEl = document.getElementById("proj-wp-ret-ym-note");
      if (retNoteEl) {
        retNoteEl.textContent = `Works through ${retYm} (max Milestone Age ${msAge}; per-Scenario slider in Overrides).`;
      }

      const thInp = document.getElementById("proj-wp-takehome-input");
      if (thInp && document.activeElement !== thInp) {
        thInp.value = bd.totalPaycheck > 0 ? ("$" + Math.round(bd.totalPaycheck).toLocaleString("en-US")) : "";
      }
      const thSlider = document.getElementById("proj-wp-takehome-slider");
      if (thSlider && document.activeElement !== thSlider) {
        thSlider.value = String(Math.min(1000000, Math.max(0, Math.round(bd.totalPaycheck))));
      }
      if (thSlider && thSlider.style) {
        thSlider.style.opacity = bd.totalPaycheck > 0 ? "1" : "0.55";
      }
      const q1Sub = document.getElementById("proj-wp-q1-sub");
      if (q1Sub) q1Sub.textContent = _formatWorkPlanQ1Sub(bd);

      const q2Enabled = Boolean(bd.q1Answered || bd.houseContrib > 0 || wp.covers_full_burn);
      const q2Card = document.getElementById("proj-wp-q2-card");
      if (q2Card && q2Card.style) {
        q2Card.style.opacity = q2Enabled ? "1" : "0.5";
      }
      const sliderMax = Math.max(1000, Math.round(bd.totalPaycheck || bd.houseContrib || bd.refBurn || 100000));
      const sliderStep = sliderMax >= 20000 ? 1000 : 100;
      const bInp = document.getElementById("proj-wp-budget-input");
      if (bInp) {
        bInp.disabled = !q2Enabled;
        if (document.activeElement !== bInp) {
          bInp.value = (bd.q2Answered || bd.houseContrib > 0)
            ? ("$" + Math.round(bd.houseContrib).toLocaleString("en-US"))
            : "";
        }
      }
      const bSlider = document.getElementById("proj-wp-budget-slider");
      if (bSlider) {
        bSlider.disabled = !q2Enabled;
        bSlider.max = String(sliderMax);
        bSlider.step = String(sliderStep);
        if (document.activeElement !== bSlider) {
          bSlider.value = String(Math.min(sliderMax, Math.max(0, Math.round(bd.houseContrib))));
        }
        if (bSlider.style) bSlider.style.opacity = bd.q2Answered ? "1" : "0.55";
      }
      const bMaxLbl = document.getElementById("proj-wp-budget-max-lbl");
      if (bMaxLbl) bMaxLbl.textContent = fmtUSD0(bd.totalPaycheck > 0 ? bd.totalPaycheck : sliderMax);
      const q2Sub = document.getElementById("proj-wp-q2-sub");
      if (q2Sub) q2Sub.textContent = _formatWorkPlanQ2Sub(bd, wp);

      const matchBurnBtn = document.getElementById("proj-wp-match-burn-btn");
      if (matchBurnBtn) {
        matchBurnBtn.disabled = !q2Enabled;
        if (bd.refBurn > 0) {
          const targetAmt = bd.totalPaycheck > 0 ? Math.min(bd.totalPaycheck, bd.refBurn) : bd.refBurn;
          matchBurnBtn.textContent = `Match Household Burn (${fmtUSD0(targetAmt)}/yr)`;
        }
      }
      const fullPayBtn = document.getElementById("proj-wp-full-paycheck-btn");
      if (fullPayBtn) fullPayBtn.disabled = !q2Enabled;
      const zeroPayBtn = document.getElementById("proj-wp-zero-budget-btn");
      if (zeroPayBtn) zeroPayBtn.disabled = !q2Enabled;

      const noRemEl = document.getElementById("proj-wp-no-remaining-note");
      if (noRemEl && noRemEl.style) {
        noRemEl.style.display = (bd.q1Answered && bd.q2Answered && !bd.showRetirementSavingsBar) ? "block" : "none";
      }

      const retRow = document.getElementById("proj-wp-ret-savings-row");
      if (retRow && retRow.style) {
        retRow.style.display = bd.showRetirementSavingsBar ? "" : "none";
        if (retRow.classList) {
          retRow.classList.remove("status-incomplete", "status-needs-review", "status-completed");
          retRow.classList.add(bd.overAllocated ? "status-needs-review" : "status-completed");
        }
      }
      const retBdEl = document.getElementById("proj-wp-ret-savings-breakdown");
      if (retBdEl) {
        retBdEl.innerHTML = _renderRetSavingsBreakdownHtml(bd);
      }
      const overBanner = document.getElementById("proj-wp-overalloc-banner");
      if (overBanner && overBanner.style) {
        if (bd.overAllocated) {
          overBanner.style.display = "block";
          overBanner.innerHTML = `&#9888; House contribution (${escHtml(fmtUSD0(bd.houseContrib))}) + after-tax employee retirement contributions (${escHtml(fmtUSD0(Math.round(bd.empAfterTaxTotal)))}) exceed annual take-home pay (${escHtml(fmtUSD0(bd.totalPaycheck))}) by <b>${escHtml(fmtUSD0(Math.round(Math.abs(bd.remainingAfterTax))))}/yr</b>.`;
        } else {
          overBanner.style.display = "none";
          overBanner.innerHTML = "";
        }
      }

      const detailRoot = document.getElementById("proj-family-detail-content");
      if (detailRoot && typeof detailRoot.querySelectorAll === "function") {
        const impEls = detailRoot.querySelectorAll("[data-wp-emp-impact]");
        impEls.forEach(el => {
          const idx = parseInt(el.getAttribute("data-wp-emp-impact"), 10);
          if (Number.isFinite(idx) && bd.rowDetails[idx]) {
            el.innerHTML = _renderWorkPlanEmpImpactNote(bd.rowDetails[idx]);
          }
        });
      }

      syncFamilyCollapsibleHeaders();
      renderProjectFamilyListOnly();
    }

    function _buildFamilyWorkPlanSectionHtml(curMem, isSsnContributor, curContribNum) {
      if (!curMem) return "";
      const msAge = Math.max(18, Math.min(100, parseInt(curMem.milestone_age, 10) || 85));
      if (!curMem.work_plan || typeof curMem.work_plan !== "object") {
        curMem.work_plan = sanitizeWorkPlanJs(null, msAge);
      }
      const wp = sanitizeWorkPlanJs(curMem.work_plan, msAge);
      curMem.work_plan = wp;

      const bDate = String(curMem.birth_date || curMem.birthdate || "");
      const bParts = bDate.split("-");
      const bYr = /^\d{4}$/.test(bParts[0] || "") ? parseInt(bParts[0], 10) : 1975;
      const bMo = /^\d{1,2}$/.test(bParts[1] || "") ? Math.max(1, Math.min(12, parseInt(bParts[1], 10))) : 1;
      const curAge = Math.max(18, ageOnIso(bYr, bMo, 1, TODAY_ISO, 0));
      const minRetAge = Math.min(msAge, curAge + 1);
      const retYr = bYr + wp.default_retirement_age;
      const retYm = `${retYr}-${String(bMo).padStart(2, "0")}`;
      const eligCount = _getEligibleWorkPlanAssets().length;
      const bd = _computeWorkPlanPaycheckBreakdown(wp, _projDraft);

      const pid = _editingPortfolioId || (_portfoliosBundle && _portfoliosBundle.active_portfolio_id) || "default";
      const mId = curMem.id || `idx_${_selectedFamilyIdx}`;
      const retSavKey = `${pid}:${mId}`;
      const hasMissingRowTarget = (wp.contributions || []).some(
        r => (Number(r.employee_annual || 0) > 0 || Number(r.employer_match_annual || 0) > 0) && !String(r.target_asset_id || "").trim()
      );
      const retSavExpanded = _wpRetSavingsOpen.has(retSavKey)
        ? Boolean(_wpRetSavingsOpen.get(retSavKey))
        : Boolean(hasMissingRowTarget || bd.overAllocated);

      const contribRowsHtml = (wp.contributions || []).map((row, cIdx) => {
        const empOpts = _renderWorkPlanAssetOptions(row.target_asset_id, null);
        const matchOpts = _renderWorkPlanAssetOptions(row.match_target_asset_id, "Same holding as employee");
        const rowDet = bd.rowDetails[cIdx] || { isPreTax: false, rowOrdRate: 0, empGross: 0, empAfterTax: 0, matchGross: 0 };
        return `
          <div style="background:rgba(15,23,42,0.58);border:1px solid ${empOpts.missingSelected ? 'rgba(245,158,11,0.55)' : '#1e293b'};border-radius:8px;padding:10px 12px;display:flex;flex-direction:column;gap:8px;">
            <div style="display:grid;grid-template-columns:1.5fr 1fr 1fr auto;gap:10px;align-items:start;">
              <div>
                <label class="ctrl-lbl" style="font-size:10.5px;display:block;margin-bottom:3px;">Target Retirement / Investment Holding</label>
                <select class="save-name-input mono" data-wp-contrib-field="target_asset_id" data-wp-idx="${cIdx}" style="width:100%;">
                  ${empOpts.html}
                </select>
              </div>
              <div>
                <label class="ctrl-lbl" style="font-size:10.5px;display:block;margin-bottom:3px;">Employee Contribution ($/yr, Today&#39;s $)</label>
                <input type="number" class="save-name-input mono" data-wp-contrib-field="employee_annual" data-wp-idx="${cIdx}" min="0" max="50000000" step="500" value="${Number(row.employee_annual || 0)}" style="width:100%;" />
                <div class="mono" data-wp-emp-impact="${cIdx}" style="font-size:10px;margin-top:3px;line-height:1.35;">${_renderWorkPlanEmpImpactNote(rowDet)}</div>
              </div>
              <div>
                <label class="ctrl-lbl" style="font-size:10.5px;display:block;margin-bottom:3px;">Employer Match ($/yr, Today&#39;s $)</label>
                <input type="number" class="save-name-input mono" data-wp-contrib-field="employer_match_annual" data-wp-idx="${cIdx}" min="0" max="50000000" step="500" value="${Number(row.employer_match_annual || 0)}" style="width:100%;" />
                <div class="muted mono" style="font-size:10px;margin-top:3px;line-height:1.35;">Added to holding (not deducted from paycheck)</div>
              </div>
              <div style="padding-top:19px;">
                <button type="button" class="btn-delete-symbol" data-wp-action="del-contrib" data-wp-idx="${cIdx}" title="Remove contribution target">${ICONS.trash}</button>
              </div>
            </div>
            <div style="display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;">
              <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;">
                <span class="muted" style="font-size:10.5px;">IRS Presets:</span>
                <button type="button" class="cap-btn mono" data-wp-action="preset-emp" data-wp-idx="${cIdx}" data-wp-val="24500" title="2026 IRS 401(k)/403(b) elective deferral limit ($24,500/yr)">401(k) Max ($24.5K)</button>
                <button type="button" class="cap-btn mono" data-wp-action="preset-emp" data-wp-idx="${cIdx}" data-wp-val="7500" title="2026 IRS Traditional/Roth IRA contribution limit ($7,500/yr)">IRA Max ($7.5K)</button>
                <label class="has-tip" data-tip="&lt;b&gt;IRS Age 50+ &amp; SECURE 2.0 Super Catch-Up&lt;/b&gt;&lt;br&gt;Automatically adds +$8,000/yr (Today's $, CPI-indexed) starting in the calendar year this member turns 50, and +$11,250/yr at attained ages 60–63." style="display:inline-flex;align-items:center;gap:5px;font-size:11px;color:#cbd5e1;cursor:pointer;margin-left:4px;">
                  <input type="checkbox" data-wp-contrib-field="age_50_catchup" data-wp-idx="${cIdx}" ${row.age_50_catchup ? "checked" : ""} style="accent-color:#38bdf8;cursor:pointer;" />
                  <span>Age 50+ IRS Catch-Up</span>
                </label>
                ${empOpts.missingSelected ? `<span class="pill pill-amber mono" style="font-size:9.5px;">&#9888; Holding missing from statements (skipped)</span>` : ""}
              </div>
              <div style="display:flex;align-items:center;gap:6px;min-width:220px;">
                <span class="muted" style="font-size:10.5px;white-space:nowrap;">Match Target:</span>
                <select class="save-name-input mono" data-wp-contrib-field="match_target_asset_id" data-wp-idx="${cIdx}" style="font-size:11px;padding:3px 7px;max-width:230px;" title="Optional separate holding for employer match (e.g. Pre-Tax 401(k) when employee contributes to Roth 401(k))">
                  ${matchOpts.html}
                </select>
              </div>
            </div>
          </div>
        `;
      }).join("");

      const wEval = _evaluateFamilyMemberSections(curMem, isSsnContributor, curContribNum).work || {
        status: "completed",
        summary: "Not Working",
        tip: "",
      };
      const wSpec = _statusCapsuleSpec(wEval.status);
      const wKey = `family:work:${pid}:${mId}`;
      const wExp = _projSectionExplicitOpen.has(wKey)
        ? Boolean(_projSectionExplicitOpen.get(wKey))
        : (wEval.status === "incomplete" || wEval.status === "needs_review");
      _projSectionAutoInitialized.add(wKey);

      const q2Enabled = Boolean(bd.q1Answered || bd.houseContrib > 0 || wp.covers_full_burn);
      const sliderBudgetMax = Math.max(1000, Math.round(bd.totalPaycheck || bd.houseContrib || bd.refBurn || 100000));
      const sliderBudgetStep = sliderBudgetMax >= 20000 ? 1000 : 100;
      const takeHomeInputVal = bd.totalPaycheck > 0 ? ("$" + Math.round(bd.totalPaycheck).toLocaleString("en-US")) : "";
      const budgetInputVal = (bd.q2Answered || bd.houseContrib > 0) ? ("$" + Math.round(bd.houseContrib).toLocaleString("en-US")) : "";
      const matchBurnTarget = bd.totalPaycheck > 0 ? Math.min(bd.totalPaycheck, bd.refBurn) : bd.refBurn;

      return `
        <div class="proj-collapsible-row ${wSpec.rowCls} ${wExp ? 'expanded' : ''}" id="proj-fam-sec-work" data-proj-sec="work">
          <div class="proj-collapse-hdr" data-proj-collapse-toggle="family:work">
            <div class="proj-collapse-hdr-left">
              <span class="proj-collapse-arrow" aria-hidden="true">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6"></polyline></svg>
              </span>
              <span class="proj-collapse-title">Work &amp; Pre-Retirement Plan</span>
              <span class="proj-collapse-sep">|</span>
              <span class="proj-collapse-summary mono" id="proj-fam-sec-work-summary">${escHtml(wEval.summary)}</span>
            </div>
            <span class="${wSpec.pillCls}" id="proj-fam-sec-work-pill" data-tip="${escHtml(wEval.tip)}">${wSpec.label}</span>
          </div>
          <div class="proj-collapse-body">
            <div id="proj-fam-work-narrative" class="muted" style="font-size:11.5px;line-height:1.45;">
              ${wp.is_working
                ? "Monthly earnings income can optionally be distributed to your monthly spending budget and portfolio accounts until the target retirement age."
                : "The simulation does not model any paycheck income or contributions to the portfolio for this family member."}
            </div>
            <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:14px;flex-wrap:wrap;">
              <div class="ctrl-group" style="padding-top:2px;">
                <button type="button" class="btn ${wp.is_working ? 'active' : ''}" data-wp-action="set-working" data-wp-val="1">Working</button>
                <button type="button" class="btn ${!wp.is_working ? 'active' : ''}" data-wp-action="set-working" data-wp-val="0">Not Working</button>
              </div>
              ${wp.is_working ? `
                <div style="flex:1;min-width:240px;max-width:380px;background:rgba(15,23,42,0.45);border:1px solid #1e293b;border-radius:8px;padding:9px 12px;">
                  <div style="display:flex;align-items:center;justify-content:space-between;gap:10px;">
                    <label for="proj-cur-fam-ret-age" class="ctrl-lbl" style="font-size:11.5px;display:inline-flex;align-items:center;gap:4px;margin:0;">
                      <span class="has-tip" style="display:inline-flex;align-items:center;gap:3px;cursor:help;" data-tip="${escHtml(_targetRetirementAgeTooltipHtml())}">
                        <span>Target retirement age</span>
                        <span class="info-dot">i</span>
                      </span>
                    </label>
                    <input type="number" id="proj-cur-fam-ret-age" class="save-name-input mono" data-wp-field="default_retirement_age" min="${minRetAge}" max="${msAge}" value="${wp.default_retirement_age}" style="width:92px;text-align:right;" />
                  </div>
                  <div class="muted mono" id="proj-wp-ret-ym-note" style="font-size:10.3px;margin-top:4px;">Works through ${escHtml(retYm)} (max Milestone Age ${msAge}; per-Scenario slider in Overrides).</div>
                </div>
              ` : ""}
            </div>
            ${wp.is_working ? `
              <div style="display:flex;flex-direction:column;gap:11px;">
                <!-- Step 1: Current Annual Take-Home (After-Tax) Income -->
                <div style="background:rgba(15,23,42,0.45);border:1px solid #1e293b;border-radius:8px;padding:11px 14px;display:flex;flex-direction:column;gap:8px;">
                  <div style="display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;">
                    <div style="min-width:200px;flex:1;">
                      <label for="proj-wp-takehome-input" style="display:block;font-size:12px;font-weight:700;color:#f8fafc;margin-bottom:2px;">
                        1. Current annual take-home (after-tax) income
                      </label>
                      <div class="muted mono" id="proj-wp-q1-sub" style="font-size:10.8px;line-height:1.35;">${escHtml(_formatWorkPlanQ1Sub(bd))}</div>
                    </div>
                    <input type="text" id="proj-wp-takehome-input" class="save-name-input mono" data-wp-field="annual_take_home_text" style="width:150px;text-align:right;padding:5px 9px;font-size:12.5px;font-weight:700;color:#38bdf8;background:#0f172a;border:1px solid #334155;border-radius:6px;" value="${escHtml(takeHomeInputVal)}" placeholder="e.g. $180,000" title="Enter current annual take-home (after-tax) income in today's dollars (type any higher amount above $1M)" />
                  </div>
                  <div style="display:flex;align-items:center;gap:10px;width:100%;">
                    <span class="mono muted" style="font-size:11px;flex-shrink:0;">$1</span>
                    <input type="range" id="proj-wp-takehome-slider" data-wp-field="annual_take_home_slider" min="0" max="1000000" step="1000" value="${Math.min(1000000, Math.max(0, Math.round(bd.totalPaycheck)))}" style="flex:1;cursor:pointer;opacity:${bd.totalPaycheck > 0 ? '1' : '0.55'};" />
                    <span class="mono muted" style="font-size:11px;flex-shrink:0;">$1M+</span>
                  </div>
                </div>

                <!-- Step 2: Household Monthly Budget Contribution -->
                <div id="proj-wp-q2-card" style="background:rgba(15,23,42,0.45);border:1px solid #1e293b;border-radius:8px;padding:11px 14px;display:flex;flex-direction:column;gap:8px;opacity:${q2Enabled ? '1' : '0.5'};transition:opacity 0.15s ease;">
                  <div style="display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;">
                    <div style="min-width:200px;flex:1;">
                      <label for="proj-wp-budget-input" style="display:block;font-size:12px;font-weight:700;color:#f8fafc;margin-bottom:2px;">
                        2. How much of their after-tax paycheck contributes to the household&#39;s monthly budget?
                      </label>
                      <div class="muted mono" id="proj-wp-q2-sub" style="font-size:10.8px;line-height:1.35;">${escHtml(_formatWorkPlanQ2Sub(bd, wp))}</div>
                    </div>
                    <input type="text" id="proj-wp-budget-input" class="save-name-input mono" data-wp-field="annual_budget_offset_text" ${!q2Enabled ? "disabled" : ""} style="width:150px;text-align:right;padding:5px 9px;font-size:12.5px;font-weight:700;color:#38bdf8;background:#0f172a;border:1px solid #334155;border-radius:6px;" value="${escHtml(budgetInputVal)}" placeholder="$/yr to budget" title="Annual after-tax paycheck amount contributing toward the household's monthly spending budget" />
                  </div>
                  <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;">
                    <span class="muted" style="font-size:10.5px;">Quick Fill:</span>
                    ${bd.refBurn > 0 ? `<button type="button" class="cap-btn mono" id="proj-wp-match-burn-btn" data-wp-action="preset-budget" data-wp-mode="match_burn" ${!q2Enabled ? "disabled" : ""} title="Set contribution to match household reference annual burn (capped at take-home pay)">Match Household Burn (${escHtml(fmtUSD0(matchBurnTarget))}/yr)</button>` : ""}
                    <button type="button" class="cap-btn mono" id="proj-wp-full-paycheck-btn" data-wp-action="preset-budget" data-wp-mode="full_paycheck" ${!q2Enabled ? "disabled" : ""} title="Allocate 100% of after-tax paycheck toward the household budget">100% of Paycheck</button>
                    <button type="button" class="cap-btn mono" id="proj-wp-zero-budget-btn" data-wp-action="preset-budget" data-wp-mode="zero" ${!q2Enabled ? "disabled" : ""} title="$0 toward household spending budget (allocate to savings/accounts instead)">$0 (None)</button>
                  </div>
                  <div style="display:flex;align-items:center;gap:10px;width:100%;">
                    <span class="mono muted" style="font-size:11px;flex-shrink:0;">$0</span>
                    <input type="range" id="proj-wp-budget-slider" data-wp-field="annual_budget_offset_slider" min="0" max="${sliderBudgetMax}" step="${sliderBudgetStep}" value="${Math.min(sliderBudgetMax, Math.max(0, Math.round(bd.houseContrib)))}" ${!q2Enabled ? "disabled" : ""} style="flex:1;cursor:pointer;opacity:${bd.q2Answered ? '1' : '0.55'};" />
                    <span class="mono muted" id="proj-wp-budget-max-lbl" style="font-size:11px;flex-shrink:0;">${escHtml(fmtUSD0(bd.totalPaycheck > 0 ? bd.totalPaycheck : sliderBudgetMax))}</span>
                  </div>
                </div>

                <!-- Note when 100% of paycheck goes to household budget ($0 remaining & no configured accounts) -->
                <div id="proj-wp-no-remaining-note" class="muted mono" style="display:${(bd.q1Answered && bd.q2Answered && !bd.showRetirementSavingsBar) ? 'block' : 'none'};font-size:11px;padding:6px 10px;border-radius:6px;background:rgba(15,23,42,0.35);border:1px dashed #334155;">
                  100% of after-tax paycheck is allocated to the household budget ($0 remaining for additional retirement savings).
                </div>

                <!-- Step 3: Collapsible "Retirement Savings" After-Tax Paycheck Breakdown & Holdings -->
                <div class="proj-collapsible-row ${bd.overAllocated ? 'status-needs-review' : 'status-completed'} ${retSavExpanded ? 'expanded' : ''}" id="proj-wp-ret-savings-row" style="display:${bd.showRetirementSavingsBar ? '' : 'none'};background:rgba(15,23,42,0.55);">
                  <div class="proj-collapse-hdr" data-wp-action="toggle-ret-savings" style="padding:9px 12px;">
                    <div class="proj-collapse-hdr-left" style="flex-wrap:wrap;row-gap:4px;">
                      <span class="proj-collapse-arrow" aria-hidden="true">
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6"></polyline></svg>
                      </span>
                      <span class="proj-collapse-title has-tip" style="display:inline-flex;align-items:center;gap:4px;" data-tip="${escHtml(_retirementSavingsTooltipHtml(bd.baseOrdRate))}">
                        <span>Retirement Savings</span>
                        <span class="info-dot">i</span>
                      </span>
                      <span class="proj-collapse-sep">|</span>
                      <span class="mono" id="proj-wp-ret-savings-breakdown" style="font-size:11.2px;display:inline-flex;align-items:center;gap:6px;flex-wrap:wrap;">
                        ${_renderRetSavingsBreakdownHtml(bd)}
                      </span>
                    </div>
                  </div>
                  <div class="proj-collapse-body" style="padding:11px 13px;border-top:1px solid rgba(51,65,85,0.45);">
                    <div id="proj-wp-overalloc-banner" class="mono" style="display:${bd.overAllocated ? 'block' : 'none'};padding:7px 10px;border-radius:6px;background:rgba(244,63,94,0.12);border:1px solid rgba(244,63,94,0.4);color:#fda4af;font-size:11px;line-height:1.4;">
                      ${bd.overAllocated ? `&#9888; House contribution (${escHtml(fmtUSD0(bd.houseContrib))}) + after-tax employee retirement contributions (${escHtml(fmtUSD0(Math.round(bd.empAfterTaxTotal)))}) exceed annual take-home pay (${escHtml(fmtUSD0(bd.totalPaycheck))}) by <b>${escHtml(fmtUSD0(Math.round(Math.abs(bd.remainingAfterTax))))}/yr</b>.` : ""}
                    </div>
                    <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap;">
                      <div>
                        <span style="font-size:12px;font-weight:700;color:#f8fafc;">Targeted Portfolio Holdings &amp; Contributions</span>
                        <span class="muted" style="font-size:11px;margin-left:6px;">(Today&#39;s $/yr, CPI-indexed monthly until target retirement age)</span>
                      </div>
                      <button type="button" class="btn btn-primary" data-wp-action="add-contrib" style="font-size:11.5px;padding:4px 11px;">+ Add Holding Target</button>
                    </div>
                    ${eligCount === 0 ? `<div class="muted" style="font-size:11.2px;color:#fbbf24;">Ingest your 401(k), IRA, or brokerage statements in Portfolio Settings &rarr; Statements to target ongoing contributions.</div>` : ""}
                    ${(wp.contributions && wp.contributions.length > 0)
                      ? contribRowsHtml
                      : `<div class="muted" style="font-size:11.2px;padding:6px 2px;">No ongoing account contributions configured (click <b>+ Add Holding Target</b> to add 401(k), IRA, or brokerage contributions).</div>`}
                  </div>
                </div>
              </div>
            ` : ""}
          </div>
        </div>
      `;
    }

    function _parseWorkPlanUsdText(rawVal, maxUsd = 50000000) {
      const cleaned = String(rawVal || "").replace(/[^0-9.]/g, "");
      if (!cleaned) return null;
      const num = parseFloat(cleaned);
      if (!Number.isFinite(num)) return null;
      return Math.round(Math.max(0, Math.min(maxUsd, num)));
    }

    let _famWorkPlanDelegatedEl = null;
    function _ensureFamilyWorkPlanDelegation(container) {
      if (!container || _famWorkPlanDelegatedEl === container || typeof container.addEventListener !== "function") return;
      _famWorkPlanDelegatedEl = container;

      const getActiveWpMem = () => {
        if (!_projDraft || !Array.isArray(_projDraft.family_members)) return null;
        const m = _projDraft.family_members[_selectedFamilyIdx];
        if (!m) return null;
        const msAge = Math.max(18, Math.min(100, parseInt(m.milestone_age, 10) || 85));
        if (!m.work_plan || typeof m.work_plan !== "object") {
          m.work_plan = sanitizeWorkPlanJs(null, msAge);
        }
        return m;
      };

      container.addEventListener("focusin", evt => {
        const t = evt.target;
        if (!t || !t.getAttribute) return;
        const wpField = t.getAttribute("data-wp-field");
        if (wpField === "annual_take_home_text" || wpField === "annual_budget_offset_text") {
          const clean = String(t.value || "").replace(/[^0-9]/g, "");
          t.value = clean;
          if (typeof t.select === "function") {
            setTimeout(() => {
              if (document.activeElement === t) t.select();
            }, 0);
          }
        }
      });

      container.addEventListener("focusout", evt => {
        const t = evt.target;
        if (!t || !t.getAttribute) return;
        const wpField = t.getAttribute("data-wp-field");
        const mem = getActiveWpMem();
        if (!mem || !mem.work_plan) return;
        if (wpField === "annual_take_home_text") {
          const v = Math.round(Number(mem.work_plan.annual_take_home || 0));
          t.value = v > 0 ? ("$" + v.toLocaleString("en-US")) : "";
        } else if (wpField === "annual_budget_offset_text") {
          const bd = _computeWorkPlanPaycheckBreakdown(mem.work_plan, _projDraft);
          t.value = (bd.q2Answered || bd.houseContrib > 0) ? ("$" + Math.round(bd.houseContrib).toLocaleString("en-US")) : "";
        }
      });

      container.addEventListener("keydown", evt => {
        const t = evt.target;
        if (!t || !t.getAttribute || evt.key !== "Enter") return;
        const wpField = t.getAttribute("data-wp-field");
        if (wpField === "annual_take_home_text" || wpField === "annual_budget_offset_text") {
          evt.preventDefault();
          if (typeof t.blur === "function") t.blur();
        }
      });

      container.addEventListener("click", evt => {
        const actBtn = evt.target && evt.target.closest ? evt.target.closest("[data-wp-action]") : null;
        if (!actBtn) return;
        const action = actBtn.getAttribute("data-wp-action");
        if (action === "toggle-ret-savings") {
          const mem = getActiveWpMem();
          if (!mem) return;
          const pid = _editingPortfolioId || (_portfoliosBundle && _portfoliosBundle.active_portfolio_id) || "default";
          const mId = mem.id || `idx_${_selectedFamilyIdx}`;
          const key = `${pid}:${mId}`;
          const rowEl = document.getElementById("proj-wp-ret-savings-row");
          const curOpen = rowEl && rowEl.classList ? rowEl.classList.contains("expanded") : Boolean(_wpRetSavingsOpen.get(key));
          const nextOpen = !curOpen;
          _wpRetSavingsOpen.set(key, nextOpen);
          if (rowEl && rowEl.classList) rowEl.classList.toggle("expanded", nextOpen);
          return;
        }
        if (isDemoModeActive()) {
          showDemoBlockedAlert();
          return;
        }
        const mem = getActiveWpMem();
        if (!mem) return;
        const msAge = Math.max(18, Math.min(100, parseInt(mem.milestone_age, 10) || 85));
        const pid = _editingPortfolioId || (_portfoliosBundle && _portfoliosBundle.active_portfolio_id) || "default";
        const mId = mem.id || `idx_${_selectedFamilyIdx}`;

        if (action === "set-working") {
          const turnOn = actBtn.getAttribute("data-wp-val") === "1";
          _projSectionExplicitOpen.set(`family:work:${pid}:${mId}`, true);
          mem.work_plan.is_working = turnOn;
          if (turnOn) {
            const bParts = String(mem.birth_date || "").split("-");
            const bYr = /^\d{4}$/.test(bParts[0] || "") ? parseInt(bParts[0], 10) : 1975;
            const bMo = /^\d{1,2}$/.test(bParts[1] || "") ? Math.max(1, Math.min(12, parseInt(bParts[1], 10))) : 1;
            const curAge = Math.max(18, ageOnIso(bYr, bMo, 1, TODAY_ISO, 0));
            if (mem.work_plan.default_retirement_age <= curAge) {
              mem.work_plan.default_retirement_age = Math.min(msAge, Math.max(65, curAge + 1));
            }
            if (mem.work_plan.default_retirement_age > msAge) {
              mem.work_plan.default_retirement_age = msAge;
            }
          }
          _syncInvestorsAndBeneficiariesFromFamily();
          renderProjectFamilyMembers();
          _scheduleProjectAutoSave(0);
        } else if (action === "preset-budget") {
          const mode = actBtn.getAttribute("data-wp-mode");
          const bd = _computeWorkPlanPaycheckBreakdown(mem.work_plan, _projDraft);
          let nextOffset = 0;
          if (mode === "match_burn") {
            nextOffset = bd.totalPaycheck > 0 ? Math.min(bd.totalPaycheck, bd.refBurn) : bd.refBurn;
          } else if (mode === "full_paycheck") {
            nextOffset = bd.totalPaycheck;
          } else {
            nextOffset = 0;
          }
          mem.work_plan.annual_budget_offset = Math.max(0, Math.round(nextOffset));
          mem.work_plan.budget_answered = true;
          mem.work_plan.covers_full_burn = false;
          _projSectionExplicitOpen.set(`family:work:${pid}:${mId}`, true);
          _syncInvestorsAndBeneficiariesFromFamily();
          _syncWorkPlanLiveDOM(mem);
          _scheduleProjectAutoSave(150);
        } else if (action === "add-contrib") {
          if (!Array.isArray(mem.work_plan.contributions)) mem.work_plan.contributions = [];
          if (mem.work_plan.contributions.length >= 10) return;
          const elig = _getEligibleWorkPlanAssets();
          const defAsset = elig.find(a => a.tax_model === "ORDINARY_FULL") || elig.find(a => a.tax_model === "TAX_FREE") || elig[0];
          const bd = _computeWorkPlanPaycheckBreakdown(mem.work_plan, _projDraft);
          let defEmpUsd = 24500;
          if (bd.q1Answered) {
            const availAfterTax = Math.max(0, bd.remainingAfterTax);
            const isDefPreTax = Boolean(defAsset && defAsset.tax_model === "ORDINARY_FULL");
            const ordRate = isDefPreTax ? _getDraftIraOrdinaryTaxRate(_projDraft, defAsset) : 0;
            const maxGrossFromRem = (isDefPreTax && ordRate < 0.95)
              ? Math.floor(availAfterTax / Math.max(0.05, 1 - ordRate))
              : Math.floor(availAfterTax);
            defEmpUsd = Math.min(24500, Math.max(0, Math.floor(maxGrossFromRem / 500) * 500 || maxGrossFromRem));
          }
          mem.work_plan.contributions.push({
            target_asset_id: defAsset ? defAsset.id : "",
            employee_annual: defEmpUsd,
            employer_match_annual: 0,
            match_target_asset_id: "",
            age_50_catchup: false,
          });
          _projSectionExplicitOpen.set(`family:work:${pid}:${mId}`, true);
          _wpRetSavingsOpen.set(`${pid}:${mId}`, true);
          _syncInvestorsAndBeneficiariesFromFamily();
          renderProjectFamilyMembers();
          _scheduleProjectAutoSave(150);
        } else if (action === "del-contrib") {
          const cIdx = parseInt(actBtn.getAttribute("data-wp-idx"), 10);
          if (Array.isArray(mem.work_plan.contributions) && cIdx >= 0 && cIdx < mem.work_plan.contributions.length) {
            _projSectionExplicitOpen.set(`family:work:${pid}:${mId}`, true);
            _wpRetSavingsOpen.set(`${pid}:${mId}`, true);
            mem.work_plan.contributions.splice(cIdx, 1);
            _syncInvestorsAndBeneficiariesFromFamily();
            renderProjectFamilyMembers();
            _scheduleProjectAutoSave(150);
          }
        } else if (action === "preset-emp") {
          const cIdx = parseInt(actBtn.getAttribute("data-wp-idx"), 10);
          const val = parseFloat(actBtn.getAttribute("data-wp-val") || "0");
          if (Array.isArray(mem.work_plan.contributions) && mem.work_plan.contributions[cIdx] && Number.isFinite(val)) {
            _projSectionExplicitOpen.set(`family:work:${pid}:${mId}`, true);
            _wpRetSavingsOpen.set(`${pid}:${mId}`, true);
            mem.work_plan.contributions[cIdx].employee_annual = val;
            _syncInvestorsAndBeneficiariesFromFamily();
            renderProjectFamilyMembers();
            _scheduleProjectAutoSave(150);
          }
        }
      });

      const handleFieldUpdate = (evt, isCommit) => {
        const t = evt.target;
        if (!t || !t.getAttribute) return;
        const wpField = t.getAttribute("data-wp-field");
        const cField = t.getAttribute("data-wp-contrib-field");
        if (!wpField && !cField) return;
        if (isDemoModeActive()) {
          showDemoBlockedAlert();
          renderProjectFamilyMembers();
          return;
        }
        const mem = getActiveWpMem();
        if (!mem) return;
        const msAge = Math.max(18, Math.min(100, parseInt(mem.milestone_age, 10) || 85));
        const pid = _editingPortfolioId || (_portfoliosBundle && _portfoliosBundle.active_portfolio_id) || "default";
        const mId = mem.id || `idx_${_selectedFamilyIdx}`;
        _projSectionExplicitOpen.set(`family:work:${pid}:${mId}`, true);

        if (wpField === "default_retirement_age") {
          const raw = parseInt(t.value, 10);
          if (Number.isFinite(raw)) {
            mem.work_plan.default_retirement_age = Math.max(18, Math.min(msAge, raw));
            if (isCommit) t.value = String(mem.work_plan.default_retirement_age);
          }
          _syncInvestorsAndBeneficiariesFromFamily();
          _syncWorkPlanLiveDOM(mem);
          _scheduleProjectAutoSave(350);
          return;
        }
        if (wpField === "annual_take_home_text" || wpField === "annual_take_home_slider") {
          const parsed = wpField === "annual_take_home_slider"
            ? Math.max(0, Math.min(1000000, Math.round(parseFloat(t.value) || 0)))
            : (_parseWorkPlanUsdText(t.value, 50000000) ?? 0);
          mem.work_plan.annual_take_home = parsed;
          mem.work_plan.covers_full_burn = false;
          if (parsed > 0 && Number(mem.work_plan.annual_budget_offset || 0) > parsed) {
            mem.work_plan.annual_budget_offset = parsed;
          }
          _syncInvestorsAndBeneficiariesFromFamily();
          _syncWorkPlanLiveDOM(mem);
          _scheduleProjectAutoSave(wpField === "annual_take_home_slider" ? 250 : 350);
          return;
        }
        if (wpField === "annual_budget_offset_text" || wpField === "annual_budget_offset_slider" || wpField === "annual_budget_offset") {
          const maxCap = Number(mem.work_plan.annual_take_home || 0) > 0
            ? Math.round(Number(mem.work_plan.annual_take_home))
            : 50000000;
          const rawParsed = wpField === "annual_budget_offset_text"
            ? _parseWorkPlanUsdText(t.value, maxCap)
            : (Number.isFinite(parseFloat(t.value)) ? Math.round(Math.max(0, Math.min(maxCap, parseFloat(t.value)))) : null);
          if (rawParsed === null) {
            if (String(t.value || "").trim() === "") {
              mem.work_plan.annual_budget_offset = 0;
              mem.work_plan.budget_answered = false;
            }
          } else {
            mem.work_plan.annual_budget_offset = rawParsed;
            mem.work_plan.budget_answered = true;
          }
          mem.work_plan.covers_full_burn = false;
          _syncInvestorsAndBeneficiariesFromFamily();
          _syncWorkPlanLiveDOM(mem);
          _scheduleProjectAutoSave(wpField === "annual_budget_offset_slider" ? 250 : 350);
          return;
        }
        if (cField) {
          const cIdx = parseInt(t.getAttribute("data-wp-idx"), 10);
          const row = Array.isArray(mem.work_plan.contributions) ? mem.work_plan.contributions[cIdx] : null;
          if (!row) return;
          _wpRetSavingsOpen.set(`${pid}:${mId}`, true);
          if (cField === "age_50_catchup") {
            if (!isCommit) return;
            row.age_50_catchup = Boolean(t.checked);
            _syncInvestorsAndBeneficiariesFromFamily();
            _syncWorkPlanLiveDOM(mem);
            _scheduleProjectAutoSave(150);
          } else if (cField === "target_asset_id" || cField === "match_target_asset_id") {
            if (!isCommit) return;
            row[cField] = String(t.value || "").trim();
            if (row.match_target_asset_id === row.target_asset_id) row.match_target_asset_id = "";
            _syncInvestorsAndBeneficiariesFromFamily();
            renderProjectFamilyMembers();
            _scheduleProjectAutoSave(150);
          } else if (cField === "employee_annual" || cField === "employer_match_annual") {
            const raw = parseFloat(t.value);
            row[cField] = Number.isFinite(raw) ? Math.max(0, Math.min(50000000, raw)) : 0;
            _syncInvestorsAndBeneficiariesFromFamily();
            _syncWorkPlanLiveDOM(mem);
            _scheduleProjectAutoSave(350);
          }
        }
      };

      container.addEventListener("input", evt => handleFieldUpdate(evt, false));
      container.addEventListener("change", evt => handleFieldUpdate(evt, true));
    }

    function renderProjectFamilyMembers() {
      const listEl = document.getElementById("proj-family-list");
      const addBtn = document.getElementById("proj-add-family-btn");
      const detailBox = document.getElementById("proj-family-detail-box");
      const detailTitle = document.getElementById("proj-family-detail-title");
      const detailBadge = document.getElementById("proj-family-detail-badge");
      const detailContent = document.getElementById("proj-family-detail-content");
      if (!_projDraft) return;

      if (!Array.isArray(_projDraft.family_members) || _projDraft.family_members.length === 0) {
        _initFamilyMembersFromDraft();
      }
      const famArr = _projDraft.family_members || [];

      if (_selectedFamilyIdx >= famArr.length) {
        _selectedFamilyIdx = Math.max(0, famArr.length - 1);
      }
      _selectedInvestorIdx = _selectedFamilyIdx;

      renderProjectFamilyListOnly();

      if (famArr.length === 0) {
        if (detailBox) detailBox.style.display = "none";
        return;
      }
      if (detailBox) detailBox.style.display = "block";

      const curMem = famArr[_selectedFamilyIdx];
      if (!curMem) return;

      let contribCount = 0;
      let curContribNum = null;
      famArr.forEach((m, idx) => {
        if (m.is_ssn_contributor) {
          contribCount++;
          if (idx === _selectedFamilyIdx) curContribNum = contribCount;
        }
      });

      const isSsnContributor = Boolean(curMem.is_ssn_contributor);
      const isRmd = Boolean(curMem.is_rmd_owner != null ? curMem.is_rmd_owner : curMem.rmd_owner);
      const bDate = curMem.birth_date || curMem.birthdate || "";
      const ssn = curMem.ssn || {};
      const isSsnCheckboxDisabled = !isSsnContributor && (contribCount >= 2);

      const hasExplicitEarnings = ssn.annual_earnings && Object.keys(ssn.annual_earnings).length > 0;
      const ssaFile = ssn.external_file || ssn.ssa_data_file || "";

      const hasXml = Boolean(ssaFile || hasExplicitEarnings);
      const xmlFileName = ssaFile ? ssaFile.split("/").pop() : (hasXml ? (ssn.statement_file || "social-security-statement.xml") : "");
      const activePid = _editingPortfolioId || (_portfoliosBundle && _portfoliosBundle.active_portfolio_id) || "portfolio";
      const ssnFolderRel = `Portfolios/${activePid}/Statements/SSN/`;
      const xmlFilePath = ssaFile || (hasXml ? `${ssnFolderRel}social-security-statement.xml` : "");
      const xmlUserName = ssn.statement_name || "";
      const xmlDob = ssn.statement_dob || "";
      const xmlFraEst = ssn.fra_estimate || 0;
      const xmlWorkSpan = ssn.work_span_label || "";
      const earnKeys = hasExplicitEarnings ? Object.keys(ssn.annual_earnings).map(x => parseInt(x, 10)).sort((a,b)=>a-b) : [];
      const earnSpan = earnKeys.length > 0 ? `${earnKeys[0]}–${earnKeys[earnKeys.length - 1]}` : (xmlWorkSpan || "");
      const earnCount = earnKeys.length > 0 ? earnKeys.length : (hasExplicitEarnings ? Object.keys(ssn.annual_earnings).length : 0);
      const showSyncDob = Boolean(xmlDob && xmlDob !== bDate);

      if (detailTitle) {
        detailTitle.textContent = `Edit Details: ${curMem.name || `Family Member #${_selectedFamilyIdx + 1}`}`;
      }
      if (detailBadge) {
        if (isSsnContributor && curContribNum) {
          detailBadge.textContent = `SSN Contributor #${curContribNum}`;
          detailBadge.className = curContribNum === 1 ? "pill pill-blue mono" : "pill pill-green mono";
        } else {
          detailBadge.textContent = `Family Member #${_selectedFamilyIdx + 1}`;
          detailBadge.className = "pill mono";
          detailBadge.style.color = "#94a3b8";
        }
      }

      if (detailContent) {
        _ensureProjCollapsibleDelegation();
        _ensureFamilyWorkPlanDelegation(detailContent);
        const famEvals = _evaluateFamilyMemberSections(curMem, isSsnContributor, curContribNum);
        const idSpec = _statusCapsuleSpec(famEvals.identity.status);
        const ssnSpec = _statusCapsuleSpec(famEvals.ssn.status);
        const mId = curMem.id || `idx_${_selectedFamilyIdx}`;
        const idKey = `family:identity:${activePid}:${mId}`;
        const ssnKey = `family:ssn:${activePid}:${mId}`;
        const idExp = _projSectionExplicitOpen.has(idKey)
          ? Boolean(_projSectionExplicitOpen.get(idKey))
          : (famEvals.identity.status === "incomplete" || famEvals.identity.status === "needs_review");
        const ssnExp = _projSectionExplicitOpen.has(ssnKey)
          ? Boolean(_projSectionExplicitOpen.get(ssnKey))
          : (famEvals.ssn.status === "incomplete" || famEvals.ssn.status === "needs_review");
        _projSectionAutoInitialized.add(idKey);
        _projSectionAutoInitialized.add(ssnKey);

        const workPlanCardHtml = _buildFamilyWorkPlanSectionHtml(curMem, isSsnContributor, curContribNum);
        detailContent.innerHTML = `
          <div class="proj-collapsible-stack">
            <!-- Row 1: Identity & Milestone -->
            <div class="proj-collapsible-row ${idSpec.rowCls} ${idExp ? 'expanded' : ''}" id="proj-fam-sec-identity" data-proj-sec="identity">
              <div class="proj-collapse-hdr" data-proj-collapse-toggle="family:identity">
                <div class="proj-collapse-hdr-left">
                  <span class="proj-collapse-arrow" aria-hidden="true">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6"></polyline></svg>
                  </span>
                  <span class="proj-collapse-title">Identity &amp; Milestone</span>
                  <span class="proj-collapse-sep">|</span>
                  <span class="proj-collapse-summary mono" id="proj-fam-sec-identity-summary">${escHtml(famEvals.identity.summary)}</span>
                </div>
                <span class="${idSpec.pillCls}" id="proj-fam-sec-identity-pill" data-tip="${escHtml(famEvals.identity.tip)}">${idSpec.label}</span>
              </div>
              <div class="proj-collapse-body">
                <div style="display:grid;grid-template-columns:1.6fr 1.1fr 1fr;gap:12px;">
                  <div>
                    <label class="ctrl-lbl" style="font-size:11px;display:block;margin-bottom:3px;">Member Name</label>
                    <input type="text" id="proj-cur-fam-name" class="save-name-input mono" style="width:100%;" value="${escHtml(curMem.name || "")}" oninput="_syncCurrentFamilyMemberFromDOM(true)" />
                  </div>
                  <div>
                    <label class="ctrl-lbl" style="font-size:11px;display:block;margin-bottom:3px;">Birthdate</label>
                    <input type="date" id="proj-cur-fam-bday" class="save-name-input mono" style="width:100%;" value="${escHtml(bDate)}" onchange="_syncCurrentFamilyMemberFromDOM(true)" />
                  </div>
                  <div>
                    <label for="proj-cur-fam-ms" class="ctrl-lbl" style="font-size:11px;display:flex;align-items:center;gap:4px;margin-bottom:3px;">
                      <span class="has-tip" style="display:inline-flex;align-items:center;gap:2px;cursor:help;" data-tip="${escHtml(_milestoneAgeTooltipHtml())}">
                        <span>Milestone Age</span>
                        <span class="info-dot">i</span>
                      </span>
                    </label>
                    <input type="number" id="proj-cur-fam-ms" class="save-name-input mono" style="width:100%;" min="65" max="105" value="${curMem.milestone_age || 85}" oninput="_syncCurrentFamilyMemberFromDOM(true)" />
                  </div>
                </div>
              </div>
            </div>

            ${workPlanCardHtml}

            <!-- Row 3: Social Security & RMD -->
            <div class="proj-collapsible-row ${ssnSpec.rowCls} ${ssnExp ? 'expanded' : ''}" id="proj-fam-sec-ssn" data-proj-sec="ssn">
              <div class="proj-collapse-hdr" data-proj-collapse-toggle="family:ssn">
                <div class="proj-collapse-hdr-left">
                  <span class="proj-collapse-arrow" aria-hidden="true">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6"></polyline></svg>
                  </span>
                  <span class="proj-collapse-title">Social Security &amp; RMD</span>
                  <span class="proj-collapse-sep">|</span>
                  <span class="proj-collapse-summary mono" id="proj-fam-sec-ssn-summary">${escHtml(famEvals.ssn.summary)}</span>
                </div>
                <span class="${ssnSpec.pillCls}" id="proj-fam-sec-ssn-pill" data-tip="${escHtml(famEvals.ssn.tip)}">${ssnSpec.label}</span>
              </div>
              <div class="proj-collapse-body" id="proj-fam-ssn-body">
                <div style="display:flex;align-items:center;justify-content:space-between;padding:8px 12px;background:rgba(30,41,59,0.45);border:1px solid #1e293b;border-radius:7px;gap:10px;flex-wrap:wrap;">
                  <label style="display:flex;align-items:center;gap:8px;cursor:${isSsnCheckboxDisabled ? 'not-allowed' : 'pointer'};margin:0;font-size:12.3px;font-weight:600;color:${isSsnContributor ? '#38bdf8' : '#94a3b8'};" title="${isSsnCheckboxDisabled ? 'Maximum of 2 SSN contributors reached. Disable contribution on another member first.' : ''}">
                    <input type="checkbox" id="proj-cur-fam-ssn-enabled" ${isSsnContributor ? 'checked' : ''} ${isSsnCheckboxDisabled ? 'disabled' : ''} onchange="onToggleFamilySsnContribution(this.checked)" style="cursor:${isSsnCheckboxDisabled ? 'not-allowed' : 'pointer'};width:15px;height:15px;accent-color:#38bdf8;" />
                    <span id="proj-fam-ssn-checkbox-label">
                      ${isSsnContributor ? 'Will contribute SSN during retirement.' : 'Will not contribute SSN during retirement.'}
                    </span>
                  </label>
                  ${isSsnContributor ? `
                    <span class="pill ${curContribNum === 1 ? 'pill-blue' : 'pill-green'} mono" style="font-size:10px;">SSN Contributor #${curContribNum || 1}</span>
                  ` : `
                    <span class="pill mono" style="font-size:10px;color:#64748b;border-color:#334155;">No SSN</span>
                  `}
                </div>

                <!-- Claim Age and RMD Owner Row -->
                <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;align-items:center;background:rgba(15,23,42,0.4);border:1px solid #1e293b;border-radius:8px;padding:12px 14px;">
                <div>
                  <label class="ctrl-lbl" style="font-size:11px;display:block;margin-bottom:3px;color:${isSsnContributor ? '#cbd5e1' : '#64748b'};">
                    Default SSN Claim Age
                  </label>
                  <input type="number" id="proj-cur-fam-claim" class="save-name-input mono" style="width:100%;${!isSsnContributor ? 'opacity:0.4;cursor:not-allowed;' : ''}" min="62" max="70" value="${ssn.default_claim_age || 67}" ${!isSsnContributor ? 'disabled' : ''} oninput="_syncCurrentFamilyMemberFromDOM(true)" />
                  <div class="muted" style="font-size:10.5px;margin-top:2px;">Claiming window: Age 62 (reduced) to 70 (delayed credits).</div>
                </div>

                <div>
                  <label class="ctrl-lbl" style="font-size:11px;display:block;margin-bottom:3px;color:#cbd5e1;">
                    Designated RMD Owner
                  </label>
                  <div style="padding:6px 0;">
                    <label class="has-tip" data-tip="&lt;div style='font-weight:700;color:#38bdf8;margin-bottom:4px;'&gt;RMD Owner (SECURE 2.0 Age 73 / 75)&lt;/div&gt;&lt;div style='line-height:1.4;'&gt;Under IRS SECURE 2.0 rules, Traditional IRAs require mandatory annual distributions once the account owner reaches age 73 (born ≤1959) or age 75 (born ≥1960).&lt;/div&gt;&lt;div style='margin-top:6px;color:#94a3b8;font-size:11px;line-height:1.4;'&gt;Any family member may be designated as the default RMD owner (independent of Social Security contribution).&lt;/div&gt;" style="font-size:12px;display:flex;align-items:center;gap:6px;cursor:pointer;color:${isRmd ? '#2dd4bf' : '#e2e8f0'};font-weight:600;">
                      <input type="checkbox" id="proj-cur-fam-rmd" ${isRmd ? 'checked' : ''} onchange="onToggleFamilyRmdOwner(this.checked)" style="cursor:pointer;accent-color:#2dd4bf;" />
                      <span>RMD Owner (Age ${(/^\d{4}/.test(String(bDate)) && parseInt(String(bDate).slice(0, 4), 10) >= 1960) ? 75 : 73}+)</span>
                      <span class="info-dot" style="cursor:help;">i</span>
                      ${isRmd ? '<span class="pill pill-teal mono" style="font-size:9.5px;margin-left:4px;">Active Owner</span>' : ''}
                    </label>
                  </div>
                  <div class="muted" style="font-size:10.5px;margin-top:2px;">Enforces mandatory IRA withdrawals across retirement timeline.</div>
                </div>
              </div>

              <!-- SSA XML Ingest Section -->
              <div style="border-top:1px dashed #334155;padding-top:14px;margin-top:10px;">
                <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px;">
                  <div style="display:flex;align-items:center;gap:7px;">
                    <span style="font-size:12.5px;font-weight:700;color:${isSsnContributor ? '#f8fafc' : '#64748b'};">Social Security (OASDI) Statement</span>
                    <span class="has-tip" data-tip="&lt;b&gt;Social Security Benefits Calculation&lt;/b&gt;&lt;br&gt;Retirement benefits are based on your highest 35 years of wage-indexed earnings. Load your official XML statement downloaded from ssa.gov/myaccount for an exact calculation." style="cursor:help;">
                      <span class="info-dot">i</span>
                    </span>
                  </div>
                  <div id="proj-ssn-status-pill">
                    ${!isSsnContributor ? `
                      <span class="pill mono" style="font-size:10px;padding:3px 8px;color:#64748b;border-color:#334155;">
                        SSN Contribution Inactive
                      </span>
                    ` : (hasXml ? `
                      <span class="pill pill-green mono" style="font-size:10px;padding:3px 8px;">
                        ✓ Using XML: ${escHtml(xmlFileName)}
                      </span>
                    ` : `
                      <span class="pill pill-cyan mono" style="font-size:10px;padding:3px 8px;">
                        No Statement Loaded (Awaiting XML)
                      </span>
                    `)}
                  </div>
                </div>

                <!-- Single Dedicated Ingest Card -->
                <div style="background:rgba(15,23,42,0.65);border:1px solid ${!isSsnContributor ? '#1e293b' : (hasXml ? 'rgba(52,211,153,0.5)' : '#334155')};border-radius:10px;padding:14px 16px;display:flex;flex-direction:column;gap:12px;box-shadow:${(isSsnContributor && hasXml) ? '0 0 14px rgba(52,211,153,0.08)' : 'none'};position:relative;${!isSsnContributor ? 'opacity:0.5;' : ''}">
                  <div>
                    <div style="font-size:12px;font-weight:700;color:#f8fafc;margin-bottom:3px;">
                      ${hasXml && isSsnContributor ? 'Official ssa.gov Statement Record' : 'Load Official ssa.gov Statement XML'}
                    </div>
                    <div class="muted" style="font-size:11.2px;line-height:1.4;">
                      Download your official statement XML file from <a href="https://www.ssa.gov/myaccount/" target="_blank" rel="noopener noreferrer" style="color:#38bdf8;text-decoration:underline;">ssa.gov/myaccount</a> and select it below. The retirement simulator uses your exact lifetime year-by-year earnings to compute your Primary Insurance Amount (PIA) and age 62–70 benefit curve.
                    </div>
                  </div>

                  <!-- File Picker Button & Controls -->
                  <div style="display:flex;align-items:center;gap:10px;position:relative;flex-wrap:wrap;">
                    <button type="button" class="btn ${hasXml ? 'btn-green' : 'btn-primary'}" id="proj-ssn-filepicker-btn" ${!isSsnContributor ? 'disabled style="opacity:0.4;cursor:not-allowed;"' : 'onclick="toggleSsnFilePickerMenu(event)"'} style="font-size:12.5px;padding:7px 16px;display:flex;align-items:center;gap:8px;font-weight:600;">
                      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                        <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path>
                        <polyline points="14 2 14 8 20 8"></polyline>
                        <line x1="16" y1="13" x2="8" y2="13"></line>
                        <line x1="16" y1="17" x2="8" y2="17"></line>
                      </svg>
                      <span id="proj-ssn-btn-label">${hasXml ? `Using: ${escHtml(xmlFileName)}` : 'Select ssa.gov XML file'}</span>
                      <span style="font-size:10px;opacity:0.75;">&#9662;</span>
                    </button>

                    ${(hasXml && isSsnContributor) ? `
                      <button type="button" class="btn" onclick="clearLoadedSsaXml()" style="font-size:11px;padding:6px 12px;color:#fda4af;border-color:rgba(239,68,68,0.35);" title="Remove loaded statement file">
                        ✕ Remove File
                      </button>
                    ` : ''}

                    <div id="proj-ssn-file-menu" class="proj-ssn-file-menu" style="display:none;"></div>
                    <input type="file" id="proj-ssn-file-input" accept=".xml,text/xml,application/xml" style="display:none;" onchange="handleSsaFilePicked(event)" />
                  </div>

                  <!-- Verified Summary Info Banner (when XML is loaded) -->
                  ${(hasXml && isSsnContributor) ? `
                    <div style="background:rgba(6,78,59,0.3);border:1px solid rgba(52,211,153,0.35);border-radius:8px;padding:12px 14px;display:flex;flex-direction:column;gap:6px;font-size:11.8px;">
                      <div style="display:flex;justify-content:space-between;align-items:center;">
                        <span style="font-weight:700;color:#34d399;font-size:12px;">✓ Official Statement Verified</span>
                        <span class="mono muted" style="font-size:10.5px;">${escHtml(xmlFilePath)}</span>
                      </div>
                      <div style="display:gap:14px;flex-wrap:wrap;color:#e2e8f0;line-height:1.45;">
                        ${xmlUserName ? `<span><strong>Worker Name:</strong> ${escHtml(xmlUserName)}</span>` : ''}
                        ${xmlDob ? `<span><strong>Statement Birthdate:</strong> ${escHtml(xmlDob)}</span>` : ''}
                        <span><strong>Lifetime Earnings:</strong> ${earnCount > 0 ? `${earnCount} working years (${earnSpan})` : 'Active'}</span>
                        ${xmlFraEst > 0 ? `<span style="color:#7dd3fc;font-weight:700;"><strong>FRA Benefit (Age 67):</strong> $${Math.round(xmlFraEst).toLocaleString()}/mo</span>` : ''}
                      </div>
                      ${showSyncDob ? `
                        <div style="margin-top:6px;padding-top:6px;border-top:1px dashed rgba(52,211,153,0.25);display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px;">
                          <span class="muted" style="font-size:11px;">Current family member birthdate (${escHtml(bDate)}) differs from statement birthdate (${escHtml(xmlDob)}).</span>
                          <button type="button" class="btn btn-primary" style="font-size:11px;padding:4px 10px;" data-sa-action="sync-dob" data-sa-dob="${escHtml(xmlDob)}">
                            📅 Sync Profile Birthdate to ${escHtml(xmlDob)}
                          </button>
                        </div>
                      ` : ''}
                    </div>
                  ` : `
                    <div class="muted" style="font-size:11.2px;line-height:1.45;">
                      ${isSsnContributor ? (_availableSsnFiles.length > 0 ? `
                        <div style="margin-bottom:6px;color:#cbd5e1;font-weight:600;">Discovered statement in <code>${escHtml(ssnFolderRel)}</code>:</div>
                        <div style="display:flex;gap:8px;flex-wrap:wrap;">
                          ${_availableSsnFiles.map(f => `
                            <button type="button" class="btn" style="font-size:11.5px;padding:4px 11px;display:flex;align-items:center;gap:6px;" data-sa-action="load-ssa-path" data-sa-path="${escHtml(f.path)}">
                              📄 ${escHtml(f.name)} (${(f.size/1024).toFixed(1)} KB)
                            </button>
                          `).join("")}
                        </div>
                      ` : `
                        Statements placed in <code>${escHtml(ssnFolderRel)}</code> are auto-discovered and ready to load in 1 click, or click above to browse any XML file on disk.
                      `) : `
                        Check "Adult Household Investor &amp; Social Security Contributor" above to activate Social Security statement ingestion and benefits calculation for this member.
                      `}
                    </div>
                  `}
                </div>
              </div>
              </div>
            </div>
          </div>
        `;
      }
    }

    function renderProjectInvestors() {
      renderProjectFamilyMembers();
    }

    function renderProjectBeneficiariesList() {
      renderProjectFamilyMembers();
    }

    function renderProjectSettingsForm(opts = {}) {
      if (!_projDraft) return;
      const hEl = document.getElementById("proj-household-name");
      if (hEl) hEl.value = _projDraft.household_name || "";
      _setDraftBadgePreviewSrc(_projDraft.badge_image || "Assets/default_badge.svg");
      _syncBurnControlsFromDraft();
      _syncTaxControlsFromDraft();

      populateBadgeModelDropdown();
      updateDefaultBadgePrompt(false);
      if (opts.fetchStatements !== false) {
        fetchPortfolioStatements(_editingPortfolioId || (_portfoliosBundle && _portfoliosBundle.active_portfolio_id));
      }
      syncBadgeSliderUI();

      renderProjectFamilyMembers();
      renderProjectSymbolsList();
      renderProjectPromptsPane();
    }

    function addProjectFamilyMemberRow() {
      _syncProjDraftFromDOM();
      if (!_projDraft.family_members) _projDraft.family_members = [];
      const isFirstMember = _projDraft.family_members.length === 0;
      const nextNum = _projDraft.family_members.length + 1;
      _projDraft.family_members.push({
        id: `family_${Date.now()}_${nextNum}`,
        name: `Family Member ${nextNum}`,
        birth_date: "",
        milestone_age: 85,
        is_rmd_owner: isFirstMember,
        is_ssn_contributor: false,
        ssn: {
          fra_age: 67,
          default_claim_age: 67,
          external_file: ""
        },
        work_plan: sanitizeWorkPlanJs({ is_working: isFirstMember }, 85),
      });
      _selectedFamilyIdx = _projDraft.family_members.length - 1;
      _selectedInvestorIdx = _selectedFamilyIdx;
      _famSsnExpanded = false;
      _syncInvestorsAndBeneficiariesFromFamily();
      renderProjectFamilyMembers();
      _scheduleProjectAutoSave(0);
    }

    function addProjectInvestorRow() {
      addProjectFamilyMemberRow();
    }

    function removeProjectFamilyMemberRow(idx) {
      _syncProjDraftFromDOM();
      if (Array.isArray(_projDraft.family_members) && _projDraft.family_members.length > 1) {
        const wasRmd = _projDraft.family_members[idx].is_rmd_owner;
        _projDraft.family_members.splice(idx, 1);
        _selectedFamilyIdx = Math.max(0, Math.min(_selectedFamilyIdx, _projDraft.family_members.length - 1));
        _selectedInvestorIdx = _selectedFamilyIdx;
        if (wasRmd) {
          const firstContrib = _projDraft.family_members.find(m => m.is_ssn_contributor);
          if (firstContrib) {
            firstContrib.is_rmd_owner = true;
          } else if (_projDraft.family_members.length > 0) {
            _projDraft.family_members[0].is_ssn_contributor = true;
            _projDraft.family_members[0].is_rmd_owner = true;
          }
        }
      }
      _syncInvestorsAndBeneficiariesFromFamily();
      renderProjectFamilyMembers();
      _scheduleProjectAutoSave(0);
    }

    function removeProjectInvestorRow(idx) {
      removeProjectFamilyMemberRow(idx);
    }

    function addProjectBeneficiaryRow() {
      addProjectFamilyMemberRow();
    }

    function removeProjectBeneficiaryRow(idx) {
      removeProjectFamilyMemberRow(idx);
    }

    async function selectPortfolioFromDialog() {
      await _flushProjectAutoSave();
      const list = Array.isArray(_portfoliosBundle && _portfoliosBundle.portfolios) ? _portfoliosBundle.portfolios : [];
      const targetId = _selectedPortfolioId || (_portfoliosBundle && _portfoliosBundle.active_portfolio_id) || null;
      const saveBtn = document.getElementById("proj-save-btn");
      if (!targetId) {
        _setProjSettingsMsg("Select or create a portfolio first.", "#fda4af");
        return;
      }
      const targetEntry = list.find(p => p.id === targetId) || null;
      if (targetEntry && targetEntry.is_complete === false) {
        const errs = _getPortfolioIncompleteMessages(targetEntry);
        _setProjSettingsMsg(`Cannot activate incomplete portfolio "${targetEntry.name || targetId}" — complete required fields in Settings: ${errs.join("; ")}`, "#fbbf24");
        editPortfolioRow(targetId);
        return;
      }
      if (saveBtn) {
        saveBtn.disabled = true;
        saveBtn.textContent = "Selecting Portfolio...";
      }
      _setProjSettingsMsg(`Activating portfolio "${targetId}" and rebuilding view...`, "#7dd3fc");
      try {
        const { ok, status, data } = await apiJson("/api/portfolios/select", { body: { portfolio_id: targetId } });
        if (ok && data && data.ok) {
          _projNeedsReloadOnClose = false;
          _setProjSettingsMsg("Portfolio activated! Reloading page...", "#34d399");
          setTimeout(() => window.location.reload(), 250);
          return;
        }
        if (data && data.portfolios_bundle) {
          _portfoliosBundle = data.portfolios_bundle;
          renderPortfoliosTab();
        }
        if (data && data.incomplete) {
          const errs = _getPortfolioIncompleteMessages(targetEntry, data);
          _setProjSettingsMsg(data.error || `Cannot activate incomplete portfolio: ${errs.join("; ")}`, "#fbbf24");
          editPortfolioRow(targetId);
          return;
        }
        const err = (data && data.error) || `HTTP ${status}`;
        _setProjSettingsMsg(`Error selecting portfolio: ${err}`, "#fda4af");
      } catch (e) {
        _setProjSettingsMsg("AdvisorBot Service is offline. Please ensure the service is running to switch portfolios.", "#fda4af");
      } finally {
        if (saveBtn) {
          saveBtn.textContent = "Select Portfolio";
          _updateSelectPortfolioButtonState();
        }
      }
    }

    // Global keyboard shortcuts: Ctrl+S / Cmd+S saves to current save file; Cmd+K toggles AI chat; Escape closes dialog
    window.addEventListener("keydown", evt => {
      // Saved Scenarios keyboard navigation (Arrow keys + Enter to load)
      if (currentScreen === "models" && !selectedModelDetailId) {
        const isInput = evt.target && (evt.target.tagName === "INPUT" || evt.target.tagName === "TEXTAREA" || evt.target.tagName === "SELECT" || evt.target.isContentEditable);
        if (!isInput && !hasOpenModal() && !evt.ctrlKey && !evt.metaKey && !evt.altKey) {
          // If the event originated on a .model-card, onModelCardKeyDown already handled it
          if (evt.target && evt.target.closest && evt.target.closest(".model-card")) {
            return;
          }
          if (evt.key === "ArrowRight") {
            evt.preventDefault();
            navigateModelCards("right");
            return;
          }
          if (evt.key === "ArrowLeft") {
            evt.preventDefault();
            navigateModelCards("left");
            return;
          }
          if (evt.key === "ArrowDown") {
            evt.preventDefault();
            navigateModelCards("down");
            return;
          }
          if (evt.key === "ArrowUp") {
            evt.preventDefault();
            navigateModelCards("up");
            return;
          }
          if (evt.key === "Enter") {
            const focusedCard = document.activeElement && document.activeElement.closest(".model-card");
            if (focusedCard && focusedCard.dataset.modelId && document.activeElement.tagName !== "BUTTON") {
              evt.preventDefault();
              loadModelById(focusedCard.dataset.modelId, false);
              return;
            }
          }
        }
      }

      if ((evt.ctrlKey || evt.metaKey) && (evt.key === "k" || evt.key === "K")) {
        evt.preventDefault();
        toggleAiChatPane();
        return;
      }
      if ((evt.ctrlKey || evt.metaKey) && (evt.key === "s" || evt.key === "S")) {
        evt.preventDefault();
        saveToCurrentFile();
        return;
      }
      if ((evt.ctrlKey || evt.metaKey) && !evt.shiftKey && (evt.key === "r" || evt.key === "R")) {
        if (typeof window !== "undefined" && window._pageScopeStale) {
          return;
        }
        if (isSaveDirty || (currentSaveName && findModelEntry(currentModelId))) {
          evt.preventDefault();
          reloadCurrentScenario();
          return;
        }
      }
      if (evt.key === "Escape" || evt.key === "Enter") {
        const rebuildBd = document.getElementById("rebuild-backdrop");
        if (rebuildBd && rebuildBd.classList.contains("open") && !isRebuildingSite) {
          evt.preventDefault();
          closeRebuildBackdrop();
          return;
        }
      }
      if (evt.key === "Escape") {
        const protBd = document.getElementById("protect-confirm-backdrop");
        if (protBd && ((protBd.classList && typeof protBd.classList.contains === "function" && protBd.classList.contains("open") === true) || (protBd.style && protBd.style.display === "flex"))) {
          evt.preventDefault();
          closeProtectConfirmDialog(false);
          return;
        }
        const symPopup = document.getElementById("nav-symbols-popup");
        if (symPopup && symPopup.style.display !== "none") {
          evt.preventDefault();
          closeSymbolsPopup();
          return;
        }
        const grantsPopup = document.getElementById("nav-grants-popup");
        if (grantsPopup && grantsPopup.style.display !== "none") {
          evt.preventDefault();
          closeGrantsPopup();
          return;
        }
        const fnBd = document.getElementById("friendly-name-dialog-backdrop");
        if (fnBd && fnBd.classList.contains("open")) {
          evt.preventDefault();
          closeFriendlyNameDialog();
          return;
        }
        const swBd = document.getElementById("sim-sw-dialog-backdrop");
        if (swBd && swBd.classList.contains("open")) {
          evt.preventDefault();
          closeSimWithdrawalDialog();
          return;
        }
        const hypBd = document.getElementById("sim-hyp-dialog-backdrop");
        if (hypBd && hypBd.classList.contains("open")) {
          evt.preventDefault();
          closeHypotheticalAssetDialog();
          return;
        }
        const demoBd = document.getElementById("demo-blocked-backdrop");
        if (demoBd && demoBd.classList.contains("open")) {
          evt.preventDefault();
          closeDemoBlockedAlert();
          return;
        }
        const delBd = document.getElementById("portfolio-delete-backdrop");
        if (delBd && delBd.classList.contains("open")) {
          evt.preventDefault();
          closePortfolioDeleteConfirm();
          return;
        }
        const projBd = document.getElementById("project-settings-backdrop");
        if (projBd && projBd.classList.contains("open")) {
          evt.preventDefault();
          closeProjectSettingsDialog();
          return;
        }
        const backdrop = document.getElementById("save-dialog-backdrop");
        if (backdrop && backdrop.classList.contains("open")) {
          evt.preventDefault();
          closeSaveDialog();
          return;
        }
        if (currentScreen === "models" && typeof selectedModelDetailId !== "undefined" && selectedModelDetailId) {
          evt.preventDefault();
          closeModelDetail();
        }
      }
    });

    // =========================================================================
    // PRIVACY SHIELD (CASE B) & DEMO MODE (CASE A)
    // =========================================================================
    let _privacyConfig = null;

    function updatePrivacyChromeBadges(statusMeta = null) {
      const st = statusMeta || (PAYLOAD && PAYLOAD.privacy_status) || {
        demo_enabled: false,
        shield_mode: "identity_aliased",
      };
      const demoBadge = document.getElementById("demo-mode-badge");
      if (demoBadge) {
        demoBadge.style.display = st.demo_enabled ? "inline-flex" : "none";
      }
      const demoNavBar = document.getElementById("nav-demo-bar");
      const demoNavSub = document.getElementById("nav-demo-sub");
      if (demoNavBar) {
        const isOn = Boolean(st.demo_enabled);
        demoNavBar.classList.toggle("active", isOn);
        demoNavBar.classList.toggle("pressed", isOn);
        demoNavBar.setAttribute("aria-pressed", isOn ? "true" : "false");
        if (demoNavSub) {
          demoNavSub.textContent = isOn ? "Active \u2022 Read-Only" : "Off \u2022 Live View";
        }
      }
      updateSaveStatusUI(false);
      // SECURITY DO: Keep `#ai-chat-shield-pill` synchronized from server-reported `privacy_shield` / `shield_mode`, including `error_blocked` (`SA-02`).
      // SECURITY DON'T: Never display "Clear Text" when server privacy resolution failed or blocked an outbound request.
      const chatPill = document.getElementById("ai-chat-shield-pill");
      const isDemo = Boolean(st.demo_enabled || isDemoModeActive());
      const m = String(st.shield_mode || "identity_aliased");
      if (chatPill) {
        chatPill.className = "pill mono";
        if (m === "error_blocked") {
          chatPill.classList.add("pill-red");
          chatPill.innerHTML = "&#9888; Shield Blocked";
          chatPill.title = "Privacy configuration error — outbound cloud calls blocked (fail-closed)";
        } else if (isDemo) {
          chatPill.classList.add("pill-yellow");
          chatPill.innerHTML = "&#128737; Shield Active";
          chatPill.title = "Cloud AI Privacy Shield active";
        } else if (m === "identity_aliased") {
          chatPill.classList.add("pill-yellow");
          chatPill.innerHTML = "&#128737; Obfuscated Names";
          chatPill.title = "In-app AI Chat Shield active (Obfuscated Names, unscaled USD & accurate birthdates)";
        } else if (m === "identity_and_values") {
          chatPill.classList.add("pill-red");
          chatPill.innerHTML = "&#128737; Names + Values";
          chatPill.title = "In-app AI Chat Shield: Names, dollar amounts, and birthdays obfuscated";
        } else if (m === "local_only") {
          chatPill.classList.add("pill-blue");
          chatPill.innerHTML = "&#127968; Clear Text (Local)";
          chatPill.title = "Clear Text restricted to localhost LLMs only";
        } else {
          chatPill.classList.add("pill-green");
          chatPill.innerHTML = "Clear Text";
          chatPill.title = "Clear Text: unmasked terms sent to selected LLM";
        }
      }
      const modeCNoteEl = document.getElementById("ai-chat-mode-c-note");
      if (modeCNoteEl) {
        if (!isDemo && m === "identity_and_values") {
          modeCNoteEl.style.display = "block";
          modeCNoteEl.innerHTML = `<strong>&#128737; Mode C:</strong> Dollar figures in chat text are obfuscated by the Privacy Shield; statutory tax constants remain exact.`;
        } else {
          modeCNoteEl.style.display = "none";
          modeCNoteEl.textContent = "";
        }
      }
    }

    function _privacyCardFromMode(mode) {
      const m = String(mode || "identity_aliased");
      if (m === "off" || m === "local_only") return "clear";
      if (m === "identity_and_values") return "names_values";
      return "names";
    }

    function _selectedPrivacyCardKey() {
      const cards = ["clear", "names", "names_values"];
      for (const k of cards) {
        const el = document.getElementById(`priv-card-${k}`);
        if (el && el.classList.contains("selected")) return k;
      }
      const mode = (_privacyConfig && _privacyConfig.ai_shield && _privacyConfig.ai_shield.mode) || "identity_aliased";
      return _privacyCardFromMode(mode);
    }

    function togglePrivacyCollapsible(boxId) {
      const box = document.getElementById(boxId);
      if (box) {
        box.classList.toggle("expanded");
      }
    }

    function _syncPrivacyModeLayout(cardKey, modeStr) {
      const cards = ["clear", "names", "names_values"];
      cards.forEach(k => {
        const cardEl = document.getElementById(`priv-card-${k}`);
        const indEl = document.getElementById(`priv-ind-${k}`);
        const isSel = k === cardKey;
        if (cardEl) {
          cardEl.classList.toggle("selected", isSel);
          cardEl.setAttribute("aria-checked", isSel ? "true" : "false");
        }
        if (indEl) {
          indEl.textContent = isSel ? "\u2713 Selected" : "Select";
        }
      });

      const panelClear = document.getElementById("priv-panel-clear");
      const panelValues = document.getElementById("priv-panel-values");
      if (panelClear) panelClear.classList.toggle("active", cardKey === "clear");
      if (panelValues) panelValues.classList.toggle("active", cardKey === "names_values");

      const aiPreviewSub = document.getElementById("priv-preview-ai-sub");
      if (aiPreviewSub) {
        if (modeStr === "off") aiPreviewSub.textContent = "Clear Text \u2022 \u2318K";
        else if (modeStr === "local_only") aiPreviewSub.textContent = "Localhost Clear Text \u2022 \u2318K";
        else if (modeStr === "identity_and_values") aiPreviewSub.textContent = "Names + Values Masked \u2022 \u2318K";
        else aiPreviewSub.textContent = "Shielded Direct API \u2022 \u2318K";
      }
    }

    async function selectPrivacyAiCard(cardKey) {
      if (!_privacyConfig) _privacyConfig = {};
      if (!_privacyConfig.ai_shield) _privacyConfig.ai_shield = {};
      const clearScopeEl = document.getElementById("priv-clear-scope");

      let nextMode = "identity_aliased";
      if (cardKey === "clear") {
        nextMode = (clearScopeEl && clearScopeEl.value === "local_only") ? "local_only" : "off";
      } else if (cardKey === "names_values") {
        nextMode = "identity_and_values";
      } else {
        nextMode = "identity_aliased";
      }
      _privacyConfig.ai_shield.mode = nextMode;
      _privacyConfig.ai_shield.notes_policy = nextMode === "identity_and_values" ? "strip" : "translate";
      _syncPrivacyModeLayout(cardKey, nextMode);
      await savePrivacySettingsFromUI();
    }

    async function loadAndRenderPrivacyTab() {
      const pid = _editingPortfolioId || (_portfoliosBundle && _portfoliosBundle.active_portfolio_id) || "";
      try {
        const qs = pid ? `?portfolio_id=${encodeURIComponent(pid)}` : "";
        const { ok, data } = await apiJson(`/api/privacy${qs}`);
        if (ok && data && data.ok && data.privacy) {
          _privacyConfig = data.privacy;
          if (Array.isArray(data.models)) {
            availableAiModels = data.models;
            renderAiModelUI();
          }
          renderPrivacyPaneFromState();
        }
      } catch (_e) {
        _setProjSettingsMsg("Local server offline; cannot load Privacy settings.", "#fda4af");
      }
    }

    function renderPrivacyPaneFromState() {
      if (!_privacyConfig) return;
      const ai = _privacyConfig.ai_shield || {};
      const dm = _privacyConfig.demo_mode || {};
      const modeStr = String(ai.mode || "identity_aliased");
      const cardKey = _privacyCardFromMode(modeStr);

      const clearScopeEl = document.getElementById("priv-clear-scope");
      const aiScaleEl = document.getElementById("priv-ai-scale");
      const aiShiftEl = document.getElementById("priv-ai-birthday-shift");
      const sPill = document.getElementById("priv-shield-status-pill");

      if (clearScopeEl) {
        clearScopeEl.value = modeStr === "local_only" ? "local_only" : "off";
      }
      if (aiScaleEl) {
        aiScaleEl.value = dm.enabled ? "" : String(ai.scale_factor ?? 0.25);
        aiScaleEl.placeholder = dm.enabled ? "Hidden" : "";
        aiScaleEl.disabled = Boolean(dm.enabled);
      }
      if (aiShiftEl) {
        aiShiftEl.value = dm.enabled ? "" : String(ai.birthday_shift_years ?? 3);
        aiShiftEl.disabled = Boolean(dm.enabled);
      }

      _syncPrivacyModeLayout(cardKey, modeStr);

      if (sPill) {
        if (modeStr === "off") {
          sPill.textContent = "A. Clear Text";
          sPill.className = "pill mono pill-green";
        } else if (modeStr === "local_only") {
          sPill.textContent = "A. Clear Text (Local-Only)";
          sPill.className = "pill mono pill-blue";
        } else if (modeStr === "identity_and_values") {
          sPill.textContent = "C. Obfuscated Names & Values";
          sPill.className = "pill mono pill-red";
        } else {
          sPill.textContent = "B. Obfuscated Names";
          sPill.className = "pill mono pill-yellow";
        }
      }

      const dmScaleEl = document.getElementById("priv-demo-scale");
      const dmScaleValEl = document.getElementById("priv-demo-scale-val");
      const dPill = document.getElementById("priv-demo-status-pill");
      const demoPreviewBar = document.getElementById("priv-preview-demo-bar");
      const demoPreviewSub = document.getElementById("priv-preview-demo-sub");

      const hasDmScale = dm.scale_factor != null && !dm.enabled;
      const scaleNum = Number(dm.scale_factor ?? 0.25);
      if (dmScaleEl) {
        dmScaleEl.value = hasDmScale ? String(scaleNum) : "1";
        dmScaleEl.disabled = Boolean(dm.enabled);
      }
      if (dmScaleValEl) {
        dmScaleValEl.textContent = dm.enabled ? "Hidden" : `${scaleNum.toFixed(2)}x`;
      }
      if (dPill) {
        dPill.textContent = dm.enabled ? "Demo: Active" : "Demo: Off";
        dPill.className = `pill mono ${dm.enabled ? "pill-green" : "pill-blue"}`;
      }
      if (demoPreviewBar) {
        const isOn = Boolean(dm.enabled);
        demoPreviewBar.classList.toggle("active", isOn);
        demoPreviewBar.classList.toggle("pressed", isOn);
        demoPreviewBar.setAttribute("aria-pressed", isOn ? "true" : "false");
        if (demoPreviewSub) {
          demoPreviewSub.textContent = isOn ? "Active \u2022 Read-Only" : "Off \u2022 Live View";
        }
      }

      const mcp = _privacyConfig.mcp_server || {};
      const mcpEnabled = Boolean(mcp.enabled);
      const mcpCheckbox = document.getElementById("priv-mcp-enabled");
      const mcpPill = document.getElementById("priv-mcp-status-pill");

      if (mcpCheckbox) mcpCheckbox.checked = mcpEnabled;
      if (mcpPill) {
        mcpPill.textContent = mcpEnabled ? "MCP: Enabled" : "MCP: Disabled";
        mcpPill.className = `pill mono ${mcpEnabled ? "pill-green" : "pill-blue"}`;
      }

      renderPrivacyEntitiesTable();
      updatePrivacyChromeBadges({
        demo_enabled: Boolean(dm.enabled),
        shield_mode: modeStr,
      });
    }

    function renderPrivacyEntitiesTable() {
      const tbody = document.getElementById("priv-entities-table-body");
      const countEl = document.getElementById("priv-entity-count");
      const filterEl = document.getElementById("priv-entity-filter");
      const aliasPill = document.getElementById("priv-alias-status-pill");
      if (!tbody || !_privacyConfig) return;
      const entities = Array.isArray(_privacyConfig.entities) ? _privacyConfig.entities : [];
      if (countEl) countEl.textContent = String(entities.length);
      if (aliasPill) {
        aliasPill.innerHTML = `<span id="priv-entity-count">${entities.length}</span> aliased names`;
      }
      const q = (filterEl ? filterEl.value : "").trim().toLowerCase();
      const filtered = q
        ? entities.filter(e =>
            `${e.token} ${e.category} ${e.canonical} ${(e.synonyms || []).join(" ")} ${e.demo_value}`
              .toLowerCase()
              .includes(q)
          )
        : entities;

      tbody.innerHTML = filtered
        .map(ent => {
          const tok = String(ent.token || "");
          const synsCsv = Array.isArray(ent.synonyms) ? ent.synonyms.join(", ") : "";
          const chk = ent.enabled !== false ? "checked" : "";
          return `
            <tr style="border-top:1px solid #1e293b;">
              <td style="padding:6px 8px;">
                <input type="checkbox" ${chk} data-sa-change="priv-enabled" data-sa-tok="${escHtml(tok)}" />
              </td>
              <td class="mono" style="padding:6px 8px;color:#7dd3fc;font-weight:700;white-space:nowrap;">${escHtml(tok)}</td>
              <td class="muted mono" style="padding:6px 8px;">${escHtml(ent.category || "")}</td>
              <td class="mono" style="padding:6px 8px;color:#e2e8f0;">${escHtml(ent.canonical || "")}</td>
              <td style="padding:6px 8px;">
                <input type="text" class="save-name-input mono" style="width:100%;font-size:11px;padding:3px 6px;" value="${escHtml(synsCsv)}" data-sa-change="priv-synonyms" data-sa-tok="${escHtml(tok)}" />
              </td>
              <td class="mono" style="padding:6px 8px;color:#6ee7b7;">${escHtml(ent.demo_value || "")}</td>
            </tr>
          `;
        })
        .join("");
    }

    async function savePrivacySettingsFromUI(extraPatch = null) {
      if (!_privacyConfig) _privacyConfig = {};
      const pid = _editingPortfolioId || (_portfoliosBundle && _portfoliosBundle.active_portfolio_id) || "";
      const cardKey = _selectedPrivacyCardKey();
      const clearScopeEl = document.getElementById("priv-clear-scope");
      const aiScaleEl = document.getElementById("priv-ai-scale");
      const aiShiftEl = document.getElementById("priv-ai-birthday-shift");
      const dmScaleEl = document.getElementById("priv-demo-scale");

      let resolvedMode = "identity_aliased";
      if (cardKey === "clear") {
        resolvedMode = (clearScopeEl && clearScopeEl.value === "local_only") ? "local_only" : "off";
      } else if (cardKey === "names_values") {
        resolvedMode = "identity_and_values";
      }

      const resolvedNotesPolicy = resolvedMode === "identity_and_values"
        ? "strip"
        : (resolvedMode === "off" || resolvedMode === "local_only" ? "none" : "translate");

      const curDmEnabled = Boolean(
        (_privacyConfig && _privacyConfig.demo_mode && _privacyConfig.demo_mode.enabled) ??
        (PAYLOAD && PAYLOAD.privacy_status && PAYLOAD.privacy_status.demo_enabled)
      );

      const mcpCheckbox = document.getElementById("priv-mcp-enabled");
      const curMcpEnabled = mcpCheckbox ? Boolean(mcpCheckbox.checked) : Boolean(_privacyConfig?.mcp_server?.enabled);

      const body = {
        portfolio_id: pid,
        ai_shield: {
          mode: resolvedMode,
          block_inapp_agy_cli: true,
          dob_granularity: resolvedMode === "identity_and_values" ? "shift_years" : "year_month",
          scale_factor: aiScaleEl ? Number(aiScaleEl.value || 0.25) : 0.25,
          birthday_shift_years: aiShiftEl ? Number(aiShiftEl.value ?? 3) : 3,
          notes_policy: resolvedNotesPolicy,
          alias_products_and_tickers: resolvedMode !== "off" && resolvedMode !== "local_only",
        },
        mcp_server: {
          enabled: curMcpEnabled,
        },
        demo_mode: {
          enabled: curDmEnabled,
          // SECURITY INVARIANT (`SA3-01` — coupled with `save_privacy_config` in `Scripts/privacy/registry.py`):
          // Preserve any custom `demo_seed` / `seed` configured on disk instead of clobbering with `"demo-4821"`.
          demo_seed: String((_privacyConfig && _privacyConfig.demo_mode && (_privacyConfig.demo_mode.demo_seed || _privacyConfig.demo_mode.seed)) || "demo-4821"),
          scale_factor: dmScaleEl ? Math.min(5.0, Math.max(0.05, Number(dmScaleEl.value || 0.25))) : 0.25,
          round_to: 1000,
        },
        entities: Array.isArray(_privacyConfig.entities) ? _privacyConfig.entities : [],
      };
      if (extraPatch && typeof extraPatch === "object") {
        Object.assign(body, extraPatch);
      }

      _setProjSettingsMsg("Saving Privacy & Demo settings...", "#7dd3fc");
      try {
        const { ok, data } = await apiJson("/api/privacy", { body });
        if (ok && data && data.ok && data.privacy) {
          _privacyConfig = data.privacy;
          if (Array.isArray(data.models)) {
            availableAiModels = data.models;
            renderAiModelUI();
          }
          renderPrivacyPaneFromState();
          _setProjSettingsMsg("Privacy settings saved ✓", "#34d399");
        } else {
          _setProjSettingsMsg((data && data.error) || "Failed saving Privacy settings.", "#fda4af");
        }
      } catch (_e) {
        _setProjSettingsMsg("Local server offline.", "#fda4af");
      }
    }

    function onDemoScaleSliderInput(val) {
      const num = Number(val || 0.25);
      const valEl = document.getElementById("priv-demo-scale-val");
      if (valEl) valEl.textContent = `${num.toFixed(2)}x`;
    }

    async function _applyDemoModeToggle(nextEnabled, curEnabled, shieldMode) {
      const demoNavBar = document.getElementById("nav-demo-bar");
      const demoSub = document.getElementById("nav-demo-sub");
      if (demoNavBar) demoNavBar.classList.add("running");
      updatePrivacyChromeBadges({ demo_enabled: nextEnabled, shield_mode: shieldMode });
      if (demoSub) {
        demoSub.textContent = nextEnabled ? "Enabling Demo Mode..." : "Restoring Live View...";
      }
      let errLabel = "";
      try {
        const { ok, status, data, error } = await apiJson("/api/privacy", {
          body: { demo_mode: { enabled: nextEnabled } },
        });
        if ((ok && data && data.ok) || status === 409) {
          if (data && data.privacy) _privacyConfig = data.privacy;
          if (demoSub) {
            demoSub.textContent = "Reloading...";
          }
          window.location.reload();
          return;
        }
        errLabel = (data && data.error) || error || `HTTP ${status || "Err"}`;
      } catch (_e) {
        errLabel = "Service offline";
      }
      if (demoNavBar) demoNavBar.classList.remove("running");
      updatePrivacyChromeBadges({ demo_enabled: curEnabled, shield_mode: shieldMode });
      if (demoSub && errLabel) {
        demoSub.textContent = errLabel;
        setTimeout(() => updatePrivacyChromeBadges({ demo_enabled: curEnabled, shield_mode: shieldMode }), 3500);
      }
    }

    async function toggleDemoModeFromNav() {
      const demoNavBar = document.getElementById("nav-demo-bar");
      if (demoNavBar && demoNavBar.classList.contains("running")) return;
      const curEnabled = Boolean(
        (_privacyConfig && _privacyConfig.demo_mode && _privacyConfig.demo_mode.enabled) ??
        (PAYLOAD && PAYLOAD.privacy_status && PAYLOAD.privacy_status.demo_enabled)
      );
      const nextEnabled = !curEnabled;
      const shieldMode = String(
        (_privacyConfig && _privacyConfig.ai_shield && _privacyConfig.ai_shield.mode) ||
        (PAYLOAD && PAYLOAD.privacy_status && PAYLOAD.privacy_status.shield_mode) ||
        "identity_aliased"
      );
      await _applyDemoModeToggle(nextEnabled, curEnabled, shieldMode);
    }

    async function setAiShieldScalePreset(val) {
      const el = document.getElementById("priv-ai-scale");
      if (el) el.value = String(val);
      await savePrivacySettingsFromUI();
    }

    async function onMcpEnabledToggle(enabled) {
      if (!_privacyConfig) _privacyConfig = {};
      if (!_privacyConfig.mcp_server) _privacyConfig.mcp_server = {};
      _privacyConfig.mcp_server.enabled = Boolean(enabled);
      const mcpPill = document.getElementById("priv-mcp-status-pill");
      if (mcpPill) {
        mcpPill.textContent = enabled ? "MCP: Enabled" : "MCP: Disabled";
        mcpPill.className = `pill mono ${enabled ? "pill-green" : "pill-blue"}`;
      }
      await savePrivacySettingsFromUI();
    }

    async function setAiShieldShiftPreset(yrs) {
      const el = document.getElementById("priv-ai-birthday-shift");
      if (el) el.value = String(yrs);
      await savePrivacySettingsFromUI();
    }

    async function syncPrivacyEntitiesUI() {
      // SECURITY INVARIANT (`SA3-01`): Block privacy registry mutations while Demo Mode is active so redacted
      // `[Hidden in Demo Mode]` entities in `_privacyConfig` can never overwrite real `privacy.json` on disk.
      if (typeof isDemoModeActive === "function" && isDemoModeActive()) {
        if (typeof showDemoBlockedAlert === "function") showDemoBlockedAlert();
        return;
      }
      const pid = _editingPortfolioId || (_portfoliosBundle && _portfoliosBundle.active_portfolio_id) || "";
      try {
        const { ok, data } = await apiJson("/api/privacy/sync", { body: { portfolio_id: pid } });
        if (ok && data && data.ok && data.privacy) {
          _privacyConfig = data.privacy;
          renderPrivacyPaneFromState();
          _setProjSettingsMsg(`Synced ${data.privacy.entities.length} entities from portfolio ✓`, "#34d399");
        }
      } catch (_e) {
        _setProjSettingsMsg("Failed syncing privacy registry.", "#fda4af");
      }
    }

    async function addCustomPrivacyEntityUI() {
      if (typeof isDemoModeActive === "function" && isDemoModeActive()) {
        if (typeof showDemoBlockedAlert === "function") showDemoBlockedAlert();
        return;
      }
      if (!_privacyConfig) return;
      const canonEl = document.getElementById("priv-custom-canon");
      const synsEl = document.getElementById("priv-custom-syns");
      const canon = canonEl ? canonEl.value.trim() : "";
      if (!canon) return;
      const rawSyns = synsEl ? synsEl.value.split(",").map(s => s.trim()).filter(Boolean) : [];
      const ents = Array.isArray(_privacyConfig.entities) ? [..._privacyConfig.entities] : [];
      const customCount = ents.filter(e => e.category === "custom").length + 1;
      ents.push({
        token: `CUSTOM_${customCount}`,
        category: "custom",
        canonical: canon,
        synonyms: [canon, ...rawSyns],
        synonyms_edited: true,
        auto_detected: false,
        enabled: true,
      });
      _privacyConfig.entities = ents;
      if (canonEl) canonEl.value = "";
      if (synsEl) synsEl.value = "";
      await savePrivacySettingsFromUI();
    }

    async function onPrivacyEntityFieldChange(tok, field, val) {
      if (typeof isDemoModeActive === "function" && isDemoModeActive()) {
        if (typeof showDemoBlockedAlert === "function") showDemoBlockedAlert();
        renderPrivacyPaneFromState();
        return;
      }
      if (!_privacyConfig || !Array.isArray(_privacyConfig.entities)) return;
      const ent = _privacyConfig.entities.find(e => e.token === tok);
      if (!ent) return;
      if (field === "enabled") {
        ent.enabled = Boolean(val);
      } else if (field === "synonyms") {
        ent.synonyms = String(val || "").split(",").map(s => s.trim()).filter(Boolean);
        ent.synonyms_edited = true;
      }
      await savePrivacySettingsFromUI();
    }

    async function runPrivacyWirePreviewUI() {
      // SECURITY INVARIANT (`SA3-01` / `SA3-02`): Block `/api/privacy/preview` in Demo Mode so canonical
      // household terms cannot be reconstructed via round-trip wire preview.
      if (typeof isDemoModeActive === "function" && isDemoModeActive()) {
        if (typeof showDemoBlockedAlert === "function") showDemoBlockedAlert();
        return;
      }
      const pid = _editingPortfolioId || (_portfoliosBundle && _portfoliosBundle.active_portfolio_id) || "";
      const inpEl = document.getElementById("priv-preview-input");
      const outEl = document.getElementById("priv-preview-result");
      if (!outEl) return;
      const sampleText = inpEl ? inpEl.value.trim() : "";
      try {
        const { ok, data } = await apiJson("/api/privacy/preview", {
          body: { portfolio_id: pid, sample_text: sampleText },
        });
        if (!ok || !data || !data.ok) {
          outEl.style.display = "flex";
          outEl.innerHTML = `<div class="mono" style="color:#fda4af;">Preview failed.</div>`;
          return;
        }
        const leakHtml = data.leak_free
          ? `<span class="pill pill-green mono" style="font-size:10px;">LEAK CHECK: 0 RESIDUAL SYNONYMS</span>`
          : `<span class="pill pill-red mono" style="font-size:10px;">LEAKS FOUND: ${escHtml(data.leaked_terms.join(", "))}</span>`;
        outEl.style.display = "flex";
        outEl.innerHTML = `
          <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap;">
            ${leakHtml}
            <span class="muted mono" style="font-size:10.5px;">Outbound Wire Payload &rarr; Restored UI Round-Trip</span>
          </div>
          <div style="background:#020617;border:1px solid #1e293b;border-radius:6px;padding:7px 9px;">
            <div class="muted mono" style="font-size:10px;margin-bottom:2px;">1. Outbound Prompt Sent to AI Provider:</div>
            <div class="mono" style="color:#7dd3fc;">${escHtml(data.wire_text || "")}</div>
          </div>
          <div style="background:#020617;border:1px solid #1e293b;border-radius:6px;padding:7px 9px;">
            <div class="muted mono" style="font-size:10px;margin-bottom:2px;">2. Restored Local Display &amp; Saved Scenario:</div>
            <div class="mono" style="color:#6ee7b7;">${escHtml(data.restored_text || "")}</div>
          </div>
        `;
      } catch (_e) {
        outEl.style.display = "flex";
        outEl.innerHTML = `<div class="mono" style="color:#fda4af;">Local server offline.</div>`;
      }
    }

    // Boot
    initHeaderAndKPIs();
    updatePrivacyChromeBadges();
    setTaxMode(taxMode);
    applyPreset(activePreset);
    applyRelPreset("all");
    recomputeSsnBars();
    applySsnPreset("40y");
    renderSsnTables();
    recomputeSimBars();
    applySimPreset("40y");
    syncSimModelControls();
    updateSimOrderPresetButtons();
    syncSimBurnControls();
    onSimInflationInput(simInflationPct);
    if (typeof syncSimRetirementAgeControls === "function") syncSimRetirementAgeControls();
    if (typeof updateSimOptimismDOM === "function") updateSimOptimismDOM();
    bindChartInteractions();
    bindUITooltips();
    renderHoldingsTable();
    renderHoldingsPie();
    renderGoogleStockTables();
    renderEventsDrawer();
    initSavesSystem();
    fetchGlobalPreferences();
    updateHeaderSymbolUI();
    fetchLiveSymbolQuote(currentActiveSymbol, false);
    initAiChatAssistant();
    renderAiPromptsDropdown();
    renderModelsScreen();
    window._simBootCompleted = true;

    function refreshActiveSiteState() {
      fetchGlobalPreferences();
      fetchLiveSymbolQuote(currentActiveSymbol, false);
      refreshSavesFromServer();
      probeAiModels();
    }
    if (typeof window !== "undefined") {
      window.refreshActiveSiteState = refreshActiveSiteState;
    }
    startServiceHealthMonitor();

    if (window.location.hash === "#portfolio" || window.location.hash === "#main") {
      switchScreen("portfolio");
    } else if (window.location.hash === "#equity-grants" || window.location.hash === "#google-stock" || window.location.hash === "#google") {
      switchScreen("google");
    } else if (window.location.hash === "#retirement" || window.location.hash === "#retirement-simulator") {
      switchScreen("retirement");
    } else if (window.location.hash === "#income-expense" || window.location.hash === "#income") {
      switchScreen("income");
    } else if (window.location.hash === "#scenarios" || window.location.hash === "#models") {
      switchScreen("models");
    }
