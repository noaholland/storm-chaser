import { STATIC } from "../api";
import type { Storm } from "../useStorm";
import { fmtSpan } from "../format";

export interface FeedStatus {
  tone: "live" | "replay" | "warm" | "stale" | "frozen" | "paused" | "idle" | "done" | "bad";
  label: string;
  detail?: string;
}

/** One honest sentence about the feed. Order matters: transport problems outrank whatever the last snapshot said. */
export function feedStatus(storm: Storm): FeedStatus {
  const { apiState, snap, symbol, stream } = storm;
  if (apiState === "down") return { tone: "bad", label: "API down" };
  if (apiState === "connecting") return { tone: "idle", label: "Connecting" };
  if (!symbol || !snap) return stream === "connecting" || storm.busy ? { tone: "warm", label: "Starting" } : { tone: "idle", label: "No feed" };
  if (stream === "reconnecting") return { tone: "stale", label: "Reconnecting", detail: "stream dropped" };
  const s = snap.session;
  switch (snap.state) {
    case "paused":
      return s.pause_reason && s.pause_reason !== "user"
        ? { tone: "frozen", label: `Frozen at ${s.pause_reason}` }
        : { tone: "paused", label: "Paused" };
    case "ended":
      return { tone: "done", label: s.kind === "live" ? "Feed stopped" : s.kind === "yfinance" ? "Snapshot, not live" : "Replay finished" };
    case "error":
      return { tone: "bad", label: "Feed error" };
    case "stale":
      return { tone: "stale", label: "Stale", detail: `${Math.round(snap.feed.age_s ?? 0)} s` };
    case "warming_up":
      return { tone: "warm", label: "Warming up", detail: `${snap.warmup.have}/${snap.warmup.need}` };
    case "no_data":
      return { tone: "warm", label: "Waiting for ticks" };
    default:
      if (s.kind === "live") return { tone: "live", label: "Live", detail: s.reconnects ? `${s.reconnects} reconnects` : undefined };
      return {
        tone: "replay",
        label: "Replaying",
        detail: s.speed && s.speed < 1_000_000 ? `${fmtSpan(s.speed)}/s` : "max",
      };
  }
}

interface Props {
  storm: Storm;
  onPick: (value: string) => void;
  onJump: () => void;
  sirenOn: boolean;
  sirenLevel: number;
  onSiren: () => void;
}

function pickerValue(storm: Storm): string {
  const s = storm.snap?.session;
  if (!s || !storm.catalogue) return "";
  if (s.kind === "replay" && s.dataset && s.dataset in storm.catalogue.datasets) return `replay:${s.dataset}`;
  if (s.kind === "live" && storm.symbol && storm.catalogue.live.includes(storm.symbol)) return `live:${storm.symbol}`;
  if (s.kind === "yfinance" && s.dataset) {
    const ticker = s.dataset.split(":")[1];
    const hit = Object.entries(storm.catalogue.yfinance_presets).find(([, t]) => t === ticker);
    if (hit) return `yf:${hit[0]}`;
  }
  return "";
}

export function Header({ storm, onPick, onJump, sirenOn, sirenLevel, onSiren }: Props) {
  const cat = storm.catalogue;
  const st = feedStatus(storm);
  const down = storm.apiState !== "up";
  return (
    <header className="header">
      <div className="brand">
        <svg width="40" height="40" viewBox="0 0 40 40" aria-hidden="true">
          <circle cx="20" cy="20" r="17" fill="none" stroke="#3ddc84" strokeWidth="2" />
          <circle cx="20" cy="20" r="9" fill="none" stroke="#3ddc84" strokeOpacity="0.45" strokeWidth="1.5" />
          <path d="M20 20 L33 10" stroke="#ff3d81" strokeWidth="3" strokeLinecap="round" />
          <circle cx="20" cy="20" r="2.5" fill="#ecf7f1" />
        </svg>
        <div>
          <div className="brand-name">Storm Chaser</div>
          <div className="brand-tag">Cloudy with a chance of margin calls</div>
        </div>
      </div>
      <div className="header-mid">
        <select
          className="picker"
          aria-label="Choose a data feed"
          data-testid="feed-picker"
          value={pickerValue(storm)}
          disabled={down || !cat || storm.busy !== null}
          onChange={(e) => e.target.value && onPick(e.target.value)}
        >
          <option value="">{!cat ? (down ? "Feeds unavailable" : "Loading feeds") : "Choose a feed"}</option>
          {cat && (
            <>
              <optgroup label="Crisis replays">
                {Object.entries(cat.datasets).map(([k, d]) => (
                  <option key={k} value={`replay:${k}`}>
                    {d.title}
                  </option>
                ))}
              </optgroup>
              {cat.live.length > 0 && (
                <optgroup label="Live, Binance">
                  {cat.live.map((s) => (
                    <option key={s} value={`live:${s}`}>
                      {s} live
                    </option>
                  ))}
                </optgroup>
              )}
              {Object.keys(cat.yfinance_presets).length > 0 && (
                <optgroup label="Markets, last 5 days via yfinance">
                  {Object.keys(cat.yfinance_presets).map((n) => (
                    <option key={n} value={`yf:${n}`}>
                      {n}
                    </option>
                  ))}
                </optgroup>
              )}
              {STATIC && (
                <optgroup label="Live feeds: run locally">
                  <option disabled>Binance live, yfinance, full SGX sets</option>
                </optgroup>
              )}
            </>
          )}
        </select>
        <button className="btn btn-storm" onClick={onJump} disabled={down || storm.busy !== null} data-testid="jump-storm" title="SGX USDJPY, 28 July to 4 August 2026: run to the first tornado and freeze there">
          Jump to the storm
        </button>
        {storm.busy && <span className="busy" data-testid="busy">{storm.busy}</span>}
      </div>
      <div className="header-right">
        <span className="pill" data-tone={st.tone} data-testid="feed-pill" role="status" aria-live="polite">
          <i />
          {st.label}
          {st.detail && <small>{st.detail}</small>}
        </span>
        <button
          className={`btn ${sirenOn ? "btn-storm" : ""}`}
          onClick={onSiren}
          aria-pressed={sirenOn}
          data-testid="siren-toggle"
          data-siren-level={sirenLevel.toFixed(2)}
          title="Storm siren. Silent below watch; louder and faster as the score climbs."
        >
          Siren {sirenOn ? "on" : "off"}
        </button>
      </div>
    </header>
  );
}
