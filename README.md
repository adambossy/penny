# Penny

A **local-first personal-finance agent**. Penny syncs your bank transactions
via Plaid, categorizes them with an LLM against a two-level taxonomy you can
tune, and answers questions about your money in a streaming chat UI — all on
your own machine, over your own database. No accounts, no cloud, no landing
page: `/` is a chat with your finances.

Two surfaces, one substrate:

- **Web UI** — `penny serve` runs the API + built web app in one process at
  `http://127.0.0.1:8000`.
- **Claude Code plugin** — `penny mcp` exposes the same tools + skills to
  Claude Code (and any MCP harness) over stdio.

Both share the same database and workspace; a local daemon keeps the data
fresh (sync every 12 h) and emails you a weekly spending report.

## Quickstart

```bash
# 1. Install (repo checkout for now)
git clone https://github.com/adambossy/penny && cd penny
uv sync --frozen
(cd frontend && npm install && npm run build)

# 2. Onboard: workspace, database, keys, email, daemon
uv run --project backend penny init

# 3. Run
uv run --project backend penny serve
# → http://127.0.0.1:8000 — ask Penny to connect your bank.
```

You'll need: a [Gemini API key](https://aistudio.google.com/apikey), your own
[Plaid](https://plaid.com/) API credentials (their production-access approval
is a real step), and SMTP credentials (e.g. a Gmail app password) for email
reports. `penny init` collects all of it into `~/.penny/config.toml`
(chmod 600) and installs the daemon as a user service (launchd/systemd).

The database is yours: SQLite by default (`~/.penny/penny.db`), or point
`PENNY_DATABASE_URL` at any Postgres — local or remote — and Penny migrates
it automatically.

## Phone access

Penny binds to localhost. To use it from your phone, put your machine on a
[Tailscale](https://tailscale.com/) tailnet and open the served URL there
(`tailscale serve` adds HTTPS). **A bare ngrok URL is a public door to your
finances** — if you must use ngrok, use its auth features. Penny deliberately
ships no auth layer of its own.

## Claude Code plugin

```
/plugin marketplace add adambossy/penny
/plugin install penny@penny
```

Requires the `penny` CLI installed and onboarded (`penny init`). The plugin
shares the web UI's database and workspace; conversation history stays
per-surface, memory (budgets, merchant rules) is shared.

## Development

See `AGENTS.md` for layout, conventions, and the verification gate
(`ruff` + `pytest` + `npm run build`). The hosted multi-tenant product lives
in a separate private repo (penny-web) that composes this core; the
pre-split monolith is preserved on the `legacy/saas-monolith` branch.
