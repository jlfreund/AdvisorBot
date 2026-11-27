// Playwright render check (CodeReview + Phase D RSI.html):
// Loads http://127.0.0.1:8765/ in headless Chrome, visits all 5 screens (`#scenarios`, `#portfolio`,
// `#google`, `#retirement`, `#income`), exercises Scenario Detail View, Forecast Capsule modal,
// Action Wizard modal, Hypothetical Account modal, Burn slider + Auto-Optimizer, and Right AI
// Advisor Drawer, failing on any console/page error or non-favicon HTTP >= 400.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { chromium } from 'playwright';

// Supply credentials explicitly for the synthetic sidecar being tested.
const target = new URL(process.argv[2] || 'http://127.0.0.1:8765/');
if (target.protocol !== 'http:' || !['127.0.0.1', 'localhost'].includes(target.hostname)
    || target.username || target.password || target.search || target.hash || target.pathname !== '/') {
  throw new Error('Expected the root URL of a loopback fixture server.');
}
const token = process.env.ADVISORBOT_SESSION_TOKEN;
if (!token || process.env.ADVISORBOT_FIXTURE_ORIGIN !== target.origin) {
  throw new Error('Set ADVISORBOT_FIXTURE_ORIGIN and ADVISORBOT_SESSION_TOKEN for the test instance.');
}
const OUTDIR = process.argv[3] || path.join(os.tmpdir(), 'advisorbot_render_check');
fs.mkdirSync(OUTDIR, { recursive: true });
const errors = [];
const browser = await chromium.launch({ channel: 'chrome', headless: true });
const page = await browser.newPage({ viewport: { width: 1440, height: 1020 } });

page.on('console', m => {
  if (m.type() === 'error' && !/Failed to load resource/.test(m.text())) {
    errors.push('console.error: ' + m.text());
  }
});
page.on('pageerror', e => errors.push('pageerror: ' + ((e && e.message) || e)));
page.on('response', r => {
  if (r.status() >= 400 && !/favicon|\.well-known/.test(r.url())) {
    errors.push('http ' + r.status() + ': ' + r.url());
  }
});
page.on('requestfailed', r => {
  const u = r.url();
  if (!/favicon|\.well-known/.test(u)) {
    errors.push('requestfailed: ' + u + ' ' + (r.failure()?.errorText || ''));
  }
});

await page.route('**/*', async route => {
  const requestUrl = new URL(route.request().url());
  if (requestUrl.origin !== target.origin) return route.abort();
  try {
    const response = await route.fetch({ maxRedirects: 0, headers: { ...route.request().headers(), 'X-Session-Token': token } });
    await route.fulfill({ response });
  } catch {
    if (!page.isClosed()) {
      errors.push('Fixture request interrupted: ' + requestUrl.pathname);
      await route.abort().catch(() => {});
    }
  }
});
await page.goto(target.href, { waitUntil: 'networkidle', timeout: 30000 });
await page.waitForTimeout(600);

// Reapplying a strategy holding's preferences must only rename its matching simulator asset.
await page.evaluate(() => {
  const group = PAYLOAD.holdings_by_strategy.find(g => {
    const asset = BASE_SIM_ASSETS.find(a => a.id === g.forecast_key);
    return asset?.account_id && g.holding_key.includes(asset.account_id) &&
      BASE_SIM_ASSETS.some(a => a !== asset && a.account_id === asset.account_id);
  });
  if (!group) return;
  const titles = BASE_SIM_ASSETS.map(a => a.title);
  applyHoldingFriendlyNameEverywhere(group.holding_key, 'Fixture renamed holding', group.statement_name);
  BASE_SIM_ASSETS.forEach((asset, i) => {
    const expected = asset.id === group.forecast_key ? 'Fixture renamed holding' : titles[i];
    if (asset.title !== expected) throw new Error('Friendly-name update targeted the wrong simulator holding.');
  });
});
await page.reload({waitUntil: 'networkidle'});

// A half-life-only edit follows the historical reference across a scenario round trip.
await page.evaluate(async () => {
  const initial = getCurrentSettingsSnapshot();
  const a = SIM_ASSETS.find(a => holdingFadeTarget(a) != null && a.default_growth_pct !== 0 &&
    a.default_growth_pct !== holdingFadeTarget(a));
  const key = a.forecast_key || a.id;
  openFriendlyNameDialog(key);
  const slider = document.getElementById('fn-half-life');
  if (slider.value !== '5' || slider.min !== '1' || slider.max !== '31' ||
      document.getElementById('fn-half-life-control').hidden) throw Error('Missing half-life control');
  const chart = document.getElementById('fn-curve-chart');
  const curve = () => chart.querySelector('polyline')?.getAttribute('points');
  const originalCurve = curve();
  const evidence = holdingGrowthEvidence(window._activeFnHolding);
  const used = document.querySelectorAll('#fn-tier-table .growth-tier-used');
  if (used.length !== 1 || parseFloat(used[0].querySelector('.growth-rate').textContent) !== holdingReferenceGrowth(a) ||
      document.querySelectorAll('#fn-tier-table tbody tr').length !== 3) throw Error('Reference table does not match Hist. Ref');
  if (chart.hidden || originalCurve?.split(' ').length !== 40 ||
      chart.querySelectorAll('.fn-tier-line').length !== new Set(evidence.map(e => e.tier)).size) throw Error('Incorrect 40-year curve or guides');
  const meta = document.querySelector('.friendly-name-dialog .fn-info-card').getBoundingClientRect();
  const fields = document.getElementById('fn-input').closest('.save-input-box').getBoundingClientRect();
  const forecast = document.querySelector('.fn-forecast-card').getBoundingClientRect();
  if (Math.abs(meta.top - fields.top) > 2 || forecast.top < Math.max(meta.bottom, fields.bottom) ||
      forecast.width < fields.right - meta.left - 2) throw Error('Holding Details card layout is incorrect');
  const rateHdr = document.querySelector('#fn-tier-table thead th.growth-rate');
  const rateHdrRect = rateHdr.getBoundingClientRect();
  if (getComputedStyle(rateHdr).textAlign !== 'right' || rateHdr.scrollWidth > rateHdr.clientWidth + 1 ||
      rateHdrRect.right > forecast.right - 8)
    throw Error('Annual rate header overflows column or purple forecast card');
  slider.value = '31';
  slider.dispatchEvent(new Event('input', {bubbles: true}));
  if (document.getElementById('fn-half-life-years').textContent !== 'Flat (no decay)' ||
      !document.getElementById('fn-return-path').textContent.includes('Flat mode holds'))
    throw Error('Flat (no decay) slider stop did not update label/description');
  slider.value = '12';
  slider.dispatchEvent(new Event('input', {bubbles: true}));
  if (!document.getElementById('fn-return-path').textContent.includes('Year 13')) throw Error('Half-life preview did not update');
  if (curve() === originalCurve) throw Error('Half-life slider did not redraw the curve');
  await saveFriendlyName();
  const snapshot = getCurrentSettingsSnapshot();
  applySettingsSnapshot(initial);
  applySettingsSnapshot(snapshot);
  if (holdingHalfLife(a) !== 12) throw Error('Lost saved half-life');
  if (JSON.stringify(snapshot.holdingForecasts) !== JSON.stringify(initial.holdingForecasts)) throw Error('Half-life edit changed holding rates');
  const fc = resolveSimAssetForecast(a);
  const rates = buildYearRateAdjuster()(computeAssetMonthlyRates(), 12);
  const annual = ((1 + rates[a.id]) ** 12 - 1) * 100 + fc.expense_ratio_pct;
  const expected = holdingReturnCurve(a, fc.base_growth_pct, 12)(12) * fc.growth_multiplier;
  if (Math.abs(annual - expected) > 1e-7) throw Error('Simulation diverged from the half-life preview');
  const unavailable = {default_risk_class:'broad_equity', growth_evidence:[
    {tier:3, projected_return_pct:8, source_detail:'Risk class'}]};
  const originalHolding = window._activeFnHolding;
  for (const onlyTier of [1, 3]) {
    unavailable.growth_evidence[0].tier = onlyTier;
    window._activeFnHolding = unavailable;
    syncFnDialogForecastUI();
    if (!chart.hidden || chart.childElementCount || !document.getElementById('fn-half-life-control').hidden)
      throw Error('Single-tier holding retained projection controls');
  }
  window._activeFnHolding = originalHolding;
  applySettingsSnapshot(initial);
});

// Exercise both table Reset controls against every row, including expanded holdings.
async function auditForecastRows(page) {
  return await page.evaluate(() => {
    const initial = getCurrentSettingsSnapshot();
    const counts = {};
    const variable = SIM_ASSETS.find(a => !a.is_hypothetical && a.rate_model !== 'fixed' &&
      a.return_source !== 'CONTRACT_CREDITED_RATE' && a.default_risk_class !== 'cash_fixed');
    if (!variable) throw Error('Multiplier regression needs a variable-rate holding');
    const key = variable.forecast_key || variable.id;
    holdingForecasts = {[key]: {growth_pct: 2.75, risk_class: variable.default_risk_class}};
    const slider = document.getElementById('sim-optimism-slider');
    slider.value = '0.45';
    slider.dispatchEvent(new Event('input', {bubbles:true}));
    renderSimLiquidationTable();
    const tuned = document.querySelector('#sim-liq-tbody .mdl-in-capsule[data-sa-key="' + CSS.escape(key) + '"]');
    if (tuned?.innerText.trim() !== '+2.75%' || !tuned.dataset.tip.includes('Holding projected growth · 2.75% / year') ||
        !tuned.dataset.tip.includes('2.75% × 0.45 = 1.24%') || holdingForecasts[key].growth_pct !== 2.75)
      throw Error('Simulation multiplier changed the individual growth display or setting');
    const annualNetPct = (Math.pow(1 + computeAssetMonthlyRates()[variable.id], 12) - 1) * 100;
    const fee = resolveSimAssetForecast(variable).expense_ratio_pct;
    if (Math.abs(annualNetPct - (2.75 * 0.45 - fee)) > 1e-8) throw Error('Monthly simulation used the wrong return');

    for (const mode of ['strategy', 'account', 'simulator']) {
      const sim = mode === 'simulator';
      holdingForecasts = Object.fromEntries(BASE_SIM_ASSETS.map(a => [a.forecast_key || a.id, {growth_pct: -13}]));
      if (!sim) { setTableGrouping(mode); expandCollapseAll(true); }
      document.getElementById(sim ? 'sim-all-reset-btn' : 'tbl-all-reset-btn').click();
      const items = sim ? SIM_ASSETS : activeGroupsList().flatMap(g => [g, ...(g.children || [])]);
      const byKey = new Map(items.map(a => [sim ? a.forecast_key || a.id : a.holding_key || a.group_id, a]));
      const cells = document.querySelectorAll((sim ? '#sim-liq-tbody' : '#holdings-tbody') + ' .mdl-in-capsule[data-sa-key]');
      counts[mode] = cells.length;
      if (items.length !== cells.length) throw Error(mode + ' missing holding rows: ' + cells.length + '/' + items.length);
      const table = document.createElement('template');
      for (const cell of cells) {
        const key = cell.dataset.saKey;
        const item = byKey.get(key);
        if (!item) throw Error(mode + ' missing row identity');
        table.innerHTML = sanitizeTooltipHtml(cell.dataset.tip);
        const rows = [...table.content.querySelectorAll('.growth-tiers tbody tr')];
        const highest = rows.find(r => r.querySelector('.growth-rate')?.textContent !== '—');
        const selected = rows.filter(r => r.classList.contains('growth-tier-used'));
        if (rows.map(r => r.querySelector('th').textContent).join() !== '1,2,3' ||
            !highest || selected.length !== 1 || highest !== selected[0]) throw Error(mode + ' incorrect tier selection: ' + key);
        for (const [i, row] of rows.entries()) {
          const source = item.growth_evidence?.find(e => e.tier === i + 1);
          const rateText = row.querySelector('.growth-rate').textContent;
          if (source && parseFloat(rateText) !== source.projected_return_pct) throw Error(mode + ' missing evidence: ' + key);
        }
        const ref = parseFloat(highest.querySelector('.growth-rate').textContent);
        const displayed = parseFloat(cell.querySelector('.mdl-in-capsule-txt').textContent);
        const heading = 'Holding projected growth · ' + ref.toFixed(2) + '% / year';
        if (displayed !== ref || !table.content.textContent.includes(heading) ||
            !table.content.querySelector('caption').textContent.includes('Ref ' + ref.toFixed(2) + '%'))
          throw Error(mode + ' Reset diverged from the highlighted tier: ' + key + ' (' + displayed + ' versus ' + ref + ')');
        if (!item.is_hypothetical) {
          const h = findHoldingByKey(key);
          if (!h || holdingReferenceGrowth(h) !== ref) throw Error(mode + ' Holding Details opened another reference: ' + key);
        }
      }
    }
    setTableGrouping("strategy");
    expandCollapseAll(false);
    applySettingsSnapshot(initial);
    return counts;
  });
}
console.log("Forecast rows checked", await auditForecastRows(page));

const screens = [
  ['models', 'nav-btn-models'],
  ['portfolio', 'nav-btn-portfolio'],
  ['google', 'nav-btn-google'],
  ['retirement', 'nav-btn-retirement'],
  ['income', 'nav-btn-income'],
];
for (const [name, btnId] of screens) {
  const btn = await page.$('#' + btnId);
  if (btn) { await btn.click(); await page.waitForTimeout(450); }
  await page.screenshot({ path: `${OUTDIR}/screen_${name}.png` });
}

// 1. Test Scenarios Card Grid + Detail View
await page.click('#nav-btn-models');
await page.waitForTimeout(350);
const modelCardsCount = await page.locator('#models-cards-container .model-card').count();
if (modelCardsCount > 0) {
  await page.locator('#models-cards-container .model-card').first().click();
  await page.waitForTimeout(300);
  await page.screenshot({ path: `${OUTDIR}/screen_models_detail.png` });
  await page.evaluate(() => closeModelDetail());
  await page.waitForTimeout(200);
}

// Test Scenario Reload Animation (Cmd/Ctrl+R)
const reloadPromise = page.evaluate(() => reloadCurrentScenario());
await page.waitForTimeout(100);
const reloadingState = await page.evaluate(() => {
  const btn = document.getElementById('nav-btn-models');
  const prog = document.getElementById('nav-models-progress');
  const sub = document.getElementById('save-status-sub');
  return {
    isReloadingClass: btn ? btn.classList.contains('is-reloading') : false,
    hasProgressEl: !!prog,
    subText: sub ? sub.textContent : ''
  };
});
await reloadPromise;
await page.waitForTimeout(600);
const completedState = await page.evaluate(() => {
  const btn = document.getElementById('nav-btn-models');
  const sub = document.getElementById('save-status-sub');
  return {
    isReloadingClass: btn ? btn.classList.contains('is-reloading') : false,
    subText: sub ? sub.textContent : ''
  };
});
if (!reloadingState.hasProgressEl) {
  errors.push('Missing #nav-models-progress element');
}
if (!completedState.subText.includes('Reloaded') && !completedState.subText.includes('Saved')) {
  errors.push('Unexpected post-reload status text: ' + completedState.subText);
}

// 2. Test Screen 1 Per-Holding Forecast Capsule -> 2-column Holding Forecast Modal + Immutable Badge + Volatility Slider + Tooltips
await page.click('#nav-btn-portfolio');
await page.waitForTimeout(350);
await page.evaluate(() => {
  if (typeof setHoldingsSummaryDrawerExpanded === 'function') setHoldingsSummaryDrawerExpanded(true);
});
await page.waitForTimeout(200);
async function checkGrowthHover(locator) {
  await locator.hover();
  const table = page.locator('#ui-tooltip table.growth-tiers');
  const tiers = await table.locator('tbody tr > th').allTextContents();
  const used = table.locator('tbody tr.growth-tier-used .growth-rate');
  if (tiers.join(',') !== '1,2,3' || await used.count() !== 1 ||
      !(await used.innerText()).includes('Used') || !/\d{4}-\d{2}-\d{2}/.test(await table.innerText())) {
    errors.push('Projected Growth tooltip is missing its three tiers, dated evidence or selected rate.');
  }
}
const firstCapsule = page.locator('#holdings-tbody .mdl-in-capsule[data-tip*="growth-tier-used"]').first();
if (await firstCapsule.count() > 0) {
  await checkGrowthHover(firstCapsule);
  const selectedRate = parseFloat(await page.locator('#ui-tooltip .growth-tier-used .growth-rate').innerText());
  await firstCapsule.click();
  await page.waitForTimeout(250);
  await checkGrowthHover(page.locator('#fn-dlg-growth-num'));
  const refButton = page.locator('#fn-dlg-ref-btn');
  await checkGrowthHover(refButton);
  if (!(await refButton.innerText()).includes(selectedRate.toFixed(2) + '%') ||
      !(await page.locator('#ui-tooltip caption').innerText()).includes('Ref ' + selectedRate.toFixed(2) + '%')) {
    errors.push('Holding Details Ref disagrees with the tier selected in Current Vested Holdings.');
  }
  await refButton.click();
  if (Number(await page.locator('#fn-dlg-growth-num').inputValue()) !== selectedRate) {
    errors.push('Ref changed the selected tier rate when copying it to the scenario.');
  }
  const fnCheck = await page.evaluate(() => {
    const badge = document.getElementById('fn-dlg-risk-badge');
    const volSl = document.getElementById('fn-dlg-vol-slider');
    const volNum = document.getElementById('fn-dlg-vol-num');
    const sum = document.getElementById('fn-dlg-risk-summary');
    const prov = document.getElementById('fn-dlg-prov-box');
    const cagrTip = document.querySelector('[data-tip*="CAGR"]');
    const volTip = document.querySelector('[data-tip*="Annual Volatility"]');
    const revertBtn = document.getElementById('fn-revert-btn');
    const saveBtn = document.getElementById('fn-save-btn');
    const beforeTxt = volNum ? volNum.value : '';
    if (volNum) {
      volNum.value = '22.5';
      volNum.dispatchEvent(new Event('input', { bubbles: true }));
    }
    const afterTxt = volNum ? volNum.value : '';
    return {
      hasBadge: !!badge && badge.textContent.trim().length > 0,
      hasVolControls: !!volSl && !!volNum,
      hasProv: !!prov,
      hasCagrTip: !!cagrTip,
      hasVolTip: !!volTip,
      volChanged: beforeTxt !== afterTxt && afterTxt === '22.5',
      revertBtnText: revertBtn ? revertBtn.textContent.trim() : '',
      saveBtnText: saveBtn ? saveBtn.textContent.trim() : '',
      hasRevertTip: !!(revertBtn && revertBtn.getAttribute('data-tip') && revertBtn.getAttribute('data-tip').includes('Baseline Defaults'))
    };
  });
  if (!fnCheck.hasBadge || !fnCheck.hasVolControls || !fnCheck.hasCagrTip || !fnCheck.hasVolTip || fnCheck.revertBtnText !== 'Reset All' || fnCheck.saveBtnText !== 'Save' || !fnCheck.hasRevertTip) {
    errors.push('holding forecast modal controls or tooltips failed check: ' + JSON.stringify(fnCheck));
  }
  await page.screenshot({ path: `${OUTDIR}/modal_holding_forecast.png` });
  await page.evaluate(() => closeFriendlyNameDialog());
  await page.waitForTimeout(150);

  // Test opening dialog via the .btn-icon-edit button in Current Vested Holdings
  const firstEditBtn = page.locator('#screen-portfolio .btn-icon-edit').first();
  if (await firstEditBtn.count() > 0) {
    await firstEditBtn.click();
    await page.waitForTimeout(250);
    const isOpen = await page.evaluate(() => {
      const bd = document.getElementById('friendly-name-dialog-backdrop');
      return bd && bd.classList.contains('open');
    });
    if (!isOpen) {
      errors.push('Failed to open holding details dialog by clicking .btn-icon-edit');
    }
    await page.evaluate(() => closeFriendlyNameDialog());
    await page.waitForTimeout(150);
  }
}

// 3. Test Screen 3 Retirement Simulator: Action Wizard + Hypothetical Modal + Slider + Auto-Optimizer
await page.click('#nav-btn-retirement');
await page.waitForTimeout(350);
await page.evaluate(() => openSimWithdrawalDialog('create'));
await page.waitForTimeout(200);
await page.screenshot({ path: `${OUTDIR}/modal_action_wizard.png` });
await page.evaluate(() => closeSimWithdrawalDialog());

await page.evaluate(() => openHypotheticalAssetDialog(null, false));
await page.waitForTimeout(200);
const hypCheck = await page.evaluate(() => {
  const sel = document.getElementById('hyp-risk-select');
  const sum = document.getElementById('hyp-risk-summary');
  const unl = document.getElementById('hyp-unlock-input');
  const unlSub = document.getElementById('hyp-unlock-sub');
  const unlLbl = document.querySelector('label[for="hyp-unlock-input"]');
  const nameLbl = document.querySelector('label[for="hyp-label-input"]');
  const optCount = sel ? sel.options.length : 0;
  if (sel) {
    sel.value = 'real_diversifier';
    sel.dispatchEvent(new Event('change', { bubbles: true }));
  }
  const sumTxt = sum ? sum.textContent : '';
  const defaultSub = unlSub ? unlSub.textContent.trim() : '';
  const placeholder = unl ? unl.getAttribute('placeholder') : '';
  const labelTxt = unlLbl ? unlLbl.textContent.trim() : '';
  const nameLblTxt = nameLbl ? nameLbl.textContent.trim() : '';
  let illegalSub = '', pastSub = '', pastCommittedFromEmpty = '', futureSub = '', pastRevertedVal = '', pastRevertedSub = '', clearedVal = '', clearedSub = '';
  if (unl && unlSub) {
    unl.value = '13-2030';
    unl.dispatchEvent(new Event('input', { bubbles: true }));
    illegalSub = unlSub.textContent.trim();

    // Past date committed when previous value was empty -> clears to ""
    unl.value = '05-2020';
    unl.dispatchEvent(new Event('input', { bubbles: true }));
    pastSub = unlSub.textContent.trim();
    unl.dispatchEvent(new Event('blur'));
    pastCommittedFromEmpty = unl.value;

    // Valid future date committed -> stored as previous value ("11-2035")
    unl.value = '11-2035';
    unl.dispatchEvent(new Event('input', { bubbles: true }));
    futureSub = unlSub.textContent.trim();
    unl.dispatchEvent(new Event('blur'));

    // Past date committed after "11-2035" -> resets back to "11-2035"
    unl.value = '05-2020';
    unl.dispatchEvent(new Event('input', { bubbles: true }));
    unl.dispatchEvent(new Event('blur'));
    pastRevertedVal = unl.value;
    pastRevertedSub = unlSub.textContent.trim();

    // Explicit clear -> clears to ""
    unl.value = '';
    unl.dispatchEvent(new Event('input', { bubbles: true }));
    unl.dispatchEvent(new Event('blur'));
    clearedVal = unl.value;
    clearedSub = unlSub.textContent.trim();
  }
  return {
    optCount, sumTxt, labelTxt, nameLblTxt, placeholder, defaultSub,
    illegalSub, pastSub, pastCommittedFromEmpty, futureSub,
    pastRevertedVal, pastRevertedSub, clearedVal, clearedSub,
  };
});
if (
  hypCheck.optCount !== 7 ||
  !hypCheck.sumTxt.includes('15.0%') ||
  !hypCheck.nameLblTxt.includes('(Required)') ||
  !hypCheck.labelTxt.includes('Maturity Date') ||
  hypCheck.placeholder !== 'MM-YYYY' ||
  hypCheck.defaultSub !== 'None (Liquid)' ||
  hypCheck.illegalSub !== 'None (Liquid)' ||
  hypCheck.pastSub !== 'None (Liquid)' ||
  hypCheck.pastCommittedFromEmpty !== '' ||
  hypCheck.futureSub !== 'Locked until 11-2035' ||
  hypCheck.pastRevertedVal !== '11-2035' ||
  hypCheck.pastRevertedSub !== 'Locked until 11-2035' ||
  hypCheck.clearedVal !== '' ||
  hypCheck.clearedSub !== 'None (Liquid)'
) {
  errors.push('hyp-dialog failed check: ' + JSON.stringify(hypCheck));
}
await page.screenshot({ path: `${OUTDIR}/modal_hypothetical_asset.png` });
await page.evaluate(() => closeHypotheticalAssetDialog());

// Test opening dialog via the .btn-icon-edit pencil button in Asset Liquidation Priority table
await page.evaluate(() => {
  if (typeof setSimLiqDrawerExpanded === 'function') setSimLiqDrawerExpanded(true);
});
await page.waitForTimeout(200);
const firstLiqEditBtn = page.locator('#sim-liq-tbody .btn-icon-edit').first();
if (await firstLiqEditBtn.count() === 0) {
  errors.push('Missing .btn-icon-edit pencil button in #sim-liq-tbody');
} else {
  await checkGrowthHover(page.locator('#sim-liq-tbody .mdl-in-capsule[data-tip*="growth-tier-used"]').first());
  await firstLiqEditBtn.click();
  await page.waitForTimeout(250);
  const liqEditOpened = await page.evaluate(() => {
    const fnBd = document.getElementById('friendly-name-dialog-backdrop');
    const hypBd = document.getElementById('sim-hyp-dialog-backdrop');
    return Boolean((fnBd && fnBd.classList.contains('open')) || (hypBd && hypBd.classList.contains('open')));
  });
  if (!liqEditOpened) {
    errors.push('Failed to open edit dialog by clicking .btn-icon-edit in #sim-liq-tbody');
  }
  await page.evaluate(() => {
    closeFriendlyNameDialog();
    closeHypotheticalAssetDialog();
  });
  await page.waitForTimeout(150);
}

// Test Hypothetical Delete button ghosting + hover tooltip when referenced by Scheduled Events
const hypDeleteGuardCheck = await page.evaluate(() => {
  openHypotheticalAssetDialog(null, false);
  const lbl = document.getElementById('hyp-label-input');
  if (lbl) lbl.value = 'Temp Guard Check ETF';
  saveHypotheticalAssetDialog();
  const createdId = 'hyp_temp_guard_check_etf';

  // 1. With 0 referencing events -> Delete button enabled, no blocking tooltip
  openHypotheticalAssetDialog(createdId, false);
  const delBtn = document.getElementById('hyp-delete-btn');
  const delWrap = document.getElementById('hyp-delete-wrap');
  const unblockedDisabled = delBtn ? delBtn.disabled : null;
  const unblockedTip = delWrap ? (delWrap.getAttribute('data-tip') || '') : '';
  closeHypotheticalAssetDialog();

  // 2. Add a Scheduled Deposit referencing createdId
  openSimWithdrawalDialog('create');
  setSimActionWizardType('deposit');
  const toSel = document.getElementById('sim-act-to-select');
  if (toSel) toSel.value = createdId;
  const amtEl = document.getElementById('sim-sw-amount-input');
  if (amtEl) amtEl.value = '50000';
  const cmtEl = document.getElementById('sim-sw-comment-input');
  if (cmtEl) cmtEl.value = 'Seed Temp Guard ETF';
  submitSimWithdrawalDialog();

  // Reopen hypothetical dialog -> Delete button must be ghosted + tooltip lists the event
  openHypotheticalAssetDialog(createdId, false);
  const blockedDisabled = delBtn ? delBtn.disabled : null;
  const blockedTip = delWrap ? (delWrap.getAttribute('data-tip') || '') : '';
  // Programmatic delete attempt must also be blocked while referenced
  deleteCurrentHypotheticalAsset();
  const stillExistsWhileBlocked = document.getElementById('sim-hyp-dialog-backdrop')?.classList.contains('open') || false;
  return { unblockedDisabled, unblockedTip, blockedDisabled, blockedTip, stillExistsWhileBlocked, createdId };
});
await page.hover('#hyp-delete-wrap', { timeout: 3000 });
await page.waitForTimeout(120);
const hypHoverTipVisible = await page.evaluate(() => {
  const tip = document.getElementById('ui-tooltip');
  return Boolean(tip && tip.style.display !== 'none' && tip.textContent.includes('Seed Temp Guard ETF'));
});
const hypCleanupCheck = await page.evaluate((createdId) => {
  closeHypotheticalAssetDialog();
  // Remove the scheduled event referencing createdId
  const idx = (typeof simScheduledActions !== 'undefined' ? simScheduledActions : []).findIndex(a => a.to_asset_id === createdId);
  if (idx >= 0) {
    selectSimWithdrawalRow(idx);
    openSimWithdrawalDialog('delete');
    submitSimWithdrawalDialog();
  }
  openHypotheticalAssetDialog(createdId, false);
  const delBtn = document.getElementById('hyp-delete-btn');
  const reEnabled = delBtn ? !delBtn.disabled : false;
  deleteCurrentHypotheticalAsset();
  const deletedCleanly = !(typeof simHypotheticalAssets !== 'undefined' && simHypotheticalAssets.some(h => h.id === createdId));
  return { reEnabled, deletedCleanly };
}, hypDeleteGuardCheck.createdId);
if (
  hypDeleteGuardCheck.unblockedDisabled !== false ||
  hypDeleteGuardCheck.unblockedTip !== '' ||
  hypDeleteGuardCheck.blockedDisabled !== true ||
  !hypDeleteGuardCheck.blockedTip.includes('Cannot Delete') ||
  !hypDeleteGuardCheck.blockedTip.includes('Seed Temp Guard ETF') ||
  !hypDeleteGuardCheck.stillExistsWhileBlocked ||
  !hypHoverTipVisible ||
  !hypCleanupCheck.reEnabled ||
  !hypCleanupCheck.deletedCleanly
) {
  errors.push('hyp-delete-guard failed check: ' + JSON.stringify({ hypDeleteGuardCheck, hypHoverTipVisible, hypCleanupCheck }));
}

const burnSlider = await page.$('#sim-burn-slider');
if (burnSlider) {
  await burnSlider.evaluate(el => {
    el.value = '480000';
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
  });
  await page.waitForTimeout(500);
}
const optBtn = await page.$('#sim-order-auto-btn');
if (optBtn) { await optBtn.click(); await page.waitForTimeout(1500); }
await page.evaluate(() => {
  if (typeof showSimTooltip === 'function') {
    showSimTooltip({ clientX: 620, clientY: 420 }, 180);
  }
});
await page.waitForTimeout(150);
await page.screenshot({ path: `${OUTDIR}/sim_tooltip.png` });
await page.evaluate(() => { if (typeof hideTooltip === 'function') hideTooltip(); });

// Test progressive Monte-Carlo / Historical refinement + animated control border interruption & completion
const progAnimCheck = await page.evaluate(() => {
  // 1. Click "Historical" -> button immediately selected + animating border (.sim-computing)
  setSimModel('historical');
  const histBtn = document.getElementById('sim-tab-btn-historical');
  const rangeBtn = document.getElementById('sim-tab-btn-range');
  const optPill = document.getElementById('sim-optimism-pill');
  const histActiveImmediate = histBtn ? histBtn.classList.contains('active') : false;
  const histAnimImmediate = histBtn ? histBtn.classList.contains('sim-computing') : false;
  const simsSliderGone = !document.getElementById('sim-mc-sims-slider') && !document.getElementById('sim-mc-sims-row');

  // 2. Interrupt immediately by clicking "Monte-Carlo" -> Historical animation stops, Monte-Carlo animates
  setSimModel('range');
  const histStoppedOnSwitch = histBtn ? !histBtn.classList.contains('sim-computing') : false;
  const rangeAnimOnSwitch = rangeBtn ? rangeBtn.classList.contains('sim-computing') : false;
  const mcNarrText = document.getElementById('sim-mc-narrative')?.textContent || '';

  // 3. Interrupt immediately by dragging Projected Growth Multiplier slider -> Monte-Carlo button stops, right capsule animates
  const tDrag0 = performance.now();
  onSimOptimismInput(1.15);
  const syncInputMs = Math.round((performance.now() - tDrag0) * 10) / 10;
  const rangeStoppedOnSlider = rangeBtn ? !rangeBtn.classList.contains('sim-computing') : false;
  const pillAnimOnSlider = optPill ? optPill.classList.contains('sim-computing') : false;
  const surfaceIds = ['sim-kpi-card-inheritance', 'sim-kpi-card-solvency', 'sim-kpi-card-taxdrag', 'sim-main-panel'];
  const surfacesAnimOnSlider = surfaceIds.every(id => document.getElementById(id)?.classList.contains('sim-surface-computing'));

  return {
    histActiveImmediate,
    histAnimImmediate,
    simsSliderGone,
    mcNarrHas500: mcNarrText.includes('500'),
    histStoppedOnSwitch,
    rangeAnimOnSwitch,
    syncInputMs,
    rangeStoppedOnSlider,
    pillAnimOnSlider,
    surfacesAnimOnSlider,
  };
});
// Wait up to 8s for progressive 500-path Monte Carlo refinement to complete and stop animating the capsule + KPI/chart surfaces
let pillStoppedAfterDone = false;
let lastPoll = {};
for (let w = 0; w < 80; w++) {
  await page.waitForTimeout(100);
  lastPoll = await page.evaluate(() => {
    const optPill = document.getElementById('sim-optimism-pill');
    const st = document.getElementById('sim-mc-status');
    const surfaceIds = ['sim-kpi-card-inheritance', 'sim-kpi-card-solvency', 'sim-kpi-card-taxdrag', 'sim-main-panel'];
    const anySurfaceAnimating = surfaceIds.some(id => document.getElementById(id)?.classList.contains('sim-surface-computing'));
    return {
      animating: optPill ? optPill.classList.contains('sim-computing') : true,
      anySurfaceAnimating,
      status: st ? st.textContent.trim() : '',
    };
  });
  if (!lastPoll.animating && !lastPoll.anySurfaceAnimating && lastPoll.status.includes('paths') && !lastPoll.status.includes('Refining') && !lastPoll.status.includes('Computing')) {
    pillStoppedAfterDone = true;
    break;
  }
}
if (
  !progAnimCheck.histActiveImmediate ||
  !progAnimCheck.histAnimImmediate ||
  !progAnimCheck.simsSliderGone ||
  !progAnimCheck.mcNarrHas500 ||
  !progAnimCheck.histStoppedOnSwitch ||
  !progAnimCheck.rangeAnimOnSwitch ||
  progAnimCheck.syncInputMs > 100 ||
  !progAnimCheck.rangeStoppedOnSlider ||
  !progAnimCheck.pillAnimOnSlider ||
  !progAnimCheck.surfacesAnimOnSlider ||
  !pillStoppedAfterDone
) {
  errors.push('progressive-sim-anim failed check: ' + JSON.stringify({ progAnimCheck, pillStoppedAfterDone, lastPoll }));
}

// 4. Test Right AI Advisor Drawer + Project Settings Tab 5 (Advisor Prompts)
await page.evaluate(() => toggleAiChatPane(true));
await page.waitForTimeout(300);
await page.screenshot({ path: `${OUTDIR}/drawer_ai_advisor.png` });
await page.evaluate(() => {
  const origStatus = PAYLOAD.privacy_status ? Object.assign({}, PAYLOAD.privacy_status) : null;
  // In Demo Mode, AI Chat must NEVER display the Mode C banner or any numeric scale factor
  updatePrivacyChromeBadges({ demo_enabled: true, shield_mode: 'identity_and_values' });
  const demoNote = document.getElementById('ai-chat-mode-c-note');
  if (demoNote && (demoNote.style.display !== 'none' || demoNote.textContent.trim() !== '')) {
    throw new Error(`Demo Mode leaked Mode C chat banner: "${demoNote.textContent}"`);
  }
  appendAiMessage('assistant', 'Demo check reply', [], [], { privacy_shield: 'identity_and_values', demo_mode: true });
  if (document.querySelector('#ai-chat-messages .ai-reply-shield-meta')) {
    throw new Error('Demo Mode leaked .ai-reply-shield-meta on assistant reply');
  }
  document.getElementById('ai-chat-messages').innerHTML = '';
  // Outside Demo Mode, Mode C banner must never print a numeric multiplier like "0.25x"
  updatePrivacyChromeBadges({ demo_enabled: false, shield_mode: 'identity_and_values' });
  if (demoNote && /\d+(\.\d+)?x/i.test(demoNote.textContent || '')) {
    throw new Error(`Mode C chat banner leaked numeric scale multiplier: "${demoNote.textContent}"`);
  }
  updatePrivacyChromeBadges(origStatus || { demo_enabled: false, shield_mode: 'identity_aliased' });
});
await page.evaluate(() => toggleAiChatPane(false));

await page.evaluate(async () => {
  await openProjectSettingsDialog('info');
});
await page.waitForTimeout(350);
await page.screenshot({ path: `${OUTDIR}/modal_project_info_collapsible.png` });

// Verify Portfolio & Family Members collapsible rows, inline summaries, status capsules, and mid-edit open-row stability
await page.evaluate(async () => {
  const portfolioRowIds = ['proj-sec-identity', 'proj-burn-box', 'proj-tax-box', 'proj-sec-graphic', 'proj-statements-section'];
  for (const id of portfolioRowIds) {
    const row = document.getElementById(id);
    if (!row || !row.classList.contains('proj-collapsible-row')) {
      throw new Error(`Missing .proj-collapsible-row for #${id}`);
    }
    const hdr = row.querySelector('.proj-collapse-hdr[data-proj-collapse-toggle]');
    const arrow = row.querySelector('.proj-collapse-arrow');
    const title = row.querySelector('.proj-collapse-title');
    const summary = row.querySelector('.proj-collapse-summary');
    const pill = row.querySelector('.proj-collapse-status-pill');
    if (!hdr || !arrow || !title || !summary || !pill) {
      throw new Error(`Incomplete collapsible header anatomy in #${id}`);
    }
    const pillTxt = pill.textContent.trim();
    if (!['Incomplete', 'Needs Review', '✓ Completed'].includes(pillTxt)) {
      throw new Error(`Unexpected status capsule text in #${id}: "${pillTxt}"`);
    }
    const summaryTxt = summary.textContent.trim();
    if (!summaryTxt) {
      throw new Error(`Empty inline summary in #${id}`);
    }
  }

  // Completed rows should auto-collapse on open; clicking header toggles .expanded; mid-edit changes update capsule without auto-collapsing
  const burnRow = document.getElementById('proj-burn-box');
  const burnHdr = burnRow.querySelector('.proj-collapse-hdr');
  const burnPill = document.getElementById('proj-sec-burn-pill');
  const burnInp = document.getElementById('proj-burn-input');
  const origBurnVal = burnInp.value;
  if (!origBurnVal || !origBurnVal.startsWith('$') || burnPill.textContent.trim() !== '✓ Completed' || burnRow.classList.contains('expanded')) {
    throw new Error(`Expected configured Annual Burn Rate to open as ✓ Completed & collapsed with $ value, got value="${origBurnVal}", pill="${burnPill.textContent.trim()}", expanded=${burnRow.classList.contains('expanded')}`);
  }
  const wasExpanded = burnRow.classList.contains('expanded');
  burnHdr.click();
  if (burnRow.classList.contains('expanded') === wasExpanded) {
    throw new Error('Clicking #proj-burn-box header did not toggle .expanded');
  }
  if (!burnRow.classList.contains('expanded')) {
    burnHdr.click();
  }
  // Mid-edit clear -> Incomplete (stays expanded), then restore -> Completed (stays expanded!)
  burnInp.value = '';
  onProjectBurnTextInput('');
  if (burnPill.textContent.trim() !== 'Incomplete' || !burnRow.classList.contains('expanded')) {
    throw new Error('Clearing burn input did not mark #proj-burn-box Incomplete while keeping row expanded');
  }
  burnInp.value = origBurnVal;
  onProjectBurnTextInput(burnInp.value);
  if (burnPill.textContent.trim() !== '✓ Completed' || !burnRow.classList.contains('expanded')) {
    throw new Error('Restoring burn input did not mark #proj-burn-box Completed or unexpectedly collapsed open row mid-edit');
  }
  if (_projAutoSaveTimer) {
    clearTimeout(_projAutoSaveTimer);
    _projAutoSaveTimer = null;
  }

  // Verify Site Graphic (#proj-sec-graphic) is nested inside Portfolio Identity (#proj-sec-identity) with a synced header thumbnail
  const idSec = document.getElementById('proj-sec-identity');
  const graphSec = document.getElementById('proj-sec-graphic');
  const hdrThumb = document.getElementById('proj-badge-hdr-thumb');
  const fullPreview = document.getElementById('proj-badge-preview');
  if (!idSec || !graphSec || !idSec.querySelector(':scope > .proj-collapse-body #proj-sec-graphic')) {
    throw new Error('Expected #proj-sec-graphic to be nested inside #proj-sec-identity > .proj-collapse-body');
  }
  if (!hdrThumb || !graphSec.querySelector(':scope > .proj-collapse-hdr #proj-badge-hdr-thumb') || !hdrThumb.getAttribute('src')) {
    throw new Error('Missing or empty #proj-badge-hdr-thumb inside #proj-sec-graphic header');
  }
  if (hdrThumb.getAttribute('src') !== fullPreview.getAttribute('src')) {
    throw new Error(`Header thumbnail src (${hdrThumb.getAttribute('src')}) out of sync with preview src (${fullPreview.getAttribute('src')})`);
  }
  if (!idSec.classList.contains('expanded')) {
    idSec.querySelector(':scope > .proj-collapse-hdr').click();
  }
  if (!idSec.classList.contains('expanded') || graphSec.classList.contains('expanded')) {
    throw new Error('Expanding #proj-sec-identity should keep nested #proj-sec-graphic collapsed initially');
  }
  graphSec.querySelector(':scope > .proj-collapse-hdr').click();
  if (!graphSec.classList.contains('expanded') || !idSec.classList.contains('expanded')) {
    throw new Error('Clicking nested #proj-sec-graphic header should expand badge editor while keeping #proj-sec-identity open');
  }

  // Verify Tax Jurisdiction labels have plain-English tooltips and #ui-tooltip sits above #project-settings-backdrop
  for (const forId of ['proj-tax-state', 'proj-tax-ltcg', 'proj-tax-ordinary', 'proj-tax-ira', 'proj-tax-floor-select']) {
    const lbl = document.querySelector(`label[for="${forId}"]`);
    const tip = lbl ? (lbl.getAttribute('data-tip') || '').trim() : '';
    if (!lbl || tip.length < 40) {
      throw new Error(`Missing or empty data-tip on Portfolio Settings tax label for="${forId}"`);
    }
  }
  const modalZ = parseInt(getComputedStyle(document.getElementById('project-settings-backdrop')).zIndex || '0', 10);
  const tipZ = parseInt(getComputedStyle(document.getElementById('ui-tooltip')).zIndex || '0', 10);
  if (!(tipZ > modalZ)) {
    throw new Error(`#ui-tooltip z-index (${tipZ}) must exceed #project-settings-backdrop z-index (${modalZ})`);
  }

  // Switch to Family Members tab and verify roster capsules + collapsible detail sections
  switchProjectSettingsTab('family');
  const famRosterPills = document.querySelectorAll('#proj-family-list .proj-collapse-status-pill');
  if (famRosterPills.length < 1) {
    throw new Error('Missing status capsule on Family Members top roster rows');
  }
  const famIdentity = document.getElementById('proj-fam-sec-identity');
  const famSsn = document.getElementById('proj-fam-sec-ssn');
  if (!famIdentity || !famSsn) {
    throw new Error('Missing collapsible sections (#proj-fam-sec-identity / #proj-fam-sec-ssn) in Family Members detail view');
  }
  const famWork = document.getElementById('proj-fam-sec-work');
  const famRows = [famIdentity, ...(famWork ? [famWork] : []), famSsn];
  for (const r of famRows) {
    const pTxt = (r.querySelector('.proj-collapse-status-pill') || {}).textContent?.trim() || '';
    const sTxt = (r.querySelector('.proj-collapse-summary') || {}).textContent?.trim() || '';
    if (!['Incomplete', 'Needs Review', '✓ Completed'].includes(pTxt) || !sTxt) {
      throw new Error(`Invalid Family Members collapsible header in #${r.id}: pill="${pTxt}" summary="${sTxt}"`);
    }
    const isCompleted = pTxt === '✓ Completed';
    if (isCompleted && r.classList.contains('expanded')) {
      throw new Error(`Completed Family section #${r.id} should be collapsed by default`);
    }
    if (!isCompleted && !r.classList.contains('expanded')) {
      throw new Error(`Non-completed Family section #${r.id} (${pTxt}) should auto-expand by default`);
    }
  }
  const idWasOpen = famIdentity.classList.contains('expanded');
  famIdentity.querySelector('.proj-collapse-hdr').click();
  if (famIdentity.classList.contains('expanded') === idWasOpen) {
    throw new Error('Clicking #proj-fam-sec-identity header did not toggle .expanded');
  }
  const msTipEl = document.querySelector('label[for="proj-cur-fam-ms"] .has-tip');
  const msTipTxt = msTipEl ? (msTipEl.getAttribute('data-tip') || '') : '';
  if (!msTipEl || !msTipEl.querySelector('.info-dot') || !msTipTxt.includes('Headline KPIs') || !msTipTxt.includes('Stop-Working Ceiling')) {
    throw new Error('Missing rich Milestone Age simulation tooltip or .info-dot on label[for="proj-cur-fam-ms"]');
  }

  // Verify Work & Pre-Retirement Plan dynamic narrative toggles cleanly between Working and Not Working
  if (famWork) {
    if (!famWork.classList.contains('expanded')) {
      famWork.querySelector(':scope > .proj-collapse-hdr').click();
    }
    const origWpJson = JSON.stringify(_projDraft.family_members[_selectedFamilyIdx]?.work_plan || {});
    const origSchedSave = _scheduleProjectAutoSave;
    _scheduleProjectAutoSave = () => {};
    try {
      document.querySelector('#proj-fam-sec-work [data-wp-action="set-working"][data-wp-val="1"]').click();
      const narWorking = (document.getElementById('proj-fam-work-narrative')?.textContent || '').trim();
      if (!narWorking.includes('Monthly earnings income can optionally be distributed')) {
        throw new Error(`Unexpected Working narrative: "${narWorking}"`);
      }
      const retTipEl = document.querySelector('label[for="proj-cur-fam-ret-age"] .has-tip');
      const retTipTxt = retTipEl ? (retTipEl.getAttribute('data-tip') || '') : '';
      if (!retTipEl || !retTipEl.textContent.includes('Target retirement age') || !retTipEl.querySelector('.info-dot') || !retTipTxt.includes('Not Working')) {
        throw new Error('Missing Target retirement age label or tooltip on label[for="proj-cur-fam-ret-age"]');
      }

      // Verify Step 1 ($1..$1M+ type-in > $1M), Step 2 household budget contribution, and Retirement Savings bar visibility & breakdown
      const thInp = document.getElementById('proj-wp-takehome-input');
      const bInp = document.getElementById('proj-wp-budget-input');
      if (!thInp || !bInp) {
        throw new Error('Missing #proj-wp-takehome-input or #proj-wp-budget-input in Working UI');
      }
      thInp.value = '1500000';
      thInp.dispatchEvent(new Event('input', { bubbles: true }));
      if (_projDraft.family_members[_selectedFamilyIdx].work_plan.annual_take_home !== 1500000) {
        throw new Error('Type-in above $1M failed on #proj-wp-takehome-input');
      }
      // Allocate 100% of paycheck -> Retirement Savings bar hides when no contributions exist
      document.getElementById('proj-wp-full-paycheck-btn').click();
      const retRowFull = document.getElementById('proj-wp-ret-savings-row');
      if (!retRowFull || retRowFull.style.display !== 'none') {
        throw new Error('Expected #proj-wp-ret-savings-row to hide when 100% of paycheck goes to household budget');
      }
      // Allocate $120,000 of $200,000 paycheck -> $80,000 remaining -> Retirement Savings bar appears and toggles open
      thInp.value = '200000';
      thInp.dispatchEvent(new Event('input', { bubbles: true }));
      bInp.value = '120000';
      bInp.dispatchEvent(new Event('input', { bubbles: true }));
      const retRowRem = document.getElementById('proj-wp-ret-savings-row');
      if (!retRowRem || retRowRem.style.display === 'none') {
        throw new Error('Expected #proj-wp-ret-savings-row to display when paycheck remains');
      }
      const bdTxt = retRowRem.textContent || '';
      if (!bdTxt.includes('Total Paycheck:') || !bdTxt.includes('House Contribution:') || !bdTxt.includes('Retirement Contributions:') || !bdTxt.includes('Remaining:')) {
        throw new Error(`Missing breakdown fields in Retirement Savings bar: "${bdTxt}"`);
      }
      const wasRetOpen = retRowRem.classList.contains('expanded');
      retRowRem.querySelector('[data-wp-action="toggle-ret-savings"]').click();
      if (retRowRem.classList.contains('expanded') === wasRetOpen) {
        throw new Error('Clicking Retirement Savings header did not toggle .expanded');
      }

      document.querySelector('#proj-fam-sec-work [data-wp-action="set-working"][data-wp-val="0"]').click();
      const narNotWorking = (document.getElementById('proj-fam-work-narrative')?.textContent || '').trim();
      if (!narNotWorking.includes('does not model any paycheck income or contributions to the portfolio')) {
        throw new Error(`Unexpected Not Working narrative: "${narNotWorking}"`);
      }
    } finally {
      _scheduleProjectAutoSave = origSchedSave;
      _projDraft.family_members[_selectedFamilyIdx].work_plan = JSON.parse(origWpJson);
      renderProjectFamilyMembers();
    }
  }
});
await page.screenshot({ path: `${OUTDIR}/modal_project_family_collapsible.png` });

await page.evaluate(() => {
  if (typeof switchProjectSettingsTab === 'function') switchProjectSettingsTab('prompts');
});
await page.waitForTimeout(250);
await page.screenshot({ path: `${OUTDIR}/modal_project_prompts.png` });
await page.evaluate(() => closeProjectSettingsDialog());

// A failed save followed by a background library refresh must retain the open draft.
await page.evaluate(async () => {
  const original = getCurrentSettingsSnapshot();
  const previousFetch = apiJson;
  const previousConfirm = window.confirm;
  try {
    loadedSettingsSnapshot = original;
    simAnnualBurn = Number(original.simAnnualBurn) + 1234;
    checkSaveDirtyState();
    const draft = getCurrentSettingsSnapshot();
    if (!isSaveDirty) throw new Error('Fixture draft did not become dirty');
    window.confirm = () => true;
    apiJson = async (path, opts) => path === '/api/models' && opts?.body
      ? {ok:false, status:0, error:'Fixture disconnected'} : previousFetch(path, opts);
    await saveToCurrentFile();
    if (!isSaveDirty || !document.getElementById('save-status-sub').textContent.includes('Retry Save')) {
      throw new Error('Missing failed-save feedback');
    }
    apiJson = previousFetch;
    await applyLiveUiEvent({type:'sync_saves', id:currentModelId});
    if (!isSaveDirty || !snapshotsEqual(getCurrentSettingsSnapshot(), draft)) {
      throw new Error('Background sync replaced the unsaved draft');
    }
  } finally {
    apiJson = previousFetch;
    window.confirm = previousConfirm;
    applySettingsSnapshot(original);
  }
});

await page.evaluate(() => {
  const snapBefore = getCurrentSettingsSnapshot();
  try {
    window.setSimConfigDrawerExpanded(true);
    window.setSimCorrectionDrawerExpanded(true);
    window.setStressScenario('dotcom2000');
    const dotcomPill = (document.getElementById('sim-stress-shock-pill') || {}).textContent || '';
    const dotcomDown = (document.getElementById('sim-stress-down-pill') || {}).textContent || '';
    const dotcomBroad = (document.getElementById('sim-stress-broad-bounce-pill') || {}).textContent || '';
    const dotcomStock = (document.getElementById('sim-stress-stock-bounce-pill') || {}).textContent || '';
    if (dotcomPill !== '-75%' || dotcomDown !== '2.5 yr' || dotcomBroad !== '4 yr' || dotcomStock !== 'No Bounce') {
      throw new Error(`Dot-com preset pills mismatch: ${dotcomPill} / ${dotcomDown} / ${dotcomBroad} / ${dotcomStock}`);
    }
    ['sim-stress-shock-pill', 'sim-stress-down-pill', 'sim-stress-broad-bounce-pill', 'sim-stress-stock-bounce-pill'].forEach(pid => {
      const tipHtml = (document.getElementById(pid) || {}).getAttribute ? document.getElementById(pid).getAttribute('data-tip') || '' : '';
      if (!tipHtml.includes('stress-pill-tip') || !tipHtml.includes('Holdings')) {
        throw new Error(`Missing rich holdings tooltip on #${pid}`);
      }
    });
    window.setStressScenario('rates2022');
    const r22Pill = (document.getElementById('sim-stress-shock-pill') || {}).textContent || '';
    const r22Down = (document.getElementById('sim-stress-down-pill') || {}).textContent || '';
    if (r22Pill !== '-38%' || r22Down !== '1 yr') {
      throw new Error(`2022 Rate Shock preset pills mismatch: ${r22Pill} / ${r22Down}`);
    }
    window.onStressDownMonthsInput(18);
    const customActive = document.querySelector('[data-stress="custom"]');
    if (!customActive || !customActive.classList.contains('active')) {
      throw new Error('Dragging Downfall Duration slider from preset did not switch Scenario to Custom');
    }
    if (((document.getElementById('sim-stress-shock-pill') || {}).textContent || '') !== '-38%') {
      throw new Error('Dragging Downfall Duration did not preserve -38% preset Downfall Size');
    }
    window.setSimOverridesDrawerExpanded(true);
    simInvestor1RetirementAge = 63;
    window.syncSimRetirementAgeControls();
    const retSlBefore = document.querySelector('#sim-retirement-age-sliders-wrap [data-ret-slider-idx]');
    if (!retSlBefore) {
      throw new Error('Expected retirement age override slider to render in #sim-retirement-age-sliders-wrap');
    }
    const wrapTxt = (document.getElementById('sim-retirement-age-sliders-wrap') || {}).textContent || '';
    if (!wrapTxt.includes("'s Retirement Age:") || wrapTxt.includes('Stop-Working Age')) {
      throw new Error(`Expected "<Name>'s Retirement Age:" label in simulator overrides, got: ${wrapTxt}`);
    }
    const rIdx = retSlBefore.getAttribute('data-ret-slider-idx');
    const minA = Number(retSlBefore.min || 18);
    const maxA = Number(retSlBefore.max || 85);
    const nextA = Math.min(maxA, Math.max(minA, (Number(retSlBefore.value) || minA) === minA ? minA + 1 : Number(retSlBefore.value) - 1));
    retSlBefore.value = String(nextA);
    retSlBefore.dispatchEvent(new Event('input', { bubbles: true }));
    const retSlAfter = document.querySelector(`#sim-retirement-age-sliders-wrap [data-ret-slider-idx="${rIdx}"]`);
    if (retSlAfter !== retSlBefore) {
      throw new Error('Dragging retirement age slider recreated the <input type="range"> element instead of updating in place');
    }
    const pillAfter = document.getElementById(`sim-ret-pill-${rIdx}`);
    if (!pillAfter || (!pillAfter.textContent.includes(String(nextA)) && !pillAfter.textContent.includes('Stop Now'))) {
      throw new Error(`Retirement age pill did not update in place: ${pillAfter ? pillAfter.textContent : 'missing'}`);
    }
  } finally {
    applySettingsSnapshot(snapBefore);
    recomputeSimBars();
    renderSimChartAndNav();
    updateSimSummaryDOM();
    checkSaveDirtyState();
  }
});

const checks = await page.evaluate(() => ({
  modelCardsCount: document.querySelectorAll('#models-cards-container .model-card').length,
  kpiTotal: (document.getElementById('kpi-total') || {}).textContent || '',
  simKpi: (document.getElementById('sim-kpi-inheritance') || {}).textContent || '',
  simSolvency: (document.getElementById('sim-kpi-solvency') || {}).textContent || '',
  simTax: (document.getElementById('sim-kpi-taxdrag') || {}).textContent || '',
  simRows: document.querySelectorAll('#sim-liq-tbody tr').length,
  capsulesCount: document.querySelectorAll('.mdl-in-capsule').length,
  goldKpiCardsCount: document.querySelectorAll('.mdl-kpi-card').length,
}));

// Optional two-household check for the isolated release walkthrough.
const otherFixture = process.env.ADVISORBOT_SECOND_FIXTURE;
if (otherFixture) {
  const scope = () => page.locator('meta[name="advisorbot-portfolio"]').getAttribute('content');
  const fixtureHeaders = { 'X-Session-Token': token };
  const selectFixture = id => page.request.post(new URL('/api/portfolios/select', target).href, {
    headers: fixtureHeaders, data: { portfolio_id: id }, maxRedirects: 0,
  });
  const original = await page.evaluate(() => PAYLOAD.active_portfolio_id);
  const priorScope = await scope();
  if (!original || original === otherFixture) throw new Error('Expected two separate fixture portfolios');
  try {
    const change = await selectFixture(otherFixture);
    if (!change.ok()) throw new Error('Unable to select the second fixture');
    const oldTab = await page.request.get(new URL('/api/project', target).href, {
      headers: { ...fixtureHeaders, 'X-AdvisorBot-Portfolio': priorScope }, maxRedirects: 0,
    });
    if (oldTab.status() !== 409) throw new Error('The old dashboard scope was accepted');
    await page.reload({ waitUntil: 'networkidle' });
    const actual = await page.evaluate(() => PAYLOAD.active_portfolio_id);
    if (actual !== otherFixture || await scope() === priorScope) throw new Error('Reload returned the wrong household');
  } finally {
    await page.goto('about:blank');
    await selectFixture(original);
  }
}
await page.unrouteAll({ behavior: 'wait' });
await browser.close();
console.log('CHECKS ' + JSON.stringify({ ...checks, initialModelCards: modelCardsCount }));
if (errors.length) {
  console.log('ERRORS(' + errors.length + '):');
  errors.slice(0, 20).forEach(e => console.log('  ' + e));
  process.exit(1);
}
console.log('RENDER OK — zero console/page/HTTP errors across all 5 screens + modals + drawer + optimizer');
