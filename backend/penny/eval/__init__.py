"""Categorizer backtest harness: replay a candidate against a frozen snapshot.

This was a daily job that replayed the categorizer over freshly synced
transactions and reported how often it agreed with the "legacy" categorization
already on the row. That number was meaningless: since the per-transaction
categorizer agent shipped, the sync-time categorizer and this replay are the
same code path, so the job compared the agent with itself and reported ~100%
agreement no matter what. Worse, the replay's snapshot carried each row's own
category, which the agent's history tools could read back.

What survives is the useful half — freeze a snapshot (``fixture``), replay a
categorizer over it (``replay``), and render the outcome (``report``) — which
answers the question the daily job could not: does a *changed* categorizer (new
prompt, model, or toolset) decide differently from the one in production?

Ground truth now comes from human labels recorded on the review page
(``penny.api.review``); accuracy is reported at ``/review/metrics``.
"""
