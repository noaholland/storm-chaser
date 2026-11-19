import { useEffect, useRef, useState } from "react";
import type { Storm } from "../useStorm";
import type { Display, Scoring, Snapshot } from "../types";
import { COMPONENT_LABEL, LEVEL_COLOR, LEVEL_LABEL, fmtPct, isLevel } from "../format";

const C = 150;
const R = 138;
const A0 = 135; // gauge starts bottom-left and sweeps 270 degrees clockwise
const SPAN = 270;
const pt = (deg: number, r = R) => {
  const a = (deg * Math.PI) / 180;
  return [C + r * Math.cos(a), C + r * Math.sin(a)] as const;
};
const arc = (from: number, to: number, r = R) => {
  const [x0, y0] = pt(A0 + (SPAN * from) / 100, r);
  const [x1, y1] = pt(A0 + (SPAN * to) / 100, r);
  return `M ${x0.toFixed(2)} ${y0.toFixed(2)} A ${r} ${r} 0 ${((to - from) / 100) * SPAN > 180 ? 1 : 0} 1 ${x1.toFixed(2)} ${y1.toFixed(2)}`;
};
const ZONES: [number, number, string][] = [
  [0, 25, LEVEL_COLOR.green],
  [25, 50, LEVEL_COLOR.watch],
  [50, 75, LEVEL_COLOR.warning],
  [75, 100, LEVEL_COLOR.tornado],
];
const SWEEP: Partial<Record<Display, string>> = { green: "7s", watch: "4.5s", warning: "2.6s", tornado: "1.3s", warming_up: "9s" };

/** What the middle of the gauge says. A score is shown only when the backend vouches for it. */
function reading(display: Display, snap: Snapshot | null, retryIn: number): { score: string | null; label: string; note: string } {
  if (display === "offline")
    return { score: null, label: "API offline", note: retryIn > 0 ? `Retrying in ${retryIn} s` : "Reconnecting" };
  if (display === "idle" || !snap) return { score: null, label: "No feed", note: "Pick a feed, or jump to the storm" };
  const last = snap.last_score;
  switch (display) {
    case "warming_up":
      return { score: null, label: "Warming up", note: `${snap.warmup.have} of ${snap.warmup.need} bars. No score until there is enough history.` };
    case "no_data":
      return { score: null, label: "Waiting", note: "Connected, no ticks yet" };
    case "stale":
      return {
        score: null,
        label: "Feed stale",
        note: `No ticks for ${Math.round(snap.feed.age_s ?? 0)} s.${last !== null ? ` Last score ${Math.round(last)}, not trusted.` : ""}`,
      };
    case "error":
      return { score: null, label: "Feed error", note: snap.session.error || "The feed stopped with an error" };
    case "ended":
      return { score: null, label: "Ended", note: "Feed ended before it warmed up" };
    default: {
      const s = snap.score ?? last;
      const done = snap.state === "ended";
      const frozen = snap.state === "paused";
      return {
        score: s === null ? null : String(Math.round(s)),
        label: LEVEL_LABEL[display],
        note: done ? (snap.session.kind === "replay" ? "Replay finished. Final reading." : "Last reading. Not live.") : frozen ? "Replay frozen" : "",
      };
    }
  }
}

const KIND_TITLE = (min: number) =>
  `Squall: a short-lived alert, such as a data release. Storm: alerts that persist or cluster for more than ${min} minutes. The level (watch, warning, tornado) says how severe; squall or storm says how long it has lasted.`;

const pctW = (x: number) => `${Math.round(100 * x)}%`;
const num = (x: number) => String(+x.toFixed(2));

/** "How the score works": a button for the panel head and a popover. Every number comes from the engine's StormConfig. */
export function ScoreInfo({ scoring }: { scoring: Scoring | undefined }) {
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLSpanElement>(null);
  useEffect(() => {
    if (!open) return;
    const key = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    const away = (e: MouseEvent) => box.current && !box.current.contains(e.target as Node) && setOpen(false);
    window.addEventListener("keydown", key);
    window.addEventListener("mousedown", away);
    return () => {
      window.removeEventListener("keydown", key);
      window.removeEventListener("mousedown", away);
    };
  }, [open]);
  const total = scoring ? scoring.sensors.reduce((a, s) => a + s.weight, 0) : 0;
  const t = scoring?.thresholds;
  return (
    <span className="score-info" ref={box}>
      <button
        className="info-btn"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        aria-controls="score-info-pop"
        disabled={!scoring}
        data-testid="score-info"
        title="How the score works"
      >
        <i aria-hidden="true">i</i> How the score works
      </button>
      {open && scoring && t && (
        <div className="info-pop" id="score-info-pop" role="dialog" aria-label="How the score works" data-testid="score-info-pop">
          <h3>How the score works</h3>
          <p>Five sensors, each scaled 0 to 1 against this market's own calm baseline:</p>
          <ul className="info-sensors">
            {scoring.sensors.map((s) => (
              <li key={s.key} data-sensor={s.key}>
                <span>{COMPONENT_LABEL[s.key] ?? s.key}</span>
                <span className="driver-bar">
                  <i style={{ width: pctW(s.weight / Math.max(...scoring.sensors.map((x) => x.weight))) }} />
                </span>
                <b className="num">{pctW(s.weight)}</b>
              </li>
            ))}
          </ul>
          <p className="info-formula num" data-testid="score-formula">
            score = 100 × ({num(scoring.mean_share)} × weighted mean + {num(scoring.worst_share)} × worst sensor)
          </p>
          <p>
            Weights add to {pctW(total)} and are rescaled over the sensors a feed actually has. The worst-sensor half means one
            screaming sensor is enough. Spread and forward curve count at {num(scoring.aux_cap)}× there, so alone they reach
            watch, never warning.
          </p>
          <p className="info-thr">
            <span style={{ color: LEVEL_COLOR.watch }}>watch <b className="num">{num(t.watch)}</b></span>
            <span style={{ color: LEVEL_COLOR.warning }}>warning <b className="num">{num(t.warning)}</b></span>
            <span style={{ color: LEVEL_COLOR.tornado }}>tornado <b className="num">{num(t.tornado)}</b></span>
          </p>
          <p>
            Hysteresis: upgrades are immediate; a downgrade needs the score {num(scoring.hysteresis)} points under the
            threshold for {scoring.cooldown_bars} bars in a row. An alert is a squall until {Math.round(scoring.squall_min)} min
            of alert time build up, then a storm.
          </p>
        </div>
      )}
    </span>
  );
}

export function Gauge({ storm }: { storm: Storm }) {
  const { display, snap, retryIn } = storm;
  const r = reading(display, snap, retryIn);
  const scoreVal = r.score !== null ? Math.min(100, Math.max(0, Number(r.score))) : null;
  const live = isLevel(display);
  const still = !(display === "warming_up" || (live && snap?.state !== "ended")); // the beam only sweeps on a real reading
  const warm = display === "warming_up" && snap ? Math.min(100, (100 * snap.warmup.have) / Math.max(1, snap.warmup.need)) : null;
  const [nx, ny] = pt(A0 + (SPAN * (scoreVal ?? 0)) / 100, R + 11);
  const [mx, my] = pt(A0 + (SPAN * (scoreVal ?? 0)) / 100, R - 11);
  const kind = live && snap ? snap.kind : null;
  const squallMin = Math.round(storm.catalogue?.scoring?.squall_min ?? 90);

  return (
    <div>
      <div
        className="gauge"
        data-testid="gauge"
        data-display={display}
        data-still={still}
        style={{ ["--sweep" as string]: SWEEP[display] ?? "7s" }}
        role="img"
        aria-label={`Storm score ${r.score ?? "unavailable"}, ${r.label}`}
      >
        <div className="scope">
          <div className="sweep" />
        </div>
        <svg viewBox="0 0 300 300">
          {[30, 60, 90].map((rr) => (
            <circle key={rr} cx={C} cy={C} r={rr} fill="none" stroke="rgba(61,220,132,0.16)" strokeWidth="1" />
          ))}
          <path d={`M ${C - 117} ${C} H ${C + 117} M ${C} ${C - 117} V ${C + 117}`} stroke="rgba(61,220,132,0.13)" strokeWidth="1" />
          {ZONES.map(([a, b, col]) => (
            <path key={a} d={arc(a + 0.6, b - 0.6)} stroke={col} strokeOpacity={live ? 0.3 : 0.12} strokeWidth="13" fill="none" />
          ))}
          {scoreVal !== null && scoreVal > 0.5 && (
            <path d={arc(0, scoreVal)} stroke="var(--lvl)" strokeWidth="13" fill="none" />
          )}
          {warm !== null && <path d={arc(0, Math.max(1, warm))} stroke="var(--lvl)" strokeWidth="5" strokeDasharray="3 5" fill="none" />}
          {scoreVal !== null && <path d={`M ${mx} ${my} L ${nx} ${ny}`} stroke="#fff" strokeWidth="4" strokeLinecap="round" />}
          {[25, 50, 75].map((t) => {
            const [x, y] = pt(A0 + (SPAN * t) / 100, R - 22);
            return (
              <text key={t} x={x} y={y + 4} textAnchor="middle" fontSize="11" fontFamily="var(--mono)" fill="rgba(180,205,194,0.75)">
                {t}
              </text>
            );
          })}
        </svg>
        <div className="gauge-read">
          {r.score !== null && (
            <div className="gauge-score" data-testid="gauge-score">
              {r.score}
            </div>
          )}
          <div className={`gauge-level${r.score === null ? " alone" : ""}`} data-testid="gauge-level">
            {r.label}
          </div>
          {r.note && <div className="gauge-note" data-testid="gauge-note">{r.note}</div>}
        </div>
      </div>
      <div className="gauge-under">
        {kind && (
          <span className="kind" data-kind={kind} data-testid="kind" title={KIND_TITLE(squallMin)}>
            {kind === "squall" ? "squall · developing" : kind}
          </span>
        )}
        {live && snap && snap.peak_score !== null && (
          <span className="chip num" title="Highest score in this session">
            peak {Math.round(snap.peak_score)}
          </span>
        )}
        {live && snap && snap.kind !== "calm" && (
          <span className="chip num" title={`Decayed minutes spent at warning or above. Past ${squallMin} a squall becomes a storm.`}>
            heat {snap.storm_heat_min.toFixed(0)} / {squallMin} min
          </span>
        )}
      </div>
      {kind === "squall" && snap && (
        <p className="kind-note" data-testid="kind-note">
          {LEVEL_LABEL[display]} reading, {snap.storm_heat_min.toFixed(0)} min into the alert. Squall becomes storm if it persists past {squallMin} min.
        </p>
      )}
    </div>
  );
}

function detail(key: string, snap: Snapshot): string {
  const f = snap.features || {};
  const n = (v: number | undefined | null, dp = 1) => (v === undefined || v === null ? "--" : v.toFixed(dp));
  switch (key) {
    case "vol":
      return `z ${f.vol_z >= 0 ? "+" : ""}${n(f.vol_z)}`;
    case "jump":
      return `${n(f.jump_peak_z)} sigma`;
    case "drawdown":
      return (f.drawdown ?? 0) > 0 ? fmtPct(-f.drawdown, 2) : "0.00%";
    case "spread":
      return snap.bid === null ? "no quotes" : `${n(f.spread_ratio)}x`;
    case "curve":
      return snap.carry ? `z ${n(snap.carry.dislocation_z)}` : "no curve";
    default:
      return "";
  }
}

export function Drivers({ storm }: { storm: Storm }) {
  const { snap, display } = storm;
  const usable = snap && isLevel(display); // components of a stale or warming feed are not shown as if current
  const keys = ["vol", "jump", "drawdown", "spread", "curve"];
  const comps = usable ? snap.components : {};
  const order = [...keys].sort((a, b) => (comps[b] ?? 0) - (comps[a] ?? 0));
  const top = usable && (comps[order[0]] ?? 0) > 0.02 ? order[0] : null;
  return (
    <div className="drivers" data-testid="drivers">
      {order.map((k) => {
        const v = comps[k] ?? 0;
        const off = !usable || (k === "spread" && snap.bid === null) || (k === "curve" && !snap.carry);
        return (
          <div className="driver" key={k} data-top={k === top} data-off={off} data-driver={k}>
            <span className="driver-name">{COMPONENT_LABEL[k]}</span>
            <span className="driver-bar">
              <i style={{ width: `${Math.round(100 * Math.min(1, Math.max(0, v)))}%` }} />
            </span>
            <span className="driver-val">{usable ? detail(k, snap) : "--"}</span>
          </div>
        );
      })}
    </div>
  );
}
