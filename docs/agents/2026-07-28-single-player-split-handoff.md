# HANDOFF — Penny went single-player (2026-07-28)

Share this with any agent working in this repo. The codebase you knew has
been restructured on branch `worktree-plan/single-player-local-first`
(PR pending into `main`). Plan of record:
`docs/superpowers/specs/2026-07-27-single-player-local-first-design.md`.

## The one-paragraph version

Penny's core is now a **single-player, locally hosted product**: no auth, no
billing, no households, **no tenancy at all** — one user, one database, a
local web UI (`penny serve`), a CLI onboarding wizard (`penny init`), a
local scheduler daemon (`penny daemon`), and a Claude Code plugin surface
(`penny mcp` + `plugin/`). Everything multi-tenant/hosted moved out: the
monolith is frozen on the permanent branch **`legacy/saas-monolith`** (the
hosted deploys pin there — operator step, verify before deploying anything),
and the web product was extracted with history into the **private repo
`adambossy/penny-web`**, which will re-add tenancy at the database level and
compose the core via its new seams.

## If your in-flight work touches…

- **`penny.tenancy`, `RequestContext`, `session_for(ctx)`, `visible_filter`,
  RLS, GUCs** — all deleted. Queries are unscoped; `db.session()` is the
  only session. Tenant columns and `Household`/`User` are gone from models.
- **`ConversationStore` / reminders / onboarding engine** — ctx-free
  signatures; conversation store tables renamed `app_*` and now live in the
  SAME database as finance (WAL mode on SQLite). `penny_web.db`,
  `web.*` schema, `api/persistence/engine.py`, `PENNY_WEB_DATABASE_URL` are
  gone.
- **`api/main.py`** — now a thin default instance. The routes live in
  `penny/api/routes.py`; composition happens in `penny/api/app.py`
  (`create_app(AppConfig(auth_dependency, turn_wiring, extra_routers,
  static_dir))`). The billing gate, sandbox branch, and R2 workspace
  bracket are host concerns (penny-web), injected via `TurnWiring`.
- **`billing/`, `auth/`, `households.py`, `sandboxes/`, `workspace_store/`,
  `admin.py`, `adapters/clerk.py`, `api/mcp_server.py`, `sandbox_wiring`,
  `deploy/`, `sandbox/`, `lib/`** — deleted from core; live on in
  `legacy/saas-monolith` and in `adambossy/penny-web`.
- **Frontend** — Clerk, the landing page, billing/invite/household screens
  are gone. The chat UI moved into `frontend/packages/chat-ui`
  (`@penny/chat-ui`, not yet published to npm); `src/` is a thin shell.
- **Migrations** — new fork-point migration `029_single_player_baseline`
  (irreversible; drops tenancy, creates `app_*`). **Never run 029+ against
  the hosted/Neon multi-tenant DB** — hosted stays frozen at 028 +
  penny-web's future overlay chain. `test_schema_drift` is enforcing again.
  This is now GUARDED, not just documented: `migrate`/`serve`/`bootstrap`
  raise `ForeignDatabaseError` on any database with a `households` table
  (commit fea480b, added after a near-miss where a dev .env carrying prod's
  DATABASE_URL let serve attempt 029 against production — alembic's
  transaction rolled it back). Audit your own .env before pointing anything
  anywhere.
- **Workspace** — default is `~/.penny` now (an existing `~/.transactoid`
  is honored). `~/.penny/config.toml` (from `penny init`) feeds env
  DEFAULTS via `penny.settings`; real env vars still win.
- **CLI** — new: `init`, `serve` (+`--all-in-one`), `daemon
  run|install|start|stop|status`, `mcp`. Changed: `sync` is single-pass;
  `run` lost `--household`. Gone: `reap-sandboxes`, `penny admin`.
- **Identity facts** — `penny.identity.local_user_id()` (workspace-minted
  UUID) replaces `users` rows; report recipients come from
  `PENNY_REPORT_RECIPIENTS` (config.toml), never the DB.
- **`DATABASE_URL`** — still works; `PENNY_DATABASE_URL` preferred; URLs
  are normalized (bare path → sqlite, `postgres://` → `postgresql://`).

## Repo/infra state

- `legacy/saas-monolith` — permanent freeze at `fbda0f1`; hosted hotfixes
  only. Offline bundle: `~/backups/transactoid-pre-single-player-20260728.bundle`.
- `adambossy/penny-web` (private) — extracted history + scaffold
  (`README.md`, `docs/specs/isolation-layer-design.md`,
  `docs/INTEGRATION-TODO.md`). Its cutover onto the new core is gated on
  its own Postgres RLS suite.
- **Public flip + rename (`transactoid` → `penny`) is PREPARED BUT HELD**:
  a full-history gitleaks scan found a real (low-risk) self-signed
  localhost TLS private key in pre-rebuild lineage; risk-acceptance is the
  owner's call. Until the flip, nothing about your remotes changes.
- `@penny/chat-ui` npm publish deferred (no npm auth in the build env).
- Gates on the branch: backend `ruff` clean, `pytest` 524 passed; frontend
  `npm run build` + `tsc --noEmit` clean.

## Rules of thumb until the PR merges

1. Web-domain feature work in this repo is frozen — take it to penny-web.
2. Don't "fix" the missing tenancy — its absence is the design.
3. Rebase in-flight branches onto the PR branch if they touch backend/
   frontend; expect large mechanical conflicts in `api/`, models, tools.
4. AGENTS.md / REQUIREMENTS.txt / README.md on the branch are the current
   truth; the versions on `main` are stale until merge.
