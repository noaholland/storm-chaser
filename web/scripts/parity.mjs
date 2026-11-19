// Parity check for the static demo: the values the browser serves must equal what the Python engine produces.
//
//   node scripts/parity.mjs <dir with tapes> <dir with expected output>
//
// The expected output is written by scripts/parity_expected.py straight from the Python stack (the FastAPI app
// and a tick-by-tick engine run), independently of the tapes. This script answers the same questions from the
// tapes with the code the site ships (src/static/) and compares every value.
// Run through `make parity`, which builds both sides first.
import { createReadStream, readFileSync } from "node:fs";
import { createInterface } from "node:readline";
import path from "node:path";
import { StaticBackend, frame, regimeReturns, seenAt } from "../src/static/backend.ts";
import { fanSteps, simulate } from "../src/static/risk.ts";

const [tapeDir, expDir] = process.argv.slice(2);
if (!tapeDir || !expDir) {
  console.error("usage: node scripts/parity.mjs <tapes dir> <expected dir>");
  process.exit(2);
}
const readJson = (dir, name) => JSON.parse(readFileSync(path.join(dir, name), "utf8"));
const fresh = () => new StaticBackend(async (name) => readJson(tapeDir, `${name}.json`));

// Wall-clock fields: the two sides ran at different times, so they cannot be equal and are not compared.
const WALL = new Set(["feed.age_s", "session.started", "uptime_s", "wall_time"]);
// Not part of the Python payload / only on one side by design.
const SKIP = new Set(["static", "notifiers", "ui"]);

let checked = 0;
const failures = [];
const fail = (msg) => failures.length < 40 && failures.push(msg);

/** Deep compare. tol = 0: numbers must be identical doubles. tol > 0: relative tolerance (libm differences). */
function same(a, b, where, tol = 0, skip = WALL, at = "") {
  if (a === null || b === null || typeof a !== "object" || typeof b !== "object") {
    checked++;
    if (a === b) return;
    if (tol > 0 && typeof a === "number" && typeof b === "number" && Math.abs(a - b) <= tol * Math.max(Math.abs(a), Math.abs(b), 1e-300)) return;
    return fail(`${where}: site ${JSON.stringify(a)} != python ${JSON.stringify(b)}`);
  }
  if (Array.isArray(a) !== Array.isArray(b)) return fail(`${where}: array vs object`);
  if (Array.isArray(a)) {
    if (a.length !== b.length) return fail(`${where}: length ${a.length} != ${b.length}`);
    for (let i = 0; i < a.length; i++) same(a[i], b[i], `${where}[${i}]`, tol, skip, at);
    return;
  }
  for (const k of new Set([...Object.keys(a), ...Object.keys(b)])) {
    const p = at ? `${at}.${k}` : k;
    if (skip.has(p) || skip.has(k) || SKIP.has(k)) continue;
    if (!(k in a)) fail(`${where}.${k}: missing on the site`);
    else if (!(k in b)) fail(`${where}.${k}: not in the python output`);
    else same(a[k], b[k], `${where}.${k}`, tol, skip, p);
  }
}

const summary = [];

for (const dataset of readJson(expDir, "datasets.json")) {
  const exp = readJson(expDir, `${dataset}.api.json`);
  const before = checked;

  // ---- 1. every tick: the recorded frame == the engine's snapshot at that tick (flowing and frozen)
  const tape = readJson(tapeDir, `${dataset}.json`);
  let ticks = 0;
  for await (const line of createInterface({ input: createReadStream(path.join(expDir, `${dataset}.ticks.jsonl`)) })) {
    const row = JSON.parse(line);
    const skip = new Set(["feed.age_s", "feed.stale_after_s", "session"]);
    same(frame(tape, row.i, false), row.run, `${dataset}#${row.i} run:`, 0, skip);
    same(frame(tape, row.i, true), row.paused, `${dataset}#${row.i} paused:`, 0, skip);
    // the running lists the session reveals tick by tick: level changes, chart points, alerts, the regime window
    const seen = seenAt(tape, row.i);
    const rets = regimeReturns(tape, row.i);
    let sum = 0;
    for (const r of rets) sum += r;
    same(
      { transitions: seen.transitions, history: seen.history, alerts: seen.alerts, suppressed: seen.suppressed, returns_len: rets.length, returns_sum: sum, returns_last: rets.length ? rets[rets.length - 1] : null },
      row.seen,
      `${dataset}#${row.i} seen:`,
    );
    ticks++;
  }
  if (ticks !== tape.n) fail(`${dataset}: ${ticks} reference ticks, tape has ${tape.n}`);
  const perTick = checked - before;

  // ---- 2. the API, call for call, against the real FastAPI app
  const b = fresh();
  same(await b.datasets().then((c) => c.datasets[dataset]), exp.catalogue_entry, `${dataset} /datasets:`);
  same((await b.datasets()).scoring, exp.scoring, `${dataset} scoring:`);
  await b.replay(dataset, "max", "tornado");
  const stages = [["frozen", exp.frozen]];
  for (const [name, want] of stages) {
    same(await b.score(), want.score, `${dataset} ${name} /score:`);
    same(await b.history(null, 1200), want.history, `${dataset} ${name} /history:`);
    same(await b.history(null, 50), want.history_50, `${dataset} ${name} /history n=50:`);
    same(await b.alerts(), want.alerts, `${dataset} ${name} /alerts:`);
    same(await b.health().then((h) => h.sessions), want.health.sessions, `${dataset} ${name} /health:`);
    // exposure is recomputed in the browser: identical up to the last bits of exp()/expm1()
    for (let k = 0; k < want.exposure.length; k++) {
      const { request, response } = want.exposure[k];
      same(await b.exposure(request), response, `${dataset} ${name} /exposure[${k}]:`, 1e-12);
    }
    // the simulator's deterministic half: inputs, clamps, calibration, reporting grid
    for (const { request, response } of want.simulate) {
      const got = await b.simulate(request);
      for (const k of ["status", "method", "steps", "n_paths", "bar_seconds", "horizon_seconds", "start_price", "position_value", "clamped", "symbol", "level", "currency", "error"])
        same(got[k] ?? null, response[k] ?? null, `${dataset} ${name} /simulate.${k}`);
      if (response.status === "ok") {
        same(got.fan.step, response.fan.step, `${dataset} ${name} /simulate.fan.step`);
        same(got.model, response.model, `${dataset} ${name} /simulate.model:`, 1e-12);
      }
    }
    if (name === "frozen") {
      await b.resume();
      stages.push(["finished", exp.finished]);
    }
  }
  b.dispose();

  // ---- 3. a paced replay passes through the same readings: user pause part-way, then compare
  let clock = 1000;
  const p = new StaticBackend(async (name) => readJson(tapeDir, `${name}.json`), () => clock);
  await p.replay(dataset, 86400, null);
  for (let k = 0; k < 50; k++) {
    clock += 0.04; // 2 s of wall time at one day of data per second
    p.pump();
  }
  await p.pause();
  for (let k = 0; k < 400 && (await p.score()).state !== "paused"; k++) {
    clock += 0.04;
    p.pump();
  }
  const paused = await p.score();
  if (!(paused.session.progress > 0.01 && paused.session.progress < 0.99)) fail(`${dataset} paced replay: progress ${paused.session.progress} after 2 s at 1 d/s`);
  const i = Math.round(paused.session.progress * tape.n) - 1;
  same(paused.state, "paused", `${dataset} paced pause state`);
  same(paused.session.pause_reason, "user", `${dataset} paced pause reason`);
  same(frame(tape, i, true).price, paused.price, `${dataset} paced pause price`);
  p.dispose();

  summary.push(`${dataset}: ${tape.n} ticks, ${perTick} per-tick values, ${checked - before - perTick} API values`);
}

// ---- 4. the simulator's random half: same model, different dice, so compare distributions
const sim = readJson(expDir, "simulate_stats.json");
let simChecks = 0;
for (const c of sim.cases) {
  const got = simulate({ ...c.regime }, c.value, { ...c.params, seed: 12345 });
  const scale = Math.abs(c.result.terminal.pnl_p95 - c.result.terminal.pnl_p5);
  for (const k of ["pnl_p5", "pnl_p50", "pnl_p95", "expected_shortfall_5", "worst_drawdown_p5"]) {
    simChecks++;
    const d = Math.abs(got.terminal[k] - c.result.terminal[k]);
    if (d > 0.06 * scale) fail(`simulate ${c.name} terminal.${k}: site ${got.terminal[k]} vs python ${c.result.terminal[k]} (band ${scale})`);
  }
  simChecks++;
  if (Math.abs(got.terminal.prob_loss - c.result.terminal.prob_loss) > 0.03) fail(`simulate ${c.name} prob_loss: ${got.terminal.prob_loss} vs ${c.result.terminal.prob_loss}`);
  const last = got.fan.step.length - 1;
  for (const k of ["price_p5", "price_p50", "price_p95"]) {
    simChecks++;
    const band = Math.abs(c.result.fan.price_p95[last] - c.result.fan.price_p5[last]);
    for (const j of [0, Math.floor(last / 2), last])
      if (Math.abs(got.fan[k][j] - c.result.fan[k][j]) > 0.06 * band) fail(`simulate ${c.name} fan.${k}[${j}]: ${got.fan[k][j]} vs ${c.result.fan[k][j]}`);
  }
}
for (const [steps, want] of sim.fan_steps) same(fanSteps(steps), want, `fanSteps(${steps})`);

for (const s of summary) console.log(s);
console.log(`simulator: ${sim.cases.length} regimes, ${simChecks} distribution checks within tolerance; ${sim.fan_steps.length} reporting grids identical`);
if (failures.length) {
  console.error(`\nPARITY FAILED (${failures.length}${failures.length >= 40 ? "+" : ""} differences):`);
  for (const f of failures) console.error("  " + f);
  process.exit(1);
}
console.log(`PARITY OK: ${checked} values compared, 0 differences (snapshots, history, alerts, hindsight: exact; exposure: 1e-12 relative)`);
