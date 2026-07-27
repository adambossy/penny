import { Link } from "react-router";
import { artifacts } from "./artifacts";
import { ColumnLabel, usePosted } from "./ledger";
import { home, type Entry } from "./copy";

/** The reconciliation spread: the page's one structural idea.
 *
 *  Three columns, in the same order on every single line — claim, difference,
 *  evidence. The alternating left/right feature row this page used to ship is
 *  exactly what makes a marketing page read as a template, so the spread never
 *  flips. What varies is density and the shape of the evidence, not the layout.
 *
 *  The centre column is a continuous ink field running the height of the page:
 *  every cell is full-bleed and the hairline rules stop at its edges, so
 *  consecutive lines fuse into one unbroken gutter. It has to be ink — gold is
 *  only 1.97:1 on the cream ground but 4.90:1 on ink, so the one place the
 *  difference can be *set in gold* is against this field. The constraint chose
 *  the composition. */

/** Single home for the column geometry; the headings row and every entry share
 *  it, so the gutter cannot drift out of alignment with the deltas above it. */
const SPREAD = "lg:grid lg:grid-cols-[minmax(0,1fr)_10rem_minmax(0,1.12fr)]";

/* ── the column headings ─────────────────────────────────────────────────── */

/** A legend, not a stat band: it names the three columns and teaches the
 *  grammar every line below obeys. It states no metric, because Penny is
 *  pre-launch and there is no usage number that would be true. */
export function ColumnHeadings() {
  const { left, right, delta } = home.headings;
  return (
    <section aria-label="How to read this page" className="border-t-2 border-navy">
      <div className={`mx-auto max-w-[88rem] ${SPREAD}`}>
        <Heading label={left.label} note={left.note} />
        <div className="flex flex-col justify-center bg-ink px-4 py-5 text-center lg:py-0">
          <ColumnLabel className="text-orange">
            <span aria-hidden="true">Δ </span>
            {delta.label}
          </ColumnLabel>
          <p className="m-0 mt-1.5 font-serif text-[0.76rem] leading-snug text-cream/75">
            {delta.note}
          </p>
        </div>
        <Heading label={right.label} note={right.note} />
      </div>
    </section>
  );
}

function Heading({ label, note }: { label: string; note: string }) {
  return (
    <div className="px-5 py-5 sm:px-8">
      <ColumnLabel className="text-navy">{label}</ColumnLabel>
      <p className="m-0 mt-1.5 max-w-[38ch] font-serif text-[0.82rem] leading-snug text-navy-700">
        {note}
      </p>
    </div>
  );
}

/* ── one reconciled line ─────────────────────────────────────────────────── */

export function SpreadEntry({ entry }: { entry: Entry }) {
  const { ref, posted } = usePosted<HTMLElement>();

  return (
    <section
      id={entry.id}
      ref={ref}
      className={`scroll-mt-24 ${posted ? "posted" : ""}`}
      aria-labelledby={`${entry.id}-title`}
    >
      <div className={`mx-auto max-w-[88rem] ${SPREAD}`}>
        {/* claim — centred against the evidence, which is always the taller cell */}
        <div className="flex flex-col justify-center border-t border-navy/30 px-5 py-12 sm:px-8 lg:py-20">
          <div className="post-target">
            <p className="m-0 flex items-baseline gap-3">
              <span className="statement text-[0.95rem] text-navy">{entry.n}</span>
              <ColumnLabel className="text-navy-700">{entry.line}</ColumnLabel>
            </p>
            <h2
              id={`${entry.id}-title`}
              className="mt-5 max-w-[19ch] font-serif text-[2.15rem] font-medium leading-[1.12] tracking-[-0.015em] text-ink sm:text-[2.6rem]"
            >
              {entry.title}
            </h2>
            <p className="mt-5 max-w-[46ch] font-ui text-[1.02rem] leading-[1.65] text-navy-700">
              {entry.body}
            </p>
            <Link
              to="/sign-up"
              className="group mt-8 inline-flex items-baseline gap-3 border-b-2 border-navy pb-1 font-ui text-[0.95rem] font-medium text-ink no-underline transition-colors hover:border-orange hover:text-navy-700 focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-navy"
            >
              <span className="text-steel" aria-hidden="true">
                &ldquo;
              </span>
              {entry.ask}
              <span
                aria-hidden="true"
                className="transition-transform duration-300 group-hover:translate-x-1"
              >
                &rarr;
              </span>
            </Link>
          </div>
        </div>

        {/* difference — the gutter */}
        <div className="flex flex-row items-center justify-center gap-4 bg-ink px-5 py-6 text-center lg:flex-col lg:justify-start lg:gap-0 lg:px-3 lg:py-20">
          <span className="column-label shrink-0 text-[0.62rem] text-cream/70 lg:mb-4">
            {entry.n}
          </span>
          <span className="post-target statement tnum text-balance text-[1.45rem] leading-none text-orange lg:text-[1.6rem]">
            {entry.delta.value}
          </span>
          <p className="post-target m-0 max-w-[22ch] text-left font-serif text-[0.76rem] leading-snug text-cream/75 lg:mt-3.5 lg:text-center">
            {entry.delta.note}
          </p>
        </div>

        {/* evidence */}
        <div className="border-t border-navy/30 px-5 py-12 sm:px-8 lg:py-20">
          <div className="post-target">{artifacts[entry.id]}</div>
        </div>
      </div>
    </section>
  );
}
