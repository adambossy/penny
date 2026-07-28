# single-player-export (transient, non-canonical)

One-off: export **one household** from the hosted multi-tenant Postgres
(schema as of migration 028, the `legacy/saas-monolith` freeze) into a fresh
**single-player** SQLite database matching the current models.

Why it exists: category corrections, transaction history beyond Plaid's
window, and eval history are not re-derivable by re-linking banks. The hosted
Neon DB stays live (the owner keeps self-hosting web Penny); this copies, it
never mutates the source.

## Usage

```bash
# Read-only source URL (the hosted DB), the household to take, and where the
# local single-player SQLite file should land:
uv run --project backend python backend/transient/single-player-export/export_household.py \
  --source "postgresql://…" \
  --household-id "22222222-…" \
  --dest ~/.penny/penny.db
```

Notes:

- Plaid access tokens are copied **as stored**. If the source encrypted them
  (`PENNY_PLAID_TOKEN_KEY` set in prod), put the SAME key into the local
  `~/.penny/config.toml` `[env]` or the tokens are undecryptable locally.
- Workspace files (memory/, reports/) are not in the DB — copy them from the
  hosted workspace store separately if wanted; local files win regardless.
- Merchants are global in the source: all referenced merchants are exported.
- Rerunning overwrites nothing: the tool refuses a non-empty destination.

This tree is non-canonical scratch (see AGENTS.md): excluded from lint/test
gates, deletable once spent.
