# Penny

Penny is a **single-player, locally hosted** personal-finance agent: it syncs
bank transactions via Plaid, categorizes them with an LLM against a two-level
taxonomy, and answers natural-language questions about the user's finances —
through a local web UI (`penny serve`) and as a Claude Code plugin
(`penny mcp`), both over the same database and workspace. No accounts, no
tenants: the user's machine and database are the boundary.

Built on two external packages by the same author:

- **[agent-harness](https://github.com/adambossy/agent-harness)** (Python) —
  the agent loop, model providers, sessions, sandboxes, skills, tool
  decorator. Penny never reimplements these.
- **[agent-ui](https://github.com/adambossy/agent-ui)** (React) — chat
  components (`Message`, `Composer`) speaking the Vercel AI SDK UI
  message-stream protocol.

The hosted multi-tenant product (Clerk auth, billing, households, tenant
isolation, Fly/Modal deploy) lives in the private **penny-web** repo, which
consumes this core as a pinned git dependency and composes it via
`create_app` (see "Host composition seam"). The pre-split monolith is
preserved on the long-lived `legacy/saas-monolith` branch — the hosted
product deploys from that freeze until penny-web's integration lands.
Plan of record: `docs/superpowers/specs/2026-07-27-single-player-local-first-design.md`.

## Canonical vs. non-canonical artifacts

Everything checked in is one of two kinds:

- **Canonical** — the current source of truth: backend/frontend code, the
  prompts in `.prompts/`, the taxonomy seed, `REQUIREMENTS.txt`, `plugin/`,
  and these agent docs. If canonical says something false, that is a bug.
- **Non-canonical** — point-in-time records kept for **history, not truth**:
  plans (`plans/`), superseded specs, migration/cut-over scripts, and
  transient one-off tooling (`backend/transient/**` — excluded from the
  ruff/pytest gates, exempt from pattern expectations, deletable once spent).

Directives: canonical wins; keep the history, don't delete it; segregate,
never intermingle; don't maintain non-canonical code.

## Layout

```
backend/
├── penny/
│   ├── api/            # app.py (create_app factory) + routes.py (chat API),
│   │                   #   bridge.py (harness events → AI SDK SSE), main.py
│   │                   #   (default instance), persistence/ (app_* tables:
│   │                   #   conversations, reminders, onboarding)
│   ├── agent_factory.py# builds the Agent: model, prompt render, toolsets
│   ├── mcp_server.py   # `penny mcp` stdio server (toolsets → harnesses)
│   ├── daemon.py       # scheduler loop (sync + weekly report)
│   ├── service_install.py # launchd / systemd-user installation
│   ├── settings.py     # workspace config.toml → env defaults
│   ├── identity.py     # workspace-minted stable local user UUID
│   ├── tools/          # thin @tool wrappers the agent sees
│   │   └── _services/  # service implementations (categorizer, persister, sync…)
│   ├── plugins/amazon/ # self-contained Amazon toolset
│   ├── adapters/       # db (SQLAlchemy façade + models), cache, clients (plaid), storage (R2)
│   ├── taxonomy/, rules/, memory/, services/, eval/, security/, observability/
│   ├── workspace.py    # ~/.penny workspace resolution ($PENNY_WORKSPACE;
│   │                   #   an existing ~/.transactoid is honored)
│   ├── bootstrap.py    # idempotent create_schema + taxonomy seed
│   ├── cli.py          # init / serve / daemon / mcp / sync / run / eval / migrate
│   └── prompts.py      # promptorium-backed load_prompt()
├── .prompts/           # prompt source of truth (promptorium layout)
├── .agent/skills/      # agent-harness SkillRegistry root (8 skills)
├── configs/taxonomy.yaml  # taxonomy seed
├── db/migrations/      # alembic chain (029 = single-player fork point)
├── transient/          # non-canonical one-off tooling (e.g. hosted-data export)
└── scripts/
frontend/               # Vite + React 19 npm workspace:
├── packages/ui         #   @penny/ui (design system: Gallery, Wordmark, …)
├── packages/chat-ui    #   @penny/chat-ui (ChatScreen, drawer, Plaid card…)
└── src/                #   thin single-player app shell consuming both
plugin/                 # Claude Code plugin (manifest, .mcp.json, skills →
                        #   symlinks into backend/.agent/skills)
```

`backend/` is the only Python project (uv workspace of one; root `uv.lock`).

## Dev loop

```bash
# Backend (from repo root) — SQLite default lives at ./backend/penny.db
uv run --project backend uvicorn penny.api.main:app --host 127.0.0.1 --port 8000 --reload

# Frontend (from frontend/) — proxies /api to :8000
npm run dev

# The product, as users run it (serves frontend/dist statically):
uv run --project backend penny serve
```

- **agent-harness is a pinned git dep** (`@v0.2.0`), so `uv sync --frozen`
  installs identically everywhere. To hack on it locally, per-machine:
  `uv sync --frozen && uv pip install -e ~/code/agent-harness` (re-run after
  any `uv sync`).
- **agent-ui** resolves from the published npm package (`@adambossy/agent-ui`
  in `package.json`) **by default**. The `resolve.dedupe` for react stays as a
  guard against a second React copy (which blank-screens the app).
  `@penny/ui` and `@penny/chat-ui` are still aliased to their workspace sources.

### Working on agent-ui or agent-harness without a release

Both are consumed as released artifacts, so the naive loop is "change the
library, release it, bump Penny". Each has a per-machine override instead —
**opt-in and never automatic**: an earlier alias switched on merely because
`~/code/agent-ui` existed, so a developer and CI silently built different
things.

```bash
PENNY_LOCAL_AGENT_UI=1 npm run dev                          # or =/path/to/agent-ui
uv sync --frozen && uv pip install -e ~/code/agent-harness
```

Vite logs `agent-ui aliased to LOCAL source: …` when the alias is live; no
line means you are on the published package. Re-run the editable install
after any `uv sync`, which reverts it to the pinned revision.

**Neither may reach a commit or CI.** The alias is env-only, but the editable
install rewrites what is *installed*: regenerating `uv.lock` can record a
`file:///Users/…` path where the pinned SHA belongs — valid on one machine,
broken everywhere else. Before committing anything that touches the lock:

```bash
uv sync --frozen          # restores the pin
git diff --stat uv.lock   # must be empty
```

- **Backend restarts**: uvicorn `--reload` only watches `backend/` `*.py`.
  Edits to `.prompts/*.md` (lru_cached) need a manual restart.
- Logs: `~/.penny/logs/penny.log` (loguru file sink, DEBUG).

## Verification

```bash
cd backend
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
cd ../frontend && npm run build   # when frontend changed
```

Run these before completing any unit of work. There is no mypy gate yet.

## Databases

- **One database, both stores.** Finance tables and app-bookkeeping tables
  (`app_conversations`, `app_conversation_messages`, `app_queued_reminders`,
  `app_onboarding_items`) share one database on the shared SQLAlchemy Base.
- **One schema authority per dialect.** SQLite (default:
  `sqlite:///./penny.db`, or `~/.penny/penny.db` via `penny init`) builds
  the schema from the models via `create_all` at bootstrap, and opens in
  WAL mode (server + daemon + MCP share the file). **Postgres is owned
  exclusively by alembic** (`penny migrate`, auto-run by init/serve;
  `create_all` refuses on Postgres).
- **Migration 029 is the single-player fork point**: it reconciles a
  chain-through-028 database to the tenant-free shape (drops tenancy,
  creates app_* tables) and is irreversible. The hosted DB stays frozen at
  028 + penny-web's overlay chain — core migrations past the fork must
  NEVER run there. `tests/test_schema_drift.py` keeps models == chain.
- **User-supplied URLs are normalized** (`penny.db.normalize_database_url`):
  bare path → sqlite, `postgres://` → `postgresql://`.
  `PENNY_DATABASE_URL` is preferred; `DATABASE_URL` still accepted.

## Conventions

- **Prompts**: promptorium-managed, single source of truth in
  `backend/.prompts/<key>/<n>.md` with the `_meta.json` manifest; loaded via
  `penny.prompts.load_prompt`. Tweak the active prompt in place; substantive
  changes add a new version file AND bump `_meta.json` (`source_file`,
  `version_dir`, `last_version`, `last_hash`). Never renumber history.
- **Tools**: agent-facing wrappers in `penny/tools/*.py` are thin `@tool`
  async functions returning JSON-serializable dicts; implementations live in
  `tools/_services/`. Wrap sync service calls in `asyncio.to_thread`.
- **Tool output**: dict/list returns ride `ToolResult.structured_content`;
  the bridge and `penny mcp` forward it verbatim. Don't re-wrap in
  `{"text": ...}`.
- **Env vars**: `PENNY_*` prefix. `~/.penny/config.toml` (written by
  `penny init`) supplies *defaults* via `penny.settings`; a real environment
  variable always wins. Keep `.env.example` current.
- **Errors**: stream-level failures surface as `{type:"error"}` SSE frames →
  red banner in ChatScreen; tool failures as `tool-output-error` frames.
- **Workspace**: `~/.penny` (memory/, reports/, logs/, config.toml,
  user_id); an existing `~/.transactoid` is used as-is.
- **Branching**: `main` is the single long-lived branch; feature branches
  merge back into it. `legacy/saas-monolith` is the permanent pre-split
  freeze — never rebase, rewrite, or delete it.
- **Observability (Langfuse)**: `penny.observability` is OTEL tracing
  exported to Langfuse over OTLP; the loop is traced by agent-harness's
  `OTELSubscriber`, the categorizer by the `categorizer_span` helpers. On
  automatically when `LANGFUSE_*` keys are set; strict no-op otherwise.

## Host composition seam (penny-web)

The one sanctioned way to build a hosted product on this core
(`REQUIREMENTS.txt` T1):

- `penny.api.app.create_app(AppConfig(...))` — inject `auth_dependency`
  (per-request auth; `None` = single-player no-auth), `turn_wiring` (how a
  chat turn gets its credential/workspace/reminders; `LocalTurnWiring`
  default), `extra_routers`, `static_dir`.
- `build_agent` / `build_model` for driving the loop elsewhere.
- The `PENNY_*` env contract.

Core code never grows auth, billing, tenancy, or hosting branches — those
live in penny-web behind these seams. `tests/api/test_app_factory.py` plays
the host role until penny-web consumes the seam for real.

## Design rules

The north star is **managing complexity** — anything that makes the system
hard to understand or change. When two rules pull apart, choose what leaves
the next reader with less to hold in their head.

**Work strategically, not tactically.** Design is continual; spend the
~10–20% it takes to leave each module cleaner than the expedient path.
**Design it twice** for anything non-trivial.

**Make modules deep.** A good module hides a lot behind a small interface —
thin `@tool` wrappers over deep `tools/_services/`; the `penny.adapters.db`
facade over SQLAlchemy. **Pull complexity downward**; distrust shallow
modules and pass-throughs.

**Hide information; don't leak it.** Each module owns a design decision and
conceals it. Different layer, different abstraction: `api/` speaks
HTTP/streaming, `services/` orchestration, `tools/_services/` finance
operations, `adapters/` I/O. Reach *down* a layer, never sideways.

**Design interfaces for the common case.** Prefer somewhat general-purpose
interfaces; add the knob when the second real caller exists. **Define errors
out of existence** (idempotent bootstrap, re-runnable sync, re-runnable
init); genuine failures still surface at defined seams — never swallowed.

**Make it obvious.** Consistency is the lever; reuse the codebase's
vocabulary (transaction, descriptor, category, period…). Comments capture
what code can't — the why, the invariant — not the mechanics.

**Reuse, and keep one source of truth.** Don't reimplement agent-harness or
agent-ui. Every value has one home (see Conventions).

**Keep change safe.** Prefer subtraction; small dependency-ordered commits;
idempotent bootstrap/migrations; no irreversible data/infra op without a
snapshot or escape hatch.

## Architecture: layered domains

Within a domain, code lives in a fixed stack whose dependencies flow one way:

    Types → Config → Repo → Service → Runtime → UI

Cross-cutting concerns (connectors, telemetry) enter through explicit
provider seams. Penny does not yet fully adhere; converge by the **campfire
rule** — when you touch out-of-alignment code, nudge it toward the target;
flag what you notice but don't fix; **stay in scope** (refactor only what
your task touches — a small in-scope improvement beats a sprawling one).

## Requirements (`REQUIREMENTS.txt`)

The living spec: what Penny must do and the rules it must hold. Maintain it
**in the same change** that alters reality — a behavioural or constraint
change not reflected there is an incomplete change.

## Gotchas

- Gemini rejects JSON schemas containing `additionalProperties` etc.; the
  harness strips them (`providers/google.py`). If a new tool 400s on Gemini,
  check its generated schema first.
- `run_sql` is read-only: `security/sql_read_guard.py` accepts only a single
  read-only SELECT before execution (REQUIREMENTS T2a).
- SQLite WAL means raw file reads (fixtures, backups) must checkpoint first
  — use `DB.dispose()` before reading the file's bytes, or the data may
  still be in the `-wal` sidecar.
- The Plaid link flow runs against the LOCAL server; linking is a desktop
  action (phone is a read/chat surface via Tailscale).

## Agent skills

### Issue tracker

Issues are tracked in Beads: data checked in under `.beads/`, operated via
the `bd` CLI, issue prefix `fly-`. See `docs/agents/issue-tracker.md`.

### Triage labels

Default vocabulary: `needs-triage`, `needs-info`, `ready-for-agent`,
`ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Multi-context: root `CONTEXT-MAP.md` pointing at per-package `CONTEXT.md`
files. See `docs/agents/domain.md`.
