// Browser ports of stormchaser/models.py (symbols), stormchaser/exposure.py and stormchaser/simulate.py,
// used only by the static demo. The storm score itself is never computed here: it is read off a tape that
// the Python engine wrote (scripts/build_static.py). `npm run parity` checks these ports against Python.

export interface Regime {
  ready: boolean;
  price: number | null;
  bar_seconds: number;
  sigma_fast: number | null;
  sigma_base: number | null;
  returns: number[];
  level: string;
  symbol?: string;
}

type Dict = Record<string, unknown>;

// ------------------------------------------------------------------ symbols (models.py)
const USD_LIKE = new Set(["USD", "USDT", "USDC", "BUSD", "FDUSD"]);
const QUOTES = ["USDT", "USDC", "BUSD", "FDUSD", "USD", "EUR", "GBP", "JPY", "CHF", "AUD", "CAD", "NZD", "BTC", "ETH"];

export function normSymbol(s: unknown): string {
  let v = String(s ?? "").toUpperCase().trim();
  if (v.endsWith("=X")) v = v.slice(0, -2);
  return v.replace(/[^\p{L}\p{N}]/gu, "");
}

export function splitSymbol(sym: string): [string, string] {
  const s = normSymbol(sym);
  for (const q of QUOTES) if (s.endsWith(q) && s.length > q.length) return [s.slice(0, -q.length), q];
  return [s, "USD"];
}

export function sameAsset(a: string, b: string): boolean {
  const na = normSymbol(a);
  const nb = normSymbol(b);
  if (!na || !nb) return false;
  if (na === nb) return true;
  const [ba, qa] = splitSymbol(na);
  const [bb, qb] = splitSymbol(nb);
  return ba === bb && (qa === qb || (USD_LIKE.has(qa) && USD_LIKE.has(qb)));
}

export const reportCurrency = (symbol: string): string => {
  const quote = splitSymbol(symbol)[1];
  return USD_LIKE.has(quote) ? "USD" : quote;
};

const fin = (x: unknown): x is number => typeof x === "number" && Number.isFinite(x);
/** Python's repr() of a plain string, for error messages that quote user input. */
export const pyRepr = (s: string): string => (s.includes("'") && !s.includes('"') ? `"${s}"` : `'${s.replace(/\\/g, "\\\\").replace(/'/g, "\\'")}'`);

// ------------------------------------------------------------------ exposure (exposure.py)
const MAX_HORIZON = 100_000;
const Z: Record<string, number> = { "0.9": 1.2815515655, "0.95": 1.644853627, "0.975": 1.9599639845, "0.99": 2.326347874, "0.995": 2.5758293035 };

/** Acklam's rational approximation, as in exposure.py. */
export function normPpf(p: number): number {
  const known = Z[String(p)];
  if (known !== undefined) return known;
  const a = [-3.969683028665376e1, 2.209460984245205e2, -2.759285104469687e2, 1.38357751867269e2, -3.066479806614716e1, 2.506628277459239];
  const b = [-5.447609879822406e1, 1.615858368580409e2, -1.556989798598866e2, 6.680131188771972e1, -1.328068155288572e1];
  const c = [-7.784894002430293e-3, -3.223964580411365e-1, -2.400758277161838, -2.549732539343734, 4.374664141464968, 2.938163982698783];
  const d = [7.784695709041462e-3, 3.224671290700398e-1, 2.445134137142996, 3.754408661907416];
  if (p < 0.02425) {
    const q = Math.sqrt(-2 * Math.log(p));
    return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1);
  }
  if (p > 1 - 0.02425) return -normPpf(1 - p);
  const q = p - 0.5;
  const r = q * q;
  return ((((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q) / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1);
}

/** Loss VaR and ES (positive numbers) when the horizon log-return is N(0, sigma_h^2). */
export function varEs(value: number, sigmaH: number, conf: number): [number, number] {
  if (value === 0 || sigmaH <= 0) return [0, 0];
  const z = normPpf(conf);
  const esZ = Math.exp(-0.5 * z * z) / Math.sqrt(2 * Math.PI) / (1 - conf);
  const sgn = value > 0 ? 1 : -1;
  const v = -value * Math.expm1(-sgn * Math.min(z * sigmaH, 5));
  const es = -value * Math.expm1(-sgn * Math.min(esZ * sigmaH, 5));
  return [Math.max(0, v), Math.max(0, es)];
}

/** Most adverse h-bar log return in the sample for a long (sign > 0) or short (sign < 0). */
export function worstMove(returns: number[], h: number, sign: number): number {
  const n = returns.length;
  if (n === 0) return 0;
  let best = sign > 0 ? Infinity : -Infinity;
  const take = (w: number) => {
    if (sign > 0 ? w < best : w > best) best = w;
  };
  if (n > h) {
    const cs = new Float64Array(n + 1);
    for (let i = 0; i < n; i++) cs[i + 1] = cs[i] + returns[i];
    for (let i = h; i <= n; i++) take(cs[i] - cs[i - h]);
  } else {
    const sq = Math.sqrt(h);
    for (const r of returns) take(r * sq);
  }
  return best;
}

export function computeExposure(positionsIn: unknown, lookup: (asset: string) => Regime | null, horizonIn: unknown = 60, confIn: unknown = 0.99): Dict {
  const notes: string[] = [];
  let confidence = confIn as number;
  if (!fin(confidence) || !(confidence >= 0.5 && confidence < 1.0)) {
    notes.push("confidence out of range; using 0.99");
    confidence = 0.99;
  }
  confidence = Math.min(confidence, 0.9999);
  let horizon = horizonIn as number;
  if (!fin(horizon) || horizon < 1) {
    notes.push("horizon_bars must be >= 1; using 1");
    horizon = 1;
  }
  if (horizon > MAX_HORIZON) {
    notes.push(`horizon_bars clamped to ${MAX_HORIZON}`);
    horizon = MAX_HORIZON;
  }
  const h = Math.trunc(horizon);
  const positions = (Array.isArray(positionsIn) ? positionsIn : []) as unknown[];

  interface Tot { value: number; gross: number; pnl: number; var: number; es: number; var_baseline: number; stressed_loss: number; var2: number; incomplete: boolean }
  const rows: Dict[] = [];
  const byCcy = new Map<string, Tot>();
  positions.forEach((raw, i) => {
    const isObj = typeof raw === "object" && raw !== null && !Array.isArray(raw);
    const p = (isObj ? raw : {}) as Dict;
    const row: Dict = { index: i, asset: isObj ? (p.asset ?? null) : null, status: "ok" };
    rows.push(row);
    if (!isObj || typeof p.asset !== "string" || !normSymbol(p.asset)) {
      Object.assign(row, { status: "error", error: "position needs an 'asset' string" });
      return;
    }
    const size = p.size;
    if (!fin(size)) {
      Object.assign(row, { status: "error", error: "size must be a finite number" });
      return;
    }
    const reg = lookup(p.asset);
    const price = fin(p.price) && p.price > 0 ? p.price : (reg?.price ?? null);
    if (reg === null && !fin(price)) {
      Object.assign(row, { status: "unknown_asset", error: `no feed or price for ${pyRepr(p.asset)}; start a replay/live session for it or pass 'price'` });
      return;
    }
    if (!fin(price) || price <= 0) {
      Object.assign(row, { status: "error", error: "no valid price yet" });
      return;
    }
    const [base, quote] = splitSymbol(reg?.symbol || p.asset);
    const ccy = normSymbol(String(p.currency || ""));
    let units: number;
    if (!ccy || ccy === base) units = size;
    else if (ccy === quote || (USD_LIKE.has(ccy) && USD_LIKE.has(quote))) units = size / price; // size given as notional in quote currency
    else {
      Object.assign(row, { status: "error", error: `currency ${ccy} is neither ${base} (units) nor ${quote} (notional)` });
      return;
    }
    const rep = USD_LIKE.has(quote) ? "USD" : quote;
    const value = units * price;
    const entry = p.entry_price;
    const pnl = fin(entry) && entry > 0 ? units * (price - entry) : null;
    Object.assign(row, { units, price, value, currency: rep, pnl });
    let tot = byCcy.get(rep);
    if (!tot) byCcy.set(rep, (tot = { value: 0, gross: 0, pnl: 0, var: 0, es: 0, var_baseline: 0, stressed_loss: 0, var2: 0, incomplete: false }));
    tot.value += value;
    tot.gross += Math.abs(value);
    tot.pnl += pnl || 0;
    if (units === 0) {
      Object.assign(row, { status: "zero", var: 0, es: 0, var_baseline: 0, stressed_loss: 0 });
      return;
    }
    const sig = fin(p.sigma_bar) && p.sigma_bar > 0 ? p.sigma_bar : null;
    const sf = sig || reg?.sigma_fast || null;
    const sb = sig || reg?.sigma_base || null;
    if ((!(reg && reg.ready) && sig === null) || !sf || !sb) {
      Object.assign(row, { status: "warming_up", var: null, es: null, var_baseline: null, stressed_loss: null, note: "risk numbers unavailable until the feed has warmed up" });
      tot.incomplete = true;
      return;
    }
    const rets = (reg?.returns ?? []).filter((x) => Number.isFinite(x));
    const sq = Math.sqrt(h);
    const [v, es] = varEs(value, sf * sq, confidence);
    const [vb] = varEs(value, sb * sq, confidence);
    const wm = worstMove(rets, h, value > 0 ? 1 : -1);
    const stressed = Math.max(es, -value * Math.expm1(Math.max(Math.min(wm, 5), -5)));
    Object.assign(row, {
      var: v, es, var_baseline: vb, stressed_loss: stressed, method: "normal", sigma_bar: sf, sigma_bar_baseline: sb,
      vol_ratio: sf / sb, level: reg?.level ?? null, worst_regime_move: Math.expm1(wm),
    });
    tot.var += v;
    tot.es += es;
    tot.var_baseline += vb;
    tot.stressed_loss += stressed;
    tot.var2 += v * v;
  });

  const totals: Dict = {};
  let anyGross = false;
  for (const [ccy, t] of byCcy) {
    if (t.gross !== 0) anyGross = true;
    totals[ccy] = {
      value: t.value, gross: t.gross, pnl: t.pnl,
      var: t.var, // undiversified: every storm hits at once
      var_diversified: Math.sqrt(t.var2), // zero-correlation floor
      es: t.es, var_baseline: t.var_baseline, stressed_loss: t.stressed_loss,
      stressed_loss_pct_gross: t.gross > 0 ? t.stressed_loss / t.gross : 0,
      incomplete: t.incomplete,
    };
  }
  if (positions.length === 0) notes.push("empty portfolio: nothing at risk");
  else if (byCcy.size === 0 || !anyGross) notes.push("no valued, non-zero positions: nothing at risk");
  return { horizon_bars: h, confidence, positions: rows, totals, notes };
}

// ------------------------------------------------------------------ cyclone simulator (simulate.py)
const MAX_STEPS = 5000, MAX_PATHS = 20000, MAX_CELLS = 4_000_000, MAX_POINTS = 200, MIN_PATHS = 200;

/** Seeded generator (mulberry32) with normal and Poisson draws. Not numpy's stream: same model, different dice. */
export class Rng {
  private s: number;
  private spare: number | null = null;
  constructor(seed?: number | null) {
    this.s = (fin(seed) ? Math.trunc(seed) : Math.floor(Math.random() * 2 ** 32)) >>> 0;
  }
  uniform(): number {
    this.s = (this.s + 0x6d2b79f5) >>> 0;
    let t = this.s;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  }
  normal(): number {
    if (this.spare !== null) {
      const v = this.spare;
      this.spare = null;
      return v;
    }
    let u = 0;
    while (u === 0) u = this.uniform();
    const r = Math.sqrt(-2 * Math.log(u));
    const th = 2 * Math.PI * this.uniform();
    this.spare = r * Math.sin(th);
    return r * Math.cos(th);
  }
  poisson(lam: number): number {
    const limit = Math.exp(-lam);
    let k = 0;
    let p = this.uniform();
    while (p > limit) {
      k++;
      p *= this.uniform();
    }
    return k;
  }
}

const pyNum = (v: number, integer: boolean) => (integer || !Number.isInteger(v) ? String(v) : v.toFixed(1));

function clampParam(name: string, val: unknown, lo: number, hi: number, dflt: number, clamped: string[], integer = true): number {
  if (!fin(val)) {
    clamped.push(`${name}: invalid, using ${pyNum(dflt, integer)}`);
    return dflt;
  }
  let v = Math.min(Math.max(val, lo), hi);
  if (integer) v = Math.trunc(v);
  if (v !== val) clamped.push(`${name}: ${val} -> ${pyNum(v, integer)}`);
  return v;
}

/** numpy.quantile, default (linear) interpolation, on an already sorted array. */
function quantileSorted(a: Float64Array, q: number): number {
  const pos = (a.length - 1) * q;
  const lo = Math.floor(pos);
  const hi = Math.min(lo + 1, a.length - 1);
  return a[lo] + (a[hi] - a[lo]) * (pos - lo);
}

/** The steps the fan is reported at: numpy's unique(round(linspace(0, steps - 1, min(steps, 200)))). */
export function fanSteps(steps: number): number[] {
  const m = Math.min(steps, MAX_POINTS);
  const out: number[] = [];
  const step = m > 1 ? (steps - 1) / (m - 1) : 0;
  for (let k = 0; k < m; k++) {
    const x = k === m - 1 && m > 1 ? steps - 1 : k * step;
    const f = Math.floor(x);
    const r = x - f === 0.5 ? (f % 2 === 0 ? f : f + 1) : Math.round(x); // numpy rounds halves to even
    if (out.length === 0 || out[out.length - 1] !== r) out.push(r);
  }
  return out;
}

export interface SimParams {
  steps?: unknown;
  n_paths?: unknown;
  method?: unknown;
  seed?: unknown;
  alpha?: unknown;
  beta?: unknown;
  vol_mult?: unknown;
}

export function simulate(regime: Regime, valueIn: number | null, params: SimParams = {}): Dict {
  const clamped: string[] = [];
  const price = regime.price;
  if (!fin(price) || price <= 0) return { status: "no_price", error: "no valid price for this asset yet" };
  let sf = regime.sigma_fast as number;
  let sb = regime.sigma_base as number;
  if (!regime.ready || !fin(sf) || !fin(sb) || sf <= 0 || sb <= 0) return { status: "warming_up", error: "not enough history to calibrate the simulator yet" };
  const steps = clampParam("steps", params.steps ?? 60, 1, MAX_STEPS, 60, clamped);
  let nPaths = clampParam("n_paths", params.n_paths ?? 2000, MIN_PATHS, MAX_PATHS, 2000, clamped);
  if (steps * nPaths > MAX_CELLS) {
    const next = Math.max(MIN_PATHS, Math.floor(MAX_CELLS / steps));
    clamped.push(`n_paths: ${nPaths} -> ${next} (steps*paths budget)`);
    nPaths = next;
  }
  const volMult = clampParam("vol_mult", params.vol_mult ?? 1.0, 0.1, 10.0, 1.0, clamped, false);
  const alpha = clampParam("alpha", params.alpha ?? 0.08, 0.0, 0.5, 0.08, clamped, false);
  let beta = clampParam("beta", params.beta ?? 0.9, 0.0, 0.999, 0.9, clamped, false);
  if (alpha + beta >= 0.999) {
    beta = 0.999 - alpha;
    clamped.push(`beta: reduced to ${beta.toFixed(3)} so alpha+beta<1 (stationary)`);
  }
  let method = params.method ?? "garch";
  if (method !== "garch" && method !== "bootstrap") {
    clamped.push(`method: ${typeof method === "string" ? pyRepr(method) : String(method)} unknown, using garch`);
    method = "garch";
  }
  const value = fin(valueIn) ? valueIn : price; // default: one unit of the asset
  const rets = regime.returns.filter((x) => Number.isFinite(x));
  if (method === "bootstrap" && rets.length < 30) {
    clamped.push("bootstrap: fewer than 30 regime returns, using garch");
    method = "garch";
  }

  const rng = new Rng(params.seed as number | null | undefined);
  sf *= volMult;
  sb *= volMult;
  const jumpK = 4.0;
  let lam = 0, jSd = 0, jMu = 0, omega = 0, cap = 0;
  let info: Dict = {};
  const variance = new Float64Array(nPaths);
  if (method === "garch") {
    const jumps = rets.filter((r) => Math.abs(r) > (jumpK * sb) / volMult).map((r) => r * volMult);
    lam = Math.min(jumps.length / Math.max(rets.length, 1), 0.25);
    if (jumps.length) {
      jSd = Math.sqrt(jumps.reduce((s, j) => s + j * j, 0) / jumps.length);
      jMu = jumps.reduce((s, j) => s + j, 0) / jumps.length;
    }
    omega = (1 - alpha - beta) * sb * sb;
    cap = (50 * Math.max(sf, sb)) ** 2;
    variance.fill(sf * sf);
    info = { jump_prob_per_bar: lam, jump_sd: jSd, alpha, beta, sigma_start: sf, sigma_long_run: sb };
  }
  const withJumps = lam > 0 && jSd > 0;

  const idx = fanSteps(steps);
  const cum = new Float64Array(nPaths); // log price relative to now
  const lowest = new Float64Array(nPaths).fill(Infinity);
  const highest = new Float64Array(nPaths).fill(-Infinity);
  const level = new Float64Array(nPaths);
  const q5: number[] = [], q50: number[] = [], q95: number[] = [];
  let next = 0;
  for (let t = 0; t < steps; t++) {
    for (let p = 0; p < nPaths; p++) {
      let r: number;
      if (method === "bootstrap") r = rets[Math.floor(rng.uniform() * rets.length)] * volMult;
      else {
        const eps = Math.sqrt(variance[p]) * rng.normal();
        variance[p] = Math.min(omega + alpha * eps * eps + beta * variance[p], cap);
        r = eps;
        if (withJumps) {
          const n = rng.poisson(lam);
          if (n > 0) r += n * jMu + Math.sqrt(n) * jSd * rng.normal();
        }
      }
      if (Number.isNaN(r)) r = 0;
      r = Math.min(Math.max(r, -0.5), 0.5);
      cum[p] += r;
      const x = Math.exp(Math.min(Math.max(cum[p], -20), 20));
      level[p] = x;
      if (x < lowest[p]) lowest[p] = x;
      if (x > highest[p]) highest[p] = x;
    }
    if (idx[next] === t) {
      const sorted = Float64Array.from(level).sort();
      q5.push(quantileSorted(sorted, 0.05));
      q50.push(quantileSorted(sorted, 0.5));
      q95.push(quantileSorted(sorted, 0.95));
      next++;
    }
  }
  let pnl = [q5, q50, q95].map((q) => q.map((x) => value * (x - 1.0)));
  if (value < 0) pnl = pnl.reverse(); // short: the bad tail is the up-move
  const term = Float64Array.from(level, (x) => value * (x - 1.0)).sort();
  const worst = Float64Array.from(value >= 0 ? lowest : highest, (x) => value * (x - 1.0)).sort();
  const t5 = quantileSorted(term, 0.05);
  let tailSum = 0, tailN = 0, losses = 0;
  for (const v of term) {
    if (v <= t5) {
      tailSum += v;
      tailN++;
    }
    if (v < 0) losses++;
  }
  const bs = regime.bar_seconds || 0;
  return {
    status: "ok", method, steps, n_paths: nPaths, bar_seconds: bs, horizon_seconds: steps * bs, start_price: price,
    position_value: value, clamped,
    fan: {
      step: idx.map((i) => i + 1),
      price_p5: q5.map((x) => price * x), price_p50: q50.map((x) => price * x), price_p95: q95.map((x) => price * x),
      pnl_p5: pnl[0], pnl_p50: pnl[1], pnl_p95: pnl[2],
    },
    terminal: {
      pnl_p5: t5, pnl_p50: quantileSorted(term, 0.5), pnl_p95: quantileSorted(term, 0.95),
      expected_shortfall_5: tailSum / tailN, prob_loss: losses / term.length, worst_drawdown_p5: quantileSorted(worst, 0.05),
    },
    model: info,
  };
}
