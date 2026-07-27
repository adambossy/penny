# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

**Primary: couples and households** — two people whose finances are
intertwined and who want them reasoned about together. The household is the
tenancy unit, not a feature bolted onto a single-player app: joint
conversations, per-member message attribution, per-row `private`/`shared`
visibility, invites, and a joint session that sees shared data only are all
core to the primary case.

Solo users are fully supported and are the on-ramp — signup auto-provisions an
isolated one-person household — but when a design decision forces a choice, the
two-person household wins.

One individual belongs to exactly one household. There is no household merge
and no multi-household membership; someone who wants into a different household
starts over and re-links their bank.

The user is financially engaged: they already care about their money closely
enough to connect real bank accounts and ask questions a spreadsheet can't
answer.

## Product Purpose

Penny is a personal-finance **agent**. It syncs bank and card transactions from
connected Plaid items, resolves each descriptor to a real merchant or
counterparty, categorizes every transaction (and every line of an itemized
order) against a two-level taxonomy, and then answers natural-language
questions about that data through a streaming chat interface. It also delivers
recurring spending reports by email — daily (rolled up to monthly on the 1st)
and weekly — produced by the same agent that powers chat.

Success is the user getting a real, reasoned answer to a hard question about
their own money — one they could not have gotten from a budgeting app, and
would not have assembled by hand.

## Positioning

Penny reasons over the user's **entire** financial history, not the last
month's totals. The mechanism a neighboring product cannot truthfully copy: an
LLM agent with read-only SQL access to the user's complete, merchant-normalized,
per-item-itemized transaction history, isolated per household.

All six of these are capabilities Penny genuinely has today, not aspirations:

- **Root-cause analysis** — detect the months where spending broke pattern and
  decompose each spike to the merchants and one-off events behind it,
  separating a real behavior shift from a single anomalous charge.
- **Scenario modeling** — project a decision forward from real balances (rent
  vs. buy, a job change, a large purchase): down payment, opportunity cost,
  principal paydown, appreciation, and the break-even point.
- **Disciplined budgeting** — build from a full 12 months of averages,
  smoothing trips and holidays, then contract each discretionary line by a
  chosen margin while holding essentials fixed.
- **Forward cash flow** — project balances from recurring income, bills and
  subscriptions, and the seasonal shape of the user's own history, flagging the
  months they will run tight.
- **Behavioral trends** — year over year, category by category, isolating where
  a baseline is quietly ratcheting up from where the user simply splurged once.
- **Goal optimization** — take a target (amount and deadline) and
  reverse-engineer it against actual spending, ranking specific cuts from least
  to most painful.

## Operating Context

**Chat is the only front door.** There is no dashboard, wizard, stepper, or
setup screen. A new user lands directly in a conversation, and setup happens
through the agent: it nudges toward connecting a bank, reviewing account
visibility, tuning the taxonomy, and adding merchant rules at deterministic
milestones — at most once per turn, never in a joint thread, and never again
once a step is dismissed (though it stays revisitable by simply asking). The
onboarding state driving these nudges is invisible to the user and never
appears in the transcript.

**Capabilities arrive as inline cards inside chat**, not as separate screens:
Plaid Link (initial connect and re-authentication of a broken connection) and
"Connect a provider" for credentials both render in the conversation. Plaid runs
in the browser; the public token is exchanged server-side.

**Surfaces that exist:** a public logged-out marketing home page at `/`
(hero with sample prompts, six-part feature tour with demo conversations,
closing call-to-action); sign-in and sign-up; new chat; a deep-linkable
conversation at `/c/:id`; a chat-history drawer listing the principal's
conversations newest-first; a providers & billing settings screen; invite
management; and a design-system gallery. The URL is the source of truth for
what is on screen, and browser back/forward walk between views.

**Beyond the browser:** recurring email reports; a persistent per-user
workspace (`memory/`, `reports/`) carrying state such as budget notes across
both chat and scheduled runs, partitioned into a shared workspace per household
and a private one per user; and optional Amazon order scraping so a lump charge
becomes per-item spending.

**Evaluation context:** the person judging Penny is looking at their own real
money. Plausible-looking output that is wrong is worse than no answer.

## Capabilities and Constraints

**Confirmed capabilities** are enumerated as P1–P13 in `REQUIREMENTS.txt`, the
living spec — that file, not this one, is the authority on functional scope, and
a behavioral claim not present there should be treated as unverified.

**Durable constraints:**

- **Light-only appearance.** The web app renders exclusively in the light
  "cream" design system on every screen. It never follows the OS
  `prefers-color-scheme: dark` preference and exposes no light/dark toggle
  (`REQUIREMENTS.txt` P11). Design work operates entirely within the light
  system; there is no dark variant to design for.
- **English (US) and `$` only.** Reports and chat responses are written in
  English with dollar currency regardless of the language of transaction
  descriptors or memory notes. There is no localization or i18n today.
- **The agent's SQL access is read-only.** `run_sql` accepts a single read-only
  `SELECT`; the agent cannot mutate finance data through it.
- **One individual = one household.** No merge, no multi-household membership.
  An email that already belongs to an active account cannot be invited.
- **Invites reach new people only.** A household member can invite accountless
  people by email; the invitee signs up straight into the inviter's household.
- **Data isolation is enforced, not advisory.** Every financial row belongs to
  exactly one household and one owning user with `private`/`shared` visibility,
  enforced by Postgres row-level security plus app-level filtering.
- **Signup is fully open.** Any visitor can sign up via social login and is
  auto-provisioned an isolated household with its own seeded taxonomy. Cost is
  bounded by the per-user subsidy cap, not by a gate.
- **BYO keys after a small subsidy.** Each user gets a small subsidized token
  runway on their first Plaid link (default $2); once spent, the user must
  connect their own provider credential — an API key or a sanctioned OAuth
  subscription — to keep chatting. There is no ambient fallback to a platform
  key on a billing-enabled deployment: with no credential and no runway, the
  turn is blocked rather than silently billed to the platform.

**Explicitly undecided:** pricing and packaging beyond the default $2 subsidy
runway. No pricing page or plan structure exists, and none should be invented.

**Terminology** is fixed by the glossary in `backend/CONTEXT.md` and must be
reused rather than paraphrased in any user-facing copy: *household*, *user*,
*pending user*, *invite*, *principal*, *joint session*, *visibility*,
*transaction* (the derived, enriched record) vs. *Plaid transaction* (the
immutable source row), *Plaid item* (one bank login), *Plaid account*,
*sync*, *descriptor*, *wrapper descriptor*, *merchant*, *counterparty*,
*normalization*, *categorization*, *itemization*. Avoid "account" for a person
and "member" as an entity name.

## Brand Commitments

- **Name:** Penny. The agent is referred to in the third person and gendered
  she/her in existing product copy.
- **Visual identity is already established and binding.** The light-only cream
  design system — its tokens, theme, primitives, logo assets, and self-hosted
  fonts — lives in `frontend/packages/ui` and is the incumbent visual
  authority for every screen. It is a canonical product constraint (P11), not a
  starting suggestion. Logo assets exist in light-only form; there are no dark
  variants.
- **Not confirmed binding:** the tagline "your finance savant" and the rest of
  `frontend/src/home/copy.ts`, which that file itself labels placeholder
  wording carried from a design reference. Copy is expected to iterate; the home
  page's structure and call-to-action routing are the requirement, not its
  words.

## Evidence on Hand

**Real, and usable:**

- The author's own live financial data — connected Plaid items, real synced and
  categorized transactions.
- A working, deployed product (backend, frontend, cron-manager, and a
  categorizer-eval app), with real scheduled email reports going out.
- Hand-authored demo conversations in `frontend/src/home/demos.tsx`, used on the
  marketing page as illustrations of Penny's reasoning. These are written
  examples, not captured transcripts from real users, and must not be presented
  as the latter.

**Absent — future work must not fabricate any of it.** Penny is pre-launch,
with the author as its only real user. There are **no** testimonials, quotes,
named customers, customer logos, user or household counts, growth or usage
metrics, retention numbers, dollars-analyzed figures, press mentions, awards,
case studies, third-party reviews, ratings, funding announcements, team
bios, uptime or accuracy benchmarks, security certifications, or completed
audits. No "trusted by", no "join N users", no star ratings, no logo wall.
Where a surface would conventionally carry social proof, it must earn belief
another way — by demonstrating the reasoning itself.

## Product Principles

1. **Chat is the product; there is no second interface.** New capability
   arrives through conversation or an inline card within it — never as another
   dashboard, wizard, or settings maze. A surface that pulls the user out of the
   conversation has to justify itself.
2. **Answer the hard question, not the easy one.** Depth over totals: root
   cause, projection, trade-off, computed across the full history. Never build a
   surface whose promise is a summary Penny already surpasses.
3. **Two people, one set of finances.** Every surface must read correctly for a
   household of two with a mix of private and shared rows — not a solo design
   retrofitted with a sharing toggle.
4. **Claim only what is true.** With one real user and no proof on hand, every
   capability claim traces to `REQUIREMENTS.txt` and every number is real or
   absent. Invented evidence is the one failure that costs a finance product
   everything.
5. **Make isolation and cost legible.** Who can see a row, which household it
   belongs to, whose credential is paying for a turn, and what remains of a
   runway are user-facing promises about money and privacy, not backend
   implementation details. Surfaces should let the user verify them, not just
   trust them.

## Accessibility & Inclusion

No product-specific accessibility standard or target has been established, and
none is claimed. Two established constraints shape the work: the interface is
light-only by requirement, so all contrast and legibility work happens within
the light cream system with no dark variant to fall back on; and the product is
English (US) with `$` currency only, with no localization today.
