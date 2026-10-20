#!/usr/bin/env node
/* Editor-race regression tests for the kanban-ui drawer (app.js).

 * Proves the PR#13 round-4 review finding (inline comment 4321368000) stays
 * fixed: "The typing check runs only before the task-detail request. If the
 * user starts editing a description or comment while that request is
 * pending, a changed response still replaces the drawer and discards the
 * unsaved text. renderDrawer() also awaits model options before replacement,
 * so checking only after the detail fetch is insufficient. Recheck editing
 * state after the final await, defer through drawerPending, and commit
 * drawerSig only when the render is applied so a deferred update can retry."
 *
 * Before the fix:
 *   - refreshDrawer() checked "typing" only at entry, then suspended on
 *     await api(/tasks/<id>) and again (via renderDrawer) on the uncached
 *     /model-options fetch — text typed during either window was destroyed
 *     by drawer.replaceChildren().
 *   - refreshDrawer() committed drawerSig BEFORE those awaits, so even when
 *     a render was dropped (round-3 drawerIsStale guard) the signature was
 *     already advanced and the retry saw "no change" and no-oped forever.
 *   - openDrawer() rendered unconditionally after its fetch, clobbering an
 *     editor the user had started in the PREVIOUS drawer while the new
 *     request was in flight.
 * After the fix every swap path rechecks drawerEditorActive() after its
 * final await, defers via S.drawerPending (+ a retry timer), and commits
 * drawerSig/S.drawerData only when renderDrawer() returned true.
 *
 * Method: app.js is loaded into a vm context with minimal DOM/fetch stubs
 * (same harness family as test_model_options_race.js). The boot IIFE is
 * stripped so nothing auto-fires. document.addEventListener is captured so
 * the real focusout retry handler runs by hand. document.activeElement is
 * scripted to simulate the user typing in the drawer's textarea, and stub
 * elements tag themselves with tagName so drawerEditorActive()'s
 * input|textarea test sees them.
 *
 * Scenarios:
 *   1. refreshDrawer core race: drawer open, background refresh's detail
 *      request in flight, user starts typing, response CHANGED -> must not
 *      replace the DOM, S.drawerPending set, drawerSig NOT advanced; after
 *      the user leaves the field the deferred update retries and applies.
 *   2. model-options window: render still suspends after the detail check;
 *      typing started in that window must also defer (the finding's second
 *      sentence — checking only after the detail fetch is insufficient).
 *   3. openDrawer mid-fetch typing: user starts typing in the old drawer's
 *      editor, then the fetch for a newly-opened task lands -> the drawer
 *      content must survive until editing ends, then apply.
 *   4. sig-not-committed retry: after a deferral the SAME payload that was
 *      deferred still applies on retry (pre-fix the pre-committed sig made
 *      the retry a no-op and the drawer stayed stale forever).
 *   5. round-3 interaction: a render dropped by drawerIsStale() (user
 *      switched cards) also leaves drawerSig behind and defers, so the
 *      selected task's refresh retries the changed payload.
 *   6. sensitivity: strip the post-fetch editor recheck from refreshDrawer
 *      and the drawer must get clobbered (unsaved text lost) while pending,
 *      proving the harness detects the bug.
 *
 * Stdlib node only (no npm install). Run from anywhere:
 *
 *     node ansible/roles/kanban_ui/tests/test_drawer_edit_race.js
 *
 * Exit 0 = all pass. Any failure prints and exits 1.
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const APP_JS = path.join(__dirname, '..', 'files', 'app', 'static', 'app.js');
const MD_JS = require('path').join(__dirname, '..', 'files', 'app', 'static', 'md.js');
// The post-detail-request rechecks inside refreshDrawer/openDrawer. This
// exact text occurs twice (openDrawer's carries a trailing comment, so it
// contains this string as a prefix); the sensitivity test strips both.
const GUARD = 'if (drawerEditorActive()) { deferDrawerApply(id); return; }';

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
function elStub(tagName) {
  const el = {
    nodeType: 1,
    tagName: tagName || 'DIV',
    children: [], style: {}, dataset: {}, classList: { add() {}, remove() {}, toggle() {} },
    append(...k) { this.children.push(...k); },
    replaceChildren(...k) { this.children = k; },
    addEventListener() {}, setAttribute(k, v) { if (k === 'contenteditable') el.isContentEditable = !!v; },
    remove() {}, focus() {}, contains() { return false; },
    querySelector() { return elStub(); }, querySelectorAll() { return []; },
    textContent: '', innerHTML: '', hidden: false, value: '', checked: false, disabled: false,
  };
  return el;
}

function makeContext(src) {
  const pending = [];      // in-flight {key, url, resolve} — FIFO per key
  const calls = [];        // every fetch url seen
  const registry = {};     // persistent elements per querySelector selector
  const timers = [];       // {fn, ms, cancelled} — every setTimeout captured
  const docListeners = {}; // capture document.addEventListener(type, fn, capture)
  const win = { addEventListener() {} };
  const ctx = {
    window: win,
    console: { error() {}, log() {} },
    URL, Set, Map, WeakMap, Object, Array, JSON, Math, Date, String, Number, Boolean, Error, Promise, RegExp,
    parseInt, parseFloat, isNaN, encodeURIComponent, decodeURIComponent,
    innerWidth: 1600, innerHeight: 900,
    setTimeout: (fn, ms) => { const t = { fn, ms, cancelled: false }; timers.push(t); return t; },
    clearTimeout: (t) => { if (t && typeof t === 'object') t.cancelled = true; },
    setInterval: () => 0, clearInterval: () => {},
    localStorage: { getItem: () => null, setItem() {} },
    location: { origin: 'https://kanban.example.internal', protocol: 'https:', host: 'kanban.example.internal', hostname: 'kanban.example.internal', pathname: '/' },
    WebSocket: function WS(url) { calls.push('ws:' + url); this.close = () => {}; this.readyState = 1; },
    document: {
      hidden: false, title: '', activeElement: null,
      addEventListener: (type, fn, capture) => { (docListeners[type] = docListeners[type] || []).push(fn); },
      createElement: (tag) => elStub(String(tag).toUpperCase()),
      createTextNode: t => ({ nodeType: 3, text: String(t) }),
      body: { dataset: {} }, documentElement: { dataset: {} },
      querySelector: sel => (registry[sel] || (registry[sel] = elStub(sel.replace(/[#]/g, '')))),
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
  // top-level lexical bindings S (and helpers the host needs) — function
  // declarations hoist onto the sandbox object, so ctx.openDrawer and
  // ctx.refreshDrawer are directly callable.
  const boot = src.indexOf('(async function boot()');
  if (boot === -1) throw new Error('boot IIFE not found — app.js shape changed, update harness');
  const code = src.slice(0, boot) + '\n;globalThis.__S = S;';
  vm.createContext(ctx);
  vm.runInContext(code, ctx, { filename: 'app.js' });
  ctx.__pending = pending;
  ctx.__calls = calls;
  ctx.__timers = timers;
  ctx.__drawer = () => registry['#drawer'];
  ctx.__focusout = (target) => { for (const fn of (docListeners.focusout || [])) fn({ target }); };

  // simulate the user typing in a drawer textarea
  ctx.__type = () => {
    const ta = elStub('TEXTAREA');
    ta.value = 'UNSAVED TEXT';
    registry['#drawer'].contains = (n) => n === ta;   // editor lives in the drawer
    ctx.document.activeElement = ta;
    ctx.__editor = ta;
    return ta;
  };
  ctx.__blur = () => { ctx.document.activeElement = null; };

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

const detail = (x, body) => ({
  task: {
    id: 'task-' + x, title: 'Task ' + x, body: body || ('body of ' + x), assignee: 'dev',
    status: 'ready', priority: 0, tenant: '', created_at: 1790000000, created_by: 'jimmy',
  },
  comments: [], attachments: [], runs: [], events: [], diagnostics: [],
  child_results: [], link_tasks: [], links: { parents: [], children: [] },
});
const MODELS = { providers: [{ slug: 'p1', label: 'P1', models: ['model-one'] }] };

const tick = () => new Promise(r => setImmediate(r));
async function flush(n = 6) { for (let i = 0; i < n; i++) await tick(); }

/* ---------------- harness helpers ---------------- */
function walk(node, out) {
  if (!node || typeof node !== 'object') return;
  if (node.className) {
    const first = node.children && node.children[0];
    const text = typeof first === 'string' ? first : (first && first.text) || '';
    out.push({ cls: String(node.className), text, html: node.innerHTML || '' });
  }
  for (const k of (node.children || [])) walk(k, out);
}
function drawerNodes(ctx) { const out = []; walk(ctx.__drawer(), out); return out; }
function drawerTaskId(ctx) {
  const n = drawerNodes(ctx).find(x => x.cls.includes('drawer-id') && /^task-/.test(x.text));
  return n ? n.text : null;
}
function editButtons(ctx) {
  return drawerNodes(ctx).filter(x => x.cls.includes('btn-quiet') && x.text === 'Edit description').length;
}
// True when the drawer still shows the "Loading…" placeholder (openDrawer
// entry state) — used to prove a deferred render left the DOM untouched.
function loadingDrawer(ctx) {
  return drawerNodes(ctx).some(x => x.cls.includes('drawer-body') && x.text === 'Loading…');
}
function runTimers(ctx) {
  let ran = 0;
  for (const t of ctx.__timers) {
    if (!t.cancelled && !t.done) { t.done = true; t.fn(); ran++; }
  }
  return ran;
}

/* ---------------- test 1: refreshDrawer core race ---------------- */
async function testRefreshRace() {
  const ctx = makeContext(fs.readFileSync(APP_JS, 'utf8'));
  ctx.__S.board = 'homelab';

  // Open A normally (cached path after model options settle) so the drawer
  // is fully rendered and drawerSig committed.
  ctx.__S.modelOpts = MODELS;
  const p = ctx.openDrawer('task-A');
  await tick();
  ctx.__settle('tasks/task-A', detail('A'));
  await p; await flush();
  check('setup: A rendered, sig committed', drawerTaskId(ctx) === 'task-A' && editButtons(ctx) >= 1,
    { drawer: drawerTaskId(ctx), edits: editButtons(ctx) });

  // Background refresh fires (WS/visibility); its detail request is in flight...
  const pr = ctx.refreshDrawer();
  await tick();
  check('setup: refresh detail pending', ctx.__pending.some(x => x.key === 'tasks/task-A'), ctx.__pending.map(x => x.key));

  // ...the user starts typing in the description box DURING the request.
  ctx.__type();

  // The changed response lands. Pre-fix this replaced the drawer and lost
  // the unsaved text; post-fix it must defer.
  ctx.__settle('tasks/task-A', detail('A', 'SERVER CHANGED BODY'));
  await pr; await flush();

  check('race: unsaved text preserved (new body never reached the DOM while typing)',
    !drawerNodes(ctx).some(x => String(x.html || '').includes('SERVER CHANGED BODY')),
    drawerNodes(ctx).filter(x => /SERVER CHANGED/.test(String(x.html || x.text))));
  check('race: no Edit-description re-render happened while typing',
    editButtons(ctx) >= 1, editButtons(ctx));
  check('race: render deferred via drawerPending', ctx.__S.drawerPending === true, ctx.__S.drawerPending);
  check('race: drawerSig NOT committed for the deferred payload (retry stays possible)',
    !JSON.stringify(ctx.__S.drawerData || {}).includes('SERVER CHANGED BODY'), ctx.__S.drawerData);

  // User leaves the field: focusout handler (captured real app handler) must
  // clear pending and re-run the refresh, which now applies the render.
  // (The stub's drawer.contains() only matches the editor element, so the
  // focusout target must be that editor — as it would be in a real blur.)
  ctx.__blur();
  ctx.__focusout(ctx.__editor);              // target inside #drawer
  await flush(3);
  runTimers(ctx);                            // fire the focusout(0ms) retry (it cleared the 2s timer)
  await flush(4);
  // The retry re-fetches; settle every in-flight detail request.
  while (ctx.__pending.some(x => x.key === 'tasks/task-A')) {
    ctx.__settle('tasks/task-A', detail('A', 'SERVER CHANGED BODY'));
    await flush(2);
  }
  runTimers(ctx);                            // any leftover timer must be a no-op now
  await flush(8);
  check('retry: after editing ends the deferred update applies',
    drawerNodes(ctx).some(x => String(x.html || '').includes('SERVER CHANGED BODY')),
    drawerNodes(ctx).filter(x => /SERVER CHANGED|drawer-id/.test(String(x.html || x.text))));
  check('retry: drawerPending cleared', ctx.__S.drawerPending === false, ctx.__S.drawerPending);
  check('retry: sig/data committed once applied',
    JSON.stringify(ctx.__S.drawerData || {}).includes('SERVER CHANGED BODY'), ctx.__S.drawerData);
}

/* ---------------- test 2: model-options window (finding sentence 2) ---------------- */
async function testModelOptionsWindow() {
  let src = fs.readFileSync(APP_JS, 'utf8');
  const ctx = makeContext(src);
  ctx.__S.board = 'homelab';
  // Uncached path: /model-options stays pending inside renderDrawer.
  const p = ctx.openDrawer('task-A');
  await tick();
  ctx.__settle('tasks/task-A', detail('A'));
  await flush(3);
  check('setup: A awaits /model-options (uncached)', ctx.__pending.some(x => x.key === 'model-options'), ctx.__pending.map(x => x.key));

  // The detail-path checks have passed by now; user starts typing in the
  // window between the detail recheck and the renderDrawer post-await check.
  ctx.__type();
  ctx.__settle('model-options', MODELS);
  await p; await flush(6);

  check('model-options window: drawer kept the Loading placeholder (swap deferred)',
    loadingDrawer(ctx), drawerNodes(ctx).filter(x => x.cls.includes('drawer-body')));
  check('model-options window: deferred via drawerPending', ctx.__S.drawerPending === true, ctx.__S.drawerPending);

  // Editing ends -> focusout/retry timer applies the render.
  ctx.__blur();
  ctx.__focusout(ctx.__editor);
  runTimers(ctx);
  await flush(4);
  while (ctx.__pending.some(x => x.key === 'tasks/task-A')) {
    ctx.__settle('tasks/task-A', detail('A'));
    await flush(2);
  }
  await flush(8);
  check('model-options window: render applied after editing ends',
    drawerTaskId(ctx) === 'task-A' && editButtons(ctx) >= 1, { drawer: drawerTaskId(ctx), edits: editButtons(ctx) });
}

/* ---------------- test 3: openDrawer mid-fetch typing ---------------- */
async function testOpenDrawerMidFetch() {
  const ctx = makeContext(fs.readFileSync(APP_JS, 'utf8'));
  ctx.__S.board = 'homelab';
  ctx.__S.modelOpts = MODELS;

  // Drawer showing A (fully rendered, sig committed).
  const pa = ctx.openDrawer('task-A');
  await tick();
  ctx.__settle('tasks/task-A', detail('A'));
  await pa; await flush();
  check('setup: A rendered', drawerTaskId(ctx) === 'task-A', drawerTaskId(ctx));

  // User clicks B; while B's detail request is in flight they start typing
  // in the still-visible A editor.
  const pb = ctx.openDrawer('task-B');
  await tick();
  ctx.__type();
  ctx.__settle('tasks/task-B', detail('B'));
  await pb; await flush(6);

  // openDrawer(B) already swapped to "Loading…" at entry; the post-fetch
  // editor check must NOT have rendered B over the user's typing session.
  check('openDrawer: B render deferred while editing',
    loadingDrawer(ctx) && ctx.__S.drawerPending === true,
    { loading: loadingDrawer(ctx), pending: ctx.__S.drawerPending });
  check('openDrawer: B sig not committed', !JSON.stringify(ctx.__S.drawerData || {}).includes('body of B'), ctx.__S.drawerData);

  // User leaves the field; deferred apply renders B.
  ctx.__blur();
  runTimers(ctx);           // the deferDrawerApply retry timer re-runs refreshDrawer
  await flush(4);
  if (ctx.__pending.some(x => x.key === 'tasks/task-B')) {
    ctx.__settle('tasks/task-B', detail('B'));
  }
  await flush(8);
  check('openDrawer: B applied after editing ends', drawerTaskId(ctx) === 'task-B' && editButtons(ctx) >= 1,
    { drawer: drawerTaskId(ctx), edits: editButtons(ctx) });
}

/* ---------------- test 4: round-3 drawerIsStale interaction ---------------- */
async function testStaleDropDefers() {
  const ctx = makeContext(fs.readFileSync(APP_JS, 'utf8'));
  ctx.__S.board = 'homelab';
  ctx.__S.modelOpts = MODELS;

  // Open A rendered. Start a refresh for A (detail pending). Switch to B.
  const pa = ctx.openDrawer('task-A');
  await tick();
  ctx.__settle('tasks/task-A', detail('A'));
  await pa; await flush();
  const pr = ctx.refreshDrawer();           // refresh A, detail in flight
  await tick();
  const pb = ctx.openDrawer('task-B');      // user switches cards mid-refresh
  await tick();
  ctx.__settle('tasks/task-A', detail('A', 'CHANGED WHILE AWAY'));  // A's refresh response — changed
  await pr; await flush(4);
  check('round-3: late A refresh response discarded (B owns drawer)',
    drawerTaskId(ctx) !== 'task-A' || loadingDrawer(ctx), drawerTaskId(ctx));
  ctx.__settle('tasks/task-B', detail('B'));
  await pb; await flush(4);
  check('round-3: B rendered', drawerTaskId(ctx) === 'task-B', drawerTaskId(ctx));
  // The dropped A render must not have advanced any committed sig state for A.
  check('round-3: A change not lost — drawerData still points at B (A sig never pre-committed)',
    ctx.__S.drawerId === 'task-B' && !JSON.stringify(ctx.__S.drawerData || {}).includes('CHANGED WHILE AWAY'),
    ctx.__S.drawerData);
}

/* ---------------- test 5: sensitivity (both new checks removed) ---------------- */
async function testSensitivity() {
  let src = fs.readFileSync(APP_JS, 'utf8');
  if (!src.includes(GUARD)) throw new Error('post-fetch editor recheck missing from app.js — sensitivity test cannot run');
  // The pre-fix code had NEITHER post-await editor check: strip both the
  // refreshDrawer recheck and renderDrawer's final recheck to reproduce the
  // original clobber. (renderDrawer's post-await check alone would already
  // block the swap, so removing only one guard under-proves the fix.)
  const REND_GUARD = 'if (drawerEditorActive()) return false;';
  if (!src.includes(REND_GUARD)) throw new Error('renderDrawer editor recheck missing — sensitivity test cannot run');
  src = src.replaceAll(GUARD, '/* guard removed for sensitivity check */')
           .replace(REND_GUARD, '/* guard removed for sensitivity check */');
  const ctx = makeContext(src);
  ctx.__S.board = 'homelab';
  ctx.__S.modelOpts = MODELS;

  const p = ctx.openDrawer('task-A');
  await tick();
  ctx.__settle('tasks/task-A', detail('A'));
  await p; await flush();

  const pr = ctx.refreshDrawer();
  await tick();
  ctx.__type();                                  // typing during the request
  ctx.__settle('tasks/task-A', detail('A', 'SERVER CHANGED BODY'));
  await pr; await flush(8);

  check('sensitivity: without the post-fetch recheck the drawer IS clobbered while typing',
    drawerNodes(ctx).some(x => String(x.html || '').includes('SERVER CHANGED BODY')),
    drawerNodes(ctx).filter(x => /SERVER CHANGED/.test(String(x.html || x.text))));
}

(async () => {
  await testRefreshRace();
  await testModelOptionsWindow();
  await testOpenDrawerMidFetch();
  await testStaleDropDefers();
  await testSensitivity();
  console.log(failures === 0 ? '\nALL PASS' : `\n${failures} FAILURE(S)`);
  process.exit(failures === 0 ? 0 : 1);
})();
