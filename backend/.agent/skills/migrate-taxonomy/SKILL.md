---
name: migrate-taxonomy
description: Orchestrate a taxonomy migration end-to-end — invoke the migrate_taxonomy tool, then propagate updates to dependent artifacts (configs, workspace taxonomy rules, merchant rules, budget, agent memory).
when_to_use: When the user asks to add, remove, rename, merge, split, or deprecate a taxonomy category, or restructure the category hierarchy.
---

# Skill: Migrate Taxonomy

## Purpose

Orchestrate a complete taxonomy migration end-to-end: execute the structural change via the `migrate_taxonomy` MCP tool, then propagate updates to all dependent artifacts (config files, prompts, merchant rules, budget, agent memory).

## When to Use

Use this skill when:
- A user asks to add, remove, rename, merge, split, or deprecate a category
- Two categories overlap and need to be consolidated
- A category needs to be retired (soft-deleted)
- The taxonomy structure needs reorganization

## What Gets Impacted

A taxonomy migration touches multiple layers. The `migrate_taxonomy` tool handles the database and transaction reassignment. Everything else is this skill's responsibility:

| Artifact | Handled by `migrate_taxonomy` tool | Handled by this skill |
|---|---|---|
| Database (categories table) | Yes | - |
| Transactions (reassignment) | Yes | - |
| Categorization cache | Yes (cleared) | - |
| `memory/merchant-rules.md` | - | Yes (via `edit-merchant-rules-memory` skill) |
| `configs/merchant-rules.md` | - | Yes |
| `configs/taxonomy.yaml` | - | Yes |
| `memory/taxonomy-rules.md` | - | Yes (via `generate-taxonomy-rules` skill) |
| `memory/budget.md` | - | Regenerate if affected (via `generate-budget` skill) |

## Workflow

### Step 1: Assess the migration

Before making changes, understand the scope:

1. **Identify affected transactions**:
   ```sql
   SELECT c.key, c.name, COUNT(dt.transaction_id) as tx_count
   FROM categories c
   LEFT JOIN derived_transactions dt ON dt.category_id = c.category_id
   WHERE c.key IN ('<source_key>', '<target_key>')
   GROUP BY c.key, c.name
   ```

2. **Identify affected merchant rules**:
   ```bash
   grep -i "<category_key>" memory/merchant-rules.md
   grep -i "<category_key>" configs/merchant-rules.md
   ```

3. **Check for budget impact**:
   ```bash
   grep -i "<category_display_name>" memory/budget.md
   ```

4. **Present the impact summary to the user** before proceeding. Include:
   - Number of transactions affected
   - Merchant rules that will change
   - Whether budget will need regeneration

### Step 2: Execute the migration tool

Call the `migrate_taxonomy` MCP tool with the appropriate operation. This handles the database changes and transaction reassignment.

**Add:**
```json
{"operation": "add", "key": "parent.child", "name": "Display Name", "parent_key": "parent", "description": "..."}
```
`description` is **required** for `add`: write the category's real definition —
what belongs here, with examples and exclusions. It becomes the source-of-truth
`categories.description` and is folded into the regenerated taxonomy rules, so a
placeholder is not acceptable.

**Remove:**
```json
{"operation": "remove", "key": "parent.old_child", "fallback_key": "parent.new_child"}
```

**Rename:**
```json
{"operation": "rename", "old_key": "old.key", "new_key": "new.key"}
```

**Merge:**
```json
{"operation": "merge", "source_keys": ["a.old1", "a.old2"], "target_key": "a.target", "recategorize": false}
```

**Split:**
```json
{"operation": "split", "source_key": "a.broad", "targets": [["a.specific1", "Name 1", null], ["a.specific2", "Name 2", null]]}
```

**Deprecate (soft-delete):**
Use `remove` with a `fallback_key` to reassign transactions, then mark the category as deprecated:
```sql
UPDATE categories SET deprecated_at = NOW() WHERE key = '<old_key>'
```
The deprecated row stays in the database (preserving FK integrity with the `transaction_category_events` audit log) but is excluded from `fetch_categories()`, `{{CATEGORY_TAXONOMY}}` prompt injection, and agent SQL queries (via `WHERE c.deprecated_at IS NULL`).

### Step 3: Update `configs/taxonomy.yaml`

The migration tool updates the database but does NOT update the YAML config. Edit the file to match:

- **Add**: Insert the new category entry in the correct position
- **Remove/Deprecate**: Delete the entry from the YAML
- **Rename**: Update the `key` field (and `parent_key` references in children)
- **Merge**: Remove source entries, keep target
- **Split**: Remove source entry, add target entries

### Step 4: Update merchant rules

Use the `edit-merchant-rules-memory` skill to update category keys in `memory/merchant-rules.md`. For each rule that references an old/removed category key, update it to the new key.

Also update `configs/merchant-rules.md` (a separate checked-in file) manually:

```bash
grep -n "<old_category_key>" configs/merchant-rules.md
```

Replace old category keys with new ones in any matching rules.

### Step 5: Regenerate the taxonomy rules

Use the `generate-taxonomy-rules` skill to update
`memory/taxonomy-rules.md` so its category definitions and
decision logic match the new taxonomy. Tell that skill what this migration
changed (and why) so it can reconcile the carried-over decision logic — where
the existing decisions conflict with this migration, the migration takes
precedence.

### Step 6: Regenerate budget (if affected)

If the migration touched categories that appear in the budget, regenerate it using the `generate-budget` skill. Check first:

```bash
grep -i "<category_name>" memory/budget.md
```

If matches are found, the budget is stale and should be regenerated.

### Step 7: Verify

Run verification queries to confirm the migration is complete:

1. **No transactions on old category** (for remove/merge/deprecate):
   ```sql
   SELECT COUNT(*) FROM derived_transactions dt
   JOIN categories c ON dt.category_id = c.category_id
   WHERE c.key = '<old_key>'
   ```

2. **Deprecated flag set** (for deprecation):
   ```sql
   SELECT key, deprecated_at FROM categories WHERE key = '<old_key>'
   ```

3. **No stale references in config files**:
   ```bash
   grep -r "<old_key>" configs/ memory/
   ```

4. **Report results to user**: transaction count moved, merchant rules updated, config files changed.

## Example: Merge Overlapping Categories

**User**: "Merge childcare_and_babysitting into childcare.care"

1. Query transactions under `education_and_childcare.childcare_and_babysitting` -> 30 transactions, 14 merchants
2. Present impact: "30 transactions across 14 merchants will move to childcare.care"
3. Call `migrate_taxonomy` with `{"operation": "merge", "source_keys": ["education_and_childcare.childcare_and_babysitting"], "target_key": "childcare.care"}`
4. Remove entry from `configs/taxonomy.yaml`
5. Use `edit-merchant-rules-memory` skill to update `memory/merchant-rules.md`
6. Update `configs/merchant-rules.md` (Julia nanny rule -> `childcare.care`)
7. Regenerate `memory/taxonomy-rules.md` via the `generate-taxonomy-rules` skill (drop the merged category's definition; fix any references to it in the decision-logic sections)
8. Verify: 0 transactions remain on old key, no stale references

## Important Notes

- **Always present impact before executing**: Taxonomy changes can affect many transactions and downstream artifacts.
- **Two merchant-rules files exist**: `configs/merchant-rules.md` (checked in) and `memory/merchant-rules.md` (agent memory). Both must be updated.
- **Taxonomy rules live in the workspace**: regenerate `memory/taxonomy-rules.md` via the `generate-taxonomy-rules` skill, not the codebase prompts.
- **Budget becomes stale**: After any migration affecting budgeted categories, regenerate using the `generate-budget` skill.
- **`configs/taxonomy.yaml` is not auto-synced**: The migration tool operates on the database. Always update the YAML to keep it in sync.