import { Wordmark } from "@penny/ui";
import { ColumnLabel } from "./ledger";
import { home } from "./copy";

/** The colophon: the ink field that anchors the page, and the honest note at
 *  the bottom of the document. The sample-data disclosure lives here in full —
 *  each artifact carries a short marker, this states it plainly. */
export function Colophon() {
  return (
    <footer className="bg-ink">
      <div className="mx-auto max-w-[88rem] px-5 py-12 sm:px-8">
        <div className="flex flex-col items-start justify-between gap-6 sm:flex-row sm:items-center">
          <Wordmark size={40} className="text-cream" />
          <p className="m-0 font-serif text-[0.95rem] text-cream/75">{home.colophon.tagline}</p>
          <nav aria-label="Reconciled lines" className="flex gap-6">
            {home.colophon.links.map((n) => (
              <a
                key={n.href}
                href={n.href}
                className="no-underline focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-cream"
              >
                <ColumnLabel className="text-cream/70 transition-colors hover:text-cream">
                  {n.label}
                </ColumnLabel>
              </a>
            ))}
          </nav>
        </div>

        <div className="mt-10 border-t border-cream/20 pt-6">
          <p className="m-0 max-w-[78ch] font-ui text-[0.8rem] leading-relaxed text-cream/70">
            {home.colophon.disclosure}
          </p>
          <p className="m-0 mt-4">
            <ColumnLabel className="text-cream/70">{home.colophon.copyright}</ColumnLabel>
          </p>
        </div>
      </div>
    </footer>
  );
}
