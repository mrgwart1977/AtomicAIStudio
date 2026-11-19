// C4 — repeatable WCAG contrast audit for the theme tokens in
// styles.css (@theme block). Text pairs assert WCAG 1.4.3 AA (4.5:1,
// normal text — the UI uses <=15px glyphs throughout); non-text
// indicators assert WCAG 1.4.11 (3:1). Disabled-opacity states are
// exempt per 1.4.3's "inactive component" clause — recorded for
// visibility, not asserted. Advisory: --color-line hairlines are
// 1.38:1 on card (decorative separators; focused inputs switch to
// border-action/focus ring, so no component relies on them alone).
import { describe, expect, it } from "vitest";

const TOKENS: Record<string, string> = {
  ink: "#0c111c", panel: "#101828", card: "#16202f", cardHi: "#1b2740",
  line: "#283850", fg: "#edf3ff", muted: "#aebbd0", faint: "#7e899b",
  teal: "#39d3bb", action: "#2f8f76", actionHi: "#39a888",
  violet: "#8b7cf6", amber: "#e8b45a", danger: "#e26d6d",
  white: "#ffffff", red300: "#fca5a5",
};

const lum = (hex: string) => {
  const c = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255)
    .map((v) => (v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4));
  return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
};
export const ratio = (a: string, b: string) => {
  const [l1, l2] = [lum(a), lum(b)];
  return (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
};

const TEXT_PAIRS: [string, string][] = [
  ["fg", "ink"], ["fg", "panel"], ["fg", "card"], ["fg", "cardHi"],
  ["muted", "ink"], ["muted", "panel"], ["muted", "card"],
  ["faint", "ink"], ["faint", "panel"], ["faint", "card"],
  ["teal", "ink"], ["teal", "card"],        // selected clips, links
  ["ink", "action"], ["ink", "actionHi"],   // primary buttons + hover
  ["ink", "teal"],                          // teal buttons
  ["danger", "card"], ["danger", "panel"],  // error text
  ["amber", "card"],                        // warning text
  ["red300", "card"],                       // hover accents
  ["violet", "card"],                       // selection cues
  ["white", "ink"], ["white", "panel"],     // white on dark overlays
];
const NONTEXT_PAIRS: [string, string][] = [
  ["teal", "ink"], ["teal", "panel"], ["teal", "card"],  // focus ring, sel ring
];

describe("theme contrast — WCAG 1.4.3 AA (text) + 1.4.11 (non-text)", () => {
  for (const [f, b] of TEXT_PAIRS) {
    it(`${f} on ${b} >= 4.5:1`, () => {
      expect(ratio(TOKENS[f], TOKENS[b])).toBeGreaterThanOrEqual(4.5);
    });
  }
  for (const [f, b] of NONTEXT_PAIRS) {
    it(`${f} on ${b} >= 3:1 (non-text indicator)`, () => {
      expect(ratio(TOKENS[f], TOKENS[b])).toBeGreaterThanOrEqual(3);
    });
  }
});
