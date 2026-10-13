#!/usr/bin/env node
/* Security regression tests for the kanban-ui Markdown renderer (md.js).
 *
 * These prove the round-1 review finding "stored XSS via raw Markdown link
 * labels restored after escaping" stays fixed: hostile labels and hostile
 * raw HTML in task bodies, results, and comments cannot create elements,
 * attributes, or scripts in the HTML that app.js assigns to innerHTML.
 *
 * Stdlib node only (no npm install). Run from anywhere:
 *
 *     node ansible/roles/kanban_ui/tests/test_md_renderer.js
 *
 * Exit 0 = all pass. Any failure prints and exits 1.
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

// Every "<tag ...>" opening in renderer output must be attribute-free except
// anchors, whose attributes must be exactly the href/target/rel whitelist.
// Quoted attribute values are stripped before scanning so payload text that
// merely LOOKS like an attribute inside href/label text is not a finding.
const ALLOWED_ANCHOR_ATTRS = new Set(['href', 'target', 'rel']);
function tagAttrs(tag) {
  const stripped = tag.replace(/"[^"]*"/g, '""');
  return [...stripped.matchAll(/([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=/g)].map(m => m[1].toLowerCase());
}
function attributeAudit(out) {
  const problems = [];
  const openTags = out.match(/<[a-zA-Z][^>]*>/g) || [];
  for (const tag of openTags) {
    const name = /^<([a-zA-Z][a-zA-Z0-9-]*)/.exec(tag)[1].toLowerCase();
    const attrs = tagAttrs(tag);
    if (name === 'a') {
      for (const a of attrs) if (!ALLOWED_ANCHOR_ATTRS.has(a)) problems.push(tag);
    } else if (attrs.length !== 0) {
      problems.push(tag); // any other tag carrying attributes
    }
    if (!md.SAFE_TAGS.test(name) && name !== 'a' && name !== 'li' && name !== 'ul' && name !== 'ol' && name !== 'pre' && name !== 'code') {
      problems.push(tag);
    }
  }
  return problems;
}
// Handler names must never appear in markup-tag structure. Quoted attribute
// values are stripped first: an entity-escaped payload riding inside an href
// value is inert text, not an attribute. A raw (unescaped) quote inside a tag
// is itself a finding, since it could close the attribute.
function handlerInTags(out, handler) {
  const tags = out.match(/<[a-zA-Z][^>]*>/g) || [];
  if (tags.some(t => /"/.test(t.replace(/"[^"]*"/g, "")))) return true;
  return tags.some(t => new RegExp(handler, 'i').test(t.replace(/"[^"]*"/g, '""')));
}

// 1. Hostile link label: image tag with event handler (the exact finding).
{
  const out = md.md('[<img src=x onerror=alert(1)>](https://allowed.example)');
  check('link label: no <img element', out.indexOf('<img') === -1, out);
  check('link label: no onerror attribute', !handlerInTags(out, 'onerror'), out);
  check('link label: attribute audit clean', attributeAudit(out).length === 0, out);
  check('link label: label rendered escaped as text', /&lt;img/.test(out), out);
}

// 2. Link label attempting attribute break-out of the generated anchor.
{
  const out = md.md('[a"><b onmouseover=alert(1)>x</b>](https://allowed.example)');
  check('label break-out: no onmouseover', !handlerInTags(out, 'onmouseover'), out);
  check('label break-out: audit clean', attributeAudit(out).length === 0, out);
  check('label break-out: exactly one anchor', (out.match(/<a /g) || []).length === 1, out);
}

// 3. Raw script in body (task body / result / comment payloads).
{
  const out = md.md('<script>alert(1)</script>');
  check('body: no <script> element', out.indexOf('<script') === -1, out);
  check('body: payload escaped', out.indexOf('&lt;script&gt;') !== -1, out);
}

// 4. Tag with attributes that is otherwise whitelisted must NOT be restored.
{
  const out = md.md('<b onclick=alert(1)>bold</b>');
  check('attributed safe tag not revived: no <b onclick', !/<b[ >]/.test(out) || !/onclick/i.test(out), out);
  check('attributed safe tag: audit clean', attributeAudit(out).length === 0, out);
}

// 5. Double-encoded payload must not decode twice into live markup.
{
  const out = md.md('&amp;lt;img src=x onerror=alert(1)&amp;gt;');
  check('double encoding: no live <img', out.indexOf('<img') === -1, out);
  check('double encoding: audit clean', attributeAudit(out).length === 0, out);
}

// 6. javascript: URL in a markdown link must never become an href.
{
  const out = md.md('[click](javascript:alert(1))');
  check('javascript: href blocked', !/href="javascript/i.test(out), out);
  check('javascript: audit clean', attributeAudit(out).length === 0, out);
}

// 7. Bare autolink with a quote to break the href attribute.
{
  const out = md.md('visit https://x.example/a"onmouseover="alert(1) now');
  check('autolink quote break-out: no injected attribute', !handlerInTags(out, 'onmouseover'), out);
  check('autolink: audit clean', attributeAudit(out).length === 0, out);
}

// 8. Code fence content stays escaped (one pass) and cannot smuggle tags.
{
  const out = md.md('```\n<img src=x onerror=alert(1)>\n```');
  check('code fence: no live <img', out.indexOf('<img') === -1, out);
  check('code fence: wrapped in <pre>', out.indexOf('<pre>') !== -1, out);
}

// 9. List-item continuation lines cannot introduce attributes.
{
  const out = md.md('- a\n  <img src=x onerror=alert(1)>');
  check('list continuation: no live <img', out.indexOf('<img') === -1, out);
  check('list continuation: audit clean', attributeAudit(out).length === 0, out);
}

// 10. Round-trip sanity: normal content still renders (no over-escaping).
{
  const out = md.md('**done** on [board](https://kanban.example) & more');
  check('normal: bold renders', out.indexOf('<b>done</b>') !== -1, out);
  check('normal: anchor renders', /<a href="https:\/\/kanban\.example"/.test(out), out);
  check('normal: ampersand escaped', out.indexOf('&amp;') !== -1, out);
}

// 11. app.js guards: the innerHTML sink is fed only through the checked
// md() wrapper (source-level contract: md.js escapes link labels and app.js
// never assigns unescaped task fields to innerHTML).
{
  const fs = require('fs');
  const appjs = fs.readFileSync(path.join(__dirname, '..', 'files', 'app', 'static', 'app.js'), 'utf8');
  check('app.js: html sink only via md() wrapper', !/innerHTML/.test(appjs.replace(/el\.innerHTML = v;/, '')), 'extra innerHTML usage found');
  check('md.js: link label escaped before preservation', /keep\('<a href="' \+ esc\(safeUrl\(url\) \|\| "#"\) \+ '" target="_blank" rel="noopener">' \+ esc\(label\)/.test(fs.readFileSync(path.join(__dirname, '..', 'files', 'app', 'static', 'md.js'), 'utf8')), 'label no longer esc()d at preservation time');
}

if (failures) {
  console.log('\n' + failures + ' test(s) FAILED');
  process.exit(1);
}
console.log('\nall md renderer tests passed');
