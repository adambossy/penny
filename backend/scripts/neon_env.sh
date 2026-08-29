# neon_env.sh — shared Neon identifiers for the Penny project's dev DB tooling.
#
# Single source of truth for pennydb and new_test_branch.sh. Sourced, not run.

ORG_ID="org-sweet-sky-03842625"
PROJECT_ID="purple-poetry-32142000"   # "Penny"
PROD_BRANCH="production"               # long-lived; never deleted, never forked over
TEST_PREFIX="test-"                    # one-off session branches carry this prefix
DB_NAME="neondb"
ROLE_NAME="neondb_owner"

# Branches that are off-limits to deletion no matter what.
PROTECTED_BRANCHES=("production" "penny-test")

# The per-machine env file the dev loop and pennydb read the test-branch
# DATABASE_URL from. Lives in the workspace so every worktree shares it.
# Default workspace mirrors penny.workspace.resolve_workspace_dir: ~/.penny,
# unless only the legacy ~/.transactoid exists.
_PENNY_WORKSPACE_DEFAULT="$HOME/.penny"
if [ ! -e "$_PENNY_WORKSPACE_DEFAULT" ] && [ -e "$HOME/.transactoid" ]; then
  _PENNY_WORKSPACE_DEFAULT="$HOME/.transactoid"
fi
PENNY_ENV_TEST_FILE="${PENNYDB_ENV_FILE:-${PENNY_WORKSPACE:-$_PENNY_WORKSPACE_DEFAULT}/env.test}"

# Project-scoped neonctl invocation, e.g.: "${NEON[@]}" branches list
NEON=(neonctl --org-id "$ORG_ID" --project-id "$PROJECT_ID")

require_neonctl() {
  command -v neonctl >/dev/null 2>&1 || {
    echo "ERROR: neonctl not found. Install it and run 'neonctl auth'." >&2
    exit 1
  }
}

# Host part of a postgres URL (creds, port, path, params stripped). Pure
# parameter expansion — no forks.
url_host() {
  local h="${1#*://}"
  h="${h#*@}"
  echo "${h%%[/:?]*}"
}
