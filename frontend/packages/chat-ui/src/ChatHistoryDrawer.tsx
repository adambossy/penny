import { useEffect, useMemo, useState } from "react";
import { SquarePen } from "lucide-react";
import { Link } from "react-router";
import { AvatarStack } from "@penny/ui";
import { authHeaders } from "./authFetch";
import type { TokenGetter } from "./authFetch";
import { conversationPath, useConversationId } from "./routes";
import { useHouseholdMembers } from "./useHouseholdMembers";

interface Conversation {
  id: string;
  title: string | null;
  updated_at: string;
  session_mode: "individual" | "joint";
}

type LoadState =
  | { status: "loading" }
  | { status: "ready"; items: Conversation[] }
  | { status: "error" };

/**
 * Push-style left chat-history drawer.
 *
 * An in-flow `<aside>` whose width animates `w-0 ↔ w-72`, so opening it shifts
 * the page body rather than overlaying it. The inner panel is a fixed `w-72`
 * under the aside's `overflow-hidden`, so its contents never reflow mid-animation.
 * Closed, the panel is `inert` (removed from tab order + pointer events) while
 * still mounted for the width transition.
 *
 * The list is (re)fetched from `GET /api/conversations` each time the drawer
 * opens — and, since the desktop drawer stays open across client-side switches,
 * whenever the active conversation changes — so it reflects newly-started or
 * newly-titled chats. Entries (and "New chat") are plain links: the URL is the
 * conversation mechanism. `onNavigate` fires on every link click; the shell
 * owns whether that dismisses the drawer (overlay vs. push layout).
 */
export function ChatHistoryDrawer({
  open,
  onClose,
  onNavigate,
  getToken,
}: {
  open: boolean;
  onClose: () => void;
  onNavigate: () => void;
  getToken: TokenGetter;
}) {
  const [state, setState] = useState<LoadState>({ status: "loading" });
  const { members, me } = useHouseholdMembers(getToken);

  // The joint-thread mark: the other member in front, the viewer behind —
  // "they are in here too". AvatarStack paints later entries on top, so the
  // other member goes last. Empty until the household actually has two
  // members (or while the members fetch is loading/failed), so individual
  // entries and solo households render exactly as before.
  const jointStack = useMemo(() => {
    const other = members.find((m) => !m.is_you);
    if (!other || !me) return [];
    return [me, other].map((m) => ({ name: m.display_name, imageUrl: m.image_url }));
  }, [members, me]);

  // The open conversation comes from the URL — the drawer renders outside the
  // matched route, so it reads the path (routes.ts) rather than useParams.
  const activeId = useConversationId();

  // Refetch on each open, and on conversation switches while open (the desktop
  // drawer stays open across client-side navigation), so the list is fresh —
  // a chat gains its title from the first user message, and new chats appear
  // without a manual reload. A refetch over an already-rendered list keeps it
  // on screen (no loading flash) and swaps in the response when it lands.
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setState((s) => (s.status === "ready" ? s : { status: "loading" }));
    authHeaders(getToken)
      .then((headers) => fetch("/api/conversations", { headers }))
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.json();
      })
      .then((data) => {
        if (!cancelled) {
          setState({ status: "ready", items: (data.conversations ?? []) as Conversation[] });
        }
      })
      .catch(() => {
        if (!cancelled) setState({ status: "error" });
      });
    return () => {
      cancelled = true;
    };
  }, [open, getToken, activeId]);

  // ESC closes the drawer (and restores focus to the toggle via onClose).
  useEffect(() => {
    if (!open) return;
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [open, onClose]);

  return (
    <aside
      aria-hidden={!open}
      aria-label="Chat history"
      // Desktop (md+): in-flow, animates width to push the content column.
      // Mobile: fixed overlay (above the backdrop) so it never squishes the chat.
      className={`h-full shrink-0 overflow-hidden border-r border-cream bg-paper transition-[width] duration-300 ease-in-out max-md:fixed max-md:inset-y-0 max-md:left-0 max-md:z-40 ${
        open ? "w-72" : "w-0"
      }`}
    >
      {/* Fixed-width inner panel: never reflows while the aside width animates. */}
      <div inert={!open} className="flex h-full w-72 flex-col">
        <div className="flex items-center justify-between px-4 pt-4 pb-2">
          <span className="font-ui text-sm font-medium text-ink">Chats</span>
          <Link
            to="/"
            onClick={onNavigate}
            className="inline-flex items-center gap-1.5 rounded-full border border-cream px-3 py-1.5 font-ui text-xs text-ink transition-colors hover:bg-cream-soft"
          >
            <SquarePen className="h-3.5 w-3.5" />
            New chat
          </Link>
        </div>

        <nav className="min-h-0 flex-1 overflow-y-auto px-2 pb-4">
          {state.status === "loading" && (
            <p className="px-2 py-2 font-ui text-sm text-steel">Loading…</p>
          )}
          {state.status === "error" && (
            <p className="px-2 py-2 font-ui text-sm text-steel">Couldn't load chats.</p>
          )}
          {state.status === "ready" && state.items.length === 0 && (
            <p className="px-2 py-2 font-ui text-sm text-steel">No chats yet</p>
          )}
          {state.status === "ready" &&
            state.items.map((conv) => (
              <Link
                key={conv.id}
                to={conversationPath(conv.id)}
                onClick={onNavigate}
                aria-current={conv.id === activeId ? "true" : undefined}
                className={`flex w-full items-center gap-2 rounded-lg px-2 py-2 text-left font-ui text-sm text-ink transition-colors hover:bg-cream-soft ${
                  conv.id === activeId ? "bg-cream-soft font-medium" : ""
                }`}
                title={conv.title ?? "New conversation"}
              >
                <span className="min-w-0 flex-1 truncate">
                  {conv.title ?? "New conversation"}
                </span>
                {conv.session_mode === "joint" && jointStack.length > 0 && (
                  <AvatarStack people={jointStack} />
                )}
              </Link>
            ))}
        </nav>
      </div>
    </aside>
  );
}
