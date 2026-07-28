import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import { usePlaidLink } from "react-plaid-link";
import { useLocation, useNavigate } from "react-router";
import { Button, Card } from "@penny/ui";
import { authedFetch } from "./authFetch";
import { conversationPath, useConversationId } from "./routes";

/**
 * Structured output of the `connect_bank_account` (mode `"hosted"`) and
 * `relink_account` (mode `"update"`) agent tools. Update mode carries the
 * existing item's identity so the card can name the institution being relinked.
 */
type Output = {
  mode?: string;
  link_token?: string;
  item_id?: string;
  institution_name?: string | null;
};

/** The tool-renderer part (agent-ui passes only `{ state, output }`). */
type ToolPart = { state?: string; output?: unknown };

/**
 * The OAuth-redirect Link stash: the bank redirects to the static
 * PLAID_REDIRECT_URI with only ?oauth_state_id, so the card records at
 * open time which conversation the flow belongs to, and {@link PlaidOauthGate}
 * records the exact URL the bank landed on (Plaid validates the resume's
 * `receivedRedirectUri` against the registered redirect URI, so the card must
 * present the landing URL, not the post-navigation one).
 *
 * localStorage, not sessionStorage — mobile banks can land the redirect in a
 * new tab, where a per-tab stash would be empty and the resume would die.
 * A single slot: two Link flows opened concurrently (e.g. two tabs) are
 * last-writer-wins, an accepted limitation — there is no join key available
 * at open time to do better (Plaid assigns oauth_state_id at redirect time).
 */
const PLAID_OAUTH_CONVERSATION_KEY = "penny:plaidOauthConversation";
const PLAID_OAUTH_RETURN_KEY = "penny:plaidOauthReturnUrl";

/** Forget a completed or abandoned OAuth round trip. */
function clearOauthStash(): void {
  localStorage.removeItem(PLAID_OAUTH_CONVERSATION_KEY);
  localStorage.removeItem(PLAID_OAUTH_RETURN_KEY);
}

/**
 * Gate for the Plaid OAuth return, mounted above the route table (the redirect
 * lands with no path context). When a Link flow is pending it records the
 * landing URL and reopens the stashed conversation — with a clean URL, so a
 * lingering ?oauth_state_id can't put a later card into resume mode — and
 * otherwise renders the app unchanged. All Plaid-redirect knowledge stays in
 * this module; the routing layer composes the gate without knowing what it
 * looks for.
 */
export function PlaidOauthGate({ children }: { children: ReactNode }): ReactNode {
  const location = useLocation();
  const navigate = useNavigate();
  const activeConversation = useConversationId();
  const resume =
    !activeConversation && location.search.includes("oauth_state_id")
      ? localStorage.getItem(PLAID_OAUTH_CONVERSATION_KEY)
      : null;

  useEffect(() => {
    if (!resume) return;
    localStorage.setItem(PLAID_OAUTH_RETURN_KEY, window.location.href);
    void navigate(conversationPath(resume), { replace: true });
  }, [resume, navigate]);

  // Render nothing while redirecting so a draft chat never flashes underneath.
  if (resume) return null;
  return children;
}

/**
 * Inline Plaid Link card rendered as generative UI for the `connect_bank_account`
 * tool output. Hosts `react-plaid-link`; on success it POSTs the `public_token`
 * to the server-side exchange (never exchanging client-side). OAuth-redirect
 * institutions resume via `receivedRedirectUri` when the conversation rehydrates.
 */
export function PlaidLinkCard({ part }: { part: ToolPart }) {
  const output = part.output as Output | undefined;
  const [done, setDone] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // The conversation the exchange resumes comes from the URL — the card only
  // renders inside a conversation transcript, whose route is /c/:id by the
  // time any tool output exists (the first send already promoted the draft).
  const conversationId = useConversationId();

  // Update mode (relink_account): re-authenticating an existing item. Plaid
  // restores the *same* item in place, so there is no public_token to exchange —
  // onSuccess just confirms and the next sync picks the connection back up.
  const isUpdate = output?.mode === "update";
  const institution = output?.institution_name ?? "your bank";

  // `fetch` only rejects on a network error, so a 4xx/5xx would otherwise flip
  // the card to "Bank linked" falsely — gate success on `res.ok` and surface a
  // failure state instead (mirrors ConnectProviderCard's try/catch + error card).
  async function exchange(publicToken: string): Promise<void> {
    setError(null);
    try {
      // If the /c/:id invariant above ever breaks, fail loudly (error card)
      // rather than exchanging the bank link into a phantom conversation.
      if (!conversationId) throw new Error("No conversation in the URL to link the bank to");
      const res = await authedFetch("/api/plaid/exchange", {
        method: "POST",
        body: JSON.stringify({
          public_token: publicToken,
          conversation_id: conversationId,
        }),
      });
      if (!res.ok) throw new Error(`Failed to link bank (${res.status})`);
      setDone(true);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  // Resuming an OAuth round trip presents the URL the bank actually landed on
  // (recorded by PlaidOauthGate), which is what Plaid validates against the
  // registered redirect URI — the in-app URL has since been rewritten.
  const oauthReturnUrl = localStorage.getItem(PLAID_OAUTH_RETURN_KEY) ?? undefined;

  const { open, ready } = usePlaidLink({
    token: output?.link_token ?? null,
    receivedRedirectUri: oauthReturnUrl,
    onSuccess: (publicToken) => {
      clearOauthStash();
      // Update mode restores the existing item — no server-side exchange.
      if (isUpdate) {
        setDone(true);
        return;
      }
      void exchange(publicToken);
    },
    // An abandoned flow must not leave a stale stash that yanks a future
    // OAuth return (or a later card's resume) into the wrong conversation.
    onExit: () => clearOauthStash(),
  });

  // E2E hook: drive the success path without the hosted Plaid popup (which can't
  // be automated headlessly — the real sandbox link-through is a manual step).
  // Dev-only (like main.tsx's `/ui` gallery), so the fake-token hook against the
  // real endpoint never ships in a production build.
  useEffect(() => {
    if (!import.meta.env.DEV) return;
    const w = window as unknown as {
      __pennyPlaidExchange?: (pt?: string) => void;
    };
    w.__pennyPlaidExchange = (pt = "public-e2e") => void exchange(pt);
    return () => {
      delete w.__pennyPlaidExchange;
    };
  }, []);

  const renderable =
    part.state === "output-available" &&
    (output?.mode === "hosted" || output?.mode === "update");
  if (!renderable) return null;
  if (done) {
    return (
      <Card data-testid="plaid-card">
        <p className="text-sm text-ink">
          {isUpdate
            ? `${institution} reconnected — syncing will resume.`
            : "Bank linked — syncing has started."}
        </p>
      </Card>
    );
  }
  return (
    <Card
      data-testid="plaid-card"
      data-tool={isUpdate ? "relink_account" : "connect_bank_account"}
    >
      <h3 className="font-ui text-sm font-medium text-ink">
        {isUpdate ? `Reconnect ${institution}` : "Connect a bank"}
      </h3>
      <p className="mt-1 text-sm text-steel">
        {isUpdate
          ? `${institution} needs to be re-authenticated so Penny can resume syncing its transactions.`
          : "Securely connect a bank account via Plaid, without leaving the chat."}
      </p>
      {error && <p className="mt-2 text-sm text-red-700">{error}</p>}
      <div className="mt-3">
        <Button
          variant="filled"
          disabled={!ready}
          onClick={() => {
            // Stash the conversation for the OAuth-redirect round trip (see
            // PLAID_OAUTH_CONVERSATION_KEY). Harmless for non-OAuth banks —
            // the exchange clears it.
            if (conversationId) {
              localStorage.setItem(PLAID_OAUTH_CONVERSATION_KEY, conversationId);
            }
            open();
          }}
          data-testid="plaid-connect"
        >
          {isUpdate ? "Reconnect" : "Connect a bank"}
        </Button>
      </div>
    </Card>
  );
}
