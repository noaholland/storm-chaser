// The static demo's "server": plays back tapes that the Python engine recorded (scripts/build_static.py)
// and answers the same calls, with the same shapes, as stormchaser/api.py + session.py.
// No scoring happens here. Runs in the browser and, for the parity test, in plain Node.
import { computeExposure, normSymbol, pyRepr, reportCurrency, sameAsset, simulate } from "./risk.ts";
import type { Regime } from "./risk.ts";

type Dict = Record<string, unknown>;
type Col = { k: unknown } | { a: unknown[] } | { d: unknown[]; i: number[] } | { "=": string };

export interface Tape {
  v: number;
  dataset: string;
  symbol: string;
  n: number;
  session: Dict & { bar_seconds: number };
  warmup_bars: number;
  regime_window: number;
  debounce_s: number;
  tick_ts: number[];
  tick_price: number[];
  cols: Record<string, Col>;
  hist: number[];
  transitions: (Dict & { to: string })[];
  tr_tick: number[];
  alerts: Dict[];
  alert_tick: number[];
  suppressed_tick: number[];
  returns: number[];
  ret_tick: number[];
  ended: Dict;
}

export class BackendError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

export const MIN_SPEED = 0.1;
export const MAX_SPEED = 1_000_000;
export const LOCAL_ONLY = "needs the Python backend: this page is the static demo. Clone the repo and run `make app` for live feeds";
const ABSENT = Symbol("absent");

/** Speed = data seconds per wall second. Garbage -> MAX (replay.py clamp_speed). */
function clampSpeed(speed: unknown): [number, boolean] {
  const s = typeof speed === "number" ? speed : Number(speed);
  if (!Number.isFinite(s) || s <= 0) return [MAX_SPEED, true];
  const c = Math.min(Math.max(s, MIN_SPEED), MAX_SPEED);
  return [c, c !== s];
}

function parseSpeed(speed: unknown): number {
  if (speed === null || speed === undefined || ["max", "fast", ""].includes(String(speed).toLowerCase())) return MAX_SPEED;
  const v = Number(speed);
  if (Number.isNaN(v)) throw new BackendError(422, `speed must be a number between ${MIN_SPEED} and ${MAX_SPEED.toExponential().replace("e+", "e+0")}, or 'max'`);
  if (!Number.isFinite(v) || v <= 0) throw new BackendError(422, `speed must be a positive finite number (${MIN_SPEED} to ${MAX_SPEED.toExponential().replace("e+", "e+0")}), or 'max'`);
  return v;
}

/** Count of entries <= x in an ascending array. */
function countLE(a: number[], x: number): number {
  let lo = 0, hi = a.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (a[mid] <= x) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}

function cell(tape: Tape, name: string, i: number): unknown {
  let c = tape.cols[name];
  if (c === undefined) return ABSENT;
  while ("=" in c) c = tape.cols[c["="]];
  if ("k" in c) return c.k;
  if ("a" in c) return c.a[i];
  const j = c.i[i];
  return j < 0 ? ABSENT : c.d[j];
}

/** How much of each running list the session has seen once tick i has been processed. */
export function seenAt(tape: Tape, i: number) {
  return {
    transitions: countLE(tape.tr_tick, i),
    history: countLE(tape.hist, i),
    alerts: countLE(tape.alert_tick, i),
    suppressed: countLE(tape.suppressed_tick, i),
    returns: countLE(tape.ret_tick, i),
  };
}

/** The engine's regime window (its last `regime_window` per-bar returns) after tick i. */
export function regimeReturns(tape: Tape, i: number): number[] {
  const n = seenAt(tape, i).returns;
  return tape.returns.slice(Math.max(0, n - tape.regime_window), n);
}

const runPaths = new WeakMap<Tape, string[]>();
function pathsOf(tape: Tape): string[] {
  let p = runPaths.get(tape);
  if (!p) runPaths.set(tape, (p = Object.keys(tape.cols).filter((k) => k.startsWith("run:")).map((k) => k.slice(4))));
  return p;
}

/** The engine's snapshot at tick i, as recorded: while flowing, or (frozen = true) while paused on that tick. */
export function frame(tape: Tape, i: number, frozen: boolean): Dict {
  const out: Dict = {};
  for (const path of pathsOf(tape)) {
    let v = frozen ? cell(tape, "paused:" + path, i) : ABSENT;
    if (v === ABSENT) v = cell(tape, "run:" + path, i);
    if (v === ABSENT) continue;
    const keys = path.split(".");
    let node = out;
    for (let k = 0; k < keys.length - 1; k++) node = (node[keys[k]] ??= {}) as Dict;
    node[keys[keys.length - 1]] = v;
  }
  return out;
}

class Session {
  readonly kind = "replay";
  state: "running" | "ended" | "stopped" = "running";
  readonly started: number;
  version = 0;
  pos = 0; // ticks processed so far
  speed: number;
  pauseOn: string | null;
  paused = false;
  pauseReason: string | null = null;
  ahead: Dict | null = null;
  credit = 0; // wall seconds not yet spent on ticks
  private pauseReq = false;
  private lastRx: number;
  private sufLo: Int32Array | null = null;
  private sufHi: Int32Array | null = null;
  readonly tape: Tape;
  readonly symbol: string;
  private readonly mgr: StaticBackend;

  constructor(tape: Tape, speed: number, pauseOn: string | null, mgr: StaticBackend) {
    this.tape = tape;
    this.symbol = tape.symbol;
    this.mgr = mgr;
    this.speed = clampSpeed(speed)[0];
    this.pauseOn = pauseOn;
    this.started = mgr.now();
    this.lastRx = this.started;
  }

  get barSeconds(): number {
    return this.tape.session.bar_seconds;
  }

  /** Process the next tick of the tape, exactly as Session.run does around engine.update(). */
  private step(): void {
    const t = this.tape;
    const i = this.pos;
    this.pos = i + 1;
    this.version++;
    this.lastRx = this.mgr.now();
    this.mgr.emitAlerts(this, i);
    let hit = false;
    if (this.pauseOn) for (let k = seenAt(t, i - 1).transitions; k < seenAt(t, i).transitions; k++) if (t.transitions[k].to === this.pauseOn) hit = true;
    if (hit || this.pauseReq) {
      this.pauseReason = hit ? this.pauseOn : "user";
      if (hit) this.pauseOn = null;
      this.pauseReq = false;
      this.paused = true;
      this.ahead = this.lookahead();
      this.version++;
    } else if (this.pos >= t.n) this.finish();
  }

  private finish(): void {
    this.state = "ended";
    this.version++;
  }

  /** Spend wall time on ticks at the current speed (ReplaySource pacing: idle gaps cost at most 1 s). */
  advance(wallSeconds: number): void {
    if (this.state !== "running" || this.paused) {
      this.credit = 0;
      return;
    }
    const t = this.tape;
    this.credit = Math.min(this.credit + wallSeconds, 2);
    while (this.state === "running" && !this.paused && this.pos < t.n) {
      const cost = this.pos === 0 || this.speed >= MAX_SPEED ? 0 : Math.min(Math.max(t.tick_ts[this.pos] - t.tick_ts[this.pos - 1], 0) / this.speed, 1);
      if (cost > this.credit) break;
      this.credit -= cost;
      this.step();
    }
  }

  pause(): boolean {
    if (this.state !== "running" || this.paused) return false;
    this.pauseReq = true;
    return true;
  }

  resume(): boolean {
    const was = this.paused || this.pauseReq;
    this.pauseReq = false;
    if (this.paused) {
      this.paused = false;
      this.pauseReason = null;
      this.ahead = null;
      this.version++;
      this.lastRx = this.mgr.now();
      this.credit = 0;
      if (this.pos >= this.tape.n) this.finish();
      else if (this.speed >= MAX_SPEED) this.advance(0);
    }
    return was;
  }

  setSpeed(speed: number): Dict {
    const [s, clamped] = clampSpeed(speed);
    this.speed = s;
    this.version++;
    if (s >= MAX_SPEED) this.advance(0);
    return { speed: s, speed_clamped: clamped };
  }

  stop(): void {
    if (this.state === "running") {
      this.state = "stopped";
      this.version++;
    }
  }

  /** What the rest of the file does from here (hindsight for the freeze callout): Session.lookahead. */
  private lookahead(): Dict | null {
    const t = this.tape;
    const i = this.pos - 1;
    const px = cell(t, "run:price", i) as number | null;
    if (!t.n || !px || this.pos >= t.n) return null;
    if (!this.sufLo || !this.sufHi) {
      const lo = (this.sufLo = new Int32Array(t.n));
      const hi = (this.sufHi = new Int32Array(t.n));
      lo[t.n - 1] = hi[t.n - 1] = t.n - 1;
      for (let k = t.n - 2; k >= 0; k--) {
        lo[k] = t.tick_price[k] <= t.tick_price[lo[k + 1]] ? k : lo[k + 1]; // ties: the earliest one, like min()
        hi[k] = t.tick_price[k] >= t.tick_price[hi[k + 1]] ? k : hi[k + 1];
      }
    }
    const lo = this.sufLo[this.pos], hi = this.sufHi[this.pos];
    return {
      from_ts: cell(t, "run:ts", i), from_price: px, end_ts: t.tick_ts[t.n - 1],
      low: t.tick_price[lo], low_ts: t.tick_ts[lo], pct_to_low: Math.min(0, t.tick_price[lo] / px - 1.0),
      high: t.tick_price[hi], high_ts: t.tick_ts[hi], pct_to_high: Math.max(0, t.tick_price[hi] / px - 1.0),
    };
  }

  progress(): number | null {
    return this.tape.n ? Number((this.pos / this.tape.n).toFixed(4)) : null;
  }

  snapshot(): Dict {
    const t = this.tape;
    const i = this.pos - 1;
    let s: Dict;
    if (this.state === "running") s = frame(t, i, this.paused);
    else if (this.state === "ended" && this.pos >= t.n) s = structuredClone(t.ended);
    else {
      // stopped part-way: the last reading, flagged as final (Session.snapshot, state != running)
      s = frame(t, i, true);
      s.state = "ended";
      if (s.last_score === null) {
        s.level = "ended";
        (s.playbook as Dict).stay_safe = "No reading.";
      }
    }
    delete s.session;
    const feed = s.feed as Dict;
    feed.age_s = this.state === "running" && !this.paused ? 0.0 : Math.max(0, this.mgr.now() - this.lastRx);
    feed.stale_after_s = Math.max(10.0, (5.0 * this.barSeconds) / this.speed + 3.0);
    s.session = {
      ...t.session, state: this.paused ? "paused" : this.state, started: this.started, speed: this.speed, progress: this.progress(),
      pause_on: this.pauseOn, pause_reason: this.pauseReason, ahead: this.ahead,
    };
    return s;
  }

  transitionsSeen(): number {
    return seenAt(this.tape, this.pos - 1).transitions;
  }

  series(n: number): Dict {
    const t = this.tape;
    const seen = seenAt(t, this.pos - 1).history;
    const step = Math.max(1, Math.ceil(seen / Math.max(1, n)));
    const ts: unknown[] = [], price: unknown[] = [], score: unknown[] = [], level: unknown[] = [];
    for (let k = 0; k < seen; k += step) {
      const i = t.hist[k];
      ts.push(cell(t, "run:ts", i));
      price.push(cell(t, "run:price", i));
      score.push(cell(t, "run:last_score", i));
      level.push(cell(t, "run:last_level", i));
    }
    return { symbol: this.symbol, points: ts.length, stride: step, ts, price, score, level, transitions: t.transitions.slice(0, this.transitionsSeen()).slice(-5000) };
  }

  /** StormEngine.regime(): what exposure and the simulator need. */
  regime(): Regime {
    const t = this.tape;
    const i = this.pos - 1;
    return {
      ready: cell(t, "run:last_score", i) !== null,
      price: cell(t, "run:price", i) as number | null,
      bar_seconds: this.barSeconds,
      sigma_fast: cell(t, "run:vol.sigma_bar_fast", i) as number | null,
      sigma_base: cell(t, "run:vol.sigma_bar_base", i) as number | null,
      returns: regimeReturns(t, i),
      level: cell(t, "run:last_level", i) as string,
      symbol: this.symbol,
    };
  }
}

export interface StreamLike {
  onmessage: ((ev: { data: string }) => void) | null;
  onerror: (() => void) | null;
  addEventListener(type: "end", fn: () => void): void;
  close(): void;
}

export class StaticBackend {
  private sessions = new Map<string, Session>();
  private latest: string | null = null;
  private tapes = new Map<string, Promise<Tape>>();
  private alertLog: Dict[] = [];
  private suppressed = 0;
  private timer: ReturnType<typeof setInterval> | null = null;
  private lastPump = 0;
  private readonly startedAt: number;
  private catalogue: Promise<Dict> | null = null;
  private readonly load: (name: string) => Promise<unknown>;
  readonly now: () => number;

  /** `load(name)` returns the parsed JSON of data/<name>.json. */
  constructor(load: (name: string) => Promise<unknown>, now: () => number = () => Date.now() / 1000) {
    this.load = load;
    this.now = now;
    this.startedAt = now();
  }

  // ------------------------------------------------------------------ pacing
  /** Advance every running replay by the wall time since the last call. */
  pump(): void {
    const now = this.now();
    const dt = Math.min(Math.max(now - this.lastPump, 0), 1);
    this.lastPump = now;
    for (const s of this.sessions.values()) s.advance(dt);
  }

  private ensureTimer(): void {
    if (this.timer === null) {
      this.lastPump = this.now();
      this.timer = setInterval(() => this.pump(), 40);
    }
  }

  dispose(): void {
    if (this.timer !== null) clearInterval(this.timer);
    this.timer = null;
  }

  // ------------------------------------------------------------------ lookups (SessionManager.get / need)
  private get(symbol?: string | null): Session | null {
    if (!symbol) return this.latest ? (this.sessions.get(this.latest) ?? null) : null;
    const key = normSymbol(symbol);
    const direct = this.sessions.get(key);
    if (direct) return direct;
    for (const [k, s] of this.sessions) if (sameAsset(k, key) || normSymbol(s.tape.dataset) === key) return s;
    return null;
  }

  private need(symbol?: string | null): Session {
    const s = this.get(symbol);
    if (s) return s;
    if (symbol) throw new BackendError(404, `no session for ${pyRepr(symbol)}; active: [${[...this.sessions.keys()].sort().map(pyRepr).join(", ")}]`);
    throw new BackendError(409, "no session running yet; start one with /replay, /live or /yfinance");
  }

  emitAlerts(sess: Session, tick: number): void {
    const t = sess.tape;
    const before = seenAt(t, tick - 1), after = seenAt(t, tick);
    for (let k = before.alerts; k < after.alerts; k++) this.alertLog.push({ ...t.alerts[k], wall_time: this.now() });
    if (this.alertLog.length > 200) this.alertLog.splice(0, this.alertLog.length - 200);
    this.suppressed += after.suppressed - before.suppressed;
  }

  // ------------------------------------------------------------------ the API
  async datasets(): Promise<Dict> {
    this.catalogue ??= this.load("catalogue") as Promise<Dict>;
    return this.catalogue;
  }

  async health(): Promise<Dict> {
    const sessions: Dict = {};
    let degraded = false;
    for (const [k, s] of this.sessions) {
      const feed = s.snapshot().state as string;
      if (feed === "stale" || feed === "error") degraded = true;
      sessions[k] = { kind: s.kind, state: s.state, feed, error: null, progress: s.progress() };
    }
    return { status: degraded ? "degraded" : "ok", uptime_s: this.now() - this.startedAt, sessions, latest: this.latest, notifiers: [], ui: true, static: true };
  }

  async replay(dataset: string | null | undefined, speed: unknown = "3600", pauseOn: string | null = null): Promise<Dict> {
    const sp = parseSpeed(speed);
    if (pauseOn && !["watch", "warning", "tornado"].includes(pauseOn)) throw new BackendError(400, "pause_on must be one of watch, warning, tornado");
    const cat = (await this.datasets()).datasets as Dict;
    const names = Object.keys(cat).sort();
    if (!dataset) throw new BackendError(400, `dataset is required; available: [${names.map(pyRepr).join(", ")}]`);
    if (!(dataset in cat)) throw new BackendError(400, `unknown dataset ${pyRepr(dataset)}; available: [${names.map(pyRepr).join(", ")}]`);
    let tape = this.tapes.get(dataset);
    if (!tape) {
      this.tapes.set(dataset, (tape = this.load(dataset) as Promise<Tape>));
      tape.catch(() => this.tapes.delete(dataset));
    }
    const t = await tape;
    const key = normSymbol(t.symbol);
    this.sessions.get(key)?.stop();
    const s = new Session(t, sp, pauseOn || null, this);
    this.sessions.set(key, s);
    this.latest = key;
    this.ensureTimer();
    s.advance(0); // the first tick is free; at max speed this runs to the pause point or the end
    return {
      started: true, symbol: s.symbol, kind: s.kind, meta: { ...(cat[dataset] as Dict), dataset },
      speed: s.speed, bar_seconds: s.barSeconds, warmup_bars: t.warmup_bars,
    };
  }

  async live(): Promise<never> {
    throw new BackendError(501, `Live Binance ${LOCAL_ONLY}.`);
  }

  async yfinance(): Promise<never> {
    throw new BackendError(501, `yfinance ${LOCAL_ONLY}.`);
  }

  async pause(symbol?: string | null): Promise<Dict> {
    return { pausing: this.need(symbol).pause() };
  }

  async resume(symbol?: string | null): Promise<Dict> {
    return { resumed: this.need(symbol).resume() };
  }

  async speed(symbol: string | null | undefined, speed: unknown): Promise<Dict> {
    return this.need(symbol).setSpeed(parseSpeed(speed));
  }

  async stop(symbol?: string | null): Promise<Dict> {
    const s = this.get(symbol);
    s?.stop();
    return { stopped: s !== null };
  }

  async score(symbol?: string | null): Promise<Dict> {
    return this.need(symbol).snapshot();
  }

  async history(symbol?: string | null, n = 500): Promise<Dict> {
    return this.need(symbol).series(n);
  }

  async alerts(): Promise<Dict> {
    return { alerts: [...this.alertLog], suppressed_by_debounce: this.suppressed, debounce_s: 300.0 };
  }

  async exposure(body: Dict = {}): Promise<Dict> {
    const positions = body.positions ?? [];
    if (!Array.isArray(positions)) throw new BackendError(422, "positions must be a list of {asset, size, currency?, entry_price?}");
    if (positions.length > 1000) throw new BackendError(422, "at most 1000 positions");
    const lookup = (asset: string) => (asset ? (this.get(asset)?.regime() ?? null) : null);
    const res = computeExposure(positions, lookup, body.horizon_bars ?? 60, body.confidence ?? 0.99);
    res.known_assets = [...this.sessions.keys()].sort();
    return res;
  }

  async simulate(p: Dict = {}): Promise<Dict> {
    const sess = this.need((p.symbol || p.asset) as string | null);
    const reg = sess.regime();
    let value = (p.value ?? null) as number | null;
    if (value === null && p.size !== null && p.size !== undefined && reg.price) {
      value = Number(p.size) * reg.price;
      if (Number.isNaN(value)) throw new BackendError(422, "size must be a number");
    }
    const res = simulate(reg, value, p);
    Object.assign(res, { symbol: sess.symbol, level: sess.snapshot().level, currency: reportCurrency(sess.symbol) });
    return res;
  }

  /** The /stream endpoint: a snapshot whenever the session changed (checked every `interval` s), a heartbeat
   *  at least every 2 s, and a final `end` event. Same contract as an EventSource. */
  stream(symbol?: string | null, interval = 0.25): StreamLike {
    let ends: (() => void)[] = [];
    let lastV = -1, lastEmit = -Infinity, nTr = 0, closed = false;
    const out: StreamLike = {
      onmessage: null,
      onerror: null,
      addEventListener: (_type, fn) => void ends.push(fn),
      close: () => {
        closed = true;
        clearInterval(id);
        ends = [];
      },
    };
    let sess: Session | null = null;
    const tick = () => {
      if (closed) return;
      if (!sess) {
        sess = this.get(symbol);
        if (!sess) {
          out.onerror?.();
          return;
        }
      }
      const s = this.get(sess.symbol) ?? sess;
      const now = this.now();
      if (s.version === lastV && now - lastEmit < 2.0) return;
      lastV = s.version;
      lastEmit = now;
      const snap = s.snapshot();
      const seen = s.transitionsSeen();
      snap.new_transitions = s.tape.transitions.slice(Math.min(nTr, seen), seen).slice(-50);
      nTr = seen;
      out.onmessage?.({ data: JSON.stringify(snap) });
      if (s.state !== "running") {
        const fns = ends;
        out.close();
        for (const fn of fns) fn();
      }
    };
    const id = setInterval(tick, interval * 1000);
    setTimeout(tick, 0);
    return out;
  }
}
