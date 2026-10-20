#!/usr/bin/env node
/* Regression test for the kanban-ui card context menu (app.js cardMenuItems
 * + openCardMenu).
 *
 * Proves the PR#13 round-4 review finding (inline comment 4321367961) stays
 * fixed. Verbatim:
 *
 *   "openCardMenu() checks `it.disabled` for truthiness, so this empty
 *    string leaves the running task's Ready action enabled. Clicking it
 *    throws `TypeError: it.fn is not a function` because the item
 *    intentionally has no callback. Use a boolean flag in the menu model;
 *    the renderer already converts it to an HTML disabled attribute."
 *
 * The bug had two halves:
 *   1. menu model: the running task's Ready item carried `disabled: ""`
 *      (falsy) so the renderer emitted no `disabled` attribute -> clickable;
 *   2. renderer: `onclick: () => { closeMenus(); it.fn(); }` was unguarded,
 *      so a click on the no-callback item threw a TypeError.
 * The fix carries a real boolean `disabled: true` in the model AND guards
 * the click handler (`if (it.disabled || typeof it.fn !== "function")
 * return;`) so no-callback items can never reach `it.fn()`.
 *
 * Method: app.js is loaded into a vm context with minimal DOM/fetch stubs
 * (same pattern as test_model_options_race.js); the boot IIFE is stripped so
 * nothing auto-fires. Function declarations are hoisted onto the sandbox, so
 * openCardMenu/cardMenuItems are directly callable. Stubs record
 * setAttribute and addEventListener per element, so we can assert the real
 * attributes the renderer set and invoke the registered click handler the
 * way a real click would.
 *
 *   1. openCardMenu(running task) -> walk the appended menu.
 *   2. Assert the "→ Ready (disabled in the WebUI)" button carries the HTML
 *      disabled attribute and a title, and has no visible enabled twin.
 *   3. Invoke its registered click handler -> assert it is inert: no throw
 *      (the TypeError path is unreachable), no fetch fired, menu not closed.
 *   4. Sanity: an enabled item ("→ Todo") still works — its handler fires
 *      moveTasks and a /tasks/bulk fetch is seen.
 *   5. Model check: the Ready item's flag is strictly === true (boolean).
 *   6. Sensitivity: revert the boolean to `disabled: ""` AND strip the
 *      handler guard; the same click MUST throw the TypeError, proving the
 *      harness detects the original bug.
 *
 * Stdlib node only (no npm install). Run from anywhere:
 *
 *     node ansible/roles/kanban_ui/tests/test_card_menu_disabled.js
 *
 * Exit 0 = all pass. Any failure prints and exits 1.
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const APP_JS = path.join(__dirname, '..', 'files', 'app', 'static', 'app.js');
const MD_JS = path.join(__dirname, '..', 'files', 'app', 'static', 'md.js');

const READY_LABEL = '→ Ready (disabled in the WebUI)';
const MODEL_FIX = 'disabled: true, title: "Requeuing a running task would release the worker claim';
const GUARD = 'onclick: () => { if (it.disabled || typeof it.fn !== "function") return; closeMenus(); it.fn(); }';
const OLD_ONCLICK = 'onclick: () => { closeMenus(); it.fn(); }';

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
    classList: { add() {}, remove() {}, toggle() {} },
    attrs: {},           // setAttribute record: {name: value}
    listeners: {},       // addEventListener record: {event: [fn...]}
    append(...k) { this.children.push(...k); },
    replaceChildren(...k) { this.children = k; },
    addEventListener(ev, fn) { (this.listeners[ev] || (this.listeners[ev] = [])).push(fn); },
    setAttribute(name, value) { this.attrs[name] = String(value); },
    remove() {}, focus() {}, contains() { return false; },
    querySelector() { return elStub(); }, querySelectorAll() { return []; },
    textContent: '', innerHTML: '', hidden: false, value: '', checked: false,
  };
  return el;
}

function makeContext(src) {
  const calls = [];        // every fetch url seen
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
      querySelector: () => elStub(),
      querySelectorAll: () => [],
    },
    fetch: (url) => { calls.push(String(url)); return new Promise(() => {}); }, // never settles; only counted
  };
  win.KanbanMd = require(MD_JS);

  // Strip the boot IIFE so nothing auto-fires at load time, and expose the
  // top-level lexical binding `S` (script lexical scope is invisible to the
  // host context object otherwise).
  const boot = src.indexOf('(async function boot()');
  if (boot === -1) throw new Error('boot IIFE not found — app.js shape changed, update harness');
  const code = src.slice(0, boot) + '\n;globalThis.__S = S;';
  vm.createContext(ctx);
  vm.runInContext(code, ctx, { filename: 'app.js' });
  ctx.__calls = calls;
  ctx.__body = body;
  return ctx;
}

const tick = () => new Promise(r => setImmediate(r));

/* Flatten the menu the way the renderer built it: each h() button carries
 * className, attrs and (string) text children. */
function walk(node, out) {
  if (!node || typeof node !== 'object') return;
  if (node.className) {
    const first = node.children && node.children[0];
    const text = typeof first === 'string' ? first : (first && first.text) || '';
    out.push({ cls: String(node.className), text, node });
  }
  for (const k of (node.children || [])) walk(k, out);
}

/* Open the card menu for `task` and return the button rows of the appended
 * menu (the first div appended to body; scrim+menu order matters: menu is
 * appended before the scrim). */
function openMenu(ctx, task) {
  ctx.__body.children.length = 0;
  ctx.__S.data = { columns: [{ tasks: [task] }] };
  ctx.__S.sel = new Set();
  ctx.openCardMenu(task, 100, 100);
  const menu = ctx.__body.children[0];
  if (!menu) throw new Error('openCardMenu appended nothing to body');
  const out = [];
  walk(menu, out);
  return out;
}

const runningTask = () => ({
  id: 'task-run', title: 'Running one', body: '', assignee: 'dev',
  status: 'running', priority: 0, tenant: '', created_at: 1790000000, created_by: 'jimmy',
});

function textOf(rows, label) { return rows.find(r => r.text === label); }

async function main() {
  const src = fs.readFileSync(APP_JS, 'utf8');
  if (!src.includes(MODEL_FIX)) throw new Error('boolean disabled flag missing from app.js — fix regressed or was rewritten, update harness');
  if (!src.includes(GUARD)) throw new Error('click guard missing from app.js — fix regressed or was rewritten, update harness');

  /* ---- fixed behaviour ---- */
  const ctx = makeContext(src);
  const rows = openMenu(ctx, runningTask());

  const ready = textOf(rows, READY_LABEL);
  check('running task menu shows the disabled Ready item', !!ready);
  check('Ready button carries the HTML disabled attribute',
    !!(ready && 'disabled' in ready.node.attrs), ready && ready.node.attrs);
  check('Ready button carries the reclaim guidance title',
    !!(ready && /hermes kanban reclaim/.test(ready.node.attrs.title || '')), ready && ready.node.attrs);
  check('menu model Ready flag is strictly boolean true',
    ctx.cardMenuItems(runningTask()).find(i => i.label === READY_LABEL).disabled === true);

  // The TypeError path: invoke the registered click handler exactly as a
  // real click would (disabled in HTML only stops real user input; our
  // handler must be inert even when driven directly).
  const handlers = (ready && ready.node.listeners.click) || [];
  check('Ready button has a registered click handler', handlers.length === 1, handlers.length);
  let threw = null;
  try { handlers[0]({ target: ready.node }); } catch (e) { threw = String((e && e.message) || e); }
  check('clicking Ready does NOT throw (no it.fn TypeError)', threw === null, threw);
  check('clicking Ready fires no fetch', ctx.__calls.length === 0, ctx.__calls);
  check('clicking Ready does not close the menu', !!ctx.window.__menuClose);

  // The sibling Ready action must not exist enabled on a running card.
  check('no enabled duplicate Ready action',
    rows.filter(r => /^→ Ready/.test(r.text)).length === 1, rows.filter(r => /^→ Ready/.test(r.text)).map(r => r.text));

  // Sanity: enabled items still work end to end.
  const todo = textOf(rows, '→ Todo');
  check('enabled "→ Todo" item present without disabled attr',
    !!todo && !('disabled' in todo.node.attrs), todo && todo.node.attrs);
  todo.node.listeners.click[0]({ target: todo.node });
  await tick(); await tick();
  check('clicking "→ Todo" fires the /tasks/bulk call',
    ctx.__calls.some(u => u.includes('/tasks/bulk')), ctx.__calls);
  check('enabled click closes the menu', !ctx.window.__menuClose);

  /* ---- sensitivity: revert both halves of the fix; the harness MUST
     detect the original TypeError ---- */
  let buggy = src.replace(MODEL_FIX, MODEL_FIX.replace('disabled: true,', 'disabled: "",'));
  if (buggy === src) throw new Error('sensitivity: could not revert model flag — pattern drift');
  buggy = buggy.replace(GUARD, OLD_ONCLICK);
  if (buggy.includes(GUARD)) throw new Error('sensitivity: could not strip click guard — pattern drift');
  const bad = makeContext(buggy);
  const badRows = openMenu(bad, runningTask());
  const badReady = textOf(badRows, READY_LABEL);
  check('sensitivity: reverted model drops the disabled attribute',
    !!badReady && !('disabled' in badReady.node.attrs), badReady && badReady.node.attrs);
  let badThrew = null;
  try { badReady.node.listeners.click[0]({ target: badReady.node }); } catch (e) { badThrew = String((e && e.message) || e); }
  check('sensitivity: unguarded click DOES throw it.fn TypeError',
    badThrew !== null && /it\.fn is not a function/.test(badThrew), badThrew);

  console.log(failures === 0 ? '\nALL PASS' : `\n${failures} FAILURE(S)`);
  process.exit(failures === 0 ? 0 : 1);
}

main().catch(e => { console.error('HARNESS ERROR:', e); process.exit(1); });
