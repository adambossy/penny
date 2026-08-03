import { defineConfig } from "@playwright/test";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

/**
 * E2E harness: boots the real backend on dedicated ports (backend :8183,
 * vite :5183) so it never collides with a developer's own servers. Runs in dev-principal mode with throwaway SQLite
 * finance + web DBs under test-results/, then drives
 * a headless chromium against the SPA. Phase 1a owns this bootstrap; later
 * phases only add feature specs on top of e2e/fixtures/app.ts.
 */

const BACKEND_DIR = path.resolve(__dirname, "../backend");
const E2E_DB_DIR = path.resolve(__dirname, "test-results");

// The same well-known dev principal the backend test suite uses.
const DEV_USER_ID = "11111111-1111-1111-1111-111111111111";
const DEV_HOUSEHOLD_ID = "22222222-2222-2222-2222-222222222222";

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  use: {
    baseURL: "http://127.0.0.1:5183",
  },
  projects: [{ name: "chromium", use: { browserName: "chromium" } }],
  webServer: [
    {
      command: `sh -c 'mkdir -p "${E2E_DB_DIR}" && uv run uvicorn penny.api.main:app --host 127.0.0.1 --port 8183'`,
      cwd: BACKEND_DIR,
      url: "http://127.0.0.1:8183/api/health",
      reuseExistingServer: !process.env.CI,
      timeout: 120_000,
      env: {
        DATABASE_URL: `sqlite:///${E2E_DB_DIR}/penny-e2e.db`,
        PENNY_WEB_DATABASE_URL: `sqlite:///${E2E_DB_DIR}/penny-e2e-web.db`,
        // The harness runs in dev-principal mode (single-player, no auth).
        PENNY_AUTH_MODE: process.env.PENNY_AUTH_MODE ?? "dev",
        PENNY_DEV_USER_ID: DEV_USER_ID,
        PENNY_DEV_HOUSEHOLD_ID: DEV_HOUSEHOLD_ID,
        PENNY_DEV_SESSION_MODE: "individual",
        PENNY_LANGFUSE_ENABLED: "false",
        // Throwaway Fernet key so the BYO-credential vault can encrypt at rest
        // (byo-credential.spec.ts). Test-only — encrypts ephemeral E2E creds in
        // the throwaway SQLite DB above; not a production secret.
        PENNY_PLAID_TOKEN_KEY:
          process.env.PENNY_PLAID_TOKEN_KEY ??
          "kSwSnHyO-f-TCPv-9uYmzHPPgO9ONdR5U83WChmXEeQ=",
      },
    },
    {
      command: "npm run dev -- --port 5183",
      url: "http://127.0.0.1:5183",
      reuseExistingServer: !process.env.CI,
      timeout: 120_000,
      env: {
        // Point the E2E vite proxy at the E2E backend (dev setups often
        // have their own server on :8000).
        BACKEND_URL: "http://127.0.0.1:8183",
      },
    },
  ],
});
