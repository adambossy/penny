"""Build Penny's core toolset (all non-plugin domain tools)."""

from __future__ import annotations

from agent_harness import StaticToolset
from agent_harness.core.toolsets import Toolset

from ._services.onboarding import OnboardingResolver
from .analytics import generate_chart, run_sql
from .audit import (
    category_history,
    find_similar_tagged_transactions,
    transaction_tags,
)
from .delivery import send_email_report, upload_artifact_to_r2
from .memory import generate_memory_index
from .onboarding import make_resolve_onboarding_item
from .plaid import connect_new_account, list_plaid_accounts, plaid_connection_status
from .plaid_link import connect_bank_account, relink_account
from .recategorize import (
    recategorize_merchant,
    recategorize_transaction,
    tag_transactions,
)
from .sign_conventions import (
    list_sign_conventions,
    re_derive_account,
    set_sign_convention,
)
from .sync import sync_status, sync_transactions
from .taxonomy import migrate_taxonomy
from .transactions import (
    hide_transactions,
    record_refund,
    split_transaction,
    unhide_transactions,
)


def build_toolset(*, onboarding_resolver: OnboardingResolver | None = None) -> Toolset:
    return StaticToolset(
        name="penny",
        tools=[
            # Plaid + sync
            list_plaid_accounts,
            plaid_connection_status,
            connect_new_account,
            connect_bank_account,
            relink_account,
            sync_transactions,
            sync_status,
            # Mutations
            recategorize_merchant,
            recategorize_transaction,
            tag_transactions,
            migrate_taxonomy,
            split_transaction,
            record_refund,
            hide_transactions,
            unhide_transactions,
            # Sign conventions + re-derive
            set_sign_convention,
            list_sign_conventions,
            re_derive_account,
            # Analytics
            run_sql,
            generate_chart,
            # Audit / history (read-only)
            category_history,
            transaction_tags,
            find_similar_tagged_transactions,
            # Delivery
            upload_artifact_to_r2,
            send_email_report,
            # Onboarding (resolver injected by the web app; None on front doors
            # that don't wire onboarding, e.g. the CLI/cron)
            make_resolve_onboarding_item(onboarding_resolver),
            # Memory
            generate_memory_index,
            # NOTE: the `bash` shell-exec tool was intentionally removed
            # (Phase 6 security finding F01/F06). InProcessSandbox.exec spawns a
            # subprocess with the full server os.environ, so any open-signup user
            # could exfiltrate every secret via `bash(["env"])`. A properly
            # sandboxed (env-stripped, network-isolated) shell tool may return
            # later; until then the agent has no subprocess-spawning surface.
        ],
    )
