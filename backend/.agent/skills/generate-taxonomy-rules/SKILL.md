---
name: generate-taxonomy-rules
description: Regenerate the workspace taxonomy-rules.md — the natural-language category guide the categorizer agent reads — from the live categories table plus the existing rules document.
when_to_use: When taxonomy category descriptions change (any add/remove/rename/merge/split, or a direct edit to categories.description), so the taxonomy rules document stays in sync. Usually invoked as the final step of migrate-taxonomy.
---

# Skill: Generate Taxonomy Rules

## Purpose

The taxonomy rules document is the natural-language guide the categorizer
agent reads when it chooses a category. It lives in the workspace:

```
$PENNY_WORKSPACE/memory/taxonomy-rules.md      (default: ~/.penny/memory/taxonomy-rules.md)
```

It does two jobs:

1. **Defines the taxonomy, listing every active category and its subcategories** — `name (key)` grouped under its parent,
   each with a definition that includes examples and exclusions. These
   definitions mirror the `description` field of the `categories` table, which
   is the **source of truth** for per-category meaning.
2. **Holds the cross-category decision logic** — the precedence and
   disambiguation rules that no single category definition can express on its
   own, because they describe how categories relate when more than one could
   apply.

So per-category meaning comes from the database; this document folds it in and
adds the decision logic on top. Because the database owns descriptions, every
path that changes a description must regenerate this file — this skill is that
path.

## When to Use

- As the final step of `migrate-taxonomy`, after the structural DB change.
- Whenever a `categories.description` changes for any reason.

## What the Decision Logic Sections Mean

These sit **below** the category listing, so the agent reads keys,
definitions and hierarchy first, then "how to choose when two could apply":

- **Global Decision Order** — an ordered precedence list applied when several
  categories plausibly fit. The agent walks it top to bottom and takes the
  first that applies (e.g. banking movements, then income, then debt, …, with
  Other/Unknown last).
- **Overlap Resolution** — rules for known cross-category ambiguities (e.g. a
  mortgage payment is Housing, not Debt; a P2P transfer with a clear memo goes
  to that purpose, otherwise to the generic transfer category).
- **Edge Case Guidance** — merchant-pattern and special-case handling (e.g.
  marketplace aggregators map to Shopping unless line items are known;
  embedded sales tax stays with the purchase it belongs to).

## Inputs to a Regeneration

1. **The existing `taxonomy-rules.md`** — the source of the decision logic and
   the prior decisions encoded in it. The
   decisions in its Global Decision Order / Overlap Resolution / Edge Case
   Guidance carry forward from one regeneration to the next to preserve stability
   for the categorizer as the taxonomy evolves.

2. **The migration just performed** — what changed and why.

## How to Regenerate

Pick the lightest mode that fits the change.

### Targeted edit (small change)

For a single add/remove/rename/merge/split: edit the file in place. Add, drop,
or rename the affected category's definition (pulling the new definition text
from the DB `description`), and fix any references to it in the decision-logic
sections so the prose matches the new taxonomy.

### Full regeneration (large change)

Rebuild the whole document:

1. **Category Definitions** — from the DB query, render `name (key)` grouped
   top-level → child, each with its `description` as the definition.
2. **Carry the decision logic forward** — bring the Global Decision Order,
   Overlap Resolution, and Edge Case Guidance over from the existing document.
   These encode choices already made; preserve them.
3. **Reconcile with the migration** — where the carried-over logic conflicts
   with what the migration changed, **the migration takes precedence**. Adapt
   the logic to match: rename references, drop rules about removed categories,
   fold a split's old rule into its successors, and so on. Use your judgment on
   how to reconcile — most conflicts are routine adaptations.
4. **Escalate only when genuinely stuck** — if a conflict is irreconcilable and
   you are not comfortable making the call, ask the user. Do not escalate
   routine adaptations; do escalate a real semantic contradiction between an
   existing decision and the migration's intent.

Write the result to `$PENNY_WORKSPACE/memory/taxonomy-rules.md` as plain
Markdown.

For both targeted edits and full regeneration, as a final pass, be sure to review
the document in its entirety, with the goal of deconflicting. Make the edits necessary
to ensure the resulting document reflects the taxonomy accurately while maintaining
Decision Order, Overlap Resolution and Edge Case Guidance that are free from conflicts.

## Document Structure

```
# Taxonomy Rules

<one short paragraph: what this taxonomy covers>

## Category Definitions

### <Parent Name> (parent.key)
- <Child Name> (parent.child) — <definition, with examples and exclusions>
- ...

## Global Decision Order
1. ...

## Overlap Resolution
- ...

## Edge Case Guidance
- ...
```

## Important Notes

- **The DB is the source of truth for definitions.** Never write a definition
  here that contradicts `categories.description` — fix the DB first, then
  regenerate.
- **Decisions persist; the migration wins conflicts.** The decision-logic
  sections carry forward across regenerations; when they clash with a
  migration's change, the migration takes precedence.
- **Write plain Markdown.**
