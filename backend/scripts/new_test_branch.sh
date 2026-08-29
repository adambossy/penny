#!/usr/bin/env bash
#
# new_test_branch.sh — spin up a FRESH, one-off Neon test branch off production
# and write backend/.env.test for the dev loop.
#
# Why: dev/test must run against a fresh copy of CURRENT PRODUCTION DATA every
# session, and NEVER against SQLite, the Supabase prod DB, or the long-lived
# Neon `production` branch directly. Re-branch aggressively: each run deletes
# prior session branches (those matching the TEST_PREFIX) and creates a new one.
#
# Safety invariants (this script must NEVER violate them):
#   - It only ever talks to the Neon project below — never Supabase.
#   - It only ever DELETES branches whose name starts with TEST_PREFIX
#     ("test-"). It will refuse to touch `production` or `penny-test`.
#   - It never mutates the `production` branch (only branches *from* it).
#
# Known neonctl bug (documented in AGENTS.md): `neonctl connection-string
# --branch-name <X>` returns the PARENT endpoint, not the new branch's. We work
# around it by reading the new branch's OWN endpoint host straight out of the
# `branches create --output json` response (connection_uris / endpoints), which
# is correct because it describes the compute we just created.
#
# Usage:
#   backend/scripts/new_test_branch.sh
#
# Requires: neonctl (authed — run `neonctl auth` if not), jq.

set -euo pipefail

# backend/ dir = parent of this scripts/ dir.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# Neon identifiers + the shared env-file path come from the single source of
# truth shared with pennydb.
# shellcheck source=neon_env.sh
source "$SCRIPT_DIR/neon_env.sh"

# The env file lives in the per-machine workspace so every worktree shares it;
# backend/.env.test becomes a symlink to it (back-compat with `source .env.test`).
ENV_TEST_FILE="$PENNY_ENV_TEST_FILE"
ENV_TEST_SYMLINK="$BACKEND_DIR/.env.test"

# --- Preflight --------------------------------------------------------------
# PENNYDB_ENV_FILE redirects pennydb's READS for offline fixtures; refresh has
# real side effects (deletes test-* branches, repoints the symlink) that the
# override cannot isolate — a redirected refresh would delete live branches
# while leaving the shared env file pointing at a dead one. Refuse it.
if [[ -n "${PENNYDB_ENV_FILE:-}" ]]; then
  echo "ERROR: refresh does not honor PENNYDB_ENV_FILE (it deletes real Neon" >&2
  echo "       branches and rewrites the shared env file). Unset it first." >&2
  exit 2
fi

require_neonctl
command -v jq >/dev/null 2>&1 || {
  echo "ERROR: jq not found. Install jq (e.g. 'brew install jq')." >&2
  exit 1
}

# Confirm auth / project access early with a cheap call.
if ! "${NEON[@]}" branches list --output json >/dev/null 2>&1; then
  echo "ERROR: cannot list branches for project $PROJECT_ID." >&2
  echo "       Are you authed? Run:  neonctl auth" >&2
  exit 1
fi

is_protected() {
  local name="$1"
  for p in "${PROTECTED_BRANCHES[@]}"; do
    [[ "$name" == "$p" ]] && return 0
  done
  return 1
}

# --- Re-branch aggressively: delete prior session test branches -------------
echo ">> Deleting prior session branches (prefix '${TEST_PREFIX}')..."
existing_json="$("${NEON[@]}" branches list --output json)"
while IFS=$'\t' read -r bid bname; do
  [[ -z "$bid" ]] && continue
  if is_protected "$bname"; then
    continue
  fi
  if [[ "$bname" == ${TEST_PREFIX}* ]]; then
    echo "   - deleting $bname ($bid)"
    "${NEON[@]}" branches delete "$bid" >/dev/null
  fi
done < <(echo "$existing_json" | jq -r '.[] | [.id, .name] | @tsv')

# --- Create the fresh one-off branch ----------------------------------------
TS="$(date +%Y%m%d-%H%M%S)"
NEW_BRANCH="${TEST_PREFIX}${TS}"

echo ">> Creating branch '$NEW_BRANCH' off '$PROD_BRANCH' (with compute)..."
create_json="$("${NEON[@]}" branches create \
  --name "$NEW_BRANCH" \
  --parent "$PROD_BRANCH" \
  --compute \
  --type read_write \
  --output json)"

NEW_BRANCH_ID="$(echo "$create_json" | jq -r '.branch.id')"
[[ -n "$NEW_BRANCH_ID" && "$NEW_BRANCH_ID" != "null" ]] || {
  echo "ERROR: failed to parse new branch id from create response." >&2
  echo "$create_json" >&2
  exit 1
}

# --- Resolve the branch's OWN endpoint + a usable connection URI ------------
# Prefer the pooled connection_uri from the create response (correct endpoint),
# falling back to assembling one from the branch's own endpoint host.
CONN_URI="$(echo "$create_json" \
  | jq -r '(.connection_uris // [])[0].connection_uri // empty')"

ENDPOINT_HOST="$(echo "$create_json" \
  | jq -r '(.endpoints // [])[0].host // empty')"

if [[ -z "$ENDPOINT_HOST" || "$ENDPOINT_HOST" == "null" ]]; then
  # Last resort: derive host from the connection_uri we got.
  if [[ -n "$CONN_URI" ]]; then
    ENDPOINT_HOST="$(url_host "$CONN_URI")"
  fi
fi

[[ -n "$ENDPOINT_HOST" && "$ENDPOINT_HOST" != "null" ]] || {
  echo "ERROR: could not resolve the new branch's endpoint host." >&2
  echo "$create_json" >&2
  exit 1
}

# Build the DATABASE_URL from the branch's OWN endpoint host. If the create
# response gave us a connection_uri (which already embeds the password), reuse
# its credentials/host but normalize to a direct sslmode=require URL.
if [[ -n "$CONN_URI" ]]; then
  DATABASE_URL="$CONN_URI"
  # Ensure sslmode=require is present.
  if [[ "$DATABASE_URL" != *"sslmode="* ]]; then
    if [[ "$DATABASE_URL" == *\?* ]]; then
      DATABASE_URL="${DATABASE_URL}&sslmode=require"
    else
      DATABASE_URL="${DATABASE_URL}?sslmode=require"
    fi
  fi
else
  # Fallback: the create response carried no connection_uri (rare). Ask neonctl
  # for one by NEW BRANCH ID. The documented bug is about --branch-NAME; passing
  # the new branch's own --branch-id targets the correct endpoint. We then
  # rewrite its host to the endpoint host we resolved above, as defense against
  # the parent-endpoint bug, and ensure sslmode=require.
  CS="$("${NEON[@]}" connection-string \
    --branch-id "$NEW_BRANCH_ID" \
    --database-name "$DB_NAME" \
    --role-name "$ROLE_NAME" \
    --pooled 2>/dev/null || true)"
  [[ -n "$CS" ]] || {
    echo "ERROR: create response had no connection_uri and connection-string" >&2
    echo "       fallback failed. Cannot assemble DATABASE_URL safely." >&2
    exit 1
  }
  # Force the host to the new branch's OWN endpoint (bug workaround).
  DATABASE_URL="$(echo "$CS" | sed -E "s#@[^/:]+#@${ENDPOINT_HOST}#")"
  if [[ "$DATABASE_URL" != *"sslmode="* ]]; then
    if [[ "$DATABASE_URL" == *\?* ]]; then
      DATABASE_URL="${DATABASE_URL}&sslmode=require"
    else
      DATABASE_URL="${DATABASE_URL}?sslmode=require"
    fi
  fi
fi

# --- Write the shared env file + backend/.env.test symlink -------------------
mkdir -p "$(dirname "$ENV_TEST_FILE")"
cat > "$ENV_TEST_FILE" <<EOF
# $ENV_TEST_FILE — generated by pennydb test refresh
# (scripts/new_test_branch.sh). Shared by every worktree; backend/.env.test is
# a symlink here. Points the dev/test loop at a FRESH one-off Neon branch
# forked from production. Regenerate any time with: pennydb test refresh
#
# Branch:      $NEW_BRANCH
# Branch ID:   $NEW_BRANCH_ID
# Forked from: $PROD_BRANCH
# Endpoint:    $ENDPOINT_HOST
# Created:     $TS
#
# Usage: pennydb test psql | pennydb test exec -- uvicorn … — or the classic
# (from backend/):
#   set -a && source .env.test && set +a
#   uv run uvicorn penny.api.main:app --host 127.0.0.1 --port 8000 --reload
#
# NOTE: main.py calls load_dotenv(override=False), so sourcing this BEFORE
# launching uvicorn ensures DATABASE_URL here wins over the root .env value.
# Values are single-quoted: the URL can contain '&' (e.g. channel_binding),
# which an unquoted sourced assignment silently backgrounds.
DATABASE_URL='$DATABASE_URL'
PENNY_TEST_BRANCH='$NEW_BRANCH'
PENNY_TEST_ENDPOINT='$ENDPOINT_HOST'
EOF

# Back-compat: keep backend/.env.test working in this checkout as a symlink to
# the shared file (replaces a stale regular file from the pre-pennydb layout).
ln -sfn "$ENV_TEST_FILE" "$ENV_TEST_SYMLINK"

# --- Report -----------------------------------------------------------------
REDACTED_HOST="$ENDPOINT_HOST"
echo
echo ">> Done."
echo "   New branch:   $NEW_BRANCH ($NEW_BRANCH_ID)"
echo "   Endpoint host: $REDACTED_HOST"
echo "   Wrote:        $ENV_TEST_FILE"
echo
echo "   To use it (from repo root):"
echo "     backend/scripts/pennydb test psql"
echo "     backend/scripts/pennydb test exec -- uv run --project backend uvicorn penny.api.main:app --host 127.0.0.1 --port 8000 --reload"
