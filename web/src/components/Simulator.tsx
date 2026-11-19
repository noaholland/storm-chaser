import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { Storm } from "../useStorm";
import type { Position, SimResult } from "../types";
import { fmtMoney, fmtPct, fmtPrice, fmtSpan, fmtTime } from "../format";

const norm = (s: string) => s.toUpperCase().replace(/[^A-Z0-9]/g, "");
/** Net size the book holds in the streamed asset ("BTC" counts for BTCUSDT). */
export function netSize(positions: Position[], symbol: string | null): number | null {
  if (!symbol) return null;
  const sym = norm(symbol);
  const hit = positions.filter((p) => {
    const a = norm(p.asset);
    return a === sym || (a.length >= 3 && sym.startsWith(a));
  });
  if (hit.length === 0) return null;
  const net = hit.reduce((s, p) => s + p.size, 0);
  return net === 0 ? null : net;
}

interface Props {
  storm: Storm;
  positions: Position[];
  /** bumped by the freeze callout to request a run */
  trigger: number;
}

export function SimulatorPanel({ storm, positions, trigger }: Props) {
  const { symbol, snap, apiState } = storm;
  const [steps, setSteps] = useState(60);
  const [method, setMethod] = useState("garch");
  const [res, setRes] = useState<SimResult | null>(null);
  const [ranAt, setRanAt] = useState<number | null>(null);
  const [ranSize, setRanSize] = useState<number | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const size = netSize(positions, symbol);
  const seq = useRef(0);

  const run = useCallback(async () => {
    if (!symbol) return;
    const mine = ++seq.current;
    setLoading(true);
    setMsg(null);
    try {
      const r = await api.simulate(symbol, size, steps, method);
      if (mine !== seq.current) return;
      if (r.status !== "ok") {
        setRes(null);
        setMsg(
          r.status === "warming_up"
            ? "The feed is still warming up. The simulator needs a calibrated regime; try again in a moment."
            : r.error || "The simulator has no valid price for this asset yet.",
        );
      } else {
        setRes(r);
        setRanAt(snap?.ts ?? null);
        setRanSize(size);
      }
    } catch (e) {
      if (mine === seq.current) setMsg(e instanceof Error ? e.message : String(e));
    } finally {
      if (mine === seq.current) setLoading(false);
    }
  }, [symbol, size, steps, method, snap?.ts]);

  const runRef = useRef(run);
  runRef.current = run;
  useEffect(() => {
    if (trigger > 0) void runRef.current();
  }, [trigger]);
  // a fan from another feed is misleading: drop it when the session changes
  const sessionKey = `${symbol}|${snap?.session.started ?? ""}`;
  useEffect(() => {
    seq.current++;
    setRes(null);
    setMsg(null);
    setLoading(false);
  }, [sessionKey]);

  const bar = snap?.session.bar_seconds;
  const usePnl = ranSize !== null; // the fan is read the way it was run: P&L for a position, price for one unit
  const moved = !!res && ranAt !== null && snap?.ts !== null && snap?.ts !== undefined && snap.ts !== ranAt;
  const disabled = !symbol || apiState !== "up" || loading;

  return (
    <div className="panel-body" data-testid="simulator">
      <div className="row" style={{ marginBottom: 8 }}>
        <button className="btn btn-sm btn-primary" onClick={() => void run()} disabled={disabled} data-testid="run-sim">
          {loading ? "Simulating" : res ? "Run again" : "Run simulator"}
        </button>
        <select className="inline-select" value={steps} onChange={(e) => setSteps(Number(e.target.value))} aria-label="Simulation horizon" data-testid="sim-steps">
          {[15, 60, 240, 1440].map((h) => (
            <option key={h} value={h}>
              next {bar ? fmtSpan(h * bar) : `${h} bars`}
            </option>
          ))}
        </select>
        <select className="inline-select" value={method} onChange={(e) => setMethod(e.target.value)} aria-label="Simulation model" data-testid="sim-method">
          <option value="garch">GARCH + jumps</option>
          <option value="bootstrap">Bootstrap</option>
        </select>
      </div>

      {!res && (
        <div className="empty" data-testid="sim-empty">
          {msg ? (
            <span data-testid="sim-message">{msg}</span>
          ) : !symbol ? (
            "Start a feed, then run 2,000 paths from the current regime to see where the next hour could go."
          ) : (
            <>
              Runs <b>2,000 Monte Carlo paths</b> from the regime the engine sees right now: storm volatility that decays
              back to calm, plus jumps. {size === null ? `No ${symbol} position in the book, so the fan is per 1 unit.` : ""}
            </>
          )}
        </div>
      )}

      {res && (
        <>
          {moved && (
            <div className="banner note" style={{ margin: "0 0 8px" }} data-testid="sim-outdated">
              <div className="grow">
                This fan was run at {fmtTime(ranAt)} UTC. The market has moved since: run it again for the current regime.
              </div>
            </div>
          )}
          <div style={{ opacity: moved ? 0.45 : 1 }}>
            <Fan res={res} usePnl={usePnl} />
          </div>
          <div className="fan-stats">
            <div className="kpi ploss" data-tone="hot">
              <label>P(loss)</label>
              <b data-testid="p-loss">{fmtPct(res.terminal.prob_loss, 0)}</b>
            </div>
            <div className="kpi">
              <label>5th pct</label>
              <b data-testid="sim-p5" className="neg">{usePnl ? fmtMoney(res.terminal.pnl_p5, true) : fmtPrice(res.fan.price_p5.at(-1))}</b>
            </div>
            <div className="kpi">
              <label>Median</label>
              <b data-testid="sim-p50">{usePnl ? fmtMoney(res.terminal.pnl_p50, true) : fmtPrice(res.fan.price_p50.at(-1))}</b>
            </div>
            <div className="kpi">
              <label>95th pct</label>
              <b data-testid="sim-p95" className="pos">{usePnl ? fmtMoney(res.terminal.pnl_p95, true) : fmtPrice(res.fan.price_p95.at(-1))}</b>
            </div>
          </div>
          <div className="small muted" style={{ marginTop: 8 }} data-testid="sim-caption">
            {res.n_paths.toLocaleString("en-GB")} paths over {fmtSpan(res.horizon_seconds)}, {res.method}, from the{" "}
            {res.level.replace("_", " ")} regime at {fmtTime(ranAt)} UTC.{" "}
            {usePnl
              ? `P&L in ${res.currency} on ${fmtMoney(ranSize, true)} ${res.symbol}; tail average ${fmtMoney(res.terminal.expected_shortfall_5, true)}.`
              : `Price of 1 ${res.symbol}. Add a ${res.symbol} position to see your own P&L.`}
            {res.clamped.length > 0 && ` Inputs adjusted: ${res.clamped.join("; ")}.`}
            {msg && <span className="neg"> {msg}</span>}
          </div>
        </>
      )}
    </div>
  );
}

function Fan({ res, usePnl }: { res: SimResult; usePnl: boolean }) {
  const f = res.fan;
  const p5 = usePnl ? f.pnl_p5 : f.price_p5;
  const p50 = usePnl ? f.pnl_p50 : f.price_p50;
  const p95 = usePnl ? f.pnl_p95 : f.price_p95;
  const origin = usePnl ? 0 : res.start_price;
  const xs = [0, ...f.step];
  const lo5 = [origin, ...p5], mid = [origin, ...p50], hi95 = [origin, ...p95];
  const W = 520, H = 230, L = 8, R = 86, T = 14, B = 26;
  const ymin = Math.min(...lo5, origin), ymax = Math.max(...hi95, origin);
  const pad = (ymax - ymin || Math.abs(origin) * 0.001 || 1) * 0.08;
  const x = (s: number) => L + (s / res.steps) * (W - L - R);
  const y = (v: number) => T + ((ymax + pad - v) / (ymax - ymin + 2 * pad)) * (H - T - B);
  const line = (arr: number[]) => arr.map((v, i) => `${i ? "L" : "M"}${x(xs[i]).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  const band = `${line(hi95)} ${[...lo5].reverse().map((v, i) => `L${x(xs[xs.length - 1 - i]).toFixed(1)},${y(v).toFixed(1)}`).join(" ")} Z`;
  const label = (v: number) => (usePnl ? fmtMoney(v, true) : fmtPrice(v));
  const xe = x(res.steps);
  // keep the three end labels from colliding when the fan is narrow
  const ys = [y(hi95.at(-1)!), y(mid.at(-1)!), y(lo5.at(-1)!)];
  if (ys[1] - ys[0] < 15) ys[0] = ys[1] - 15;
  if (ys[2] - ys[1] < 15) ys[2] = ys[1] + 15;
  const ticks = [0, 0.5, 1].map((q) => q * res.steps);
  return (
    <div className="fan" data-testid="fan">
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Simulated 5th, 50th and 95th percentile paths">
        <path d={band} fill="var(--lvl)" fillOpacity="0.2" stroke="none" data-testid="fan-band" />
        <path d={line(hi95)} fill="none" stroke="var(--lvl)" strokeWidth="1.5" strokeOpacity="0.9" />
        <path d={line(lo5)} fill="none" stroke="var(--lvl)" strokeWidth="1.5" strokeOpacity="0.9" />
        <line x1={L} x2={xe} y1={y(origin)} y2={y(origin)} stroke="rgba(236,247,241,0.5)" strokeDasharray="4 4" strokeWidth="1" />
        <path d={line(mid)} fill="none" stroke="#fff" strokeWidth="2.5" data-testid="fan-median" />
        <circle cx={x(0)} cy={y(origin)} r="4" fill="#fff" />
        {[
          ["95%", hi95.at(-1)!, ys[0], "#3ddc84"],
          ["50%", mid.at(-1)!, ys[1], "#fff"],
          ["5%", lo5.at(-1)!, ys[2], "#ff5c5c"],
        ].map(([name, v, yy, col]) => (
          <text key={name as string} x={xe + 6} y={(yy as number) + 4} fontSize="12.5" fontFamily="var(--mono)" fill={col as string}>
            {name} {label(v as number)}
          </text>
        ))}
        {ticks.map((s) => (
          <text key={s} x={x(s)} y={H - 8} fontSize="11.5" fontFamily="var(--mono)" fill="#7d998e" textAnchor={s === 0 ? "start" : s === res.steps ? "end" : "middle"}>
            {s === 0 ? "now" : `+${fmtSpan(s * res.bar_seconds)}`}
          </text>
        ))}
      </svg>
    </div>
  );
}
