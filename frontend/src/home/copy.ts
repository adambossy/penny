/** All landing-page copy in one place — edit here, never in the section
 *  components.
 *
 *  Voice: this page is a RECONCILIATION. Every line poses two figures against
 *  each other and names the difference, because every one of Penny's six real
 *  capabilities is exactly that shape. Copy states only what Penny actually
 *  does (REQUIREMENTS.txt P1–P13); the figures in `entries` are illustrative
 *  sample data, labelled as such wherever they render.
 *
 *  Four strings are pinned by e2e/home.spec.ts and must not be reworded
 *  casually: the h1 contains "Meet Penny", the header CTA is "Meet Penny", the
 *  sign-in link is "Sign in", and the closing CTA matches /start chatting with
 *  penny/i. The six entry `id`s are the section anchors the same test asserts. */

const nav = [
  { label: "Surge", href: "#analyze" },
  { label: "Break-even", href: "#project" },
  { label: "Budget", href: "#budget" },
  { label: "Forecast", href: "#forecast" },
  { label: "Drift", href: "#trends" },
] as const;

export interface Entry {
  /** Ledger line number. The sequence is real: the gutter posts these in order
   *  and the close counts them, so the numbering carries information. */
  n: string;
  id: "analyze" | "project" | "budget" | "forecast" | "trends" | "optimize";
  /** The ledger line name, set in condensed caps. */
  line: string;
  title: string;
  body: string;
  /** The finding, posted into the gutter. `value` is gold on the ink field. */
  delta: { value: string; note: string };
  /** The question a visitor would actually type. Doubles as the line's CTA. */
  ask: string;
}

export const home = {
  nav,
  header: { signIn: "Sign in", cta: "Meet Penny" },

  opening: {
    /** Rendered as the h1 — "Meet Penny" is asserted by home.spec.ts. */
    title: "Meet Penny",
    lede: "She reads every transaction you have ever made, then shows you the difference between what you assume you spend and what you actually did.",
    askLabel: "Ask her something",
    inputPlaceholder: "Which months broke my spending pattern last year?",
    asks: [
      "Which months broke my spending pattern last year?",
      "Rent, or buy at $750k — where do the lines cross?",
      "Build a budget from twelve months, tightened 10%.",
    ],
    /** Sits under the ask line, in the annotation voice. */
    footnote: "Connect a bank in about a minute. Two people, one household, one ledger.",
  },

  /** The column headings for the whole spread — a legend, not a stat band.
   *  It teaches the reader how to read every line below it, and invents no
   *  metric (Penny is pre-launch; there are no usage numbers to show). */
  headings: {
    left: { label: "What you'd assume", note: "The figure you carry in your head." },
    right: {
      label: "What the ledger says",
      note: "Computed across every transaction Penny has synced.",
    },
    delta: { label: "The difference", note: "And the reason for it." },
  },

  entries: [
    {
      n: "01",
      id: "analyze",
      line: "Spending surge",
      title: "Your average is hiding three months.",
      body: "Penny walks a full year, finds the months that broke your own pattern, then splits each one into the merchants and the one-off events behind it — so a trip you took once stops reading as a habit you've formed.",
      delta: { value: "+$6,730", note: "Three months over baseline — all of it one-off" },
      ask: "Find my spending surges and explain them",
    },
    {
      n: "02",
      id: "project",
      line: "Rent against buy",
      title: "The break-even is later than you think.",
      body: "Name the house and Penny runs it off your real balances: the down payment, the monthly carry, principal paid down, appreciation, and the market return on the cash you didn't spend. Both paths on one axis, and the year the lines actually cross.",
      delta: { value: "Year 8", note: "Where buying finally overtakes renting" },
      ask: "Rent vs. buy a $750k house",
    },
    {
      n: "03",
      id: "budget",
      line: "Average against target",
      title: "A budget that argues with you.",
      body: "Not last month handed back to you. Penny builds from twelve months of averages — the trips and the holidays smoothed out — then contracts each discretionary line by the margin you set and holds the essentials exactly where they are.",
      delta: { value: "−$412", note: "Per month, without touching an essential" },
      ask: "Twelve-month averages, contracted 10%",
    },
    {
      n: "04",
      id: "forecast",
      line: "Balance against comfort",
      title: "See the tight month while it's still far away.",
      body: "Penny projects your balance forward from recurring income, every bill and subscription, and the seasonal shape of your own history — then marks the months you drop under the line you set, early enough that you can still move something.",
      delta: { value: "−$1,140", note: "Under your line in February — flagged in August" },
      ask: "Forecast my cash flow to year-end",
    },
    {
      n: "05",
      id: "trends",
      line: "Last year against this",
      title: "Lifestyle creep never announces itself.",
      body: "Year over year, category by category, adjusted for inflation. Penny separates the baseline that is quietly ratcheting up from the one weekend you overspent, and names which lines are drifting.",
      delta: { value: "+$186", note: "Per month of real baseline drift, in four lines" },
      ask: "Where is lifestyle creep setting in?",
    },
    {
      n: "06",
      id: "optimize",
      line: "Goal against trajectory",
      title: "Name the number. Penny finds the cuts.",
      body: "Forty thousand dollars in twenty-four months. Penny reverse-engineers it against what you actually spend and ranks the cuts from least to most painful, so the math closes without assuming you'll become a different person.",
      delta: { value: "22 months", note: "Goal closes two months early on ranked cuts" },
      ask: "Reverse-engineer my $40k goal",
    },
  ] satisfies readonly Entry[],

  closing: {
    line: "Balance",
    title: "Ask her something hard.",
    body: "Penny is standing by with your whole history in front of her. Give her a question you couldn't answer yourself.",
    cta: "Start chatting with Penny",
  },

  colophon: {
    tagline: "Penny — she reads the whole ledger",
    /** Required disclosure: every figure on this page is authored sample data.
     *  PRODUCT.md forbids presenting it as a real household's results. */
    disclosure:
      "Every figure on this page is illustrative sample data, authored to show the shape of Penny's answers. It is not a real household's ledger, and none of it is financial or tax advice.",
    copyright: "© 2026 Penny",
    links: nav.slice(0, 3),
  },
} as const;
