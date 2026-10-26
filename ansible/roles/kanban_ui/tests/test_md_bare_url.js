#!/usr/bin/env node
/* Regression tests for the bare-URL autolinker in md.js (step 5).
 *
 * Proves the round-2 review finding "double-escaped bare URLs in markdown
 * hrefs" (Copilot inline comment 4301554474, md.js:59) stays fixed: a bare
 * URL like https://example.invalid/?a=1&b=2 must emit a SINGLE-escaped
 * href (`&amp;`, not `&amp;amp;`) so the browser requests `b=2`, while the
 * scheme gate (https/mailto only) and quote-neutralisation stay intact.
 *
 * Includes a guard-stripped sensitivity control that runs the OLD callback
 * so this suite cannot pass vacuously if the fix regresses to double
 * escaping.
 *
 * Stdlib node only. Run from anywhere:
 *     node ansible/roles/kanban_ui/tests/test_md_bare_url.js
 */
'use strict';

const path = require('path');
const md = require(path.join(__dirname, '..', 'files', 'app', 'static', 'md.js'));

let failures = 0;
function check(name, cond, detail) {
  if (cond) {
    console.log('PASS  ' + name);
  } else {
    failures += 1;
    console.log('FAIL  ' + name + (detail ? '  -> output was: ' + JSON.stringify(detail) : ''));
  }
}

const URL_SRC = 'see https://example.invalid/?a=1&b=2 done';

// 1. The finding's exact payload: href and label carry ONE escape pass.
{
  const out = md.md(URL_SRC);
  const href = /href="([^"]*)"/.exec(out);
  check('bare url: anchor rendered', href !== null, out);
  check('bare url: href single-escaped (&amp; present)', href && href[1].indexOf('&amp;') !== -1, out);
  check('bare url: href NOT double-escaped', href && href[1].indexOf('&amp;amp;') === -1, out);
  check('bare url: href query is b=2 after one browser decode', href && href[1].replace(/&amp;/g, '&') === 'https://example.invalid/?a=1&b=2', out);
  check('bare url: label single-escaped', (out.match(/&amp;amp;/g) || []).length === 0, out);
  check('bare url: label reuses escaped url text', out.indexOf('>https://example.invalid/?a=1&amp;b=2</a>') !== -1, out);
}

// 2. Pre-encoded source (&amp;amp; in the card body) round-trips to a single
//    escape pass in the href: browser-decoded query is amp;r=2, exactly once.
{
  const out = md.md('x https://ok.example/a?q=1&amp;amp;r=2');
  const href = /href="([^"]*)"/.exec(out);
  check('pre-encoded: href decoded-then-escaped once', href && href[1] === 'https://ok.example/a?q=1&amp;amp;r=2', out);
}

// 3. Scheme gate unchanged: non-https schemes never become hrefs.
{
  const out = md.md('click javascript:alert(1) and data:text/html,hi');
  check('scheme gate: no javascript: href', !/href="javascript/i.test(out), out);
  check('scheme gate: no data: href', !/href="data:/i.test(out), out);
  const out2 = md.md('[click](javascript:alert(1))');
  check('scheme gate: markdown link javascript: stays inert text', !/href="javascript/i.test(out2) && out2.indexOf('<a') === -1, out2);
}

// 4. Quote break-out of the href attribute stays neutralised (%22 via
//    safeUrl, then escaped once; no live attribute can form).
{
  const out = md.md('visit https://x.example/a"onmouseover="alert(1) now');
  const href = /href="([^"]*)"/.exec(out);
  check('break-out: quotes percent-encoded in href', href && href[1].indexOf('%22') !== -1, out);
  check('break-out: no live onmouseover attribute', !/<a[^>]*\sonmouseover/i.test(out), out);
  check('break-out: label quotes escaped', out.indexOf('&quot;') !== -1, out);
}

// 5. Sensitivity control (guard-stripped): the OLD callback must reproduce
//    the double-escape bug on the same input, proving checks 1/2 are real.
{
  // emulate step-3 state: text already esc()d, then old step-5 callback
  const escaped = md.esc('see https://example.invalid/?a=1&b=2 done');
  const oldOut = escaped.replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g,
    (_, pre, url) => pre + '<a href="' + md.esc(md.safeUrl(url) || "#") + '" target="_blank" rel="noopener">' + md.esc(url) + "</a>");
  check('control: old callback reproduces &amp;amp; bug', oldOut.indexOf('&amp;amp;') !== -1 && oldOut !== md.md(URL_SRC), oldOut);
}

if (failures) {
  console.log('\n' + failures + ' test(s) FAILED');
  process.exit(1);
}
console.log('\nall bare-url tests passed');
