import { defineConfig } from "@playwright/test";

export const PORT = 8787;
export const BASE = `http://127.0.0.1:${PORT}`;

// The tests share one backend (sessions are server state), so they run one at a time.
export default defineConfig({
  testDir: "./e2e",
  timeout: 120_000,
  expect: { timeout: 15_000 },
  workers: 1,
  fullyParallel: false,
  retries: 0,
  reporter: [["list"]],
  use: {
    baseURL: BASE,
    viewport: { width: 1440, height: 900 },
    deviceScaleFactor: 2,
    launchOptions: { args: ["--autoplay-policy=no-user-gesture-required"] },
    trace: "retain-on-failure",
  },
  webServer: {
    command: `make -C .. app PORT=${PORT} HOST=127.0.0.1`,
    url: `${BASE}/health`,
    timeout: 180_000,
    reuseExistingServer: false,
    stdout: "ignore",
    stderr: "pipe",
  },
});
