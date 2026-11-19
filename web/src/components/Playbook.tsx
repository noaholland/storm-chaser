import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { Storm } from "../useStorm";
import type { Position } from "../types";
import type { ExposureState } from "./Exposure";
import { fmtMoney, isLevel } from "../format";

const TAG: Record<string, string> = { hedge: "Hedge", long_vol: "Long volatility", carry: "Carry" };

interface Props {
  storm: Storm;
  positions: Position[];
  exposure: ExposureState;
  horizon: number;
  confidence: number;
}

/** Pillar 4. Ideas come from the backend playbook; the hedge what-if re-prices the real book through /exposure. */
export function Playbook({ storm, positions, exposure, horizon, confidence }: Props) {
  const { snap, display, apiState } = storm;
  const [ratio, setRatio] = useState(50);
  const [hedged, setHedged] = useState<Record<string, number> | null>(null);
  const seq = useRef(0);
  const totals = exposure.data?.totals ?? {};
  const base = Object.entries(totals).filter(([, t]) => t.var > 0);
  const baseKey = base.map(([c, t]) => `${c}:${t.var.toPrecision(6)}`).join("|");

  useEffect(() => {
    if (apiState !== "up" || base.length === 0) {
      setHedged(null);
      return;
    }
    const mine = ++seq.current;
    const keep = 1 - ratio / 100;
    const id = window.setTimeout(() => {
      api
        .exposure(positions.map((p) => ({ ...p, size: p.size * keep })), horizon, confidence)
        .then((d) => {
          if (mine !== seq.current) return;
          setHedged(Object.fromEntries(Object.entries(d.totals).map(([c, t]) => [c, t.var])));
        })
        .catch(() => mine === seq.current && setHedged(null));
    }, 180);
    return () => window.clearTimeout(id);
  }, [apiState, ratio, baseKey, horizon, confidence]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!snap || display === "offline" || display === "idle") {
    return (
      <div className="panel-body" data-testid="playbook">
        <div className="empty" data-testid="playbook-empty">
          {display === "offline"
            ? "No playbook while the API is offline."
            : "The storm playbook reads the live regime. Start a feed to see a hedge and a long-volatility idea for it."}
        </div>
      </div>
    );
  }
  const pb = snap.playbook;
  const ideas = isLevel(display) ? pb.ideas ?? [] : [];
  return (
    <div className="panel-body" data-testid="playbook">
      <div className="posture" data-testid="posture">
        {pb.stay_safe}
      </div>
      {pb.opportunity && isLevel(display) && (
        <p className="small" style={{ margin: "8px 0 0", color: "var(--ink-2)" }} data-testid="opportunity">
          {pb.opportunity}
        </p>
      )}
      <div className="ideas">
        {ideas.map((i) => (
          <div className="idea" key={i.kind + i.title} data-testid="idea" data-kind={i.kind}>
            <div className="idea-top">
              <h4>{i.title}</h4>
              <span className="tag-ill" title="An illustration of the kind of trade that fits this regime. Not a recommendation.">
                ILLUSTRATIVE · {TAG[i.kind]?.toUpperCase() ?? i.kind}
              </span>
            </div>
            <p>{i.detail}</p>
          </div>
        ))}
        {ideas.length === 0 && (
          <div className="empty small" data-testid="ideas-empty">
            No trade ideas without a trustworthy reading. They return when the score does.
          </div>
        )}
      </div>
      {base.length > 0 && (
        <div className="hedge" data-testid="hedge">
          <div className="row space">
            <label htmlFor="hedge-ratio" className="small">
              What if you hedged <b className="num">{ratio}%</b> of the book?
            </label>
            <span className="tag-ill">ILLUSTRATIVE</span>
          </div>
          <input id="hedge-ratio" type="range" min={0} max={100} step={10} value={ratio} onChange={(e) => setRatio(Number(e.target.value))} data-testid="hedge-ratio" />
          {base.map(([ccy, t]) => (
            <div className="hedge-out" key={ccy} data-testid="hedge-out">
              VaR {fmtMoney(t.var)} to <b>{hedged && ccy in hedged ? fmtMoney(hedged[ccy]) : ratio === 100 ? "0.00" : "…"}</b> {ccy}
            </div>
          ))}
        </div>
      )}
      <div className="disclaimer" data-testid="disclaimer">
        Illustrative only, not advice. {pb.disclaimer}
      </div>
    </div>
  );
}
