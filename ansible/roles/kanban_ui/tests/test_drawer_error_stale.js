#!/usr/bin/env node
/* Stale-error regression tests for the kanban-ui drawer (app.js openDrawer).
 *
 * Proves the PR#13 round-4 review finding (inline comment 4321367982) stays
 * fixed: "The success path discards responses for an abandoned task, but
 * this error path does not. If task A's request fails after the user opens
 * task B, A's error replaces B's drawer while `S.drawerId` still points to
 * B. Apply the same selected-task check before rendering the failure."
 *
 * openDrawer()'s success path already had `if (S.drawerId !== id) return;`
 * after the /tasks/<id> await; its catch rendered
 * `drawer.replaceChildren(... "Failed: " + e.message)` unconditionally, so a
 * late rejection for an abandoned task A overwrote the drawer the user was
 * now viewing for B. The catch now applies the same selected-task check.
 *
 * Method: app.js is loaded into a vm context with minimal DOM/fetch stubs
 * (same harness family as test_model_options_race.js). The boot IIFE is
 * stripped so nothing auto-fires. Every /tasks/<id> fetch returns a deferred
 * we settle (or fail) by hand:
 *   1. openDrawer(A)  -> A detail pending
 *   2. openDrawer(B)  -> B detail pending, S.drawerId = B, drawer = B "Loading…"
 *   3. FAIL A detail  (HTTP 500) — the race: A's error must NOT touch the DOM
 * Then assert B's drawer is untouched (no "Failed:" node), S.drawerId stayed
 * on B, and B still renders normally once its own response lands. A
 * regression test covers the solo-open failure (the drawer the user IS
 * viewing must still show the error) and the closed-drawer case (closeDrawer
 * nulls S.drawerId; a late failure must not write into the hidden drawer).
 * A sensitivity check strips the guard line from the source and asserts A's
 * error DOES clobber B's drawer, proving the harness detects the bug.
 *
 * Stdlib node only (no npm install). Run from anywhere:
 *
 *     node ansible/roles/kanban_ui/tests/test_drawer_error_stale.js
 *
 * Exit 0 = all pass. Any failure prints and exits 1.
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const APP_JS = path.join(__dirname, '..', 'files', 'app', 'static', 'app.js');
const MD_JS = path.join(__dirname, '..', 'files', 'app', 'static', 'md.js');
const GUARD = 'if (S.drawerId !== id) return;              // stale failure for an abandoned task';

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

  // settle the nth (default: first) in-flight request matching key with 200
  ctx.__settle = (key, payload, nth = 0) => {
    const idxs = pending.map((p, i) => (p.key === key ? i : -1)).filter(i => i >= 0);
    if (idxs.length <= nth) {
      throw new Error(`no in-flight request #${nth} for ${key} (pending: ` + pending.map(p => p.key).join(', ') + ')');
    }
    const [p] = pending.splice(idxs[nth], 1);
    p.resolve({ ok: true, status: 200, statusText: 'OK', json: async () => payload });
  };
  // fail the nth in-flight request matching key with an HTTP error whose
  // body carries {detail} — api() throws Error(detail) on !res.ok
  ctx.__fail = (key, status, detail, nth = 0) => {
    const idxs = pending.map((p, i) => (p.key === key ? i : -1)).filter(i => i >= 0);
    if (idxs.length <= nth) {
      throw new Error(`no in-flight request #${nth} for ${key} (pending: ` + pending.map(p => p.key).join(', ') + ')');
    }
    const [p] = pending.splice(idxs[nth], 1);
    p.resolve({
      ok: false, status, statusText: 'Internal Server Error',
      json: async () => ({ detail }),
    });
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
function failedNodes(ctx) {
  return drawerNodes(ctx).filter(x => x.cls.includes('drawer-body') && /^Failed:/.test(x.text));
}
function loadingNodes(ctx) {
  return drawerNodes(ctx).filter(x => x.cls.includes('drawer-body') && x.text === 'Loading…');
}
function editButtons(ctx) {
  return drawerNodes(ctx).filter(x => x.cls.includes('btn-quiet') && x.text === 'Edit description').length;
}

/* Core interleaving. `guard: false` strips the selected-task check from the
 * catch to prove the harness detects the original bug. */
async function staleErrorScenario(guard = true) {
  let src = fs.readFileSync(APP_JS, 'utf8');
  if (!src.includes(GUARD)) throw new Error('guard line missing from app.js — sensitivity test cannot run');
  if (!guard) src = src.replace(GUARD, '/* guard removed for sensitivity check */');
  const ctx = makeContext(src);
  ctx.__S.board = 'homelab';

  // 1. User clicks task A; its /tasks/task-A request goes in flight.
  const pA = ctx.openDrawer('task-A').catch(e => String((e && e.stack) || e));
  await tick();
  check('setup: A drawer open showing Loading…',
    ctx.__S.drawerId === 'task-A' && loadingNodes(ctx).length === 1,
    { drawerId: ctx.__S.drawerId, loading: loadingNodes(ctx).length });

  // 2. User clicks task B while A's request is still pending. openDrawer(B)
  //    sets S.drawerId=B and resets the drawer to B's Loading… state.
  const pB = ctx.openDrawer('task-B').catch(e => String((e && e.stack) || e));
  await tick();
  check('setup: both task fetches pending, S.drawerId=B',
    ctx.__pending.filter(p => /^tasks\/task-/.test(p.key)).length === 2 && ctx.__S.drawerId === 'task-B',
    ctx.__pending.map(p => p.key));

  // 3. A's request FAILS after the user abandoned it. The catch runs while
  //    S.drawerId is task-B: its error must be discarded.
  ctx.__fail('tasks/task-A', 500, 'boom from A');
  const aErr = await pA;
  await tick(); await tick();
  check('stale error: A openDrawer resolved cleanly (error swallowed)', !aErr, aErr);
  check('stale error: B drawer NOT replaced by A\'s error',
    failedNodes(ctx).length === 0,
    drawerNodes(ctx).filter(x => x.cls.includes('drawer-body')).map(x => x.text));
  check('stale error: S.drawerId still task-B', ctx.__S.drawerId === 'task-B', ctx.__S.drawerId);

  // B's own response still lands and renders normally (drawer not wedged).
  ctx.__settle('tasks/task-B', detail('B'));
  await tick(); await tick(); await tick();
  const mo = ctx.__pending.filter(p => p.key === 'model-options');
  if (mo.length) ctx.__settle('model-options', MODELS, mo.length - 1);
  await pB;
  await tick(); await tick();
  check('stale error: B renders intact after A failed',
    drawerTaskId(ctx) === 'task-B' && editButtons(ctx) >= 1 && failedNodes(ctx).length === 0,
    { drawer: drawerTaskId(ctx), edits: editButtons(ctx), failed: failedNodes(ctx).map(x => x.text) });
  return ctx;
}

(async () => {
  // ---- core race: late failure for abandoned task must not touch the drawer
  await staleErrorScenario(true);

  // ---- regression: failure for the task the user IS viewing still renders
  {
    const ctx = makeContext(fs.readFileSync(APP_JS, 'utf8'));
    ctx.__S.board = 'homelab';
    const p = ctx.openDrawer('task-A').catch(e => String((e && e.stack) || e));
    await tick();
    ctx.__fail('tasks/task-A', 500, 'boom from A');
    const err = await p;
    await tick(); await tick();
    check('regression: solo open failure renders Failed: in the drawer',
      !err && failedNodes(ctx).length === 1 && failedNodes(ctx)[0].text.includes('boom from A'),
      { err, failed: failedNodes(ctx).map(x => x.text) });
  }

  // ---- regression: drawer closed mid-fetch -> late failure stays discarded
  {
    const ctx = makeContext(fs.readFileSync(APP_JS, 'utf8'));
    ctx.__S.board = 'homelab';
    const p = ctx.openDrawer('task-A').catch(e => String((e && e.stack) || e));
    await tick();
    ctx.closeDrawer();
    ctx.__fail('tasks/task-A', 500, 'boom from A');
    const err = await p;
    await tick(); await tick();
    check('regression: failure after closeDrawer does not write Failed: into hidden drawer',
      !err && ctx.__S.drawerId === null && failedNodes(ctx).length === 0,
      { err, drawerId: ctx.__S.drawerId, failed: failedNodes(ctx).map(x => x.text) });
  }

  // ---- sensitivity (isolated): guard stripped, assert A's error clobbers B
  {
    let src = fs.readFileSync(APP_JS, 'utf8');
    if (!src.includes(GUARD)) throw new Error('guard line missing from app.js — sensitivity test cannot run');
    src = src.replace(GUARD, '/* guard removed for sensitivity check */');
    const ctx = makeContext(src);
    ctx.__S.board = 'homelab';
    const pA = ctx.openDrawer('task-A').catch(e => String((e && e.stack) || e));
    await tick();
    ctx.openDrawer('task-B').catch(() => {});
    await tick();
    ctx.__fail('tasks/task-A', 500, 'boom from A');
    await pA;
    await tick(); await tick();
    check('sensitivity: guard removed -> A\'s error DOES replace B\'s drawer',
      failedNodes(ctx).length === 1 && failedNodes(ctx)[0].text.includes('boom from A') && ctx.__S.drawerId === 'task-B',
      { failed: failedNodes(ctx).map(x => x.text), drawerId: ctx.__S.drawerId });
  }

  console.log(failures ? `\n${failures} FAILURE(S)` : '\nall drawer-stale-error checks passed');
  process.exit(failures ? 1 : 0);
})().catch(e => { console.error('harness error:', e); process.exit(2); });
