/**
 * The conversation route — the single home for the `/c/:id` pattern.
 *
 * The route table (main.tsx), every URL builder, and every "which conversation
 * is on screen?" reader consume these; nothing else hand-encodes the path, so
 * renaming the route is a one-file change.
 */
import { generatePath, useMatch } from "react-router";

export const CONVERSATION_PATH = "/c/:id";

/** URL for conversation `id`. */
export function conversationPath(id: string): string {
  return generatePath(CONVERSATION_PATH, { id });
}

/** The conversation id in the current URL, or undefined outside a conversation. */
export function useConversationId(): string | undefined {
  return useMatch(CONVERSATION_PATH)?.params.id;
}
