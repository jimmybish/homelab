#!/usr/bin/env node
/* Regression tests for theme-aware CSS custom properties (app.css).

 * These prove the round-3 review finding 4319617469 stays fixed: the bold
 * Markdown rule used var(--text-bright, #fff) but --text-bright was never
 * declared in :root or [data-theme="light"], so bold text and rendered
 * headings (md.js maps ^#{1,4} headings to <b>) were always white and
 * nearly invisible on the light theme's #ffffff/#f2f4fb surfaces.

 * Guard 1: every var() token referenced by app.css must be declared in the
 * same file (catches the whole undefined-token class, not just this one).
 * Guard 2: the .md b/.md strong rule must use --text (the theme-aware
 * token) and must not reference --text-bright or a bare #fff fallback.
 * Guard 3: WCAG contrast of the resolved bold color against the card
 * surface must clear 4.5:1 in BOTH themes (dark #151a2e + light #ffffff).

 * Stdlib node only (no npm install). Run from anywhere:
 *
 *     node ansible/roles/kanban_ui/tests/test_css_theme_tokens.js
 *
 * Exit 0 = all pass. Any failure prints and exits 1.
 */
'use strict';

const fs = require('fs');
const path = require('path');

const cssPath = path.join(__dirname, '..', 'files', 'app', 'static', 'app.css');
const css = fs.readFileSync(cssPath, 'utf8');

let failures = 0;
function check(name, cond, detail) {
  if (cond) {
    console.log('PASS  ' + name);
  } else {
    failures += 1;
    console.log('FAIL  ' + name + (detail ? '  -> ' + JSON.stringify(detail) : ''));
  }
}

// ---- token declaration / usage audit -------------------------------------

// Declared tokens: "--name:" occurrences (definitions), not var() uses.
const declared = new Set();
for (const m of css.matchAll(/(--[a-zA-Z0-9-]+)\s*:/g)) declared.add(m[1]);

// Used tokens: first argument of every var(--name ...) (fallback ignored).
const used = new Set();
for (const m of css.matchAll(/var\(\s*(--[a-zA-Z0-9-]+)/g)) used.add(m[1]);

const undefinedTokens = [...used].filter((t) => !declared.has(t));
check('every var() token is declared in app.css', undefinedTokens.length === 0, undefinedTokens);
check('--text-bright is not referenced anywhere', !used.has('--text-bright') && !css.includes('--text-bright'));

// ---- the fixed rule itself ------------------------------------------------

const boldRule = css.match(/\.md b,\s*\.md strong\s*\{([^}]*)\}/);
check('.md b/.md strong rule exists', boldRule !== null);
if (boldRule) {
  const decl = boldRule[1];
  check('bold rule uses var(--text)', /color:\s*var\(\s*--text\s*\)/.test(decl), decl);
  check('bold rule has no white fallback', !/#fff/i.test(decl), decl);
}

// ---- contrast in both themes ----------------------------------------------

function blockVars(selector) {
  const i = css.indexOf(selector);
  if (i < 0) return null;
  const open = css.indexOf('{', i);
  const close = css.indexOf('}', open);
  const body = css.slice(open + 1, close);
  const vars = {};
  for (const m of body.matchAll(/(--[a-zA-Z0-9-]+)\s*:\s*([^;]+);/g)) vars[m[1]] = m[2].trim();
  return vars;
}
function hex(v) {
  const h = v.replace(/^#/, '');
  const n = h.length === 3 ? [...h].map((c) => c + c).join('') : h;
  return [0, 2, 4].map((i) => parseInt(n.slice(i, i + 2), 16));
}
function lum([r, g, b]) {
  const f = (c) => {
    const s = c / 255;
    return s <= 0.03928 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4);
  };
  return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
}
function contrast(a, b) {
  const [l1, l2] = [lum(hex(a)), lum(hex(b))].sort((x, y) => y - x);
  return (l1 + 0.05) / (l2 + 0.05);
}

const dark = blockVars(':root');
const light = blockVars('[data-theme="light"]');
check(':root and light theme blocks parse', dark !== null && light !== null);
if (dark && light) {
  // Bold color resolves to --text; surface behind task/comment cards is --surface.
  const cd = contrast(dark['--text'], dark['--surface']);
  const cl = contrast(light['--text'], light['--surface']);
  check('dark theme bold-on-surface contrast >= 4.5:1', cd >= 4.5, cd.toFixed(2));
  check('light theme bold-on-surface contrast >= 4.5:1', cl >= 4.5, cl.toFixed(2));
  // Regression canary: pure white on the light surface would be ~1.07:1.
  check('white would NOT pass light contrast (canary)', contrast('#ffffff', light['--surface']) < 4.5);
}

if (failures) {
  console.log('\n' + failures + ' FAILURE(S)');
  process.exit(1);
}
console.log('\nAll CSS theme-token checks passed.');
