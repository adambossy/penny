# Context Map

## Contexts

- [Agent core (`backend/`)](./backend/CONTEXT.md) — the Penny agent and the
  finance domain: sync, categorization, itemization, workspace, the local
  daemon, and the app surfaces (web API, CLI, MCP)
- Frontend (`frontend/`) — the chat web app (no `CONTEXT.md` yet; created
  lazily when its first term is resolved)
- Plugin (`plugin/`) — the Claude Code plugin exposing the same toolsets
  over `penny mcp` (no `CONTEXT.md` yet)

## Relationships

- **Frontend → Backend**: the chat UI drives the backend's streaming chat API
  (Vercel AI SDK UI message-stream protocol); the frontend holds no finance
  concepts of its own
- **Plugin → Backend**: the plugin's MCP server (`penny mcp`) exposes the
  backend's toolsets verbatim over stdio; plugin skills are symlinks into
  `backend/.agent/skills`. Plugin and web UI share one database (WAL) and
  one workspace
