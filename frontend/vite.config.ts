import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import path from "node:path";
import os from "node:os";

// Frontend dev server proxies /api/* to the Python backend so the browser
// talks to a single origin (no CORS) and cookies/streaming pass through.
const BACKEND_URL = process.env.BACKEND_URL ?? "http://localhost:8000";

// Resolve @adambossy/agent-ui to the local source checkout so edits in
// ~/code/agent-ui hot-reload into Penny. Mirrors the alias the playground
// uses internally. Set AGENT_UI_USE_PUBLISHED=1 to instead resolve the
// published package from node_modules (CI / production builds, where the
// local source checkout doesn't exist).
const AGENT_UI_SRC = path.resolve(
  process.env.AGENT_UI_PATH ?? path.join(os.homedir(), "code/agent-ui/packages/agent-ui"),
  "src",
);
const usePublished = process.env.AGENT_UI_USE_PUBLISHED === "1";

export default defineConfig({
  plugins: [react(), tailwindcss()],
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
