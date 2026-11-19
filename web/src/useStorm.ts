// One hook owns the conversation with the backend: health polling, the SSE stream (with reconnect),
// chart history, the alert log and the session actions. Components only render what it returns.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError } from "./api";
import type { AlertRec, Catalogue, Display, Health, History, Snapshot, Transition } from "./types";

export type StreamState = "idle" | "connecting" | "open" | "reconnecting" | "ended";
export type ApiState = "connecting" | "up" | "down";

const HEALTH_MS = 2000;
const HISTORY_MS = 1000;
const ALERTS_MS = 3000;
const STREAM_SILENCE_MS = 7000; // the server heartbeats every 2 s; this long without one means the pipe is dead

export interface Storm {
  apiState: ApiState;
  retryIn: number;
  retryNow: () => void;
  health: Health | null;
  catalogue: Catalogue | null;
  symbol: string | null;
  snap: Snapshot | null;
  stream: StreamState;
  reconnects: number;
  history: History | null;
  transitions: Transition[];
  alerts: AlertRec[];
  display: Display;
  busy: string | null;
  actionError: string | null;
  clearError: () => void;
  startReplay: (dataset: string, speed: number | "max", pauseOn: string | null) => Promise<void>;
  startLive: (symbol: string) => Promise<void>;
  startYf: (name: string) => Promise<void>;
  pause: () => Promise<void>;
  resume: () => Promise<void>;
  setSpeed: (speed: number | "max") => Promise<void>;
  stop: () => Promise<void>;
}

export function useStorm(): Storm {
  const [apiState, setApiState] = useState<ApiState>("connecting");
  const [retryIn, setRetryIn] = useState(0);
  const [health, setHealth] = useState<Health | null>(null);
  const [catalogue, setCatalogue] = useState<Catalogue | null>(null);
  const [symbol, setSymbol] = useState<string | null>(null);
  const [snap, setSnap] = useState<Snapshot | null>(null);
  const [stream, setStream] = useState<StreamState>("idle");
  const [reconnects, setReconnects] = useState(0);
  const [epoch, setEpoch] = useState(0); // bump to force a fresh SSE connection
  const [history, setHistory] = useState<History | null>(null);
  const [transitions, setTransitions] = useState<Transition[]>([]);
  const [alerts, setAlerts] = useState<AlertRec[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const symbolRef = useRef(symbol);
  symbolRef.current = symbol;
  const streamRef = useRef(stream);
  streamRef.current = stream;
  const sessionKey = useRef<string | null>(null);
  const trMap = useRef(new Map<string, Transition>());
  const healthTick = useRef<() => void>(() => {});

  const mergeTransitions = useCallback((list: Transition[] | undefined, replace = false) => {
    if (!list) return;
    const m = replace ? new Map<string, Transition>() : trMap.current;
    const before = trMap.current.size;
    for (const t of list) m.set(`${t.ts}|${t.frm}|${t.to}`, t);
    if (replace || m.size !== before) {
      trMap.current = m;
      setTransitions([...m.values()].sort((a, b) => a.ts - b.ts));
    }
  }, []);

  const resetSession = useCallback(() => {
    trMap.current = new Map();
    setTransitions([]);
    setHistory(null);
  }, []);

  // ---------------------------------------------------------------- health: is the API there at all?
  useEffect(() => {
    let alive = true;
    let timer = 0;
    let fails = 0;
    let countdown = 0;
    const tick = async () => {
      window.clearTimeout(timer);
      window.clearInterval(countdown);
      try {
        const h = await api.health();
        if (!alive) return;
        fails = 0;
        setHealth(h);
        setApiState("up");
        setRetryIn(0);
        const cur = symbolRef.current;
        if (h.latest && (!cur || !h.sessions[cur])) setSymbol(h.latest); // adopt whatever the server is running
        else if (cur && !h.sessions[cur]) setSymbol(null); // server restarted: our session is gone
        // a session that restarted (from this tab or another) after our stream ended: reconnect
        if (cur && h.sessions[cur]?.state === "running" && streamRef.current === "ended") setEpoch((e) => e + 1);
        timer = window.setTimeout(tick, HEALTH_MS);
      } catch {
        if (!alive) return;
        fails += 1;
        setApiState("down");
        const wait = fails <= 3 ? 2 : 5;
        let left = wait;
        setRetryIn(left);
        countdown = window.setInterval(() => setRetryIn((left = Math.max(0, left - 1))), 1000);
        timer = window.setTimeout(tick, wait * 1000);
      }
    };
    healthTick.current = tick;
    tick();
    return () => {
      alive = false;
      window.clearTimeout(timer);
      window.clearInterval(countdown);
    };
  }, []);

  const retryNow = useCallback(() => {
    setRetryIn(0);
    healthTick.current();
  }, []);

  const apiUp = apiState === "up";

  useEffect(() => {
    if (!apiUp || catalogue) return;
    let alive = true;
    api
      .datasets()
      .then((c) => alive && setCatalogue(c))
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [apiUp, catalogue]);

  // ---------------------------------------------------------------- SSE stream with reconnect
  useEffect(() => {
    if (!apiUp || !symbol) {
      setStream((s) => (symbol && s !== "idle" ? "reconnecting" : "idle"));
      if (!symbol) setSnap(null);
      return;
    }
    let closed = false;
    let retry = 0;
    let watchdog = 0;
    let lastMsg = Date.now();
    setStream((s) => (s === "reconnecting" ? s : "connecting"));
    const es = api.openStream(symbol);

    const drop = async () => {
      if (closed) return;
      closed = true;
      es.close();
      window.clearInterval(watchdog);
      setStream("reconnecting");
      setReconnects((n) => n + 1);
      try {
        await api.score(symbol); // is the session still there?
        retry = window.setTimeout(() => setEpoch((e) => e + 1), 1000);
      } catch (e) {
        if (e instanceof ApiError && !e.network) {
          setStream("idle"); // the API answered: this session no longer exists
          setSnap(null);
          setSymbol(null);
        } else {
          healthTick.current(); // the API is down: the health loop owns the banner and brings us back
          retry = window.setTimeout(() => setEpoch((e) => e + 1), 2500);
        }
      }
    };

    es.onmessage = (ev) => {
      lastMsg = Date.now();
      let s: Snapshot;
      try {
        s = JSON.parse(ev.data);
      } catch {
        return;
      }
      const key = `${s.symbol}|${s.session?.started}`;
      if (key !== sessionKey.current) {
        sessionKey.current = key;
        resetSession();
      }
      mergeTransitions(s.new_transitions);
      setSnap(s);
      setStream("open");
    };
    es.addEventListener("end", () => {
      closed = true;
      es.close();
      window.clearInterval(watchdog);
      setStream("ended");
    });
    es.onerror = () => void drop();
    watchdog = window.setInterval(() => {
      if (Date.now() - lastMsg > STREAM_SILENCE_MS) void drop();
    }, 1000);

    return () => {
      closed = true;
      es.close();
      window.clearInterval(watchdog);
      window.clearTimeout(retry);
    };
  }, [apiUp, symbol, epoch, mergeTransitions, resetSession]);

  // ---------------------------------------------------------------- chart history (the source of truth for the chart)
  const running = stream === "open" && (snap?.state === "live" || snap?.state === "warming_up" || snap?.state === "stale");
  const settleKey = `${symbol}|${snap?.session.started}|${snap?.state}|${stream}`;
  useEffect(() => {
    if (!apiUp || !symbol || stream === "idle" || stream === "connecting") return;
    let alive = true;
    const load = () =>
      api
        .history(symbol)
        .then((h) => {
          if (!alive) return;
          setHistory(h);
          mergeTransitions(h.transitions, true);
        })
        .catch(() => {});
    load();
    const id = running ? window.setInterval(load, HISTORY_MS) : 0;
    return () => {
      alive = false;
      window.clearInterval(id);
    };
    // settleKey: refetch once whenever the feed changes state (paused, ended, new session)
  }, [apiUp, symbol, running, settleKey, mergeTransitions]); // eslint-disable-line react-hooks/exhaustive-deps

  // ---------------------------------------------------------------- alert log
  useEffect(() => {
    if (!apiUp) return;
    let alive = true;
    const load = () =>
      api
        .alerts()
        .then((a) => alive && setAlerts(a.alerts))
        .catch(() => {});
    load();
    const id = window.setInterval(load, ALERTS_MS);
    return () => {
      alive = false;
      window.clearInterval(id);
    };
  }, [apiUp]);

  // ---------------------------------------------------------------- actions
  const act = useCallback(async (label: string, fn: () => Promise<unknown>) => {
    setBusy(label);
    setActionError(null);
    try {
      await fn();
    } catch (e) {
      setActionError(e instanceof Error ? e.message : String(e));
      if (e instanceof ApiError && e.network) healthTick.current();
    } finally {
      setBusy(null);
    }
  }, []);

  const begin = useCallback(
    (label: string, fn: () => Promise<{ symbol: string }>) =>
      act(label, async () => {
        const r = await fn();
        sessionKey.current = null;
        resetSession();
        setSnap(null);
        setSymbol(r.symbol);
        setEpoch((e) => e + 1);
      }),
    [act, resetSession],
  );

  const startReplay = useCallback(
    (dataset: string, speed: number | "max", pauseOn: string | null) =>
      begin("Loading replay", () => api.replay(dataset, speed, pauseOn)),
    [begin],
  );
  const startLive = useCallback((sym: string) => begin("Connecting to Binance", () => api.live(sym)), [begin]);
  const startYf = useCallback((name: string) => begin("Fetching from yfinance", () => api.yfinance(name)), [begin]);
  const pause = useCallback(() => act("Pausing", () => api.pause(symbolRef.current || "")), [act]);
  const resume = useCallback(() => act("Resuming", () => api.resume(symbolRef.current || "")), [act]);
  const setSpeed = useCallback(
    (speed: number | "max") => act("Changing speed", () => api.speed(symbolRef.current || "", speed)),
    [act],
  );
  const stop = useCallback(() => act("Stopping", () => api.stop(symbolRef.current || "")), [act]);
  const clearError = useCallback(() => setActionError(null), []);

  const display: Display = useMemo(() => {
    if (apiState === "down") return "offline";
    if (!symbol || !snap) return "idle";
    return snap.level;
  }, [apiState, symbol, snap]);

  return {
    apiState, retryIn, retryNow, health, catalogue, symbol, snap, stream, reconnects, history, transitions, alerts,
    display, busy, actionError, clearError, startReplay, startLive, startYf, pause, resume, setSpeed, stop,
  };
}
