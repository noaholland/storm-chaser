import { defineConfig } from "@playwright/test";

export const PORT = 8899;
export const SUBPATH = "/storm-chaser/";

// The static demo (docs/, built by `make site`) served under a sub-path, as on GitHub Pages. No Python involved.
export default defineConfig({
  testDir: "./e2e-static",
  timeout: 120_000,
  expect: { timeout: 15_000 },
  workers: 1,
  retries: 0,
  reporter: [["list"]],
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    viewport: { width: 1440, height: 900 },
    deviceScaleFactor: 2,
    trace: "retain-on-failure",
  },
  webServer: {
    command: `node scripts/serve-static.mjs ${PORT} ${SUBPATH}`,
    url: `http://127.0.0.1:${PORT}${SUBPATH}`,
    timeout: 30_000,
    reuseExistingServer: false,
  },
});
