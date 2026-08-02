import { useEffect, useMemo, useRef, useState } from "react";
import { useChat } from "@ai-sdk/react";
import { DefaultChatTransport } from "ai";
import type { ChatTransport, UIMessage as AiUIMessage } from "ai";
import { AlertCircle, Brain } from "lucide-react";
import { Link, useLocation, useNavigate, useParams } from "react-router";
import { Message, Composer } from "@adambossy/agent-ui";
import type { UIMessage } from "@adambossy/agent-ui";
import { conversationPath } from "./routes";

/** The active model, as reported by the backend (`GET /api/config`). */
type ActiveModel = { id: string; label: string };

/** Which model is answering. Configuration lives server-side
 * (`PENNY_AGENT_MODEL`), so the UI asks rather than assumes — a hardcoded
 * name here misreports the moment the model is switched.
 *
 * `null` until the fetch lands (and if it fails): the composer then shows no
 * model chip at all, which is honest, where a guessed default would not be.
 */
function useActiveModel(): ActiveModel | null {
  const [model, setModel] = useState<ActiveModel | null>(null);
  useEffect(() => {
    let cancelled = false;
    fetch("/api/config")
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.json();
      })
      .then((data) => {
        if (!cancelled && data?.model?.id) setModel(data.model as ActiveModel);
      })
      .catch(() => {
        /* leave it unset — better a missing label than a wrong one */
      });
    return () => {
      cancelled = true;
    };
  }, []);
  return model;
}

// Reshape the AI SDK send body to the shape the backend's /api/chat expects.
// `selectedChatModel` is advisory — this backend picks the model from its own
// config and ignores the field — so it carries whatever the server reported,
// never a name the client invented.
function makeTransport(modelId: string | undefined): ChatTransport<AiUIMessage> {
  return new DefaultChatTransport<AiUIMessage>({
    api: "/api/chat",
    prepareSendMessagesRequest: ({ id, messages }) => {
      const latest = messages[messages.length - 1];
      return {
        body: {
          id,
          message: { id: latest.id, role: "user", parts: latest.parts },
          selectedChatModel: modelId,
          selectedVisibilityType: "private",
        },
      };
    },
  });
}

/** Extract a human-readable string from any AI SDK `error` state. */
function errorMessage(error: unknown): string {
  if (!error) return "";
  if (error instanceof Error) return error.message;
  if (typeof error === "string") return error;
  try {
    return JSON.stringify(error);
  } catch {
    return String(error);
  }
}

/** Pull a stream-level `error` SSE frame out of the latest assistant message,
 * if the AI SDK didn't already surface it via the top-level `error` state.
 * Only the latest assistant message can carry the current turn's error, so
 * the scan stops there — this runs on every streamed delta. */
function findStreamError(messages: AiUIMessage[]): string | null {
  for (let i = messages.length - 1; i >= 0; i--) {
    const msg = messages[i];
    if (msg.role !== "assistant") continue;
    const parts = (msg.parts ?? []) as Array<{ type?: string; errorText?: string; text?: string }>;
    for (const part of parts) {
      if (part?.type === "error" && typeof part.errorText === "string") return part.errorText;
    }
    return null;
  }
  return null;
}

/** True once the assistant message has anything the transcript can render —
 * the pending indicator yields to the real reasoning/text/tool parts. */
function hasVisibleParts(message: AiUIMessage): boolean {
  const parts = (message.parts ?? []) as Array<{ type?: string }>;
  return parts.some(
    (part) =>
      part?.type === "text" ||
      part?.type === "reasoning" ||
      part?.type === "dynamic-tool" ||
      (part?.type ?? "").startsWith("tool-"),
  );
}

/** Placeholder shown between message send and the first streamed part, so the
 * agent never looks unresponsive. Styled to match agent-ui's Reasoning header
 * (which replaces it once real reasoning deltas arrive). */
function PendingThinking() {
  return (
    <div className="my-2 text-sm text-muted-foreground">
      <div className="inline-flex items-center gap-1.5 px-2 py-1 text-[13px]">
        <Brain size={13} />
        <span className="streaming-caret">Thinking…</span>
      </div>
    </div>
  );
}

/**
 * Route adapter: derives the conversation id from the URL — the single source
 * of truth for which conversation is on screen.
 *
 * On `/c/:id` the param is the id. On `/` (a draft chat) a fresh id is minted
 * per navigation — keyed to `location.key` so "New chat" from anywhere always
 * yields a clean conversation, while the first-send `/` → `/c/<id>` URL
 * replacement keeps `key={sessionId}` stable and the in-flight turn mounted.
 */
export function ChatRoute() {
  const { id } = useParams();
  const location = useLocation();
  // The draft id lives in state, not useMemo — React may discard memo caches,
  // which would remint the id and remount the draft mid-composition. The
  // render-phase reset ("adjusting state when props change") mints a fresh id
  // per navigation (location.key), so "New chat" always starts clean.
  const [minted, setMinted] = useState(() => ({
    key: location.key,
    id: crypto.randomUUID(),
  }));
  if (minted.key !== location.key) {
    setMinted({ key: location.key, id: crypto.randomUUID() });
  }
  const sessionId = id ?? minted.id;
  return <ChatScreen key={sessionId} sessionId={sessionId} draft={!id} />;
}

/** Hydration hit a genuine failure (5xx, network) — surface it, don't render
 * an existing conversation as a deceptively empty chat the user would re-send
 * context into. */
function ConversationLoadFailed() {
  return (
    <div className="flex h-full w-full flex-col items-center justify-center bg-background text-center">
      <h1 className="text-2xl font-semibold sm:text-3xl">Couldn't load this conversation</h1>
      <p className="mt-2 text-sm text-muted-foreground">
        Something went wrong fetching its history.
      </p>
      <button
        type="button"
        onClick={() => window.location.reload()}
        className="mt-6 rounded-full border border-cream px-4 py-2 font-ui text-sm text-ink transition-colors hover:bg-cream-soft"
      >
        Try again
      </button>
    </div>
  );
}

/** Deep link to a conversation that doesn't exist. */
function ConversationNotFound() {
  return (
    <div className="flex h-full w-full flex-col items-center justify-center bg-background text-center">
      <h1 className="text-2xl font-semibold sm:text-3xl">Conversation not found</h1>
      <p className="mt-2 text-sm text-muted-foreground">This conversation doesn't exist.</p>
      <Link
        to="/"
        className="mt-6 rounded-full border border-cream px-4 py-2 font-ui text-sm text-ink transition-colors hover:bg-cream-soft"
      >
        Start a new chat
      </Link>
    </div>
  );
}

export function ChatScreen({ sessionId, draft }: { sessionId: string; draft: boolean }) {
  // What hydration produced: pending (null), a transcript, "not-found", or
  // "error". A draft starts hydrated-empty — it has no history to load, and
  // the first-send `/` → `/c/<id>` URL replacement flips `draft` without
  // remounting (same key), so fetching then would clobber the in-flight turn.
  const [history, setHistory] = useState<AiUIMessage[] | "not-found" | "error" | null>(
    draft ? [] : null,
  );

  // Hydrate persisted history before mounting the chat so refreshes and
  // backend restarts don't blank the transcript. A 404 (unknown id) renders
  // the not-found state rather than a silent empty chat a message could be
  // sent into.
  //
  // Hydration happens once per mount (a draft counts as already hydrated):
  // `hydratedRef` keeps a re-run from refetching and — worse — flipping a
  // live transcript into the not-found state if the conversation vanished
  // server-side mid-view.
  const hydratedRef = useRef(draft);
  useEffect(() => {
    if (hydratedRef.current) return;
    let cancelled = false;

    const hydrate = async (): Promise<AiUIMessage[] | "not-found" | "error"> => {
      try {
        const res = await fetch(`/api/sessions/${sessionId}`);
        if (res.status === 404) return "not-found";
        if (!res.ok) return "error";
        const data = (await res.json()) as { messages?: AiUIMessage[] };
        return data.messages ?? [];
      } catch {
        return "error";
      }
    };

    void hydrate().then((outcome) => {
      if (cancelled) return;
      hydratedRef.current = true;
      setHistory(outcome);
    });
    return () => {
      cancelled = true;
    };
  }, [sessionId]);

  if (history === "not-found") {
    return <ConversationNotFound />;
  }

  if (history === "error") {
    return <ConversationLoadFailed />;
  }

  if (history === null) {
    return (
      <div className="flex h-full w-full items-center justify-center bg-background text-muted-foreground">
        Loading conversation…
      </div>
    );
  }

  return <Chat sessionId={sessionId} draft={draft} initialMessages={history} />;
}

function Chat({
  sessionId,
  draft,
  initialMessages,
}: {
  sessionId: string;
  draft: boolean;
  initialMessages: AiUIMessage[];
}) {
  const navigate = useNavigate();
  const model = useActiveModel();
  // Rebuilt when the model id arrives; `useChat` picks up the new transport
  // without resetting the conversation.
  const transport = useMemo(() => makeTransport(model?.id), [model?.id]);

  const { messages, sendMessage, status, error } = useChat({
    id: sessionId,
    transport,
    messages: initialMessages,
    generateId: () => crypto.randomUUID(),
  });

  const transcriptRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    const el = transcriptRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages, status, error]);

  const isStreaming = status === "streaming" || status === "submitted";
  const showEmpty = messages.length === 0;
  const lastMessage = messages[messages.length - 1];
  const awaitingResponse =
    isStreaming &&
    lastMessage !== undefined &&
    (lastMessage.role === "user" || !hasVisibleParts(lastMessage as AiUIMessage));

  // Top-level errors come from two paths:
  //   1. `useChat` exposes `error` for transport / parse failures.
  //   2. Stream-level `{type: "error", errorText}` frames are appended as a
  //      part on the assistant message — surface those too.
  const surfacedError = errorMessage(error) || findStreamError(messages as AiUIMessage[]);

  // pb-10: lift the composer + footer hint 40px off the bottom edge.
  return (
    <div className="flex h-full w-full flex-col bg-background pb-10 text-foreground">
      {/* `relative` contains absolutely-positioned descendants — unpositioned,
          they'd escape this scroller's overflow and stretch the document past
          the composer. */}
      <div ref={transcriptRef} className="relative flex-1 overflow-y-auto">
        {/* data-testid/data-role: stable hooks for the Playwright specs. */}
        <div data-testid="transcript" className="mx-auto max-w-3xl px-3 pt-2 pb-2 sm:px-4">
          {showEmpty ? (
            <div className="flex h-[70vh] flex-col items-center justify-center text-center">
              <h1 className="text-2xl font-semibold sm:text-3xl">What can I help with?</h1>
              <p className="mt-2 text-sm text-muted-foreground">
                Ask me anything — try <em>"What did I spend this week?"</em>
              </p>
            </div>
          ) : (
            messages.map((m, i) => (
              <div key={m.id ?? i} data-role={m.role} data-message-role={m.role}>
                <Message
                  message={m as unknown as UIMessage}
                  isStreaming={isStreaming && i === messages.length - 1}
                />
              </div>
            ))
          )}
          {awaitingResponse && <PendingThinking />}
          {surfacedError && (
            <div
              role="alert"
              className="mt-3 flex items-start gap-2 rounded-lg border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
            >
              <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
              <div className="min-w-0 flex-1 whitespace-pre-wrap break-words">
                <strong className="font-medium">Error.</strong> {surfacedError}
              </div>
            </div>
          )}
        </div>
      </div>

      <Composer
        disabled={isStreaming}
        onSend={(text) => {
          sendMessage({ text });
          // First send promotes the draft: the conversation now exists
          // server-side, so give it its real URL. Replace, not push — this
          // renames the view in place; back should not revisit a ghost empty
          // chat. The route re-renders with `draft` false, so this runs once.
          if (draft) void navigate(conversationPath(sessionId), { replace: true });
        }}
        modelLabel={model?.label}
        footerHint="Penny can make mistakes — verify important numbers"
      />
    </div>
  );
}
