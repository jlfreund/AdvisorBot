    // =========================================================================
    // SCREEN 3: RETIREMENT SIMULATOR (MONTHLY ASSET & BURN MODEL)
    // =========================================================================

    const MONTH_NAMES_SHORT = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

    function resolveSimAssetForecast(asset) {
      if (!asset) return { growth_pct: 0.0, risk_class: "broad_equity", expense_ratio_pct: 0.0, is_override: false, risk_overridden: false };
      const aid = String(asset.id || "");
      const fk = String(asset.forecast_key || aid);
      const parentKeys = Array.isArray(asset.parent_forecast_keys) ? [...asset.parent_forecast_keys] : [];
      if (!asset.is_hypothetical && asset.bucket === "goog" && PRIMARY_SYMBOL && !parentKeys.includes(PRIMARY_SYMBOL) && fk !== PRIMARY_SYMBOL) {
        parentKeys.push(PRIMARY_SYMBOL);
      }
      const defRc = String(
        asset.default_risk_class || asset.risk_class || (asset.bucket === "goog" ? "single_stock" : (asset.rate_model === "fixed" ? "cash_fixed" : "broad_equity"))
      );
      const hasConstituents = Array.isArray(asset.constituent_forecasts) && asset.constituent_forecasts.length > 0;
      let defGp = 0.0;
      if (asset.is_hypothetical) {
        defGp = Number(asset.growth_pct) || 0.0;
      } else if (asset.rate_model === "fixed" && asset.annual_rate_pct != null && !hasConstituents) {
        defGp = Number(asset.annual_rate_pct) || 0.0;
      } else {
        defGp = Number(asset.default_growth_pct ?? asset.annual_rate_pct ?? 0.0) || 0.0;
      }
      const baseRes = _resolveForecastByKey(fk, parentKeys, defGp, defRc);
      const resolved = _applyConstituentRollup(asset, baseRes, defRc);
      const baseGp = Number(resolved.growth_pct) || 0.0;
      const resolvedRc = String(resolved.risk_class || defRc);
      const isContractualFixed = (
        asset.rate_model === "fixed"
        || String(asset.return_source || "") === "CONTRACT_CREDITED_RATE"
        || resolvedRc === "cash_fixed"
      );
      const mult = (typeof simOptimismMultiplier === "number") ? simOptimismMultiplier : 1.0;
      const effGp = isContractualFixed ? baseGp : (baseGp * mult);
      const liveFee = (typeof holdingFeePct !== "undefined" && holdingFeePct && holdingFeePct[fk] != null)
        ? Number(holdingFeePct[fk])
        : (asset.fee_pct != null ? Number(asset.fee_pct) : 0.0);
      const advFee = Number.isFinite(liveFee) ? Math.max(0, liveFee) : 0.0;
      const erPct = asset.is_hypothetical ? advFee : (advFee + (Number(asset.expense_ratio_pct) || 0.0));
      return {
        growth_pct: effGp,
        net_growth_pct: effGp - erPct,
        base_growth_pct: baseGp,
        growth_multiplier: isContractualFixed ? 1 : mult,
        constituent_override: resolved.constituent_override,
        inherited_from: resolved.inherited_from,
        risk_class: resolvedRc,
        vol_pct: resolved.vol_pct != null ? Number(resolved.vol_pct) : null,
        expense_ratio_pct: erPct,
        fee_pct: advFee,
        is_override: Boolean(resolved.is_override),
        risk_overridden: Boolean(resolved.risk_overridden),
      };
    }

    // Recenter an existing monthly draw on the forecast for its projection year.
    // Restore the annual fee before applying the gross-return ratio, then deduct it once.
    function buildYearRateAdjuster(mc = {}) {
      const curves = [];
      for (const a of SIM_ASSETS) {
        if (holdingFadeTarget(a) == null) continue;
        const fc = resolveSimAssetForecast(a);
        const forced = fc.risk_class === "single_stock" ? mc.googExpectedPct ?? mc.singleStockExpectedPct
          : mc.nonGoogExpectedPct ?? mc.broadEquityExpectedPct;
        const lockYrs = contractualLockYears(a);
        if (forced != null || fc.growth_multiplier === 0 || (fc.risk_class === "cash_fixed" && (fc.risk_overridden || lockYrs === 0))) continue; // Explicit Monte Carlo test assumptions specify a constant mean.
        const curve = holdingReturnCurve(a, fc.base_growth_pct);
        curves.push({id: a.id, curve, mult: fc.growth_multiplier, start: fc.growth_pct / 100, fee: fc.expense_ratio_pct / 100});
      }
      const factors = [];
      const last = [];
      return (rates, year) => {
        if (year <= 0 || !curves.length) return rates;
        if (last[year]?.input === rates) return last[year].output;
        const ratios = factors[year] || (factors[year] = curves.map(c =>
          (1 + c.curve(year) * c.mult / 100) / (1 + c.start)));
        const out = {...rates};
        curves.forEach((c, i) => {
          if (ratios[i] === 1 || rates[c.id] == null) return;
          const gross = (1 + rates[c.id]) ** 12 + c.fee;
          out[c.id] = Math.max(0.0001, gross * ratios[i] - c.fee) ** (1 / 12) - 1;
        });
        last[year] = {input: rates, output: out};
        return out;
      };
    }

    function computeAssetMonthlyRates(riskClassAnnualOverrides = null) {
      const rates = {};
      (SIM_ASSETS || []).forEach(a => {
        const fc = resolveSimAssetForecast(a);
        a._is_forecast_override = Boolean(fc.is_override);
        a._live_fee_pct = fc.fee_pct || 0.0;
        const rc = fc.risk_class;
        const annDec = (riskClassAnnualOverrides && riskClassAnnualOverrides[rc] != null)
          ? Number(riskClassAnnualOverrides[rc])
          : (fc.growth_pct / 100.0);
        const netAnn = annDec - (fc.expense_ratio_pct / 100.0);
        rates[a.id] = (1.0 + netAnn <= 0.0) ? -0.9999 : (Math.pow(1.0 + netAnn, 1.0 / 12.0) - 1.0);
      });
      return rates;
    }

    function simBarScale(b) {
      if (!b || simDollarMode !== "real") return 1.0;
      return 1.0 / (b.deflator || 1.0);
    }

    function simVal(b, rawVal) {
      return (rawVal || 0) * simBarScale(b);
    }

    // =========================================================================
    // 2026 FEDERAL PROGRESSIVE TAX SCHEDULE (1:1 MIRROR OF simulator.py — DA-C03)
    // =========================================================================
    // PARITY & STATUTORY INVARIANTS:
    //   1. `_FED_STD_DED_2026`, `FED_ORD_TOPS_*`, and `FED_LTCG_TOPS_*` scale by CPI `dfl`.
    //      DO NOT scale the $250k/$200k 3.8% NIIT threshold, $32k/$44k ($25k/$34k) IRC §86
    //      Social Security provisional-income tiers, or $3,000/yr IRC §1211 ordinary loss offset!
    //   2. Topic A Filing Rules: `MFJ` is eligible ONLY when `totalFamily > 1`; when eligible and
    //      unset, defaults to `true` iff `adultCount >= 2` (age >= 18).
    //   3. `simMinTaxBracketFloor` seeds starting `ytd.agi_ord` for households with external
    //      retirement income without charging tax on the synthetic outside-income floor itself.
    const { FED_STD_DED_2026, FED_ORD_TOPS_MFJ, FED_ORD_TOPS_SINGLE, FED_LTCG_TOPS_MFJ, FED_LTCG_TOPS_SINGLE, FED_ORD_LOW_RATES, FLOOR_TAXABLE_STARTS_MFJ, FLOOR_TAXABLE_STARTS_SINGLE } = __FEDERAL_TAX_2026__;

    function rmdStartAgeForBirthYear(birthYear) {
      return Number(birthYear) >= 1960 ? 75 : 73;
    }

    function buildTaxScheduleConfig() {
      const proj = (typeof PROJECT_CFG !== "undefined" && PROJECT_CFG) ? PROJECT_CFG : {};
      const tr = (proj.tax_rates && typeof proj.tax_rates === "object") ? proj.tax_rates : {};
      const curYr = parseInt(String(TODAY_ISO || "2026").slice(0, 4), 10) || 2026;
      const famMembers = Array.isArray(proj.family_members) ? proj.family_members : [];
      const invs = Array.isArray(proj.investors) ? proj.investors : [];
      const bens = Array.isArray(proj.beneficiaries) ? proj.beneficiaries : [];

      let totalFamily = 0;
      let adultCount = 0;
      if (famMembers.length > 0) {
        totalFamily = famMembers.length;
        famMembers.forEach(m => {
          const bStr = String((m && m.birth_date) || "").slice(0, 4);
          const by = /^\d{4}$/.test(bStr) ? parseInt(bStr, 10) : (curYr - 30);
          if ((curYr - by) >= 18) adultCount++;
        });
      } else {
        const allP = [...invs, ...bens];
        totalFamily = allP.length;
        allP.forEach(p => {
          const bStr = String((p && p.birth_date) || "").slice(0, 4);
          const by = /^\d{4}$/.test(bStr) ? parseInt(bStr, 10) : (curYr - 30);
          if ((curYr - by) >= 18) adultCount++;
        });
        if (totalFamily === 0) {
          totalFamily = (typeof SSN_INVESTORS !== "undefined" && SSN_INVESTORS.length) ? SSN_INVESTORS.length : 1;
          adultCount = totalFamily;
        }
      }

      const mfjEligible = totalFamily > 1;
      let isMfj;
      if (!mfjEligible) {
        isMfj = false;
      } else if (typeof simMarriedFilingJointly === "boolean") {
        isMfj = simMarriedFilingJointly;
      } else if (tr.married_filing_jointly != null) {
        isMfj = Boolean(tr.married_filing_jointly);
      } else {
        isMfj = adultCount >= 2;
      }

      let rawFloor = (typeof simMinTaxBracketFloor !== "undefined" && simMinTaxBracketFloor != null && String(simMinTaxBracketFloor).trim() !== "")
        ? simMinTaxBracketFloor
        : (tr.min_tax_bracket_floor ?? "none");
      let floorKey = String(rawFloor).trim().toLowerCase().replace(/%/g, "");
      if (!(floorKey in FLOOR_TAXABLE_STARTS_MFJ)) floorKey = "none";

      const fedLtcgTop = tr.fed_ltcg_top != null ? Number(tr.fed_ltcg_top) : 0.20;
      const niitRate = tr.niit_rate != null ? Number(tr.niit_rate) : 0.038;
      const fedOrdTop = tr.fed_ordinary_top != null ? Number(tr.fed_ordinary_top) : 0.37;
      let stateRate = 0.0;
      if (tr.state_rate != null) {
        stateRate = Number(tr.state_rate);
      } else if (tr.ltcg_top != null) {
        stateRate = Math.max(0.0, Math.round((Number(tr.ltcg_top) - fedLtcgTop - niitRate) * 10000) / 10000);
      }
      const stateSsnRate = Number(tr.state_ssn_rate || 0.0);
      const ssnFedCap = Number(tr.ssn_fed_taxable_portion || 0.85);
      const netRetRate = Math.max(0.25, Math.min(0.95, Number(NET_RATE || 0.50)));

      let defBy = (typeof RMD_BIRTH_YEAR !== "undefined" ? RMD_BIRTH_YEAR : 1975), defBm = 1, defBd = 1;
      const ssnList = (typeof SSN_INVESTORS !== "undefined" && SSN_INVESTORS.length) ? SSN_INVESTORS : invs;
      const rmdCandidates = famMembers.length > 0 ? famMembers : [...invs, ...bens, ...ssnList];
      const rmdInv = rmdCandidates.find(x => x && (x.is_rmd_owner || x.rmd_owner)) || rmdCandidates[0];
      if (rmdInv) {
        const bRaw = String(rmdInv.birth_date || rmdInv.birth_iso || "");
        const bPts = bRaw.split("-");
        defBy = Number(rmdInv.birth_year || (/^\d{4}$/.test(bPts[0] || "") ? parseInt(bPts[0], 10) : 0)) || 1975;
        defBm = Number(rmdInv.birth_month || (/^\d{1,2}$/.test(bPts[1] || "") ? parseInt(bPts[1], 10) : 1)) || 1;
        defBd = Number(rmdInv.birth_day) || 1;
      }

      return {
        progressive: true,
        is_mfj: isMfj,
        mfj_eligible: mfjEligible,
        total_family: totalFamily,
        adult_count: adultCount,
        floor_key: floorKey,
        fed_ltcg_top: fedLtcgTop,
        fed_ord_top: fedOrdTop,
        niit_rate: niitRate,
        state_rate: stateRate,
        state_ssn_rate: stateSsnRate,
        ssn_fed_cap: ssnFedCap,
        net_retention_rate: netRetRate,
        default_birth_year: defBy,
        default_birth_month: defBm,
        default_birth_day: defBd,
        work_plan: buildWorkPlanConfig(),
      };
    }

    const IRS_CATCHUP_50_USD = 8000.0;
    const IRS_SUPER_CATCHUP_60_63_USD = 11250.0;
    const MAX_WORK_PLAN_ANNUAL_USD_JS = 50000000;
    const _SAFE_BUCKET_ID_RE_JS = /^[A-Za-z0-9_.:-]{0,80}$/;

    function sanitizeWorkPlanJs(rawWp, milestoneAge = 85, clampBudget = false) {
      const msClamped = Math.max(18, Math.min(100, Math.round(Number(milestoneAge) || 85)));
      const defAge = Math.min(65, msClamped);
      if (!rawWp || typeof rawWp !== "object") {
        return {
          is_working: false,
          default_retirement_age: defAge,
          annual_take_home: 0.0,
          budget_answered: false,
          covers_full_burn: false,
          annual_budget_offset: 0.0,
          contributions: [],
        };
      }
      const isWorking = Boolean(rawWp.is_working);
      let retAge = defAge;
      if (rawWp.default_retirement_age != null && typeof rawWp.default_retirement_age !== "boolean" && Number.isFinite(Number(rawWp.default_retirement_age))) {
        retAge = Math.round(Number(rawWp.default_retirement_age));
      }
      retAge = Math.max(18, Math.min(msClamped, retAge));
      const thRaw = Number(rawWp.annual_take_home || 0);
      const takeHomeUsd = Number.isFinite(thRaw) ? Math.round(Math.max(0, Math.min(MAX_WORK_PLAN_ANNUAL_USD_JS, thRaw)) * 100) / 100 : 0.0;
      const coversFull = rawWp.covers_full_burn !== undefined ? Boolean(rawWp.covers_full_burn) : false;
      const offRaw = Number(rawWp.annual_budget_offset || 0);
      let offsetUsd = Number.isFinite(offRaw) ? Math.round(Math.max(0, Math.min(MAX_WORK_PLAN_ANNUAL_USD_JS, offRaw)) * 100) / 100 : 0.0;
      if (clampBudget && takeHomeUsd > 0 && offsetUsd > takeHomeUsd) {
        offsetUsd = takeHomeUsd;
      }
      const contribs = [];
      if (Array.isArray(rawWp.contributions)) {
        rawWp.contributions.slice(0, 10).forEach(row => {
          if (!row || typeof row !== "object") return;
          let tid = String(row.target_asset_id || "").trim();
          if (!_SAFE_BUCKET_ID_RE_JS.test(tid)) tid = "";
          let mid = String(row.match_target_asset_id || "").trim();
          if (!_SAFE_BUCKET_ID_RE_JS.test(mid) || mid === tid) mid = "";
          const empR = Number(row.employee_annual || 0);
          const empUsd = Number.isFinite(empR) ? Math.round(Math.max(0, Math.min(MAX_WORK_PLAN_ANNUAL_USD_JS, empR)) * 100) / 100 : 0.0;
          const mR = Number(row.employer_match_annual || 0);
          const matchUsd = Number.isFinite(mR) ? Math.round(Math.max(0, Math.min(MAX_WORK_PLAN_ANNUAL_USD_JS, mR)) * 100) / 100 : 0.0;
          const mrRaw = Number(row.match_rate_pct || 0);
          const matchRatePct = Number.isFinite(mrRaw) ? Math.round(Math.max(0, Math.min(200, mrRaw)) * 100) / 100 : 0.0;
          const rawPacing = String(row.pacing || "monthly").trim().toLowerCase();
          const pacing = rawPacing === "q1_frontloaded" ? "q1_frontloaded" : "monthly";
          const catchup = Boolean(row.age_50_catchup);
          if (!tid && empUsd <= 0 && matchUsd <= 0 && !catchup) return;
          contribs.push({
            target_asset_id: tid,
            employee_annual: empUsd,
            employer_match_annual: matchUsd,
            match_rate_pct: matchRatePct,
            match_target_asset_id: mid,
            age_50_catchup: catchup,
            pacing,
          });
        });
      }
      const budgetAnswered = Boolean(rawWp.budget_answered || offsetUsd > 0 || coversFull || contribs.length > 0);
      return {
        is_working: isWorking,
        default_retirement_age: retAge,
        annual_take_home: takeHomeUsd,
        budget_answered: budgetAnswered,
        covers_full_burn: coversFull,
        annual_budget_offset: offsetUsd,
        contributions: contribs,
      };
    }

    function buildWorkPlanConfig() {
      const startYm = String(TODAY_ISO || "2026-01-01").slice(0, 7);
      const projFams = Array.isArray(PROJECT_CFG.family_members) ? PROJECT_CFG.family_members : [];
      const projInvs = Array.isArray(PROJECT_CFG.investors) ? PROJECT_CFG.investors : [];
      const projBens = Array.isArray(PROJECT_CFG.beneficiaries) ? PROJECT_CFG.beneficiaries : [];
      const ssnInvs = (typeof SSN_INVESTORS !== "undefined" && Array.isArray(SSN_INVESTORS)) ? SSN_INVESTORS : [];

      const rawMembers = [];
      if (projFams.length > 0) {
        projFams.forEach((m, idx) => {
          if (m && typeof m === "object") {
            rawMembers.push(Object.assign({}, m, {
              is_ssn_contributor: Boolean(m.is_ssn_contributor != null ? m.is_ssn_contributor : (idx < projInvs.length)),
            }));
          }
        });
      } else if (projInvs.length > 0 || projBens.length > 0) {
        projInvs.forEach(inv => {
          if (inv && typeof inv === "object") rawMembers.push(Object.assign({}, inv, { is_ssn_contributor: true }));
        });
        projBens.forEach(ben => {
          if (ben && typeof ben === "object") rawMembers.push(Object.assign({}, ben, { is_ssn_contributor: false }));
        });
      } else {
        ssnInvs.forEach(sInv => {
          if (sInv && typeof sInv === "object") {
            rawMembers.push(Object.assign({}, sInv, { is_ssn_contributor: Boolean(sInv.is_ssn_contributor !== false) }));
          }
        });
      }

      const anySsn = rawMembers.some(m => Boolean(m.is_ssn_contributor));
      const memOvrMap = (typeof simMemberRetirementAges === "object" && simMemberRetirementAges) ? simMemberRetirementAges : {};
      const workers = [];
      const allInvestors = [];
      let ssnRank = 0;

      rawMembers.forEach((pMem, idx) => {
        const isSsn = Boolean(pMem.is_ssn_contributor);
        let sInv = {};
        if (isSsn && ssnRank < ssnInvs.length && ssnInvs[ssnRank] && typeof ssnInvs[ssnRank] === "object") {
          sInv = ssnInvs[ssnRank];
        } else if (!anySsn && idx === 0 && ssnInvs[0] && typeof ssnInvs[0] === "object") {
          sInv = ssnInvs[0];
        }

        const bStr = String(pMem.birth_date || pMem.birth_iso || sInv.birth_iso || "");
        let bYr = Number(pMem.birth_year || 0);
        let bMo = Number(pMem.birth_month || 0);
        if ((bYr <= 0 || bMo <= 0) && bStr) {
          const parts = bStr.split("-").slice(0, 2).map(x => parseInt(x, 10));
          if (bYr <= 0 && Number.isFinite(parts[0])) bYr = parts[0];
          if (bMo <= 0 && Number.isFinite(parts[1])) bMo = parts[1];
        }
        if (bYr <= 0) bYr = Number(sInv.birth_year || 1975);
        if (bMo <= 0) bMo = Number(sInv.birth_month || 1);
        bMo = Math.max(1, Math.min(12, bMo));

        const msRaw = Number(pMem.milestone_age ?? sInv.milestone_age ?? 85);
        const msAge = Number.isFinite(msRaw) && msRaw > 0 ? Math.max(18, Math.min(100, Math.round(msRaw))) : 85;
        const rawWp = (pMem.work_plan && typeof pMem.work_plan === "object") ? pMem.work_plan : sInv.work_plan;
        const wp = sanitizeWorkPlanJs(rawWp, msAge, true);

        const memKey = `member_${idx}_retirement_age`;
        let scenOvr = memOvrMap[memKey];
        if (scenOvr === undefined || scenOvr === null) scenOvr = memOvrMap[String(idx)];
        if (scenOvr === undefined || scenOvr === null) {
          if (isSsn && ssnRank < 2) {
            scenOvr = ssnRank === 0 ? simInvestor1RetirementAge : simInvestor2RetirementAge;
          } else if (!anySsn && idx < 2) {
            scenOvr = idx === 0 ? simInvestor1RetirementAge : simInvestor2RetirementAge;
          }
        }
        const curSsnRank = isSsn ? ssnRank : -1;
        if (isSsn) ssnRank++;

        let effRetAge = wp.default_retirement_age;
        let isActive = Boolean(wp.is_working);
        if (scenOvr != null && typeof scenOvr !== "boolean" && scenOvr !== "" && Number.isFinite(Number(scenOvr))) {
          effRetAge = Math.max(18, Math.min(msAge, Math.round(Number(scenOvr))));
          isActive = true;
        }
        const retYm = `${String(bYr + effRetAge).padStart(4, "0")}-${String(bMo).padStart(2, "0")}`;
        const name = String(pMem.name || sInv.name || `Member ${idx + 1}`);
        const entry = {
          idx,
          member_key: memKey,
          is_ssn_contributor: isSsn,
          ssn_rank: curSsnRank,
          name,
          birth_year: bYr,
          birth_month: bMo,
          milestone_age: msAge,
          retirement_age: effRetAge,
          default_retirement_age: wp.default_retirement_age,
          ret_ym: retYm,
          base_is_working: Boolean(wp.is_working),
          is_working: isActive && (retYm > startYm),
          annual_take_home: Number(wp.annual_take_home || 0),
          covers_full_burn: Boolean(wp.covers_full_burn),
          annual_budget_offset: Number(wp.annual_budget_offset || 0),
          contributions: wp.contributions,
        };
        allInvestors.push(entry);
        if (entry.is_working) workers.push(entry);
      });
      return { start_ym: startYm, workers, investors: allInvestors };
    }

    function computeDrawdownStartMonth(wpCfg, todayIso, annBurn, infMonthlyRate) {
      const workers = (wpCfg && Array.isArray(wpCfg.workers)) ? wpCfg.workers : [];
      if (!workers.length) return 1;
      const startYear = parseInt(String(todayIso || "2026-01-01").slice(0, 4), 10) || 2026;
      const startMonth0 = (parseInt(String(todayIso || "2026-01-01").slice(5, 7), 10) || 1) - 1;
      if (annBurn <= 0.01) {
        for (let m = 1; m <= SIM_TOTAL_MONTHS; m++) {
          const totM = startYear * 12 + startMonth0 + m;
          const ym = `${String(Math.floor(totM / 12)).padStart(4, "0")}-${String((totM % 12) + 1).padStart(2, "0")}`;
          const activeW = workers.filter(w => ym < w.ret_ym);
          if (!activeW.length || !activeW.some(w => w.covers_full_burn)) return m;
        }
        return SIM_TOTAL_MONTHS + 1;
      }
      for (let m = 1; m <= SIM_TOTAL_MONTHS; m++) {
        const totM = startYear * 12 + startMonth0 + m;
        const ym = `${String(Math.floor(totM / 12)).padStart(4, "0")}-${String((totM % 12) + 1).padStart(2, "0")}`;
        const activeW = workers.filter(w => ym < w.ret_ym);
        if (!activeW.length) return m;
        if (activeW.some(w => w.covers_full_burn)) continue;
        const dfl = Math.pow(1.0 + infMonthlyRate, m);
        const rawMoBurn = (annBurn * dfl) / 12.0;
        const offMo = activeW.reduce((s, w) => s + (w.annual_budget_offset * dfl) / 12.0, 0.0);
        if (rawMoBurn - offMo > 0.01) return m;
      }
      return SIM_TOTAL_MONTHS + 1;
    }

    function depositWorkPlanLeg(stateMap, targetId, iso, nomAmt, isEmployerMatch = false) {
      if (nomAmt <= 0 || !targetId) return 0.0;
      const st = stateMap[targetId];
      if (!st || st.is_unvested_goog || isSimBucketLocked(st, iso)) return 0.0;
      const tm = String(st.tax_model || "");
      if (tm === "ORDINARY_FULL") {
        st.val += nomAmt;
      } else if (tm === "TAX_FREE") {
        st.val += nomAmt;
        if (!isEmployerMatch) st.costBasis += nomAmt;
      } else if (Array.isArray(st.tranches) && tm === "LTCG_TRANCHES") {
        st.tranches.push({ vestIso: iso, shares: 0.0, vested: true, val: nomAmt, costBasis: nomAmt });
        st.val = st.tranches.reduce((s, t) => s + t.val, 0.0);
        st.costBasis = st.tranches.reduce((s, t) => s + t.costBasis, 0.0);
      } else {
        st.val += nomAmt;
        st.costBasis += nomAmt;
      }
      return nomAmt;
    }

    function ensureTaxYtd(stateMap, iso, dfl) {
      const tc = stateMap._taxCfg;
      if (!tc || !tc.progressive) return null;
      const yr = parseInt(iso.slice(0, 4), 10);
      let ytd = stateMap._taxYtd;
      if (!ytd || ytd.year !== yr) {
        const isMfj = Boolean(tc.is_mfj);
        const stdDed = FED_STD_DED_2026[isMfj] * dfl;
        const flMap = isMfj ? FLOOR_TAXABLE_STARTS_MFJ : FLOOR_TAXABLE_STARTS_SINGLE;
        const flStart = flMap[String(tc.floor_key || "none")] ?? -1.0;
        const initAgi = flStart < 0 ? 0.0 : (stdDed + flStart * dfl);
        ytd = {
          year: yr,
          dfl,
          agi_ord: initAgi,
          init_floor_ord: initAgi,
          rmd_ord: 0.0,
          vest_ord: 0.0,
          yield_ord: 0.0,
          ssn_taxable: 0.0,
          ltcg: 0.0,
          muni_int: 0.0,
          ord_loss_offset_used: 0.0,
        };
        stateMap._taxYtd = ytd;
      } else {
        ytd.dfl = dfl;
      }
      return ytd;
    }

    function ownerIsPre59Half(st, stateMap, iso) {
      const tc = stateMap._taxCfg;
      if (!tc) return false;
      const oby = Number(st.owner_birth_year || st.ownerBirthYear || tc.default_birth_year || 1975);
      const obm = Number(st.owner_birth_month || st.ownerBirthMonth || tc.default_birth_month || 1);
      const obd = Number(st.owner_birth_day || st.ownerBirthDay || tc.default_birth_day || 1);
      const yr = parseInt(iso.slice(0, 4), 10);
      const mo = parseInt(iso.slice(5, 7), 10);
      const dy = parseInt(iso.slice(8, 10), 10);
      const age = (yr - oby) + ((mo - obm) + (dy - obd) / 30.4375) / 12.0;
      return age < (59.5 - 1e-6);
    }

    function _inheritRolloverOwnerMeta(fromSt, toSt) {
      if (!fromSt || !toSt) return;
      const dstIdx = (toSt.rmd_owner_index !== null && toSt.rmd_owner_index !== undefined && toSt.rmd_owner_index !== "")
        ? toSt.rmd_owner_index
        : ((toSt.rmdOwnerIndex !== null && toSt.rmdOwnerIndex !== undefined && toSt.rmdOwnerIndex !== "") ? toSt.rmdOwnerIndex : null);
      const srcIdx = (fromSt.rmd_owner_index !== null && fromSt.rmd_owner_index !== undefined && fromSt.rmd_owner_index !== "")
        ? fromSt.rmd_owner_index
        : ((fromSt.rmdOwnerIndex !== null && fromSt.rmdOwnerIndex !== undefined && fromSt.rmdOwnerIndex !== "") ? fromSt.rmdOwnerIndex : null);
      const srcBy = fromSt.owner_birth_year || fromSt.ownerBirthYear || null;
      if (dstIdx == null && (srcIdx != null || srcBy)) {
        const srcId = String(fromSt.owner_id || fromSt.ownerId || "").trim();
        const srcBm = fromSt.owner_birth_month || fromSt.ownerBirthMonth || null;
        const srcBd = fromSt.owner_birth_day || fromSt.ownerBirthDay || null;
        if (srcIdx != null) {
          toSt.rmd_owner_index = srcIdx;
          toSt.rmdOwnerIndex = srcIdx;
        }
        if (srcId) {
          toSt.owner_id = srcId;
          toSt.ownerId = srcId;
        }
        if (srcBy) {
          toSt.owner_birth_year = Number(srcBy);
          toSt.ownerBirthYear = Number(srcBy);
        }
        if (srcBm) {
          toSt.owner_birth_month = Number(srcBm);
          toSt.ownerBirthMonth = Number(srcBm);
        }
        if (srcBd) {
          toSt.owner_birth_day = Number(srcBd);
          toSt.ownerBirthDay = Number(srcBd);
        }
      }
    }

    function applyHypotheticalRollovers(stateOrAssets, rollovers) {
      if (!stateOrAssets || !Array.isArray(rollovers) || rollovers.length === 0) return stateOrAssets;
      const isArray = Array.isArray(stateOrAssets);
      const byId = isArray
        ? Object.fromEntries(stateOrAssets.filter(Boolean).map(a => [a.id, a]))
        : stateOrAssets;
      rollovers.forEach(tr => {
        if (!tr || typeof tr !== "object") return;
        const fromId = String(tr.from_id || tr.from_asset_id || tr.from || "").trim();
        const toId = String(tr.to_id || tr.to_asset_id || tr.to || "").trim();
        const fromSt = byId[fromId];
        const toSt = byId[toId];
        if (!fromSt || !toSt || fromSt === toSt) return;
        _inheritRolloverOwnerMeta(fromSt, toSt);
      });
      return stateOrAssets;
    }

    function isShortTermLot(lot, iso) {
      const viso = String(lot.vestIso || "");
      if (viso.length < 10 || !iso || iso.length < 10) return false;
      const t0 = Date.UTC(parseInt(viso.slice(0, 4), 10), parseInt(viso.slice(5, 7), 10) - 1, parseInt(viso.slice(8, 10), 10));
      const t1 = Date.UTC(parseInt(iso.slice(0, 4), 10), parseInt(iso.slice(5, 7), 10) - 1, parseInt(iso.slice(8, 10), 10));
      if (!Number.isFinite(t0) || !Number.isFinite(t1)) return false;
      const acquired = new Date(t0);
      const year = acquired.getUTCFullYear() + 1;
      const month = acquired.getUTCMonth();
      const lastDay = new Date(Date.UTC(year, month + 1, 0)).getUTCDate();
      const anniversary = Date.UTC(year, month, Math.min(acquired.getUTCDate(), lastDay));
      return t1 >= t0 && t1 <= anniversary;
    }

    function ordMarginalRateAt(agiOrdPos, magiPos, tc, dfl, applyNiit, effStateRate, extraPenalty) {
      const isMfj = Boolean(tc.is_mfj);
      const stdDed = FED_STD_DED_2026[isMfj] * dfl;
      let rate = extraPenalty;
      if (agiOrdPos >= stdDed - 1e-9) {
        const taxOrd = Math.max(0.0, agiOrdPos - stdDed);
        const tops = isMfj ? FED_ORD_TOPS_MFJ : FED_ORD_TOPS_SINGLE;
        let fedR = Number(tc.fed_ord_top);
        for (let i = 0; i < tops.length; i++) {
          if (taxOrd < tops[i] * dfl - 1e-9) { fedR = FED_ORD_LOW_RATES[i]; break; }
        }
        rate += fedR + effStateRate;
      }
      if (applyNiit) {
        const niitT = isMfj ? 250000.0 : 200000.0;
        if (magiPos >= niitT - 1e-9) rate += Number(tc.niit_rate);
      }
      return Math.min(0.95, Math.max(0.0, rate));
    }

    function ordDxBreakpoints(curOrd, curMagi, maxDx, lossCap, tc, dfl, applyNiit) {
      const isMfj = Boolean(tc.is_mfj);
      const stdDed = FED_STD_DED_2026[isMfj] * dfl;
      const tops = isMfj ? FED_ORD_TOPS_MFJ : FED_ORD_TOPS_SINGLE;
      const pts = [0.0, maxDx];
      if (lossCap > 0.0 && lossCap < maxDx) pts.push(lossCap);
      const rawThresholds = [stdDed, ...tops.map(t => stdDed + t * dfl)];
      for (let i = 0; i < rawThresholds.length; i++) {
        const dx = lossCap + (rawThresholds[i] - curOrd);
        if (dx > 0.0 && dx < maxDx) pts.push(dx);
      }
      if (applyNiit) {
        const niitT = isMfj ? 250000.0 : 200000.0;
        const dxN = lossCap + (niitT - curMagi);
        if (dxN > 0.0 && dxN < maxDx) pts.push(dxN);
      }
      pts.sort((a, b) => a - b);
      const uniq = [pts[0]];
      for (let i = 1; i < pts.length; i++) {
        if (pts[i] - uniq[uniq.length - 1] > 1e-12) uniq.push(pts[i]);
      }
      return uniq;
    }

    function taxOrdinaryIncrement(stateMap, grossAmount, iso, dfl, applyNiit, stateRateOverride = null, extraPenalty = 0.0, allowLossOffset = true, commit = true) {
      if (grossAmount <= 1e-9) return { tax: 0.0, lossSheltered: 0.0, taxSaved: 0.0 };
      const tc = stateMap._taxCfg;
      const ytd = ensureTaxYtd(stateMap, iso, dfl);
      if (!tc || !ytd) return { tax: 0.0, lossSheltered: 0.0, taxSaved: 0.0 };
      const effState = stateRateOverride == null ? Number(tc.state_rate) : Number(stateRateOverride);
      const availLoss = Number(stateMap._lossCarryforward || 0.0);
      const remOrdOffset = allowLossOffset ? Math.max(0.0, 3000.0 - Number(ytd.ord_loss_offset_used || 0.0)) : 0.0;
      const lossCap = Math.min(grossAmount, availLoss, remOrdOffset);

      const curOrd = Number(ytd.agi_ord);
      const curMagi = curOrd + Number(ytd.ltcg);
      const pts = ordDxBreakpoints(curOrd, curMagi, grossAmount, lossCap, tc, dfl, applyNiit);
      let tax = 0.0;
      for (let i = 0; i < pts.length - 1; i++) {
        const a0 = pts[i], a1 = pts[i + 1];
        const seg = a1 - a0;
        if (seg <= 1e-12) continue;
        const mid = 0.5 * (a0 + a1);
        if (mid < lossCap) {
          tax += seg * extraPenalty;
        } else {
          const taxableDx = mid - lossCap;
          const mr = ordMarginalRateAt(curOrd + taxableDx, curMagi + taxableDx, tc, dfl, applyNiit, effState, extraPenalty);
          tax += seg * mr;
        }
      }
      let taxSaved = 0.0;
      if (lossCap > 0.005) {
        const mrSaved = ordMarginalRateAt(curOrd + 0.5 * lossCap, curMagi + 0.5 * lossCap, tc, dfl, applyNiit, effState, 0.0);
        taxSaved = lossCap * mrSaved;
        if (commit) {
          stateMap._lossCarryforward = Math.max(0.0, availLoss - lossCap);
          stateMap._totalLossUsed = (stateMap._totalLossUsed || 0) + lossCap;
          stateMap._totalTaxSavedByLoss = (stateMap._totalTaxSavedByLoss || 0) + taxSaved;
          ytd.ord_loss_offset_used = (ytd.ord_loss_offset_used || 0) + lossCap;
        }
      }
      if (commit) ytd.agi_ord = curOrd + Math.max(0.0, grossAmount - lossCap);
      return { tax, lossSheltered: lossCap, taxSaved };
    }

    // DA2-06 PARITY INVARIANT: Exact piecewise STCG net-to-gross solver (1:1 mirror of `_gross_for_net_stcg` in `simulator.py`).
    function grossForNetStcg(stateMap, lotVal, gainRatio, targetNet, iso, dfl, applyState = true) {
      if (lotVal <= 1e-9 || targetNet <= 1e-9 || gainRatio <= 1e-9) return Math.min(lotVal, Math.max(0.0, targetNet));
      const tc = stateMap._taxCfg;
      const ytd = ensureTaxYtd(stateMap, iso, dfl);
      if (!tc || !ytd) return Math.min(lotVal, targetNet);
      const effState = applyState ? Number(tc.state_rate) : 0.0;
      const curOrd = Number(ytd.agi_ord);
      const curMagi = curOrd + Number(ytd.ltcg);
      const maxU = lotVal * gainRatio;
      const pts = ordDxBreakpoints(curOrd, curMagi, maxU, 0.0, tc, dfl, true);
      let remNet = targetNet;
      let grossAcc = 0.0;
      for (let i = 0; i < pts.length - 1; i++) {
        const u0 = pts[i], u1 = pts[i + 1];
        const segU = u1 - u0;
        if (segU <= 1e-12) continue;
        const segGross = segU / gainRatio;
        const mid = 0.5 * (u0 + u1);
        const mrGain = ordMarginalRateAt(curOrd + mid, curMagi + mid, tc, dfl, true, effState, 0.0);
        const nf = Math.max(0.01, 1.0 - mrGain * gainRatio);
        const segNet = segGross * nf;
        if (remNet <= segNet + 1e-9) {
          grossAcc += Math.min(segGross, remNet / nf);
          return Math.min(lotVal, grossAcc);
        }
        grossAcc += segGross;
        remNet -= segNet;
      }
      return lotVal;
    }

    function grossForNetOrdinary(stateMap, maxGross, targetNet, iso, dfl, applyNiit, stateRateOverride = null, extraPenalty = 0.0, allowLossOffset = true) {
      if (maxGross <= 1e-9 || targetNet <= 1e-9) return 0.0;
      const tc = stateMap._taxCfg;
      const ytd = ensureTaxYtd(stateMap, iso, dfl);
      if (!tc || !ytd) return Math.min(maxGross, targetNet);
      const effState = stateRateOverride == null ? Number(tc.state_rate) : Number(stateRateOverride);
      const availLoss = Number(stateMap._lossCarryforward || 0.0);
      const remOrdOffset = allowLossOffset ? Math.max(0.0, 3000.0 - Number(ytd.ord_loss_offset_used || 0.0)) : 0.0;
      const lossCap = Math.min(maxGross, availLoss, remOrdOffset);

      const curOrd = Number(ytd.agi_ord);
      const curMagi = curOrd + Number(ytd.ltcg);
      const pts = ordDxBreakpoints(curOrd, curMagi, maxGross, lossCap, tc, dfl, applyNiit);
      let remNet = targetNet;
      let grossAcc = 0.0;
      for (let i = 0; i < pts.length - 1; i++) {
        const a0 = pts[i], a1 = pts[i + 1];
        const seg = a1 - a0;
        if (seg <= 1e-12) continue;
        const mid = 0.5 * (a0 + a1);
        const mr = mid < lossCap
          ? extraPenalty
          : ordMarginalRateAt(curOrd + (mid - lossCap), curMagi + (mid - lossCap), tc, dfl, applyNiit, effState, extraPenalty);
        const nf = Math.max(0.01, 1.0 - mr);
        const segNet = seg * nf;
        if (remNet <= segNet + 1e-9) {
          grossAcc += Math.min(seg, remNet / nf);
          return Math.min(maxGross, grossAcc);
        }
        grossAcc += seg;
        remNet -= segNet;
      }
      return maxGross;
    }

    function ltcgMarginalRateAt(stackPos, magiPos, tc, dfl, effStateRate) {
      const isMfj = Boolean(tc.is_mfj);
      const stdDed = FED_STD_DED_2026[isMfj] * dfl;
      const tops = isMfj ? FED_LTCG_TOPS_MFJ : FED_LTCG_TOPS_SINGLE;
      let fedR;
      if (stackPos < tops[0] * dfl - 1e-9) fedR = 0.0;
      else if (stackPos < tops[1] * dfl - 1e-9) fedR = 0.15;
      else fedR = Number(tc.fed_ltcg_top);
      const niitT = isMfj ? 250000.0 : 200000.0;
      const niitR = magiPos >= niitT - 1e-9 ? Number(tc.niit_rate) : 0.0;
      const stR = magiPos >= stdDed - 1e-9 ? effStateRate : 0.0;
      return Math.min(0.95, Math.max(0.0, fedR + niitR + stR));
    }

    function ltcgUBreakpoints(stackBase, curMagi, maxU, tc, dfl) {
      const isMfj = Boolean(tc.is_mfj);
      const stdDed = FED_STD_DED_2026[isMfj] * dfl;
      const tops = isMfj ? FED_LTCG_TOPS_MFJ : FED_LTCG_TOPS_SINGLE;
      const niitT = isMfj ? 250000.0 : 200000.0;
      const pts = [0.0, maxU];
      for (let i = 0; i < tops.length; i++) {
        const du = tops[i] * dfl - stackBase;
        if (du > 0.0 && du < maxU) pts.push(du);
      }
      for (const mT of [stdDed, niitT]) {
        const duM = mT - curMagi;
        if (duM > 0.0 && duM < maxU) pts.push(duM);
      }
      pts.sort((a, b) => a - b);
      const uniq = [pts[0]];
      for (let i = 1; i < pts.length; i++) {
        if (pts[i] - uniq[uniq.length - 1] > 1e-12) uniq.push(pts[i]);
      }
      return uniq;
    }

    function taxLtcgIncrement(stateMap, taxableGain, iso, dfl, applyState = true, commit = true) {
      if (taxableGain <= 1e-9) return 0.0;
      const tc = stateMap._taxCfg;
      const ytd = ensureTaxYtd(stateMap, iso, dfl);
      if (!tc || !ytd) return 0.0;
      const isMfj = Boolean(tc.is_mfj);
      const stdDed = FED_STD_DED_2026[isMfj] * dfl;
      const effState = applyState ? Number(tc.state_rate) : 0.0;
      const curOrdTaxable = Math.max(0.0, Number(ytd.agi_ord) - stdDed);
      const stackBase = curOrdTaxable + Number(ytd.ltcg);
      const curMagi = Number(ytd.agi_ord) + Number(ytd.ltcg);
      const pts = ltcgUBreakpoints(stackBase, curMagi, taxableGain, tc, dfl);
      let tax = 0.0;
      for (let i = 0; i < pts.length - 1; i++) {
        const u0 = pts[i], u1 = pts[i + 1];
        const seg = u1 - u0;
        if (seg <= 1e-12) continue;
        const mid = 0.5 * (u0 + u1);
        tax += seg * ltcgMarginalRateAt(stackBase + mid, curMagi + mid, tc, dfl, effState);
      }
      if (commit) ytd.ltcg = Number(ytd.ltcg) + taxableGain;
      return tax;
    }

    function grossForNetLtcg(stateMap, lotVal, gainRatio, targetNet, iso, dfl, applyState = true) {
      if (lotVal <= 1e-9 || targetNet <= 1e-9 || gainRatio <= 1e-9) return Math.min(lotVal, Math.max(0.0, targetNet));
      const tc = stateMap._taxCfg;
      const ytd = ensureTaxYtd(stateMap, iso, dfl);
      if (!tc || !ytd) return Math.min(lotVal, targetNet);
      const isMfj = Boolean(tc.is_mfj);
      const stdDed = FED_STD_DED_2026[isMfj] * dfl;
      const effState = applyState ? Number(tc.state_rate) : 0.0;
      const curOrdTaxable = Math.max(0.0, Number(ytd.agi_ord) - stdDed);
      const stackBase = curOrdTaxable + Number(ytd.ltcg);
      const curMagi = Number(ytd.agi_ord) + Number(ytd.ltcg);
      const maxU = lotVal * gainRatio;
      const pts = ltcgUBreakpoints(stackBase, curMagi, maxU, tc, dfl);
      let remNet = targetNet;
      let grossAcc = 0.0;
      for (let i = 0; i < pts.length - 1; i++) {
        const u0 = pts[i], u1 = pts[i + 1];
        const segU = u1 - u0;
        if (segU <= 1e-12) continue;
        const segGross = segU / gainRatio;
        const mid = 0.5 * (u0 + u1);
        const mrGain = ltcgMarginalRateAt(stackBase + mid, curMagi + mid, tc, dfl, effState);
        const nf = Math.max(0.01, 1.0 - mrGain * gainRatio);
        const segNet = segGross * nf;
        if (remNet <= segNet + 1e-9) {
          grossAcc += Math.min(segGross, remNet / nf);
          return Math.min(lotVal, grossAcc);
        }
        grossAcc += segGross;
        remNet -= segNet;
      }
      return lotVal;
    }

    // DA2-05 PARITY INVARIANT (lockstep with `_compute_progressive_ssn_mo` in `simulator.py`):
    // Annualize recurring non-SSN income when testing IRC §86 full-year `$32k/$44k` (`$25k/$34k` Single) thresholds.
    function computeProgressiveSsnMo(stateMap, ssnGrossMo, iso, dfl, estJanNonSsnIncome = 0.0) {
      if (ssnGrossMo <= 1e-6) return { tax: 0.0, net: 0.0 };
      const tc = stateMap._taxCfg;
      const ytd = ensureTaxYtd(stateMap, iso, dfl);
      if (!tc || !ytd) return { tax: 0.0, net: ssnGrossMo };
      const isMfj = Boolean(tc.is_mfj);
      const t1 = isMfj ? 32000.0 : 25000.0;
      const t2 = isMfj ? 44000.0 : 34000.0;
      const annSsn = ssnGrossMo * 12.0;
      const moNum = Math.max(1, Math.min(12, parseInt(iso.slice(5, 7), 10) || 1));
      const lumpOrd = Number(ytd.init_floor_ord || 0.0) + Number(ytd.rmd_ord || 0.0) + Number(ytd.vest_ord || 0.0);
      const yieldOrdYtd = Number(ytd.yield_ord || 0.0);
      const muniYtd = Number(ytd.muni_int || 0.0);
      const ssnTaxableYtd = Number(ytd.ssn_taxable || 0.0);
      const annYieldAndMuni = (yieldOrdYtd + muniYtd) * (12.0 / moNum);
      let annDraw = 0.0;
      if (moNum > 1) {
        const drawOrdYtd = Math.max(0.0, Number(ytd.agi_ord) - lumpOrd - yieldOrdYtd - ssnTaxableYtd);
        annDraw = (drawOrdYtd + Number(ytd.ltcg)) * (12.0 / (moNum - 1.0));
      } else {
        annDraw = Math.max(0.0, estJanNonSsnIncome);
      }
      const prov = lumpOrd + annYieldAndMuni + annDraw + 0.5 * annSsn;
      let taxableFrac = 0.0;
      if (prov <= t1) {
        taxableFrac = 0.0;
      } else if (prov <= t2) {
        taxableFrac = Math.min(0.5, (0.5 * (prov - t1)) / annSsn);
      } else {
        const tier1Max = 0.5 * (t2 - t1);
        taxableFrac = Math.min(0.85, (tier1Max + 0.85 * (prov - t2)) / annSsn);
      }
      taxableFrac = Math.min(Number(tc.ssn_fed_cap ?? 0.85), Math.max(0.0, taxableFrac));
      const taxableMo = ssnGrossMo * taxableFrac;
      ytd.ssn_taxable = ssnTaxableYtd + taxableMo;
      const fedRes = taxOrdinaryIncrement(stateMap, taxableMo, iso, dfl, false, 0.0, 0.0, false, true);
      const stTax = ssnGrossMo * Number(tc.state_ssn_rate || 0.0);
      const totTax = Math.min(ssnGrossMo, fedRes.tax + stTax);
      return { tax: totTax, net: Math.max(0.0, ssnGrossMo - totTax) };
    }

    function cloneSimAssetState() {
      const stateMap = {
        _lossCarryforward: 0,
        _totalLossHarvested: 0,
        _totalLossUsed: 0,
        _totalTaxSavedByLoss: 0,
        _taxCfg: buildTaxScheduleConfig(),
      };
      SIM_ASSETS.forEach(a => {
        const baseStats = {
          firstTappedLbl: null,
          lastTappedLbl: null,
          depletedLbl: null,
          totalGrossSold: 0,
          totalNetProceeds: 0,
          totalTaxPaid: 0,
          totalLossHarvested: 0,
          totalLossSheltered: 0,
          totalTaxSaved: 0,
        };
        if (a.is_unvested_goog) {
          if (!simIncludeUnvested) {
            stateMap[a.id] = {
              ...a,
              ...baseStats,
              val: 0,
              costBasis: 0,
              tranches: [],
            };
          } else {
            const trs = (a.tranches || []).map(t => ({
              vestIso: t.vest_date,
              shares: Number(t.shares) || 0,
              vested: String(t.vest_date) <= TODAY_ISO,
              val: Number(t.current_value) || 0,
              costBasis: Number(t.current_value) || 0,
            }));
            const vSum = trs.reduce((s, t) => s + t.val, 0);
            stateMap[a.id] = {
              ...a,
              ...baseStats,
              val: vSum,
              costBasis: vSum,
              tranches: trs,
            };
          }
        } else if (a.tax_model === "LTCG_TRANCHES") {
          const trs = (a.tranches || []).map(t => ({
            vestIso: t.vest_date,
            shares: Number(t.shares) || 0,
            vested: true,
            val: Number(t.current_value) || 0,
            costBasis: Number(t.cost_basis) || 0,
          }));
          // Fallback to bucket current_value/cost_basis if lot history is absent so Month-0 value isn't zeroed
          if (!trs.length && (Number(a.current_value) || 0) > 0) {
            trs.push({
              vestIso: "",
              shares: Number(a.shares) || 0,
              vested: true,
              val: Number(a.current_value) || 0,
              costBasis: Number(a.cost_basis) || 0,
            });
          }
          let vSum = trs.reduce((s, t) => s + t.val, 0);
          let bSum = trs.reduce((s, t) => s + t.costBasis, 0);
          const bucketCv = Number(a.current_value) || 0;
          const bucketCb = Number(a.cost_basis) || 0;
          if (trs.length > 0 && bucketCv - vSum > 0.01) {
            const diffV = bucketCv - vSum;
            const diffB = Math.max(0, bucketCb - bSum);
            trs.push({ vestIso: "", shares: 0, vested: true, val: diffV, costBasis: diffB });
            vSum += diffV;
            bSum += diffB;
          }
          stateMap[a.id] = {
            ...a,
            ...baseStats,
            val: vSum,
            costBasis: bSum,
            tranches: trs,
          };
        } else {
          stateMap[a.id] = {
            ...a,
            ...baseStats,
            val: Number(a.current_value) || 0,
            costBasis: Number(a.cost_basis) || 0,
            tranches: [],
          };
        }
      });
      return stateMap;
    }

    // Sells from a capital-gain lot (`lot.val`, `lot.costBasis`), harvesting realized capital losses when underwater
    // and applying `stateMap._lossCarryforward` to shelter realized capital gains dollar-for-dollar before paying LTCG tax.
    function sellCapitalGainLot(lot, remNetNeeded, taxRate, stateMap, iso = "", dfl = 1.0, applyState = true) {
      let grossSold = 0;
      let netProceeds = 0;
      let taxPaid = 0;
      let lossHarvested = 0;
      let lossSheltered = 0;
      let taxSaved = 0;

      if (lot.val <= 0.005 || remNetNeeded <= 0.005) {
        return { grossSold, netProceeds, taxPaid, lossHarvested, lossSheltered, taxSaved };
      }

      const tc = stateMap._taxCfg;
      const useProg = Boolean(tc && tc.progressive && iso);

      if (lot.val <= lot.costBasis + 1e-6 || (!useProg && taxRate <= 0)) {
        const takeGross = Math.min(remNetNeeded, lot.val);
        const frac = Math.min(1.0, takeGross / lot.val);
        const basisSold = lot.costBasis * frac;
        const realizedLoss = Math.max(0, basisSold - takeGross);
        lot.val = Math.max(0, lot.val - takeGross);
        lot.costBasis = Math.max(0, lot.costBasis - basisSold);
        grossSold += takeGross;
        netProceeds += takeGross;
        if (realizedLoss > 0.005) {
          lossHarvested += realizedLoss;
          stateMap._lossCarryforward = (stateMap._lossCarryforward || 0) + realizedLoss;
          stateMap._totalLossHarvested = (stateMap._totalLossHarvested || 0) + realizedLoss;
        }
        return { grossSold, netProceeds, taxPaid, lossHarvested, lossSheltered, taxSaved };
      }

      const gainRatio = Math.max(0, (lot.val - lot.costBasis) / lot.val);
      let remNeed = remNetNeeded;
      const isStcg = useProg && isShortTermLot(lot, iso);

      const availLoss = stateMap._lossCarryforward || 0;
      if (availLoss > 0.005 && gainRatio > 1e-6 && remNeed > 0.005 && lot.val > 0.005) {
        const maxGrossForLoss = availLoss / gainRatio;
        const shelteredGross = Math.min(remNeed, lot.val, maxGrossForLoss);
        const frac1 = Math.min(1.0, shelteredGross / lot.val);
        const basisSold1 = lot.costBasis * frac1;
        const gainRealized1 = Math.max(0, shelteredGross - basisSold1);
        let savedTax1;
        if (useProg) {
          savedTax1 = isStcg
            ? taxOrdinaryIncrement(stateMap, gainRealized1, iso, dfl, true, applyState ? null : 0.0, 0.0, false, false).tax
            : taxLtcgIncrement(stateMap, gainRealized1, iso, dfl, applyState, false);
        } else {
          savedTax1 = gainRealized1 * taxRate;
        }

        lot.val = Math.max(0, lot.val - shelteredGross);
        lot.costBasis = Math.max(0, lot.costBasis - basisSold1);
        stateMap._lossCarryforward = Math.max(0, availLoss - gainRealized1);
        stateMap._totalLossUsed = (stateMap._totalLossUsed || 0) + gainRealized1;
        stateMap._totalTaxSavedByLoss = (stateMap._totalTaxSavedByLoss || 0) + savedTax1;

        grossSold += shelteredGross;
        netProceeds += shelteredGross;
        lossSheltered += gainRealized1;
        taxSaved += savedTax1;
        remNeed -= shelteredGross;
      }

      if (remNeed > 0.005 && lot.val > 0.005) {
        const curGainRatio = Math.max(0, (lot.val - lot.costBasis) / lot.val);
        let grossTaken, taxTaken, netTaken, frac2;
        if (useProg) {
          const stOvr = applyState ? null : 0.0;
          if (isStcg && curGainRatio > 1e-6) {
            grossTaken = grossForNetStcg(stateMap, lot.val, curGainRatio, remNeed, iso, dfl, applyState);
            frac2 = Math.min(1.0, grossTaken / lot.val);
            const gainTaken = Math.max(0.0, grossTaken - lot.costBasis * frac2);
            const ordRes = taxOrdinaryIncrement(stateMap, gainTaken, iso, dfl, true, stOvr, 0.0, false, true);
            taxTaken = ordRes.tax;
            netTaken = Math.max(0.0, grossTaken - taxTaken);
          } else {
            grossTaken = grossForNetLtcg(stateMap, lot.val, curGainRatio, remNeed, iso, dfl, applyState);
            frac2 = Math.min(1.0, grossTaken / lot.val);
            const gainTaken = Math.max(0.0, grossTaken - lot.costBasis * frac2);
            taxTaken = taxLtcgIncrement(stateMap, gainTaken, iso, dfl, applyState, true);
            netTaken = Math.max(0.0, grossTaken - taxTaken);
          }
        } else {
          const effRate = taxRate * curGainRatio;
          const netFactor = Math.max(0.01, 1.0 - effRate);
          const maxNet = lot.val * netFactor;
          netTaken = Math.min(remNeed, maxNet);
          grossTaken = netTaken / netFactor;
          taxTaken = grossTaken - netTaken;
          frac2 = Math.min(1.0, grossTaken / lot.val);
        }

        lot.val = Math.max(0, lot.val - grossTaken);
        lot.costBasis = Math.max(0, lot.costBasis * (1.0 - frac2));
        grossSold += grossTaken;
        netProceeds += netTaken;
        taxPaid += taxTaken;
      }

      return { grossSold, netProceeds, taxPaid, lossHarvested, lossSheltered, taxSaved };
    }

    function sellCapitalGainLotGross(lot, grossToSell, taxRate, stateMap, iso = "", dfl = 1.0, applyState = true) {
      let grossSold = 0, netProceeds = 0, taxPaid = 0, lossHarvested = 0, lossSheltered = 0, taxSaved = 0;
      if (lot.val <= 0.005 || grossToSell <= 0.005) {
        return { grossSold, netProceeds, taxPaid, lossHarvested, lossSheltered, taxSaved };
      }
      const tc = stateMap._taxCfg;
      const useProg = Boolean(tc && tc.progressive && iso);
      const isStcg = useProg && isShortTermLot(lot, iso);

      const takeGross = Math.min(grossToSell, lot.val);
      const frac = Math.min(1.0, takeGross / lot.val);
      const basisSold = lot.costBasis * frac;
      const gainOrLoss = takeGross - basisSold;
      lot.val = Math.max(0, lot.val - takeGross);
      lot.costBasis = Math.max(0, lot.costBasis - basisSold);
      grossSold = takeGross;

      if (gainOrLoss < -0.005) {
        lossHarvested = Math.abs(gainOrLoss);
        stateMap._lossCarryforward = (stateMap._lossCarryforward || 0) + lossHarvested;
        stateMap._totalLossHarvested = (stateMap._totalLossHarvested || 0) + lossHarvested;
        netProceeds = takeGross;
      } else if (gainOrLoss > 0.005 && (useProg || taxRate > 0)) {
        const availLoss = stateMap._lossCarryforward || 0;
        const sheltered = Math.min(availLoss, gainOrLoss);
        if (sheltered > 0.005) {
          stateMap._lossCarryforward = Math.max(0, availLoss - sheltered);
          stateMap._totalLossUsed = (stateMap._totalLossUsed || 0) + sheltered;
          if (useProg) {
            taxSaved = isStcg
              ? taxOrdinaryIncrement(stateMap, sheltered, iso, dfl, true, applyState ? null : 0.0, 0.0, false, false).tax
              : taxLtcgIncrement(stateMap, sheltered, iso, dfl, applyState, false);
          } else {
            taxSaved = sheltered * taxRate;
          }
          stateMap._totalTaxSavedByLoss = (stateMap._totalTaxSavedByLoss || 0) + taxSaved;
          lossSheltered = sheltered;
        }
        const taxableGain = Math.max(0, gainOrLoss - sheltered);
        if (taxableGain > 0.005) {
          if (useProg) {
            if (isStcg) {
              const stOvr = applyState ? null : 0.0;
              taxPaid = taxOrdinaryIncrement(stateMap, taxableGain, iso, dfl, true, stOvr, 0.0, false).tax;
            } else {
              taxPaid = taxLtcgIncrement(stateMap, taxableGain, iso, dfl, applyState, true);
            }
          } else {
            taxPaid = taxableGain * taxRate;
          }
        }
        netProceeds = Math.max(0, takeGross - taxPaid);
      } else {
        netProceeds = takeGross;
      }
      return { grossSold, netProceeds, taxPaid, lossHarvested, lossSheltered, taxSaved };
    }

    function isSimBucketLocked(st, iso) {
      if (st.unlock_ym && iso.slice(0, 7) < String(st.unlock_ym)) return true;
      if (st.maturity_iso && iso < String(st.maturity_iso) && (st.tax_model === "ANNUITY_LIFO" || st.tax_model === "ORDINARY_FULL" || st.is_hypothetical || st.maturity_locked)) {
        return true;
      }
      return false;
    }

    function recordSimBucketTapStats(st, iso, fullLbl, rowGross, rowNet, rowTax, rowLossHarv, rowLossShelt, rowTaxSav) {
      if (!st.firstTappedLbl) {
        st.firstTappedLbl = fullLbl;
        st.firstTappedIso = iso;
      }
      st.lastTappedLbl = fullLbl;
      st.lastTappedIso = iso;
      st.totalGrossSold += rowGross;
      st.totalNetProceeds += rowNet;
      st.totalTaxPaid += rowTax;
      st.totalLossHarvested += rowLossHarv;
      st.totalLossSheltered += rowLossShelt;
      st.totalTaxSaved += rowTaxSav;
      const hasUnvestedLeft = Boolean(st.is_unvested_goog && (st.tranches || []).some(t => !t.vested && t.val > 0.5));
      if (st.val <= 1.0 && !hasUnvestedLeft && !st.depletedLbl) {
        st.depletedLbl = fullLbl;
        st.depletedIso = iso;
      }
    }

    // DA-E04 / DA2-09 PARITY INVARIANT (lockstep with `_lot_sort_key` in `simulator.py`):
    // Sell underwater/0%-gain lots first (`stTier=0, gr<=0`), then `>=365d` LTCG lots (`stTier=0, gr>0`),
    // and appreciated `<365d` STCG lots strictly last (`stTier=1`).
    function lotSortKey(t, iso, useProg) {
      const gr = (t.val - t.costBasis) / t.val;
      const stTier = (useProg && gr > 1e-6 && isShortTermLot(t, iso)) ? 1 : 0;
      return [stTier, gr];
    }

    function compareLots(t1, t2, iso, useProg) {
      const k1 = lotSortKey(t1, iso, useProg);
      const k2 = lotSortKey(t2, iso, useProg);
      return (k1[0] - k2[0]) || (k1[1] - k2[1]);
    }

    function liquidateNetFromBucket(st, netNeeded, stateMap, iso, fullLbl, recordStats, dfl = 1.0) {
      let remNetNeeded = netNeeded;
      let rowGross = 0, rowNet = 0, rowTax = 0, rowLossHarv = 0, rowLossShelt = 0, rowTaxSav = 0;

      const tc = stateMap._taxCfg;
      const useProg = Boolean(tc && tc.progressive && iso);
      const earlyPen = (useProg && ownerIsPre59Half(st, stateMap, iso)) ? 0.10 : 0.0;
      const applyState = st.tax_model !== "TREASURY";

      if (st.tranches && st.tranches.length > 0) {
        const avail = st.tranches
          .filter(t => t.vested && t.val > 0.005)
          .sort((t1, t2) => compareLots(t1, t2, iso, useProg));
        for (let k = 0; k < avail.length; k++) {
          if (remNetNeeded <= 0.005) break;
          const lotRes = sellCapitalGainLot(avail[k], remNetNeeded, st.tax_rate, stateMap, iso, dfl, applyState);
          remNetNeeded -= lotRes.netProceeds;
          rowGross += lotRes.grossSold; rowNet += lotRes.netProceeds; rowTax += lotRes.taxPaid;
          rowLossHarv += lotRes.lossHarvested; rowLossShelt += lotRes.lossSheltered; rowTaxSav += lotRes.taxSaved;
        }
        let vSum = 0, bSum = 0;
        st.tranches.forEach(t => { vSum += t.val; bSum += t.costBasis; });
        st.val = vSum; st.costBasis = bSum;
      } else if (st.tax_model === "ANNUITY_LIFO") {
        if (st.val > 0.005) {
          const earnings = Math.max(0, st.val - st.costBasis);
          if (earnings > 0.005 && remNetNeeded > 0.005) {
            let grossEarn, taxEarn, netEarn;
            if (useProg) {
              grossEarn = grossForNetOrdinary(stateMap, earnings, remNetNeeded, iso, dfl, true, null, earlyPen, true);
              const ordRes = taxOrdinaryIncrement(stateMap, grossEarn, iso, dfl, true, null, earlyPen, true);
              taxEarn = ordRes.tax;
              netEarn = Math.max(0, grossEarn - taxEarn);
              rowLossShelt += ordRes.lossSheltered;
              rowTaxSav += ordRes.taxSaved;
            } else {
              const netFactor = Math.max(0.01, 1.0 - st.tax_rate);
              const maxNetEarn = earnings * netFactor;
              netEarn = Math.min(remNetNeeded, maxNetEarn);
              grossEarn = netEarn / netFactor;
              taxEarn = grossEarn - netEarn;
            }
            st.val = Math.max(0, st.val - grossEarn);
            remNetNeeded -= netEarn;
            rowGross += grossEarn; rowNet += netEarn; rowTax += taxEarn;
          }
          if (st.val > 0.005 && remNetNeeded > 0.005) {
            const netPrin = Math.min(remNetNeeded, st.val);
            st.val = Math.max(0, st.val - netPrin);
            st.costBasis = Math.max(0, st.costBasis - netPrin);
            remNetNeeded -= netPrin;
            rowGross += netPrin; rowNet += netPrin;
          }
        }
      } else if (st.tax_model === "TAX_FREE") {
        if (st.val > 0.005 && remNetNeeded > 0.005) {
          if (earlyPen > 0.0 && st.val > st.costBasis + 0.005) {
            const contrib = Math.min(st.val, Math.max(0, st.costBasis));
            if (contrib > 0.005) {
              const takeC = Math.min(remNetNeeded, contrib);
              st.val = Math.max(0, st.val - takeC);
              st.costBasis = Math.max(0, st.costBasis - takeC);
              remNetNeeded -= takeC;
              rowGross += takeC; rowNet += takeC;
            }
            if (st.val > 0.005 && remNetNeeded > 0.005) {
              const grossE = grossForNetOrdinary(stateMap, st.val, remNetNeeded, iso, dfl, false, null, earlyPen, true);
              const ordE = taxOrdinaryIncrement(stateMap, grossE, iso, dfl, false, null, earlyPen, true);
              const taxE = ordE.tax;
              const netE = Math.max(0, grossE - taxE);
              st.val = Math.max(0, st.val - grossE);
              remNetNeeded -= netE;
              rowGross += grossE; rowNet += netE; rowTax += taxE;
              rowLossShelt += ordE.lossSheltered; rowTaxSav += ordE.taxSaved;
            }
          } else {
            const netTake = Math.min(remNetNeeded, st.val);
            const frac = Math.min(1.0, netTake / st.val);
            st.val = Math.max(0, st.val - netTake);
            st.costBasis = Math.max(0, st.costBasis * (1.0 - frac));
            remNetNeeded -= netTake;
            rowGross += netTake; rowNet += netTake;
          }
        }
      } else if (st.tax_model === "ORDINARY_FULL") {
        if (st.val > 0.005 && remNetNeeded > 0.005) {
          let gross, tax, netTake;
          if (useProg) {
            gross = grossForNetOrdinary(stateMap, st.val, remNetNeeded, iso, dfl, false, null, earlyPen, true);
            const ordRes = taxOrdinaryIncrement(stateMap, gross, iso, dfl, false, null, earlyPen, true);
            tax = ordRes.tax;
            netTake = Math.max(0, gross - tax);
            rowLossShelt += ordRes.lossSheltered; rowTaxSav += ordRes.taxSaved;
          } else {
            const netFactor = Math.max(0.01, 1.0 - (st.tax_rate || 0));
            const maxNet = st.val * netFactor;
            netTake = Math.min(remNetNeeded, maxNet);
            gross = netTake / netFactor;
            tax = gross - netTake;
          }
          const frac = Math.min(1.0, gross / st.val);
          st.val = Math.max(0, st.val - gross);
          st.costBasis = Math.max(0, st.costBasis * (1.0 - frac));
          remNetNeeded -= netTake;
          rowGross += gross; rowNet += netTake; rowTax += tax;
        }
      } else if (st.val > 0.005) {
        const lotRes = sellCapitalGainLot(st, remNetNeeded, st.tax_rate, stateMap, iso, dfl, applyState);
        remNetNeeded -= lotRes.netProceeds;
        rowGross += lotRes.grossSold; rowNet += lotRes.netProceeds; rowTax += lotRes.taxPaid;
        rowLossHarv += lotRes.lossHarvested; rowLossShelt += lotRes.lossSheltered; rowTaxSav += lotRes.taxSaved;
      }

      if (rowGross > 0.01 && recordStats) {
        recordSimBucketTapStats(st, iso, fullLbl, rowGross, rowNet, rowTax, rowLossHarv, rowLossShelt, rowTaxSav);
      }
      return { grossSold: rowGross, netProceeds: rowNet, taxPaid: rowTax, lossHarvested: rowLossHarv, lossSheltered: rowLossShelt, taxSaved: rowTaxSav };
    }

    function liquidateGrossFromBucket(st, grossTarget, stateMap, iso, fullLbl, recordStats, newlyVestedOnlyYm = null, dfl = 1.0, suppressEarlyPenalty = false) {
      let remGross = grossTarget;
      let rowGross = 0, rowNet = 0, rowTax = 0, rowLossHarv = 0, rowLossShelt = 0, rowTaxSav = 0;

      const tc = stateMap._taxCfg;
      const useProg = Boolean(tc && tc.progressive && iso);
      const earlyPen = suppressEarlyPenalty ? 0.0 : ((useProg && ownerIsPre59Half(st, stateMap, iso)) ? 0.10 : 0.0);
      const applyState = st.tax_model !== "TREASURY";

      if (st.tranches && st.tranches.length > 0) {
        let avail = st.tranches.filter(t => t.vested && t.val > 0.005);
        if (newlyVestedOnlyYm) avail = avail.filter(t => t._vestedYm === newlyVestedOnlyYm);
        avail.sort((t1, t2) => compareLots(t1, t2, iso, useProg));
        for (let k = 0; k < avail.length; k++) {
          if (remGross <= 0.005) break;
          const lotRes = sellCapitalGainLotGross(avail[k], remGross, st.tax_rate, stateMap, iso, dfl, applyState);
          remGross -= lotRes.grossSold;
          rowGross += lotRes.grossSold; rowNet += lotRes.netProceeds; rowTax += lotRes.taxPaid;
          rowLossHarv += lotRes.lossHarvested; rowLossShelt += lotRes.lossSheltered; rowTaxSav += lotRes.taxSaved;
        }
        let vSum = 0, bSum = 0;
        st.tranches.forEach(t => { vSum += t.val; bSum += t.costBasis; });
        st.val = vSum; st.costBasis = bSum;
      } else if (st.tax_model === "ANNUITY_LIFO") {
        if (st.val > 0.005 && remGross > 0.005) {
          const earnings = Math.max(0, st.val - st.costBasis);
          if (earnings > 0.005) {
            const grossEarn = Math.min(remGross, earnings);
            let taxEarn;
            if (useProg) {
              const ordRes = taxOrdinaryIncrement(stateMap, grossEarn, iso, dfl, true, null, earlyPen, true);
              taxEarn = ordRes.tax;
              rowLossShelt += ordRes.lossSheltered; rowTaxSav += ordRes.taxSaved;
            } else {
              taxEarn = grossEarn * st.tax_rate;
            }
            const netEarn = Math.max(0, grossEarn - taxEarn);
            st.val = Math.max(0, st.val - grossEarn);
            remGross -= grossEarn;
            rowGross += grossEarn; rowNet += netEarn; rowTax += taxEarn;
          }
          if (st.val > 0.005 && remGross > 0.005) {
            const grossPrin = Math.min(remGross, st.val);
            st.val = Math.max(0, st.val - grossPrin);
            st.costBasis = Math.max(0, st.costBasis - grossPrin);
            remGross -= grossPrin;
            rowGross += grossPrin; rowNet += grossPrin;
          }
        }
      } else if (st.tax_model === "TAX_FREE") {
        if (st.val > 0.005 && remGross > 0.005) {
          if (earlyPen > 0.0 && st.val > st.costBasis + 0.005) {
            const contrib = Math.min(st.val, Math.max(0, st.costBasis));
            const takeC = Math.min(remGross, contrib);
            if (takeC > 0.005) {
              st.val = Math.max(0, st.val - takeC);
              st.costBasis = Math.max(0, st.costBasis - takeC);
              remGross -= takeC;
              rowGross += takeC; rowNet += takeC;
            }
            if (st.val > 0.005 && remGross > 0.005) {
              const grossE = Math.min(remGross, st.val);
              const ordE = taxOrdinaryIncrement(stateMap, grossE, iso, dfl, false, null, earlyPen, true);
              const taxE = ordE.tax;
              const netE = Math.max(0, grossE - taxE);
              st.val = Math.max(0, st.val - grossE);
              rowGross += grossE; rowNet += netE; rowTax += taxE;
              rowLossShelt += ordE.lossSheltered; rowTaxSav += ordE.taxSaved;
            }
          } else {
            const gross = Math.min(remGross, st.val);
            const frac = Math.min(1.0, gross / st.val);
            st.val = Math.max(0, st.val - gross);
            st.costBasis = Math.max(0, st.costBasis * (1.0 - frac));
            rowGross += gross; rowNet += gross;
          }
        }
      } else if (st.tax_model === "ORDINARY_FULL") {
        if (st.val > 0.005 && remGross > 0.005) {
          const gross = Math.min(remGross, st.val);
          let tax;
          if (useProg) {
            const ordRes = taxOrdinaryIncrement(stateMap, gross, iso, dfl, false, null, earlyPen, true);
            tax = ordRes.tax;
            rowLossShelt += ordRes.lossSheltered; rowTaxSav += ordRes.taxSaved;
          } else {
            tax = gross * (st.tax_rate || 0);
          }
          const net = Math.max(0, gross - tax);
          const frac = Math.min(1.0, gross / st.val);
          st.val = Math.max(0, st.val - gross);
          st.costBasis = Math.max(0, st.costBasis * (1.0 - frac));
          rowGross += gross; rowNet += net; rowTax += tax;
        }
      } else if (st.val > 0.005 && remGross > 0.005) {
        const lotRes = sellCapitalGainLotGross(st, remGross, st.tax_rate, stateMap, iso, dfl, applyState);
        rowGross += lotRes.grossSold; rowNet += lotRes.netProceeds; rowTax += lotRes.taxPaid;
        rowLossHarv += lotRes.lossHarvested; rowLossShelt += lotRes.lossSheltered; rowTaxSav += lotRes.taxSaved;
      }

      if (rowGross > 0.01 && recordStats) {
        recordSimBucketTapStats(st, iso, fullLbl, rowGross, rowNet, rowTax, rowLossHarv, rowLossShelt, rowTaxSav);
      }
      return { grossSold: rowGross, netProceeds: rowNet, taxPaid: rowTax, lossHarvested: rowLossHarv, lossSheltered: rowLossShelt, taxSaved: rowTaxSav };
    }

    // DA2-09 PARITY INVARIANT: 1:1 mirror of `_liquidate_locked_annuity_corridor` in `simulator.py`.
    function liquidateLockedAnnuityCorridor(st, netNeeded, maxGross, stateMap, iso, fullLbl, recordStats, dfl = 1.0, surrFeeRate = 0.0) {
      let capG = Math.min(Number(st.val) || 0, maxGross);
      if (capG <= 0.005 || netNeeded <= 0.005) {
        return { grossSold: 0, netProceeds: 0, taxPaid: 0, lossHarvested: 0, lossSheltered: 0, taxSaved: 0 };
      }
      const keepMult = Math.max(0.50, 1.0 - Math.max(0.0, surrFeeRate));
      let remPreSurr = netNeeded / keepMult;
      let rowGross = 0, rowNet = 0, rowTax = 0, rowLossShelt = 0, rowTaxSav = 0;
      const tc = stateMap._taxCfg;
      const useProg = Boolean(tc && tc.progressive && iso);
      const earlyPen = (useProg && ownerIsPre59Half(st, stateMap, iso)) ? 0.10 : 0.0;

      if (st.tax_model === "ANNUITY_LIFO") {
        const earnings = Math.min(capG, Math.max(0, st.val - st.costBasis));
        if (earnings > 0.005 && remPreSurr > 0.005) {
          let grossE, taxE, netE;
          if (useProg) {
            grossE = grossForNetOrdinary(stateMap, earnings, remPreSurr, iso, dfl, true, null, earlyPen, true);
            const ordRes = taxOrdinaryIncrement(stateMap, grossE, iso, dfl, true, null, earlyPen, true, true);
            taxE = ordRes.tax;
            netE = Math.max(0, grossE - taxE);
            rowLossShelt += ordRes.lossSheltered;
            rowTaxSav += ordRes.taxSaved;
          } else {
            const nf = Math.max(0.01, 1.0 - (st.tax_rate || 0));
            netE = Math.min(remPreSurr, earnings * nf);
            grossE = netE / nf;
            taxE = grossE - netE;
          }
          st.val = Math.max(0, st.val - grossE);
          capG = Math.max(0, capG - grossE);
          remPreSurr -= netE;
          rowGross += grossE; rowNet += netE; rowTax += taxE;
        }
        if (capG > 0.005 && remPreSurr > 0.005 && st.val <= st.costBasis + 0.01) {
          const prin = Math.min(remPreSurr, capG, st.val);
          st.val = Math.max(0, st.val - prin);
          st.costBasis = Math.max(0, st.costBasis - prin);
          rowGross += prin; rowNet += prin;
        }
      } else if (st.tax_model === "ORDINARY_FULL") {
        let gross, tax, netT;
        if (useProg) {
          gross = grossForNetOrdinary(stateMap, capG, remPreSurr, iso, dfl, false, null, earlyPen, true);
          const ordRes = taxOrdinaryIncrement(stateMap, gross, iso, dfl, false, null, earlyPen, true, true);
          tax = ordRes.tax;
          netT = Math.max(0, gross - tax);
          rowLossShelt += ordRes.lossSheltered;
          rowTaxSav += ordRes.taxSaved;
        } else {
          const nf = Math.max(0.01, 1.0 - (st.tax_rate || 0));
          netT = Math.min(remPreSurr, capG * nf);
          gross = netT / nf;
          tax = gross - netT;
        }
        const frac = st.val > 0.005 ? Math.min(1.0, gross / st.val) : 0.0;
        st.val = Math.max(0, st.val - gross);
        st.costBasis = Math.max(0, st.costBasis * (1.0 - frac));
        rowGross += gross; rowNet += netT; rowTax += tax;
      }

      if (surrFeeRate > 1e-6 && rowGross > 0.005) {
        const surrFee = Math.min(rowNet, rowGross * surrFeeRate);
        rowNet = Math.max(0, rowNet - surrFee);
        rowTax += surrFee;
      }
      if (rowGross > 0.01 && recordStats) {
        recordSimBucketTapStats(st, iso, fullLbl, rowGross, rowNet, rowTax, 0, rowLossShelt, rowTaxSav);
      }
      return { grossSold: rowGross, netProceeds: rowNet, taxPaid: rowTax, lossHarvested: 0, lossSheltered: rowLossShelt, taxSaved: rowTaxSav };
    }

    const _SIM_PRETAX_WRAPPERS = new Set(["TRAD_IRA", "401K", "TRAD_401K", "SEP_IRA", "SIMPLE_IRA", "403B"]);
    function stepSimMonth(stateMap, iso, fullLbl, gMonthlyRate, ngMonthlyRate, targetNetBurn, recordStats, customOrder = null, assetMonthlyRates = null, deflator = 1.0, ssnGrossMo = null, maturedRollRatePct = null) {
      let growthGain = 0;
      const ym = iso.slice(0, 7);
      const dfl = deflator > 0 ? deflator : 1.0;
      const vestedByAsset = {};
      const ratesMap = assetMonthlyRates || computeAssetMonthlyRates();
      const tc = stateMap._taxCfg;
      const useProg = Boolean(tc && tc.progressive && iso);
      const ytd = useProg ? ensureTaxYtd(stateMap, iso, dfl) : null;
      const activeOrder = customOrder || simAssetOrder;

      const januaryOpening = iso.slice(5, 7) === "01"
        ? new Map(SIM_ASSETS.map(a => [a.id, stateMap[a.id]?.val || 0])) : null;

      // 1. Compound each active asset for 1 month
      let monthlyYieldTax = 0;
      let totLossSheltered = 0;
      let totTaxSaved = 0;
      SIM_ASSETS.forEach(a => {
        if (a.is_unvested_goog && !simIncludeUnvested) return;
        const st = stateMap[a.id];
        if (!st) return;
        let r;
        const matIsoA = a.maturity_iso;
        const hasTierRollover = Boolean(
          ratesMap && ratesMap[a.id] != null
          && new Set((a.growth_evidence || []).filter(e => [1, 2, 3].includes(e.tier)).map(e => e.tier)).size > 1
        );
        const isMaturedFixed = Boolean(
          matIsoA
          && iso >= String(matIsoA)
          && !a._is_forecast_override
          && (maturedRollRatePct != null || !hasTierRollover)
          && (a.rate_model === "fixed" || String(a.return_source || "") === "CONTRACT_CREDITED_RATE")
        );
        if (isMaturedFixed) {
          const effRollPct = maturedRollRatePct == null ? 3.5 : maturedRollRatePct;
          const feeDec = Number(a._live_fee_pct ?? a.fee_pct ?? 0.0);
          const rollAnn = (effRollPct - feeDec) / 100.0;
          r = Math.pow(1.0 + Math.max(-0.9999, rollAnn), 1.0 / 12.0) - 1.0;
        } else if (ratesMap && ratesMap[a.id] != null) {
          r = ratesMap[a.id];
        } else if (a.rate_model === "fixed" && a.annual_rate_pct != null) {
          r = Math.pow(1.0 + a.annual_rate_pct / 100.0, 1.0 / 12.0) - 1.0;
        } else if (a.bucket === "goog") {
          r = gMonthlyRate;
        } else {
          r = ngMonthlyRate;
        }

        if (st.tranches && st.tranches.length > 0) {
          let vSum = 0;
          let bSum = 0;
          st.tranches.forEach(t => {
            if (t.val <= 0) return;
            const oldV = t.val;
            t.val = oldV * (1.0 + r);
            growthGain += (t.val - oldV);
            if (a.is_unvested_goog && !t.vested) {
              t.costBasis = t.val;
              if (String(t.vestIso || "").slice(0, 7) <= ym) {
                t.vested = true;
                t._vestedYm = ym;
                vestedByAsset[a.id] = (vestedByAsset[a.id] || 0) + t.val;
                if (useProg && ytd) {
                  const netRet = Number(tc.net_retention_rate || 0.50);
                  const grossVest = t.val / Math.max(0.25, netRet);
                  ytd.agi_ord = Number(ytd.agi_ord) + grossVest;
                  ytd.vest_ord = Number(ytd.vest_ord || 0.0) + grossVest;
                }
              }
            }
            vSum += t.val;
            bSum += t.costBasis;
          });
          st.val = vSum;
          st.costBasis = bSum;
        } else if (st.val > 0) {
          const oldV = st.val;
          st.val = oldV * (1.0 + r);
          const gain = st.val - oldV;
          growthGain += gain;
          if (a.tax_model === "MUNI" && gain > 0) {
            st.costBasis += gain;
            if (useProg && ytd) ytd.muni_int = (ytd.muni_int || 0) + gain;
          } else if ((a.tax_model === "CASH" || a.tax_model === "TREASURY" || a.tax_model === "TAXABLE_YIELD") && gain > 0) {
            let intTax;
            if (useProg) {
              const stOvr = a.tax_model === "TREASURY" ? 0.0 : null;
              const ordY = taxOrdinaryIncrement(stateMap, gain, iso, dfl, true, stOvr, 0.0, true, true);
              intTax = ordY.tax;
              totLossSheltered += ordY.lossSheltered;
              totTaxSaved += ordY.taxSaved;
              if (ytd) ytd.yield_ord = Number(ytd.yield_ord || 0.0) + Math.max(0.0, gain - ordY.lossSheltered);
            } else {
              intTax = gain * (st.tax_rate || 0);
            }
            st.val = Math.max(0, st.val - intTax);
            if (a.tax_model === "CASH") {
              st.costBasis = st.val;
            } else {
              st.costBasis += Math.max(0, gain - intTax);
            }
            monthlyYieldTax += intTax;
            if (recordStats) st.totalTaxPaid += intTax;
          }
        }
      });

      // 1b. Required Minimum Distribution (`DA-C01` SECURE 2.0 age 73/75 + `DA-E05` locked IRA contracts + `DA2-05` BEFORE Step 1a-2 SSN tax)
      let rmdGross = 0;
      let rmdTax = 0;
      let rmdNet = 0;
      if (iso.slice(5, 7) === "01") {
        const PRETAX_WRAPPERS = _SIM_PRETAX_WRAPPERS;
        const curYrI = parseInt(iso.slice(0, 4), 10);
        const pretaxAll = SIM_ASSETS.filter(a => stateMap[a.id] && PRETAX_WRAPPERS.has(String(a.tax_wrapper || "").toUpperCase()));
        const _pickOwner = (...vals) => {
          for (const v of vals) {
            if (v !== null && v !== undefined && v !== "") return v;
          }
          return null;
        };
        const byGroups = new Map();
        pretaxAll.forEach(a => {
          const stA = stateMap[a.id] || a;
          const oby = Number(stA.owner_birth_year || stA.ownerBirthYear || a.owner_birth_year || (tc ? tc.default_birth_year : RMD_BIRTH_YEAR) || RMD_BIRTH_YEAR);
          const owner = _pickOwner(stA.rmd_owner_index, stA.rmdOwnerIndex, a.rmd_owner_index, stA.owner_id, stA.ownerId, a.owner_id);
          const wrapper = String(stA.tax_wrapper || a.tax_wrapper || "").toUpperCase();
          const group = owner != null && !["401K", "TRAD_401K"].includes(wrapper)
            ? `owner:${owner}:${wrapper === "403B" ? "403B" : "IRA"}` : `account:${a.account_id || a.id}`;
          const key = JSON.stringify([oby, group]);
          if (!byGroups.has(key)) byGroups.set(key, { oby, ownerBuckets: [] });
          byGroups.get(key).ownerBuckets.push(a);
        });
        for (const { oby, ownerBuckets } of byGroups.values()) {
          const ageReached = curYrI - oby;
          const reqAge = rmdStartAgeForBirthYear(oby);
          const div = ageReached >= reqAge ? ULT_DIVISORS[Math.min(ageReached, 120)] : null;
          if (!div || div <= 0) continue;
          const tradBal = ownerBuckets.reduce((s, a) => s + januaryOpening.get(a.id), 0);
          let remaining = tradBal / div;
          const orderedIr = [...ownerBuckets].sort((x, y) => (isSimBucketLocked(stateMap[x.id], iso) ? 1 : 0) - (isSimBucketLocked(stateMap[y.id], iso) ? 1 : 0));
          for (const a of orderedIr) {
            if (remaining <= 0.005) break;
            const st = stateMap[a.id];
            if (st.val <= 0.005) continue;
            const gross = Math.min(remaining, st.val);
            const frac = Math.min(1.0, gross / st.val);
            let tax;
            if (useProg) {
              const ordR = taxOrdinaryIncrement(stateMap, gross, iso, dfl, false, null, 0.0, true, true);
              tax = ordR.tax;
              totLossSheltered += ordR.lossSheltered;
              totTaxSaved += ordR.taxSaved;
              if (ytd) ytd.rmd_ord = Number(ytd.rmd_ord || 0.0) + Math.max(0.0, gross - ordR.lossSheltered);
            } else {
              tax = gross * (st.tax_rate || 0);
            }
            const net = Math.max(0, gross - tax);
            st.val = Math.max(0, st.val - gross);
            st.costBasis = Math.max(0, st.costBasis * (1.0 - frac));
            remaining -= gross;
            rmdGross += gross; rmdTax += tax; rmdNet += net;
            if (recordStats) {
              if (!st.firstTappedLbl) { st.firstTappedLbl = fullLbl; st.firstTappedIso = iso; }
              st.lastTappedLbl = fullLbl; st.lastTappedIso = iso;
              st.totalGrossSold += gross; st.totalTaxPaid += tax;
            }
          }
        }
      }

      // 1a-1. Pre-Retirement Work Plan (Ongoing Contributions & Paycheck Burn Offset — lockstep with `_step_sim_month` in `simulator.py`)
      let monthlyWorkContrib = 0.0;
      let monthlyPaycheckOffset = 0.0;
      const wpCfg = tc ? tc.work_plan : null;
      if (wpCfg && Array.isArray(wpCfg.workers) && wpCfg.workers.length > 0) {
        const activeW = wpCfg.workers.filter(w => ym < w.ret_ym);
        if (activeW.length > 0) {
          if (activeW.some(w => w.covers_full_burn)) {
            monthlyPaycheckOffset = targetNetBurn;
            targetNetBurn = 0.0;
          } else {
            const rawOff = activeW.reduce((s, w) => s + (w.annual_budget_offset * dfl) / 12.0, 0.0);
            monthlyPaycheckOffset = Math.min(targetNetBurn, rawOff);
            targetNetBurn = Math.max(0.0, targetNetBurn - monthlyPaycheckOffset);
          }
          if (ym > String(wpCfg.start_ym || "")) {
            const curYrW = parseInt(iso.slice(0, 4), 10);
            const calMoW = parseInt(iso.slice(5, 7), 10);
            activeW.forEach(w => {
              const attainedAgeW = curYrW - Number(w.birth_year);
              (w.contributions || []).forEach(crow => {
                const pacingMode = String(crow.pacing || "monthly");
                const paceMult = pacingMode === "q1_frontloaded"
                  ? ((calMoW >= 1 && calMoW <= 3) ? (1.0 / 3.0) : 0.0)
                  : (1.0 / 12.0);
                if (paceMult <= 0.0) return;
                const empBase = Number(crow.employee_annual || 0);
                const catchupW = crow.age_50_catchup
                  ? ((attainedAgeW >= 60 && attainedAgeW <= 63) ? IRS_SUPER_CATCHUP_60_63_USD : (attainedAgeW >= 50 ? IRS_CATCHUP_50_USD : 0.0))
                  : 0.0;
                const empTot = empBase + catchupW;
                if (empTot > 0.0) {
                  const empMo = (empTot * dfl) * paceMult;
                  monthlyWorkContrib += depositWorkPlanLeg(stateMap, crow.target_asset_id, iso, empMo, false);
                }
                const matchBase = Number(crow.employer_match_annual || 0);
                const matchRatePct = Number(crow.match_rate_pct || 0);
                let effMatch = matchBase;
                if (matchRatePct > 0.0) {
                  const rawMatch = empBase * (matchRatePct / 100.0);
                  effMatch = matchBase > 0.0 ? Math.min(matchBase, rawMatch) : rawMatch;
                }
                if (effMatch > 0.0) {
                  const matchMo = (effMatch * dfl) * paceMult;
                  const matchTid = crow.match_target_asset_id || crow.target_asset_id;
                  monthlyWorkContrib += depositWorkPlanLeg(stateMap, matchTid, iso, matchMo, true);
                }
              });
            });
          }
        }
      }

      // 1a-2. Progressive Social Security Provisional-Income Tax (`DA-C03` + `DA2-05` annualized non-SSN income)
      let ssnTaxMo = 0.0;
      let ssnNetMo = 0.0;
      if (ssnGrossMo !== null && ssnGrossMo !== undefined) {
        if (ssnGrossMo > 0.005) {
          if (useProg) {
            let estJanDraw = 0.0;
            if (iso.slice(5, 7) === "01") {
              const estMoGap = Math.max(0.0, targetNetBurn - ssnGrossMo);
              const estAnnGap = Math.max(0.0, 12.0 * estMoGap - rmdNet);
              if (estAnnGap > 0.005) {
                for (let li = 0; li < activeOrder.length; li++) {
                  const stL = stateMap[activeOrder[li]];
                  if (!stL || stL.val <= 0.005) continue;
                  if (stL.is_unvested_goog && !simIncludeUnvested) continue;
                  if (isSimBucketLocked(stL, iso)) continue;
                  const tmL = stL.tax_model;
                  if (tmL === "ORDINARY_FULL") {
                    estJanDraw = estAnnGap;
                  } else if (tmL === "ANNUITY_LIFO" && stL.val > stL.costBasis + 0.005) {
                    estJanDraw = Math.min(estAnnGap, stL.val - stL.costBasis);
                  } else if (tmL !== "TAX_FREE" && tmL !== "CASH" && tmL !== "MUNI") {
                    const grL = Math.max(0.0, (stL.val - stL.costBasis) / stL.val);
                    estJanDraw = estAnnGap * grL;
                  }
                  break;
                }
              }
            }
            const pSsn = computeProgressiveSsnMo(stateMap, ssnGrossMo, iso, dfl, estJanDraw);
            ssnTaxMo = pSsn.tax;
            ssnNetMo = pSsn.net;
          } else {
            ssnNetMo = ssnGrossMo;
          }
        }
        targetNetBurn = Math.max(0.0, targetNetBurn - ssnNetMo);
      }

      // 1c. Unified Scheduled Actions (Deposits, Transfers, Withdrawals)
      let totGrossSold = 0;
      let totNetProceeds = 0;
      let totTaxPaid = 0;
      let totLossHarvested = 0;
      let extraAutoWithdrawNet = 0;
      let schedWithdrawNom = 0;
      let schedWithdrawReal = 0;
      let schedDepositNom = 0;
      let schedTransferNom = 0;
      const actionEvents = [];
      const sales = [];

      const pushSaleRow = (st, liq) => {
        if (liq.grossSold <= 0.01) return;
        sales.push({
          id: st.id,
          shortTitle: st.short_title,
          color: st.color || "#38bdf8",
          bucket: st.bucket,
          grossSold: liq.grossSold,
          netProceeds: liq.netProceeds,
          taxPaid: liq.taxPaid,
          effTaxPct: (liq.taxPaid / liq.grossSold) * 100.0,
          lossHarvested: liq.lossHarvested,
          lossSheltered: liq.lossSheltered,
          taxSaved: liq.taxSaved,
        });
      };

      if (simIncludeScheduledActions && Array.isArray(simScheduledActions) && simScheduledActions.length > 0) {
        for (let idx = 0; idx < simScheduledActions.length; idx++) {
          const act = simScheduledActions[idx];
          const actType = act.type || "withdrawal";
          const cad = act.cadence || "one_time";
          const fromId = String(act.from_asset_id || "");
          const toId = String(act.to_asset_id || "");
          const vestAmt = vestedByAsset[fromId] || 0.0;
          if (!actionTriggersInYm(act, ym, vestAmt > 0.005)) continue;

          if (actType === "deposit") {
            const toSt = stateMap[toId];
            if (!toSt || (toSt.is_unvested_goog && !simIncludeUnvested)) continue;
            const rawAmt = Number(act.amount) || 0;
            if (rawAmt <= 0) continue;
            const nomAmt = act.dollar_mode === "nominal" ? rawAmt : rawAmt * dfl;
            const todayAmt = nomAmt / dfl;
            if (Array.isArray(toSt.tranches) && toSt.tax_model === "LTCG_TRANCHES") {
              toSt.tranches.push({ vestIso: iso, shares: 0, vested: true, val: nomAmt, costBasis: nomAmt });
              toSt.val = toSt.tranches.reduce((s, t) => s + t.val, 0);
              toSt.costBasis = toSt.tranches.reduce((s, t) => s + t.costBasis, 0);
            } else {
              toSt.val += nomAmt;
              if ((toSt.tax_model || toSt.taxModel) !== "ORDINARY_FULL") {
                toSt.costBasis += nomAmt;
              }
            }
            schedDepositNom += nomAmt;
            actionEvents.push({ ...act, rowNum: idx + 1, ym, nomAmt, todayAmt, netAmt: nomAmt, taxPaid: 0 });
          } else if (actType === "transfer") {
            const fromSt = stateMap[fromId];
            const toSt = stateMap[toId];
            if (!fromSt || !toSt || fromSt === toSt) continue;
            if ((fromSt.is_unvested_goog && !simIncludeUnvested) || isSimBucketLocked(fromSt, iso)) continue;
            const fromTm = String(fromSt.tax_model || "");
            const toTm = String(toSt.tax_model || "");
            const isSameWrapperRollover = (fromTm === toTm) && (fromTm === "ORDINARY_FULL" || fromTm === "TAX_FREE" || fromTm === "ANNUITY_LIFO");
            const isRothConv = (fromTm === "ORDINARY_FULL") && (toTm === "TAX_FREE");
            let grossTarget = 0, newlyYmArg = null;
            if (cad === "on_vest") {
              if (vestAmt <= 0.005) continue;
              grossTarget = act.amount_mode === "pct_of_source"
                ? vestAmt * ((Number(act.amount) || 0) / 100.0)
                : Math.min(vestAmt, act.dollar_mode === "nominal" ? Number(act.amount) : Number(act.amount) * dfl);
              newlyYmArg = ym;
            } else {
              const availBase = (fromSt.is_unvested_goog && Array.isArray(fromSt.tranches))
                ? fromSt.tranches.filter(t => t.vested).reduce((s, t) => s + t.val, 0)
                : fromSt.val;
              if (availBase <= 0.005) continue;
              grossTarget = act.amount_mode === "pct_of_source"
                ? availBase * ((Number(act.amount) || 0) / 100.0)
                : Math.min(availBase, act.dollar_mode === "nominal" ? Number(act.amount) : Number(act.amount) * dfl);
            }
            // DA2-09 PARITY INVARIANT (lockstep with `_step_sim_month` in `simulator.py`):
            // Same-wrapper rollovers move 0% tax & preserve basis; pre-59.5 Roth conversion principal is exempt from +10% penalty.
            if (isSameWrapperRollover) {
              const grossMoved = Math.min(grossTarget, fromSt.val);
              if (grossMoved > 0.01) {
                _inheritRolloverOwnerMeta(fromSt, toSt);
                const fracMv = Math.min(1.0, grossMoved / fromSt.val);
                const basisMv = fromSt.costBasis * fracMv;
                fromSt.val = Math.max(0, fromSt.val - grossMoved);
                fromSt.costBasis = Math.max(0, fromSt.costBasis - basisMv);
                toSt.val += grossMoved;
                toSt.costBasis += basisMv;
                if (recordStats) recordSimBucketTapStats(fromSt, iso, fullLbl, grossMoved, grossMoved, 0, 0, 0, 0);
                schedTransferNom += grossMoved;
                actionEvents.push({ ...act, rowNum: idx + 1, ym, nomAmt: grossMoved, todayAmt: grossMoved / dfl, netAmt: grossMoved, taxPaid: 0 });
              }
              continue;
            }
            const liq = liquidateGrossFromBucket(fromSt, grossTarget, stateMap, iso, fullLbl, recordStats, newlyYmArg, dfl, isRothConv);
            if (isRothConv && act.pay_tax_from_proceeds !== false && useProg && ownerIsPre59Half(fromSt, stateMap, iso) && liq.taxPaid > 0.005) {
              const penWh = Math.min(liq.netProceeds, liq.taxPaid * 0.10);
              liq.taxPaid += penWh;
              liq.netProceeds -= penWh;
              if (recordStats) {
                fromSt.totalTaxPaid += penWh;
                fromSt.totalNetProceeds = Math.max(0, fromSt.totalNetProceeds - penWh);
              }
            }
            if (liq.grossSold > 0.01) {
              _inheritRolloverOwnerMeta(fromSt, toSt);
              const netIn = liq.netProceeds;
              if (Array.isArray(toSt.tranches) && toSt.tax_model === "LTCG_TRANCHES") {
                toSt.tranches.push({ vestIso: iso, shares: 0, vested: true, val: netIn, costBasis: netIn });
                toSt.val = toSt.tranches.reduce((s, t) => s + t.val, 0);
                toSt.costBasis = toSt.tranches.reduce((s, t) => s + t.costBasis, 0);
              } else {
                toSt.val += netIn;
                toSt.costBasis += netIn;
              }
              totGrossSold += liq.grossSold;
              totTaxPaid += liq.taxPaid;
              totLossHarvested += liq.lossHarvested;
              totLossSheltered += liq.lossSheltered;
              totTaxSaved += liq.taxSaved;
              schedTransferNom += liq.grossSold;
              pushSaleRow(fromSt, liq);
              actionEvents.push({ ...act, rowNum: idx + 1, ym, nomAmt: liq.grossSold, todayAmt: liq.grossSold / dfl, netAmt: netIn, taxPaid: liq.taxPaid });
            }
          } else {
            let nomAmt = 0;
            if (act.amount_mode === "pct_of_source" && fromId && fromId !== "auto") {
              const srcSt = stateMap[fromId];
              const srcVal = (srcSt && !isSimBucketLocked(srcSt, iso)) ? srcSt.val : 0;
              nomAmt = srcVal * ((Number(act.amount) || 0) / 100.0);
            } else {
              const rawAmt = Number(act.amount) || 0;
              nomAmt = act.dollar_mode === "nominal" ? rawAmt : rawAmt * dfl;
            }
            if (nomAmt <= 0.005) continue;
            const todayAmt = nomAmt / dfl;
            schedWithdrawNom += nomAmt;
            schedWithdrawReal += todayAmt;

            let gotNet = 0, directTax = 0;
            if (fromId && fromId !== "auto") {
              const srcSt = stateMap[fromId];
              if (srcSt && !(srcSt.is_unvested_goog && !simIncludeUnvested) && !isSimBucketLocked(srcSt, iso)) {
                const liq = liquidateNetFromBucket(srcSt, nomAmt, stateMap, iso, fullLbl, recordStats, dfl);
                gotNet = liq.netProceeds;
                directTax = liq.taxPaid;
                totGrossSold += liq.grossSold;
                totNetProceeds += liq.netProceeds;
                totTaxPaid += liq.taxPaid;
                totLossHarvested += liq.lossHarvested;
                totLossSheltered += liq.lossSheltered;
                totTaxSaved += liq.taxSaved;
                pushSaleRow(srcSt, liq);
              }
              if (act.amount_mode !== "pct_of_source" && nomAmt - gotNet > 0.005) {
                extraAutoWithdrawNet += (nomAmt - gotNet);
              }
            } else {
              extraAutoWithdrawNet += nomAmt;
            }
            actionEvents.push({ ...act, rowNum: idx + 1, ym, nomAmt, todayAmt, netAmt: gotNet, taxPaid: directTax });
          }
        }
      }

      // 2. Apply mandatory after-tax January RMD cash against net spending first, then sell unlocked assets in `simAssetOrder`
      const directEventNet = totNetProceeds;
      const beforePoolTax = totTaxPaid;
      const effectiveTargetNetBurn = targetNetBurn + extraAutoWithdrawNet;
      const rmdUsedForBurn = Math.min(rmdNet, effectiveTargetNetBurn);
      let remNetNeeded = effectiveTargetNetBurn - rmdUsedForBurn;
      totNetProceeds += rmdUsedForBurn;
      const rmdSurplus = rmdNet - rmdUsedForBurn;
      if (rmdSurplus > 0.005) {
        let cashA = SIM_ASSETS.find(a => stateMap[a.id] && a.tax_model === "CASH" && String(a.tax_wrapper || "TAXABLE").toUpperCase() === "TAXABLE");
        if (!cashA) {
          cashA = SIM_ASSETS.find(a => stateMap[a.id] && String(a.tax_wrapper || "TAXABLE").toUpperCase() === "TAXABLE" && !a.is_unvested_goog && !isSimBucketLocked(stateMap[a.id], iso));
        }
        if (cashA && stateMap[cashA.id]) {
          const cSt = stateMap[cashA.id];
          if (Array.isArray(cSt.tranches) && cSt.tranches.length > 0 && cSt.tax_model === "LTCG_TRANCHES") {
            cSt.tranches.push({ vestIso: iso, shares: 0, vested: true, val: rmdSurplus, costBasis: rmdSurplus });
            cSt.val = cSt.tranches.reduce((s, t) => s + t.val, 0);
            cSt.costBasis = cSt.tranches.reduce((s, t) => s + t.costBasis, 0);
          } else {
            cSt.val += rmdSurplus;
            cSt.costBasis += rmdSurplus;
          }
        }
      }

      for (let i = 0; i < activeOrder.length; i++) {
        if (remNetNeeded <= 0.005) break;
        const st = stateMap[activeOrder[i]];
        if (!st || st.val <= 0.005) continue;
        if (st.is_unvested_goog && !simIncludeUnvested) continue;
        if (isSimBucketLocked(st, iso)) continue;

        const liq = liquidateNetFromBucket(st, remNetNeeded, stateMap, iso, fullLbl, recordStats, dfl);
        if (liq.grossSold > 0.01) {
          remNetNeeded -= liq.netProceeds;
          totGrossSold += liq.grossSold;
          totNetProceeds += liq.netProceeds;
          totTaxPaid += liq.taxPaid;
          totLossHarvested += liq.lossHarvested;
          totLossSheltered += liq.lossSheltered;
          totTaxSaved += liq.taxSaved;
          pushSaleRow(st, liq);
        }
      }

      // DA2-09 PARITY INVARIANT (lockstep with `_step_sim_month` in `simulator.py`):
      // When all unlocked buckets are exhausted, tap locked annuities via their 10%/yr free corridor, then surrender.
      if (remNetNeeded > 0.005) {
        const curYrAnn = parseInt(iso.slice(0, 4), 10);
        for (let i = 0; i < activeOrder.length; i++) {
          if (remNetNeeded <= 0.005) break;
          const st = stateMap[activeOrder[i]];
          if (!st || st.val <= 0.005 || !isSimBucketLocked(st, iso)) continue;
          const isAnn = (st.tax_model === "ANNUITY_LIFO" || st.tax_model === "ORDINARY_FULL")
            && ((Number(st.free_withdrawal_pct) || 0) > 0 || String(st.id || "").startsWith("ann_"));
          if (!isAnn) continue;
          if (st._annFreeYr !== curYrAnn) {
            const freePct = Number(st.free_withdrawal_pct) || 10.0;
            st._annFreeYr = curYrAnn;
            st._annFreeRem = st.val * (freePct / 100.0);
          }
          const availFree = Number(st._annFreeRem || 0.0);
          if (availFree <= 0.005) continue;
          const liqF = liquidateLockedAnnuityCorridor(st, remNetNeeded, availFree, stateMap, iso, fullLbl, recordStats, dfl, 0.0);
          if (liqF.grossSold > 0.01) {
            st._annFreeRem = Math.max(0, availFree - liqF.grossSold);
            remNetNeeded -= liqF.netProceeds;
            totGrossSold += liqF.grossSold;
            totNetProceeds += liqF.netProceeds;
            totTaxPaid += liqF.taxPaid;
            totLossSheltered += liqF.lossSheltered;
            totTaxSaved += liqF.taxSaved;
            pushSaleRow(st, liqF);
          }
        }
      }

      if (remNetNeeded > 0.005) {
        for (let i = 0; i < activeOrder.length; i++) {
          if (remNetNeeded <= 0.005) break;
          const st = stateMap[activeOrder[i]];
          if (!st || st.val <= 0.005 || !isSimBucketLocked(st, iso)) continue;
          const isSurrAnn = (st.tax_model === "ANNUITY_LIFO" || st.tax_model === "ORDINARY_FULL")
            && (st.surrender_value != null || (Number(st.free_withdrawal_pct) || 0) > 0 || String(st.id || "").startsWith("ann_"));
          if (!isSurrAnn) continue;
          const surrKeep = (st.surrender_value != null && Number(st.current_value) > 0)
            ? Math.max(0.75, Math.min(1.0, Number(st.surrender_value) / Number(st.current_value)))
            : 0.93;
          const liqS = liquidateLockedAnnuityCorridor(st, remNetNeeded, st.val, stateMap, iso, fullLbl, recordStats, dfl, Math.max(0, 1.0 - surrKeep));
          if (liqS.grossSold > 0.01) {
            remNetNeeded -= liqS.netProceeds;
            totGrossSold += liqS.grossSold;
            totNetProceeds += liqS.netProceeds;
            totTaxPaid += liqS.taxPaid;
            totLossSheltered += liqS.lossSheltered;
            totTaxSaved += liqS.taxSaved;
            pushSaleRow(st, liqS);
          }
        }
      }

      if (actionEvents.length) {
        const rmdForLiving = Math.min(rmdUsedForBurn, targetNetBurn);
        const rmdForEvents = Math.max(0, rmdUsedForBurn - rmdForLiving);
        const poolNet = Math.max(0, totNetProceeds - directEventNet);
        let remainingPool = Math.max(0, poolNet - targetNetBurn);
        const initialEventPool = remainingPool;
        const waterfallNetTotal = Math.max(0, totNetProceeds - directEventNet - rmdUsedForBurn);
        const waterfallForLiving = Math.max(0, targetNetBurn - rmdForLiving);
        const waterfallForEvents = Math.max(0, waterfallNetTotal - waterfallForLiving);
        const waterfallTaxTotal = Math.max(0, totTaxPaid - beforePoolTax);
        const eventPoolTax =
          (rmdNet > 0 ? rmdTax * (rmdForEvents / rmdNet) : 0) +
          (waterfallTaxTotal > 0 && waterfallNetTotal > 0 ? waterfallTaxTotal * (waterfallForEvents / waterfallNetTotal) : 0);
        for (const event of actionEvents) {
          if ((event.type || "withdrawal") !== "withdrawal") continue;
          const need = Math.max(0, event.nomAmt - event.netAmt);
          const sourceOnly = event.amount_mode === "pct_of_source" && event.from_asset_id && event.from_asset_id !== "auto";
          const pooled = sourceOnly ? 0 : Math.min(need, remainingPool);
          remainingPool -= pooled;
          event.netAmt += pooled;
          event.pooledNet = pooled;
          event.shortfall = need - pooled;
          if (initialEventPool > 0) event.taxPaid += eventPoolTax * (pooled / initialEventPool);
        }
      }

      return {
        growthGain,
        monthlyBurn: totNetProceeds,
        targetMonthlyBurn: targetNetBurn + schedWithdrawNom,
        unmetNetBurn: Math.max(0, targetNetBurn + schedWithdrawNom - totNetProceeds),
        monthlyTax: totTaxPaid + rmdTax + monthlyYieldTax,
        ssnTaxMo,
        ssnNetMo,
        monthlyWorkContrib,
        monthlyPaycheckOffset,
        monthlyGrossSold: totGrossSold + rmdGross,
        monthlyRmdGross: rmdGross,
        monthlyRmdTax: rmdTax,
        monthlyLossHarvested: totLossHarvested,
        monthlyLossSheltered: totLossSheltered,
        monthlyTaxSaved: totTaxSaved,
        schedWithdrawNom,
        schedWithdrawReal,
        schedDepositNom,
        schedTransferNom,
        schedActionEvents: actionEvents,
        schedWithdrawEvents: actionEvents.filter(e => (e.type || "withdrawal") === "withdrawal"),
        sales,
      };
    }

    function summarizeSimStateBuckets(stateMap) {
      let googVestedVal = 0;
      let googUnvestedVal = 0;
      let nonGoogVal = 0;
      let totalBasis = 0;
      const assetVals = {};

      SIM_ASSETS.forEach(a => {
        if (a.is_unvested_goog && !simIncludeUnvested) {
          assetVals[a.id] = 0;
          return;
        }
        const st = stateMap[a.id];
        if (!st) {
          assetVals[a.id] = 0;
          return;
        }
        // DA2-11 INVARIANT (lockstep with `_summarize_state_buckets` in `simulator.py`):
        // `assetVals[a.id]` holds ONLY vested spendable value so `sum(assetVals) === totalVal` at every month.
        if (a.is_unvested_goog && st.tranches) {
          let vOnly = 0, uOnly = 0, bOnly = 0;
          st.tranches.forEach(t => {
            if (t.vested) { vOnly += t.val; bOnly += t.costBasis; }
            else uOnly += t.val;
          });
          assetVals[a.id] = vOnly;
          totalBasis += bOnly;
          if (a.bucket === "goog") {
            googVestedVal += vOnly;
            googUnvestedVal += uOnly;
          } else {
            nonGoogVal += vOnly;
          }
        } else {
          assetVals[a.id] = st.val;
          totalBasis += st.costBasis;
          if (a.bucket === "goog") {
            googVestedVal += st.val;
          } else {
            nonGoogVal += st.val;
          }
        }
      });

      const googVal = googVestedVal + googUnvestedVal;
      // A8: unvested equity grants grow in the background but are not counted as spendable net worth
      // until they vest (matching the Timeline). Milestones at/after the primary 85th are
      // unaffected since all tranches vest by 2029.
      const totalVal = googVestedVal + nonGoogVal;
      return { googVestedVal, googUnvestedVal, googVal, nonGoogVal, totalVal, totalBasis, assetVals };
    }

    function ssaFraForBirthYear(birthYear) {
      const by = Number(birthYear) || 1975;
      if (by <= 1937) return 65.0;
      if (by <= 1942) return 65.0 + (by - 1937) * (2.0 / 12.0);
      if (by <= 1954) return 66.0;
      if (by <= 1959) return 66.0 + (by - 1954) * (2.0 / 12.0);
      return 67.0;
    }

    function ssaClaimMultiplier(claimAge, fraAge = 67.0) {
      const mDiff = Math.round((claimAge - fraAge) * 12.0);
      if (mDiff === 0) return 1.0;
      if (mDiff < 0) {
        const earlyMo = Math.min(60, Math.abs(mDiff));
        const first36 = Math.min(36, earlyMo);
        const rem24 = Math.max(0, earlyMo - 36);
        return Math.max(0.70, 1.0 - (first36 * (5.0 / 900.0) + rem24 * (5.0 / 1200.0)));
      }
      const maxDelay = Math.max(0, Math.round((70.0 - fraAge) * 12.0));
      const delayMo = Math.min(maxDelay, mDiff);
      return 1.0 + delayMo * (2.0 / 300.0);
    }

    function ssaSpousalMultiplier(claimAge, fraAge = 67.0) {
      const mDiff = Math.round((claimAge - fraAge) * 12.0);
      if (mDiff >= 0) return 1.0;
      const earlyMo = Math.min(60, Math.abs(mDiff));
      const first36 = Math.min(36, earlyMo);
      const rem24 = Math.max(0, earlyMo - 36);
      return Math.max(0.65, 1.0 - (first36 * (25.0 / 3600.0) + rem24 * (5.0 / 1200.0)));
    }

    function computeMonthlySsnForIso(iso, monthIdx, jClaimAge = ssnInv0ClaimAge, yClaimAge = ssnInv1ClaimAge, colaPct = ssnColaPct) {
      const colaDeflator = Math.pow(1.0 + colaPct / 100.0, monthIdx / 12.0);
      const jAge = ageOnIso(INV0.birth_year, INV0.birth_month, INV0.birth_day, iso, monthIdx);
      const yAge = HAS_SECOND_INVESTOR
        ? ageOnIso(INV1.birth_year, INV1.birth_month, INV1.birth_day, iso, monthIdx)
        : 0;
      const fra0 = INV0.fra_age != null ? Number(INV0.fra_age) : ssaFraForBirthYear(INV0.birth_year);
      const fra1 = (HAS_SECOND_INVESTOR && INV1) ? (INV1.fra_age != null ? Number(INV1.fra_age) : ssaFraForBirthYear(INV1.birth_year)) : 67.0;
      // DA2-05 / DA2-14 PARITY INVARIANT (lockstep with `compute_monthly_ssn_for_iso` in `simulator.py`):
      // Allow `monthIdx >= 0` for Bar 0 preview, and apply SSA §202(e)/(f) 100% survivor step-up when `mortality_age` is set.
      const jMort = INV0 && INV0.mortality_age != null ? Number(INV0.mortality_age) : null;
      const yMort = (HAS_SECOND_INVESTOR && INV1 && INV1.mortality_age != null) ? Number(INV1.mortality_age) : null;
      const jAlive = jMort == null || jAge < jMort;
      const yAlive = Boolean(HAS_SECOND_INVESTOR && (yMort == null || yAge < yMort));
      const jActive = monthIdx >= 0 && jAge >= jClaimAge && jAlive;
      const yActive = Boolean(HAS_SECOND_INVESTOR && monthIdx >= 0 && yAge >= yClaimAge && yAlive);
      const jMult = ssaClaimMultiplier(jClaimAge, fra0);
      const yMult = HAS_SECOND_INVESTOR ? ssaClaimMultiplier(yClaimAge, fra1) : 0;
      const jOwnReal = SSN_INV0_PIA_2026 * jMult;
      const yOwnReal = HAS_SECOND_INVESTOR ? (SSN_INV1_PIA_2026 * yMult) : 0;
      let jRealMo = jActive ? jOwnReal : 0;
      let yRealMo = yActive ? yOwnReal : 0;
      // DA-C07: SSA §202(b) Spousal Top-Up Floor when both spouses are alive & claiming and one PIA < 50% of the other
      if (jActive && yActive) {
        if (SSN_INV0_PIA_2026 > 0 && 0.5 * SSN_INV0_PIA_2026 > SSN_INV1_PIA_2026) {
          yRealMo += (0.5 * SSN_INV0_PIA_2026 - SSN_INV1_PIA_2026) * ssaSpousalMultiplier(yClaimAge, fra1);
        } else if (SSN_INV1_PIA_2026 > 0 && 0.5 * SSN_INV1_PIA_2026 > SSN_INV0_PIA_2026) {
          jRealMo += (0.5 * SSN_INV1_PIA_2026 - SSN_INV0_PIA_2026) * ssaSpousalMultiplier(jClaimAge, fra0);
        }
      } else if (jActive && HAS_SECOND_INVESTOR && !yAlive) {
        jRealMo = Math.max(jRealMo, yOwnReal);
      } else if (yActive && !jAlive) {
        yRealMo = Math.max(yRealMo, jOwnReal);
      }
      const jNomMo = jRealMo * colaDeflator;
      const yNomMo = yRealMo * colaDeflator;
      const totRealGross = jRealMo + yRealMo;
      const totNomGross = jNomMo + yNomMo;
      const totNomTax = totNomGross * SSN_EFF_TAX_RATE;
      const totNomNet = totNomGross - totNomTax;
      const totRealTax = totRealGross * SSN_EFF_TAX_RATE;
      const totRealNet = totRealGross - totRealTax;
      return {
        jAge,
        yAge,
        jActive,
        yActive,
        colaDeflator,
        jRealMo,
        yRealMo,
        jNomMo,
        yNomMo,
        totRealGross,
        totNomGross,
        totNomTax,
        totNomNet,
        totRealTax,
        totRealNet,
      };
    }

    // =========================================================================
    // SCHEDULED WITHDRAWALS (LUMP-SUM EVENTS FOR RETIREMENT SIMULATOR)
    // =========================================================================

    const SIM_FAMILY_BIRTHDAYS = (() => {
      const fb = PROJECT_CFG.family_birthdays;
      if (Array.isArray(fb) && fb.length > 0) return fb;
      if (fb && typeof fb === "object" && Object.keys(fb).length > 0) {
        return Object.entries(fb).map(([name, dob]) => {
          const parts = String(dob || "").split("-");
          return {
            name,
            y: parseInt(parts[0], 10) || 1975,
            m: parseInt(parts[1], 10) || 1,
            d: parseInt(parts[2], 10) || 1,
          };
        });
      }
      return SSN_INVESTORS.map(x => ({ name: x.name, y: x.birth_year, m: x.birth_month, d: x.birth_day }));
    })();

    function simMonthIdxToInfo(mIdxRaw) {
      const mIdx = Math.max(0, Math.min(SIM_TOTAL_MONTHS, Math.round(Number(mIdxRaw) || 0)));
      const startYear = parseInt(TODAY_ISO.slice(0, 4), 10) || 2026;
      const startMonth0 = (parseInt(TODAY_ISO.slice(5, 7), 10) || 11) - 1;
      const startDay = parseInt(TODAY_ISO.slice(8, 10), 10) || 10;
      const totMonthsFromEpoch = startYear * 12 + startMonth0 + mIdx;
      const yr = Math.floor(totMonthsFromEpoch / 12);
      const mo0 = totMonthsFromEpoch % 12;
      const daysInMo = new Date(Date.UTC(yr, mo0 + 1, 0)).getUTCDate();
      const day = Math.min(startDay, daysInMo);
      const ym = `${yr}-${String(mo0 + 1).padStart(2, "0")}`;
      const iso = `${ym}-${String(day).padStart(2, "0")}`;
      const shortYr = String(yr).slice(2);
      const monthLbl = `${MONTH_NAMES_SHORT[mo0]} '${shortYr}`;
      const fullLbl = `${MONTH_NAMES_SHORT[mo0]} ${yr}`;
      const infMonthlyRate = Math.pow(1.0 + simInflationPct / 100.0, 1.0 / 12.0) - 1.0;
      const deflator = Math.pow(1.0 + infMonthlyRate, mIdx);
      return { mIdx, yr, mo0, day, ym, iso, monthLbl, fullLbl, yearsOut: mIdx / 12.0, deflator };
    }

    function simYmToMonthIdx(ymStr) {
      const s = String(ymStr || "").trim();
      const m = s.match(/^(\d{4})-(\d{2})/);
      if (!m) return 12;
      const yr = parseInt(m[1], 10);
      const mo0 = parseInt(m[2], 10) - 1;
      const startYear = parseInt(TODAY_ISO.slice(0, 4), 10) || 2026;
      const startMonth0 = (parseInt(TODAY_ISO.slice(5, 7), 10) || 11) - 1;
      const diff = (yr * 12 + mo0) - (startYear * 12 + startMonth0);
      return Math.max(1, Math.min(SIM_TOTAL_MONTHS, diff));
    }

    function formatSimFamilyAgesAtMonthIdx(mIdx) {
      const info = simMonthIdxToInfo(mIdx);
      const targetUtc = Date.UTC(info.yr, info.mo0, info.day);
      const ages = SIM_FAMILY_BIRTHDAYS.map(p => {
        const birthUtc = Date.UTC(p.y, p.m - 1, p.d);
        const ageYrs = (targetUtc - birthUtc) / (365.2425 * 86400000);
        return { name: p.name, age: ageYrs };
      }).sort((a, b) => b.age - a.age);
      return ages.map((a, idx) => `${a.name}${idx === 0 ? " " : ": "}${a.age.toFixed(1)}yrs`).join(", ");
    }

    function _canonicalizeHypWrapper(rawWrapper) {
      const tw = String(rawWrapper || "taxable_ltcg").trim().toLowerCase();
      if (tw === "state_tax_exempt" || tw === "treasury" || tw === "us_treasury") return "state_tax_exempt";
      if (tw === "taxable_yield" || tw === "taxable_bond") return "taxable_yield";
      if (tw === "dual_tax_exempt" || tw === "muni") return "dual_tax_exempt";
      if (tw === "roth_ira") return "ROTH_IRA";
      if (tw === "tax_free" || tw === "roth") return "tax_free";
      if (tw === "annuity_lifo" || tw === "ordinary_deferred") return "annuity_lifo";
      if (tw === "trad_ira" || tw === "traditional_ira" || tw === "ordinary_full") return "TRAD_IRA";
      if (tw === "403b") return "403B";
      if (tw === "401k" || tw === "trad_401k") return "401K";
      if (tw === "sep_ira") return "SEP_IRA";
      if (tw === "simple_ira") return "SIMPLE_IRA";
      if (tw === "cash_sweep" || tw === "cash") return "cash_sweep";
      return "taxable_ltcg";
    }

    function _hypWrapperToSelectVal(tw) {
      const c = _canonicalizeHypWrapper(tw);
      if (c === "state_tax_exempt") return "TREASURY";
      if (c === "taxable_yield") return "TAXABLE_YIELD";
      if (c === "dual_tax_exempt") return "MUNI";
      if (c === "tax_free" || c === "ROTH_IRA") return "ROTH";
      if (c === "annuity_lifo") return "ANNUITY_LIFO";
      if (c === "403B") return "403B";
      if (c === "401K") return "401K";
      if (c === "TRAD_IRA" || c === "SEP_IRA" || c === "SIMPLE_IRA") return "TRAD_IRA";
      if (c === "cash_sweep") return "CASH";
      return "TAXABLE";
    }

    function _isHypRetirementWrapper(rawWrapper) {
      const tw = String(rawWrapper || "").trim().toLowerCase();
      return [
        "trad_ira", "traditional_ira", "ordinary_full",
        "roth", "roth_ira", "tax_free",
        "403b", "401k", "trad_401k", "sep_ira", "simple_ira",
      ].includes(tw);
    }

    function _getSimInvestorsMeta() {
      const projInvs = Array.isArray(PROJECT_CFG?.investors) ? PROJECT_CFG.investors : [];
      const ssnInvs = Array.isArray(SSN_INVESTORS) ? SSN_INVESTORS : [];
      const source = (projInvs.length >= ssnInvs.length && projInvs.length > 0) ? projInvs : ssnInvs;
      return source.map((inv, idx) => {
        const ssnMatch = ssnInvs[idx]
          || ssnInvs.find(s => s && (
            (inv.id && s.id === inv.id)
            || (inv.name && String(s.name || "").trim().toLowerCase() === String(inv.name || "").trim().toLowerCase())
          ))
          || {};
        const dobParts = String(inv.birth_date || "").split("-");
        const by = Number(inv.birth_year || ssnMatch.birth_year || parseInt(dobParts[0], 10)) || RMD_BIRTH_YEAR;
        const bm = Number(inv.birth_month || ssnMatch.birth_month || parseInt(dobParts[1], 10)) || 1;
        const bd = Number(inv.birth_day || ssnMatch.birth_day || parseInt(dobParts[2], 10)) || 1;
        const id = String(inv.id || ssnMatch.id || `investor_${idx + 1}`).trim();
        const name = String(inv.name || ssnMatch.name || `Investor ${idx + 1}`).trim();
        return { idx, id, name, birth_year: by, birth_month: bm, birth_day: bd };
      });
    }

    function syncHypOwnerSelectorUI(preferredOwnerId = undefined) {
      const rowEl = document.getElementById("hyp-owner-row");
      const selEl = document.getElementById("hyp-owner-select");
      const twEl = document.getElementById("hyp-wrapper-select");
      if (!rowEl || !selEl) return;
      const invs = _getSimInvestorsMeta();
      const showOwner = invs.length >= 2 && _isHypRetirementWrapper(twEl ? twEl.value : "");
      const prevVal = preferredOwnerId !== undefined ? String(preferredOwnerId || "").trim() : String(selEl.value || "").trim();
      const opts = [`<option value="">Auto (Inherit on Rollover / Primary Investor)</option>`];
      invs.forEach(inv => {
        const isSel = prevVal && (inv.id.toLowerCase() === prevVal.toLowerCase() || inv.name.toLowerCase() === prevVal.toLowerCase());
        opts.push(`<option value="${escHtml(inv.id)}"${isSel ? " selected" : ""}>${escHtml(inv.name)}</option>`);
      });
      selEl.innerHTML = opts.join("");
      const matchedOpt = invs.find(inv => prevVal && (inv.id.toLowerCase() === prevVal.toLowerCase() || inv.name.toLowerCase() === prevVal.toLowerCase()));
      selEl.value = matchedOpt ? matchedOpt.id : "";
      rowEl.style.display = showOwner ? "block" : "none";
    }

    function normalizeHypotheticalAssets(rawHyps) {
      if (!Array.isArray(rawHyps)) return [];
      const seen = new Set();
      const out = [];
      rawHyps.forEach((raw, idx) => {
        if (!raw || typeof raw !== "object") return;
        let rawId = String(raw.id || raw.label || `hyp_${idx + 1}`).trim().toLowerCase().replace(/[^a-z0-9_]+/g, "_").replace(/^_+|_+$/g, "");
        if (!rawId.startsWith("hyp_")) rawId = `hyp_${rawId || (idx + 1)}`;
        if (seen.has(rawId)) return;
        seen.add(rawId);
        const label = String(raw.label || raw.title || rawId).replace(/^\u2726\s*/, "").trim().slice(0, 80) || rawId;
        const inst = String(raw.institution || "Hypothetical Account").trim().slice(0, 60);
        const prodRef = String(raw.real_product_ref || raw.subtitle || "Real-world hypothetical product").trim().slice(0, 160);
        const rcRaw = String(raw.risk_class || "broad_equity").trim().toLowerCase();
        const rc = RISK_CLASSES_META[rcRaw] ? rcRaw : "broad_equity";
        const gpNum = Number(raw.growth_pct ?? raw.rate_or_spread_pct ?? 0);
        const gp = Number.isFinite(gpNum) ? Math.round(Math.max(-20, Math.min(25, gpNum)) * 100) / 100 : 0.0;
        const erNum = Number(raw.expense_ratio_pct ?? 0);
        const er = Number.isFinite(erNum) ? Math.round(Math.max(0, Math.min(5, erNum)) * 10000) / 10000 : 0.0;
        const tw = _canonicalizeHypWrapper(raw.tax_wrapper);
        const unlockYm = String(raw.unlock_ym || "").trim() || null;
        const ownerId = String(raw.owner_id || "").trim();
        const rawOwnerIdx = raw.rmd_owner_index;
        const rmdOwnerIndex = (
          rawOwnerIdx !== null && rawOwnerIdx !== undefined && rawOwnerIdx !== ""
          && typeof rawOwnerIdx !== "boolean"
          && Number.isInteger(Number(rawOwnerIdx)) && Number(rawOwnerIdx) >= 0
        ) ? Number(rawOwnerIdx) : null;
        out.push({
          id: rawId,
          label,
          institution: inst,
          real_product_ref: prodRef,
          initial_value: 0.0,
          growth_pct: gp,
          risk_class: rc,
          expense_ratio_pct: er,
          tax_wrapper: tw,
          unlock_ym: unlockYm,
          owner_id: ownerId,
          rmd_owner_index: rmdOwnerIndex,
        });
      });
      return out;
    }
    const normalizeSimHypotheticalAssets = normalizeHypotheticalAssets;

    function materializeSimAssets() {
      // PORTABILITY DO: Derive `ltcgTop`, `ordTop`, and `stateRate` from `PAYLOAD.tax_rates` / `PROJECT_CFG.tax_rates` (`LL-10`, `LL-23`, `LL-33`).
      // PORTABILITY DON'T: Subtract a hardcoded state marginal rate literal or assume a fixed two-investor layout.
      const taxRates = (PAYLOAD && PAYLOAD.tax_rates) || (PROJECT_CFG && PROJECT_CFG.tax_rates) || {};
      const ltcgTop = Number(taxRates.ltcg_top ?? 0);
      const ordTop = Number(taxRates.ordinary_top ?? 0);
      const niitRate = Number(taxRates.niit_rate ?? 0.038);
      const stateRate = Number(
        taxRates.state_rate ?? (ltcgTop > 0 ? Math.max(0, ltcgTop - Number(taxRates.fed_ltcg_top ?? 0.20) - niitRate) : 0)
      );
      const cashTax = Number(PROJECT_CFG?.sim_cash_interest_tax_rate ?? ordTop);
      const base = (BASE_SIM_ASSETS || []).filter(a => !a.is_hypothetical).map(a => ({
        ...a,
        tranches: Array.isArray(a.tranches) ? a.tranches.map(t => ({ ...t })) : [],
      }));
      const baseById = Object.fromEntries(base.map(a => [a.id, a]));
      const investorsMeta = _getSimInvestorsMeta();
      const hyps = normalizeHypotheticalAssets(simHypotheticalAssets);
      const existingIds = new Set(base.map(a => a.id));
      hyps.forEach((h, idx) => {
        if (existingIds.has(h.id)) return;
        existingIds.add(h.id);
        let taxModel = "LTCG_POOL", taxRate = ltcgTop, taxTitle = `Progressive LTCG (0%–${(ltcgTop * 100).toFixed(1)}% ceiling) above Stepped-Up Basis`;
        if (h.tax_wrapper === "state_tax_exempt") {
          taxModel = "TREASURY";
          taxRate = Math.round(Math.max(0, ordTop - stateRate) * 10000) / 10000;
          taxTitle = `US Treasury (Prog. Fed Ord.+NIIT up to ${(taxRate * 100).toFixed(1)}% on Yield, 0% State)`;
        } else if (h.tax_wrapper === "taxable_yield") {
          taxModel = "TAXABLE_YIELD";
          taxRate = ordTop;
          taxTitle = `Taxable Fixed Income (Prog. Ord.+NIIT up to ${(ordTop * 100).toFixed(1)}% on Yield)`;
        } else if (h.tax_wrapper === "dual_tax_exempt") {
          taxModel = "MUNI";
          taxRate = ltcgTop;
          taxTitle = "0% Fed/State Tax-Exempt Yield";
        } else if (h.tax_wrapper === "tax_free" || h.tax_wrapper === "ROTH_IRA") {
          taxModel = "TAX_FREE";
          taxRate = 0.0;
          taxTitle = "0% Tax-Free Wrapper";
        } else if (h.tax_wrapper === "annuity_lifo") {
          taxModel = "ANNUITY_LIFO";
          taxRate = ordTop;
          taxTitle = `IRS §72 LIFO (Prog. Ord.+NIIT up to ${(ordTop * 100).toFixed(1)}% on Gain)`;
        } else if (["TRAD_IRA", "403B", "401K", "SEP_IRA", "SIMPLE_IRA"].includes(h.tax_wrapper)) {
          taxModel = "ORDINARY_FULL";
          taxRate = Math.round(Math.max(0, ordTop - niitRate) * 10000) / 10000;
          const wrapTitle = h.tax_wrapper === "403B" ? "403(b)" : (h.tax_wrapper === "401K" ? "401(k)" : "Traditional IRA");
          taxTitle = `${wrapTitle} (Prog. Ord. Income up to ${(taxRate * 100).toFixed(1)}% + RMD, 0% NIIT)`;
        } else if (h.tax_wrapper === "cash_sweep") {
          taxModel = "CASH";
          taxRate = cashTax;
          taxTitle = "0% Tax on Principal (Prog. Ord. Tax on Sweep Interest)";
        }

        const ownerMeta = {};
        let matchedInv = null;
        if (h.owner_id) {
          const oidLow = h.owner_id.toLowerCase();
          matchedInv = investorsMeta.find(inv => inv.id.toLowerCase() === oidLow || inv.name.toLowerCase() === oidLow) || null;
        }
        if (!matchedInv && h.rmd_owner_index != null && investorsMeta[h.rmd_owner_index]) {
          matchedInv = investorsMeta[h.rmd_owner_index];
        }
        if (matchedInv) {
          ownerMeta.owner_id = matchedInv.id;
          ownerMeta.ownerId = matchedInv.id;
          ownerMeta.rmd_owner_index = matchedInv.idx;
          ownerMeta.rmdOwnerIndex = matchedInv.idx;
          ownerMeta.owner_birth_year = matchedInv.birth_year;
          ownerMeta.ownerBirthYear = matchedInv.birth_year;
          ownerMeta.owner_birth_month = matchedInv.birth_month;
          ownerMeta.ownerBirthMonth = matchedInv.birth_month;
          ownerMeta.owner_birth_day = matchedInv.birth_day;
          ownerMeta.ownerBirthDay = matchedInv.birth_day;
        } else if (_isHypRetirementWrapper(h.tax_wrapper)) {
          const candidateSources = [];
          if (Array.isArray(simScheduledActions)) {
            simScheduledActions.forEach(act => {
              if (!act || (act.type || "withdrawal") !== "transfer") return;
              const toId = String(act.to_asset_id || act.target_asset_id || act.to_id || act.dest || "").trim();
              const fromId = String(act.from_asset_id || act.source_asset_id || act.from_id || act.source || "").trim();
              if (toId === h.id && fromId) candidateSources.push(fromId);
            });
          }
          const cfgRollovers = Array.isArray(PROJECT_CFG?.hypothetical_rollovers) ? PROJECT_CFG.hypothetical_rollovers : [];
          cfgRollovers.forEach(tr => {
            if (!tr || typeof tr !== "object") return;
            const toId = String(tr.to_asset_id || tr.to_id || tr.to || "").trim();
            const fromId = String(tr.from_asset_id || tr.from_id || tr.from || "").trim();
            if (toId === h.id && fromId) candidateSources.push(fromId);
          });
          for (const srcId of candidateSources) {
            const srcA = baseById[srcId];
            if (srcA && (srcA.rmd_owner_index != null || srcA.owner_birth_year)) {
              const resolvedOwnerId = String(
                srcA.owner_id
                || (srcA.rmd_owner_index != null && investorsMeta[srcA.rmd_owner_index] ? investorsMeta[srcA.rmd_owner_index].id : "")
              ).trim();
              if (resolvedOwnerId) {
                ownerMeta.owner_id = resolvedOwnerId;
                ownerMeta.ownerId = resolvedOwnerId;
              }
              if (srcA.rmd_owner_index != null) {
                ownerMeta.rmd_owner_index = srcA.rmd_owner_index;
                ownerMeta.rmdOwnerIndex = srcA.rmd_owner_index;
              }
              if (srcA.owner_birth_year) {
                ownerMeta.owner_birth_year = Number(srcA.owner_birth_year);
                ownerMeta.ownerBirthYear = Number(srcA.owner_birth_year);
              }
              if (srcA.owner_birth_month) {
                ownerMeta.owner_birth_month = Number(srcA.owner_birth_month);
                ownerMeta.ownerBirthMonth = Number(srcA.owner_birth_month);
              }
              if (srcA.owner_birth_day) {
                ownerMeta.owner_birth_day = Number(srcA.owner_birth_day);
                ownerMeta.ownerBirthDay = Number(srcA.owner_birth_day);
              }
              break;
            }
          }
        }

        const fcEntry = holdingForecasts && holdingForecasts[h.id];
        const effGp = fcEntry && Number.isFinite(Number(fcEntry.growth_pct)) ? Number(fcEntry.growth_pct) : h.growth_pct;
        const effRc = fcEntry && RISK_CLASSES_META[fcEntry.risk_class] ? fcEntry.risk_class : h.risk_class;
        base.push({
          ...h,
          ...ownerMeta,
          growth_pct: effGp,
          risk_class: effRc,
          default_rank: 90 + idx,
          color: "#2dd4bf",
          title: `\u2726 ${h.label}`,
          short_title: `\u2726 ${h.label.slice(0, 28)}`,
          subtitle: `${h.institution} \u2022 ${h.real_product_ref}`,
          account_label: `${h.institution} (Hypothetical)`,
          bucket: effRc === "single_stock" ? "goog" : "non_goog",
          is_hypothetical: true,
          is_unvested_goog: false,
          is_goog_priced: false,
          shares: null,
          current_value: 0.0,
          cost_basis: 0.0,
          basis_sub: "Starts at $0 \u2022 Funded via Scheduled Events",
          maturity_iso: h.unlock_ym ? `${h.unlock_ym}-01` : null,
          liquidity_label: h.unlock_ym
            ? `Locked until ${/^(\d{4})-(\d{2})$/.test(String(h.unlock_ym)) ? `${String(h.unlock_ym).slice(5, 7)}-${String(h.unlock_ym).slice(0, 4)}` : h.unlock_ym}`
            : "Liquid ($0 Day 0)",
          liquidity_sub: h.real_product_ref,
          tax_model: taxModel,
          tax_rate: taxRate,
          tax_rule_title: taxTitle,
          tax_rule_detail: `Hypothetical product (${h.real_product_ref}). Starts at $0 and receives net proceeds from Scheduled Events.`,
          rate_model: "slider",
          annual_rate_pct: effGp,
          forecast_key: h.id,
          parent_forecast_keys: [],
          ref_growth_pct: PAYLOAD.risk_classes?.[h.risk_class]?.benchmark_return_pct ?? h.growth_pct,
          default_growth_pct: h.growth_pct,
          default_risk_class: h.risk_class,
          tranches: [],
        });
      });
      // DA-A01: Guarantee a taxable CASH sink exists whenever pre-tax retirement buckets exist so
      // surplus after-tax January RMD cash can never vanish even on portfolios with 0 cash sweeps.
      const pretaxSet = new Set(["TRAD_IRA", "401K", "TRAD_401K", "SEP_IRA", "SIMPLE_IRA", "403B"]);
      const hasPretax = base.some(a => pretaxSet.has(String(a.tax_wrapper || "").toUpperCase()));
      const hasTaxableCash = base.some(a => a.tax_model === "CASH" && String(a.tax_wrapper || "TAXABLE").toUpperCase() === "TAXABLE");
      if (hasPretax && !hasTaxableCash && !existingIds.has("rmd_cash_sweep")) {
        base.push({
          id: "rmd_cash_sweep",
          account_id: null,
          tax_wrapper: "TAXABLE",
          default_rank: 45,
          color: "#94a3b8",
          title: "Taxable Cash Sweep (RMD Reinvestment)",
          short_title: "Cash Sweep (RMD)",
          subtitle: "Taxable FDIC Sweep \u2022 Receives Surplus After-Tax RMDs",
          account_label: "Taxable Sweep",
          bucket: "non_goog",
          is_unvested_goog: false,
          is_goog_priced: false,
          shares: null,
          current_value: 0.0,
          cost_basis: 0.0,
          basis_sub: "1:1 USD Principal (0% gain)",
          maturity_iso: null,
          liquidity_label: "Liquid Now",
          liquidity_sub: "Immediate cash availability",
          tax_model: "CASH",
          tax_rate: cashTax,
          tax_rule_title: "0% Tax on Principal",
          tax_rule_detail: "Receives surplus after-tax RMD distributions; only future sweep interest is taxed.",
          rate_model: "slider",
          annual_rate_pct: null,
          forecast_key: "rmd_cash_sweep",
          parent_forecast_keys: [],
          ref_growth_pct: 3.5,
          default_growth_pct: 3.5,
          default_risk_class: "cash_fixed",
          tranches: [],
        });
      }
      SIM_ASSETS = base;
      const validIds = new Set(base.map(a => a.id));
      if (Array.isArray(simAssetOrder) && simAssetOrder.length > 0) {
        const nextOrder = simAssetOrder.filter(id => validIds.has(id));
        base.forEach(a => { if (!nextOrder.includes(a.id)) nextOrder.push(a.id); });
        simAssetOrder = nextOrder;
      } else {
        simAssetOrder = base.slice().sort((a, b) => (a.default_rank || 999) - (b.default_rank || 999)).map(a => a.id);
      }
      return base;
    }
    const getMaterializedSimAssets = materializeSimAssets;

    function syncHypRiskSummaryUI() {
      const rcEl = document.getElementById("hyp-risk-select");
      const chipsEl = document.getElementById("hyp-risk-chips");
      const descEl = document.getElementById("hyp-risk-desc");
      const rc = (rcEl && RISK_CLASSES_META[rcEl.value]) ? rcEl.value : "broad_equity";
      const meta = RISK_CLASSES_META[rc] || RISK_CLASSES_META.broad_equity;
      const growthInput = document.getElementById("hyp-growth-input");
      if (growthInput) growthInput.setAttribute("data-tip", projectedGrowthTooltip(
        {is_hypothetical: true, risk_class: rc}, {growth_pct: Number(growthInput.value) || 0}, "Editable scenario growth"));
      if (chipsEl) {
        const gfcSign = meta.gfcShock > 0 ? "+" : "";
        chipsEl.innerHTML =
          `<span class="risk-stat-chip">Preset Vol: <strong style="color:#f8fafc;">${meta.vol.toFixed(1)}%</strong></span>` +
          `<span class="risk-stat-chip">S&amp;P 500 Corr: <strong style="color:#f8fafc;">${meta.corr.toFixed(2)}</strong></span>` +
          `<span class="risk-stat-chip">GFC Yr-1: <strong style="color:${meta.gfcShock < 0 ? '#fda4af' : '#6ee7b7'};">${gfcSign}${meta.gfcShock}%</strong></span>`;
      }
      if (descEl) {
        descEl.textContent = meta.summary || "";
      }
    }

    function _currentSimYm() {
      const iso = String((typeof PAYLOAD !== "undefined" && PAYLOAD && PAYLOAD.today_iso) || new Date().toISOString());
      return /^\d{4}-\d{2}/.test(iso) ? iso.slice(0, 7) : "2026-01";
    }

    function _formatYmToMmYyyy(ym) {
      const m = String(ym || "").trim().match(/^(\d{4})-(0[1-9]|1[0-2])$/);
      if (!m) return "";
      const canonYm = `${m[1]}-${m[2]}`;
      if (canonYm <= _currentSimYm()) return "";
      return `${m[2]}-${m[1]}`;
    }

    function _parseHypMaturityInput(rawVal) {
      const raw = String(rawVal || "").trim();
      if (!raw) return { status: "empty", validFuture: false, unlockYm: null, displayMmYyyy: "" };
      const m = raw.match(/^(\d{1,2})-(\d{4})$/);
      if (!m) return { status: "illegal", validFuture: false, unlockYm: null, displayMmYyyy: "" };
      const mmNum = parseInt(m[1], 10);
      const yyyyNum = parseInt(m[2], 10);
      if (!Number.isFinite(mmNum) || mmNum < 1 || mmNum > 12 || !Number.isFinite(yyyyNum) || yyyyNum < 1900 || yyyyNum > 2200) {
        return { status: "illegal", validFuture: false, unlockYm: null, displayMmYyyy: "" };
      }
      const mmPad = String(mmNum).padStart(2, "0");
      const yyyyStr = String(yyyyNum);
      const canonYm = `${yyyyStr}-${mmPad}`;
      if (canonYm <= _currentSimYm()) {
        return { status: "past", validFuture: false, unlockYm: null, displayMmYyyy: "" };
      }
      return { status: "valid_future", validFuture: true, unlockYm: canonYm, displayMmYyyy: `${mmPad}-${yyyyStr}` };
    }

    let _hypPrevMaturityVal = "";

    function syncHypMaturitySubtextUI() {
      const unlEl = document.getElementById("hyp-unlock-input");
      const subEl = document.getElementById("hyp-unlock-sub");
      if (!subEl) return;
      const parsed = _parseHypMaturityInput(unlEl ? unlEl.value : "");
      if (parsed.validFuture) {
        subEl.textContent = `Locked until ${parsed.displayMmYyyy}`;
        subEl.style.color = "#38bdf8";
      } else {
        subEl.textContent = "None (Liquid)";
        subEl.style.color = "#94a3b8";
      }
    }

    function _commitHypMaturityInput() {
      const unlEl = document.getElementById("hyp-unlock-input");
      if (!unlEl) return;
      const parsed = _parseHypMaturityInput(unlEl.value);
      if (parsed.status === "empty") {
        _hypPrevMaturityVal = "";
        unlEl.value = "";
      } else if (parsed.status === "valid_future") {
        _hypPrevMaturityVal = parsed.displayMmYyyy;
        if (unlEl.value !== parsed.displayMmYyyy) {
          unlEl.value = parsed.displayMmYyyy;
        }
      } else {
        // Past or illegal date committed -> reset to previous valid value (or "" if cleared/unset)
        unlEl.value = _hypPrevMaturityVal || "";
      }
      syncHypMaturitySubtextUI();
    }

    let _hypMaturityListenersBound = false;
    function _ensureHypMaturityListeners() {
      if (_hypMaturityListenersBound) return;
      const unlEl = document.getElementById("hyp-unlock-input");
      if (!unlEl || typeof unlEl.addEventListener !== "function") return;
      _hypMaturityListenersBound = true;
      unlEl.addEventListener("input", () => {
        syncHypMaturitySubtextUI();
      });
      unlEl.addEventListener("blur", () => {
        _commitHypMaturityInput();
      });
      unlEl.addEventListener("change", () => {
        _commitHypMaturityInput();
      });
      unlEl.addEventListener("keydown", (evt) => {
        if (evt && evt.key === "Enter") {
          evt.preventDefault();
          _commitHypMaturityInput();
        }
      });
    }

    function _getScheduledEventsReferencingAsset(assetId) {
      if (!assetId || !Array.isArray(simScheduledActions)) return [];
      const out = [];
      simScheduledActions.forEach((ev, idx) => {
        if (!ev) return;
        if (ev.from_asset_id === assetId || ev.to_asset_id === assetId) {
          out.push({ idx, ev });
        }
      });
      return out;
    }

    function _buildHypDeleteBlockedTooltipHtml(assetId, hypLabel) {
      const refs = _getScheduledEventsReferencingAsset(assetId);
      if (refs.length === 0) return "";
      const assetMap = Object.fromEntries((SIM_ASSETS || []).map(a => [a.id, a]));
      const shortBucket = id => {
        if (!id || id === "auto") return "Auto Priority";
        const a = assetMap[id];
        return a ? (a.short_title || a.title || id) : id;
      };
      const cadMap = { one_time: "One-Time", monthly: "Monthly", quarterly: "Quarterly", annual: "Annual", on_vest: "On Equity Vest" };
      const maxShown = 8;
      const itemRows = refs.slice(0, maxShown).map(({ idx, ev }) => {
        const actType = ev.type || "withdrawal";
        const typeLbl = actType === "transfer" ? "⇄ Transfer" : (actType === "deposit" ? "↑ Deposit" : "↓ Withdrawal");
        const cad = ev.cadence || "one_time";
        const refYm = ev.ym || ev.start_ym;
        const mIdx = simYmToMonthIdx(refYm);
        const info = simMonthIdxToInfo(mIdx);
        const schedLbl = cad === "one_time"
          ? info.fullLbl
          : `${cadMap[cad] || cad} (${ev.start_ym || ""} → ${ev.end_ym || ""})`;
        let amtLbl = "";
        if (ev.amount_mode === "pct_of_source") {
          amtLbl = `${Number(ev.amount || 0).toFixed(1)}% of source`;
        } else {
          const isNom = ev.dollar_mode === "nominal";
          amtLbl = `${fmtUSD0(Number(ev.amount) || 0)} ${isNom ? "(Nominal $)" : "(Today's $)"}`;
        }
        const routeLbl = actType === "transfer"
          ? `${shortBucket(ev.from_asset_id)} → ${shortBucket(ev.to_asset_id)}`
          : (actType === "deposit"
            ? `External Cash → ${shortBucket(ev.to_asset_id)}`
            : `${shortBucket(ev.from_asset_id)} → Spend/Gift`);
        const rawComment = String(ev.comment || "").trim();
        const cmtPreview = rawComment.length > 44 ? `${rawComment.slice(0, 41)}...` : rawComment;
        const cmtHtml = cmtPreview ? ` &bull; <span style="color:#cbd5e1;">&ldquo;${escHtml(cmtPreview)}&rdquo;</span>` : "";
        return (
          `<div style="line-height:1.38;">` +
          `&bull; <strong style="color:#7dd3fc;">#${idx + 1}</strong> ` +
          `<span style="color:#f1f5f9;font-weight:600;">${escHtml(typeLbl)}</span> ` +
          `<span style="color:#94a3b8;">(${escHtml(schedLbl)} &bull; ${escHtml(amtLbl)})</span>` +
          `<div style="padding-left:10px;font-size:10.5px;color:#94a3b8;">${escHtml(routeLbl)}${cmtHtml}</div>` +
          `</div>`
        );
      });
      const extraCount = refs.length - itemRows.length;
      const extraHtml = extraCount > 0
        ? `<div style="font-size:10.5px;color:#94a3b8;padding-left:10px;">+${extraCount} more scheduled event${extraCount > 1 ? "s" : ""}…</div>`
        : "";
      const n = refs.length;
      return (
        `<div style="font-weight:700;color:#fda4af;margin-bottom:5px;">Cannot Delete — Referenced by ${n} Scheduled Event${n > 1 ? "s" : ""}</div>` +
        `<div style="font-size:11px;color:#cbd5e1;margin-bottom:6px;">Manually edit or delete ${n > 1 ? "these Scheduled Events" : "this Scheduled Event"} before deleting <strong>${escHtml(hypLabel || assetId)}</strong>:</div>` +
        `<div style="display:flex;flex-direction:column;gap:5px;font-size:11px;">` +
        itemRows.join("") +
        extraHtml +
        `</div>`
      );
    }

    function _syncHypDeleteButtonState(existing) {
      const delWrap = document.getElementById("hyp-delete-wrap");
      const delBtn = document.getElementById("hyp-delete-btn");
      if (!existing) {
        if (delWrap) {
          delWrap.style.display = "none";
          delWrap.style.cursor = "";
          delWrap.removeAttribute("data-tip");
        }
        if (delBtn) {
          delBtn.style.display = "none";
          delBtn.disabled = false;
          delBtn.style.pointerEvents = "";
        }
        return;
      }
      if (delBtn) delBtn.style.display = "inline-flex";
      if (delWrap) delWrap.style.display = "inline-flex";
      const tipHtml = _buildHypDeleteBlockedTooltipHtml(existing.id, existing.label);
      if (tipHtml) {
        if (delBtn) {
          delBtn.disabled = true;
          delBtn.style.pointerEvents = "none";
        }
        if (delWrap) {
          delWrap.style.cursor = "not-allowed";
          delWrap.setAttribute("data-tip", tipHtml);
        }
      } else {
        if (delBtn) {
          delBtn.disabled = false;
          delBtn.style.pointerEvents = "";
        }
        if (delWrap) {
          delWrap.style.cursor = "";
          delWrap.removeAttribute("data-tip");
        }
      }
    }

    function openHypotheticalAssetDialog(hypId = null, fromWizard = false) {
      if (typeof setSimLiqDrawerExpanded === "function") setSimLiqDrawerExpanded(true);
      _hypOpenedFromWizard = Boolean(fromWizard);
      _ensureHypMaturityListeners();
      const bd = document.getElementById("sim-hyp-dialog-backdrop");
      const titleEl = document.getElementById("sim-hyp-dlg-title");
      const msgEl = document.getElementById("hyp-dlg-msg");
      if (msgEl) msgEl.textContent = "";
      const existing = hypId ? (simHypotheticalAssets || []).find(h => h.id === hypId) : null;
      _editingHypId = existing ? existing.id : null;
      if (titleEl) titleEl.textContent = existing ? `Edit Hypothetical Account (${existing.label})` : "Add Hypothetical Account / Product";
      _syncHypDeleteButtonState(existing);

      const lblEl = document.getElementById("hyp-label-input");
      const instEl = document.getElementById("hyp-inst-input");
      const refEl = document.getElementById("hyp-ref-input");
      const gpEl = document.getElementById("hyp-growth-input");
      const rcEl = document.getElementById("hyp-risk-select");
      const erEl = document.getElementById("hyp-er-input");
      const twEl = document.getElementById("hyp-wrapper-select");
      const unlEl = document.getElementById("hyp-unlock-input");

      if (existing) {
        const fc = (holdingForecasts && holdingForecasts[existing.id]) || existing;
        if (lblEl) lblEl.value = existing.label || "";
        if (instEl) instEl.value = existing.institution || "";
        if (refEl) refEl.value = existing.real_product_ref || "";
        if (gpEl) gpEl.value = String(fc.growth_pct ?? existing.growth_pct ?? 7.5);
        if (rcEl) rcEl.value = fc.risk_class || existing.risk_class || "broad_equity";
        if (erEl) erEl.value = String(existing.expense_ratio_pct ?? 0.09);
        if (twEl) twEl.value = _hypWrapperToSelectVal(existing.tax_wrapper);
        const initOwner = existing.owner_id
          || (existing.rmd_owner_index != null && _getSimInvestorsMeta()[existing.rmd_owner_index]
            ? _getSimInvestorsMeta()[existing.rmd_owner_index].id
            : "");
        syncHypOwnerSelectorUI(initOwner);
        const initMatVal = _formatYmToMmYyyy(existing.unlock_ym);
        _hypPrevMaturityVal = initMatVal;
        if (unlEl) unlEl.value = initMatVal;
      } else {
        if (lblEl) lblEl.value = "";
        if (instEl) instEl.value = (PAYLOAD.holdings_by_account && PAYLOAD.holdings_by_account[0] && PAYLOAD.holdings_by_account[0].institution) || "Brokerage";
        if (refEl) refEl.value = "";
        if (gpEl) gpEl.value = "7.5";
        if (rcEl) rcEl.value = "broad_equity";
        if (erEl) erEl.value = "0.09";
        if (twEl) twEl.value = "TAXABLE";
        syncHypOwnerSelectorUI("");
        _hypPrevMaturityVal = "";
        if (unlEl) unlEl.value = "";
      }
      syncHypRiskSummaryUI();
      syncHypMaturitySubtextUI();
      if (bd) bd.classList.add("open");
      if (lblEl && !existing) setTimeout(() => lblEl.focus(), 30);
    }

    function closeHypotheticalAssetDialog() {
      const bd = document.getElementById("sim-hyp-dialog-backdrop");
      if (bd) bd.classList.remove("open");
      const uiTip = document.getElementById("ui-tooltip");
      if (uiTip) uiTip.style.display = "none";
    }

    function saveHypotheticalAssetDialog() {
      _commitHypMaturityInput();
      const lblEl = document.getElementById("hyp-label-input");
      const instEl = document.getElementById("hyp-inst-input");
      const refEl = document.getElementById("hyp-ref-input");
      const gpEl = document.getElementById("hyp-growth-input");
      const rcEl = document.getElementById("hyp-risk-select");
      const erEl = document.getElementById("hyp-er-input");
      const twEl = document.getElementById("hyp-wrapper-select");
      const unlEl = document.getElementById("hyp-unlock-input");
      const msgEl = document.getElementById("hyp-dlg-msg");

      const label = (lblEl?.value || "").trim();
      if (!label) {
        if (msgEl) { msgEl.textContent = "Enter a friendly name for the hypothetical account/product."; msgEl.style.color = "#fda4af"; }
        if (lblEl) lblEl.focus();
        return;
      }
      const slug = label.toLowerCase().replace(/[^a-z0-9_]+/g, "_").replace(/^_+|_+$/g, "");
      const id = _editingHypId || `hyp_${slug || Date.now().toString(36)}`;
      const gp = Math.max(-20, Math.min(25, Number(gpEl?.value) || 0));
      const rc = RISK_CLASSES_META[rcEl?.value] ? rcEl.value : "broad_equity";
      const er = Math.max(0, Math.min(5, Number(erEl?.value) || 0));
      const twCanon = _canonicalizeHypWrapper(twEl?.value);
      const ownerSel = document.getElementById("hyp-owner-select");
      const rawOwnerId = (_isHypRetirementWrapper(twCanon) && ownerSel) ? String(ownerSel.value || "").trim() : "";
      const matchedOwner = rawOwnerId
        ? (_getSimInvestorsMeta().find(inv => inv.id.toLowerCase() === rawOwnerId.toLowerCase() || inv.name.toLowerCase() === rawOwnerId.toLowerCase()) || null)
        : null;
      const entry = {
        id,
        label,
        institution: (instEl?.value || "").trim() || "Hypothetical Account",
        real_product_ref: (refEl?.value || "").trim() || label,
        initial_value: 0.0,
        growth_pct: gp,
        risk_class: rc,
        expense_ratio_pct: er,
        tax_wrapper: twCanon,
        unlock_ym: _parseHypMaturityInput(unlEl?.value).unlockYm,
        owner_id: matchedOwner ? matchedOwner.id : rawOwnerId,
        rmd_owner_index: matchedOwner ? matchedOwner.idx : null,
      };

      const next = [...(simHypotheticalAssets || [])];
      const idx = next.findIndex(x => x.id === id);
      if (idx >= 0) next[idx] = entry;
      else next.push(entry);
      simHypotheticalAssets = normalizeHypotheticalAssets(next);
      holdingForecasts[id] = { growth_pct: gp, risk_class: rc };
      materializeSimAssets();
      closeHypotheticalAssetDialog();

      if (_hypOpenedFromWizard) {
        const curFrom = document.getElementById("sim-act-from-select")?.value || "";
        populateActionAccountSelects(curFrom, id);
      }
      recomputeSimBars();
      renderSimChartAndNav();
      checkSaveDirtyState();
    }

    function deleteCurrentHypotheticalAsset() {
      if (!_editingHypId) return;
      const targetId = _editingHypId;
      if (_getScheduledEventsReferencingAsset(targetId).length > 0) return;
      simHypotheticalAssets = (simHypotheticalAssets || []).filter(h => h.id !== targetId);
      simScheduledActions = (simScheduledActions || []).filter(a => a.from_asset_id !== targetId && a.to_asset_id !== targetId);
      simScheduledWithdrawals = simScheduledActions;
      delete holdingForecasts[targetId];
      simAssetOrder = (simAssetOrder || []).filter(id => id !== targetId);
      materializeSimAssets();
      closeHypotheticalAssetDialog();
      recomputeSimBars();
      renderSimChartAndNav();
      checkSaveDirtyState();
    }

    function _ymToAbsMonths(ymStr) {
      const m = String(ymStr || "").trim().match(/^(\d{4})-(0[1-9]|1[0-2])$/);
      if (!m) return null;
      return parseInt(m[1], 10) * 12 + (parseInt(m[2], 10) - 1);
    }

    function _absMonthsToYm(absM) {
      const yr = Math.floor(absM / 12);
      const mo0 = ((absM % 12) + 12) % 12;
      return `${String(yr).padStart(4, "0")}-${String(mo0 + 1).padStart(2, "0")}`;
    }

    function _formatYmFullLabel(ymStr) {
      const m = String(ymStr || "").trim().match(/^(\d{4})-(0[1-9]|1[0-2])$/);
      if (!m) return String(ymStr || "");
      const yr = parseInt(m[1], 10);
      const mo0 = parseInt(m[2], 10) - 1;
      return `${MONTH_NAMES_SHORT[mo0]} ${yr}`;
    }

    function resolveSimFamilyBirthdayYm(person, age) {
      const pLow = String(person || "").trim().toLowerCase();
      const ageNum = Number(age);
      if (!pLow || age == null || age === "" || typeof age === "boolean" || !Number.isFinite(ageNum)) return "";
      const candidates = [...(Array.isArray(SIM_FAMILY_BIRTHDAYS) ? SIM_FAMILY_BIRTHDAYS : [])];
      const seenNames = new Set(candidates.map(c => String((c && c.name) || "").trim().toLowerCase()).filter(Boolean));
      const extraMembers = [
        ...(Array.isArray(PROJECT_CFG?.family_members) ? PROJECT_CFG.family_members : []),
        ...(Array.isArray(PROJECT_CFG?.investors) ? PROJECT_CFG.investors : []),
        ...(Array.isArray(PROJECT_CFG?.beneficiaries) ? PROJECT_CFG.beneficiaries : []),
      ];
      extraMembers.forEach(m => {
        if (!m || !m.name) return;
        const nmLow = String(m.name).trim().toLowerCase();
        if (!nmLow || seenNames.has(nmLow)) return;
        const parts = String(m.birth_date || "").split("-");
        const y = Number(m.birth_year || parseInt(parts[0], 10));
        const mo = Number(m.birth_month || parseInt(parts[1], 10)) || 1;
        const d = Number(m.birth_day || parseInt(parts[2], 10)) || 1;
        if (Number.isFinite(y) && y > 1800) {
          seenNames.add(nmLow);
          candidates.push({ name: String(m.name), y, m: mo, d });
        }
      });
      let b = candidates.find(f => f && String(f.name || "").trim().toLowerCase() === pLow);
      if (!b) {
        b = candidates.find(f => {
          if (!f) return false;
          const fNameLow = String(f.name || "").trim().toLowerCase();
          if (!fNameLow) return false;
          return fNameLow.split(/\s+/)[0] === pLow || fNameLow.startsWith(pLow + " ");
        });
      }
      if (!b) return "";
      const wholeYears = Math.floor(ageNum);
      const remMonths = Math.round((ageNum - wholeYears) * 12);
      const totMonths = (Number(b.y) + wholeYears) * 12 + (Number(b.m) - 1) + remMonths;
      return `${String(Math.floor(totMonths / 12)).padStart(4, "0")}-${String((((totMonths % 12) + 12) % 12) + 1).padStart(2, "0")}`;
    }

    function normalizeSimScheduledActions(rawList) {
      if (!Array.isArray(rawList)) return [];
      const startYear = parseInt(TODAY_ISO.slice(0, 4), 10) || 2026;
      const startMonth0 = (parseInt(TODAY_ISO.slice(5, 7), 10) || 11) - 1;
      const minAbs = startYear * 12 + startMonth0 + 1;
      const maxAbs = startYear * 12 + startMonth0 + SIM_TOTAL_MONTHS;

      const resolveYm = (rawVal, person, age, defaultAbs = null) => {
        const s = String(rawVal || "").trim().toLowerCase();
        if (s === "next_month" || s === "immediate" || s === "now" || s === "day_1") {
          return _absMonthsToYm(minAbs);
        }
        if (/^\d{4}-(0[1-9]|1[0-2])$/.test(s)) {
          return s;
        }
        if (person && age != null && age !== "" && typeof age !== "boolean" && Number.isFinite(Number(age))) {
          const byAge = resolveSimFamilyBirthdayYm(person, age);
          if (/^\d{4}-(0[1-9]|1[0-2])$/.test(byAge)) {
            return byAge;
          }
        }
        return defaultAbs != null ? _absMonthsToYm(defaultAbs) : "";
      };

      const cleaned = [];
      rawList.forEach((it, idx) => {
        if (!it || typeof it !== "object") return;
        let actType = String(it.type || "withdrawal").trim().toLowerCase();
        if (!["withdrawal", "deposit", "transfer"].includes(actType)) actType = "withdrawal";
        let cadence = String(it.cadence || "one_time").trim().toLowerCase();
        if (!["one_time", "monthly", "quarterly", "annual", "on_vest"].includes(cadence)) cadence = "one_time";
        let amountMode = String(it.amount_mode || (it.pct != null && it.amount == null ? "pct_of_source" : "usd")).trim().toLowerCase();
        if (!["usd", "pct_of_source"].includes(amountMode)) amountMode = "usd";
        let amt = Number(it.amount ?? it.pct) || 0;
        if (amt <= 0) return;
        amt = amountMode === "pct_of_source" ? Math.min(100, Math.round(amt * 100) / 100) : Math.round(amt * 100) / 100;
        const dollarMode = String(it.dollar_mode || "").toLowerCase() === "nominal" ? "nominal" : "today";
        let fromId = String(it.from_asset_id || it.source_asset_id || it.from_id || it.source || (actType === "withdrawal" ? "auto" : "")).trim();
        const toId = String(it.to_asset_id || it.target_asset_id || it.to_id || it.dest || "").trim();
        if (actType === "transfer" && (!fromId || !toId || fromId === toId)) return;
        if (actType === "deposit" && !toId) return;
        if (actType === "withdrawal" && !fromId) fromId = "auto";

        let startYm = "", endYm = "";
        if (cadence === "one_time") {
          const ym = resolveYm(it.ym || it.date, it.person, it.age);
          if (!ym) return;
          startYm = ym;
          endYm = ym;
        } else {
          startYm = resolveYm(it.start_ym || it.ym || it.date, it.person, it.start_age ?? it.age, minAbs);
          endYm = resolveYm(it.end_ym, it.person, it.end_age, maxAbs);
          if ((_ymToAbsMonths(endYm) || maxAbs) < (_ymToAbsMonths(startYm) || minAbs)) endYm = startYm;
        }
        const defLbl = actType === "deposit" ? "Scheduled deposit" : (actType === "transfer" ? `Transfer ${fromId} → ${toId}` : "Scheduled withdrawal");
        const comment = String(it.comment || it.purpose || it.note || "").trim().slice(0, 140) || defLbl;
        const entry = {
          id: String(it.id || `act_${idx + 1}`),
          type: actType,
          cadence,
          ym: cadence === "one_time" ? startYm : "",
          start_ym: startYm,
          end_ym: endYm,
          amount: amt,
          amount_mode: amountMode,
          dollar_mode: dollarMode,
          from_asset_id: fromId,
          to_asset_id: toId,
          comment,
        };
        if (it.person) entry.person = String(it.person);
        if (it.age != null) entry.age = Number(it.age);
        cleaned.push(entry);
      });

      const rankOf = t => (t === "deposit" ? 0 : (t === "transfer" ? 1 : 2));
      cleaned.sort((a, b) => {
        if (a.start_ym !== b.start_ym) return a.start_ym.localeCompare(b.start_ym);
        if (rankOf(a.type) !== rankOf(b.type)) return rankOf(a.type) - rankOf(b.type);
        return a.id.localeCompare(b.id);
      });
      return cleaned;
    }
    const normalizeSimScheduledWithdrawals = normalizeSimScheduledActions;

    function actionTriggersInYm(action, ym, vestedThisMonth = false) {
      const cad = action.cadence || "one_time";
      if (cad === "one_time") return action.ym === ym;
      const curAbs = _ymToAbsMonths(ym);
      const sAbs = _ymToAbsMonths(action.start_ym || "");
      const eAbs = _ymToAbsMonths(action.end_ym || "");
      if (curAbs == null || sAbs == null || eAbs == null) return false;
      if (curAbs < sAbs || curAbs > eAbs) return false;
      const diff = curAbs - sAbs;
      if (cad === "monthly") return true;
      if (cad === "quarterly") return (diff % 3) === 0;
      if (cad === "annual") return (diff % 12) === 0;
      if (cad === "on_vest") return Boolean(vestedThisMonth);
      return false;
    }

    function getSimWithdrawalAmounts(ev, deflator) {
      const d = deflator > 0 ? deflator : 1.0;
      const rawAmt = Number(ev.amount) || 0;
      if (ev.dollar_mode === "nominal") {
        return { nomAmt: rawAmt, todayAmt: rawAmt / d };
      }
      return { todayAmt: rawAmt, nomAmt: rawAmt * d };
    }

    function setSimIncludeScheduledActions(include) {
      simIncludeScheduledActions = Boolean(include);
      simIncludeScheduledWithdrawals = simIncludeScheduledActions;
      const offBtn = document.getElementById("sim-sw-off-btn");
      const onBtn = document.getElementById("sim-sw-on-btn");
      if (offBtn) offBtn.classList.toggle("active", !simIncludeScheduledActions);
      if (onBtn) onBtn.classList.toggle("active", simIncludeScheduledActions);
      setActiveSimComputingControl(simIncludeScheduledActions ? onBtn : offBtn, true);
      if (typeof updateSimBaselineSummary === "function") updateSimBaselineSummary();
      updateSimOverridesSummary();
      recomputeSimBars();
      renderSimChartAndNav();
      checkSaveDirtyState();
    }
    const setSimIncludeScheduledWithdrawals = setSimIncludeScheduledActions;
    const setSimIncludeScheduledEvents = setSimIncludeScheduledActions;
    window.setSimIncludeScheduledEvents = setSimIncludeScheduledEvents;

    function setSimWithdrawalsDollarMode(mode) {
      simWithdrawalsDollarMode = mode === "nominal" ? "nominal" : "today";
      const tBtn = document.getElementById("sim-sw-dlr-today-btn");
      const nBtn = document.getElementById("sim-sw-dlr-nom-btn");
      if (tBtn) tBtn.classList.toggle("active", simWithdrawalsDollarMode === "today");
      if (nBtn) nBtn.classList.toggle("active", simWithdrawalsDollarMode === "nominal");
      renderSimWithdrawalsTable();
    }

    function selectSimWithdrawalRow(idx) {
      if (idx < 0 || idx >= simScheduledActions.length) {
        selectedSimWithdrawalIdx = -1;
      } else {
        selectedSimWithdrawalIdx = idx;
      }
      renderSimWithdrawalsTable();
    }

    function renderSimWithdrawalsTable() {
      const tbody = document.getElementById("sim-sw-tbody");
      const pillEl = document.getElementById("sim-sw-summary-pill");
      const hdrEl = document.getElementById("sim-sw-amt-col-hdr");
      const editBtn = document.getElementById("sim-sw-edit-btn");
      const delBtn = document.getElementById("sim-sw-del-btn");
      const tBtn = document.getElementById("sim-sw-dlr-today-btn");
      const nBtn = document.getElementById("sim-sw-dlr-nom-btn");

      const isToday = simWithdrawalsDollarMode === "today";
      if (tBtn) tBtn.classList.toggle("active", isToday);
      if (nBtn) nBtn.classList.toggle("active", !isToday);
      if (hdrEl) hdrEl.textContent = isToday ? "Amount (Today's $)" : "Amount (Nominal $)";

      if (selectedSimWithdrawalIdx >= simScheduledActions.length) {
        selectedSimWithdrawalIdx = simScheduledActions.length - 1;
      }
      const hasSel = selectedSimWithdrawalIdx >= 0 && selectedSimWithdrawalIdx < simScheduledActions.length;
      if (editBtn) editBtn.disabled = !hasSel;
      if (delBtn) delBtn.disabled = !hasSel;

      const assetMap = Object.fromEntries((SIM_ASSETS || []).map(a => [a.id, a]));
      const shortBucket = id => {
        if (!id || id === "auto") return "Auto Priority";
        const a = assetMap[id];
        return a ? (a.short_title || a.title || id) : id;
      };

      const minYm = simMonthIdxToInfo(1).ym;
      const rowsHtml = simScheduledActions.map((ev, idx) => {
        const refYm = ev.ym || ev.start_ym;
        const isOneTime = (ev.cadence || "one_time") === "one_time";
        const isPastOneTime = Boolean(isOneTime && refYm && /^\d{4}-(0[1-9]|1[0-2])$/.test(refYm) && refYm < minYm);
        const mIdx = simYmToMonthIdx(refYm);
        const info = simMonthIdxToInfo(mIdx);
        const isSel = idx === selectedSimWithdrawalIdx;
        let agesStr = formatSimFamilyAgesAtMonthIdx(mIdx);
        if (isPastOneTime) {
          const pastM = refYm.match(/^(\d{4})-(0[1-9]|1[0-2])$/);
          if (pastM) {
            const pastUtc = Date.UTC(parseInt(pastM[1], 10), parseInt(pastM[2], 10) - 1, info.day);
            const pastAges = SIM_FAMILY_BIRTHDAYS.map(p => ({
              name: p.name,
              age: Math.max(0, (pastUtc - Date.UTC(p.y, p.m - 1, p.d)) / (365.2425 * 86400000)),
            })).sort((a, b) => b.age - a.age);
            agesStr = pastAges.map((a, aIdx) => `${a.name}${aIdx === 0 ? " " : ": "}${a.age.toFixed(1)}yrs`).join(", ");
          }
        }
        const actType = ev.type || "withdrawal";
        const typeBadge = actType === "transfer"
          ? `<span class="pill pill-cyan mono" style="font-size:10.5px;">&#8644; Transfer</span>`
          : (actType === "deposit"
            ? `<span class="pill pill-green mono" style="font-size:10.5px;">&uarr; Deposit</span>`
            : `<span class="pill pill-rose mono" style="font-size:10.5px;">&darr; Withdraw</span>`);
        const cadMap = { one_time: "One-Time", monthly: "Monthly", quarterly: "Quarterly", annual: "Annual", on_vest: "On Equity Vest" };
        const cadTxt = cadMap[ev.cadence || "one_time"] || ev.cadence;
        const schedHtml = isOneTime
          ? (isPastOneTime
              ? `<strong>${escHtml(_formatYmFullLabel(refYm))}</strong> <span class="pill pill-slate mono" style="font-size:10px;">Past &middot; Skipped</span>`
              : `<strong>${escHtml(info.fullLbl)}</strong> <span class="muted" style="font-size:10.5px;">(+${info.yearsOut.toFixed(1)}y)</span>`)
          : `<strong>${escHtml(cadTxt)}</strong><div class="row-sub mono">${escHtml(ev.start_ym)} &rarr; ${escHtml(ev.end_ym)}</div>`;
        const routeHtml = actType === "transfer"
          ? `<span class="mono" style="font-size:11.5px;color:#cbd5e1;">${escHtml(shortBucket(ev.from_asset_id))}</span> <span style="color:#38bdf8;">&rarr;</span> <span class="mono" style="font-size:11.5px;color:#5eead4;">${escHtml(shortBucket(ev.to_asset_id))}</span>`
          : (actType === "deposit"
            ? `<span class="muted" style="font-size:11px;">External Cash &rarr;</span> <span class="mono" style="font-size:11.5px;color:#34d399;">${escHtml(shortBucket(ev.to_asset_id))}</span>`
            : `<span class="mono" style="font-size:11.5px;color:#cbd5e1;">${escHtml(shortBucket(ev.from_asset_id))}</span> <span class="muted" style="font-size:11px;">&rarr; Spend/Gift</span>`);

        let amtHtml = "";
        if (ev.amount_mode === "pct_of_source") {
          amtHtml = `<strong style="color:#38bdf8;">${Number(ev.amount).toFixed(1)}%</strong><div class="row-sub mono">of source balance</div>`;
        } else {
          const { nomAmt, todayAmt } = getSimWithdrawalAmounts(ev, isPastOneTime ? 1.0 : info.deflator);
          const dispAmt = isToday ? todayAmt : nomAmt;
          const altAmt = isToday ? nomAmt : todayAmt;
          const altLbl = isToday ? "Nominal $" : "Today's $";
          const col = actType === "deposit" ? "#34d399" : (actType === "transfer" ? "#38bdf8" : "#fda4af");
          const sign = actType === "deposit" ? "+" : (actType === "transfer" ? "⇄ " : "-");
          amtHtml = `<strong style="color:${col};">${sign}${fmtUSD0(dispAmt)}</strong><div class="row-sub mono">${altLbl}: ${fmtUSD0(altAmt)}</div>`;
        }

        return `
          <tr class="sim-sw-row ${isSel ? 'selected' : ''}"
              onclick="selectSimWithdrawalRow(${idx})"
              ondblclick="selectSimWithdrawalRow(${idx}); openSimWithdrawalDialog('edit');"
              title="Click to select Event #${idx + 1} • Double-click to edit">
            <td class="mono" style="font-weight:700;color:#7dd3fc;">#${idx + 1}</td>
            <td>${typeBadge}</td>
            <td class="mono">${schedHtml}</td>
            <td>${routeHtml}</td>
            <td class="num mono">${amtHtml}</td>
            <td>
              <div style="font-weight:600;color:#f8fafc;">${escHtml(ev.comment)}</div>
              <div class="row-sub mono">${escHtml(agesStr)}</div>
            </td>
          </tr>
        `;
      }).join("");

      const swOnBtn = document.getElementById("sim-sw-on-btn");
      const swOffBtn = document.getElementById("sim-sw-off-btn");
      if (swOffBtn) swOffBtn.classList.toggle("active", !simIncludeScheduledActions);
      if (swOnBtn) {
        swOnBtn.classList.toggle("active", simIncludeScheduledActions);
        swOnBtn.textContent = "Included";
      }

      if (pillEl) {
        const cnt = simScheduledActions.length;
        const lbl = `${cnt} ${cnt === 1 ? "event" : "events"}`;
        pillEl.textContent = (cnt > 0 && !simIncludeScheduledActions) ? `${lbl} • Excluded` : lbl;
        pillEl.className = "mdl-in-pill mono";
      }

      if (typeof updateSimBaselineSummary === "function") updateSimBaselineSummary();
      updateSimOverridesSummary();

      if (!tbody) return;
      if (simScheduledActions.length === 0) {
        tbody.innerHTML = `
          <tr>
            <td colspan="6" class="muted" style="text-align:center;padding:18px;font-size:12.5px;">
              No scheduled events configured in this Scenario. Click <strong>+ Add Event</strong> above to model a tax-aware rollover/transfer, equity vest sweep, recurring deposit, or gift/withdrawal.
            </td>
          </tr>
        `;
      } else {
        tbody.innerHTML = rowsHtml;
      }
    }

    let _simWizType = "withdrawal";
    let _simWizCadence = "one_time";
    let _simWizAmtMode = "usd";
    let _editingHypId = null;
    let _hypOpenedFromWizard = false;
    let _swEditingOriginalPastYm = null;
    let _swDateModifiedByUser = false;

    function populateActionAccountSelects(selectedFrom = "", selectedTo = "") {
      const fromSel = document.getElementById("sim-act-from-select");
      const toSel = document.getElementById("sim-act-to-select");
      const assets = SIM_ASSETS || [];
      if (fromSel) {
        const opts = [];
        if (_simWizType === "withdrawal") {
          opts.push(`<option value="auto" ${(!selectedFrom || selectedFrom === "auto") ? "selected" : ""}>Auto (Follow Liquidation Priority)</option>`);
        }
        assets.forEach(a => {
          if (_simWizCadence === "on_vest" && !a.is_unvested_goog) return;
          const tag = a.is_hypothetical ? "✦ " : "";
          const sel = a.id === selectedFrom ? "selected" : "";
          opts.push(`<option value="${escHtml(a.id)}" ${sel}>${tag}${escHtml(a.short_title || a.title || a.id)}</option>`);
        });
        fromSel.innerHTML = opts.join("");
      }
      if (toSel) {
        const opts = assets
          .filter(a => !a.is_unvested_goog)
          .map(a => {
            const tag = a.is_hypothetical ? "✦ " : "";
            const sel = a.id === selectedTo ? "selected" : "";
            return `<option value="${escHtml(a.id)}" ${sel}>${tag}${escHtml(a.short_title || a.title || a.id)}</option>`;
          });
        toSel.innerHTML = opts.join("");
      }
    }

    function setSimActionWizardType(t, keepSelections = false) {
      _simWizType = ["withdrawal", "deposit", "transfer"].includes(t) ? t : "withdrawal";
      ["withdrawal", "deposit", "transfer"].forEach(k => {
        const b = document.getElementById(`sim-act-type-${k}`);
        if (b) b.classList.toggle("active", k === _simWizType);
      });
      const onVestBtn = document.getElementById("sim-act-cad-onvest-btn");
      if (onVestBtn) {
        onVestBtn.style.display = _simWizType === "deposit" ? "none" : "";
        if (_simWizType === "deposit" && _simWizCadence === "on_vest") _simWizCadence = "one_time";
      }
      const fromWrap = document.getElementById("sim-act-from-wrap");
      const toWrap = document.getElementById("sim-act-to-wrap");
      const routeGrid = document.getElementById("sim-act-route-grid");
      const fromLbl = document.getElementById("sim-act-from-lbl");
      const pctBtn = document.getElementById("sim-act-mode-pct-btn");
      const taxNote = document.getElementById("sim-act-tax-note");
      if (fromWrap) fromWrap.style.display = _simWizType === "deposit" ? "none" : "block";
      if (toWrap) toWrap.style.display = _simWizType === "withdrawal" ? "none" : "block";
      if (routeGrid) routeGrid.style.gridTemplateColumns = _simWizType === "transfer" ? "1fr 1fr" : "1fr";
      if (fromLbl) fromLbl.textContent = _simWizType === "transfer" ? "Source Account (Sell)" : "Source Account / Bucket";
      if (pctBtn) {
        pctBtn.style.display = _simWizType === "deposit" ? "none" : "";
        if (_simWizType === "deposit" && _simWizAmtMode === "pct_of_source") _simWizAmtMode = "usd";
      }
      if (taxNote) {
        if (_simWizType === "transfer") {
          taxNote.textContent = "Realized LTCG / §72 / Ordinary taxes are deducted from gross proceeds; net proceeds step up cost basis in Destination.";
        } else if (_simWizType === "deposit") {
          taxNote.textContent = "Adds fresh cash directly to Destination with full cost basis (0% deposit tax).";
        } else {
          taxNote.textContent = "Withdraws net spendable cash (selling gross + tax when needed from Source or Liquidation Priority).";
        }
      }
      const prevFrom = keepSelections ? (document.getElementById("sim-act-from-select")?.value || "") : "";
      const prevTo = keepSelections ? (document.getElementById("sim-act-to-select")?.value || "") : "";
      populateActionAccountSelects(prevFrom, prevTo);
      setSimActionCadence(_simWizCadence, true);
      setSimActionAmountMode(_simWizAmtMode);
    }

    function setSimActionCadence(cad, skipSelectRefresh = false) {
      const valid = ["one_time", "monthly", "quarterly", "annual", "on_vest"];
      const prevCad = _simWizCadence;
      _simWizCadence = valid.includes(cad) ? cad : "one_time";
      if (!skipSelectRefresh && _simWizCadence !== prevCad) {
        _swDateModifiedByUser = true;
      }
      document.querySelectorAll("[data-act-cad]").forEach(b => b.classList.toggle("active", b.dataset.actCad === _simWizCadence));
      const oneBox = document.getElementById("sim-act-onetime-box");
      const recBox = document.getElementById("sim-act-recurring-box");
      if (oneBox) oneBox.style.display = _simWizCadence === "one_time" ? "block" : "none";
      if (recBox) recBox.style.display = _simWizCadence === "one_time" ? "none" : "grid";
      if (!skipSelectRefresh) {
        const curFrom = document.getElementById("sim-act-from-select")?.value || "";
        const curTo = document.getElementById("sim-act-to-select")?.value || "";
        populateActionAccountSelects(curFrom, curTo);
      }
      updateSimSwDialogConversionPreview();
    }

    function setSimActionAmountMode(mode) {
      _simWizAmtMode = mode === "pct_of_source" ? "pct_of_source" : "usd";
      const usdBtn = document.getElementById("sim-act-mode-usd-btn");
      const pctBtn = document.getElementById("sim-act-mode-pct-btn");
      const dlrWrap = document.getElementById("sim-act-dlr-toggle-wrap");
      const amtLbl = document.getElementById("sim-act-amt-lbl");
      const amtEl = document.getElementById("sim-sw-amount-input");
      if (usdBtn) usdBtn.classList.toggle("active", _simWizAmtMode === "usd");
      if (pctBtn) pctBtn.classList.toggle("active", _simWizAmtMode === "pct_of_source");
      if (dlrWrap) dlrWrap.style.display = _simWizAmtMode === "usd" ? "inline-flex" : "none";
      if (amtLbl) amtLbl.textContent = _simWizAmtMode === "pct_of_source" ? "Share (%)" : "Amount";
      if (amtEl && _simWizAmtMode === "pct_of_source" && (Number(amtEl.value) > 100 || Number(amtEl.value) <= 0)) {
        amtEl.value = "100";
      }
      updateSimSwDialogConversionPreview();
    }

    function openSimWithdrawalDialog(mode) {
      if (typeof setSimSwDrawerExpanded === "function") setSimSwDrawerExpanded(true);
      const m = (mode === "edit" || mode === "delete") ? mode : "create";
      if ((m === "edit" || m === "delete") && (selectedSimWithdrawalIdx < 0 || selectedSimWithdrawalIdx >= simScheduledActions.length)) {
        return;
      }
      simSwDialogMode = m;
      _swEditingOriginalPastYm = null;
      _swDateModifiedByUser = false;
      const bd = document.getElementById("sim-sw-dialog-backdrop");
      const titleEl = document.getElementById("sim-sw-dlg-title");
      const delBanner = document.getElementById("sim-sw-delete-banner");
      const formWrap = document.getElementById("sim-sw-form-wrap");
      const slEl = document.getElementById("sim-sw-month-slider");
      const startYmEl = document.getElementById("sim-act-start-ym");
      const endYmEl = document.getElementById("sim-act-end-ym");
      const minLbl = document.getElementById("sim-sw-min-lbl");
      const maxLbl = document.getElementById("sim-sw-max-lbl");
      const amtEl = document.getElementById("sim-sw-amount-input");
      const cmtEl = document.getElementById("sim-sw-comment-input");
      const msgEl = document.getElementById("sim-sw-dlg-msg");
      const submitBtn = document.getElementById("sim-sw-dlg-submit-btn");
      const dlrTodayBtn = document.getElementById("sim-sw-dlg-dlr-today");
      const dlrNomBtn = document.getElementById("sim-sw-dlg-dlr-nominal");

      const firstInfo = simMonthIdxToInfo(1);
      const lastInfo = simMonthIdxToInfo(SIM_TOTAL_MONTHS);
      if (minLbl) minLbl.textContent = firstInfo.monthLbl;
      if (maxLbl) maxLbl.textContent = lastInfo.monthLbl;
      if (msgEl) msgEl.textContent = "";

      const isDelete = m === "delete";
      if (delBanner) delBanner.style.display = isDelete ? "block" : "none";
      if (formWrap) formWrap.style.opacity = isDelete ? "0.55" : "1";
      if (formWrap) formWrap.style.pointerEvents = isDelete ? "none" : "auto";

      if (m === "create") {
        if (titleEl) titleEl.textContent = "Add Scheduled Event";
        simSwDialogDollarMode = "today";
        _simWizType = "withdrawal";
        _simWizCadence = "one_time";
        _simWizAmtMode = "usd";
        if (slEl) slEl.value = "12";
        if (startYmEl) startYmEl.value = firstInfo.ym;
        if (endYmEl) endYmEl.value = "";
        if (amtEl) amtEl.value = "100000";
        if (cmtEl) cmtEl.value = "";
        setSimActionWizardType("withdrawal", false);
        if (submitBtn) {
          submitBtn.textContent = "Add Scheduled Event";
          submitBtn.className = "btn btn-green";
        }
      } else {
        const ev = simScheduledActions[selectedSimWithdrawalIdx];
        _simWizType = ev.type || "withdrawal";
        _simWizCadence = ev.cadence || "one_time";
        _simWizAmtMode = ev.amount_mode || "usd";
        simSwDialogDollarMode = ev.dollar_mode === "nominal" ? "nominal" : "today";
        const refYm = ev.ym || ev.start_ym || firstInfo.ym;
        if (m === "edit" && _simWizCadence === "one_time" && /^\d{4}-(0[1-9]|1[0-2])$/.test(refYm) && refYm < firstInfo.ym) {
          _swEditingOriginalPastYm = refYm;
          _swDateModifiedByUser = false;
        }
        const mIdx = simYmToMonthIdx(refYm);
        if (slEl) slEl.value = String(mIdx);
        if (startYmEl) startYmEl.value = ev.start_ym || refYm;
        if (endYmEl) endYmEl.value = (ev.end_ym && ev.end_ym !== lastInfo.ym) ? ev.end_ym : "";
        setSimActionWizardType(_simWizType, false);
        populateActionAccountSelects(ev.from_asset_id || "", ev.to_asset_id || "");
        if (amtEl) amtEl.value = String(ev.amount);
        if (cmtEl) cmtEl.value = ev.comment || "";
        if (titleEl) {
          titleEl.textContent = isDelete
            ? `Delete Scheduled Event (#${selectedSimWithdrawalIdx + 1})`
            : `Edit Scheduled Event (#${selectedSimWithdrawalIdx + 1})`;
        }
        if (submitBtn) {
          submitBtn.textContent = isDelete ? "Delete Event" : "Save Changes";
          submitBtn.className = isDelete ? "btn btn-danger" : "btn btn-primary";
        }
      }

      if (dlrTodayBtn) dlrTodayBtn.classList.toggle("active", simSwDialogDollarMode === "today");
      if (dlrNomBtn) dlrNomBtn.classList.toggle("active", simSwDialogDollarMode === "nominal");
      onSimSwSliderInput(slEl ? slEl.value : 12, false);

      if (bd) bd.classList.add("open");
      if (!isDelete && cmtEl && m === "create") {
        setTimeout(() => cmtEl.focus(), 30);
      }
    }

    function closeSimWithdrawalDialog() {
      _swEditingOriginalPastYm = null;
      _swDateModifiedByUser = false;
      const bd = document.getElementById("sim-sw-dialog-backdrop");
      if (bd) bd.classList.remove("open");
    }

    function onSimSwDateFieldInput() {
      _swDateModifiedByUser = true;
      updateSimSwDialogConversionPreview();
    }

    function onSimSwSliderInput(val, fromUser = true) {
      if (fromUser) {
        _swDateModifiedByUser = true;
      }
      const mIdx = Math.max(1, Math.min(SIM_TOTAL_MONTHS, Math.round(Number(val) || 1)));
      const slEl = document.getElementById("sim-sw-month-slider");
      if (slEl && String(slEl.value) !== String(mIdx)) slEl.value = String(mIdx);
      const info = simMonthIdxToInfo(mIdx);
      const datePill = document.getElementById("sim-sw-date-pill");
      if (datePill) {
        if (_swEditingOriginalPastYm && !_swDateModifiedByUser && _simWizCadence === "one_time") {
          datePill.textContent = `${_formatYmFullLabel(_swEditingOriginalPastYm)} (Past · Skipped)`;
        } else {
          datePill.textContent = `${info.fullLbl} (+${info.yearsOut.toFixed(1)} yrs)`;
        }
      }
      const agesEl = document.getElementById("sim-sw-ages-line");
      if (agesEl) {
        agesEl.textContent = formatSimFamilyAgesAtMonthIdx(mIdx);
      }
      updateSimSwDialogConversionPreview();
    }

    function setSimSwDialogDollarMode(nextMode) {
      const target = nextMode === "nominal" ? "nominal" : "today";
      if (target === simSwDialogDollarMode) return;
      if (_simWizAmtMode === "usd") {
        const slEl = document.getElementById("sim-sw-month-slider");
        const startYmEl = document.getElementById("sim-act-start-ym");
        const amtEl = document.getElementById("sim-sw-amount-input");
        const mIdx = _simWizCadence === "one_time"
          ? (slEl ? Math.max(1, Math.min(SIM_TOTAL_MONTHS, Math.round(Number(slEl.value) || 1))) : 12)
          : simYmToMonthIdx(startYmEl?.value || "");
        const info = simMonthIdxToInfo(mIdx);
        const curAmt = amtEl ? (Number(amtEl.value) || 0) : 0;
        if (curAmt > 0 && amtEl) {
          const converted = target === "nominal"
            ? Math.round(curAmt * info.deflator)
            : Math.round(curAmt / (info.deflator || 1.0));
          amtEl.value = String(converted);
        }
      }
      simSwDialogDollarMode = target;
      const dlrTodayBtn = document.getElementById("sim-sw-dlg-dlr-today");
      const dlrNomBtn = document.getElementById("sim-sw-dlg-dlr-nominal");
      if (dlrTodayBtn) dlrTodayBtn.classList.toggle("active", simSwDialogDollarMode === "today");
      if (dlrNomBtn) dlrNomBtn.classList.toggle("active", simSwDialogDollarMode === "nominal");
      updateSimSwDialogConversionPreview();
    }

    function updateSimSwDialogConversionPreview() {
      const convEl = document.getElementById("sim-sw-amount-conv");
      const slEl = document.getElementById("sim-sw-month-slider");
      const startYmEl = document.getElementById("sim-act-start-ym");
      const amtEl = document.getElementById("sim-sw-amount-input");
      if (!convEl) return;
      const mIdx = _simWizCadence === "one_time"
        ? (slEl ? Math.max(1, Math.min(SIM_TOTAL_MONTHS, Math.round(Number(slEl.value) || 1))) : 12)
        : simYmToMonthIdx(startYmEl?.value || "");
      const info = simMonthIdxToInfo(mIdx);
      const amt = amtEl ? (Number(amtEl.value) || 0) : 0;
      if (amt <= 0) {
        convEl.textContent = "Enter a positive value for the scheduled event.";
        return;
      }
      if (_simWizAmtMode === "pct_of_source") {
        const pct = Math.min(100, amt);
        convEl.textContent = `Liquidates ${pct.toFixed(1)}% of source balance (${_simWizCadence === "on_vest" ? "each newly vested equity tranche" : info.fullLbl}).`;
        return;
      }
      if (simSwDialogDollarMode === "today") {
        const nom = amt * info.deflator;
        convEl.textContent = `Today's $: ${fmtUSD0(amt)}  →  Nominal $ in ${info.fullLbl}: ${fmtUSD0(nom)} (@${simInflationPct.toFixed(1)}%/yr)`;
      } else {
        const real = amt / (info.deflator || 1.0);
        convEl.textContent = `Nominal $ in ${info.fullLbl}: ${fmtUSD0(amt)}  →  Today's $: ${fmtUSD0(real)} (@${simInflationPct.toFixed(1)}%/yr)`;
      }
    }

    function onSimSwInputKeyDown(evt) {
      if (evt.key === "Enter") {
        evt.preventDefault();
        submitSimWithdrawalDialog();
      }
    }

    function submitSimWithdrawalDialog() {
      const msgEl = document.getElementById("sim-sw-dlg-msg");
      if (simSwDialogMode === "delete") {
        if (selectedSimWithdrawalIdx >= 0 && selectedSimWithdrawalIdx < simScheduledActions.length) {
          simScheduledActions.splice(selectedSimWithdrawalIdx, 1);
          simScheduledWithdrawals = simScheduledActions;
          if (selectedSimWithdrawalIdx >= simScheduledActions.length) {
            selectedSimWithdrawalIdx = simScheduledActions.length - 1;
          }
          recomputeSimBars();
          renderSimChartAndNav();
          renderSimWithdrawalsTable();
          checkSaveDirtyState();
        }
        closeSimWithdrawalDialog();
        return;
      }

      const slEl = document.getElementById("sim-sw-month-slider");
      const startYmEl = document.getElementById("sim-act-start-ym");
      const endYmEl = document.getElementById("sim-act-end-ym");
      const fromSel = document.getElementById("sim-act-from-select");
      const toSel = document.getElementById("sim-act-to-select");
      const amtEl = document.getElementById("sim-sw-amount-input");
      const cmtEl = document.getElementById("sim-sw-comment-input");

      const rawAmt = amtEl ? Number(amtEl.value) : 0;
      if (!Number.isFinite(rawAmt) || rawAmt <= 0) {
        if (msgEl) { msgEl.textContent = "Please enter an amount greater than 0."; msgEl.style.color = "#fda4af"; }
        if (amtEl) amtEl.focus();
        return;
      }
      const fromId = _simWizType === "deposit" ? "" : (fromSel?.value || (_simWizType === "withdrawal" ? "auto" : ""));
      const toId = _simWizType === "withdrawal" ? "" : (toSel?.value || "");
      if (_simWizType === "transfer" && (!fromId || !toId || fromId === toId)) {
        if (msgEl) { msgEl.textContent = "Choose distinct Source and Destination accounts for a Transfer."; msgEl.style.color = "#fda4af"; }
        return;
      }
      if (_simWizType === "deposit" && !toId) {
        if (msgEl) { msgEl.textContent = "Choose a Destination account for the Cash Deposit."; msgEl.style.color = "#fda4af"; }
        return;
      }

      const mIdx = slEl ? Math.max(1, Math.min(SIM_TOTAL_MONTHS, Math.round(Number(slEl.value) || 1))) : 12;
      const info = simMonthIdxToInfo(mIdx);
      const firstInfo = simMonthIdxToInfo(1);
      const lastInfo = simMonthIdxToInfo(SIM_TOTAL_MONTHS);
      const preservePastYm = (
        simSwDialogMode === "edit"
        && _simWizCadence === "one_time"
        && Boolean(_swEditingOriginalPastYm)
        && !_swDateModifiedByUser
        && mIdx === 1
      );
      const oneTimeYm = preservePastYm ? _swEditingOriginalPastYm : info.ym;
      const startYm = _simWizCadence === "one_time" ? oneTimeYm : (startYmEl?.value || firstInfo.ym);
      const endYm = _simWizCadence === "one_time" ? oneTimeYm : (endYmEl?.value || lastInfo.ym);
      const existingId = (simSwDialogMode === "edit" && simScheduledActions[selectedSimWithdrawalIdx])
        ? simScheduledActions[selectedSimWithdrawalIdx].id
        : `act_${Date.now().toString(36)}`;

      const rawEntry = {
        id: existingId,
        type: _simWizType,
        cadence: _simWizCadence,
        ym: _simWizCadence === "one_time" ? startYm : "",
        start_ym: startYm,
        end_ym: endYm,
        amount: _simWizAmtMode === "pct_of_source" ? Math.min(100, Math.round(rawAmt * 100) / 100) : Math.round(rawAmt * 100) / 100,
        amount_mode: _simWizAmtMode,
        dollar_mode: simSwDialogDollarMode === "nominal" ? "nominal" : "today",
        from_asset_id: fromId,
        to_asset_id: toId,
        comment: (cmtEl ? cmtEl.value : "").trim().slice(0, 140),
      };

      if (simSwDialogMode === "edit" && selectedSimWithdrawalIdx >= 0 && selectedSimWithdrawalIdx < simScheduledActions.length) {
        simScheduledActions[selectedSimWithdrawalIdx] = rawEntry;
      } else {
        simScheduledActions.push(rawEntry);
      }

      simScheduledActions = normalizeSimScheduledActions(simScheduledActions);
      simScheduledWithdrawals = simScheduledActions;
      selectedSimWithdrawalIdx = simScheduledActions.findIndex(x => x.id === existingId);

      recomputeSimBars();
      renderSimChartAndNav();
      renderSimWithdrawalsTable();
      checkSaveDirtyState();
      closeSimWithdrawalDialog();
    }

    function recomputeSimBars() {
      _cancelSimIdleAudit();
      materializeSimAssets();
      const bearOrder = simBearShield ? computeBearShieldOrder(simAssetOrder) : simAssetOrder;
      const adjustYear = buildYearRateAdjuster();
      const baseRates = computeAssetMonthlyRates();
      const infMonthlyRate = Math.pow(1.0 + simInflationPct / 100.0, 1.0 / 12.0) - 1.0;
      const wpCfgMain = buildWorkPlanConfig();
      const ddStartM = computeDrawdownStartMonth(wpCfgMain, TODAY_ISO, simAnnualBurn, infMonthlyRate);
      simDrawdownStartMonth = ddStartM;
      const stressRes = buildStressMonthResolver(baseRates, adjustYear, ddStartM);
      const stressProf = stressRes.profile;
      const getStressRates = stressRes.getRatesForMonth;
      const bsEndIncl = stressProf ? (ddStartM + stressProf.bear_shield_months - 1) : 0;

      const startYear = parseInt(TODAY_ISO.slice(0, 4), 10) || 2026;
      const startMonth0 = (parseInt(TODAY_ISO.slice(5, 7), 10) || 11) - 1;
      const startDay = parseInt(TODAY_ISO.slice(8, 10), 10) || 10;

      const stateMap = cloneSimAssetState();
      const bars = [];
      let depletedAtBar = null;
      let cumGross = 0;
      let cumNet = 0;
      let cumTax = 0;
      let cumRealGross = 0;
      let cumRealNet = 0;
      let cumRealTax = 0;
      let cumSsnGross = 0;
      let cumSsnNet = 0;
      let cumWorkContrib = 0;
      let cumRealWorkContrib = 0;

      for (let m = 0; m <= SIM_TOTAL_MONTHS; m++) {
        const totMonthsFromEpoch = startYear * 12 + startMonth0 + m;
        const yr = Math.floor(totMonthsFromEpoch / 12);
        const mo0 = totMonthsFromEpoch % 12;
        const daysInMo = new Date(Date.UTC(yr, mo0 + 1, 0)).getUTCDate();
        const day = Math.min(startDay, daysInMo);
        const iso = `${yr}-${String(mo0 + 1).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
        const shortYr = String(yr).slice(2);
        const monthLbl = `${MONTH_NAMES_SHORT[mo0]} '${shortYr}`;
        const fullLbl = `${MONTH_NAMES_SHORT[mo0]} ${yr}`;
        const yearLbl = String(yr);
        const ym = iso.slice(0, 7);

        const deflator = Math.pow(1.0 + infMonthlyRate, m);
        const annualBurnAtM = simAnnualBurn * deflator;
        const rawTargetNetBurn = annualBurnAtM / 12.0;
        const ssnRes = simIncludeSSN
          ? computeMonthlySsnForIso(iso, m)
          : { jNomMo: 0, yNomMo: 0, totNomGross: 0, totNomTax: 0, totNomNet: 0 };
        const ssnGrossMo = ssnRes.totNomGross || 0;
        const ratesForM = (stressProf && m >= 1) ? getStressRates(m) : adjustYear(baseRates, Math.max(0, Math.floor((m - 1) / 12)));
        const ordForM = (simBearShield && stressProf && m >= ddStartM && m <= bsEndIncl) ? bearOrder : null;

        let stepRes;
        if (m === 0) {
          // Dry-run 1-month preview on a cloned state so Bar 0 displays Today's starting balances + Month 1 cashflow preview
          const previewMap = cloneSimAssetState();
          stepRes = stepSimMonth(previewMap, iso, fullLbl, 0.0, 0.0, rawTargetNetBurn, false, ordForM, ratesForM, deflator, ssnGrossMo);
        } else {
          stepRes = stepSimMonth(stateMap, iso, fullLbl, 0.0, 0.0, rawTargetNetBurn, true, ordForM, ratesForM, deflator, ssnGrossMo);
          cumGross += stepRes.monthlyGrossSold;
          cumNet += stepRes.monthlyBurn;
          cumTax += stepRes.monthlyTax;
          cumRealGross += stepRes.monthlyGrossSold / deflator;
          cumRealNet += stepRes.monthlyBurn / deflator;
          cumRealTax += stepRes.monthlyTax / deflator;
          cumSsnGross += ssnGrossMo;
          cumSsnNet += stepRes.ssnNetMo || 0;
          cumWorkContrib += stepRes.monthlyWorkContrib || 0;
          cumRealWorkContrib += (stepRes.monthlyWorkContrib || 0) / deflator;
        }

        const sums = summarizeSimStateBuckets(stateMap);
        if (m > 0 && (sums.totalVal <= 1.0 || (stepRes.unmetNetBurn || 0) > 1.0) && !depletedAtBar) {
          depletedAtBar = { monthIdx: m, iso, fullLbl, years: (m / 12.0).toFixed(1), yearsNum: m / 12.0 };
        }

        const swNom = m > 0 ? (stepRes.schedWithdrawNom || 0) : 0;
        const swReal = m > 0 ? (swNom / deflator) : 0;

        bars.push({
          idx: m,
          iso,
          ym,
          bearShieldActive: Boolean(m > 0 && ordForM),
          inv0Age: ageOnIso(INV0.birth_year, INV0.birth_month, INV0.birth_day, iso, m),
          monthLbl,
          fullLbl,
          yearLbl,
          yearsOut: m / 12.0,
          deflator,
          googVestedVal: sums.googVestedVal,
          googUnvestedVal: sums.googUnvestedVal,
          unvestedVal: sums.googUnvestedVal,
          googVal: sums.googVal,
          nonGoogVal: sums.nonGoogVal,
          totalVal: sums.totalVal,
          total: sums.totalVal,
          totalBasis: sums.totalBasis,
          assetVals: sums.assetVals,
          balances: sums.assetVals,
          growthGain: stepRes.growthGain,
          rawMonthlyExpense: rawTargetNetBurn,
          schedWithdrawNom: swNom,
          schedWithdrawReal: swReal,
          schedWithdrawEvents: m > 0 ? (stepRes.schedWithdrawEvents || []) : [],
          schedActionEvents: m > 0 ? (stepRes.schedActionEvents || []) : [],
          ssnGrossMo: ssnGrossMo,
          ssnTaxMo: stepRes.ssnTaxMo || 0,
          ssnNetMo: stepRes.ssnNetMo || 0,
          monthlyWorkContrib: m > 0 ? (stepRes.monthlyWorkContrib || 0) : 0,
          monthlyPaycheckOffset: stepRes.monthlyPaycheckOffset || 0,
          cumWorkContrib,
          cumRealWorkContrib,
          monthlyBurn: stepRes.monthlyBurn,
          monthlyTax: stepRes.monthlyTax,
          monthlyGrossSold: stepRes.monthlyGrossSold,
          monthlyLossHarvested: stepRes.monthlyLossHarvested || 0,
          monthlyLossSheltered: stepRes.monthlyLossSheltered || 0,
          monthlyTaxSaved: stepRes.monthlyTaxSaved || 0,
          cumGross,
          cumNet,
          cumTax,
          cumRealGross,
          cumRealNet,
          cumRealTax,
          cumSsnGross,
          cumSsnNet,
          cumLossHarvested: stateMap._totalLossHarvested || 0,
          cumLossUsed: stateMap._totalLossUsed || 0,
          cumTaxSaved: stateMap._totalTaxSavedByLoss || 0,
          lossCarryforwardRem: stateMap._lossCarryforward || 0,
          sales: stepRes.sales,
          annualBurnRate: annualBurnAtM,
        });
      }

      SIM_BARS = bars;
      simAssetStats = stateMap;
      simCumGrossSold = cumGross;
      simCumNetProceeds = cumNet;
      simCumTaxPaid = cumTax;
      simCumLossHarvested = stateMap._totalLossHarvested || 0;
      simCumLossUsed = stateMap._totalLossUsed || 0;
      simCumTaxSaved = stateMap._totalTaxSavedByLoss || 0;
      simCumSsnGross = cumSsnGross;
      simCumSsnNet = cumSsnNet;

      // If portfolio survives past 40 years (480 months), continue stepping a cloned state up to 100 years (1200 months) for exact Solvency
      let solvencyDepleted = depletedAtBar;
      if (!solvencyDepleted) {
        const extMap = {
          _lossCarryforward: stateMap._lossCarryforward || 0,
          _totalLossHarvested: stateMap._totalLossHarvested || 0,
          _totalLossUsed: stateMap._totalLossUsed || 0,
          _totalTaxSavedByLoss: stateMap._totalTaxSavedByLoss || 0,
          _taxCfg: stateMap._taxCfg ? { ...stateMap._taxCfg } : null,
          _taxYtd: stateMap._taxYtd ? { ...stateMap._taxYtd } : null,
        };
        Object.keys(stateMap).forEach(k => {
          if (k.startsWith("_")) return;
          const s = stateMap[k];
          extMap[k] = {
            ...s,
            tranches: (s.tranches || []).map(t => ({ ...t })),
          };
        });
        for (let m = SIM_TOTAL_MONTHS + 1; m <= 1200; m++) {
          const totMonthsFromEpoch = startYear * 12 + startMonth0 + m;
          const yr = Math.floor(totMonthsFromEpoch / 12);
          const mo0 = totMonthsFromEpoch % 12;
          const daysInMo = new Date(Date.UTC(yr, mo0 + 1, 0)).getUTCDate();
          const day = Math.min(startDay, daysInMo);
          const iso = `${yr}-${String(mo0 + 1).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
          const fullLbl = `${MONTH_NAMES_SHORT[mo0]} ${yr}`;
          const deflatorExt = Math.pow(1.0 + infMonthlyRate, m);
          const annualBurnAtM = simAnnualBurn * deflatorExt;
          const ssnExtGross = simIncludeSSN ? (computeMonthlySsnForIso(iso, m).totNomGross || 0) : 0.0;
          // DA2-09 / DA2-19 PARITY INVARIANT (lockstep with `run_simulation` months 481..1200 in `simulator.py`):
          // `stepSimMonth` honors global `simIncludeScheduledActions` / `simScheduledActions` across years 41–100,
          // and flags cashflow insolvency if either `sums.totalVal <= 1.0` or `stepExt.unmetNetBurn > 1.0`.
          const stepExt = stepSimMonth(extMap, iso, fullLbl, 0.0, 0.0, annualBurnAtM / 12.0, false, null, adjustYear(baseRates, Math.floor((m - 1) / 12)), deflatorExt, ssnExtGross);
          const sums = summarizeSimStateBuckets(extMap);
          if (sums.totalVal <= 1.0 || (stepExt.unmetNetBurn || 0) > 1.0) {
            solvencyDepleted = { monthIdx: m, iso, fullLbl, years: (m / 12.0).toFixed(1), yearsNum: m / 12.0 };
            break;
          }
        }
      }
      simSolvencyDepleted = solvencyDepleted;

      updateSimSummaryDOM();
      renderSimLiquidationTable();
      renderSimWithdrawalsTable();
      if (typeof syncStressCapsuleTooltips === "function") syncStressCapsuleTooltips();
      scheduleSimIdleAudit();
    }

    function updateSimSummaryDOM() {
      if (!SIM_BARS.length) return;
      const startYear = parseInt(TODAY_ISO.slice(0, 4), 10) || 2026;
      const startMonth0 = (parseInt(TODAY_ISO.slice(5, 7), 10) || 11) - 1;

      const metricsEl = document.getElementById("sim-summary-metrics");
      const solvLblEl = document.getElementById("sim-kpi-solvency-lbl");
      const solvValEl = document.getElementById("sim-kpi-solvency");
      const solvSubEl = document.getElementById("sim-kpi-solvency-sub");
      const inhLblEl = document.getElementById("sim-kpi-inheritance-lbl");
      const inhValEl = document.getElementById("sim-kpi-inheritance");
      const inhRealEl = document.getElementById("sim-kpi-inheritance-real");
      const inhSubEl = document.getElementById("sim-kpi-inheritance-sub");
      const taxLblEl = document.getElementById("sim-kpi-taxdrag-lbl");
      const taxValEl = document.getElementById("sim-kpi-taxdrag");
      const taxSheltEl = document.getElementById("sim-kpi-taxdrag-shelter");
      const taxSubEl = document.getElementById("sim-kpi-taxdrag-sub");

      const b40 = SIM_BARS[SIM_BARS.length - 1];
      const inv0TargetAge = Number(INV0.milestone_age) || 85;
      const inv0TargetYr = (Number(INV0.birth_year) || 1975) + inv0TargetAge;
      const inv0TargetMo0 = Math.max(0, (Number(INV0.birth_month) || 1) - 1);
      const inv0MsIdx = SIM_BARS.findIndex((b, idx) => ageOnIso(INV0.birth_year, INV0.birth_month, INV0.birth_day, b.iso, idx) >= inv0TargetAge);
      const payloadMsMonth = (PAYLOAD && PAYLOAD.milestone_month != null && Number.isFinite(Number(PAYLOAD.milestone_month)))
        ? Math.max(0, Math.min(SIM_BARS.length - 1, Math.round(Number(PAYLOAD.milestone_month))))
        : (SIM_BARS.length - 1);
      const inv0MsBar = inv0MsIdx >= 0 ? SIM_BARS[inv0MsIdx] : SIM_BARS[payloadMsMonth];
      const inv0MsYears = inv0MsIdx >= 0
        ? (inv0MsIdx / 12.0)
        : Math.max(0, ((inv0TargetYr * 12 + inv0TargetMo0) - (startYear * 12 + startMonth0)) / 12.0);
      const inv0MsLbl = inv0MsBar ? inv0MsBar.fullLbl : `${MONTH_NAMES_SHORT[inv0TargetMo0]} ${inv0TargetYr}`;
      const isReal = simDollarMode === "real";
      const dlrTag = isReal ? " (Today's $)" : "";

      // Card 1: ★ Solvency
      const stressTag = simActiveModel === "stress" ? "stress path" : "single run";
      if (solvLblEl) solvLblEl.textContent = `★ Solvency — ${stressTag} (Vs. ${INV0.name}'s ${inv0TargetAge}th Bday)`;
      if (solvValEl && solvSubEl) {
        if (simSolvencyDepleted) {
          const isGreen = inv0MsIdx >= 0
            ? (simSolvencyDepleted.monthIdx >= inv0MsIdx)
            : (simSolvencyDepleted.yearsNum >= inv0MsYears);
          solvValEl.textContent = `${simSolvencyDepleted.years} Years`;
          solvValEl.className = "kpi-val mono " + (isGreen ? "pos" : "neg");
          const jAgeDep = ageOnIso(INV0.birth_year, INV0.birth_month, INV0.birth_day, simSolvencyDepleted.iso, simSolvencyDepleted.monthIdx);
          const agesDepStr = HAS_SECOND_INVESTOR
            ? `${INV0.name}: ${jAgeDep}, ${INV1.name}: ${ageOnIso(INV1.birth_year, INV1.birth_month, INV1.birth_day, simSolvencyDepleted.iso, simSolvencyDepleted.monthIdx)}`
            : `${INV0.name}: ${jAgeDep}`;
          const diffJ85 = simSolvencyDepleted.yearsNum - inv0MsYears;
          const diffTxt = diffJ85 >= 0
            ? `+${diffJ85.toFixed(1)} yrs past ${INV0.name}'s ${inv0TargetAge}th (${inv0MsLbl}, ${inv0MsYears.toFixed(1)} yrs)`
            : `${Math.abs(diffJ85).toFixed(1)} yrs short of ${INV0.name}'s ${inv0TargetAge}th (${inv0MsLbl}, ${inv0MsYears.toFixed(1)} yrs)`;
          solvSubEl.textContent = `Depletes in ${simSolvencyDepleted.fullLbl} (${agesDepStr}) • ${diffTxt}`;
        } else {
          solvValEl.textContent = "100+ Years";
          solvValEl.className = "kpi-val mono pos";
          const yr40Disp = b40 ? simVal(b40, b40.totalVal) : 0;
          solvSubEl.textContent = `Sustains past ${INV0.name}'s ${inv0TargetAge}th (${inv0MsLbl}, ${inv0MsYears.toFixed(1)} yrs) & beyond 100 yrs • Yr 40${dlrTag}: ${fmtUSD0(yr40Disp)}`;
        }
      }

      // Card 2: ★ Inheritance
      if (inhValEl && inhSubEl && inv0MsBar) {
        if (inhLblEl) {
          inhLblEl.textContent = `★ Age ${inv0TargetAge} Wealth (At ${INV0.name}'s ${inv0TargetAge}th • ${inv0MsLbl})`;
        }
        const nomVal85 = Math.max(0, inv0MsBar.totalVal || 0);
        const realVal85 = Math.max(0, nomVal85 / (inv0MsBar.deflator || 1.0));
        inhValEl.className = "kpi-val mono " + (nomVal85 > 0 ? "pos" : "neg");
        inhValEl.style.display = "flex";
        inhValEl.style.justifyContent = "space-between";
        inhValEl.style.alignItems = "baseline";
        inhValEl.style.gap = "10px";
        inhValEl.innerHTML =
          `<span>${fmtUSD0(nomVal85)}</span>` +
          `<span class="mono" style="font-size:15.5px;font-weight:600;color:var(--muted);">${fmtUSD0(realVal85)}</span>`;

        const jAge85 = ageOnIso(INV0.birth_year, INV0.birth_month, INV0.birth_day, inv0MsBar.iso, inv0MsBar.idx);
        const ages85Str = HAS_SECOND_INVESTOR
          ? `${INV0.name}: ${jAge85}, ${INV1.name}: ${ageOnIso(INV1.birth_year, INV1.birth_month, INV1.birth_day, inv0MsBar.iso, inv0MsBar.idx)}`
          : `${INV0.name}: ${jAge85}`;
        if (inhRealEl) {
          inhRealEl.style.textAlign = "right";
          inhRealEl.textContent = `Purchasing power in today's $ (@${simInflationPct.toFixed(1)}%/yr)`;
        }
        if (nomVal85 > 0) {
          const gDisp = simVal(inv0MsBar, inv0MsBar.googVal);
          const ngDisp = simVal(inv0MsBar, inv0MsBar.nonGoogVal);
          const primaryPart = PRIMARY_SYMBOL ? ` • ${PRIMARY_SYMBOL}: ${fmtUSD0(gDisp)}` : "";
          inhSubEl.textContent = `${inv0MsLbl} (${ages85Str})${primaryPart} • Diversified: ${fmtUSD0(ngDisp)}`;
        } else if (simSolvencyDepleted) {
          inhSubEl.textContent = `Portfolio depleted in ${simSolvencyDepleted.fullLbl} (${simSolvencyDepleted.years} yrs) before ${INV0.name}'s ${inv0TargetAge}th (${inv0MsLbl})`;
        } else {
          inhSubEl.textContent = `At ${inv0MsLbl} (${ages85Str})`;
        }
      }

      // Card 3: ★ Lifetime Tax Drag / Efficiency
      if (taxValEl && taxSubEl && inv0MsBar) {
        if (taxLblEl) {
          taxLblEl.textContent = `★ Tax Efficiency (To ${INV0.name}'s ${inv0TargetAge}th • ${inv0MsLbl})`;
        }
        // DA2-07a PARITY INVARIANT: Compute `effPct85` & `eff40` in the active dollar mode (`cumRealTax / cumRealGross`
        // when `isReal` is true), matching `simulator.py` and Monte Carlo / Historical `tax_to_ms`.
        const tax85 = isReal ? (inv0MsBar.cumRealTax || 0) : (inv0MsBar.cumTax || 0);
        const gross85 = isReal ? (inv0MsBar.cumRealGross || 0) : (inv0MsBar.cumGross || 0);
        const net85 = isReal ? (inv0MsBar.cumRealNet || 0) : (inv0MsBar.cumNet || 0);
        const effPct85 = gross85 > 0 ? (tax85 / gross85) * 100.0 : 0;
        taxValEl.textContent = `-${fmtUSD0(tax85)} (${effPct85.toFixed(1)}% eff.)`;
        taxValEl.className = "kpi-val mono " + (effPct85 <= 12.0 ? "pos" : "neg");

        if (taxSheltEl) {
          const msDfl = isReal ? (inv0MsBar.deflator || 1.0) : 1.0;
          const lossSav85 = (inv0MsBar.cumTaxSaved || 0) / msDfl;
          const lossHarv85 = (inv0MsBar.cumLossHarvested || 0) / msDfl;
          if (lossSav85 > 1) {
            taxSheltEl.innerHTML = `Cap. Loss Carryforward Saved: <strong class="mono" style="color:#34d399;">+${fmtUSD0(lossSav85)} tax</strong> <span class="muted">(${fmtUSD0(lossHarv85)} losses harvested)</span>`;
          } else if (lossHarv85 > 1) {
            taxSheltEl.innerHTML = `Cap. Losses Harvested: <strong class="mono" style="color:#7dd3fc;">${fmtUSD0(lossHarv85)}</strong> <span class="muted">(available to shelter future LTCG)</span>`;
          } else {
            taxSheltEl.textContent = `Sold ${fmtUSD0(gross85)} gross → ${fmtUSD0(net85)} net spend through ${inv0MsLbl}`;
          }
        }
        const gross40Disp = isReal && b40 ? (b40.cumRealGross || 0) : simCumGrossSold;
        const tax40Disp = isReal && b40 ? (b40.cumRealTax || 0) : simCumTaxPaid;
        const eff40 = gross40Disp > 0 ? (tax40Disp / gross40Disp) * 100.0 : 0;
        taxSubEl.textContent = `Through ${inv0MsLbl}: ${(100 - effPct85).toFixed(1)}% kept • Total Tax${dlrTag}: -${fmtUSD0(tax40Disp)} (${eff40.toFixed(1)}%)`;
      }

      if (metricsEl && inv0MsBar && b40) {
        const j85Disp = simVal(inv0MsBar, inv0MsBar.totalVal);
        const b40Disp = simVal(b40, b40.totalVal);
        metricsEl.innerHTML =
          `<span>Start: <strong class="mono" style="color:var(--text);">${fmtUSD0(SIM_BARS[0].totalVal)}</strong></span>` +
          `<span style="margin:0 6px;">&bull;</span>` +
          `<span>${escHtml(INV0.name)}'s ${inv0TargetAge}th (${inv0MsBar.yearLbl}): <strong class="mono" style="color:${j85Disp > 0 ? '#34d399' : '#fb7185'};">${fmtUSD0(j85Disp)}</strong></span>` +
          `<span style="margin:0 6px;">&bull;</span>` +
          `<span>Yr 40 (${b40.yearLbl}): <strong class="mono" style="color:${b40Disp > 0 ? '#34d399' : '#fb7185'};">${fmtUSD0(b40Disp)}</strong></span>`;
      }

      // === Monte Carlo DISTRIBUTION (Range) ===
      if (simMcActive()) {
        ensureSimMcResult();
        const mc = getLiveMcDisplayResult();
        if (mc) {
          const bandAt = (monthIdx) => {
            const band = mc.band;
            if (!band.length) return { nominal: { p10: 0, p25: 0, p50: 0, p75: 0, p90: 0 }, real: { p10: 0, p25: 0, p50: 0, p75: 0, p90: 0 } };
            if (monthIdx <= band[0].monthIdx) return band[0];
            if (monthIdx >= band[band.length - 1].monthIdx) return band[band.length - 1];
            let i = 0; while (i < band.length - 1 && band[i + 1].monthIdx < monthIdx) i++;
            const a = band[i], b2 = band[i + 1];
            const f = (monthIdx - a.monthIdx) / (b2.monthIdx - a.monthIdx);
            const lp = (sp) => ({
              p10: a[sp].p10 + (b2[sp].p10 - a[sp].p10) * f,
              p25: (a[sp].p25 ?? a[sp].p10) + ((b2[sp].p25 ?? b2[sp].p10) - (a[sp].p25 ?? a[sp].p10)) * f,
              p50: a[sp].p50 + (b2[sp].p50 - a[sp].p50) * f,
              p75: (a[sp].p75 ?? a[sp].p90) + ((b2[sp].p75 ?? b2[sp].p90) - (a[sp].p75 ?? a[sp].p90)) * f,
              p90: a[sp].p90 + (b2[sp].p90 - a[sp].p90) * f,
            });
            return { nominal: lp("nominal"), real: lp("real") };
          };
          const colPct = (p) => (p >= 90 ? "pos" : (p >= 75 ? "" : "neg"));
          const pathCountLbl = mc.is_partial && mc.total_paths
            ? `${mc.n_paths.toLocaleString("en-US")}/${mc.total_paths.toLocaleString("en-US")}`
            : mc.n_paths.toLocaleString("en-US");

          if (solvLblEl) solvLblEl.textContent = `★ Solvency — probability (to ${INV0.name}'s ${inv0TargetAge}th)`;
          if (solvValEl && solvSubEl) {
            const s85 = mc.success_to_ms_pct, s40 = mc.success_to_horizon_pct;
            solvValEl.textContent = `${s85.toFixed(0)}% funded to ${inv0TargetAge}`;
            solvValEl.className = "kpi-val mono " + colPct(s85);
            solvSubEl.textContent = `${s85.toFixed(0)}% of ${pathCountLbl} ${mc.model === "historical" ? "Historical" : "Normal"}-model paths funded at ${INV0.name}'s ${inv0TargetAge}th (${inv0MsLbl}) • ${s40.toFixed(0)}% fund full 40 yrs`;
          }

          if (inhValEl && inhSubEl) {
            const at85 = bandAt(mc.ms_month);
            const msR = mc.ms_real || at85.real;
            const medNom = Math.max(0, at85.nominal.p50), medReal = Math.max(0, msR.p50 ?? at85.real.p50);
            if (inhLblEl) inhLblEl.textContent = `★ Inheritance — median (At ${INV0.name}'s ${inv0TargetAge}th • ${inv0MsLbl})`;
            inhValEl.className = "kpi-val mono " + (medNom > 0 ? "pos" : "neg");
            inhValEl.style.display = "flex";
            inhValEl.style.justifyContent = "space-between";
            inhValEl.style.alignItems = "baseline";
            inhValEl.style.gap = "10px";
            inhValEl.innerHTML = `<span>${fmtUSD0(medNom)}</span><span class="mono" style="font-size:15.5px;font-weight:600;color:var(--muted);">${fmtUSD0(medReal)}</span>`;
            if (inhRealEl) { inhRealEl.style.textAlign = "right"; inhRealEl.textContent = "Median (p50) • today's $ at right"; }
            inhSubEl.textContent = `p25 · p50 · p75 (today's $): ${fmtUSD0(Math.max(0, msR.p25 || 0))} · ${fmtUSD0(medReal)} · ${fmtUSD0(Math.max(0, msR.p75 || 0))}`;
          }

          if (taxValEl && taxSubEl && mc.tax_to_ms) {
            const tx = mc.tax_to_ms;
            if (taxLblEl) taxLblEl.textContent = `★ Tax Efficiency — median path (To ${INV0.name}'s ${inv0TargetAge}th)`;
            taxValEl.textContent = `${tx.net_kept_pct.toFixed(1)}% kept (${tx.eff_tax_pct.toFixed(1)}% eff.)`;
            taxValEl.className = "kpi-val mono " + (tx.eff_tax_pct <= 12.0 ? "pos" : "neg");
            if (taxSheltEl) taxSheltEl.textContent = `Median path cumulative tax to Age ${inv0TargetAge}: -${fmtUSD0(tx.cum_tax_real)} (today's $)`;
            taxSubEl.textContent = `Evaluated per path across ${pathCountLbl} Monte-Carlo simulations`;
          }
        }
      }

      // === Historical cohort cards ===
      if (simCohortActive()) {
        ensureSimCohortResult();
        const co = getLiveCohortDisplayResult();
        if (co && co.available) {
          const bandAt = (monthIdx) => {
            const band = co.band;
            if (!band.length) return { nominal: { p10: 0, p25: 0, p50: 0, p75: 0, p90: 0 }, real: { p10: 0, p25: 0, p50: 0, p75: 0, p90: 0 } };
            if (monthIdx <= band[0].monthIdx) return band[0];
            if (monthIdx >= band[band.length - 1].monthIdx) return band[band.length - 1];
            let i = 0; while (i < band.length - 1 && band[i + 1].monthIdx < monthIdx) i++;
            const a = band[i], b2 = band[i + 1], f = (monthIdx - a.monthIdx) / (b2.monthIdx - a.monthIdx);
            const lp = (sp) => ({
              p10: a[sp].p10 + (b2[sp].p10 - a[sp].p10) * f,
              p25: (a[sp].p25 ?? a[sp].p10) + ((b2[sp].p25 ?? b2[sp].p10) - (a[sp].p25 ?? a[sp].p10)) * f,
              p50: a[sp].p50 + (b2[sp].p50 - a[sp].p50) * f,
              p75: (a[sp].p75 ?? a[sp].p90) + ((b2[sp].p75 ?? b2[sp].p90) - (a[sp].p75 ?? a[sp].p90)) * f,
              p90: a[sp].p90 + (b2[sp].p90 - a[sp].p90) * f,
            });
            return { nominal: lp("nominal"), real: lp("real") };
          };
          const colPct = (p) => (p >= 90 ? "pos" : (p >= 75 ? "" : "neg"));
          const cohortCountLbl = co.is_partial && co.total_cohorts ? `${co.n_cohorts}/${co.total_cohorts}` : String(co.n_cohorts);
          const dlrModeLbl = isReal ? "Today's $" : "Nominal $";
          const dlrModeLower = isReal ? "today's $" : "nominal $";
          if (solvLblEl) solvLblEl.textContent = `★ Solvency — historical cohorts (to ${INV0.name}'s ${inv0TargetAge}th)`;
          if (solvValEl && solvSubEl) {
            const s85 = co.success_to_ms_pct, n85 = co.success_to_ms_n;
            const lastBand = Array.isArray(co.band) && co.band.length ? co.band[co.band.length - 1] : null;
            const endMed = lastBand ? Math.max(0, isReal ? (lastBand.real?.p50 || 0) : (lastBand.nominal?.p50 || 0)) : null;
            const endYrLbl = lastBand && lastBand.yearsOut ? `Yr ${Math.round(lastBand.yearsOut)} P50` : "End P50";
            const endTxt = endMed != null ? ` • ${endYrLbl} (${dlrModeLbl}): ${fmtUSD0(endMed)}` : "";
            if (s85 != null) {
              solvValEl.textContent = `${s85.toFixed(0)}% funded to ${inv0TargetAge}`;
              solvValEl.className = "kpi-val mono " + colPct(s85);
              const fullTxt = co.success_full_pct != null ? ` • ${co.success_full_n[0]}/${co.success_full_n[1]} funded full 40 yrs` : "";
              solvSubEl.textContent = `${n85[0]} of ${n85[1]} start years (${co.data_range[0]}–${co.data_range[1]}) still funded at ${INV0.name}'s ${inv0TargetAge}th${fullTxt}${endTxt}`;
            } else {
              solvValEl.textContent = "—"; solvSubEl.textContent = "Not enough history for this horizon.";
            }
          }
          if (inhValEl && inhSubEl) {
            const at85 = bandAt(co.ms_month);
            const msR = co.ms_real || at85.real;
            const msN = co.ms_nominal || at85.nominal;
            const medNom = Math.max(0, msN.p50 ?? at85.nominal.p50);
            const medReal = Math.max(0, msR.p50 ?? at85.real.p50);
            const primaryMed = isReal ? medReal : medNom;
            const secondaryMed = isReal ? medNom : medReal;
            const activeQ = isReal ? msR : msN;
            if (inhLblEl) inhLblEl.textContent = `★ Inheritance — median cohort (At ${INV0.name}'s ${inv0TargetAge}th • ${inv0MsLbl} • ${dlrModeLbl})`;
            inhValEl.className = "kpi-val mono " + (primaryMed > 0 ? "pos" : "neg");
            inhValEl.style.display = "flex"; inhValEl.style.justifyContent = "space-between"; inhValEl.style.alignItems = "baseline"; inhValEl.style.gap = "10px";
            inhValEl.innerHTML = `<span>${fmtUSD0(primaryMed)}</span><span class="mono" style="font-size:15.5px;font-weight:600;color:var(--muted);">${fmtUSD0(secondaryMed)}</span>`;
            if (inhRealEl) {
              inhRealEl.style.textAlign = "right";
              inhRealEl.textContent = isReal
                ? "Median start year (today's $) • nominal $ at right"
                : "Median start year (nominal $) • today's $ at right";
            }
            inhSubEl.textContent = `p25 · p50 · p75 (${dlrModeLower}): ${fmtUSD0(Math.max(0, activeQ.p25 || 0))} · ${fmtUSD0(primaryMed)} · ${fmtUSD0(Math.max(0, activeQ.p75 || 0))}`;
          }
          if (taxValEl && taxSubEl && co.tax_to_ms) {
            const tx = co.tax_to_ms;
            const effPct = isReal ? tx.eff_tax_pct : (tx.eff_tax_nominal_pct ?? tx.eff_tax_pct);
            const keptPct = isReal ? tx.net_kept_pct : (tx.net_kept_nominal_pct ?? tx.net_kept_pct);
            const cumTaxDisp = isReal ? tx.cum_tax_real : (tx.cum_tax_nominal ?? tx.cum_tax_real);
            if (taxLblEl) taxLblEl.textContent = `★ Tax Efficiency — median cohort (To ${INV0.name}'s ${inv0TargetAge}th)`;
            taxValEl.textContent = `${keptPct.toFixed(1)}% kept (${effPct.toFixed(1)}% eff.)`;
            taxValEl.className = "kpi-val mono " + (effPct <= 12.0 ? "pos" : "neg");
            if (taxSheltEl) taxSheltEl.textContent = `Median cohort cumulative tax to Age ${inv0TargetAge}: -${fmtUSD0(cumTaxDisp)} (${dlrModeLower})`;
            taxSubEl.textContent = `Evaluated across ${cohortCountLbl} historical rolling cohorts (${dlrModeLbl})`;
          }
        }
      }
    }

    function computeCurrentKpiSummary() {
      if (!SIM_BARS.length) recomputeSimBars();
      const inv0TargetAge = Number(INV0.milestone_age) || 85;
      const msIdx = SIM_BARS.findIndex((b, idx) => ageOnIso(INV0.birth_year, INV0.birth_month, INV0.birth_day, b.iso, idx) >= inv0TargetAge);
      const msBar = msIdx >= 0 ? SIM_BARS[msIdx] : SIM_BARS[SIM_BARS.length - 1];
      const yearsSolvent = simSolvencyDepleted ? Number(simSolvencyDepleted.yearsNum.toFixed(1)) : 100.0;
      const activeActs = simIncludeScheduledActions ? (simScheduledActions || []) : [];
      const actionSummary = {
        hypothetical_count: (simHypotheticalAssets || []).length,
        hypothetical_labels: (simHypotheticalAssets || []).map(h => String(h.label || h.id || "")),
        actions_included: Boolean(simIncludeScheduledActions),
        transfers: activeActs.filter(a => a && a.type === "transfer").length,
        deposits: activeActs.filter(a => a && a.type === "deposit").length,
        withdrawals: activeActs.filter(a => a && (a.type || "withdrawal") === "withdrawal").length,
      };

      const lens = ["single_path", "stress", "range", "historical"].includes(simActiveModel) ? simActiveModel : "single_path";
      if (lens === "range") {
        const mc = getMcResult();
        const msQ = mc.ms_real || { p25: 0, p50: 0, p75: 0 };
        return {
          active_lens: "range",
          age_85_real: {
            has_quantiles: true,
            value: Math.round(msQ.p50 || 0),
            p25: Math.round(msQ.p25 || 0),
            p50: Math.round(msQ.p50 || 0),
            p75: Math.round(msQ.p75 || 0),
          },
          solvency: {
            funded_to_85_pct: Number(mc.success_to_ms_pct || 0),
            years_solvent: mc.median_years_solvent != null ? Number(mc.median_years_solvent) : yearsSolvent,
          },
          tax_to_85: mc.tax_to_ms || { net_kept_pct: 100, eff_tax_pct: 0, cum_tax_real: 0 },
          action_summary: actionSummary,
        };
      }
      if (lens === "historical") {
        const co = getCohortResult();
        if (co && co.available) {
          const msQ = co.ms_real || { p25: 0, p50: 0, p75: 0 };
          return {
            active_lens: "historical",
            age_85_real: {
              has_quantiles: true,
              value: Math.round(msQ.p50 || 0),
              p25: Math.round(msQ.p25 || 0),
              p50: Math.round(msQ.p50 || 0),
              p75: Math.round(msQ.p75 || 0),
            },
            solvency: {
              funded_to_85_pct: Number(co.success_to_ms_pct || 0),
              years_solvent: co.median_years_solvent != null ? Number(co.median_years_solvent) : yearsSolvent,
            },
            tax_to_85: co.tax_to_ms || { net_kept_pct: 100, eff_tax_pct: 0, cum_tax_real: 0 },
            action_summary: actionSummary,
          };
        }
      }
      const val85 = msBar ? Math.max(0, (msBar.totalVal || 0) / (msBar.deflator || 1.0)) : 0;
      // DA2-07a PARITY INVARIANT (lockstep with `evaluate_active_model` in `simulator.py`):
      // Always compute `effTax` in Today's real dollars (`cumRealTax / cumRealGross * 100`).
      const effTax = (msBar && (msBar.cumRealGross || 0) > 0) ? Math.round(((msBar.cumRealTax || 0) / msBar.cumRealGross) * 1000) / 10 : 0.0;
      const fundedPct = (!simSolvencyDepleted || (msIdx >= 0 && simSolvencyDepleted.monthIdx > msIdx)) ? 100.0 : 0.0;
      return {
        active_lens: lens === "stress" ? "stress" : "single_path",
        age_85_real: {
          has_quantiles: false,
          value: Math.round(val85),
          p25: Math.round(val85),
          p50: Math.round(val85),
          p75: Math.round(val85),
        },
        solvency: {
          funded_to_85_pct: fundedPct,
          years_solvent: yearsSolvent,
        },
        tax_to_85: {
          net_kept_pct: Math.round((100.0 - effTax) * 10) / 10,
          eff_tax_pct: effTax,
          cum_tax_real: Math.round(msBar ? (msBar.cumRealTax || 0) : 0),
        },
        action_summary: actionSummary,
      };
    }

    function setSimDollarMode(mode) {
      simDollarMode = mode === "real" ? "real" : "nominal";
      const nomBtn = document.getElementById("sim-dlr-nom-btn");
      const realBtn = document.getElementById("sim-dlr-real-btn");
      if (nomBtn) nomBtn.classList.toggle("active", simDollarMode === "nominal");
      if (realBtn) realBtn.classList.toggle("active", simDollarMode === "real");
      updateSimSummaryDOM();
      renderSimChartAndNav();
      renderSimLiquidationTable();
    }

    function setSimScaleMode(mode) {
      simScaleMode = mode === "log" ? "log" : (mode === "linear" ? "linear" : "auto");
      const autoBtn = document.getElementById("sim-scale-auto-btn");
      const logBtn = document.getElementById("sim-scale-log-btn");
      const linBtn = document.getElementById("sim-scale-lin-btn");
      if (autoBtn) autoBtn.classList.toggle("active", simScaleMode === "auto");
      if (logBtn) logBtn.classList.toggle("active", simScaleMode === "log");
      if (linBtn) linBtn.classList.toggle("active", simScaleMode === "linear");
      renderSimChartAndNav();
    }

    function setSimIncludeUnvested(include) {
      simIncludeUnvested = Boolean(include);
      const offBtn = document.getElementById("sim-unvested-off-btn");
      const onBtn = document.getElementById("sim-unvested-on-btn");
      if (offBtn) offBtn.classList.toggle("active", !simIncludeUnvested);
      if (onBtn) onBtn.classList.toggle("active", simIncludeUnvested);
      setActiveSimComputingControl(simIncludeUnvested ? onBtn : offBtn, true);
      if (typeof updateSimBaselineSummary === "function") updateSimBaselineSummary();
      updateSimOverridesSummary();
      recomputeSimBars();
      renderSimChartAndNav();
      checkSaveDirtyState();
    }

    function setSimIncludeSSN(include) {
      simIncludeSSN = Boolean(include);
      ["sim-ssn-off-btn", "ssn-feed-off-btn"].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.classList.toggle("active", !simIncludeSSN);
      });
      ["sim-ssn-on-btn", "ssn-feed-on-btn"].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.classList.toggle("active", simIncludeSSN);
      });
      if (currentScreen === "retirement") {
        setActiveSimComputingControl(simIncludeSSN ? "sim-ssn-on-btn" : "sim-ssn-off-btn", true);
      }
      if (typeof updateSimBaselineSummary === "function") updateSimBaselineSummary();
      updateSimOverridesSummary();
      recomputeSimBars();
      if (currentScreen === "retirement") {
        renderSimChartAndNav();
      } else if (currentScreen === "income") {
        updateSsnSummaryDOM();
      }
      checkSaveDirtyState();
    }

    function buildHeirLegacyOrder() {
      const sym = String(PRIMARY_SYMBOL || "").toUpperCase();
      function keyTuple(a) {
        const aid = String(a.id || "");
        const tm = a.tax_model;
        const tw = a.tax_wrapper;
        const fc = resolveSimAssetForecast(a);
        const gp = Number(fc.growth_pct) || 0.0;
        const isSs = (a.bucket === "goog") || (fc.risk_class === "single_stock");
        let tier = 7, sub = -gp;
        if (aid.includes("high_basis") || a.is_unvested_goog) {
          tier = 1; sub = -gp;
        } else if (tm === "ANNUITY_LIFO" && gp < 5.0) {
          tier = 2; sub = gp;
        } else if (tm === "LTCG_POOL" && !isSs) {
          tier = 3; sub = gp;
        } else if (tm === "MUNI") {
          tier = 4; sub = gp;
        } else if (tw === "TRAD_IRA" || tm === "ORDINARY_FULL") {
          tier = 5; sub = gp;
        } else if (tm === "ANNUITY_LIFO") {
          tier = 6; sub = gp;
        } else if (tm === "CASH") {
          tier = 8; sub = 0.0;
        } else if (tw === "ROTH_IRA" || tm === "TAX_FREE") {
          tier = 9; sub = gp;
        }
        return [tier, sub, a.default_rank || 999, aid];
      }
      return [...SIM_ASSETS].sort((a, b) => {
        const ka = keyTuple(a), kb = keyTuple(b);
        for (let i = 0; i < 3; i++) {
          if (Math.abs(ka[i] - kb[i]) > 1e-9) return ka[i] - kb[i];
        }
        return String(ka[3]).localeCompare(String(kb[3]));
      }).map(a => a.id);
    }

    function computeBearShieldOrder(baseOrder) {
      const ord = Array.isArray(baseOrder) && baseOrder.length ? baseOrder : simAssetOrder;
      const byId = Object.fromEntries(SIM_ASSETS.map(a => [a.id, a]));
      const pos = Object.fromEntries(ord.map((id, i) => [id, i]));
      function tier(aid) {
        const a = byId[aid] || {};
        const tm = a.tax_model;
        const tw = a.tax_wrapper;
        const rc = a.default_risk_class || "";
        if (tm === "CASH") return 0;
        if (tm === "MUNI") return 1;
        if (tm === "ANNUITY_LIFO" || (a.rate_model === "fixed" && tw !== "TRAD_IRA") || rc === "cash_fixed" || rc === "fixed_income") {
          return tw !== "TRAD_IRA" ? 2 : 4;
        }
        if (tw === "ROTH_IRA" || tm === "TAX_FREE") return 5;
        return 3;
      }
      return [...ord].sort((a, b) => (tier(a) - tier(b)) || ((pos[a] ?? 999) - (pos[b] ?? 999)));
    }

    function toggleSimBearShield() {
      simBearShield = !simBearShield;
      updateSimOrderPresetButtons();
      setActiveSimComputingControl("sim-bear-shield-btn", true);
      recomputeSimBars();
      renderSimChartAndNav();
      updateSimSummaryDOM();
      checkSaveDirtyState();
    }

    function updateSimOrderPresetButtons() {
      const optBtn = document.getElementById("sim-order-opt-btn");
      const autoBtn = document.getElementById("sim-order-auto-btn");
      const legBtn = document.getElementById("sim-order-legacy-btn");
      const gBtn = document.getElementById("sim-order-goog-btn");
      const bsBtn = document.getElementById("sim-bear-shield-btn");
      if (optBtn) optBtn.classList.toggle("active", simOrderPreset === "tax_optimal");
      if (autoBtn) autoBtn.classList.toggle("active", simOrderPreset === "auto_optimal");
      if (legBtn) legBtn.classList.toggle("active", simOrderPreset === "heir_legacy");
      if (gBtn) gBtn.classList.toggle("active", simOrderPreset === "goog_first");
      if (bsBtn) {
        bsBtn.classList.toggle("active", Boolean(simBearShield));
        bsBtn.setAttribute("aria-pressed", simBearShield ? "true" : "false");
        bsBtn.innerHTML = simBearShield ? "Bear Shield: On" : "Bear Shield: Off";
      }

      const baseLabels = {
        "sim-order-opt-btn": "Tax-Optimal",
        "sim-order-auto-btn": "Auto-Optimize",
        "sim-order-legacy-btn": "Heir Legacy (&sect;1014)",
        "sim-order-goog-btn": "Concentrated First",
      };
      const presetToId = {
        tax_optimal: "sim-order-opt-btn",
        auto_optimal: "sim-order-auto-btn",
        heir_legacy: "sim-order-legacy-btn",
        goog_first: "sim-order-goog-btn",
      };
      Object.entries(presetToId).forEach(([preset, btnId]) => {
        const el = document.getElementById(btnId);
        if (!el) return;
        const isWinner = simMostTaxEfficientPreset === preset;
        const starPrefix = isWinner ? '<span style="color:#facc15;font-weight:700;margin-right:4px;">&#9733;</span>' : '';
        el.innerHTML = `${starPrefix}${baseLabels[btnId]}`;
        el.classList.toggle("tax-winner-btn", isWinner);
      });

      if (typeof _setSimRefTip === "function") {
        _setSimRefTip(optBtn, {
          title: "Tax-Optimal",
          label1: "Withdrawal order",
          meaning: "Restores the portfolio’s default priority for selling holdings to cover spending.",
          label2: "Tax effect",
          calculation: "High-basis shares usually come early because selling them realizes less gain. Actual taxes depend on your holdings, account types and configured ranks.",
          clickHint: "Click to apply the default order.",
        });
        _setSimRefTip(autoBtn, {
          title: "Auto-Optimize",
          label1: "Spending comes first",
          meaning: "Simple and Early Crash favor the order that delays depletion or a spending shortfall longest. Monte Carlo and Historical favor the order that fully funds the most tested paths or cohorts.",
          label2: "When funding ties",
          calculation: "After-tax wealth at the milestone is the next consideration. The search uses four Monte Carlo paths or up to four historical cohorts, so the result depends on the sample and your assumptions.",
          clickHint: "Click to search and apply the selected order immediately.",
        });
        _setSimRefTip(legBtn, {
          title: "Heir Legacy (§1014)",
          label1: "Withdrawal order",
          meaning: "Prioritizes high-basis shares and newly vested shares, then low-growth annuities. Traditional IRA holdings generally come ahead of low-basis single stock.",
          label2: "Why use it",
          calculation: "Keeping appreciated taxable shares for heirs can preserve a potential basis step-up. Earlier pretax withdrawals can bring income tax forward.",
          clickHint: "Click to apply the inheritance-focused preset.",
        });
        _setSimRefTip(gBtn, {
          title: "Concentrated First",
          label1: "Withdrawal order",
          meaning: "Moves single-stock holdings and the primary-equity bucket ahead of other holdings. Each group keeps its default priority.",
          label2: "Why use it",
          calculation: "As spending comes due, selling eligible shares first reduces exposure to a single company. Selling low-basis shares can realize gains sooner.",
          clickHint: "Click to apply the stock-first order.",
        });
        _setSimRefTip(bsBtn, {
          title: "Bear Shield (Downturn Drawdown Guard)",
          label1: "Priority shift:",
          meaningHtml:
            "Temporarily routes monthly spending through <strong>Cash</strong> &rarr; <strong>Munis</strong> &rarr; <strong>Unlocked Fixed Income / Annuities</strong> ahead of <strong>Equities</strong>, keeping <strong>Traditional IRAs</strong> behind stocks and <strong>Roth</strong> strictly last (surrender locks, RMDs, and Scheduled Events still apply).",
          label2: "When it activates:",
          calcHtml:
            "&bull; <strong>Historical &amp; Bootstrap Monte Carlo:</strong> Year <em>after</em> the broad market drops more than 5% or your primary stock drops more than 10% (no look-ahead).<br/>" +
            "&bull; <strong>Normal Monte Carlo:</strong> Year after a meaningfully down market year.<br/>" +
            "&bull; <strong>Market Correction:</strong> Throughout the downfall and first half of the broad-market bounce-back.<br/>" +
            "<span style='color:#94a3b8;display:block;margin-top:4px;'><em>In practice:</em> No market timing needed&mdash;when stocks sit &gt;5&ndash;10% below their prior close or peak, fund monthly spending from cash and bonds until equities recover.</span>",
          clickHint: simBearShield ? "Click to turn Bear Shield off." : "Click to turn Bear Shield on.",
        });
      }
    }

    let _optScoreCache = null;  // O4: memoize candidate scores within one optimizer run.

    // O3 / DA2-14 PARITY INVARIANT (lockstep with `_embedded_tax_liability` & `_opt_total_embedded_liability` in `simulator.py`):
    // Models SECURE Act 10-year progressive IRD distributions on inherited IRAs/annuities + 40% Federal Estate Tax above `$14M`/`$28M` CPI-indexed exemption.
    function embeddedTaxLiability(st, a, stateMap = null) {
      const rate = a.tax_rate || 0;
      const val = st.val || 0;
      const basis = st.costBasis || 0;
      const m = a.tax_model;
      if (val <= 0 || m === "TAX_FREE" || m === "MUNI" || m === "CASH" || m === "LTCG_POOL" || m === "LTCG_TRANCHES" || m === "UNVESTED_GOOG") {
        return 0;
      }
      const taxableAmt = m === "ORDINARY_FULL" ? val : Math.max(0, val - basis);
      if (taxableAmt <= 0.005) return 0;
      const tc = stateMap && stateMap._taxCfg;
      if (tc && tc.progressive) {
        const dfl = Number((stateMap._taxYtd && stateMap._taxYtd.dfl) || 1.0);
        const isMfj = Boolean(tc.is_mfj);
        const curOrd = FED_STD_DED_2026[isMfj] * dfl;
        const annChunk = 0.10 * taxableAmt;
        const applyNiit = m === "ANNUITY_LIFO";
        const effSt = Number(tc.state_rate);
        const pts = ordDxBreakpoints(curOrd, curOrd, annChunk, 0.0, tc, dfl, applyNiit);
        let annTax = 0.0;
        for (let i = 0; i < pts.length - 1; i++) {
          const a0 = pts[i], a1 = pts[i + 1];
          const seg = a1 - a0;
          if (seg <= 1e-12) continue;
          const mid = 0.5 * (a0 + a1);
          annTax += seg * ordMarginalRateAt(curOrd + mid, curOrd + mid, tc, dfl, applyNiit, effSt, 0.0);
        }
        return 10.0 * annTax;
      }
      return rate * taxableAmt;
    }

    function _optSpendableTot(stateMap) {
      let totalVal = 0;
      for (let i = 0; i < SIM_ASSETS.length; i++) {
        const a = SIM_ASSETS[i];
        if (a.is_unvested_goog && !simIncludeUnvested) continue;
        const st = stateMap[a.id];
        if (!st) continue;
        if (a.bucket === "goog" && a.is_unvested_goog && st.tranches) {
          for (const t of st.tranches) { if (t.vested) totalVal += t.val; }
        } else {
          totalVal += st.val;
        }
      }
      return totalVal;
    }

    function _optEmbeddedTot(stateMap, spendableTot = null) {
      const tot = spendableTot != null ? spendableTot : _optSpendableTot(stateMap);
      let irEmb = 0;
      for (let i = 0; i < SIM_ASSETS.length; i++) {
        const a = SIM_ASSETS[i];
        if (a.is_unvested_goog && !simIncludeUnvested) continue;
        const st = stateMap[a.id];
        if (st) irEmb += embeddedTaxLiability(st, a, stateMap);
      }
      const tc = stateMap && stateMap._taxCfg;
      if (tc && tc.progressive) {
        const dfl = Number((stateMap._taxYtd && stateMap._taxYtd.dfl) || 1.0);
        const exemption = (tc.is_mfj ? 28000000.0 : 14000000.0) * dfl;
        const estateTax = 0.40 * Math.max(0.0, Math.max(0.0, tot - irEmb) - exemption);
        return irEmb + estateTax;
      }
      return irEmb;
    }

    // DA-C04 (User Decision A8 — Mode-Aware Auto-Optimize):
    // PARITY LOCKSTEP WARNING: Must match `optimize_sim_asset_order()` in `simulator.py` 1:1!
    // Changing `mcSeededRng(1)`, CRN path count (`4`), stratified cohort anchors (`[1929, 1966, 1973, 2000]`),
    // or multi-path pass count (`1` pairwise-swap pass from best-of-4 seeds vs `4` passes in single-path)
    // REQUIRES updating `simulator.py` simultaneously (`test_engine_parity.py::test_optimizer_matches`).
    function _buildOptimizerContext(msTargetIdx) {
      const adjustYear = buildYearRateAdjuster();
      const baseRates = computeAssetMonthlyRates();
      const infMonthlyRate = Math.pow(1.0 + simInflationPct / 100.0, 1.0 / 12.0) - 1.0;
      const ddStartOpt = computeDrawdownStartMonth(buildWorkPlanConfig(), TODAY_ISO, simAnnualBurn, infMonthlyRate);
      const stressRes = buildStressMonthResolver(baseRates, adjustYear, ddStartOpt);
      const stressProf = stressRes.profile;
      const getStressRates = stressRes.getRatesForMonth;
      const bsEndM = stressProf ? stressProf.bear_shield_months : 0;
      const startYear = parseInt(TODAY_ISO.slice(0, 4), 10) || 2026;
      const startMonth0 = (parseInt(TODAY_ISO.slice(5, 7), 10) || 11) - 1;
      const startDay = parseInt(TODAY_ISO.slice(8, 10), 10) || 10;

      const monthlySchedule = [];
      for (let m = 1; m <= SIM_TOTAL_MONTHS; m++) {
        const totMonthsFromEpoch = startYear * 12 + startMonth0 + m;
        const yr = Math.floor(totMonthsFromEpoch / 12);
        const mo0 = totMonthsFromEpoch % 12;
        const daysInMo = new Date(Date.UTC(yr, mo0 + 1, 0)).getUTCDate();
        const day = Math.min(startDay, daysInMo);
        const iso = `${yr}-${String(mo0 + 1).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
        const deflator = Math.pow(1.0 + infMonthlyRate, m);
        const rawBurnMo = (simAnnualBurn * deflator) / 12.0;
        const ssnGross = simIncludeSSN ? (computeMonthlySsnForIso(iso, m).totNomGross || 0) : 0.0;
        monthlySchedule.push({ iso, rawBurnMo, ssnGross, deflator });
      }
      const msDeflatorOpt = msTargetIdx >= 1 ? monthlySchedule[msTargetIdx - 1].deflator : 1.0;
      const activeLens = String(simActiveModel || "single_path").trim().toLowerCase();
      const useBs = Boolean(simBearShield);

      let mcOptPaths = null;
      let histOptCohorts = null;
      const histOptCyRates = {};
      const histOptCyInf = {};
      const histOptCyBear = {};
      const histOptCyTbill = {};
      const histOptSsnBase = [];
      let histLastYo = 2025;

      if (activeLens === "range") {
        const rawSsVol = mcSingleStockVolPct != null ? Number(mcSingleStockVolPct) : 36.2;
        const rawMktVol = mcBroadEquityVolPct != null ? Number(mcBroadEquityVolPct) : 13.9;
        const rawCorr = Math.max(-0.999, Math.min(0.999, Number(mcSingleStockCorr ?? 0.52)));
        const zeroVolOpt = (rawSsVol <= 0.0 && rawMktVol <= 0.0);
        const defaultRhoOpt = {
          cash_fixed: 0.0, fixed_income: 0.20, hybrid_buffered: 0.75,
          broad_equity: 1.00, real_diversifier: 0.05, single_stock: 0.55, crypto_speculative: 0.45,
        };
        const defaultVolOpt = {
          cash_fixed: 0.0, fixed_income: 0.045, hybrid_buffered: 0.085,
          broad_equity: 0.140, real_diversifier: 0.150, single_stock: 0.360, crypto_speculative: 0.680,
        };
        const optSpecs = SIM_ASSETS.map(a => {
          const fc = resolveSimAssetForecast(a);
          const rc = fc.risk_class;
          const meanDec = fc.growth_pct / 100.0;
          const mLog = meanDec > -0.999 ? Math.log(1.0 + meanDec) : -6.9;
          const erDec = (fc.expense_ratio_pct || 0.0) / 100.0;
          const rvol = a.realized_vol_pct;
          const cvol = (fc.vol_pct != null && Number.isFinite(Number(fc.vol_pct))) ? Number(fc.vol_pct) : null;
          let sdA = 0.0, rhoA = 0.0;
          if (rc === "cash_fixed" || zeroVolOpt) {
            sdA = 0.0; rhoA = 0.0;
          } else if (cvol != null) {
            sdA = Math.max(0.0, cvol / 100.0);
            rhoA = a.corr_spy != null ? Number(a.corr_spy) : (defaultRhoOpt[rc] ?? 0.55);
          } else if (rvol != null && Number(rvol) > 0.0 && !fc.risk_overridden && a.rate_model !== "fixed") {
            sdA = Math.max(0.0, Number(rvol) / 100.0);
            rhoA = a.corr_spy != null ? Number(a.corr_spy) : (defaultRhoOpt[rc] ?? 0.55);
          } else if (rc === "single_stock") {
            sdA = Math.max(0.0, rawSsVol / 100.0);
            rhoA = rawCorr;
          } else if (rc === "broad_equity") {
            sdA = Math.max(0.0, rawMktVol / 100.0);
            rhoA = 1.00;
          } else {
            sdA = defaultVolOpt[rc] ?? 0.140;
            rhoA = defaultRhoOpt[rc] ?? 0.55;
          }
          rhoA = Math.max(-0.999, Math.min(0.999, Number(rhoA)));
          const isDet = (rc === "cash_fixed") || (sdA <= 0.0);
          const cMkt = sdA * rhoA;
          const cOrth = sdA * Math.sqrt(Math.max(0.0, 1.0 - rhoA * rhoA));
          const rawSym = String(a.symbol || "").trim().toUpperCase();
          const pfk = Array.isArray(a.parent_forecast_keys) ? a.parent_forecast_keys : [];
          let sgrp;
          if (rawSym && !rawSym.startsWith("#") && !rawSym.includes(" ")) {
            sgrp = `SYM:${rawSym}`;
          } else if (pfk.length > 0 && String(pfk[0] || "").trim()) {
            sgrp = `SYM:${String(pfk[0]).trim().toUpperCase()}`;
          } else if (a.bucket === "goog" || a.bucket === "primary_equity") {
            sgrp = PRIMARY_SYMBOL ? `SYM:${PRIMARY_SYMBOL}` : "BUCKET:PRIMARY_EQUITY";
          } else {
            sgrp = `ID:${a.id}`;
          }
          return { id: a.id, meanDec, mLog, erDec, isDet, cMkt, cOrth, sgrp };
        });

        const rngOpt = mcSeededRng(1);
        const nOptYrs = Math.ceil(msTargetIdx / 12);
        mcOptPaths = [];
        for (let p = 0; p < 4; p++) {
          const yrRatesP = [];
          const yrBearP = [];
          for (let y = 0; y < nOptYrs; y++) {
            const zM = rngOpt.gauss();
            yrBearP.push(useBs && !zeroVolOpt && (zM < -0.50));
            const rmapY = {};
            const geps = {};
            for (let si = 0; si < optSpecs.length; si++) {
              const sp = optSpecs[si];
              let na;
              if (sp.isDet) {
                na = sp.meanDec - sp.erDec;
              } else {
                if (!(sp.sgrp in geps)) geps[sp.sgrp] = rngOpt.gauss();
                na = (Math.exp(sp.mLog + sp.cMkt * zM + sp.cOrth * geps[sp.sgrp]) - 1.0) - sp.erDec;
              }
              rmapY[sp.id] = Math.pow(1.0 + Math.max(-0.9999, na), 1.0 / 12.0) - 1.0;
            }
            yrRatesP.push(adjustYear(rmapY, y));
          }
          mcOptPaths.push({ yrRatesP, yrBearP });
        }
      } else if (activeLens === "historical") {
        const cohRows = ((window.COHORT_DATA || {}).years) || [];
        if (cohRows.length > 0) {
          const ydataOpt = {};
          cohRows.forEach(r => { ydataOpt[Number(r.year)] = r; });
          const allYOpt = Object.keys(ydataOpt).map(Number).sort((a, b) => a - b);
          const firstYo = allYOpt[0];
          histLastYo = allYOpt[allYOpt.length - 1];
          const cmOpt = String(cohortCenterMode || "anchor").trim().toLowerCase();
          const imOpt = String(cohortInflationMode || "real_cpi").trim().toLowerCase();
          const byYrRates = buildCohortAssetMonthlyRates(allYOpt, ydataOpt, cmOpt);
          for (const yk of allYOpt) {
            const rw = ydataOpt[yk];
            histOptCyRates[yk] = byYrRates[yk];
            histOptCyTbill[yk] = Number(rw.tbill_3m_pct ?? 3.5);
            histOptCyInf[yk] = imOpt === "real_cpi"
              ? (Math.pow(1.0 + Number(rw.inflation_pct) / 100.0, 1.0 / 12.0) - 1.0)
              : infMonthlyRate;
            const spTp = Number(rw.sp_total_ret_pct ?? rw.sp_ret_pct);
            const ssP = Number(rw.single_stock_ret_pct ?? rw.goog_ret_pct ?? 0.0);
            histOptCyBear[yk] = useBs && (spTp < -5.0 || Number(rw.sp_ret_pct) < -5.0 || ssP < -10.0);
          }
          for (let m = 1; m <= msTargetIdx; m++) {
            const isoM = monthlySchedule[m - 1].iso;
            histOptSsnBase.push(
              simIncludeSSN
                ? (computeMonthlySsnForIso(isoM, m, ssnInv0ClaimAge, ssnInv1ClaimAge, imOpt === "real_cpi" ? 0.0 : ssnColaPct).totNomGross || 0.0)
                : 0.0
            );
          }
          const anchors = [1929, 1966, 1973, 2000];
          const fromYo = Math.max(firstYo, Math.round(cohortStartFrom != null && cohortStartFrom >= 0 ? cohortStartFrom : firstYo));
          let chosenY = anchors.filter(y => y >= fromYo && y <= histLastYo - 5);
          if (chosenY.length < 3) {
            const step = Math.max(1, Math.floor(allYOpt.length / 4));
            chosenY = [];
            for (let k = 0; k < allYOpt.length && chosenY.length < 4; k += step) {
              const y = allYOpt[k];
              if (y >= fromYo && (histLastYo - y + 1) * 12 >= 60) chosenY.push(y);
            }
          }
          histOptCohorts = chosenY.map(y => ({ yStart: y, runM: Math.min(msTargetIdx, (histLastYo - y + 1) * 12) }));
        }
      }

      return {
        adjustYear, baseRates, ddStartOpt, stressProf, getStressRates, monthlySchedule, msDeflatorOpt, useBs,
        mcOptPaths, histOptCohorts, histOptCyRates, histOptCyInf, histOptCyBear, histOptCyTbill, histOptSsnBase, histLastYo,
        cmOpt: String(cohortCenterMode || "anchor").trim().toLowerCase(),
      };
    }

    function evaluateSimCandidateOrder(candidateOrder, msTargetIdx, optCtx = null) {
      if (_optScoreCache) {
        const ck = candidateOrder.join("|");
        if (_optScoreCache.has(ck)) return _optScoreCache.get(ck);
      }
      const ctx = optCtx || _buildOptimizerContext(msTargetIdx);

      if (ctx.mcOptPaths) {
        const bearCand = ctx.useBs ? computeBearShieldOrder(candidateOrder) : candidateOrder;
        let survived = 0;
        const netVals = [];
        for (let p = 0; p < ctx.mcOptPaths.length; p++) {
          const { yrRatesP, yrBearP } = ctx.mcOptPaths[p];
          const sm = cloneSimAssetState();
          let depP = false;
          for (let m = 1; m <= msTargetIdx; m++) {
            const sc = ctx.monthlySchedule[m - 1];
            const yi = Math.floor((m - 1) / 12);
            const sres = stepSimMonth(
              sm, sc.iso, "", 0.0, 0.0, sc.rawBurnMo, false,
              yrBearP[yi - 1] ? bearCand : candidateOrder,
              yrRatesP[yi], sc.deflator, sc.ssnGross
            );
            if (!depP && sres.targetMonthlyBurn > 1.0 && sres.monthlyBurn < sres.targetMonthlyBurn - 1.0) depP = true;
          }
          if (!depP) survived++;
          const totV = _optSpendableTot(sm);
          const embV = _optEmbeddedTot(sm, totV);
          netVals.push((totV - embV) / ctx.msDeflatorOpt);
        }
        const sv = netVals.slice().sort((a, b) => a - b);
        const p25 = mcPercentile(sv, 0.25);
        const p50 = mcPercentile(sv, 0.50);
        const meanV = sv.reduce((s, v) => s + v, 0.0) / sv.length;
        const res = {
          s0: survived,
          s1: Math.round(0.5 * (p25 + p50) * 10) / 10,
          s2: Math.round(p50 * 10) / 10,
          s3: Math.round(meanV * 10) / 10,
        };
        if (_optScoreCache) _optScoreCache.set(candidateOrder.join("|"), res);
        return res;
      }

      if (ctx.histOptCohorts && ctx.histOptCohorts.length > 0) {
        const bearCand = ctx.useBs ? computeBearShieldOrder(candidateOrder) : candidateOrder;
        const cmOpt = ctx.cmOpt || String(cohortCenterMode || "anchor").trim().toLowerCase();
        const imOpt = String(cohortInflationMode || "real_cpi").trim().toLowerCase();
        let survived = 0;
        const netVals = [];
        for (let ci = 0; ci < ctx.histOptCohorts.length; ci++) {
          const { yStart, runM } = ctx.histOptCohorts[ci];
          const sm = cloneSimAssetState();
          let dflC = 1.0;
          let depC = false;
          let cyR = ctx.histOptCyRates[yStart];
          let cyI = ctx.histOptCyInf[yStart];
          let cyB = false;
          let cyTb = ctx.histOptCyTbill[yStart] ?? 3.5;
          for (let m = 1; m <= runM; m++) {
            if ((m - 1) % 12 === 0) {
              const hy = Math.min(ctx.histLastYo, yStart + Math.floor((m - 1) / 12));
              cyR = cmOpt === "anchor" ? ctx.adjustYear(ctx.histOptCyRates[hy], hy - yStart) : ctx.histOptCyRates[hy];
              cyI = ctx.histOptCyInf[hy];
              cyB = hy > yStart && ctx.histOptCyBear[hy - 1];
              cyTb = ctx.histOptCyTbill[hy] ?? 3.5;
            }
            dflC *= 1.0 + cyI;
            const iso = ctx.monthlySchedule[m - 1].iso;
            const rawB = (simAnnualBurn * dflC) / 12.0;
            const ssnG = imOpt === "real_cpi" ? (ctx.histOptSsnBase[m - 1] * dflC) : ctx.histOptSsnBase[m - 1];
            const sres = stepSimMonth(
              sm, iso, "", 0.0, 0.0, rawB, false,
              cyB ? bearCand : candidateOrder,
              cyR, dflC, ssnG, cyTb
            );
            if (!depC && sres.targetMonthlyBurn > 1.0 && sres.monthlyBurn < sres.targetMonthlyBurn - 1.0) depC = true;
          }
          if (!depC) survived++;
          const totV = _optSpendableTot(sm);
          const embV = _optEmbeddedTot(sm, totV);
          netVals.push((totV - embV) / dflC);
        }
        const sv = netVals.slice().sort((a, b) => a - b);
        const p25 = mcPercentile(sv, 0.25);
        const p50 = mcPercentile(sv, 0.50);
        const meanV = sv.reduce((s, v) => s + v, 0.0) / sv.length;
        const res = {
          s0: survived,
          s1: Math.round(0.5 * (p25 + p50) * 10) / 10,
          s2: Math.round(p50 * 10) / 10,
          s3: Math.round(meanV * 10) / 10,
        };
        if (_optScoreCache) _optScoreCache.set(candidateOrder.join("|"), res);
        return res;
      }

      const stateMap = cloneSimAssetState();
      let depMonth = SIM_TOTAL_MONTHS + 1;
      let valAt85 = 0;
      let embed85 = 0;
      let valAt40 = 0;
      let embed40 = 0;

      const bearCand = (ctx.useBs && ctx.stressProf) ? computeBearShieldOrder(candidateOrder) : candidateOrder;
      const ddStartO = ctx.ddStartOpt || 1;
      const bsEndOptIncl = ctx.stressProf ? (ddStartO + ctx.stressProf.bear_shield_months - 1) : 0;
      for (let m = 1; m <= SIM_TOTAL_MONTHS; m++) {
        const sc = ctx.monthlySchedule[m - 1];
        const ratesForM = ctx.stressProf ? ctx.getStressRates(m) : ctx.adjustYear(ctx.baseRates, Math.floor((m - 1) / 12));
        const ordForM = (ctx.stressProf && m >= ddStartO && m <= bsEndOptIncl) ? bearCand : candidateOrder;

        const sresD = stepSimMonth(stateMap, sc.iso, "", 0.0, 0.0, sc.rawBurnMo, false, ordForM, ratesForM, sc.deflator, sc.ssnGross);

        const totalVal = _optSpendableTot(stateMap);
        if (depMonth > SIM_TOTAL_MONTHS && (totalVal <= 1.0 || (sresD.unmetNetBurn || 0) > 1.0)) {
          depMonth = m;
        }
        if (m === msTargetIdx) {
          valAt85 = totalVal;
          embed85 = _optEmbeddedTot(stateMap, totalVal);
        }
        if (m === SIM_TOTAL_MONTHS) {
          valAt40 = totalVal;
          embed40 = _optEmbeddedTot(stateMap, totalVal);
        }
      }

      const res = {
        s0: depMonth,
        s1: Math.round((valAt85 - embed85) * 10) / 10,
        s2: Math.round(valAt85 * 10) / 10,
        s3: Math.round((valAt40 - embed40) * 10) / 10,
      };
      if (_optScoreCache) _optScoreCache.set(candidateOrder.join("|"), res);
      return res;
    }

    function isBetterSimOrderScore(cand, best) {
      if (cand.s0 !== best.s0) return cand.s0 > best.s0;
      for (const k of ["s1", "s2", "s3"]) {
        const tol = Math.max(1.0, 0.0005 * Math.abs(best[k]));
        if (Math.abs(cand[k] - best[k]) > tol) return cand[k] > best[k];
      }
      return false;
    }

    function createAutoOptimizerStepper(opts = {}) {
      _optScoreCache = new Map();
      const inv0TargetAge = Number(INV0.milestone_age) || 85;
      let msTargetIdx = SIM_BARS.findIndex((b, idx) => ageOnIso(INV0.birth_year, INV0.birth_month, INV0.birth_day, b.iso, idx) >= inv0TargetAge);
      if (msTargetIdx <= 0) {
        msTargetIdx = SIM_TOTAL_MONTHS;
        for (let m = 1; m <= SIM_TOTAL_MONTHS; m++) {
          const info = simMonthIdxToInfo(m);
          if (ageOnIso(INV0.birth_year, INV0.birth_month, INV0.birth_day, info.iso, m) >= inv0TargetAge) { msTargetIdx = m; break; }
        }
      }
      const optCtx = _buildOptimizerContext(msTargetIdx);

      const isSingleStock = a => resolveSimAssetForecast(a).risk_class === "single_stock" || a.bucket === "goog";
      const defOrder = [...SIM_ASSETS].sort((a, b) => (a.default_rank || 999) - (b.default_rank || 999) || String(a.id).localeCompare(String(b.id))).map(a => a.id);
      const heirOrder = buildHeirLegacyOrder();
      const gFirstOrder = [...SIM_ASSETS].sort((a, b) => (isSingleStock(a) !== isSingleStock(b) ? (isSingleStock(a) ? -1 : 1) : ((a.default_rank || 999) - (b.default_rank || 999) || String(a.id).localeCompare(String(b.id))))).map(a => a.id);

      const byId = Object.fromEntries(SIM_ASSETS.map(a => [a.id, a]));
      const cashTail = defOrder.filter(aid => (byId[aid] || {}).tax_model === "CASH");
      const rothTail = defOrder.filter(aid => (byId[aid] || {}).tax_model === "TAX_FREE" && !cashTail.includes(aid));
      const lockedTail = [...cashTail, ...rothTail];
      const lockedSet = new Set(lockedTail);

      const curSeed = (simAssetOrder || []).filter(aid => defOrder.includes(aid));
      defOrder.forEach(aid => { if (!curSeed.includes(aid)) curSeed.push(aid); });

      const seeds = [defOrder, heirOrder, gFirstOrder, curSeed];
      let globalBestOrder = [...defOrder.filter(x => !lockedSet.has(x)), ...lockedTail];
      let globalBestScore = evaluateSimCandidateOrder(globalBestOrder, msTargetIdx, optCtx);

      const isMultiOpt = Boolean(optCtx.mcOptPaths) || Boolean(optCtx.histOptCohorts && optCtx.histOptCohorts.length > 0);
      const singleBestSeed = Boolean(opts.singleBestSeedOnly) || isMultiOpt;
      const includeInserts = opts.includeInserts !== undefined ? Boolean(opts.includeInserts) : !isMultiOpt;
      const maxOptPasses = opts.maxPasses != null ? opts.maxPasses : (singleBestSeed ? 1 : 4);

      let phase = singleBestSeed ? "eval_seeds" : "search_seed_init";
      let seedEvalIdx = 0;
      let searchSeeds = singleBestSeed ? null : seeds;
      let sIdx = 0;
      let passIdx = 0;
      let stage = "swaps"; // 'swaps' -> 'inserts' -> 'pass_end'
      let curMov = [];
      let curOrder = [];
      let curScore = globalBestScore;
      let bestNeighborMov = null;
      let bestNeighborScore = globalBestScore;
      let iCursor = 0;
      let jCursor = 1;
      let fromCursor = 0;
      let toCursor = 0;
      let finished = false;

      function initSeed() {
        while (sIdx < searchSeeds.length) {
          curMov = searchSeeds[sIdx].filter(x => !lockedSet.has(x));
          curOrder = [...curMov, ...lockedTail];
          curScore = evaluateSimCandidateOrder(curOrder, msTargetIdx, optCtx);
          passIdx = 0;
          if (curMov.length < 2 || maxOptPasses <= 0) {
            if (isBetterSimOrderScore(curScore, globalBestScore)) {
              globalBestScore = curScore;
              globalBestOrder = curOrder;
            }
            sIdx++;
            continue;
          }
          initPass();
          return;
        }
        finishAll();
      }

      function initPass() {
        bestNeighborMov = null;
        bestNeighborScore = curScore;
        stage = "swaps";
        iCursor = 0;
        jCursor = 1;
        fromCursor = 0;
        toCursor = 0;
      }

      function finishAll() {
        _optScoreCache = null;
        finished = true;
      }

      function advanceOneStep() {
        if (finished) return true;
        if (phase === "eval_seeds") {
          if (seedEvalIdx < seeds.length) {
            const candOrd = [...seeds[seedEvalIdx].filter(x => !lockedSet.has(x)), ...lockedTail];
            const scS = evaluateSimCandidateOrder(candOrd, msTargetIdx, optCtx);
            if (isBetterSimOrderScore(scS, globalBestScore)) {
              globalBestScore = scS;
              globalBestOrder = candOrd;
            }
            seedEvalIdx++;
            return false;
          }
          searchSeeds = [globalBestOrder];
          sIdx = 0;
          phase = "search";
          initSeed();
          return finished;
        }

        if (phase === "search_seed_init") {
          phase = "search";
          initSeed();
          return finished;
        }

        const n = curMov.length;
        if (stage === "swaps") {
          if (iCursor < n - 1) {
            const swapped = [...curMov];
            const tmp = swapped[iCursor];
            swapped[iCursor] = swapped[jCursor];
            swapped[jCursor] = tmp;
            const sc = evaluateSimCandidateOrder([...swapped, ...lockedTail], msTargetIdx, optCtx);
            if (isBetterSimOrderScore(sc, bestNeighborScore)) {
              bestNeighborScore = sc;
              bestNeighborMov = swapped;
            }
            jCursor++;
            if (jCursor >= n) {
              iCursor++;
              jCursor = iCursor + 1;
            }
            return false;
          }
          stage = includeInserts ? "inserts" : "pass_end";
          return false;
        }

        if (stage === "inserts") {
          if (fromCursor < n) {
            if (toCursor !== fromCursor) {
              const moved = [...curMov];
              const [item] = moved.splice(fromCursor, 1);
              moved.splice(toCursor, 0, item);
              const sc = evaluateSimCandidateOrder([...moved, ...lockedTail], msTargetIdx, optCtx);
              if (isBetterSimOrderScore(sc, bestNeighborScore)) {
                bestNeighborScore = sc;
                bestNeighborMov = moved;
              }
            }
            toCursor++;
            if (toCursor >= n) {
              fromCursor++;
              toCursor = 0;
            }
            return false;
          }
          stage = "pass_end";
          return false;
        }

        // stage === "pass_end"
        if (bestNeighborMov) {
          curMov = bestNeighborMov;
          curOrder = [...curMov, ...lockedTail];
          curScore = bestNeighborScore;
          passIdx++;
          if (passIdx < maxOptPasses) {
            initPass();
            return false;
          }
        }
        if (isBetterSimOrderScore(curScore, globalBestScore)) {
          globalBestScore = curScore;
          globalBestOrder = curOrder;
        }
        sIdx++;
        initSeed();
        return finished;
      }

      return {
        stepSlice(maxMs) {
          const t0 = _simNowMs();
          let steps = 0;
          while (!finished) {
            advanceOneStep();
            steps++;
            if (!finished && maxMs !== Infinity && steps >= 1 && (_simNowMs() - t0) >= maxMs) {
              return false;
            }
          }
          return true;
        },
        getBestOrder() {
          return globalBestOrder;
        },
        movableCount: Math.max(0, defOrder.length - lockedTail.length),
      };
    }

    function findAutoOptimizedOrder() {
      const stepper = createAutoOptimizerStepper();
      stepper.stepSlice(Infinity);
      return stepper.getBestOrder();
    }

    function optimizeSimAssetOrder() {
      setActiveSimComputingControl("sim-order-auto-btn", true);
      const globalBestOrder = findAutoOptimizedOrder();
      simAssetOrder = globalBestOrder;
      simOrderPreset = "auto_optimal";
      updateSimOrderPresetButtons();
      recomputeSimBars();
      renderSimChartAndNav();
      checkSaveDirtyState();
    }

    function evaluateSimTaxEfficiency(candidateOrder) {
      const adjustYear = buildYearRateAdjuster();
      const baseRates = computeAssetMonthlyRates();
      const infMonthlyRate = Math.pow(1.0 + simInflationPct / 100.0, 1.0 / 12.0) - 1.0;
      const ddStartTax = computeDrawdownStartMonth(buildWorkPlanConfig(), TODAY_ISO, simAnnualBurn, infMonthlyRate);
      const stressRes = buildStressMonthResolver(baseRates, adjustYear, ddStartTax);
      const stressProf = stressRes.profile;
      const getStressRates = stressRes.getRatesForMonth;
      const bsEndIncl = stressProf ? (ddStartTax + stressProf.bear_shield_months - 1) : 0;

      const startYear = parseInt(TODAY_ISO.slice(0, 4), 10) || 2026;
      const startMonth0 = (parseInt(TODAY_ISO.slice(5, 7), 10) || 11) - 1;
      const startDay = parseInt(TODAY_ISO.slice(8, 10), 10) || 10;

      const stateMap = cloneSimAssetState();
      let cumGross = 0;
      let cumTax = 0;
      let cumNet = 0;
      let depMonth = SIM_TOTAL_MONTHS + 1;
      const bearOrder = simBearShield ? computeBearShieldOrder(candidateOrder) : candidateOrder;

      for (let m = 1; m <= SIM_TOTAL_MONTHS; m++) {
        const totMonthsFromEpoch = startYear * 12 + startMonth0 + m;
        const yr = Math.floor(totMonthsFromEpoch / 12);
        const mo0 = totMonthsFromEpoch % 12;
        const daysInMo = new Date(Date.UTC(yr, mo0 + 1, 0)).getUTCDate();
        const day = Math.min(startDay, daysInMo);
        const iso = `${yr}-${String(mo0 + 1).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
        const deflator = Math.pow(1.0 + infMonthlyRate, m);
        const annualBurnAtM = simAnnualBurn * deflator;
        const ssnGross = simIncludeSSN ? (computeMonthlySsnForIso(iso, m).totNomGross || 0) : 0.0;
        const ratesForM = stressProf ? getStressRates(m) : adjustYear(baseRates, Math.floor((m - 1) / 12));
        const ordForM = (simBearShield && stressProf && m >= ddStartTax && m <= bsEndIncl) ? bearOrder : candidateOrder;

        const res = stepSimMonth(stateMap, iso, "", 0.0, 0.0, annualBurnAtM / 12.0, false, ordForM, ratesForM, deflator, ssnGross);
        cumGross += res.monthlyGrossSold || 0;
        cumTax += res.monthlyTax || 0;
        cumNet += res.monthlyBurn || 0;

        const totalVal = _optSpendableTot(stateMap);
        if (depMonth > SIM_TOTAL_MONTHS && (totalVal <= 1.0 || (res.unmetNetBurn || 0) > 1.0)) {
          depMonth = m;
        }
      }

      const endSpendTot = _optSpendableTot(stateMap);
      const terminalNetWealth = Math.max(0, endSpendTot - _optEmbeddedTot(stateMap, endSpendTot));

      const effTaxPct = cumGross > 0 ? (cumTax / cumGross) * 100.0 : 0.0;
      return { cumTax, cumGross, cumNet, effTaxPct, terminalNetWealth, depMonth };
    }

    let _simIdleAuditTimer = null;
    let _simIdleSliceTimer = null;
    let _simIdleAuditSeq = 0;
    let simMostTaxEfficientPreset = null;
    let simTaxAuditDetails = null;

    function _cancelSimIdleAudit() {
      _simIdleAuditSeq++;
      if (_simIdleAuditTimer) {
        clearTimeout(_simIdleAuditTimer);
        _simIdleAuditTimer = null;
      }
      if (_simIdleSliceTimer) {
        clearTimeout(_simIdleSliceTimer);
        _simIdleSliceTimer = null;
      }
      _optScoreCache = null;
    }

    function activateMostTaxEfficientStrategy() {
      if (!simMostTaxEfficientPreset) return;
      if (simMostTaxEfficientPreset === "auto_optimal") {
        optimizeSimAssetOrder();
      } else {
        resetSimAssetOrder(simMostTaxEfficientPreset);
      }
    }

    function _applyIdleTaxAuditStrategies(strategies) {
      // Rank strategies by:
      // 1. Solvency (longer survival month)
      // 2. Lowest lifetime tax paid (cumTax)
      // 3. Highest terminal net wealth (terminalNetWealth)
      strategies.sort((a, b) => {
        if (a.eval.depMonth !== b.eval.depMonth) return b.eval.depMonth - a.eval.depMonth;
        const taxDiff = a.eval.cumTax - b.eval.cumTax;
        if (Math.abs(taxDiff) > 100) return taxDiff;
        return b.eval.terminalNetWealth - a.eval.terminalNetWealth;
      });

      const winner = strategies[0];
      simMostTaxEfficientPreset = winner.preset;

      const otherTaxes = strategies.slice(1).map(s => s.eval.cumTax);
      const maxTax = Math.max(...otherTaxes);
      const taxOptStrategy = strategies.find(s => s.preset === "tax_optimal");
      const taxSavingsVsBaseline = (taxOptStrategy && taxOptStrategy !== winner)
        ? Math.max(0, taxOptStrategy.eval.cumTax - winner.eval.cumTax)
        : Math.max(0, maxTax - winner.eval.cumTax);

      simTaxAuditDetails = {
        winner,
        strategies,
        taxSavingsVsBaseline,
      };

      updateSimOrderPresetButtons();

      const legendEl = document.getElementById("sim-order-star-legend");
      if (legendEl) {
        legendEl.style.display = "inline-flex";
        legendEl.innerHTML = `&#9733; Most tax efficient`;

        const tipRows = strategies.map(s => {
          const isW = s.preset === winner.preset;
          const mark = isW ? "★ " : "   ";
          return `${mark}${s.name}: ${fmtUSD0(s.eval.cumTax)} tax (Nominal $, ${s.eval.effTaxPct.toFixed(1)}% eff.)`;
        }).join("\n");

        legendEl.setAttribute("data-tip", `${winner.name} ranks first in the 40-year single-path check. It favors later depletion or shortfall first, then lower total tax. If funding ties and taxes are within $100, higher ending after-tax wealth decides.\n${tipRows}\n\nClick to activate.`);
      }
    }

    function runSimIdleTaxAudit(syncMode = true) {
      if (!Array.isArray(SIM_ASSETS) || !SIM_ASSETS.length) return;
      _cancelSimIdleAudit();
      const seq = _simIdleAuditSeq;

      const isSingleStock = a => resolveSimAssetForecast(a).risk_class === "single_stock" || a.bucket === "goog";
      const taxOptOrder = [...SIM_ASSETS].sort((a, b) => (a.default_rank || 999) - (b.default_rank || 999) || String(a.id).localeCompare(String(b.id))).map(a => a.id);
      const heirOrder = buildHeirLegacyOrder();
      const googFirstOrder = [...SIM_ASSETS].sort((a, b) => {
        const sa = isSingleStock(a), sb = isSingleStock(b);
        if (sa !== sb) return sa ? -1 : 1;
        return (a.default_rank || 999) - (b.default_rank || 999) || String(a.id).localeCompare(String(b.id));
      }).map(a => a.id);

      // Bound background work on large multi-account portfolios so idle audit never stalls or overheats
      const optOpts = (!syncMode && SIM_ASSETS.length > 14)
        ? { singleBestSeedOnly: true, includeInserts: false, maxPasses: 1 }
        : {};
      const stepper = createAutoOptimizerStepper(optOpts);

      if (syncMode) {
        stepper.stepSlice(Infinity);
        const autoOptOrder = stepper.getBestOrder();
        const strategies = [
          { preset: "tax_optimal", name: "Tax-Optimal", btnId: "sim-order-opt-btn", order: taxOptOrder },
          { preset: "auto_optimal", name: "Auto-Optimize", btnId: "sim-order-auto-btn", order: autoOptOrder },
          { preset: "heir_legacy", name: "Heir Legacy (§1014)", btnId: "sim-order-legacy-btn", order: heirOrder },
          { preset: "goog_first", name: "Concentrated First", btnId: "sim-order-goog-btn", order: googFirstOrder },
        ];
        strategies.forEach(s => { s.eval = evaluateSimTaxEfficiency(s.order); });
        _applyIdleTaxAuditStrategies(strategies);
        return;
      }

      let strategies = null;
      let evalIdx = 0;
      function stepBg() {
        _simIdleSliceTimer = null;
        if (seq !== _simIdleAuditSeq) return;
        if (strategies === null) {
          const done = stepper.stepSlice(12);
          if (!done) {
            _simIdleSliceTimer = setTimeout(stepBg, 8);
            return;
          }
          const autoOptOrder = stepper.getBestOrder();
          strategies = [
            { preset: "tax_optimal", name: "Tax-Optimal", btnId: "sim-order-opt-btn", order: taxOptOrder },
            { preset: "auto_optimal", name: "Auto-Optimize", btnId: "sim-order-auto-btn", order: autoOptOrder },
            { preset: "heir_legacy", name: "Heir Legacy (§1014)", btnId: "sim-order-legacy-btn", order: heirOrder },
            { preset: "goog_first", name: "Concentrated First", btnId: "sim-order-goog-btn", order: googFirstOrder },
          ];
          _simIdleSliceTimer = setTimeout(stepBg, 8);
          return;
        }
        if (evalIdx < strategies.length) {
          strategies[evalIdx].eval = evaluateSimTaxEfficiency(strategies[evalIdx].order);
          evalIdx++;
          _simIdleSliceTimer = setTimeout(stepBg, 8);
          return;
        }
        _applyIdleTaxAuditStrategies(strategies);
      }
      _simIdleSliceTimer = setTimeout(stepBg, 0);
    }

    function scheduleSimIdleAudit() {
      _cancelSimIdleAudit();
      _simIdleAuditTimer = setTimeout(() => {
        _simIdleAuditTimer = null;
        runSimIdleTaxAudit(false);
      }, 5000);
    }

    // === CodeReview A3: Monte Carlo engine (mirror of valuation/simulator.run_monte_carlo) ======
    // Reuses stepSimMonth unchanged; only the two equity buckets are randomized per path/year.
    function mcSeededRng(seed) {
      // mulberry32 uniform in [0,1) + Box-Muller gaussian; deterministic given seed.
      let a = (seed >>> 0) || 0x9e3779b9;
      let spare = null;
      function u() {
        a |= 0; a = (a + 0x6D2B79F5) | 0;
        let t = Math.imul(a ^ (a >>> 15), 1 | a);
        t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
        return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
      }
      function gauss() {
        if (spare !== null) { const s = spare; spare = null; return s; }
        let u1 = 0, u2 = 0;
        while (u1 <= 1e-12) u1 = u();
        u2 = u();
        const mag = Math.sqrt(-2.0 * Math.log(u1));
        spare = mag * Math.sin(2.0 * Math.PI * u2);
        return mag * Math.cos(2.0 * Math.PI * u2);
      }
      function int(nExclusive) { return Math.floor(u() * nExclusive); }
      return { u, gauss, int };
    }

    function mcPercentile(sortedVals, q) {
      const n = sortedVals.length;
      if (n === 0) return 0.0;
      if (n === 1) return sortedVals[0];
      const pos = q * (n - 1);
      const lo = Math.floor(pos), hi = Math.ceil(pos);
      if (lo === hi) return sortedVals[lo];
      const frac = pos - lo;
      return sortedVals[lo] * (1.0 - frac) + sortedVals[hi] * frac;
    }

    function _simNowMs() {
      return (typeof performance !== "undefined" && performance && typeof performance.now === "function")
        ? performance.now()
        : Date.now();
    }

    function _collectScheduledCheckpointMonths(m, iso, cpSet) {
      if (m === 0 || m % 12 === 0) cpSet.add(m);
      if (m >= 1 && simIncludeScheduledActions && Array.isArray(simScheduledActions) && simScheduledActions.length > 0) {
        const ym = iso.slice(0, 7);
        for (let i = 0; i < simScheduledActions.length; i++) {
          const act = simScheduledActions[i];
          if (!act) continue;
          const cad = act.cadence || "one_time";
          if (cad === "monthly") continue;
          if (actionTriggersInYm(act, ym, false)) {
            cpSet.add(m - 1);
            cpSet.add(m);
            break;
          }
        }
      }
    }

    // mc: { singleStockVolPct?, broadEquityVolPct?, singleStockCorr?, googVolPct?, nonGoogVolPct?, googRestCorr?,
    //       returnModel: 'normal'|'historical', simulations, histPairs?: [[gRet,bRet],...] }
    function createMonteCarloStepper(mc, seed) {
      mc = mc || {};
      const horizon = SIM_TOTAL_MONTHS;
      const infMonthlyRate = Math.pow(1.0 + simInflationPct / 100.0, 1.0 / 12.0) - 1.0;
      const startYear = parseInt(TODAY_ISO.slice(0, 4), 10) || 2026;
      const startMonth0 = (parseInt(TODAY_ISO.slice(5, 7), 10) || 11) - 1;
      const startDay = parseInt(TODAY_ISO.slice(8, 10), 10) || 10;

      // Deterministic monthly schedule (identical across every path).
      const sched = [];
      const cpSet = new Set();
      let msTargetMonth = horizon;
      const targetAge = Number(INV0.milestone_age) || 85;
      for (let m = 0; m <= horizon; m++) {
        const totMonths = startYear * 12 + startMonth0 + m;
        const yr = Math.floor(totMonths / 12);
        const mo0 = totMonths % 12;
        const daysInMo = new Date(Date.UTC(yr, mo0 + 1, 0)).getUTCDate();
        const day = Math.min(startDay, daysInMo);
        const iso = `${yr}-${String(mo0 + 1).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
        const fullLbl = `${MONTH_NAMES_SHORT[mo0]} ${yr}`;
        const deflator = Math.pow(1.0 + infMonthlyRate, m);
        const rawTargetNet = (simAnnualBurn * deflator) / 12.0;
        const ssnGross = simIncludeSSN ? (computeMonthlySsnForIso(iso, m).totNomGross || 0.0) : 0.0;
        sched.push({ iso, fullLbl, deflator, rawTarget: rawTargetNet, ssnGross });
        _collectScheduledCheckpointMonths(m, iso, cpSet);
        if (m < msTargetMonth && ageOnIso(INV0.birth_year, INV0.birth_month, INV0.birth_day, iso, m) >= targetAge) msTargetMonth = m;
      }
      cpSet.add(horizon);
      const checkpoints = Array.from(cpSet).sort((a, b) => a - b);

      const rawSsVol = mc.singleStockVolPct ?? mc.googVolPct ?? mcSingleStockVolPct ?? mcGoogVolPct ?? 36.2;
      const rawMktVol = mc.broadEquityVolPct ?? mc.nonGoogVolPct ?? mcBroadEquityVolPct ?? mcNonGoogVolPct ?? 13.9;
      const ssSd = Math.max(0.0, Number(rawSsVol) / 100.0);
      const mktSd = Math.max(0.0, Number(rawMktVol) / 100.0);
      const zeroVolMode = (ssSd === 0.0 && mktSd === 0.0 && mc.fixedIncomeVolPct == null);
      const fiSdDefault = zeroVolMode ? 0.0 : 4.5;
      const fiSd = Math.max(0.0, Number(mc.fixedIncomeVolPct ?? fiSdDefault) / 100.0);
      const rawCorr = mc.singleStockCorr ?? mc.googRestCorr ?? mcSingleStockCorr ?? mcGoogRestCorr ?? 0.52;
      const corr = Math.max(-0.999, Math.min(0.999, Number(rawCorr)));
      let model = String(mc.returnModel || mcReturnModel || "normal").toLowerCase();
      const nPaths = Math.max(1, parseInt(mc.simulations != null ? mc.simulations : (mcSimulations || 500), 10) || 500);

      const overrideSsPct = mc.googExpectedPct ?? mc.singleStockExpectedPct;
      const overrideMktPct = mc.nonGoogExpectedPct ?? mc.broadEquityExpectedPct;
      const forcePresetVols = Boolean(mc.useClassPresetVols);

      const defaultRhoMap = {
        cash_fixed: 0.0,
        fixed_income: 0.20,
        hybrid_buffered: 0.75,
        broad_equity: 1.00,
        real_diversifier: 0.05,
        single_stock: 0.55,
        crypto_speculative: 0.45,
      };
      const defaultBetaMap = {
        broad_equity: 1.00,
        hybrid_buffered: 0.55,
        fixed_income: 0.30,
        real_diversifier: 0.05,
      };
      const defaultVolMap = {
        cash_fixed: 0.0,
        fixed_income: 0.045,
        hybrid_buffered: 0.085,
        broad_equity: 0.140,
        real_diversifier: 0.150,
        single_stock: 0.360,
        crypto_speculative: 0.680,
      };

      const adjustYear = buildYearRateAdjuster(mc);
      const assetSpecs = SIM_ASSETS.map(a => {
        const fc = resolveSimAssetForecast(a);
        const rc = fc.risk_class;
        let gp = fc.growth_pct;
        if (overrideSsPct != null && rc === "single_stock") gp = Number(overrideSsPct);
        else if (overrideMktPct != null && rc === "broad_equity" && a.rate_model !== "fixed") gp = Number(overrideMktPct);
        const meanDec = gp / 100.0;
        const mLog = meanDec > -0.999 ? Math.log(1.0 + meanDec) : -6.9;
        const erDec = (fc.expense_ratio_pct || 0.0) / 100.0;

        const rvolRaw = a.realized_vol_pct;
        const customVol = (fc.vol_pct != null && Number.isFinite(Number(fc.vol_pct))) ? Number(fc.vol_pct) : null;
        let sdA = 0.0, rhoA = 0.0;
        if (rc === "cash_fixed" || zeroVolMode) {
          sdA = 0.0; rhoA = 0.0;
        } else if (customVol != null) {
          sdA = Math.max(0.0, customVol / 100.0);
          const cRaw = a.corr_spy;
          rhoA = cRaw != null ? Number(cRaw) : (defaultRhoMap[rc] ?? 0.55);
        } else if (rvolRaw != null && Number(rvolRaw) > 0.0 && !forcePresetVols && !fc.risk_overridden && a.rate_model !== "fixed") {
          sdA = Math.max(0.0, Number(rvolRaw) / 100.0);
          const cRaw = a.corr_spy;
          rhoA = cRaw != null ? Number(cRaw) : (defaultRhoMap[rc] ?? 0.55);
        } else if (rc === "single_stock") {
          // DA2-08 PARITY INVARIANT (lockstep with `run_monte_carlo` in `simulator.py`):
          // Always use `ssSd` and `corr` (never gated on `mc.singleStockVolPct != null`).
          sdA = ssSd;
          rhoA = corr;
        } else if (rc === "broad_equity") {
          sdA = mktSd;
          rhoA = 1.00;
        } else if (rc === "fixed_income" && mc.fixedIncomeVolPct != null) {
          sdA = fiSd;
          rhoA = defaultRhoMap[rc] ?? 0.20;
        } else {
          sdA = defaultVolMap[rc] ?? 0.140;
          rhoA = defaultRhoMap[rc] ?? 0.55;
        }
        rhoA = Math.max(-0.999, Math.min(0.999, Number(rhoA)));

        const isDetNormal = (rc === "cash_fixed") || (sdA <= 0.0);
        const cMkt = sdA * rhoA;
        const cOrth = sdA * Math.sqrt(Math.max(0.0, 1.0 - rhoA * rhoA));

        let histKind = "mkt", histMult = 1.0;
        if (rc === "cash_fixed") {
          histKind = "zero"; histMult = 0.0;
        } else if (rc === "single_stock") {
          histKind = "ss";
          histMult = (sdA > 1e-6) ? (sdA / 0.36) : 1.0;
        } else if (rc === "crypto_speculative") {
          histKind = "ss"; histMult = 1.65;
        } else {
          histKind = "mkt";
          histMult = (a.beta_spy != null) ? Number(a.beta_spy) : (defaultBetaMap[rc] ?? 1.0);
        }

        // Group buckets that hold the same underlying ticker / primary equity so they share one
        // idiosyncratic `eps` draw per year (matching simulator.py:run_monte_carlo 1:1).
        const rawSym = String(a.symbol || "").trim().toUpperCase();
        const pfk = Array.isArray(a.parent_forecast_keys) ? a.parent_forecast_keys : [];
        let shockGroup;
        if (rawSym && !rawSym.startsWith("#") && !rawSym.includes(" ")) {
          shockGroup = `SYM:${rawSym}`;
        } else if (pfk.length > 0 && String(pfk[0] || "").trim()) {
          shockGroup = `SYM:${String(pfk[0]).trim().toUpperCase()}`;
        } else if (a.bucket === "goog" || a.bucket === "primary_equity") {
          shockGroup = PRIMARY_SYMBOL ? `SYM:${PRIMARY_SYMBOL}` : "BUCKET:PRIMARY_EQUITY";
        } else {
          shockGroup = `ID:${a.id}`;
        }

        return { id: a.id, rc, meanDec, mLog, erDec, isDetNormal, cMkt, cOrth, histKind, histMult, shockGroup };
      });

      const block = 52;
      const histPairs = Array.isArray(mc.histPairs) ? mc.histPairs : [];
      if (model === "historical" && histPairs.length <= block) model = "normal";
      let histMuLogG = 0.0, histMuLogB = 0.0;
      if (model === "historical") {
        const nhp = histPairs.length;
        // A middle week belongs to more available blocks than an endpoint week.
        const windows = nhp - block + 1;
        for (let i = 0; i < nhp; i++) {
          const weight = Math.min(i + 1, block, windows, nhp - i) / windows;
          histMuLogG += weight * Math.log1p(histPairs[i][0]);
          histMuLogB += weight * Math.log1p(histPairs[i][1]);
        }
      }

      const effSeed = (seed == null ? SIM_MC_SEED : seed) >>> 0;
      const rng = mcSeededRng(effSeed);
      const nYears = Math.ceil(horizon / 12);
      const chol = Math.sqrt(Math.max(0.0, 1.0 - corr * corr));

      function toMonthly(annual) {
        const base = 1.0 + annual;
        if (base <= 0.0) return -0.9999;
        return Math.pow(base, 1.0 / 12.0) - 1.0;
      }

      const cpNominal = {};
      for (const cp of checkpoints) cpNominal[cp] = [];
      const msRealVals = [];
      const msTaxRealVals = [];
      const msEffTaxVals = [];
      const endingReal = [];
      const depleteMonths = [];
      const endDeflator = sched[horizon].deflator;
      const msDeflator = sched[msTargetMonth].deflator;

      const useBearShield = Boolean(simBearShield);
      const bearOrder = useBearShield ? computeBearShieldOrder(simAssetOrder) : simAssetOrder;
      const allPathInputs = new Array(nPaths);
      let pCursor = 0;

      function stepSlice(maxMs, minPaths) {
        const t0 = _simNowMs();
        const minN = Math.max(1, Number(minPaths) || 1);
        let ran = 0;
        while (pCursor < nPaths) {
          const p = pCursor;
          const yearlyRates = new Array(nYears);
          const yearlyBear = new Array(nYears);
          for (let y = 0; y < nYears; y++) {
            let fSs = 0.0, fMkt = 0.0, zMkt = 0.0;
            if (model === "historical") {
              const start = rng.int(histPairs.length - block + 1);
              let lg = 0.0, lb = 0.0;
              for (let k = start; k < start + block; k++) {
                lg += Math.log(1.0 + histPairs[k][0]);
                lb += Math.log(1.0 + histPairs[k][1]);
              }
              fSs = lg - histMuLogG;
              fMkt = lb - histMuLogB;
              yearlyBear[y] = useBearShield && ((Math.exp(0.08 + fMkt) - 1.0) < -0.05 || (Math.exp(0.08 + fSs) - 1.0) < -0.10);
            } else {
              zMkt = rng.gauss();
              yearlyBear[y] = useBearShield && !zeroVolMode && (zMkt < -0.50);
            }

            const rmap = {};
            const grpEps = {};
            for (let si = 0; si < assetSpecs.length; si++) {
              const sp = assetSpecs[si];
              let netAnn;
              if (model === "historical") {
                if (sp.histKind === "zero") netAnn = sp.meanDec - sp.erDec;
                else if (sp.histKind === "ss") netAnn = (Math.exp(sp.mLog + sp.histMult * fSs) - 1.0) - sp.erDec;
                else netAnn = (Math.exp(sp.mLog + sp.histMult * fMkt) - 1.0) - sp.erDec;
              } else {
                if (sp.isDetNormal) {
                  netAnn = sp.meanDec - sp.erDec;
                } else {
                  if (!(sp.shockGroup in grpEps)) {
                    grpEps[sp.shockGroup] = rng.gauss();
                  }
                  const eps = grpEps[sp.shockGroup];
                  const delta = sp.cMkt * zMkt + sp.cOrth * eps;
                  netAnn = (Math.exp(sp.mLog + delta) - 1.0) - sp.erDec;
                }
              }
              rmap[sp.id] = toMonthly(netAnn);
            }
            yearlyRates[y] = adjustYear(rmap, y);
          }
          allPathInputs[p] = { yearlyRates, yearlyBear };
          const stateMap = cloneSimAssetState();
          let firstDeplete = horizon + 1;
          let cpI = 0;
          let lastTotal = 0.0;
          let pathMsGrossReal = 0.0;
          let pathMsTaxReal = 0.0;

          for (let m = 0; m <= horizon; m++) {
            const sc = sched[m];
            if (m > 0) {
              const yrI = Math.floor((m - 1) / 12);
              const ratesM = yearlyRates[yrI];
              const ordM = yearlyBear[yrI - 1] ? bearOrder : null;
              const stepRes = stepSimMonth(stateMap, sc.iso, sc.fullLbl, 0.0, 0.0, sc.rawTarget, false, ordM, ratesM, sc.deflator, sc.ssnGross);
              const tgt = stepRes.targetMonthlyBurn;
              if (firstDeplete > horizon && tgt > 1.0 && stepRes.monthlyBurn < tgt - 1.0) firstDeplete = m;
              if (m <= msTargetMonth) {
                pathMsGrossReal += stepRes.monthlyGrossSold / sc.deflator;
                pathMsTaxReal += stepRes.monthlyTax / sc.deflator;
              }
            }
            const needCp = cpI < checkpoints.length && m === checkpoints[cpI];
            const needMs = m === msTargetMonth;
            if (needCp || needMs) {
              let totalVal = 0.0;
              for (let i = 0; i < SIM_ASSETS.length; i++) {
                const a = SIM_ASSETS[i];
                if (a.is_unvested_goog && !simIncludeUnvested) continue;
                const st = stateMap[a.id];
                if (!st) continue;
                if (a.bucket === "goog" && a.is_unvested_goog && st.tranches) {
                  for (const t of st.tranches) { if (t.vested) totalVal += t.val; }
                } else {
                  totalVal += st.val;
                }
              }
              if (needCp) {
                cpNominal[checkpoints[cpI]].push(totalVal);
                lastTotal = totalVal;
                cpI++;
              }
              if (needMs) {
                msRealVals.push(totalVal / msDeflator);
              }
            }
          }
          endingReal.push(lastTotal / endDeflator);
          depleteMonths.push(firstDeplete);
          msTaxRealVals.push(pathMsTaxReal);
          msEffTaxVals.push(pathMsGrossReal > 0 ? (pathMsTaxReal / pathMsGrossReal * 100.0) : 0.0);
          pCursor++;
          ran++;
          if (ran >= minN && (_simNowMs() - t0) >= maxMs) break;
        }
        return pCursor >= nPaths;
      }

      function buildResult(includeRepBars) {
        const doneCount = pCursor;
        const qs = [0.10, 0.25, 0.50, 0.75, 0.90];
        const keyOf = { 0.10: "p10", 0.25: "p25", 0.50: "p50", 0.75: "p75", 0.90: "p90" };
        const band = [];
        for (const cp of checkpoints) {
          const vals = cpNominal[cp].slice().sort((x, y) => x - y);
          const dfl = sched[cp].deflator;
          const nom = {}, real = {};
          for (const q of qs) { const pv = mcPercentile(vals, q); nom[keyOf[q]] = Math.round(pv * 100) / 100; real[keyOf[q]] = Math.round((pv / dfl) * 100) / 100; }
          band.push({ monthIdx: cp, yearsOut: Math.round((cp / 12.0) * 10) / 10, iso: sched[cp].iso, nominal: nom, real });
        }
        function successThrough(monthIdx) {
          if (!depleteMonths.length) return 0.0;
          let ok = 0;
          for (const d of depleteMonths) if (d > monthIdx) ok++;
          return Math.round((1000.0 * ok) / depleteMonths.length) / 10;
        }
        const threshold = Math.max(0.0, Number(mc.successThresholdUsd || 0) || 0);
        let thrOk = 0;
        for (let i = 0; i < endingReal.length; i++) if (depleteMonths[i] > horizon && endingReal[i] >= threshold) thrOk++;
        const successThresholdPct = endingReal.length ? Math.round((1000.0 * thrOk) / endingReal.length) / 10 : 0.0;

        const msSorted = msRealVals.slice().sort((x, y) => x - y);
        const msTaxSorted = msTaxRealVals.slice().sort((x, y) => x - y);
        const msEffSorted = msEffTaxVals.slice().sort((x, y) => x - y);
        const p50EffTax = msEffSorted.length ? Math.round(mcPercentile(msEffSorted, 0.50) * 10) / 10 : 0.0;
        const p50TaxReal = msTaxSorted.length ? Math.round(mcPercentile(msTaxSorted, 0.50) * 100) / 100 : 0.0;

        const endSorted = endingReal.slice().sort((x, y) => x - y);
        let hi = mcPercentile(endSorted, 0.99);
        if (hi <= 0.0) hi = endSorted.length ? endSorted[endSorted.length - 1] : 1.0;
        const nb = 20;
        const width = hi > 0 ? hi / nb : 1.0;
        const bins = new Array(nb).fill(0);
        for (const v of endSorted) { let bi = width > 0 ? Math.floor(v / width) : 0; bins[Math.min(nb - 1, Math.max(0, bi))]++; }
        const histogram = [];
        for (let i = 0; i < nb; i++) histogram.push({ lo: Math.round(i * width * 100) / 100, hi: Math.round((i + 1) * width * 100) / 100, count: bins[i] });

        const msRealObj = { p10: 0, p25: 0, p50: 0, p75: 0, p90: 0 };
        if (msSorted.length) {
          for (const q of qs) msRealObj[keyOf[q]] = Math.round(mcPercentile(msSorted, q) * 100) / 100;
        }

        const depSorted = depleteMonths.slice().sort((x, y) => x - y);
        const medDepMo = depSorted.length ? mcPercentile(depSorted, 0.50) : (horizon + 1);
        const medYearsSolvent = medDepMo > horizon ? Math.round((horizon / 12.0) * 10) / 10 : Math.round((medDepMo / 12.0) * 10) / 10;

        // DA-A05: Replay the single representative P50 path so hover popup sales & holdings reconcile with P50
        let repBars = null;
        if (includeRepBars && doneCount > 0) {
          repBars = new Array(horizon + 1);
          let bestP = 0;
          let bestDist = Infinity;
          for (let p = 0; p < msRealVals.length; p++) {
            const dist = Math.abs(msRealVals[p] - msRealObj.p50);
            if (dist < bestDist) { bestDist = dist; bestP = p; }
          }
          const repIn = allPathInputs[bestP];
          if (repIn) {
            const repState = cloneSimAssetState();
            const snap0 = summarizeSimStateBuckets(repState);
            repBars[0] = { totalVal: snap0.totalVal, assetVals: snap0.assetVals, sales: [], deflator: 1.0 };
            for (let m = 1; m <= horizon; m++) {
              const sc = sched[m];
              const yrI = Math.floor((m - 1) / 12);
              const sres = stepSimMonth(
                repState, sc.iso, sc.fullLbl, 0.0, 0.0, sc.rawTarget, false,
                repIn.yearlyBear[yrI - 1] ? bearOrder : null,
                repIn.yearlyRates[yrI], sc.deflator, sc.ssnGross
              );
              const snapM = summarizeSimStateBuckets(repState);
              repBars[m] = {
                totalVal: snapM.totalVal,
                assetVals: snapM.assetVals,
                sales: sres.sales || [],
                schedActionEvents: sres.schedActionEvents || [],
                schedWithdrawEvents: sres.schedWithdrawEvents || [],
                deflator: sc.deflator,
                rawMonthlyExpense: sc.rawTarget,
                ssnNetMo: sres.ssnNetMo || 0,
                bearShieldActive: Boolean(repIn.yearlyBear[yrI - 1]),
                monthlyWorkContrib: sres.monthlyWorkContrib || 0,
                monthlyPaycheckOffset: sres.monthlyPaycheckOffset || 0,
              };
            }
          }
        }

        const out = {
          model, n_paths: doneCount, seed: effSeed,
          horizon_months: horizon,
          median_years_solvent: medYearsSolvent,
          success_to_ms_pct: successThrough(msTargetMonth),
          success_to_horizon_pct: successThrough(horizon),
          success_threshold_pct: successThresholdPct,
          success_threshold_usd: Math.round(threshold * 100) / 100,
          ms_month: msTargetMonth,
          ms_real: msRealObj,
          tax_to_ms: {
            net_kept_pct: Math.round((100.0 - p50EffTax) * 10) / 10,
            eff_tax_pct: p50EffTax,
            cum_tax_real: Math.round(p50TaxReal),
          },
          band,
          ending_real: {
            p10: Math.round(mcPercentile(endSorted, 0.10) * 100) / 100,
            p25: Math.round(mcPercentile(endSorted, 0.25) * 100) / 100,
            p50: Math.round(mcPercentile(endSorted, 0.50) * 100) / 100,
            p75: Math.round(mcPercentile(endSorted, 0.75) * 100) / 100,
            p90: Math.round(mcPercentile(endSorted, 0.90) * 100) / 100,
            min: endSorted.length ? Math.round(endSorted[0] * 100) / 100 : 0.0,
            max: endSorted.length ? Math.round(endSorted[endSorted.length - 1] * 100) / 100 : 0.0,
          },
          ending_histogram: histogram,
          _repBars: repBars || new Array(horizon + 1),
        };
        if (doneCount < nPaths) {
          out.is_partial = true;
          out.total_paths = nPaths;
        }
        return out;
      }

      return { totalPaths: nPaths, completedPaths: () => pCursor, stepSlice, buildResult };
    }

    function runMonteCarlo(mc, seed) {
      const stepper = createMonteCarloStepper(mc, seed);
      stepper.stepSlice(Infinity, stepper.totalPaths);
      return stepper.buildResult(true);
    }

    // === DA-C05 (Topic B): 3-Tier Multi-Asset Historical Cohort Cascade ===
    function buildCohortAssetMonthlyRates(allYears, ydata, centerMode) {
      const nAll = allYears.length;
      const spTotMap = {};
      let sumSpTot = 0.0;
      for (const y of allYears) {
        const spTot = Number(ydata[y].sp_total_ret_pct ?? ydata[y].sp_ret_pct) / 100.0;
        spTotMap[y] = spTot;
        sumSpTot += spTot;
      }
      const meanSpTot = sumSpTot / nAll;
      let varSp = 0.0, sumLogSp = 0.0;
      for (const y of allYears) {
        const d = spTotMap[y] - meanSpTot;
        varSp += d * d;
        sumLogSp += Math.log(Math.max(0.01, 1.0 + spTotMap[y]));
      }
      const sdSpTot = nAll > 1 ? Math.sqrt(varSp / Math.max(1, nAll - 1)) : 0.18;
      const meanLogSpTot = sumLogSp / nAll;

      const byYearOut = {};
      for (const y of allYears) byYearOut[y] = {};

      for (let i = 0; i < SIM_ASSETS.length; i++) {
        const a = SIM_ASSETS[i];
        const fc = resolveSimAssetForecast(a);
        const aid = a.id;
        const rc = fc.risk_class;
        const meanDec = fc.growth_pct / 100.0;
        const mLog = meanDec > -0.999 ? Math.log(1.0 + meanDec) : -6.9;
        const erDec = (fc.expense_ratio_pct || 0.0) / 100.0;

        const isContractualFixed = (
          a.rate_model === "fixed" || String(a.return_source || "") === "CONTRACT_CREDITED_RATE"
        );
        if (isContractualFixed) {
          const mRate = Math.pow(1.0 + Math.max(-0.9999, meanDec - erDec), 1.0 / 12.0) - 1.0;
          for (const y of allYears) byYearOut[y][aid] = mRate;
          continue;
        }

        const tier1Map = {};
        const hArr = Array.isArray(a.hist_annual_returns) ? a.hist_annual_returns : [];
        for (let hi = 0; hi < hArr.length; hi++) {
          const entry = hArr[hi];
          if (entry && entry.year != null && entry.ret_pct != null) {
            tier1Map[Number(entry.year)] = Number(entry.ret_pct) / 100.0;
          }
        }

        const acUp = String(a.asset_class || "").toUpperCase();
        const isRila = acUp.includes("RILA") || (String(aid).startsWith("ann_") && rc === "hybrid_buffered");
        const isFia = acUp.includes("INDEXED_ANNUITY") || (String(aid).startsWith("ann_") && rc === "fixed_income");

        const rawSeries = {};
        let sumAssetLog = 0.0;
        for (const y of allYears) {
          let rVal;
          if (y in tier1Map) {
            rVal = tier1Map[y];
          } else {
            const row = ydata[y];
            const spR = spTotMap[y];
            if (isRila) {
              rVal = spR >= 0.0 ? Math.min(spR, 0.16) : (spR >= -0.15 ? 0.0 : spR + 0.15);
            } else if (isFia) {
              rVal = Math.max(0.0, Math.min(spR, 0.095));
            } else {
              const ustR = Number(row.ust_10y_ret_pct ?? (0.25 * Number(row.sp_ret_pct))) / 100.0;
              const tbillR = Number(row.tbill_3m_pct ?? 3.0) / 100.0;
              const goldR = Number(row.gold_ret_pct ?? 0.0) / 100.0;
              if (rc === "cash_fixed") {
                rVal = tbillR;
              } else if (rc === "fixed_income") {
                rVal = a.tax_model === "MUNI" ? 0.85 * ustR : ustR;
              } else if (rc === "hybrid_buffered") {
                rVal = 0.60 * spR + 0.40 * ustR;
              } else if (rc === "real_diversifier") {
                rVal = goldR;
              } else if (rc === "broad_equity") {
                const beta = a.beta_spy != null ? Number(a.beta_spy) : 1.0;
                rVal = Math.max(-0.95, tbillR + beta * (spR - tbillR));
              } else {
                const defV = rc === "single_stock" ? 35.0 : 68.0;
                const rvolA = a.realized_vol_pct;
                const hv = (fc.vol_pct != null && Number.isFinite(Number(fc.vol_pct)))
                  ? Number(fc.vol_pct)
                  : ((rvolA != null && Number(rvolA) > 0.0 && !fc.risk_overridden) ? Number(rvolA) : defV);
                const kH = (hv / 100.0) / Math.max(0.08, sdSpTot);
                rVal = Math.exp(meanLogSpTot + kH * (Math.log(Math.max(0.01, 1.0 + spR)) - meanLogSpTot)) - 1.0;
              }
            }
          }
          rawSeries[y] = rVal;
          sumAssetLog += Math.log(Math.max(0.01, 1.0 + rVal));
        }

        const avgLog = sumAssetLog / nAll;
        for (const y of allYears) {
          let netAnn;
          if (centerMode === "anchor") {
            const innY = Math.log(Math.max(0.01, 1.0 + rawSeries[y])) - avgLog;
            netAnn = (Math.exp(mLog + innY) - 1.0) - erDec;
          } else {
            netAnn = rawSeries[y] - erDec;
          }
          byYearOut[y][aid] = Math.pow(1.0 + Math.max(-0.9999, netAnn), 1.0 / 12.0) - 1.0;
        }
      }

      return byYearOut;
    }

    // === CodeReview A3: Historical Cohort engine (mirror of simulator.run_historical_cohorts) =====
    function createHistoricalCohortStepper(opts) {
      opts = opts || {};
      const cohorts = window.COHORT_DATA || {};
      const rows = cohorts.years || [];
      if (!rows.length) return { available: false, totalCohorts: 0, completedCohorts: () => 0, stepSlice: () => true, buildResult: () => ({ available: false, cohorts: [], band: [], named: [] }) };

      const ydata = {};
      rows.forEach(r => { ydata[r.year] = r; });
      const allYears = rows.map(r => r.year).sort((a, b) => a - b);
      const firstY = allYears[0], lastY = allYears[allYears.length - 1];

      const horizon = Math.round(opts.horizonMonths != null ? opts.horizonMonths : SIM_TOTAL_MONTHS);
      const inflationMode = String(opts.inflationMode || cohortInflationMode || "real_cpi").toLowerCase();
      const centerMode = String(opts.centerMode || cohortCenterMode || "anchor").toLowerCase();
      const spotlight = new Set((opts.spotlight || []).map(Number));
      const flatInfMonthly = Math.pow(1.0 + simInflationPct / 100.0, 1.0 / 12.0) - 1.0;

      const startYear = parseInt(TODAY_ISO.slice(0, 4), 10) || 2026;
      const startMonth0 = (parseInt(TODAY_ISO.slice(5, 7), 10) || 11) - 1;
      const startDay = parseInt(TODAY_ISO.slice(8, 10), 10) || 10;

      const ssRetPct = r => Number(r.single_stock_ret_pct ?? r.goog_ret_pct ?? 0);
      const adjustYear = centerMode === "anchor" ? buildYearRateAdjuster() : rates => rates;
      const byYearAssetRates = buildCohortAssetMonthlyRates(allYears, ydata, centerMode);

      const skel = [];
      const cpSet = new Set();
      let msTargetMonth = horizon;
      const targetAge = Number(INV0.milestone_age) || 85;
      for (let m = 0; m <= horizon; m++) {
        const totM = startYear * 12 + startMonth0 + m;
        const yr = Math.floor(totM / 12), mo0 = totM % 12;
        const daysInMo = new Date(Date.UTC(yr, mo0 + 1, 0)).getUTCDate();
        const iso = `${yr}-${String(mo0 + 1).padStart(2, "0")}-${String(Math.min(startDay, daysInMo)).padStart(2, "0")}`;
        const fullLbl = `${MONTH_NAMES_SHORT[mo0]} ${yr}`;
        const ssnGrossFlat = simIncludeSSN ? (computeMonthlySsnForIso(iso, m).totNomGross || 0.0) : 0.0;
        const ssnGrossRealBase = simIncludeSSN
          ? (computeMonthlySsnForIso(iso, m, ssnInv0ClaimAge, ssnInv1ClaimAge, 0.0).totNomGross || 0.0)
          : 0.0;
        skel.push({ iso, fullLbl, ssnGrossFlat, ssnGrossRealBase });
        _collectScheduledCheckpointMonths(m, iso, cpSet);
        if (m < msTargetMonth && ageOnIso(INV0.birth_year, INV0.birth_month, INV0.birth_day, iso, m) >= targetAge) msTargetMonth = m;
      }
      cpSet.add(horizon);
      const checkpoints = Array.from(cpSet).sort((a, b) => a - b);

      const defFrom = (cohortStartFrom != null && cohortStartFrom >= 0) ? cohortStartFrom : firstY;
      const yFrom = Math.max(firstY, Math.round(opts.startYearFrom != null ? opts.startYearFrom : defFrom));
      const yTo = Math.min(lastY, Math.round(opts.startYearTo != null ? opts.startYearTo : lastY));

      const validYears = [];
      for (let Y = yFrom; Y <= yTo; Y++) {
        const availYears = lastY - Y + 1;
        const runMonths = Math.min(horizon, availYears * 12);
        if (runMonths >= 12) validYears.push(Y);
      }

      // Multi-pass strided order across [yFrom..yTo] so the very first <100ms slice samples the whole century evenly
      const evalOrder = [];
      const queuedY = new Set();
      const pushY = y => {
        if (!queuedY.has(y)) { queuedY.add(y); evalOrder.push(y); }
      };
      if (validYears.length > 0) {
        [validYears[0], 1929, 1937, 1966, 1973, 1982, 2000, 2008, validYears[validYears.length - 1]].forEach(y => {
          if (y >= yFrom && y <= yTo && validYears.includes(y)) pushY(y);
        });
        [8, 4, 2, 1].forEach(stride => {
          for (let i = 0; i < validYears.length; i += stride) pushY(validYears[i]);
        });
      }

      const useBearShield = Boolean(simBearShield);
      const bearOrder = useBearShield ? computeBearShieldOrder(simAssetOrder) : simAssetOrder;
      const recordsByYear = {};
      let cCursor = 0;

      function stepSlice(maxMs, minCohorts) {
        const t0 = _simNowMs();
        const minN = Math.max(1, Number(minCohorts) || 1);
        let ran = 0;
        while (cCursor < evalOrder.length) {
          const Y = evalOrder[cCursor];
          const availYears = lastY - Y + 1;
          const runMonths = Math.min(horizon, availYears * 12);

          const stateMap = cloneSimAssetState();
          let deflator = 1.0, depletedMonth = null;
          const line = [];
          const cpNomByM = {}, cpRealByM = {};
          let hasMs = false, msRealY = 0.0, msNomY = 0.0, msTaxRealY = 0.0, msTaxNomY = 0.0, msEffTaxY = 0.0, msEffTaxNomY = 0.0;
          let cyRates = {}, cyInf = 0, cyBear = false, cyTbill = 3.5;
          let cohortMsGrossReal = 0.0, cohortMsTaxReal = 0.0, cohortMsGrossNom = 0.0, cohortMsTaxNom = 0.0;

          for (let m = 0; m <= runMonths; m++) {
            const sc = skel[m];
            if (m === 0 || (m - 1) % 12 === 0) {
              const histYear = Math.min(lastY, Y + (m === 0 ? 0 : Math.floor((m - 1) / 12)));
              const row = ydata[histYear] || ydata[lastY];
              const prior = histYear > Y && ydata[histYear - 1];
              cyBear = useBearShield && prior && (Number(prior.sp_total_ret_pct ?? prior.sp_ret_pct) < -5 || Number(prior.sp_ret_pct) < -5 || ssRetPct(prior) < -10);
              cyRates = adjustYear(byYearAssetRates[histYear] || byYearAssetRates[lastY], histYear - Y);
              cyTbill = Number(row.tbill_3m_pct ?? 3.5);
              cyInf = inflationMode === "real_cpi" ? Math.pow(1 + row.inflation_pct / 100, 1 / 12) - 1 : flatInfMonthly;
            }
            if (m > 0) {
              deflator *= 1 + cyInf;
              const rawTarget = (simAnnualBurn * deflator) / 12;
              const ssnGrossC = inflationMode === "real_cpi" ? (sc.ssnGrossRealBase * deflator) : sc.ssnGrossFlat;
              const stepRes = stepSimMonth(stateMap, sc.iso, sc.fullLbl, 0.0, 0.0, rawTarget, false, cyBear ? bearOrder : null, cyRates, deflator, ssnGrossC, cyTbill);
              const tgt = stepRes.targetMonthlyBurn;
              if (depletedMonth === null && tgt > 1 && stepRes.monthlyBurn < tgt - 1) depletedMonth = m;
              if (m <= msTargetMonth) {
                cohortMsGrossReal += stepRes.monthlyGrossSold / deflator;
                cohortMsTaxReal += stepRes.monthlyTax / deflator;
                cohortMsGrossNom += stepRes.monthlyGrossSold;
                cohortMsTaxNom += stepRes.monthlyTax;
              }
            }
            const isCp = cpSet.has(m);
            if (isCp || m === msTargetMonth) {
              let total = 0;
              for (let i = 0; i < SIM_ASSETS.length; i++) {
                const a = SIM_ASSETS[i];
                if (a.is_unvested_goog && !simIncludeUnvested) continue;
                const st = stateMap[a.id];
                if (!st) continue;
                if (a.bucket === "goog" && a.is_unvested_goog && st.tranches) { for (const t of st.tranches) if (t.vested) total += t.val; }
                else total += st.val;
              }
              if (isCp) {
                cpNomByM[m] = total;
                cpRealByM[m] = total / deflator;
                line.push({ monthIdx: m, nominal: Math.round(total * 100) / 100, real: Math.round((total / deflator) * 100) / 100 });
              }
              if (m === msTargetMonth) {
                hasMs = true;
                msNomY = total;
                msRealY = total / deflator;
                msTaxRealY = cohortMsTaxReal;
                msTaxNomY = cohortMsTaxNom;
                msEffTaxY = cohortMsGrossReal > 0 ? (cohortMsTaxReal / cohortMsGrossReal * 100.0) : 0.0;
                msEffTaxNomY = cohortMsGrossNom > 0 ? (cohortMsTaxNom / cohortMsGrossNom * 100.0) : 0.0;
              }
            }
          }

          const rec = {
            startYear: Y, runMonths, depletedMonth,
            endNominal: line.length ? line[line.length - 1].nominal : 0,
            endReal: line.length ? line[line.length - 1].real : 0,
            spotlight: spotlight.has(Y), line,
          };
          recordsByYear[Y] = {
            rec, cpNomByM, cpRealByM, hasMs, msRealY, msNomY, msTaxRealY, msTaxNomY, msEffTaxY, msEffTaxNomY,
            reachedH: runMonths >= horizon,
            reachedJ85: runMonths >= msTargetMonth,
            survivedH: depletedMonth === null,
            survivedJ85: depletedMonth === null || depletedMonth > msTargetMonth,
          };
          cCursor++;
          ran++;
          if (ran >= minN && (_simNowMs() - t0) >= maxMs) break;
        }
        return cCursor >= evalOrder.length;
      }

      function buildResult(includeRepBars) {
        const qs = [0.10, 0.25, 0.50, 0.75, 0.90];
        const qkey = { 0.10: "p10", 0.25: "p25", 0.50: "p50", 0.75: "p75", 0.90: "p90" };
        const cpNom = {}, cpReal = {};
        checkpoints.forEach(cp => { cpNom[cp] = []; cpReal[cp] = []; });
        const msRealVals = [], msNomVals = [], msRealYears = [], msTaxRealVals = [], msTaxNomVals = [], msEffTaxVals = [], msEffTaxNomVals = [];
        const cohortOut = [], namedOut = [];
        let fullCohorts = 0, fullSuccess = 0, j85Cohorts = 0, j85Success = 0;

        for (let i = 0; i < validYears.length; i++) {
          const Y = validYears[i];
          const item = recordsByYear[Y];
          if (!item) continue;
          cohortOut.push(item.rec);
          if (item.rec.spotlight) namedOut.push(item.rec);
          if (item.reachedH) { fullCohorts++; if (item.survivedH) fullSuccess++; }
          if (item.reachedJ85) { j85Cohorts++; if (item.survivedJ85) j85Success++; }
          for (let ci = 0; ci < checkpoints.length; ci++) {
            const cp = checkpoints[ci];
            if (item.cpNomByM[cp] !== undefined) {
              cpNom[cp].push(item.cpNomByM[cp]);
              cpReal[cp].push(item.cpRealByM[cp]);
            }
          }
          if (item.hasMs) {
            msRealVals.push(item.msRealY);
            msNomVals.push(item.msNomY);
            msRealYears.push(Y);
            msTaxRealVals.push(item.msTaxRealY);
            msTaxNomVals.push(item.msTaxNomY);
            msEffTaxVals.push(item.msEffTaxY);
            msEffTaxNomVals.push(item.msEffTaxNomY);
          }
        }

        const band = [];
        checkpoints.forEach(cp => {
          const nv = cpNom[cp].slice().sort((a, b) => a - b), rv = cpReal[cp].slice().sort((a, b) => a - b);
          if (!nv.length) return;
          const nom = {}, real = {};
          qs.forEach(q => { nom[qkey[q]] = Math.round(mcPercentile(nv, q) * 100) / 100; real[qkey[q]] = Math.round(mcPercentile(rv, q) * 100) / 100; });
          band.push({ monthIdx: cp, yearsOut: Math.round((cp / 12) * 10) / 10, nCohorts: nv.length, nominal: nom, real });
        });

        const ranked = cohortOut.filter(c => c.runMonths >= horizon);
        const pool = ranked.length ? ranked : cohortOut;
        let best = null, worst = null;
        pool.forEach(c => { if (!best || c.endReal > best.endReal) best = c; if (!worst || c.endReal < worst.endReal) worst = c; });

        const cohDepVals = pool.map(c => c.depletedMonth != null ? c.depletedMonth : (horizon + 1)).sort((a, b) => a - b);
        const cohMedDep = cohDepVals.length ? mcPercentile(cohDepVals, 0.50) : (horizon + 1);
        const cohMedYearsSolvent = cohMedDep > horizon ? Math.round((horizon / 12.0) * 10) / 10 : Math.round((cohMedDep / 12.0) * 10) / 10;

        const msSorted = msRealVals.slice().sort((a, b) => a - b);
        const msNomSorted = msNomVals.slice().sort((a, b) => a - b);
        const msTaxSorted = msTaxRealVals.slice().sort((a, b) => a - b);
        const msTaxNomSorted = msTaxNomVals.slice().sort((a, b) => a - b);
        const msEffSorted = msEffTaxVals.slice().sort((a, b) => a - b);
        const msEffNomSorted = msEffTaxNomVals.slice().sort((a, b) => a - b);
        const p50EffTax = msEffSorted.length ? Math.round(mcPercentile(msEffSorted, 0.50) * 10) / 10 : 0.0;
        const p50EffTaxNom = msEffNomSorted.length ? Math.round(mcPercentile(msEffNomSorted, 0.50) * 10) / 10 : 0.0;
        const p50TaxReal = msTaxSorted.length ? Math.round(mcPercentile(msTaxSorted, 0.50) * 100) / 100 : 0.0;
        const p50TaxNom = msTaxNomSorted.length ? Math.round(mcPercentile(msTaxNomSorted, 0.50) * 100) / 100 : 0.0;
        const msRealObj = { p10: 0, p25: 0, p50: 0, p75: 0, p90: 0 };
        const msNomObj = { p10: 0, p25: 0, p50: 0, p75: 0, p90: 0 };
        if (msSorted.length) {
          qs.forEach(q => { msRealObj[qkey[q]] = Math.round(mcPercentile(msSorted, q) * 100) / 100; });
        }
        if (msNomSorted.length) {
          qs.forEach(q => { msNomObj[qkey[q]] = Math.round(mcPercentile(msNomSorted, q) * 100) / 100; });
        }

        // DA-A05: Replay the single representative median cohort so hover popup sales & holdings reconcile with P50
        let repBars = null;
        if (includeRepBars) {
          repBars = new Array(horizon + 1);
          let repY = pool.length ? pool[Math.floor(pool.length / 2)].startYear : null;
          if (msRealVals.length > 0) {
            let bestDist = Infinity;
            for (let k = 0; k < msRealVals.length; k++) {
              const recK = cohortOut.find(c => c.startYear === msRealYears[k]);
              const bonus = (recK && recK.runMonths >= horizon) ? 0.0 : 1e9;
              const dist = Math.abs(msRealVals[k] - msRealObj.p50) + bonus;
              if (dist < bestDist) { bestDist = dist; repY = msRealYears[k]; }
            }
          }
          if (repY != null) {
            const repState = cloneSimAssetState();
            const snap0 = summarizeSimStateBuckets(repState);
            repBars[0] = { totalVal: snap0.totalVal, assetVals: snap0.assetVals, sales: [], deflator: 1.0 };
            let repDfl = 1.0, repRates = {}, repInf = 0.0, repBear = false, repTbill = 3.5;
            const repRun = Math.min(horizon, (lastY - repY + 1) * 12);
            for (let m = 1; m <= repRun; m++) {
              const sc = skel[m];
              if ((m - 1) % 12 === 0) {
                const hy = Math.min(lastY, repY + Math.floor((m - 1) / 12));
                const row = ydata[hy] || ydata[lastY];
                const prior = hy > repY && ydata[hy - 1];
                repBear = useBearShield && prior && (Number(prior.sp_total_ret_pct ?? prior.sp_ret_pct) < -5 || Number(prior.sp_ret_pct) < -5 || ssRetPct(prior) < -10);
                repRates = adjustYear(byYearAssetRates[hy] || byYearAssetRates[lastY], hy - repY);
                repTbill = Number(row.tbill_3m_pct ?? 3.5);
                repInf = inflationMode === "real_cpi" ? Math.pow(1 + row.inflation_pct / 100, 1 / 12) - 1 : flatInfMonthly;
              }
              repDfl *= 1.0 + repInf;
              const rawTarget = (simAnnualBurn * repDfl) / 12.0;
              const ssnGrossC = inflationMode === "real_cpi" ? (sc.ssnGrossRealBase * repDfl) : sc.ssnGrossFlat;
              const sres = stepSimMonth(repState, sc.iso, sc.fullLbl, 0.0, 0.0, rawTarget, false, repBear ? bearOrder : null, repRates, repDfl, ssnGrossC, repTbill);
              const snapM = summarizeSimStateBuckets(repState);
              repBars[m] = {
                totalVal: snapM.totalVal,
                assetVals: snapM.assetVals,
                sales: sres.sales || [],
                schedActionEvents: sres.schedActionEvents || [],
                schedWithdrawEvents: sres.schedWithdrawEvents || [],
                deflator: repDfl,
                rawMonthlyExpense: rawTarget,
                ssnNetMo: sres.ssnNetMo || 0,
                bearShieldActive: Boolean(repBear),
                monthlyWorkContrib: sres.monthlyWorkContrib || 0,
                monthlyPaycheckOffset: sres.monthlyPaycheckOffset || 0,
              };
            }
          }
        }

        const out = {
          available: true, center_mode: centerMode, inflation_mode: inflationMode,
          horizon_months: horizon, median_years_solvent: cohMedYearsSolvent,
          ms_month: msTargetMonth, data_range: [firstY, lastY],
          start_year_from: yFrom, start_year_to: yTo, n_cohorts: cohortOut.length,
          success_to_ms_pct: j85Cohorts ? Math.round((1000 * j85Success) / j85Cohorts) / 10 : null,
          success_to_ms_n: [j85Success, j85Cohorts],
          success_full_pct: fullCohorts ? Math.round((1000 * fullSuccess) / fullCohorts) / 10 : null,
          success_full_n: [fullSuccess, fullCohorts],
          ms_real: msRealObj,
          ms_nominal: msNomObj,
          tax_to_ms: {
            net_kept_pct: Math.round((100.0 - p50EffTax) * 10) / 10,
            eff_tax_pct: p50EffTax,
            cum_tax_real: Math.round(p50TaxReal),
            net_kept_nominal_pct: Math.round((100.0 - p50EffTaxNom) * 10) / 10,
            eff_tax_nominal_pct: p50EffTaxNom,
            cum_tax_nominal: Math.round(p50TaxNom),
          },
          best_cohort: best ? { startYear: best.startYear, endReal: best.endReal } : null,
          worst_cohort: worst ? { startYear: worst.startYear, endReal: worst.endReal, depletedMonth: worst.depletedMonth } : null,
          band, cohorts: cohortOut, named: namedOut,
          _repBars: repBars || new Array(horizon + 1),
        };
        if (cCursor < evalOrder.length) {
          out.is_partial = true;
          out.total_cohorts = evalOrder.length;
        }
        return out;
      }

      return { available: true, totalCohorts: evalOrder.length, completedCohorts: () => cCursor, stepSlice, buildResult };
    }

    function runHistoricalCohorts(opts) {
      const stepper = createHistoricalCohortStepper(opts);
      if (!stepper.available) return { available: false, cohorts: [], band: [], named: [] };
      stepper.stepSlice(Infinity, stepper.totalCohorts);
      return stepper.buildResult(true);
    }

    // === CodeReview A3: Projection-model tab + control handlers ================================
    const SIM_MODEL_NARRATIVES = {
      range:
        "<b>Monte-Carlo</b> replaces the single projection line with a <b>range of possible futures</b>: it runs " +
        "<span id='sim-mc-sims-inline'>500</span> randomized market simulations and shades the middle 80% of outcomes " +
        "(10th to 90th percentile) around the median path based on each holding's historical ups and downs. " +
        "The <b>Return Model</b> below chooses how each year's market return is generated:" +
        "<br><br>" +
        "<b>Normal (Multi-Asset)</b> — draws randomized annual returns centered on each holding's " +
        "<b>◆ Proj. Growth setting</b>, reflecting how widely each asset typically fluctuates and how closely it moves with the S&amp;P 500 while allowing individual stocks to move on company-specific risks." +
        "<br><br>" +
        "<b>Historical (Bootstrap)</b> — shuffles real 12-month stretches of past market history adjusted to each " +
        "holding's <b>◆ Proj. Growth setting</b>, preserving real-world crashes, rebounds, and how your assets moved together." +
        "<br><br>" +
        `The gold-crested KPI cards above report the ${(INV0 && Number(INV0.milestone_age)) ? `Age-${Number(INV0.milestone_age)}` : "Milestone"} inheritance range (25th · 50th · 75th percentile), solvency rate, and ` +
        "median tax efficiency.",
      historical:
        "<b>Historical</b> replays your plan through <b>real history</b> &mdash; once for every start year from " +
        "<span id='sim-cohort-range-inline'>1929</span> through 2025 (cohorts starting after 1986 stop at 2025). Every cohort pairs actual historical CPI inflation with a 3-tier return cascade across your 7 holding risk classes: " +
        "(1) exact ticker or direct-indexing sleeve annual total returns where trading history exists, " +
        "(2) contractual MYGA/CD fixed rates and RILA/FIA cap-and-buffer payoffs, and " +
        "(3) 1929–2025 S&amp;P 500 total returns (including reinvested dividends), 10-Year US Treasuries, 3-Month T-Bills, and Gold.",
    };

    function _setPill(id, text, cls) {
      const el = document.getElementById(id);
      if (!el) return;
      const keepComputing = el.classList && el.classList.contains("sim-computing");
      el.textContent = text;
      if (cls === "mdl-in-pill") el.className = "mdl-in-pill mono" + (keepComputing ? " sim-computing" : "");
      else if (cls) el.className = "pill mono " + cls + (keepComputing ? " sim-computing" : "");
    }
    function _setRefActive(id, on) {
      const el = document.getElementById(id);
      if (el) el.classList.toggle("active", !!on);
    }

    const STRESS_PRESET_PROFILES = {
      gfc2008: {
        down_months: 18,
        broad_bounce_months: 36,
        stock_bounce_months: 54,
        shocks: {
          single_stock: -0.60,
          crypto_speculative: -0.72,
          broad_equity: -0.50,
          hybrid_buffered: -0.25,
          fixed_income: 0.05,
          real_diversifier: 0.05,
        },
      },
      dotcom2000: {
        down_months: 30,
        broad_bounce_months: 48,
        stock_bounce_months: STRESS_NO_BOUNCE_MONTHS,
        shocks: {
          single_stock: -0.75,
          crypto_speculative: -0.80,
          broad_equity: -0.45,
          hybrid_buffered: -0.20,
          fixed_income: 0.24,
          real_diversifier: 0.04,
        },
      },
      covid2020: {
        down_months: 3,
        broad_bounce_months: 6,
        stock_bounce_months: 6,
        shocks: {
          single_stock: -0.34,
          crypto_speculative: -0.48,
          broad_equity: -0.34,
          hybrid_buffered: -0.16,
          fixed_income: 0.03,
          real_diversifier: 0.08,
        },
      },
      rates2022: {
        down_months: 12,
        broad_bounce_months: 15,
        stock_bounce_months: 18,
        shocks: {
          single_stock: -0.38,
          crypto_speculative: -0.64,
          broad_equity: -0.24,
          hybrid_buffered: -0.16,
          fixed_income: -0.13,
          real_diversifier: 0.01,
        },
      },
    };

    function resolveStressSpec(scenArg = null) {
      const scen = String(scenArg != null ? scenArg : (simStressScenario || "none")).trim().toLowerCase();
      if (scen === "none") return null;
      let downM, broadB, stockB, shocks;
      if (scen === "custom") {
        const c = Math.max(-90, Math.min(0, Number(simCustomShockPct ?? -30))) / 100.0;
        downM = Math.max(3, Math.min(36, Math.round(Number(simCustomDownMonths ?? 12))));
        broadB = Math.max(3, Math.min(STRESS_NO_BOUNCE_MONTHS, Math.round(Number(simCustomBroadBounceMonths ?? 24))));
        stockB = Math.max(3, Math.min(STRESS_NO_BOUNCE_MONTHS, Math.round(Number(simCustomStockBounceMonths ?? 36))));
        shocks = {
          single_stock: c,
          crypto_speculative: Math.max(-0.95, 1.35 * c),
          broad_equity: 0.60 * c,
          hybrid_buffered: 0.32 * c,
          fixed_income: 0.10 * c,
          real_diversifier: -0.05 * c,
        };
      } else {
        const preset = STRESS_PRESET_PROFILES[scen];
        if (!preset) return null;
        downM = preset.down_months;
        broadB = preset.broad_bounce_months;
        stockB = preset.stock_bounce_months;
        shocks = { ...preset.shocks };
      }
      const hasDrop = (shocks.single_stock < -1e-6) || (shocks.broad_equity < -1e-6);
      const bearExtra = (broadB < STRESS_NO_BOUNCE_MONTHS && shocks.broad_equity < -1e-6) ? Math.floor(broadB / 2) : 0;
      const bearShieldM = hasDrop ? (downM + bearExtra) : 0;
      return {
        scenario: scen,
        down_months: downM,
        broad_bounce_months: broadB,
        stock_bounce_months: stockB,
        bear_shield_months: bearShieldM,
        shocks,
      };
    }

    // Peak-to-trough crash shock across the 7 risk classes when in `stress` mode (`cash_fixed` omitted so contractual rates stay unshocked).
    function stressYear1ShocksByRiskClass() {
      if (simActiveModel !== "stress" || simStressScenario === "none") return null;
      const spec = resolveStressSpec(simStressScenario);
      return spec ? { ...spec.shocks } : null;
    }

    function stressYear1Shocks() {
      const rc = stressYear1ShocksByRiskClass();
      return rc ? { g: rc.single_stock * 100.0, ng: rc.broad_equity * 100.0, fi: rc.fixed_income * 100.0 } : null;
    }

    function buildStressMonthResolver(baseRates, adjustYear, startMonth = 1) {
      if (simActiveModel !== "stress" || simStressScenario === "none") {
        return { profile: null, getRatesForMonth: null };
      }
      const prof = resolveStressSpec(simStressScenario);
      if (!prof) return { profile: null, getRatesForMonth: null };
      const downM = prof.down_months;
      const broadB = prof.broad_bounce_months;
      const stockB = prof.stock_bounce_months;
      const shocks = prof.shocks;
      const sM = Math.max(1, Math.round(Number(startMonth) || 1));

      const assetSpecs = [];
      let maxOverrideDur = downM;
      (SIM_ASSETS || []).forEach(a => {
        const aid = String(a.id);
        const fc = resolveSimAssetForecast(a);
        const rc = String(fc.risk_class);
        if (shocks[rc] == null) {
          assetSpecs.push({ aid, downNetMo: null, bEnd: 0, recNetMo: null });
          return;
        }
        const s = Number(shocks[rc]);
        const erDec = Math.max(0.0, Math.min(0.99, (Number(fc.expense_ratio_pct) || 0.0) / 100.0));
        const feeMo = Math.pow(1.0 - erDec, 1.0 / 12.0);
        const downGrossMo = Math.pow(Math.max(1e-6, 1.0 + s), 1.0 / downM) - 1.0;
        const downNetMo = Math.max(-0.9999, (1.0 + downGrossMo) * feeMo - 1.0);

        const rawB = (rc === "single_stock" || rc === "crypto_speculative") ? stockB : broadB;
        let bEnd = downM;
        let recNetMo = null;
        if (s < -1e-6 && rawB < STRESS_NO_BOUNCE_MONTHS) {
          bEnd = downM + rawB;
          const recGrossMo = Math.pow(1.0 / Math.max(1e-6, 1.0 + s), 1.0 / rawB) - 1.0;
          recNetMo = Math.max(-0.9999, (1.0 + recGrossMo) * feeMo - 1.0);
          if (bEnd > maxOverrideDur) maxOverrideDur = bEnd;
        }
        assetSpecs.push({ aid, downNetMo, bEnd, recNetMo });
      });

      const maxEndM = Math.min(SIM_TOTAL_MONTHS, sM + maxOverrideDur - 1);
      const monthlyMaps = new Map();
      for (let m = sM; m <= maxEndM; m++) {
        const relM = m - sM + 1;
        const normM = adjustYear(baseRates, Math.floor((m - 1) / 12));
        const rm = {};
        for (let i = 0; i < assetSpecs.length; i++) {
          const sp = assetSpecs[i];
          const baseR = normM[sp.aid];
          if (sp.downNetMo === null) rm[sp.aid] = baseR;
          else if (relM <= downM) rm[sp.aid] = sp.downNetMo;
          else if (sp.recNetMo !== null && relM <= sp.bEnd) rm[sp.aid] = Math.max(baseR, sp.recNetMo);
          else rm[sp.aid] = baseR;
        }
        monthlyMaps.set(m, rm);
      }

      const getRatesForMonth = m => {
        if (monthlyMaps.has(m)) return monthlyMaps.get(m);
        return adjustYear(baseRates, Math.max(0, Math.floor((m - 1) / 12)));
      };
      return { profile: prof, getRatesForMonth };
    }

    function selectSimSimulationNone() {
      const target = (simStressScenario && simStressScenario !== "none") ? "stress" : "single_path";
      setSimModel(target);
    }
    window.selectSimSimulationNone = selectSimSimulationNone;

    function setSimModel(model) {
      const valid = ["single_path", "stress", "range", "historical"];
      if (!valid.includes(model)) model = "single_path";
      simActiveModel = model;
      syncSimModelControls();
      const ctrlId = (model === "stress") ? "sim-tab-btn-single_path" : ("sim-tab-btn-" + model);
      setActiveSimComputingControl(ctrlId, true);
      recomputeSimBars();
      renderSimChartAndNav();
      updateSimSummaryDOM();
      checkSaveDirtyState();
    }

    // Pushes state -> DOM for the Simulation selector, Market Correction presets, and sub-row panels.
    function syncSimModelControls() {
      const isSimNone = (simActiveModel === "single_path" || simActiveModel === "stress");
      const btnNone = document.getElementById("sim-tab-btn-single_path");
      const btnRange = document.getElementById("sim-tab-btn-range");
      const btnHist = document.getElementById("sim-tab-btn-historical");
      if (btnNone) { btnNone.classList.toggle("active", isSimNone); btnNone.setAttribute("aria-selected", isSimNone ? "true" : "false"); }
      if (btnRange) { btnRange.classList.toggle("active", simActiveModel === "range"); btnRange.setAttribute("aria-selected", simActiveModel === "range" ? "true" : "false"); }
      if (btnHist) { btnHist.classList.toggle("active", simActiveModel === "historical"); btnHist.setAttribute("aria-selected", simActiveModel === "historical" ? "true" : "false"); }

      const pSingle = document.getElementById("sim-tabpanel-single_path");
      const pStress = document.getElementById("sim-tabpanel-stress");
      const pMc = document.getElementById("sim-tabpanel-mc");
      const pCohort = document.getElementById("sim-tabpanel-cohort");
      if (pSingle) pSingle.hidden = !isSimNone;
      if (pStress) pStress.hidden = false;
      if (pMc) pMc.hidden = simActiveModel !== "range";
      if (pCohort) pCohort.hidden = simActiveModel !== "historical";

      // Historical (cohort) controls
      const cd = window.COHORT_DATA || {};
      const firstY = cd.first_year || 1929, lastY = cd.last_year || 2025;
      document.getElementById("sim-cohort-center-anchor-btn") && document.getElementById("sim-cohort-center-anchor-btn").classList.toggle("active", cohortCenterMode === "anchor");
      document.getElementById("sim-cohort-center-raw-btn") && document.getElementById("sim-cohort-center-raw-btn").classList.toggle("active", cohortCenterMode === "raw");
      const centerSummaryEl = document.getElementById("sim-cohort-center-summary");
      if (centerSummaryEl) {
        centerSummaryEl.innerHTML = cohortCenterMode === "raw"
          ? "Replays literal historical returns minus fees (ignores <b>&#9670; Proj. Growth</b>, fade &amp; multiplier)."
          : "Re-centers historical sequences onto your <b>&#9670; Proj. Growth</b>, fade curve &amp; multiplier.";
      }
      const fromSl = document.getElementById("sim-cohort-from-slider");
      const effFrom = cohortStartFrom < 0 ? firstY : Math.max(firstY, Math.min(lastY - 1, cohortStartFrom));
      if (fromSl) { fromSl.min = String(firstY); fromSl.max = String(lastY - 10); fromSl.value = String(effFrom); }
      const fromMin = document.getElementById("sim-cohort-from-min"); if (fromMin) fromMin.textContent = String(firstY);
      const fromMax = document.getElementById("sim-cohort-from-max"); if (fromMax) fromMax.textContent = String(lastY - 10);
      _setPill("sim-cohort-from-pill", String(effFrom), "mdl-in-pill");
      _setRefActive("sim-cohort-from-ref-btn", cohortStartFrom < 0);
      const maxCohortStart = Math.max(firstY, lastY - 10);
      const cohortSpanCount = Math.max(1, maxCohortStart - firstY + 1);
      _setSimRefTip("sim-cohort-from-ref-btn", {
        title: `Reference Earliest Start Year (${firstY})`,
        meaning: "Starts the historical backtest from the oldest available market year so survival and median results include major crashes, depressions, and high-inflation decades.",
        calculation: `Begins at ${firstY}, the earliest year in the loaded ${firstY}–${lastY} U.S. market and inflation record, yielding ${cohortSpanCount} rolling retirement start years through ${maxCohortStart} (each tested over at least 10 years).`,
        clickHint: `Click to reset Earliest Start Year to ${firstY} (full history).`,
      });
      const cnarr = document.getElementById("sim-cohort-narrative");
      if (cnarr) cnarr.innerHTML = SIM_MODEL_NARRATIVES.historical || "";
      const crange = document.getElementById("sim-cohort-range-inline"); if (crange) crange.textContent = String(effFrom);

      // Market Correction (Stress) controls
      const effCorrection = simStressScenario || "none";
      document.querySelectorAll("[data-stress]").forEach(b => b.classList.toggle("active", b.dataset.stress === effCorrection));
      const slidersGrid = document.getElementById("sim-stress-sliders-grid");
      if (slidersGrid) {
        slidersGrid.style.opacity = effCorrection === "none" ? "0.55" : "1";
      }
      const activeSpec = effCorrection !== "none" ? resolveStressSpec(effCorrection) : null;
      const dispG = activeSpec ? Math.round(activeSpec.shocks.single_stock * 100) : 0;
      const dispNg = activeSpec ? Math.round(activeSpec.shocks.broad_equity * 100) : 0;
      const dispDownM = activeSpec ? activeSpec.down_months : 0;
      const dispBroadB = activeSpec ? activeSpec.broad_bounce_months : STRESS_NO_BOUNCE_MONTHS;
      const dispStockB = activeSpec ? activeSpec.stock_bounce_months : STRESS_NO_BOUNCE_MONTHS;

      _setPill("sim-stress-shock-pill", `${dispG}%`, "mdl-in-pill");
      const shockSl = document.getElementById("sim-stress-shock-slider");
      if (shockSl) shockSl.value = String(effCorrection === "none" ? 0 : dispG);
      const ngReadout = document.getElementById("sim-stress-ng-readout");
      if (ngReadout) {
        ngReadout.textContent = effCorrection !== "none"
          ? `Single stock (broad: ${dispNg}%)`
          : "Single stock (no shock)";
      }

      const downSl = document.getElementById("sim-stress-down-slider");
      if (downSl) downSl.value = String(effCorrection === "none" ? 3 : dispDownM);
      _setPill("sim-stress-down-pill", effCorrection === "none" ? "0 mo" : formatStressMonths(dispDownM, false), "mdl-in-pill");

      const bbSl = document.getElementById("sim-stress-broad-bounce-slider");
      if (bbSl) bbSl.value = String(effCorrection === "none" ? STRESS_NO_BOUNCE_MONTHS : dispBroadB);
      _setPill("sim-stress-broad-bounce-pill", effCorrection === "none" ? "—" : formatStressMonths(dispBroadB, true), "mdl-in-pill");

      const sbSl = document.getElementById("sim-stress-stock-bounce-slider");
      if (sbSl) sbSl.value = String(effCorrection === "none" ? STRESS_NO_BOUNCE_MONTHS : dispStockB);
      _setPill("sim-stress-stock-bounce-pill", effCorrection === "none" ? "—" : formatStressMonths(dispStockB, true), "mdl-in-pill");
      syncStressCapsuleTooltips();

      // Monte Carlo controls (Range)
      const normBtn = document.getElementById("sim-mc-model-normal-btn");
      const histBtn = document.getElementById("sim-mc-model-historical-btn");
      const wkAligned = Number(_mcRef && _mcRef.weeks_aligned) || 0;
      if (normBtn) normBtn.classList.toggle("active", mcReturnModel === "normal");
      if (histBtn) histBtn.classList.toggle("active", mcReturnModel === "historical");
      _setSimRefTip(normBtn, {
        title: "Normal Return Model (Multi-Asset Bell Curve)",
        meaning: "Simulates randomized market years based on how widely each holding typically fluctuates and how closely it moves with the S&P 500.",
        calculation: "Each simulated year generates a broad market move plus independent company-specific swings, keeping realistic market co-movement without forcing individual stocks to move in lockstep.",
        clickHint: "Click to use the Normal multi-asset return model.",
      });
      _setSimRefTip(histBtn, {
        title: "Historical Return Model (Real 1-Year Market Slices)",
        meaning: "Builds simulated futures by shuffling real 12-month stretches of past market history—naturally capturing real crashes, rebounds, and how assets behaved together.",
        calculation: wkAligned > 52
          ? `Each simulated year draws a random 52-week stretch from ${wkAligned.toLocaleString("en-US")} weeks of historical market data, adjusts its average onto each holding's Projected Growth rate minus fees, and compounds monthly.`
          : "Each simulated year draws a random 52-week stretch of historical market data, adjusts its average onto each holding's Projected Growth rate minus fees, and falls back to Normal if under a year of paired history is available.",
        clickHint: "Click to use the Historical 1-year slice model.",
      });

      const narr = document.getElementById("sim-mc-narrative");
      if (narr) narr.innerHTML = SIM_MODEL_NARRATIVES[simActiveModel] || "";
      const narrInline = document.getElementById("sim-mc-sims-inline");
      if (narrInline) narrInline.textContent = (mcSimulations || 500).toLocaleString("en-US");

      updateSimSummarySubtext();
    }

    function _setSimDrawerExpandedState(prefix, expanded, collapseTitle, expandTitle, openDisplay = "block") {
      const body = document.getElementById(`${prefix}-body`);
      const caret = document.getElementById(`${prefix}-caret`);
      const btn = document.getElementById(`${prefix}-toggle-btn`);
      if (body) body.style.display = expanded ? openDisplay : "none";
      if (caret) {
        caret.style.transform = expanded ? "rotate(90deg)" : "rotate(0deg)";
        caret.style.color = expanded ? "#38bdf8" : "#94a3b8";
      }
      if (btn) {
        btn.setAttribute("aria-expanded", expanded ? "true" : "false");
        btn.title = expanded ? collapseTitle : expandTitle;
      }
    }

    let simConfigExpanded = false;

    function toggleSimConfigDrawer() {
      setSimConfigDrawerExpanded(!simConfigExpanded);
    }

    function setSimConfigDrawerExpanded(expanded) {
      simConfigExpanded = Boolean(expanded);
      _setSimDrawerExpandedState(
        "sim-config",
        simConfigExpanded,
        "Click to collapse Simulation Controls",
        "Click expansion arrow to view and edit Simulation Controls",
        "flex"
      );
    }
    window.toggleSimConfigDrawer = toggleSimConfigDrawer;
    window.setSimConfigDrawerExpanded = setSimConfigDrawerExpanded;

    let simCorrectionExpanded = false;

    function toggleSimCorrectionDrawer() {
      setSimCorrectionDrawerExpanded(!simCorrectionExpanded);
    }

    function setSimCorrectionDrawerExpanded(expanded) {
      simCorrectionExpanded = Boolean(expanded);
      _setSimDrawerExpandedState(
        "sim-correction",
        simCorrectionExpanded,
        "Click to collapse Market Correction",
        "Click expansion arrow to adjust Market Correction"
      );
    }
    window.toggleSimCorrectionDrawer = toggleSimCorrectionDrawer;
    window.setSimCorrectionDrawerExpanded = setSimCorrectionDrawerExpanded;

    let simSimulationExpanded = false;

    function toggleSimSimulationDrawer() {
      setSimSimulationDrawerExpanded(!simSimulationExpanded);
    }

    function setSimSimulationDrawerExpanded(expanded) {
      simSimulationExpanded = Boolean(expanded);
      _setSimDrawerExpandedState(
        "sim-simulation",
        simSimulationExpanded,
        "Click to collapse Simulation",
        "Click expansion arrow to adjust Simulation"
      );
    }
    window.toggleSimSimulationDrawer = toggleSimSimulationDrawer;
    window.setSimSimulationDrawerExpanded = setSimSimulationDrawerExpanded;

    let simOverridesExpanded = false;

    function toggleSimOverridesDrawer() {
      setSimOverridesDrawerExpanded(!simOverridesExpanded);
    }

    function setSimOverridesDrawerExpanded(expanded) {
      simOverridesExpanded = Boolean(expanded);
      _setSimDrawerExpandedState(
        "sim-overrides",
        simOverridesExpanded,
        "Click to collapse Overrides",
        "Click expansion arrow to adjust Overrides",
        "flex"
      );
    }
    window.toggleSimOverridesDrawer = toggleSimOverridesDrawer;
    window.setSimOverridesDrawerExpanded = setSimOverridesDrawerExpanded;

    let simSwExpanded = false;

    function toggleSimSwDrawer() {
      setSimSwDrawerExpanded(!simSwExpanded);
    }

    function setSimSwDrawerExpanded(expanded) {
      simSwExpanded = Boolean(expanded);
      _setSimDrawerExpandedState(
        "sim-sw",
        simSwExpanded,
        "Click to collapse Scheduled Events",
        "Click to expand Scheduled Events"
      );
    }
    window.toggleSimSwDrawer = toggleSimSwDrawer;
    window.setSimSwDrawerExpanded = setSimSwDrawerExpanded;

    let simLiqExpanded = false;

    function toggleSimLiqDrawer() {
      setSimLiqDrawerExpanded(!simLiqExpanded);
    }

    function setSimLiqDrawerExpanded(expanded) {
      simLiqExpanded = Boolean(expanded);
      _setSimDrawerExpandedState(
        "sim-liq",
        simLiqExpanded,
        "Click to collapse Asset Liquidation Priority",
        "Click to expand Asset Liquidation Priority"
      );
    }
    window.toggleSimLiqDrawer = toggleSimLiqDrawer;
    window.setSimLiqDrawerExpanded = setSimLiqDrawerExpanded;

    function updateSimSummarySubtext() {
      const topEl = document.getElementById("sim-model-summary-subtext");
      const corrEl = document.getElementById("sim-correction-summary-subtext");
      const simEl = document.getElementById("sim-simulation-summary-subtext");

      const scenNames = { gfc2008: "2008 GFC", dotcom2000: "Dot-com", covid2020: "COVID", rates2022: "2022 Rate Shock", custom: "Custom", none: "None" };
      const scen = simStressScenario || "none";
      const corrEnabled = simActiveModel === "stress" && scen !== "none";
      const activeSpec = corrEnabled ? resolveStressSpec(scen) : null;
      const dispG = activeSpec ? Math.round(activeSpec.shocks.single_stock * 100) : 0;
      const dispNg = activeSpec ? Math.round(activeSpec.shocks.broad_equity * 100) : 0;
      const corrLbl = corrEnabled ? (scenNames[scen] || "Custom") : "None";

      let corrHtml = "";
      let corrTopShort = "";
      if (corrEnabled && activeSpec) {
        const downLbl = formatStressMonths(activeSpec.down_months, false);
        const bbLbl = formatStressMonths(activeSpec.broad_bounce_months, true);
        const sbLbl = formatStressMonths(activeSpec.stock_bounce_months, true);
        corrHtml = `<b style="color:#fb7185;">${corrLbl}</b> &bull; Down <b style="color:#f8fafc;">${dispG}% / ${dispNg}%</b> (${downLbl}) &bull; Bounce <b style="color:#f8fafc;">${bbLbl}</b> Broad / <b style="color:#f8fafc;">${sbLbl}</b> Stock`;
        corrTopShort = `<b style="color:#fb7185;">${corrLbl} (${dispG}% / ${dispNg}%, ${downLbl})</b>`;
      } else if (scen !== "none" && (simActiveModel === "range" || simActiveModel === "historical")) {
        const dormantLbl = scenNames[scen] || "Custom";
        corrHtml = `<b style="color:#f8fafc;">None</b> (applies when Simulation is None; ${dormantLbl} saved)`;
        corrTopShort = `<b style="color:#f8fafc;">None</b>`;
      } else {
        corrHtml = `<b style="color:#f8fafc;">None</b> &bull; No early market correction`;
        corrTopShort = `<b style="color:#f8fafc;">None</b>`;
      }
      if (corrEl) corrEl.innerHTML = corrHtml;

      let simHtml = "";
      let simTopShort = "";
      if (simActiveModel === "single_path" || simActiveModel === "stress") {
        simHtml = `<b style="color:#f8fafc;">None</b> &bull; Deterministic single path`;
        simTopShort = `<b style="color:#f8fafc;">None</b>`;
      } else if (simActiveModel === "range") {
        const sims = (mcSimulations || 500).toLocaleString("en-US");
        const modLbl = mcReturnModel === "historical" ? "Bootstrap" : "Normal";
        simHtml = `<b style="color:#34d399;">Monte-Carlo</b> &bull; ${sims} paths &bull; ${modLbl}`;
        simTopShort = `<b style="color:#34d399;">Monte-Carlo</b> (${sims} paths &bull; ${modLbl})`;
      } else if (simActiveModel === "historical") {
        const cd = window.COHORT_DATA || {};
        const firstY = cd.first_year || 1929, lastY = cd.last_year || 2025;
        const effFrom = cohortStartFrom < 0 ? firstY : Math.max(firstY, Math.min(lastY - 1, cohortStartFrom));
        const maxCohortStart = Math.max(firstY, lastY - 10);
        const cohortSpanCount = Math.max(1, maxCohortStart - effFrom + 1);
        const centerLbl = cohortCenterMode === "anchor" ? "Anchored" : "Raw History";
        simHtml = `<b style="color:#14b8a6;">Historical</b> &bull; ${effFrom}&ndash;${maxCohortStart} (${cohortSpanCount} cohorts) &bull; ${centerLbl} &bull; Real CPI`;
        simTopShort = `<b style="color:#14b8a6;">Historical</b> (${effFrom}&ndash;${maxCohortStart} &bull; ${centerLbl})`;
      }
      if (simEl) simEl.innerHTML = simHtml;

      if (topEl) {
        topEl.innerHTML = `Market Correction: ${corrTopShort} &bull; Simulation: ${simTopShort}`;
      }
    }

    function _onSimModelControlChanged() {
      syncSimModelControls();
      renderSimChartAndNav();
      updateSimSummaryDOM();
      checkSaveDirtyState();
    }
    function setMcReturnModel(m) {
      mcReturnModel = m === "historical" ? "historical" : "normal";
      setActiveSimComputingControl(mcReturnModel === "historical" ? "sim-mc-model-historical-btn" : "sim-mc-model-normal-btn", true);
      _onSimModelControlChanged();
    }
    function onMcVolGoogInput(v) {
      mcGoogVolPct = Math.max(0, Math.min(80, Number(v) || 0));
      mcSingleStockVolPct = mcGoogVolPct;
      _onSimModelControlChanged();
    }
    function onMcVolNonGoogInput(v) {
      mcNonGoogVolPct = Math.max(0, Math.min(40, Number(v) || 0));
      mcBroadEquityVolPct = mcNonGoogVolPct;
      _onSimModelControlChanged();
    }
    function onMcCorrInput(v) {
      mcGoogRestCorr = Math.max(-0.99, Math.min(0.99, Number(v) || 0));
      mcSingleStockCorr = mcGoogRestCorr;
      _onSimModelControlChanged();
    }
    function _fmtSignedPct(p, digits = 0) {
      const n = Number(p) || 0;
      const s = n.toFixed(digits);
      return n > 1e-6 ? `+${s}%` : `${s}%`;
    }

    function _collectOrderedSimHoldingsForTip() {
      const assets = Array.isArray(SIM_ASSETS) && SIM_ASSETS.length ? SIM_ASSETS : (BASE_SIM_ASSETS || []);
      const assetMap = Object.fromEntries(assets.map(a => [a.id, a]));
      const seen = new Set();
      const orderedIds = [];
      (simAssetOrder || []).forEach(id => {
        if (assetMap[id] && !seen.has(id)) {
          seen.add(id);
          orderedIds.push(id);
        }
      });
      assets.forEach(a => {
        if (a && a.id && !seen.has(a.id)) {
          seen.add(a.id);
          orderedIds.push(a.id);
        }
      });
      const out = [];
      let rank = 0;
      orderedIds.forEach(id => {
        const a = assetMap[id];
        if (!a || (a.is_unvested_goog && !simIncludeUnvested)) return;
        rank += 1;
        let curVal = Number(a.current_value) || 0;
        if (curVal <= 0 && a.is_unvested_goog && Array.isArray(a.unvested_tranches)) {
          curVal = a.unvested_tranches.reduce((sum, t) => sum + (Number(t.est_net_val || t.val) || 0), 0);
        }
        if (curVal <= 0.5 && !a.is_hypothetical) return;
        const fc = resolveSimAssetForecast(a);
        const rc = String(fc.risk_class || "broad_equity");
        const rcMeta = RISK_CLASSES_META[rc] || { label: rc };
        const mult = (a.rate_model === "fixed" && !fc.risk_overridden) ? 1.0 : (typeof simOptimismMultiplier === "number" ? simOptimismMultiplier : 1.0);
        const effNormPct = (Number(fc.growth_pct) || 0) * mult - (Number(fc.expense_ratio_pct) || 0);
        out.push({
          id,
          rank,
          shortTitle: String(a.short_title || a.title || id),
          color: String(a.color || "#38bdf8"),
          val: curVal,
          rc,
          rcLabel: String(rcMeta.label || rc),
          erDec: Math.max(0.0, Math.min(0.99, (Number(fc.expense_ratio_pct) || 0.0) / 100.0)),
          normNetPct: effNormPct,
        });
      });
      return out;
    }

    function _renderStressTipHoldingsSection(rows, headerLeft, headerRight, emptyText, footerNoteHtml = "") {
      if (!rows.length) {
        return `<div style="border-top:1px solid rgba(51,65,85,0.85);padding-top:5px;margin-top:6px;">` +
          `<div class="muted" style="font-size:10px;text-transform:uppercase;letter-spacing:0.04em;margin-bottom:2px;">${escHtml(headerLeft)}</div>` +
          `<div class="mono muted" style="font-size:10.8px;">${escHtml(emptyText)}</div>` +
          `${footerNoteHtml}</div>`;
      }
      const shown = rows.slice(0, 8);
      const extra = rows.slice(8);
      const linesHtml = shown.map(r =>
        `<div style="display:flex;justify-content:space-between;align-items:baseline;gap:10px;margin:2px 0;font-size:11px;">` +
          `<span style="min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">` +
            `<span class="muted mono" style="font-size:9.8px;display:inline-block;width:22px;">#${r.rank}</span>` +
            `<span class="swatch" style="background:${escHtml(r.color)};width:8px;height:8px;margin-right:5px;"></span>` +
            `<span style="color:#f1f5f9;">${escHtml(r.shortTitle)}</span> ` +
            `<span class="muted" style="font-size:9.8px;">(${escHtml(r.rcLabel)})</span>` +
          `</span>` +
          `<span class="mono" style="white-space:nowrap;flex-shrink:0;">${r.rightHtml}</span>` +
        `</div>`
      ).join("");
      let extraHtml = "";
      if (extra.length > 0) {
        const extraSum = extra.reduce((s, x) => s + x.val, 0);
        extraHtml = `<div class="mono muted" style="display:flex;justify-content:space-between;gap:10px;margin:2px 0;font-size:10.5px;">` +
          `<span style="padding-left:22px;">+${extra.length} other holding${extra.length === 1 ? "" : "s"}</span>` +
          `<span>${fmtUSD0(extraSum)}</span>` +
        `</div>`;
      }
      return `<div style="border-top:1px solid rgba(51,65,85,0.85);padding-top:5px;margin-top:6px;">` +
        `<div style="display:flex;justify-content:space-between;align-items:baseline;gap:8px;font-size:9.8px;text-transform:uppercase;letter-spacing:0.04em;color:#94a3b8;margin-bottom:3px;">` +
          `<span>${escHtml(headerLeft)}</span>` +
          `<span>${escHtml(headerRight)}</span>` +
        `</div>` +
        `${linesHtml}${extraHtml}${footerNoteHtml}` +
      `</div>`;
    }

    function syncStressCapsuleTooltips() {
      const shockEl = document.getElementById("sim-stress-shock-pill");
      const downEl = document.getElementById("sim-stress-down-pill");
      const bbEl = document.getElementById("sim-stress-broad-bounce-pill");
      const sbEl = document.getElementById("sim-stress-stock-bounce-pill");
      if (!shockEl && !downEl && !bbEl && !sbEl) return;

      const scenNames = {
        none: "Scenario: None",
        gfc2008: "2008 GFC",
        dotcom2000: "Dot-com",
        covid2020: "COVID",
        rates2022: "2022 Rate Shock",
        custom: "Custom",
      };
      const effScen = simStressScenario || "none";
      const spec = effScen !== "none" ? resolveStressSpec(effScen) : null;
      const isDormant = effScen !== "none" && simActiveModel !== "stress";
      const pillBadgeCls = effScen === "none" ? "pill-blue" : (isDormant ? "pill-cyan" : "pill-green");
      const badgeTxt = (scenNames[effScen] || "Custom") + (isDormant ? " (Simulation Override)" : "");
      const holdings = _collectOrderedSimHoldingsForTip();

      const setTipHtml = (el, html) => {
        if (!el) return;
        if (typeof el.removeAttribute === "function") el.removeAttribute("title");
        if (typeof el.setAttribute === "function") el.setAttribute("data-tip", html);
      };

      const rcOrder = [
        ["single_stock", "Single Stock"],
        ["crypto_speculative", "Crypto / Spec"],
        ["broad_equity", "Broad Equity"],
        ["hybrid_buffered", "Buffered / Hybrid"],
        ["fixed_income", "Bonds / Fixed Inc"],
        ["real_diversifier", "Gold / Real"],
      ];
      const broadSet = new Set(["broad_equity", "hybrid_buffered", "fixed_income", "real_diversifier"]);
      const stockSet = new Set(["single_stock", "crypto_speculative"]);

      const cashHoldings = holdings.filter(h => h.rc === "cash_fixed");
      const cashSum = cashHoldings.reduce((s, h) => s + h.val, 0);
      const cashFooterHtml = cashHoldings.length > 0
        ? `<div class="mono muted" style="font-size:10px;margin-top:4px;padding-top:3px;border-top:1px dashed rgba(51,65,85,0.65);">Unshocked Fixed / Cash: ${cashHoldings.length} holding${cashHoldings.length === 1 ? "" : "s"} (${fmtUSD0(cashSum)}) keep contractual rate</div>`
        : "";

      // 1. Downfall Size tooltip
      if (shockEl) {
        const dispG = spec ? Math.round(spec.shocks.single_stock * 100) : 0;
        const downLbl = spec ? formatStressMonths(spec.down_months, false) : "—";
        const rcChips = rcOrder.map(([rcKey, lbl]) => {
          const pct = spec ? Math.round((spec.shocks[rcKey] || 0) * 100) : 0;
          const col = pct < 0 ? "#fb7185" : (pct > 0 ? "#34d399" : "#cbd5e1");
          return `<span class="mono" style="font-size:10px;padding:1.5px 6px;border-radius:4px;background:rgba(30,41,59,0.88);border:1px solid #334155;">${escHtml(lbl)}: <strong style="color:${col};">${_fmtSignedPct(pct, 0)}</strong></span>`;
        }).join("") + `<span class="mono muted" style="font-size:10px;padding:1.5px 6px;border-radius:4px;background:rgba(15,23,42,0.7);border:1px solid #1e293b;">Fixed / Cash: Unshocked</span>`;

        const shockRows = holdings.filter(h => h.rc !== "cash_fixed").map(h => {
          const sDec = spec ? Number(spec.shocks[h.rc] || 0) : 0;
          const sPct = Math.round(sDec * 100);
          const col = sPct < 0 ? "#fb7185" : (sPct > 0 ? "#34d399" : "#94a3b8");
          const troughVal = h.val * Math.max(0, 1 + sDec);
          const rightHtml = spec
            ? `<strong style="color:${col};">${_fmtSignedPct(sPct, 0)}</strong> <span class="muted">&middot; ${fmtUSD0(h.val)} &rarr; ${fmtUSD0(troughVal)}</span>`
            : `<span class="muted">${fmtUSD0(h.val)} &middot; 0%</span>`;
          return { ...h, rightHtml };
        });

        const shockHtml = `<div class="stress-pill-tip" style="line-height:1.42;">` +
          `<div style="display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:4px;">` +
            `<strong style="font-size:12.5px;color:#f8fafc;">Downfall Size: <span class="mono" style="color:${dispG < 0 ? '#fb7185' : '#e2e8f0'};">${dispG}%</span></strong>` +
            `<span class="pill ${pillBadgeCls} mono" style="font-size:9.8px;">${escHtml(badgeTxt)}</span>` +
          `</div>` +
          `<div style="font-size:11px;color:#cbd5e1;margin-bottom:6px;">` +
            (spec
              ? `Cumulative peak-to-trough return over the <strong>${escHtml(downLbl)}</strong> downfall window (before withdrawals &amp; fees). Directly sets <strong>Single Stock</strong> and scales the other 5 variable Risk Profiles:`
              : `Currently <strong>inactive</strong> (<em>Scenario: None</em>). Sets the cumulative peak-to-trough drop for <strong>Single Stock</strong> and scales the other 5 variable Risk Profiles while leaving <strong>Fixed / Cash</strong> unshocked:`) +
          `</div>` +
          `<div style="display:flex;flex-wrap:wrap;gap:4px;margin-bottom:4px;">${rcChips}</div>` +
          _renderStressTipHoldingsSection(shockRows, "Holdings Impacted (Sell Priority)", spec ? "Shock · Peak → Trough" : "Current Balance", "No variable-rate holdings in portfolio", cashFooterHtml) +
        `</div>`;
        setTipHtml(shockEl, shockHtml);
      }

      // 2. Downfall Duration tooltip
      if (downEl) {
        const downM = spec ? spec.down_months : 0;
        const downLbl = spec ? formatStressMonths(downM, false) : "0 mo";
        const bsNote = (spec && simBearShield && spec.bear_shield_months > 0)
          ? ` <strong>&#128737; Bear Shield</strong> stays active through Month ${spec.bear_shield_months} (${escHtml(formatStressMonths(spec.bear_shield_months, false))}) to prioritize defensive withdrawals.`
          : "";
        const downRows = holdings.filter(h => h.rc !== "cash_fixed").map(h => {
          if (!spec) {
            return { ...h, rightHtml: `<span class="muted">${fmtUSD0(h.val)} &middot; Normal ${_fmtSignedPct(h.normNetPct, 1)}/yr</span>` };
          }
          const sDec = Number(spec.shocks[h.rc] || 0);
          const feeMo = Math.pow(1.0 - h.erDec, 1.0 / 12.0);
          const downGrossMo = Math.pow(Math.max(1e-6, 1.0 + sDec), 1.0 / downM) - 1.0;
          const downNetMo = (1.0 + downGrossMo) * feeMo - 1.0;
          const paceStr = downM < 12
            ? `${_fmtSignedPct(downNetMo * 100, 1)}/mo`
            : `${_fmtSignedPct((Math.pow(1.0 + downNetMo, 12) - 1.0) * 100, 1)}/yr`;
          const col = sDec < -1e-6 ? "#fb7185" : (sDec > 1e-6 ? "#34d399" : "#cbd5e1");
          return {
            ...h,
            rightHtml: `<strong style="color:${col};">${paceStr}</strong> <span class="muted">(${_fmtSignedPct(Math.round(sDec * 100), 0)} total)</span>`,
          };
        });

        const downHtml = `<div class="stress-pill-tip" style="line-height:1.42;">` +
          `<div style="display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:4px;">` +
            `<strong style="font-size:12.5px;color:#f8fafc;">Downfall Duration: <span class="mono" style="color:#f8fafc;">${escHtml(downLbl)}</span></strong>` +
            `<span class="pill ${pillBadgeCls} mono" style="font-size:9.8px;">${escHtml(badgeTxt)}</span>` +
          `</div>` +
          `<div style="font-size:11px;color:#cbd5e1;margin-bottom:4px;">` +
            (spec
              ? `Spans <strong>Months 1&ndash;${downM}</strong> from initial peak to trough across all <strong>6 variable Risk Profiles</strong> (<em>Single Stock</em>, <em>Crypto</em>, <em>Broad Equity</em>, <em>Buffered</em>, <em>Bonds</em>, <em>Gold/Real</em>).${bsNote}`
              : `Currently <strong>inactive</strong> (<em>Scenario: None</em>). Controls how many months (<code>3 mo</code> to <code>3 yr</code>) all 6 variable Risk Profiles take to compound from peak to trough.`) +
          `</div>` +
          _renderStressTipHoldingsSection(downRows, "Holdings Impacted (Sell Priority)", spec ? "Downfall Pace (Cumulative)" : "Balance · Normal Rate", "No variable-rate holdings in portfolio", cashFooterHtml) +
        `</div>`;
        setTipHtml(downEl, downHtml);
      }

      // 3. Broad-Equity Bounce tooltip
      if (bbEl) {
        const broadB = spec ? spec.broad_bounce_months : STRESS_NO_BOUNCE_MONTHS;
        const bbLbl = spec ? formatStressMonths(broadB, true) : "—";
        const downM = spec ? spec.down_months : 0;
        const isNoBounce = broadB >= STRESS_NO_BOUNCE_MONTHS;
        const broadRows = holdings.filter(h => broadSet.has(h.rc)).map(h => {
          if (!spec) {
            return { ...h, rightHtml: `<span class="muted">${fmtUSD0(h.val)} &middot; Normal ${_fmtSignedPct(h.normNetPct, 1)}/yr</span>` };
          }
          const sDec = Number(spec.shocks[h.rc] || 0);
          if (sDec >= -1e-6) {
            return { ...h, rightHtml: `<span style="color:#34d399;">Gained ${_fmtSignedPct(Math.round(sDec * 100), 0)}</span> <span class="muted">&middot; Normal ${_fmtSignedPct(h.normNetPct, 1)}/yr</span>` };
          }
          if (isNoBounce) {
            return { ...h, rightHtml: `<span style="color:#fbbf24;">No Bounce</span> <span class="muted">&middot; Normal ${_fmtSignedPct(h.normNetPct, 1)}/yr</span>` };
          }
          const feeMo = Math.pow(1.0 - h.erDec, 1.0 / 12.0);
          const recGrossMo = Math.pow(1.0 / Math.max(1e-6, 1.0 + sDec), 1.0 / broadB) - 1.0;
          const recNetAnnPct = (Math.pow((1.0 + recGrossMo) * feeMo, 12) - 1.0) * 100.0;
          const effAnnPct = Math.max(h.normNetPct, recNetAnnPct);
          return {
            ...h,
            rightHtml: `<strong style="color:#38bdf8;">${_fmtSignedPct(effAnnPct, 1)}/yr</strong> <span class="muted">(${fmtUSD0(h.val)})</span>`,
          };
        });

        const bbHtml = `<div class="stress-pill-tip" style="line-height:1.42;">` +
          `<div style="display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:4px;">` +
            `<strong style="font-size:12.5px;color:#f8fafc;">Broad-Equity Bounce: <span class="mono" style="color:#7dd3fc;">${escHtml(bbLbl)}</span></strong>` +
            `<span class="pill ${pillBadgeCls} mono" style="font-size:9.8px;">${escHtml(badgeTxt)}</span>` +
          `</div>` +
          `<div style="font-size:11px;color:#cbd5e1;margin-bottom:4px;">` +
            (spec
              ? (isNoBounce
                  ? `Set to <strong>No Bounce (L-shape)</strong>: after Month ${downM}, underwater diversified holdings resume their normal <strong>&#9670; Proj. Growth</strong> rate directly from the trough without an extra recovery surge.`
                  : `Spans <strong>Months ${downM + 1}&ndash;${downM + broadB}</strong>: rebounds underwater diversified holdings from trough back to pre-crash par (floored at normal <strong>&#9670; Proj. Growth</strong>) before resuming normal growth.`)
              : `Controls post-trough recovery back to pre-crash par for diversified holdings (drag right to <em>None</em> for L-shape no bounce).`) +
          `</div>` +
          `<div style="font-size:10.4px;color:#94a3b8;margin-bottom:4px;">` +
            `<strong>Asset classes affected:</strong> <em>Broad Equity</em>, <em>Buffered / Hybrid</em>, <em>Bonds / Fixed Income</em>, and <em>Gold / Real Assets</em> (classes that gained during the crash skip bounce-back; <em>Single Stock</em> &amp; <em>Crypto</em> use Single-Stock Bounce).` +
          `</div>` +
          _renderStressTipHoldingsSection(broadRows, "Diversified Holdings Governed", spec ? "Bounce Rate (Day-0 Balance)" : "Balance · Normal Rate", "No diversified holdings in portfolio") +
        `</div>`;
        setTipHtml(bbEl, bbHtml);
      }

      // 4. Single-Stock Bounce tooltip
      if (sbEl) {
        const stockB = spec ? spec.stock_bounce_months : STRESS_NO_BOUNCE_MONTHS;
        const sbLbl = spec ? formatStressMonths(stockB, true) : "—";
        const downM = spec ? spec.down_months : 0;
        const isNoBounce = stockB >= STRESS_NO_BOUNCE_MONTHS;
        const stockRows = holdings.filter(h => stockSet.has(h.rc)).map(h => {
          if (!spec) {
            return { ...h, rightHtml: `<span class="muted">${fmtUSD0(h.val)} &middot; Normal ${_fmtSignedPct(h.normNetPct, 1)}/yr</span>` };
          }
          const sDec = Number(spec.shocks[h.rc] || 0);
          if (sDec >= -1e-6) {
            return { ...h, rightHtml: `<span class="muted">${fmtUSD0(h.val)} &middot; Normal ${_fmtSignedPct(h.normNetPct, 1)}/yr</span>` };
          }
          if (isNoBounce) {
            return { ...h, rightHtml: `<span style="color:#fbbf24;">No Bounce</span> <span class="muted">&middot; Normal ${_fmtSignedPct(h.normNetPct, 1)}/yr (${fmtUSD0(h.val)})</span>` };
          }
          const feeMo = Math.pow(1.0 - h.erDec, 1.0 / 12.0);
          const recGrossMo = Math.pow(1.0 / Math.max(1e-6, 1.0 + sDec), 1.0 / stockB) - 1.0;
          const recNetAnnPct = (Math.pow((1.0 + recGrossMo) * feeMo, 12) - 1.0) * 100.0;
          const effAnnPct = Math.max(h.normNetPct, recNetAnnPct);
          return {
            ...h,
            rightHtml: `<strong style="color:#38bdf8;">${_fmtSignedPct(effAnnPct, 1)}/yr</strong> <span class="muted">(${fmtUSD0(h.val)})</span>`,
          };
        });

        const sbHtml = `<div class="stress-pill-tip" style="line-height:1.42;">` +
          `<div style="display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:4px;">` +
            `<strong style="font-size:12.5px;color:#f8fafc;">Single-Stock Bounce: <span class="mono" style="color:${isNoBounce && spec ? '#fbbf24' : '#7dd3fc'};">${escHtml(sbLbl)}</span></strong>` +
            `<span class="pill ${pillBadgeCls} mono" style="font-size:9.8px;">${escHtml(badgeTxt)}</span>` +
          `</div>` +
          `<div style="font-size:11px;color:#cbd5e1;margin-bottom:4px;">` +
            (spec
              ? (isNoBounce
                  ? `Set to <strong>No Bounce (Valuation Reset)</strong>: after Month ${downM}, concentrated equities compound from their depressed trough at normal <strong>&#9670; Proj. Growth</strong> without snapping back to peak multiples.`
                  : `Spans <strong>Months ${downM + 1}&ndash;${downM + stockB}</strong>: rebounds concentrated holdings from trough back to pre-crash par (floored at normal <strong>&#9670; Proj. Growth</strong>) before resuming normal growth.`)
              : `Controls post-trough recovery back to pre-crash par for concentrated stocks (drag right to <em>None</em> to model permanent valuation-multiple reset).`) +
          `</div>` +
          `<div style="font-size:10.4px;color:#94a3b8;margin-bottom:4px;">` +
            `<strong>Asset classes affected:</strong> <em>Single Stock</em> (individual equities &amp; unvested equity awards) and <em>Crypto / Speculative</em> (diversified funds use Broad-Equity Bounce; <em>Fixed / Cash</em> is unshocked).` +
          `</div>` +
          _renderStressTipHoldingsSection(stockRows, "Concentrated Holdings Governed", spec ? "Bounce Rate (Day-0 Balance)" : "Balance · Normal Rate", "No single-stock or speculative holdings in portfolio") +
        `</div>`;
        setTipHtml(sbEl, sbHtml);
      }

      // If user is currently hovering one of the 4 capsules while dragging its slider, live-refresh #ui-tooltip!
      const uiTip = document.getElementById("ui-tooltip");
      if (uiTip && uiTip.style.display !== "none") {
        [shockEl, downEl, bbEl, sbEl].forEach(el => {
          if (el && typeof el.matches === "function" && el.matches(":hover")) {
            const raw = el.getAttribute("data-tip") || "";
            if (raw && typeof sanitizeTooltipHtml === "function") {
              uiTip.innerHTML = sanitizeTooltipHtml(raw);
            }
          }
        });
      }
    }

    function setStressScenario(s) {
      const valid = ["none", "gfc2008", "dotcom2000", "covid2020", "rates2022", "custom"];
      simStressScenario = valid.includes(s) ? s : "none";
      if (simStressScenario === "none") {
        if (simActiveModel === "stress") simActiveModel = "single_path";
      } else {
        simActiveModel = "stress";
      }
      syncSimModelControls();
      const stressBtn = document.querySelector(`[data-stress="${simStressScenario}"]`);
      if (stressBtn) setActiveSimComputingControl(stressBtn, true);
      recomputeSimBars();
      renderSimChartAndNav();
      updateSimSummaryDOM();
      checkSaveDirtyState();
    }
    function _seedCustomStressFromCurrentIfPreset() {
      if (simStressScenario && simStressScenario !== "none" && simStressScenario !== "custom") {
        const spec = resolveStressSpec(simStressScenario);
        if (spec) {
          simCustomShockPct = Math.round(spec.shocks.single_stock * 100);
          simCustomDownMonths = spec.down_months;
          simCustomBroadBounceMonths = spec.broad_bounce_months;
          simCustomStockBounceMonths = spec.stock_bounce_months;
        }
      }
      simStressScenario = "custom";
      simActiveModel = "stress";
    }
    function _applyStressCustomEdit(pillId) {
      syncSimModelControls();
      setActiveSimComputingControl(pillId, false);
      recomputeSimBars();
      renderSimChartAndNav();
      updateSimSummaryDOM();
      checkSaveDirtyState();
    }
    function onStressShockInput(v) {
      _seedCustomStressFromCurrentIfPreset();
      simCustomShockPct = Math.max(-90, Math.min(0, Math.round(Number(v) || 0)));
      _applyStressCustomEdit("sim-stress-shock-pill");
    }
    function onStressDownMonthsInput(v) {
      _seedCustomStressFromCurrentIfPreset();
      simCustomDownMonths = Math.max(3, Math.min(36, Math.round((Number(v) || 12) / 3) * 3));
      _applyStressCustomEdit("sim-stress-down-pill");
    }
    function onStressBroadBounceInput(v) {
      _seedCustomStressFromCurrentIfPreset();
      simCustomBroadBounceMonths = Math.max(3, Math.min(STRESS_NO_BOUNCE_MONTHS, Math.round((Number(v) || 24) / 3) * 3));
      _applyStressCustomEdit("sim-stress-broad-bounce-pill");
    }
    function onStressStockBounceInput(v) {
      _seedCustomStressFromCurrentIfPreset();
      simCustomStockBounceMonths = Math.max(3, Math.min(STRESS_NO_BOUNCE_MONTHS, Math.round((Number(v) || 36) / 3) * 3));
      _applyStressCustomEdit("sim-stress-stock-bounce-pill");
    }
    window.onStressDownMonthsInput = onStressDownMonthsInput;
    window.onStressBroadBounceInput = onStressBroadBounceInput;
    window.onStressStockBounceInput = onStressStockBounceInput;

    function onSimOptimismInput(v) {
      const num = (v != null) ? Number(v) : 1.0;
      simOptimismMultiplier = Number.isFinite(num) ? Math.max(-2.0, Math.min(2.0, Math.round(num * 100) / 100)) : 1.0;
      updateSimOptimismDOM();
      setActiveSimComputingControl("sim-optimism-pill", false);
      recomputeSimBars();
      renderSimChartAndNav();
      updateSimSummaryDOM();
      checkSaveDirtyState();
    }
    window.onSimOptimismInput = onSimOptimismInput;

    function updateSimOptimismDOM() {
      const sl = document.getElementById("sim-optimism-slider");
      if (sl && Math.abs(parseFloat(sl.value) - simOptimismMultiplier) > 0.001) {
        sl.value = String(simOptimismMultiplier);
      }
      const pill = document.getElementById("sim-optimism-pill");
      if (pill) {
        pill.textContent = `${simOptimismMultiplier.toFixed(2)}×`;
      }
      const refBtn = document.getElementById("sim-optimism-ref-btn");
      if (refBtn) {
        refBtn.classList.toggle("active", Math.abs(simOptimismMultiplier - 1.0) < 0.001);
      }
      const sub = document.getElementById("sim-optimism-subtext");
      if (sub) sub.textContent = `Projects variable-rate holdings at ${simOptimismMultiplier.toFixed(2)}× their individual growth settings. Fixed rates use their stated return.`;
      updateSimOverridesSummary();
    }
    window.updateSimOptimismDOM = updateSimOptimismDOM;

    function onSimMonthlySpendingInput(v) {
      if (v === undefined || v === null || v === "") {
        simAnnualBurn = RETIREMENT_ANNUAL_BURN;
      } else {
        const parsed = Number(v);
        simAnnualBurn = Number.isFinite(parsed) ? Math.max(0, Math.min(1000000, Math.round(parsed))) : RETIREMENT_ANNUAL_BURN;
      }
      syncSimBurnControls();
      setActiveSimComputingControl("sim-monthly-spending-pill", false);
      recomputeSimBars();
      renderSimChartAndNav();
      updateSimSummaryDOM();
      checkSaveDirtyState();
    }
    window.onSimMonthlySpendingInput = onSimMonthlySpendingInput;

    function updateSimMonthlySpendingDOM() {
      const currentBurn = (simAnnualBurn != null && Number.isFinite(Number(simAnnualBurn)))
        ? Math.max(0, Math.round(Number(simAnnualBurn)))
        : RETIREMENT_ANNUAL_BURN;
      const refAnnual = Math.max(0, Math.round(Number(RETIREMENT_ANNUAL_BURN) || 50000));
      const refMoStr = formatMonthlySpend(refAnnual);
      const curMoStr = formatMonthlySpend(currentBurn);
      const curYrStr = formatBurnCompact(currentBurn);

      const sl = document.getElementById("sim-monthly-spending-slider");
      if (sl) {
        const slVal = Math.max(0, Math.min(1000000, currentBurn));
        if (Math.abs(parseFloat(sl.value) - slVal) > 0.5) {
          sl.value = String(slVal);
        }
      }
      const pill = document.getElementById("sim-monthly-spending-pill");
      if (pill) {
        pill.textContent = `${curMoStr} (${curYrStr}/yr)`;
        pill.onclick = () => onSimMonthlySpendingInput(refAnnual);
      }
      const refBtn = document.getElementById("sim-monthly-spending-ref-btn");
      if (refBtn) {
        refBtn.textContent = `Ref: ${refMoStr}`;
        refBtn.onclick = () => onSimMonthlySpendingInput(refAnnual);
        refBtn.classList.toggle("active", Math.abs(currentBurn - refAnnual) < 500);
        if (typeof _setSimRefTip === "function") {
          _setSimRefTip(refBtn, {
            title: `Portfolio Reference Spending (${refMoStr})`,
            meaning: `Resets Monthly Spending (after-tax) to your Portfolio Settings spending baseline (${refMoStr}, or ${fmtUSD0(refAnnual)}/yr) in today's dollars without changing the Portfolio Settings value.`,
            calculation: `Calculated as Portfolio Settings Annual Burn Rate (${fmtUSD0(refAnnual)}/yr) divided by 12 months. Moving the slider creates a scenario-only override saved in this Scenario.`,
            clickHint: `Click to reset Monthly Spending to ${refMoStr} (${formatBurnCompact(refAnnual)}/yr).`,
          });
        }
      }
      const sub = document.getElementById("sim-monthly-spending-subtext");
      if (sub) {
        if (Math.abs(currentBurn - refAnnual) < 500) {
          sub.textContent = `Monthly after-tax spending in today's dollars (auto-adjusts for inflation; matches Portfolio setting of ${refMoStr})`;
        } else {
          sub.textContent = `Scenario override active: ${curMoStr} (${fmtUSD0(currentBurn)}/yr) vs. Portfolio baseline ${refMoStr} (${fmtUSD0(refAnnual)}/yr) — save Scenario to persist override`;
        }
      }
      updateSimOverridesSummary();
    }
    window.updateSimMonthlySpendingDOM = updateSimMonthlySpendingDOM;

    function updateSimOverridesSummary() {
      const el = document.getElementById("sim-overrides-summary-subtext");
      if (!el) return;
      const mult = typeof simOptimismMultiplier === "number" ? simOptimismMultiplier : 1.0;
      const multOverridden = Math.abs(mult - 1.0) >= 0.001;
      const multColor = multOverridden ? "#38bdf8" : "#f8fafc";

      const currentBurn = (simAnnualBurn != null && Number.isFinite(Number(simAnnualBurn)))
        ? Math.max(0, Math.round(Number(simAnnualBurn)))
        : RETIREMENT_ANNUAL_BURN;
      const refAnnual = Math.max(0, Math.round(Number(RETIREMENT_ANNUAL_BURN) || 50000));
      const burnOverridden = Math.abs(currentBurn - refAnnual) >= 500;
      const burnColor = burnOverridden ? "#38bdf8" : "#f8fafc";
      const curMoStr = formatMonthlySpend(currentBurn);
      const curYrStr = formatBurnCompact(currentBurn);

      const refInf = typeof RETIREMENT_INFLATION_REF === "number" ? RETIREMENT_INFLATION_REF : 3.1;
      const curInf = typeof simInflationPct === "number" ? simInflationPct : refInf;
      const infOverridden = Math.abs(curInf - refInf) >= 0.05;
      const infColor = infOverridden ? "#38bdf8" : "#f8fafc";

      const wpSummary = buildWorkPlanConfig();
      const retParts = [];
      const anySsnSummary = (wpSummary.investors || []).some(m => Boolean(m.is_ssn_contributor));
      (wpSummary.investors || []).forEach(inv => {
        const scenOvr = _getScenarioMemberRetirementOverride(inv, anySsnSummary);
        const projInvs = Array.isArray(PROJECT_CFG.investors) ? PROJECT_CFG.investors : [];
        const ssnInvs = (typeof SSN_INVESTORS !== "undefined" && Array.isArray(SSN_INVESTORS)) ? SSN_INVESTORS : [];
        const rawP = projInvs[inv.idx] || {};
        const rawS = ssnInvs[inv.idx] || {};
        const baseWp = sanitizeWorkPlanJs(rawP.work_plan || rawS.work_plan, inv.milestone_age);
        if (!baseWp.is_working && scenOvr == null) return;
        const isOvr = scenOvr != null && (!baseWp.is_working || Math.round(Number(scenOvr)) !== baseWp.default_retirement_age);
        const c = isOvr ? "#38bdf8" : "#f8fafc";
        const statusTxt = inv.is_working ? `Age ${inv.retirement_age}` : "Now";
        retParts.push(`${escHtml(inv.name)}: <b style="color:${c};">${statusTxt}</b>`);
      });
      const retPrefix = retParts.length > 0 ? `Retire (${retParts.join(", ")}) &bull; ` : "";

      el.innerHTML =
        retPrefix +
        `Growth Multiplier: <b style="color:${multColor};">${mult.toFixed(2)}&times;</b>` +
        ` &bull; Monthly Spending: <b style="color:${burnColor};">${curMoStr}</b> (${curYrStr}/yr)` +
        ` &bull; Inflation: <b style="color:${infColor};">${curInf.toFixed(1)}%/yr</b>`;
    }
    window.updateSimOverridesSummary = updateSimOverridesSummary;

    let _simRetSlidersDelegated = false;
    function _ensureSimRetSlidersDelegation(wrap) {
      if (_simRetSlidersDelegated || !wrap || typeof wrap.addEventListener !== "function") return;
      _simRetSlidersDelegated = true;
      wrap.addEventListener("input", evt => {
        const sl = evt.target && evt.target.closest ? evt.target.closest("[data-ret-slider-idx]") : null;
        if (!sl) return;
        const idx = parseInt(sl.getAttribute("data-ret-slider-idx"), 10);
        if (Number.isInteger(idx) && idx >= 0) onSimRetirementAgeInput(idx, sl.value, false);
      });
      wrap.addEventListener("click", evt => {
        const btn = evt.target && evt.target.closest ? evt.target.closest("[data-ret-reset-idx]") : null;
        if (!btn) return;
        const idx = parseInt(btn.getAttribute("data-ret-reset-idx"), 10);
        if (Number.isInteger(idx) && idx >= 0) onSimRetirementAgeInput(idx, null, true);
      });
    }

    function _getScenarioMemberRetirementOverride(inv, anySsn) {
      const memKey = inv.member_key || `member_${inv.idx}_retirement_age`;
      if (simMemberRetirementAges && simMemberRetirementAges[memKey] != null) {
        return simMemberRetirementAges[memKey];
      }
      if (inv.is_ssn_contributor && inv.ssn_rank === 0) return simInvestor1RetirementAge;
      if (inv.is_ssn_contributor && inv.ssn_rank === 1) return simInvestor2RetirementAge;
      if (!anySsn && inv.idx === 0) return simInvestor1RetirementAge;
      if (!anySsn && inv.idx === 1) return simInvestor2RetirementAge;
      return null;
    }

    function syncSimRetirementAgeControls() {
      const wrap = document.getElementById("sim-retirement-age-sliders-wrap");
      if (!wrap) return;
      _ensureSimRetSlidersDelegation(wrap);
      const wpCfg = buildWorkPlanConfig();
      const allMembers = wpCfg.investors || [];
      const anySsn = allMembers.some(m => Boolean(m.is_ssn_contributor));
      const visibleCards = [];

      allMembers.forEach(inv => {
        const idx = inv.idx;
        const scenOvr = _getScenarioMemberRetirementOverride(inv, anySsn);
        if (!inv.base_is_working && scenOvr == null) return;

        const curAge = Math.max(18, ageOnIso(inv.birth_year, inv.birth_month, 1, TODAY_ISO, 0));
        const maxAge = Math.max(curAge, inv.milestone_age);
        const effAge = Math.max(curAge, Math.min(maxAge, inv.retirement_age));
        const refAge = Math.max(curAge, Math.min(maxAge, inv.default_retirement_age));
        const isAtRef = scenOvr == null || (inv.base_is_working && Math.round(Number(scenOvr)) === inv.default_retirement_age);
        const isStoppedNow = !inv.is_working || effAge <= curAge;
        const pillLbl = isStoppedNow ? `Stop Now (${curAge})` : `Age ${effAge}`;
        const refLbl = inv.base_is_working ? `Ref: Age ${refAge}` : "Ref: Not Working";
        const totAnnualContrib = (inv.contributions || []).reduce(
          (s, r) => s + Number(r.employee_annual || 0) + Number(r.employer_match_annual || 0),
          0
        );
        const payDesc = inv.covers_full_burn
          ? "covers 100% of household burn"
          : (inv.annual_budget_offset > 0 ? `offsets ${fmtUSD0(inv.annual_budget_offset)}/yr of burn` : "no burn offset");
        const contribDesc = totAnnualContrib > 0 ? ` + ${fmtUSD0(totAnnualContrib)}/yr account contributions` : "";
        const subTxt = isStoppedNow
          ? `Not working in this Scenario (Portfolio baseline: ${inv.base_is_working ? `Age ${refAge}` : "Not Working"}).`
          : `Works through ${escHtml(inv.ret_ym)} (${payDesc}${contribDesc}). Bounded to [${curAge}..${maxAge}].`;

        visibleCards.push({
          idx,
          name: inv.name,
          curAge,
          maxAge,
          effAge,
          isAtRef,
          pillLbl,
          refLbl,
          subTxt,
          structKey: `${idx}:${inv.name}:${curAge}:${maxAge}`,
        });
      });

      if (visibleCards.length === 0) {
        wrap.style.display = "none";
        wrap.innerHTML = "";
        if (wrap.dataset) wrap.dataset.retCardsSig = "";
      } else {
        wrap.style.display = "flex";
        const nextSig = visibleCards.map(c => c.structKey).join("|");
        const prevSig = wrap.dataset ? wrap.dataset.retCardsSig : wrap.getAttribute("data-ret-cards-sig");
        // Only construct card DOM when visible members or bounds change so dragging `<input type="range">` never loses capture.
        if (prevSig !== nextSig || !wrap.querySelector("[data-ret-slider-idx]")) {
          wrap.innerHTML = visibleCards.map(c => `
            <div class="growth-slider-box mdl-in-box" data-ret-card-idx="${c.idx}" style="display:flex;flex-direction:column;align-items:stretch;gap:4px;box-sizing:border-box;width:100%;margin:0;padding:8px 14px;">
              <div style="display:flex;align-items:center;justify-content:flex-start;gap:12px;width:100%;flex-wrap:wrap;">
                <span class="mdl-in-lbl" style="margin:0;white-space:nowrap;">
                  <span class="mdl-in-glyph">&#9670;</span> ${escHtml(c.name)}'s Retirement Age:
                </span>
                <div style="display:flex;align-items:center;gap:6px;min-width:0;flex:1 1 360px;max-width:520px;">
                  <button type="button" class="cap-btn mono ${c.isAtRef ? "active" : ""}" data-ret-ref-btn="${c.idx}" data-ret-reset-idx="${c.idx}" title="Reset ${escHtml(c.name)}'s retirement age to Portfolio Settings baseline">${escHtml(c.refLbl)}</button>
                  <span class="mono muted" style="font-size:11px;flex-shrink:0;">Now (${c.curAge})</span>
                  <input type="range" data-ret-slider-idx="${c.idx}" min="${c.curAge}" max="${c.maxAge}" step="1" value="${c.effAge}" style="flex:1 1 160px;min-width:110px;cursor:pointer;" />
                  <span class="mono muted" style="font-size:11px;flex-shrink:0;">${c.maxAge}</span>
                  <button type="button" id="sim-ret-pill-${c.idx}" class="mdl-in-pill mono" data-ret-pill-idx="${c.idx}" data-ret-reset-idx="${c.idx}" style="cursor:pointer;min-width:106px;text-align:center;white-space:nowrap;flex-shrink:0;" title="Click to reset to Portfolio baseline">${escHtml(c.pillLbl)}</button>
                </div>
              </div>
              <div class="muted" data-ret-sub-idx="${c.idx}" style="font-size:11.8px;color:#94a3b8;line-height:1.35;margin-top:2px;">${c.subTxt}</div>
            </div>
          `).join("");
          if (wrap.dataset) wrap.dataset.retCardsSig = nextSig;
          else wrap.setAttribute("data-ret-cards-sig", nextSig);
        } else {
          visibleCards.forEach(c => {
            const refBtn = wrap.querySelector(`[data-ret-ref-btn="${c.idx}"]`);
            if (refBtn) {
              refBtn.textContent = c.refLbl;
              refBtn.classList.toggle("active", c.isAtRef);
            }
            const sl = wrap.querySelector(`[data-ret-slider-idx="${c.idx}"]`);
            if (sl && Math.round(Number(sl.value)) !== c.effAge) {
              sl.value = String(c.effAge);
            }
            const pill = wrap.querySelector(`[data-ret-pill-idx="${c.idx}"]`);
            if (pill) {
              pill.textContent = c.pillLbl;
            }
            const sub = wrap.querySelector(`[data-ret-sub-idx="${c.idx}"]`);
            if (sub) {
              sub.innerHTML = c.subTxt;
            }
          });
        }
      }
      updateSimOverridesSummary();
    }
    window.syncSimRetirementAgeControls = syncSimRetirementAgeControls;

    function onSimRetirementAgeInput(invIdx, rawVal, isReset = false) {
      const wpCfg = buildWorkPlanConfig();
      const allMembers = wpCfg.investors || [];
      const anySsn = allMembers.some(m => Boolean(m.is_ssn_contributor));
      const inv = allMembers.find(x => x.idx === invIdx);
      if (!inv) return;
      const curAge = Math.max(18, ageOnIso(inv.birth_year, inv.birth_month, 1, TODAY_ISO, 0));
      const maxAge = Math.max(curAge, inv.milestone_age);

      let nextOvr = null;
      if (!isReset && rawVal != null && rawVal !== "") {
        const parsed = Math.max(curAge, Math.min(maxAge, Math.round(Number(rawVal))));
        if (!(inv.base_is_working && parsed === inv.default_retirement_age)) {
          nextOvr = parsed;
        }
      }
      if (!simMemberRetirementAges || typeof simMemberRetirementAges !== "object") {
        simMemberRetirementAges = {};
      }
      const memKey = inv.member_key || `member_${invIdx}_retirement_age`;
      if (nextOvr == null) {
        delete simMemberRetirementAges[memKey];
      } else {
        simMemberRetirementAges[memKey] = nextOvr;
      }
      if ((inv.is_ssn_contributor && inv.ssn_rank === 0) || (!anySsn && invIdx === 0)) {
        simInvestor1RetirementAge = nextOvr;
      } else if ((inv.is_ssn_contributor && inv.ssn_rank === 1) || (!anySsn && invIdx === 1)) {
        simInvestor2RetirementAge = nextOvr;
      }

      syncSimRetirementAgeControls();
      if (typeof setActiveSimComputingControl === "function") {
        setActiveSimComputingControl(`sim-ret-pill-${invIdx}`, isReset);
      }
      if (typeof scheduleSimRecompute === "function") {
        scheduleSimRecompute();
      } else {
        recomputeSimBars();
        renderSimChartAndNav();
        updateSimSummaryDOM();
        checkSaveDirtyState();
      }
    }
    window.onSimRetirementAgeInput = onSimRetirementAgeInput;

    function _onCohortControlChanged() {
      syncSimModelControls();
      renderSimChartAndNav();
      updateSimSummaryDOM();
      checkSaveDirtyState();
    }
    function setCohortCenter(m) {
      cohortCenterMode = m === "raw" ? "raw" : "anchor";
      setActiveSimComputingControl(cohortCenterMode === "raw" ? "sim-cohort-center-raw-btn" : "sim-cohort-center-anchor-btn", true);
      _onCohortControlChanged();
    }
    function onCohortStartFrom(v) {
      cohortStartFrom = Number(v) < 0 ? -1 : Math.round(Number(v));
      setActiveSimComputingControl("sim-cohort-from-pill", false);
      _onCohortControlChanged();
    }

    function resetSimAssetOrder(preset) {
      simOrderPreset = preset || "tax_optimal";
      const orderBtnMap = {
        tax_optimal: "sim-order-opt-btn",
        heir_legacy: "sim-order-legacy-btn",
        goog_first: "sim-order-goog-btn",
      };
      if (orderBtnMap[simOrderPreset]) {
        setActiveSimComputingControl(orderBtnMap[simOrderPreset], true);
      }
      if (simOrderPreset === "heir_legacy") {
        simAssetOrder = buildHeirLegacyOrder();
      } else {
        const copy = [...SIM_ASSETS];
        const isSingleStock = a => resolveSimAssetForecast(a).risk_class === "single_stock" || a.bucket === "goog";
        if (simOrderPreset === "non_goog_first") {
          copy.sort((a, b) => {
            const sa = isSingleStock(a), sb = isSingleStock(b);
            if (sa !== sb) return !sa ? -1 : 1;
            return (a.default_rank || 999) - (b.default_rank || 999) || String(a.id).localeCompare(String(b.id));
          });
        } else if (simOrderPreset === "goog_first") {
          copy.sort((a, b) => {
            const sa = isSingleStock(a), sb = isSingleStock(b);
            if (sa !== sb) return sa ? -1 : 1;
            return (a.default_rank || 999) - (b.default_rank || 999) || String(a.id).localeCompare(String(b.id));
          });
        } else {
          copy.sort((a, b) => (a.default_rank || 999) - (b.default_rank || 999) || String(a.id).localeCompare(String(b.id)));
        }
        simAssetOrder = copy.map(a => a.id);
      }
      updateSimOrderPresetButtons();
      recomputeSimBars();
      renderSimChartAndNav();
      checkSaveDirtyState();
    }

    function moveSimAssetRow(assetId, delta) {
      const idx = simAssetOrder.indexOf(assetId);
      if (idx < 0) return;
      const targetIdx = idx + delta;
      if (targetIdx < 0 || targetIdx >= simAssetOrder.length) return;
      const next = [...simAssetOrder];
      const [item] = next.splice(idx, 1);
      next.splice(targetIdx, 0, item);
      simAssetOrder = next;
      simOrderPreset = "custom";
      updateSimOrderPresetButtons();
      recomputeSimBars();
      renderSimChartAndNav();
      checkSaveDirtyState();
    }

    function onSimRowDragStart(evt, assetId) {
      _draggedSimAssetId = assetId;
      if (evt.dataTransfer) {
        evt.dataTransfer.effectAllowed = "move";
        evt.dataTransfer.setData("text/plain", assetId);
      }
      const tr = evt.currentTarget;
      if (tr) setTimeout(() => tr.classList.add("dragging"), 0);
    }

    function onSimRowDragOver(evt, targetAssetId) {
      evt.preventDefault();
      if (!_draggedSimAssetId || _draggedSimAssetId === targetAssetId) return;
      if (evt.dataTransfer) evt.dataTransfer.dropEffect = "move";
      const tr = evt.currentTarget;
      if (!tr) return;
      const rect = tr.getBoundingClientRect();
      const isTopHalf = (evt.clientY - rect.top) < rect.height / 2;
      tr.classList.toggle("drag-over-top", isTopHalf);
      tr.classList.toggle("drag-over-bottom", !isTopHalf);
    }

    function onSimRowDragLeave(evt) {
      const tr = evt.currentTarget;
      if (tr) {
        tr.classList.remove("drag-over-top", "drag-over-bottom");
      }
    }

    function onSimRowDrop(evt, targetAssetId) {
      evt.preventDefault();
      const tr = evt.currentTarget;
      let insertBefore = true;
      if (tr) {
        const rect = tr.getBoundingClientRect();
        insertBefore = (evt.clientY - rect.top) < rect.height / 2;
        tr.classList.remove("drag-over-top", "drag-over-bottom");
      }
      const srcId = _draggedSimAssetId || (evt.dataTransfer && evt.dataTransfer.getData("text/plain"));
      _draggedSimAssetId = null;
      if (!srcId || srcId === targetAssetId) return;

      const next = simAssetOrder.filter(id => id !== srcId);
      const targetIdx = next.indexOf(targetAssetId);
      if (targetIdx < 0) return;
      next.splice(insertBefore ? targetIdx : targetIdx + 1, 0, srcId);
      simAssetOrder = next;
      simOrderPreset = "custom";
      updateSimOrderPresetButtons();
      recomputeSimBars();
      renderSimChartAndNav();
      checkSaveDirtyState();
    }

    function onSimRowDragEnd(evt) {
      _draggedSimAssetId = null;
      document.querySelectorAll(".sim-liq-row").forEach(r => {
        r.classList.remove("dragging", "drag-over-top", "drag-over-bottom");
      });
    }

    function renderSimLiquidationTable() {
      const tbody = document.getElementById("sim-liq-tbody");
      const tfoot = document.getElementById("sim-liq-tfoot");
      if (!tbody || !tfoot) return;

      const assetMap = Object.fromEntries(SIM_ASSETS.map(a => [a.id, a]));
      const totActiveStartVal = SIM_ASSETS.reduce(
        (s, a) => s + ((a.is_unvested_goog && !simIncludeUnvested) ? 0 : (Number(a.current_value) || 0)),
        0
      );
      const totActiveStartBasis = SIM_ASSETS.reduce(
        (s, a) => s + ((a.is_unvested_goog && !simIncludeUnvested) ? 0 : (Number(a.cost_basis) || 0)),
        0
      );

      const riskBadgeMap = {
        single_stock: `<span class="pill pill-blue mono">Single Stock</span>`,
        crypto_speculative: `<span class="pill pill-rose mono">Crypto / Spec</span>`,
        broad_equity: `<span class="pill pill-green mono">Broad Equity</span>`,
        real_diversifier: `<span class="pill pill-amber mono">Real / Gold</span>`,
        hybrid_buffered: `<span class="pill pill-teal mono">Balanced / Buf</span>`,
        fixed_income: `<span class="pill pill-cyan mono">Fixed Income</span>`,
        cash_fixed: `<span class="pill pill-slate mono">Cash / Fixed</span>`,
      };

      let activeRank = 0;
      tbody.innerHTML = simAssetOrder.map((id, idx) => {
        const a = assetMap[id];
        if (!a) return "";
        const isDisabled = a.is_unvested_goog && !simIncludeUnvested;
        if (!isDisabled) activeRank += 1;

        const curVal = Number(a.current_value) || 0;
        const basisVal = Number(a.cost_basis) || 0;
        const gainUsd = curVal - basisVal;
        const gainRatio = curVal > 0 ? Math.max(0, gainUsd / curVal) : 0;
        const gainPctOfVal = curVal > 0 ? (gainUsd / curVal) * 100.0 : 0;
        const portPct = (!isDisabled && totActiveStartVal > 0) ? (curVal / totActiveStartVal) * 100.0 : 0;

        let initEffTaxPct = 0;
        if (a.tax_model === "ANNUITY_LIFO" || a.tax_model === "ORDINARY_FULL") {
          initEffTaxPct = (a.tax_model === "ORDINARY_FULL" || gainUsd > 0) ? (a.tax_rate || 0) * 100.0 : 0;
        } else {
          initEffTaxPct = (a.tax_rate || 0) * gainRatio * 100.0;
        }

        const resolvedFc = resolveHoldingForecast(a);
        const bucketBadge = riskBadgeMap[resolvedFc.risk_class] || riskBadgeMap.broad_equity;

        let liqBadge = `<span class="pill pill-green mono">${escHtml(a.liquidity_label)}</span>`;
        if (a.is_unvested_goog) {
          liqBadge = isDisabled
            ? `<span class="pill pill-slate mono">Excluded via Toggle</span>`
            : `<span class="pill pill-blue mono">${escHtml(a.liquidity_label)}</span>`;
        } else if (a.is_hypothetical && !a.unlock_ym && !a.maturity_iso) {
          liqBadge = `<span class="pill pill-cyan mono">${escHtml(a.liquidity_label || "Liquid ($0 Day 0)")}</span>`;
        } else if (a.maturity_iso || a.unlock_ym) {
          liqBadge = `<span class="pill pill-cyan mono">${escHtml(a.liquidity_label)}</span>`;
        }

        const gainBadgeCls = gainUsd <= 0 ? "pill-green" : (initEffTaxPct < 10 ? "pill-blue" : "pill-rose");
        const gainBadgeTxt = a.is_hypothetical
          ? (a.expense_ratio_pct > 0 ? `ER ${Number(a.expense_ratio_pct).toFixed(2)}%/yr` : "Starts at $0")
          : (gainUsd <= 0
              ? `${gainPctOfVal.toFixed(1)}% (0% tax now)`
              : `+${gainPctOfVal.toFixed(1)}% gain (prog. \u2264${initEffTaxPct.toFixed(1)}% tax)`);

        const st = simAssetStats[id] || {};
        let drawdownHtml = "";
        if (isDisabled) {
          drawdownHtml = `<span class="muted mono">Excluded</span>`;
        } else if (st.firstTappedLbl) {
          const endHorizonYr = (SIM_BARS.length ? String(SIM_BARS[SIM_BARS.length - 1].yearLbl || "") : "") || String((parseInt((TODAY_ISO || "2026").slice(0, 4), 10) || 2026) + 40);
          const spanStr = st.depletedLbl
            ? `${escHtml(st.firstTappedLbl)} &rarr; <strong>Depleted ${escHtml(st.depletedLbl)}</strong>`
            : `${escHtml(st.firstTappedLbl)} &rarr; ${escHtml(st.lastTappedLbl || endHorizonYr)}`;
          const effRowPct = st.totalGrossSold > 0 ? (st.totalTaxPaid / st.totalGrossSold) * 100.0 : 0;
          let lossNoteHtml = "";
          if ((st.totalLossHarvested || 0) > 1 && (st.totalTaxSaved || 0) > 1) {
            lossNoteHtml = `<div class="row-sub mono" style="color:#34d399;">Harvested ${fmtUSD0(st.totalLossHarvested)} loss &bull; Sheltered +${fmtUSD0(st.totalTaxSaved)} tax</div>`;
          } else if ((st.totalLossHarvested || 0) > 1) {
            lossNoteHtml = `<div class="row-sub mono" style="color:#7dd3fc;">Harvested ${fmtUSD0(st.totalLossHarvested)} cap. loss carryforward</div>`;
          } else if ((st.totalTaxSaved || 0) > 1) {
            lossNoteHtml = `<div class="row-sub mono" style="color:#34d399;">Loss carryforward saved +${fmtUSD0(st.totalTaxSaved)} tax (${fmtUSD0(st.totalLossSheltered)} gain sheltered)</div>`;
          }
          drawdownHtml = `
            <div class="mono" style="color:${st.depletedLbl ? '#fb7185' : '#38bdf8'};">${spanStr}</div>
            <div class="row-sub mono">Sold ${fmtUSD0(st.totalGrossSold)} gross &bull; Tax: <span style="color:#fb7185;">-${fmtUSD0(st.totalTaxPaid)}</span> (${effRowPct.toFixed(1)}%)</div>
            ${lossNoteHtml}
          `;
        } else {
          const b40 = SIM_BARS[SIM_BARS.length - 1];
          const yr40Val = simDollarMode === "real" && b40 ? (st.val || 0) / (b40.deflator || 1.0) : (st.val || 0);
          drawdownHtml = `
            <div class="mono" style="color:${(st.val || 0) > 1 ? '#34d399' : '#64748b'};">${(st.val || 0) > 1 ? "Untouched (40 yrs)" : (a.is_hypothetical ? "Unfunded ($0)" : "Untouched")}</div>
            <div class="row-sub mono">Yr 40 Val (${simDollarMode === "real" ? "Today's $" : "Nominal $"}): ${fmtUSD0(yr40Val)}</div>
          `;
        }

        const editKey = a.is_hypothetical ? a.id : (a.forecast_key || a.id);
        const hasCustomFn = !a.is_hypothetical && Boolean(
          a.friendly_name || (typeof globalPreferences === "object" && globalPreferences?.friendly_names?.[editKey]?.friendly_name)
        );
        const editBtnHtml = a.is_hypothetical
          ? `<button type="button" class="btn-icon-edit" draggable="false" style="flex-shrink:0;" title="Edit Hypothetical Account: ${escHtml(a.title)}" data-sa-action="open-hyp" data-sa-key="${escHtml(a.id)}" data-sa-id="${escHtml(a.id)}" data-sa-stop="1"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/><path d="m15 5 4 4"/></svg></button>`
          : `<button type="button" class="btn-icon-edit ${hasCustomFn ? 'has-friendly' : ''}" draggable="false" style="flex-shrink:0;" title="Edit Holding Details &amp; Scenario Forecast: ${escHtml(a.title)}" data-sa-action="open-friendly" data-sa-key="${escHtml(editKey)}" data-sa-stop="1"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/><path d="m15 5 4 4"/></svg></button>`;

        const hypBadgeHtml = a.is_hypothetical
          ? `<span class="pill pill-cyan mono" style="font-size:9.5px;padding:1px 6px;border-color:rgba(45,212,191,0.5);color:#5eead4;">&#10022; HYPOTHETICAL</span>`
          : "";

        const capsuleHtml = a.is_hypothetical
          ? renderForecastCapsuleHtml(a, a.id, true)
          : renderForecastCapsuleHtml(a, a.forecast_key || a.id, false);

        let valBasisSub = "";
        if (isDisabled) {
          valBasisSub = `Basis: ${fmtUSD2(basisVal)} &bull; Excluded`;
        } else if (a.is_hypothetical) {
          valBasisSub = `Basis: $0.00 &bull; Funded via Actions`;
        } else {
          const gainSign = gainUsd > 0 ? "+" : (gainUsd < 0 ? "-" : "");
          const gainColor = gainUsd > 0 ? "#34d399" : (gainUsd < 0 ? "#fb7185" : "#94a3b8");
          const tipAttr = a.basis_sub ? ` class="has-tip" data-tip="${escHtml(a.basis_sub)}"` : "";
          const dotHtml = a.basis_sub ? `<span class="info-dot">i</span>` : "";
          const gainTxt = gainUsd === 0 ? "$0 gain" : `${gainSign}${fmtUSD2(Math.abs(gainUsd))}${curVal > 0 ? `, ${gainSign}${gainPctOfVal.toFixed(1)}%` : ""}`;
          valBasisSub = `<span${tipAttr}>Basis: ${fmtUSD2(basisVal)}${dotHtml}</span> <span style="color:${gainColor};">(${gainTxt})</span> &bull; ${portPct.toFixed(1)}%`;
        }

        let taxSubTxt = "Return of basis 0% tax &bull; Appreciation taxed";
        if (a.tax_model === "TAX_FREE") {
          taxSubTxt = "100% Tax-Free distributions";
        } else if (a.tax_model === "ORDINARY_FULL") {
          taxSubTxt = "100% Taxed as ordinary income";
        } else if (a.tax_model === "ANNUITY_LIFO") {
          taxSubTxt = "IRS &sect;72 LIFO (earnings taxed first)";
        }

        return `<tr class="sim-liq-row ${isDisabled ? 'disabled-row' : ''}"
          draggable="true"
          data-sa-drag-id="${escHtml(id)}"
          ondragstart="onSimRowDragStart(event, this.getAttribute('data-sa-drag-id'))"
          ondragover="onSimRowDragOver(event, this.getAttribute('data-sa-drag-id'))"
          ondragleave="onSimRowDragLeave(event)"
          ondrop="onSimRowDrop(event, this.getAttribute('data-sa-drag-id'))"
          ondragend="onSimRowDragEnd(event)">
          <td class="mono" style="white-space:nowrap;">
            <div style="display:inline-flex;align-items:center;gap:5px;">
              <span class="drag-handle" title="Drag row up or down to change liquidation priority">&#8942;&#8942;</span>
              <strong style="min-width:22px;color:${isDisabled ? '#64748b' : '#f8fafc'};">${isDisabled ? "—" : `#${activeRank}`}</strong>
              <span style="display:inline-flex;flex-direction:column;gap:2px;">
                <button class="row-move-btn" data-sa-action="move-sim-asset" data-sa-id="${escHtml(id)}" data-sa-dir="-1" ${idx === 0 ? "disabled" : ""} title="Move up in liquidation priority">&#9650;</button>
                <button class="row-move-btn" data-sa-action="move-sim-asset" data-sa-id="${escHtml(id)}" data-sa-dir="1" ${idx === simAssetOrder.length - 1 ? "disabled" : ""} title="Move down in liquidation priority">&#9660;</button>
              </span>
            </div>
          </td>
          <td>
            <div style="display:flex;align-items:center;flex-wrap:wrap;gap:6px;">
              ${editBtnHtml}
              <span class="swatch" style="background:${a.color || '#38bdf8'};width:10px;height:10px;vertical-align:middle;flex-shrink:0;"></span>
              <strong>${escHtml(a.title)}</strong>
              ${bucketBadge}
              ${liqBadge}
              ${hypBadgeHtml}
            </div>
            <div class="row-sub mono" style="margin-top:2px;padding-left:30px;">${escHtml(a.subtitle)}${a.liquidity_sub ? ` &bull; <span class="muted">${escHtml(a.liquidity_sub)}</span>` : ""}</div>
          </td>
          <td class="num">
            <div class="mono"><strong>${fmtUSD2(curVal)}</strong></div>
            <div class="row-sub mono">${valBasisSub}</div>
          </td>
          <td class="num mdl-in-td">${capsuleHtml}</td>
          <td>
            <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;">
              <strong class="has-tip" data-tip="${escHtml(a.tax_rule_detail)}">${escHtml(a.tax_rule_title)}<span class="info-dot">i</span></strong>
              <span class="pill ${gainBadgeCls} mono" style="font-size:10.3px;padding:1px 6px;">${escHtml(gainBadgeTxt)}</span>
            </div>
            <div class="row-sub mono" style="margin-top:2px;">${taxSubTxt}</div>
          </td>
          <td class="num">${drawdownHtml}</td>
        </tr>`;
      }).join("");

      const totGain = totActiveStartVal - totActiveStartBasis;
      const totGainPct = totActiveStartVal > 0 ? (totGain / totActiveStartVal) * 100.0 : 0;
      const cumEffTaxPct = simCumGrossSold > 0 ? (simCumTaxPaid / simCumGrossSold) * 100.0 : 0;
      const wGrowthPct = computeWeightedPortfolioForecastPct();
      const wFillPct = Math.max(0, Math.min(100, (Math.max(0, wGrowthPct) / 20.0) * 100.0));
      const wSign = wGrowthPct >= 0 ? "+" : "";

      tfoot.innerHTML = `<tr>
        <td colspan="2">Total Simulated Portfolio (${simIncludeUnvested ? "Incl. Unvested Equity" : "Vested Holdings Only"})</td>
        <td class="num mono">
          <div><strong>${fmtUSD2(totActiveStartVal)}</strong></div>
          <div class="row-sub mono">Basis: ${fmtUSD2(totActiveStartBasis)} <span style="color:#34d399;">(+${fmtUSD2(totGain)}, +${totGainPct.toFixed(1)}%)</span></div>
        </td>
        <td class="num mdl-in-td">
          <span class="mdl-in-capsule mono has-tip" style="cursor:default;" data-tip="Value-weighted average annual return across active Day-0 holdings">
            <span class="mdl-in-capsule-fill" style="width:${wFillPct.toFixed(1)}%;"></span>
            <span class="mdl-in-capsule-txt">${wSign}${wGrowthPct.toFixed(1)}%</span>
          </span>
        </td>
        <td class="mono muted" style="font-weight:500;">
          <div>Gross Sold: <strong style="color:var(--text);">${fmtUSD0(simCumGrossSold)}</strong></div>
          <div style="margin-top:2px;">Net Spend: <strong style="color:#34d399;">${fmtUSD0(simCumNetProceeds)}</strong></div>
        </td>
        <td class="num mono" style="color:#fb7185;">
          <div>Total Tax: -${fmtUSD0(simCumTaxPaid)}</div>
          <div class="row-sub mono" style="color:#fb7185;">Eff. Tax: ${cumEffTaxPct.toFixed(1)}%</div>
        </td>
      </tr>`;
    }

    function clampSimViewport(s, e) {
      const total = SIM_BARS.length;
      if (total <= MIN_SIM_VISIBLE_BARS) return [0, Math.max(0, total - 1)];
      let span = Math.max(MIN_SIM_VISIBLE_BARS - 1, Math.min(total - 1, e - s));
      let ns = s;
      let ne = s + span;
      if (ns < 0) { ns = 0; ne = span; }
      if (ne > total - 1) { ne = total - 1; ns = ne - span; }
      return [ns, ne];
    }

    function applySimPreset(preset) {
      simActivePreset = preset;
      document.querySelectorAll("[data-sim-preset]").forEach(b => {
        b.classList.toggle("active", b.dataset.simPreset === preset);
      });
      if (!SIM_BARS.length) recomputeSimBars();
      const maxIdx = Math.max(0, SIM_BARS.length - 1);
      if (preset === "10y") {
        [simViewStart, simViewEnd] = clampSimViewport(0, Math.min(maxIdx, 120));
      } else if (preset === "20y") {
        [simViewStart, simViewEnd] = clampSimViewport(0, Math.min(maxIdx, 240));
      } else if (preset === "30y") {
        [simViewStart, simViewEnd] = clampSimViewport(0, Math.min(maxIdx, 360));
      } else {
        [simViewStart, simViewEnd] = [0, maxIdx];
      }
      renderSimChartAndNav();
    }

    function _expandBandToLastResult(band, nBars) {
      if (!Array.isArray(band) || band.length === 0 || nBars <= 0) return null;
      const out = { p10: [], p25: [], p50: [], p75: [], p90: [] };
      const qs = ["p10", "p25", "p50", "p75", "p90"];
      let bIdx = 0;
      for (let i = 0; i < nBars; i++) {
        while (bIdx < band.length - 2 && band[bIdx + 1].monthIdx < i) bIdx++;
        const a = band[bIdx];
        const b = band[Math.min(band.length - 1, bIdx + 1)];
        const f = b.monthIdx > a.monthIdx ? Math.max(0, Math.min(1, (i - a.monthIdx) / (b.monthIdx - a.monthIdx))) : 0;
        for (const q of qs) {
          const va = (a.nominal && a.nominal[q]) || 0;
          const vb = (b.nominal && b.nominal[q]) || 0;
          out[q].push(va + (vb - va) * f);
        }
      }
      return out;
    }

    // === Monte Carlo overlay compute (cached + debounced, 1:1 seed=1 parity with simulator.py) ===
    // === Progressive time-sliced Monte Carlo & Historical Cohort scheduler + active control border animation ===
    let _mcResult = null, _mcResultSig = null;
    let _mcProgressiveResult = null, _mcProgressiveSig = null;
    let MC_LAST_RESULT = null;
    const SIM_MC_SEED = 1; // Shared 32-bit mulberry32 seed matching simulator.py default seed=1

    let _cohortResult = null, _cohortResultSig = null;
    let _cohortProgressiveResult = null, _cohortProgressiveSig = null;
    let HIST_LAST_RESULT = null;

    let _activeSimControlEl = null;
    let _simControlDetTimer = null;
    let _progJobSeq = 0;
    let _progTimer = null;
    let _progRaf = 0;
    let _progKind = null;
    let _progSig = null;
    let _progStepper = null;
    let _progStartMs = 0;
    let _lastProgDrawMs = 0;
    let _progYieldButtonPaint = false;

    function _simRaf(cb) {
      return (typeof requestAnimationFrame === "function") ? requestAnimationFrame(cb) : setTimeout(cb, 16);
    }
    function _simCancelRaf(id) {
      if (!id) return;
      if (typeof cancelAnimationFrame === "function") cancelAnimationFrame(id);
      else clearTimeout(id);
    }

    const _SIM_PROGRESS_SURFACE_IDS = [
      "sim-kpi-card-inheritance",
      "sim-kpi-card-solvency",
      "sim-kpi-card-taxdrag",
      "sim-main-panel",
    ];

    function setSimSurfacesComputing(on) {
      if (typeof document === "undefined") return;
      const active = Boolean(on);
      _SIM_PROGRESS_SURFACE_IDS.forEach(id => {
        const el = document.getElementById(id);
        if (el && el.classList) el.classList.toggle("sim-surface-computing", active);
      });
    }

    function clearActiveSimComputingControl() {
      if (_simControlDetTimer) {
        clearTimeout(_simControlDetTimer);
        _simControlDetTimer = null;
      }
      if (_activeSimControlEl && _activeSimControlEl.classList) {
        _activeSimControlEl.classList.remove("sim-computing");
      }
      _activeSimControlEl = null;
      if (typeof document !== "undefined" && document.querySelectorAll) {
        document.querySelectorAll(".sim-computing").forEach(el => {
          if (el && el.classList) el.classList.remove("sim-computing");
        });
      }
      if (!_progKind) setSimSurfacesComputing(false);
    }

    function cancelSimProgressiveJob(clearControl = false) {
      _progJobSeq++;
      if (_progTimer) {
        clearTimeout(_progTimer);
        _progTimer = null;
      }
      if (_progRaf) {
        _simCancelRaf(_progRaf);
        _progRaf = 0;
      }
      _progKind = null;
      _progSig = null;
      _progStepper = null;
      if (clearControl) {
        setSimSurfacesComputing(false);
        clearActiveSimComputingControl();
      }
    }

    function setActiveSimComputingControl(elOrId, isButtonClick = false) {
      if (typeof window !== "undefined" && !window._simBootCompleted) return;
      if (typeof _suppressDirtyCheck !== "undefined" && _suppressDirtyCheck) return;
      const el = typeof elOrId === "string" ? document.getElementById(elOrId) : elOrId;
      if (_simControlDetTimer) {
        clearTimeout(_simControlDetTimer);
        _simControlDetTimer = null;
      }
      if (_activeSimControlEl && _activeSimControlEl !== el && _activeSimControlEl.classList) {
        _activeSimControlEl.classList.remove("sim-computing");
      }
      _activeSimControlEl = el || null;
      _progYieldButtonPaint = Boolean(isButtonClick);
      if (_activeSimControlEl && _activeSimControlEl.classList) {
        _activeSimControlEl.classList.add("sim-computing");
      }
      setSimSurfacesComputing(true);
      // If active model is deterministic (Simple / Early Crash), cancel any lingering MC/Cohort job and settle shortly
      if (!simMcActive() && !simCohortActive()) {
        cancelSimProgressiveJob(false);
        _simControlDetTimer = setTimeout(() => {
          _simControlDetTimer = null;
          clearActiveSimComputingControl();
        }, 180);
      }
    }
    if (typeof window !== "undefined") {
      window.setActiveSimComputingControl = setActiveSimComputingControl;
      window.clearActiveSimComputingControl = clearActiveSimComputingControl;
      window.cancelSimProgressiveJob = cancelSimProgressiveJob;
    }

    function simMcActive() { return simActiveModel === "range"; }
    function simCohortActive() { return simActiveModel === "historical"; }

    function simMcInputsSig() {
      const totCur = Math.round((SIM_ASSETS || []).reduce((s, a) => s + (Number(a.current_value) || 0), 0) * 100);
      return JSON.stringify({
        totCur, hf: holdingForecasts, halfLives: holdingHalfLives, hyps: simHypotheticalAssets, burn: simAnnualBurn, inf: simInflationPct,
        unv: simIncludeUnvested, ssn: simIncludeSSN, sa: simIncludeScheduledActions,
        sal: simScheduledActions, order: simAssetOrder, jc: ssnInv0ClaimAge, yc: ssnInv1ClaimAge, cola: ssnColaPct,
        vol: mcSingleStockVolPct, nvol: mcBroadEquityVolPct, corr: mcSingleStockCorr, rm: mcReturnModel, sims: mcSimulations,
        thr: mcSuccessThresholdUsd, opt: simOptimismMultiplier, bs: simBearShield,
        mfj: simMarriedFilingJointly, fl: simMinTaxBracketFloor, fees: holdingFeePct, tr: PROJECT_CFG.tax_rates,
        r1: simInvestor1RetirementAge, r2: simInvestor2RetirementAge, wp: buildWorkPlanConfig(),
      });
    }
    function _currentMcParams() {
      return {
        singleStockVolPct: mcSingleStockVolPct, broadEquityVolPct: mcBroadEquityVolPct, singleStockCorr: mcSingleStockCorr,
        googVolPct: mcSingleStockVolPct, nonGoogVolPct: mcBroadEquityVolPct, googRestCorr: mcSingleStockCorr,
        returnModel: mcReturnModel, simulations: mcSimulations, successThresholdUsd: mcSuccessThresholdUsd,
        histPairs: (window.MC_HIST_PAIRS || []),
      };
    }
    function _mcStatus(html) { const el = document.getElementById("sim-mc-status"); if (el) el.innerHTML = html; }

    function _cohortOpts() {
      return {
        centerMode: cohortCenterMode, inflationMode: cohortInflationMode,
        startYearFrom: cohortStartFrom < 0 ? undefined : cohortStartFrom,
        spotlight: cohortSpotlight.slice(),
      };
    }
    function cohortInputsSig() {
      const totCur = Math.round((SIM_ASSETS || []).reduce((s, a) => s + (Number(a.current_value) || 0), 0) * 100);
      return JSON.stringify({
        totCur, hf: holdingForecasts, halfLives: holdingHalfLives, hyps: simHypotheticalAssets, burn: simAnnualBurn, inf: simInflationPct,
        unv: simIncludeUnvested, ssn: simIncludeSSN, sa: simIncludeScheduledActions,
        sal: simScheduledActions, order: simAssetOrder, jc: ssnInv0ClaimAge, yc: ssnInv1ClaimAge, cola: ssnColaPct,
        cm: cohortCenterMode, im: cohortInflationMode, from: cohortStartFrom, opt: simOptimismMultiplier, bs: simBearShield,
        mfj: simMarriedFilingJointly, fl: simMinTaxBracketFloor, fees: holdingFeePct, tr: PROJECT_CFG.tax_rates,
        r1: simInvestor1RetirementAge, r2: simInvestor2RetirementAge, wp: buildWorkPlanConfig(),
      });
    }
    function _cohortStatus(html) { const el = document.getElementById("sim-cohort-status"); if (el) el.innerHTML = html; }

    function getMcResult() {
      const sig = simMcInputsSig();
      if (_mcResult && _mcResultSig === sig) return _mcResult;
      const res = runMonteCarlo(_currentMcParams(), SIM_MC_SEED);
      _mcResult = res; _mcResultSig = sig;
      _mcProgressiveResult = null; _mcProgressiveSig = null;
      MC_LAST_RESULT = _expandBandToLastResult(res.band, SIM_BARS.length);
      return res;
    }

    function getCohortResult() {
      const sig = cohortInputsSig();
      if (_cohortResult && _cohortResultSig === sig) return _cohortResult;
      const res = runHistoricalCohorts(_cohortOpts());
      _cohortResult = res; _cohortResultSig = sig;
      _cohortProgressiveResult = null; _cohortProgressiveSig = null;
      HIST_LAST_RESULT = res.available ? _expandBandToLastResult(res.band, SIM_BARS.length) : null;
      return res;
    }

    function getLiveMcDisplayResult() {
      if (!simMcActive()) return null;
      const sig = simMcInputsSig();
      if (_mcResult && _mcResultSig === sig) return _mcResult;
      if (_mcProgressiveResult && _mcProgressiveSig === sig) return _mcProgressiveResult;
      return _mcProgressiveResult || _mcResult || null;
    }

    function getLiveCohortDisplayResult() {
      if (!simCohortActive()) return null;
      const sig = cohortInputsSig();
      if (_cohortResult && _cohortResultSig === sig) return _cohortResult;
      if (_cohortProgressiveResult && _cohortProgressiveSig === sig) return _cohortProgressiveResult;
      const fallback = _cohortProgressiveResult || _cohortResult || null;
      return (fallback && fallback.available) ? fallback : null;
    }

    function _scheduleProgressiveSim(kind) {
      const isMc = kind === "mc";
      const sig = isMc ? simMcInputsSig() : cohortInputsSig();
      if (isMc && _mcResult && _mcResultSig === sig) {
        if (_progKind) cancelSimProgressiveJob(false);
        if (_activeSimControlEl && !_simControlDetTimer) {
          _simControlDetTimer = setTimeout(() => { _simControlDetTimer = null; clearActiveSimComputingControl(); }, 140);
        } else if (!_activeSimControlEl) {
          setSimSurfacesComputing(false);
        }
        return;
      }
      if (!isMc && _cohortResult && _cohortResultSig === sig) {
        if (_progKind) cancelSimProgressiveJob(false);
        if (_activeSimControlEl && !_simControlDetTimer) {
          _simControlDetTimer = setTimeout(() => { _simControlDetTimer = null; clearActiveSimComputingControl(); }, 140);
        } else if (!_activeSimControlEl) {
          setSimSurfacesComputing(false);
        }
        return;
      }
      if (_progKind === kind && _progSig === sig && (_progTimer || _progRaf || _progStepper)) {
        return;
      }

      // Discard any superseded simulation immediately and animate all output surfaces responding to simulation progress
      cancelSimProgressiveJob(false);
      const jobId = ++_progJobSeq;
      _progKind = kind;
      _progSig = sig;
      _progStepper = null;
      _progStartMs = _simNowMs();
      const yieldButtonPaint = _progYieldButtonPaint;
      _progYieldButtonPaint = false;
      setSimSurfacesComputing(true);

      if (isMc) {
        _mcStatus(`<span class="mc-spin"></span>Computing 0 / ${mcSimulations.toLocaleString("en-US")} paths…`);
      } else {
        _cohortStatus(`<span class="mc-spin"></span>Replaying historical cohorts…`);
      }

      let sliceNum = 0;
      function runSlice(isFirstSlice) {
        _progTimer = null;
        _progRaf = 0;
        if (jobId !== _progJobSeq) return;
        if (isMc ? (!simMcActive() || simMcInputsSig() !== sig) : (!simCohortActive() || cohortInputsSig() !== sig)) {
          return;
        }
        if (!_progStepper) {
          _progStepper = isMc
            ? createMonteCarloStepper(_currentMcParams(), SIM_MC_SEED)
            : createHistoricalCohortStepper(_cohortOpts());
        }
        if (!isMc && !_progStepper.available) {
          const emptyRes = { available: false, cohorts: [], band: [], named: [] };
          _cohortResult = emptyRes;
          _cohortResultSig = sig;
          _cohortProgressiveResult = null;
          _cohortProgressiveSig = null;
          HIST_LAST_RESULT = null;
          _progKind = null;
          _progSig = null;
          _progStepper = null;
          _cohortStatus(`Historical market return dataset is unavailable for this installation.`);
          clearActiveSimComputingControl();
          renderSimChartAndNav();
          updateSimSummaryDOM();
          return;
        }

        function commitProgSnapshot() {
          const snap = _progStepper.buildResult(false);
          if (isMc) {
            _mcProgressiveResult = snap;
            _mcProgressiveSig = sig;
            MC_LAST_RESULT = _expandBandToLastResult(snap.band, SIM_BARS.length);
            _mcStatus(`<span class="mc-spin"></span>Refining ${snap.n_paths.toLocaleString("en-US")} / ${snap.total_paths.toLocaleString("en-US")} paths…`);
          } else {
            _cohortProgressiveResult = snap;
            _cohortProgressiveSig = sig;
            HIST_LAST_RESULT = _expandBandToLastResult(snap.band, SIM_BARS.length);
            _cohortStatus(`<span class="mc-spin"></span>Refining ${snap.n_cohorts} / ${snap.total_cohorts} cohorts…`);
          }
          _lastProgDrawMs = _simNowMs();
          setSimSurfacesComputing(true);
          if (_activeSimControlEl && _activeSimControlEl.classList) {
            _activeSimControlEl.classList.add("sim-computing");
          }
          renderSimChartAndNav();
          updateSimSummaryDOM();
        }

        // Run interactive progressive draws on their own ~10ms turn every ~100ms instead of stacking
        // on top of a compute slice, and cap compute slices at 18ms + 10ms yield (>=30 fps paints).
        if (!isFirstSlice && sliceNum > 0 && (_simNowMs() - _lastProgDrawMs >= 100)) {
          commitProgSnapshot();
          _progTimer = setTimeout(() => runSlice(false), 10);
          return;
        }

        const budgetMs = isFirstSlice ? 12 : 18;
        const minItems = 1;
        sliceNum++;
        const done = _progStepper.stepSlice(budgetMs, minItems);

        if (!done) {
          if (isFirstSlice) {
            commitProgSnapshot();
          }
          // Yield ~10ms between <=18ms compute slices so border animations paint at >=30 fps independently of ~100ms graph updates
          _progTimer = setTimeout(() => runSlice(false), 10);
          return;
        }

        // 100% of paths/cohorts completed — finalize result + _repBars and stop active widget animation
        const finalRes = _progStepper.buildResult(true);
        const elapsedMs = Math.max(1, Math.round(_simNowMs() - _progStartMs));
        _progKind = null;
        _progSig = null;
        _progStepper = null;
        if (isMc) {
          _mcResult = finalRes;
          _mcResultSig = sig;
          _mcProgressiveResult = null;
          _mcProgressiveSig = null;
          MC_LAST_RESULT = _expandBandToLastResult(finalRes.band, SIM_BARS.length);
          _mcStatus(`${finalRes.n_paths.toLocaleString("en-US")} paths &bull; ${finalRes.model === "historical" ? "Historical bootstrap" : "7-Risk-Class Normal"} model &bull; ${elapsedMs} ms`);
        } else {
          _cohortResult = finalRes;
          _cohortResultSig = sig;
          _cohortProgressiveResult = null;
          _cohortProgressiveSig = null;
          HIST_LAST_RESULT = finalRes.available ? _expandBandToLastResult(finalRes.band, SIM_BARS.length) : null;
          const centerLbl = finalRes.center_mode === "anchor" ? "anchored" : "raw";
          const inflLbl = finalRes.inflation_mode === "real_cpi" ? "real CPI" : "flat inflation";
          _cohortStatus(`${finalRes.n_cohorts} cohorts (${finalRes.data_range[0]}–${finalRes.data_range[1]}) &bull; ${centerLbl} returns &bull; ${inflLbl} &bull; ${elapsedMs} ms`);
        }
        _lastProgDrawMs = _simNowMs();
        clearActiveSimComputingControl();
        renderSimChartAndNav();
        updateSimSummaryDOM();
      }

      if (yieldButtonPaint) {
        // Yield 16ms so newly selected button + border animation paints immediately on frame 1 before Slice 1 runs
        _progTimer = setTimeout(() => runSlice(true), 16);
      } else {
        // Adaptive <100ms drag throttle: draw immediately if >=60ms since last progressive draw, else wait until the 60ms mark
        const sinceLast = _simNowMs() - _lastProgDrawMs;
        const delayMs = sinceLast >= 60 ? 0 : Math.max(4, Math.round(60 - sinceLast));
        _progTimer = setTimeout(() => runSlice(true), delayMs);
      }
    }

    function ensureSimMcResult() {
      if (!simMcActive()) return;
      _scheduleProgressiveSim("mc");
    }
    function scheduleSimMcCompute() {
      ensureSimMcResult();
    }
    function ensureSimCohortResult() {
      if (!simCohortActive()) return;
      _scheduleProgressiveSim("cohort");
    }

    function fmtSimAxisUSD(v) {
      if (v === 0) return "$0";
      if (v >= 1e9) {
        const b = v / 1e9;
        return "$" + (Math.abs(b * 10 - Math.round(b * 10)) < 1e-4 ? b.toFixed(1) : b.toFixed(2)).replace(/\.0$/, "") + "B";
      }
      if (v >= 1e6) {
        const m = v / 1e6;
        return "$" + (Math.abs(m * 10 - Math.round(m * 10)) < 1e-4 ? m.toFixed(1) : m.toFixed(2)).replace(/\.0$/, "") + "M";
      }
      return "$" + Math.round(v / 1000).toLocaleString("en-US") + "k";
    }

    function getSimTickStyle(v, isLog, normY) {
      if (!isLog) {
        return {
          textColor: v === 0 ? "#cbd5e1" : "#94a3b8",
          fontSize: 11,
          fontWeight: v === 0 ? 600 : 500,
          gridStroke: v === 0 ? "#334155" : "#1e293b",
          gridDash: "none",
          gridWidth: v === 0 ? 1.5 : 1,
        };
      }
      // Logarithmic scale: near-term reality zone starts bright white/slate-50,
      // and smoothly fades into darker, subtler grays as values trail off into rarefied multi-hundred million / billion territory.
      const t = Math.min(1.0, Math.max(0, normY));
      let r, g, b, weight, sz;
      if (t < 0.35) {
        const u = t / 0.35;
        r = Math.round(248 - u * 20);
        g = Math.round(250 - u * 16);
        b = Math.round(252 - u * 10);
        weight = 700;
        sz = 11.5;
      } else if (t < 0.65) {
        const u = (t - 0.35) / 0.30;
        r = Math.round(228 - u * 76);
        g = Math.round(234 - u * 68);
        b = Math.round(242 - u * 56);
        weight = 600;
        sz = 11.0;
      } else {
        const u = (t - 0.65) / 0.35;
        r = Math.round(152 - u * 80);
        g = Math.round(166 - u * 82);
        b = Math.round(186 - u * 82);
        weight = u > 0.5 ? 400 : 500;
        sz = Math.max(9.6, 10.8 - u * 1.2);
      }
      const textColor = `rgb(${r},${g},${b})`;
      const gridAlpha = Math.max(0.10, 0.38 - t * 0.22);
      const gridStroke = v === 0 ? "#475569" : `rgba(${r},${g},${b},${gridAlpha.toFixed(2)})`;
      const gridDash = t > 0.55 ? "3,3" : "none";
      const gridWidth = v === 0 ? 1.5 : (t < 0.35 ? 1.0 : 0.8);
      return { textColor, fontSize: sz, fontWeight: weight, gridStroke, gridDash, gridWidth };
    }

    function renderSimChartAndNav() {
      const svg = document.getElementById("sim-svg");
      if (!svg) return;
      if (typeof updateSimBaselineSummary === "function") updateSimBaselineSummary();
      updateSimOverridesSummary();
      if (!SIM_BARS.length) recomputeSimBars();
      if (simMcActive()) ensureSimMcResult();
      if (simCohortActive()) ensureSimCohortResult();
      const mc = getLiveMcDisplayResult();
      const cohort = getLiveCohortDisplayResult();

      const assetMap = Object.fromEntries(SIM_ASSETS.map(a => [a.id, a]));
      const iStart = Math.max(0, Math.floor(simViewStart));
      const iEnd = Math.min(SIM_BARS.length - 1, Math.ceil(simViewEnd));
      const visible = SIM_BARS.slice(iStart, iEnd + 1);
      const spanSlots = Math.max(1, (simViewEnd - simViewStart) + 1);
      const approxN = Math.round(spanSlots);

      const { W, H, padL, padR, padT, padB } = CHART_GEOM;
      const plotW = W - padL - padR;
      const plotH = H - padT - padB;
      const axisY = padT + plotH;

      window._simBarGeom = {};

      let maxVal = 1;
      visible.forEach(b => {
        const tv = simVal(b, b.totalVal);
        if (tv > maxVal) maxVal = tv;
      });
      // Monte Carlo band can rise above the single-path backbone — grow the axis to fit its p90.
      if (mc) {
        mc.band.forEach(cp => {
          if (cp.monthIdx < iStart || cp.monthIdx > iEnd) return;
          const v = simDollarMode === "real" ? cp.real.p90 : cp.nominal.p90;
          if (v > maxVal) maxVal = v;
        });
      }
      // Historical cohort band likewise — fit its p90 (the best cohorts can rise very high).
      if (cohort) {
        cohort.band.forEach(cp => {
          if (cp.monthIdx < iStart || cp.monthIdx > iEnd) return;
          const v = simDollarMode === "real" ? cp.real.p90 : cp.nominal.p90;
          if (v > maxVal) maxVal = v;
        });
      }

      const firstBar = visible[0] || SIM_BARS[0];
      const startVal = firstBar ? simVal(firstBar, firstBar.totalVal) : 15.5e6;
      // In auto mode, engage log scale when maxVal rises above $60M or >= 3.2x the starting value
      const isLog = simScaleMode === "log" || (simScaleMode === "auto" && (maxVal > 60e6 || (startVal > 0 && maxVal / startVal >= 3.2)));

      const y0 = 2.5e6;
      let yMax;
      if (!isLog) {
        const ySteps = 5;
        yMax = niceAxisMax(maxVal, ySteps);
      } else {
        const logMilestones = [
          5e6, 10e6, 20e6, 30e6, 50e6, 75e6, 100e6, 150e6, 200e6, 250e6, 300e6, 500e6,
          750e6, 1e9, 1.5e9, 2e9, 3e9, 5e9, 10e9, 20e9, 50e9
        ];
        yMax = logMilestones.find(m => m >= maxVal * 1.04) || (Math.ceil(maxVal / 1e9) * 1e9);
      }

      function valToNorm(v) {
        const val = Math.max(0, v || 0);
        if (!isLog) {
          return yMax > 0 ? val / yMax : 0;
        }
        return Math.log10(1 + val / y0) / Math.log10(1 + yMax / y0);
      }

      function valToY(v) {
        const norm = Math.min(1.0, Math.max(0, valToNorm(v)));
        return axisY - norm * plotH;
      }

      let ticks = [];
      if (!isLog) {
        const ySteps = 5;
        for (let s = 0; s <= ySteps; s++) {
          ticks.push((yMax / ySteps) * s);
        }
      } else {
        const candidates = [
          0, 1e6, 2.5e6, 5e6, 10e6, 25e6, 50e6, 100e6, 250e6, 500e6,
          1e9, 2e9, 3e9, 5e9, 10e9, 20e9, 50e9
        ];
        ticks = [0];
        let lastY = valToY(0);
        for (const c of candidates) {
          if (c <= 0 || c > yMax) continue;
          const yPos = valToY(c);
          if (lastY - yPos >= 22) {
            ticks.push(c);
            lastY = yPos;
          }
        }
        const maxYPos = valToY(yMax);
        if (lastY - maxYPos >= 16) {
          ticks.push(yMax);
        } else if (ticks.length > 1 && maxYPos < lastY) {
          ticks[ticks.length - 1] = yMax;
        }
      }

      let parts = [];
      parts.push(`<defs>
        <clipPath id="sim-plot-clip">
          <rect x="${padL}" y="0" width="${plotW}" height="${H}"></rect>
        </clipPath>
      </defs>`);

      const scaleBadge = isLog
        ? `<text x="${padL}" y="${padT - 6}" fill="#64748b" font-size="9.5" font-weight="600" font-family="ui-monospace, 'SF Mono', monospace" letter-spacing="0.04em">LOG SCALE &bull; EXPANDED NEAR-TERM</text>`
        : `<text x="${padL}" y="${padT - 6}" fill="#64748b" font-size="9.5" font-weight="600" font-family="ui-monospace, 'SF Mono', monospace" letter-spacing="0.04em">LINEAR SCALE</text>`;
      parts.push(scaleBadge);

      ticks.forEach((v) => {
        const y = valToY(v);
        const norm = valToNorm(v);
        const style = getSimTickStyle(v, isLog, norm);
        const dashAttr = style.gridDash !== "none" ? ` stroke-dasharray="${style.gridDash}"` : "";
        parts.push(`<line x1="${padL}" y1="${y.toFixed(1)}" x2="${W - padR}" y2="${y.toFixed(1)}" stroke="${style.gridStroke}" stroke-width="${style.gridWidth}"${dashAttr}/>`);
        parts.push(`<text x="${padL - 9}" y="${(y + 4).toFixed(1)}" text-anchor="end" fill="${style.textColor}" font-size="${style.fontSize}" font-weight="${style.fontWeight}" font-family="ui-monospace, 'SF Mono', 'Cascadia Code', Menlo, monospace" style="font-variant-numeric:tabular-nums;">${fmtSimAxisUSD(v)}</text>`);
      });

      const slotW = plotW / spanSlots;
      const barW = Math.max(1.1, Math.min(34, slotW * (approxN > 180 ? 0.88 : 0.76)));
      const labelStride = Math.max(1, Math.ceil(approxN / 13));

      let clipped = [];
      clipped.push(`<rect id="sim-focus-slot-bg" x="0" y="${padT}" width="0" height="${plotH}" fill="rgba(56, 189, 248, 0.14)" display="none" pointer-events="none"/>`);

      let hitRects = [];

      visible.forEach((b, localIdx) => {
        const globalIdx = iStart + localIdx;
        const slotX = padL + (globalIdx - simViewStart) * slotW;
        const cx = slotX + 0.5 * slotW;
        const bx = cx - barW / 2;
        const scale = simBarScale(b);

        // Stack each asset from lowest liquidation priority at bottom up to Priority #1 at the top of the bar
        const aVals = b.assetVals || {};
        let cumVal = 0;
        for (let rIdx = simAssetOrder.length - 1; rIdx >= 0; rIdx--) {
          const aid = simAssetOrder[rIdx];
          const a = assetMap[aid];
          if (!a || (a.is_unvested_goog && !simIncludeUnvested)) continue;
          const v = (aVals[aid] || 0) * scale;
          if (v <= 0) continue;
          
          const nextVal = cumVal + v;
          const yBot = valToY(cumVal);
          const yTop = valToY(nextVal);
          const sliceH = Math.max(0.6, yBot - yTop);
          
          // In Monte Carlo mode the stacked bars become a faint "expected composition" backbone
          // behind the probabilistic band that is drawn on top.
          const op = (a.is_unvested_goog ? 0.75 : 0.93) * ((mc || cohort) ? 0.4 : 1.0);
          clipped.push(`<rect x="${bx.toFixed(2)}" y="${yTop.toFixed(2)}" width="${barW.toFixed(2)}" height="${sliceH.toFixed(2)}" fill="${a.color || '#38bdf8'}" opacity="${op.toFixed(2)}"/>`);
          cumVal = nextVal;
        }

        const topY = valToY(cumVal);
        const barH = Math.max(1, axisY - topY);
        window._simBarGeom[globalIdx] = { slotX, slotW, bx, topY, barW, barH, cx };

        if (globalIdx % labelStride === 0 && cx >= padL + 22 && cx <= W - padR - 22) {
          const lbl = approxN > 140 ? b.yearLbl : b.monthLbl;
          clipped.push(`<text x="${cx.toFixed(1)}" y="${H - 15}" text-anchor="middle" fill="#94a3b8" font-size="10.8" font-family="ui-monospace, monospace">${escHtml(lbl)}</text>`);
        }

        hitRects.push(`<rect x="${slotX.toFixed(2)}" y="${padT}" width="${slotW.toFixed(2)}" height="${plotH}" fill="transparent" onmousemove="showSimTooltip(event, ${globalIdx})" onmouseleave="hideTooltip()"/>`);
      });

      // === Monte Carlo band overlay (Range) — drawn on top of the faint bars ===
      if (mc) {
        const cps = mc.band.filter(cp => cp.monthIdx <= SIM_BARS.length - 1);
        const bx2 = (monthIdx) => padL + (monthIdx - simViewStart + 0.5) * slotW;
        const by = (val) => valToY(val);
        const dv = (cp, q) => (simDollarMode === "real" ? cp.real[q] : cp.nominal[q]);
        const ptsTop90 = cps.map(cp => `${bx2(cp.monthIdx).toFixed(1)},${by(dv(cp, "p90")).toFixed(1)}`);
        const ptsBot10 = cps.slice().reverse().map(cp => `${bx2(cp.monthIdx).toFixed(1)},${by(dv(cp, "p10")).toFixed(1)}`);
        const ptsTop75 = cps.map(cp => `${bx2(cp.monthIdx).toFixed(1)},${by(dv(cp, "p75")).toFixed(1)}`);
        const ptsBot25 = cps.slice().reverse().map(cp => `${bx2(cp.monthIdx).toFixed(1)},${by(dv(cp, "p25")).toFixed(1)}`);
        const med = cps.map(cp => `${bx2(cp.monthIdx).toFixed(1)},${by(dv(cp, "p50")).toFixed(1)}`).join(" ");
        clipped.push(`<polygon points="${ptsTop90.concat(ptsBot10).join(" ")}" fill="rgba(90,216,166,0.12)" stroke="none" pointer-events="none"/>`);
        clipped.push(`<polygon points="${ptsTop75.concat(ptsBot25).join(" ")}" fill="rgba(90,216,166,0.22)" stroke="none" pointer-events="none"/>`);
        clipped.push(`<polyline points="${ptsTop90.join(" ")}" fill="none" stroke="rgba(90,216,166,0.55)" stroke-width="1.2" stroke-dasharray="4,3" pointer-events="none"/>`);
        clipped.push(`<polyline points="${cps.map(cp => `${bx2(cp.monthIdx).toFixed(1)},${by(dv(cp, "p10")).toFixed(1)}`).join(" ")}" fill="none" stroke="rgba(90,216,166,0.55)" stroke-width="1.2" stroke-dasharray="4,3" pointer-events="none"/>`);
        clipped.push(`<polyline points="${med}" fill="none" stroke="#5AD8A6" stroke-width="2.2" pointer-events="none"/>`);
        cps.forEach(cp => {
          const cxp = bx2(cp.monthIdx), cyp = by(dv(cp, "p50"));
          if (cxp >= padL && cxp <= W - padR) clipped.push(`<circle cx="${cxp.toFixed(1)}" cy="${cyp.toFixed(1)}" r="1.9" fill="#5AD8A6" pointer-events="none"/>`);
        });
        const lastCp = cps.filter(cp => cp.monthIdx <= iEnd).slice(-1)[0] || cps[cps.length - 1];
        if (lastCp) {
          const lx = Math.min(W - padR - 86, bx2(lastCp.monthIdx) + 6);
          const lyTop = by(dv(lastCp, "p90"));
          clipped.push(`<text x="${lx.toFixed(1)}" y="${(lyTop - 4).toFixed(1)}" fill="#5AD8A6" font-size="10" font-weight="700" font-family="ui-monospace, monospace" pointer-events="none">p10–p90 range</text>`);
        }
      }

      // === Historical cohort overlay (spaghetti) — one line per start year on the faint bars ===
      if (cohort) {
        const bx2 = (monthIdx) => padL + (monthIdx - simViewStart + 0.5) * slotW;
        const by = (val) => valToY(val);
        const dvl = (pt) => (simDollarMode === "real" ? pt.real : pt.nominal);
        const COH = { worst: "#fb7185", best: "#34d399", med: "#e2e8f0" };
        // p10–p90 envelope across cohorts (sky-tinted, non-colliding with Category B Gold)
        const cps = cohort.band.filter(cp => cp.monthIdx <= SIM_BARS.length - 1);
        if (cps.length) {
          const dvq = (cp, q) => (simDollarMode === "real" ? cp.real[q] : cp.nominal[q]);
          const top90 = cps.map(cp => `${bx2(cp.monthIdx).toFixed(1)},${by(dvq(cp, "p90")).toFixed(1)}`);
          const bot10 = cps.slice().reverse().map(cp => `${bx2(cp.monthIdx).toFixed(1)},${by(dvq(cp, "p10")).toFixed(1)}`);
          clipped.push(`<polygon points="${top90.concat(bot10).join(" ")}" fill="rgba(56,189,248,0.10)" stroke="none" pointer-events="none"/>`);
        }
        const bestY = cohort.best_cohort ? cohort.best_cohort.startYear : null;
        const worstY = cohort.worst_cohort ? cohort.worst_cohort.startYear : null;
        const boldYears = new Set([bestY, worstY].filter(y => y != null));
        cohort.cohorts.forEach(c => {
          if (boldYears.has(c.startYear) || !c.line.length) return;
          const pts = c.line.filter(pt => pt.monthIdx <= SIM_BARS.length - 1).map(pt => `${bx2(pt.monthIdx).toFixed(1)},${by(dvl(pt)).toFixed(1)}`).join(" ");
          clipped.push(`<polyline points="${pts}" fill="none" stroke="rgba(148,163,184,0.22)" stroke-width="1" pointer-events="none"/>`);
        });
        if (cps.length) {
          const med = cps.map(cp => `${bx2(cp.monthIdx).toFixed(1)},${by(simDollarMode === "real" ? cp.real.p50 : cp.nominal.p50).toFixed(1)}`).join(" ");
          clipped.push(`<polyline points="${med}" fill="none" stroke="${COH.med}" stroke-width="2.2" pointer-events="none"/>`);
        }
        Array.from(boldYears).map(y => cohort.cohorts.find(c => c.startYear === y)).filter(c => c && c.line.length).forEach(c => {
          const depleted = c.depletedMonth != null;
          const col = c.startYear === worstY || depleted ? COH.worst : COH.best;
          const wide = 2.4;
          const pts = c.line.filter(pt => pt.monthIdx <= SIM_BARS.length - 1).map(pt => `${bx2(pt.monthIdx).toFixed(1)},${by(dvl(pt)).toFixed(1)}`).join(" ");
          clipped.push(`<polyline points="${pts}" fill="none" stroke="${col}" stroke-width="${wide}" pointer-events="none"/>`);
          const last = c.line[c.line.length - 1];
          const lxp = bx2(last.monthIdx), lyp = Math.max(padT + 9, Math.min(axisY - 2, by(dvl(last))));
          if (lxp >= padL && lxp <= W - padR) {
            if (depleted) clipped.push(`<circle cx="${bx2(c.depletedMonth).toFixed(1)}" cy="${axisY.toFixed(1)}" r="4" fill="${COH.worst}" pointer-events="none"/>`);
            const lbl = `${c.startYear}${depleted ? " ✗" : ""}`;
            const tx = Math.min(W - padR - 30, lxp + 5);
            clipped.push(`<text x="${tx.toFixed(1)}" y="${(lyp - 3).toFixed(1)}" fill="${col}" font-size="10" font-weight="700" font-family="ui-monospace, monospace" pointer-events="none">${lbl}</text>`);
          }
        });
      }

      // === Market Correction: shade Downfall + Bounce-Back windows and label the phases ===
      if (!mc && simActiveModel === "stress") {
        const sp = resolveStressSpec(simStressScenario);
        if (sp) {
          const dm = sp.down_months;
          const sOff = Math.max(0, (simDrawdownStartMonth || 1) - 1);
          const x0 = padL + (sOff - simViewStart) * slotW;
          const xDown = padL + ((sOff + dm) - simViewStart) * slotW;
          const xa = Math.max(padL, x0), xb = Math.min(W - padR, xDown);
          if (xb > xa) clipped.push(`<rect x="${xa.toFixed(1)}" y="${padT}" width="${(xb - xa).toFixed(1)}" height="${plotH}" fill="rgba(244,63,94,0.13)" pointer-events="none"/>`);

          const maxBounceM = Math.max(
            sp.broad_bounce_months < STRESS_NO_BOUNCE_MONTHS ? sp.broad_bounce_months : 0,
            sp.stock_bounce_months < STRESS_NO_BOUNCE_MONTHS ? sp.stock_bounce_months : 0
          );
          if (maxBounceM > 0 && (sp.shocks.single_stock < -1e-6 || sp.shocks.broad_equity < -1e-6)) {
            const xRecEnd = padL + ((sOff + dm + maxBounceM) - simViewStart) * slotW;
            const rxa = Math.max(padL, xDown), rxb = Math.min(W - padR, xRecEnd);
            if (rxb > rxa) clipped.push(`<rect x="${rxa.toFixed(1)}" y="${padT}" width="${(rxb - rxa).toFixed(1)}" height="${plotH}" fill="rgba(56,189,248,0.06)" pointer-events="none"/>`);
          }

          const gPct = Math.round(sp.shocks.single_stock * 100);
          const ngPct = Math.round(sp.shocks.broad_equity * 100);
          const downTxt = formatStressMonths(dm, false);
          const bbTxt = formatStressMonths(sp.broad_bounce_months, true);
          const sbTxt = formatStressMonths(sp.stock_bounce_months, true);
          const bounceSummary = (bbTxt === sbTxt) ? `Bounce ${bbTxt}` : `Bounce ${bbTxt} / ${sbTxt}`;
          const lx = Math.min(W - padR - 295, Math.max(padL + 4, x0 + 6));
          clipped.push(`<text x="${lx.toFixed(1)}" y="${(padT + 32).toFixed(1)}" fill="#fb7185" font-size="10.5" font-weight="700" font-family="ui-monospace, monospace" pointer-events="none">Down ${gPct}% / ${ngPct}% (${downTxt}) &bull; ${bounceSummary}</text>`);
        }
      }

      // Annual tick marks on the X axis at every year boundary
      for (let k = Math.max(0, iStart - 1); k < Math.min(SIM_BARS.length - 1, iEnd + 1); k++) {
        if (SIM_BARS[k].yearLbl !== SIM_BARS[k + 1].yearLbl) {
          const tickX = padL + ((k + 1) - simViewStart) * slotW;
          if (tickX >= padL - 1 && tickX <= W - padR + 1) {
            clipped.push(`<line x1="${tickX.toFixed(2)}" y1="${(axisY - 7).toFixed(1)}" x2="${tickX.toFixed(2)}" y2="${(axisY + 10).toFixed(1)}" stroke="#e2e8f0" stroke-width="2" stroke-linecap="round" pointer-events="none"/>`);
          }
        }
      }

      // Vertical milestone lines every 10 years (+10y, +20y, +30y, +40y)
      const placedTopTags = [];
      [120, 240, 360, 480].forEach(mIdx => {
        if (mIdx >= SIM_BARS.length) return;
        const mCx = padL + (mIdx - simViewStart + 0.5) * slotW;
        if (mCx >= padL && mCx <= W - padR) {
          const yrsNum = Math.round(mIdx / 12);
          clipped.push(`<line x1="${mCx.toFixed(1)}" y1="${padT}" x2="${mCx.toFixed(1)}" y2="${axisY}" stroke="#cbd5e1" stroke-width="1.4" stroke-dasharray="5,4" opacity="0.75" pointer-events="none"/>`);
          const tagW = 54;
          const tagX = Math.min(W - padR - tagW - 2, Math.max(padL + 2, mCx - tagW / 2));
          placedTopTags.push({ cx: tagX + tagW / 2, w: tagW, rowSlot: 0 });
          clipped.push(`<rect x="${tagX.toFixed(1)}" y="${padT + 2}" width="${tagW}" height="16" rx="4" fill="rgba(15, 23, 42, 0.9)" stroke="#64748b" stroke-width="1" pointer-events="none"/>`);
          clipped.push(`<text x="${(tagX + tagW / 2).toFixed(1)}" y="${padT + 13}" text-anchor="middle" fill="#e2e8f0" font-size="9.8" font-weight="700" font-family="ui-monospace, monospace" pointer-events="none">+${yrsNum} Yrs</text>`);
        }
      });

      // Teal / Sky dotted vertical lines and boxes for Investor milestone birthdays (non-colliding with Gold KPIs)
      const _msLbl0 = `${escHtml(INV0.name)}&#39;s ${INV0.milestone_age || 85}th`;
      const bdayMilestones = [];
      if (HAS_SECOND_INVESTOR && INV1) {
        const _msLbl1 = `${escHtml(INV1.name)}&#39;s ${INV1.milestone_age || 85}th`;
        bdayMilestones.push({
          label: _msLbl1,
          stroke: "#38bdf8",
          textFill: "#bae6fd",
          tagW: Math.max(76, Math.round((INV1.name.length + 7) * 6.5 + 14)),
          idx: SIM_BARS.findIndex((b, idx) => ageOnIso(INV1.birth_year, INV1.birth_month, INV1.birth_day, b.iso, idx) >= (INV1.milestone_age || 85))
        });
      }
      bdayMilestones.push({
        label: _msLbl0,
        stroke: "#2dd4bf",
        textFill: "#99f6e4",
        tagW: Math.max(76, Math.round((INV0.name.length + 7) * 6.5 + 14)),
        idx: SIM_BARS.findIndex((b, idx) => ageOnIso(INV0.birth_year, INV0.birth_month, INV0.birth_day, b.iso, idx) >= (INV0.milestone_age || 85))
      });
      const _chartWp = buildWorkPlanConfig();
      (_chartWp.workers || []).forEach(w => {
        const retIdx = SIM_BARS.findIndex((b, idx) => idx > 0 && b.ym >= w.ret_ym);
        if (retIdx > 0 && retIdx < SIM_BARS.length) {
          const retLbl = `${escHtml(w.name)} Retires (${w.retirement_age})`;
          bdayMilestones.push({
            label: retLbl,
            stroke: w.idx === 0 ? "#a78bfa" : "#34d399",
            textFill: w.idx === 0 ? "#ddd6fe" : "#a7f3d0",
            tagW: Math.max(88, Math.round((w.name.length + 13) * 6.3 + 14)),
            idx: retIdx,
          });
        }
      });
      bdayMilestones.forEach(bm => {
        if (bm.idx < 0 || bm.idx >= SIM_BARS.length) return;
        const bCx = padL + (bm.idx - simViewStart + 0.5) * slotW;
        if (bCx < padL || bCx > W - padR) return;
        clipped.push(`<line x1="${bCx.toFixed(1)}" y1="${padT}" x2="${bCx.toFixed(1)}" y2="${axisY}" stroke="${bm.stroke}" stroke-width="1.7" stroke-dasharray="3,3" opacity="0.95" pointer-events="none"/>`);
        const tagW = bm.tagW;
        const tagX = Math.min(W - padR - tagW - 2, Math.max(padL + 2, bCx - tagW / 2));
        const tagCx = tagX + tagW / 2;
        let rowSlot = 0;
        while (rowSlot < 3 && placedTopTags.some(p => p.rowSlot === rowSlot && Math.abs(p.cx - tagCx) < (p.w / 2 + tagW / 2 + 5))) {
          rowSlot++;
        }
        placedTopTags.push({ cx: tagCx, w: tagW, rowSlot });
        const tagY = padT + 2 + rowSlot * 19;
        clipped.push(`<rect x="${tagX.toFixed(1)}" y="${tagY}" width="${tagW}" height="16" rx="4" fill="rgba(15, 23, 42, 0.94)" stroke="${bm.stroke}" stroke-width="1.1" pointer-events="none"/>`);
        clipped.push(`<text x="${tagCx.toFixed(1)}" y="${tagY + 11}" text-anchor="middle" fill="${bm.textFill}" font-size="9.8" font-weight="700" font-family="ui-monospace, monospace" pointer-events="none">${bm.label}</text>`);
      });

      clipped.push(`<rect id="sim-focus-bar-ring" x="0" y="0" width="0" height="0" rx="1.5" fill="rgba(255, 255, 255, 0.14)" stroke="#ffffff" stroke-width="2" display="none" pointer-events="none"/>`);
      clipped.push(`<circle id="sim-focus-bar-dot" cx="0" cy="0" r="3.5" fill="#38bdf8" stroke="#ffffff" stroke-width="1.4" display="none" pointer-events="none"/>`);
      clipped.push(...hitRects);

      // Unified Scheduled Actions markers:
      // - One-time actions: vertical dashed lines + clickable pills (Cyan ⇄ Transfer, Emerald ↑ Deposit, Rose ↓ Withdrawal)
      // - Recurring actions: bottom horizontal span bars just above the X-axis
      if (simIncludeScheduledActions) {
        const placedSwTags = [];
        const ACT_STYLE = {
          transfer:   { stroke: "#22d3ee", bg: "rgba(8, 51, 68, 0.94)",   fg: "#a5f3fc", sym: "⇄" },
          deposit:    { stroke: "#10b981", bg: "rgba(6, 78, 59, 0.94)",   fg: "#a7f3d0", sym: "↑" },
          withdrawal: { stroke: "#f43f5e", bg: "rgba(76, 5, 25, 0.94)",   fg: "#fecdd3", sym: "↓" },
        };
        SIM_BARS.forEach((b, mIdx) => {
          const evs = (b.schedActionEvents || b.schedWithdrawEvents || []).filter(ev => (ev.cadence || "one_time") === "one_time");
          if (evs.length === 0) return;
          const wCx = padL + (mIdx - simViewStart + 0.5) * slotW;
          if (wCx < padL || wCx > W - padR) return;
          const firstEv = evs[0];
          const st = ACT_STYLE[firstEv.type] || ACT_STYLE.withdrawal;
          clipped.push(`<line x1="${wCx.toFixed(1)}" y1="${padT}" x2="${wCx.toFixed(1)}" y2="${axisY}" stroke="${st.stroke}" stroke-width="1.8" stroke-dasharray="4,3" opacity="0.95" pointer-events="none"/>`);

          const extraCnt = evs.length - 1;
          const rawCmt = `${st.sym} ${firstEv.comment || firstEv.type || "Action"}`;
          const maxChars = extraCnt > 0 ? 13 : 16;
          const truncCmt = (rawCmt.length > maxChars ? rawCmt.slice(0, maxChars - 1) + "…" : rawCmt) + (extraCnt > 0 ? ` (+${extraCnt})` : "");
          const tagW = Math.max(70, Math.min(128, Math.round(truncCmt.length * 6.2 + 16)));
          const tagX = Math.min(W - padR - tagW - 2, Math.max(padL + 2, wCx - tagW / 2));

          let rowSlot = 0;
          while (rowSlot < 5 && placedSwTags.some(p => p.rowSlot === rowSlot && Math.abs(p.cx - (tagX + tagW / 2)) < (p.w / 2 + tagW / 2 + 6))) {
            rowSlot++;
          }
          placedSwTags.push({ cx: tagX + tagW / 2, w: tagW, rowSlot });
          const tagY = padT + 40 + rowSlot * 20;

          const agesStr = formatSimFamilyAgesAtMonthIdx(mIdx);
          const tipLines = evs.map(ev =>
            `#${ev.rowNum} [${escHtml((ev.type || "withdrawal").toUpperCase())}] (${escHtml(b.fullLbl)}): ${fmtUSD0(ev.todayAmt)} Today's $ / ${fmtUSD0(ev.nomAmt)} Nominal $ &mdash; &ldquo;${escHtml(ev.comment || "")}&rdquo;`
          ).join(" &bull; ");
          const fullTip = `<div><strong>Scheduled Action</strong> &mdash; ${tipLines} (${escHtml(agesStr)})</div>`;
          const firstRowIdx = Math.max(0, (firstEv.rowNum || 1) - 1);

          clipped.push(
            `<g class="has-tip" data-tip="${escHtml(fullTip)}" style="cursor:pointer;" onclick="selectSimWithdrawalRow(${firstRowIdx})">` +
              `<rect x="${tagX.toFixed(1)}" y="${tagY}" width="${tagW}" height="17" rx="4" fill="${st.bg}" stroke="${st.stroke}" stroke-width="1.2"/>` +
              `<text x="${(tagX + tagW / 2).toFixed(1)}" y="${tagY + 12}" text-anchor="middle" fill="${st.fg}" font-size="9.8" font-weight="700" font-family="ui-monospace, monospace">${escHtml(truncCmt)}</text>` +
            `</g>`
          );
        });

        // Recurring Scheduled Action span bars along bottom of plot
        let recLane = 0;
        simScheduledActions.forEach((act, aIdx) => {
          if (!act || (act.cadence || "one_time") === "one_time") return;
          const sIdx = simYmToMonthIdx(act.start_ym || act.ym);
          const eIdx = act.end_ym ? simYmToMonthIdx(act.end_ym) : SIM_TOTAL_MONTHS;
          const x1 = Math.max(padL, padL + (sIdx - simViewStart) * slotW);
          const x2 = Math.min(W - padR, padL + (eIdx - simViewStart + 1) * slotW);
          if (x2 - x1 < 4) return;
          const st = ACT_STYLE[act.type] || ACT_STYLE.withdrawal;
          const barY = axisY - 12 - (recLane % 3) * 11;
          recLane++;
          const tip = `<div><strong>Recurring ${escHtml(act.cadence)} ${escHtml(act.type)}:</strong> &ldquo;${escHtml(act.comment || "")}&rdquo; (${escHtml(act.start_ym || "")} &rarr; ${escHtml(act.end_ym || "Yr 40")})</div>`;
          clipped.push(
            `<g class="has-tip" data-tip="${escHtml(tip)}" style="cursor:pointer;" onclick="selectSimWithdrawalRow(${aIdx})">` +
              `<rect x="${x1.toFixed(1)}" y="${barY.toFixed(1)}" width="${(x2 - x1).toFixed(1)}" height="9" rx="3" fill="${st.bg}" stroke="${st.stroke}" stroke-width="1" opacity="0.9"/>` +
            `</g>`
          );
        });
      }

      parts.push(`<g clip-path="url(#sim-plot-clip)">${clipped.join("")}</g>`);

      svg.innerHTML = parts.join("");
      if (typeof svg.matches === "function" && !svg.matches(":hover") && typeof hideTooltip === "function") {
        hideTooltip();
      }
      renderSimNavigator();
      renderSimLegend();
    }

    function renderSimNavigator() {
      const nav = document.getElementById("sim-nav-svg");
      const totalN = SIM_BARS.length;
      if (!nav || !totalN) return;

      const { W, H, padL, padR, padT, padB } = NAV_GEOM;
      const plotW = W - padL - padR;
      const plotH = H - padT - padB;

      let maxAll = 1;
      SIM_BARS.forEach(b => {
        const tv = simVal(b, b.totalVal);
        if (tv > maxAll) maxAll = tv;
      });

      const xFromIdx = idx => padL + (idx / Math.max(1, totalN - 1)) * plotW;
      let totPts = [`${padL},${padT + plotH}`];
      let googPts = [`${padL},${padT + plotH}`];

      const firstNavBar = SIM_BARS[0];
      const startNavVal = firstNavBar ? simVal(firstNavBar, firstNavBar.totalVal) : 15.5e6;
      const isNavLog = simScaleMode === "log" || (simScaleMode === "auto" && (maxAll > 60e6 || (startNavVal > 0 && maxAll / startNavVal >= 3.2)));
      const y0 = 2.5e6;
      const navCap = maxAll * 1.05;
      const normNav = (v) => {
        const val = Math.max(0, v || 0);
        if (!isNavLog) return navCap > 0 ? val / navCap : 0;
        return Math.log10(1 + val / y0) / Math.log10(1 + navCap / y0);
      };

      SIM_BARS.forEach((b, i) => {
        const x = xFromIdx(i);
        const yT = padT + plotH - Math.min(1.0, Math.max(0, normNav(simVal(b, b.totalVal)))) * plotH;
        const yG = padT + plotH - Math.min(1.0, Math.max(0, normNav(simVal(b, b.googVal)))) * plotH;
        totPts.push(`${x.toFixed(1)},${yT.toFixed(1)}`);
        googPts.push(`${x.toFixed(1)},${yG.toFixed(1)}`);
      });
      totPts.push(`${padL + plotW},${padT + plotH}`);
      googPts.push(`${padL + plotW},${padT + plotH}`);

      const winX1 = xFromIdx(simViewStart);
      const winX2 = xFromIdx(simViewEnd);
      const winW = Math.max(8, winX2 - winX1);

      let out = [];
      out.push(`<rect x="${padL}" y="${padT}" width="${plotW}" height="${plotH}" rx="4" fill="#0b1324" stroke="#1e293b"/>`);
      out.push(`<polygon points="${totPts.join(' ')}" fill="rgba(16, 185, 129, 0.34)"/>`);
      out.push(`<polygon points="${googPts.join(' ')}" fill="rgba(56, 189, 248, 0.38)"/>`);

      let prevYr = "";
      SIM_BARS.forEach((b, i) => {
        if (b.yearLbl !== prevYr) {
          prevYr = b.yearLbl;
          const yrNum = parseInt(b.yearLbl, 10);
          const x = xFromIdx(i);
          if (yrNum % 5 === 0) {
            out.push(`<line x1="${x.toFixed(1)}" y1="${padT}" x2="${x.toFixed(1)}" y2="${padT + plotH}" stroke="rgba(148,163,184,0.45)" stroke-width="1.2"/>`);
            out.push(`<text x="${(x + 4).toFixed(1)}" y="${H - 5}" fill="#f1f5f9" font-size="12" font-weight="700" font-family="ui-monospace, monospace">${b.yearLbl}</text>`);
          }
        }
      });

      if (HAS_SECOND_INVESTOR && INV1) {
        const inv1NavIdx = SIM_BARS.findIndex((b, idx) => ageOnIso(INV1.birth_year, INV1.birth_month, INV1.birth_day, b.iso, idx) >= (INV1.milestone_age || 85));
        if (inv1NavIdx >= 0 && inv1NavIdx < SIM_BARS.length) {
          const yNavX = xFromIdx(inv1NavIdx);
          out.push(`<line x1="${yNavX.toFixed(1)}" y1="${padT}" x2="${yNavX.toFixed(1)}" y2="${padT + plotH}" stroke="#38bdf8" stroke-width="1.4" stroke-dasharray="2,2"/>`);
        }
      }

      const inv0NavIdx = SIM_BARS.findIndex((b, idx) => ageOnIso(INV0.birth_year, INV0.birth_month, INV0.birth_day, b.iso, idx) >= (INV0.milestone_age || 85));
      if (inv0NavIdx >= 0 && inv0NavIdx < SIM_BARS.length) {
        const jNavX = xFromIdx(inv0NavIdx);
        out.push(`<line x1="${jNavX.toFixed(1)}" y1="${padT}" x2="${jNavX.toFixed(1)}" y2="${padT + plotH}" stroke="#2dd4bf" stroke-width="1.4" stroke-dasharray="2,2"/>`);
      }

      if (simIncludeScheduledActions) {
        SIM_BARS.forEach((b, mIdx) => {
          const evs = (b.schedActionEvents || b.schedWithdrawEvents || []).filter(ev => (ev.cadence || "one_time") === "one_time");
          if (evs.length > 0) {
            const swNavX = xFromIdx(mIdx);
            const t = evs[0].type || "withdrawal";
            const col = t === "transfer" ? "#22d3ee" : (t === "deposit" ? "#10b981" : "#f43f5e");
            out.push(`<line x1="${swNavX.toFixed(1)}" y1="${padT}" x2="${swNavX.toFixed(1)}" y2="${padT + plotH}" stroke="${col}" stroke-width="1.5" stroke-dasharray="2,2"/>`);
          }
        });
      }

      out.push(`<rect x="${padL}" y="${padT}" width="${Math.max(0, winX1 - padL).toFixed(1)}" height="${plotH}" fill="rgba(2, 6, 23, 0.58)" style="cursor:pointer;" data-sim-nav-action="jump"/>`);
      out.push(`<rect x="${(winX1 + winW).toFixed(1)}" y="${padT}" width="${Math.max(0, padL + plotW - (winX1 + winW)).toFixed(1)}" height="${plotH}" fill="rgba(2, 6, 23, 0.58)" style="cursor:pointer;" data-sim-nav-action="jump"/>`);

      out.push(`<rect x="${winX1.toFixed(1)}" y="${padT}" width="${winW.toFixed(1)}" height="${plotH}" fill="rgba(56, 189, 248, 0.16)" stroke="#38bdf8" stroke-width="1.4" style="cursor:grab;" data-sim-nav-action="move"/>`);
      out.push(`<rect x="${(winX1 - 4).toFixed(1)}" y="${padT + 6}" width="8" height="${plotH - 12}" rx="3" fill="#7dd3fc" style="cursor:ew-resize;" data-sim-nav-action="left"/>`);
      out.push(`<rect x="${(winX1 + winW - 4).toFixed(1)}" y="${padT + 6}" width="8" height="${plotH - 12}" rx="3" fill="#7dd3fc" style="cursor:ew-resize;" data-sim-nav-action="right"/>`);

      nav.innerHTML = out.join("");

      const sIdx = Math.max(0, Math.floor(simViewStart));
      const eIdx = Math.min(totalN - 1, Math.ceil(simViewEnd));
      document.getElementById("sim-nav-range-text").textContent =
        `${SIM_BARS[sIdx].fullLbl} (+${SIM_BARS[sIdx].yearsOut.toFixed(1)}y) → ${SIM_BARS[eIdx].fullLbl} (+${SIM_BARS[eIdx].yearsOut.toFixed(1)}y)`;
    }

    function renderSimLegend() {
      const leg = document.getElementById("sim-legend");
      if (!leg) return;
      const assetMap = Object.fromEntries(SIM_ASSETS.map(a => [a.id, a]));
      let rank = 0;
      const itemsHtml = simAssetOrder.map(id => {
        const a = assetMap[id];
        if (!a || (a.is_unvested_goog && !simIncludeUnvested)) return "";
        rank += 1;
        return `<span class="legend-item" title="#${rank} in Liquidation Order: ${escHtml(a.title)} (${fmtUSD0(a.current_value)})"><span class="swatch" style="background:${a.color || '#38bdf8'};"></span><span class="mono muted" style="font-size:10.5px;">#${rank}</span> ${escHtml(a.short_title)}</span>`;
      }).join("");

      let mcLegendHtml = "";
      const liveMcLeg = getLiveMcDisplayResult();
      if (liveMcLeg) {
        const sw = (bg, extra) => `<span class="swatch" style="background:${bg};${extra || ""}"></span>`;
        const pCnt = liveMcLeg.is_partial && liveMcLeg.total_paths
          ? `${liveMcLeg.n_paths.toLocaleString("en-US")}/${liveMcLeg.total_paths.toLocaleString("en-US")}`
          : liveMcLeg.n_paths.toLocaleString("en-US");
        mcLegendHtml =
          `<span class="legend-item" title="The median outcome across simulated futures.">${sw("#5AD8A6", "height:3px;border-radius:2px;")}<b style="color:#5AD8A6;">Median (p50)</b></span>` +
          `<span class="legend-item" title="The middle half of outcomes — 25th to 75th percentile.">${sw("rgba(90,216,166,0.55)")}p25&ndash;p75 (middle half)</span>` +
          `<span class="legend-item" title="The 10th-to-90th percentile range: 8 of every 10 simulated futures land inside this band.">${sw("rgba(90,216,166,0.22)")}p10&ndash;p90 range (dashed edges)</span>` +
          `<span class="legend-item" title="The faded stacked bars behind the band show the expected asset composition.">${sw("rgba(255,255,255,0.18)")}Faded bars: expected mix</span>` +
          `<span class="legend-item muted" style="width:100%;font-size:11.2px;margin-top:2px;border-bottom:1px solid rgba(38,51,77,0.65);padding-bottom:7px;margin-bottom:3px;">` +
            `${pCnt} Monte Carlo paths &bull; ${liveMcLeg.model === "historical" ? "Historical bootstrap" : "7-Risk-Class Normal"} model &bull; percentiles taken at each year-end</span>`;
      }

      let cohortLegendHtml = "";
      const liveCohLeg = getLiveCohortDisplayResult();
      if (liveCohLeg) {
        const co = liveCohLeg;
        const cCnt = co.is_partial && co.total_cohorts ? `${co.n_cohorts}/${co.total_cohorts}` : String(co.n_cohorts);
        const sw = (bg, extra) => `<span class="swatch" style="background:${bg};${extra || ""}"></span>`;
        cohortLegendHtml =
          `<span class="legend-item" title="One faint line per historical start year.">${sw("rgba(148,163,184,0.5)", "height:2px;")}each start year</span>` +
          `<span class="legend-item" title="The median start year at each point.">${sw("#e2e8f0", "height:3px;border-radius:2px;")}<b>median cohort</b></span>` +
          `<span class="legend-item" title="The best-ending cohort start year.">${sw("#34d399", "height:3px;border-radius:2px;")}best (${co.best_cohort ? co.best_cohort.startYear : "—"})</span>` +
          `<span class="legend-item" title="The worst start year; ✗ marks where it ran dry.">${sw("#fb7185", "height:3px;border-radius:2px;")}worst (${co.worst_cohort ? co.worst_cohort.startYear : "—"})</span>` +
          `<span class="legend-item muted" style="width:100%;font-size:11.2px;margin-top:2px;border-bottom:1px solid rgba(38,51,77,0.65);padding-bottom:7px;margin-bottom:3px;">` +
            `${cCnt} historical start years (${co.data_range[0]}–${co.data_range[1]}) &bull; ${co.center_mode === "anchor" ? "returns anchored to ◆ Proj. Growth setting" : "raw proxy returns"} &bull; ${co.inflation_mode === "real_cpi" ? "each cohort's real CPI" : "flat inflation"}</span>`;
      }

      leg.innerHTML = `
        ${mcLegendHtml}
        ${cohortLegendHtml}
        ${itemsHtml}
        <span class="legend-item muted" style="width:100%;font-size:11.2px;margin-top:2px;">Stacked top-to-bottom in Liquidation Priority Order.</span>
      `;
    }

    function ageOnIso(birthY, birthM, birthD, isoStr, barIdx = 0) {
      const yr = parseInt(isoStr.slice(0, 4), 10) || 2026;
      const mo = parseInt(isoStr.slice(5, 7), 10) || 1;
      const dy = parseInt(isoStr.slice(8, 10), 10) || 1;
      let age = yr - birthY;
      if (mo < birthM || (barIdx === 0 && mo === birthM && dy < birthD)) age -= 1;
      return age;
    }

    function showSimTooltip(evt, idx) {
      const b = SIM_BARS[idx];
      if (!b) return;
      highlightBarInSvg(window._simBarGeom, idx, "sim-");

      const assetMap = Object.fromEntries(SIM_ASSETS.map(a => [a.id, a]));
      const tip = document.getElementById("chart-tooltip");
      const isReal = simDollarMode === "real";
      const scale = simBarScale(b);
      const dispTotal = b.totalVal * scale;
      const dispGrowth = b.growthGain * scale;
      const dispTax = b.monthlyTax * scale;
      const dispGrossSold = b.monthlyGrossSold * scale;

      // (A) Date & Age of all configured family members
      const familyAgesHtml = SIM_FAMILY_BIRTHDAYS.map(p => {
        const age = ageOnIso(p.y, p.m, p.d, b.iso, idx);
        const ageStr = age >= 0 ? String(age) : "<0";
        return `<span>${escHtml(p.name)}: <strong style="color:var(--text);">${ageStr}</strong></span>`;
      }).join(' <span class="muted">&bull;</span> ');

      // (B) Current Portfolio Value, P10/P50/P90 (if applicable), and 1-Month Change
      const getBandPctAt = (barIdx) => {
        if (barIdx <= 0) return null;
        const liveMcTip = getLiveMcDisplayResult();
        if (liveMcTip) {
          const band = liveMcTip.band || [];
          if (!band.length) return null;
          let lo = band[0], hi = band[band.length - 1], pctAt;
          if (barIdx <= lo.monthIdx) pctAt = lo;
          else if (barIdx >= hi.monthIdx) pctAt = hi;
          else {
            let i = 0; while (i < band.length - 1 && band[i + 1].monthIdx < barIdx) i++;
            const a2 = band[i], b2 = band[i + 1];
            const f = (barIdx - a2.monthIdx) / (b2.monthIdx - a2.monthIdx);
            const sp = isReal ? "real" : "nominal";
            const ip = (k) => a2[sp][k] + (b2[sp][k] - a2[sp][k]) * f;
            pctAt = { p10: ip("p10"), p50: ip("p50"), p90: ip("p90") };
          }
          if (pctAt && pctAt.p50 === undefined) {
            const sp = isReal ? "real" : "nominal";
            pctAt = { p10: pctAt[sp].p10, p50: pctAt[sp].p50, p90: pctAt[sp].p90 };
          }
          return pctAt;
        }
        const liveCohTip = getLiveCohortDisplayResult();
        if (liveCohTip) {
          const co = liveCohTip, spc = isReal ? "real" : "nominal";
          const interp = (arr, keyFn, getMonth) => {
            if (!arr.length) return null;
            if (barIdx <= getMonth(arr[0])) return keyFn(arr[0]);
            if (barIdx >= getMonth(arr[arr.length - 1])) return keyFn(arr[arr.length - 1]);
            let i = 0; while (i < arr.length - 1 && getMonth(arr[i + 1]) < barIdx) i++;
            const a2 = arr[i], b2 = arr[i + 1], f = (barIdx - getMonth(a2)) / (getMonth(b2) - getMonth(a2));
            return keyFn(a2) + (keyFn(b2) - keyFn(a2)) * f;
          };
          const band = co.band || [];
          if (!band.length) return null;
          return {
            p10: interp(band, cp => cp[spc].p10, cp => cp.monthIdx),
            p50: interp(band, cp => cp[spc].p50, cp => cp.monthIdx),
            p90: interp(band, cp => cp[spc].p90, cp => cp.monthIdx),
          };
        }
        return null;
      };

      const bandPct = getBandPctAt(idx);
      const dispHeadline = bandPct ? Math.max(0, bandPct.p50) : dispTotal;

      const prevBar = idx > 0 ? SIM_BARS[idx - 1] : null;
      let netMoDelta;
      if (bandPct && prevBar) {
        const prevBandPct = idx === 1 ? null : getBandPctAt(idx - 1);
        const prevHeadline = prevBandPct ? Math.max(0, prevBandPct.p50) : (prevBar.totalVal * simBarScale(prevBar));
        netMoDelta = dispHeadline - prevHeadline;
      } else if (prevBar) {
        netMoDelta = dispTotal - (prevBar.totalVal * simBarScale(prevBar));
      } else {
        netMoDelta = dispGrowth + ((b.schedDepositNom || 0) * scale) - dispGrossSold;
      }
      const netCls = netMoDelta >= 0 ? "color:#34d399;" : "color:#fb7185;";

      const pctBandHtml = bandPct
        ? `<div class="mono" style="display:flex;justify-content:space-between;align-items:baseline;gap:12px;background:rgba(15,23,42,0.55);border:1px solid rgba(51,65,85,0.65);border-radius:5px;padding:4px 8px;margin:4px 0 2px;font-size:10.6px;white-space:nowrap;">
            <span><span class="muted">P10</span> <strong style="color:#cbd5e1;">${fmtUSD0(Math.max(0, bandPct.p10))}</strong></span>
            <span><span style="color:#5AD8A6;font-weight:600;">P50</span> <strong style="color:#f8fafc;">${fmtUSD0(Math.max(0, bandPct.p50))}</strong></span>
            <span><span class="muted">P90</span> <strong style="color:#cbd5e1;">${fmtUSD0(Math.max(0, bandPct.p90))}</strong></span>
          </div>`
        : "";

      // (C) Current Income (after-tax SSN & active paycheck), Expenses (Inf. Adjusted), Work Contributions, and Net Cashflow
      let repBarForCf = null;
      if (bandPct && idx > 0) {
        if (simMcActive() && _mcResult && Array.isArray(_mcResult._repBars) && _mcResult._repBars[idx]) {
          repBarForCf = _mcResult._repBars[idx];
        } else if (simCohortActive() && _cohortResult && Array.isArray(_cohortResult._repBars) && _cohortResult._repBars[idx]) {
          repBarForCf = _cohortResult._repBars[idx];
        }
      }
      const cfScale = repBarForCf ? (isReal ? (1.0 / (repBarForCf.deflator || 1.0)) : 1.0) : scale;
      const dispAfterTaxExp = repBarForCf && repBarForCf.rawMonthlyExpense != null
        ? (repBarForCf.rawMonthlyExpense * cfScale)
        : ((b.rawMonthlyExpense != null ? b.rawMonthlyExpense : b.monthlyBurn) * scale);
      const dispSsnNet = repBarForCf && repBarForCf.ssnNetMo != null
        ? (repBarForCf.ssnNetMo * cfScale)
        : ((b.ssnNetMo || 0) * scale);
      const dispPaycheckOff = repBarForCf
        ? ((repBarForCf.monthlyPaycheckOffset || 0) * cfScale)
        : ((b.monthlyPaycheckOffset || 0) * scale);
      const dispWorkContrib = idx > 0
        ? (repBarForCf ? ((repBarForCf.monthlyWorkContrib || 0) * cfScale) : ((b.monthlyWorkContrib || 0) * scale))
        : 0;
      const netCashflow = dispSsnNet + dispPaycheckOff - dispAfterTaxExp;
      const isNetExpense = netCashflow < -0.5;
      const netCashflowLbl = isNetExpense ? "Net Expenses (Inf. Adjusted)" : "Net Income (Inf. Adjusted)";
      const netCashflowValStr = isNetExpense
        ? `-${fmtUSD0(Math.abs(netCashflow))}`
        : (netCashflow > 0.5 ? `+${fmtUSD0(netCashflow)}` : "$0");
      const netCashflowColor = isNetExpense ? "#fb7185" : (netCashflow > 0.5 ? "#34d399" : "#94a3b8");
      const workCashflowLinesHtml =
        (dispPaycheckOff > 0.5
          ? `<div style="display:flex;justify-content:space-between;gap:10px;font-size:11px;margin:1.5px 0;">
              <span class="muted">Paycheck Covers Expenses (Inf. Adjusted)</span>
              <strong style="color:#34d399;">+${fmtUSD0(dispPaycheckOff)}</strong>
            </div>`
          : "") +
        (dispWorkContrib > 0.5
          ? `<div style="display:flex;justify-content:space-between;gap:10px;font-size:11px;margin:1.5px 0;">
              <span class="muted">&#8593; Work Contributions</span>
              <strong style="color:#38bdf8;">+${fmtUSD0(dispWorkContrib)}</strong>
            </div>`
          : "");

      const actBar = repBarForCf || b;
      const actScale = repBarForCf ? cfScale : scale;
      const actEvs = actBar.schedActionEvents || actBar.schedWithdrawEvents || b.schedActionEvents || b.schedWithdrawEvents || [];
      const swLine = actEvs.length > 0
        ? actEvs.map(ev => {
            const t = ev.type || "withdrawal";
            const col = t === "transfer" ? "#22d3ee" : (t === "deposit" ? "#34d399" : "#fda4af");
            const prefix = (t === "transfer" ? "⇄ Transfer" : (t === "deposit" ? "↑ Deposit" : "↓ Scheduled Withdrawal")) + (bandPct ? (repBarForCf ? " · Median Path" : " · Single Path ledger") : "");
            if (t === "withdrawal") {
              const money = value => fmtUSD0((value || 0) * actScale);
              const isAutoWf = !ev.from_asset_id || ev.from_asset_id === "auto";
              const poolLbl = isAutoWf ? "Auto waterfall / cash" : "From pooled cash";
              return `<div class="mono" style="font-size:11px;color:${col};margin:2px 0;">
                ${prefix} · ${escHtml(ev.comment)} · ${escHtml(ev.from_asset_id || "auto")} → spending<br>
                Requested ${money(ev.nomAmt)} · Funded ${money(ev.netAmt)} · Shortfall ${money(ev.shortfall)}<br>
                ${poolLbl} ${money(ev.pooledNet)} · Attributed tax ${money(ev.taxPaid)}
              </div>`;
            }
            return `<div class="mono" style="display:flex;justify-content:space-between;gap:10px;font-size:11px;color:${col};margin:1.5px 0;">
              <span>${prefix} <span class="muted">(${escHtml(ev.comment)})</span></span>
              <strong>${fmtUSD0((ev.nomAmt || 0) * actScale)}</strong>
            </div>`;
          }).join("")
        : "";

      // DA-A05 (User Decision A5): In Monte Carlo / Historical modes, reconcile Section (D) sales &
      // Section (E) bucket balances with the P50 headline rather than deterministic Single-Path `SIM_BARS[idx]`.
      // `_repBars` replays the single P50-closest path/cohort (after PRNG draws complete so seeded MC parity
      // is untouched), and `valDispMult = dispHeadline / repBar.totalVal` guarantees Section (E) holding
      // rows sum to 100.0% of the displayed `P50` headline in both Nominal $ and Today's $.
      let repBar = null;
      if (bandPct && idx > 0) {
        if (simMcActive() && _mcResult && Array.isArray(_mcResult._repBars) && _mcResult._repBars[idx]) {
          repBar = _mcResult._repBars[idx];
        } else if (simCohortActive() && _cohortResult && Array.isArray(_cohortResult._repBars) && _cohortResult._repBars[idx]) {
          repBar = _cohortResult._repBars[idx];
        }
      }

      const bearActive = Boolean(idx > 0 && (repBar ? repBar.bearShieldActive : b.bearShieldActive));

      // (D) Holdings sold to cover net expense (shown only when there are net expenses or sales)
      const activeSales = repBar ? (repBar.sales || []) : (b.sales || []);
      const saleScale = repBar ? (isReal ? (1.0 / (repBar.deflator || 1.0)) : 1.0) : scale;
      const soldIds = new Set(activeSales.map(s => s.id));
      const hasSalesSection = isNetExpense || activeSales.length > 0;
      const salesHeaderLbl = (repBar ? "Sold to Cover Net Expense (Median Path)" : "Sold to Cover Net Expense") + (bearActive ? " · 🛡 Bear Shield" : "");
      const salesSectionHtml = hasSalesSection
        ? `<div style="border-top:1px dashed rgba(51,65,85,0.75);padding-top:4px;margin-bottom:4px;">
            <div class="muted" style="font-size:10px;text-transform:uppercase;letter-spacing:0.04em;margin-bottom:2px;">${salesHeaderLbl}</div>
            ${activeSales.length > 0
              ? activeSales.map(s => `
                  <div class="mono" style="font-size:11px;display:flex;justify-content:space-between;gap:10px;margin:2px 0;">
                    <span style="color:#e2e8f0;"><span class="swatch" style="background:${s.color || '#38bdf8'};width:8px;height:8px;margin-right:5px;"></span>${escHtml(s.shortTitle)}</span>
                    <span><strong>${fmtUSD0(s.grossSold * saleScale)}</strong> sold <span class="muted">&middot; ${fmtUSD0(s.taxPaid * saleScale)} tax</span></span>
                  </div>`).join("")
              : `<div class="mono muted" style="font-size:10.8px;">No holdings sold this month</div>`}
          </div>`
        : "";

      // (E) Top 5 holdings in sell priority order + N other holdings
      const aVals = repBar ? (repBar.assetVals || {}) : (b.assetVals || {});
      const baseTotForPct = repBar ? Math.max(0, repBar.totalVal || 0) : b.totalVal;
      const valDispMult = repBar
        ? (baseTotForPct > 1.0 ? (dispHeadline / baseTotForPct) : 0.0)
        : scale;
      const nonZeroAssets = [];
      const effectiveOrder = bearActive ? computeBearShieldOrder(simAssetOrder) : simAssetOrder;
      effectiveOrder.forEach((aid, orderIdx) => {
        const a = assetMap[aid];
        if (!a || (a.is_unvested_goog && !simIncludeUnvested)) return;
        const rawV = aVals[aid] || 0;
        if (rawV <= 0.5 && !soldIds.has(aid)) return;
        nonZeroAssets.push({
          id: aid,
          orderIdx,
          rank: orderIdx + 1,
          asset: a,
          rawVal: rawV,
          dispVal: rawV * valDispMult,
        });
      });

      const shownItems = nonZeroAssets.slice(0, 5);
      const collapsedItems = nonZeroAssets.slice(5);

      const activeAssetLines = shownItems.map(item => {
        const pct = baseTotForPct > 0 ? (item.rawVal / baseTotForPct) * 100 : 0;
        return `<div style="display:flex;justify-content:space-between;gap:10px;margin:1.8px 0;font-size:11px;">
          <span><span class="muted mono" style="font-size:10px;display:inline-block;width:24px;">#${item.rank}</span><span class="swatch" style="background:${item.asset.color || '#38bdf8'};width:8px;height:8px;margin-right:5px;"></span>${escHtml(item.asset.short_title)}</span>
          <span class="mono">${fmtUSD0(item.dispVal)} <span class="muted">(${pct.toFixed(1)}%)</span></span>
        </div>`;
      }).join("");

      let collapsedLine = "";
      if (collapsedItems.length > 0) {
        const colRawSum = collapsedItems.reduce((s, x) => s + x.rawVal, 0);
        const colDispSum = colRawSum * valDispMult;
        const colPct = baseTotForPct > 0 ? (colRawSum / baseTotForPct) * 100 : 0;
        collapsedLine = `<div class="mono muted" style="display:flex;justify-content:space-between;gap:10px;margin:2px 0;font-size:10.8px;">
          <span style="padding-left:24px;">+${collapsedItems.length} other holding${collapsedItems.length === 1 ? "" : "s"}</span>
          <span>${fmtUSD0(colDispSum)} (${colPct.toFixed(1)}%)</span>
        </div>`;
      }

      const dlrLabel = bandPct
        ? (isReal ? "P50, Today&#39;s $" : "P50, Nominal $")
        : (isReal ? "Today&#39;s $" : "Nominal $");

      tip.innerHTML = `
        <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;">
          <strong class="mono" style="font-size:12.5px;">${escHtml(b.fullLbl)}</strong>
          <div style="display:inline-flex;align-items:center;gap:5px;">
            ${bearActive ? `<span class="pill pill-cyan mono" style="font-size:9.8px;padding:1px 6px;">&#128737; Bear Shield</span>` : ""}
            <span class="pill ${idx === 0 ? 'pill-blue' : 'pill-green'} mono" style="font-size:10px;">${idx === 0 ? "TODAY" : `+${b.yearsOut.toFixed(1)} YRS`}</span>
          </div>
        </div>
        <div class="mono muted" style="font-size:11px;margin:2px 0 5px;">
          ${familyAgesHtml}
        </div>

        <div style="border-top:1px solid rgba(51,65,85,0.75);padding-top:5px;margin-bottom:5px;">
          <div class="mono" style="display:flex;justify-content:space-between;align-items:baseline;gap:12px;">
            <div>
              <span style="font-size:16px;font-weight:700;color:#f8fafc;">${fmtUSD0(dispHeadline)}</span>
              <span class="muted" style="font-size:10.3px;margin-left:3px;">(${dlrLabel})</span>
            </div>
            <div style="font-size:11.2px;${netCls}">
              <strong>${fmtSignedUSD0(netMoDelta)}</strong> <span class="muted" style="font-size:10px;">vs last mo</span>
            </div>
          </div>
          ${pctBandHtml}
        </div>

        <div style="border-top:1px dashed rgba(51,65,85,0.75);padding-top:4px;margin-bottom:4px;" class="mono">
          <div style="display:flex;justify-content:space-between;gap:10px;font-size:11px;margin:1.5px 0;">
            <span class="muted">Income (After-Tax SSN)</span>
            <strong style="color:${dispSsnNet > 1 ? '#34d399' : '#94a3b8'};">${dispSsnNet > 1 ? `+${fmtUSD0(dispSsnNet)}` : "$0"}</strong>
          </div>
          ${workCashflowLinesHtml}
          <div style="display:flex;justify-content:space-between;gap:10px;font-size:11px;margin:1.5px 0;">
            <span class="muted">Expenses (Inf. Adjusted)</span>
            <strong style="color:#e2e8f0;">-${fmtUSD0(dispAfterTaxExp)}</strong>
          </div>
          <div style="display:flex;justify-content:space-between;gap:10px;font-size:11px;margin:1.5px 0;">
            <span class="muted">${netCashflowLbl}</span>
            <strong style="color:${netCashflowColor};">${netCashflowValStr}</strong>
          </div>
          ${swLine}
        </div>

        ${salesSectionHtml}

        <div style="border-top:1px solid rgba(51,65,85,0.85);padding-top:4px;">
          <div class="muted" style="font-size:10px;text-transform:uppercase;letter-spacing:0.04em;margin-bottom:2px;">${bearActive ? "Holdings (&#128737; Bear Shield Priority)" : "Holdings (Sell Priority Order)"}</div>
          ${activeAssetLines}
          ${collapsedLine}
        </div>
      `;

      placeFloatingBoxAwayFromPointer(tip, evt.clientX, evt.clientY);
    }

