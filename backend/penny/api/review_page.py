"""Render the category review page and its scoreboard.

Two self-contained HTML pages — no build step, no CDN, no framework. The
review page is a keyboard-first labeling tool: every row's combobox starts on
the categorizer's own pick, so confirming is Enter and correcting is a few
letters. The labels it records are the golden dataset the scoreboard scores
the categorizer against.

Pure rendering over plain dicts (the facade's queue/scoreboard rows), so both
pages are testable without a server.
"""

from __future__ import annotations

import html
import json
from typing import Any

_STYLE = """
  body { font: 15px/1.55 -apple-system, system-ui, sans-serif; max-width: 1240px;
         margin: 2rem auto; padding: 0 1rem; color: #1a1a1a; }
  h1 { margin-bottom: .25rem; }
  .meta { color:#666; margin:.25rem 0 1.25rem; }
  a { color:#06c; }
  table { border-collapse: collapse; width: 100%; }
  th { text-align: left; font-size: .8rem; text-transform: uppercase;
       letter-spacing: .04em; color:#666; border-bottom:1px solid #ddd;
       padding:.4rem .5rem; }
  td { border-bottom:1px solid #f0f0f0; padding:.5rem; vertical-align: top; }
  tr.done { opacity:.45; }
  tr.active td { background:#f6f6ff; }
  .desc { font-weight:600; }
  .sub { color:#888; font-size:.85rem; word-break:break-word; }
  .amt { text-align:right; font-variant-numeric: tabular-nums; white-space:nowrap; }
  .plaid { font-family: ui-monospace, monospace; font-size:.75rem; color:#555; }
  /* Plaid's detailed labels are long underscore-joined strings that wrap into
     an unreadable stack; keep them to one line with the full value on hover. */
  .plaid .d { display:block; color:#888; white-space:nowrap; overflow:hidden;
              text-overflow:ellipsis; }
  .tag { display:inline-block; background:#eee; border-radius:6px;
         padding:.05rem .4rem; font-size:.75rem; color:#555; }
  .combo { position:relative; }
  /* Category keys are long (parent.child); monospace at .8rem fits the
     longest of them in the column without truncating the reviewer's view of
     what they are about to confirm. */
  .combo input { font-family: ui-monospace, monospace; font-size:.8rem;
                 width: 100%; padding:.35rem .4rem;
                 border:1px solid #ccc; border-radius:6px; box-sizing:border-box; }
  .combo input.changed { border-color:#c60; background:#fffaf5; }
  .combo input.saved { border-color:#0a7; background:#f4fff9; }
  .menu { position:absolute; z-index:10; left:0; right:0; top:100%;
          background:#fff; border:1px solid #ccc; border-radius:6px;
          box-shadow:0 6px 18px rgba(0,0,0,.12); max-height:16rem;
          overflow:auto; display:none; }
  .menu.open { display:block; }
  .opt { padding:.3rem .5rem; cursor:pointer; font-size:.9rem; }
  .opt .k { color:#888; font-size:.75rem; font-family: ui-monospace, monospace;
            word-break:break-all; }
  .opt.sel { background:#e8eeff; }
  .bar { position:sticky; top:0; background:#fff; padding:.6rem 0;
         border-bottom:1px solid #eee; margin-bottom:.5rem; z-index:20;
         display:flex; gap:1rem; align-items:center; }
  .count { font-weight:600; }
  .hint { color:#888; font-size:.85rem; }
  .err { color:#c33; }
  .empty { padding:3rem 1rem; text-align:center; color:#666; }
  .num { font-variant-numeric: tabular-nums; }
  .big { font-size:1.6rem; font-weight:600; }
  .cards { display:flex; gap:1rem; flex-wrap:wrap; margin:1rem 0 2rem; }
  .card { border:1px solid #ddd; border-radius:10px; padding:1rem 1.25rem;
          min-width:11rem; }
  .card .k { color:#666; font-size:.85rem; }
"""


def render_review_page(batch: dict[str, Any], categories: list[dict[str, str]]) -> str:
    """The labeling page: one sync day's batch, prefilled with the agent's picks."""
    rows = batch["rows"]
    day = batch.get("day")
    older_day, older_pending = batch.get("older_day"), batch.get("older_pending") or 0
    # Where "you're done with this batch" sends you next. Offering the next
    # older day (rather than the whole backlog) keeps every sitting bounded.
    next_link = (
        f'<a href="/review?day={older_day}">Review {older_day} '
        f"({older_pending} older waiting) &rarr;</a>"
        if older_day
        else '<a href="/review/metrics">See the scoreboard &rarr;</a>'
    )
    # The backlog is reachable but never the default view: someone who wants a
    # long catch-up session can ask for one, and nobody is handed one unasked.
    backlog_note = (
        f' &middot; <a href="/review?all=1">{batch["total_pending"]} pending in all</a>'
        if day and batch.get("total_pending", 0) > len(rows)
        else ""
    )
    if not rows:
        body = (
            '<div class="empty"><p class="big">Nothing to review</p>'
            f"<p>{next_link}</p></div>"
        )
    else:
        scope = f"synced {html.escape(day)}" if day else "all pending"
        body = f"""
<div class="bar">
  <span class="count"><span id="left">{len(rows)}</span> to review</span>
  <span class="hint">{scope} &middot; Enter confirms and moves on &middot;
    type to change &middot; &uarr;&darr; to pick &middot; Esc to skip</span>
  <span id="err" class="err"></span>
</div>
<div id="done" class="empty" hidden>
  <p class="big">Batch done</p><p>{next_link}</p>
</div>
<table><thead><tr>
  <th style="width:34%">Transaction</th><th class="amt">Amount</th>
  <th style="width:17%">Plaid says</th><th style="width:33%">Category</th>
</tr></thead><tbody id="rows"></tbody></table>"""

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Review categories</title>
<style>{_STYLE}</style></head><body>
<h1>Review categories</h1>
<p class="meta">Confirm or correct the categorizer, one transaction at a time.
  Your labels are the ground truth it gets scored against &middot;
  <a href="/review/metrics">scoreboard &rarr;</a>{backlog_note}</p>
{body}
<script>
const ROWS = {json.dumps(rows)};
const CATS = {json.dumps(categories)};
const esc = s => (s==null?'':String(s)).replace(/[&<>"]/g,
  c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}}[c]));

// Rows carry taxonomy KEYS; the reviewer reads and types NAMES, which are
// unique, half the length, and the words a person actually thinks in. The key
// stays the thing submitted, and the menu shows it under each name.
const BY_KEY = new Map(CATS.map(c => [c.key, c]));
const BY_NAME = new Map(CATS.map(c => [c.name.toLowerCase(), c]));
const nameOf = key => (BY_KEY.get(key) || {{}}).name || '';
const keyOf = text => {{
  const s = (text || '').trim().toLowerCase();
  const hit = BY_NAME.get(s) || BY_KEY.get(text.trim());
  return hit ? hit.key : null;
}};

// Match on key and name, preferring prefix hits so typing "gro" surfaces
// groceries before anything merely containing it.
function search(q) {{
  const s = q.trim().toLowerCase();
  if (!s) return CATS.slice(0, 50);
  const scored = [];
  for (const c of CATS) {{
    const key = c.key.toLowerCase(), name = c.name.toLowerCase();
    let rank = -1;
    if (key === s || name === s) rank = 0;
    else if (name.startsWith(s) || key.startsWith(s)) rank = 1;
    else if (key.includes(s) || name.includes(s)) rank = 2;
    else if (s.split(/\\s+/).every(w => (key + ' ' + name).includes(w))) rank = 3;
    if (rank >= 0) scored.push([rank, c]);
  }}
  scored.sort((a, b) => a[0] - b[0] || a[1].key.localeCompare(b[1].key));
  return scored.slice(0, 50).map(x => x[1]);
}}

function rowHtml(r, i) {{
  const plaid = r.plaid_category
    ? `<span class="plaid">${{esc(r.plaid_category.primary)}}` +
      `<span class="d" title="${{esc(r.plaid_category.detailed || '')}}">` +
      `${{esc(r.plaid_category.detailed || '')}}</span></span>`
    : '<span class="sub">\\u2014</span>';
  const raw = r.raw_name && r.raw_name !== r.merchant_descriptor
    ? `<div class="sub">${{esc(r.raw_name)}}</div>` : '';
  const fast = r.is_fast_path ? ' <span class="tag">fast path</span>' : '';
  const acct = r.account_name ? `<span class="sub">${{esc(r.account_name)}}</span>` : '';
  const amt = r.amount == null ? '\\u2014'
    : (r.amount < 0 ? '-' : '') + '$' + Math.abs(r.amount).toFixed(2);
  return `<tr id="r${{i}}" data-i="${{i}}">
    <td><div class="desc">${{esc(r.merchant_descriptor) || '(no descriptor)'}}${{fast}}</div>
        ${{raw}}<div class="sub">${{esc(r.posted_at)}} ${{acct}}</div></td>
    <td class="amt">${{amt}}</td>
    <td>${{plaid}}</td>
    <td><div class="combo">
      <input id="i${{i}}" value="${{esc(nameOf(r.agent_key))}}"
             title="${{esc(r.agent_key || '')}}"
             placeholder="uncategorized \\u2014 pick one" autocomplete="off">
      <div class="menu" id="m${{i}}"></div>
    </div></td></tr>`;
}}

const tbody = document.getElementById('rows');
if (tbody) tbody.innerHTML = ROWS.map(rowHtml).join('');

let active = -1;          // row index whose menu is open
let sel = 0;              // highlighted option within that menu
let opts = [];            // current option list
let remaining = ROWS.length;

function closeMenu() {{
  if (active >= 0) document.getElementById('m' + active).classList.remove('open');
  active = -1; opts = [];
}}

function openMenu(i) {{
  const input = document.getElementById('i' + i);
  // An untouched prefill lists everything rather than only itself, so the
  // menu is a chooser on arrow-down instead of a one-item dead end.
  opts = search(input.value === nameOf(ROWS[i].agent_key) ? '' : input.value);
  const menu = document.getElementById('m' + i);
  menu.innerHTML = opts.map((c, n) =>
    `<div class="opt${{n === sel ? ' sel' : ''}}" data-n="${{n}}">${{esc(c.name)}}
      <div class="k">${{esc(c.key)}}</div></div>`).join('') ||
    '<div class="opt">no match</div>';
  menu.classList.add('open');
  active = i;
  const cur = menu.querySelector('.opt.sel');
  if (cur) cur.scrollIntoView({{block: 'nearest'}});
}}

function focusRow(i) {{
  // Past the end with nothing left: the batch is finished, so say so and
  // offer the next one rather than leaving the reviewer on a grey table.
  if (i >= ROWS.length) {{
    if (remaining === 0) {{
      const done = document.getElementById('done');
      if (done) {{ done.hidden = false; done.scrollIntoView({{block: 'center'}}); }}
    }}
    return;
  }}
  if (i < 0) return;
  const input = document.getElementById('i' + i);
  if (!input || input.disabled) {{ focusRow(i + 1); return; }}
  document.querySelectorAll('tr.active').forEach(t => t.classList.remove('active'));
  document.getElementById('r' + i).classList.add('active');
  input.focus();
  input.select();
}}

async function save(i, key) {{
  const input = document.getElementById('i' + i);
  const errEl = document.getElementById('err');
  input.disabled = true;
  try {{
    const res = await fetch('/api/review/label', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{transaction_id: ROWS[i].transaction_id, category_key: key}}),
    }});
    if (!res.ok) throw new Error((await res.json()).detail || res.statusText);
    errEl.textContent = '';
    input.value = nameOf(key);
    input.title = key;
    input.classList.remove('changed');
    input.classList.add('saved');
    document.getElementById('r' + i).classList.add('done');
    remaining -= 1;
    document.getElementById('left').textContent = remaining;
    return true;
  }} catch (e) {{
    // Leave the row editable and in the queue: a failed write must not look
    // like a recorded label.
    errEl.textContent = 'Could not save: ' + e.message;
    input.disabled = false;
    input.classList.remove('saved');
    return false;
  }}
}}

function commit(i) {{
  const input = document.getElementById('i' + i);
  // An open menu means the user is choosing; take the highlighted option.
  // Otherwise resolve what is typed — a name, or a key pasted in.
  const key = (active === i && opts.length) ? opts[sel].key : keyOf(input.value);
  if (!key) {{
    document.getElementById('err').textContent =
      'Not a category: ' + (input.value.trim() || '(empty)');
    return;
  }}
  closeMenu();
  save(i, key).then(ok => {{ if (ok) focusRow(i + 1); }});
}}

document.addEventListener('input', e => {{
  if (!e.target.id.startsWith('i')) return;
  const i = Number(e.target.id.slice(1));
  e.target.classList.add('changed');
  sel = 0;
  openMenu(i);
}});

document.addEventListener('keydown', e => {{
  if (!e.target.id || !e.target.id.startsWith('i')) return;
  const i = Number(e.target.id.slice(1));
  if (e.key === 'Enter') {{ e.preventDefault(); commit(i); }}
  else if (e.key === 'ArrowDown') {{
    e.preventDefault();
    if (active !== i) {{ sel = 0; openMenu(i); }}
    else {{ sel = Math.min(sel + 1, opts.length - 1); openMenu(i); }}
  }} else if (e.key === 'ArrowUp') {{
    e.preventDefault();
    if (active === i) {{ sel = Math.max(sel - 1, 0); openMenu(i); }}
  }} else if (e.key === 'Escape') {{
    e.preventDefault(); closeMenu(); focusRow(i + 1);
  }}
}});

document.addEventListener('click', e => {{
  const opt = e.target.closest('.opt');
  if (opt && active >= 0 && opt.dataset.n !== undefined) {{
    sel = Number(opt.dataset.n);
    commit(active);
    return;
  }}
  if (!e.target.closest('.combo')) closeMenu();
}});

document.addEventListener('focusin', e => {{
  if (e.target.id && e.target.id.startsWith('i')) {{
    const i = Number(e.target.id.slice(1));
    document.querySelectorAll('tr.active').forEach(t => t.classList.remove('active'));
    document.getElementById('r' + i).classList.add('active');
  }}
}});

focusRow(0);
</script></body></html>
"""


def render_metrics_page(scoreboard: dict[str, Any], pending: int) -> str:
    """The scoreboard: how the categorizer does against the human labels."""
    items = scoreboard["items"]
    scored = [it for it in items if it["agent_key"] and it["human_key"]]
    exact = sum(1 for it in scored if it["agent_key"] == it["human_key"])
    parent = sum(
        1
        for it in scored
        if it["agent_key"].split(".")[0] == it["human_key"].split(".")[0]
    )
    unscored = len(items) - len(scored)
    pct = lambda n, d: f"{100 * n / d:.0f}%" if d else "—"  # noqa: E731

    misses = [it for it in scored if it["agent_key"] != it["human_key"]][:100]
    miss_rows = (
        "".join(
            f"<tr><td>{html.escape(it['merchant_descriptor'] or '')}</td>"
            f"<td class='plaid'>{html.escape(it['agent_key'])}</td>"
            f"<td class='plaid'>{html.escape(it['human_key'])}</td></tr>"
            for it in misses
        )
        or "<tr><td colspan='3' class='sub'>No corrections yet.</td></tr>"
    )

    # human label x Plaid label — the raw evidence for a future Plaid mapping,
    # deliberately not scored (mapping Plaid's taxonomy onto Penny's is a
    # judgment call this table exists to inform).
    pairs: dict[tuple[str, str], int] = {}
    for it in items:
        if it["human_key"] and it["plaid_category"]:
            key = (it["human_key"], it["plaid_category"]["primary"] or "—")
            pairs[key] = pairs.get(key, 0) + 1
    plaid_rows = (
        "".join(
            f"<tr><td class='plaid'>{html.escape(h)}</td>"
            f"<td class='plaid'>{html.escape(p)}</td><td class='num'>{n}</td></tr>"
            for (h, p), n in sorted(pairs.items(), key=lambda kv: -kv[1])[:100]
        )
        or "<tr><td colspan='3' class='sub'>No Plaid categories on labeled rows yet.</td></tr>"
    )

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Categorizer scoreboard</title>
<style>{_STYLE}</style></head><body>
<h1>Categorizer scoreboard</h1>
<p class="meta">Scored against {len(scored)} human-labeled transaction(s) &middot;
  {pending} still to review &middot; <a href="/review">review queue &rarr;</a></p>

<div class="cards">
  <div class="card"><div class="k">Exact match</div>
    <div class="big">{pct(exact, len(scored))}</div>
    <div class="k num">{exact} / {len(scored)}</div></div>
  <div class="card"><div class="k">Right top-level</div>
    <div class="big">{pct(parent, len(scored))}</div>
    <div class="k num">{parent} / {len(scored)}</div></div>
  <div class="card"><div class="k">Labeled</div>
    <div class="big num">{scoreboard["reviewed"]}</div>
    <div class="k">{unscored} not scored</div></div>
</div>

<p class="meta">"Not scored" are labeled rows the model never decided on its own —
  a fast-path reuse of an earlier label, or never categorized. Counting them
  would inflate accuracy with decisions the model didn't make.</p>

<h2>Corrections</h2>
<p class="meta">Where your label differs from what the categorizer chose.</p>
<table><thead><tr><th>Transaction</th><th>Categorizer said</th><th>You said</th></tr>
</thead><tbody>{miss_rows}</tbody></table>

<h2>Your labels vs. Plaid's</h2>
<p class="meta">Plaid's own category, verbatim and unmapped. This tally is the
  evidence for writing a Plaid&rarr;Penny mapping; until one exists, Plaid is
  reported, not scored.</p>
<table><thead><tr><th>Your label</th><th>Plaid primary</th><th class="num">n</th></tr>
</thead><tbody>{plaid_rows}</tbody></table>
</body></html>
"""
