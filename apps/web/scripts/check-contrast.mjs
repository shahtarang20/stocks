#!/usr/bin/env node
// Checks that design-token colour pairs in src/app/globals.css meet WCAG AA (4.5:1)
// for normal text, in both the light (:root) and dark (.dark) themes.
// Usage: node scripts/check-contrast.mjs   (exit 1 on any failing pair)
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const MIN = 4.5;
const css = readFileSync(
  fileURLToPath(new URL("../src/app/globals.css", import.meta.url)),
  "utf8",
);

function tokens(selectorRe) {
  const m = css.match(selectorRe);
  if (!m) throw new Error(`block not found: ${selectorRe}`);
  const out = {};
  for (const [, name, h, s, l] of m[1].matchAll(
    /--([a-z0-9-]+):\s*([\d.]+)\s+([\d.]+)%\s+([\d.]+)%\s*;/g,
  )) {
    out[name] = [Number(h), Number(s) / 100, Number(l) / 100];
  }
  return out;
}

function hslToRgb([h, s, l]) {
  const c = (1 - Math.abs(2 * l - 1)) * s;
  const x = c * (1 - Math.abs(((h / 60) % 2) - 1));
  const m = l - c / 2;
  const sector = Math.floor(h / 60) % 6;
  const [r, g, b] = [
    [c, x, 0], [x, c, 0], [0, c, x], [0, x, c], [x, 0, c], [c, 0, x],
  ][sector];
  return [r + m, g + m, b + m];
}

function luminance(hsl) {
  const [r, g, b] = hslToRgb(hsl).map((v) =>
    v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4,
  );
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

export function ratio(a, b) {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

// [foreground token, background token]
const PAIRS = [
  ["foreground", "background"],
  ["foreground", "surface"],
  ["foreground", "surface-2"],
  ["foreground", "muted"],
  ["muted-foreground", "background"],
  ["muted-foreground", "surface"],
  ["muted-foreground", "surface-2"],
  ["primary-foreground", "primary"],
  ["success-foreground", "success"],
  ["warning-foreground", "warning"],
  ["danger-foreground", "danger"],
  // Coloured text on the page and card backgrounds (text-primary, text-danger, ...).
  ["primary", "background"],
  ["primary", "surface"],
  ["success", "background"],
  ["success", "surface"],
  ["warning", "background"],
  ["warning", "surface"],
  ["danger", "background"],
  ["danger", "surface"],
];

const themes = {
  light: tokens(/:root\s*{([^}]*)}/),
  dark: tokens(/\.dark\s*{([^}]*)}/),
};

let failures = 0;
for (const [theme, t] of Object.entries(themes)) {
  for (const [fg, bg] of PAIRS) {
    if (!t[fg] || !t[bg]) {
      console.error(`${theme}: missing token for ${fg}/${bg}`);
      failures++;
      continue;
    }
    const r = ratio(t[fg], t[bg]);
    const ok = r >= MIN;
    if (!ok) failures++;
    console.log(`${ok ? "PASS" : "FAIL"} ${theme.padEnd(5)} ${fg} on ${bg}: ${r.toFixed(2)}:1`);
  }
}
if (failures) {
  console.error(`\n${failures} pair(s) below ${MIN}:1`);
  process.exit(1);
}
console.log(`\nAll pairs >= ${MIN}:1`);
