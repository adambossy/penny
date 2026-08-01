import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import fs from "node:fs";
import path from "node:path";
import os from "node:os";

// Frontend dev server proxies /api/* to the Python backend so the browser
// talks to a single origin (no CORS) and cookies/streaming pass through.
const BACKEND_URL = process.env.BACKEND_URL ?? "http://localhost:8000";

// Resolve @adambossy/agent-ui to the local source checkout so edits in
// ~/code/agent-ui hot-reload into Penny. Mirrors the alias the playground
// uses internally. When the checkout doesn't exist (CI, other machines) the
// published package from node_modules is used automatically; set
// AGENT_UI_USE_PUBLISHED=1 to force it even with a checkout present.
const AGENT_UI_SRC = path.resolve(
  process.env.AGENT_UI_PATH ?? path.join(os.homedir(), "code/agent-ui/packages/agent-ui"),
  "src",
);
const usePublished =
  process.env.AGENT_UI_USE_PUBLISHED === "1" || !fs.existsSync(AGENT_UI_SRC);

// Stamp the build so `penny serve` can tell a dist built by this app from a
// stale or foreign one (the incident: a gitignored pre-split dist — Clerk
// landing page and all — survived the single-player merge and was served
// happily). serve refuses a dist without the stamp; see penny/cli.py.
function buildStamp() {
  let outDir = "";
  return {
    name: "penny-build-stamp",
    apply: "build" as const,
    configResolved(config: { root: string; build: { outDir: string } }) {
      outDir = path.resolve(config.root, config.build.outDir);
    },
    closeBundle() {
      fs.writeFileSync(
        path.join(outDir, "penny-build.json"),
        JSON.stringify(
          { app: "penny-single-player", builtAt: new Date().toISOString() },
          null,
          2,
        ) + "\n",
      );
    },
  };
}

export default defineConfig({
  plugins: [react(), tailwindcss(), buildStamp()],
  resolve: {
    // Force a single React instance. Without this, source-aliasing
    // agent-ui makes its sibling `node_modules/react` resolve as a
    // SECOND React copy (separate from Penny's `frontend/node_modules/react`),
    // and hooks fire against a null dispatcher → blank screen.
    dedupe: ["react", "react-dom", "react/jsx-runtime", "react/jsx-dev-runtime"],
    alias: [
      {
        find: "@penny/ui/styles.css",
        replacement: path.resolve(__dirname, "packages/ui/src/theme.css"),
      },
      {
        find: "@penny/ui/gallery",
        replacement: path.resolve(__dirname, "packages/ui/src/Gallery.tsx"),
      },
      { find: "@penny/ui", replacement: path.resolve(__dirname, "packages/ui/src/index.ts") },
      {
        find: "@penny/chat-ui",
        replacement: path.resolve(__dirname, "packages/chat-ui/src/index.ts"),
      },
      ...(usePublished
        ? []
        : [
            {
              find: "@adambossy/agent-ui/styles.css",
              replacement: path.join(AGENT_UI_SRC, "styles.css"),
            },
            { find: "@adambossy/agent-ui", replacement: path.join(AGENT_UI_SRC, "index.ts") },
          ]),
    ],
  },
  server: {
    // Pinned: every doc/bookmark in this project says 5174 (historically vite
    // bumped here because another dev server squatted on 5173).
    port: 5174,
    strictPort: true,
    host: true,
    // Allow the ngrok tunnel host (dev-only, for phone testing over a tunnel).
    allowedHosts: true,
    proxy: {
      "/api": {
        target: BACKEND_URL,
        changeOrigin: true,
        ws: false,
      },
      // The sandboxed agent reaches the MCP tool server through this same
      // origin (so one public tunnel serves the UI, /api, and /mcp).
      "/mcp": {
        target: BACKEND_URL,
        changeOrigin: true,
        ws: false,
      },
    },
  },
});
