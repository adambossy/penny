---
name: pdf
description: >
  Use this skill whenever the user wants to do anything with PDF files in the
  context of personal finance. This includes extracting transactions or balances
  from PDF bank/credit-card statements, reading PDF receipts or invoices,
  generating financial reports as PDFs, merging or splitting statement PDFs,
  and OCR-ing scanned statements to make them searchable. If the user mentions a
  .pdf file or asks to produce one, use this skill.
license: Derived from Anthropic skills/pdf (Proprietary). Original LICENSE.txt has complete terms.
---

# Skill: PDF Processing

## Purpose

Handle all PDF operations that arise in a personal-finance workflow:

- **Ingest**: extract transaction rows, balances, and dates from PDF bank or
  credit-card statements so they can be fed into the categorisation pipeline.
- **Generate**: produce downloadable PDF reports from the markdown or HTML
  reports that Penny generates (complement to the `render-report-html`
  skill).
- **Manipulate**: merge multiple monthly statements into one archive, split
  large PDFs, rotate or clean up scanned pages.
- **OCR**: make scanned (image-only) statements machine-readable so text
  extraction and transaction parsing can proceed.

## When to Use

Use this skill when:

- A user uploads or references a `.pdf` file containing bank, credit-card, or
  investment account statements.
- A user asks to extract transactions, balances, or dates from a PDF document.
- A user wants a spending or budget report saved as a PDF file.
- A user needs to merge, split, rotate, or otherwise manipulate statement PDFs.
- Text extraction returns blank results, indicating a scanned (image-only) PDF
  that requires OCR.

## Required Inputs

Identify these inputs before starting work:

1. **pdf_path** (str): Absolute or relative path to the input PDF file.
2. **operation** (str): One of `extract_text`, `extract_tables`,
   `extract_transactions`, `merge`, `split`, `rotate`, `generate`, `ocr`.
3. **output_path** (str, optional): Destination path for the result file or
   directory.
4. **page_range** (str, optional): Page range to process, e.g. `"1-3"` or
   `"all"` (default `"all"`).

For `merge` operations also gather:

5. **input_paths** (list[str]): Ordered list of PDF files to merge.

## Guardrails

- Never overwrite the user's original PDF without explicit confirmation; always
  write to a new output path.
- Do not retain or log sensitive financial data (account numbers, SSNs) beyond
  what is necessary to complete the task.
- If a PDF is password-protected, ask the user for the password; do not attempt
  to brute-force it.
- Prefer `pdfplumber` for text and table extraction from digital (non-scanned)
  statements; fall back to OCR only when `pdfplumber` returns blank pages.
- When outputting a generated PDF report, apply the same formatting rules
  defined in the `render-report-html` skill (colours, fonts, table layout)
  translated to the chosen PDF library.

## Dependencies

`pdfplumber` is a **bundled project dependency** — it is always available in the
penny virtualenv without any installation step. Import it directly:

```python
import pdfplumber
```

`pdftotext` (poppler-utils) must be installed at the OS level (`brew install poppler` on
macOS). Use it only as a fallback when a Python-based approach is insufficient.

## Python Libraries

### pdfplumber — Statement Text and Table Extraction (preferred)

```python
import pdfplumber

with pdfplumber.open("statement.pdf") as pdf:
    for page in pdf.pages:
        text = page.extract_text()       # full page text
        tables = page.extract_tables()   # list of row-lists
```

Use `pdfplumber` first; it preserves layout and handles tables well for most
digital bank statements.

### pypdf — Merge, Split, Rotate, Metadata

```python
from pypdf import PdfReader, PdfWriter

# Merge two monthly statements
writer = PdfWriter()
for path in ["jan.pdf", "feb.pdf"]:
    reader = PdfReader(path)
    for page in reader.pages:
        writer.add_page(page)

with open("jan-feb.pdf", "wb") as out:
    writer.write(out)

# Split: one file per page
reader = PdfReader("statement.pdf")
for idx, page in enumerate(reader.pages):
    w = PdfWriter()
    w.add_page(page)
    with open(f"page_{idx + 1}.pdf", "wb") as out:
        w.write(out)

# Rotate a sideways-scanned page
page = reader.pages[0]
page.rotate(90)
```

### reportlab — Generate PDF Reports

```python
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib import colors

doc = SimpleDocTemplate("spending-report.pdf", pagesize=letter)
styles = getSampleStyleSheet()
story: list[object] = []

story.append(Paragraph("Monthly Spending Report", styles["Title"]))
story.append(Spacer(1, 12))

data = [["Category", "Amount"], ["Food & Dining", "$1,240"], ["Transport", "$310"]]
table = Table(data)
table.setStyle(TableStyle([
    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2563eb")),
    ("TEXTCOLOR",  (0, 0), (-1, 0), colors.white),
    ("GRID",       (0, 0), (-1, -1), 0.5, colors.HexColor("#e5e7eb")),
    ("ALIGN",      (1, 1), (-1, -1), "RIGHT"),
]))
story.append(table)
doc.build(story)
```

**Important:** Never use Unicode subscript/superscript characters (₀–₉, ⁰–⁹)
in reportlab. Use `<sub>` / `<super>` XML tags inside `Paragraph` objects
instead.

### pytesseract + pdf2image — OCR for Scanned Statements

```python
import pytesseract
from pdf2image import convert_from_path

images = convert_from_path("scanned-statement.pdf")
text = ""
for idx, image in enumerate(images):
    text += f"\n--- Page {idx + 1} ---\n"
    text += pytesseract.image_to_string(image)
```

Only invoke OCR after confirming `pdfplumber` returns blank or near-blank text.

## Transaction Extraction Workflow

1. **Open and probe** — open the PDF with `pdfplumber` and check whether
   `page.extract_text()` returns meaningful content.

2. **Choose path**:
   - Digital PDF → proceed with `pdfplumber` table or text extraction.
   - Scanned PDF (blank text) → run OCR via `pytesseract` to obtain raw text.

3. **Locate transaction rows** — identify the header row (Date, Description,
   Amount) and parse each data row. Strip whitespace and normalise amounts
   (remove `$`, handle `(negatives)`).

4. **Map to `NormalizedTransaction`** — construct a `NormalizedTransaction`
   for each row using the appropriate bank adapter if one exists in
   `tools/ingest/adapters/`, or create a minimal ad-hoc mapping:

   ```python
   from penny.tools.ingest.adapters.base import NormalizedTransaction
   from datetime import date

   tx = NormalizedTransaction(
       date=date.fromisoformat("2025-01-15"),
       description="WHOLE FOODS #123",
       amount=42.50,
       external_id="pdf-sha256-abc123-row-7",
   )
   ```

5. **Hand off to categorisation** — pass the list of `NormalizedTransaction`
   objects to the `categorize` tool or agent pipeline.

## Report Generation Workflow

1. Obtain the markdown or HTML report string from the agent (e.g., output of
   the `render-report-html` skill).
2. Convert to a `reportlab` PDF following the style guide in the `render-report-html`
   skill (blue primary `#2563eb`, monospace amounts, summary-card structure).
3. Save to the requested `output_path`.
4. Confirm the file path and page count to the user.

## Validation Checklist

Before returning results:

1. **Extraction quality** — confirm extracted text is non-empty; if blank,
   escalate to OCR rather than silently returning nothing.
2. **Row count sanity** — parsed transaction count should be plausible for the
   date range (warn if zero or suspiciously low).
3. **Amount normalisation** — all amounts are floats; credits (inflows) are
   negative, debits (outflows) are positive (Penny convention).
4. **No source file overwritten** — output path differs from input path.
5. **Sensitive data** — do not echo full account numbers in confirmations;
   mask to last four digits.

## Output Format

Always confirm completed operations with a short summary:

```
Extracted 47 transactions from statement.pdf (pages 1–4, Jan 1–Jan 31 2025).
Amounts range from $3.50 to $1,240.00. Ready to pass to categorisation pipeline.
```

For generated PDFs:

```
Saved spending-report.pdf (3 pages, 48 KB) to reports/.
```

## Quick Reference

| Task | Best Library | Key Method |
|------|-------------|------------|
| Extract text (digital) | pdfplumber | `page.extract_text()` |
| Extract tables | pdfplumber | `page.extract_tables()` |
| Merge statements | pypdf | `writer.add_page(page)` |
| Split pages | pypdf | One `PdfWriter` per page |
| Rotate pages | pypdf | `page.rotate(90)` |
| Generate report PDF | reportlab | `SimpleDocTemplate.build(story)` |
| OCR scanned pages | pytesseract | `image_to_string(image)` |
| Decrypt protected PDF | pypdf | `reader.decrypt("password")` |

## Example Interactions

**Extracting transactions from a PDF statement:**
> User: "Here is my Chase statement for January (chase-jan-2025.pdf). Can you
> import the transactions?"

1. Open `chase-jan-2025.pdf` with `pdfplumber`; confirm text is present.
2. Locate the transaction table (header: Date / Description / Amount).
3. Parse each row into `NormalizedTransaction` objects.
4. Report: "Found 34 transactions, Jan 2–Jan 31. Passing to categorisation."

**Generating a PDF report:**
> User: "Export my monthly spending report as a PDF."

1. Obtain the rendered HTML/markdown from the agent.
2. Build a `reportlab` PDF matching the Penny report style.
3. Save to `reports/spending-YYYY-MM.pdf`.
4. Confirm: "Saved spending-2025-01.pdf (2 pages) to reports/."
