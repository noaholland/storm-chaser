import { useEffect, useMemo, useRef } from "react";
import {
  ColorType,
  HistogramSeries,
  LineSeries,
  LineStyle,
  createChart,
  createSeriesMarkers,
  type IChartApi,
  type ISeriesApi,
  type ISeriesMarkersPluginApi,
  type SeriesMarker,
  type Time,
  type UTCTimestamp,
} from "lightweight-charts";
import type { Storm } from "../useStorm";
import type { Transition } from "../types";
import { COMPONENT_LABEL, LEVELS, LEVEL_COLOR, fmtPct, fmtPrice, fmtTime } from "../format";

const BAND: Record<string, string> = {
  green: "rgba(61,220,132,0.035)",
  watch: "rgba(255,210,63,0.20)",
  warning: "rgba(255,138,31,0.30)",
  tornado: "rgba(255,61,129,0.42)",
  warming_up: "rgba(127,180,201,0.10)",
};
const LINE: Record<string, string> = { ...LEVEL_COLOR, warming_up: "#7fb4c9" };

function precision(p: number): number {
  const a = Math.abs(p);
  return a >= 10000 ? 1 : a >= 1000 ? 2 : a >= 20 ? 3 : a >= 2 ? 4 : 5;
}

export function PriceChart({ storm, onJump, onSimulate }: { storm: Storm; onJump: () => void; onSimulate: () => void }) {
  const { history, snap, transitions, display, apiState } = storm;
  const el = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const series = useRef<{
    bands: ISeriesApi<"Histogram">;
    price: ISeriesApi<"Line">;
    score: ISeriesApi<"Line">;
    markers: ISeriesMarkersPluginApi<Time>;
  } | null>(null);
  const lastCount = useRef(0);

  useEffect(() => {
    if (!el.current) return;
    const c = createChart(el.current, {
      autoSize: true,
      layout: {
        background: { type: ColorType.Solid, color: "transparent" },
        textColor: "#b4cdc2",
        fontFamily: "IBM Plex Mono, ui-monospace, monospace",
        fontSize: 13,
        attributionLogo: false,
        panes: { separatorColor: "#1d3b32", separatorHoverColor: "#2b5547", enableResize: false },
      },
      grid: { vertLines: { color: "rgba(61,220,132,0.06)" }, horzLines: { color: "rgba(61,220,132,0.06)" } },
      rightPriceScale: { borderColor: "#1d3b32" },
      timeScale: { borderColor: "#1d3b32", timeVisible: true, secondsVisible: false, rightOffset: 3 },
      crosshair: { vertLine: { color: "#7d998e", labelBackgroundColor: "#12261f" }, horzLine: { color: "#7d998e", labelBackgroundColor: "#12261f" } },
      localization: { timeFormatter: (t: Time) => fmtTime(Number(t)) + " UTC" },
    });
    const bands = c.addSeries(HistogramSeries, { priceScaleId: "bands", priceLineVisible: false, lastValueVisible: false, base: 0 });
    c.priceScale("bands").applyOptions({ scaleMargins: { top: 0, bottom: 0 }, visible: false });
    const price = c.addSeries(LineSeries, {
      color: "#ecf7f1",
      lineWidth: 2,
      priceLineColor: "rgba(236,247,241,0.5)",
      crosshairMarkerRadius: 4,
    });
    c.priceScale("right").applyOptions({ scaleMargins: { top: 0.2, bottom: 0.08 } });
    const score = c.addSeries(LineSeries, { lineWidth: 2, priceLineVisible: false, lastValueVisible: true, priceFormat: { type: "price", precision: 0, minMove: 1 }, autoscaleInfoProvider: () => ({ priceRange: { minValue: 0, maxValue: 100 } }) }, 1);
    for (const [v, col] of [[25, LEVEL_COLOR.watch], [50, LEVEL_COLOR.warning], [75, LEVEL_COLOR.tornado]] as const)
      score.createPriceLine({ price: v, color: col, lineWidth: 1, lineStyle: LineStyle.Dashed, axisLabelVisible: false, title: "" });
    const panes = c.panes();
    if (panes[1]) panes[1].setHeight(92);
    chart.current = c;
    series.current = { bands, price, score, markers: createSeriesMarkers(price, []) };
    return () => {
      c.remove();
      chart.current = null;
      series.current = null;
    };
  }, []);

  const data = useMemo(() => {
    if (!history || history.ts.length === 0) return null;
    const ts: number[] = [], px: number[] = [], sc: (number | null)[] = [], lv: string[] = [];
    let prev = -Infinity;
    for (let i = 0; i < history.ts.length; i++) {
      const t = Math.floor(history.ts[i]);
      const p = history.price[i];
      if (!(t > prev) || !Number.isFinite(p)) continue; // the chart needs strictly increasing times
      prev = t;
      ts.push(t);
      px.push(p);
      sc.push(history.score[i]);
      lv.push(history.level[i]);
    }
    return { ts, px, sc, lv };
  }, [history]);

  useEffect(() => {
    const s = series.current;
    const c = chart.current;
    if (!s || !c) return;
    if (!data) {
      s.bands.setData([]);
      s.price.setData([]);
      s.score.setData([]);
      s.markers.setMarkers([]);
      lastCount.current = 0;
      return;
    }
    const { ts, px, sc, lv } = data;
    const pr = precision(px[px.length - 1]);
    s.price.applyOptions({ priceFormat: { type: "price", precision: pr, minMove: 1 / 10 ** pr } });
    s.price.setData(ts.map((t, i) => ({ time: t as UTCTimestamp, value: px[i] })));
    s.bands.setData(ts.map((t, i) => ({ time: t as UTCTimestamp, value: 1, color: BAND[lv[i]] ?? "rgba(0,0,0,0)" })));
    s.score.setData(
      ts.flatMap((t, i) => (sc[i] === null ? [] : [{ time: t as UTCTimestamp, value: sc[i] as number, color: LINE[lv[i]] ?? "#7d998e" }])),
    );
    if (ts.length !== lastCount.current) {
      lastCount.current = ts.length;
      c.timeScale().fitContent();
    }
  }, [data]);

  // alert markers: upgrades into warning or tornado, pinned to the bar they happened on
  useEffect(() => {
    const s = series.current;
    if (!s) return;
    if (!data) return;
    const { ts } = data;
    const at = (t: number) => {
      let lo = 0, hi = ts.length - 1;
      while (lo < hi) {
        const mid = (lo + hi) >> 1;
        if (ts[mid] < t) lo = mid + 1;
        else hi = mid;
      }
      return ts[lo];
    };
    const byBar = new Map<number, Transition>();
    for (const t of transitions) {
      const up = LEVELS.indexOf(t.to as never) > LEVELS.indexOf(t.frm as never);
      if (!up || (t.to !== "warning" && t.to !== "tornado")) continue;
      const bar = at(t.ts);
      const cur = byBar.get(bar);
      if (!cur || LEVELS.indexOf(t.to as never) > LEVELS.indexOf(cur.to as never)) byBar.set(bar, t);
    }
    const marks: SeriesMarker<Time>[] = [...byBar.entries()]
      .sort((a, b) => a[0] - b[0])
      .slice(-80)
      .map(([bar, t]) => ({
        time: bar as UTCTimestamp,
        position: "aboveBar",
        shape: "arrowDown",
        color: t.to === "tornado" ? LEVEL_COLOR.tornado : LEVEL_COLOR.warning,
        size: t.to === "tornado" ? 2 : 1,
        text: "",
      }));
    s.markers.setMarkers(marks);
  }, [data, transitions]);

  const frozen = snap?.state === "paused" && snap.session.pause_reason && snap.session.pause_reason !== "user";
  const offline = apiState === "down";
  const spread = snap && snap.bid !== null && snap.ask !== null ? snap.ask - snap.bid : null;

  return (
    <>
    {frozen && snap && !offline && <Callout storm={storm} onSimulate={onSimulate} />}
    <div className="chart-wrap" data-testid="chart" data-points={data?.ts.length ?? 0}>
      <div className="chart" ref={el} style={{ opacity: offline ? 0.35 : 1 }} />
      {snap && (
        <div className="chart-legend">
          <span className="chart-symbol" data-testid="chart-symbol">{snap.symbol}</span>
          <span className="chart-price" data-testid="chart-price">{fmtPrice(snap.price)}</span>
          <span className="chart-meta">
            {fmtTime(snap.ts, true)} UTC
            {spread !== null && snap.price ? ` · spread ${((spread / snap.price) * 1e4).toFixed(2)} bp` : ""}
          </span>
        </div>
      )}
      {!snap && !offline && (
        <div className="chart-empty" data-testid="chart-empty">
          <h3>No feed on the radar</h3>
          <p>
            Pick a crisis replay, a live Binance feed or a market from the picker above. Or go straight to the SGX
            USDJPY storm of 28 July to 4 August 2026.
          </p>
          <button className="btn btn-storm" onClick={onJump} disabled={storm.busy !== null}>
            Jump to the storm
          </button>
        </div>
      )}
      {offline && (
        <div className="chart-empty" data-testid="chart-offline">
          <h3>Radar offline</h3>
          <p>The API is not answering. Anything still on screen is the last reading and is not being updated.</p>
        </div>
      )}
      {snap && !offline && display === "warming_up" && (data?.ts.length ?? 0) < 2 && (
        <div className="chart-empty" style={{ background: "transparent", justifyContent: "flex-end", paddingBottom: 120 }}>
          <p>Collecting the first bars.</p>
        </div>
      )}
    </div>
    </>
  );
}

/** The freeze frame: the replay stopped itself the moment the level hit the trigger. */
function Callout({ storm, onSimulate }: { storm: Storm; onSimulate: () => void }) {
  const snap = storm.snap!;
  const level = snap.session.pause_reason || "tornado";
  const trig = [...storm.transitions].reverse().find((t) => t.to === level);
  const ahead = snap.session.ahead;
  const fall = ahead ? Math.abs(ahead.pct_to_low) : 0;
  const drivers = [trig ? (COMPONENT_LABEL[trig.why] ?? trig.why).toLowerCase() : null, trig?.carry_unwind || snap.carry?.unwind ? "carry unwind" : null].filter(Boolean).join(", ");
  const title = level === "tornado" ? "Tornado warning" : level === "warning" ? "Storm warning" : "Storm watch";
  return (
    <div className="callout" data-testid="callout" data-level={level} role="alert">
      <div>
        <h2 className="callout-title" data-testid="callout-title">
          {title}
          <span>
            {fmtTime(snap.ts)} <small>UTC</small>
          </span>
        </h2>
        <p className="callout-line">
          {snap.symbol} at <span className="num">{fmtPrice(snap.price)}</span>, score{" "}
          <span className="num">{Math.round(snap.score ?? snap.last_score ?? 0)}</span>
          {drivers && <>, driven by {drivers}</>}. Replay frozen at the alert.{" "}
          {ahead && (
            <span className="callout-hind" data-testid="callout-hindsight">
              Hindsight from the rest of the file, unseen by the engine: low{" "}
              <span className="num">{fmtPrice(ahead.low)}</span> on {fmtTime(ahead.low_ts)}.
            </span>
          )}
        </p>
      </div>
      {ahead && (
        <div className="callout-big" data-testid="callout-fall">
          {fall >= 0.001 ? (
            <>
              <b>{fmtPct(fall, fall >= 0.1 ? 0 : 1)}</b>
              <em>still to fall</em>
            </>
          ) : (
            <>
              <b>{fmtPct(ahead.pct_to_high, 1, true)}</b>
              <em>the low is in</em>
            </>
          )}
        </div>
      )}
      <div className="callout-actions">
        <button className="btn btn-primary" onClick={() => void storm.resume()} data-testid="resume-callout">
          Resume replay
        </button>
        <button className="btn" onClick={onSimulate} data-testid="callout-simulate">
          Run cyclone simulator
        </button>
      </div>
    </div>
  );
}

export function BandKey() {
  return (
    <span className="band-key" aria-label="Background colour key">
      {(["watch", "warning", "tornado"] as const).map((l) => (
        <span key={l}>
          <i style={{ background: LEVEL_COLOR[l], opacity: 0.75 }} />
          {l}
        </span>
      ))}
    </span>
  );
}

