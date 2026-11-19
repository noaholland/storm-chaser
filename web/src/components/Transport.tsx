import type { Storm } from "../useStorm";
import { fmtSpan, fmtTime } from "../format";

export type Speed = number | "max";
export const SPEEDS: { label: string; value: Speed; title: string }[] = [
  { label: "1m/s", value: 60, title: "One minute of data per second" },
  { label: "10m/s", value: 600, title: "Ten minutes of data per second" },
  { label: "1h/s", value: 3600, title: "One hour of data per second" },
  { label: "4h/s", value: 14400, title: "Four hours of data per second" },
  { label: "1d/s", value: 86400, title: "One day of data per second" },
  { label: "max", value: "max", title: "As fast as the engine can score it" },
];
const MAX = 1_000_000;
const same = (a: Speed, b: number | null | undefined) => (a === "max" ? b === MAX : a === b);

interface Props {
  storm: Storm;
  speed: Speed;
  onSpeed: (s: Speed) => void;
  freeze: boolean;
  onFreeze: (b: boolean) => void;
  onRestart: () => void;
}

export function Transport({ storm, speed, onSpeed, freeze, onFreeze, onRestart }: Props) {
  const { snap, busy, apiState } = storm;
  const down = apiState !== "up";
  const sess = snap?.session;
  const replayable = !!sess && sess.kind !== "live";
  const paused = snap?.state === "paused";
  const over = snap?.state === "ended" || snap?.state === "error";
  const running = !!snap && !paused && !over;
  const current: Speed = replayable && sess.speed ? (SPEEDS.find((s) => same(s.value, sess.speed))?.value ?? speed) : speed;
  const pct = sess?.progress !== null && sess?.progress !== undefined ? Math.round(sess.progress * 100) : null;

  let main: { label: string; fn: () => void; disabled: boolean };
  if (!snap) main = { label: "Play", fn: () => {}, disabled: true };
  else if (!replayable) main = { label: over ? "Stopped" : "Stop feed", fn: () => void storm.stop(), disabled: over };
  else if (paused) main = { label: "Resume", fn: () => void storm.resume(), disabled: false };
  else if (over) main = { label: sess.kind === "yfinance" ? "Refresh" : "Replay again", fn: onRestart, disabled: false };
  else main = { label: "Pause", fn: () => void storm.pause(), disabled: false };

  return (
    <div className="transport" data-testid="transport">
      <div className="progress" data-testid="progress">
        <span data-testid="progress-label">
          {!snap
            ? "no replay loaded"
            : !replayable
              ? "live feed, no timeline"
              : over
                ? sess.kind === "yfinance" ? "snapshot loaded" : "replay finished"
                : `${pct ?? 0}% · ${fmtTime(snap.ts)} UTC`}
        </span>
        <div className="progress-track">
          <i style={{ width: `${pct ?? 0}%` }} />
        </div>
        <span>{snap && replayable && sess.bar_seconds ? `${fmtSpan(sess.bar_seconds)} bars` : ""}</span>
      </div>
      <button
        className={`btn btn-sm ${paused || over ? "btn-primary" : ""}`}
        onClick={main.fn}
        disabled={main.disabled || down || busy !== null}
        data-testid="play-pause"
        style={{ minWidth: 104 }}
      >
        {main.label}
      </button>
      <div className="speeds" role="group" aria-label="Replay speed, data time per second">
        {SPEEDS.map((s) => (
          <button
            key={s.label}
            title={s.title}
            aria-pressed={s.value === current}
            disabled={down || (!!snap && !replayable)}
            data-testid={`speed-${s.label.replace("/", "")}`}
            onClick={() => {
              onSpeed(s.value);
              if (replayable && (running || paused)) void storm.setSpeed(s.value);
            }}
          >
            {s.label}
          </button>
        ))}
      </div>
      <label className="check" title="Freeze the replay the moment the level reaches tornado, so the alert can be inspected">
        <input type="checkbox" checked={freeze} onChange={(e) => onFreeze(e.target.checked)} data-testid="freeze-toggle" />
        Freeze on tornado{sess?.pause_on ? " (armed)" : ""}
      </label>
    </div>
  );
}
