# Single-player local-first Penny: local web UI + Claude plugin, with `penny-web` layered on top

**Status:** aligned design (grilled 2026-07-27), not yet executed. Second plan
in the repo-split lineage — builds on and **revises**
`2026-07-18-repo-split-design.md` (plan 1). Where this doc is silent, plan 1
stands; where they conflict, this doc wins. The deviation ledger at the end
lists every point where plan 1 is overridden.

## Goal

The core repo becomes a **complete single-player product**: a locally hosted
finance agent with a full web UI served off a local server, plus (post-split)
a Claude Code plugin surface — both driving the same agent, database, and
workspace.

- **No accounts, no roles, no tenants, no tenant isolation.** Single player.
- **User-supplied database URL** — SQLite or Postgres, local or remote,
  auto-detected.
- **CLI onboarding** (`penny init`) sets up the workspace, DB, credentials,
  and the daemon.
- **No landing page**; the web UI opens straight into chat.
- **Web UI complete and functional**: chat, streaming, history drawer,
  Plaid link card, reports.
- **Plaid link flow and MCP server run locally** (stdio MCP; Link in the
  local browser with local token exchange).
- **Cron + email reporting** via a local `penny daemon`; direct SMTP.
- **No sandboxes; no remote-FS (R2) workspace machinery.**
- Phone access via **Tailscale (blessed) or ngrok (warned)** pointing at the
  local server; no auth layer of our own.

`penny-web` (private) still splits out and sits on top: Clerk auth, billing,
households + tenant isolation, sandboxes, R2 workspace store, Fly/Modal
deploy, consuming core as a pinned git dependency.

## Decisions (grilling record, 2026-07-27)

| # | Decision | Choice |
| --- | --- | --- |
| 1 | Composition seam | Core exposes `create_app(principal, turn_wiring, extra_routers)`; penny-web injects, never forks the chat route |
| 2 | Tenancy | **Ripped out of core entirely** — no Scope protocol, no tenant columns on core models. penny-web owns isolation (RLS + GUC defaults + parameterized views), designed in its own spec |
| 3 | Sequencing | Seams first in the mono-repo; split late |
| 4 | Frontend reuse | Core chat UI published as a **public npm package**; core repo stays package-source + single-player app shell; penny-web wraps the package |
| 5 | Single-player data | **One SQLite file** (or one Postgres DB): finance + app tables together, app tables namespaced by table prefix (SQLite has no schemas). Chat history + onboarding-nudge state are single-player features |
| 6 | Postgres handling | `penny migrate` folded into `penny init`/startup; alembic stays sole Postgres authority; URL normalization (bare path → sqlite, `postgres://` → `postgresql+psycopg://`) |
| 7 | Existing data | One-off export tool (hosted household → fresh local DB). The Neon prod DB **stays live** — the author keeps self-hosting web Penny |
| 8 | Email | Direct SMTP from the daemon, creds collected at onboarding (local MTA works for free via `SMTP_HOST=localhost`) |
| 9 | Daemon supervision | **Both**: `penny init` installs a user service (launchd / systemd user unit) by default; `penny serve --all-in-one` co-runs everything |
| 10 | Plaid | Whole link flow local (Link in browser + local exchange); **desktop-only linking**; phone is a chat/read surface |
| 11 | Visibility / name | Core goes **public**, renamed `transactoid` → `penny` |
| 12 | Eval | `backend/penny/eval` stays core; **`deploy/eval/` + the penny-eval Fly app move to penny-web** (deviation from plan 1) |
| 13 | UI auth | None. Bind `127.0.0.1` by default; document Tailscale as the blessed remote path; warn hard on bare ngrok |
| 14 | penny-web isolation | RLS + GUC-based server defaults via overlay migrations **plus parameterized views** (managed by penny-web) fencing what `run_sql` sees; exact design in a penny-web spec, validated by its own Postgres RLS suite before repinning onto tenancy-free core |
| 15 | Snapshot branch | Long-lived snapshot branch **stays in the core repo** (public history accepted) |
| 16 | Core history | **No rewrite.** Web-domain code removed via ordinary deletion commits; history stays intact and public. Plan 1's destructive clearing phase is dropped |
| 17 | Onboarding config | `config.toml` in the workspace; env vars (`PENNY_*`) still override; secrets chmod 600 |
| 18 | Workspace rename | `~/.transactoid` → **`~/.penny`**, in scope (fallback check for existing `~/.transactoid`) |
| 19 | UI package | Public npm (e.g. `@penny/chat-ui`); core `frontend/` is a workspace holding package + app shell |
| 20 | Claude plugin | `.claude-plugin/` in core + marketplace listing. **Planned now, implemented after the split lands** |
| 21 | Plugin ↔ daemon | Nudge only: `sync_status` reports staleness; instructions suggest `penny daemon start`; the plugin never spawns services |
| 22 | Schedules | Defaults: sync every 12 h, weekly report (config-adjustable) |
| 23 | Hosted continuity | Fly deploys **freeze onto the snapshot branch** (hotfix-only) while `main` undergoes surgery; penny-web is extracted from the snapshot and repins onto new core when its isolation layer is validated |

## What changes vs. plan 1

Plan 1 drew the boundary at "web backend and frontend move out entirely; core
is headless (CLI + MCP only)". This plan moves the boundary: **core keeps a
web front door** — a slim FastAPI app and the chat frontend — because the
single-player product *is* a locally hosted web app.

| Concern | Plan 1 | This plan |
| --- | --- | --- |
| `api/` (chat route, bridge, conversation persistence) | moves to penny-web | slim core version **stays**; web-domain routes move |
| `frontend/` | moves to penny-web | core keeps the chat UI as npm package + app shell |
| Agent loop | none in core | core owns its own user-session loop (in-process `stream_and_persist`, as today) |
| Tenancy | Scope protocol (LocalScope/RequestScope) | **no seam at all** — tenancy deleted from core; isolation is a penny-web DB-level concern |
| Tenant columns | declared-nullable on core models | **dropped from core models**; penny-web overlay migrations own them (with GUC-based defaults) |
| Core history | destructive filter-repo purge + force-push | **kept intact**; deletion commits only |
| Migrations tree | relocate under package for wheel install | unchanged in intent (still required for penny-web's git-dep `penny migrate`) |
| `deploy/eval/` | stays in core | moves to penny-web |
| Workspace rename | deferred | in scope (`~/.penny`) |
| MCP surface | `penny mcp` stdio (phase 6) | same, plus `.claude-plugin/` packaging (post-split) |

Unchanged from plan 1: penny-web extraction via ref-scoped `git filter-repo`;
frozen-baseline + two-forward-chains migration structure (core chain =
finance shape; web overlay chain = tenancy/RLS/web tables, separate version
table); semver-ish `v0.x` tagging with penny-web bumping the pin; sandboxes,
workspace store, cron-manager, and deploy all web-side.

## Current-state findings that drive the design

1. **Dev mode is most of single-player already.** `PENNY_AUTH_MODE=dev`
   resolves an env-pinned principal, no Clerk anywhere; the frontend without
   `VITE_CLERK_PUBLISHABLE_KEY` mounts no AuthGate, no landing page, no
   bearer token. Single-player is dev mode promoted to *the* mode, with the
   multi-tenant machinery removed rather than dormant.
2. **The chat turn path carries four web-domain brackets** to remove from
   core (`api/main.py`): the billing gate, the R2 workspace
   materialize/flush bracket (hard-coded `R2BlobStore`), onboarding-reminder
   enqueueing, and the `PENNY_SANDBOX_TURNS` branch. `build_agent` already
   supports the plain-local-workspace path.
3. **Tenancy enforcement is concentrated**: the DB facade's three hooks
   (`before_flush` stamping, `after_begin` RLS GUCs, `visible_filter` /
   `_household_scoped` scoping), shallow identity uses in four tools
   (`delivery`, `plaid_link`, `analytics`, `onboarding`), per-household cache
   keys, and `build_agent(ctx=...)`. ~36 files import `penny.tenancy`; the
   deletions are wide but mechanical outside the facade.
4. **DB auto-detect exists** (`DB(url)`, `_sql_dialect_from_env`); the gaps
   are URL normalization and running the alembic chain automatically on
   user-supplied Postgres.
5. **Email is SMTP-first already** (`EmailService`, `EMAIL_PROVIDER=smtp`).
   Recipients currently derive from `users` rows — single-player replaces
   that with configured recipients from `config.toml`.
6. **Cron is Fly-shaped** (`deploy/cron-manager/schedules.json` spawning
   ephemeral machines for `penny sync` / `penny eval-categorizer`); the
   local daemon replaces the spawn primitive and the CLI's all-households
   loop collapses to single-tenant.
7. **Plaid** already has hosted Link + `/api/plaid/exchange` in the web UI
   and a `PENNY_PLAID_LINK_MODE=localhost` dev flow — local linking works
   with user-supplied Plaid credentials.
8. **The plugin's guts exist**: 8 skills in `backend/.agent/skills/`, the
   toolset registry, and the prompt render with dynamic blocks.

## Design

### D1. Core app factory — the composition seam

```python
# penny/api/app.py (core)
def create_app(
    *,
    principal: PrincipalProvider = LocalPrincipal(),  # identity facts (user ref, emails)
    turn_wiring: TurnWiring = LocalTurnWiring(),      # per-turn credential / workspace / reminders
    extra_routers: Sequence[APIRouter] = (),
    lifespan_extras: Sequence[Lifespan] = (),
) -> FastAPI: ...
```

Core default (`penny serve`): no auth dependency; every request is the one
local player; env credential; plain local workspace; DB-backed reminders; no
billing, no sandbox. penny-web injects its Clerk principal provider, a
`TurnWiring` reinstating gate + R2 bracket + sandbox dispatch, and its
billing/household routers. The bridge (`bridge.py`, `accumulator.py`,
`hydration.py`, `persistence/`) is core code both products share.

`PrincipalProvider` is **identity, not tenancy**: it supplies the stable
local user ref (UUID minted at `penny init` into `config.toml`), report
recipient email(s), and the Plaid link user id. Core has no household/user
tables and no request-scoping of any kind.

### D2. Tenancy: deleted from core

- Remove `penny/tenancy/` and every import; facade hooks
  (`_stamp_tenant_columns`, `_apply_rls_settings`, `visible_filter`,
  `_household_scoped`, `session_for(ctx)`) deleted; queries are unscoped —
  single-player, the database *is* the boundary.
- **Core models drop `household_id` / `owner_user_id` / `visibility` and the
  `Household`/`User` ORM classes.** Delivery recipients and identity come
  from the principal provider, not `users` rows. Per-household caches become
  plain process caches.
- `build_agent` loses its `ctx` requirement.
- penny-web re-adds tenancy **at the database level**, never in core code:
  overlay migrations own the tenant columns (with server defaults reading
  session GUCs), RLS policies, and **parameterized views** that fence what
  `run_sql` (and potentially all web reads) can see. Exact design is a
  penny-web spec + prototype; its Postgres RLS test suite gates the repin
  onto tenancy-free core.
- Migration note: the frozen baseline chain (which *created* those columns)
  stays untouched in core; a new core migration does **not** drop them —
  column removal on the hosted DB is penny-web's call in its overlay chain.
  On fresh single-player DBs, `create_all` (SQLite) / the core chain
  (Postgres) simply never creates them. A core-chain migration marks the
  divergence point (see Risks).

### D3. Database: one URL, one store, auto-detected

- Single input URL (`PENNY_DATABASE_URL`, accepting `DATABASE_URL`).
  Normalization: bare filesystem path → `sqlite:///…`; `postgres://` →
  `postgresql+psycopg://`; otherwise pass through with a clear dial error.
  Default: `sqlite:///~/.penny/penny.db`.
- **One database.** App/bookkeeping tables (conversations, messages,
  onboarding-nudge state, reminders) live in the same SQLite file / Postgres
  DB as finance tables, namespaced by table prefix (e.g. `app_*`), since
  SQLite has no schemas. The separate `penny_web.db` / `web.*` derivation is
  deleted from core. (Hosted penny-web keeps real segregation its own way —
  independent DBs or views — per its isolation spec.)
- Single-player `run_sql` can therefore see app tables: accepted — it is the
  user's own data. The read-only parse guard
  (`security/sql_read_guard.py`) remains the write fence;
  `PENNY_AGENT_READONLY_DATABASE_URL` stays supported as optional
  defense-in-depth on user-owned Postgres.
- Schema authority unchanged in kind: SQLite → `create_all` at bootstrap;
  Postgres → alembic only, with `penny init` (and `penny serve` startup,
  idempotently) running the chain automatically. Concurrency: SQLite opens
  in WAL mode (server + daemon + MCP share the file).

### D4. CLI onboarding — `penny init`

Interactive wizard; every step re-runnable; answers land in
`~/.penny/config.toml` (env still overrides; secrets chmod 600):

1. **Workspace**: default `~/.penny` (fallback: offer to adopt an existing
   `~/.transactoid`); creates `memory/`, `reports/`, `logs/`; mints the
   local user UUID.
2. **Database**: default `sqlite:///~/.penny/penny.db`; any URL accepted;
   auto-detect; create_all-or-migrate; taxonomy seed.
3. **Model provider**: API key (Gemini default), model name.
4. **Plaid**: client id/secret, environment.
5. **Email**: SMTP host/port/username/password, From, recipient(s); report
   schedule (default weekly) and sync cadence (default 12 h).
6. **Daemon**: install + start the user service (launchd agent on macOS,
   systemd user unit on Linux); `penny daemon install|start|stop|status`.
7. Parting instructions: `penny serve`, the URL, Tailscale/ngrok guidance,
   and (post-split) the Claude plugin add command.

### D5. Daemon — local cron + email

`penny daemon` owns all scheduled work (replaces the Fly cron-manager
locally): sync every 12 h (single-tenant `penny sync` core: Plaid pull +
categorize, writing watermarks `sync_status` reads) and the weekly report
(existing `run-scheduled-report` path, emailed via SMTP). In-process
croniter-style loop — not OS cron entries; `daemon status` is the one place
to look. Failed sends retry on the next tick. `penny serve --all-in-one`
co-runs the same scheduler in-process for casual use; the installed service
is the default from onboarding.

### D6. Web UI — complete, single-player, no landing

- Strip: `ClerkProvider`/`AuthGate`, `home/` (landing),
  `ProvidersBillingScreen`, `ConnectProviderCard`, `InviteScreen`,
  `HouseholdHeader`, bearer-token plumbing (`authFetch`). `/` is a new chat.
- Keep: `ChatScreen`, history drawer, `PlaidLinkCard` +
  `/api/plaid/exchange`, gallery/report rendering, cancel; sandbox
  resume-stream goes web-side.
- **Packaging**: `frontend/` becomes a small npm workspace — the published
  chat-UI package (e.g. `@penny/chat-ui`, public npm, joining the existing
  `@penny/ui` precedent) plus the thin single-player app shell consuming it.
  penny-web installs the package inside its Clerk shell.
- `penny serve` serves the built frontend as static files (one process, one
  port, default bind `127.0.0.1:8000`); Vite dev server + proxy remains the
  contributor loop.
- Remote access: Tailscale documented as the blessed path (`tailscale
  serve` for tailnet HTTPS); ngrok documented with a hard warning. No auth
  of our own.

### D7. Claude plugin — one agent, two surfaces (post-split)

- `penny mcp` (stdio): toolsets passed through verbatim; dynamic prompt
  blocks (date, schema, taxonomy, memory) rendered into MCP `initialize`
  instructions; static identity/behavior in a checked-in agents-doc.
- `.claude-plugin/` in the core repo: manifest wiring `penny mcp`, the
  `.agent/skills/` mapped as plugin skills, the agents-doc as instructions;
  listed in a marketplace so users add it by name (requires the installed
  `penny` CLI).
- **Seamlessness = shared substrate, not shared transcript**: both surfaces
  hit the same DB (WAL) and workspace memory; the daemon keeps data fresh
  for both. Conversation history stays surface-owned (Claude Code's
  transcript vs. the app store). Daemon not running → nudge only
  (`sync_status` staleness + "run `penny daemon start`"); the plugin never
  spawns services.
- Scope: **planned here, implemented after the split lands.**

### D8. Deletions from core (become penny-web-only or die)

`billing/`, `households.py`, `auth/` (Clerk JWT), `adapters/clerk`,
`api/billing_routes.py`, `api/household_routes.py`, `api/sandbox_wiring.py`,
`api/mcp_server.py` (capability-token sandbox server — distinct from
`penny mcp`), `sandboxes/` + `sandbox/` + `lib/` (Modal runner + wire
protocol), `workspace_store/` + `admin.py` (+ the `penny admin` CLI mount),
`cli.py reap-sandboxes`, `penny/tenancy/`, the onboarding trigger engine's
multi-tenant parts, `deploy/` **including `deploy/eval/`** and the
cron-manager, frontend auth/billing/landing components, and the
`PENNY_AUTH_MODE` / `PENNY_DEV_*` / cron-principal env contract. Tests move
or die with their subjects. (The web UI's onboarding-nudge mechanics stay —
they are a single-player feature.)

## Repo, branches, hosted continuity

1. **Snapshot first**: create and push a long-lived branch of the full
   monolith in the core repo (name: `legacy/saas-monolith`), **plus** an
   offline `git bundle`. It stays in the repo after it goes public —
   web-domain history being publicly readable is accepted (#15/#16).
   ⚠ Third similarly-named ref alongside `backup/pre-split` (Transactoid→
   Penny rebuild) and plan 1's proposed `backup/pre-web-split` (now moot) —
   never confuse or clean up the wrong one.
2. **Hosted continuity**: Fly deploys pin to the snapshot branch
   (feature-frozen, hotfix-only) the moment surgery starts on `main`.
   penny-web is later extracted **from the snapshot** via plan 1's
   ref-scoped filter-repo mechanics, stands up its isolation design, then
   repins onto the new tenancy-free core.
3. **No core history rewrite.** All removals are ordinary deletion commits.
   Plan 1's destructive clearing phase (invert-paths + force-push) is
   dropped; its ref-hygiene notes still apply as optional cleanup.
4. **Public-flip gate**: before the repo is made public — a full-history
   secret scan (gitleaks/trufflehog over all refs) and a review of committed
   configs. Any hit forces remediation (rotation at minimum) first. Rename
   `transactoid` → `penny` at the same moment (GitHub auto-redirects).

## Phases

0. **Snapshot + freeze.** Push `legacy/saas-monolith` + offline bundle; pin
   Fly deploys to it; freeze web-domain feature work on `main`.
1. **Tenancy rip-out (mono-repo).** Delete `penny/tenancy` and all
   enforcement (facade hooks, scoping, stamping, GUCs); drop tenant columns
   + `Household`/`User` from core models; `build_agent` without `ctx`;
   single-tenant `penny sync`; identity facts via the principal provider
   reading workspace config. Full suite green. **Review checkpoint** (the
   spiritual successor of plan 1's phase-0 gate).
2. **One-database consolidation.** App store tables join the finance DB
   under a table-name prefix; `penny_web.db` / `web.*` derivation deleted;
   WAL mode; conversation store + reminders + nudges working against the
   unified DB.
3. **App factory + slim surface.** `create_app(principal, turn_wiring,
   extra_routers)`; billing gate / R2 bracket / sandbox branch leave the
   core chat path; frontend stripped (no Clerk/landing/billing) and split
   into package + shell; `penny serve` with static frontend; URL
   normalization + Postgres migrate-on-start.
4. **`penny init` + `penny daemon`.** Wizard, `config.toml`, service
   install, scheduler (sync + weekly report + SMTP), `sync_status`,
   `--all-in-one`; workspace rename to `~/.penny` with `~/.transactoid`
   adoption.
5. **Data export tool.** Transient one-off: one household from the live
   Neon DB → a fresh local single-player DB (transactions, categories,
   corrections, Plaid items + re-encrypted tokens, memory files).
6. **Extract `penny-web`.** filter-repo from the snapshot per plan 1
   mechanics (moves list = D8); penny-web writes its **isolation spec**
   (RLS + GUC defaults + parameterized views) and prototypes it; composes
   `create_app`; two-chain migrations per plan 1; repins onto new core when
   its Postgres RLS suite is green; cuts Fly/Modal deploys over; unfreezes.
7. **Public flip.** Secret-scan gate → remediate → rename to `penny` →
   public; publish `@penny/chat-ui` to npm; README/docs for self-hosters;
   slim `AGENTS.md` / `REQUIREMENTS.txt` for the single-player core.
8. **Claude plugin.** `penny mcp` stdio + `.claude-plugin/` + marketplace
   listing + agents-doc; validate `initialize`-instruction authority on
   Claude Code (plan 1's open question).

## Risks & open questions

- **Schema fork point.** After phase 1, core models no longer match the
  hosted DB shape. The frozen baseline stays valid for penny-web; core's
  forward chain must mark the divergence (a no-op "single-player baseline"
  revision) so fresh-Postgres single-player installs and hosted penny-web
  upgrades never share post-fork revisions. penny-web's drift test guards
  its side; core's `test_schema_drift` now asserts the tenant-free shape.
- **penny-web isolation design is deferred** (deliberately): RLS + GUC
  defaults + parameterized views, spec'd in penny-web scope after
  extraction. Until it lands, hosted Penny runs from the frozen snapshot —
  the freeze window should stay short, and hotfixes on the snapshot must be
  kept minimal.
- **create_app seams have no second consumer until phase 6.** The injection
  points (principal, turn wiring) are designed against a consumer that
  doesn't exist yet in-repo. Mitigation: while mono-repo (phase 3), keep a
  test-only wiring exercising the injection surface the way penny-web will.
- **Public history exposure is accepted, not accidental**: web/billing/Clerk
  lineage will be publicly readable. The secret-scan gate is what turns
  "accepted" into "safe"; treat any scan hit as rotate-first.
- **Plaid credentials are the user's own** (their client id/secret); Plaid's
  approval process for production API access is a real onboarding hurdle for
  third-party self-hosters — document it honestly.
- **SQLite WAL concurrency** across server + daemon + MCP is adequate for
  single-player write volumes, but long categorizer write bursts should keep
  transactions short; watch for `SQLITE_BUSY` in the daemon logs.
- Open: exact app-table prefix (`app_*` vs `web_*`); npm package name;
  marketplace choice for the plugin; how much authority harnesses grant MCP
  `initialize` instructions (validate in phase 8).

## Deviation ledger vs. plan 1

1. Core keeps a web front door (slim FastAPI + chat UI); plan 1 made core
   headless.
2. Tenancy: full rip-out, no Scope protocol; plan 1's Option A dropped.
3. Tenant columns + `Household`/`User` leave core models; plan 1 kept them
   declared-nullable.
4. Core history kept (deletion commits); plan 1's destructive filter-repo
   purge dropped.
5. Snapshot branch is permanent and public in core; plan 1's
   `backup/pre-web-split` was temporary.
6. `deploy/eval/` + penny-eval Fly app move to penny-web; plan 1 kept them
   core.
7. Workspace rename `~/.penny` in scope; plan 1 deferred it.
8. Single-player app data joins the finance DB (namespaced); plan 1 kept the
   separate derived web store in all modes.
9. penny-web isolation = DB-level (RLS/GUC/views) designed post-split in
   penny-web; plan 1 put RequestScope in web code and rejected RLS-only.
