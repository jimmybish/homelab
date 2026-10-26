#!/usr/bin/env node
/* Regression test for the kanban-ui drawer actions footer (app.js
 * footerActionButtons + renderDrawer).
 *
 * Proves the PR#13 round-4 review finding (inline comment 4321368019) stays
 * fixed. Verbatim:
 *
 *   "The running task's Ready item is among these first five actions, but
 *    the footer drops its disabled state and explanation. It renders an
 *    enabled button whose callback calls the missing `it.fn`, causing a
 *    TypeError. Pass disabled/title through `h()` here as well; fixing the
 *    context-menu flag alone does not fix this renderer."
 *
 * The footer renders cardMenuItems(t).filter(!sep).slice(0,5); for a RUNNING
 * task position 5 (index 4) is the no-callback item
 * { label: "→ Ready (disabled in the WebUI)", disabled: true, title: ... }.
 * The old renderer ignored disabled/title and did `onclick: () => it.fn()`
 * (TypeError on click). The fix renders the footer through the same contract
 * as openCardMenu: HTML disabled attribute + tooltip from the boolean model
 * flag, plus an inert click guard.
 *
 * Method (same vm-stub pattern as test_card_menu_disabled.js and
 * test_model_options_race.js): app.js is loaded into a vm context with
 * minimal DOM/fetch stubs and the boot IIFE stripped. Function declarations
 * are hoisted onto the sandbox, so footerActionButtons/openCardMenu are
 * directly callable, and openDrawer is driven end-to-end by hand-settling
 * the /tasks/<id> and /model-options fetches so the REAL drawer (including
 * its .drawer-actions footer) is built. Stubs record setAttribute and
 * addEventListener per element, so we assert the actual attributes the
 * renderer set and invoke the registered click handler the way a real click
 * would.
 *
 *   1. Full openDrawer(running task) -> locate the .drawer-actions footer.
 *   2. Footer Ready button carries the HTML disabled attribute + reclaim
 *      title; no enabled duplicate Ready exists in the footer.
 *   3. Invoking its click handler is inert: no throw (TypeError path
 *      unreachable), no extra fetch, no menu opened.
 *   4. Enabled footer sibling ("→ Todo") has no disabled attr and its click
 *      fires the /tasks/bulk call.
 *   5. Consistency: openCardMenu for the SAME running task renders the Ready
 *      item with identical disabled/title attributes — footer and context
 *      menu cannot drift.
 *   6. Direct model check: footerActionButtons(running) yields exactly the
 *      first five non-sep items with the Ready button flagged.
 *   7. Sensitivity: revert ONLY the footer renderer to the pre-fix shape
 *      (no disabled/title attrs, unguarded onclick) -> the same click MUST
 *      throw "it.fn is not a function", proving the harness detects the
 *      reported bug.
 *
 * Stdlib node only (no npm install). Run from anywhere:
 *
 *     node ansible/roles/kanban_ui/tests/test_footer_actions_disabled.js
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
/* The exact fixed renderer body (asserted present so a rewrite forces a
 * harness review, and used as the sensitivity-revert needle). */
const FIXED_BODY = [
  'function footerActionButtons(t) {',
  '  return cardMenuItems(t).filter(i => !i.sep).slice(0, 5).map(it => h("button", {',
  '    class: "btn btn-ghost btn-sm" + (it.danger ? " danger" : ""),',
  '    ...(it.disabled ? { disabled: "" } : {}), ...(it.title ? { title: it.title } : {}),',
  '    onclick: () => { if (it.disabled || typeof it.fn !== "function") return; it.fn(); }',
  '  }, it.label));',
  '}',
].join('\n');
const BUGGY_BODY = [
  'function footerActionButtons(t) {',
  '  return cardMenuItems(t).filter(i => !i.sep).slice(0, 5).map(it => h("button", {',
  '    class: "btn btn-ghost btn-sm" + (it.danger ? " danger" : ""), onclick: () => it.fn()',
  '  }, it.label));',
  '}',
].join('\n');

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
    setAttribute(name, value) { this.attrs[name] = String(value); if (name === "value") this.value = String(value); },
    // statusControl() reads sel.options for the running-task ready option
    get options() { return this.children.filter(c => c && c.tag === "option"); },
    remove() {}, focus() {}, contains() { return false; },
    querySelector() { return elStub(); }, querySelectorAll() { return []; },
    textContent: '', innerHTML: '', hidden: false, value: '', checked: false,
  };
  return el;
}

function makeContext(src) {
  const pending = [];      // in-flight {key, url, resolve}
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
      const parsed = new URL(u);
      const m = parsed.pathname.match(/^\/api\/kanban\/(model-options|tasks\/[^/?]+)$/);
      const key = m ? m[1] : parsed.pathname;
      return new Promise((resolve) => { pending.push({ key, url: u, resolve }); });
    },
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
  ctx.__pending = pending;
  ctx.__calls = calls;
  ctx.__body = body;
  ctx.__drawer = () => registry['#drawer'];
  ctx.__settle = (key, payload, nth = 0) => {
    const idxs = pending.map((p, i) => (p.key === key ? i : -1)).filter(i => i >= 0);
    if (idxs.length <= nth) {
      throw new Error(`no in-flight request #${nth} for ${key} (pending: ` + pending.map(p => p.key).join(', ') + ')');
    }
    const [p] = pending.splice(idxs[nth], 1);
    p.resolve({ ok: true, status: 200, statusText: 'OK', json: async () => payload });
  };
  return ctx;
}

const tick = () => new Promise(r => setImmediate(r));

const runningTask = () => ({
  id: 'task-run', title: 'Running one', body: '', assignee: 'dev',
  status: 'running', priority: 0, tenant: '', created_at: 1790000000, created_by: 'jimmy',
});
const detail = (t) => ({
  task: t,
  comments: [], attachments: [], runs: [], events: [], diagnostics: [],
  child_results: [], link_tasks: [], links: { parents: [], children: [] },
});
const MODELS = { providers: [{ slug: 'p1', label: 'P1', models: ['model-one'] }] };

/* Flatten nodes that carry a className; record node + attrs + listeners. */
function walk(node, out) {
  if (!node || typeof node !== 'object') return;
  if (node.className) {
    const first = node.children && node.children[0];
    const text = typeof first === 'string' ? first : (first && first.text) || '';
    out.push({ cls: String(node.className), text, node });
  }
  for (const k of (node.children || [])) walk(k, out);
}

/* Drive the REAL openDrawer -> renderDrawer path for the running task and
 * return the buttons of the rendered .drawer-actions footer. */
async function openDrawerFooter(ctx, task) {
  ctx.__S.data = { columns: [{ tasks: [task] }] };
  ctx.__S.sel = new Set();
  const p = ctx.openDrawer(task.id);
  await tick();
  ctx.__settle('tasks/' + task.id, detail(task));
  await tick(); await tick();          // renderDrawer reaches the model-options await
  ctx.__settle('model-options', MODELS);
  await tick(); await tick(); await tick();
  await p;
  const all = [];
  walk(ctx.__drawer(), all);
  const acts = all.find(x => x.cls.includes('drawer-actions'));
  if (!acts) throw new Error('no .drawer-actions element in rendered drawer');
  const out = [];
  walk(acts.node, out);
  return { all, rows: out };
}

function textOf(rows, label) { return rows.find(r => r.text === label); }

async function main() {
  const src = fs.readFileSync(APP_JS, 'utf8');
  if (!src.includes(FIXED_BODY)) throw new Error('footer renderer body missing/drifted from FIXED_BODY — fix regressed or was rewritten, update harness');
  if (!src.includes('...footerActionButtons(t)')) throw new Error('drawer footer not wired through footerActionButtons — update harness');

  /* ---- fixed behaviour: footer built by the REAL drawer render ---- */
  const ctx = makeContext(src);
  const { rows } = await openDrawerFooter(ctx, runningTask());

  const ready = textOf(rows, READY_LABEL);
  check('running task footer shows the Ready item (within the first five)', !!ready, rows.map(r => r.text));
  check('footer Ready button carries the HTML disabled attribute',
    !!(ready && 'disabled' in ready.node.attrs), ready && ready.node.attrs);
  check('footer Ready button carries the reclaim guidance title',
    !!(ready && /hermes kanban reclaim/.test(ready.node.attrs.title || '')), ready && ready.node.attrs);
  check('footer has no second (enabled) Ready action',
    rows.filter(r => /^→ Ready/.test(r.text)).length === 1, rows.filter(r => /^→ Ready/.test(r.text)).map(r => r.text));

  // TypeError path: invoke the registered click handler exactly as a real
  // click would; the disabled attribute alone only stops real user input.
  const handlers = (ready && ready.node.listeners.click) || [];
  check('footer Ready button has a registered click handler', handlers.length === 1, handlers.length);
  const callsBefore = ctx.__calls.length;
  let threw = null;
  try { handlers[0]({ target: ready.node }); } catch (e) { threw = String((e && e.message) || e); }
  await tick();
  check('clicking footer Ready does NOT throw (no it.fn TypeError)', threw === null, threw);
  check('clicking footer Ready fires no fetch', ctx.__calls.length === callsBefore, ctx.__calls.slice(callsBefore));
  check('clicking footer Ready opens no context menu', !ctx.window.__menuClose);

  // Enabled sibling still works end to end through the footer.
  const todo = textOf(rows, '→ Todo');
  check('enabled "→ Todo" footer button present without disabled attr',
    !!todo && !('disabled' in todo.node.attrs), todo && todo.node.attrs);
  const callsBefore2 = ctx.__calls.length;
  todo.node.listeners.click[0]({ target: todo.node });
  await tick(); await tick();
  check('clicking "→ Todo" fires the /tasks/bulk call',
    ctx.__calls.slice(callsBefore2).some(u => u.includes('/tasks/bulk')), ctx.__calls.slice(callsBefore2));

  /* ---- consistency: context menu for the SAME state matches the footer ---- */
  ctx.__body.children.length = 0;
  ctx.openCardMenu(runningTask(), 100, 100);
  const menu = ctx.__body.children[0];
  if (!menu) throw new Error('openCardMenu appended nothing to body');
  const menuRows = [];
  walk(menu, menuRows);
  const menuReady = textOf(menuRows, READY_LABEL);
  check('context menu Ready item present for the same state', !!menuReady);
  check('footer and context menu agree on disabled + title for the same state',
    !!ready && !!menuReady
    && ('disabled' in ready.node.attrs) === ('disabled' in menuReady.node.attrs)
    && (ready.node.attrs.title || null) === (menuReady.node.attrs.title || null),
    { footer: ready && ready.node.attrs, menu: menuReady && menuReady.node.attrs });

  /* ---- direct renderer check: exactly the first five non-sep model items ---- */
  const direct = makeContext(src);
  const btns = direct.footerActionButtons(runningTask());
  const texts = btns.map(b => (b.children[0] && (b.children[0].text ?? b.children[0])) || '');
  check('footerActionButtons yields the first five non-sep items',
    texts.length === 5 && texts[4] === READY_LABEL && texts[0] === '⛔ Block…', texts);
  check('footer Ready button is flagged via the boolean menu-model flag',
    btns[4].attrs.disabled === '' && /hermes kanban reclaim/.test(btns[4].attrs.title || ''), btns[4].attrs);

  /* ---- sensitivity: revert ONLY the footer renderer; the harness MUST
     reproduce the reported TypeError through the real drawer render ---- */
  if (!src.includes(BUGGY_BODY)) { /* needle is the target, not source; guard drift only */ }
  const buggy = src.replace(FIXED_BODY, BUGGY_BODY);
  if (buggy === src) throw new Error('sensitivity: could not revert footer renderer — pattern drift');
  const bad = makeContext(buggy);
  const { rows: badRows } = await openDrawerFooter(bad, runningTask());
  const badReady = textOf(badRows, READY_LABEL);
  check('sensitivity: reverted footer drops the disabled attribute',
    !!badReady && !('disabled' in badReady.node.attrs), badReady && badReady.node.attrs);
  let badThrew = null;
  try { badReady.node.listeners.click[0]({ target: badReady.node }); } catch (e) { badThrew = String((e && e.message) || e); }
  check('sensitivity: unguarded footer click DOES throw it.fn TypeError',
    badThrew !== null && /it\.fn is not a function/.test(badThrew), badThrew);

  console.log(failures === 0 ? '\nALL PASS' : `\n${failures} FAILURE(S)`);
  process.exit(failures === 0 ? 0 : 1);
}

main().catch(e => { console.error('HARNESS ERROR:', e); process.exit(1); });
