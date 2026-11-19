// Shapes of the Storm Chaser API responses (see assets/samples/*.json).

export type Level = "green" | "watch" | "warning" | "tornado";
/** What the backend puts in `level`: a real level, or the reason there is none. */
export type Reading = Level | "warming_up" | "stale" | "no_data" | "ended" | "error";
/** What the UI shows: adds states the browser detects by itself. */
export type Display = Reading | "offline" | "idle";

export interface Transition {
  ts: number;
  frm: string;
  to: string;
  score: number;
  price: number;
  why: string;
  kind: string;
  components: Record<string, number>;
  carry_unwind: boolean;
}

export interface Carry {
  curve: Record<string, number>;
  median: number | null;
  earning_side: string | null;
  dislocation_z: number | null;
  dislocation_peak_z: number | null;
  unwind_score: number | null;
  unwind: boolean;
}

export interface Ahead {
  from_ts: number;
  from_price: number;
  end_ts: number;
  low: number;
  low_ts: number;
  pct_to_low: number;
  high: number;
  high_ts: number;
  pct_to_high: number;
}

export interface Idea {
  kind: "hedge" | "long_vol" | "carry";
  title: string;
  detail: string;
}

export interface Snapshot {
  symbol: string;
  state: "live" | "warming_up" | "stale" | "no_data" | "paused" | "ended" | "error";
  level: Reading;
  score: number | null;
  last_score: number | null;
  last_level: string;
  peak_score: number | null;
  kind: "calm" | "squall" | "storm";
  storm_heat_min: number;
  carry: Carry | null;
  price: number | null;
  bid: number | null;
  ask: number | null;
  ts: number | null;
  components: Record<string, number>;
  features: Record<string, number>;
  vol: {
    sigma_bar_fast: number | null;
    sigma_bar_base: number | null;
    annualised_fast: number | null;
    annualised_base: number | null;
  };
  warmup: { have: number; need: number };
  feed: {
    age_s: number | null;
    stale_after_s: number;
    accepted: number;
    rejected: Record<string, number>;
    gaps: number;
    pending_outlier: boolean;
  };
  session: {
    kind: "replay" | "live" | "yfinance";
    state: string;
    error: string | null;
    started: number;
    dataset: string | null;
    speed: number | null;
    progress: number | null;
    reconnects: number | null;
    feed_error: string | null;
    pause_on: string | null;
    pause_reason: string | null;
    ahead: Ahead | null;
    title: string | null;
    bar_seconds: number;
    curve_tenors: string[];
  };
  playbook: {
    stay_safe: string;
    early_warning: string | null;
    opportunity: string | null;
    disclaimer: string;
    ideas: Idea[];
  };
  new_transitions?: Transition[];
}

export interface History {
  symbol: string;
  points: number;
  stride: number;
  ts: number[];
  price: number[];
  score: (number | null)[];
  level: string[];
  transitions: Transition[];
}

export interface Health {
  status: "ok" | "degraded";
  uptime_s: number;
  sessions: Record<string, { kind: string; state: string; feed: string; error: string | null; progress: number | null }>;
  latest: string | null;
  notifiers: string[];
}

export interface DatasetMeta {
  symbol: string;
  title: string;
  source: string;
  description: string;
}

export interface Catalogue {
  datasets: Record<string, DatasetMeta>;
  yfinance_presets: Record<string, string>;
  live: string[];
  speed: { min: number; max: number };
  scoring?: Scoring;
}

/** How the score is built, as configured in the engine (StormConfig). */
export interface Scoring {
  sensors: { key: string; weight: number; core: boolean }[];
  mean_share: number;
  worst_share: number;
  aux_cap: number;
  thresholds: { watch: number; warning: number; tornado: number };
  hysteresis: number;
  cooldown_bars: number;
  squall_min: number;
  heat_halflife_min: number;
}

export interface AlertRec {
  symbol: string;
  kind: string;
  text: string;
  wall_time: number;
  level: string;
  score?: number;
  price?: number;
  data_ts?: number | null;
  age_s?: number;
}

export interface Position {
  id: string;
  asset: string;
  size: number;
  entry_price?: number;
}

export interface ExposureRow {
  index: number;
  asset: string | null;
  status: "ok" | "unknown_asset" | "error" | "zero" | "warming_up";
  error?: string;
  note?: string;
  units?: number;
  price?: number;
  value?: number;
  currency?: string;
  pnl?: number | null;
  var?: number | null;
  es?: number | null;
  var_baseline?: number | null;
  stressed_loss?: number | null;
  vol_ratio?: number;
  level?: string;
}

export interface ExposureTotals {
  value: number;
  gross: number;
  pnl: number;
  var: number;
  var_diversified: number;
  es: number;
  var_baseline: number;
  stressed_loss: number;
  stressed_loss_pct_gross: number;
  incomplete: boolean;
}

export interface Exposure {
  horizon_bars: number;
  confidence: number;
  positions: ExposureRow[];
  totals: Record<string, ExposureTotals>;
  notes: string[];
  known_assets: string[];
}

export interface SimResult {
  status: string;
  error?: string;
  method: string;
  steps: number;
  n_paths: number;
  bar_seconds: number;
  horizon_seconds: number;
  start_price: number;
  position_value: number;
  clamped: string[];
  fan: {
    step: number[];
    price_p5: number[];
    price_p50: number[];
    price_p95: number[];
    pnl_p5: number[];
    pnl_p50: number[];
    pnl_p95: number[];
  };
  terminal: {
    pnl_p5: number;
    pnl_p50: number;
    pnl_p95: number;
    expected_shortfall_5: number;
    prob_loss: number;
    worst_drawdown_p5: number;
  };
  symbol: string;
  level: string;
  currency: string;
}
