/**
 * Typed mirror of the palette + font families declared in theme.css's @theme
 * block, for JS/TS consumers that can't reach Tailwind utilities or CSS vars
 * (inline styles, charts). theme.css is the source of truth; tokens.drift.test.ts
 * fails if these values drift from it.
 */
export const tokens = {
  paper: "#FAF4E7",
  cream: "#ECE0C0",
  creamSoft: "#F3E9CF",
  ink: "#1C3E3A",
  navy: "#1E4846",
  navy700: "#2A625D",
  steel: "#3C7A72",
  orange: "#D69E3D",
  orangeSoft: "#E3B255",

  fontDisplay: "'Archivo','Helvetica Neue',Arial,sans-serif",
  fontSerif: "'Literata',Georgia,serif",
  fontUi: "'Work Sans',system-ui,sans-serif",
} as const;

export type Tokens = typeof tokens;
