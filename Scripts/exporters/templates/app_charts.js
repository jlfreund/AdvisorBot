    // =========================================================================
    // SCREEN 4: INCOME & EXPENSE — SOCIAL SECURITY (OASDI) PROJECTOR
    // =========================================================================

    // DESIGN INTENT & CROSS-FILE INVARIANT (`DA2-02` / `DA-A04` — coupled with `scheduleSimRecompute` in `app_core.js` and `computeMonthlySsnForIso` in `app_simulator.js`):
    // Screen 4 "Today's $" MUST deflate nominal COLA-escalated checks (`jNomMo`, `yNomMo`, `totNom*`) by CPI (`simInflationPct` compounded monthly, matching Screen 3's exact `cpiDeflator`), NEVER by `colaDeflator` (`ssnColaPct`).
    // Note: `computeMonthlySsnForIso` returns `ssn.jRealMo = ssn.jNomMo / colaDeflator` (static PIA base before COLA). Reading `ssn.*Real*` directly on Screen 4 algebraically cancels COLA and falsely shows flat purchasing power when COLA != CPI (e.g., 0% COLA under 3% CPI).
    function _ssnCpiDeflatorForMonth(mIdx) {
      const infPct = (typeof simInflationPct === "number" && Number.isFinite(simInflationPct)) ? simInflationPct : RETIREMENT_INFLATION_REF;
      const infMonthlyRate = Math.pow(1.0 + infPct / 100.0, 1.0 / 12.0) - 1.0;
      const cpiDeflator = Math.pow(1.0 + infMonthlyRate, Math.max(0, Number(mIdx) || 0));
      return cpiDeflator > 0 ? cpiDeflator : 1.0;
    }

    function ssnBarScale(b) {
      if (!b || ssnDollarMode !== "real") return 1.0;
      return 1.0 / _ssnCpiDeflatorForMonth(b.idx ?? b.m ?? 0);
    }

    function recomputeSsnBars() {
      const startYear = parseInt(TODAY_ISO.slice(0, 4), 10) || 2026;
      const startMonth0 = (parseInt(TODAY_ISO.slice(5, 7), 10) || 11) - 1;
      const startDay = parseInt(TODAY_ISO.slice(8, 10), 10) || 10;

      const bars = [];
      let cumJNom = 0, cumYNom = 0, cumTotNom = 0, cumNetNom = 0;
      let cumJReal = 0, cumYReal = 0, cumTotReal = 0, cumNetReal = 0;

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

        const ssn = computeMonthlySsnForIso(iso, m, ssnInv0ClaimAge, ssnInv1ClaimAge, ssnColaPct);
        const cpiDefl = _ssnCpiDeflatorForMonth(m);
        const jRealMo = ssn.jNomMo / cpiDefl;
        const yRealMo = ssn.yNomMo / cpiDefl;
        const totRealGross = ssn.totNomGross / cpiDefl;
        const totRealTax = ssn.totNomTax / cpiDefl;
        const totRealNet = ssn.totNomNet / cpiDefl;
        if (m > 0) {
          cumJNom += ssn.jNomMo;
          cumYNom += ssn.yNomMo;
          cumTotNom += ssn.totNomGross;
          cumNetNom += ssn.totNomNet;
          cumJReal += jRealMo;
          cumYReal += yRealMo;
          cumTotReal += totRealGross;
          cumNetReal += totRealNet;
        }

        bars.push({
          idx: m,
          iso,
          monthLbl,
          fullLbl,
          yearLbl,
          yearsOut: m / 12.0,
          colaDeflator: ssn.colaDeflator,
          cpiDeflator: cpiDefl,
          jAge: ssn.jAge,
          yAge: ssn.yAge,
          jActive: ssn.jActive,
          yActive: ssn.yActive,
          jNomMo: ssn.jNomMo,
          yNomMo: ssn.yNomMo,
          totNomGross: ssn.totNomGross,
          totNomTax: ssn.totNomTax,
          totNomNet: ssn.totNomNet,
          jRealMo,
          yRealMo,
          totRealGross,
          totRealTax,
          totRealNet,
          cumJNom,
          cumYNom,
          cumTotNom,
          cumNetNom,
          cumJReal,
          cumYReal,
          cumTotReal,
          cumNetReal,
        });
      }

      SSN_BARS = bars;
      updateSsnSummaryDOM();
    }

    function simulateSsnTotalsForAges(jAgeClaim, yAgeClaim, colaPct = ssnColaPct) {
      const startYear = parseInt(TODAY_ISO.slice(0, 4), 10) || 2026;
      const startMonth0 = (parseInt(TODAY_ISO.slice(5, 7), 10) || 11) - 1;
      const startDay = parseInt(TODAY_ISO.slice(8, 10), 10) || 10;

      const msAge0 = INV0.milestone_age || 85;
      const msAge1 = HAS_SECOND_INVESTOR ? (INV1.milestone_age || 85) : msAge0;
      const msYr0 = INV0.birth_year + msAge0;
      const msYr1 = HAS_SECOND_INVESTOR ? (INV1.birth_year + msAge1) : msYr0;

      const fra0 = INV0.fra_age != null ? Number(INV0.fra_age) : ssaFraForBirthYear(INV0.birth_year);
      const fra1 = (HAS_SECOND_INVESTOR && INV1) ? (INV1.fra_age != null ? Number(INV1.fra_age) : ssaFraForBirthYear(INV1.birth_year)) : 67.0;
      const estMonths0 = Math.max(0, (INV0.birth_year + jAgeClaim - startYear) * 12 + ((INV0.birth_month || 1) - 1 - startMonth0));
      const estMonths1 = HAS_SECOND_INVESTOR ? Math.max(0, (INV1.birth_year + yAgeClaim - startYear) * 12 + ((INV1.birth_month || 1) - 1 - startMonth0)) : 0;
      const colaMonthly = Math.pow(1.0 + (Number(colaPct) || 0) / 100.0, 1.0 / 12.0) - 1.0;
      let jBy85Nom = 0, jBy85Real = 0;
      let yBy85Nom = 0, yBy85Real = 0;
      let yByJ85Nom = 0, yByJ85Real = 0;
      let jFirstMoNom = 0;
      let jFirstMoReal = (SSN_INV0_PIA_2026 * ssaClaimMultiplier(jAgeClaim, fra0) * Math.pow(1.0 + colaMonthly, estMonths0)) / _ssnCpiDeflatorForMonth(estMonths0);
      let yFirstMoNom = 0;
      let yFirstMoReal = HAS_SECOND_INVESTOR
        ? ((SSN_INV1_PIA_2026 * ssaClaimMultiplier(yAgeClaim, fra1) * Math.pow(1.0 + colaMonthly, estMonths1)) / _ssnCpiDeflatorForMonth(estMonths1))
        : 0;
      let jStartLbl = `${MONTH_NAMES_SHORT[(INV0.birth_month || 1) - 1]} ${INV0.birth_year + jAgeClaim}`;
      let yStartLbl = HAS_SECOND_INVESTOR
        ? `${MONTH_NAMES_SHORT[(INV1.birth_month || 1) - 1]} ${INV1.birth_year + yAgeClaim}`
        : "—";

      for (let m = 1; m <= SIM_TOTAL_MONTHS; m++) {
        const totMonthsFromEpoch = startYear * 12 + startMonth0 + m;
        const yr = Math.floor(totMonthsFromEpoch / 12);
        const mo0 = totMonthsFromEpoch % 12;
        const daysInMo = new Date(Date.UTC(yr, mo0 + 1, 0)).getUTCDate();
        const day = Math.min(startDay, daysInMo);
        const iso = `${yr}-${String(mo0 + 1).padStart(2, "0")}-${String(day).padStart(2, "0")}`;

        const ssn = computeMonthlySsnForIso(iso, m, jAgeClaim, yAgeClaim, colaPct);
        const cpiDefl = _ssnCpiDeflatorForMonth(m);
        const jRealMo = ssn.jNomMo / cpiDefl;
        const yRealMo = ssn.yNomMo / cpiDefl;
        const jReached85 = ssn.jAge >= msAge0 && (yr > msYr0 || (yr === msYr0 && (mo0 + 1) > INV0.birth_month));
        const yReached85 = !HAS_SECOND_INVESTOR || (ssn.yAge >= msAge1 && (yr > msYr1 || (yr === msYr1 && (mo0 + 1) > INV1.birth_month)));

        if (ssn.jActive && jFirstMoNom === 0) {
          jFirstMoNom = ssn.jNomMo;
          jFirstMoReal = jRealMo;
        }
        if (ssn.yActive && yFirstMoNom === 0) {
          yFirstMoNom = ssn.yNomMo;
          yFirstMoReal = yRealMo;
        }
        if (!jReached85) {
          jBy85Nom += ssn.jNomMo;
          jBy85Real += jRealMo;
          yByJ85Nom += ssn.yNomMo;
          yByJ85Real += yRealMo;
        }
        if (!yReached85) {
          yBy85Nom += ssn.yNomMo;
          yBy85Real += yRealMo;
        }
      }

      return {
        jAgeClaim,
        yAgeClaim,
        jStartLbl,
        yStartLbl,
        jMultPct: ssaClaimMultiplier(jAgeClaim, fra0) * 100.0,
        yMultPct: ssaClaimMultiplier(yAgeClaim, fra1) * 100.0,
        jFirstMoNom,
        jFirstMoReal,
        yFirstMoNom,
        yFirstMoReal,
        jBy85Nom,
        jBy85Real,
        yBy85Nom,
        yBy85Real,
        yByJ85Nom,
        yByJ85Real,
        combByJ85Nom: jBy85Nom + yByJ85Nom,
        combByJ85Real: jBy85Real + yByJ85Real,
      };
    }

    function getSsnOptimalClaimAge(investorIndex = 0) {
      const isReal = ssnDollarMode === "real";
      let bestAge = 67, bestVal = -1;
      for (let age = 62; age <= 70; age++) {
        const sim = simulateSsnTotalsForAges(age, age, ssnColaPct);
        const val = investorIndex === 0 ? (isReal ? sim.jBy85Real : sim.jBy85Nom) : (isReal ? sim.yBy85Real : sim.yBy85Nom);
        if (val > bestVal) {
          bestVal = val;
          bestAge = age;
        }
      }
      return { age: bestAge, val: bestVal };
    }

    function onSsnInv0MaxRefClick() {
      const opt = getSsnOptimalClaimAge(0);
      onSsnInv0AgeInput(opt.age);
    }

    function onSsnInv1MaxRefClick() {
      if (!HAS_SECOND_INVESTOR) return;
      const opt = getSsnOptimalClaimAge(1);
      onSsnInv1AgeInput(opt.age);
    }
    window.onSsnInv0MaxRefClick = onSsnInv0MaxRefClick;
    window.onSsnInv1MaxRefClick = onSsnInv1MaxRefClick;

    function updateSsnSummaryDOM() {
      if (!SSN_BARS.length) return;
      const isReal = ssnDollarMode === "real";
      const dlrTag = isReal ? " (Today's $)" : "";

      const msAge0 = INV0.milestone_age || 85;
      const msAge1 = HAS_SECOND_INVESTOR ? (INV1.milestone_age || 85) : msAge0;
      const inv0MsIdx = SSN_BARS.findIndex((b, idx) => ageOnIso(INV0.birth_year, INV0.birth_month, INV0.birth_day, b.iso, idx) >= msAge0);
      const inv1MsIdx = HAS_SECOND_INVESTOR
        ? SSN_BARS.findIndex((b, idx) => ageOnIso(INV1.birth_year, INV1.birth_month, INV1.birth_day, b.iso, idx) >= msAge1)
        : -1;
      const j85Bar = inv0MsIdx >= 0 ? SSN_BARS[inv0MsIdx] : SSN_BARS[SSN_BARS.length - 1];
      const y85Bar = inv1MsIdx >= 0 ? SSN_BARS[inv1MsIdx] : SSN_BARS[SSN_BARS.length - 1];
      const b40 = SSN_BARS[SSN_BARS.length - 1];

      const jStartIdx = SSN_BARS.findIndex(b => b.jActive);
      const yStartIdx = HAS_SECOND_INVESTOR ? SSN_BARS.findIndex(b => b.yActive) : -1;
      const jStartBar = jStartIdx >= 0 ? SSN_BARS[jStartIdx] : null;
      const yStartBar = yStartIdx >= 0 ? SSN_BARS[yStartIdx] : null;

      const moName0 = MONTH_NAMES_SHORT[(INV0.birth_month || 1) - 1];
      const moName1 = HAS_SECOND_INVESTOR ? MONTH_NAMES_SHORT[(INV1.birth_month || 1) - 1] : moName0;
      const msYr0 = INV0.birth_year + msAge0;
      const msYr1 = HAS_SECOND_INVESTOR ? (INV1.birth_year + msAge1) : msYr0;

      // Update Ref: Max Payout buttons & subtext
      const optJ = getSsnOptimalClaimAge(0);
      const optY = HAS_SECOND_INVESTOR ? getSsnOptimalClaimAge(1) : { age: 67, val: 0 };

      const jRefBtn = document.getElementById("ssn-inv0-max-ref-btn");
      if (jRefBtn) {
        jRefBtn.classList.toggle("active", ssnInv0ClaimAge === optJ.age);
        const dlrLabel = isReal ? "Today's $" : "Nominal $";
        jRefBtn.title = `Set ${escHtml(INV0.name)}'s First Draw Age to Age ${optJ.age} to maximize cumulative pre-tax Social Security payout (${fmtUSD0(optJ.val)} ${dlrLabel}) collected prior to goal age ${msAge0} (calculated across claiming ages 62–70).`;
      }

      const yRefBtn = document.getElementById("ssn-inv1-max-ref-btn");
      if (yRefBtn && HAS_SECOND_INVESTOR) {
        yRefBtn.classList.toggle("active", ssnInv1ClaimAge === optY.age);
        const dlrLabel = isReal ? "Today's $" : "Nominal $";
        yRefBtn.title = `Set ${escHtml(INV1.name)}'s First Draw Age to Age ${optY.age} to maximize cumulative pre-tax Social Security payout (${fmtUSD0(optY.val)} ${dlrLabel}) collected prior to goal age ${msAge1} (calculated across claiming ages 62–70).`;
      }

      const fra0 = INV0.fra_age != null ? Number(INV0.fra_age) : ssaFraForBirthYear(INV0.birth_year);
      const fra1 = (HAS_SECOND_INVESTOR && INV1) ? (INV1.fra_age != null ? Number(INV1.fra_age) : ssaFraForBirthYear(INV1.birth_year)) : 67.0;
      const jSubtext = document.getElementById("ssn-inv0-subtext");
      if (jSubtext) {
        const jStartMo = jStartBar ? (isReal ? jStartBar.jRealMo : jStartBar.jNomMo) : (SSN_INV0_PIA_2026 * ssaClaimMultiplier(ssnInv0ClaimAge, fra0));
        const startStr = jStartBar ? jStartBar.monthLbl : `${moName0} '${String(INV0.birth_year + ssnInv0ClaimAge).slice(2)}`;
        const multPct = (ssaClaimMultiplier(ssnInv0ClaimAge, fra0) * 100.0).toFixed(1);
        const dlrLabel = isReal ? "Today's $" : "Nominal $";
        jSubtext.innerHTML =
          `<span>Age ${ssnInv0ClaimAge} &bull; First Check: <strong class="mono" style="color:var(--text);">${startStr}</strong> (${multPct}% of PIA)</span>` +
          `<span>Monthly Check: <strong class="mono" style="color:#ffffff;font-size:12px;">${fmtUSD0(jStartMo)}/mo</strong> <span class="muted" style="font-size:10.5px;">(Pre-tax, ${dlrLabel})</span></span>`;
      }

      const ySubtext = document.getElementById("ssn-inv1-subtext");
      if (ySubtext && HAS_SECOND_INVESTOR) {
        const yStartMo = yStartBar ? (isReal ? yStartBar.yRealMo : yStartBar.yNomMo) : (SSN_INV1_PIA_2026 * ssaClaimMultiplier(ssnInv1ClaimAge, fra1));
        const startStr = yStartBar ? yStartBar.monthLbl : `${moName1} '${String(INV1.birth_year + ssnInv1ClaimAge).slice(2)}`;
        const multPct = (ssaClaimMultiplier(ssnInv1ClaimAge, fra1) * 100.0).toFixed(1);
        const dlrLabel = isReal ? "Today's $" : "Nominal $";
        ySubtext.innerHTML =
          `<span>Age ${ssnInv1ClaimAge} &bull; First Check: <strong class="mono" style="color:var(--text);">${startStr}</strong> (${multPct}% of PIA)</span>` +
          `<span>Monthly Check: <strong class="mono" style="color:#ffffff;font-size:12px;">${fmtUSD0(yStartMo)}/mo</strong> <span class="muted" style="font-size:10.5px;">(Pre-tax, ${dlrLabel})</span></span>`;
      }

      const kpi0Lbl = document.getElementById("ssn-kpi-inv0-lbl");
      if (kpi0Lbl) kpi0Lbl.textContent = `${INV0.name} Total Income (Age ${ssnInv0ClaimAge}–${msAge0})`;
      const kpi1Lbl = document.getElementById("ssn-kpi-inv1-lbl");
      if (kpi1Lbl && HAS_SECOND_INVESTOR) kpi1Lbl.textContent = `${INV1.name} Total Income (Age ${ssnInv1ClaimAge}–${msAge1})`;
      const kpiCombLbl = document.getElementById("ssn-kpi-comb-lbl");
      if (kpiCombLbl) kpiCombLbl.textContent = HAS_SECOND_INVESTOR ? "Total Household Income" : `After-Tax Net Income (Age ${ssnInv0ClaimAge}–${msAge0})`;

      // Card 1: Investor 1 Total Income (Age N-M)
      const jNomPreTax = j85Bar.cumJNom;
      const jRealPreTax = j85Bar.cumJReal;

      const jValEl = document.getElementById("ssn-kpi-inv0");
      const jSubEl = document.getElementById("ssn-kpi-inv0-sub");
      if (jValEl) {
        jValEl.innerHTML = `${fmtUSD0(jNomPreTax)} <span style="font-size:12.5px;font-weight:500;color:#6ee7b7;font-family:var(--sans);letter-spacing:0;vertical-align:1px;">(Nominal $, Pre-tax)</span>`;
      }
      if (jSubEl) {
        jSubEl.innerHTML = `<span class="mono" style="color:#7dd3fc;font-weight:600;">${fmtUSD0(jRealPreTax)}</span> <span style="color:var(--muted);">(Today's $, Pre-tax)</span>`;
      }

      // Card 2: Investor 2 Total Income (Age N-M)
      const yNomPreTax = HAS_SECOND_INVESTOR ? y85Bar.cumYNom : 0;
      const yRealPreTax = HAS_SECOND_INVESTOR ? y85Bar.cumYReal : 0;

      const yValEl = document.getElementById("ssn-kpi-inv1");
      const ySubEl = document.getElementById("ssn-kpi-inv1-sub");
      if (HAS_SECOND_INVESTOR) {
        if (yValEl) {
          yValEl.innerHTML = `${fmtUSD0(yNomPreTax)} <span style="font-size:12.5px;font-weight:500;color:#6ee7b7;font-family:var(--sans);letter-spacing:0;vertical-align:1px;">(Nominal $, Pre-tax)</span>`;
        }
        if (ySubEl) {
          ySubEl.innerHTML = `<span class="mono" style="color:#7dd3fc;font-weight:600;">${fmtUSD0(yRealPreTax)}</span> <span style="color:var(--muted);">(Today's $, Pre-tax)</span>`;
        }
      }

      // Card 3: Combined Household Total Income (or Net Income when single investor)
      const combNomVal = HAS_SECOND_INVESTOR ? (jNomPreTax + yNomPreTax) : (jNomPreTax * SSN_NET_FACTOR);
      const combRealVal = HAS_SECOND_INVESTOR ? (jRealPreTax + yRealPreTax) : (jRealPreTax * SSN_NET_FACTOR);
      const combTag = HAS_SECOND_INVESTOR ? "Pre-tax" : "After-tax net";

      const cValEl = document.getElementById("ssn-kpi-comb");
      const cSubEl = document.getElementById("ssn-kpi-comb-sub");
      if (cValEl) {
        cValEl.innerHTML = `${fmtUSD0(combNomVal)} <span style="font-size:12.5px;font-weight:500;color:#6ee7b7;font-family:var(--sans);letter-spacing:0;vertical-align:1px;">(Nominal $, ${combTag})</span>`;
      }
      if (cSubEl) {
        cSubEl.innerHTML = `<span class="mono" style="color:#7dd3fc;font-weight:600;">${fmtUSD0(combRealVal)}</span> <span style="color:var(--muted);">(Today's $, ${combTag})</span>`;
      }

      const statusEl = document.getElementById("ssn-status-pill");
      if (statusEl) {
        let bothMoReal = (SSN_INV0_PIA_2026 * ssaClaimMultiplier(ssnInv0ClaimAge, fra0)) + (HAS_SECOND_INVESTOR ? (SSN_INV1_PIA_2026 * ssaClaimMultiplier(ssnInv1ClaimAge, fra1)) : 0);
        if (HAS_SECOND_INVESTOR) {
          if (SSN_INV0_PIA_2026 > 0 && 0.5 * SSN_INV0_PIA_2026 > SSN_INV1_PIA_2026) {
            bothMoReal += (0.5 * SSN_INV0_PIA_2026 - SSN_INV1_PIA_2026) * ssaSpousalMultiplier(ssnInv1ClaimAge, fra1);
          } else if (SSN_INV1_PIA_2026 > 0 && 0.5 * SSN_INV1_PIA_2026 > SSN_INV0_PIA_2026) {
            bothMoReal += (0.5 * SSN_INV1_PIA_2026 - SSN_INV0_PIA_2026) * ssaSpousalMultiplier(ssnInv0ClaimAge, fra0);
          }
        }
        const prefixLbl = HAS_SECOND_INVESTOR ? "Combined Base" : "Monthly Base";
        statusEl.textContent = `${prefixLbl} (Today's $): ${fmtUSD0(bothMoReal)}/mo (${fmtUSD0(bothMoReal * 12)}/yr gross • ${fmtUSD0(bothMoReal * 12 * SSN_NET_FACTOR)}/yr net)`;
      }

      const metricsEl = document.getElementById("ssn-summary-metrics");
      if (metricsEl) {
        const ssnRuleLbl = SSN_MODEL.tax_rule_label || `${((SSN_MODEL.fed_taxable_portion ?? 0.85) * 100).toFixed(0)}% × ${((SSN_MODEL.fed_top_rate ?? 0.37) * 100).toFixed(0)}% Fed = ${(SSN_EFF_TAX_RATE * 100).toFixed(2)}%`;
        const inv1StartPart = HAS_SECOND_INVESTOR
          ? `<span>${escHtml(INV1.name)} Starts: <strong class="mono" style="color:#10b981;">${yStartBar ? yStartBar.fullLbl : moName1 + " " + (INV1.birth_year + ssnInv1ClaimAge)} (Age ${ssnInv1ClaimAge})</strong></span><span style="margin:0 6px;">&bull;</span>`
          : "";
        metricsEl.innerHTML =
          inv1StartPart +
          `<span>${escHtml(INV0.name)} Starts: <strong class="mono" style="color:#38bdf8;">${jStartBar ? jStartBar.fullLbl : moName0 + " " + (INV0.birth_year + ssnInv0ClaimAge)} (Age ${ssnInv0ClaimAge})</strong></span>` +
          `<span style="margin:0 6px;">&bull;</span>` +
          `<span>Tax Rule: <strong class="mono" style="color:var(--text);">${escHtml(ssnRuleLbl)}</strong></span>`;
      }
    }

    function onSsnInv0AgeInput(val) {
      ssnInv0ClaimAge = Math.max(62, Math.min(70, Math.round(parseFloat(val) || 67)));
      const sl = document.getElementById("ssn-inv0-age-slider");
      if (sl && parseInt(sl.value, 10) !== ssnInv0ClaimAge) {
        sl.value = String(ssnInv0ClaimAge);
      }
      recomputeSsnBars();
      recomputeSimBars();
      if (currentScreen === "income") {
        renderSsnChartAndNav();
        renderSsnTables();
      } else if (currentScreen === "retirement") {
        renderSimChartAndNav();
      }
      checkSaveDirtyState();
    }

    function onSsnInv1AgeInput(val) {
      ssnInv1ClaimAge = Math.max(62, Math.min(70, Math.round(parseFloat(val) || 67)));
      const sl = document.getElementById("ssn-inv1-age-slider");
      if (sl && parseInt(sl.value, 10) !== ssnInv1ClaimAge) {
        sl.value = String(ssnInv1ClaimAge);
      }
      recomputeSsnBars();
      recomputeSimBars();
      if (currentScreen === "income") {
        renderSsnChartAndNav();
        renderSsnTables();
      } else if (currentScreen === "retirement") {
        renderSimChartAndNav();
      }
      checkSaveDirtyState();
    }

    function onSsnColaInput(val) {
      if (val === undefined || val === null || val === "") {
        val = SSN_COLA_REF;
      }
      ssnColaPct = Math.max(0, Math.min(6, Math.round((parseFloat(val) || 0) * 10) / 10));
      const sl = document.getElementById("ssn-cola-slider");
      if (sl && parseFloat(sl.value) !== ssnColaPct) {
        sl.value = String(ssnColaPct);
      }
      const sub = document.getElementById("ssn-cola-subtext");
      if (sub && typeof SSN_COLA_SPAN !== "undefined") {
        sub.textContent = `Trailing 10-year historical SSA COLA average (${SSN_COLA_SPAN})`;
      }
      const pill = document.getElementById("ssn-cola-pill");
      if (pill) {
        pill.textContent = `${ssnColaPct.toFixed(1)}% / yr`;
        pill.className = "mdl-in-pill mono";
        pill.title = `Average Annual COLA (click to reset to reference ${SSN_COLA_REF.toFixed(1)}%/yr)`;
        pill.onclick = () => onSsnColaInput(SSN_COLA_REF);
      }
      const zeroBtn = document.getElementById("ssn-cola-zero-btn");
      if (zeroBtn) zeroBtn.classList.toggle("active", Math.abs(ssnColaPct) < 0.05);
      const refBtn = document.getElementById("ssn-cola-ref-btn");
      if (refBtn) {
        refBtn.textContent = `Ref: ${SSN_COLA_REF.toFixed(1)}%`;
        refBtn.classList.toggle("active", Math.abs(ssnColaPct - SSN_COLA_REF) < 0.05);
        refBtn.title = `Set to ${SSN_COLA_REF.toFixed(1)}%/yr reference rate: average Social Security COLA for ${SSN_COLA_SPAN}.`;
        refBtn.onclick = () => onSsnColaInput(SSN_COLA_REF);
      }
      recomputeSsnBars();
      recomputeSimBars();
      if (currentScreen === "income") {
        renderSsnChartAndNav();
        renderSsnTables();
      } else if (currentScreen === "retirement") {
        renderSimChartAndNav();
      }
      checkSaveDirtyState();
    }

    function setSsnDollarMode(mode) {
      ssnDollarMode = mode === "real" ? "real" : "nominal";
      const nomBtn = document.getElementById("ssn-dlr-nom-btn");
      const realBtn = document.getElementById("ssn-dlr-real-btn");
      if (nomBtn) nomBtn.classList.toggle("active", ssnDollarMode === "nominal");
      if (realBtn) realBtn.classList.toggle("active", ssnDollarMode === "real");
      const matNomBtn = document.getElementById("ssn-mat-dlr-nom-btn");
      const matRealBtn = document.getElementById("ssn-mat-dlr-real-btn");
      if (matNomBtn) matNomBtn.classList.toggle("active", ssnDollarMode === "nominal");
      if (matRealBtn) matRealBtn.classList.toggle("active", ssnDollarMode === "real");
      updateSsnSummaryDOM();
      renderSsnChartAndNav();
      renderSsnTables();
    }

    function clampSsnViewport(s, e) {
      const maxIdx = Math.max(0, SSN_BARS.length - 1);
      const minSpan = Math.min(MIN_SIM_VISIBLE_BARS - 1, maxIdx);
      let span = Math.max(minSpan, Math.min(maxIdx, e - s));
      let ns = s;
      let ne = s + span;
      if (ns < 0) { ns = 0; ne = span; }
      if (ne > maxIdx) { ne = maxIdx; ns = Math.max(0, maxIdx - span); }
      return [ns, ne];
    }

    function applySsnPreset(preset) {
      ssnActivePreset = preset;
      document.querySelectorAll("[data-ssn-preset]").forEach(b => {
        b.classList.toggle("active", b.dataset.ssnPreset === preset);
      });
      const maxIdx = Math.max(0, SSN_BARS.length - 1);
      if (preset === "10y") {
        [ssnViewStart, ssnViewEnd] = clampSsnViewport(0, Math.min(maxIdx, 120));
      } else if (preset === "20y") {
        [ssnViewStart, ssnViewEnd] = clampSsnViewport(0, Math.min(maxIdx, 240));
      } else if (preset === "30y") {
        [ssnViewStart, ssnViewEnd] = clampSsnViewport(0, Math.min(maxIdx, 360));
      } else {
        [ssnViewStart, ssnViewEnd] = [0, maxIdx];
      }
      renderSsnChartAndNav();
    }

    function renderSsnChartAndNav() {
      const svg = document.getElementById("ssn-svg");
      if (!svg || !SSN_BARS.length) return;
      const { W, H, padL, padR, padT, padB } = CHART_GEOM;
      const plotW = W - padL - padR;
      const plotH = H - padT - padB;

      const startIdx = Math.max(0, Math.floor(ssnViewStart));
      const endIdx = Math.min(SSN_BARS.length - 1, Math.ceil(ssnViewEnd));
      const visible = SSN_BARS.slice(startIdx, endIdx + 1);
      const n = visible.length;
      if (n === 0) return;

      const isReal = ssnDollarMode === "real";
      let maxVal = 0;
      for (const b of visible) {
        const v = isReal ? b.totRealGross : b.totNomGross;
        if (v > maxVal) maxVal = v;
      }
      if (maxVal <= 0) {
        for (const b of SSN_BARS) {
          const v = isReal ? b.totRealGross : b.totNomGross;
          if (v > maxVal) maxVal = v;
        }
      }
      if (maxVal <= 0) maxVal = 15000;
      const yTicks = 5;
      const yMax = niceAxisMax(maxVal, yTicks);
      window._ssnBarGeom = {};

      let out = [];
      out.push(`
        <defs>
          <clipPath id="ssn-plot-clip">
            <rect x="${padL}" y="${padT}" width="${plotW}" height="${plotH}" />
          </clipPath>
        </defs>
      `);

      // Y-Axis Gridlines & Labels (Monthly Benefit $)
      for (let i = 0; i <= yTicks; i++) {
        const v = (yMax / yTicks) * i;
        const y = padT + plotH - (v / yMax) * plotH;
        out.push(`<line x1="${padL}" y1="${y.toFixed(1)}" x2="${W - padR}" y2="${y.toFixed(1)}" stroke="#1e293b" stroke-width="1"/>`);
        out.push(`<text x="${padL - 10}" y="${(y + 4).toFixed(1)}" text-anchor="end" fill="#94a3b8" font-size="11" font-family="ui-monospace, monospace">${fmtAxisUSD(v)}/mo</text>`);
      }

      const slotW = plotW / n;
      const barW = Math.max(1.2, Math.min(24, slotW * 0.80));
      const xCenter = i => padL + (i + 0.5) * slotW;
      const yPos = v => padT + plotH - (v / yMax) * plotH;

      out.push(`<g clip-path="url(#ssn-plot-clip)">`);
      out.push(`<rect id="ssn-focus-slot-bg" x="0" y="${padT}" width="0" height="${plotH}" fill="rgba(56, 189, 248, 0.14)" display="none" pointer-events="none"/>`);

      let netLinePts = [];
      let hitRects = [];
      for (let i = 0; i < n; i++) {
        const b = visible[i];
        const globalIdx = startIdx + i;
        const slotX = padL + i * slotW;
        const cx = xCenter(i);
        const x = cx - barW / 2;

        const yGross = isReal ? b.yRealMo : b.yNomMo;
        const jGross = isReal ? b.jRealMo : b.jNomMo;
        const totNet = isReal ? b.totRealNet : b.totNomNet;

        let cum = 0;
        if (yGross > 0) {
          const y1 = yPos(cum + yGross);
          const y2 = yPos(cum);
          const h = Math.max(0.5, y2 - y1);
          out.push(`<rect x="${x.toFixed(2)}" y="${y1.toFixed(2)}" width="${barW.toFixed(2)}" height="${h.toFixed(2)}" fill="#10b981" opacity="0.92"/>`);
          cum += yGross;
        }
        if (jGross > 0) {
          const y1 = yPos(cum + jGross);
          const y2 = yPos(cum);
          const h = Math.max(0.5, y2 - y1);
          out.push(`<rect x="${x.toFixed(2)}" y="${y1.toFixed(2)}" width="${barW.toFixed(2)}" height="${h.toFixed(2)}" fill="#38bdf8" opacity="0.92"/>`);
          cum += jGross;
        }

        const topY = yPos(cum);
        const barH = Math.max(0, (padT + plotH) - topY);
        window._ssnBarGeom[globalIdx] = {
          slotX,
          slotW,
          bx: x,
          barW,
          cx,
          topY,
          barH,
        };

        const netY = yPos(totNet);
        netLinePts.push(`${cx.toFixed(1)},${netY.toFixed(1)}`);

        hitRects.push(`<rect x="${slotX.toFixed(2)}" y="${padT}" width="${slotW.toFixed(2)}" height="${plotH}" fill="transparent" style="cursor:crosshair;" onmouseenter="showSsnTooltip(event, ${globalIdx})" onmousemove="showSsnTooltip(event, ${globalIdx})" onmouseleave="hideTooltip()"/>`);
      }

      // Draw dashed After-Tax Net SSN line across bars
      if (netLinePts.length > 1) {
        out.push(`<polyline points="${netLinePts.join(' ')}" fill="none" stroke="#22d3ee" stroke-width="1.8" stroke-dasharray="4,3" pointer-events="none"/>`);
      }

      out.push(`<rect id="ssn-focus-bar-ring" x="0" y="0" width="0" height="0" fill="none" stroke="#f8fafc" stroke-width="1.6" rx="1.5" display="none" pointer-events="none"/>`);
      out.push(`<circle id="ssn-focus-bar-dot" cx="0" cy="0" r="3" fill="#22d3ee" stroke="#0f172a" stroke-width="1.2" display="none" pointer-events="none"/>`);
      out.push(hitRects.join(""));

      // Milestone Vertical Lines: Investor Draw Start(s) & Milestone(s)
      const msAge0 = INV0.milestone_age || 85;
      const jStartIdx = SSN_BARS.findIndex(b => b.jActive);
      const j85Idx = SSN_BARS.findIndex((b, idx) => ageOnIso(INV0.birth_year, INV0.birth_month, INV0.birth_day, b.iso, idx) >= msAge0);
      const moName0 = MONTH_NAMES_SHORT[(INV0.birth_month || 1) - 1];

      const milestones = [];
      if (HAS_SECOND_INVESTOR) {
        const msAge1 = INV1.milestone_age || 85;
        const yStartIdx = SSN_BARS.findIndex(b => b.yActive);
        const y85Idx = SSN_BARS.findIndex((b, idx) => ageOnIso(INV1.birth_year, INV1.birth_month, INV1.birth_day, b.iso, idx) >= msAge1);
        const moName1 = MONTH_NAMES_SHORT[(INV1.birth_month || 1) - 1];
        milestones.push(
          { idx: yStartIdx, color: "#10b981", textColor: "#6ee7b7", row: 0, label: `${INV1.name} Starts (Age ${ssnInv1ClaimAge})` },
          { idx: y85Idx, color: "#14b8a6", textColor: "#99f6e4", row: 0, label: `${INV1.name}'s ${msAge1}th (${moName1} '${String(INV1.birth_year + msAge1).slice(2)})` }
        );
      }
      const row0Or1 = HAS_SECOND_INVESTOR ? 1 : 0;
      milestones.push(
        { idx: jStartIdx, color: "#38bdf8", textColor: "#7dd3fc", row: row0Or1, label: `${INV0.name} Starts (Age ${ssnInv0ClaimAge})` },
        { idx: j85Idx, color: "#2dd4bf", textColor: "#ccfbf1", row: row0Or1, label: `${INV0.name}'s ${msAge0}th (${moName0} '${String(INV0.birth_year + msAge0).slice(2)})` }
      );

      milestones.forEach(ms => {
        if (ms.idx >= startIdx && ms.idx <= endIdx) {
          const mLocal = ms.idx - startIdx;
          const mX = xCenter(mLocal);
          const badgeW = 148;
          const badgeX = Math.min(Math.max(padL + 4, mX - badgeW / 2), W - padR - badgeW - 4);
          const badgeY = padT + 6 + ms.row * 22;
          out.push(`<line x1="${mX.toFixed(1)}" y1="${padT}" x2="${mX.toFixed(1)}" y2="${padT + plotH}" stroke="${ms.color}" stroke-width="1.7" stroke-dasharray="4,3" pointer-events="none"/>`);
          out.push(`<rect x="${badgeX.toFixed(1)}" y="${badgeY}" width="${badgeW}" height="18" rx="4" fill="#0f172a" stroke="${ms.color}" stroke-width="1" pointer-events="none"/>`);
          out.push(`<text x="${(badgeX + badgeW / 2).toFixed(1)}" y="${badgeY + 12.5}" text-anchor="middle" fill="${ms.textColor}" font-size="10.2" font-weight="600" font-family="ui-monospace, monospace" pointer-events="none">${escHtml(ms.label)}</text>`);
        }
      });

      out.push(`</g>`);

      // X-Axis Labels
      const maxLabels = 12;
      const step = Math.max(1, Math.ceil(n / maxLabels));
      for (let i = 0; i < n; i += step) {
        const b = visible[i];
        const cx = xCenter(i);
        const lbl = n > 140 ? b.yearLbl : b.monthLbl;
        out.push(`<line x1="${cx.toFixed(1)}" y1="${padT + plotH}" x2="${cx.toFixed(1)}" y2="${padT + plotH + 5}" stroke="#475569"/>`);
        out.push(`<text x="${cx.toFixed(1)}" y="${padT + plotH + 19}" text-anchor="middle" fill="#94a3b8" font-size="11" font-family="ui-monospace, monospace">${escHtml(lbl)}</text>`);
      }

      svg.innerHTML = out.join("");

      renderSsnNavigator();
      renderSsnLegend();
    }

    function renderSsnNavigator() {
      const nav = document.getElementById("ssn-nav-svg");
      if (!nav || !SSN_BARS.length) return;
      const { W, H, padL, padR, padT, padB } = NAV_GEOM;
      const plotW = W - padL - padR;
      const plotH = H - padT - padB;
      const totalN = SSN_BARS.length;

      const isReal = ssnDollarMode === "real";
      let maxAll = 0;
      SSN_BARS.forEach(b => {
        const v = isReal ? b.totRealGross : b.totNomGross;
        if (v > maxAll) maxAll = v;
      });
      if (maxAll <= 0) maxAll = 1;

      const xFromIdx = i => padL + (i / Math.max(1, totalN - 1)) * plotW;
      let totPts = [`${padL},${padT + plotH}`];
      let inv1Pts = [`${padL},${padT + plotH}`];

      SSN_BARS.forEach((b, i) => {
        const x = xFromIdx(i);
        const vTot = isReal ? b.totRealGross : b.totNomGross;
        const vInv1 = isReal ? b.yRealMo : b.yNomMo;
        const yT = padT + plotH - (vTot / (maxAll * 1.08)) * plotH;
        const yY = padT + plotH - (vInv1 / (maxAll * 1.08)) * plotH;
        totPts.push(`${x.toFixed(1)},${yT.toFixed(1)}`);
        inv1Pts.push(`${x.toFixed(1)},${yY.toFixed(1)}`);
      });
      totPts.push(`${padL + plotW},${padT + plotH}`);
      inv1Pts.push(`${padL + plotW},${padT + plotH}`);

      const winX1 = xFromIdx(ssnViewStart);
      const winX2 = xFromIdx(ssnViewEnd);
      const winW = Math.max(8, winX2 - winX1);

      let out = [];
      out.push(`<rect x="${padL}" y="${padT}" width="${plotW}" height="${plotH}" rx="4" fill="#0b1324" stroke="#1e293b"/>`);
      out.push(`<polygon points="${totPts.join(' ')}" fill="rgba(56, 189, 248, 0.36)"/>`);
      if (HAS_SECOND_INVESTOR) {
        out.push(`<polygon points="${inv1Pts.join(' ')}" fill="rgba(16, 185, 129, 0.38)"/>`);
      }

      let prevYr = "";
      SSN_BARS.forEach((b, i) => {
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

      const j85Idx = SSN_BARS.findIndex((b, idx) => ageOnIso(INV0.birth_year, INV0.birth_month, INV0.birth_day, b.iso, idx) >= (INV0.milestone_age || 85));
      if (j85Idx >= 0) {
        const jX = xFromIdx(j85Idx);
        out.push(`<line x1="${jX.toFixed(1)}" y1="${padT}" x2="${jX.toFixed(1)}" y2="${padT + plotH}" stroke="#2dd4bf" stroke-width="1.4" stroke-dasharray="2,2"/>`);
      }

      out.push(`<rect x="${padL}" y="${padT}" width="${Math.max(0, winX1 - padL).toFixed(1)}" height="${plotH}" fill="rgba(2, 6, 23, 0.58)" style="cursor:pointer;" data-ssn-nav-action="jump"/>`);
      out.push(`<rect x="${(winX1 + winW).toFixed(1)}" y="${padT}" width="${Math.max(0, padL + plotW - (winX1 + winW)).toFixed(1)}" height="${plotH}" fill="rgba(2, 6, 23, 0.58)" style="cursor:pointer;" data-ssn-nav-action="jump"/>`);

      out.push(`<rect x="${winX1.toFixed(1)}" y="${padT}" width="${winW.toFixed(1)}" height="${plotH}" fill="rgba(56, 189, 248, 0.16)" stroke="#38bdf8" stroke-width="1.4" style="cursor:grab;" data-ssn-nav-action="move"/>`);
      out.push(`<rect x="${(winX1 - 4).toFixed(1)}" y="${padT + 6}" width="8" height="${plotH - 12}" rx="3" fill="#7dd3fc" style="cursor:ew-resize;" data-ssn-nav-action="left"/>`);
      out.push(`<rect x="${(winX1 + winW - 4).toFixed(1)}" y="${padT + 6}" width="8" height="${plotH - 12}" rx="3" fill="#7dd3fc" style="cursor:ew-resize;" data-ssn-nav-action="right"/>`);

      nav.innerHTML = out.join("");

      const sIdx = Math.max(0, Math.floor(ssnViewStart));
      const eIdx = Math.min(totalN - 1, Math.ceil(ssnViewEnd));
      const rangeEl = document.getElementById("ssn-nav-range-text");
      if (rangeEl) {
        rangeEl.textContent = `${SSN_BARS[sIdx].fullLbl} (+${SSN_BARS[sIdx].yearsOut.toFixed(1)}y) → ${SSN_BARS[eIdx].fullLbl} (+${SSN_BARS[eIdx].yearsOut.toFixed(1)}y)`;
      }
    }

    function renderSsnLegend() {
      const leg = document.getElementById("ssn-legend");
      if (!leg) return;
      const ssnStateCode = String(SSN_MODEL.state_code || "").trim();
      const ssnStateNote = ssnStateCode ? `0% ${ssnStateCode} Tax` : "0% State Tax";
      const inv1Item = HAS_SECOND_INVESTOR
        ? `<span class="legend-item"><span class="swatch" style="background:#10b981;"></span>${escHtml(INV1.name)} Gross SSN (Draws Age ${ssnInv1ClaimAge}+ • FRA PIA $${Math.round(SSN_INV1_PIA_2026).toLocaleString("en-US")}/mo)</span>`
        : "";
      const netPctStr = ((1 - SSN_EFF_TAX_RATE) * 100).toFixed(2).replace(/\.00$/, "") + "%";
      leg.innerHTML = `
        ${inv1Item}
        <span class="legend-item"><span class="swatch" style="background:#38bdf8;"></span>${escHtml(INV0.name)} Gross SSN (Draws Age ${ssnInv0ClaimAge}+ • FRA PIA $${Math.round(SSN_INV0_PIA_2026).toLocaleString("en-US")}/mo)</span>
        <span class="legend-item" title="Standalone reference net line using ${(SSN_EFF_TAX_RATE * 100).toFixed(2)}% Fed tax; in the Retirement Simulator, Social Security is taxed dynamically via IRC §86 provisional income (0%/50%/85% inclusion)."><span style="display:inline-block;width:16px;height:0;border-top:2px dashed #22d3ee;margin-right:6px;vertical-align:middle;"></span>Reference Net Household SSN (${netPctStr} after ${(SSN_EFF_TAX_RATE * 100).toFixed(2)}% Ref Fed Tax; ${escHtml(ssnStateNote)} &bull; IRC &sect;86 dynamic in Simulator)</span>
      `;
    }

    function showSsnTooltip(evt, idx) {
      const b = SSN_BARS[idx];
      if (!b) return;
      highlightBarInSvg(window._ssnBarGeom, idx, "ssn-");

      const tip = document.getElementById("chart-tooltip");
      const isReal = ssnDollarMode === "real";
      const jGross = isReal ? b.jRealMo : b.jNomMo;
      const yGross = isReal ? b.yRealMo : b.yNomMo;
      const totGross = isReal ? b.totRealGross : b.totNomGross;
      const totTax = isReal ? b.totRealTax : b.totNomTax;
      const totNet = isReal ? b.totRealNet : b.totNomNet;

      const cumJ = isReal ? b.cumJReal : b.cumJNom;
      const cumY = isReal ? b.cumYReal : b.cumYNom;
      const cumTot = isReal ? b.cumTotReal : b.cumTotNom;
      const cumNet = isReal ? b.cumNetReal : b.cumNetNom;
      const netPctStr = ((1 - SSN_EFF_TAX_RATE) * 100).toFixed(2).replace(/\.00$/, "") + "%";

      const inv1AgeLine = HAS_SECOND_INVESTOR
        ? ` &bull; ${escHtml(INV1.name)}: <strong style="color:#10b981;">Age ${b.yAge}</strong> (${b.yActive ? "Drawing" : `Starts Age ${ssnInv1ClaimAge}`})`
        : "";
      const inv1CheckRow = HAS_SECOND_INVESTOR
        ? `<div style="display:flex;justify-content:space-between;margin:2px 0;">
            <span><span class="swatch" style="background:#10b981;width:8px;height:8px;margin-right:5px;"></span>${escHtml(INV1.name)} Monthly Check:</span>
            <strong>${fmtUSD0(yGross)}/mo (${fmtUSD0(yGross * SSN_NET_FACTOR)} net)</strong>
          </div>`
        : "";
      const inv1CumRow = HAS_SECOND_INVESTOR
        ? `<div style="display:flex;justify-content:space-between;margin:1.5px 0;">
            <span>${escHtml(INV1.name)} Cumulative:</span>
            <strong style="color:var(--text);">${fmtUSD0(cumY)}</strong>
          </div>`
        : "";

      tip.innerHTML = `
        <div style="display:flex;justify-content:space-between;align-items:center;gap:10px;">
          <strong class="mono">${escHtml(b.fullLbl)} (${escHtml(b.iso)})</strong>
          <span class="pill ${idx === 0 ? 'pill-blue' : 'pill-green'} mono">${idx === 0 ? "TODAY" : `+${b.yearsOut.toFixed(1)} YRS (M${idx})`}</span>
        </div>
        <div class="mono muted" style="font-size:11.5px;margin-top:2px;margin-bottom:4px;">
          ${escHtml(INV0.name)}: <strong style="color:#38bdf8;">Age ${b.jAge}</strong> (${b.jActive ? "Drawing" : `Starts Age ${ssnInv0ClaimAge}`})${inv1AgeLine}
        </div>
        <div class="mono" style="font-size:15.5px;font-weight:700;margin:2px 0;">
          ${fmtUSD0(totGross)} / mo gross <span class="muted" style="font-size:11.5px;font-weight:500;">(${fmtUSD0(totGross * 12)}/yr${isReal ? ", Today's $" : ""})</span>
        </div>
        <div class="mono" style="font-size:12px;color:#22d3ee;margin-bottom:5px;">
          Reference Net (${netPctStr}): <strong>${fmtUSD0(totNet)} / mo</strong> <span class="muted">(&minus;${fmtUSD0(totTax)}/mo Ref Fed; IRC &sect;86 in Sim)</span>
        </div>
        <div style="border-top:1px solid #26334d;padding-top:5px;margin-top:4px;font-size:11.3px;" class="mono">
          ${inv1CheckRow}
          <div style="display:flex;justify-content:space-between;margin:2px 0;">
            <span><span class="swatch" style="background:#38bdf8;width:8px;height:8px;margin-right:5px;"></span>${escHtml(INV0.name)} Monthly Check:</span>
            <strong>${fmtUSD0(jGross)}/mo (${fmtUSD0(jGross * SSN_NET_FACTOR)} net)</strong>
          </div>
        </div>
        <div style="border-top:1px dashed rgba(51,65,85,0.85);padding-top:5px;margin-top:5px;font-size:11px;" class="mono muted">
          <div style="text-transform:uppercase;font-size:10px;letter-spacing:0.04em;margin-bottom:2px;">Cumulative SSN Collected to Date${isReal ? " (Today's $)" : ""}:</div>
          <div style="display:flex;justify-content:space-between;margin:1.5px 0;">
            <span>${escHtml(INV0.name)} Cumulative:</span>
            <strong style="color:var(--text);">${fmtUSD0(cumJ)}</strong>
          </div>
          ${inv1CumRow}
          <div style="display:flex;justify-content:space-between;margin:1.5px 0;color:#34d399;">
            <span>${HAS_SECOND_INVESTOR ? "Combined" : "Total"} Gross &rarr; Net:</span>
            <strong>${fmtUSD0(cumTot)} &rarr; ${fmtUSD0(cumNet)} net</strong>
          </div>
        </div>
      `;

      placeFloatingBoxAwayFromPointer(tip, evt.clientX, evt.clientY);
    }

    function renderSsnTables() {
      const matBody = document.getElementById("ssn-matrix-tbody");
      const modePill = document.getElementById("ssn-matrix-mode-pill");
      const formEl = document.getElementById("ssn-formula-summary");
      const histBody = document.getElementById("ssn-history-tbody");
      const isReal = ssnDollarMode === "real";

      if (modePill) {
        modePill.textContent = isReal ? "Showing Today's $" : `Showing Nominal $ (${ssnColaPct.toFixed(1)}% COLA)`;
      }

      if (matBody) {
        const rows = [];
        let bestJAge = 67, bestJVal = -1;
        let bestYAge = 67, bestYVal = -1;
        for (let age = 62; age <= 70; age++) {
          const sim = simulateSsnTotalsForAges(age, age, ssnColaPct);
          const jTot = isReal ? sim.jBy85Real : sim.jBy85Nom;
          const yTot = isReal ? sim.yBy85Real : sim.yBy85Nom;
          if (jTot > bestJVal) { bestJVal = jTot; bestJAge = age; }
          if (yTot > bestYVal) { bestYVal = yTot; bestYAge = age; }
          rows.push({ age, sim, jTot, yTot, combTot: isReal ? sim.combByJ85Real : sim.combByJ85Nom });
        }

        // DESIGN INTENT (`DA2-18`): Derive statutory FRA from `INV0.fra_age` / `ssaFraForBirthYear(INV0.birth_year)`
        // (42 U.S.C. §416(l) 1937–1960 schedule) rather than hardcoding Age 67.
        const fra0 = INV0.fra_age != null ? Number(INV0.fra_age) : ssaFraForBirthYear(INV0.birth_year);
        const fra0Round = Math.round(fra0);
        const fra0BadgeTxt = Math.abs(fra0 - fra0Round) < 0.01
          ? "FRA"
          : `FRA ${Math.floor(fra0)}y ${Math.round((fra0 % 1) * 12)}m`;
        matBody.innerHTML = rows.map(r => {
          const isSelected = HAS_SECOND_INVESTOR
            ? (r.age === ssnInv0ClaimAge && r.age === ssnInv1ClaimAge)
            : (r.age === ssnInv0ClaimAge);
          const isJSelected = r.age === ssnInv0ClaimAge;
          const isYSelected = HAS_SECOND_INVESTOR && r.age === ssnInv1ClaimAge;
          const jMo = isReal ? r.sim.jFirstMoReal : r.sim.jFirstMoNom;
          const yMo = isReal ? r.sim.yFirstMoReal : r.sim.yFirstMoNom;
          const rowBg = isSelected ? "background:rgba(56,189,248,0.12);" : "";
          const fraBadge = r.age === fra0Round ? ` <span class="pill pill-blue mono" style="font-size:9.5px;padding:1px 5px;" title="Full Retirement Age (${fra0BadgeTxt}): 100% of Primary Insurance Amount (PIA) with no early claiming penalty or delayed retirement credits.">${escHtml(fra0BadgeTxt)}</span>` : "";
          const clickAttr = HAS_SECOND_INVESTOR
            ? `onSsnInv0AgeInput(${r.age});onSsnInv1AgeInput(${r.age});`
            : `onSsnInv0AgeInput(${r.age});`;
          const rowTitle = HAS_SECOND_INVESTOR
            ? `Click to set both First Draw Ages to ${r.age}`
            : `Click to set First Draw Age to ${r.age}`;
          const inv1Cells = HAS_SECOND_INVESTOR
            ? `<td class="num mono" style="${isYSelected ? 'color:#10b981;font-weight:700;' : ''}">${fmtUSD0(yMo)}/mo<div class="row-sub mono">${r.sim.yStartLbl}</div></td>
               <td class="num mono" style="${isYSelected ? 'color:#10b981;font-weight:700;' : ''}">${fmtUSD0(r.yTot)}</td>`
            : "";

          return `<tr style="cursor:pointer;${rowBg}" onclick="${clickAttr}" title="${rowTitle}">
            <td class="mono"><strong>Age ${r.age}</strong>${fraBadge}</td>
            <td class="num mono">${r.sim.jMultPct.toFixed(1)}%</td>
            <td class="num mono" style="${isJSelected ? 'color:#38bdf8;font-weight:700;' : ''}">${fmtUSD0(jMo)}/mo<div class="row-sub mono">${r.sim.jStartLbl}</div></td>
            <td class="num mono" style="${isJSelected ? 'color:#38bdf8;font-weight:700;' : ''}">${fmtUSD0(r.jTot)}</td>
            ${inv1Cells}
            <td class="num mono pos"><strong>${fmtUSD0(r.combTot)}</strong><div class="row-sub mono">${fmtUSD0(r.combTot * SSN_NET_FACTOR)} net</div></td>
          </tr>`;
        }).join("");
      }

      if (formEl && INV0) {
        const j = INV0;
        const moName0 = MONTH_NAMES_SHORT[(j.birth_month || 1) - 1];
        let inv1CardHtml = "";
        if (HAS_SECOND_INVESTOR && INV1) {
          const y = INV1;
          const moName1 = MONTH_NAMES_SHORT[(y.birth_month || 1) - 1];
          const yNoXml = !y.has_statement && !(y.pia_monthly_2026 > 0);
          inv1CardHtml = `
            <div style="background:rgba(15,23,42,0.7);border:1px solid #1e293b;border-radius:8px;padding:12px 14px;">
              <div style="display:flex;justify-content:space-between;align-items:center;">
                <strong style="color:#10b981;font-size:13px;">${escHtml(y.name)} (Born ${moName1} ${y.birth_year})</strong>
                <span class="pill ${yNoXml ? 'pill-amber' : 'pill-green'} mono" style="font-size:11px;padding:2px 8px;">PIA: ${fmtUSD2(y.pia_monthly_2026 || 0)}/mo</span>
              </div>
              <div class="muted" style="font-size:11.3px;margin-top:3px;${yNoXml ? 'color:#fcd34d;' : ''}">${escHtml(y.work_span_label || "")} · ${escHtml(y.benefit_basis || "")}</div>
              <div class="mono" style="font-size:11.8px;margin-top:6px;padding:6px 10px;background:rgba(0,0,0,0.3);border-radius:5px;border:1px solid rgba(16,185,129,0.15);">
                35-Yr Indexed Earnings: <strong>${fmtUSD0(y.sum_indexed_35 || 0)}</strong> &divide; 420 mos = <strong style="color:#10b981;">${fmtUSD0(y.aime || 0)}/mo AIME</strong>
              </div>
              <div style="margin-top:8px;display:grid;grid-template-columns:repeat(3,1fr);gap:6px;" class="mono">
                <div style="background:rgba(255,255,255,0.03);padding:6px 8px;border-radius:4px;border:1px solid rgba(51,65,85,0.5);">
                  <span class="muted" style="font-size:10px;display:block;">90% &le; ${fmtUSD0(y.bend1 ?? 1286)}:</span>
                  <strong style="color:#f8fafc;">$${(y.tier1_90pct || 0).toFixed(2)}</strong>
                </div>
                <div style="background:rgba(255,255,255,0.03);padding:6px 8px;border-radius:4px;border:1px solid rgba(51,65,85,0.5);">
                  <span class="muted" style="font-size:10px;display:block;">32% &le; ${fmtUSD0(y.bend2 ?? 7749)}:</span>
                  <strong style="color:#f8fafc;">$${(y.tier2_32pct || 0).toFixed(2)}</strong>
                </div>
                <div style="background:rgba(255,255,255,0.03);padding:6px 8px;border-radius:4px;border:1px solid rgba(51,65,85,0.5);">
                  <span class="muted" style="font-size:10px;display:block;">15% &gt; ${fmtUSD0(y.bend2 ?? 7749)}:</span>
                  <strong style="color:#f8fafc;">$${(y.tier3_15pct || 0).toFixed(2)}</strong>
                </div>
              </div>
            </div>`;
        }
        const jNoXml = !j.has_statement && !(j.pia_monthly_2026 > 0);
        const gridCols = HAS_SECOND_INVESTOR ? "repeat(2,minmax(0,1fr))" : "minmax(0,1fr)";
        formEl.innerHTML = `
          <div style="display:grid;grid-template-columns:${gridCols};gap:12px;">
            <div style="background:rgba(15,23,42,0.7);border:1px solid #1e293b;border-radius:8px;padding:12px 14px;">
              <div style="display:flex;justify-content:space-between;align-items:center;">
                <strong style="color:#38bdf8;font-size:13px;">${escHtml(j.name)} (Born ${moName0} ${j.birth_year})</strong>
                <span class="pill ${jNoXml ? 'pill-amber' : 'pill-blue'} mono" style="font-size:11px;padding:2px 8px;">PIA: ${fmtUSD2(j.pia_monthly_2026 || 0)}/mo</span>
              </div>
              <div class="muted" style="font-size:11.3px;margin-top:3px;${jNoXml ? 'color:#fcd34d;' : ''}">${escHtml(j.work_span_label || "")} · ${escHtml(j.benefit_basis || "")}</div>
              <div class="mono" style="font-size:11.8px;margin-top:6px;padding:6px 10px;background:rgba(0,0,0,0.3);border-radius:5px;border:1px solid rgba(56,189,248,0.15);">
                35-Yr Indexed Earnings: <strong>${fmtUSD0(j.sum_indexed_35 || 0)}</strong> &divide; 420 mos = <strong style="color:#38bdf8;">${fmtUSD0(j.aime || 0)}/mo AIME</strong>
              </div>
              <div style="margin-top:8px;display:grid;grid-template-columns:repeat(3,1fr);gap:6px;" class="mono">
                <div style="background:rgba(255,255,255,0.03);padding:6px 8px;border-radius:4px;border:1px solid rgba(51,65,85,0.5);">
                  <span class="muted" style="font-size:10px;display:block;">90% &le; ${fmtUSD0(j.bend1 ?? 1286)}:</span>
                  <strong style="color:#f8fafc;">$${(j.tier1_90pct || 0).toFixed(2)}</strong>
                </div>
                <div style="background:rgba(255,255,255,0.03);padding:6px 8px;border-radius:4px;border:1px solid rgba(51,65,85,0.5);">
                  <span class="muted" style="font-size:10px;display:block;">32% &le; ${fmtUSD0(j.bend2 ?? 7749)}:</span>
                  <strong style="color:#f8fafc;">$${(j.tier2_32pct || 0).toFixed(2)}</strong>
                </div>
                <div style="background:rgba(255,255,255,0.03);padding:6px 8px;border-radius:4px;border:1px solid rgba(51,65,85,0.5);">
                  <span class="muted" style="font-size:10px;display:block;">15% &gt; ${fmtUSD0(j.bend2 ?? 7749)}:</span>
                  <strong style="color:#f8fafc;">$${(j.tier3_15pct || 0).toFixed(2)}</strong>
                </div>
              </div>
            </div>
            ${inv1CardHtml}
          </div>
        `;
      }

      if (histBody && INV0) {
        const jHist = INV0.history || [];
        const yHist = HAS_SECOND_INVESTOR && INV1 ? (INV1.history || []) : [];
        histBody.innerHTML = jHist.map((jr, idx) => {
          const yr = yHist[idx] || {};
          const inv1Td = HAS_SECOND_INVESTOR
            ? `<td class="num mono">${yr.capped_earnings > 0 ? `${fmtUSD0(yr.capped_earnings)} &rarr; <strong style="color:#10b981;">${fmtUSD0(yr.indexed_earnings)}</strong>` : '<span class="muted">$0</span>'}</td>`
            : "";
          return `<tr>
            <td class="mono"><strong>${jr.year}</strong></td>
            <td class="num mono">${fmtUSD0(jr.oasdi_cap)}</td>
            <td class="num mono">${jr.index_factor.toFixed(2)}&times;</td>
            <td class="num mono">${jr.capped_earnings > 0 ? `${fmtUSD0(jr.capped_earnings)} &rarr; <strong>${fmtUSD0(jr.indexed_earnings)}</strong>` : '<span class="muted">$0 (Not working)</span>'}</td>
            ${inv1Td}
          </tr>`;
        }).reverse().join("");
      }
    }

    function toggleSsnAimeCollapse() {
      const content = document.getElementById("ssn-aime-content");
      const arrow = document.getElementById("ssn-aime-arrow");
      const hint = document.getElementById("ssn-aime-toggle-hint");
      if (!content) return;
      const isHidden = content.style.display === "none" || content.style.display === "";
      content.style.display = isHidden ? "block" : "none";
      if (arrow) arrow.innerHTML = isHidden ? "&#9660;" : "&#9654;";
      if (hint) hint.innerHTML = isHidden ? "Hide Calculation Breakdown &uarr;" : "Show Calculation Breakdown &darr;";
    }
    window.toggleSsnAimeCollapse = toggleSsnAimeCollapse;

    const SAFE_TOOLTIP_TAGS = new Set([
      "B", "STRONG", "I", "EM", "BR", "CODE", "SPAN", "DIV", "UL", "LI", "HR", "SMALL", "SUP", "SUB",
      "TABLE", "CAPTION", "THEAD", "TBODY", "TR", "TH", "TD"
    ]);
    const STRIP_TOOLTIP_TAGS = new Set([
      "SCRIPT", "STYLE", "IMG", "SVG", "MATH", "IFRAME", "FRAME", "FRAMESET",
      "OBJECT", "EMBED", "APPLET", "NOSCRIPT", "TEMPLATE", "LINK", "META",
      "BASE", "FORM", "INPUT", "BUTTON", "SELECT", "TEXTAREA", "VIDEO", "AUDIO",
      "SOURCE", "TRACK", "CANVAS"
    ]);

    function sanitizeTooltipHtml(rawHtml) {
      const src = String(rawHtml || "");
      if (!src) return "";
      if (typeof DOMParser === "undefined") {
        // String-level allowlist sanitizer for headless/Node VM environments without DOMParser
        let s = src
          .replace(/<!--[\s\S]*?-->/g, "")
          .replace(/<(script|style|iframe|frame|frameset|object|embed|applet|noscript|template|svg|math|form)[^>]*>[\s\S]*?<\/\1>/gi, "")
          .replace(/<(script|style|img|svg|math|iframe|frame|frameset|object|embed|applet|link|meta|base|form|input|button|select|textarea|video|audio|source|track|canvas)\b[^>]*\/?>/gi, "");
        return s.replace(/<\/?([a-zA-Z0-9:-]+)(\s[^>]*)?>/g, (full, rawTag, rawAttrs) => {
          const upper = String(rawTag || "").toUpperCase();
          if (!SAFE_TOOLTIP_TAGS.has(upper)) return "";
          if (full.startsWith("</")) return `</${upper.toLowerCase()}>`;
          if (upper === "BR" || upper === "HR") return `<${upper.toLowerCase()}>`;
          let safeAttrs = "";
          if (rawAttrs) {
            const clsM = rawAttrs.match(/\bclass\s*=\s*(?:"([^"]*)"|'([^']*)')/i);
            if (clsM) {
              const cv = clsM[1] !== undefined ? clsM[1] : clsM[2];
              safeAttrs += ` class="${escHtml(cv)}"`;
            }
            const styM = rawAttrs.match(/\bstyle\s*=\s*(?:"([^"]*)"|'([^']*)')/i);
            if (styM) {
              const sv = styM[1] !== undefined ? styM[1] : styM[2];
              if (!/url\s*\(|expression\s*\(|javascript\s*:|-moz-binding|@import/i.test(sv)) {
                safeAttrs += ` style="${escHtml(sv)}"`;
              }
            }
          }
          return `<${upper.toLowerCase()}${safeAttrs}>`;
        });
      }
      const doc = new DOMParser().parseFromString(src, "text/html");
      const rootNodes = doc && doc.body ? Array.from(doc.body.childNodes) : [];
      if (!doc) return escHtml(src);

      const outWrap = doc.createElement("div");
      function cleanNode(node) {
        if (!node) return null;
        if (node.nodeType === 3) {
          return doc.createTextNode(node.nodeValue || "");
        }
        if (node.nodeType !== 1) {
          return null;
        }
        const tag = String(node.tagName || "").toUpperCase();
        if (STRIP_TOOLTIP_TAGS.has(tag)) {
          return null;
        }
        if (!SAFE_TOOLTIP_TAGS.has(tag)) {
          const frag = doc.createDocumentFragment();
          Array.from(node.childNodes).forEach(ch => {
            const cleaned = cleanNode(ch);
            if (cleaned) frag.appendChild(cleaned);
          });
          return frag;
        }
        const el = doc.createElement(tag.toLowerCase());
        const clsVal = node.getAttribute("class");
        if (clsVal) {
          el.setAttribute("class", clsVal);
        }
        const styleVal = node.getAttribute("style");
        if (styleVal && !/url\s*\(|expression\s*\(|javascript\s*:|-moz-binding|@import/i.test(styleVal)) {
          el.setAttribute("style", styleVal);
        }
        Array.from(node.childNodes).forEach(ch => {
          const cleaned = cleanNode(ch);
          if (cleaned) el.appendChild(cleaned);
        });
        return el;
      }

      rootNodes.forEach(n => {
        const cleaned = cleanNode(n);
        if (cleaned) outWrap.appendChild(cleaned);
      });
      return outWrap.innerHTML;
    }
    if (typeof window !== "undefined") {
      window.sanitizeTooltipHtml = sanitizeTooltipHtml;
    }

    // Delegated UI tooltip for table headers & cells (`[data-tip]`), also >= 1cm away from pointer
    function bindUITooltips() {
      const uiTip = document.getElementById("ui-tooltip");
      const chartTip = document.getElementById("chart-tooltip");
      const timelineSvgSel = "#weekly-svg, #releases-svg, #sim-svg, #ssn-svg";
      let activeTipEl = null;

      function hideAllTooltips() {
        activeTipEl = null;
        if (uiTip) uiTip.style.display = "none";
        if (typeof hideTooltip === "function") hideTooltip();
        if (typeof hidePieTooltip === "function") hidePieTooltip();
      }

      document.addEventListener("mouseover", e => {
        const target = e.target && e.target.closest ? e.target.closest("[data-tip]") : null;
        if (!target) return;
        const msg = target.getAttribute("data-tip");
        if (!msg) return;
        activeTipEl = target;
        // Never leave a chart bar tooltip open behind a Scheduled Action / UI tooltip
        if (typeof hideTooltip === "function" && target.closest(timelineSvgSel)) {
          hideTooltip();
        }
        // SECURITY DO (SA2-14): Always pass `[data-tip]` HTML through `sanitizeTooltipHtml(msg)` before
        // assigning `uiTip.innerHTML`, because `target.getAttribute("data-tip")` automatically decodes
        // HTML entities (e.g. `&lt;img onerror=...&gt;` -> `<img onerror=...>`) from user/statement text.
        // SECURITY DON'T: Never assign raw `msg` directly to `uiTip.innerHTML`.
        if (msg.includes("<") && msg.includes(">")) {
          uiTip.innerHTML = sanitizeTooltipHtml(msg);
        } else {
          uiTip.textContent = msg;
        }
        placeFloatingBoxAwayFromPointer(uiTip, e.clientX, e.clientY);
      });

      document.addEventListener("mousemove", e => {
        const targetEl = e.target && e.target.nodeType === 1 ? e.target : null;
        if (activeTipEl) {
          if (!activeTipEl.isConnected || !targetEl || !activeTipEl.contains(targetEl)) {
            activeTipEl = null;
            if (uiTip) uiTip.style.display = "none";
          } else {
            placeFloatingBoxAwayFromPointer(uiTip, e.clientX, e.clientY);
          }
        }
        if (chartTip && chartTip.style.display !== "none") {
          const inTimelineSvg = targetEl && targetEl.closest ? targetEl.closest(timelineSvgSel) : null;
          const inPieSvg = targetEl && targetEl.closest ? targetEl.closest("#pie-svg") : null;
          const onHitRect = Boolean(inTimelineSvg && targetEl && targetEl.tagName && targetEl.tagName.toLowerCase() === "rect" && (targetEl.hasAttribute("onmousemove") || targetEl.hasAttribute("onmouseenter")));
          if (!inPieSvg && !onHitRect) {
            if (typeof hideTooltip === "function") hideTooltip();
            if (typeof hidePieTooltip === "function") hidePieTooltip();
          }
        }
      });

      document.addEventListener("mouseout", e => {
        if (!activeTipEl) return;
        const related = e.relatedTarget;
        if (related && activeTipEl.contains(related)) return;
        activeTipEl = null;
        if (uiTip) uiTip.style.display = "none";
      });

      document.documentElement?.addEventListener?.("mouseleave", hideAllTooltips);
      window.addEventListener("blur", hideAllTooltips);
      window.addEventListener("scroll", hideAllTooltips, { passive: true, capture: true });
    }

    /** Bind pan, navigator jump/brush-drag, and double-click reset for one chart + navigator pair. */
    function bindTimelineNavigator(cfg, dragState) {
      const svg = document.getElementById(cfg.svgId);
      const nav = document.getElementById(cfg.navId);
      if (!svg || !nav) return;

      svg.addEventListener("mouseleave", () => hideTooltip());

      svg.addEventListener("mousedown", e => {
        if (e.button !== 0) return;
        const [origStart, origEnd] = cfg.getRange();
        dragState.active = { kind: "main", cfg, svg, startX: e.clientX, origStart, origEnd };
        svg.style.cursor = "grabbing";
        hideTooltip();
      });
      svg.addEventListener("dblclick", () => cfg.onDblClick());

      nav.addEventListener("mousedown", e => {
        const action = e.target.getAttribute(cfg.navActionAttr);
        const rect = nav.getBoundingClientRect();
        if (action === "jump") {
          cfg.onClearPresets();
          const plotLeftPx = (NAV_GEOM.padL / NAV_GEOM.W) * rect.width;
          const plotWidthPx = ((NAV_GEOM.W - NAV_GEOM.padL - NAV_GEOM.padR) / NAV_GEOM.W) * rect.width;
          const clickIdx = ((e.clientX - rect.left - plotLeftPx) / Math.max(1, plotWidthPx)) * (cfg.getLen() - 1);
          const [curStart, curEnd] = cfg.getRange();
          const half = (curEnd - curStart) / 2;
          const [ns, ne] = cfg.clamp(clickIdx - half, clickIdx + half);
          cfg.setRange(ns, ne);
          cfg.render();
          return;
        }
        if (action === "move" || action === "left" || action === "right") {
          const [origStart, origEnd] = cfg.getRange();
          dragState.active = { kind: "nav", action, cfg, startX: e.clientX, origStart, origEnd, rect };
        }
      });
    }

    // Attach Pan & Zoom Listeners for all charts (mouse wheel left unbound so wheel always scrolls webpage)
    function bindChartInteractions() {
      const dragState = { active: null };
      const pxToIndexDelta = (dx, rect, len) => {
        const plotWidthPx = ((NAV_GEOM.W - NAV_GEOM.padL - NAV_GEOM.padR) / NAV_GEOM.W) * rect.width;
        return (dx / Math.max(1, plotWidthPx)) * Math.max(1, len - 1);
      };

      const configs = [
        {
          svgId: "weekly-svg",
          navId: "nav-svg",
          navActionAttr: "data-nav-action",
          minBars: MIN_VISIBLE_BARS,
          getLen: () => TIMELINE.length,
          getRange: () => [viewStart, viewEnd],
          setRange: (s, e) => { viewStart = s; viewEnd = e; },
          clamp: clampViewport,
          onClearPresets: () => {
            isExactPresetWindow = false;
            activePreset = null;
            activeYearJump = null;
            document.querySelectorAll("[data-preset]").forEach(b => b.classList.remove("active"));
            document.querySelectorAll("[data-year]").forEach(b => b.classList.remove("active"));
          },
          onDblClick: () => resetZoom(),
          render: () => renderChartAndNav(),
        },
        {
          svgId: "releases-svg",
          navId: "rel-nav-svg",
          navActionAttr: "data-rel-nav-action",
          minBars: MIN_REL_VISIBLE_BARS,
          getLen: () => REL_BARS.length,
          getRange: () => [relViewStart, relViewEnd],
          setRange: (s, e) => { relViewStart = s; relViewEnd = e; },
          clamp: clampRelViewport,
          onClearPresets: () => {
            relActivePreset = null;
            relActiveYearJump = null;
            document.querySelectorAll("[data-rel-preset]").forEach(b => b.classList.remove("active"));
            document.querySelectorAll("[data-rel-year]").forEach(b => b.classList.remove("active"));
          },
          onDblClick: () => applyRelPreset(relActivePreset || "all"),
          render: () => renderRelChartAndNav(),
        },
        {
          svgId: "sim-svg",
          navId: "sim-nav-svg",
          navActionAttr: "data-sim-nav-action",
          minBars: MIN_SIM_VISIBLE_BARS,
          getLen: () => SIM_BARS.length,
          getRange: () => [simViewStart, simViewEnd],
          setRange: (s, e) => { simViewStart = s; simViewEnd = e; },
          clamp: clampSimViewport,
          onClearPresets: () => {
            simActivePreset = null;
            document.querySelectorAll("[data-sim-preset]").forEach(b => b.classList.remove("active"));
          },
          onDblClick: () => applySimPreset(simActivePreset || "40y"),
          render: () => renderSimChartAndNav(),
        },
        {
          svgId: "ssn-svg",
          navId: "ssn-nav-svg",
          navActionAttr: "data-ssn-nav-action",
          minBars: MIN_SIM_VISIBLE_BARS,
          getLen: () => SSN_BARS.length,
          getRange: () => [ssnViewStart, ssnViewEnd],
          setRange: (s, e) => { ssnViewStart = s; ssnViewEnd = e; },
          clamp: clampSsnViewport,
          onClearPresets: () => {
            ssnActivePreset = null;
            document.querySelectorAll("[data-ssn-preset]").forEach(b => b.classList.remove("active"));
          },
          onDblClick: () => applySsnPreset(ssnActivePreset || "40y"),
          render: () => renderSsnChartAndNav(),
        },
      ];

      configs.forEach(cfg => bindTimelineNavigator(cfg, dragState));

      window.addEventListener("mousemove", e => {
        const d = dragState.active;
        if (!d) return;
        const dx = e.clientX - d.startX;
        if (d.kind === "main") {
          if (Math.abs(dx) <= 1.5) return;
          d.cfg.onClearPresets();
          const rect = d.svg.getBoundingClientRect();
          const plotWidthPx = ((CHART_GEOM.W - CHART_GEOM.padL - CHART_GEOM.padR) / CHART_GEOM.W) * rect.width;
          const shift = -(dx / Math.max(1, plotWidthPx)) * (d.origEnd - d.origStart);
          const [ns, ne] = d.cfg.clamp(d.origStart + shift, d.origEnd + shift);
          d.cfg.setRange(ns, ne);
          d.cfg.render();
        } else if (d.kind === "nav") {
          if (Math.abs(dx) <= 1.0) return;
          d.cfg.onClearPresets();
          const len = d.cfg.getLen();
          const dIdx = pxToIndexDelta(dx, d.rect, len);
          if (d.action === "move") {
            const [ns, ne] = d.cfg.clamp(d.origStart + dIdx, d.origEnd + dIdx);
            d.cfg.setRange(ns, ne);
          } else if (d.action === "left") {
            const ns = Math.max(0, Math.min(d.origEnd - (d.cfg.minBars - 1), d.origStart + dIdx));
            d.cfg.setRange(ns, d.origEnd);
          } else if (d.action === "right") {
            const ne = Math.min(len - 1, Math.max(d.origStart + (d.cfg.minBars - 1), d.origEnd + dIdx));
            d.cfg.setRange(d.origStart, ne);
          }
          d.cfg.render();
        }
      });

      window.addEventListener("mouseup", () => {
        const d = dragState.active;
        if (d && d.kind === "main" && d.svg) {
          d.svg.style.cursor = "grab";
        }
        dragState.active = null;
      });
    }

    // Current Holdings Table & Pie Chart Views (Non-colliding Cool Cyan/Emerald/Teal/Sky/Rose palette)
    const PIE_PALETTE = [
      "#38bdf8", "#10b981", "#14b8a6", "#0ea5e9", "#22d3ee",
      "#34d399", "#fb7185", "#2dd4bf", "#60a5fa", "#94a3b8",
      "#f43f5e", "#0284c7"
    ];

    function activeGroupsList() {
      return tableGrouping === "strategy" ? PAYLOAD.holdings_by_strategy : PAYLOAD.holdings_by_account;
    }

    let holdingsSummaryExpanded = false;

    function toggleHoldingsSummaryDrawer() {
      setHoldingsSummaryDrawerExpanded(!holdingsSummaryExpanded);
    }

    function setHoldingsSummaryDrawerExpanded(expanded) {
      holdingsSummaryExpanded = Boolean(expanded);
      const body = document.getElementById("holdings-summary-body");
      const caret = document.getElementById("holdings-summary-caret");
      const btn = document.getElementById("holdings-summary-toggle-btn");
      if (body) body.style.display = holdingsSummaryExpanded ? "block" : "none";
      if (caret) {
        caret.style.transform = holdingsSummaryExpanded ? "rotate(90deg)" : "rotate(0deg)";
        caret.style.color = holdingsSummaryExpanded ? "#38bdf8" : "#94a3b8";
      }
      if (btn) {
        btn.setAttribute("aria-expanded", holdingsSummaryExpanded ? "true" : "false");
        btn.title = holdingsSummaryExpanded ? "Click to collapse Current Vested Holdings Summary" : "Click to expand Current Vested Holdings Summary";
      }
      if (holdingsSummaryExpanded && holdingsViewMode === "pie") {
        renderHoldingsPie();
      }
    }
    window.toggleHoldingsSummaryDrawer = toggleHoldingsSummaryDrawer;
    window.setHoldingsSummaryDrawerExpanded = setHoldingsSummaryDrawerExpanded;

    function setHoldingsViewMode(mode) {
      holdingsViewMode = mode === "pie" ? "pie" : "table";
      document.getElementById("holdings-view-table").classList.toggle("active", holdingsViewMode === "table");
      document.getElementById("holdings-view-pie").classList.toggle("active", holdingsViewMode === "pie");
      document.getElementById("holdings-table-wrap").style.display = holdingsViewMode === "table" ? "block" : "none";
      document.getElementById("holdings-pie-wrap").style.display = holdingsViewMode === "pie" ? "grid" : "none";
      const tblCtrls = document.getElementById("holdings-table-ctrls");
      if (tblCtrls) tblCtrls.style.display = holdingsViewMode === "table" ? "inline-flex" : "none";
      const subHint = document.getElementById("holdings-sub-hint");
      if (subHint) {
        subHint.innerHTML = holdingsViewMode === "pie"
          ? "Hover any slice or legend row to inspect position details, principal investment, and annualized growth"
          : `Click rows with <strong style="color:var(--accent);font-size:22px;line-height:0.85;vertical-align:-2px;">&#9656;</strong> to expand constituent securities, bond issues, sub-segments, and per-account balances`;
      }
      hideTooltip();
      if (holdingsViewMode === "pie") renderHoldingsPie();
      else renderHoldingsTable();
    }

    function setTableGrouping(mode) {
      tableGrouping = mode;
      document.getElementById("tbl-mode-strategy").classList.toggle("active", mode === "strategy");
      document.getElementById("tbl-mode-account").classList.toggle("active", mode === "account");
      renderHoldingsTable();
      renderHoldingsPie();
    }

    function toggleGroup(gid) {
      if (expandedGroups.has(gid)) expandedGroups.delete(gid);
      else expandedGroups.add(gid);
      renderHoldingsTable();
    }

    function expandCollapseAll(expand) {
      if (!expand) expandedGroups.clear();
      else activeGroupsList().forEach(g => { if (g.children && g.children.length) expandedGroups.add(g.group_id); });
      renderHoldingsTable();
    }

    function renderSymbolAccountCell(item) {
      const symPart = item.link
        ? `<a href="${escHtml(item.link)}" target="_blank" rel="noopener" onclick="event.stopPropagation()">${escHtml(item.symbol)} &#8599;</a>`
        : escHtml(item.symbol);
      return `<div><strong class="mono">${symPart}</strong></div><div class="row-sub">${escHtml(item.account_label || "")}</div>`;
    }

    function renderHoldingSymbolPill(item) {
      const rawSym = String(item.symbol || "").trim();
      if (!rawSym) return "";
      // Omit contract ID -- it belongs in details
      if (rawSym.startsWith("#ANN") || rawSym.startsWith("ANN-") || rawSym === "RILA Segment") {
        return "";
      }
      if (item.link) {
        return `<a href="${escHtml(item.link)}" target="_blank" rel="noopener" class="holding-sym-pill mono" onclick="event.stopPropagation()" title="Open live quote for ${escHtml(rawSym)}">${escHtml(rawSym)} &#8599;</a>`;
      }
      return `<span class="holding-sym-pill mono">${escHtml(rawSym)}</span>`;
    }

    // DA-D04: Must cover all 7 mandatory RiskClass values (`ingest/models.py`) so Screen 1 tooltips
    // never fall back to raw snake_case identifiers (`crypto_speculative`, `real_diversifier`, etc.).
    const HIST_GROWTH_RISK_NAMES = {
      single_stock: "Single Stock (High Volatility)",
      crypto_speculative: "Crypto / Speculative (~68% Vol)",
      broad_equity: "Broad US / Global Equity",
      real_diversifier: "Real Assets / Gold (~15% Vol)",
      hybrid_buffered: "Hybrid / Buffered (~8.5% Vol)",
      fixed_income: "Fixed Income / Municipal Bonds",
      cash_fixed: "Cash & Fixed Guarantees",
      liquid_cash: "Liquid Cash & Sweeps",
      options_hedged: "Options Hedged / Collar"
    };

    function resolveHistGrowthInfo(item, parent) {
      const g = item || {};
      const cagr = g.cagr_pct;
      const refRate = g.ref_growth_pct;
      const src = g.return_source || (parent ? parent.return_source : "");
      const confRaw = String(g.confidence || (parent ? parent.confidence : "") || "HIGH").toUpperCase();
      const riskClass = g.default_risk_class || (parent ? parent.default_risk_class : "broad_equity");
      const isSweep = (g.growth_str && g.growth_str.includes("Sweep")) || riskClass === "cash_fixed" || (g.title && g.title.toLowerCase().includes("cash"));
      const isBond = src === "STATEMENT_YTM" || (g.growth_str && g.growth_str.includes("Yld"));

      let rateDisplay = "—";
      let rateSign = 0;
      let subtext = "";
      let pillLabel = "Inception CAGR";
      let pillCls = "pill-green";
      let confLabel = confRaw === "HIGH" ? "HIGH" : (confRaw === "MEDIUM" ? "MEDIUM" : "LOW");
      let methodTitle = "";
      let methodDetail = "";
      let dateRangeStr = "";
      let basisGainStr = "";

      if (cagr != null && !isNaN(cagr)) {
        rateDisplay = (cagr >= 0 ? "+" : "") + Number(cagr).toFixed(2) + "% / yr";
        rateSign = cagr >= 0 ? 1 : -1;
        pillLabel = "Inception CAGR";
        methodTitle = "Personal Statement Inception CAGR";
        methodDetail = "Annualized compound return calculated directly from your custodial/carrier statement acquisition date and cost basis to today.";
        if (g.principal_val != null && g.current_value != null && g.principal_val > 0) {
          const gain = g.current_value - g.principal_val;
          const gainPct = (gain / g.principal_val) * 100;
          basisGainStr = `${fmtUSD2(g.principal_val)} → ${fmtUSD2(g.current_value)} (${gain >= 0 ? "+" : ""}${fmtUSD2(gain)}, ${gainPct >= 0 ? "+" : ""}${gainPct.toFixed(1)}%)`;
          subtext = `${gain >= 0 ? "+" : ""}${fmtUSD2(gain)} (${gainPct >= 0 ? "+" : ""}${gainPct.toFixed(1)}%)`;
        }
        if (g.principal_date) {
          dateRangeStr = `${g.principal_date} → Today`;
        }
      } else if (parent && parent.cagr_pct != null && src === "STATEMENT_INCEPTION_CAGR") {
        rateDisplay = (parent.cagr_pct >= 0 ? "+" : "") + Number(parent.cagr_pct).toFixed(2) + "% / yr";
        rateSign = parent.cagr_pct >= 0 ? 1 : -1;
        pillLabel = "Inception CAGR";
        methodTitle = "Parent Contract Inception CAGR";
        methodDetail = `Tracked at contract level (${parent.friendly_name || parent.title || "Annuity"}). Inception CAGR applies across all investment segments.`;
        if (parent.principal_date) dateRangeStr = `${parent.principal_date} → Today`;
        if (parent.principal_val != null && parent.current_value != null && parent.principal_val > 0) {
          const pGain = parent.current_value - parent.principal_val;
          const pPct = (pGain / parent.principal_val) * 100;
          basisGainStr = `Contract: ${fmtUSD2(parent.principal_val)} → ${fmtUSD2(parent.current_value)} (${pGain >= 0 ? "+" : ""}${fmtUSD2(pGain)}, ${pPct >= 0 ? "+" : ""}${pPct.toFixed(1)}%)`;
          subtext = `In contract (${parent.friendly_name || "Annuity"})`;
        }
      } else if (src === "STATEMENT_REPORTED_RETURN" && refRate != null && !isNaN(refRate)) {
        rateDisplay = (refRate >= 0 ? "+" : "") + Number(refRate).toFixed(2) + "% / yr";
        rateSign = refRate >= 0 ? 1 : -1;
        pillLabel = "Statement Return";
        methodTitle = "Statement-Reported Annualized Return";
        methodDetail = g.source_detail || (parent && parent.source_detail) || "Annualized personal rate of return reported directly by your plan custodian across historical contributions and investment gains.";
        if (g.principal_val != null && g.current_value != null && g.principal_val > 0) {
          const gain = g.current_value - g.principal_val;
          const gainPct = (gain / g.principal_val) * 100;
          basisGainStr = `${fmtUSD2(g.principal_val)} → ${fmtUSD2(g.current_value)} (${gain >= 0 ? "+" : ""}${fmtUSD2(gain)}, ${gainPct >= 0 ? "+" : ""}${gainPct.toFixed(1)}%)`;
          subtext = `${gain >= 0 ? "+" : ""}${fmtUSD2(gain)} (${gainPct >= 0 ? "+" : ""}${gainPct.toFixed(1)}%)`;
        } else {
          subtext = g.growth_sub || "";
        }
      } else if (isBond) {
        const yld = refRate != null ? refRate : 0.0;
        rateDisplay = Number(yld).toFixed(2) + "% Yld";
        rateSign = 1;
        pillLabel = "Bond YTM";
        subtext = g.growth_sub || "Tax-exempt yield";
        methodTitle = "Statement Yield to Maturity (YTM)";
        methodDetail = "Weighted average tax-exempt coupon cash-flow yield for municipal bonds held to maturity.";
      } else if (isSweep) {
        rateDisplay = "—";
        rateSign = 0;
        pillLabel = "Liquid Cash";
        pillCls = "pill-slate";
        subtext = g.growth_sub || "Sweep deposits";
        methodTitle = "Liquid Cash & Bank Deposit Sweeps";
        methodDetail = "Holding growth CAGR is not applicable to liquid cash balances. Cash sweeps earn monthly interest (see Sweep APY).";
      } else if (g.growth_str === "Stock Plan Equity" || g.growth_str === "Equity Grants ($0)" || (g.principal_str && g.principal_str.includes("Grants ($0)"))) {
        rateDisplay = "—";
        rateSign = 0;
        pillLabel = "Stock Plan";
        pillCls = "pill-slate";
        subtext = g.growth_sub || "Equity awards ($0 outlay)";
        methodTitle = "Vested Employee Stock Plan Awards";
        methodDetail = "Personal Inception CAGR is not applicable because employee stock plan awards vest at $0 cash outlay (ordinary income taxes are withheld via shares at each vest). Historical market reference returns are available in the Scenario Forecast details.";
      } else if (String(g.group_id || "").startsWith("rollup_uma_") || (g.investment_type && g.investment_type.includes("Direct-Indexing"))) {
        const nChild = Array.isArray(g.children) ? g.children.length : 0;
        rateDisplay = "—";
        rateSign = 0;
        pillLabel = "Direct Index";
        pillCls = "pill-slate";
        subtext = g.growth_sub || "Inception N/A";
        methodTitle = "Personalized Direct Indexing SMA";
        methodDetail = `Custodian monthly snapshot statement does not provide opening tax-lot purchase dates or single initial cash basis${nChild ? ` for the ${nChild} constituent equities` : ""}. Historical market reference returns for Large Cap Equities are available in the Scenario Forecast details.`;
      } else {
        rateDisplay = "—";
        rateSign = 0;
        pillLabel = "Inception N/A";
        pillCls = "pill-slate";
        subtext = g.growth_sub || "Inception N/A";
        methodTitle = "Personal Inception Date / Basis Unavailable";
        methodDetail = "Statement does not report an initial purchase date or cost basis. Market reference rates are available in the Scenario Forecast details.";
      }

      if (confRaw === "HIGH") pillCls = "pill-green";
      else if (confRaw === "MEDIUM") pillCls = "pill-yellow";
      else if (confRaw === "LOW") pillCls = "pill-red";
      else if (pillLabel === "Risk Class") pillCls = "pill-red";

      const confColor = pillCls === "pill-green" ? "#6ee7b7" : (pillCls === "pill-yellow" ? "#fde047" : "#fda4af");
      const acct = g.account_label || (parent ? parent.account_label : "") || g.account_id || "";
      const inst = g.institution || (parent ? parent.institution : "") || "";
      const title = g.friendly_name || g.title || "";
      const riskLabel = HIST_GROWTH_RISK_NAMES[riskClass] || riskClass;

      let tooltipHtml = `<div style="min-width:240px;max-width:340px;line-height:1.4;">`;
      tooltipHtml += `<div style="font-weight:700;font-size:12.5px;color:#38bdf8;margin-bottom:3px;">${escHtml(title)}</div>`;
      if (acct || inst) {
        tooltipHtml += `<div style="font-size:11px;color:#94a3b8;margin-bottom:6px;"><span class="mono" style="color:#cbd5e1;">${escHtml(acct)}</span>${inst ? " &bull; " + escHtml(inst) : ""}</div>`;
      }
      tooltipHtml += `<div style="display:flex;align-items:center;gap:6px;margin-bottom:6px;">
        <span class="pill ${pillCls} mono" style="font-size:9.5px;padding:1px 6px;">${escHtml(pillLabel)}</span>
        <span style="font-size:11px;color:#94a3b8;">Confidence: <strong style="color:${confColor};">${escHtml(confLabel)}</strong></span>
      </div>`;
      tooltipHtml += `<div style="font-size:11.5px;color:#f1f5f9;margin-bottom:4px;"><strong>${escHtml(methodTitle)}</strong></div>`;
      tooltipHtml += `<div style="font-size:11px;color:#cbd5e1;margin-bottom:4px;">${escHtml(methodDetail)}</div>`;
      if (dateRangeStr || basisGainStr) {
        tooltipHtml += `<div style="margin-top:6px;padding-top:4px;border-top:1px solid rgba(255,255,255,0.08);font-size:10.5px;color:#94a3b8;">`;
        if (dateRangeStr) tooltipHtml += `<div>Date Range: <span class="mono" style="color:#cbd5e1;">${escHtml(dateRangeStr)}</span></div>`;
        if (basisGainStr) tooltipHtml += `<div>Performance: <span class="mono" style="color:#cbd5e1;">${escHtml(basisGainStr)}</span></div>`;
        tooltipHtml += `</div>`;
      }
      if (riskLabel) {
        tooltipHtml += `<div style="margin-top:5px;padding-top:4px;border-top:1px solid rgba(255,255,255,0.08);font-size:10.5px;color:#94a3b8;"><span class="muted">Risk Class:</span> <strong style="color:#f8fafc;">${escHtml(riskLabel)}</strong></div>`;
      }
      tooltipHtml += `</div>`;

      return {
        rateDisplay,
        rateSign,
        subtext,
        pillLabel,
        pillCls,
        tooltipHtml
      };
    }

    let recentChangeMode = "adaptive"; // "adaptive" | "1w"

    function setRecentChangeMode(mode) {
      recentChangeMode = mode === "1w" ? "1w" : "adaptive";
      const adaptBtn = document.getElementById("chg-mode-adaptive-btn");
      const w1Btn = document.getElementById("chg-mode-1w-btn");
      if (adaptBtn) adaptBtn.classList.toggle("active", recentChangeMode === "adaptive");
      if (w1Btn) w1Btn.classList.toggle("active", recentChangeMode === "1w");
      renderHoldingsTable();
    }
    window.setRecentChangeMode = setRecentChangeMode;

    function renderRecentChangeCellHtml(item) {
      if (recentChangeMode === "1w") {
        const w = (item && item.wow_change) ? item.wow_change : 0;
        const cls = w > 0 ? "pos" : (w < 0 ? "neg" : "muted");
        const str = w === 0 ? "—" : fmtSignedUSD2(w);
        const priorStr = (window.PAYLOAD && window.PAYLOAD.prior_bar_iso) ? window.PAYLOAD.prior_bar_iso : "prior week Friday";
        const todayStr = (window.PAYLOAD && window.PAYLOAD.today_iso) ? window.PAYLOAD.today_iso : "evaluation date";
        const tip = w === 0
          ? `1-Week Market Price Change: $0.00 (—).\n• Measured against: Prior week Friday market close on ${priorStr}.\n• Valuation date: ${todayStr} (non-market-ticked asset with no weekly trade delta).`
          : `1-Week Market Price Change: ${fmtSignedUSD2(w)}.\n• Measured against: Prior week Friday market close on ${priorStr}.\n• Valuation date: ${todayStr} (market quote as of latest exchange close).`;
        return `<td class="num mono ${cls}"><span class="has-tip" data-tip="${escHtml(tip)}">${str}</span></td>`;
      }

      // Adaptive mode:
      const rcVal = item.recent_change_val;
      const rcSub = item.recent_change_sub || "";
      const rcTip = item.recent_change_tip || "";
      const rcSign = item.recent_change_sign !== undefined ? item.recent_change_sign : (rcVal > 0 ? 1 : (rcVal < 0 ? -1 : 0));
      const grCls = rcSign > 0 ? "pos" : (rcSign < 0 ? "neg" : "muted");

      if (rcVal === null || rcVal === undefined) {
        return `<td class="num mono muted">
          <span>—</span>
          ${rcSub ? `<div class="row-sub">${escHtml(rcSub)}</div>` : ""}
        </td>`;
      }

      if (rcVal === 0 && !rcSub) {
        return `<td class="num mono muted">—</td>`;
      }

      if (rcVal === 0) {
        return `<td class="num">
          <div class="mono muted has-tip" style="display:inline-block;" data-tip="${escHtml(rcTip)}">—</div>
          <div class="row-sub">${escHtml(rcSub)}</div>
        </td>`;
      }

      const str = fmtSignedUSD2(rcVal);
      return `<td class="num">
        <div class="mono ${grCls} has-tip" style="display:inline-block;font-weight:600;" data-tip="${escHtml(rcTip)}">${str}</div>
        <div class="row-sub">${escHtml(rcSub)}</div>
      </td>`;
    }

    function renderHistGrowthCellHtml(item, parent) {
      const info = resolveHistGrowthInfo(item, parent);
      const grCls = info.rateSign > 0 ? "pos" : (info.rateSign < 0 ? "neg" : "muted");
      const tipAttr = escHtml(info.tooltipHtml);
      return `<td class="num">
        <div class="mono ${grCls} has-tip" style="display:inline-block;font-weight:600;" data-tip="${tipAttr}">${escHtml(info.rateDisplay)}</div>
        ${info.subtext ? `<div class="row-sub" style="margin-top:1px;">${escHtml(info.subtext)}</div>` : ""}
        <div style="margin-top:2px;">
          <span class="pill ${info.pillCls} has-tip" style="font-size:9.5px;padding:1px 6px;cursor:help;" data-tip="${tipAttr}">${escHtml(info.pillLabel)}</span>
        </div>
      </td>`;
    }

    function renderHoldingsTable() {
      const q = (document.getElementById("holding-search").value || "").trim().toLowerCase();
      const rawGroups = activeGroupsList() || [];
      const groups = rawGroups.slice().sort((a, b) => (b.pct_portfolio - a.pct_portfolio) || (b.current_value - a.current_value));
      const tbody = document.getElementById("holdings-tbody");
      let html = [];
      let shownSum = 0;
      let shownWow = 0;

      const matchesQuery = obj => {
        if (!q) return true;
        return (
          (obj.title || "").toLowerCase().includes(q) ||
          (obj.symbol || "").toLowerCase().includes(q) ||
          (obj.account_label || "").toLowerCase().includes(q) ||
          (obj.maturity_str || "").toLowerCase().includes(q)
        );
      };

      groups.forEach(g => {
        const children = g.children || [];
        const parentMatch = matchesQuery(g);
        const matchingChildren = q ? children.filter(matchesQuery) : children;

        if (q && !parentMatch && matchingChildren.length === 0) return;

        shownSum += g.current_value;
        shownWow += g.wow_change;

        const hasKids = children.length > 0;
        const isOpen = hasKids && (expandedGroups.has(g.group_id) || (q.length > 0 && matchingChildren.length > 0));
        const caret = hasKids ? `<span class="caret">${isOpen ? "&#9662;" : "&#9656;"}</span>` : "";
        const srcPillCls = g.is_scrapeable ? "pill-green" : "pill-slate";
        const wowCls = g.wow_change > 0 ? "pos" : (g.wow_change < 0 ? "neg" : "muted");
        const grCls = g.growth_sign > 0 ? "pos" : (g.growth_sign < 0 ? "neg" : "muted");
        const gKey = g.holding_key || g.group_id;

        const feeBadge = (g.fee_pct !== null && g.fee_pct !== undefined && g.fee_pct !== "")
          ? `<span class="pill pill-red mono" style="font-size:10px;padding:1px 6px;vertical-align:1px;" title="Annual Fee / Expense: ${g.fee_pct}%">${g.fee_pct}% Fee</span>`
          : "";
        const symPill = renderHoldingSymbolPill(g);

        html.push(`<tr class="parent-row ${hasKids ? 'clickable' : ''}" ${hasKids ? `data-sa-action="toggle-group" data-sa-id="${escHtml(g.group_id)}"` : ""}>
          <td style="width:34px;text-align:center;padding:6px 4px;" data-sa-action="open-friendly" data-sa-key="${escHtml(gKey)}" data-sa-stop="1">
            <button type="button" class="btn-icon-edit ${g.friendly_name ? 'has-friendly' : ''}" title="Edit Holding Details &amp; Scenario Forecast: ${escHtml(g.statement_name || g.title)}" data-sa-action="open-friendly" data-sa-key="${escHtml(gKey)}" data-sa-stop="1">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/>
                <path d="m15 5 4 4"/>
              </svg>
            </button>
          </td>
          <td style="text-align:center;" class="mono"><span class="has-tip" data-tip="${escHtml(g.pct_tooltip || '')}">${g.pct_portfolio.toFixed(2)}%</span></td>
          <td>
            <div class="holding-cell-row">
              <span class="holding-title-group">${caret}<strong>${escHtml(g.title)}</strong></span>
              ${(symPill || feeBadge) ? `<span class="holding-badge-group">${symPill}${feeBadge}</span>` : ""}
            </div>
          </td>
          <td class="num">
            <div class="mono"><strong>${fmtUSD2(g.current_value)}</strong></div>
            <div style="margin-top:2px;"><span class="pill ${srcPillCls} has-tip" style="font-size:9.5px;padding:1px 5px;" data-tip="${escHtml(g.source_tooltip || '')}">${escHtml(g.source_label)}</span></div>
          </td>
          ${renderRecentChangeCellHtml(g)}
          ${renderHistGrowthCellHtml(g, null)}
          <td class="num mdl-in-td">${renderForecastCapsuleHtml(g, gKey)}</td>
        </tr>`);

        if (isOpen) {
          const kidsToRender = (q && !parentMatch) ? matchingChildren : children;
          kidsToRender.forEach(c => {
            const cKey = c.is_sub_segment ? gKey : (c.holding_key || (c.account_id + ":" + c.security_id));
            const cWowCls = c.wow_change > 0 ? "pos" : (c.wow_change < 0 ? "neg" : "muted");
            const cGrCls = c.growth_sign > 0 ? "pos" : (c.growth_sign < 0 ? "neg" : "muted");
            const cFeeBadge = (c.fee_pct !== null && c.fee_pct !== undefined && c.fee_pct !== "")
              ? `<span class="pill pill-red mono" style="font-size:9.5px;padding:1px 5px;vertical-align:1px;" title="Annual Fee / Expense: ${c.fee_pct}%">${c.fee_pct}% Fee</span>`
              : "";
            const cSymPill = renderHoldingSymbolPill(c);
            html.push(`<tr class="child-row">
              <td style="width:34px;text-align:center;padding:6px 4px;" data-sa-action="open-friendly" data-sa-key="${escHtml(cKey)}" data-sa-stop="1">
                <button type="button" class="btn-icon-edit ${c.friendly_name ? 'has-friendly' : ''}" title="Edit Holding Details &amp; Scenario Forecast: ${escHtml(c.statement_name || c.title)}" data-sa-action="open-friendly" data-sa-key="${escHtml(cKey)}" data-sa-stop="1">
                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/>
                    <path d="m15 5 4 4"/>
                  </svg>
                </button>
              </td>
              <td style="text-align:center;" class="mono muted"><span class="has-tip" data-tip="${escHtml(c.pct_tooltip || '')}">${c.pct_portfolio.toFixed(2)}%</span></td>
              <td style="padding-left:26px;">
                <div class="holding-cell-row">
                  <span class="holding-title-group"><span class="child-arrow" style="color:var(--muted);font-weight:600;margin-right:5px;display:inline-block;vertical-align:middle;">&#8627;</span><span>${escHtml(c.title)}</span></span>
                  ${(cSymPill || cFeeBadge) ? `<span class="holding-badge-group">${cSymPill}${cFeeBadge}</span>` : ""}
                </div>
              </td>
              <td class="num">
                <div class="mono">${fmtUSD2(c.current_value)}</div>
                <div class="row-sub" style="margin-top:1px;"><span class="muted has-tip" style="font-size:10px;" data-tip="${escHtml(c.source_tooltip || '')}">${escHtml(c.source_label)}</span></div>
              </td>
              ${renderRecentChangeCellHtml(c)}
              ${renderHistGrowthCellHtml(c, g)}
              <td class="num mdl-in-td">${renderForecastCapsuleHtml(c, cKey)}</td>
            </tr>`);
          });
        }
      });

      tbody.innerHTML = html.join("");
      const tfoot = document.getElementById("holdings-tfoot");
      const totWowCls = shownWow >= 0 ? "pos" : "neg";
      const totPct = PAYLOAD.kpis.total_vested > 0 ? ((shownSum / PAYLOAD.kpis.total_vested) * 100).toFixed(1) : "100.0";
      const weightedGrowth = computeWeightedPortfolioForecastPct();
      const wgSign = weightedGrowth > 0.005 ? "+" : "";
      const wgFill = Math.max(0, Math.min(100, (Math.abs(weightedGrowth) / 20.0) * 100));
      tfoot.innerHTML = `<tr>
        <td></td>
        <td style="text-align:center;" class="mono"><span class="has-tip" data-tip="Sum of shown holdings (${fmtUSD2(shownSum)}) as a percentage of Total Vested Portfolio (${fmtUSD2(PAYLOAD.kpis.total_vested)}).">${totPct}%</span></td>
        <td><strong>Total Shown Vested Portfolio Value</strong></td>
        <td class="num mono"><strong>${fmtUSD2(shownSum)}</strong></td>
        <td class="num mono ${totWowCls}">
          <div class="has-tip" style="display:inline-block;" data-tip="Total 1-Week Friday-to-Friday portfolio market delta across all shown holdings.&#10;• Measured against: Prior week Friday market close on ${(window.PAYLOAD && window.PAYLOAD.prior_bar_iso) || 'prior week'}.&#10;• Valuation date: ${(window.PAYLOAD && window.PAYLOAD.today_iso) || 'today'}.&#10;• Reported on: Live market exchange price feeds.">${fmtSignedUSD2(shownWow)}</div>
          <div class="row-sub">Total 1W Change</div>
        </td>
        <td class="num mono muted">—</td>
        <td class="num mdl-in-td">
          <span class="mdl-in-capsule has-tip" data-tip="${escHtml(`<div>Value-weighted annual projected growth across all vested holdings in active Scenario (<strong>${escHtml(currentSaveName || 'Default')}</strong>).</div>`)}">
            <span class="mdl-in-capsule-fill" style="width:${wgFill.toFixed(1)}%;"></span>
            <span class="mdl-in-capsule-txt">${wgSign}${weightedGrowth.toFixed(1)}%</span>
          </span>
        </td>
      </tr>`;
      if (typeof updatePortfolioForecastPresetUI === "function") {
        updatePortfolioForecastPresetUI();
      }
    }

    // =========================================================================
    // HOLDING FRIENDLY NAMES, GLOBAL PREFERENCES & 2-COLUMN FORECAST MODAL
    // =========================================================================
    let globalPreferences = PAYLOAD.global_preferences || { friendly_names: {} };
    try {
      localStorage.removeItem("holdings_global_preferences");
    } catch (_e) {}

    function findHoldingByKey(key) {
      if (!key) return null;
      const allHoldings = [
        ...(PAYLOAD.holdings_by_strategy || []),
        ...(PAYLOAD.holdings_by_account || []),
      ];
      for (const g of allHoldings) {
        if (g.holding_key === key || g.group_id === key || g.security_id === key || g.forecast_key === key) return g;
        if (g.children) {
          for (const c of g.children) {
            const childKey = c.holding_key || (c.account_id && c.security_id ? (c.account_id + ":" + c.security_id) : null);
            if (c.holding_key === key || childKey === key || c.security_id === key || c.symbol === key || c.forecast_key === key) return c;
          }
        }
      }
      if (Array.isArray(SIM_ASSETS)) {
        const simMatch = SIM_ASSETS.find(a => a.id === key || a.forecast_key === key);
        if (simMatch) {
          const rawTitle = simMatch.title || simMatch.label || simMatch.short_title || simMatch.short_label || simMatch.id;
          const origStmtName = simMatch._orig_title || simMatch.statement_name || rawTitle;
          const subParts = String(simMatch.subtitle || "").split("•").map(s => s.trim()).filter(Boolean);
          return {
            ...simMatch,
            holding_key: simMatch.id,
            title: rawTitle,
            friendly_name: simMatch.friendly_name || "",
            statement_name: origStmtName,
            subtitle: simMatch.subtitle || "",
            institution: simMatch.institution || subParts[0] || (simMatch.is_hypothetical ? "Hypothetical" : "Portfolio Bucket"),
            account_label: simMatch.account_label || subParts[1] || simMatch.account_id || simMatch.id,
            account_id: simMatch.account_id || null,
            investment_type: simMatch.tax_wrapper || simMatch.tax_treatment || "Retirement Bucket",
            maturity_date: simMatch.maturity_iso || simMatch.maturity_date || null,
            maturity_str: simMatch.unlock_ym ? `Locked until ${simMatch.unlock_ym}` : (simMatch.maturity_iso || ""),
            current_value: simMatch.current_value ?? simMatch.initial_value ?? 0,
            principal_str: simMatch.cost_basis != null ? fmtUSD2(simMatch.cost_basis) : undefined,
            principal_sub: simMatch.basis_sub || "",
            tax_rule_title: simMatch.tax_rule_title || "",
            tax_rule_detail: simMatch.tax_rule_detail || "",
            tax_wrapper: simMatch.tax_wrapper || "TAXABLE",
            tax_model: simMatch.tax_model || "",
            tax_rate: simMatch.tax_rate ?? null,
            forecast_key: simMatch.forecast_key || simMatch.id,
            parent_forecast_keys: simMatch.parent_forecast_keys || [],
            default_growth_pct: simMatch.default_growth_pct ?? simMatch.annual_rate_pct ?? 0,
            ref_growth_pct: simMatch.ref_growth_pct ?? simMatch.default_growth_pct ?? simMatch.annual_rate_pct ?? 0,
            default_risk_class: simMatch.default_risk_class || simMatch.risk_class || "broad_equity",
            growth_evidence: simMatch.growth_evidence || [],
            rate_model: simMatch.rate_model,
            return_source: simMatch.return_source,
            forecast_confidence: simMatch.forecast_confidence || "HIGH",
            forecast_source: simMatch.forecast_source || "",
            forecast_source_detail: simMatch.forecast_source_detail || "",
            realized_vol_pct: simMatch.realized_vol_pct ?? null,
            beta_spy: simMatch.beta_spy ?? null,
            corr_spy: simMatch.corr_spy ?? null,
            constituent_forecasts: simMatch.constituent_forecasts || [],
            fee_pct: simMatch.fee_pct ?? simMatch.expense_ratio_pct,
          };
        }
      }
      return null;
    }

    window._activeFnHoldingKey = null;
    window._activeFnHolding = null;
    window._fnDraftGrowthPct = 0;
    window._fnDraftRiskClass = "broad_equity";
    window._fnDraftVolPct = 14.5;
    window._fnHoldingRefVolPct = 14.5;
    window._fnHoldingRealizedVolPct = null;

    function editableHoldingGrowth(value) {
      return Math.max(-20, Math.min(25, Math.round((Number(value) || 0) * 100) / 100));
    }

    function renderHoldingReturnChart(item, start, halfLife, eligible) {
      const figure = document.getElementById("fn-curve-chart");
      if (!figure) return;
      figure.hidden = !eligible;
      if (!eligible) { figure.replaceChildren(); return; }

      const curve = holdingReturnCurve(item, start, halfLife);
      const yearly = Array.from({length: 40}, (_, year) => curve(year));
      const references = [];
      for (const row of holdingGrowthEvidence(item)) {
        if (!references.some(ref => ref.tier === row.tier)) references.push(row);
      }
      const values = yearly.concat(references.map(row => row.projected_return_pct));
      const low = Math.floor(Math.min(...values)) - 1;
      const high = Math.ceil(Math.max(...values)) + 1;
      const x = i => 54 + i * 570 / 39;
      const y = value => 12 + (high - value) * 130 / (high - low);
      const line = yearly.map((rate, i) => `${x(i).toFixed(2)},${y(rate).toFixed(2)}`).join(" ");
      const guides = [low, (high + low) / 2, high].map(rate =>
        `<text x="46" y="${y(rate) + 4}" text-anchor="end">${rate}%</text>` +
        `<line class="fn-plot-grid" x1="54" x2="624" y1="${y(rate)}" y2="${y(rate)}"/>`).join("");
      const ticks = [1, 10, 20, 30, 40].map(year =>
        `<text x="${x(year - 1)}" y="160" text-anchor="middle">${year}</text>`).join("");
      const tierLines = references.map(row =>
        `<line class="fn-tier-line fn-tier-${row.tier}" x1="54" x2="624" y1="${y(row.projected_return_pct)}" y2="${y(row.projected_return_pct)}"/>`).join("");
      const legend = references.map(row =>
        `<span class="fn-tier-${row.tier}"><i></i>Tier ${row.tier} · ${row.projected_return_pct.toFixed(2)}%</span>`).join("");
      figure.innerHTML = `<figcaption>Projected annual return</figcaption>
        <div class="fn-curve-legend"><span class="fn-projection"><i></i>Scenario</span>${legend}</div>
        <svg viewBox="0 0 646 186" role="img" aria-label="Projected annual return from ${yearly[0].toFixed(2)}% in year 1 to ${yearly[39].toFixed(2)}% in year 40">
          ${guides}${tierLines}
          <polyline class="fn-projection" fill="none" points="${line}"/>
          ${ticks}<text x="339" y="180" text-anchor="middle">Projection year</text>
        </svg>`;
    }

    function syncFnDialogForecastUI(editingGrowthNumber = false) {
      const g = editableHoldingGrowth(window._fnDraftGrowthPct);
      const h = window._activeFnHolding || {};
      const ingestedRc = RISK_CLASSES_META[h.default_risk_class] ? h.default_risk_class : "broad_equity";
      const meta = RISK_CLASSES_META[ingestedRc] || RISK_CLASSES_META.broad_equity;
      window._fnDraftRiskClass = ingestedRc;

      const refVol = meta.vol;
      const realizedVol = (h.realized_vol_pct != null && Number(h.realized_vol_pct) > 0 && ingestedRc !== "cash_fixed") ? Number(h.realized_vol_pct) : null;
      const baselineVol = realizedVol != null ? realizedVol : refVol;

      const activeVol = window._fnDraftVolPct != null
        ? Math.max(0, Math.min(150, Math.round(Number(window._fnDraftVolPct) * 10) / 10))
        : baselineVol;
      window._fnDraftVolPct = activeVol;

      // CAGR elements
      const sl = document.getElementById("fn-dlg-growth-slider");
      const num = document.getElementById("fn-dlg-growth-num");
      const pill = document.getElementById("fn-dlg-growth-pill");
      if (sl) sl.value = String(Math.max(-20, Math.min(25, g)));
      if (num && !editingGrowthNumber) num.value = g.toFixed(2);
      const halfLife = window._fnDraftHalfLife || 5;
      const path = describeHoldingReturnPath(h, g, halfLife);
      const halfControl = document.getElementById("fn-half-life-control");
      if (halfControl) halfControl.hidden = !path.eligible;
      const halfSlider = document.getElementById("fn-half-life");
      if (halfSlider) halfSlider.value = String(halfLife);
      const halfLabel = document.getElementById("fn-half-life-years");
      if (halfLabel) halfLabel.textContent = formatHalfLifeLabel(halfLife);
      const pathEl = document.getElementById("fn-return-path");
      if (pathEl) pathEl.textContent = path.text;
      renderHoldingReturnChart(h, g, halfLife, path.eligible);
      const draftTip = projectedGrowthTooltip(h, {growth_pct: g, halfLife}, "Editable scenario growth");
      for (const el of [sl, num, document.getElementById("fn-dlg-growth-label")]) {
        if (el) el.setAttribute("data-tip", draftTip);
      }
      if (pill) pill.textContent = `${g >= 0 ? "+" : ""}${g.toFixed(2)}% / yr`;

      // Classification Badge (immutable archetype)
      const badgeEl = document.getElementById("fn-dlg-risk-badge");
      if (badgeEl) {
        badgeEl.textContent = meta.label || ingestedRc;
      }

      // Volatility controls
      const volSl = document.getElementById("fn-dlg-vol-slider");
      const volNum = document.getElementById("fn-dlg-vol-num");
      const volStatusEl = document.getElementById("fn-dlg-vol-status");
      const refVolBtn = document.getElementById("fn-dlg-ref-vol-btn");
      const realVolBtn = document.getElementById("fn-dlg-realized-vol-btn");

      if (volSl) volSl.value = String(Math.min(80, activeVol));
      if (volNum) volNum.value = activeVol.toFixed(1);

      if (refVolBtn) {
        refVolBtn.textContent = `Hist. Ref Vol (${refVol.toFixed(1)}%)`;
        refVolBtn.setAttribute("data-tip",
          `<div style="min-width:240px;max-width:320px;line-height:1.4;">` +
          `<div style="font-weight:700;font-size:12px;color:#c4b5fd;margin-bottom:3px;">Category Reference Volatility (${refVol.toFixed(1)}%)</div>` +
          `<div style="font-size:11px;color:#e2e8f0;margin-bottom:4px;">Typical historical year-to-year return fluctuation for the <strong>${escHtml(meta.label || ingestedRc)}</strong> category.</div>` +
          `<div style="font-size:10.5px;color:#94a3b8;">In Monte Carlo simulations, this controls how widely annual returns swing between good and bad years when holding-specific price history isn't used.</div>` +
          `</div>`
        );
      }
      if (realVolBtn) {
        if (realizedVol != null) {
          realVolBtn.style.display = "inline-flex";
          realVolBtn.textContent = `10Y Realized Vol (${realizedVol.toFixed(1)}%)`;
          const betaNote = (h.beta_spy != null) ? `, moving roughly ${Number(h.beta_spy).toFixed(2)}× as much as the S&P 500` : "";
          realVolBtn.setAttribute("data-tip",
            `<div style="min-width:240px;max-width:330px;line-height:1.4;">` +
            `<div style="font-weight:700;font-size:12px;color:#6ee7b7;margin-bottom:3px;">10-Year Observed Volatility (${realizedVol.toFixed(1)}%)</div>` +
            `<div style="font-size:11px;color:#e2e8f0;margin-bottom:4px;">Measured from 10 years of actual weekly ups and downs on Yahoo Finance (annualized${escHtml(betaNote)}).</div>` +
            `<div style="font-size:10.5px;color:#94a3b8;">Click to use this holding's actual historical market swings rather than the broader category average.</div>` +
            `</div>`
          );
        } else {
          realVolBtn.style.display = "none";
        }
      }

      const isVolOverridden = Math.abs(activeVol - baselineVol) > 0.05;
      if (volStatusEl) {
        if (isVolOverridden) {
          volStatusEl.style.color = "#c4b5fd";
          volStatusEl.textContent = `◆ Scenario volatility override: ${activeVol.toFixed(1)}% (ingested baseline: ${baselineVol.toFixed(1)}%)`;
        } else if (realizedVol != null) {
          volStatusEl.style.color = "#6ee7b7";
          volStatusEl.textContent = `✓ 10Y empirical realized volatility active (${realizedVol.toFixed(1)}%)`;
        } else {
          volStatusEl.style.color = "#94a3b8";
          volStatusEl.textContent = `✓ Standard asset-class benchmark volatility active (${refVol.toFixed(1)}%)`;
        }
      }

      const descEl = document.getElementById("fn-dlg-risk-desc");
      if (descEl) {
        descEl.textContent = meta.summary || "";
        descEl.title = meta.summary || "";
      }
      if (typeof updateFnResetAllTooltip === "function") {
        updateFnResetAllTooltip();
      }
    }

    function setFnDialogGrowth(val, editingGrowthNumber = false) {
      window._fnDraftGrowthPct = editableHoldingGrowth(val);
      const noteEl = document.getElementById("fn-dlg-inherit-note");
      if (noteEl) {
        noteEl.textContent = "Explicit per-holding override for the active Scenario (saved on Save).";
      }
      syncFnDialogForecastUI(editingGrowthNumber);
    }

    function applyFnDialogRefGrowth() {
      const refVal = Number(window._fnHoldingRefGrowthPct ?? 0) || 0;
      setFnDialogGrowth(refVal);
    }

    function setFnDialogVol(val) {
      const num = parseFloat(val);
      window._fnDraftVolPct = Number.isFinite(num) ? Math.max(0, Math.min(150, Math.round(num * 10) / 10)) : 0;
      syncFnDialogForecastUI();
    }

    function applyFnDialogRefVol() {
      const refVal = Number(window._fnHoldingRefVolPct ?? 0) || 0;
      setFnDialogVol(refVal);
    }

    function applyFnDialogRealizedVol() {
      const realVal = Number(window._fnHoldingRealizedVolPct ?? 0) || 0;
      setFnDialogVol(realVal);
    }

    function applyFnDialogIngestedBaseline() {
      const h = window._activeFnHolding || {};
      const baseGp = window._fnHoldingRefGrowthPct;
      const baseRc = RISK_CLASSES_META[h.default_risk_class] ? h.default_risk_class : "broad_equity";
      const meta = RISK_CLASSES_META[baseRc] || RISK_CLASSES_META.broad_equity;
      const realizedVol = (h.realized_vol_pct != null && Number(h.realized_vol_pct) > 0 && baseRc !== "cash_fixed") ? Number(h.realized_vol_pct) : null;
      const baseVol = realizedVol != null ? realizedVol : meta.vol;

      window._fnDraftGrowthPct = editableHoldingGrowth(baseGp);
      window._fnDraftRiskClass = baseRc;
      window._fnDraftVolPct = baseVol;

      const noteEl = document.getElementById("fn-dlg-inherit-note");
      if (noteEl) {
        noteEl.textContent = "Reset to ingested baseline return & volatility (saved on Save).";
      }
      syncFnDialogForecastUI();
    }

    function resolveHoldingTaxRule(h) {
      if (!h) return null;
      if (h.tax_rule_title || h.tax_rule_detail) {
        return {
          title: h.tax_rule_title || "Tax-Aware Drawdown",
          detail: h.tax_rule_detail || "",
          wrapper: h.tax_wrapper || "TAXABLE",
          model: h.tax_model || "",
          rate: h.tax_rate ?? null,
        };
      }

      const assetList = (typeof SIM_ASSETS !== "undefined" && Array.isArray(SIM_ASSETS) && SIM_ASSETS.length > 0)
        ? SIM_ASSETS
        : (typeof PAYLOAD !== "undefined" && Array.isArray(PAYLOAD.retirement_sim_assets) ? PAYLOAD.retirement_sim_assets : []);

      const hKey = String(h.holding_key || "");
      const fKey = String(h.forecast_key || "");
      const gId = String(h.group_id || "");
      const aId = String(h.account_id || "");
      const sId = String(h.security_id || "");
      const title = String(h.title || h.statement_name || "");
      const isAnnuityH = String(h.investment_type || "").toUpperCase().includes("ANNUITY") ||
                         String(h.security_id || "").startsWith("CONTRACT:") ||
                         String(h.product_type || "").toUpperCase().includes("ANNUITY");

      let matched = null;

      // Pass 1: Direct key matches (id, forecast_key, rollup_group_id)
      for (const a of assetList) {
        if (!a) continue;
        if ((hKey && a.id === hKey) ||
            (fKey && (a.id === fKey || a.forecast_key === fKey)) ||
            (gId && (a._rollup_gid === gId || a.rollup_group_id === gId)) ||
            (hKey && (a._rollup_gid === hKey || a.rollup_group_id === hKey))) {
          matched = a; break;
        }
      }

      // Pass 2: Security ID match (stripping CONTRACT: or TICKER: prefixes)
      if (!matched && sId) {
        const cleanSId = sId.replace(/^(CONTRACT|TICKER):/i, "");
        if (cleanSId) {
          for (const a of assetList) {
            if (!a) continue;
            if (a.id && a.id.includes(cleanSId)) {
              matched = a; break;
            }
          }
        }
      }

      // Pass 3: Title match with category compatibility
      if (!matched && title) {
        const cleanTitle = title.toLowerCase().trim();
        for (const a of assetList) {
          if (!a) continue;
          const isAnnuityA = (a.category === "annuity") || (a.tax_model === "ANNUITY_LIFO") || String(a.id || "").startsWith("ann_");
          if (isAnnuityH !== isAnnuityA) continue;
          const aTitle = String(a.title || a.short_title || "").toLowerCase().trim();
          if (aTitle && (cleanTitle.includes(aTitle) || aTitle.includes(cleanTitle))) {
            matched = a; break;
          }
        }
      }

      // Pass 4: Account ID fallback, strictly enforcing category compatibility
      if (!matched && aId) {
        for (const a of assetList) {
          if (!a) continue;
          const isAnnuityA = (a.category === "annuity") || (a.tax_model === "ANNUITY_LIFO") || String(a.id || "").startsWith("ann_");
          if (isAnnuityH !== isAnnuityA) continue;
          if ((a.id && a.id.includes(aId)) || (a.subtitle && a.subtitle.includes(aId)) || (a.account_id && a.account_id === aId)) {
            matched = a; break;
          }
        }
      }

      if (matched && (matched.tax_rule_title || matched.tax_rule_detail)) {
        return {
          title: matched.tax_rule_title || "Tax-Aware Drawdown",
          detail: matched.tax_rule_detail || "",
          wrapper: matched.tax_wrapper || "TAXABLE",
          model: matched.tax_model || "",
          rate: matched.tax_rate ?? null,
        };
      }

      // PORTABILITY DO: Format tax badges and municipal bond labels from `PAYLOAD.project.tax_rates` (`LL-10`, `LL-11`, `LL-18`, `LL-20`).
      // PORTABILITY DON'T: Hardcode state codes or marginal rate string literals in chart/table tooltips.
      // Fallback heuristic inference if unlinked
      const invType = String(h.investment_type || "").toUpperCase();
      const acctLbl = String(h.account_label || h.account_id || "").toUpperCase();
      const projTax = (typeof PAYLOAD !== "undefined" && PAYLOAD && (PAYLOAD.tax_rates || (PAYLOAD.project && PAYLOAD.project.tax_rates))) || {};
      const ltcgRate = Number(projTax.ltcg_top ?? 0);
      const ordRate = Number(projTax.ordinary_top ?? 0);
      const ltcgPct = (ltcgRate * 100).toFixed(1);
      const ordPct = (ordRate * 100).toFixed(1);
      const stateCode = String(projTax.state_code || "").trim().toUpperCase();
      const muniJuris = stateCode ? `0% Fed/${stateCode}` : "0% Fed/State";
      const muniStateLbl = stateCode ? `${stateCode} state` : "state";

      if (acctLbl.includes("ROTH")) {
        return {
          title: "ROTH IRA — 0% Tax on Qualified Distributions",
          detail: "Held in a tax-free Roth retirement account. Qualified withdrawals are 100% Federal & state tax-free; growth compounds tax-free.",
          wrapper: "ROTH_IRA",
          model: "TAX_FREE",
          rate: 0.0,
        };
      }
      if (acctLbl.includes("IRA") || acctLbl.includes("401K") || acctLbl.includes("SEP")) {
        return {
          title: `Tax-Deferred IRA / 401(k) — ${ordPct}% Ordinary Income`,
          detail: "Pre-tax retirement account. Entire distribution (both basis and earnings) is taxed as ordinary income upon withdrawal.",
          wrapper: "TRAD_IRA",
          model: "ORDINARY_FULL",
          rate: Number(ordPct) / 100,
        };
      }
      if (invType.includes("MUNICIPAL") || invType.includes("MUNI")) {
        return {
          title: `${muniJuris} Tax on Yield • LTCG on Premium`,
          detail: `Municipal bond coupon yield is 100% Federal & ${muniStateLbl} tax-exempt; capital gains above purchase cost basis are taxed at LTCG rates upon sale.`,
          wrapper: "TAXABLE",
          model: "MUNI",
          rate: Number(ltcgPct) / 100,
        };
      }
      if (invType.includes("ANNUITY")) {
        return {
          title: `IRS §72 LIFO: ${ordPct}% Ord. Inc. on Gain, 0% Prin.`,
          detail: "Under IRS §72 annuity rules, withdrawals pull accumulated earnings first at ordinary income rates; original contract principal is recovered tax-free afterwards.",
          wrapper: "TAXABLE",
          model: "ANNUITY_LIFO",
          rate: Number(ordPct) / 100,
        };
      }
      if (invType.includes("CASH") || invType.includes("DEPOSIT") || invType.includes("SWEEP")) {
        return {
          title: "0% Tax on Principal",
          detail: "Principal withdrawals are 100% tax-free; only new accumulated cash interest is subject to annual ordinary income taxation.",
          wrapper: "TAXABLE",
          model: "CASH",
          rate: 0.0,
        };
      }
      return {
        title: `LTCG (${ltcgPct}%) on Gain above Cost Basis`,
        detail: `Return of principal is 0% tax; embedded net capital gains above cost basis are taxed at ${ltcgPct}% combined top LTCG rate upon sale.`,
        wrapper: "TAXABLE",
        model: "LTCG_POOL",
        rate: Number(ltcgPct) / 100,
      };
    }
    window.resolveHoldingTaxRule = resolveHoldingTaxRule;

    function openFriendlyNameDialog(key, evt) {
      if (evt) {
        evt.stopPropagation();
        evt.preventDefault();
      }
      if (String(key || "").startsWith("hyp_")) {
        openHypotheticalAssetDialog(key, false);
        return;
      }
      const h = findHoldingByKey(key) || { holding_key: key, title: key, statement_name: key };
      window._activeFnHoldingKey = key;
      window._activeFnHolding = h;
      const tierTable = document.getElementById("fn-tier-table");
      if (tierTable) tierTable.innerHTML = growthTierTableHtml(h);

      const bankEl = document.getElementById("fn-dlg-bank");
      const acctEl = document.getElementById("fn-dlg-acct");
      const stmtEl = document.getElementById("fn-dlg-stmt-name");
      const typeEl = document.getElementById("fn-dlg-type");
      const matEl = document.getElementById("fn-dlg-maturity");
      const curValEl = document.getElementById("fn-dlg-curval");
      const srcBadgeEl = document.getElementById("fn-dlg-source-badge");
      const pctEl = document.getElementById("fn-dlg-pct");
      const qtyEl = document.getElementById("fn-dlg-qty");
      const priceEl = document.getElementById("fn-dlg-price");
      const princEl = document.getElementById("fn-dlg-princ");
      const princSubEl = document.getElementById("fn-dlg-princ-sub");
      const inputEl = document.getElementById("fn-input");
      const msgEl = document.getElementById("fn-dialog-msg");

      if (bankEl) bankEl.textContent = h.institution || (h.account_label ? h.account_label.split("•")[0].trim() : "—");
      if (acctEl) acctEl.textContent = h.account_label || h.account_id || "—";
      if (stmtEl) stmtEl.textContent = h.statement_name || h.title || "—";
      if (typeEl) {
        const rawType = h.investment_type || h.asset_class || "";
        typeEl.textContent = typeof formatInvestmentType === "function" ? formatInvestmentType(rawType) : (rawType || "—");
      }
      if (matEl) matEl.textContent = h.maturity_str || h.maturity_date || "— (Perpetual / N/A)";

      if (curValEl) {
        curValEl.textContent = (h.current_value !== undefined && h.current_value !== null) ? fmtUSD2(h.current_value) : "—";
      }
      if (srcBadgeEl) {
        if (h.source_label) {
          srcBadgeEl.textContent = h.source_label;
          srcBadgeEl.title = h.source_tooltip || "";
          srcBadgeEl.className = `pill mono ${h.is_scrapeable ? 'pill-green' : 'pill-slate'}`;
          srcBadgeEl.style.display = "inline-block";
        } else {
          srcBadgeEl.style.display = "none";
        }
      }
      if (pctEl) {
        if (h.pct_portfolio !== undefined && h.pct_portfolio !== null) {
          pctEl.textContent = `${h.pct_portfolio.toFixed(2)}%`;
        } else if (PAYLOAD && PAYLOAD.kpis && PAYLOAD.kpis.total_vested > 0 && h.current_value) {
          pctEl.textContent = `${((h.current_value / PAYLOAD.kpis.total_vested) * 100).toFixed(2)}%`;
        } else {
          pctEl.textContent = "—";
        }
      }
      if (qtyEl) {
        qtyEl.textContent = h.quantity_str || "—";
      }
      if (priceEl) {
        priceEl.textContent = h.stmt_price_str || "—";
      }
      if (princEl) {
        princEl.textContent = h.principal_str || "—";
      }
      if (princSubEl) {
        princSubEl.textContent = h.principal_sub ? `(${h.principal_sub})` : "";
      }

      // Surrender Value
      const surrItem = document.getElementById("fn-dlg-surrender-item");
      const surrEl = document.getElementById("fn-dlg-surrender");
      let surrVal = h.surrender_val;
      if (surrVal === undefined && h.subtitle && h.subtitle.includes("Surrender val:")) {
        const m = h.subtitle.match(/Surrender val:\s*(\$[0-9,]+(?:\.[0-9]{2})?)/);
        if (m) surrVal = m[1];
      }
      if (surrItem && surrEl) {
        if (surrVal != null && surrVal !== "") {
          surrEl.textContent = typeof surrVal === "number" ? fmtUSD2(surrVal) : surrVal;
          surrItem.style.display = "flex";
        } else {
          surrItem.style.display = "none";
        }
      }

      // Accrued Coupon / Interest
      const accrItem = document.getElementById("fn-dlg-accrued-item");
      const accrEl = document.getElementById("fn-dlg-accrued");
      let accrVal = h.accrued_interest;
      if (accrVal === undefined && h.subtitle && h.subtitle.includes("accrued coupon")) {
        const m = h.subtitle.match(/([+\-]?\$[0-9,]+(?:\.[0-9]{2})?)\s*accrued coupon/);
        if (m) accrVal = m[1];
      }
      if (accrItem && accrEl) {
        if (accrVal != null && accrVal !== "") {
          accrEl.textContent = typeof accrVal === "number" ? fmtUSD2(accrVal) : accrVal;
          accrItem.style.display = "flex";
        } else {
          accrItem.style.display = "none";
        }
      }

      // Call Provision
      const callItem = document.getElementById("fn-dlg-call-item");
      const callEl = document.getElementById("fn-dlg-call");
      let callVal = h.call_info;
      if (!callVal && h.subtitle && h.subtitle.includes("Call ")) {
        const m = h.subtitle.match(/Call\s+([^•\n]+)/);
        if (m) callVal = m[1].trim();
      }
      if (callItem && callEl) {
        if (callVal) {
          callEl.textContent = callVal;
          callItem.style.display = "flex";
        } else {
          callItem.style.display = "none";
        }
      }

      // Valuation / As of Date
      const asofItem = document.getElementById("fn-dlg-asof-item");
      const asofEl = document.getElementById("fn-dlg-asof");
      let asofVal = h.asof_date || h.stmt_date;
      if (!asofVal && h.subtitle && h.subtitle.includes("As of ")) {
        const m = h.subtitle.match(/As of\s+([0-9]{4}-[0-9]{2}-[0-9]{2})/);
        if (m) asofVal = m[1];
      }
      if (asofItem && asofEl) {
        if (asofVal) {
          asofEl.textContent = asofVal;
          asofItem.style.display = "flex";
        } else {
          asofItem.style.display = "none";
        }
      }

      // Recent Activity / Notes
      const actItem = document.getElementById("fn-dlg-activity-item");
      const actEl = document.getElementById("fn-dlg-activity");
      let actVal = h.recent_vest_note;
      if (!actVal && h.subtitle && h.subtitle.includes("vest")) {
        const m = h.subtitle.match(/(Incl\.[^•\n]+)/);
        if (m) actVal = m[1].trim();
      }
      if (actItem && actEl) {
        if (actVal) {
          actEl.textContent = actVal;
          actItem.style.display = "flex";
        } else {
          actItem.style.display = "none";
        }
      }

      // Tax Rules When Sold / Liquidated
      const taxRuleItem = document.getElementById("fn-dlg-tax-rule-item");
      const taxTitleEl = document.getElementById("fn-dlg-tax-title");
      const taxDetailEl = document.getElementById("fn-dlg-tax-detail");
      const taxBadgeEl = document.getElementById("fn-dlg-tax-wrapper-badge");
      const taxInfo = resolveHoldingTaxRule(h);
      if (taxRuleItem && taxTitleEl) {
        if (taxInfo && (taxInfo.title || taxInfo.detail)) {
          taxTitleEl.textContent = taxInfo.title || "Standard Tax Rules";
          if (taxDetailEl) taxDetailEl.textContent = taxInfo.detail || "";
          if (taxBadgeEl) {
            const rawWrap = (taxInfo.wrapper || "TAXABLE").replace(/_/g, " ").toUpperCase();
            taxBadgeEl.textContent = rawWrap;
            taxBadgeEl.style.display = "inline-block";
            if (taxInfo.model === "TAX_FREE" || rawWrap.includes("ROTH")) {
              taxBadgeEl.className = "pill pill-green mono";
            } else if (taxInfo.model === "ORDINARY_FULL" || rawWrap.includes("IRA") || rawWrap.includes("401K")) {
              taxBadgeEl.className = "pill pill-amber mono";
            } else if (taxInfo.model === "MUNI") {
              taxBadgeEl.className = "pill pill-cyan mono";
            } else if (taxInfo.model === "ANNUITY_LIFO") {
              taxBadgeEl.className = "pill pill-rose mono";
            } else {
              taxBadgeEl.className = "pill pill-blue mono";
            }
          }
          taxRuleItem.style.display = "block";
        } else {
          taxRuleItem.style.display = "none";
        }
      }

      const prefEntry = (globalPreferences && globalPreferences.friendly_names && globalPreferences.friendly_names[key]) || {};
      const currentFn = (prefEntry && prefEntry.friendly_name) || h.friendly_name || "";
      if (inputEl) {
        inputEl.value = currentFn;
        inputEl.placeholder = `e.g. ${h.statement_name || h.title || 'Friendly Name'}`;
      }

      const feeInputEl = document.getElementById("fn-fee-input");
      const commentInputEl = document.getElementById("fn-comment-input");
      const currentFee = (prefEntry && prefEntry.fee_pct !== undefined && prefEntry.fee_pct !== null)
        ? prefEntry.fee_pct
        : (h.fee_pct !== undefined && h.fee_pct !== null ? h.fee_pct : null);
      if (feeInputEl) {
        feeInputEl.value = (currentFee !== null && currentFee !== undefined) ? `${currentFee}%` : "";
      }

      const currentComment = (prefEntry && prefEntry.comment) || h.comment || (prefEntry && prefEntry.notes) || h.notes || "";
      if (commentInputEl) {
        commentInputEl.value = currentComment;
      }

      // Populate Right Column: Active Scenario Forecast & Volatility Risk Class
      const mdlNameEl = document.getElementById("fn-dlg-model-name");
      const fkEl = document.getElementById("fn-dlg-forecast-key");
      const refBtn = document.getElementById("fn-dlg-ref-btn");
      const inhNoteEl = document.getElementById("fn-dlg-inherit-note");
      const provRateEl = document.getElementById("fn-dlg-prov-rate");
      const provConfEl = document.getElementById("fn-dlg-prov-conf");
      const provDetailEl = document.getElementById("fn-dlg-prov-detail");

      const resolved = resolveHoldingForecast(h);
      const fk = h.forecast_key || `pos:${key}`;
      window._fnDraftGrowthPct = resolved.growth_pct;
      window._fnDraftHalfLife = holdingHalfLife(h);

      const defRisk = RISK_CLASSES_META[h.default_risk_class] ? h.default_risk_class : "broad_equity";
      window._fnDraftRiskClass = defRisk; // Immutable ingested classification!

      const meta = RISK_CLASSES_META[defRisk] || RISK_CLASSES_META.broad_equity;
      const realizedVol = (h.realized_vol_pct != null && Number(h.realized_vol_pct) > 0 && defRisk !== "cash_fixed") ? Number(h.realized_vol_pct) : null;
      const refVol = meta.vol;

      if (resolved.vol_pct != null && Number.isFinite(Number(resolved.vol_pct))) {
        window._fnDraftVolPct = Number(resolved.vol_pct);
      } else if (realizedVol != null) {
        window._fnDraftVolPct = realizedVol;
      } else {
        window._fnDraftVolPct = refVol;
      }
      window._fnHoldingRealizedVolPct = realizedVol;
      window._fnHoldingRefVolPct = refVol;

      if (mdlNameEl) mdlNameEl.textContent = currentSaveName || "Default";
      if (fkEl) fkEl.textContent = fk;
      const scopePillEl = document.getElementById("fn-dlg-scenario-scope-pill");
      if (scopePillEl) {
        const activeScenLbl = String(currentSaveName || "").trim();
        scopePillEl.textContent = activeScenLbl ? `\u25C6 Scenario: ${activeScenLbl}` : "\u25C6 Saved to Scenario";
      }

      const isSingleRef = h.is_goog || (PRIMARY_SYMBOL && (h.symbol === PRIMARY_SYMBOL || String(fk).toUpperCase() === PRIMARY_SYMBOL)) || h.default_risk_class === "single_stock";
      const computedRefG = Number(holdingReferenceGrowth(h) ?? (h.rate_model === "fixed" && h.annual_rate_pct != null
        ? h.annual_rate_pct : (isSingleRef ? getGoogReferenceRate() : getNonGoogReferenceRate())));
      window._fnHoldingRefGrowthPct = computedRefG;
      if (refBtn) {
        const signStr = computedRefG >= 0 ? "+" : "";
        refBtn.textContent = `Hist. Ref (${signStr}${computedRefG.toFixed(2)}%)`;

        refBtn.setAttribute("data-tip", projectedGrowthTooltip(h, {growth_pct: computedRefG}, "Reference growth")
          + "<div>Click to use this baseline in the scenario.</div>");
      }

      // Ingested Baseline Provenance Card
      const ingGp = computedRefG;
      const conf = String(h.confidence || h.forecast_confidence || "MEDIUM").toUpperCase();
      if (provRateEl) {
        provRateEl.textContent = `${ingGp >= 0 ? "+" : ""}${ingGp.toFixed(2)}% / yr`;
      }
      if (provConfEl) {
        provConfEl.textContent = conf;
        provConfEl.className = "fn-conf-badge mono " + (
          conf === "LOW" ? "fn-conf-low" : (conf === "MEDIUM" ? "fn-conf-medium" : "fn-conf-high")
        );
      }
      if (provDetailEl) {
        const parts = [];
        if (h.source_detail || h.forecast_source_detail) parts.push(h.source_detail || h.forecast_source_detail);
        else if (h.forecast_source) parts.push(`Source: ${h.forecast_source}`);
        if (h.realized_vol_pct != null && Number(h.realized_vol_pct) > 0) {
          const betaTxt = h.beta_spy != null ? `, S&P 500 Sensitivity=${Number(h.beta_spy).toFixed(2)}` : "";
          parts.push(`Realized Vol=${Number(h.realized_vol_pct).toFixed(1)}%${betaTxt}`);
        }
        provDetailEl.textContent = parts.join(" • ") || "Ingested baseline from statement & market data.";
      }

      if (inhNoteEl) {
        if (resolved.inherited) {
          inhNoteEl.textContent = `Currently inheriting ${resolved.growth_pct >= 0 ? "+" : ""}${resolved.growth_pct.toFixed(1)}% from parent sleeve (${resolved.inherited_from}). Saving sets an explicit override on ${fk}.`;
        } else if (resolved.is_override) {
          inhNoteEl.textContent = `Scenario override active on "${fk}" (ingested baseline: ${ingGp >= 0 ? "+" : ""}${ingGp.toFixed(2)}%/yr).`;
        } else {
          inhNoteEl.textContent = `Using ingested baseline (${ingGp >= 0 ? "+" : ""}${ingGp.toFixed(2)}%/yr). Saving sets an explicit Scenario override on "${fk}".`;
        }
      }
      syncFnDialogForecastUI();

      // Bind dynamic tooltip & input listeners for Reset All button
      const revertBtn = document.getElementById("fn-revert-btn");
      if (revertBtn && !revertBtn._hasResetTipBound) {
        revertBtn._hasResetTipBound = true;
        revertBtn.addEventListener("mouseenter", updateFnResetAllTooltip);
      }
      ["fn-input", "fn-fee-input", "fn-comment-input"].forEach(id => {
        const el = document.getElementById(id);
        if (el && !el._hasResetTipBound) {
          el._hasResetTipBound = true;
          el.addEventListener("input", updateFnResetAllTooltip);
        }
      });
      updateFnResetAllTooltip();

      if (msgEl) {
        msgEl.className = "save-dlg-msg mono";
        msgEl.textContent = "";
      }

      const bd = document.getElementById("friendly-name-dialog-backdrop");
      if (bd) {
        bd.classList.add("open");
        setTimeout(() => {
          const numEl = document.getElementById("fn-dlg-growth-num");
          if (numEl) { numEl.focus(); numEl.select(); }
          else if (inputEl) inputEl.focus();
        }, 60);
      }
    }

    function closeFriendlyNameDialog() {
      const bd = document.getElementById("friendly-name-dialog-backdrop");
      if (bd) bd.classList.remove("open");
      window._activeFnHoldingKey = null;
      window._activeFnHolding = null;
    }

    let _fnBackdropMouseDown = false;

    function onFriendlyNameBackdropMouseDown(evt) {
      const bd = document.getElementById("friendly-name-dialog-backdrop");
      _fnBackdropMouseDown = Boolean(evt && bd && evt.target === bd);
    }

    function onFriendlyNameBackdropClick(evt) {
      const bd = document.getElementById("friendly-name-dialog-backdrop");
      const startedOnBackdrop = _fnBackdropMouseDown;
      _fnBackdropMouseDown = false;
      if (evt.target === bd && startedOnBackdrop) closeFriendlyNameDialog();
    }

    function onFriendlyNameInputKeyDown(evt) {
      if (evt.key === "Enter") {
        if (evt.target && evt.target.tagName === "TEXTAREA") {
          if (evt.metaKey || evt.ctrlKey) {
            evt.preventDefault();
            saveFriendlyName();
          }
          return;
        }
        evt.preventDefault();
        saveFriendlyName();
      } else if (evt.key === "Escape") {
        evt.preventDefault();
        closeFriendlyNameDialog();
      }
    }

    function applyHoldingFriendlyNameEverywhere(key, newName, origStmtName, feePct, comment) {
      const cleanName = (newName || "").trim();
      const linkedForecastKeys = new Set([key]);
      function updateObj(obj) {
        if (!obj.is_sub_segment && obj.forecast_key) linkedForecastKeys.add(obj.forecast_key);
        obj.friendly_name = cleanName;
        obj.title = cleanName || obj.statement_name || origStmtName || obj.title;
        if (feePct !== undefined) obj.fee_pct = feePct;
        if (comment !== undefined) obj.comment = comment;
      }

      const updateList = (list) => {
        (list || []).forEach(g => {
          if (g.holding_key === key || g.group_id === key || g.security_id === key || g.forecast_key === key) {
            updateObj(g);
          }
          if (g.children) {
            g.children.forEach(c => {
              const childKey = c.holding_key || (c.account_id && c.security_id ? (c.account_id + ":" + c.security_id) : null);
              if (c.holding_key === key || childKey === key || c.security_id === key || c.symbol === key) {
                updateObj(c);
              }
            });
          }
        });
      };

      updateList(PAYLOAD.holdings_by_strategy);
      updateList(PAYLOAD.holdings_by_account);

      if (feePct !== undefined && typeof holdingFeePct === "object" && holdingFeePct) {
        if (feePct != null && Number.isFinite(Number(feePct))) holdingFeePct[key] = Number(feePct);
        else delete holdingFeePct[key];
      }

      const simBucketsToUpdate = [
        ...(PAYLOAD.retirement_sim_assets || []),
        ...(BASE_SIM_ASSETS || []),
        ...(SIM_ASSETS || []),
      ];
      const seenBucketObjs = new Set();
      simBucketsToUpdate.forEach(a => {
        if (!a || seenBucketObjs.has(a)) return;
        seenBucketObjs.add(a);
        if (!a._orig_title) a._orig_title = a.title || a.label;
        if (!a._orig_short_title) a._orig_short_title = a.short_title || a.short_label;
        const matchesSim = linkedForecastKeys.has(a.id) || linkedForecastKeys.has(a.forecast_key);
        if (matchesSim) {
          a.friendly_name = cleanName;
          if (cleanName) {
            a.title = cleanName;
            a.short_title = cleanName;
            a.label = cleanName;
            a.short_label = cleanName;
          } else {
            a.title = a._orig_title;
            a.short_title = a._orig_short_title;
            a.label = a._orig_title;
            a.short_label = a._orig_short_title;
          }
          if (feePct !== undefined) {
            a.fee_pct = feePct;
            const afk = a.forecast_key || a.id;
            if (afk && typeof holdingFeePct === "object" && holdingFeePct) {
              if (feePct != null && Number.isFinite(Number(feePct))) holdingFeePct[afk] = Number(feePct);
              else delete holdingFeePct[afk];
            }
          }
        }
      });
    }

    function updateFnResetAllTooltip() {
      const btn = document.getElementById("fn-revert-btn");
      if (!btn) return;

      const key = window._activeFnHoldingKey;
      const h = window._activeFnHolding || (typeof findHoldingByKey === "function" ? findHoldingByKey(key) : {}) || {};
      const stmtName = (h.statement_name || h.title || "Statement Name").trim();

      // 1. Friendly Name
      const fnInput = document.getElementById("fn-input");
      const currFn = (fnInput ? fnInput.value : "").trim();
      const targetFn = stmtName;

      // 2. Advisory / Management Fee
      const feeInput = document.getElementById("fn-fee-input");
      const currFeeRaw = (feeInput ? feeInput.value : "").replace("%", "").trim();
      const currFee = currFeeRaw !== "" ? parseFloat(currFeeRaw) : null;
      const targetFee = (h.fee_pct !== undefined && h.fee_pct !== null) ? Number(h.fee_pct) : null;

      // 3. Global Holding Note
      const commentInput = document.getElementById("fn-comment-input");
      const currComment = (commentInput ? commentInput.value : "").trim();

      // 4. Projected Annual Return (CAGR)
      const targetGrowth = editableHoldingGrowth(window._fnHoldingRefGrowthPct);
      const growthNumEl = document.getElementById("fn-dlg-growth-num");
      const currGrowth = growthNumEl && Number.isFinite(parseFloat(growthNumEl.value))
        ? editableHoldingGrowth(growthNumEl.value)
        : (window._fnDraftGrowthPct != null ? editableHoldingGrowth(window._fnDraftGrowthPct) : targetGrowth);

      // 5. Annual Volatility
      const baseRc = RISK_CLASSES_META[h.default_risk_class] ? h.default_risk_class : "broad_equity";
      const meta = RISK_CLASSES_META[baseRc] || RISK_CLASSES_META.broad_equity;
      const realizedVol = (h.realized_vol_pct != null && Number(h.realized_vol_pct) > 0 && baseRc !== "cash_fixed") ? Number(h.realized_vol_pct) : null;
      const targetVol = Math.round((realizedVol != null ? realizedVol : meta.vol) * 10) / 10;
      const volNumEl = document.getElementById("fn-dlg-vol-num");
      const currVol = volNumEl && Number.isFinite(parseFloat(volNumEl.value))
        ? Math.round(parseFloat(volNumEl.value) * 10) / 10
        : (window._fnDraftVolPct != null ? Math.round(Number(window._fnDraftVolPct) * 10) / 10 : targetVol);

      const changes = [];
      let unchangedCount = 0;

      if (holdingFadeTarget(h) != null && window._fnDraftHalfLife !== holdingHalfLife(h, true)) {
        changes.push(`<div>Return half-life · ${formatHalfLifeLabel(window._fnDraftHalfLife)} → ${formatHalfLifeLabel(holdingHalfLife(h, true))}</div>`);
      }
      // Check Friendly Name
      if (currFn && currFn !== targetFn) {
        const preview = currFn.length > 28 ? currFn.slice(0, 25) + "..." : currFn;
        const tgtPreview = targetFn.length > 28 ? targetFn.slice(0, 25) + "..." : targetFn;
        changes.push(`<div>&bull; <strong>Friendly Name:</strong> <span style="color:#cbd5e1;">&ldquo;${escHtml(preview)}&rdquo;</span> &rarr; <span style="color:#38bdf8;">&ldquo;${escHtml(tgtPreview)}&rdquo;</span> (Statement default)</div>`);
      } else {
        unchangedCount++;
      }

      // Check Advisory Fee
      const feesDiffer = (currFee === null && targetFee !== null) || (currFee !== null && targetFee === null) || (currFee !== null && targetFee !== null && Math.abs(currFee - targetFee) > 0.0001);
      if (feesDiffer) {
        const fromStr = currFee !== null ? `${currFee}%` : "None";
        const toStr = targetFee !== null ? `${targetFee}%` : "None (0%)";
        changes.push(`<div>&bull; <strong>Advisory Fee:</strong> <span style="color:#cbd5e1;">${fromStr}</span> &rarr; <span style="color:#38bdf8;">${toStr}</span></div>`);
      } else {
        unchangedCount++;
      }

      // Check Note
      if (currComment) {
        const preview = currComment.length > 24 ? currComment.slice(0, 21) + "..." : currComment;
        changes.push(`<div>&bull; <strong>Holding Note:</strong> <span style="color:#cbd5e1;">&ldquo;${escHtml(preview)}&rdquo;</span> &rarr; <span style="color:#38bdf8;">Cleared (None)</span></div>`);
      } else {
        unchangedCount++;
      }

      // Check CAGR
      const fk = h.forecast_key || `pos:${key}`;
      const hasOverride = Boolean(holdingForecasts && (fk in holdingForecasts || key in holdingForecasts));
      if (currGrowth !== targetGrowth || hasOverride) {
        const fromStr = `${currGrowth >= 0 ? "+" : ""}${currGrowth.toFixed(2)}%`;
        const toStr = `${targetGrowth >= 0 ? "+" : ""}${targetGrowth.toFixed(2)}% / yr (Ingested Baseline)`;
        changes.push(`<div>&bull; <strong>Projected CAGR:</strong> <span style="color:#cbd5e1;">${fromStr}</span> &rarr; <span style="color:#38bdf8;">${toStr}</span></div>`);
      } else {
        unchangedCount++;
      }

      // Check Volatility
      if (Math.abs(currVol - targetVol) > 0.05) {
        const fromStr = `${currVol.toFixed(1)}%`;
        const toStr = `${targetVol.toFixed(1)}% (Baseline Vol)`;
        changes.push(`<div>&bull; <strong>Annual Volatility:</strong> <span style="color:#cbd5e1;">${fromStr}</span> &rarr; <span style="color:#38bdf8;">${toStr}</span></div>`);
      } else {
        unchangedCount++;
      }

      let html;
      if (changes.length > 0) {
        html = `<div style="font-weight:700;color:#f87171;margin-bottom:6px;">Reset All to Baseline Defaults</div>` +
               `<div style="font-size:11px;color:#94a3b8;margin-bottom:6px;">Clicking Reset All will revert ${changes.length} field${changes.length > 1 ? "s" : ""}:</div>` +
               `<div style="display:flex;flex-direction:column;gap:4px;font-size:11px;line-height:1.4;">` +
               changes.join("") +
               `</div>` +
               (unchangedCount > 0 ? `<div style="font-size:10px;color:#64748b;margin-top:6px;border-top:1px solid rgba(148,163,184,0.2);padding-top:4px;">${unchangedCount} other field${unchangedCount > 1 ? "s are" : " is"} already at default.</div>` : "");
      } else {
        html = `<div style="font-weight:700;color:#38bdf8;margin-bottom:6px;">Reset All to Baseline Defaults</div>` +
               `<div style="font-size:11px;color:#94a3b8;margin-bottom:6px;">All fields currently match baseline defaults:</div>` +
               `<div style="display:flex;flex-direction:column;gap:3px;font-size:11px;line-height:1.4;color:#cbd5e1;">` +
               `<div>&bull; <strong>Friendly Name:</strong> &ldquo;${escHtml(targetFn.length > 28 ? targetFn.slice(0, 25) + '...' : targetFn)}&rdquo;</div>` +
               `<div>&bull; <strong>Advisory Fee:</strong> ${targetFee !== null ? targetFee + "%" : "None (0%)"}</div>` +
               `<div>&bull; <strong>Holding Note:</strong> Cleared</div>` +
               `<div>&bull; <strong>Projected CAGR:</strong> ${targetGrowth >= 0 ? "+" : ""}${targetGrowth.toFixed(2)}% / yr (Ingested Baseline)</div>` +
               `<div>&bull; <strong>Annual Volatility:</strong> ${targetVol.toFixed(1)}% (Baseline Vol)</div>` +
               `</div>`;
      }

      btn.setAttribute("data-tip", html);
      btn.setAttribute("title", changes.length > 0 ? `Reset ${changes.length} field${changes.length > 1 ? 's' : ''} to defaults` : "All fields at default");
    }

    async function saveFriendlyName(isReset = false) {
      // SECURITY INVARIANT (`SA3-13` / `SA2-05`): Block holding friendly-name/fee/comment writes in Demo Mode
      // before mutating DOM or calling `/api/holding-meta` (which the server also rejects with 403).
      if (typeof isDemoModeActive === "function" && isDemoModeActive()) {
        if (typeof showDemoBlockedAlert === "function") showDemoBlockedAlert();
        return;
      }
      const resetMode = Boolean(isReset === true);
      const key = window._activeFnHoldingKey;
      const h = window._activeFnHolding || findHoldingByKey(key) || {};
      if (!key) {
        closeFriendlyNameDialog();
        return;
      }
      const inputEl = document.getElementById("fn-input");
      const newName = resetMode ? "" : (inputEl ? inputEl.value : "").trim();
      const stmtName = h.statement_name || h.title || "";

      const feeInputEl = document.getElementById("fn-fee-input");
      const commentInputEl = document.getElementById("fn-comment-input");
      let parsedFee = null;
      if (!resetMode && feeInputEl) {
        const rawFee = feeInputEl.value.replace("%", "").trim();
        if (rawFee !== "") {
          const num = parseFloat(rawFee);
          if (!isNaN(num)) parsedFee = Math.round(num * 10000) / 10000;
        }
      }
      const cleanComment = resetMode ? "" : (commentInputEl ? commentInputEl.value.trim() : "");

      // 1. Update global holding metadata
      applyHoldingFriendlyNameEverywhere(key, newName, stmtName, parsedFee, cleanComment);

      if (!globalPreferences) globalPreferences = { friendly_names: {} };
      if (!globalPreferences.friendly_names) globalPreferences.friendly_names = {};

      if (resetMode) {
        delete globalPreferences.friendly_names[key];
      } else {
        const entry = {
          holding_key: key,
          friendly_name: newName,
          statement_name: stmtName,
          fee_pct: parsedFee,
          comment: cleanComment,
          account_id: h.account_id || null,
          security_id: h.security_id || null,
          institution: h.institution || null,
          investment_type: h.investment_type || null,
          maturity_date: h.maturity_date || null,
          updated_at: new Date().toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric", hour: "2-digit", minute: "2-digit" })
        };
        globalPreferences.friendly_names[key] = entry;
      }

      // 2. Update Active Scenario Per-Holding Forecast & Volatility Fine-Tuning
      const fk = h.forecast_key || `pos:${key}`;
      const prior = resolveHoldingForecast(h);
      const onlyHalfLifeChanged = !resetMode && window._fnDraftHalfLife !== holdingHalfLife(h)
        && editableHoldingGrowth(window._fnDraftGrowthPct) === editableHoldingGrowth(prior.growth_pct)
        && Math.abs(window._fnDraftVolPct - (prior.vol_pct ?? window._fnHoldingRealizedVolPct ?? window._fnHoldingRefVolPct)) <= 0.051;
      if (resetMode) {
        const parentKeys = Array.isArray(h.parent_forecast_keys) ? h.parent_forecast_keys : (h.parent_key ? [h.parent_key] : []);
        const parentHasHalfLifeOverride = parentKeys.some(pk => pk && holdingHalfLives && Object.prototype.hasOwnProperty.call(holdingHalfLives, pk) && holdingHalfLives[pk] != null);
        if (parentHasHalfLifeOverride) {
          holdingHalfLives[fk] = 5;
        } else {
          delete holdingHalfLives[fk];
        }
      } else if (holdingFadeTarget(h) != null) {
        const years = window._fnDraftHalfLife || 5;
        if (years === holdingHalfLife(h, true)) delete holdingHalfLives[fk];
        else holdingHalfLives[fk] = years;
      }
      if (resetMode) {
        if (holdingForecasts && typeof holdingForecasts === "object") {
          delete holdingForecasts[fk];
          if (h.holding_key) delete holdingForecasts[h.holding_key];
          if (key) delete holdingForecasts[key];
          const resolved = resolveHoldingForecast(h);
          if (resolved.inherited || resolved.constituent_override) {
            // Follow this row's own reference across refreshes without rewriting related holdings.
            holdingForecasts[fk] = {use_reference: true, growth_pct: window._fnHoldingRefGrowthPct,
              risk_class: h.default_risk_class || "broad_equity"};
          }
        }
      } else if (!onlyHalfLifeChanged) {
        const numEl = document.getElementById("fn-dlg-growth-num");
        const finalGrowth = numEl && Number.isFinite(parseFloat(numEl.value))
          ? editableHoldingGrowth(numEl.value)
          : (Number(window._fnDraftGrowthPct) || 0);
        const defRisk = RISK_CLASSES_META[h.default_risk_class] ? h.default_risk_class : "broad_equity";
        const finalRisk = defRisk; // Locked to immutable ingested archetype!

        const volNumEl = document.getElementById("fn-dlg-vol-num");
        const finalVol = volNumEl && Number.isFinite(parseFloat(volNumEl.value))
          ? Math.max(0, Math.min(150, Math.round(parseFloat(volNumEl.value) * 10) / 10))
          : (Number(window._fnDraftVolPct) || 0);

        if (!holdingForecasts || typeof holdingForecasts !== "object") {
          holdingForecasts = {};
        }
        const fcObj = { growth_pct: finalGrowth, risk_class: finalRisk };

        const meta = RISK_CLASSES_META[defRisk] || RISK_CLASSES_META.broad_equity;
        const realizedVol = (h.realized_vol_pct != null && Number(h.realized_vol_pct) > 0 && defRisk !== "cash_fixed") ? Number(h.realized_vol_pct) : null;
        const baseVol = realizedVol != null ? realizedVol : meta.vol;
        if (Math.abs(finalVol - baseVol) > 0.05) {
          fcObj.vol_pct = finalVol;
        }
        for (const parent of h.parent_forecast_keys || []) {
          if (holdingForecasts[parent]?.use_reference) delete holdingForecasts[parent];
        }
        if (finalGrowth === window._fnHoldingRefGrowthPct && h.constituent_forecasts?.some(c => holdingForecasts[c.forecast_key])) {
          fcObj.use_reference = true;
        }
        holdingForecasts[fk] = fcObj;
      }
      syncLegacyGrowthAliasesFromForecasts();

      // 3. Persist global metadata to backend
      try {
        const postEntry = {
          holding_key: key,
          friendly_name: newName,
          statement_name: stmtName,
          fee_pct: parsedFee,
          comment: cleanComment,
          account_id: h.account_id || null,
          security_id: h.security_id || null,
          institution: h.institution || null,
          investment_type: h.investment_type || null,
          maturity_date: h.maturity_date || null
        };
        await apiJson("/api/preferences/global", { body: postEntry });
      } catch (_e) {
        // Offline localStorage fallback already stored
      }

      // 4. Recompute & refresh all affected charts, tables, and Scenario dirty state
      SIM_ASSETS = materializeSimAssets();
      updateFutureKPI();
      recomputeSimBars();
      renderChartAndNav();
      renderHoldingsTable();
      renderHoldingsPie();
      updateSimSummaryDOM();
      renderSimChartAndNav();
      renderSimLiquidationTable();
      checkSaveDirtyState();

      closeFriendlyNameDialog();
    }

    async function revertFriendlyNameToStatement() {
      const inputEl = document.getElementById("fn-input");
      if (inputEl) inputEl.value = "";
      const feeInputEl = document.getElementById("fn-fee-input");
      if (feeInputEl) feeInputEl.value = "";
      const commentInputEl = document.getElementById("fn-comment-input");
      if (commentInputEl) commentInputEl.value = "";

      const key = window._activeFnHoldingKey;
      const h = window._activeFnHolding || (typeof findHoldingByKey === "function" ? findHoldingByKey(key) : {}) || {};
      const baseGp = editableHoldingGrowth(window._fnHoldingRefGrowthPct);

      const baseRc = RISK_CLASSES_META[h.default_risk_class] ? h.default_risk_class : "broad_equity";
      const meta = RISK_CLASSES_META[baseRc] || RISK_CLASSES_META.broad_equity;
      const realizedVol = (h.realized_vol_pct != null && Number(h.realized_vol_pct) > 0 && baseRc !== "cash_fixed") ? Number(h.realized_vol_pct) : null;
      const baseVol = realizedVol != null ? realizedVol : meta.vol;

      window._fnDraftGrowthPct = baseGp;
      window._fnDraftVolPct = baseVol;
      setFnDialogGrowth(baseGp);
      setFnDialogVol(baseVol);

      await saveFriendlyName(true);
    }

    async function fetchGlobalPreferences() {
      try {
        const { ok, data } = await apiJson("/api/preferences/global");
        if (!ok || !data) return;
        if (data && data.ok && data.friendly_names) {
          if (!globalPreferences) globalPreferences = { friendly_names: {} };
          if (!globalPreferences.friendly_names) globalPreferences.friendly_names = {};
          let anyChange = false;
          for (const [k, item] of Object.entries(data.friendly_names)) {
            const fn = (item && typeof item === "object") ? item.friendly_name : item;
            const fee = (item && typeof item === "object") ? item.fee_pct : null;
            const cmt = (item && typeof item === "object") ? (item.comment || item.notes || "") : "";
            const existing = globalPreferences.friendly_names[k]?.friendly_name || "";
            const existingFee = globalPreferences.friendly_names[k]?.fee_pct;
            const existingCmt = globalPreferences.friendly_names[k]?.comment;
            if (fn !== undefined && (fn !== existing || fee !== existingFee || cmt !== existingCmt)) {
              globalPreferences.friendly_names[k] = item;
              applyHoldingFriendlyNameEverywhere(k, fn, item.statement_name, fee, cmt);
              anyChange = true;
            }
          }
          if (anyChange) {
            renderHoldingsTable();
            renderHoldingsPie();
            if (typeof renderSimLiquidationTable === "function") renderSimLiquidationTable();
          }
        }
      } catch (e) {
        // advisorbot_server offline or initial boot
      }
    }

    function renderHoldingsPie() {
      const svg = document.getElementById("holdings-pie-svg");
      const leg = document.getElementById("holdings-pie-legend");
      if (!svg || !leg) return;

      const rawGroups = activeGroupsList() || [];
      const groups = rawGroups.slice().filter(g => g.current_value > 0).sort((a, b) => (b.pct_portfolio - a.pct_portfolio) || (b.current_value - a.current_value));
      window._pieGroups = groups;

      const totalVal = groups.reduce((s, g) => s + g.current_value, 0) || 1;
      const cx = 200, cy = 200, rOuter = 165, rInner = 84;
      let startAngle = -Math.PI / 2;
      let svgParts = [];
      let legParts = [];

      groups.forEach((g, idx) => {
        const col = PIE_PALETTE[idx % PIE_PALETTE.length];
        g._pieColor = col;
        const frac = Math.max(0, g.current_value / totalVal);
        const sweep = Math.min(Math.PI * 2 - 0.0005, frac * Math.PI * 2);
        const endAngle = startAngle + sweep;

        const x1 = cx + rOuter * Math.cos(startAngle);
        const y1 = cy + rOuter * Math.sin(startAngle);
        const x2 = cx + rOuter * Math.cos(endAngle);
        const y2 = cy + rOuter * Math.sin(endAngle);
        const x3 = cx + rInner * Math.cos(endAngle);
        const y3 = cy + rInner * Math.sin(endAngle);
        const x4 = cx + rInner * Math.cos(startAngle);
        const y4 = cy + rInner * Math.sin(startAngle);
        const largeArc = sweep > Math.PI ? 1 : 0;

        const d = [
          `M ${x1.toFixed(2)} ${y1.toFixed(2)}`,
          `A ${rOuter} ${rOuter} 0 ${largeArc} 1 ${x2.toFixed(2)} ${y2.toFixed(2)}`,
          `L ${x3.toFixed(2)} ${y3.toFixed(2)}`,
          `A ${rInner} ${rInner} 0 ${largeArc} 0 ${x4.toFixed(2)} ${y4.toFixed(2)}`,
          "Z"
        ].join(" ");

        svgParts.push(`<path id="pie-slice-${idx}" class="pie-slice" d="${d}" fill="${col}" stroke="#0f172a" stroke-width="2" onmousemove="showPieTooltip(event, ${idx})" onmouseleave="hidePieTooltip()"/>`);

        if (frac >= 0.045) {
          const midAngle = startAngle + sweep / 2;
          const rLbl = (rOuter + rInner) / 2;
          const lx = cx + rLbl * Math.cos(midAngle);
          const ly = cy + rLbl * Math.sin(midAngle);
          svgParts.push(`<text x="${lx.toFixed(1)}" y="${(ly + 4).toFixed(1)}" text-anchor="middle" fill="#0f172a" font-size="11" font-weight="800" font-family="ui-monospace, monospace" pointer-events="none">${(frac * 100).toFixed(1)}%</text>`);
        }

        startAngle = endAngle;

        legParts.push(`<div id="pie-leg-${idx}" class="pie-legend-item" onmousemove="showPieTooltip(event, ${idx})" onmouseleave="hidePieTooltip()">
          <div style="display:flex;align-items:center;gap:8px;min-width:0;">
            <span class="swatch" style="background:${col};flex-shrink:0;width:11px;height:11px;"></span>
            <span style="font-size:12.2px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${escHtml(g.title)}</span>
          </div>
          <div class="mono" style="font-size:11.8px;white-space:nowrap;flex-shrink:0;">
            <strong>${g.pct_portfolio.toFixed(1)}%</strong> <span class="muted">(${fmtUSD0(g.current_value)})</span>
          </div>
        </div>`);
      });

      const grpLbl = tableGrouping === "strategy" ? `${groups.length} Holdings` : `${groups.length} Accounts`;
      svgParts.push(`<circle cx="${cx}" cy="${cy}" r="${rInner - 2}" fill="#0f172a" pointer-events="none"/>`);
      svgParts.push(`<text x="${cx}" y="${cy - 10}" text-anchor="middle" fill="#94a3b8" font-size="10.5" font-weight="700" letter-spacing="0.05em" pointer-events="none">TOTAL VESTED</text>`);
      svgParts.push(`<text x="${cx}" y="${cy + 10}" text-anchor="middle" fill="#f8fafc" font-size="15.5" font-weight="800" font-family="ui-monospace, monospace" pointer-events="none">${fmtUSD0(totalVal)}</text>`);
      svgParts.push(`<text x="${cx}" y="${cy + 27}" text-anchor="middle" fill="#7dd3fc" font-size="11" font-family="ui-monospace, monospace" pointer-events="none">${grpLbl}</text>`);

      svg.innerHTML = svgParts.join("");
      leg.innerHTML = legParts.join("");
    }

    function showPieTooltip(evt, idx) {
      const groups = window._pieGroups || [];
      const g = groups[idx];
      if (!g) return;

      groups.forEach((_, i) => {
        const sEl = document.getElementById(`pie-slice-${i}`);
        const lEl = document.getElementById(`pie-leg-${i}`);
        if (sEl) sEl.classList.toggle("active", i === idx);
        if (lEl) lEl.classList.toggle("active", i === idx);
      });

      const tip = document.getElementById("chart-tooltip");
      const wowCls = g.wow_change > 0 ? "pos" : (g.wow_change < 0 ? "neg" : "muted");
      const grCls = g.growth_sign > 0 ? "pos" : (g.growth_sign < 0 ? "neg" : "muted");
      const matRow = g.maturity_str
        ? `<div style="margin-top:3px;"><span class="mat-tag mono">${escHtml(g.maturity_str)}</span></div>`
        : "";
      const kidsNote = (g.children && g.children.length > 0)
        ? `<div class="muted" style="font-size:11px;margin-top:5px;border-top:1px solid #26334d;padding-top:4px;">Includes ${g.children.length} constituent positions (switch to Table view &amp; click &#9656; to inspect all ${g.children.length} items).</div>`
        : "";

      tip.innerHTML = `
        <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:10px;">
          <div>
            <div style="font-weight:700;font-size:13.2px;">
              <span class="swatch" style="background:${g._pieColor || '#38bdf8'};width:10px;height:10px;margin-right:6px;"></span>${escHtml(g.title)}
            </div>
            <div class="row-sub" style="margin-top:2px;">${escHtml(g.subtitle || "")}</div>
            ${matRow}
          </div>
          <span class="pill ${g.is_scrapeable ? 'pill-green' : 'pill-slate'} mono" style="flex-shrink:0;">${escHtml(g.source_label)}</span>
        </div>
        <div class="mono" style="font-size:16px;font-weight:700;margin:6px 0 4px 0;">
          ${fmtUSD2(g.current_value)} <span style="color:#7dd3fc;font-size:13px;">(${g.pct_portfolio.toFixed(2)}% of portfolio)</span>
        </div>
        <div style="border-top:1px solid #26334d;padding-top:6px;margin-top:4px;font-size:12px;display:grid;gap:3px;">
          <div style="display:flex;justify-content:space-between;gap:14px;">
            <span class="muted">Symbol / Account:</span>
            <span class="mono"><strong>${escHtml(g.symbol)}</strong> &bull; ${escHtml(g.account_label)}</span>
          </div>
          <div style="display:flex;justify-content:space-between;gap:14px;">
            <span class="muted">Quantity / Par:</span>
            <span class="mono">${escHtml(g.quantity_str)} (Stmt: ${escHtml(g.stmt_price_str)})</span>
          </div>
          <div style="display:flex;justify-content:space-between;gap:14px;">
            <span class="muted">Principal Investment:</span>
            <span class="mono"><strong>${escHtml(g.principal_str)}</strong> <span class="muted">(${escHtml(g.principal_sub || "")})</span></span>
          </div>
          <div style="display:flex;justify-content:space-between;gap:14px;">
            <span class="muted">Total Growth:</span>
            <span class="mono"><strong class="${grCls}">${escHtml(g.growth_str)}</strong> <span class="muted">${escHtml(g.growth_sub || "")}</span></span>
          </div>
          <div style="display:flex;justify-content:space-between;gap:14px;">
            <span class="muted">${(recentChangeMode === '1w' || g.is_scrapeable) ? '1-Week Change:' : 'Recent Performance:'}</span>
            <span class="mono ${g.recent_change_val ? (g.recent_change_sign > 0 ? 'pos' : (g.recent_change_sign < 0 ? 'neg' : 'muted')) : wowCls}">
              <strong>${recentChangeMode === '1w' ? (g.wow_change === 0 ? "—" : fmtSignedUSD2(g.wow_change)) : (g.recent_change_val !== undefined && g.recent_change_val !== null ? (g.recent_change_val === 0 && !g.recent_change_sub ? "—" : fmtSignedUSD2(g.recent_change_val)) : (g.wow_change === 0 ? "—" : fmtSignedUSD2(g.wow_change)))}</strong>
              ${(recentChangeMode !== '1w' && g.recent_change_sub) ? `<span class="muted"> (${escHtml(g.recent_change_sub)})</span>` : ''}
            </span>
          </div>
        </div>
        ${kidsNote}
      `;

      placeFloatingBoxAwayFromPointer(tip, evt.clientX, evt.clientY);
    }

    function hidePieTooltip() {
      document.getElementById("chart-tooltip").style.display = "none";
      const groups = window._pieGroups || [];
      groups.forEach((_, i) => {
        const sEl = document.getElementById(`pie-slice-${i}`);
        const lEl = document.getElementById(`pie-leg-${i}`);
        if (sEl) sEl.classList.remove("active");
        if (lEl) lEl.classList.remove("active");
      });
    }

    function renderEventsDrawer() {
      const evs = PAYLOAD.recent_events || [];
      document.getElementById("events-drawer-pill").textContent = `${evs.length} recent holdings updated`;
      document.getElementById("events-tbody").innerHTML = evs.map(ev => `
        <tr>
          <td class="mono muted">${escHtml(ev.occurred_at_iso)}</td>
          <td><span class="pill pill-blue mono">${escHtml(ev.event_type)}</span></td>
          <td class="mono">${escHtml(ev.account_id || "—")}</td>
          <td class="mono">${escHtml(ev.as_of_date || "—")}</td>
          <td>${escHtml(ev.summary)}</td>
        </tr>
      `).join("");
    }

    // =========================================================================
    // LIVE BROWSER EQUITY QUOTE AUTO-REFRESH & FULL SITE PROPAGATION
    // =========================================================================

    function updateHeaderGoogDisplay(overridePx = null) {
      const px = overridePx != null ? Number(overridePx) : Number(PAYLOAD.goog_live_price);
      const prevPx = Number(PAYLOAD.goog_prev_close);
      const googEl = document.getElementById("hdr-goog");
      const dispSym = currentActiveSymbol || PRIMARY_SYMBOL || "";
      if (googEl && dispSym && Number.isFinite(px) && px > 0) {
        googEl.textContent = `${dispSym} ${fmtUSD2(px)}`;
      }
      const chgEl = document.getElementById("hdr-goog-chg");
      if (chgEl) {
        if (Number.isFinite(px) && px > 0 && Number.isFinite(prevPx) && prevPx > 0) {
          const diff = px - prevPx;
          const pct = (diff / prevPx) * 100;
          const arrow = diff > 0.0001 ? "▲" : (diff < -0.0001 ? "▼" : "•");
          const sign = pct > 0.0001 ? "+" : "";
          const dSign = diff >= 0 ? "+" : "-";
          chgEl.textContent = `${arrow} ${sign}${pct.toFixed(2)}% today`;
          chgEl.style.color = diff > 0.0001 ? "#34d399" : (diff < -0.0001 ? "#fb7185" : "var(--muted)");
          chgEl.style.fontWeight = "700";
          chgEl.title = `Today's change vs. previous close (${fmtUSD2(prevPx)}): ${dSign}${fmtUSD2(Math.abs(diff))} (${sign}${pct.toFixed(2)}%)`;
        } else {
          chgEl.textContent = "";
          chgEl.title = "";
        }
      }
    }

    function applyLiveGoogPrice(newPxRaw, statusText) {
      const newPx = Math.round(Number(newPxRaw) * 100) / 100;
      const oldPx = Number(PAYLOAD.goog_live_price);
      if (!Number.isFinite(newPx) || newPx <= 0 || !Number.isFinite(oldPx) || oldPx <= 0) return;

      updateHeaderGoogDisplay(newPx);

      if (Math.abs(newPx - oldPx) < 1e-4) {
        return;
      }

      const dPx = newPx - oldPx;
      const pxRatio = newPx / oldPx;
      const k = PAYLOAD.kpis;
      const primSym = PROJECT_CFG.primary_equity_symbol || PRIMARY_SYMBOL || "";

      const msSh = Number(k.goog_available_shares) || 0;
      const allGoogSh = Number(k.goog_vested_shares) || msSh;
      const wfaSh = Math.max(0, allGoogSh - msSh);
      const combSh = allGoogSh;

      const dMsVal = msSh * dPx;
      const dWfaVal = wfaSh * dPx;
      const dCombVal = combSh * dPx;
      const dAllGoogVal = allGoogSh * dPx;

      // 1. Update top-level KPIs
      const priorPortfolioVal = k.total_vested - k.wow_change;
      PAYLOAD.goog_live_price = newPx;
      k.total_vested = Math.round((k.total_vested + dAllGoogVal) * 100) / 100;
      k.wow_change = Math.round((k.total_vested - priorPortfolioVal) * 100) / 100;
      k.wow_pct = priorPortfolioVal > 0 ? Math.round((k.wow_change / priorPortfolioVal) * 10000) / 100 : 0;
      k.live_scraped_val = Math.round((k.live_scraped_val + dAllGoogVal) * 100) / 100;
      k.live_scraped_pct = k.total_vested > 0 ? Math.round((k.live_scraped_val / k.total_vested) * 1000) / 10 : 0;
      k.anchored_pct = Math.round((100 - k.live_scraped_pct) * 10) / 10;

      k.goog_available_val = Math.round(msSh * newPx * 100) / 100;
      k.goog_available_pct = k.total_vested > 0 ? Math.round((k.goog_available_val / k.total_vested) * 1000) / 10 : 0;
      if (k.goog_vested_to_date_net_shares) {
        k.goog_vested_to_date_net_val = Math.round(k.goog_vested_to_date_net_shares * newPx * 100) / 100;
      }
      if (k.goog_vested_to_date_gross_shares) {
        k.goog_vested_to_date_gross_val = Math.round(k.goog_vested_to_date_gross_shares * newPx * 100) / 100;
      }
      k.goog_vested_val = Math.round((k.goog_vested_val + dAllGoogVal) * 100) / 100;
      k.goog_vested_pct = k.total_vested > 0 ? Math.round((k.goog_vested_val / k.total_vested) * 1000) / 10 : 0;

      k.future_gsu_gross_val = Math.round(k.future_gsu_gross_shares * newPx * 100) / 100;
      k.future_gsu_net_val = Math.round(k.future_gsu_gross_shares * NET_RATE * newPx * 100) / 100;

      // 2. Update TIMELINE (Today & Forecast bars)
      TIMELINE.forEach(w => {
        if (w.isToday || w.kind === "TODAY" || w.kind === "FORECAST") {
          w.vestedTotal = Math.round((w.vestedTotal + dAllGoogVal) * 100) / 100;
          if (w.projRelShares > 0) {
            w.projRelGross = Math.round(w.projRelShares * newPx * 100) / 100;
          }
          if (w.buckets && w.buckets.goog_combined != null) {
            w.buckets.goog_combined = Math.round((w.buckets.goog_combined + dCombVal) * 100) / 100;
          }
          if (w.assets && w.assets.EQUITY != null) {
            w.assets.EQUITY = Math.round((w.assets.EQUITY + dAllGoogVal) * 100) / 100;
          }
        }
      });

      // 3. Update Current Vested Holdings Summary (Strategy & Account rollups)
      const updateGroupWeights = list => {
        (list || []).forEach(g => {
          g.pct_portfolio = k.total_vested > 0 ? Math.round((g.current_value / k.total_vested) * 10000) / 100 : 0;
          if (g.children) {
            g.children.forEach(c => {
              c.pct_portfolio = k.total_vested > 0 ? Math.round((c.current_value / k.total_vested) * 10000) / 100 : 0;
            });
          }
        });
        list.sort((a, b) => (b.pct_portfolio - a.pct_portfolio) || (b.current_value - a.current_value));
      };

      (PAYLOAD.holdings_by_strategy || []).forEach(g => {
        if (g.symbol === primSym && g.current_value > 0) {
          const gSh = g.current_value / oldPx;
          const dVal = gSh * dPx;
          g.current_value = Math.round(gSh * newPx * 100) / 100;
          g.wow_change = Math.round((g.wow_change + dVal) * 100) / 100;
          if (g.subtitle) {
            g.subtitle = g.subtitle.replace(/Live quote: \$[0-9,.]+\/sh/, `Live quote: ${fmtUSD2(newPx)}/sh`);
          }
          if (g.principal_val && g.principal_date) {
            const yrs = (Date.parse(TODAY_ISO + "T00:00:00Z") - Date.parse(g.principal_date + "T00:00:00Z")) / (365.25 * 86400000);
            if (yrs > 0) {
              const cagr = (Math.pow(g.current_value / g.principal_val, 1 / yrs) - 1) * 100;
              const gain = g.current_value - g.principal_val;
              const gpct = (gain / g.principal_val) * 100;
              g.cagr_pct = cagr;
              g.growth_str = `${cagr >= 0 ? "+" : ""}${cagr.toFixed(2)}% / yr`;
              g.growth_sub = `${gain >= 0 ? "+" : "-"}$${Math.round(Math.abs(gain)).toLocaleString("en-US")} (${gpct >= 0 ? "+" : ""}${gpct.toFixed(1)}%)`;
            }
          }
        }
      });
      updateGroupWeights(PAYLOAD.holdings_by_strategy);

      (PAYLOAD.holdings_by_account || []).forEach(g => {
        let acctDelta = 0;
        (g.children || []).forEach(c => {
          if (c.symbol === primSym && c.current_value > 0) {
            const cSh = c.current_value / oldPx;
            const dVal = cSh * dPx;
            c.current_value = Math.round(cSh * newPx * 100) / 100;
            c.wow_change = Math.round((c.wow_change + dVal) * 100) / 100;
            acctDelta += dVal;
          }
        });
        if (acctDelta !== 0) {
          g.current_value = Math.round((g.current_value + acctDelta) * 100) / 100;
          g.wow_change = Math.round((g.wow_change + acctDelta) * 100) / 100;
        }
      });
      updateGroupWeights(PAYLOAD.holdings_by_account);

      // 4. Update Bucket Summary Box
      if (PAYLOAD.bucket_summary) {
        const bg = PAYLOAD.bucket_summary.goog;
        const bng = PAYLOAD.bucket_summary.non_goog;
        if (bg) {
          bg.value = Math.round(combSh * newPx * 100) / 100;
          bg.end_price = newPx;
          bg.pct_portfolio = k.total_vested > 0 ? Math.round((bg.value / k.total_vested) * 10000) / 100 : 0;
          if (bg.start_price > 0 && bg.start_date) {
            const yrs = (Date.parse(TODAY_ISO + "T00:00:00Z") - Date.parse(bg.start_date + "T00:00:00Z")) / (365.25 * 86400000);
            if (yrs > 0) {
              bg.annual_appreciation_pct = Math.round((Math.pow(newPx / bg.start_price, 1 / yrs) - 1) * 10000) / 100;
            }
          }
          if (bg.five_yr_start_price > 0 && bg.five_yr_start_date) {
            const yrs5 = (Date.parse(TODAY_ISO + "T00:00:00Z") - Date.parse(bg.five_yr_start_date + "T00:00:00Z")) / (365.25 * 86400000);
            if (yrs5 > 0) {
              bg.five_yr_cagr_pct = Math.round((Math.pow(newPx / bg.five_yr_start_price, 1 / yrs5) - 1) * 10000) / 100;
            }
          }
        }
        if (bng) {
          bng.pct_portfolio = Math.round((100 - (bg ? bg.pct_portfolio : 0)) * 100) / 100;
          if (bng.wfb_accounts_value != null) {
            bng.wfb_accounts_value = Math.round((bng.wfb_accounts_value + dWfaVal) * 100) / 100;
          }
        }
      }

      // 5. Update Stock Vesting Tab (REL_BARS, by_year, by_grant)
      // 5. Update Grant Schedules (`STOCK_GRANTS` for primary symbol + alias `PAYLOAD.google_stock`) & Retirement Sim Assets
      const primGrantObj = (STOCK_GRANTS.by_symbol && STOCK_GRANTS.by_symbol[primSym]) || PAYLOAD.google_stock;
      if (primGrantObj) {
        primGrantObj.live_price = newPx;
        primGrantObj.future_gross_val = Math.round((primGrantObj.future_gross_shares || 0) * newPx * 100) / 100;
        primGrantObj.future_net_val = Math.round((primGrantObj.future_est_net_shares || 0) * newPx * 100) / 100;
        const primComp = getGrantCompanies().find(c => c.symbol === primSym);
        if (primComp) {
          primComp.live_price = newPx;
          primComp.future_gross_val = primGrantObj.future_gross_val;
          primComp.future_net_val = primGrantObj.future_net_val;
        }
        (primGrantObj.release_bars || []).forEach(b => {
          b.totTodayNetVal = Math.round(b.totNetShares * newPx * 100) / 100;
          b.totTodayGrossVal = Math.round(b.totGrossShares * newPx * 100) / 100;
          if (b.kind === "ESTIMATED") {
            b.releasePrice = newPx;
            b.totRelNetVal = b.totTodayNetVal;
            b.totRelGrossVal = b.totTodayGrossVal;
          }
          Object.values(b.byGrant || {}).forEach(gb => {
            gb.val_today_net = Math.round(gb.net_sh * newPx * 100) / 100;
            gb.val_today_gross = Math.round(gb.gross_sh * newPx * 100) / 100;
            if (b.kind === "ESTIMATED") {
              gb.val_rel_net = gb.val_today_net;
              gb.val_rel_gross = gb.val_today_gross;
            }
          });
        });
        (primGrantObj.by_year || []).forEach(y => {
          const ybars = (primGrantObj.release_bars || []).filter(b => b.yearLbl === String(y.year));
          y.gross_val = Math.round(ybars.reduce((s, b) => s + b.totRelGrossVal, 0) * 100) / 100;
          y.net_val = Math.round(ybars.reduce((s, b) => s + b.totRelNetVal, 0) * 100) / 100;
          y.today_gross_val = Math.round(ybars.reduce((s, b) => s + b.totTodayGrossVal, 0) * 100) / 100;
          y.today_net_val = Math.round(ybars.reduce((s, b) => s + b.totTodayNetVal, 0) * 100) / 100;
        });
        (primGrantObj.by_grant || []).forEach(g => {
          g.gross_val = Math.round(g.total_qty * newPx * 100) / 100;
          g.net_val = Math.round(g.est_net_qty * newPx * 100) / 100;
        });
        const comps = getGrantCompanies();
        if (comps.length > 0) {
          k.future_gsu_gross_val = Math.round(comps.reduce((s, c) => s + (Number(c.future_gross_val) || 0), 0) * 100) / 100;
          k.future_gsu_net_val = Math.round(comps.reduce((s, c) => s + (Number(c.future_net_val) || 0), 0) * 100) / 100;
        }
        loadActiveGrantCompany(activeGrantSymbol);
      }

      if (Array.isArray(BASE_SIM_ASSETS)) {
        BASE_SIM_ASSETS.forEach(a => {
          if (!a.is_goog_priced) return;
          if (Array.isArray(a.tranches) && a.tranches.length > 0) {
            a.tranches.forEach(t => {
              t.current_value = Math.round(t.shares * newPx * 100) / 100;
              if (a.is_unvested_goog) {
                t.basis_per_share = newPx;
                t.cost_basis = t.current_value;
              }
            });
            a.current_value = Math.round(a.tranches.reduce((s, t) => s + t.current_value, 0) * 100) / 100;
            a.initial_value = a.current_value;
            if (a.is_unvested_goog) a.cost_basis = a.current_value;
          } else if (a.shares) {
            a.current_value = Math.round(a.shares * newPx * 100) / 100;
            a.initial_value = a.current_value;
          }
        });
      }
      SIM_ASSETS = materializeSimAssets();
      currentGoogPrice = newPx;

      // 6. Re-render all views
      initHeaderAndKPIs();
      renderChartAndNav();
      renderHoldingsTable();
      renderHoldingsPie();
      renderRelChartAndNav();
      renderGoogleStockTables();
      recomputeSimBars();
      renderSimChartAndNav();
      renderSimLiquidationTable();
      if (currentScreen === "models") renderModelsScreen();
    }

    // =========================================================================
    // PRIMARY SYMBOL & MULTI-SYMBOL TRACKER SYSTEM
    // =========================================================================
    let _trackedSymbolQuotes = {};
    try {
      const cached = sessionStorage.getItem("tracked_symbol_quotes");
      if (cached) _trackedSymbolQuotes = JSON.parse(cached) || {};
    } catch (_e) {}

    function _seedDefaultSymbolQuotes() {
      const primSym = String(PROJECT_CFG.primary_equity_symbol || PAYLOAD?.primary_equity_symbol || PRIMARY_SYMBOL || "").toUpperCase();
      if (primSym && PAYLOAD?.goog_live_price) {
        const defaultQ = {
          price: Number(PAYLOAD.goog_live_price),
          prev_close: Number(PAYLOAD.goog_prev_close || PAYLOAD.goog_live_price),
          change: Number(PAYLOAD.goog_daily_change || 0),
          change_pct: Number(PAYLOAD.goog_daily_change_pct || 0),
          timestamp_str: PAYLOAD.generated_at || "",
          fetched_at: Date.now()
        };
        if (!_trackedSymbolQuotes[primSym]) _trackedSymbolQuotes[primSym] = defaultQ;
      }
    }
    _seedDefaultSymbolQuotes();

    async function fetchQuoteForSymbol(sym, force = false) {
      if (!sym) return null;
      const upper = String(sym).trim().toUpperCase();
      const existing = _trackedSymbolQuotes[upper];
      if (!force && existing && existing.fetched_at && (Date.now() - existing.fetched_at < 60000)) {
        return existing;
      }
      try {
        const { ok, data } = await apiJson(`/api/quote?symbol=${encodeURIComponent(upper)}&_t=${Date.now()}`, { timeoutMs: 6000 });
        if (ok && data) {
          const px = Number(data?.price);
          if (data?.ok && Number.isFinite(px) && px > 0) {
            const prevClose = Number(data?.prev_close);
            const chg = data?.change != null ? Number(data.change) : (Number.isFinite(prevClose) ? px - prevClose : 0);
            const chgPct = data?.change_pct != null ? Number(data.change_pct) : (Number.isFinite(prevClose) && prevClose > 0 ? (chg / prevClose) * 100 : 0);
            const quoteObj = {
              price: px,
              prev_close: prevClose,
              change: chg,
              change_pct: chgPct,
              timestamp_str: data.timestamp_str || (data.market_time
                ? new Date(data.market_time * 1000).toLocaleString("en-US", { month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit" })
                : ""),
              fetched_at: Date.now()
            };
            _trackedSymbolQuotes[upper] = quoteObj;
            try { sessionStorage.setItem("tracked_symbol_quotes", JSON.stringify(_trackedSymbolQuotes)); } catch (_e) {}
            return quoteObj;
          }
        }
      } catch (_e) {
        // SA-09: never fall back to external third-party CORS proxies
      }
      return null;
    }

    // Built-in client-side mirror of `COMMON_SYMBOL_LOOKUP` in `Scripts/valuation/market_data.py`.
    // CROSS-FILE INVARIANT: Keep household-neutral (`LL-01`..`LL-33`) and never shadow listed stock tickers (`DOW`, `GOLD`, `COMP`, `BTC`, `ETH`, `SOL`).
    const SYMBOL_LOOKUP_DICT = {
      "NASDAQ": "^IXIC", "NASDAQ COMPOSITE": "^IXIC", "NASDAQ INDEX": "^IXIC", "IXIC": "^IXIC",
      "NASDAQ 100": "^NDX", "NASDAQ100": "^NDX", "NAS100": "^NDX", "NDX": "^NDX",
      "S&P": "^GSPC", "S&P 500": "^GSPC", "S&P500": "^GSPC", "SP 500": "^GSPC", "SP500": "^GSPC", "SPX": "^GSPC", "GSPC": "^GSPC",
      "DOW JONES": "^DJI", "DOW 30": "^DJI", "DJIA": "^DJI", "DJI": "^DJI", "INDU": "^DJI",
      "RUSSELL 2000": "^RUT", "RUSSELL2000": "^RUT", "RUS2000": "^RUT", "RUT": "^RUT",
      "RUSSELL 1000": "^RUI", "RUSSELL 3000": "^RUA", "WILSHIRE 5000": "^W5000", "NYSE COMPOSITE": "^NYA",
      "VIX": "^VIX", "VOLATILITY INDEX": "^VIX", "PHLX SEMICONDUCTOR": "^SOX",
      "FTSE": "^FTSE", "FTSE 100": "^FTSE", "UK100": "^FTSE",
      "DAX": "^GDAXI", "GDAXI": "^GDAXI", "GER40": "^GDAXI",
      "CAC 40": "^FCHI", "FCHI": "^FCHI",
      "EURO STOXX": "^STOXX50E", "EURO STOXX 50": "^STOXX50E", "STOXX50": "^STOXX50E", "STOXX50E": "^STOXX50E",
      "NIKKEI": "^N225", "NIKKEI 225": "^N225", "N225": "^N225",
      "HANG SENG": "^HSI", "SHANGHAI COMPOSITE": "000001.SS", "S&P TSX": "^GSPTSE", "GSPTSE": "^GSPTSE",
      "ASX 200": "^AXJO", "AXJO": "^AXJO", "KOSPI": "^KS11", "SENSEX": "^BSESN", "NIFTY 50": "^NSEI",
      "13W TREASURY": "^IRX", "3M TREASURY": "^IRX", "IRX": "^IRX",
      "5Y TREASURY": "^FVX", "US5Y": "^FVX", "FVX": "^FVX",
      "10Y TREASURY": "^TNX", "10YR TREASURY": "^TNX", "US10Y": "^TNX", "TNX": "^TNX",
      "30Y TREASURY": "^TYX", "30YR TREASURY": "^TYX", "US30Y": "^TYX", "TYX": "^TYX",
      "DXY": "DX-Y.NYB", "DOLLAR INDEX": "DX-Y.NYB", "USD INDEX": "DX-Y.NYB",
      "GOLD FUTURES": "GC=F", "GOLD SPOT": "GC=F", "XAUUSD": "GC=F",
      "SILVER FUTURES": "SI=F", "SILVER SPOT": "SI=F", "XAGUSD": "SI=F",
      "CRUDE OIL": "CL=F", "WTI": "CL=F", "WTI OIL": "CL=F", "BRENT OIL": "BZ=F",
      "NATURAL GAS": "NG=F", "NATGAS": "NG=F",
      "BITCOIN": "BTC-USD", "BTCUSD": "BTC-USD", "BTC/USD": "BTC-USD",
      "ETHEREUM": "ETH-USD", "ETHER": "ETH-USD", "ETHUSD": "ETH-USD", "ETH/USD": "ETH-USD",
      "SOLANA": "SOL-USD", "SOLUSD": "SOL-USD", "SOL/USD": "SOL-USD",
      "DOGECOIN": "DOGE-USD", "DOGEUSD": "DOGE-USD", "RIPPLE": "XRP-USD", "XRPUSD": "XRP-USD", "CARDANO": "ADA-USD",
      "BERKSHIRE": "BRK-B", "BERKSHIRE HATHAWAY": "BRK-B", "BRK/B": "BRK-B", "BRK.B": "BRK-B", "BRKB": "BRK-B",
      "BRK/A": "BRK-A", "BRK.A": "BRK-A", "BRKA": "BRK-A",
      "APPLE": "AAPL", "MICROSOFT": "MSFT", "NVIDIA": "NVDA", "AMAZON": "AMZN", "META PLATFORMS": "META",
      "TESLA": "TSLA", "BROADCOM": "AVGO", "NETFLIX": "NFLX", "COSTCO": "COST", "WALMART": "WMT",
      "ELI LILLY": "LLY", "MASTERCARD": "MA", "VISA": "V",
    };

    const SYMBOL_DISPLAY_LABELS = {
      "^IXIC": "NASDAQ Composite",
      "^NDX": "NASDAQ-100",
      "^GSPC": "S&P 500",
      "^DJI": "Dow Jones",
      "^RUT": "Russell 2000",
      "^RUI": "Russell 1000",
      "^RUA": "Russell 3000",
      "^W5000": "Wilshire 5000",
      "^NYA": "NYSE Composite",
      "^VIX": "CBOE VIX",
      "^SOX": "PHLX Semiconductor",
      "^FTSE": "FTSE 100",
      "^GDAXI": "DAX",
      "^FCHI": "CAC 40",
      "^STOXX50E": "Euro Stoxx 50",
      "^N225": "Nikkei 225",
      "^HSI": "Hang Seng",
      "^IRX": "13W T-Bill Yield",
      "^FVX": "5Y Treasury Yield",
      "^TNX": "10Y Treasury Yield",
      "^TYX": "30Y Treasury Yield",
      "DX-Y.NYB": "US Dollar Index",
      "GC=F": "Gold Futures",
      "SI=F": "Silver Futures",
      "CL=F": "WTI Crude Oil",
      "BZ=F": "Brent Crude Oil",
      "NG=F": "Natural Gas",
      "BTC-USD": "Bitcoin (USD)",
      "ETH-USD": "Ethereum (USD)",
      "SOL-USD": "Solana (USD)",
    };

    function resolveSymbolLookup(rawInput) {
      const raw = String(rawInput || "").trim().toUpperCase();
      if (!raw) return "";
      const customAliases = (PROJECT_CFG && PROJECT_CFG.ticker_aliases) || (PAYLOAD && PAYLOAD.project && PAYLOAD.project.ticker_aliases) || {};
      if (customAliases && typeof customAliases === "object") {
        for (const [k, v] of Object.entries(customAliases)) {
          if (String(k || "").trim().toUpperCase() === raw && v) {
            return String(v).trim().toUpperCase();
          }
        }
      }
      const norm = raw.replace(/[\s_-]+/g, " ").trim();
      const noAmp = norm.replace(/&/g, "").replace(/\s+/g, " ").trim();
      const compact = noAmp.replace(/\s+/g, "");
      for (const cand of [raw, norm, noAmp, compact]) {
        if (SYMBOL_LOOKUP_DICT[cand]) return SYMBOL_LOOKUP_DICT[cand];
      }
      return raw;
    }

    function formatSymbolQuotePrice(sym, px) {
      const n = Number(px) || 0;
      const isIndex = String(sym || "").startsWith("^");
      const numStr = n >= 1000
        ? n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })
        : n.toFixed(2);
      return isIndex ? numStr : `$${numStr}`;
    }

    function formatSymbolQuoteDelta(sym, chg) {
      const n = Number(chg) || 0;
      const sign = n >= 0 ? "+" : "-";
      const absVal = Math.abs(n);
      const isIndex = String(sym || "").startsWith("^");
      const numStr = absVal >= 1000
        ? absVal.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })
        : absVal.toFixed(2);
      return isIndex ? `${sign}${numStr}` : `${sign}$${numStr}`;
    }

    function renderSymbolQuoteCellHtml(sym) {
      const q = _trackedSymbolQuotes[sym];
      if (!q || !Number.isFinite(q.price) || q.price <= 0) {
        return `<span class="mono" id="popup-px-${escHtml(sym)}" style="font-size:11px;color:#64748b;">Loading...</span>`;
      }
      const chg = Number(q.change) || 0;
      const chgPct = Number(q.change_pct) || 0;
      const sign = chg >= 0 ? "+" : "";
      const chgColor = chg > 0 ? "#34d399" : (chg < 0 ? "#fb7185" : "#94a3b8");
      return `
        <div style="display:flex;flex-direction:column;align-items:flex-end;line-height:1.2;">
          <span class="mono" id="popup-px-${escHtml(sym)}" style="font-size:11.5px;font-weight:700;color:#f8fafc;">${formatSymbolQuotePrice(sym, q.price)}</span>
          <span class="mono" id="popup-chg-${escHtml(sym)}" style="font-size:9.5px;color:${chgColor};font-weight:600;">${sign}${chgPct.toFixed(2)}%</span>
        </div>`;
    }

    function getTrackedSymbols() {
      if (Array.isArray(PROJECT_CFG.symbols) && PROJECT_CFG.symbols.length > 0) {
        return PROJECT_CFG.symbols;
      }
      if (Array.isArray(PAYLOAD?.project?.symbols) && PAYLOAD.project.symbols.length > 0) {
        return PAYLOAD.project.symbols;
      }
      if (PROJECT_CFG.primary_equity_symbol) {
        return [PROJECT_CFG.primary_equity_symbol];
      }
      if (PAYLOAD?.primary_equity_symbol) {
        return [PAYLOAD.primary_equity_symbol];
      }
      if (PRIMARY_SYMBOL) {
        return [PRIMARY_SYMBOL];
      }
      return [];
    }

    let currentActiveSymbol = (function() {
      try {
        const saved = localStorage.getItem("active_symbol");
        const syms = getTrackedSymbols();
        if (saved && syms.includes(saved.toUpperCase())) return saved.toUpperCase();
        return syms[0] || "";
      } catch (_e) {
        return "";
      }
    })();

    function updateHeaderSymbolUI() {
      const symbols = getTrackedSymbols();
      const barEl = document.getElementById("nav-goog-bar");
      const btnEl = document.getElementById("nav-goog-btn");
      const hdrSymEl = document.getElementById("hdr-goog");
      const hdrChgEl = document.getElementById("hdr-goog-chg");
      const hdrTsEl = document.getElementById("hdr-goog-ts");

      if (symbols.length === 0) {
        if (barEl) {
          barEl.title = "No symbols configured. Click to configure symbols in Project Settings";
        }
        if (btnEl) {
          btnEl.title = "Configure Symbols";
          btnEl.innerHTML = `
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <line x1="12" y1="5" x2="12" y2="19"></line>
              <line x1="5" y1="12" x2="19" y2="12"></line>
            </svg>
          `;
        }
        if (hdrSymEl) hdrSymEl.textContent = "+ Add Symbol";
        if (hdrChgEl) { hdrChgEl.textContent = "No symbols tracked"; hdrChgEl.style.color = "#94a3b8"; }
        if (hdrTsEl) hdrTsEl.textContent = "Click to set up";
        closeSymbolsPopup();
        return;
      }

      if (!currentActiveSymbol || !symbols.includes(currentActiveSymbol)) {
        currentActiveSymbol = symbols[0];
      }

      if (symbols.length === 1) {
        if (barEl) {
          barEl.title = `Click to refresh live ${currentActiveSymbol} quote (Shift+Click to enter a custom price)`;
        }
        if (btnEl) {
          btnEl.title = `Refresh ${currentActiveSymbol} quote`;
          btnEl.innerHTML = `
            <svg class="nav-wrench-svg" width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M21 12a9 9 0 1 1-2.64-6.36"></path>
              <polyline points="21 3 21 9 15 9"></polyline>
            </svg>
          `;
        }
        closeSymbolsPopup();
      } else {
        // 2+ symbols: down-arrow on button
        if (barEl) {
          barEl.title = `Click to refresh live ${currentActiveSymbol} quote (Shift+Click to enter a custom price)`;
        }
        if (btnEl) {
          btnEl.title = "Select tracked symbol...";
          btnEl.innerHTML = `
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
              <polyline points="6 9 12 15 18 9"></polyline>
            </svg>
          `;
        }
        renderSymbolsPopupList();
      }
      symbols.forEach(sym => {
        fetchQuoteForSymbol(sym).then(q => {
          if (q && Number.isFinite(q.price)) {
            const pxEl = document.getElementById(`popup-px-${sym}`);
            const chgEl = document.getElementById(`popup-chg-${sym}`);
            if (pxEl) pxEl.textContent = formatSymbolQuotePrice(sym, q.price);
            if (chgEl) {
              const sign = (q.change || 0) >= 0 ? "+" : "";
              chgEl.textContent = `${sign}${(q.change_pct || 0).toFixed(2)}%`;
              chgEl.style.color = (q.change || 0) > 0 ? "#34d399" : ((q.change || 0) < 0 ? "#fb7185" : "#94a3b8");
            }
          }
        });
      });
    }

    function onHeaderSymbolBtnClick(evt) {
      const symbols = getTrackedSymbols();
      if (symbols.length === 0) {
        evt.stopPropagation();
        openProjectSettingsDialog("symbols");
        return;
      }
      if (symbols.length >= 2) {
        evt.stopPropagation();
        toggleSymbolsPopup();
        return;
      }
      // If 1 symbol, let it bubble to onHeaderSymbolClick to refresh
    }

    function onHeaderSymbolClick(evt) {
      closeSymbolsPopup();
      const symbols = getTrackedSymbols();
      if (symbols.length === 0) {
        openProjectSettingsDialog("symbols");
        return;
      }
      const targetSym = currentActiveSymbol || symbols[0] || "";
      if (evt && evt.shiftKey) {
        const manual = window.prompt(`Enter custom ${targetSym} share price ($):`, String(PAYLOAD.goog_live_price || ""));
        if (manual !== null && Number.isFinite(Number(manual)) && Number(manual) > 0) {
          const px = Number(manual);
          const tsEl = document.getElementById("hdr-goog-ts");
          if (tsEl) tsEl.textContent = "Custom Quote";
          const hdrSymEl = document.getElementById("hdr-goog");
          // SECURITY INVARIANT (`SA3-10a`): Always escape ticker symbols before `innerHTML` interpolation (`localStorage["active_symbol"]` is untrusted).
          if (hdrSymEl) hdrSymEl.innerHTML = `${escHtml(targetSym)} &bull; ${formatSymbolQuotePrice(targetSym, px)}`;
          const primSym = String(PROJECT_CFG.primary_equity_symbol || PAYLOAD.primary_equity_symbol || PRIMARY_SYMBOL || "").toUpperCase();
          if (primSym && targetSym === primSym) {
            applyLiveGoogPrice(px, "↻");
          }
        }
      } else {
        fetchLiveSymbolQuote(targetSym, true);
      }
    }

    function toggleSymbolsPopup() {
      const popup = document.getElementById("nav-symbols-popup");
      const btn = document.getElementById("nav-goog-bar") || document.getElementById("nav-goog-wrap");
      if (!popup || !btn) return;
      if (popup.style.display === "none" || !popup.style.display) {
        renderSymbolsPopupList();
        const rect = btn.getBoundingClientRect();
        popup.style.position = "fixed";
        popup.style.left = `${Math.round(rect.right + 10)}px`;
        const estHeight = Math.min(320, 50 + (getTrackedSymbols().length * 36) + 36);
        const top = Math.max(10, Math.min(window.innerHeight - estHeight - 12, rect.top));
        popup.style.top = `${Math.round(top)}px`;
        popup.style.display = "flex";
      } else {
        popup.style.display = "none";
      }
    }

    function closeSymbolsPopup() {
      const popup = document.getElementById("nav-symbols-popup");
      if (popup) popup.style.display = "none";
    }

    function selectTrackedSymbol(sym) {
      currentActiveSymbol = String(sym || "").toUpperCase();
      try { localStorage.setItem("active_symbol", currentActiveSymbol); } catch (_e) {}
      closeSymbolsPopup();
      const q = _trackedSymbolQuotes[currentActiveSymbol];
      if (q && Number.isFinite(q.price) && q.price > 0) {
        const hdrSymEl = document.getElementById("hdr-goog");
        const hdrChgEl = document.getElementById("hdr-goog-chg");
        const hdrTsEl = document.getElementById("hdr-goog-ts");
        if (hdrSymEl) hdrSymEl.innerHTML = `${escHtml(currentActiveSymbol)} &bull; ${formatSymbolQuotePrice(currentActiveSymbol, q.price)}`;
        if (hdrChgEl && q.change != null) {
          const sign = q.change >= 0 ? "+" : "";
          const pctStr = q.change_pct != null ? ` (${sign}${q.change_pct.toFixed(2)}%)` : "";
          hdrChgEl.textContent = `${formatSymbolQuoteDelta(currentActiveSymbol, q.change)}${pctStr}`;
          hdrChgEl.style.color = q.change >= 0 ? "#34d399" : "#fda4af";
        }
        if (hdrTsEl && q.timestamp_str) hdrTsEl.textContent = q.timestamp_str;
      }
      updateHeaderSymbolUI();
      fetchLiveSymbolQuote(currentActiveSymbol, true);
    }

    function renderSymbolsPopupList() {
      const popup = document.getElementById("nav-symbols-popup");
      if (!popup) return;
      const symbols = getTrackedSymbols();
      popup.innerHTML = `
        <div style="font-size:10.5px;font-weight:700;color:#64748b;padding:5px 8px 3px 8px;text-transform:uppercase;letter-spacing:0.05em;display:flex;justify-content:space-between;align-items:center;">
          <span>Tracked Symbols (${symbols.length})</span>
          <span style="font-size:10px;color:#38bdf8;cursor:pointer;" onclick="closeSymbolsPopup();openProjectSettingsDialog('symbols');">&#9881; Edit</span>
        </div>
        <div style="display:flex;flex-direction:column;gap:1px;max-height:240px;overflow-y:auto;">
          ${symbols.map((sym, idx) => `
            <div class="nav-symbol-item ${sym === currentActiveSymbol ? "active" : ""}" data-sa-action="select-tracked-symbol" data-sa-sym="${escHtml(sym)}">
              <div style="display:flex;align-items:center;gap:6px;">
                <span class="mono" style="font-weight:700;">${escHtml(sym)}</span>
                ${SYMBOL_DISPLAY_LABELS[sym] ? `<span class="muted" style="font-size:10px;">${escHtml(SYMBOL_DISPLAY_LABELS[sym])}</span>` : ""}
                ${idx === 0 ? '<span class="pill pill-blue mono" style="font-size:8.5px;padding:1px 4px;">Primary</span>' : ""}
              </div>
              <div style="display:flex;align-items:center;gap:8px;">
                ${renderSymbolQuoteCellHtml(sym)}
                ${sym === currentActiveSymbol ? '<span style="color:#38bdf8;font-size:12px;font-weight:700;">&#10003;</span>' : '<span style="display:inline-block;width:12px;"></span>'}
              </div>
            </div>
          `).join("")}
        </div>
        <div class="nav-symbols-popup-footer">
          <a onclick="closeSymbolsPopup();openProjectSettingsDialog('symbols');">&#9881; Manage Symbols...</a>
        </div>
      `;

      symbols.forEach(async sym => {
        const q = await fetchQuoteForSymbol(sym);
        const pxEl = document.getElementById(`popup-px-${sym}`);
        const chgEl = document.getElementById(`popup-chg-${sym}`);
        if (q && Number.isFinite(q.price)) {
          if (pxEl) pxEl.textContent = formatSymbolQuotePrice(sym, q.price);
          if (chgEl) {
            const sign = (q.change || 0) >= 0 ? "+" : "";
            chgEl.textContent = `${sign}${(q.change_pct || 0).toFixed(2)}%`;
            chgEl.style.color = (q.change || 0) > 0 ? "#34d399" : ((q.change || 0) < 0 ? "#fb7185" : "#94a3b8");
          }
        } else if (pxEl && pxEl.textContent === "Loading...") {
          // SECURITY DO: Fetch quotes solely via the local sidecar (`/api/quotes`) and degrade to `"sidecar offline"` when unreachable (`SA-10`).
          // SECURITY DON'T: Never call external CORS proxies (`api.allorigins.win`, `corsproxy.io`, etc.) directly from browser JS.
          pxEl.textContent = "sidecar offline";
        }
      });
    }

    function onGrantsNavClick(evt) {
      const comps = getGrantCompanies();
      if (comps.length === 0) return;
      if (comps.length >= 2 && currentScreen === "google") {
        evt.stopPropagation();
        toggleGrantsPopup();
        return;
      }
      closeGrantsPopup();
      switchScreen("google");
    }

    function onGrantsChevronClick(evt) {
      evt.stopPropagation();
      toggleGrantsPopup();
    }

    function toggleGrantsPopup() {
      const popup = document.getElementById("nav-grants-popup");
      const btn = document.getElementById("nav-btn-google");
      const comps = getGrantCompanies();
      if (!popup || !btn || comps.length < 2) return;
      if (popup.style.display === "none" || !popup.style.display) {
        renderGrantsPopupList();
        const rect = btn.getBoundingClientRect();
        popup.style.position = "fixed";
        popup.style.left = `${Math.round(rect.right + 10)}px`;
        const estHeight = Math.min(340, 44 + (comps.length * 46));
        const top = Math.max(10, Math.min(window.innerHeight - estHeight - 12, rect.top));
        popup.style.top = `${Math.round(top)}px`;
        popup.style.display = "flex";
      } else {
        popup.style.display = "none";
      }
    }

    function closeGrantsPopup() {
      const popup = document.getElementById("nav-grants-popup");
      if (popup) popup.style.display = "none";
    }

    function renderGrantsPopupList() {
      const popup = document.getElementById("nav-grants-popup");
      if (!popup) return;
      const comps = getGrantCompanies();
      popup.innerHTML = `
        <div style="font-size:10.5px;font-weight:700;color:#64748b;padding:5px 8px 3px 8px;text-transform:uppercase;letter-spacing:0.05em;">
          Stock Grant Schedules (${comps.length})
        </div>
        <div style="display:flex;flex-direction:column;gap:2px;max-height:260px;overflow-y:auto;">
          ${comps.map(c => {
            const isActive = c.symbol === activeGrantSymbol;
            const shTxt = `${Math.round(c.future_gross_shares || 0).toLocaleString("en-US")} sh`;
            const valTxt = fmtUSD0(c.future_net_val || 0);
            return `
              <div class="nav-symbol-item ${isActive ? "active" : ""}" data-sa-action="select-grant-company" data-sa-sym="${escHtml(c.symbol)}">
                <div style="min-width:0;">
                  <div style="display:flex;align-items:center;gap:6px;">
                    <span style="font-weight:700;">${escHtml(c.short_name || c.symbol)}</span>
                    <span class="pill pill-blue mono" style="font-size:9px;padding:1px 4px;">${escHtml(c.symbol)}</span>
                  </div>
                  <div class="mono muted" style="font-size:10px;margin-top:1px;">${escHtml(c.award_type || "RSU")} &bull; ${shTxt} (${valTxt} net)</div>
                </div>
                <div style="margin-left:8px;">
                  ${isActive ? '<span style="color:#38bdf8;font-size:12px;font-weight:700;">&#10003;</span>' : '<span style="display:inline-block;width:12px;"></span>'}
                </div>
              </div>
            `;
          }).join("")}
        </div>
      `;
    }

    function selectGrantCompany(sym) {
      closeGrantsPopup();
      const target = (sym || "").toUpperCase();
      try { localStorage.setItem("active_grant_symbol", target); } catch (_e) {}
      loadActiveGrantCompany(target);
      rebuildRelYearJumpButtons();
      applyRelPreset(relActivePreset || "all");
      updateGrantScreenKPIs();
      if (currentScreen !== "google") {
        switchScreen("google");
      } else {
        renderRelChartAndNav();
        renderGoogleStockTables();
        const comps = getGrantCompanies();
        const curC = comps.find(c => c.symbol === activeGrantSymbol) || comps[0];
        if (curC) {
          document.getElementById("hdr-title").textContent = comps.length >= 2
            ? `Stock Grants — ${curC.short_name || curC.symbol} (${curC.symbol})`
            : `${curC.short_name || curC.symbol} Stock & ${curC.award_type || "RSU"} Releases`;
        }
      }
    }

    window.addEventListener("click", evt => {
      const wrap = document.getElementById("nav-goog-wrap");
      const popup = document.getElementById("nav-symbols-popup");
      if (popup && popup.style.display !== "none") {
        if (!wrap?.contains(evt.target) && !popup.contains(evt.target)) {
          closeSymbolsPopup();
        }
      }
      const gBtn = document.getElementById("nav-btn-google");
      const gPopup = document.getElementById("nav-grants-popup");
      if (gPopup && gPopup.style.display !== "none") {
        if (!gBtn?.contains(evt.target) && !gPopup.contains(evt.target)) {
          closeGrantsPopup();
        }
      }
    });

    window.addEventListener("scroll", () => { closeSymbolsPopup(); closeGrantsPopup(); }, { passive: true });
    window.addEventListener("resize", () => { closeSymbolsPopup(); closeGrantsPopup(); }, { passive: true });
    const leftNavEl = document.querySelector(".left-nav");
    if (leftNavEl) leftNavEl.addEventListener("scroll", () => { closeSymbolsPopup(); closeGrantsPopup(); }, { passive: true });

    async function fetchLiveSymbolQuote(sym = null, manualTrigger = false) {
      const symbols = getTrackedSymbols();
      if (symbols.length === 0) {
        updateHeaderSymbolUI();
        return;
      }
      const targetSym = String(sym || currentActiveSymbol || symbols[0] || "").toUpperCase();
      if (!targetSym) return;
      currentActiveSymbol = targetSym;
      try { localStorage.setItem("active_symbol", targetSym); } catch (_e) {}

      const googBarEl = document.getElementById("nav-goog-bar");
      if (googBarEl) googBarEl.classList.add("running");

      const applyFetchedQuote = (px, prevClose, quoteDateOrStr, chgVal, chgPct) => {
        let dateStr = "";
        if (typeof quoteDateOrStr === "string" && quoteDateOrStr.trim()) {
          dateStr = quoteDateOrStr.trim();
        } else {
          const d = quoteDateOrStr instanceof Date ? quoteDateOrStr : new Date();
          dateStr = d.toLocaleString("en-US", {
            month: "short",
            day: "numeric",
            year: "numeric",
            hour: "numeric",
            minute: "2-digit",
          });
        }

        _trackedSymbolQuotes[targetSym] = {
          price: px,
          prev_close: prevClose,
          change: chgVal,
          change_pct: chgPct,
          timestamp_str: dateStr,
          fetched_at: Date.now()
        };
        try { sessionStorage.setItem("tracked_symbol_quotes", JSON.stringify(_trackedSymbolQuotes)); } catch (_e) {}

        const hdrSymEl = document.getElementById("hdr-goog");
        const hdrChgEl = document.getElementById("hdr-goog-chg");
        const hdrTsEl = document.getElementById("hdr-goog-ts");

        if (hdrSymEl) {
          hdrSymEl.innerHTML = `${escHtml(targetSym)} &bull; ${formatSymbolQuotePrice(targetSym, px)}`;
        }
        if (hdrChgEl && chgVal != null && Number.isFinite(chgVal)) {
          const sign = chgVal >= 0 ? "+" : "";
          const pctStr = chgPct != null ? ` (${sign}${chgPct.toFixed(2)}%)` : "";
          hdrChgEl.textContent = `${formatSymbolQuoteDelta(targetSym, chgVal)}${pctStr}`;
          hdrChgEl.style.color = chgVal >= 0 ? "#34d399" : "#fda4af";
        }
        if (hdrTsEl) {
          hdrTsEl.textContent = dateStr;
        }

        const popupPxEl = document.getElementById(`popup-px-${targetSym}`);
        if (popupPxEl) popupPxEl.textContent = `$${px.toFixed(2)}`;
        const popupChgEl = document.getElementById(`popup-chg-${targetSym}`);
        if (popupChgEl && chgPct != null) {
          const sign = (chgVal || 0) >= 0 ? "+" : "";
          popupChgEl.textContent = `${sign}${Number(chgPct).toFixed(2)}%`;
          popupChgEl.style.color = (chgVal || 0) > 0 ? "#34d399" : ((chgVal || 0) < 0 ? "#fb7185" : "#94a3b8");
        }

        const primSym = String(PROJECT_CFG.primary_equity_symbol || PAYLOAD.primary_equity_symbol || PRIMARY_SYMBOL || "").toUpperCase();
        if (primSym && targetSym === primSym) {
          if (Number.isFinite(prevClose) && prevClose > 10) {
            PAYLOAD.goog_prev_close = prevClose;
          }
          currentGoogQuoteTs = dateStr;
          applyLiveGoogPrice(px, "↻");
        }
      };

      try {
        const q = await fetchQuoteForSymbol(targetSym, true);
        if (q && Number.isFinite(q.price) && q.price > 0) {
          applyFetchedQuote(q.price, q.prev_close, q.timestamp_str || new Date(), q.change, q.change_pct);
        }
      } finally {
        if (googBarEl) googBarEl.classList.remove("running");
      }
    }

    async function fetchLiveGoogQuote(manualTrigger = false) {
      return fetchLiveSymbolQuote(currentActiveSymbol || PRIMARY_SYMBOL || "", manualTrigger);
    }

