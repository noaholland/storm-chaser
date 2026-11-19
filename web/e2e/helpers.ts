import { expect, type Page } from "@playwright/test";
import { spawn, type ChildProcess } from "node:child_process";
import { mkdirSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

export const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
export const SHOTS = path.join(ROOT, "assets", "screenshots");
mkdirSync(SHOTS, { recursive: true });

export const tid = (page: Page, id: string) => page.getByTestId(id);

/** Fail the test on any uncaught exception in the page. */
export function watchErrors(page: Page): string[] {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  return errors;
}

export async function shot(page: Page, name: string, fullPage = false): Promise<string> {
  const file = path.join(SHOTS, name);
  await page.screenshot({ path: file, fullPage, animations: "disabled" });
  return file;
}

/** A second, disposable backend for the tests that kill it. */
export function startBackend(port: number): ChildProcess {
  return spawn(path.join(ROOT, ".venv", "bin", "python"), ["-m", "uvicorn", "stormchaser.api:app", "--host", "127.0.0.1", "--port", String(port)], {
    cwd: ROOT,
    stdio: "ignore",
  });
}

export async function waitHealthy(port: number, timeoutMs = 30_000): Promise<void> {
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    try {
      const r = await fetch(`http://127.0.0.1:${port}/health`);
      if (r.ok) return;
    } catch {
      /* not up yet */
    }
    await new Promise((r) => setTimeout(r, 200));
  }
  throw new Error(`backend on ${port} did not come up`);
}

export async function killBackend(p: ChildProcess): Promise<void> {
  if (p.exitCode !== null) return;
  await new Promise<void>((resolve) => {
    p.once("exit", () => resolve());
    p.kill("SIGKILL");
  });
}

/** Jump to the SGX USDJPY storm and wait for the freeze frame. */
export async function jumpToStorm(page: Page): Promise<void> {
  await tid(page, "jump-storm").click();
  await expect(tid(page, "callout")).toBeVisible({ timeout: 30_000 });
  await expect(tid(page, "gauge-level")).toHaveText("Tornado");
}
