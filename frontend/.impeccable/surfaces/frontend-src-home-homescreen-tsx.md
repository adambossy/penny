---
version: 1
slug: "frontend-src-home-homescreen-tsx"
primary_target: "frontend/src/home/HomeScreen.tsx"
related_targets: ["frontend/src/home/spread.tsx","frontend/src/home/artifacts.tsx"]
---

Scope: the public logged-out landing page (`/`, dev preview `/home`). Visitor
mode: **Persuade** — design is the product here; the visitor decides and acts.

Audience & job: a couple with intertwined finances, arriving skeptical of one
more budgeting app. They need to believe Penny reasons over their whole history
rather than reporting last month's totals, and the action is a single one —
sign up.

Proof available: none of the usual kind. Penny is pre-launch, so there are no
testimonials, customers, metrics, or press, and none may be invented
(`REQUIREMENTS.txt` P13a). The page therefore earns belief by *demonstrating*
the reasoning: six worked artifacts showing what Penny actually returns, built
from authored sample data that is internally consistent and labelled
illustrative wherever it renders.

Direction: **the reconciliation spread**. Three columns in the same order on
every line — claim, difference, evidence — because all six capabilities are
literally two figures and the gap between them. The composition never flips
sides; alternating feature rows are the template tell this page exists to
refuse. Density and evidence type vary, layout does not.

Memorable moment: the continuous ink gutter running the height of the page,
posting each finding in gold. It doubles as the first viewport's thesis — the
opening shows the whole page in miniature ($4,100 assumed against $4,661
actual, +$561 in gold) before the reader meets line 01, which is that same
$6,730 spread across twelve months.

Constraints this surface must hold:
- `e2e/home.spec.ts` pins the contract: h1 contains "Meet Penny"; header links
  "Meet Penny" → /sign-up and "Sign in" → /sign-in; an "Ask Penny" button;
  six sections with ids analyze/project/budget/forecast/trends/optimize;
  **exactly 8** `a[href="/sign-up"]` (1 header + 6 entries + 1 close); a closing
  link matching /start chatting with penny/i.
- The page never talks to the agent — no auth, no API calls. Every affordance,
  including the ask input and the sample questions, routes to sign-up.
- Sample questions and the ask button are `<button>`, not links, so the 8-link
  count stays exact. Adding any CTA means updating the test deliberately.

Unresolved: pricing and packaging are undecided, so the page makes no pricing
claim and has no pricing section. Copy is expected to keep iterating; the
structure and CTA routing are the requirement, not the words.
