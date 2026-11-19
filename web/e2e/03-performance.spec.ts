// Fast replays must not stall the page. Kept in its own file so it can run without tracing and at 1x:
// both add main-thread work that belongs to the test harness, not to the dashboard.
import { expect, test } from "@playwright/test";
import { tid, watchErrors } from "./helpers";

test.use({ deviceScaleFactor: 1, trace: "off" });

test("replay at maximum speed: two months in one burst, no main-thread stalls, finish is announced", async ({ page }) => {
  const errors = watchErrors(page);
  await page.goto("/ui/");
  await expect(tid(page, "app")).toHaveAttribute("data-api", "up");
  // Jank is measured as main-thread long tasks. (Frame rate is not asserted: headless Chromium rasterises
  // in software at 2x, so its rAF rate says nothing about the page. Headed Chrome held 59 fps on this run.)
  await page.evaluate(() => {
    const w = window as unknown as { __long: number[] };
    w.__long = [];
    new PerformanceObserver((l) => l.getEntries().forEach((e) => w.__long.push(e.duration))).observe({ entryTypes: ["longtask"] });
  });
  await tid(page, "freeze-toggle").uncheck();
  await tid(page, "speed-max").click();
  await tid(page, "feed-picker").selectOption("replay:sgx_usdjpy_2026"); // 64k one-minute bars
  await expect(tid(page, "replay-finished")).toBeVisible({ timeout: 60_000 });
  await expect(tid(page, "chart-symbol")).toHaveText("USDJPY");
  await expect.poll(async () => Number(await tid(page, "chart").getAttribute("data-points"))).toBeGreaterThan(1000);
  await expect(tid(page, "alert-feed").locator('li[data-to="tornado"]')).not.toHaveCount(0);
  await expect(tid(page, "alert-count")).toContainText(/\d{3} level changes/);
  const long = await page.evaluate(() => (window as unknown as { __long: number[] }).__long);
  expect(Math.max(0, ...long), `longest main-thread task, ms (all: ${long.map(Math.round).join(",")})`).toBeLessThan(300);

  // a set without quotes or a forward curve says so instead of drawing empty charts
  await tid(page, "feed-picker").selectOption("replay:gbpusd_minibudget_2022");
  await expect(tid(page, "chart-symbol")).toHaveText("GBPUSD");
  await expect(tid(page, "replay-finished")).toBeVisible({ timeout: 30_000 });
  await expect(tid(page, "carry-empty")).toBeVisible();
  await expect(tid(page, "drivers").locator('[data-driver="spread"]')).toContainText("no quotes");
  await tid(page, "replay-again").click();
  await expect(tid(page, "replay-finished")).toBeVisible({ timeout: 30_000 });
  expect(errors).toEqual([]);
});
