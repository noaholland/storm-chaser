import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Asset URLs are relative (base "./"), so one build works wherever it is mounted:
//   default        -> web/dist, served by FastAPI under /ui/
//   --mode static  -> ../docs, the backend-free demo for GitHub Pages at /<repo>/. The replays it plays are
//                     written into docs/data/ afterwards by scripts/build_static.py (see `make site`).
export default defineConfig(({ mode }) => ({
  base: "./",
  plugins: [react()],
  define: mode === "static" ? { "import.meta.env.VITE_STATIC": JSON.stringify("1") } : {},
  build: { outDir: mode === "static" ? "../docs" : "dist", emptyOutDir: true, chunkSizeWarningLimit: 900 },
  server: { port: 5173 },
}));
