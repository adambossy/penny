import { lazy, StrictMode, Suspense } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Navigate, Route, Routes } from "react-router";
import { registerToolRenderer } from "@adambossy/agent-ui";
import { ChatRoute, CONVERSATION_PATH, PlaidLinkCard, PlaidOauthGate } from "@penny/chat-ui";
import { AppShell } from "./AppShell";
import "./index.css";

// Render the connect_bank_account (new link) and relink_account (update-mode
// re-auth) tool outputs as the same inline Plaid Link card — it branches on the
// output `mode` (`"hosted"` vs `"update"`).
registerToolRenderer("connect_bank_account", PlaidLinkCard);
registerToolRenderer("relink_account", PlaidLinkCard);

// `/` is always a new chat; a conversation lives at /c/:id; anything unmatched
// goes home (replace, so the dead URL doesn't trap back).
//
// PlaidOauthGate sits ABOVE the route table (it intercepts the bank's OAuth
// return, which lands with no path context) so both chat routes render the
// identical element type — wrapping only `/` would change the tree shape
// across the first-send replace-navigation and remount the in-flight chat,
// defeating the stable key.
function AppRoutes() {
  return (
    <PlaidOauthGate>
      <Routes>
        <Route path="/" element={<ChatRoute />} />
        <Route path={CONVERSATION_PATH} element={<ChatRoute />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </PlaidOauthGate>
  );
}

// Dev-only design-system preview, lazily loaded through its own subpath entry
// (not the package index — that's statically imported by AppShell, which would
// pin Gallery into the main chunk) so it code-splits out of production builds.
const Gallery = lazy(() =>
  import("@penny/ui/gallery").then((m) => ({ default: m.Gallery })),
);

function Root() {
  return (
    <Routes>
      {/* Dev-only design-system preview: `/ui` renders the @penny/ui Gallery.
          The route is only registered in dev builds (import.meta.env.DEV), so it
          never exists in production. */}
      {import.meta.env.DEV && (
        <Route
          path="/ui/*"
          element={
            <Suspense fallback={null}>
              <Gallery />
            </Suspense>
          }
        />
      )}
      <Route
        path="*"
        element={
          <AppShell>
            <AppRoutes />
          </AppShell>
        }
      />
    </Routes>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <BrowserRouter>
      <Root />
    </BrowserRouter>
  </StrictMode>,
);
