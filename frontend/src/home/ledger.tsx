import { useEffect, useRef, useState, type ReactNode } from "react";

/** Shared vocabulary for the reconciliation spread.
 *
 *  The landing page deliberately does NOT reuse @penny/ui's pill Button /
 *  EyebrowPill / Chip / Card primitives: those are the app's rounded chrome, and
 *  a rounded pill dropped into a ruled ledger reads as a foreign object. The
 *  palette and the type tokens are shared (one design system, P11); the
 *  component vocabulary on this surface is the ledger's own. */

/* ── Motion ───────────────────────────────────────────────────────────────── */

/** Adds `posted` once the element scrolls into view, which is what arms the
 *  post/rule-draw animations in theme.css. Fires once and disconnects: the
 *  ledger is written, not re-written. Content is styled visible by default, so
 *  a reader with JS disabled or reduced motion on still gets the finished page. */
export function usePosted<T extends HTMLElement>() {
  const ref = useRef<T>(null);
  const [posted, setPosted] = useState(false);

  useEffect(() => {
    const el = ref.current;
    if (!el || posted) return;
    const io = new IntersectionObserver(
      (records) => {
        if (records.some((r) => r.isIntersecting)) {
          setPosted(true);
          io.disconnect();
        }
      },
      // Wait until the line is meaningfully on screen rather than one pixel in.
      { rootMargin: "-10% 0px -15% 0px" },
    );
    io.observe(el);
    return () => io.disconnect();
  }, [posted]);

  return { ref, posted };
}

/* ── Type ─────────────────────────────────────────────────────────────────── */

/** Condensed caps: labels a column, a line item, or a figure. */
export function ColumnLabel({
  children,
  className = "",
}: {
  children: ReactNode;
  className?: string;
}) {
  return <span className={`column-label text-[0.68rem] ${className}`}>{children}</span>;
}

/** The sample-data marker. Penny is pre-launch and every figure here is
 *  authored, so each artifact carries this — it reads as a ledger annotation
 *  rather than a disclaimer bolted on. */
export function Illustrative({ className = "" }: { className?: string }) {
  return (
    <ColumnLabel className={`text-steel ${className}`}>
      <span aria-hidden="true">◇ </span>Illustrative figures
    </ColumnLabel>
  );
}

/* ── Rules ────────────────────────────────────────────────────────────────── */

/** A hairline ledger rule. `draw` opts it into the scroll-in draw animation. */
export function Rule({ className = "", draw = false }: { className?: string; draw?: boolean }) {
  return (
    <div
      className={`h-px w-full bg-navy/25 ${draw ? "rule-target" : ""} ${className}`}
      role="presentation"
    />
  );
}

/* ── The artifact panel ───────────────────────────────────────────────────── */

/** The panel every worked answer sits in: ruled, square-cornered, headed by a
 *  caption the way a figure in a report is. No ring, no shadow, no radius —
 *  it is a region of the document, not a floating card. */
export function Figure({
  caption,
  note,
  children,
}: {
  caption: string;
  note?: string;
  children: ReactNode;
}) {
  return (
    <figure className="m-0 border-t border-navy/30 bg-cream-soft/55">
      <figcaption className="flex flex-wrap items-baseline justify-between gap-x-6 gap-y-1 border-b border-navy/15 px-5 py-3">
        <ColumnLabel className="text-navy">{caption}</ColumnLabel>
        <Illustrative />
      </figcaption>
      <div className="px-5 py-6">{children}</div>
      {note ? (
        <p className="m-0 border-t border-navy/15 px-5 py-3 font-serif text-[0.8rem] leading-snug text-navy-700">
          {note}
        </p>
      ) : null}
    </figure>
  );
}

/* ── Dense content ────────────────────────────────────────────────────────── */

/** Data graphics and money columns have a legible minimum width. Below it the
 *  panel scrolls sideways rather than scaling axis labels and figures down into
 *  illegibility — a chart nobody can read is not a responsive chart. Only this
 *  container ever scrolls horizontally; the page itself never does. */
export function Scroller({ children, min }: { children: ReactNode; min: string }) {
  return (
    <div className="overflow-x-auto">
      <div className={min}>{children}</div>
    </div>
  );
}

/* ── Ledger table ─────────────────────────────────────────────────────────── */

/** Rows on the 32px rhythm the `ruled` background is cut to, with money right
 *  aligned and tabular so the columns actually line up. */
export function LedgerTable({
  head,
  children,
}: {
  head: readonly string[];
  children: ReactNode;
}) {
  return (
    <Scroller min="min-w-[30rem]">
      <table className="w-full border-collapse text-left">
        <thead>
          <tr>
            {head.map((h, i) => (
              <th
                key={h}
                scope="col"
                className={`border-b border-navy/25 pb-2 align-bottom ${i === 0 ? "text-left" : "text-right"}`}
              >
                <ColumnLabel className="text-navy-700">{h}</ColumnLabel>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>{children}</tbody>
      </table>
    </Scroller>
  );
}

/** One posted line. `total` gives it the accountant's double rule. */
export function LedgerRow({
  cells,
  total = false,
  muted = false,
}: {
  /** First cell is the label; the rest are right-aligned figures. */
  cells: readonly ReactNode[];
  total?: boolean;
  muted?: boolean;
}) {
  return (
    <tr className={total ? "text-navy" : muted ? "text-navy-700" : "text-ink"}>
      {cells.map((c, i) => (
        <td
          key={i}
          className={[
            "h-8 align-middle font-ui text-[0.82rem]",
            i === 0 ? "text-left pr-4" : "tnum text-right pl-4",
            total ? "rule-total border-navy pt-1 font-semibold" : "border-b border-navy/10",
          ].join(" ")}
        >
          {c}
        </td>
      ))}
    </tr>
  );
}
