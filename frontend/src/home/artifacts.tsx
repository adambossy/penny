import type { ReactNode } from "react";
import { ColumnLabel, Figure, LedgerRow, LedgerTable, Scroller } from "./ledger";
import type { home } from "./copy";

/** The six worked answers — what Penny actually hands back, not a chat bubble
 *  about it. Each is a small data graphic or ruled table in the ledger's own
 *  vocabulary, keyed by the copy's entry id so renaming an entry without
 *  updating its artifact is a compile error rather than an empty panel.
 *
 *  Every figure is authored sample data (Penny is pre-launch; PRODUCT.md
 *  forbids passing invented numbers off as a real household's results), and
 *  each panel renders an "Illustrative figures" marker. The numbers are
 *  internally consistent on purpose: the twelve bars below really do sum to the
 *  +$6,730 the gutter posts, the two projections in 02 genuinely meet at year
 *  8, and the budget deltas in 03 total −$412.
 *
 *  Colour never carries meaning alone. Gold marks the difference Penny found,
 *  but it is only 1.97:1 against the cream ground, so every gold shape also
 *  gets a navy outline and a text label. */

/* ── shared svg helpers ───────────────────────────────────────────────────── */

const NAVY = "var(--color-navy)";
const GOLD = "var(--color-orange)";
const INK = "var(--color-ink)";

/** Axis/among-the-marks lettering: condensed, small, and never below 10px. */
function Tick({
  x,
  y,
  children,
  anchor = "middle",
  bold = false,
  fill = NAVY,
}: {
  x: number;
  y: number;
  children: ReactNode;
  anchor?: "start" | "middle" | "end";
  bold?: boolean;
  fill?: string;
}) {
  return (
    <text
      x={x}
      y={y}
      textAnchor={anchor}
      fill={fill}
      style={{
        fontFamily: "var(--font-display)",
        fontStretch: "64%",
        fontSize: 11,
        fontWeight: bold ? 700 : 500,
        letterSpacing: "0.06em",
        textTransform: "uppercase",
      }}
    >
      {children}
    </text>
  );
}

/** A figure set in the money voice: tabular, lining, right-weighted. */
function Amount({
  x,
  y,
  children,
  anchor = "middle",
  size = 13,
  fill = INK,
}: {
  x: number;
  y: number;
  children: ReactNode;
  anchor?: "start" | "middle" | "end";
  size?: number;
  fill?: string;
}) {
  return (
    <text
      x={x}
      y={y}
      textAnchor={anchor}
      fill={fill}
      style={{
        fontFamily: "var(--font-ui)",
        fontSize: size,
        fontWeight: 600,
        fontVariantNumeric: "tabular-nums lining-nums",
      }}
    >
      {children}
    </text>
  );
}

function Chart({
  viewBox,
  label,
  children,
}: {
  viewBox: string;
  label: string;
  children: ReactNode;
}) {
  return (
    <Scroller min="min-w-[34rem]">
      <svg
        viewBox={viewBox}
        role="img"
        aria-label={label}
        className="block h-auto w-full overflow-visible"
      >
        {children}
      </svg>
    </Scroller>
  );
}

/* ── 01 · spending surge ──────────────────────────────────────────────────── */

const MONTHS = [
  ["Jan", 4020],
  ["Feb", 3880],
  ["Mar", 5400],
  ["Apr", 4150],
  ["May", 4090],
  ["Jun", 4260],
  ["Jul", 3990],
  ["Aug", 4110],
  ["Sep", 5930],
  ["Oct", 4080],
  ["Nov", 4200],
  ["Dec", 7700],
] as const;

const BASELINE = 4100;
const SURGE_TOP = 8200;

function SurgeChart() {
  const bottom = 196;
  const height = 168;
  const y = (v: number) => bottom - (v / SURGE_TOP) * height;
  const bw = 34;
  const step = 50;
  const x0 = 30;

  return (
    <Chart
      viewBox="0 0 640 236"
      label="Twelve months of spending against a $4,100 baseline. March, September and December break the pattern by $1,300, $1,830 and $3,600 — $6,730 in total, all of it one-off."
    >
      {MONTHS.map(([m, v], i) => {
        const x = x0 + i * step;
        const broke = v > BASELINE + 500;
        return (
          <g key={m}>
            {/* the within-pattern part of the month */}
            <rect
              x={x}
              y={y(Math.min(v, BASELINE))}
              width={bw}
              height={bottom - y(Math.min(v, BASELINE))}
              fill={NAVY}
              opacity={broke ? 0.32 : 0.22}
            />
            {/* the excess: gold, outlined so the shape survives without colour */}
            {broke ? (
              <>
                <rect
                  x={x}
                  y={y(v)}
                  width={bw}
                  height={y(BASELINE) - y(v)}
                  fill={GOLD}
                  stroke={NAVY}
                  strokeWidth={1}
                />
                <Amount x={x + bw / 2} y={y(v) - 8} size={12} fill={INK}>
                  {`+$${(v - BASELINE).toLocaleString()}`}
                </Amount>
              </>
            ) : null}
            <Tick x={x + bw / 2} y={bottom + 16} bold={broke}>
              {m}
            </Tick>
          </g>
        );
      })}

      {/* the baseline itself — the thing the three months broke */}
      <line
        x1={16}
        x2={624}
        y1={y(BASELINE)}
        y2={y(BASELINE)}
        stroke={NAVY}
        strokeWidth={1.5}
        strokeDasharray="5 4"
      />
      <Tick x={16} y={y(BASELINE) - 9} anchor="start">
        Baseline · $4,100/mo
      </Tick>
    </Chart>
  );
}

/* ── 02 · rent against buy ────────────────────────────────────────────────── */

/** Buying minus renting, in $k. Plotted as the DIFFERENCE rather than as two
 *  absolute net-worth curves: at a scale that fits $1.3m, a $54k gap between
 *  the two paths is about three pixels, so the chart would hide the very thing
 *  it exists to show. The difference curve is also the page's own thesis —
 *  every other line here reconciles two figures the same way. */
const GAP = [-19, -30, -40, -47, -52, -54, -48, -30, 0, 38, 84, 138, 199];

function CrossoverChart() {
  const zero = 148;
  const x = (i: number) => 66 + i * (540 / 12);
  const y = (v: number) => zero - v * 0.5;
  const at = (i: number) => `${x(i)},${y(GAP[i])}`;

  // Split the curve at the crossing so each side can be filled on its own.
  const rentingAhead = `M${x(0)},${zero} ${GAP.slice(0, 9).map((_, i) => `L${at(i)}`).join(" ")} L${x(8)},${zero} Z`;
  const buyingAhead = `M${x(8)},${zero} ${GAP.slice(8).map((_, i) => `L${at(i + 8)}`).join(" ")} L${x(12)},${zero} Z`;

  return (
    <Chart
      viewBox="0 0 640 232"
      label="The gap between buying and renting, projected twelve years. Renting stays ahead for eight years, peaking about $54,000 ahead at year 5, then the gap closes and buying pulls ahead to roughly $199,000 by year 12."
    >
      {/* renting ahead: gold, because this is the difference Penny found */}
      <path d={rentingAhead} fill={GOLD} opacity={0.6} stroke={NAVY} strokeWidth={1} />
      {/* buying ahead */}
      <path d={buyingAhead} fill={NAVY} opacity={0.22} stroke={NAVY} strokeWidth={1} />

      {/* break-even: the axis everything is measured from */}
      <line x1={40} x2={620} y1={zero} y2={zero} stroke={INK} strokeWidth={1.75} />
      <Tick x={40} y={zero - 8} anchor="start">
        Break-even
      </Tick>

      {[-50, 100, 200].map((v) => (
        <g key={v}>
          <line x1={66} x2={606} y1={y(v)} y2={y(v)} stroke={NAVY} strokeWidth={1} opacity={0.13} />
          <Tick x={58} y={y(v) + 4} anchor="end">
            {v < 0 ? `−$${-v}k` : `+$${v}k`}
          </Tick>
        </g>
      ))}

      <Tick x={x(3.2)} y={166} fill={INK}>
        Renting ahead
      </Tick>
      <Tick x={x(10.4)} y={y(60)}>
        Buying ahead
      </Tick>

      {/* the crossing — the answer to the question that was asked */}
      <circle cx={x(8)} cy={zero} r={6} fill={GOLD} stroke={NAVY} strokeWidth={1.5} />
      <Tick x={x(8)} y={zero + 26} bold>
        Year 8
      </Tick>

      {[0, 5, 12].map((i) => (
        <Tick key={i} x={x(i)} y={210}>
          {`Yr ${i}`}
        </Tick>
      ))}
      <Tick x={x(5)} y={190} fill={INK} bold>
        −$54k
      </Tick>
    </Chart>
  );
}

/* ── 04 · balance against comfort ─────────────────────────────────────────── */

const BALANCE = [
  ["Aug", 6400],
  ["Sep", 5900],
  ["Oct", 5200],
  ["Nov", 4300],
  ["Dec", 2900],
  ["Jan", 2150],
  ["Feb", 860],
  ["Mar", 2300],
] as const;

const COMFORT = 2000;

function ForecastChart() {
  const bottom = 176;
  const height = 140;
  const step = 556 / 7;
  const x = (i: number) => 46 + i * step;
  const y = (v: number) => bottom - (v / 7200) * height;
  const pts = BALANCE.map(([, v], i) => `${x(i)},${y(v)}`).join(" ");

  // Where the balance actually crosses the comfort line, so the shaded breach
  // covers only the months that are genuinely under it.
  const enter = x(5) + ((2150 - COMFORT) / (2150 - 860)) * step;
  const exit = x(6) + ((COMFORT - 860) / (2300 - 860)) * step;

  return (
    <Chart
      viewBox="0 0 640 246"
      label="Projected balance from August to March. February lands at $860, which is $1,140 under the $2,000 comfort line — the only month that breaches it."
    >
      {/* the breach: the gap has area, not just a point */}
      <polygon
        points={`${enter},${y(COMFORT)} ${x(6)},${y(860)} ${exit},${y(COMFORT)}`}
        fill={GOLD}
        opacity={0.6}
        stroke={NAVY}
        strokeWidth={1}
      />

      <line
        x1={30}
        x2={618}
        y1={y(COMFORT)}
        y2={y(COMFORT)}
        stroke={NAVY}
        strokeWidth={1.5}
        strokeDasharray="5 4"
      />
      <Tick x={30} y={y(COMFORT) - 9} anchor="start">
        Your comfort line · $2,000
      </Tick>

      <polyline points={pts} fill="none" stroke={INK} strokeWidth={2.25} />

      {BALANCE.map(([m, v], i) => (
        <g key={m}>
          <circle
            cx={x(i)}
            cy={y(v)}
            r={m === "Feb" ? 5.5 : 3}
            fill={m === "Feb" ? GOLD : INK}
            stroke={NAVY}
            strokeWidth={m === "Feb" ? 1.5 : 0}
          />
          <Tick x={x(i)} y={228} bold={m === "Feb"}>
            {m}
          </Tick>
        </g>
      ))}

      {/* the annotation sits in the empty ground below the curve, clear of the
          month ticks — the way a note is written in a statement's margin */}
      <Amount x={x(6)} y={196} size={13} fill={INK}>
        $860
      </Amount>
      <Tick x={x(6)} y={211} bold>
        −$1,140 under
      </Tick>
    </Chart>
  );
}

/* ── 05 · last year against this ──────────────────────────────────────────── */

const DRIFT = [
  ["Groceries", 940, 1012],
  ["Dining out", 585, 634],
  ["Subscriptions", 118, 152],
  ["Wellness", 402, 433],
] as const;

function DriftChart() {
  const x0 = 132;
  const w = 438;
  const max = 1100;
  const bar = (v: number) => (v / max) * w;

  return (
    <Chart
      viewBox="0 0 640 210"
      label="Four categories where this year's inflation-adjusted baseline sits above last year's: groceries up $72 a month, dining out $49, subscriptions $34, wellness $31 — $186 a month in total."
    >
      {DRIFT.map(([label, last, now], i) => {
        const y = 8 + i * 52;
        return (
          <g key={label}>
            <Tick x={x0 - 14} y={y + 21} anchor="end">
              {label}
            </Tick>
            {/* last year */}
            <rect x={x0} y={y} width={bar(last)} height={14} fill={NAVY} opacity={0.26} />
            {/* this year, with the drift called out in gold + outline */}
            <rect x={x0} y={y + 18} width={bar(last)} height={14} fill={NAVY} opacity={0.62} />
            <rect
              x={x0 + bar(last)}
              y={y + 18}
              width={bar(now - last)}
              height={14}
              fill={GOLD}
              stroke={NAVY}
              strokeWidth={1}
            />
            <Amount x={x0 + bar(now) + 12} y={y + 29} anchor="start" size={12}>
              {`+$${now - last}`}
            </Amount>
          </g>
        );
      })}
    </Chart>
  );
}

/* ── 06 · goal against trajectory ─────────────────────────────────────────── */

function GoalChart() {
  const x = (m: number) => 150 + (m / 30) * 470;
  return (
    <Chart
      viewBox="0 0 640 104"
      label="At the current rate of $1,450 a month the $40,000 goal takes 28 months. The four ranked cuts lift it to $1,900 a month and close it in 22 — two months inside the 24-month deadline."
    >
      {/* today */}
      <Tick x={140} y={31} anchor="end">
        Today
      </Tick>
      <rect x={x(0)} y={20} width={x(28) - x(0)} height={15} fill={NAVY} opacity={0.28} />
      <Tick x={x(28) - 9} y={31} anchor="end" fill={INK}>
        28 mo
      </Tick>

      {/* with the cuts */}
      <Tick x={140} y={57} anchor="end" bold>
        With the cuts
      </Tick>
      <rect
        x={x(0)}
        y={46}
        width={x(22) - x(0)}
        height={15}
        fill={GOLD}
        stroke={NAVY}
        strokeWidth={1}
      />
      <Tick x={x(22) - 9} y={57} anchor="end" fill={INK} bold>
        22 mo
      </Tick>

      {/* the deadline both are measured against */}
      <line
        x1={x(24)}
        x2={x(24)}
        y1={12}
        y2={70}
        stroke={INK}
        strokeWidth={1.5}
        strokeDasharray="4 3"
      />
      <Tick x={x(24)} y={86}>
        Your 24-month deadline
      </Tick>
    </Chart>
  );
}

/* ── the six ─────────────────────────────────────────────────────────────── */

export const artifacts: Record<(typeof home.entries)[number]["id"], ReactNode> = {
  analyze: (
    <Figure
      caption="Monthly spend against your own baseline · last 12 months"
      note="Every dollar of the $6,730 traces to a dated, one-off event, so your recurring baseline never actually moved. December is the one to watch — it repeats every year."
    >
      <SurgeChart />
      <div className="mt-6">
        <LedgerTable head={["Month", "Over baseline", "What drove it"]}>
          <LedgerRow cells={["March", "+$1,300", "Dental $620 · new tires $680"]} />
          <LedgerRow cells={["September", "+$1,830", "Italy — flights & hotels"]} />
          <LedgerRow cells={["December", "+$3,600", "Gifts $2,100 · travel $1,150 · hosting $350"]} />
          <LedgerRow cells={["Three months", "+$6,730", "All one-off"]} total />
        </LedgerTable>
      </div>
    </Figure>
  ),

  project: (
    <Figure
      caption="The gap between the two paths · 12 years from today's balances"
      note="Renting and investing stays ahead for eight years because the $150,000 you did not put down keeps compounding. Buying wins after that, and keeps winning. Illustrative only — not tax advice."
    >
      <CrossoverChart />
      <div className="mt-6">
        <LedgerTable head={["", "Rent + invest", "Buy at $750k", "Δ"]}>
          <LedgerRow cells={["Year 5", "$530k", "$476k", "−$54k"]} />
          <LedgerRow cells={["Year 8", "$812k", "$812k", "—"]} />
          <LedgerRow cells={["Year 12", "$1,285k", "$1,484k", "+$199k"]} total />
        </LedgerTable>
      </div>
    </Figure>
  ),

  budget: (
    <Figure
      caption="Twelve-month average against a 10% contracted target"
      note="Rent, groceries and utilities are held exactly where they are. The $412 comes entirely out of discretionary lines, which is why the plan survives contact with a normal month."
    >
      <LedgerTable head={["Line", "12-mo avg", "Target", "Δ"]}>
        <LedgerRow cells={["Shopping", "$810", "$729", "−$81"]} />
        <LedgerRow cells={["Travel", "$720", "$648", "−$72"]} />
        <LedgerRow cells={["Dining out", "$640", "$576", "−$64"]} />
        <LedgerRow cells={["Wellness", "$460", "$414", "−$46"]} />
        <LedgerRow cells={["Entertainment", "$380", "$342", "−$38"]} />
        <LedgerRow cells={["Gifts", "$320", "$288", "−$32"]} />
        <LedgerRow cells={["Four more discretionary lines", "$795", "$716", "−$79"]} muted />
        <LedgerRow cells={["Essentials — rent, groceries, utilities", "$3,430", "$3,430", "—"]} muted />
        <LedgerRow cells={["Monthly total", "$7,555", "$7,143", "−$412"]} total />
      </LedgerTable>
    </Figure>
  ),

  forecast: (
    <Figure
      caption="Projected balance · August through March"
      note="Flagged in August, six months before it happens: February is the only month that breaches the line, and moving one annual bill clears it entirely."
    >
      <ForecastChart />
    </Figure>
  ),

  trends: (
    <Figure
      caption="This year against last · inflation-adjusted monthly baseline"
      note="Four quiet lines, none of them dramatic on its own, add $2,232 a year. Penny separates them from the one-off weekend that looks worse but changes nothing."
    >
      <p className="m-0 mb-5">
        <ColumnLabel className="text-navy-700">
          Upper bar · last year — lower · this year, inflation-adjusted
        </ColumnLabel>
      </p>
      <DriftChart />
      <p className="m-0 mt-5 border-t border-navy/20 pt-4">
        <ColumnLabel className="text-steel">
          Travel fell $15 — one weekend, not a trend, so it is not drift
        </ColumnLabel>
      </p>
    </Figure>
  ),

  optimize: (
    <Figure
      caption="$40,000 in 24 months · ranked from least to most painful"
      note="Ranked so you can stop reading at the point it stops being worth it. The first two cuts alone are worth $180 a month and neither one changes a weekend."
    >
      <LedgerTable head={["Cut", "Per month", "Running rate"]}>
        <LedgerRow cells={["Current saving rate", "—", "$1,450"]} muted />
        <LedgerRow cells={["1 · Unused subscriptions", "+$62", "$1,512"]} />
        <LedgerRow cells={["2 · Coffee & lunch out", "+$118", "$1,630"]} />
        <LedgerRow cells={["3 · Rideshare → transit", "+$96", "$1,726"]} />
        <LedgerRow cells={["4 · Dining out, down 15%", "+$174", "$1,900"]} />
        <LedgerRow cells={["Closes in 22 months", "+$450", "$1,900"]} total />
      </LedgerTable>
      <div className="mt-6">
        <GoalChart />
      </div>
    </Figure>
  ),
};
