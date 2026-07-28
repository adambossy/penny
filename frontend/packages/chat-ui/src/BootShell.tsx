import { Menu } from "lucide-react";
import { Header, IconButton } from "@penny/ui";

/**
 * Chrome-only stand-in rendered while the app boots: the header paints
 * immediately so a returning user sees "the app" instead of a blank frame, and
 * the swap to the real shell is seamless because the chrome geometry matches.
 * The controls are inert placeholders.
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
