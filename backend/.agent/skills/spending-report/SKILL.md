---
name: spending-report
description: Generate a comprehensive spending report or summary over a period — daily, weekly, monthly, or annual — from transaction data, with comparisons to the prior period, optional charts, optional HTML rendering, and optional email delivery.
when_to_use: When the user asks for a spending report or summary scoped to a time period — daily, weekly, monthly, or annual. Triggers on requests like "how did I do this week", "give me my weekly/monthly report", "year in review", "what did I spend yesterday", or "summarize my spending for May".
---

# Skill: Spending Report

One report procedure, parameterized by **PERIOD** (`daily` | `weekly` | `monthly` |
`annual`). The pipeline is the same for every period; only the date window, the
comparison baseline, the trend granularity, and a few section details differ.
The per-period deltas are tabulated below so the flow stays DRY and the four
periods cannot drift apart.

## Step 1 — Determine the period and date window

1. **Pick PERIOD** from the user's request: daily, weekly, monthly, or annual.
   If it is genuinely ambiguous ("give me a report"), ask which period; otherwise
   infer it (e.g. "year in review" → annual, "yesterday" → daily).
2. **Resolve the exact date window** using the system prompt's Runtime Context
   definitions — do NOT use your training-data sense of date. "This week" is the
   ISO 8601 Monday–Sunday week containing today (`{{WEEK_START}}`–`{{WEEK_END}}`),
   "this month" is the current calendar month, "this year" is the current calendar
   year. The user may name an explicit period instead ("for May", "for 2025"); use
   that and an `as_of_date` if given. **Restate the resolved window dates in your
   reply** so the user can confirm the scope.

See the per-period table for the precise current window, comparison baseline, and
trend granularity.

## Step 2 — Query the data (`run_sql` against `derived_transactions`)

All aggregations run against `derived_transactions` (the PRIMARY spending table —
never `plaid_transactions`), JOINing `categories` with `WHERE c.deprecated_at IS
NULL`. Apply the system prompt's **Default Query Filters** exactly: positive
amounts only (`amount_cents > 0`), exclude `income.*`,
`banking_movements_transfers_refunds_and_fees.*`, and `savings_and_investments.*`
categories, exclude `reporting_mode = 'DEFAULT_EXCLUDE'`, and pair refunds against
their originals. **Enumerate the active filters in your methodology** so a mismatch
is spottable. Maintain strict consistency: reuse identical filters and date math
across the current-period, comparison, and trend queries so the numbers reconcile.

Batch the queries — do not issue one query per category. Pull, in as few queries as
practical:
- Current-period total and per-category breakdown.
- Comparison-baseline total and per-category breakdown (see table).
- Trend series at the period's granularity (see table).
- The transaction rows needed for the "Unusual / Major Expenses" scan.

## Step 3 — Budget context (optional)

Check whether `memory/budget.md` exists (filesystem/`bash` tools). It stores **monthly** figures. If present, derive the period
target per the table (weekly ÷ 4.33, monthly ×1, annual ×12; daily ÷ 30.4 if a
daily target is useful) and include budget-vs-actual columns. If absent, omit all
budget comparisons.

## Step 4 — Analyze and compare to the prior period

Compute the deltas the sections need: current vs comparison baseline (amount and
% change), per-category over/under, and the trend direction (rising / falling /
stable) at the period's granularity. Flag transactions meeting the **"Unusual"**
heuristics, with the baseline scaled to the period:
- Any single non-recurring transaction over $500 (rent, car payment, etc. excluded).
- Any first-time merchant with charges over $100.
- Any category 50%+ above its baseline average (the baseline is the period's trend
  average — 4-week for weekly, 3-month for monthly, prior-year for annual, etc.).
- Duplicate charges from the same merchant on the same day.

## Step 5 — Optional chart

If a visualization would help (and especially when asked for a presentation-ready
or emailed report), call `generate_chart` for the category breakdown or the trend
series. Skip for a quick daily report unless requested.

## Step 6 — Compose the markdown report

Always include the full markdown report in your final response. Use tables for
numerical comparisons, real dollar amounts, percentages, and specific merchant
names. Be direct and honest about overspending — actionable insights, not
sugar-coating. Lightly judge clear overspending and sparingly credit genuine
frugality per Penny's tone.

**Canonical sections (weekly / monthly / annual):**
1. **Executive Summary** — total this period vs comparison baseline (amount and %);
   top 3 concerns; top 3 positive trends; if budget data exists, on-track-to-target?
2. **Category Spending Breakdown** — per major category: amount this period; budget
   target vs actual (if available); comparison to baseline; flag 50%+-over categories.
3. **Spending Trends** — per category: overspending / underspending / stable, at the
   period's granularity (see table), with specific numbers and percentages.
4. **Unusual or Major Expenses** — each flagged transaction (date, merchant, amount,
   category), why it's flagged, and whether it's one-time or worth watching.
5. **Predicted Upcoming Expenses** — recurring subscriptions/bills due in the
   period's lookahead, plus seasonal/pattern-based expectations.
6. **Actionable Recommendations** — Cut back on / Maintain / OK to splurge / Watch
   out for. Annual adds **Year-ahead planning**.

**Daily uses a leaner, markdown-only section list** (see the daily column).

## Step 7 — Optional HTML render and delivery

For weekly / monthly / annual reports (and any time the user asks for an HTML,
presentation-ready, or emailed report): convert the markdown to a styled HTML
document by following the **`render-report-html`** skill (load it via the `Skill`
tool — do not duplicate its CSS here). Write the result into the workspace
`reports/` directory as `report-<period>-latest.html` using the filesystem/`bash`
tools.

If the user asked for an emailed or shareable report, deliver it with
`upload_artifact_to_r2` and `send_email_report`.

Daily reports are markdown-only by default — skip HTML and delivery unless asked.

## Per-period deltas

| Aspect | daily | weekly | monthly | annual |
|---|---|---|---|---|
| Current window | yesterday (plus rolling 7/14-day context) | this ISO week (Mon–Sun) | current calendar month | current calendar year |
| Comparison baseline | same-length window one month earlier (7-day vs 7-day, 14-day vs 14-day) | last week | last month | last year (YoY) |
| Trend granularity | rolling 7 & 14-day totals; per-category 7/14-day | 4, 8, and 12 weeks | 3, 6, and 12 months | YoY + quarterly (Q1–Q4) seasonality |
| "Unusual" baseline | 7/14-day rolling average | 4-week average | 3-month average | prior-year average |
| Budget target from monthly | ÷ 30.4 (if useful) | ÷ 4.33 | ×1 | ×12 |
| Upcoming lookahead | next ~7 days + 15-day-forward projection | next 7 days | next 30 days | next year (annual charges) |
| Sections | leaner daily set (below) | canonical 1–6 | canonical 1–6 | canonical 1–6 + Year-ahead planning |
| HTML / delivery | markdown only by default | render + write `report-weekly-latest.html` | render + write `report-monthly-latest.html` | render + write `report-annual-latest.html` |

### Daily-specific section list (use this exact order, markdown only)

1. **Yesterday at a Glance** — total spent yesterday (one dollar amount) and
   transaction count.
2. **Yesterday Transactions + Category Context** — one row per yesterday
   transaction: date, merchant, amount, category, and the running month-to-date
   spend for that category as of now. Use a table.
3. **Actionable Recommendations** — 3–5 short, concrete actions from the data.
4. **Rolling Totals (7 and 14 days)** — total spending for the last 7 and last 14 days.
5. **Prior-Month Comparison Windows** — last 7 days vs the equivalent 7-day window
   one month earlier; last 14 days vs the equivalent 14-day window one month earlier.
6. **Sliding Monthly Projection (15 days back to 15 days forward)** — a 30-day
   centered window: historical portion = 15 days prior through today; projection
   portion = tomorrow through 15 days ahead. Give the projected total for the full
   30-day window and briefly explain the projection basis.
7. **Category-Level 7/14-Day + Sliding Projection** — per active category: last
   7-day spend, last 14-day spend, and projected spend for the same 30-day centered
   window. Use a table.

Keep the daily report direct and concise, with exact dollar amounts.
