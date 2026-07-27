# Design

The one visual system for every Penny surface. It is light-only by
requirement (`REQUIREMENTS.txt` P11): there is no dark variant, the OS
`prefers-color-scheme: dark` preference is never followed, and no toggle
exists. All contrast work happens inside the light cream system.

Source of truth for tokens is `frontend/packages/ui/src/theme.css`; `tokens.ts`
mirrors it for JS consumers and `tokens.drift.test.ts` fails if the two drift.
Change values there, never in a component.

## Ground and palette

| Token | Value | Role |
|---|---|---|
| `paper` | `#FAF4E7` | The ground. Every surface starts here. |
| `cream` / `cream-soft` | `#ECE0C0` / `#F3E9CF` | Quiet fills — panel grounds, inset regions. |
| `ink` | `#1C3E3A` | Body text, and the dark **field** (see below). |
| `navy` / `navy-700` | `#1E4846` / `#2A625D` | Rules, headings, secondary text. |
| `steel` | `#3C7A72` | Tertiary text and placeholders — the lightest text allowed on paper. |
| `orange` (gold) | `#D69E3D` | **The difference, and nothing else.** |

**Colour strategy is Committed, not accent-scattered.** `ink` is used as a
field that owns whole regions — the marketing gutter, the colophon — not only
as text colour. A light system is not a pale one.

### The gold rule

Gold means *the difference Penny found*: a delta, a gap, a breach, a drift. It
is never decoration, never a generic accent, and never a brand flourish.

It also has a hard technical constraint that enforces the rule:

- gold on `paper` measures **2.17:1** — it fails even large-text contrast, so
  gold text on the ground is forbidden;
- gold on `ink` measures **4.90:1** — it passes.

So **gold text only ever appears on an ink field.** This is why the marketing
spread has a continuous ink gutter at all: the constraint chose the
composition. Where gold appears as a *shape* on the cream ground (a bar
segment, a shaded region), it must carry a `navy` outline and a text label, so
meaning never rests on the colour alone.

Measured against `paper`: `ink` 10.6:1, `navy` 9.3:1, `navy-700` 6.4:1,
`steel` 4.6:1. Against `ink`: `cream` 8.9:1, `cream/75` 5.7:1, `cream/70`
5.2:1 — treat `cream/70` as the floor for small text on ink.

## Type

Three families, each with one job. Two of them are single variable files.

- **`font-display` — Archivo** (variable, `wght` 100–900 **and `wdth` 62–125**).
  The document voice. Used **only at its width extremes**, never near normal
  width, so it can never muddle against Work Sans:
  - `statement` — extended (`118%`), uppercase. Heads a spread; names a total.
  - `column-label` — condensed (`64%`), uppercase, tracked. Labels a column or
    a line item.
  There is no third register. If a size needs Archivo at normal width, it
  wanted Work Sans.
- **`font-serif` — Literata** (variable). The reading and annotation voice:
  headlines, marginalia, notes, the wordmark.
- **`font-ui` — Work Sans**. Interface text, body copy, and figures in tables.

**Money is always tabular.** Any figure that sits in a column takes `tnum`
(`tabular-nums lining-nums`), so columns align and Literata cannot drop to
old-style figures.

Ceilings: display never exceeds 6rem; body measure stays 65–75ch.

> Fraunces and Cormorant Garamond were the previous display pair and are gone.
> Cream ground + high-contrast serif display + a gold accent is the single most
> common machine-generated aesthetic, and both faces sit on that default list.
> Do not reintroduce them, or reach for Playfair, Space Grotesk, DM Serif,
> Instrument Sans, or Inter-as-display in their place.

## Structure is the ornament

On **marketing surfaces** the only lines on the page are ones that carry
meaning: hairline rules that separate ledger lines, and `rule-total`, the
accountant's double rule under a summed figure. Consequently, on those
surfaces:

- no cards, and never a nested card;
- no pill/rounded chrome, no drop shadows, no glass or backdrop blur;
- no applied texture (no dot grain, no noise) — ruling supplies the texture;
- no decorative blobs, gradient text, or coloured slabs;
- no tracked uppercase eyebrow over every section. Section numbering is allowed
  **only** where the sequence carries information — in the spread it does,
  because the gutter posts lines in order and the close counts them.

This is scoped deliberately. **App surfaces keep their own chrome**: the
`@penny/ui` pill `Button`, `Chip`, `Card`, and agent-ui's rounded chat
furniture are correct there and must not be flattened to match marketing. The
two share the palette, the type system, and the gold rule — not the component
vocabulary.

## Motion

One authored idea, matching what the world does in life: a figure is **posted**
to a line, and a rule is **drawn** across it. `--animate-post` and
`rule-draw`, armed by an `IntersectionObserver` that adds `.posted` once and
disconnects.

Content is styled **visible by default**; only the `.posted` class moves
anything. A reader with JS disabled, or with `prefers-reduced-motion: reduce`,
gets the finished page rather than a blank one. Do not add per-section
entrance effects on top of this.

## Responsive

Dense content — data graphics and money columns — has a legible minimum width
and scrolls sideways inside its own container (`Scroller`) rather than scaling
labels into illegibility. **The page itself must never scroll horizontally**;
only that container may.

## Accessibility floor

Body and placeholder text ≥4.5:1, large text ≥3:1. Colour is never the sole
carrier of meaning. Every chart has a `role="img"` and an `aria-label` stating
its finding in words. Interactive elements carry a visible
`focus-visible` outline.
