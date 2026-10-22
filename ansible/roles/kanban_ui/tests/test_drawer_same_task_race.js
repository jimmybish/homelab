#!/usr/bin/env node
/* Same-task reversed-response regression tests for the kanban-ui drawer
 * (app.js openDrawer/refreshDrawer/renderDrawer).
 *
 * Proves the PR#13 round-7 review finding (inline comment 4343269707,
 * Medium) stays fixed, verbatim:
 *
 *   "This sequence is allocated after openDrawer() or refreshDrawer() has
 *   already awaited the task-detail request. If two requests for the same
 *   task overlap and the older response arrives last, it receives the
 *   larger sequence, passes drawerIsStale(), and replaces the newer details
 *   and committed signature. The current guard protects the model-options
 *   wait, not the preceding HTTP request. Allocate a shared drawer request
 *   generation before each detail fetch, carry it through rendering and
 *   signature commits, and add a same-task reversed-response regression
 *   case while preserving the editor-deferral checks."
 *
 * Pre-fix shape: renderDrawer() ran `const mySeq = ++_drawerRenderSeq` at
 * ENTRY — i.e. after both callers had already awaited /tasks/<id>. Two
 * same-task requests were therefore ordered by ARRIVAL, not by ISSUE: the
 * older response resolving last entered renderDrawer last, got the larger
 * sequence, passed drawerIsStale(), swapped the drawer with its stale
 * payload, and its caller committed that payload into S.drawerData +
 * drawerSig.
 *
 * Post-fix shape: openDrawer()/refreshDrawer() each call
 * nextDrawerRequestSeq(id) BEFORE the detail fetch (shared counter — same
 * capture-before-fetch shape as boardReqGen in loadBoard, one pattern two
 * scopes) and carry the sequence through renderDrawer(d, mySeq) and the
 * signature commits. The reversed older response holds the LOWER sequence
 * and fails drawerIsStale() at the post-fetch check, before it can render
 * or touch the sig.
 *
 * Harness: same family as test_model_options_race.js /
 * test_drawer_edit_race.js — app.js in a vm context with DOM/fetch stubs,
 * boot IIFE stripped, every fetch a hand-settled deferred (FIFO per key).
 * The module-lexical `drawerSig` is exposed via __getSig() so the
 * "committed signature" claim is asserted directly, not inferred.
 *
 * Scenarios:
 *   1. reversed race: openDrawer(A) (gen 1) then refreshDrawer(A) (gen 2);
 *      newer response lands first (renders + commits), older response lands
 *      LAST -> must be discarded; drawer keeps the newer details,
 *      S.drawerData + drawerSig stay on the newer payload, no defer noise.
 *   2. in-order same-task overlap: both responses in issue order -> both
 *      apply, newest wins (guards must not over-discard).
 *   3. normal single open renders and commits (regression).
 *   4. deferred sig retry stays possible: after the reversed race a later
 *      refresh with the NEW payload is a sig-equal no-op (sig really sits
 *      on NEW, not OLD).
 *   5. sensitivity: strip the caller generation checks and restore
 *      renderDrawer's pre-fix entry allocation (arrival ordering) -> the
 *      older response DOES clobber the drawer, S.drawerData, and drawerSig,
 *      proving the harness detects the bug the fix prevents.
 *
 * Stdlib node only (no npm install). Run from anywhere:
 *
 *     node ansible/roles/kanban_ui/tests/test_drawer_same_task_race.js
 *
 * Exit 0 = all pass. Any failure prints and exits 1.
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const APP_JS = path.join(__dirname, '..', 'files', 'app', 'static', 'app.js');
const MD_JS = path.join(__dirname, '..', 'files', 'app', 'static', 'md.js');

// The caller-side post-fetch generation checks (openDrawer + refreshDrawer,
// identical text, stripped together by the sensitivity case).
const CALLER_GUARD = 'if (drawerIsStale(id, mySeq)) return;        // a newer same-task request was issued mid-fetch — older response loses (4343269707)';
// The defensive fallback that keeps renderDrawer safe when called without a
// caller-allocated sequence; the sensitivity case forces entry allocation.
const DEF_ALLOC = 'if (mySeq === undefined) mySeq = nextDrawerRequestSeq(myId);';
// The commit-time generation check (openDrawer + refreshDrawer share this
// exact line). Pre-fix it did not exist (callers committed on `applied`
// alone), so the sensitivity case strips it too — otherwise the inflated
// global sequence blocks BOTH commits and the stale-signature clobber the
// finding describes cannot reproduce.
const COMMIT_GUARD = 'if (drawerIsStale(id, mySeq)) return;        // superseded while the render awaited model options — the newer request owns the sig commit (4343269707)';

let failures = 0;
function check(name, cond, detail) {
  if (cond) {
    console.log('PASS  ' + name);
  } else {
    failures += 1;
    console.log('FAIL  ' + name + (detail !== undefined ? '  -> got: ' + JSON.stringify(detail) : ''));
  }
}

/* ---------------- stubs (same family as test_model_options_race) -------- */
function elStub(tagName) {
  const el = {
    nodeType: 1,
    tagName: tagName || 'DIV',
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

  const boot = src.indexOf('(async function boot()');
  if (boot === -1) throw new Error('boot IIFE not found — app.js shape changed, update harness');
  // Expose the module-lexical S plus drawerSig (via closures) so the
  // committed-signature claim is asserted directly.
  const code = src.slice(0, boot) +
    '\n;globalThis.__S = S;' +
    '\n;globalThis.__getSig = () => drawerSig;' +
    '\n;globalThis.__sigOf = (d) => drawerSigOf(d);';
  vm.createContext(ctx);
  vm.runInContext(code, ctx, { filename: 'app.js' });
  ctx.__pending = pending;
  ctx.__calls = calls;
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
function showsBody(ctx, body) {
  return drawerNodes(ctx).some(x => String(x.html || '').includes(body) || String(x.text || '').includes(body));
}
function editButtons(ctx) {
  return drawerNodes(ctx).filter(x => x.cls.includes('btn-quiet') && x.text === 'Edit description').length;
}

/* Core interleaving: two same-task detail requests in flight, the OLDER
 * (openDrawer's, generation 1) resolves LAST. `fix: true` runs the shipped
 * source; `fix: false` restores the pre-fix arrival ordering:
 *   - strips both caller-side post-fetch generation checks, and
 *   - makes renderDrawer allocate its sequence at ENTRY again (its own
 *     counter bump, ignoring the caller's), exactly as pre-fix.
 */
async function reversedScenario(fix = true) {
  let src = fs.readFileSync(APP_JS, 'utf8');
  // Needle absence is itself a failure (e.g. running against the pre-fix
  // app.js during the local-revert proof): report FAIL, don't crash the
  // harness, so `git checkout app.js && node <this test>` shows exactly
  // which race checks the fix is responsible for.
  if (!src.includes(CALLER_GUARD)) check('needles: caller generation guard present in app.js', false, 'missing CALLER_GUARD');
  if (!src.includes(DEF_ALLOC)) check('needles: renderDrawer caller-sequence fallback present', false, 'missing DEF_ALLOC');
  if (!src.includes(COMMIT_GUARD)) check('needles: commit generation check present', false, 'missing COMMIT_GUARD');
  if (!fix) {
    src = src.replaceAll(CALLER_GUARD, '/* caller generation check removed for sensitivity check */')
             .replaceAll(COMMIT_GUARD, '/* commit generation check removed for sensitivity check */')
             .replace(DEF_ALLOC, 'mySeq = nextDrawerRequestSeq(myId);  // sensitivity: pre-fix ENTRY allocation (arrival ordering)');
  }
  const ctx = makeContext(src);
  ctx.__S.board = 'homelab';
  ctx.__S.modelOpts = MODELS;   // cached: renderDrawer stays synchronous, no model-options race

  // 1. User clicks task A. Its detail request (generation 1) is issued.
  const pOpen = ctx.openDrawer('task-A').catch(e => String((e && e.stack) || e));
  await tick();
  check('setup: open detail request in flight',
    ctx.__S.drawerId === 'task-A' && ctx.__pending.filter(p => p.key === 'tasks/task-A').length === 1,
    ctx.__pending.map(p => p.key));

  // 2. The 30s safety-net poll fires refreshDrawer while the open is still
  //    in flight — a SECOND same-task request (generation 2), issued after.
  const pRef = ctx.refreshDrawer().catch(e => String((e && e.stack) || e));
  await tick();
  check('setup: two same-task detail requests in flight',
    ctx.__pending.filter(p => p.key === 'tasks/task-A').length === 2,
    ctx.__pending.map(p => p.key));

  // 3. The NEWER response (index 1) arrives FIRST with a changed body.
  ctx.__settle('tasks/task-A', detail('A', 'NEW SERVER BODY'), 1);
  await pRef;
  await flush();
  if (fix) {
    check('setup: newer response rendered and committed',
      drawerTaskId(ctx) === 'task-A' && showsBody(ctx, 'NEW SERVER BODY') &&
      JSON.stringify(ctx.__S.drawerData || {}).includes('NEW SERVER BODY'),
      { drawer: drawerTaskId(ctx), data: JSON.stringify(ctx.__S.drawerData || {}).slice(0, 120) });
    const sigAfterNew = ctx.__getSig();
    check('setup: committed signature equals the newer payload',
      sigAfterNew === ctx.__sigOf(detail('A', 'NEW SERVER BODY')),
      String(sigAfterNew).slice(0, 80));
  } else {
    // Pre-fix the newer render lands in the DOM, but the OLDER render (set
    // below) re-enters renderDrawer last, wins the render race, and then
    // blocks BOTH callers' commit checks with the inflated global sequence
    // — so mid-race nothing is committed. Assert only the DOM state here.
    check('setup (pre-fix): newer response rendered',
      drawerTaskId(ctx) === 'task-A' && showsBody(ctx, 'NEW SERVER BODY'),
      { drawer: drawerTaskId(ctx) });
  }

  // 4. The OLDER response (index 0 now) arrives LAST — the race Copilot
  //    flagged. Pre-fix it would take the larger sequence here.
  ctx.__settle('tasks/task-A', detail('A', 'OLD STALE BODY'), 0);
  const openErr = await pOpen;
  await flush();
  return { ctx, openErr };
}

/* ---------------- test 1: reversed same-task race (fix must discard) ---- */
async function testReversedRace() {
  const { ctx, openErr } = await reversedScenario(true);
  check('race: late older response resolved cleanly (discarded, no error path)', !openErr, openErr);
  check('race: stale OLD body never reached the drawer DOM',
    !showsBody(ctx, 'OLD STALE BODY'),
    drawerNodes(ctx).filter(x => /OLD STALE/.test(String(x.html || x.text))));
  check('race: drawer keeps the NEWER details',
    drawerTaskId(ctx) === 'task-A' && showsBody(ctx, 'NEW SERVER BODY') && editButtons(ctx) >= 1,
    { drawer: drawerTaskId(ctx), shows: showsBody(ctx, 'NEW SERVER BODY'), edits: editButtons(ctx) });
  check('race: S.drawerData still the newer payload',
    JSON.stringify(ctx.__S.drawerData || {}).includes('NEW SERVER BODY'),
    JSON.stringify(ctx.__S.drawerData || {}).slice(0, 120));
  check('race: committed signature still the newer payload',
    ctx.__getSig() === ctx.__sigOf(detail('A', 'NEW SERVER BODY')),
    String(ctx.__getSig()).slice(0, 80));
  check('race: no false deferral triggered (editor checks untouched)',
    !ctx.__S.drawerPending, ctx.__S.drawerPending);

  // sig-carried-through proof: a later refresh with the SAME newer payload
  // must hit the sig-equal fast path (no re-render) and stay put.
  const p = ctx.refreshDrawer();
  await tick();
  ctx.__settle('tasks/task-A', detail('A', 'NEW SERVER BODY'), 0);
  await p;
  await flush();
  check('sig retry: sig-equal refresh is a no-op and state stays on NEW',
    showsBody(ctx, 'NEW SERVER BODY') && !showsBody(ctx, 'OLD STALE BODY') &&
    JSON.stringify(ctx.__S.drawerData || {}).includes('NEW SERVER BODY'),
    { drawer: drawerTaskId(ctx), data: JSON.stringify(ctx.__S.drawerData || {}).slice(0, 120) });
}

/* ---------------- test 2: in-order same-task overlap (no over-discard) -- */
async function testInOrderOverlap() {
  const ctx = makeContext(fs.readFileSync(APP_JS, 'utf8'));
  ctx.__S.board = 'homelab';
  ctx.__S.modelOpts = MODELS;
  const pOpen = ctx.openDrawer('task-A').catch(e => String((e && e.stack) || e));
  await tick();
  const pRef = ctx.refreshDrawer().catch(e => String((e && e.stack) || e));
  await tick();
  // Responses arrive in ISSUE order: older (index 0) first, newer last.
  ctx.__settle('tasks/task-A', detail('A', 'OLDER BODY'), 0);
  await flush(3);
  ctx.__settle('tasks/task-A', detail('A', 'LATEST BODY'), 0);
  await pOpen; await pRef;
  await flush();
  check('in-order: newest response wins the drawer',
    drawerTaskId(ctx) === 'task-A' && showsBody(ctx, 'LATEST BODY') && !showsBody(ctx, 'OLDER BODY'),
    { drawer: drawerTaskId(ctx), latest: showsBody(ctx, 'LATEST BODY'), older: showsBody(ctx, 'OLDER BODY') });
  check('in-order: newest payload committed',
    JSON.stringify(ctx.__S.drawerData || {}).includes('LATEST BODY'),
    JSON.stringify(ctx.__S.drawerData || {}).slice(0, 120));
}

/* ---------------- test 3: normal single open (regression) --------------- */
async function testNormalOpen() {
  const ctx = makeContext(fs.readFileSync(APP_JS, 'utf8'));
  ctx.__S.board = 'homelab';
  ctx.__S.modelOpts = MODELS;
  const p = ctx.openDrawer('task-A').catch(e => String((e && e.stack) || e));
  await tick();
  ctx.__settle('tasks/task-A', detail('A'));
  const err = await p;
  await flush();
  check('normal: single open renders and commits',
    !err && drawerTaskId(ctx) === 'task-A' && editButtons(ctx) >= 1 &&
    ctx.__getSig() === ctx.__sigOf(detail('A')),
    { err, drawer: drawerTaskId(ctx), edits: editButtons(ctx) });
}

/* ---------------- test 4: sensitivity (pre-fix shape clobbers) ---------- */
async function testSensitivity() {
  const { ctx, openErr } = await reversedScenario(false);
  check('sensitivity: pre-fix run resolved without error', !openErr, openErr);
  check('sensitivity: without the shared generation the OLD response DOES clobber the drawer',
    showsBody(ctx, 'OLD STALE BODY') && !showsBody(ctx, 'NEW SERVER BODY'),
    { old: showsBody(ctx, 'OLD STALE BODY'), new: showsBody(ctx, 'NEW SERVER BODY') });
  check('sensitivity: pre-fix S.drawerData is the stale payload',
    JSON.stringify(ctx.__S.drawerData || {}).includes('OLD STALE BODY'),
    JSON.stringify(ctx.__S.drawerData || {}).slice(0, 120));
  check('sensitivity: pre-fix committed signature is the stale payload',
    ctx.__getSig() === ctx.__sigOf(detail('A', 'OLD STALE BODY')),
    String(ctx.__getSig()).slice(0, 80));
  // The harness itself stays sound pre-fix: a later refresh still repairs.
  const p = ctx.refreshDrawer();
  await tick();
  ctx.__settle('tasks/task-A', detail('A', 'NEW SERVER BODY'), 0);
  await p;
  await flush();
  check('sensitivity: a follow-up refresh still applies the newer payload (harness sound)',
    showsBody(ctx, 'NEW SERVER BODY') &&
    JSON.stringify(ctx.__S.drawerData || {}).includes('NEW SERVER BODY'),
    { new: showsBody(ctx, 'NEW SERVER BODY') });
}

(async () => {
  await testReversedRace();
  await testInOrderOverlap();
  await testNormalOpen();
  await testSensitivity();
  console.log(failures === 0 ? '\nALL PASS' : `\n${failures} FAILURE(S)`);
  process.exit(failures === 0 ? 0 : 1);
})().catch(e => { console.error('harness error:', e); process.exit(2); });
