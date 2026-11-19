// Thin client for the Storm Chaser API. No engine logic lives in the browser.
// A static build (`npm run build:static`, VITE_STATIC=1) has no server: the same calls are answered by
// src/static/backend.ts, which plays back replays recorded from the Python engine.
import type { AlertRec, Catalogue, Exposure, Health, History, Position, SimResult, Snapshot } from "./types";
import { BackendError, StaticBackend } from "./static/backend.ts";
import type { StreamLike } from "./static/backend.ts";

/** True on the backend-free demo site (GitHub Pages). */
export const STATIC: boolean = import.meta.env.VITE_STATIC === "1";

const override = new URLSearchParams(window.location.search).get("api") || import.meta.env.VITE_API;
/** Same origin when served by FastAPI under /ui/; `?api=` or VITE_API points a dev build elsewhere. */
export const API_BASE: string = (override || window.location.origin).replace(/\/$/, "");

export class ApiError extends Error {
  status: number;
  /** true when the server could not be reached at all (as opposed to answering with an error) */
  network: boolean;
  constructor(message: string, status: number, network = false) {
    super(message);
    this.status = status;
    this.network = network;
  }
}

const backend = STATIC
  ? new StaticBackend(async (name) => {
      const res = await fetch(`${import.meta.env.BASE_URL}data/${name}.json`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return res.json();
    })
  : null;

/** Static build: run a call against the in-browser backend, with the errors the HTTP client would raise. */
async function local<T>(fn: (b: StaticBackend) => Promise<unknown>): Promise<T> {
  try {
    return (await fn(backend!)) as T;
  } catch (e) {
    if (e instanceof BackendError) throw new ApiError(e.message, e.status);
    throw new ApiError("could not load the replay data for this page; check the connection and try again", 0);
  }
}

async function call<T>(path: string, init: RequestInit = {}, timeoutMs = 8000): Promise<T> {
  const ctl = new AbortController();
  const timer = window.setTimeout(() => ctl.abort(), timeoutMs);
  let res: Response;
  try {
    res = await fetch(API_BASE + path, { ...init, signal: ctl.signal });
  } catch {
    throw new ApiError(`cannot reach the API at ${API_BASE}`, 0, true);
  } finally {
    window.clearTimeout(timer);
  }
  let body: unknown = null;
  try {
    body = await res.json();
  } catch {
    /* non-JSON error page */
  }
  // /simulate answers 409 with a useful JSON body (status: warming_up); let the caller read it
  if (!res.ok && !(body && typeof body === "object" && "status" in (body as object))) {
    const detail = body && typeof body === "object" && "detail" in body ? (body as { detail: unknown }).detail : null;
    const msg = typeof detail === "string" ? detail : detail ? JSON.stringify(detail) : `HTTP ${res.status}`;
    throw new ApiError(msg, res.status);
  }
  return body as T;
}

const qs = (params: Record<string, string | number | null | undefined>) => {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== null && v !== undefined && v !== "") p.set(k, String(v));
  const s = p.toString();
  return s ? `?${s}` : "";
};
const post = (body?: unknown): RequestInit =>
  body === undefined
    ? { method: "POST" }
    : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };

const http = {
  health: () => call<Health>("/health", {}, 2500),
  datasets: () => call<Catalogue>("/datasets"),
  score: (symbol?: string) => call<Snapshot>("/score" + qs({ symbol })),
  history: (symbol: string, n = 1200) => call<History>("/history" + qs({ symbol, n })),
  alerts: () => call<{ alerts: AlertRec[]; suppressed_by_debounce: number }>("/alerts"),
  replay: (dataset: string, speed: number | "max", pauseOn: string | null) =>
    call<{ symbol: string }>("/replay" + qs({ dataset, speed, pause_on: pauseOn }), post(), 30000),
  live: (symbol: string) => call<{ symbol: string }>("/live" + qs({ symbol }), post()),
  yfinance: (symbol: string) => call<{ symbol: string }>("/yfinance" + qs({ symbol }), post(), 45000),
  pause: (symbol: string) => call<{ pausing: boolean }>("/pause" + qs({ symbol }), post()),
  resume: (symbol: string) => call<{ resumed: boolean }>("/resume" + qs({ symbol }), post()),
  speed: (symbol: string, speed: number | "max") => call<{ speed: number }>("/speed" + qs({ symbol, speed }), post()),
  stop: (symbol: string) => call<{ stopped: boolean }>("/stop" + qs({ symbol }), post()),
  exposure: (positions: Position[], horizonBars: number, confidence: number) =>
    call<Exposure>(
      "/exposure",
      post({
        positions: positions.map((p) => ({ asset: p.asset, size: p.size, entry_price: p.entry_price })),
        horizon_bars: horizonBars,
        confidence,
      }),
    ),
  simulate: (symbol: string, size: number | null, steps: number, method: string) =>
    call<SimResult>("/simulate", post({ symbol, size, steps, method, n_paths: 2000 }), 30000),
  /** The SSE stream of snapshots. */
  openStream: (symbol: string): StreamLike => new EventSource(API_BASE + "/stream" + qs({ symbol })) as unknown as StreamLike,
};

const offline: typeof http = {
  health: () => local((b) => b.health()),
  datasets: () => local((b) => b.datasets()),
  score: (symbol) => local((b) => b.score(symbol)),
  history: (symbol, n = 1200) => local((b) => b.history(symbol, n)),
  alerts: () => local((b) => b.alerts()),
  replay: (dataset, speed, pauseOn) => local((b) => b.replay(dataset, speed, pauseOn)),
  live: () => local((b) => b.live()),
  yfinance: () => local((b) => b.yfinance()),
  pause: (symbol) => local((b) => b.pause(symbol)),
  resume: (symbol) => local((b) => b.resume(symbol)),
  speed: (symbol, speed) => local((b) => b.speed(symbol, speed)),
  stop: (symbol) => local((b) => b.stop(symbol)),
  exposure: (positions, horizonBars, confidence) =>
    local((b) =>
      b.exposure({
        positions: positions.map((p) => ({ asset: p.asset, size: p.size, entry_price: p.entry_price })),
        horizon_bars: horizonBars,
        confidence,
      }),
    ),
  simulate: (symbol, size, steps, method) => local((b) => b.simulate({ symbol, size, steps, method, n_paths: 2000 })),
  openStream: (symbol) => backend!.stream(symbol),
};

export const api = STATIC ? offline : http;
