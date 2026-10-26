#!/usr/bin/env node
/* Regression tests for keyboard-accessible task cards (app.js makeCard /
 * applyCard).
 *
 * Proves two PR#13 review findings stay fixed, verbatim.
 *
 * Round-6 inline comment 4331497750 (keyboard operability):
 *   "Task cards are non-focusable divs with only a click handler.
 *    Keyboard-only users can reach the selection checkboxes but cannot open
 *    task details, leaving the drawer's editing, comments, and attachments
 *    inaccessible. Add a focusable, labelled task-opening control with
 *    Enter/Space activation, keeping the bulk-selection checkbox
 *    independently operable."
 *
 * Round-8 Copilot review on t_9e8c5065 (MEDIUM, accessibility), which
 * rejected the first attempt that put role="button" on the whole card:
 *   "Do not put role=\"button\" (or a native button) on an ancestor of the
 *    checkbox. Use separate sibling controls inside a non-interactive card
 *    wrapper — e.g. a dedicated native task-opening <button type=\"button\">
 *    for the title/card content and the checkbox as its sibling. Keep the
 *    outer wrapper for drag/context-menu behavior."
 *   WAI-ARIA 1.2 makes button descendants presentational; W3C ACT rule
 *   307n5z forbids sequentially focusable descendants of elements with
 *   presentational children. https://www.w3.org/WAI/standards-guidelines/act/rules/307n5z/
 *
 * The accepted shape:
 *   1. makeCard emits a NON-interactive wrapper div (drag + contextmenu +
 *      mouse-click-to-open only): no tabindex, no role="button", no keydown
 *      listener that could hijack Space/Enter from inner controls;
 *   2. applyCard builds TWO SIBLING focusable controls directly under the
 *      wrapper: a native <button type="button" class="card-open"
 *      aria-label="Open task: <title>"> (free Enter/Space activation) and
 *      <input type="checkbox" class="card-check" aria-label="Select task for
 *      bulk actions: <title>">, so the checkbox never sits inside a
 *      button-role/node subtree and keeps its checkbox semantics;
 *   3. the open control stops its own click propagation so wrapper bubbling
 *      cannot double-open it; the checkbox stops propagation so selection
 *      never opens the drawer;
 *   4. button content is phrasing-only (<span>s), wrapper mouse click
 *      behavior and the legacy checkbox-target bail guard unchanged.
 *
 * Method (same vm harness style as test_card_menu_disabled.js): app.js is
 * loaded into a vm context with minimal DOM/fetch stubs; the boot IIFE is
 * stripped so nothing auto-fires. Element stubs RECORD parent links,
 * attributes and listeners, so this suite asserts the STRUCTURE the
 * renderer builds (ancestor roles, distinct labelled controls) rather than
 * only the listeners it registers — the round-8 finding was exactly that
 * listener-stub assertions can pass on an AX-invalid tree. Click events are
 * dispatched through a bubbling simulation that honours stopPropagation,
 * and native <button type="button"> is asserted as the Enter/Space
 * activation mechanism (browser-native; no synthetic keydown handlers to
 * fake).
 *
 *   1. STRUCTURE: wrapper has no tabindex/role=button and no keydown
 *      listener; the open control and checkbox are distinct labelled
 *      sibling children of the wrapper.
 *   2. STRUCTURE (ACT 307n5z): the checkbox has NO ancestor with
 *      role="button" or native <button> semantics.
 *   3. Open control is a native <button type="button"> labelled with the
 *      task title; checkbox keeps type=checkbox + its own accessible name.
 *   4. LABELS track the title and survive a no-change re-render (cardSig
 *      short-circuit intact).
 *   5. BEHAVIOR: click-through from the open control opens the drawer
 *      EXACTLY once (button stopPropagation prevents a wrapper double-open).
 *   6. BEHAVIOR: checkbox click toggles S.sel, stops propagation, never
 *      opens the drawer, and the checkbox needs no keydown handler to work.
 *   7. Mouse regression: wrapper click still opens; click targeting the
 *      checkbox still bails (existing guard).
 *   8. SENSITIVITY A (round-6 bug): remove the open control from the
 *      rendered children and assert the suite flags a card with no
 *      task-opening control.
 *   9. SENSITIVITY B (round-8 rejection): re-nest the checkbox under the
 *      button (round-7 shape, equivalently role=button on the wrapper) and
 *      assert the suite flags a focusable checkbox inside a button subtree.
 *      These prove this harness actually detects both rejected shapes.
 *
 * Stdlib node only (no npm install). Run from anywhere:
 *
 *     node ansible/roles/kanban_ui/tests/test_card_keyboard_access.js
 *
 * Exit 0 = all pass. Any failure prints and exits 1.
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const APP_JS = path.join(__dirname, '..', 'files', 'app', 'static', 'app.js');
const MD_JS = path.join(__dirname, '..', 'files', 'app', 'static', 'md.js');

// The exact shapes the fix introduced. If app.js is rewritten these throw and
// force a deliberate harness update (same convention as the other tests).
const WRAPPER = 'const el = h("div", { class: "card", draggable: "true", "data-id": id });';
const OPEN_BTN = 'const open = h("button", { type: "button", class: "card-open", "aria-label": "Open task: " + label, onclick: e => { e.stopPropagation(); openDrawer(t.id); } },';
const CHK = '"aria-label": "Select task for bulk actions: " + label';
const SIBLINGS = 'el.replaceChildren(chk, open);';
const CHECKBOX_GUARD = 'onclick: e => { e.stopPropagation(); if (e.target.checked) S.sel.add(t.id); else S.sel.delete(t.id); render(); }';

let failures = 0;
function check(name, cond, detail) {
  if (cond) {
    console.log('PASS  ' + name);
  } else {
    failures += 1;
    console.log('FAIL  ' + name + (detail !== undefined ? '  -> got: ' + JSON.stringify(detail) : ''));
  }
}

/* ---------------- stubs ---------------- */
// Parent links are recorded so structural assertions can walk the real tree
// the renderer built — the round-8 finding was listener stubs passing while
// the checkbox sat inside a presentational button subtree.
function elStub(tag) {
  const el = {
    nodeType: 1,
    tag: tag || 'div',
    children: [], style: {}, dataset: {},
    parent: null,
    classList: { _s: new Set(),
      add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); },
      toggle(c, on) { if (on === undefined) { this._s.has(c) ? this._s.delete(c) : this._s.add(c); } else if (on) this._s.add(c); else this._s.delete(c); },
      contains(c) { return this._s.has(c); } },
    attrs: {},           // setAttribute record: {name: value}
    listeners: {},       // addEventListener record: {event: [fn...]}
    append(...k) { this.children.push(...k); for (const c of k) if (c && typeof c === 'object') c.parent = this; },
    replaceChildren(...k) { this.children = k; for (const c of k) if (c && typeof c === 'object') c.parent = this; },
    addEventListener(ev, fn) { (this.listeners[ev] || (this.listeners[ev] = [])).push(fn); },
    setAttribute(name, value) { this.attrs[name] = String(value); },
    remove() {}, focus() {}, contains() { return false; },
    querySelector() { return elStub(); }, querySelectorAll() { return []; },
    textContent: '', innerHTML: '', hidden: false, value: '', checked: false,
  };
  return el;
}

function makeContext(src) {
  const win = { addEventListener() {} };
  const body = elStub('body');
  const ctx = {
    window: win,
    console: { error() {}, log() {} },
    URL, Set, Map, WeakMap, Object, Array, JSON, Math, Date, String, Number, Boolean, Error, Promise, RegExp,
    parseInt, parseFloat, isNaN, encodeURIComponent, decodeURIComponent,
    innerWidth: 1600, innerHeight: 900,
    setTimeout: () => 0, clearTimeout: () => {}, setInterval: () => 0, clearInterval: () => {},
    localStorage: { getItem: () => null, setItem() {} },
    location: { origin: 'https://kanban.example.internal', protocol: 'https:', host: 'kanban.example.internal', hostname: 'kanban.example.internal', pathname: '/' },
    WebSocket: function WS(url) { this.close = () => {}; this.readyState = 1; },
    document: {
      hidden: false, title: '', activeElement: null,
      addEventListener() {},
      createElement: (tag) => elStub(tag),
      createTextNode: t => ({ nodeType: 3, text: String(t) }),
      body, documentElement: { dataset: {} },
      querySelector: () => elStub(),
      querySelectorAll: () => [],
    },
    fetch: () => new Promise(() => {}), // never settles; unused here
  };
  win.KanbanMd = require(MD_JS);

  const boot = src.indexOf('(async function boot()');
  if (boot === -1) throw new Error('boot IIFE not found — app.js shape changed, update harness');
  const code = src.slice(0, boot) + '\n;globalThis.__S = S;';
  vm.createContext(ctx);
  vm.runInContext(code, ctx, { filename: 'app.js' });

  // Spies: the card handlers resolve openDrawer/render on the context global
  // at call time, so swapping them here captures every activation.
  ctx.__opened = [];
  ctx.__renders = 0;
  ctx.openDrawer = async (id) => { ctx.__opened.push(id); };
  ctx.render = async () => { ctx.__renders += 1; };
  ctx.__body = body;
  return ctx;
}

const task = () => ({
  id: 't-kb-1', title: 'Keyboard one', body: '', assignee: 'dev',
  status: 'todo', priority: 0, tenant: '', created_at: 1790000000, created_by: 'jimmy',
});

// Simulate a real click bubbling from `target` up through recorded parents,
// honouring stopPropagation — listener stubs that never model bubbling were
// part of why the round-7 structure passed while being AX-invalid.
function clickThrough(target) {
  const ev = { target, preventDefault() {}, _stopped: false, stopPropagation() { this._stopped = true; } };
  let node = target;
  while (node) {
    for (const fn of (node.listeners && node.listeners.click) || []) fn.call(node, ev);
    if (ev._stopped) return ev;
    node = node.parent;
  }
  return ev;
}

// h() assigns el.className directly, so class membership lives in BOTH
// className and classList._s (classList.toggle is used by applyCard).
function hasCls(node, cls) {
  if (!node) return false;
  if ((node.className || '').split(/\s+/).includes(cls)) return true;
  return !!(node.classList && node.classList._s && node.classList._s.has(cls));
}

function isButtonish(node) {
  return node.tag === 'button' || node.attrs.role === 'button';
}

function buttonAncestor(node) {
  for (let p = node && node.parent; p; p = p.parent) if (isButtonish(p)) return p;
  return null;
}

function buildCard(ctx, t) {
  ctx.__S.sel = new Set();
  const card = ctx.makeCard(t.id);
  ctx.applyCard(card, t, 1790000001);
  return card;
}

function openBtnOf(card) {
  const btn = card.children.find(c => c && c.tag === 'button' && hasCls(c, 'card-open'));
  if (!btn) throw new Error('card-open button missing — applyCard shape changed, update harness');
  return btn;
}

function checkboxOf(card) {
  const chk = card.children.find(c => c && c.tag === 'input' && hasCls(c, 'card-check'));
  if (!chk) throw new Error('card-check input missing from card children — applyCard shape changed, update harness');
  return chk;
}

async function main() {
  const src = fs.readFileSync(APP_JS, 'utf8');
  if (!src.includes(WRAPPER)) throw new Error('non-interactive card wrapper shape missing from app.js — fix regressed or was rewritten, update harness');
  if (!src.includes(OPEN_BTN)) throw new Error('native card-open button missing from app.js — fix regressed or was rewritten, update harness');
  if (!src.includes(CHK)) throw new Error('checkbox accessible name missing from app.js — fix regressed or was rewritten, update harness');
  if (!src.includes(SIBLINGS)) throw new Error('checkbox/open-control sibling render missing from app.js — fix regressed or was rewritten, update harness');
  if (!src.includes(CHECKBOX_GUARD)) throw new Error('checkbox stopPropagation handler changed — update harness');
  if (src.includes('role: "button"')) throw new Error('role="button" reintroduced in app.js — W3C ACT 307n5z forbids focusable descendants of presentational buttons');

  const t = task();

  /* ---- fixed behaviour: STRUCTURE ---- */
  const ctx = makeContext(src);
  const card = buildCard(ctx, t);
  const btn = openBtnOf(card);
  const chk = checkboxOf(card);

  check('card wrapper is not focusable (no tabindex)', !('tabindex' in card.attrs), card.attrs);
  check('card wrapper has no button role', !('role' in card.attrs), card.attrs);
  check('card wrapper is a div (non-interactive)', card.tag === 'div', card.tag);
  check('card wrapper registers no keydown listener (inner controls own their keys)', !(card.listeners.keydown || []).length, Object.keys(card.listeners));
  check('ACT 307n5z: checkbox has no role=button/native-button ancestor', buttonAncestor(chk) === null, chk.parent && { tag: chk.parent.tag, role: chk.parent.attrs.role });
  check('open control is a native <button type="button">', btn.tag === 'button' && btn.attrs.type === 'button', { tag: btn.tag, type: btn.attrs.type });
  check('open control is a labelled control', btn.attrs['aria-label'] === 'Open task: ' + t.title, btn.attrs);
  check('checkbox keeps native checkbox type', chk.attrs.type === 'checkbox', chk.attrs);
  check('checkbox has its own accessible name with the title', typeof chk.attrs['aria-label'] === 'string' && chk.attrs['aria-label'] === 'Select task for bulk actions: ' + t.title, chk.attrs);
  check('open control and checkbox are distinct labelled controls', btn !== chk && btn.attrs['aria-label'] !== chk.attrs['aria-label']);
  check('open control and checkbox are siblings under the wrapper', btn.parent === card && chk.parent === card && btn.parent === chk.parent);

  // Idempotent re-render: same sig must short-circuit; both labels survive.
  ctx.applyCard(card, task(), 1790000002);
  check('open-control label survives a no-change re-render', btn.attrs['aria-label'] === 'Open task: ' + t.title, btn.attrs['aria-label']);
  check('checkbox label survives a no-change re-render', chk.attrs['aria-label'] === 'Select task for bulk actions: ' + t.title, chk.attrs['aria-label']);

  /* ---- fixed behaviour: ACTIVATION ---- */
  // Native <button type=button> gives Enter/Space activation for free in
  // the browser; the harness models that as the button's own click and
  // proves bubbling cannot double-open through the wrapper handler.
  const evOpen = clickThrough(btn);
  check('activating the open control opens the drawer exactly once (no double-open via bubbling)', ctx.__opened.length === 1 && ctx.__opened[0] === t.id && evOpen._stopped === true, ctx.__opened);
  check('open control stops its own click propagation', evOpen._stopped === true);

  // Checkbox click toggles selection WITHOUT opening the drawer, and stops
  // propagation so the card click handler never sees it.
  chk.checked = true;
  const evChk = clickThrough(chk);
  check('checkbox click stops propagation', evChk._stopped === true);
  check('checkbox click toggles selection', ctx.__S.sel.has(t.id) === true, [...ctx.__S.sel]);
  check('checkbox click does NOT open the drawer', ctx.__opened.length === 1, ctx.__opened);
  check('checkbox needs no keydown handler (native toggle is independent of the card)', !(chk.listeners.keydown || []).length, Object.keys(chk.listeners));

  // Mouse regression: a plain click on the card body still opens...
  clickThrough(card);
  check('mouse click on the card still opens the drawer', ctx.__opened.length === 2 && ctx.__opened[1] === t.id, ctx.__opened);
  // ...while a click whose target is the checkbox still bails (existing guard).
  const chkTarget = elStub('input');
  chkTarget.classList.add('card-check');
  chkTarget.parent = card;
  clickThrough(chkTarget);
  check('click targeting the checkbox still bails', ctx.__opened.length === 2, ctx.__opened);

  /* ---- SENSITIVITY A: the round-6 bug (no task-open control) ---- */
  const noOpen = src.replace(SIBLINGS, 'el.replaceChildren(chk);');
  if (noOpen === src) throw new Error('sensitivity A patch did not apply — update harness');
  const ctxA = makeContext(noOpen);
  ctxA.__S.sel = new Set();
  const cardA = ctxA.makeCard(t.id);
  ctxA.applyCard(cardA, task(), 1790000001);
  const openMissing = !cardA.children.some(c => c && c.tag === 'button' && hasCls(c, 'card-open'));
  check('SENSITIVITY A: harness detects a card with no task-open control', openMissing === true, cardA.children.map(c => c && c.tag));

  /* ---- SENSITIVITY B: the round-7 rejection (checkbox inside a button) ---- */
  const nested = src.replace(SIBLINGS, 'open.append(chk); el.replaceChildren(open);');
  if (nested === src) throw new Error('sensitivity B patch did not apply — update harness');
  const ctxB = makeContext(nested);
  ctxB.__S.sel = new Set();
  const cardB = ctxB.makeCard(t.id);
  ctxB.applyCard(cardB, task(), 1790000001);
  const chkB = cardB.children.find(c => c && c.tag === 'input' && hasCls(c, 'card-check'))
    || (function descend(n) { for (const c of n.children || []) { if (c && c.tag === 'input' && hasCls(c, 'card-check')) return c; const r = descend(c); if (r) return r; } return null; })(cardB);
  check('SENSITIVITY B: harness detects the checkbox nested in a button subtree',
    !!chkB && buttonAncestor(chkB) !== null, chkB && { parentTag: chkB.parent && chkB.parent.tag });

  if (failures) { console.log('\n' + failures + ' FAILURE(S)'); process.exit(1); }
  console.log('\nALL PASS');
}

main().catch(e => { console.error('harness error:', e); process.exit(1); });
