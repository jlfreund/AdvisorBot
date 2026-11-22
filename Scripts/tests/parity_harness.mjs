import fs from 'node:fs';
import vm from 'node:vm';

const html = fs.readFileSync(process.argv[2] || 'index.html', 'utf8');
// Extract the LAST <script>...</script> (the app script; PAYLOAD + engine live here)
const m = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)];
const script = m[m.length - 1][1];

// Universal DOM stub: a callable Proxy that returns itself for any access.
const mk = () => new Proxy(function(){}, {
  get: (t,p) => (p === Symbol.toPrimitive ? (()=>0) : (p==='length'?0:(p==='value'?'':(p==='classList'?mk():(p==='style'?mk():(p==='dataset'?mk():stub)))))),
  apply: () => stub, set: () => true, has: () => true,
});
const stub = mk();
const doc = { getElementById:()=>stub, querySelector:()=>stub, querySelectorAll:()=>[], createElement:()=>stub, createElementNS:()=>stub, addEventListener:()=>{}, body:stub, documentElement:stub };
const win = { addEventListener:()=>{}, location:{hash:'',href:''}, matchMedia:()=>({matches:false,addEventListener:()=>{}}), devicePixelRatio:1, getComputedStyle:()=>stub };
const ctx = {
  document:doc, window:win, navigator:{userAgent:'node'}, console,
  localStorage:{getItem:()=>null,setItem:()=>{},removeItem:()=>{}},
  setInterval:()=>0, clearInterval:()=>{}, setTimeout:(f)=>0, clearTimeout:()=>{}, requestAnimationFrame:()=>0,
  fetch:()=>Promise.resolve({ok:false,json:async()=>({})}),
  Date, Math, JSON, parseInt, parseFloat, isNaN, isFinite, Array, Object, String, Number, Boolean, Map, Set, RegExp, Error, Symbol,
};
ctx.globalThis = ctx; ctx.window = ctx; ctx.self = ctx;
ctx.addEventListener=()=>{}; ctx.removeEventListener=()=>{};
ctx.location={hash:'',href:'',search:''}; ctx.matchMedia=()=>({matches:false,addEventListener:()=>{}});
ctx.getComputedStyle=()=>stub; ctx.devicePixelRatio=1; ctx.innerWidth=1200; ctx.innerHeight=800;
ctx.URL=URL; ctx.Promise=Promise; ctx.structuredClone=structuredClone;
ctx.AbortController=AbortController; ctx.TextEncoder=TextEncoder; ctx.TextDecoder=TextDecoder;

const controller = `
globalThis.__applyCfg = function(cfg){
  applySettingsSnapshot(cfg);
  if (Array.isArray(cfg.simAssetOrder) && cfg.simAssetOrder.length > 0) {
    simAssetOrder = cfg.simAssetOrder.slice();
  }
  recomputeSsnBars();
  recomputeSimBars();
};
globalThis.__run = function(cfg){
  globalThis.__applyCfg(cfg);
  const inv0Age = Number(INV0.milestone_age) || 85;
  const msBar = SIM_BARS.find((b,i)=>ageOnIso(INV0.birth_year,INV0.birth_month,INV0.birth_day,b.iso,i)>=inv0Age) || SIM_BARS[SIM_BARS.length-1];
  const y40 = SIM_BARS[SIM_BARS.length-1];
  return { ms_nominal: Math.round(msBar.totalVal*100)/100, ms_real: Math.round(msBar.totalVal/msBar.deflator*100)/100,
           ms_cumTax: Math.round(msBar.cumTax*100)/100, y40_nominal: Math.round(y40.totalVal*100)/100,
           depleted: simSolvencyDepleted ? simSolvencyDepleted.monthIdx : null,
           kpi_summary: computeCurrentKpiSummary() };
};
globalThis.__optimize = function(){ return findAutoOptimizedOrder(); };
globalThis.__cohorts = function(cfg){
  globalThis.__applyCfg(cfg);
  return runHistoricalCohorts(cfg.__opts || {});
};
globalThis.__releaseExamples = function(){
  const balances = [0, 1, 2, 3].map(i => ({
    id: "account" + i, account_id: "account" + i, rmd_owner_index: i === 1 ? 1 : 0,
    tax_wrapper: i < 2 ? "TRAD_IRA" : "401K", owner_birth_year: 1953,
    tax_model: "ORDINARY_FULL", tax_rate: 0, current_value: 26500, cost_basis: 0,
  }));
  SIM_ASSETS = balances;
  simScheduledActions = [];
  const state = cloneSimAssetState();
  delete state._taxCfg;
  const order = balances.map(a => a.id);
  const rates = Object.fromEntries(order.map(id => [id, .1]));
  const month = stepSimMonth(state, "2026-01-31", "Jan", .1, .1, 4000, true, order, rates);
  const actions = normalizeSimScheduledActions([
    {type:"deposit", ym:"2023-01", to_id:"cash", amount:10000},
    {cadence:"quarterly", start_ym:"2023-02", amount:100}, {amount:250},
  ]);
  const taxCfg = {...buildTaxScheduleConfig(), is_mfj:true, floor_key:"none", state_rate:0};
  return {
    balances: order.map(id => state[id].val), rmd: month.monthlyRmdGross,
    lot: ["2024-02-29","2025-02-28","2025-03-01"].map(iso => isShortTermLot({vestIso:"2024-02-29"},iso)),
    dates: actions.map(a => a.start_ym),
    quarters: ["2026-12","2027-02"].map(ym => actionTriggersInYm(actions[1], ym)),
    tax: [32200,57000,133000].map(gross => taxOrdinaryIncrement({_taxCfg:taxCfg},gross,"2026-01-01",1,false).tax),
  };
};
globalThis.__monteCarlo = function(cfg){
  globalThis.__applyCfg(cfg);
  return runMonteCarlo(cfg.__mc || {}, cfg.__seed == null ? 1 : cfg.__seed);
};
`;
vm.createContext(ctx);
try {
  new vm.Script(script + controller).runInContext(ctx);
} catch (e) {
  if (typeof ctx.__run !== 'function') { console.error('SCRIPT_EVAL_ERROR:', e.message); process.exit(3); }
}

const cfgRaw = fs.readFileSync(process.argv[3], 'utf8');
const cfg = JSON.parse(cfgRaw);
const mode = process.argv[4] || 'run';
const outPath = process.argv[5] || '';

let outObj;
if (mode === 'release_examples') {
  outObj = ctx.__releaseExamples();
} else if (mode === 'optimize') {
  ctx.__applyCfg(cfg);
  outObj = ctx.__optimize();
} else if (mode === 'cohorts') {
  outObj = ctx.__cohorts(cfg);
} else if (mode === 'montecarlo') {
  outObj = ctx.__monteCarlo(cfg);
} else {
  outObj = ctx.__run(cfg);
}

const payloadStr = JSON.stringify(outObj);
if (outPath) {
  fs.writeFileSync(outPath, payloadStr, 'utf8');
} else {
  fs.writeSync(1, payloadStr + '\n');
}
