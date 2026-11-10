#!/usr/bin/env node
/* Regression tests for the column-header select-all checkbox (app.js
 * renderInner column-create block + applyColumnCheck).
 *
 * Feature (change request t_95bcd740): a checkbox at the top of every board
 * column that selects/deselects every card CURRENTLY VISIBLE in that column
 * (search/tenant/assignee filters respected), integrating with the existing
 * per-card S.sel bulk-selection mechanism, and reflecting tri-state
 * (none / some / all) as cards are individually toggled.
 *
 * The header checkbox is built to the same accessibility contract the PR#13
 * round-8 finding pinned for .card-check (W3C ACT 307n5z / WAI-ARIA 1.2):
 * a native <input type="checkbox"> with its own accessible name, a SIBLING
 * of the title text and the col-add button, never a descendant of any
 * button-role/native-button node, so it keeps real checkbox semantics and
 * its own tab stop.
 *
 * checked / indeterminate / disabled are DOM PROPERTIES set by
 * applyColumnCheck on every render for the visible (post-filter) set — the
 * cached column nodes reuse the same input element, so property (not
 * attribute) assignment is what keeps per-card toggles in sync.
 *
 * Method (same vm-stub pattern as test_drop_target_registration.js): app.js
 * is loaded into a vm context with minimal DOM/fetch stubs, the boot IIFE
 * stripped, and S exported. S.data is seeded with two tasks per column;
 * render() builds the column tree; column nodes are found by data-col and
 * walked for their header controls. Checkbox clicks are dispatched through
 * the recorded click listeners with e.target = the input (the handler reads
 * e.target.checked, mirroring the per-card checkbox contract).
 *
 *   1. STRUCTURE: every column head contains exactly one input.col-check
 *      with type=checkbox + its own aria-label; it has NO button-role/
 *      native-button ancestor (ACT 307n5z); the col-add button is its
 *      SIBLING in the head, not its parent.
 *   2. STATE: fresh render -> all unchecked, not indeterminate; a column
 *      with zero visible tasks is disabled.
 *   3. BEHAVIOR (all): clicking the todo head checkbox with checked=true
 *      adds EXACTLY that column's visible ids to S.sel (other columns
 *      untouched) and re-renders; the checkbox shows checked afterwards.
 *   4. BEHAVIOR (clear): clicking with checked=false removes exactly that
 *      column's ids from S.sel, leaving foreign selections intact.
 *   5. TRI-STATE: selecting one card via its per-card checkbox flips the
 *      column checkbox to indeterminate (not checked); selecting the last
 *      card makes it checked; deselecting all makes it unchecked.
 *   6. FILTERS: with S.search active, select-all takes only the VISIBLE
 *      ids; a hidden task in the same column is never selected. The tri-
 *      state math also uses only the visible set (all visible selected =>
 *      checked even while hidden tasks are unselected).
 *   7. SENSITIVITY: revert only the applyColumnCheck call in renderInner ->
 *      the header checkbox never reflects selection (stays unchecked/not
 *      indeterminate), proving the harness detects a broken sync.
 *
 * Stdlib node only (no npm install). Run from anywhere:
 *
 *     node ansible/roles/kanban_ui/tests/test_column_select_all.js
 *
 * Exit 0 = all pass. Any failure prints and exits 1.
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const APP_JS = path.join(__dirname, '..', 'files', 'app', 'static', 'app.js');
const MD_JS = path.join(__dirname, '..', 'files', 'app', 'static', 'md.js');

/* Fixed shapes (needles). The sensitivity revert targets the sync call. */
const SYNC_CALL = 'applyColumnCheck(rec, tasks);';
const CHECK_FN = 'function applyColumnCheck(rec, tasks) {';
const HEAD_CHK = 'colChk,';

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
function elStub(tag) {
  const el = {
    nodeType: 1,
    tag: tag || 'div',
    children: [], style: {}, dataset: {},
    className: '',
    classList: {
      _set: new Set(),
      add(c) { this._set.add(c); },
      remove(c) { this._set.delete(c); },
      toggle(c, on) { if (on === undefined) on = !this._set.has(c); on ? this._set.add(c) : this._set.delete(c); },
      contains(c) { return this._set.has(c); },
    },
    attrs: {},           // setAttribute record: {name: value}
    listeners: {},       // addEventListener record: {event: [fn...]
    append(...k) { this.children.push(...k); for (const c of k) if (c && typeof c === 'object') c.parent = this; },
    replaceChildren(...k) { this.children = k; for (const c of k) if (c && typeof c === 'object') c.parent = this; },
    insertBefore(node, ref) {
      const i = this.children.indexOf(ref);
      if (i === -1) this.children.push(node); else this.children.splice(i, 0, node);
    },
    addEventListener(ev, fn) { (this.listeners[ev] || (this.listeners[ev] = [])).push(fn); },
    setAttribute(name, value) { this.attrs[name] = String(value); if (name === 'value') this.value = String(value); },
    remove() {}, focus() {}, contains() { return false; },
    querySelector() { return elStub(); }, querySelectorAll() { return []; },
    textContent: '', innerHTML: '', hidden: false, value: '',
    checked: false, indeterminate: false, disabled: false,
  };
  // parent links are recorded (append/replaceChildren above) so the ACT
  // 307n5z walk can inspect the real ancestor chain the renderer built.
  return el;
}

function findColumn(root, colName) {
  const out = [];
  const walk = (el) => {
    if (!el || el.nodeType !== 1) return;
    if (el.attrs && el.attrs['data-col'] === colName) out.push(el);
    for (const c of el.children || []) walk(c);
  };
  walk(root);
  return out[0];
}

function isButtonish(node) {
  return node.tag === 'button' || (node.attrs && node.attrs.role === 'button');
}
function buttonAncestor(node) {
  for (let p = node && node.parent; p; p = p.parent) if (isButtonish(p)) return p;
  return null;
}
function findDescendant(el, pred) {
  for (const c of el.children || []) {
    if (!c || c.nodeType !== 1) continue;
    if (pred(c)) return c;
    const d = findDescendant(c, pred);
    if (d) return d;
  }
  return null;
}
function colCheckOf(colEl) {
  const chk = findDescendant(colEl, c => c.tag === 'input' && ((c.className || '') + ' ' + [...(c.classList._set || [])]).includes('col-check'));
  if (!chk) throw new Error('col-check input missing from column head — app.js shape changed, update harness');
  return chk;
}
function cardCheckOf(colEl, id) {
  const card = findDescendant(colEl, c => c.tag === 'div' && c.attrs['data-id'] === id);
  if (!card) throw new Error('card ' + id + ' not rendered — update harness');
  const chk = (card.children || []).find(c => c && c.tag === 'input' && ((c.className || '') + ' ' + [...(c.classList._set || [])]).includes('card-check'));
  if (!chk) throw new Error('card-check missing on card ' + id + ' — update harness');
  return chk;
}

function boardPayload() {
  let i = 0;
  const mk = (status, title) => ({
    id: 't_' + (++i), title, status, assignee: 'dev',
    priority: 0, progress: null, link_counts: {}, comment_count: 0,
    warnings: null, tenant: null, created_at: 1760000000, completed_at: null,
    latest_summary: null,
  });
  const cols = [
    ...['triage', 'todo', 'scheduled', 'ready', 'running', 'blocked', 'review', 'done'].map(
      n => ({ name: n, tasks: [mk(n, n + ' alpha'), mk(n, n + ' beta')] })),
    { name: 'archived', tasks: [mk('archived', 'archived alpha'), mk('archived', 'archived beta')] },
  ];
  return { board: 'testboard', columns: cols, now: 1760000100, tenants: [], latest_event_id: 0 };
}

function makeContext(src) {
  const calls = [];
  const registry = {};
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
    WebSocket: function WS(url) { calls.push('ws:' + url); this.close = () => {}; this.readyState = 1; },
    document: {
      hidden: false, title: '', activeElement: null,
      addEventListener() {},
      createElement: (tag) => elStub(tag),
      createTextNode: t => ({ nodeType: 3, text: String(t) }),
      body, documentElement: { dataset: {} },
      querySelector: sel => (registry[sel] || (registry[sel] = elStub())),
      querySelectorAll: () => [],
    },
    fetch: (url) => { calls.push(String(url)); return new Promise(() => {}); },
  };
  win.KanbanMd = require(MD_JS);

  const boot = src.indexOf('(async function boot()');
  if (boot === -1) throw new Error('boot IIFE not found — app.js shape changed, update harness');
  const code = src.slice(0, boot) + '\n;globalThis.__S = S;';
  vm.createContext(ctx);
  vm.runInContext(code, ctx, { filename: 'app.js' });
  ctx.__calls = calls;
  ctx.__body = body;
  ctx.__el = sel => registry[sel];
  return ctx;
}

function renderBoard(src) {
  const ctx = makeContext(src);
  ctx.__S.data = boardPayload();
  ctx.__S.remount = true;
  ctx.render();
  return ctx;
}

/* Fire the header checkbox's click listeners the way a real click would
   (target = the input; the handler reads e.target.checked). */
function fireClick(node, ev) {
  for (const fn of (node.listeners.click || [])) fn.call(node, ev);
}
function clickCheckbox(chk, checked) {
  chk.checked = checked; // native behaviour precedes the click handler
  fireClick(chk, { target: chk, preventDefault() {}, stopPropagation() {} });
}

const src = fs.readFileSync(APP_JS, 'utf8');

/* ---------------- 0. fixed shapes ---------------- */
check('applyColumnCheck exists', src.includes(CHECK_FN));
check('renderInner syncs column checkbox state', src.includes(SYNC_CALL));
check('header checkbox rendered in col-head', src.includes(HEAD_CHK));

/* ---------------- 1. STRUCTURE ---------------- */
{
  const ctx = renderBoard(src);
  const boardEl = ctx.__el('#board');
  const names = ['triage', 'todo', 'scheduled', 'ready', 'running', 'blocked', 'review', 'done', 'archived'];
  let structOk = true, actOk = true, labelOk = true, sibOk = true;
  for (const n of names) {
    const colEl = findColumn(boardEl, n);
    if (!colEl) { structOk = false; continue; }
    const chk = colCheckOf(colEl);
    if (chk.attrs.type !== 'checkbox') structOk = false;
    if (!chk.attrs['aria-label'] || !chk.attrs['aria-label'].toLowerCase().includes(n)) labelOk = false;
    if (buttonAncestor(chk) !== null) actOk = false;
    // col-add (when present) must be a sibling inside the head, not an ancestor
    const add = findDescendant(colEl, c => c.tag === 'button' && (c.className || '').includes('col-add'));
    if (add && add.parent !== chk.parent) sibOk = false;
    if (add && chk.parent && chk.parent.children.indexOf(chk) >= chk.parent.children.indexOf(add)) sibOk = false;
  }
  check('every column head has a native col-check input', structOk);
  check('ACT 307n5z: col-check has no button ancestor in any column', actOk);
  check('col-check aria-label names the column', labelOk);
  check('col-add stays a sibling of col-check (checkbox first)', sibOk);
}

/* ---------------- 2. STATE: fresh render + empty column ---------------- */
{
  const ctx = renderBoard(src);
  const boardEl = ctx.__el('#board');
  const todoChk = colCheckOf(findColumn(boardEl, 'todo'));
  check('fresh render: col-check unchecked', todoChk.checked === false);
  check('fresh render: col-check not indeterminate', todoChk.indeterminate === false);
  check('non-empty column: col-check enabled', todoChk.disabled === false);
  // empty the ready column and re-render
  ctx.__S.data.columns.find(c => c.name === 'ready').tasks = [];
  ctx.render();
  const readyChk = colCheckOf(findColumn(ctx.__el('#board'), 'ready'));
  check('empty column: col-check disabled', readyChk.disabled === true);
}

/* ---------------- 3. BEHAVIOR: select-all adds exactly the column ids ---- */
{
  const ctx = renderBoard(src);
  let boardEl = ctx.__el('#board');
  const todoIds = ctx.__S.data.columns.find(c => c.name === 'todo').tasks.map(t => t.id);
  const clickChk = colCheckOf(findColumn(boardEl, 'todo'));
  // a foreign selection that must survive
  ctx.__S.sel.add('t_foreign');
  clickCheckbox(clickChk, true);
  boardEl = ctx.__el('#board');
  const sel = ctx.__S.sel;
  const allIds = ctx.__S.data.columns.flatMap(c => c.tasks.map(t => t.id));
  const otherIds = allIds.filter(id => !todoIds.includes(id));
  check('select-all adds every todo id', todoIds.every(id => sel.has(id)), [...sel]);
  check('select-all leaves other columns unselected', otherIds.every(id => !sel.has(id)), [...sel].filter(id => otherIds.includes(id)));
  check('select-all preserves foreign selection', sel.has('t_foreign'));
  const reChk = colCheckOf(findColumn(boardEl, 'todo'));
  check('column checkbox reads checked after select-all', reChk.checked === true);
  check('column checkbox not indeterminate when all selected', reChk.indeterminate === false);
  check('bulk selection visible to bulk bar (S.sel.size)', sel.size === todoIds.length + 1);

  /* ---------------- 4. BEHAVIOR: deselect removes exactly the column ids -- */
  clickCheckbox(reChk, false);
  const sel2 = ctx.__S.sel;
  check('deselect removes every todo id', todoIds.every(id => !sel2.has(id)), [...sel2]);
  check('deselect preserves foreign selection', sel2.has('t_foreign'));
  const offChk = colCheckOf(findColumn(ctx.__el('#board'), 'todo'));
  check('column checkbox reads unchecked after deselect', offChk.checked === false && offChk.indeterminate === false);
}

/* ---------------- 5. TRI-STATE via per-card toggles ---------------- */
{
  const ctx = renderBoard(src);
  let boardEl = ctx.__el('#board');
  const col = ctx.__S.data.columns.find(c => c.name === 'ready');
  const [a, b] = col.tasks.map(t => t.id);
  clickCheckbox(cardCheckOf(findColumn(boardEl, 'ready'), a), true);   // one of two
  let chk = colCheckOf(findColumn(ctx.__el('#board'), 'ready'));
  check('one of two selected: indeterminate, not checked', chk.indeterminate === true && chk.checked === false);
  boardEl = ctx.__el('#board');
  clickCheckbox(cardCheckOf(findColumn(boardEl, 'ready'), b), true);   // both
  chk = colCheckOf(findColumn(ctx.__el('#board'), 'ready'));
  check('all selected: checked, not indeterminate', chk.checked === true && chk.indeterminate === false);
  // header deselect then per-card uncheck the other one -> nothing selected
  clickCheckbox(chk, false);
  boardEl = ctx.__el('#board');
  clickCheckbox(cardCheckOf(findColumn(boardEl, 'ready'), a), false);
  chk = colCheckOf(findColumn(ctx.__el('#board'), 'ready'));
  check('none selected after clear: unchecked, not indeterminate', chk.checked === false && chk.indeterminate === false);
}

/* ---------------- 6. FILTERS: select-all takes visible ids only ---------- */
{
  const ctx = renderBoard(src);
  ctx.__S.search = 'alpha'; // hides every "* beta" card
  ctx.render();
  const boardEl = ctx.__el('#board');
  const col = ctx.__S.data.columns.find(c => c.name === 'todo');
  const visible = col.tasks.filter(t => t.title.toLowerCase().includes('alpha')).map(t => t.id);
  const hidden = col.tasks.filter(t => !visible.includes(t.id)).map(t => t.id);
  clickCheckbox(colCheckOf(findColumn(boardEl, 'todo')), true);
  const sel = ctx.__S.sel;
  check('filtered select-all takes visible ids', visible.every(id => sel.has(id)), [...sel]);
  check('filtered select-all skips hidden cards', hidden.every(id => !sel.has(id)), [...sel]);
  const chk = colCheckOf(findColumn(ctx.__el('#board'), 'todo'));
  check('tri-state uses visible set only (checked while hidden unselected)', chk.checked === true && chk.indeterminate === false);
}

/* ---------------- 7. SENSITIVITY: drop the sync call -------------------- */
{
  if (!src.includes(SYNC_CALL)) throw new Error('sync call needle missing — update harness');
  const broken = src.replace(SYNC_CALL, '/* sensitivity: sync removed */');
  let detected = false;
  try {
    const ctx = renderBoard(broken);
    const todoIds = ctx.__S.data.columns.find(c => c.name === 'todo').tasks.map(t => t.id);
    clickCheckbox(cardCheckOf(findColumn(ctx.__el('#board'), 'todo'), todoIds[0]), true);
    const chk = colCheckOf(findColumn(ctx.__el('#board'), 'todo'));
    detected = !(chk.indeterminate === true); // broken build never updates state
  } catch (e) {
    detected = true; // harness threw on the broken shape; still a detection
  }
  check('SENSITIVITY: harness detects a column checkbox that never syncs', detected);
}

console.log(failures ? '\n' + failures + ' FAILURE(S)' : '\nALL PASS');
process.exit(failures ? 1 : 0);
