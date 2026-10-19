#!/usr/bin/env node
/* Race regression tests for the kanban-ui board refresh (app.js loadBoard).

 * Proves the PR#13 round-2 review finding (inline comment 4301554435) stays
 * fixed: "An in-flight HTTP refresh for board A can finish after
 * switchBoard() loads B. These assignments then display A's tasks under B's
 * name and restore A's event cursor; subsequent card actions use B."
 *
 * The WebSocket identity guard (if (S.ws !== ws) return) never covered HTTP
 * responses, so loadBoard() now captures S.board at request time and drops
 * the payload if the selection changed while awaiting.
 *
 * Method: app.js is loaded into a vm context with minimal DOM/fetch stubs.
 * The boot IIFE is stripped so no timers fire. fetch for the /board endpoint
 * returns a deferred we resolve by hand — the slow-response harness:
 *   1. fire loadBoard() for A (request in flight)
 *   2. switchBoard() to B (its requests complete)
 *   3. resolve A's response late
 * Then assert: S.data is still B's payload, S.wsCursor never took A's
 * latest_event_id, and no stale render fired. A regression test covers the
 * normal in-order refresh path (payload and cursor must land, cursor must
 * not regress). A sensitivity check strips the guard line from the source
 * and asserts the stale payload DOES land — proving the harness actually
 * detects the bug the guard fixes.
 *
 * Stdlib node only (no npm install). Run from anywhere:
 *
 *     node ansible/roles/kanban_ui/tests/test_board_refresh_race.js
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
    children: [], style: {}, dataset: {}, classList: { add() {}, remove() {}, toggle() {} },
    append(...k) { this.children.push(...k); },
    replaceChildren(...k) { this.children = k; },
    addEventListener() {}, setAttribute() {}, remove() {}, focus() {}, contains() { return false; },
    querySelector() { return elStub(); }, querySelectorAll() { return []; },
    textContent: '', innerHTML: '', hidden: false, value: '', checked: false, disabled: false,
  };
  return el;
}

const API_PATHS = {
  board: '/api/kanban/board',
  switch: '/api/kanban/boards/B/switch',
  boards: '/api/kanban/boards',
  assignees: '/api/kanban/assignees',
};

function makeContext(src) {
  const pending = [];      // in-flight {key, url, resolve}
  const calls = [];        // every fetch/WS url seen
  let renders = 0;
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
      addEventListener() {}, createElement: () => elStub(), createTextNode: t => ({ t }),
      body: { dataset: {} }, documentElement: { dataset: {} },
      querySelector: () => elStub(), querySelectorAll: () => [],
    },
    fetch: (url) => {
      const u = String(url);
      calls.push(u);
      const parsed = new URL(u);
      const key = parsed.pathname === API_PATHS.board
        ? 'board:' + parsed.searchParams.get('board')
        : parsed.pathname;
      return new Promise((resolve) => { pending.push({ key, url: u, resolve }); });
    },
  };
  win.KanbanMd = require(MD_JS);

  // Strip the boot IIFE so nothing auto-fires at load time, then expose the
  // top-level lexical bindings (const S etc.) on the global object — script
  // lexical scope is invisible to the host context object otherwise.
  const boot = src.indexOf('(async function boot()');
  if (boot === -1) throw new Error('boot IIFE not found — app.js shape changed, update harness');
  const code = src.slice(0, boot) +
    '\n;let __renders = 0;' +
    '\n;globalThis.__S = S;' +
    '\n;render = () => { __renders += 1; }; globalThis.__renderCount = () => __renders;';
  vm.createContext(ctx);
  vm.runInContext(code, ctx, { filename: 'app.js' });
  // The tail ran inside the script's own scope: `render` (a global function
  // declaration living in the global lexical environment, invisible on the
  // sandbox object) was rebound to the host spy, so loadBoard()'s closure
  // resolves to the counter. __renders is captured from the host closure.
  ctx.__pending = pending;
  ctx.__calls = calls;

  // resolve a queued fetch by key with a JSON payload
  ctx.__settle = (key, payload) => {
    const idx = pending.findIndex(p => p.key === key);
    if (idx === -1) throw new Error('no in-flight request for ' + key + ' (pending: ' + pending.map(p => p.key).join(', ') + ')');
    const [p] = pending.splice(idx, 1);
    p.resolve({ ok: true, status: 200, statusText: 'OK', json: async () => payload });
  };
  return ctx;
}

const boardPayload = (name, cursor) => ({
  board: name, latest_event_id: cursor,
  columns: [{ name: 'ready', tasks: [{ id: name + '-task-1', title: 'task on ' + name, body: '', assignee: null, tenant: '' }] }],
});
const BOARDS = { boards: [{ slug: 'A', name: 'Board A' }, { slug: 'B', name: 'Board B' }] };
const ASSIGNEES = { assignees: [] };

const tick = () => new Promise(r => setImmediate(r));

function switchToB(ctx) {
  // switchBoard(B) while A's refresh is in flight; settle its own requests
  const pSwitch = ctx.switchBoard('B');
  return (async () => {
    await tick();
    ctx.__settle(API_PATHS.switch, {});                    // global switch ack
    await tick();
    ctx.__settle('board:B', boardPayload('B', 10));        // B's fresh load
    ctx.__settle(API_PATHS.boards, BOARDS);                // loadBoards()
    ctx.__settle(API_PATHS.assignees, ASSIGNEES);          // loadAssignees()
    await pSwitch;
  })();
}

/* ---------------- test 1: the race (guard must discard A) ---------------- */
async function testRace() {
  const ctx = makeContext(fs.readFileSync(APP_JS, 'utf8'));
  ctx.__S.board = 'A';

  const pA = ctx.loadBoard(true);                          // refresh for A, in flight
  await tick();
  check('race: A /board request is in flight', ctx.__pending.some(p => p.key === 'board:A'), ctx.__pending.map(p => p.key));

  await switchToB(ctx);
  const rendersAfterSwitch = ctx.__renderCount();

  // A's stale response arrives AFTER the switch completed
  ctx.__settle('board:A', boardPayload('A', 999));
  await pA.catch(() => {});
  await tick(); await tick();

  check('race: S.data is B payload, not A', ctx.__S.data && ctx.__S.data.board === 'B', ctx.__S.data && ctx.__S.data.board);
  check('race: no A task titles rendered under B', !JSON.stringify(ctx.__S.data).includes('task on A'), ctx.__S.data);
  check('race: A cursor 999 never restored', ctx.__S.wsCursor === 10, ctx.__S.wsCursor);
  check('race: S.board is B', ctx.__S.board === 'B');
  check('race: stale response triggered no extra render', ctx.__renderCount() === rendersAfterSwitch, { before: rendersAfterSwitch, after: ctx.__renderCount() });
  check('race: B socket is the current one', ctx.__S.ws && ctx.__calls.some(c => c.startsWith('ws:') && c.includes('board=B')), ctx.__calls.filter(c => c.startsWith('ws:')));
}

/* ---------------- test 2: normal refresh applies (regression) ---------------- */
async function testNormalRefresh() {
  const ctx = makeContext(fs.readFileSync(APP_JS, 'utf8'));
  ctx.__S.board = 'A';
  const p = ctx.loadBoard(true);
  await tick();
  ctx.__settle('board:A', boardPayload('A', 42));
  await p;
  check('normal: payload applied', ctx.__S.data && ctx.__S.data.board === 'A', ctx.__S.data);
  check('normal: cursor advanced to 42', ctx.__S.wsCursor === 42, ctx.__S.wsCursor);
  check('normal: render fired', ctx.__renderCount() === 1, ctx.__renderCount());

  // second in-order refresh with a lower cursor must not move it backwards
  const p2 = ctx.loadBoard(true);
  await tick();
  ctx.__settle('board:A', boardPayload('A', 7));
  await p2;
  check('normal: cursor never regresses', ctx.__S.wsCursor === 42, ctx.__S.wsCursor);
}

/* ---------------- test 3: harness sensitivity (guard removed) ---------------- */
async function testGuardlessIsDetected() {
  const src = fs.readFileSync(APP_JS, 'utf8');
  const GUARD = 'if (S.board !== requestedBoard) return;';
  if (!src.includes(GUARD)) throw new Error('guard line missing from app.js — sensitivity test cannot run');
  const ctx = makeContext(src.replace(GUARD, '/* guard removed for sensitivity check */'));
  ctx.__S.board = 'A';

  const pA = ctx.loadBoard(true);
  await tick();
  await switchToB(ctx);
  ctx.__settle('board:A', boardPayload('A', 999));   // stale lands without the guard
  await pA.catch(() => {});
  await tick(); await tick();

  check('sensitivity: without the guard A overwrites B and restores A cursor',
    ctx.__S.data && ctx.__S.data.board === 'A' && ctx.__S.wsCursor === 999,
    { board: ctx.__S.data && ctx.__S.data.board, cursor: ctx.__S.wsCursor });
}

(async () => {
  await testRace();
  await testNormalRefresh();
  await testGuardlessIsDetected();
  console.log(failures === 0 ? '\nALL PASS' : `\n${failures} FAILURE(S)`);
  process.exit(failures === 0 ? 0 : 1);
})();
