#!/usr/bin/env node
/* Race regression tests for the kanban-ui drawer model-options render
 * (app.js openDrawer/renderDrawer).
 *
 * Proves the PR#13 round-3 review finding (inline comment 4319617532) stays
 * fixed: "When model options are not cached, opening task A and then B can
 * leave both model-options requests pending. If A finishes last,
 * renderDrawer() replaces B's drawer with A's edit controls while
 * S.drawerId still points to B. The caller checks run before this additional
 * await. Recheck the selected task immediately afterward, before replacing
 * the drawer contents."
 *
 * The caller-side check `if (S.drawerId !== id) return;` in openDrawer runs
 * BEFORE renderDrawer()'s internal `await modelSelect(...)` (uncached
 * /model-options fetch), so a late A response re-rendered A's whole drawer
 * over B's. renderDrawer now captures the task id and a monotonically
 * increasing sequence at entry (same pattern as the board-refresh guard in
 * loadBoard) and rechecks both immediately after that await, before any DOM
 * replacement.
 *
 * Method: app.js is loaded into a vm context with minimal DOM/fetch stubs.
 * The boot IIFE is stripped so nothing auto-fires. Every fetch returns a
 * deferred we settle by hand, in chosen order — the stale-response harness:
 *   1. openDrawer(A)  -> A detail pending
 *   2. openDrawer(B)  -> B detail pending
 *   3. settle B detail  (B renders, then awaits /model-options)
 *   4. settle A detail  (A renders, then awaits /model-options)
 *   5. settle B /model-options   (B's render completes)
 *   6. settle A /model-options   LAST — the race: A must NOT touch the DOM
 * Then assert the drawer still shows B's id and B's edit controls, no
 * task-A marker anywhere, and S.drawerId stayed on B. A regression test
 * covers the normal uncached single-open path and the cached second open
 * (no extra /model-options request). A sensitivity check strips the guard
 * line from the source and asserts A's edit controls DO clobber B's drawer,
 * proving the harness detects the bug the guard fixes.
 *
 * Stdlib node only (no npm install). Run from anywhere:
 *
 *     node ansible/roles/kanban_ui/tests/test_model_options_race.js
 *
 * Exit 0 = all pass. Any failure prints and exits 1.
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const APP_JS = path.join(__dirname, '..', 'files', 'app', 'static', 'app.js');
const MD_JS = path.join(__dirname, '..', 'files', 'app', 'static', 'md.js');
const GUARD = 'if (drawerIsStale(myId, mySeq)) return false;';

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
function elStub() {
  const el = {
    nodeType: 1,
    children: [], style: {}, dataset: {}, classList: { add() {}, remove() {}, toggle() {} },
    append(...k) { this.children.push(...k); },
    replaceChildren(...k) { this.children = k; },
    addEventListener() {}, setAttribute() {}, remove() {}, focus() {}, contains() { return false; },
    querySelector() { return elStub(); }, querySelectorAll() { return []; },
    textContent: '', innerHTML: '', hidden: false, value: '', checked: false, disabled: false,
  };
  return el;
}

function makeContext(src) {
  const pending = [];      // in-flight {key, url, resolve} — FIFO per key
  const calls = [];        // every fetch url seen
  const registry = {};     // persistent elements per querySelector selector
  const win = { addEventListener() {} };
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
      createElement: () => elStub(),
      createTextNode: t => ({ nodeType: 3, text: String(t) }),
      body: { dataset: {} }, documentElement: { dataset: {} },
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
  // host context object otherwise). Function declarations are hoisted onto
  // the sandbox object, so ctx.openDrawer is directly callable.
  const boot = src.indexOf('(async function boot()');
  if (boot === -1) throw new Error('boot IIFE not found — app.js shape changed, update harness');
  const code = src.slice(0, boot) + '\n;globalThis.__S = S;';
  vm.createContext(ctx);
  vm.runInContext(code, ctx, { filename: 'app.js' });
  ctx.__pending = pending;
  ctx.__calls = calls;
  ctx.__drawer = () => registry['#drawer'];

  // settle the nth (default: first) in-flight request matching key
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

const detail = (x) => ({
  task: {
    id: 'task-' + x, title: 'Task ' + x, body: 'body of ' + x, assignee: 'dev',
    status: 'ready', priority: 0, tenant: '', created_at: 1790000000, created_by: 'jimmy',
  },
  comments: [], attachments: [], runs: [], events: [], diagnostics: [],
  child_results: [], link_tasks: [], links: { parents: [], children: [] },
});
const MODELS = { providers: [{ slug: 'p1', label: 'P1', models: ['model-one'] }] };

const tick = () => new Promise(r => setImmediate(r));

/* ---------------- harness helpers ---------------- */
function walk(node, out) {
  if (!node || typeof node !== 'object') return;
  if (node.className) {
    const first = node.children && node.children[0];
    const text = typeof first === 'string' ? first : (first && first.text) || '';
    out.push({ cls: String(node.className), text });
  }
  for (const k of (node.children || [])) walk(k, out);
}
function drawerNodes(ctx) { const out = []; walk(ctx.__drawer(), out); return out; }
function drawerTaskId(ctx) {
  const n = drawerNodes(ctx).find(x => x.cls.includes('drawer-id') && /^task-/.test(x.text));
  return n ? n.text : null;
}
/* The edit control set is rendered by renderBody(): the "Edit description"
 * button (which flips to Cancel/Save once editing). Present only after a
 * renderDrawer() for that task completed past the model-options await. */
function editButtons(ctx) {
  return drawerNodes(ctx).filter(x => x.cls.includes('btn-quiet') && x.text === 'Edit description').length;
}

/* The core interleaving. Returns the context after A's model-options lands
 * last. Caller asserts. `guard: false` strips the staleness recheck from
 * the source to prove the harness detects the original bug. */
async function raceScenario(guard = true) {
  let src = fs.readFileSync(APP_JS, 'utf8');
  if (!src.includes(GUARD)) throw new Error('guard line missing from app.js — sensitivity test cannot run');
  if (!guard) src = src.replace(GUARD, '/* guard removed for sensitivity check */');
  const ctx = makeContext(src);
  ctx.__S.board = 'homelab';

  // A's detail lands FIRST, so its openDrawer passes the caller-side
  // `S.drawerId !== id` check and renderDrawer suspends on the uncached
  // /model-options await (model-options request #1 in flight).
  const pA = ctx.openDrawer('task-A').catch(e => String((e && e.stack) || e));
  await tick();
  ctx.__settle('tasks/task-A', detail('A'));
  await tick(); await tick(); await tick();
  check('race: A renders and awaits /model-options (uncached)',
    ctx.__S.drawerId === 'task-A' && ctx.__pending.filter(p => p.key === 'model-options').length === 1,
    ctx.__pending.map(p => p.key));

  // User clicks B while A's model-options request is still in flight.
  // B's detail lands too, and B's renderDrawer suspends on its OWN
  // /model-options request (loadModelOpts does not dedupe concurrent calls).
  const bErr = null;
  const pB = ctx.openDrawer('task-B').catch(e => String((e && e.stack) || e));
  await tick();
  ctx.__settle('tasks/task-B', detail('B'));
  await tick(); await tick(); await tick();
  check('race: both model-options requests pending (A behind B)',
    ctx.__pending.filter(p => p.key === 'model-options').length === 2,
    ctx.__pending.map(p => p.key));

  // B's model-options resolves first and B's render completes — the drawer
  // legitimately shows B with B's edit controls. (A's request was pushed
  // first, so B's in-flight call is FIFO index 1.)
  ctx.__settle('model-options', MODELS, 1);
  await pB;
  await tick(); await tick();
  check('race: B drawer has its edit controls before A lands',
    drawerTaskId(ctx) === 'task-B' && editButtons(ctx) >= 1 && !bErr,
    { drawer: drawerTaskId(ctx), saves: editButtons(ctx), bErr });

  // ...then A's model-options resolves LAST — the race. A still points at
  // the task the user abandoned; its continuation must not touch the drawer.
  ctx.__settle('model-options', MODELS, 0);
  const aResult = await pA;
  await tick(); await tick();
  check('race: A openDrawer resolved cleanly', !aResult, aResult);
  return ctx;
}

/* ---------------- test 1: the race (guard must discard A) ---------------- */
async function testRace() {
  const ctx = await raceScenario(true);
  check('race: drawer still shows B, not A', drawerTaskId(ctx) === 'task-B', drawerTaskId(ctx));
  check('race: no task-A marker ever replaced the drawer',
    !drawerNodes(ctx).some(x => x.text === 'task-A'), drawerNodes(ctx).filter(x => /task-/.test(x.text)));
  check('race: B edit controls intact after stale A landed', editButtons(ctx) >= 1, editButtons(ctx));
  check('race: S.drawerId stayed on B', ctx.__S.drawerId === 'task-B', ctx.__S.drawerId);
  check('race: model options cached from settled responses',
    !!ctx.__S.modelOpts && Array.isArray(ctx.__S.modelOpts.providers), ctx.__S.modelOpts);
}

/* ---------------- test 2: normal uncached open renders (regression) ---------------- */
async function testNormalOpen() {
  const ctx = makeContext(fs.readFileSync(APP_JS, 'utf8'));
  ctx.__S.board = 'homelab';
  const p = ctx.openDrawer('task-A');
  await tick();
  ctx.__settle('tasks/task-A', detail('A'));
  await tick(); await tick();
  ctx.__settle('model-options', MODELS);
  await p;
  await tick();
  check('normal: drawer shows task-A', drawerTaskId(ctx) === 'task-A', drawerTaskId(ctx));
  check('normal: drawer shows the title', drawerNodes(ctx).some(x => x.cls.includes('drawer-title') && x.text === 'Task A'),
    drawerNodes(ctx).filter(x => x.cls.includes('drawer-title')));
  check('normal: edit controls rendered', editButtons(ctx) >= 1, editButtons(ctx));
  check('normal: model options cached', !!ctx.__S.modelOpts, ctx.__S.modelOpts);

  // cached second open: no extra /model-options request
  const callsBefore = ctx.__calls.filter(c => c.includes('/model-options')).length;
  const p2 = ctx.openDrawer('task-B');
  await tick();
  ctx.__settle('tasks/task-B', detail('B'));
  await p2;
  await tick();
  const callsAfter = ctx.__calls.filter(c => c.includes('/model-options')).length;
  check('normal: cached open fires no new /model-options request', callsAfter === callsBefore, { callsBefore, callsAfter });
  check('normal: second open renders task-B', drawerTaskId(ctx) === 'task-B', drawerTaskId(ctx));
}

/* ---------------- test 3: harness sensitivity (guard removed) ---------------- */
async function testGuardlessIsDetected() {
  const ctx = await raceScenario(false);
  check('sensitivity: without the guard A\'s edit controls clobber B\'s drawer',
    drawerTaskId(ctx) === 'task-A' && ctx.__S.drawerId === 'task-B' && editButtons(ctx) >= 1,
    { drawer: drawerTaskId(ctx), drawerId: ctx.__S.drawerId, saves: editButtons(ctx) });
}

(async () => {
  await testRace();
  await testNormalOpen();
  await testGuardlessIsDetected();
  console.log(failures === 0 ? '\nALL PASS' : `\n${failures} FAILURE(S)`);
  process.exit(failures === 0 ? 0 : 1);
})();
