# Penny Agent Core

The agent and the finance domain: syncing bank transactions, resolving who
they were with, categorizing them, and answering questions about them — for
a single user on their own machine. No accounts, no tenants: the user's
machine and database are the boundary. (The multi-tenant vocabulary —
household, invite, principal, visibility — lives on in the hosted product's
penny-web repo and the `legacy/saas-monolith` freeze, not here.)

## Language

### Identity

**User**:
The one person this installation serves. A stable local user UUID is minted
in the workspace (`penny.identity`) for things that need a durable ref (e.g.
Plaid link tokens). _Avoid_: account (that's a bank account)

### Money movement

**Transaction**:
Unqualified, the derived record — the mutable, enriched, categorized
transaction that queries, reports, and chat operate on.
_Avoid_: bare "transaction" for the immutable Plaid source row

**Plaid transaction**:
The immutable source record of a bank/card transaction as synced from Plaid
(or imported from CSV). Never edited; all enrichment happens on the
transaction derived from it.

**Plaid item**:
A bank connection — one login at one institution, holding one or more
accounts. _Avoid_: bare "item"

**Plaid account**:
A single bank or card account within a Plaid item.

**Sync**:
The idempotent, re-runnable pull of transactions from every connected Plaid
item into the finance database. One broken connection never aborts the rest.

**Descriptor**:
The raw string a bank statement shows for a transaction's counterparty
(e.g. "AplPay MY FAVORITE CBROOKLYN").

**Wrapper descriptor**:
A descriptor naming a payment rail (Venmo, Zelle, ATM, bill-pay) rather than
the real counterparty behind it.

**Merchant**:
The resolved, stable identity of who a transaction was with — the umbrella
term, even when that identity is a person. Produced by normalization.

**Counterparty**:
The human or entity behind a wrapper descriptor (e.g. the friend behind a
Venmo payment).

**Normalization**:
Resolving a raw descriptor to its merchant.

**Sign convention**:
The per-account mapping of amount sign to money-in vs money-out.

**Refund match**:
The link from a refund transaction back to the original transaction it
reverses, made by the user or automatically.

### Categorization

**Taxonomy**:
The user's two-level tree of categories used to classify spending; seeded at
bootstrap and editable to match how they think about their money.

**Category**:
A top-level node of the taxonomy. Unqualified "category" implies the top
level.

**Subcategory**:
A child node of a category. _Avoid_: child category

**Categorization**:
Assigning a category (or subcategory) to each transaction with an LLM against
the taxonomy; split transactions are categorized per line item.

**Merchant rule**:
A user-authored rule that pins how a merchant's transactions are
categorized, overriding the LLM.

**Deprecated category**:
A category retired from the taxonomy without erasing the history that used it.

### Itemization

**Line item**:
A single line within a transaction — description, amount, quantity — sourced
from Amazon scraping, an email receipt, or manual entry. _Avoid_: bare "item"

**Itemization**:
Enriching a lump charge into its per-item lines so spending is attributed per
item rather than per charge.

**Amazon item**:
A line of a scraped Amazon order, the raw material for itemizing the matching
transaction. _Avoid_: bare "item"

**Email receipt**:
A parsed receipt email used to itemize the transaction it matches.

**Split**:
Dividing one transaction into several so each part carries its own category
(e.g. an itemized Amazon order).

### Agent

**Workspace**:
The persistent store of agent state (memory notes, reports, logs, config)
that carries across chat, scheduled runs, and the MCP surface — `~/.penny`
(an existing `~/.transactoid` is honored).

**Memory**:
Durable notes the agent writes to the workspace to carry user context (e.g.
budget notes) across runs.

**Report**:
The recurring weekly spending report produced by the agent (run by the local
daemon) and delivered by email over the user's configured SMTP.

**Nudge**:
An agent-initiated onboarding prompt toward a setup step, appearing at most
once per turn, until the step is accepted or dismissed.

**Daemon**:
The one long-lived local scheduler (`penny daemon`), installed as a user
service, owning all scheduled work: sync every N hours and the weekly
report. Job state is inspectable via `penny daemon status` and the agent's
`sync_status` tool.
