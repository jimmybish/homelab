#!/usr/bin/env node
/* Regression test for kanban-ui column drop-target registration (app.js
 * renderInner column-create block).
 *
 * Proves the PR#13 round-8 review finding (inline comment 4354363915)
 * stays fixed. Verbatim:
 *
 *   "Every column is registered as a drop target, but moveTasks() submits
 *    the column name as the bulk status. The pinned Hermes API explicitly
 *    rejects `running` because it requires the dispatcher/claim path, and
 *    rejects `review` as an unknown mutation status. Dropping cards onto
 *    either column therefore fails instead of moving them. Keep those
 *    columns display-only by excluding them from drop-target registration."
 *
 * OPERATOR RULING 2026-10-24 (Jimmy): the live system is authoritative,
 * not the stale review-citation pin b3aa561f. Verified against the
 * actually-mounted Hermes checkout e06df9c2 (plugins/kanban plugin_api.py):
 * _bulk_apply_one refuses status "running" per-id ("Cannot set status to
 * 'running' directly; use the dispatcher/claim path"; reproduced live
 * against the deployed app) but "review" IS a real mutation
 * (_STATUS_HANDLERS implements "review" -> kanban_db.request_review; live
 * bulk review move returned ok:true). The API-accepted bulk mutation
 * surface is therefore todo|triage|scheduled|ready|review|blocked|done
 * plus archive:true (rendered `archived`). DROP_COLS mirrors exactly that
 * list; only `running` is display-only.
 *
 * The fix: renderInner registers dragover/dragleave/drop ONLY for columns
 * in DROP_COLS and tags every other column `col-display-only` (app.css
 * additionally pins the .drag-over highlight off for that class), so a
 * drag onto Running is a clean no-op that never constructs a /tasks/bulk
 * request, while Review stays a fully working drop target.
 *
 * Method (same vm-stub pattern as test_dispatch_controls_disabled.js):
 * app.js is loaded into a vm context with minimal DOM/fetch stubs and the
 * boot IIFE stripped; the top-level lexical bindings S and DROP_COLS are
 * exported to the host context. S.data is seeded with one task per column
 * (triage..done + archived) and render() builds the column elements;
 * every column node's addEventListener record and className are inspected.
 * A fired drop listener must produce exactly one /tasks/bulk fetch; the
 * display-only columns must have ZERO drag listeners and never a fetch.
 *
 *   1. DROP_COLS exports exactly the 8 API-accepted targets and excludes
 *      running (no silent drift of the shared list).
 *   2. running column node: col-display-only class, 0 dragover,
 *      0 dragleave, 0 drop listeners.
 *   3. All 8 supported columns (incl. review per operator ruling): exactly
 *      1 drop listener each, className WITHOUT col-display-only.
 *   4. Firing todo's and review's drop listeners (ids from dataTransfer)
 *      each constructs a /tasks/bulk request (supported targets move).
 *   5. Firing nothing on running + a drag-over simulation leaves
 *      the fetch log empty (clean no-op, no failing request).
 *   6. Sensitivity: revert ONLY the `droppable` gate to the pre-fix
 *      always-true shape -> running regains drag listeners and loses
 *      the class, proving the harness detects the reported bug.
 *
 * Stdlib node only (no npm install). Run from anywhere:
 *
 *     node ansible/roles/kanban_ui/tests/test_drop_target_registration.js
 *
 * Exit 0 = all pass. Any failure prints and exits 1.
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const APP_JS = path.join(__dirname, '..', 'files', 'app', 'static', 'app.js');
const MD_JS = path.join(__dirname, '..', 'files', 'app', 'static', 'md.js');
const APP_CSS = path.join(__dirname, '..', 'files', 'app', 'static', 'app.css');

/* Fixed shapes (needles; the sensitivity revert targets the gate). */
const FIXED_GATE = 'const droppable = DROP_COLS.has(name); // PR#13 finding 4354363915';
const BUGGY_GATE = 'const droppable = true; // pre-fix: every column is a drop target';
const SUPPORTED = ['triage', 'todo', 'scheduled', 'ready', 'review', 'blocked', 'done', 'archived'];
const DISPLAY_ONLY = ['running'];

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
    listeners: {},       // addEventListener record: {event: [fn...]}
    append(...k) { this.children.push(...k); },
    replaceChildren(...k) { this.children = k; },
    insertBefore(node, ref) {
      const i = this.children.indexOf(ref);
      if (i === -1) this.children.push(node); else this.children.splice(i, 0, node);
    },
    addEventListener(ev, fn) { (this.listeners[ev] || (this.listeners[ev] = [])).push(fn); },
    setAttribute(name, value) { this.attrs[name] = String(value); if (name === "value") this.value = String(value); },
    remove() {}, focus() {}, contains() { return false; },
    querySelector() { return elStub(); }, querySelectorAll() { return []; },
    textContent: '', innerHTML: '', hidden: false, value: '', checked: false,
  };
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

function dragListenerCount(el) {
  return ['dragover', 'dragleave', 'drop'].reduce((n, ev) => n + (el.listeners[ev] || []).length, 0);
}

function boardPayload() {
  let i = 0;
  const task = (status) => ({
    id: 't_' + (++i), title: status + ' task', status, assignee: 'dev',
    priority: 0, progress: null, link_counts: {}, comment_count: 0,
    warnings: null, tenant: null, created_at: 1760000000, completed_at: null,
    latest_summary: null,
  });
  const cols = [
    ...['triage', 'todo', 'scheduled', 'ready', 'running', 'blocked', 'review', 'done'].map(n => ({ name: n, tasks: [task(n)] })),
    { name: 'archived', tasks: [task('archived')] },
  ];
  return { board: 'testboard', columns: cols, now: 1760000100, tenants: [], latest_event_id: 0 };
}

function makeContext(src) {
  const calls = [];        // every fetch url seen
  const registry = {};     // persistent elements per querySelector selector
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
    fetch: (url) => {
      const u = String(url);
      calls.push(u);
      return new Promise(() => {}); // never settles; url capture is the point
    },
  };

  win.KanbanMd = require(MD_JS);

  // Strip the boot IIFE so nothing auto-fires at load time, and expose the
  // top-level lexical bindings S and DROP_COLS (script lexical scope is
  // invisible to the host context object otherwise).
  const boot = src.indexOf('(async function boot()');
  if (boot === -1) throw new Error('boot IIFE not found — app.js shape changed, update harness');
  const code = src.slice(0, boot) + '\n;globalThis.__S = S; globalThis.__DROP = [...DROP_COLS];';
  vm.createContext(ctx);
  vm.runInContext(code, ctx, { filename: 'app.js' });
  ctx.__calls = calls;
  ctx.__body = body;
  ctx.__el = sel => registry[sel];
  return ctx;
}

/* Render a board and return {ctx, boardEl, colsByName}. */
function renderBoard(src) {
  const ctx = makeContext(src);
  ctx.__S.data = boardPayload();
  ctx.__S.remount = true;
  ctx.render();
  const boardEl = ctx.__el('#board');
  return { ctx, boardEl };
}

const src = fs.readFileSync(APP_JS, 'utf8');
const css = fs.readFileSync(APP_CSS, 'utf8');

/* ---------------- 1. DROP_COLS is the API mutation surface ---------------- */
{
  const ctx = makeContext(src);
  const drop = [...(ctx.__DROP || [])].sort();
  check('DROP_COLS exports exactly the 8 API-accepted targets',
    JSON.stringify(drop) === JSON.stringify([...SUPPORTED].sort()), drop);
  check('DROP_COLS excludes only running', !drop.includes('running') && drop.includes('review'), drop);
}

/* ---------------- 2. running is display-only ---------------- */
{
  const { ctx, boardEl } = renderBoard(src);
  for (const name of DISPLAY_ONLY) {
    const col = findColumn(boardEl, name);
    check(`display-only column rendered (${name})`, !!col);
    if (!col) continue;
    check(`${name}: col-display-only class`, col.className.includes('col-display-only'), col.className);
    check(`${name}: zero drag listeners (dragover+dragleave+drop)`, dragListenerCount(col) === 0,
      ['dragover', 'dragleave', 'drop'].map(ev => (col.listeners[ev] || []).length));
  }
  /* ---------------- 5. dragging over it constructs NO request ---------------- */
  check('drag over running: no fetch constructed at all',
    !ctx.__calls.some(u => u.includes('/tasks/bulk')), ctx.__calls);
}

/* ---------------- 3. supported columns keep drop targets ---------------- */
{
  const { ctx, boardEl } = renderBoard(src);
  for (const name of SUPPORTED) {
    const col = findColumn(boardEl, name);
    check(`droppable column rendered (${name})`, !!col);
    if (!col) continue;
    check(`${name}: no col-display-only class`, !col.className.includes('col-display-only'), col.className);
    check(`${name}: exactly 1 drop + 1 dragover + 1 dragleave listener`,
      (col.listeners.drop || []).length === 1 && (col.listeners.dragover || []).length === 1 && (col.listeners.dragleave || []).length === 1,
      ['dragover', 'dragleave', 'drop'].map(ev => (col.listeners[ev] || []).length));
  }

  /* ---------------- 4. firing a supported drop submits /tasks/bulk ---------------- */
  let fired = 0;
  for (const target of ['todo', 'review']) {
    const col = findColumn(boardEl, target);
    const dropFn = col && (col.listeners.drop || [])[0];
    check(`${target} drop listener present`, typeof dropFn === 'function');
    if (!dropFn) continue;
    let prevented = false;
    dropFn({
      preventDefault() { prevented = true; },
      dataTransfer: { getData: (k) => (k === 'text/plain' ? JSON.stringify(['t_2']) : '') },
    });
    check(`${target} drop handler called preventDefault`, prevented);
    fired += 1;
  }
  check('supported drops (todo + review) construct one bulk-status request each',
    fired === 2 && ctx.__calls.filter(u => u.includes('/tasks/bulk')).length === 2, ctx.__calls);
}

/* ---------------- 6. css guard + sensitivity ---------------- */
{
  check('app.css pins the drag-over highlight off for display-only columns',
    css.includes('.column.col-display-only.drag-over'));

  check('needle lock: fixed droppable gate present in app.js', src.includes(FIXED_GATE));

  // Revert ONLY the gate -> pre-fix shape: every column becomes a drop target.
  const buggySrc = src.replace(FIXED_GATE, BUGGY_GATE);
  check('sensitivity setup: gate revert applied', buggySrc !== src && buggySrc.includes(BUGGY_GATE));
  const { ctx, boardEl } = renderBoard(buggySrc);
  for (const name of DISPLAY_ONLY) {
    const col = findColumn(boardEl, name);
    check(`sensitivity: pre-fix ${name} column had drag listeners (bug detected)`,
      !!col && dragListenerCount(col) === 3 && !col.className.includes('col-display-only'),
      col && [dragListenerCount(col), col.className]);
  }
}

console.log(failures === 0 ? '\nALL PASS' : `\n${failures} FAILURE(S)`);
process.exit(failures === 0 ? 0 : 1);
