// The demo path, end to end against the real stack: SGX USDJPY replay to the tornado freeze,
// a position, the simulator, the playbook, then on to the end of the replay.
import { expect, test } from "@playwright/test";
import { jumpToStorm, shot, tid, watchErrors } from "./helpers";

test("SGX USDJPY storm: calm, tornado freeze, exposure, simulator, playbook, finish", async ({ page }) => {
  const errors = watchErrors(page);
  await page.goto("/ui/");

  // ---- nothing running yet: an honest idle state, not a zero
  await expect(tid(page, "app")).toHaveAttribute("data-api", "up");
  await expect(tid(page, "gauge-level")).toHaveText("No feed");
  await expect(tid(page, "gauge-score")).toHaveCount(0);
  await expect(tid(page, "feed-pill")).toHaveText(/No feed/);
  await expect(tid(page, "chart-empty")).toBeVisible();
  await expect(tid(page, "exposure-empty")).toBeVisible();
  await expect(tid(page, "siren-toggle")).toHaveAttribute("aria-pressed", "false"); // siren is off by default
  for (const title of ["Early warning", "Stay safe", "Predict what's next", "Spot opportunities"])
    await expect(page.getByRole("heading", { name: title })).toBeVisible(); // the four pillars

  // ---- the picker lists every feed family
  const options = await tid(page, "feed-picker").locator("option").allTextContents();
  for (const want of ["SGX USDJPY storm", "SGX EURUSD", "SGX USDSGD", "FTX collapse", "UK mini-budget", "BTCUSDT live", "ETHUSDT live", "Gold", "S&P 500"])
    expect(options.some((o) => o.includes(want)), `picker has ${want}`).toBeTruthy();

  // ---- calm: replay the SGX set at one hour of data per second
  await tid(page, "speed-1hs").click();
  await tid(page, "feed-picker").selectOption("replay:sgx_usdjpy_storm");
  await expect(tid(page, "gauge-level")).toHaveText("Clear");
  await expect(tid(page, "feed-pill")).toHaveText(/Replaying/);
  await expect(tid(page, "chart-symbol")).toHaveText("USDJPY");
  await tid(page, "quick-add").click(); // long 1m USDJPY
  await expect(tid(page, "kpi-var")).toContainText("JPY");
  await expect(tid(page, "kind")).toHaveText("calm");
  await expect(tid(page, "carry-unwind")).toHaveText("No unwind");
  await expect.poll(async () => Number(await tid(page, "chart").getAttribute("data-points"))).toBeGreaterThan(100);
  await shot(page, "01-calm.png");

  // ---- the early-warning pillar is one group (01a/01b/01c) and explains its own score from the engine's config
  await expect(page.locator(".pillar")).toHaveText(["01a", "01b", "01c", "02", "03", "04"]);
  await tid(page, "score-info").click();
  await expect(tid(page, "score-info-pop").locator("li[data-sensor]")).toHaveCount(5);
  await expect(tid(page, "score-info-pop").locator('li[data-sensor="vol"]')).toContainText("40%");
  await expect(tid(page, "score-formula")).toHaveText("score = 100 × (0.5 × weighted mean + 0.5 × worst sensor)");
  await expect(tid(page, "score-info-pop")).toContainText(/watch\s*25.*warning\s*50.*tornado\s*75/i);
  await page.keyboard.press("Escape");
  await expect(tid(page, "score-info-pop")).toHaveCount(0);

  // ---- user pause and resume
  await tid(page, "play-pause").click();
  await expect(tid(page, "feed-pill")).toHaveText(/Paused/);
  await expect(tid(page, "callout")).toHaveCount(0); // a manual pause is not an alert
  await tid(page, "play-pause").click();
  await expect(tid(page, "feed-pill")).toHaveText(/Replaying/);

  // ---- speed up into the storm; the replay freezes itself at the first tornado
  await tid(page, "speed-1ds").click();
  await expect(tid(page, "callout")).toBeVisible({ timeout: 60_000 });
  await expect(tid(page, "callout-title")).toContainText("Tornado warning");
  await expect(tid(page, "callout-title")).toContainText("29 Jul 18:01");
  await expect(tid(page, "callout-fall")).toContainText("5.0%");
  await expect(tid(page, "callout-fall")).toContainText(/still to fall/i);
  await expect(tid(page, "callout-hindsight")).toContainText("155.26");
  await expect(tid(page, "gauge-score")).toHaveText("93");
  await expect(tid(page, "gauge-level")).toHaveText("Tornado");
  await expect(tid(page, "app")).toHaveAttribute("data-display", "tornado");
  await expect(tid(page, "feed-pill")).toHaveText(/Frozen at tornado/);
  await expect(tid(page, "kind")).toHaveText("squall · developing"); // tornado level, but the alert is still young
  await expect(tid(page, "kind-note")).toHaveText(/^Tornado reading, \d+ min into the alert\. Squall becomes storm if it persists past 90 min\.$/);
  await expect(tid(page, "carry-unwind")).toHaveText("Carry unwind");
  await expect(tid(page, "alert-feed").locator('li[data-to="tornado"]')).toHaveCount(1);
  await expect(tid(page, "alert-feed").locator('li[data-to="tornado"]')).toContainText("carry unwind");
  await expect(tid(page, "drivers").locator('[data-top="true"]')).toHaveAttribute("data-driver", /vol|jump|curve/);
  await expect(tid(page, "chart").locator("canvas").first()).toBeVisible();
  await expect(tid(page, "vignette")).toBeVisible();
  await shot(page, "02-tornado-freeze.png");

  // ---- exposure at the freeze: 1m USDJPY, 99% VaR over the next 60 bars
  await expect(tid(page, "kpi-var")).toContainText("1.15m"); // engine: 1,149,318 JPY (closed form checked in make demo)
  await expect(tid(page, "kpi-es")).toContainText("1.32m");
  await expect(tid(page, "versus-note")).toContainText("8.0x");
  await expect(tid(page, "position-row")).toHaveCount(1);

  // unknown asset: flagged on its row, left out of the totals
  await tid(page, "pos-asset").fill("DOGE");
  await tid(page, "pos-size").fill("250k");
  await tid(page, "pos-add").click();
  await expect(tid(page, "position-row")).toHaveCount(2);
  await expect(tid(page, "row-problem")).toContainText("No feed for DOGE");
  await expect(tid(page, "kpi-var")).toContainText("1.15m");
  await tid(page, "remove-position").nth(1).click();
  await expect(tid(page, "position-row")).toHaveCount(1);

  // bad input never reaches the API
  await tid(page, "pos-asset").fill("");
  await tid(page, "pos-size").fill("lots");
  await tid(page, "pos-add").click();
  await expect(tid(page, "pos-error")).toContainText("Size must be a number");
  await expect(tid(page, "position-row")).toHaveCount(1);
  await tid(page, "pos-size").fill("");

  // ---- cyclone simulator
  await tid(page, "run-sim").click();
  await expect(tid(page, "fan")).toBeVisible();
  await expect(tid(page, "fan-band")).toBeVisible();
  await expect(tid(page, "p-loss")).toHaveText(/^\d{1,3}%$/);
  await expect(tid(page, "sim-p5")).toHaveText(/^-/); // the 5th percentile is a loss
  await expect(tid(page, "sim-p95")).toHaveText(/^\+/);
  await expect(tid(page, "sim-caption")).toContainText("2,000 paths");
  await expect(tid(page, "sim-caption")).toContainText("tornado regime");

  // ---- playbook: illustrative ideas and a hedge what-if priced by the real engine
  await expect(tid(page, "idea")).toHaveCount(3);
  await expect(tid(page, "idea").first()).toContainText("ILLUSTRATIVE");
  await expect(tid(page, "playbook").locator('[data-kind="long_vol"]')).toBeVisible();
  await expect(tid(page, "disclaimer")).toContainText("not advice");
  await expect(tid(page, "hedge-out")).toContainText("574.7k"); // half the book, half of 1,149,318
  await tid(page, "simulator").scrollIntoViewIfNeeded();
  await shot(page, "03-exposure-and-fan.png");
  await shot(page, "03b-full-page.png", true);

  // ---- 1280px laptop: nothing spills sideways
  await page.setViewportSize({ width: 1280, height: 720 });
  await page.evaluate(() => window.scrollTo(0, 0));
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(1280);
  await expect(tid(page, "callout")).toBeInViewport();
  await expect(tid(page, "gauge-score")).toBeInViewport();
  await shot(page, "05-tornado-1280x720.png");
  await page.setViewportSize({ width: 1440, height: 900 });

  // ---- siren: follows the score once switched on
  await tid(page, "siren-toggle").click();
  await expect(tid(page, "siren-toggle")).toHaveAttribute("aria-pressed", "true");
  await expect.poll(async () => Number(await tid(page, "siren-toggle").getAttribute("data-siren-level"))).toBeGreaterThan(0.8);
  await tid(page, "siren-toggle").click();
  await expect(tid(page, "siren-toggle")).toHaveAttribute("aria-pressed", "false");

  // ---- resume, run flat out to the end of the file
  await tid(page, "resume-callout").click();
  await expect(tid(page, "callout")).toHaveCount(0);
  await tid(page, "speed-max").click();
  await expect(tid(page, "replay-finished")).toBeVisible({ timeout: 60_000 });
  await expect(tid(page, "feed-pill")).toHaveText(/Replay finished/);
  await expect(tid(page, "play-pause")).toHaveText("Replay again");
  await expect(tid(page, "progress-label")).toHaveText("replay finished");
  await expect(tid(page, "gauge-note")).toContainText("Final reading");
  await expect(tid(page, "sim-outdated")).toContainText("run it again"); // the tornado fan is flagged as old
  await expect(tid(page, "kind")).toHaveText(/squall|storm|calm/);
  expect(await tid(page, "alert-feed").locator("li[data-to]").count()).toBeGreaterThan(20);
  await expect.poll(async () => Number(await tid(page, "chart").getAttribute("data-points"))).toBeGreaterThan(800);
  await shot(page, "06-replay-finished.png");

  // ---- empty portfolio again
  await tid(page, "clear-positions").click();
  await expect(tid(page, "exposure-empty")).toBeVisible();
  await expect(tid(page, "hedge")).toHaveCount(0);

  expect(errors, "no uncaught errors in the page").toEqual([]);
});

test("jump to the storm goes straight to the freeze frame", async ({ page }) => {
  const errors = watchErrors(page);
  await page.goto("/ui/");
  await jumpToStorm(page);
  await expect(tid(page, "callout-title")).toContainText("29 Jul 18:01");
  await expect(tid(page, "gauge-score")).toHaveText("93");
  // the freeze callout can launch the simulator; with no position the fan is per unit, in price terms
  await tid(page, "callout-simulate").click();
  await expect(tid(page, "fan")).toBeVisible();
  await expect(tid(page, "sim-caption")).toContainText("Price of 1 USDJPY");
  // a reload picks the frozen session back up from the server
  await page.reload();
  await expect(tid(page, "callout")).toBeVisible();
  await expect(tid(page, "feed-pill")).toHaveText(/Frozen at tornado/);
  expect(errors).toEqual([]);
});
