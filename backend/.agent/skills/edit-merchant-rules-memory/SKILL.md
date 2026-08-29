---
name: edit-merchant-rules-memory
description: Edit the merchant categorization rules stored in the workspace's memory/merchant-rules.md so future syncs apply the same categorization automatically.
when_to_use: When the user asks to add, update, or remove a merchant rule, codify how a merchant should be categorized, or correct an incorrect categorization for a specific merchant.
---

# Skill: Edit Merchant Rules Memory

## Purpose

Edit the merchant categorization rules stored in `memory/merchant-rules.md` using shell-based file operations. This skill enables the agent to create, update, or remove merchant rules that map specific merchant descriptors to taxonomy categories.

## When to Use

Use this skill when:
- A user asks to add a new merchant rule
- A pattern of miscategorized merchants is identified
- Existing rules need updating or removal
- Multiple transactions from the same merchant need consistent categorization

## Required Inputs

When adding or updating a rule, gather these details:

1. **rule_name** (str): Short, descriptive name (e.g., "Costco Gas", "Amazon Fresh")
2. **category_key** (str): Exact taxonomy key (e.g., `transportation.fuel`, `food.groceries`)
3. **patterns** (list[str]): Fuzzy pattern clues for merchant descriptors
4. **description** (str): One short disambiguating sentence

## Validation Checklist

Before creating or updating a rule:

1. **Validate taxonomy key**: Ensure `category_key` exists in the taxonomy
   ```bash
   # Use the taxonomy tool or query to verify the key is valid
   ```

2. **Check for duplicates**: Review existing rules to avoid redundancy
   ```bash
   grep -i "<merchant_pattern>" memory/merchant-rules.md
   ```

3. **Format correctly**: Follow the exact rule format from memory/index.md

## Shell Editing Workflow

### Read Current Rules

```bash
cat memory/merchant-rules.md
```

### Append New Rule

```bash
cat >> memory/merchant-rules.md << 'EOF'

## Rule: <rule_name>
- **Category:** `<category_key>`
- **Patterns:** `<pattern_1>`, `<pattern_2>`, `<pattern_3>`
- **Description:** <description>
EOF
```

### Update Existing Rule (Replace)

First, identify the line numbers of the rule to update:

```bash
grep -n "## Rule: <rule_name>" memory/merchant-rules.md
```

Then use sed to replace specific lines or patterns:

```bash
sed -i '' 's/old_pattern/new_pattern/' memory/merchant-rules.md
```

Or manually edit specific sections by reading, modifying, and rewriting.

### Remove Rule

```bash
# Use sed to delete the rule block (requires start and end markers)
sed -i '' '/## Rule: <rule_name>/,/^$/d' memory/merchant-rules.md
```

## Validation After Edit

After any edit operation:

1. **Read back the file** to confirm changes:
   ```bash
   cat memory/merchant-rules.md
   ```

2. **Verify format**: Check that markdown structure is preserved

3. **Validate category keys**: Ensure all category keys are still valid

4. **Test categorization**: If possible, test with sample transactions

## Output Format

Always confirm the operation with:
- **Action taken**: What was added/updated/removed
- **Category key**: Which taxonomy category was used
- **Pattern count**: How many patterns are in the rule
- **Validation result**: Whether taxonomy key validation passed

## Example Interaction

**User**: "Can you add a rule for Target groceries?"

**Agent**:
1. Validates that `food.groceries` is a valid taxonomy key
2. Checks for existing Target rules
3. Appends new rule using the format:
   ```md
   ## Rule: Target Groceries
   - **Category:** `food.groceries`
   - **Patterns:** `TARGET`, `TGT`, `TARGET.COM`
   - **Description:** Target store grocery purchases
   ```
4. Confirms: "Added merchant rule 'Target Groceries' with category `food.groceries` and 3 patterns. The rule will auto-verify matching transactions."

## Important Notes

- **Taxonomy validation is mandatory**: Never add a rule with an invalid category key
- **Rules are case-insensitive**: Patterns match descriptors case-insensitively
- **Auto-verification**: Transactions matching rules are automatically marked as verified
- **Workspace files**: Memory files live in `memory/`; paths are relative to the workspace root, where the filesystem and `bash` tools are rooted