---
name: recategorize-merchant
description: Recategorize all transactions for a merchant via the recategorize_merchant tool and persist a matching rule in memory/merchant-rules.md.
when_to_use: When the user asks to recategorize a merchant, change which category a merchant maps to, or fix systematically wrong categorizations for a specific merchant across all their transactions.
---

# Skill: Recategorize Merchant

## Purpose

Recategorize all transactions for a merchant and persist the rule in `memory/merchant-rules.md` so future syncs apply the same categorization automatically.

## When to Use

Use this skill when:
- A user asks to recategorize a merchant's transactions
- A merchant is consistently miscategorized and needs a permanent fix
- The user says something like "X should be categorized as Y"

## Workflow

### Step 1: Identify the merchant

If the user provides a merchant name (not an ID), look it up:

```sql
SELECT merchant_id, name, COUNT(*) as tx_count
FROM merchants m
JOIN derived_transactions dt ON dt.merchant_id = m.merchant_id
WHERE LOWER(m.name) LIKE LOWER('%<merchant_name>%')
GROUP BY m.merchant_id, m.name
ORDER BY tx_count DESC
```

Use the `run_sql` MCP tool to execute this query. If multiple merchants match, ask the user to disambiguate.

### Step 2: Validate the category key

Confirm the target `category_key` exists in the taxonomy. Use `run_sql` to check:

```sql
SELECT category_id, key, name FROM categories WHERE key = '<category_key>'
```

If the user provides a natural-language category (e.g., "groceries"), find the best match:

```sql
SELECT key, name FROM categories WHERE LOWER(name) LIKE LOWER('%<term>%')
```

Present options if ambiguous.

### Step 3: Recategorize transactions

Call the `recategorize_merchant` MCP tool:

```
recategorize_merchant(merchant_id=<id>, category_key="<key>")
```

This updates all **unverified** transactions for that merchant. Verified transactions are immutable.

### Step 4: Persist the rule in memory

After successful recategorization, check if a rule already exists for this merchant in `memory/merchant-rules.md`:

```bash
grep -i "<merchant_name>" memory/merchant-rules.md
```

**If no existing rule**, append a new one:

```bash
cat >> memory/merchant-rules.md << 'EOF'

## Rule: <Rule Name>
- **Category:** `<category_key>`
- **Patterns:** `<PATTERN_1>`, `<PATTERN_2>`
- **Description:** <One sentence describing the rule>
EOF
```

**If a rule already exists**, update the category and patterns using the Edit tool.

#### Rule field guidelines

- **Rule Name**: Short, descriptive (e.g., "Target Groceries", "Uber Rides")
- **Category**: The exact taxonomy key used in step 3
- **Patterns**: Merchant descriptor substrings that match this merchant. Use the merchant name and common variations. Query for actual descriptors if needed:
  ```sql
  SELECT DISTINCT rt.name
  FROM raw_transactions rt
  JOIN derived_transactions dt ON dt.raw_transaction_id = rt.raw_transaction_id
  WHERE dt.merchant_id = <merchant_id>
  LIMIT 10
  ```
- **Description**: One sentence explaining why this categorization is correct

### Step 5: Confirm to user

Report:
- How many transactions were recategorized
- The merchant name and new category
- That a rule was saved for future syncs

## Example Interaction

**User**: "Recategorize Jubilee Market as groceries"

**Agent**:
1. Queries merchants table for "Jubilee Market" -> finds merchant_id=42
2. Validates `food_and_dining.groceries` is a valid taxonomy key
3. Calls `recategorize_merchant(merchant_id=42, category_key="food_and_dining.groceries")`
4. Checks `memory/merchant-rules.md` for existing Jubilee Market rule
5. Appends or updates the rule with patterns from raw transaction descriptors
6. Confirms: "Recategorized 8 transactions for Jubilee Market to food_and_dining.groceries. Saved rule for future syncs."

## Important Notes

- **Always persist the rule**: The recategorization is only half the job. Without a memory rule, the next sync will re-categorize transactions using the LLM default.
- **Taxonomy validation is mandatory**: Never recategorize with an invalid key.
- **Verified transactions are immutable**: The MCP tool handles this; report the count accurately.
- **Patterns should be broad enough**: Include common descriptor variations so the rule catches future transactions from the same merchant.