#!/usr/bin/env node
/* Dependency-link normalization regression tests for the kanban-ui task
 * detail path (app.js api() response boundary + normalizeTaskDetail()).
 *
 * Proves the PR#13 round-3 review finding (inline comment 4319617504) stays
 * fixed: "The pinned plugin's _links_for() returns links.parents and
 * links.children as task-ID strings, but both existingDepsBox() (lines
 * 609-618) and renderDrawer() (lines 971-978) treat entries as objects.
 * Dependency labels are blank, opening one requests /tasks/undefined, and
 * removal sends an undefined endpoint ID. Normalize task-detail link IDs
 * into { id } objects at this shared response boundary so both consumers
 * receive the shape they expect."
 *
 * The pinned plugin_api.py get_task response carries
 *   "links": {"parents": ["t_..", ...], "children": ["t_..", ...]}   (strings)
 * plus "link_tasks": [{"id","title","status"}, ...] for surviving rows.
 * normalizeTaskDetail() maps each string entry to { id, ...link_tasks row }
 * (or bare { id } when the row is absent, so labels fall back to the id),
 * passes object entries through untouched, and is gated to task-detail
 * payloads (d.task + d.links) so every other endpoint returns byte-identical
 * objects.
 *
 * Method (same vm harness style as test_board_select_local.js): app.js is
 * loaded into a vm context with minimal DOM/fetch stubs and the boot IIFE
 * stripped. Tests:
 *   1. boundary unit: api('/tasks/x') resolves with enriched object entries;
 *      deleted ids degrade to { id }; object entries keep identity; null
 *      entries drop; non-detail payloads return the SAME object (gate).
 *   2. real consumer existingDepsBox(): labels render link_tasks titles
 *      (raw id for deleted rows) and the Remove button's DELETE carries the
 *      real parent_id/child_id on the query string.
 *   3. the dependency-row loop SHIPPED in renderDrawer() is sliced verbatim
 *      from the source and executed: clicking a label calls openDrawer with
 *      the real task id, and Remove builds the correct
 *      /links?parent_id=..&child_id=.. URL for both directions.
 *   4. sensitivity: with the normalizeTaskDetail call stripped from api(),
 *      test 2's scenario reproduces the bug (blank label, parent_id=undefined)
 *      — proving the harness actually detects what the fix prevents.
 *
 * Stdlib node only (no npm install). Run from anywhere:
 *
 *     node ansible/roles/kanban_ui/tests/test_dep_links_normalize.js
 *
 * Exit 0 = all pass. Any failure prints and exits 1.
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const APP_JS = path.join(__dirname, '..', 'files', 'app', 'static', 'app.js');
const MD_JS = path.join(__dirname, '..', 'files', 'app', 'static', 'md.js');

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
    __listeners: {},
    append(...k) { this.children.push(...k); },
    replaceChildren(...k) { this.children = k; },
    addEventListener(ev, fn) { this.__listeners[ev] = fn; },
    setAttribute() {}, remove() {}, focus() {}, contains() { return false; },
    querySelector() { return elStub(); }, querySelectorAll() { return []; },
    textContent: '', innerHTML: '', hidden: false, value: '', checked: false, disabled: false,
  };
  return el;
}

/* get_task-shaped payload: links are bare strings exactly as the pinned
 * plugin's _links_for() returns them; link_tasks carries the surviving rows
 * (t_gone is deleted/foreign -> no row). */
function detailPayload() {
  return {
    task: { id: 't_self', title: 'Self task', status: 'running', body: '' },
    comments: [], events: [], attachments: [],
    links: { parents: ['t_p1', 't_gone'], children: ['t_c1'] },
    link_tasks: [
      { id: 't_p1', title: 'Parent One', status: 'done' },
      { id: 't_c1', title: 'Child One', status: 'blocked' },
    ],
    child_results: [], runs: [],
  };
}

function makeContext(src) {
  const pending = [];      // in-flight {key, url, resolve}
  const calls = [];        // every fetch/WS url seen
  const win = { addEventListener() {} };
  const ctx = {
    window: win,
    console: { error() {}, log() {} },
    URL, Set, Map, WeakMap, Object, Array, JSON, Math, Date, String, Number, Boolean, Error, Promise, RegExp,
    parseInt, parseFloat, isNaN, encodeURIComponent, decodeURIComponent,
    setTimeout: () => 0, clearTimeout: () => {}, setInterval: () => 0, clearInterval: () => {},
    localStorage: { getItem: () => null, setItem() {} },
    location: { origin: 'https://kanban.example.internal', protocol: 'https:', host: 'kanban.example.internal', hostname: 'kanban.example.internal', pathname: '/' },
    WebSocket: function WS(url) { calls.push('ws:' + url); this.close = () => {}; this.readyState = 1; },
    document: {
      hidden: false, title: '', activeElement: null,
      addEventListener() {}, createElement: () => elStub(), createTextNode: t => ({ nodeType: 3, t }),
      body: { dataset: {} }, documentElement: { dataset: {} },
      querySelector: () => elStub(), querySelectorAll: () => [],
    },
    fetch: (url) => {
      const u = String(url);
      calls.push(u);
      const parsed = new URL(u);
      return new Promise((resolve) => {
        pending.push({ key: parsed.pathname, url: u, resolve });
      });
    },
  };
  win.KanbanMd = require(MD_JS);

  const boot = src.indexOf('(async function boot()');
  if (boot === -1) throw new Error('boot IIFE not found — app.js shape changed, update harness');
  const code = src.slice(0, boot)
    + '\n;const __toasts = []; const opened = [];'
    + '\n;globalThis.__S = S;'
    + '\n;globalThis.__opened = opened;'
    + '\n;globalThis.__toast = (m) => { __toasts.push(m); }; toast = __toast;'
    + '\n;globalThis.__loadBoard = async () => {}; loadBoard = __loadBoard;'
    + '\n;globalThis.__refreshDrawer = () => {}; refreshDrawer = __refreshDrawer;'
    + '\n;globalThis.__openDrawer = (id) => { opened.push(id); }; openDrawer = __openDrawer;';
  vm.createContext(ctx);
  vm.runInContext(code, ctx, { filename: 'app.js' });
  ctx.__pending = pending;
  ctx.__calls = calls;
  ctx.__settle = (key, payload) => {
    const idx = pending.findIndex(p => p.key === key);
    if (idx === -1) throw new Error('no in-flight request for ' + key + ' (pending: ' + pending.map(p => p.key).join(', ') + ')');
    const [p] = pending.splice(idx, 1);
    p.resolve({ ok: true, status: 200, statusText: 'OK', json: async () => payload });
  };
  return ctx;
}

const tick = () => new Promise(r => setImmediate(r));

/* ---------------- test 1: response-boundary normalization ---------------- */
async function testBoundaryNormalization() {
  const ctx = makeContext(fs.readFileSync(APP_JS, 'utf8'));
  ctx.__S.board = 'homelab';

  const p = ctx.api('/tasks/t_self');
  await tick();
  ctx.__settle('/api/kanban/tasks/t_self', detailPayload());
  const d = await p;

  check('parents normalized to objects',
    Array.isArray(d.links.parents) && d.links.parents.every(x => x && typeof x === 'object'));
  check('parent t_p1 enriched from link_tasks row',
    d.links.parents[0].id === 't_p1' && d.links.parents[0].title === 'Parent One'
    && d.links.parents[0].status === 'done', d.links.parents[0]);
  check('deleted parent t_gone degrades to bare {id}',
    d.links.parents[1].id === 't_gone' && d.links.parents[1].title === undefined
    && d.links.parents[1].status === undefined, d.links.parents[1]);
  check('child t_c1 enriched from link_tasks row',
    d.links.children[0].id === 't_c1' && d.links.children[0].title === 'Child One'
    && d.links.children[0].status === 'blocked', d.links.children[0]);
  check('link_tasks left intact for other consumers',
    Array.isArray(d.link_tasks) && d.link_tasks.length === 2);
  check('task payload untouched', d.task.id === 't_self' && d.task.title === 'Self task');

  // gate: a /board-shaped payload must come back as the SAME object (no walk)
  const boardPayload = { board: 'homelab', latest_event_id: 7, columns: [] };
  const bp = ctx.api('/board');
  await tick();
  ctx.__settle('/api/kanban/board', boardPayload);
  const got = await bp;
  check('non-detail payload returned identity-unchanged (gate)', got === boardPayload);

  // direct-call edge shapes via the exported function
  const objRow = { id: 't_x', title: 'X', status: 'todo' };
  const e = ctx.normalizeTaskDetail({
    task: { id: 't' }, links: { parents: [objRow, null, 42], children: undefined }, link_tasks: [],
  });
  check('object entries pass through by identity', e.links.parents[0] === objRow);
  check('null/invalid entries filtered out', e.links.parents.length === 1 && e.links.children.length === 0);
  check('null payload tolerated', ctx.normalizeTaskDetail(null) === null);
  check('task-without-links payload untouched',
    (() => { const o = { task: { id: 't' }, comments: [] }; return ctx.normalizeTaskDetail(o) === o; })());
}

/* ---------------- test 2: real consumer existingDepsBox() ---------------- */
async function runDepsBoxFlow(src) {
  const ctx = makeContext(src);
  ctx.__S.board = 'homelab';
  const box = ctx.existingDepsBox({ id: 't_self', title: 'Self task' });
  await tick();
  ctx.__settle('/api/kanban/tasks/t_self', detailPayload());
  await tick();
  return { ctx, box };
}

async function testExistingDepsBoxConsumer() {
  const { ctx, box } = await runDepsBoxFlow(fs.readFileSync(APP_JS, 'utf8'));
  const rows = box.children;
  check('three dependency rows rendered', rows.length === 3, rows.length);

  const labelOf = (row) => row.children[1].children.map(k => k.t).join('');
  check('parent label shows link_tasks title (not blank/undefined)',
    labelOf(rows[0]) === 'Parent One', labelOf(rows[0]));
  check('deleted parent label falls back to raw id',
    labelOf(rows[1]) === 't_gone', labelOf(rows[1]));
  check('child label shows link_tasks title',
    labelOf(rows[2]) === 'Child One', labelOf(rows[2]));
  check('status badge rendered', rows[0].children[2].children.map(k => k.t).join('') === 'done');

  // Remove dependency on the parent row must DELETE with real endpoint ids
  const removeBtn = rows[0].children[3];
  const clickP = removeBtn.__listeners.click();
  await tick();
  ctx.__settle('/api/kanban/links', { ok: true });
  await tick();
  ctx.__settle('/api/kanban/tasks/t_self', detailPayload()); // render() refresh
  await clickP;
  const del = ctx.__calls.filter(c => c.includes('/api/kanban/links?'));
  check('remove DELETE sent exactly once', del.length === 1, del);
  const u = new URL(del[0], 'https://kanban.example.internal');
  check('DELETE carries real parent_id', u.searchParams.get('parent_id') === 't_p1', u.search);
  check('DELETE carries real child_id', u.searchParams.get('child_id') === 't_self', u.search);
  check('DELETE carries board', u.searchParams.get('board') === 'homelab', u.search);
  return ctx;
}

/* ---------------- test 3: shipped renderDrawer dependency loop ------------- */
/* Slice the dependency block out of renderDrawer() verbatim (from the depBox
 * construction through the end of the deps for-loop, stopping before the
 * comments section) and run it as a function. This exercises the REAL label
 * span + openDrawer + remove-button code, not a hand-copied approximation. */
function extractDepLoop(src) {
  const start = src.indexOf('  // dependencies\n  const depBox = h(');
  const end = src.indexOf('  // comments', start);
  if (start === -1 || end === -1) throw new Error('renderDrawer dependency block not found — app.js shape changed, update harness');
  const block = src.slice(start, end);
  if (!block.includes('openDrawer(x.id)') || !block.includes('parent_id=${rel === "parent" ? x.id : t.id}')) {
    throw new Error('extracted block lost its dependency wiring — update harness');
  }
  return block;
}

async function testRenderDrawerLoop() {
  const src = fs.readFileSync(APP_JS, 'utf8');
  const block = extractDepLoop(src);
  const ctx = makeContext(src);
  ctx.__S.board = 'homelab';
  vm.runInContext(
    'globalThis.__depRows = function (t, d) {' + block + '\nreturn depBox; };',
    ctx, { filename: 'app.js#dep-loop' });

  const t = { id: 't_self', title: 'Self task', status: 'running' };
  // Feed the loop exactly what api() now hands renderDrawer: normalized objects.
  const d = ctx.normalizeTaskDetail(detailPayload());
  const depBox = ctx.__depRows(t, d);
  const rows = depBox.children.slice(1); // [0] is the section header + "＋ link"
  check('drawer rendered 3 dependency rows (2 parents, 1 child)', rows.length === 3, rows.length);

  const labelSpan = rows[0].children[1];
  check('drawer parent label is the task title',
    labelSpan.children.map(k => k.t).join('') === 'Parent One');
  labelSpan.__listeners.click();
  check('drawer label click opens the REAL task', ctx.__opened.join(',') === 't_p1', ctx.__opened);

  // child row (rows[2]: parents first, then children) remove -> parent_id=t_self&child_id=t_c1
  // (row children for a status-carrying entry: [chip, label, badge, remove-btn])
  const btn = rows[2].children[3];
  const clickP = btn.__listeners.click();
  await tick();
  ctx.__settle('/api/kanban/links', { ok: true });
  await clickP;
  const del = ctx.__calls.filter(c => c.includes('/api/kanban/links?'));
  check('drawer child remove DELETE sent once', del.length === 1, del);
  const u = new URL(del[0], 'https://kanban.example.internal');
  check('child remove DELETE keeps self as parent', u.searchParams.get('parent_id') === 't_self', u.search);
  check('child remove DELETE sends real child id', u.searchParams.get('child_id') === 't_c1', u.search);
  return ctx;
}

/* ---------------- test 4: sensitivity — pre-fix shape reproduces the bug --- */
async function testSensitivity() {
  const src = fs.readFileSync(APP_JS, 'utf8');
  const fixed = 'return normalizeTaskDetail(await res.json());';
  if (!src.includes(fixed)) throw new Error('normalizeTaskDetail call site not found — app.js shape changed, update harness');
  const stripped = src.replace(fixed, 'return await res.json();');
  if (stripped === src) throw new Error('strip was a no-op — update harness');

  const { ctx, box } = await runDepsBoxFlow(stripped);
  const rows = box.children;
  const labelOf = (row) => row.children[1].children.map(k => k.t).join('');
  check('sensitivity: raw string entries leave the label blank',
    rows.length === 3 && labelOf(rows[0]) === '', rows.map(labelOf));
  const removeBtn = rows[0].children[3];
  const clickP = removeBtn.__listeners.click();
  await tick();
  ctx.__settle('/api/kanban/links', { ok: true });
  await tick();
  ctx.__settle('/api/kanban/tasks/t_self', detailPayload());
  await clickP;
  const del = ctx.__calls.filter(c => c.includes('/api/kanban/links?'));
  const hasUndefined = del.some(c => c.includes('parent_id=undefined'));
  check('sensitivity: unfixed build sends parent_id=undefined (bug reproduced)',
    hasUndefined, del);
}

/* ---------------- run ---------------- */
(async () => {
  await testBoundaryNormalization();
  await testExistingDepsBoxConsumer();
  await testRenderDrawerLoop();
  await testSensitivity();
  console.log(failures === 0 ? '\nALL PASS' : '\n' + failures + ' FAILURE(S)');
  process.exit(failures === 0 ? 0 : 1);
})().catch(e => { console.error('HARNESS ERROR:', e && e.stack || e); process.exit(2); });
