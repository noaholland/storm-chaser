// Edge states: warming up, API down and back, stale feed, bad feed choice, fast replay.
import { expect, test } from "@playwright/test";
import { jumpToStorm, killBackend, shot, startBackend, tid, waitHealthy, watchErrors } from "./helpers";

const GREEN = "rgb(61, 220, 132)";

test("warming up shows 'warming up', never a zero or a green", async ({ page, request }) => {
  const errors = watchErrors(page);
  // one minute of data per second on one-minute bars: the 60-bar warm-up lasts about a minute
  const r = await request.post("/replay?dataset=btc_ftx_2022&speed=60");
  expect(r.ok()).toBeTruthy();
  await page.goto("/ui/");
  await expect(tid(page, "app")).toHaveAttribute("data-display", "warming_up");
  await expect(tid(page, "gauge-level")).toHaveText("Warming up");
  await expect(tid(page, "gauge-score")).toHaveCount(0); // no number at all, in particular no 0
  await expect(tid(page, "gauge-note")).toContainText(/\d+ of 60 bars/);
  await expect(tid(page, "feed-pill")).toHaveText(/Warming up\s*\d+\/60/);
  await expect(tid(page, "feed-pill")).toHaveAttribute("data-tone", "warm");
  await expect(tid(page, "gauge-level")).not.toHaveCSS("color", GREEN);
  await expect(tid(page, "kind")).toHaveCount(0);
  await expect(tid(page, "ideas-empty")).toBeVisible(); // no trade ideas without a reading
  await expect(tid(page, "posture")).toContainText("Warming up");

  // risk numbers are withheld too, and say why
  await tid(page, "quick-add").click();
  await expect(tid(page, "position-row")).toContainText("warming up");
  await expect(tid(page, "kpi-var")).toContainText("--");
  await expect(tid(page, "versus-note")).toContainText("warming up");
  await tid(page, "run-sim").click();
  await expect(tid(page, "sim-message")).toContainText("still warming up");
  await expect(tid(page, "fan")).toHaveCount(0);
  await shot(page, "07-warming-up.png");

  // speeding up finishes the warm-up and a real score appears
  await tid(page, "speed-1hs").click();
  await expect(tid(page, "gauge-level")).toHaveText("Clear", { timeout: 30_000 });
  await expect(tid(page, "gauge-score")).toHaveText(/^\d{1,3}$/);
  await request.post("/stop");
  expect(errors).toEqual([]);
});

test("API down: banner, nothing looks live, retry recovers", async ({ page }) => {
  const errors = watchErrors(page);
  const port = 8788;
  let backend = startBackend(port);
  try {
    await waitHealthy(port);
    await page.goto(`http://127.0.0.1:${port}/ui/`);
    await jumpToStorm(page);
    await expect(tid(page, "app")).toHaveAttribute("data-stream", "open");

    await killBackend(backend); // the API dies mid-demo; the SSE stream drops with it
    await expect(tid(page, "api-down")).toBeVisible({ timeout: 20_000 });
    await expect(tid(page, "api-down")).toContainText("unreachable");
    await expect(tid(page, "app")).toHaveAttribute("data-display", "offline");
    await expect(tid(page, "feed-pill")).toHaveText(/API down/);
    await expect(tid(page, "gauge-level")).toHaveText("API offline");
    await expect(tid(page, "gauge-score")).toHaveCount(0); // the 93 is gone: not a live reading any more
    await expect(tid(page, "gauge-level")).not.toHaveCSS("color", GREEN);
    await expect(tid(page, "chart-offline")).toBeVisible();
    await expect(tid(page, "callout")).toHaveCount(0);
    await expect(tid(page, "jump-storm")).toBeDisabled();
    await expect(tid(page, "feed-picker")).toBeDisabled();
    await shot(page, "04-api-down.png");

    // retry while it is still down: the banner stays
    await tid(page, "retry").click();
    await expect(tid(page, "api-down")).toBeVisible();

    backend = startBackend(port);
    await waitHealthy(port);
    // "Retry now" brings it back at once; the background retry may get there first, which is also fine
    await tid(page, "retry").click({ timeout: 2000 }).catch(() => {});
    await expect(tid(page, "api-down")).toHaveCount(0, { timeout: 15_000 });
    await expect(tid(page, "app")).toHaveAttribute("data-api", "up");
    // the restarted server has no sessions: say so rather than keep the old reading
    await expect(tid(page, "gauge-level")).toHaveText("No feed");
    // and the stream reconnects for a new session
    await jumpToStorm(page);
    await expect(tid(page, "app")).toHaveAttribute("data-stream", "open");
    await expect(tid(page, "gauge-score")).toHaveText("93");
  } finally {
    await killBackend(backend);
  }
  expect(errors).toEqual([]);
});

test("stale feed is never shown as green (stream mocked)", async ({ page }) => {
  // A replay cannot go stale, and a real stale feed needs a dead exchange socket, so this one test
  // feeds the UI the exact snapshot shape the backend emits for a silent live feed.
  const errors = watchErrors(page);
  const now = Date.now() / 1000;
  const snap = {
    symbol: "BTCUSDT", state: "stale", level: "stale", score: null, last_score: 4.2, last_level: "green", peak_score: 31,
    kind: "calm", elevated_bars: 0, storm_heat_min: 0, carry: null, price: 101234.5, bid: 101234.0, ask: 101235.0, ts: now - 42,
    components: { vol: 0.05, jump: 0, drawdown: 0.02, spread: 0, curve: 0 },
    features: { vol_z: 0.2, jump_z: 0, jump_peak_z: 0.4, drawdown: 0.0002, runup: 0, drawdown_z: 0.1, spread: 1e-5, spread_ratio: 1.0 },
    vol: { sigma_bar_fast: 0.0002, sigma_bar_base: 0.0002, annualised_fast: 0.4, annualised_base: 0.4 },
    warmup: { have: 400, need: 90 },
    feed: { age_s: 42, stale_after_s: 10, accepted: 400, rejected: {}, gaps: 0, pending_outlier: false },
    session: { kind: "live", state: "running", error: null, started: now - 900, dataset: "binance:BTCUSDT", speed: null, progress: null,
      reconnects: 3, feed_error: null, pause_on: null, pause_reason: null, ahead: null, title: null, bar_seconds: 2, curve_tenors: [] },
    playbook: { stay_safe: "Feed is stale: treat risk as unknown and do not trust the last score.", early_warning: null, opportunity: null,
      disclaimer: "Heuristic reading of the data, not investment advice.", ideas: [] },
    new_transitions: [],
  };
  await page.addInitScript((s) => {
    class FakeEventSource {
      onmessage: ((e: { data: string }) => void) | null = null;
      onerror: (() => void) | null = null;
      private timer: number;
      constructor() {
        this.timer = window.setInterval(() => this.onmessage?.({ data: JSON.stringify(s) }), 300);
      }
      addEventListener() {}
      close() {
        window.clearInterval(this.timer);
      }
    }
    (window as unknown as { EventSource: unknown }).EventSource = FakeEventSource;
  }, snap);
  const json = (body: unknown) => ({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  await page.route("**/health", (r) =>
    r.fulfill(json({ status: "degraded", uptime_s: 900, latest: "BTCUSDT", notifiers: ["log"],
      sessions: { BTCUSDT: { kind: "live", state: "running", feed: "stale", error: null, progress: null } } })));
  await page.route("**/history?*", (r) => r.fulfill(json({ symbol: "BTCUSDT", points: 0, stride: 1, ts: [], price: [], score: [], level: [], transitions: [] })));
  await page.route("**/alerts", (r) =>
    r.fulfill(json({ alerts: [{ symbol: "BTCUSDT", kind: "stale", text: "stale", wall_time: now, level: "stale", age_s: 42 }], suppressed_by_debounce: 0 })));

  await page.goto("/ui/");
  await expect(tid(page, "app")).toHaveAttribute("data-display", "stale");
  await expect(tid(page, "feed-pill")).toHaveText(/Stale\s*42 s/);
  await expect(tid(page, "feed-pill")).toHaveAttribute("data-tone", "stale");
  await expect(tid(page, "gauge-level")).toHaveText("Feed stale");
  await expect(tid(page, "gauge-score")).toHaveCount(0);
  await expect(tid(page, "gauge-note")).toContainText("not trusted");
  for (const id of ["gauge-level", "feed-pill"]) await expect(tid(page, id)).not.toHaveCSS("color", GREEN);
  await expect(tid(page, "stale-banner")).toContainText("a silent feed is not a calm market");
  await expect(tid(page, "alert-feed")).toContainText("Feed stale");
  await expect(tid(page, "ideas-empty")).toBeVisible();
  await expect(tid(page, "carry-empty")).toBeVisible(); // no forward curve on a crypto feed
  await expect(tid(page, "play-pause")).toHaveText("Stop feed"); // a live feed has no transport
  await shot(page, "08-stale-feed.png");
  expect(errors).toEqual([]);
});

test("a feed that cannot start reports why and leaves the screen usable", async ({ page }) => {
  const errors = watchErrors(page);
  await page.route("**/yfinance?*", (r) =>
    r.fulfill({ status: 502, contentType: "application/json", body: JSON.stringify({ detail: "yfinance returned no rows for GC=F" }) }));
  await page.goto("/ui/");
  await expect(tid(page, "app")).toHaveAttribute("data-api", "up");
  await tid(page, "feed-picker").selectOption("yf:Gold");
  await expect(tid(page, "toast")).toContainText("yfinance returned no rows");
  await expect(tid(page, "feed-picker")).toBeEnabled();
  await expect(tid(page, "api-down")).toHaveCount(0); // a refused feed is not an outage
  expect(errors).toEqual([]);
});

test("SSE stream drops while the API stays up: says so, reconnects, carries on", async ({ page, request }) => {
  const errors = watchErrors(page);
  expect((await request.post("/replay?dataset=btc_ftx_2022&speed=600")).ok()).toBeTruthy();
  // the first stream connection is cut from the browser side after a second and a half
  await page.addInitScript(() => {
    const Real = window.EventSource;
    let n = 0;
    window.EventSource = class extends Real {
      constructor(url: string | URL) {
        super(url);
        if (n++ === 0)
          window.setTimeout(() => {
            this.close();
            this.onerror?.(new Event("error"));
          }, 1500);
      }
    };
  });
  await page.goto("/ui/");
  await expect(tid(page, "app")).toHaveAttribute("data-stream", "open");
  await expect(tid(page, "stream-dropped")).toBeVisible({ timeout: 5000 });
  await expect(tid(page, "feed-pill")).toHaveText(/Reconnecting/);
  await expect(tid(page, "api-down")).toHaveCount(0); // a dropped stream is not an API outage
  await expect(tid(page, "app")).toHaveAttribute("data-stream", "open", { timeout: 10_000 });
  await expect(tid(page, "app")).toHaveAttribute("data-reconnects", "1");
  await expect(tid(page, "stream-dropped")).toHaveCount(0);
  await expect(tid(page, "feed-pill")).toHaveText(/Replaying|Warming up/);
  // data keeps arriving on the new connection
  const before = await tid(page, "chart-price").textContent();
  await expect.poll(() => tid(page, "chart-price").textContent(), { timeout: 10_000 }).not.toBe(before);
  await request.post("/stop");
  expect(errors).toEqual([]);
});
