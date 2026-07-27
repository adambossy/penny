import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router";
import { Logo } from "@penny/ui";
import { ColumnLabel, Illustrative } from "./ledger";
import { home } from "./copy";

/** The opening balance — the first viewport, and a thesis rather than a header.
 *
 *  The right-hand block is the whole page in miniature: two figures posed
 *  against each other and the difference set in gold on ink. A visitor learns
 *  the grammar of every line below before they reach the first one.
 *
 *  Those figures tie to line 01 exactly: the $6,730 that three one-off months
 *  put over baseline is $561 a month spread across twelve, and $4,100 + $561 is
 *  the $4,661 average. Nothing here is a round number chosen to look good. */
export function OpeningBalance() {
  const [question, setQuestion] = useState("");
  const navigate = useNavigate();
  const goSignUp = () => navigate("/sign-up");

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    goSignUp();
  }

  return (
    <section aria-labelledby="opening-title" className="mx-auto max-w-[88rem] px-5 sm:px-8">
      {/* letterhead line */}
      <div className="flex items-center justify-between gap-6 border-b border-navy/25 py-4">
        <ColumnLabel className="text-navy-700">Household reconciliation</ColumnLabel>
        <Logo variant="emblem" size={44} className="shrink-0" />
      </div>

      <div className="grid gap-x-16 gap-y-12 pt-12 pb-16 lg:grid-cols-[1.05fr_0.95fr] lg:pt-16 lg:pb-20">
        {/* the claim */}
        <div>
          <h1
            id="opening-title"
            className="statement m-0 text-[3.4rem] text-ink sm:text-[4.6rem] lg:text-[5.2rem]"
          >
            {home.opening.title}
          </h1>
          <p className="mt-7 max-w-[46ch] font-serif text-[1.24rem] leading-[1.5] text-navy-700">
            {home.opening.lede}
          </p>

          <form onSubmit={onSubmit} className="mt-10 max-w-[34rem]">
            <label htmlFor="opening-ask" className="block">
              <ColumnLabel className="text-navy-700">{home.opening.askLabel}</ColumnLabel>
            </label>
            <div className="mt-2.5 flex items-stretch gap-3 border-b-2 border-navy">
              <input
                id="opening-ask"
                type="text"
                value={question}
                onChange={(e) => setQuestion(e.target.value)}
                placeholder={home.opening.inputPlaceholder}
                className="min-w-0 flex-1 bg-transparent py-3 pr-3 font-ui text-[0.95rem] text-ink placeholder:text-steel focus:outline-none"
              />
              <button
                type="submit"
                className="shrink-0 bg-ink px-5 text-paper transition-colors hover:bg-navy-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-navy"
              >
                <ColumnLabel>Ask Penny</ColumnLabel>
              </button>
            </div>

            <ul className="m-0 mt-5 flex list-none flex-col gap-2.5 p-0">
              {home.opening.asks.map((a) => (
                <li key={a}>
                  <button
                    type="button"
                    onClick={goSignUp}
                    className="group flex items-baseline gap-2.5 text-left font-ui text-[0.9rem] text-navy-700 transition-colors hover:text-ink focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-navy"
                  >
                    <span className="text-orange" aria-hidden="true">
                      &#9670;
                    </span>
                    <span className="border-b border-transparent transition-colors group-hover:border-navy">
                      {a}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          </form>

          <p className="mt-8 max-w-[42ch] font-serif text-[0.88rem] leading-snug text-steel">
            {home.opening.footnote}
          </p>
        </div>

        {/* the page in miniature */}
        <div className="self-center border-t-2 border-navy">
          <div className="grid sm:grid-cols-2">
            <Assumption
              label={home.headings.left.label}
              amount="$4,100"
              note="A month. Your baseline — the number you would quote from memory."
            />
            <Assumption
              label={home.headings.right.label}
              amount="$4,661"
              note="A month. What you actually averaged across the last twelve."
              divided
            />
          </div>

          <div className="bg-ink px-6 py-7">
            <ColumnLabel className="text-orange">
              <span aria-hidden="true">Δ </span>
              {home.headings.delta.label}
            </ColumnLabel>
            <p className="statement tnum m-0 mt-3 text-[2.6rem] leading-none text-orange">+$561</p>
            <p className="m-0 mt-3 max-w-[36ch] font-serif text-[0.9rem] leading-snug text-cream/75">
              A month — and every dollar of it lands in three one-off months, not in a habit you
              have formed. That is line 01.
            </p>
          </div>

          <div className="flex justify-end border-t border-navy/20 px-6 py-3">
            <Illustrative />
          </div>
        </div>
      </div>
    </section>
  );
}

function Assumption({
  label,
  amount,
  note,
  divided = false,
}: {
  label: string;
  amount: string;
  note: string;
  divided?: boolean;
}) {
  return (
    <div
      className={`px-6 py-7 ${divided ? "border-t border-navy/25 sm:border-t-0 sm:border-l" : ""}`}
    >
      <ColumnLabel className="text-navy-700">{label}</ColumnLabel>
      <p className="statement tnum m-0 mt-3 text-[2.35rem] leading-none text-ink">{amount}</p>
      <p className="m-0 mt-3 max-w-[26ch] font-serif text-[0.85rem] leading-snug text-navy-700">
        {note}
      </p>
    </div>
  );
}
