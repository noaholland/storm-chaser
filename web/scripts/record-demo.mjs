// Records the demo walkthrough to assets/demo.mp4 (about 2 min, 1440x900, H.264) and a short cut, assets/demo-short.mp4.
// Run with `make video`. It starts its own backend on port 8790, drives the real dashboard in headless Chromium,
// grabs frames over the DevTools screencast (sharper than Playwright's built-in video) and encodes them with ffmpeg.
import { chromium } from "@playwright/test";
import { spawn, spawnSync } from "node:child_process";
import { mkdirSync, mkdtempSync, writeFileSync, statSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const PORT = Number(process.env.DEMO_PORT || 8790);
const BASE = `http://127.0.0.1:${PORT}`;
const W = 1440, H = 900;
const OUT = path.join(ROOT, "assets");
const FRAMES = mkdtempSync(path.join(process.env.TMPDIR || os.tmpdir(), "storm-demo-"));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function run(cmd, args) {
  const r = spawnSync(cmd, args, { encoding: "utf8" });
  if (r.status !== 0) throw new Error(`${cmd} failed: ${(r.stderr || "").slice(-1500)}`);
  return r.stdout;
}

async function waitHealthy() {
  for (let i = 0; i < 150; i++) {
    try {
      if ((await fetch(`${BASE}/health`)).ok) return;
    } catch {
      /* not up yet */
    }
    await sleep(200);
  }
  throw new Error("backend did not come up");
}

const server = spawn(path.join(ROOT, ".venv", "bin", "python"), ["-m", "uvicorn", "stormchaser.api:app", "--host", "127.0.0.1", "--port", String(PORT)], { cwd: ROOT, stdio: "ignore" });
let browser;
try {
  await waitHealthy();
  browser = await chromium.launch({ args: ["--autoplay-policy=no-user-gesture-required"] });
  const ctx = await browser.newContext({ viewport: { width: W, height: H }, deviceScaleFactor: 1 });
  const page = await ctx.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  await page.goto(`${BASE}/ui/`);
  await page.getByTestId("app").and(page.locator('[data-api="up"]')).waitFor();
  await page.evaluate(() => document.fonts.ready);

  // ---- overlay: caption bar, fake cursor (headless Chromium draws none), highlight ring
  await page.addStyleTag({
    content: `
    #demo-cap { position: fixed; left: 50%; transform: translateX(-50%); z-index: 9999; max-width: 1100px; padding: 12px 22px 13px;
      background: rgba(4,10,8,0.93); border: 1px solid #3ddc84; box-shadow: 0 10px 40px rgba(0,0,0,.75); color: #ecf7f1;
      font: 600 25px/1.2 "Barlow Condensed","Arial Narrow",sans-serif; letter-spacing: .03em; text-align: center;
      opacity: 0; transition: opacity .35s; pointer-events: none; white-space: nowrap; }
    #demo-cap[data-on="true"] { opacity: 1; }
    #demo-cap[data-pos="bottom"] { bottom: 22px; } #demo-cap[data-pos="top"] { top: 84px; }
    #demo-cap b { color: #3ddc84; font-family: "IBM Plex Mono",monospace; font-size: 17px; font-weight: 500; margin-right: 12px; vertical-align: 2px; }
    #demo-cap small { display: block; font: 400 16px/1.3 "Barlow",sans-serif; letter-spacing: 0; color: #b4cdc2; margin-top: 3px; }
    #demo-cur { position: fixed; z-index: 10000; width: 22px; height: 22px; margin: -11px 0 0 -11px; border-radius: 50%;
      border: 2px solid #fff; background: rgba(255,255,255,.28); box-shadow: 0 0 0 1px rgba(0,0,0,.6); pointer-events: none; left: -50px; top: -50px;
      transition: transform .12s, background .12s; }
    #demo-cur[data-down="true"] { transform: scale(.6); background: rgba(61,220,132,.9); }
    #demo-ring { position: fixed; z-index: 9998; border: 3px solid #ffd23f; box-shadow: 0 0 0 4px rgba(255,210,63,.25), 0 0 30px rgba(255,210,63,.45);
      pointer-events: none; opacity: 0; transition: opacity .3s; }
    #demo-ring[data-on="true"] { opacity: 1; }`,
  });
  await page.evaluate(() => {
    for (const id of ["demo-cap", "demo-cur", "demo-ring"]) {
      const el = document.createElement("div");
      el.id = id;
      document.body.appendChild(el);
    }
    const cur = document.getElementById("demo-cur");
    window.addEventListener("mousemove", (e) => { cur.style.left = e.clientX + "px"; cur.style.top = e.clientY + "px"; }, true);
    window.addEventListener("mousedown", () => (cur.dataset.down = "true"), true);
    window.addEventListener("mouseup", () => setTimeout(() => (cur.dataset.down = "false"), 160), true);
  });
  const caption = (step, text, sub = "", pos = "bottom") =>
    page.evaluate(([step, text, sub, pos]) => {
      const c = document.getElementById("demo-cap");
      c.dataset.pos = pos;
      c.innerHTML = (step ? `<b>${step}</b>` : "") + text + (sub ? `<small>${sub}</small>` : "");
      c.dataset.on = "true";
    }, [step, text, sub, pos]);
  const ring = async (loc, pad = 5) => {
    const b = loc ? await loc.boundingBox() : null;
    await page.evaluate(([b, pad]) => {
      const r = document.getElementById("demo-ring");
      if (!b) return void (r.dataset.on = "false");
      Object.assign(r.style, { left: b.x - pad + "px", top: b.y - pad + "px", width: b.width + 2 * pad + "px", height: b.height + 2 * pad + "px" });
      r.dataset.on = "true";
    }, [b, pad]);
  };
  const tid = (id) => page.getByTestId(id);
  const moveTo = async (loc) => {
    const b = await loc.boundingBox();
    await page.mouse.move(b.x + b.width / 2, b.y + b.height / 2, { steps: 28 });
    await sleep(350);
  };
  const click = async (loc) => {
    await moveTo(loc);
    await page.mouse.down();
    await sleep(90);
    await page.mouse.up();
  };
  const scrollTo = async (y) => {
    await page.evaluate((y) => window.scrollTo({ top: y, behavior: "smooth" }), y);
    await sleep(1100);
  };

  // ---- frame capture
  const cdp = await ctx.newCDPSession(page);
  const frames = [];
  let n = 0;
  cdp.on("Page.screencastFrame", (f) => {
    const file = path.join(FRAMES, `f${String(n++).padStart(6, "0")}.jpg`);
    writeFileSync(file, Buffer.from(f.data, "base64"));
    frames.push({ file, t: f.metadata.timestamp });
    cdp.send("Page.screencastFrameAck", { sessionId: f.sessionId }).catch(() => {});
  });
  await page.mouse.move(720, 450);
  await cdp.send("Page.startScreencast", { format: "jpeg", quality: 93, maxWidth: W, maxHeight: H, everyNthFrame: 1 });
  const t0 = Date.now();
  const marks = {};
  const mark = (k) => (marks[k] = (Date.now() - t0) / 1000);

  // ================================================================ the walkthrough
  await caption("", "Storm Chaser", "Cloudy with a chance of margin calls: an early-warning radar for market storms");
  await sleep(4500);

  // 1. calm
  await caption("1 / 7", "A calm market", "SGX USDJPY, 28 July 2026, replayed at one hour of data per second");
  await click(tid("speed-1hs"));
  await moveTo(tid("feed-picker"));
  await tid("feed-picker").selectOption("replay:sgx_usdjpy_storm");
  await tid("gauge-level").filter({ hasText: "Clear" }).waitFor({ timeout: 30000 });
  await sleep(2500);
  await ring(tid("gauge"));
  await caption("1 / 7", "Storm score near zero: all clear", "Five sensors watch volatility, jumps, drawdown speed, bid/ask spread and the forward curve");
  await sleep(6000);
  await ring(null);

  // 1b. how the score works
  await click(tid("score-info"));
  await caption("1 / 7", "How the score works", "Weights and thresholds are read from the engine's config, not typed into the page");
  await sleep(6500);
  await click(tid("score-info"));
  await sleep(600);

  // 2. jump to the storm
  await caption("2 / 7", "Jump to the storm", "Fast-forward the same file until the engine raises its first tornado warning");
  await moveTo(tid("jump-storm"));
  await sleep(1200);
  mark("jump");
  await click(tid("jump-storm"));
  await caption("2 / 7", "The gauge climbs: watch, warning, tornado");
  await tid("callout").waitFor({ timeout: 60000 });
  mark("freeze");
  await sleep(2500);

  // 3. the freeze frame
  await ring(tid("callout"));
  await caption("3 / 7", "Tornado warning, 29 Jul 18:01 UTC: the replay freezes itself", "Hindsight the engine never saw: USDJPY still had 5% to fall");
  await sleep(9000);
  await ring(tid("gauge"));
  await caption("3 / 7", "Score 93. A squall for now: it becomes a storm if the alert persists", "The badge says how long it has lasted; the level says how bad it is");
  await sleep(7000);

  // 4. carry
  mark("carry");
  await ring(tid("carry"));
  await moveTo(tid("carry-unwind"));
  await caption("4 / 7", "Forward curve: carry unwind flagged", "The curve dislocates while the spot move hurts whoever was earning the carry");
  await sleep(8000);
  await ring(tid("alert-feed"));
  await caption("4 / 7", "Every level change lands in the alert feed", "Debounced, with the sensor that drove it");
  await sleep(5000);
  await ring(null);

  // 5. stay safe
  await scrollTo(100000);
  mark("safe");
  await caption("5 / 7", "Stay safe: what does this weather cost me?", "Add a long 1m USDJPY position", "top");
  await click(tid("pos-asset"));
  await page.keyboard.type("USDJPY", { delay: 110 });
  await click(tid("pos-size"));
  await page.keyboard.type("1m", { delay: 160 });
  await sleep(500);
  await click(tid("pos-add"));
  await tid("kpi-var").waitFor({ timeout: 15000 });
  await sleep(800);
  await ring(tid("exposure"));
  await caption("5 / 7", "99% one-hour VaR: 1.15m JPY, 8x a calm market", "Value at risk, expected shortfall and stressed loss, priced by the engine in the storm regime", "top");
  await sleep(8500);
  await ring(null);

  // 6. simulator
  mark("sim");
  await caption("6 / 7", "Predict what's next: the cyclone simulator", "2,000 GARCH paths started from the tornado regime", "top");
  await click(tid("run-sim"));
  await tid("fan").waitFor({ timeout: 30000 });
  await sleep(900);
  await ring(tid("simulator"));
  await caption("6 / 7", "The fan of outcomes for this position over the next hour", "5th to 95th percentile P&L, with the chance of a loss", "top");
  await sleep(8500);
  await ring(null);

  // 7. playbook
  mark("playbook");
  await ring(tid("playbook"));
  await moveTo(tid("idea").first());
  await caption("7 / 7", "Spot opportunities: the storm playbook", "Illustrative ideas only, not advice: hedge, long volatility, carry", "top");
  await sleep(7000);
  await ring(tid("hedge"));
  await moveTo(tid("hedge-ratio"));
  await caption("7 / 7", "Hedge what-if: half the book hedged halves the VaR", "Re-priced by the same engine as you drag", "top");
  for (const v of ["60", "70", "80", "70", "60", "50"]) {
    await tid("hedge-ratio").evaluate((el, v) => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set.call(el, v); // React tracks the native setter
      el.dispatchEvent(new Event("input", { bubbles: true }));
    }, v);
    await sleep(650);
  }
  await sleep(3500);
  await ring(null);

  // outro
  await scrollTo(0);
  mark("outro");
  await caption("", "Storm Chaser", "Early warning · Stay safe · Predict what's next · Spot opportunities");
  await sleep(5000);
  mark("end");
  // =================================================================================

  await cdp.send("Page.stopScreencast");
  const total = (Date.now() - t0) / 1000;
  await browser.close();
  browser = null;
  if (errors.length) throw new Error("page errors during the recording: " + errors.join("; "));

  // ---- encode: screencast frames arrive only when the page changes, so each one carries its own duration
  const origin = frames[0].t;
  const lines = ["ffconcat version 1.0"];
  frames.forEach((f, i) => {
    const next = i + 1 < frames.length ? frames[i + 1].t : origin + total;
    lines.push(`file '${f.file}'`, `duration ${Math.max(0.001, next - f.t).toFixed(4)}`);
  });
  lines.push(`file '${frames.at(-1).file}'`);
  const list = path.join(FRAMES, "frames.txt");
  writeFileSync(list, lines.join("\n") + "\n");
  mkdirSync(OUT, { recursive: true });
  const full = path.join(OUT, "demo.mp4");
  const x264 = ["-c:v", "libx264", "-preset", "slow", "-crf", "21", "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-an"];
  run("ffmpeg", ["-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", list, "-vf", `fps=30,scale=${W}:${H}:flags=lanczos`, ...x264, full]);

  // ---- short cut: jump -> freeze, the position and the fan, a glimpse of the playbook
  const cuts = [
    [marks.jump - 2.5, marks.freeze + 9],
    [marks.safe + 5.5, marks.safe + 12.5],
    [marks.sim + 1, marks.sim + 8],
    [marks.playbook + 1, marks.playbook + 6],
  ];
  const filter =
    cuts.map(([a, b], i) => `[0:v]trim=${a.toFixed(2)}:${b.toFixed(2)},setpts=PTS-STARTPTS[v${i}]`).join(";") +
    `;${cuts.map((_, i) => `[v${i}]`).join("")}concat=n=${cuts.length}:v=1[out]`;
  const short = path.join(OUT, "demo-short.mp4");
  run("ffmpeg", ["-y", "-loglevel", "error", "-i", full, "-filter_complex", filter, "-map", "[out]", ...x264, short]);

  writeFileSync(path.join(FRAMES, "marks.json"), JSON.stringify({ marks, cuts, frames: frames.length, total }, null, 2));
  for (const f of [full, short]) {
    const dur = run("ffprobe", ["-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", f]).trim();
    console.log(`${path.relative(ROOT, f)}  ${Number(dur).toFixed(1)} s  ${(statSync(f).size / 1e6).toFixed(2)} MB`);
  }
  console.log(`frames: ${frames.length} in ${FRAMES}`);
  console.log("marks: " + JSON.stringify(marks));
} finally {
  if (browser) await browser.close().catch(() => {});
  server.kill("SIGKILL");
}
