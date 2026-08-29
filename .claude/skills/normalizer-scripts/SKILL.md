---
name: normalizer-scripts
description: How to run the merchant-normalizer dev/validation scripts in backend/scripts — descriptor sampling, the normalizer dry-run + review page, the eval suite, and the read-only Venmo Plaid inspection. Use when asked to sample descriptors, validate/eval the normalizer, regenerate the review page, or inspect raw Plaid fields.
when_to_use: When working on the LLM merchant normalizer (penny/normalizer) and you need to run its offline tooling — collect the descriptor corpus, score extraction against held-out fixtures, produce the human-review HTML, or check what raw fields Plaid returns. NOT for the Penny agent at runtime; these are developer/CI scripts.
---

# Skill: Run the merchant-normalizer scripts

Four offline scripts under `backend/scripts/` support the Tier 2 LLM merchant
normalizer. None of them write to the database. Run everything **from
`backend/`** with `uv`.

## Prerequisites

- **A TEST-branch `DATABASE_URL`** (only for the scripts that read the DB —
  `sample_descriptors` and `inspect_venmo_plaid`). Never point at production
  (see AGENTS.md). Generate a fresh Neon test branch + `.env.test` with:
  ```bash
  backend/scripts/new_test_branch.sh        # needs an authed neonctl + jq
  set -a && source .env.test && set +a      # exports DATABASE_URL
  ```
- **An LLM API key** for the scripts that call the model (`normalize_eval`,
  `normalize_dryrun`). The normalizer resolves its model in this order:
  `PENNY_NORMALIZER_MODEL` → `PENNY_CATEGORIZER_MODEL` → `PENNY_AGENT_MODEL` →
  default. The provider is inferred from the model name, so set the matching
  key: `OPENAI_API_KEY` for `gpt-*`, `GOOGLE_API_KEY` for `gemini-*`.
  ```bash
  export PENNY_NORMALIZER_MODEL=gemini-2.5-flash
  export GOOGLE_API_KEY=...                 # or OPENAI_API_KEY for a gpt model
  ```
  Do **not** `source` the root `.env` wholesale — it carries a production
  `DATABASE_URL`/`PLAID_*`. Export only the LLM key you need.

## Scripts

### 1. `sample_descriptors.py` — collect the descriptor corpus (read-only DB)

First step of any normalizer work: survey the real descriptors before touching
rules. Reads `plaid_transactions.merchant_descriptor`.

```bash
set -a && source .env.test && set +a
uv run python scripts/sample_descriptors.py
```

Writes (gitignored, real merchant strings) to `.descriptor-corpus/`:
`descriptors.json` (every distinct descriptor + count + coarse channel guess),
`report.md` (by-channel summary), `other_tokens.txt` (leading-token frequencies
for unbucketed rows — surfaces unanticipated vendors).

### 2. `normalize_eval.py` — score extraction against held-out fixtures (LLM)

Runs the normalizer over the held-out cases in
`penny/normalizer/eval_fixtures.yaml` and prints channel / normalized_name /
counterparty accuracy plus failing cases. Use it to decide whether a
`rules.yaml` edit helped. No DB needed.

```bash
export PENNY_NORMALIZER_MODEL=gemini-2.5-flash GOOGLE_API_KEY=...
uv run python scripts/normalize_eval.py
```

Exit code is non-zero unless every case passes (so it can gate CI). After
editing `rules.yaml`, re-run; the change is reflected immediately (no cache).

### 3. `normalize_dryrun.py` — produce the human-review page (LLM)

The validation step that replaces a backfill: runs the normalizer over the
sampled corpus **without writing the DB**, groups the proposed merchant
identities, and emits a review HTML page. Requires `descriptors.json` from
step 1.

```bash
export PENNY_NORMALIZER_MODEL=gemini-2.5-flash GOOGLE_API_KEY=...
uv run python scripts/normalize_dryrun.py --only-wrappers   # [--limit N]
```

- `--only-wrappers` restricts the LLM pass to non-`direct` channels (cost
  control; direct merchants are deterministic anyway). The exclusion is logged.
- `--limit N` caps the descriptor count.

Writes `.descriptor-corpus/review.html` (open in a browser; checkbox per
proposed identity, localStorage-persisted, "Export results") and
`proposals.json`. (`scripts/review.py` is the HTML builder imported by this
script — not run directly.)

### 4. `inspect_venmo_plaid.py` — inspect raw Plaid fields (read-only, hits Plaid)

One-off diagnostic: pulls transactions straight from Plaid for every stored
item, keeps the Venmo ones, and dumps the raw fields (`name`, `merchant_name`,
`original_description`, `payment_meta`, `counterparties`) to verify where the
counterparty actually lives. Read-only; **calls the Plaid API** (production env
per `.env`).

```bash
set -a && source .env.test && set +a                          # DATABASE_URL -> test branch (access tokens)
eval "$(grep -E '^PLAID_' .env | sed 's/^/export /')"   # PLAID_* creds
uv run python scripts/inspect_venmo_plaid.py
```

## Typical workflow

1. `new_test_branch.sh` → `source .env.test` (get a test DB).
2. `sample_descriptors.py` (build the corpus).
3. Edit `penny/normalizer/rules.yaml`; run `normalize_eval.py` until green.
4. `normalize_dryrun.py --only-wrappers`; open `review.html` and check the
   proposed merges.

## Notes

- All four are read-only w.r.t. the Penny DB — safe to run repeatedly.
- `.descriptor-corpus/` is gitignored (contains real merchant strings); never commit it.
- The LLM scripts cost tokens per distinct descriptor (no result cache); use
  `--only-wrappers` / `--limit` to bound a dry-run.
- The merchant normalizer itself is wired into the sync path; these scripts are
  for validating its rules offline, not for production runs.
