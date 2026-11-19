import { useCallback, useEffect, useRef, useState } from "react";
import { API_BASE, STATIC } from "./api";
import { useStorm } from "./useStorm";
import { Siren } from "./siren";
import type { Position } from "./types";
import { fmtTime, isLevel } from "./format";
import { Header } from "./components/Header";
import { Drivers, Gauge, ScoreInfo } from "./components/Gauge";
import { Carry } from "./components/Carry";
import { BandKey, PriceChart } from "./components/PriceChart";
import { Transport, type Speed } from "./components/Transport";
import { AlertFeed } from "./components/AlertFeed";
import { ExposurePanel, loadPositions, savePositions, useExposure } from "./components/Exposure";
import { SimulatorPanel } from "./components/Simulator";
import { Playbook } from "./components/Playbook";

const STORM_SET = "sgx_usdjpy_storm";

export function App() {
  const storm = useStorm();
  const { snap, display, apiState } = storm;
  const [speed, setSpeed] = useState<Speed>(14400);
  const [freeze, setFreeze] = useState(true);
  const [positions, setPositionsState] = useState<Position[]>(loadPositions);
  const [horizon, setHorizon] = useState(60);
  const [confidence, setConfidence] = useState(0.99);
  const [simTrigger, setSimTrigger] = useState(0);
  const exposure = useExposure(storm, positions, horizon, confidence);

  const setPositions = useCallback((p: Position[]) => {
    setPositionsState(p);
    savePositions(p);
  }, []);

  const pick = useCallback(
    (value: string) => {
      const [kind, ...rest] = value.split(":");
      const name = rest.join(":");
      if (kind === "replay") void storm.startReplay(name, speed, freeze ? "tornado" : null);
      else if (kind === "live") void storm.startLive(name);
      else if (kind === "yf") void storm.startYf(name);
    },
    [storm, speed, freeze],
  );
  const jump = useCallback(() => void storm.startReplay(STORM_SET, "max", "tornado"), [storm]);
  const restart = useCallback(() => {
    const s = snap?.session;
    if (!s) return;
    if (s.kind === "replay" && s.dataset) void storm.startReplay(s.dataset, speed, freeze ? "tornado" : null);
    else if (s.kind === "yfinance" && s.dataset) void storm.startYf(s.dataset.split(":")[1]);
  }, [snap, storm, speed, freeze]);

  // ---------------------------------------------------------------- siren: off until asked for
  const siren = useRef<Siren | null>(null);
  const [sirenOn, setSirenOn] = useState(false);
  const [sirenLevel, setSirenLevel] = useState(0);
  const audible = isLevel(display) && (snap?.state === "live" || snap?.state === "paused") ? snap.score : null;
  useEffect(() => {
    if (!sirenOn || !siren.current) return;
    siren.current.setScore(audible);
    setSirenLevel(siren.current.level);
  }, [sirenOn, audible]);
  const toggleSiren = useCallback(async () => {
    if (sirenOn) {
      setSirenOn(false);
      setSirenLevel(0);
      await siren.current?.stop();
      return;
    }
    siren.current ??= new Siren();
    const ok = await siren.current.start();
    setSirenOn(ok);
  }, [sirenOn]);
  useEffect(() => () => void siren.current?.stop(), []);

  // ---------------------------------------------------------------- the freeze frame
  const frozen = snap?.state === "paused" && !!snap.session.pause_reason && snap.session.pause_reason !== "user";
  const [flash, setFlash] = useState(0);
  const wasFrozen = useRef(false);
  useEffect(() => {
    if (frozen && !wasFrozen.current) setFlash((f) => f + 1);
    wasFrozen.current = frozen;
  }, [frozen]);

  const stale = apiState === "up" && display === "stale" && snap;
  const dropped = apiState === "up" && storm.stream === "reconnecting";
  const ended = apiState === "up" && snap?.state === "ended" && snap.session.kind !== "live";
  const feedErr = apiState === "up" && snap && (snap.state === "error" ? snap.session.error : snap.session.feed_error);

  return (
    <div className="app" data-display={display} data-testid="app" data-api={apiState} data-stream={storm.stream} data-reconnects={storm.reconnects}>
      <Header storm={storm} onPick={pick} onJump={jump} sirenOn={sirenOn} sirenLevel={sirenLevel} onSiren={() => void toggleSiren()} />

      {apiState === "down" && (
        <div className="banner" role="alert" data-testid="api-down">
          <div className="grow">
            <strong>Storm Chaser API unreachable</strong>
            <div>
              Nothing on this screen is live. Trying <code>{API_BASE}</code> again{" "}
              {storm.retryIn > 0 ? `in ${storm.retryIn} s` : "now"}. Start it with <code>make app</code>.
            </div>
          </div>
          <button className="btn" onClick={storm.retryNow} data-testid="retry">
            Retry now
          </button>
        </div>
      )}
      {dropped && (
        <div className="banner info" role="status" data-testid="stream-dropped">
          <strong>Stream dropped</strong>
          <div className="grow">Reconnecting to the live stream. The reading below is the last one received.</div>
        </div>
      )}
      {stale && (
        <div className="banner note" role="alert" data-testid="stale-banner">
          <strong>Feed stale</strong>
          <div className="grow">
            No ticks for {Math.round(snap.feed.age_s ?? 0)} s (limit {Math.round(snap.feed.stale_after_s)} s). The score is
            withheld until data flows again: a silent feed is not a calm market.
          </div>
        </div>
      )}
      {feedErr && (
        <div className="banner note" role="alert" data-testid="feed-error">
          <strong>Feed problem</strong>
          <div className="grow">{feedErr}</div>
        </div>
      )}
      {ended && (
        <div className="banner info" role="status" data-testid="replay-finished">
          <strong>{snap.session.kind === "yfinance" ? "Market snapshot" : "Replay finished"}</strong>
          <div className="grow">
            {snap.session.kind === "yfinance"
              ? `${snap.session.title ?? snap.symbol}: recent bars scored up to ${fmtTime(snap.ts, true)} UTC. Delayed data, not a live feed.`
              : `${snap.session.title ?? snap.symbol} ran to its last bar, ${fmtTime(snap.ts, true)} UTC. The final reading stays on screen.`}
          </div>
          <button className="btn btn-sm" onClick={restart} disabled={storm.busy !== null} data-testid="replay-again">
            {snap.session.kind === "yfinance" ? "Refresh" : "Replay again"}
          </button>
        </div>
      )}

      <main>
        <div className="grid-top">
          <section className="panel keep-bright" aria-labelledby="t-warning">
            <div className="panel-head">
              <span className="pillar">01a</span>
              <h2 className="panel-title" id="t-warning">Early warning</h2>
              <span className="panel-sub keep">
                <ScoreInfo scoring={storm.catalogue?.scoring} />
              </span>
            </div>
            <div className="panel-body">
              <Gauge storm={storm} />
              <Drivers storm={storm} />
            </div>
          </section>

          <section className="panel" aria-labelledby="t-track">
            <div className="panel-head">
              <h2 className="panel-title shrink" id="t-track">{snap?.session.title ?? (snap?.session.kind === "live" ? `${snap.symbol} live, Binance` : "Storm track")}</h2>
              <span className="panel-sub keep">
                <BandKey />
              </span>
            </div>
            <PriceChart storm={storm} onJump={jump} onSimulate={() => setSimTrigger((n) => n + 1)} />
            <Transport storm={storm} speed={speed} onSpeed={setSpeed} freeze={freeze} onFreeze={setFreeze} onRestart={restart} />
          </section>

          <div className="col">
            <section className="panel grow" aria-labelledby="t-alerts">
              <div className="panel-head">
                <span className="pillar">01b</span>
                <h2 className="panel-title" id="t-alerts">Alert feed</h2>
                <span className="panel-sub">early warning</span>
              </div>
              <AlertFeed storm={storm} />
            </section>
            <section className="panel" aria-labelledby="t-carry">
              <div className="panel-head">
                <span className="pillar">01c</span>
                <h2 className="panel-title" id="t-carry">Forward curve</h2>
                <span className="panel-sub">early warning: carry</span>
              </div>
              <div className="panel-body">
                <Carry storm={storm} />
              </div>
            </section>
          </div>
        </div>

        <div className="grid-bottom">
          <section className="panel" aria-labelledby="t-safe">
            <div className="panel-head">
              <span className="pillar">02</span>
              <h2 className="panel-title" id="t-safe">Stay safe</h2>
              <span className="panel-sub">your exposure to this weather</span>
            </div>
            <ExposurePanel
              storm={storm}
              positions={positions}
              setPositions={setPositions}
              exposure={exposure}
              horizon={horizon}
              setHorizon={setHorizon}
              confidence={confidence}
              setConfidence={setConfidence}
            />
          </section>
          <section className="panel" aria-labelledby="t-next">
            <div className="panel-head">
              <span className="pillar">03</span>
              <h2 className="panel-title" id="t-next">Predict what's next</h2>
              <span className="panel-sub">cyclone simulator</span>
            </div>
            <SimulatorPanel storm={storm} positions={positions} trigger={simTrigger} />
          </section>
          <section className="panel" aria-labelledby="t-opp">
            <div className="panel-head">
              <span className="pillar">04</span>
              <h2 className="panel-title" id="t-opp">Spot opportunities</h2>
              <span className="panel-sub">storm playbook</span>
            </div>
            <Playbook storm={storm} positions={positions} exposure={exposure} horizon={horizon} confidence={confidence} />
          </section>
        </div>
      </main>

      {STATIC && (
        <footer className="static-note" data-testid="static-note">
          <strong>Static demo.</strong> The three replays were scored by the Python engine and are played back here bar for bar;
          exposure and the cyclone simulator run in your browser. Live Binance, yfinance, Telegram alerts and the full two-month
          SGX sets need the backend: <b>run locally for live feeds</b> (<code>make install &amp;&amp; make app</code>, see the README).
        </footer>
      )}

      {storm.actionError && (
        <div className="toast" role="alert" data-testid="toast">
          <span>{storm.actionError}</span>
          <button className="btn btn-sm" onClick={storm.clearError}>
            Dismiss
          </button>
        </div>
      )}
      {frozen && apiState === "up" && snap?.session.pause_reason === "tornado" && <div className="vignette" data-testid="vignette" />}
      {flash > 0 && frozen && apiState === "up" && <div className="flash" key={flash} />}
    </div>
  );
}
