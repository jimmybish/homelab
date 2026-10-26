#!/usr/bin/env node
/* Regression test for the kanban-ui dispatcher controls (app.js
 * renderBulkBar + wire(), index.html #nudgeBtn).
 *
 * Proves the PR#13 round-5 review finding (inline comment 4323915007)
 * stays fixed. Verbatim:
 *
 *   "POST /api/kanban/dispatch remains exposed and runs a dispatcher
 *    inside this container, rather than nudging the host. At the pinned
 *    Hermes revision (b3aa561f), dispatch_once() calls
 *    release_stale_claims(): an expired host claim is treated as foreign,
 *    so its live-PID extension and worker signalling are skipped, but
 *    the claim is still cleared. The host worker can remain running
 *    while another worker is dispatched. Reject this endpoint server-side
 *    and disable both UI dispatch controls until dispatch can delegate
 *    to the host."
 *
 * The two dispatch controls are (a) the selection bulkbar "⇢ dispatch"
 * button and (b) the topbar #nudgeBtn. Both used to POST /api/kanban/dispatch
 * (a container-local dispatch_once() tick). The fix:
 *   - bulkbar button renders with the HTML disabled attribute + a
 *     host-CLI-pointer title and NO click listener (no /dispatch fetch
 *     is ever constructed);
 *   - wire() sets nudgeBtn.disabled = true + the same title and never
 *     registers a click listener on it;
 *   - index.html ships #nudgeBtn already disabled (no enabled
 *     flash-of-control before wire() runs).
 * The server refuses POST /api/kanban/dispatch fail-closed (403) in
 * server.py's worker_control_mw -- locked by test_worker_control_guard.py.
 *
 * Method (same vm-stub pattern as test_footer_actions_disabled.js):
 * app.js is loaded into a vm context with minimal DOM/fetch stubs and the
 * boot IIFE stripped. Function declarations hoist onto the sandbox, so
 * renderBulkBar/wire are directly callable and every fetch url is recorded.
 * Stubs record setAttribute and addEventListener per element.
 *
 *   1. Bulkbar: with S.sel non-empty, renderBulkBar appends #bulkBar; the
 *      "⇢ dispatch" button carries disabled="" + the host-CLI title and
 *      registered NO click listener; invoking nothing fires no fetch.
 *   2. Bulkbar siblings (Todo/Ready/Blocked/Done/Archive/Clear) remain
 *      enabled with click listeners (the guard does not over-reach).
 *   3. wire(): the #nudgeBtn stub ends disabled with the host-CLI title
 *      and zero click listeners; no /dispatch fetch seen.
 *   4. index.html: the static #nudgeBtn ships disabled + host-CLI title.
 *   5. Sensitivity: revert ONLY the bulkbar dispatch button to the
 *      pre-fix shape -> the harness MUST see a click listener (and the
 *      nudgeBtn revert MUST register one), proving it detects the
 *      reported bug rather than passing vacuously.
 *
 * Stdlib node only (no npm install). Run from anywhere:
 *
 *     node ansible/roles/kanban_ui/tests/test_dispatch_controls_disabled.js
 *
 * Exit 0 = all pass. Any failure prints and exits 1.
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const APP_JS = path.join(__dirname, '..', 'files', 'app', 'static', 'app.js');
const MD_JS = path.join(__dirname, '..', 'files', 'app', 'static', 'md.js');
const INDEX_HTML = path.join(__dirname, '..', 'files', 'app', 'static', 'index.html');
const TITLE = 'Dispatcher passes are only available on the Hermes host: hermes kanban dispatch (disabled in the WebUI)';

/* Fixed shapes (needles; the sensitivity reverts target them). */
const FIXED_BULK = 'h("button", { class: "btn btn-sm btn-quiet", disabled: "", title: "Dispatcher passes are only available on the Hermes host: hermes kanban dispatch (disabled in the WebUI)" }, "\u21e2 dispatch"),';
const BUGGY_BULK = 'h("button", { class: "btn btn-sm btn-quiet", onclick: async () => { const ids = [...S.sel]; S.sel.clear(); render(); await act(api("/dispatch" + qs(), { method: "POST" }), "Dispatcher pass done"); loadBoard(true); } }, "\u21e2 dispatch"),';
const FIXED_NUDGE = [
  '  const nudgeBtn = $("#nudgeBtn");',
  '  nudgeBtn.disabled = true;',
  '  nudgeBtn.title = "Dispatcher passes are only available on the Hermes host: hermes kanban dispatch (disabled in the WebUI)";',
].join('\n');
const BUGGY_NUDGE = [
  '  $("#nudgeBtn").addEventListener("click", async () => {',
  '    try { const r = await api("/dispatch" + qs(), { method: "POST" }); toast("Dispatch: " + JSON.stringify(r).slice(0, 120), "ok"); await loadBoard(true); }',
  '    catch (e) { toast(e.message, "err"); }',
  '  });',
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
    remove() {}, focus() {}, contains() { return false; },
    querySelector() { return elStub(); }, querySelectorAll() { return []; },
    textContent: '', innerHTML: '', hidden: false, value: '', checked: false,
  };
  return el;
}

function textOf(el) {
  return el.children.map(c => (c && c.nodeType === 3 ? c.text : '')).join('');
}

function findDeep(root, pred) {
  const out = [];
  const walk = (el) => {
    if (!el || el.nodeType !== 1) return;
    if (pred(el)) out.push(el);
    for (const c of el.children || []) walk(c);
  };
  walk(root);
  return out;
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
  // top-level lexical binding `S` (script lexical scope is invisible to the
  // host context object otherwise).
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

const src = fs.readFileSync(APP_JS, 'utf8');

/* ---------------- 1+2. fixed build: bulkbar ---------------- */
{
  const ctx = makeContext(src);
  ctx.__S.sel.add('t_1');
  ctx.__S.sel.add('t_2');
  ctx.renderBulkBar();
  const bar = findDeep(ctx.__body, el => el.attrs.id === 'bulkBar')[0];
  check('bulkbar rendered', !!bar);
  if (bar) {
    const btns = findDeep(bar, el => el.tag === 'button');
    const dispatch = btns.filter(el => textOf(el).includes('dispatch'));
    check('exactly one dispatch button in bulkbar', dispatch.length === 1, dispatch.length);
    const b = dispatch[0];
    if (b) {
      check('bulkbar dispatch button disabled attribute', b.attrs.disabled === '', b.attrs);
      check('bulkbar dispatch button host-CLI title', b.attrs.title === TITLE, b.attrs.title);
      check('bulkbar dispatch button has NO click listener',
        (b.listeners.click || []).length === 0, (b.listeners.click || []).length);
    }
    const others = btns.filter(el => !textOf(el).includes('dispatch'));
    check('bulkbar siblings still enabled+clickable (>=6)',
      others.length >= 6 && others.every(el => el.attrs.disabled === undefined && (el.listeners.click || []).length === 1),
      others.map(el => [textOf(el), el.attrs.disabled, (el.listeners.click || []).length]));
  }
  check('fixed build: no /dispatch fetch constructed',
    !ctx.__calls.some(u => u.includes('/dispatch')), ctx.__calls);
}

/* ---------------- 3. fixed build: wire() disables #nudgeBtn ---------------- */
{
  const ctx = makeContext(src);
  ctx.wire();
  const nudge = ctx.__el('#nudgeBtn');
  check('#nudgeBtn present after wire()', !!nudge);
  if (nudge) {
    check('#nudgeBtn disabled property set', nudge.disabled === true, nudge.disabled);
    check('#nudgeBtn host-CLI title set', nudge.title === TITLE, nudge.title);
    check('#nudgeBtn registers NO click listener',
      (nudge.listeners.click || []).length === 0, (nudge.listeners.click || []).length);
  }
  check('wire(): no /dispatch fetch constructed',
    !ctx.__calls.some(u => u.includes('/dispatch')), ctx.__calls);
}

/* ---------------- 4. static shell ships #nudgeBtn disabled ---------------- */
{
  const html = fs.readFileSync(INDEX_HTML, 'utf8');
  const line = html.split('\n').find(ln => ln.includes('id="nudgeBtn"'));
  check('index.html has exactly one #nudgeBtn',
    html.split('\n').filter(ln => ln.includes('id="nudgeBtn"')).length === 1);
  check('index.html #nudgeBtn ships disabled', !!line && /\bdisabled\b/.test(line), line);
  check('index.html #nudgeBtn carries host-CLI title', !!line && line.includes('hermes kanban dispatch'), line);
}

/* ---------------- 5. sensitivity: pre-fix shapes MUST be detected ---------------- */
{
  check('needle lock: fixed bulkbar shape present in app.js', src.includes(FIXED_BULK));
  check('needle lock: fixed nudge shape present in app.js', src.includes(FIXED_NUDGE));

  // Revert ONLY the bulkbar dispatch button -> click listener reappears.
  const buggyBulkSrc = src.replace(FIXED_BULK, BUGGY_BULK);
  check('sensitivity setup: bulkbar revert applied', buggyBulkSrc !== src && buggyBulkSrc.includes(BUGGY_BULK));
  const ctxB = makeContext(buggyBulkSrc);
  ctxB.__S.sel.add('t_1');
  ctxB.renderBulkBar();
  const barB = findDeep(ctxB.__body, el => el.attrs.id === 'bulkBar')[0];
  const bB = barB ? findDeep(barB, el => el.tag === 'button' && textOf(el).includes('dispatch'))[0] : null;
  check('sensitivity: pre-fix bulkbar button had a click listener (bug detected)',
    !!bB && (bB.listeners.click || []).length === 1 && bB.attrs.disabled === undefined,
    bB && [bB.attrs.disabled, (bB.listeners.click || []).length]);

  // Revert ONLY the nudge wiring -> click listener reappears, not disabled.
  const buggyNudgeSrc = src.replace(FIXED_NUDGE, BUGGY_NUDGE);
  check('sensitivity setup: nudge revert applied', buggyNudgeSrc !== src && buggyNudgeSrc.includes(BUGGY_NUDGE));
  const ctxN = makeContext(buggyNudgeSrc);
  ctxN.wire();
  const nN = ctxN.__el('#nudgeBtn');
  check('sensitivity: pre-fix #nudgeBtn registered a click listener (bug detected)',
    !!nN && (nN.listeners.click || []).length === 1 && nN.disabled === undefined,
    nN && [nN.disabled, (nN.listeners.click || []).length]);
}

console.log(failures === 0 ? '\nALL PASS' : `\n${failures} FAILURE(S)`);
process.exit(failures === 0 ? 0 : 1);
