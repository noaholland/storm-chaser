import type { Display, Level } from "./types";

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const pad = (n: number) => String(n).padStart(2, "0");

/** "29 Jul 18:01" in UTC. Market data time is always shown in UTC. */
export function fmtTime(ts: number | null | undefined, withYear = false): string {
  if (ts === null || ts === undefined || !Number.isFinite(ts)) return "--";
  const d = new Date(ts * 1000);
  const day = `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]}`;
  return `${day}${withYear ? " " + d.getUTCFullYear() : ""} ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`;
}
export function fmtDay(ts: number | null | undefined): string {
  if (ts === null || ts === undefined || !Number.isFinite(ts)) return "--";
  const d = new Date(ts * 1000);
  return `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]}`;
}

/** Price with a sensible number of decimals for FX (163.494, 1.03500) and crypto (19,748.1). */
export function fmtPrice(p: number | null | undefined): string {
  if (p === null || p === undefined || !Number.isFinite(p)) return "--";
  const a = Math.abs(p);
  const dp = a >= 10000 ? 1 : a >= 1000 ? 2 : a >= 20 ? 3 : a >= 2 ? 4 : 5;
  return p.toLocaleString("en-GB", { minimumFractionDigits: dp, maximumFractionDigits: dp });
}

/** Money, compact: 1,149,318 -> "1.15m". Small values keep their digits. */
export function fmtMoney(v: number | null | undefined, signed = false): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "--";
  const a = Math.abs(v);
  const sign = v < 0 ? "-" : signed && v > 0 ? "+" : "";
  let body: string;
  if (a >= 1e9) body = (a / 1e9).toFixed(2) + "bn";
  else if (a >= 1e6) body = (a / 1e6).toFixed(2) + "m";
  else if (a >= 1e4) body = (a / 1e3).toFixed(1) + "k";
  else if (a >= 100) body = a.toFixed(0);
  else body = a.toFixed(2);
  return sign + body;
}

export function fmtPct(x: number | null | undefined, dp = 1, signed = false): string {
  if (x === null || x === undefined || !Number.isFinite(x)) return "--";
  const v = x * 100;
  return `${signed && v > 0 ? "+" : ""}${v.toFixed(dp)}%`;
}

/** Duration of data time: 3600 -> "1 h", 86400 -> "1 d". */
export function fmtSpan(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return "--";
  if (seconds < 90) return `${Math.round(seconds)} s`;
  if (seconds < 5400) return `${Math.round(seconds / 60)} min`;
  if (seconds < 86400 * 2) return `${+(seconds / 3600).toFixed(1)} h`;
  return `${+(seconds / 86400).toFixed(1)} d`;
}

export const LEVELS: Level[] = ["green", "watch", "warning", "tornado"];
export const isLevel = (x: string | null | undefined): x is Level => !!x && (LEVELS as string[]).includes(x);

export const LEVEL_LABEL: Record<Display, string> = {
  green: "Clear",
  watch: "Watch",
  warning: "Warning",
  tornado: "Tornado",
  warming_up: "Warming up",
  stale: "Feed stale",
  no_data: "Waiting for data",
  ended: "No reading",
  error: "Feed error",
  offline: "API offline",
  idle: "No feed",
};

/** Radar reflectivity palette: green, yellow, orange, magenta. Chosen for projector contrast on near-black. */
export const LEVEL_COLOR: Record<Display, string> = {
  green: "#3ddc84",
  watch: "#ffd23f",
  warning: "#ff8a1f",
  tornado: "#ff3d81",
  warming_up: "#7fb4c9",
  stale: "#b9a27a",
  no_data: "#7fb4c9",
  ended: "#8a9a93",
  error: "#ff5c5c",
  offline: "#ff5c5c",
  idle: "#8a9a93",
};

export const COMPONENT_LABEL: Record<string, string> = {
  vol: "Volatility",
  jump: "Jumps",
  drawdown: "Drawdown speed",
  spread: "Bid/ask spread",
  curve: "Forward curve",
};
