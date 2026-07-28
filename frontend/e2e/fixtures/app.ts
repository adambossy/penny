/**
 * Shared E2E fixture: re-exports Playwright's test/expect against the app
 * booted by playwright.config.ts (backend in dev-principal mode + vite).
 *
 * The backend runs in dev-principal mode, so every browser request is scoped
 * without real auth. Feature specs extend this fixture rather than re-wiring
 * servers.
 */
export { expect, test } from "@playwright/test";
