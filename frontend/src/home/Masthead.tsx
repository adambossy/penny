import { Link } from "react-router";
import { Wordmark } from "@penny/ui";
import { ColumnLabel } from "./ledger";
import { home } from "./copy";

/** The statement masthead. Solid paper and a double rule, not a translucent
 *  blurred bar: this is the head of a document, and the rule under it is the
 *  same rule the spread below is built from. */
export function Masthead() {
  return (
    <header className="sticky top-0 z-40 border-b-[3px] border-double border-navy bg-paper">
      <div className="mx-auto flex max-w-[88rem] items-center justify-between gap-3 px-5 py-3.5 sm:gap-6 sm:px-8">
        {/* The lockup steps down on narrow screens so the wordmark, Sign in and
            the CTA all fit on one line at 390px instead of each wrapping. */}
        <a href="/" className="no-underline" aria-label="Penny — home">
          <span className="sm:hidden">
            <Wordmark size={32} className="text-ink" />
          </span>
          <span className="hidden sm:block">
            <Wordmark size={40} className="text-ink" />
          </span>
        </a>

        <nav aria-label="Reconciled lines" className="hidden items-center gap-7 lg:flex">
          {home.nav.map((n) => (
            <a
              key={n.href}
              href={n.href}
              className="no-underline transition-colors hover:text-ink focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-navy"
            >
              <ColumnLabel className="text-navy-700 transition-colors hover:text-ink">
                {n.label}
              </ColumnLabel>
            </a>
          ))}
        </nav>

        <div className="flex shrink-0 items-center gap-3 sm:gap-5">
          <Link
            to="/sign-in"
            className="whitespace-nowrap font-ui text-[0.9rem] text-navy no-underline transition-colors hover:text-ink focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-navy"
          >
            {home.header.signIn}
          </Link>
          <Link
            to="/sign-up"
            className="whitespace-nowrap bg-ink px-3.5 py-2.5 text-paper no-underline transition-colors hover:bg-navy-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-navy sm:px-5"
          >
            <ColumnLabel>{home.header.cta}</ColumnLabel>
          </Link>
        </div>
      </div>
    </header>
  );
}
