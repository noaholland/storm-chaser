// Serves the built static site (../docs) under a sub-path, the way GitHub Pages serves a project site:
//   node scripts/serve-static.mjs [port] [/sub-path/]      -> http://127.0.0.1:8899/storm-chaser/
// Anything outside the sub-path is a 404, so a build that assumed it lives at "/" fails loudly.
import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../docs");
const port = Number(process.argv[2] || 8899);
const base = (process.argv[3] || "/storm-chaser/").replace(/\/?$/, "/");
const TYPES = { ".html": "text/html; charset=utf-8", ".js": "text/javascript", ".css": "text/css", ".json": "application/json", ".woff": "font/woff", ".woff2": "font/woff2", ".png": "image/png", ".svg": "image/svg+xml" };

createServer(async (req, res) => {
  const url = decodeURIComponent(new URL(req.url, "http://x").pathname);
  if (url + "/" === base) return res.writeHead(301, { Location: base }).end();
  if (!url.startsWith(base)) return res.writeHead(404).end("not found: the site lives under " + base);
  const file = path.join(ROOT, path.normalize(url.slice(base.length) || "index.html"));
  if (!file.startsWith(ROOT)) return res.writeHead(403).end();
  try {
    const body = await readFile(file.endsWith(path.sep) ? path.join(file, "index.html") : file);
    res.writeHead(200, { "Content-Type": TYPES[path.extname(file)] || "application/octet-stream", "Cache-Control": "no-store" }).end(body);
  } catch {
    res.writeHead(404).end("not found");
  }
}).listen(port, "127.0.0.1", () => console.log(`static site on http://127.0.0.1:${port}${base}`));
