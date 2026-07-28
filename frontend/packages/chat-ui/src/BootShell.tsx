import { Menu } from "lucide-react";
import { Header, IconButton } from "@penny/ui";

/**
 * Chrome-only stand-in rendered while clerk-js confirms a hinted session
 * (see AuthGate's __client_uat check): the app header paints immediately so a
 * returning user sees "the app" instead of a blank frame, and the swap to the
 * real AppShell is seamless because the chrome geometry matches. The controls
 * are inert placeholders — the session isn't confirmed yet.
 */
export function BootShell() {
  return (
    <div className="flex h-full flex-col" data-testid="boot-shell">
      <Header
        leading={
          <IconButton aria-label="Open chat history" disabled>
            <Menu className="h-5 w-5" />
          </IconButton>
        }
        actions={<span className="h-7 w-7 animate-pulse rounded-full bg-cream" />}
      />
      <main className="flex-1" />
    </div>
  );
}
