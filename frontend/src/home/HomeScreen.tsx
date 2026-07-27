import { Masthead } from "./Masthead";
import { OpeningBalance } from "./OpeningBalance";
import { ColumnHeadings, SpreadEntry } from "./spread";
import { ClosingTotal } from "./ClosingTotal";
import { Colophon } from "./Colophon";
import { home } from "./copy";

/**
 * The public logged-out landing page (meetpenny.app). Pure marketing chrome —
 * no auth, no API calls; every CTA routes to /sign-up (see home.spec.ts).
 *
 * THESIS: this page is a reconciliation — two figures posed against each other
 * until the difference is explained, because all six of Penny's capabilities
 * are that shape. It refuses the alternating feature-row landing page.
 *
 * OWN-WORLD: the pinned cream palette at its full range, not its softest
 * rendition — paper ground, hairline navy rules, and one continuous ink gutter
 * down the spread. Archivo lives only at its width extremes (extended caps
 * head, condensed caps label); Literata annotates. Gold means the difference
 * Penny found and nothing else, and appears only on ink, where it clears
 * 4.9:1. Square corners throughout: no cards, pills, blur, or applied texture.
 *
 * STORY: a household sees the gap between what it assumes it spends and what it
 * actually did; six worked lines prove Penny can close it; they sign up.
 *
 * FIRST VIEWPORT: letterhead rule and seal, "MEET PENNY" in extended caps, the
 * lede, a ruled ask line — and on the right the whole page in miniature,
 * $4,100 against $4,661 with +$561 set in gold on ink.
 *
 * FORM: reconciliation spread; candidate 6 of 7 on the resonance-ordered list;
 * concept-seed key 766a4827.
 */
export function HomeScreen() {
  return (
    <div className="marketing h-full overflow-y-auto bg-paper font-ui text-ink">
      <Masthead />
      <main>
        <OpeningBalance />
        <ColumnHeadings />
        {home.entries.map((entry) => (
          <SpreadEntry key={entry.id} entry={entry} />
        ))}
        {/* closes the columns — the rule that ends a ledger page */}
        <div aria-hidden="true" className="mx-auto max-w-[88rem] border-t border-navy/30" />
        <ClosingTotal />
      </main>
      <Colophon />
    </div>
  );
}
