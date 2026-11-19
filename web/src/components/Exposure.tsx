import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { Storm } from "../useStorm";
import type { Exposure, Position } from "../types";
import { fmtMoney, fmtPrice, fmtSpan } from "../format";

const STORE = "stormchaser.positions.v1";

export function loadPositions(): Position[] {
  try {
    const raw = JSON.parse(window.localStorage.getItem(STORE) || "[]");
    if (!Array.isArray(raw)) return [];
    return raw.filter((p) => p && typeof p.asset === "string" && Number.isFinite(p.size)).slice(0, 200);
  } catch {
    return []; // corrupt or blocked storage is an empty book, not a crash
  }
}
export function savePositions(p: Position[]): void {
  try {
    window.localStorage.setItem(STORE, JSON.stringify(p));
  } catch {
    /* private mode: keep them in memory only */
  }
}

/** "1m", "250k", "-2.5m", "1,000,000" -> number; null if it is not a size. */
export function parseSize(text: string): number | null {
  const m = text.trim().toLowerCase().replace(/[, _]/g, "").match(/^([+-]?\d*\.?\d+)(k|m|mm|bn|b)?$/);
  if (!m) return null;
  const mult = { k: 1e3, m: 1e6, mm: 1e6, bn: 1e9, b: 1e9 }[m[2] as "k"] ?? 1;
  const v = Number(m[1]) * mult;
  return Number.isFinite(v) ? v : null;
}

export interface ExposureState {
  data: Exposure | null;
  error: string | null;
}

/** Re-prices the book against the live regime: on every edit, and about once a second while data flows. */
export function useExposure(storm: Storm, positions: Position[], horizon: number, confidence: number): ExposureState {
  const [state, setState] = useState<ExposureState>({ data: null, error: null });
  const [beat, setBeat] = useState(0);
  const { apiState, snap, stream } = storm;
  const flowing = apiState === "up" && stream === "open" && !!snap && snap.state !== "paused" && snap.state !== "ended";
  useEffect(() => {
    if (!flowing) return;
    const id = window.setInterval(() => setBeat((b) => b + 1), 1200);
    return () => window.clearInterval(id);
  }, [flowing]);
  const seq = useRef(0);
  const key = JSON.stringify(positions.map((p) => [p.asset, p.size, p.entry_price]));
  const settle = `${storm.symbol}|${snap?.session.started}|${snap?.state}`;
  useEffect(() => {
    if (apiState !== "up") return;
    if (positions.length === 0) {
      setState({ data: null, error: null });
      return;
    }
    const mine = ++seq.current;
    api
      .exposure(positions, horizon, confidence)
      .then((d) => mine === seq.current && setState({ data: d, error: null }))
      .catch((e) => mine === seq.current && setState((s) => ({ data: s.data, error: e instanceof Error ? e.message : String(e) })));
  }, [apiState, key, horizon, confidence, beat, settle]); // eslint-disable-line react-hooks/exhaustive-deps
  return state;
}

interface Props {
  storm: Storm;
  positions: Position[];
  setPositions: (p: Position[]) => void;
  exposure: ExposureState;
  horizon: number;
  setHorizon: (n: number) => void;
  confidence: number;
  setConfidence: (n: number) => void;
}

export function ExposurePanel({ storm, positions, setPositions, exposure, horizon, setHorizon, confidence, setConfidence }: Props) {
  const { symbol, snap, apiState } = storm;
  const [asset, setAsset] = useState("");
  const [size, setSize] = useState("");
  const [entry, setEntry] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const bar = snap?.session.bar_seconds;
  const known = exposure.data?.known_assets ?? Object.keys(storm.health?.sessions ?? {});

  const add = (a: string, sz: number, en?: number) =>
    setPositions([...positions, { id: `${Date.now()}-${Math.random().toString(36).slice(2, 7)}`, asset: a, size: sz, entry_price: en }]);

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    const a = (asset || symbol || "").trim().toUpperCase();
    const sz = parseSize(size);
    const en = entry.trim() === "" ? undefined : Number(entry.replace(/,/g, ""));
    if (!a) return setErr("Name the asset, for example USDJPY.");
    if (sz === null) return setErr("Size must be a number, for example 1m or -250k (negative is short).");
    if (sz === 0) return setErr("A zero-size position has no risk. Enter a size.");
    if (en !== undefined && !(Number.isFinite(en) && en > 0)) return setErr("Entry price must be a positive number, or left empty.");
    if (positions.length >= 200) return setErr("That is enough positions for one demo.");
    setErr(null);
    add(a, sz, en);
    setSize("");
    setEntry("");
  };

  const rows = exposure.data?.positions ?? [];
  const totals = Object.entries(exposure.data?.totals ?? {});
  const unknown = rows.filter((r) => r.status === "unknown_asset").length;

  return (
    <div className="panel-body" data-testid="exposure">
      {positions.length === 0 ? (
        <div className="empty" data-testid="exposure-empty">
          <b>No positions yet.</b> Add one below to see what this weather costs you: value at risk, expected shortfall
          and stressed loss, now against a calm market.
          {symbol && (
            <div style={{ marginTop: 10 }}>
              <button className="btn btn-sm" onClick={() => add(symbol, 1_000_000)} data-testid="quick-add">
                Add long 1m {symbol}
              </button>
            </div>
          )}
        </div>
      ) : (
        <>
          {totals.length === 0 && (
            <div className="empty" data-testid="exposure-norisk">
              {apiState !== "up"
                ? "Risk numbers need the API."
                : unknown === rows.length && rows.length > 0
                  ? "None of these assets has a feed, so there is nothing to price yet."
                  : exposure.error || "Pricing the book."}
            </div>
          )}
          {totals.map(([ccy, t]) => {
            const ratio = t.var_baseline > 0 ? t.var / t.var_baseline : null;
            const max = Math.max(t.var, t.var_baseline, 1e-12);
            const blank = t.incomplete && t.var === 0;
            return (
              <div key={ccy} data-testid={`totals-${ccy}`}>
                <div className="kpis">
                  <div className="kpi" data-tone="hot">
                    <label>VaR {Math.round(confidence * 100)}%, now</label>
                    <b data-testid="kpi-var">
                      {t.incomplete && t.var === 0 ? "--" : fmtMoney(t.var)}
                      <small>{ccy}</small>
                    </b>
                  </div>
                  <div className="kpi">
                    <label>Expected shortfall</label>
                    <b data-testid="kpi-es">
                      {t.incomplete && t.es === 0 ? "--" : fmtMoney(t.es)}
                      <small>{ccy}</small>
                    </b>
                  </div>
                  <div className="kpi">
                    <label>Stressed loss</label>
                    <b data-testid="kpi-stress">
                      {t.incomplete && t.stressed_loss === 0 ? "--" : fmtMoney(t.stressed_loss)}
                      <small>{ccy}</small>
                    </b>
                  </div>
                </div>
                <div className="versus" data-testid="versus">
                  <div className="versus-row">
                    <span>Calm market</span>
                    <div className="versus-bar">
                      <i style={{ width: `${(100 * t.var_baseline) / max}%`, background: "var(--green)" }} />
                    </div>
                    <b>{blank ? "--" : fmtMoney(t.var_baseline)}</b>
                  </div>
                  <div className="versus-row">
                    <span>Right now</span>
                    <div className="versus-bar">
                      <i style={{ width: `${(100 * t.var) / max}%`, background: "var(--lvl)" }} />
                    </div>
                    <b>{blank ? "--" : fmtMoney(t.var)}</b>
                  </div>
                  <div className="versus-note" data-testid="versus-note">
                    {t.incomplete
                      ? "Some positions are on a feed that is still warming up, so their risk is not in these totals yet."
                      : ratio === null
                        ? "No calm baseline yet."
                        : <>VaR is <b>{ratio.toFixed(1)}x</b> its calm-market level. Book value {fmtMoney(t.value)} {ccy}{t.pnl ? <>, P&amp;L <span className={t.pnl >= 0 ? "pos" : "neg"}>{fmtMoney(t.pnl, true)}</span></> : null}.</>}
                  </div>
                </div>
              </div>
            );
          })}
          <table className="table" data-testid="positions">
            <thead>
              <tr>
                <th>Asset</th>
                <th>Size</th>
                <th>Value</th>
                <th>P&amp;L</th>
                <th>VaR now</th>
                <th>VaR calm</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {positions.map((p, i) => {
                const r = rows[i];
                const bad = r && (r.status === "unknown_asset" || r.status === "error");
                return (
                  <tr key={p.id} data-status={r?.status ?? "pending"} data-testid="position-row">
                    <td>{p.asset}</td>
                    <td>{fmtMoney(p.size, true)}</td>
                    {bad ? (
                      <td colSpan={4} className="rowmsg" data-testid="row-problem">
                        {r.status === "unknown_asset"
                          ? `No feed for ${p.asset}. Start one from the picker, or remove this row. It is left out of the totals.`
                          : r.error}
                      </td>
                    ) : (
                      <>
                        <td>{r?.value !== undefined ? `${fmtMoney(r.value)} ${r.currency ?? ""}` : "--"}</td>
                        <td className={r?.pnl ? (r.pnl >= 0 ? "pos" : "neg") : ""}>{r?.pnl !== null && r?.pnl !== undefined ? fmtMoney(r.pnl, true) : "--"}</td>
                        <td>{r?.status === "warming_up" ? "warming up" : fmtMoney(r?.var)}</td>
                        <td>{r?.status === "warming_up" ? "--" : fmtMoney(r?.var_baseline)}</td>
                      </>
                    )}
                    <td>
                      <button className="x" aria-label={`Remove ${p.asset}`} onClick={() => setPositions(positions.filter((q) => q.id !== p.id))} data-testid="remove-position">
                        ×
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </>
      )}

      <form className="addrow" onSubmit={submit} data-testid="add-position" noValidate>
        <div className="field">
          <label htmlFor="pos-asset">Asset</label>
          <input id="pos-asset" list="known-assets" placeholder={symbol ?? "USDJPY"} value={asset} onChange={(e) => setAsset(e.target.value)} autoComplete="off" data-testid="pos-asset" />
          <datalist id="known-assets">
            {known.map((k) => (
              <option key={k} value={k} />
            ))}
          </datalist>
        </div>
        <div className="field">
          <label htmlFor="pos-size">Size, units (- is short)</label>
          <input id="pos-size" placeholder="1m" value={size} onChange={(e) => {
            setSize(e.target.value);
            setErr(null);
          }} aria-invalid={err !== null && parseSize(size) === null} autoComplete="off" data-testid="pos-size" />
        </div>
        <div className="field">
          <label htmlFor="pos-entry">Entry price (optional)</label>
          <input id="pos-entry" placeholder={snap?.price ? fmtPrice(snap.price) : ""} value={entry} onChange={(e) => setEntry(e.target.value)} autoComplete="off" data-testid="pos-entry" />
        </div>
        <button className="btn btn-sm btn-primary" type="submit" data-testid="pos-add" style={{ height: 34 }}>
          Add
        </button>
      </form>
      <div className="form-error" role="alert" data-testid="pos-error">
        {err}
      </div>
      <div className="row small muted" style={{ marginTop: "auto" }}>
        Horizon
        <select className="inline-select" value={horizon} onChange={(e) => setHorizon(Number(e.target.value))} aria-label="Risk horizon in bars" data-testid="horizon">
          {[15, 60, 240, 1440].map((h) => (
            <option key={h} value={h}>
              {h} bars{bar ? ` (${fmtSpan(h * bar)})` : ""}
            </option>
          ))}
        </select>
        Confidence
        <select className="inline-select" value={confidence} onChange={(e) => setConfidence(Number(e.target.value))} aria-label="Confidence level" data-testid="confidence">
          <option value={0.95}>95%</option>
          <option value={0.99}>99%</option>
        </select>
        {positions.length > 0 && (
          <button className="btn btn-sm btn-ghost" style={{ marginLeft: "auto", height: 28, fontSize: 14 }} onClick={() => setPositions([])} data-testid="clear-positions">
            Clear all
          </button>
        )}
      </div>
    </div>
  );
}
