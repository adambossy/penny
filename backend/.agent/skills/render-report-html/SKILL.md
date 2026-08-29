---
name: render-report-html
description: Convert a markdown spending report into a complete, styled HTML document suitable for email or web delivery.
when_to_use: When the user asks for an HTML version of a report, wants to email a report, or asks for a presentation-ready spending summary.
---

# Render Report as HTML

This skill describes how to convert a markdown spending report into a well-structured, styled HTML document.

Take the markdown report you have generated and convert it into a complete HTML document following the CSS and structural guidelines below.

## CSS Styles

Include this CSS in a `<style>` tag in the `<head>`:

```css
:root {
    --primary: #2563eb;
    --primary-dark: #1d4ed8;
    --success: #16a34a;
    --warning: #ca8a04;
    --danger: #dc2626;
    --gray-50: #f9fafb;
    --gray-100: #f3f4f6;
    --gray-200: #e5e7eb;
    --gray-300: #d1d5db;
    --gray-600: #4b5563;
    --gray-700: #374151;
    --gray-800: #1f2937;
    --gray-900: #111827;
}

* { box-sizing: border-box; margin: 0; padding: 0; }

body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
    line-height: 1.6;
    color: var(--gray-800);
    background: var(--gray-50);
    padding: 2rem;
}

.container {
    max-width: 900px;
    margin: 0 auto;
    background: white;
    border-radius: 12px;
    box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1), 0 2px 4px -1px rgba(0, 0, 0, 0.06);
    overflow: hidden;
}

header {
    background: linear-gradient(135deg, var(--primary) 0%, var(--primary-dark) 100%);
    color: white;
    padding: 2rem;
}

header h1 { font-size: 1.75rem; font-weight: 700; margin-bottom: 0.5rem; }
header .subtitle { opacity: 0.9; font-size: 0.95rem; }

.content { padding: 2rem; }

h2 {
    font-size: 1.25rem;
    font-weight: 600;
    color: var(--gray-900);
    margin: 2rem 0 1rem 0;
    padding-bottom: 0.5rem;
    border-bottom: 2px solid var(--primary);
}
h2:first-child { margin-top: 0; }

h3 { font-size: 1.1rem; font-weight: 600; color: var(--gray-700); margin: 1.5rem 0 0.75rem 0; }

p { margin-bottom: 1rem; color: var(--gray-700); }

.summary-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 1rem;
    margin: 1.5rem 0;
}

.summary-card {
    background: var(--gray-50);
    border-radius: 8px;
    padding: 1rem;
    border-left: 4px solid var(--primary);
}
.summary-card.danger { border-left-color: var(--danger); }
.summary-card.warning { border-left-color: var(--warning); }
.summary-card.success { border-left-color: var(--success); }

.summary-card .label {
    font-size: 0.8rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--gray-600);
    margin-bottom: 0.25rem;
}
.summary-card .value { font-size: 1.5rem; font-weight: 700; color: var(--gray-900); }
.summary-card .change { font-size: 0.85rem; margin-top: 0.25rem; }

.change.positive { color: var(--danger); }
.change.negative { color: var(--success); }

table { width: 100%; border-collapse: collapse; margin: 1rem 0; font-size: 0.9rem; }
th, td { padding: 0.75rem; text-align: left; border-bottom: 1px solid var(--gray-200); }
th {
    background: var(--gray-100);
    font-weight: 600;
    color: var(--gray-700);
    font-size: 0.8rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
}
tr:hover { background: var(--gray-50); }
td.amount { font-family: 'SF Mono', Monaco, 'Courier New', monospace; text-align: right; }

.flag {
    display: inline-block;
    padding: 0.2rem 0.5rem;
    border-radius: 4px;
    font-size: 0.75rem;
    font-weight: 500;
}
.flag.danger { background: #fef2f2; color: var(--danger); }
.flag.warning { background: #fefce8; color: var(--warning); }
.flag.success { background: #f0fdf4; color: var(--success); }

.alert { padding: 1rem; border-radius: 8px; margin: 1rem 0; }
.alert.danger { background: #fef2f2; border: 1px solid #fecaca; color: #991b1b; }
.alert.warning { background: #fefce8; border: 1px solid #fef08a; color: #854d0e; }
.alert.success { background: #f0fdf4; border: 1px solid #bbf7d0; color: #166534; }
.alert strong { display: block; margin-bottom: 0.25rem; }

ul, ol { margin: 1rem 0; padding-left: 1.5rem; }
li { margin-bottom: 0.5rem; color: var(--gray-700); }

.recommendation {
    background: var(--gray-50);
    border-radius: 8px;
    padding: 1rem;
    margin: 1rem 0;
}
.recommendation h4 { font-size: 0.9rem; font-weight: 600; color: var(--gray-800); margin-bottom: 0.5rem; }
.recommendation p { font-size: 0.9rem; margin-bottom: 0; }

.trend-up { color: var(--danger); }
.trend-down { color: var(--success); }
.trend-stable { color: var(--gray-600); }

footer {
    background: var(--gray-100);
    padding: 1rem 2rem;
    font-size: 0.85rem;
    color: var(--gray-600);
    text-align: center;
}

@media (max-width: 640px) {
    body { padding: 1rem; }
    .content { padding: 1rem; }
    table { font-size: 0.8rem; }
    th, td { padding: 0.5rem; }
}
```

## HTML Structure Requirements

1. Use `<!DOCTYPE html>` and proper HTML5 structure
2. Wrap everything in a `<div class="container">`
3. Create a `<header>` with `<h1>` for the main title and `<div class="subtitle">` for the date/period
4. Wrap the main content in `<div class="content">`
5. Use `<h2>` for main sections (## in markdown) and `<h3>` for subsections (### in markdown)
6. Convert markdown tables to HTML `<table>` elements with proper `<thead>` and `<tbody>`
7. For monetary amounts in tables, use `class="amount"` on the `<td>`
8. Use summary cards (`.summary-grid` and `.summary-card`) for key metrics in the executive summary
9. Use `.alert` divs with `.danger`/`.warning`/`.success` classes for important callouts
10. Use `.recommendation` divs for actionable items
11. Use `.flag` spans with `.danger`/`.warning`/`.success` for status indicators in tables
12. Use `.trend-up`/`.trend-down` classes for percentage changes
13. Add a `<footer>` at the end with "Generated by Penny" and the current date
14. Make sure all HTML is valid and properly escaped

## Output

Output ONLY the complete HTML document, no explanations or markdown code blocks.