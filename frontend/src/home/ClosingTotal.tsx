import { Link } from "react-router";
import { ColumnLabel } from "./ledger";
import { home } from "./copy";

/** The close: the accountant's double rule under a finished column, and the
 *  last posted line is the one the visitor writes. No coloured slab, no
 *  centred emblem — the ledger simply totals and stops. */
export function ClosingTotal() {
  return (
    <section aria-labelledby="closing-title" className="mx-auto max-w-[88rem] px-5 sm:px-8">
      <div className="rule-total border-navy pt-12 pb-24 lg:pt-16">
        <ColumnLabel className="text-navy-700">{home.closing.line}</ColumnLabel>

        <h2
          id="closing-title"
          className="statement mt-5 max-w-[16ch] text-[2.9rem] text-ink sm:text-[3.9rem]"
        >
          {home.closing.title}
        </h2>

        <p className="mt-6 max-w-[50ch] font-serif text-[1.14rem] leading-[1.55] text-navy-700">
          {home.closing.body}
        </p>

        <div className="mt-10 flex flex-wrap items-center gap-x-8 gap-y-4">
          <Link
            to="/sign-up"
            className="bg-ink px-8 py-4 text-paper no-underline transition-colors hover:bg-navy-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-navy"
          >
            <ColumnLabel className="text-[0.78rem]">{home.closing.cta}</ColumnLabel>
          </Link>
          <ColumnLabel className="text-steel">
            {home.entries.length} lines reconciled above
          </ColumnLabel>
        </div>
      </div>
    </section>
  );
}
