import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import path from "node:path";

// Frontend dev server proxies /api/* to the Python backend so the browser
// talks to a single origin (no CORS) and cookies/streaming pass through.
const BACKEND_URL = process.env.BACKEND_URL ?? "http://localhost:8000";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    // Force a single React instance. Historically, source-aliasing
    // agent-ui made its sibling `node_modules/react` resolve as a
    // SECOND React copy (separate from Penny's `frontend/node_modules/react`),
    // and hooks fired against a null dispatcher → blank screen. The alias is
    // gone (agent-ui resolves from node_modules like any dependency), but the
    // dedupe stays as a harmless guard.
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
