# AGENTS.local.md

@AGENTS.md

## Architectural segregation: agent vs website (HARD CONSTRAINT)

Penny is two domains that must never commingle. This applies to **all** features, not any single one. Treat it as a hard constraint when designing and reviewing any change.

**The two domains:**

- **Agent domain** — what the LLM uses to do finance work: `backend/penny/tools/` (the `@tool` wrappers and `tools/_services/`), `backend/.agent/skills/`, `backend/penny/agent_factory.py`, `backend/penny/prompts.py`, and the prompts. It operates on the finance data, notably via the intentionally unrestricted `run_sql` tool.
- **Website/app domain** — the application that hosts the agent and owns all user-facing CRUD and persistence: the FastAPI surface (`backend/penny/api/`), account management, settings, conversation/message persistence, and any future app-owned data.

**Dependency rule (one-directional): Website → Agent only.** The website constructs and invokes the agent; the API bridge (`api/bridge.py`) is the allowed seam — it runs the agent and translates its event stream, and is itself website code. The agent domain must have **zero** imports of website/app persistence or CRUD code. The website persistence layer must not import agent tools/skills internals. Never let a tool or skill import an app store to "log itself," and never let an app store reach into `tools/_services` for helpers.

**Concrete rules every feature must follow:**

1. **App-owned data lives in website-owned packages with their own models and store** (e.g. `backend/penny/api/persistence/`), never scattered into `penny/adapters/db` (`models.py`, `DB`) as ad-hoc additions — that layer stays the finance domain's own model surface, and the persistence package owns its models as a separate, self-contained set. In **single-player**, these app tables intentionally share the finance `Base`/database (namespaced by an `app_` prefix), per AGENTS.md's "one database, both stores" — there is no cross-tenant exposure to protect against, and `run_sql` already gives the one user full access to their own finance data by design, so giving it read access to that same user's own chat history is not a privacy boundary crossing. What must **not** happen is app CRUD logic or persistence-layer imports leaking into `penny/tools`/`agent_factory.py` (see the dependency rule and guardrail test below) — the shared database is a storage-layout decision, not license to blur the domains' code.
2. **Residual risk, not a current violation:** a prompt-injected transaction descriptor could direct the agent to read chat history back to the user via `run_sql` — accepted as in-scope (the user already owns that data and that database) but worth remembering if a *hosted*, multi-tenant context (penny-web) is layered on later, since cross-tenant exposure would change this calculus and revive the case for a separate schema/role scoped to finance-only for `run_sql`.
3. **A guardrail test enforces the import boundary:** assert nothing under `penny/tools` or the skills tree imports the website persistence package, and that the persistence package imports neither `penny/tools` nor `penny/agent_factory`. (`backend/tests/test_domain_segregation.py`.)

### Deploy vs. app (HARD CONSTRAINT)

Deploy is a **fifth segregated domain** alongside the four above (the agent-harness library, the agent-ui library, the agent, and the website/CRUD app). Treat it as a hard constraint for **all** future work, not any single feature.

**The deploy domain** is everything release- and runtime-shaped: a single top-level `deploy/` directory holding the Dockerfiles, `fly.toml`(s), cron schedules, deploy scripts, and env templates, plus the thin `.github/workflows` shim (which must stay at the repo root per GitHub's requirement). Application code lives under `backend/`/`frontend/` and holds **zero** deploy artifacts.

**Dependency rule (one-directional): Deploy → App only.** The `deploy/` domain may copy from and invoke `backend/`/`frontend/` and supply their env — Dockerfiles `COPY` from `backend/`/`frontend/` with the build context set to the repo root, and the cron-manager names app entrypoints (`uvicorn penny.api.main:app`, `penny run-scheduled-report`) in its container command. Application code must **never** import from `deploy/`, read a `fly.toml`/`schedules.json`, or branch on deployment topology — no "am I the cron container?" / "am I on Fly?" conditionals. If the app must behave differently, it reads a `PENNY_*` env var that deploy sets; the app never names a deployable.

**The only runtime seam is the env/config contract:** `backend/penny/config.py` + the `PENNY_*` variables + `backend/.env.example`. Deploy supplies values (fly `[env]`, fly secrets, the cron `config.env`); the app declares its needs and reads them through `config.py`/`os.environ`. Nothing else crosses the boundary.

**Concrete rules every feature must follow:**

1. **`deploy/` mirrors the Fly-app topology:** `deploy/backend/`, `deploy/frontend/`, `deploy/cron-manager/`, and `deploy/scripts/`. Each deployable's `fly.toml`, `Dockerfile`, and `.dockerignore` live under its own subdirectory; `backend/`/`frontend/` hold no `Dockerfile`, no `fly.toml`, and no deploy script.
2. **Dev-tooling is NOT deploy and stays out of `deploy/`:** `.pre-commit-config.yaml`, ruff/mypy config, `.mcp.json` (and `.claude/`, `.agent/` discovery roots) are developer/local concerns that run locally or in a *test* CI job, not the release pipeline. The litmus test: if removing the file would break a *developer's local loop* but not a *production release*, it is dev-tooling and stays put.
3. **A headless front door (the Typer CLI) is app code, not deploy code, and not agent-internal.** `backend/penny/cli.py` is a peer entrypoint beside `backend/penny/api/main.py`: it may construct and drive the agent (import `agent_factory`, `bootstrap`, the services), exactly as the web bridge does. It must **not** move into `deploy/` (deploy only *invokes* it), and it must **not** live under `penny/tools`/`.agent/skills` — front doors drive the agent; the agent never drives a front door.

## Env files across worktrees

`backend/.env` and `frontend/.env` live only in the primary checkout
(`~/code/penny`) and are the single source of truth. The `WorktreeCreate`
hook (`~/.claude/hooks/create-worktree-with-env.sh`) **symlinks** them (and any
other gitignored `*.env`, e.g. `backend/.env.test`) into every new worktree, so
a secret edited once is live everywhere. The hook creates empty placeholders in
the primary if either is missing, so the links never dangle.

## Scheduler path (Sprites-forward)

The cron-manager uses the **manager-spawns-ephemeral-job-machines** pattern with the headless Typer CLI as the job contract, deliberately chosen to pave the way for Fly's ephemeral-job-machine (Sprites) service. The job contract is: *run `penny <command>` in an ephemeral, auto-destroyed machine built from the app image, with per-job env injected from the cron `config.env`.* Migrating from `fly machine run` to Sprites later swaps only the **spawn primitive** — the job/CLI/image/env contract is unchanged. Do not adopt native Machine `schedule` or an in-container scheduler: neither produces per-job ephemeral machines, so both would be dead-ends for the Sprites trajectory.
