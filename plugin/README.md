# Penny — Claude Code plugin

Exposes the local single-player Penny agent to Claude Code: the finance
toolsets over a stdio MCP server (`penny mcp`) plus Penny's skills
(spending reports, budgets, taxonomy work, merchant rules).

## Requirements

- The `penny` CLI installed and onboarded (`pip install …` / `uv tool
  install …`, then `penny init`). The plugin talks to the same database and
  workspace as the local web UI (`penny serve`) — one substrate, two
  surfaces.
- The daemon keeps data fresh for both (`penny daemon status`). The plugin
  never starts it for you; the `sync_status` tool reports staleness and the
  agent will suggest `penny daemon start` when needed.

## Install

From the marketplace at the repo root:

```
/plugin marketplace add adambossy/penny
/plugin install penny@penny
```

Skills under `skills/` are symlinks into `backend/.agent/skills/` — the
single source of truth shared with the web UI's agent.
