import { useCallback, useRef, useState } from "react";
import type { ReactNode } from "react";
import { Menu } from "lucide-react";
import { Link } from "react-router";
import { Header, IconButton } from "@penny/ui";
import { ChatHistoryDrawer } from "@penny/chat-ui";

/**
 * The app chrome: the left chat-history drawer, the header (logo, nav,
 * hamburger), and the routed screen below it. Owns the drawer's open state so
 * the hamburger and the drawer share it.
 *
 * On mobile the drawer overlays (with a tap-to-close backdrop) instead of
 * pushing the content, which would otherwise squish the chat off a phone.
 */
export function AppShell({ children }: { children: ReactNode }) {
  const [drawerOpen, setDrawerOpen] = useState(false);
  const hamburgerRef = useRef<HTMLButtonElement>(null);
  const closeDrawer = useCallback(() => {
    setDrawerOpen(false);
    hamburgerRef.current?.focus();
  }, []);

  // Drawer link clicks: below md the drawer overlays (see the md:hidden
  // backdrop below — 768px is Tailwind's md) and must dismiss on navigation;
  // on desktop it pushes content and stays open across client-side switches.
  // The shell owns this policy so the layout split lives in one module.
  const onDrawerNavigate = useCallback(() => {
    if (!window.matchMedia("(min-width: 768px)").matches) closeDrawer();
  }, [closeDrawer]);

  return (
    <div className="relative flex h-full w-full bg-background">
      {/* Mobile-only backdrop behind the overlay drawer; tap to dismiss. */}
      {drawerOpen && (
        <button
          type="button"
          aria-label="Close chat history"
          onClick={closeDrawer}
          className="fixed inset-0 z-30 bg-black/30 md:hidden"
        />
      )}
      <ChatHistoryDrawer
        open={drawerOpen}
        onClose={closeDrawer}
        onNavigate={onDrawerNavigate}
      />
      <div className="flex min-w-0 flex-1 flex-col">
        <Header
          leading={
            <IconButton
              ref={hamburgerRef}
              aria-label={drawerOpen ? "Close chat history" : "Open chat history"}
              aria-expanded={drawerOpen}
              onClick={() => setDrawerOpen((o) => !o)}
            >
              <Menu className="h-5 w-5" />
            </IconButton>
          }
          nav={
            // A router Link, not <a href> — a hard navigation reboots the SPA.
            <Link to="/" className="hover:underline">
              Chat
            </Link>
          }
        />
        <div className="min-h-0 flex-1">{children}</div>
      </div>
    </div>
  );
}
