// The static demo (docs/), served under /storm-chaser/ with no backend at all: the same demo path as
// e2e/01-storm.spec.ts, with the same numbers, because the replay it plays was recorded from the Python engine.
import { expect, test, type Page } from "@playwright/test";
import { mkdirSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { SUBPATH } from "../playwright.static.config";

const SHOTS = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../assets/screenshots");
mkdirSync(SHOTS, { recursive: true });
const tid = (page: Page, id: string) => page.getByTestId(id);
const shot = (page: Page, name: string, fullPage = false) => page.screenshot({ path: path.join(SHOTS, name), fullPage, animations: "disabled" });

/** Everything the page asks the network for must live under the sub-path, and must load. */
function watch(page: Page) {
  const errors: string[] = [];
  const outside: string[] = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  page.on("console", (m) => m.type() === "error" && errors.push(m.text()));
  page.on("request", (r) => {
    const u = new URL(r.url());
    if (u.protocol.startsWith("http") && !u.pathname.startsWith(SUBPATH)) outside.push(r.url());
  });
  page.on("response", (r) => r.status() >= 400 && errors.push(`${r.status()} ${r.url()}`));
  return { errors, outside };
}

test("static demo under a sub-path: jump to the storm, exposure, simulator, finish", async ({ page }) => {
  const seen = watch(page);
  await page.goto(SUBPATH);

  // ---- idle: honest empty state, static note, live feeds marked as local-only
  await expect(tid(page, "app")).toHaveAttribute("data-api", "up");
  await expect(tid(page, "gauge-level")).toHaveText("No feed");
  await expect(tid(page, "static-note")).toContainText("run locally for live feeds");
  const options = await tid(page, "feed-picker").locator("option").allTextContents();
  for (const want of ["SGX USDJPY storm", "FTX collapse", "UK mini-budget"]) expect(options.some((o) => o.includes(want)), `picker has ${want}`).toBeTruthy();
  expect(options.some((o) => /live$/.test(o))).toBeFalsy(); // no dead Binance entries
  await expect(tid(page, "feed-picker").locator('optgroup[label="Live feeds: run locally"]')).toHaveCount(1);

  // ---- Jump to the storm: the SGX USDJPY replay freezes at its first tornado
  await tid(page, "jump-storm").click();
  await expect(tid(page, "callout")).toBeVisible({ timeout: 30_000 });
  await expect(tid(page, "callout-title")).toContainText("Tornado warning");
  await expect(tid(page, "callout-title")).toContainText("29 Jul 18:01");
  await expect(tid(page, "callout-fall")).toContainText("5.0%");
  await expect(tid(page, "callout-fall")).toContainText(/still to fall/i);
  await expect(tid(page, "callout-hindsight")).toContainText("155.26");
  await expect(tid(page, "callout-hindsight")).toContainText("3 Aug");
  await expect(tid(page, "gauge-score")).toHaveText("93");
  await expect(tid(page, "gauge-level")).toHaveText("Tornado");
  await expect(tid(page, "app")).toHaveAttribute("data-display", "tornado");
  await expect(tid(page, "feed-pill")).toHaveText(/Frozen at tornado/);
  await expect(tid(page, "kind")).toHaveText("squall · developing");
  await expect(tid(page, "carry-unwind")).toHaveText("Carry unwind");
  await expect(tid(page, "alert-feed").locator('li[data-to="tornado"]')).toHaveCount(1);
  await expect(tid(page, "progress-label")).toContainText("29 Jul 18:01");
  await expect(tid(page, "alert-count")).toHaveText("7 level changes · 1 alert sent"); // the alert log replays too
  await expect.poll(async () => Number(await tid(page, "chart").getAttribute("data-points"))).toBeGreaterThan(800);
  const fullYear = await page.evaluate(() => new Date(1785348060 * 1000).toISOString());
  expect(fullYear).toBe("2026-07-29T18:01:00.000Z"); // the timestamp the callout is showing
  await shot(page, "static-01-tornado-freeze.png");

  // ---- exposure, computed in the browser from the engine's regime: same figures as the full stack
  await tid(page, "quick-add").click(); // long 1m USDJPY
  await expect(tid(page, "kpi-var")).toContainText("1.15m"); // engine: 1,149,318 JPY
  await expect(tid(page, "kpi-es")).toContainText("1.32m");
  await expect(tid(page, "versus-note")).toContainText("8.0x");
  await tid(page, "pos-asset").fill("DOGE");
  await tid(page, "pos-size").fill("250k");
  await tid(page, "pos-add").click();
  await expect(tid(page, "row-problem")).toContainText("No feed for DOGE");
  await expect(tid(page, "kpi-var")).toContainText("1.15m");
  await tid(page, "remove-position").nth(1).click();

  // ---- cyclone simulator, in the browser
  await tid(page, "run-sim").click();
  await expect(tid(page, "fan")).toBeVisible();
  await expect(tid(page, "p-loss")).toHaveText(/^\d{1,3}%$/);
  await expect(tid(page, "sim-p5")).toHaveText(/^-/);
  await expect(tid(page, "sim-p95")).toHaveText(/^\+/);
  await expect(tid(page, "sim-caption")).toContainText("2,000 paths");
  await expect(tid(page, "sim-caption")).toContainText("tornado regime");

  // ---- playbook and the hedge what-if
  await expect(tid(page, "idea")).toHaveCount(3);
  await expect(tid(page, "disclaimer")).toContainText("not advice");
  await expect(tid(page, "hedge-out")).toContainText("574.7k");
  await shot(page, "static-02-full-page.png", true);

  // ---- resume and run flat out to the end of the file
  await tid(page, "resume-callout").click();
  await expect(tid(page, "callout")).toHaveCount(0);
  await tid(page, "speed-max").click();
  await expect(tid(page, "replay-finished")).toBeVisible({ timeout: 30_000 });
  await expect(tid(page, "play-pause")).toHaveText("Replay again");
  expect(await tid(page, "alert-feed").locator("li[data-to]").count()).toBeGreaterThan(20);

  // ---- a paced replay of another crisis moves, pauses and resumes
  await tid(page, "clear-positions").click();
  await tid(page, "speed-1ds").click();
  await tid(page, "freeze-toggle").uncheck();
  await tid(page, "feed-picker").selectOption("replay:btc_ftx_2022");
  await expect(tid(page, "chart-symbol")).toHaveText("BTCUSDT");
  await expect(tid(page, "feed-pill")).toHaveText(/Replaying/);
  await expect.poll(async () => Number(await tid(page, "chart").getAttribute("data-points")), { timeout: 20_000 }).toBeGreaterThan(300);
  await tid(page, "play-pause").click();
  await expect(tid(page, "feed-pill")).toHaveText(/Paused/);
  await expect(tid(page, "callout")).toHaveCount(0);
  await tid(page, "play-pause").click();
  await expect(tid(page, "feed-pill")).toHaveText(/Replaying/);

  // ---- and the third bundled crisis loads
  await tid(page, "feed-picker").selectOption("replay:gbpusd_minibudget_2022");
  await expect(tid(page, "chart-symbol")).toHaveText("GBPUSD");

  expect(seen.outside, "every request stays under the sub-path").toEqual([]);
  expect(seen.errors, "no page errors, no failed requests").toEqual([]);
});

test("390 px phone: the freeze frame fits, nothing scrolls sideways", async ({ page }) => {
  const seen = watch(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(SUBPATH);
  const overflow = () => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(await overflow(), "idle page").toBeLessThanOrEqual(0);
  await tid(page, "jump-storm").click();
  await expect(tid(page, "callout")).toBeVisible({ timeout: 30_000 });
  await expect(tid(page, "callout-title")).toContainText("29 Jul 18:01");
  await expect(tid(page, "callout-fall")).toContainText("5.0%");
  await tid(page, "quick-add").click();
  await expect(tid(page, "kpi-var")).toContainText("1.15m");
  await tid(page, "run-sim").click();
  await expect(tid(page, "fan")).toBeVisible();
  expect(await overflow(), "freeze frame with a position and a fan").toBeLessThanOrEqual(0);
  expect(await page.evaluate(() => document.documentElement.clientWidth)).toBe(390);
  await tid(page, "callout").scrollIntoViewIfNeeded();
  await shot(page, "static-03-phone-390.png");
  await shot(page, "static-04-phone-390-full.png", true);
  expect(seen.outside).toEqual([]);
  expect(seen.errors).toEqual([]);
});
