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
  ctx.__settle = (key, payload) => ctx.__settleOcc(key, 0, payload);
  // resolve the occ-th (0-based, oldest first) in-flight request for a key —
  // lets the same-board reverse-order test settle the NEWER request first.
  ctx.__settleOcc = (key, occ, payload) => {
    const matching = pending.filter(p => p.key === key);
    if (matching.length <= occ) throw new Error('no in-flight request #' + occ + ' for ' + key + ' (pending: ' + pending.map(p => p.key).join(', ') + ')');
    const p = matching[occ];
    pending.splice(pending.indexOf(p), 1);
    p.resolve({ ok: true, status: 200, statusText: 'OK', json: async () => payload });
  };
  return ctx;
}

const boardPayload = (name, cursor, taskStatus) => ({
  board: name, latest_event_id: cursor,
  columns: [{ name: 'ready', tasks: [{ id: name + '-task-1', title: 'task on ' + name, body: '', assignee: null, tenant: '', ...(taskStatus ? { status: taskStatus } : {}) }] }],
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

/* ---------------- test 3: same-board reverse-order completion (generation guard must discard the older poll) ---------------- */
/* PR#13 round-7 finding 4343269664: the 30s safety-net poll and a
 * WS/action-triggered refresh can both be in flight for the SAME board. The
 * board-name identity guard cannot tell them apart, so before the generation
 * guard the older snapshot resolving last overwrote the newer task state
 * (Done -> Running) while S.wsCursor sat at the newer value — and because
 * the WS replays only events strictly after the cursor, the clobbered state
 * was never healed. Harness: fire poll P1, fire refresh P2, settle P2 (the
 * NEWER, occ 1) first with the Done snapshot + cursor 10, then settle P1
 * (occ 0) last with the stale Running snapshot + cursor 5. */
async function testSameBoardReverseOrder() {
  const ctx = makeContext(fs.readFileSync(APP_JS, 'utf8'));
  ctx.__S.board = 'A';

  const pPoll = ctx.loadBoard(true);                       // P1: safety-net poll
  await tick();
  const pAct = ctx.loadBoard(true);                        // P2: WS/action refresh, same board
  await tick();
  check('same-board: two A /board requests in flight',
    ctx.__pending.filter(p => p.key === 'board:A').length === 2, ctx.__pending.map(p => p.key));

  ctx.__settleOcc('board:A', 1, boardPayload('A', 10, 'done'));   // newer resolves first
  await pAct;
  await tick();
  check('same-board: newer Done snapshot applied',
    ctx.__S.data && ctx.__S.data.columns[0].tasks[0].status === 'done', ctx.__S.data);
  check('same-board: cursor advanced to newer 10', ctx.__S.wsCursor === 10, ctx.__S.wsCursor);
  const rendersAfterNewer = ctx.__renderCount();

  // replay claim: the WS subscribes strictly after the cursor, so once the
  // cursor sits at 10 the Done event (id 10) is never replayed — a clobber
  // would stand until the next poll. Prove the URL really pins since=10.
  ctx.connectWS();
  check('same-board: WS replays only events after cursor 10',
    ctx.__calls.some(c => c.startsWith('ws:') && c.includes('since=10')),
    ctx.__calls.filter(c => c.startsWith('ws:')));

  ctx.__settleOcc('board:A', 0, boardPayload('A', 5, 'running')); // older poll resolves LAST
  await pPoll.catch(() => {});
  await tick(); await tick();

  check('same-board: stale Running snapshot never replaced Done',
    ctx.__S.data && ctx.__S.data.columns[0].tasks[0].status === 'done',
    ctx.__S.data && ctx.__S.data.columns[0].tasks[0].status);
  check('same-board: wsCursor preserved at newer value', ctx.__S.wsCursor === 10, ctx.__S.wsCursor);
  check('same-board: superseded response triggered no extra render',
    ctx.__renderCount() === rendersAfterNewer, { before: rendersAfterNewer, after: ctx.__renderCount() });
}

/* ---------------- test 4: harness sensitivity (guards removed) ---------------- */
async function testGuardlessIsDetected() {
  const src = fs.readFileSync(APP_JS, 'utf8');
  const GUARD = 'if (S.board !== requestedBoard) return;';
  const GEN_GUARD = 'if (gen !== boardReqGen) return;';
  if (!src.includes(GUARD)) throw new Error('identity guard line missing from app.js — sensitivity test cannot run');
  if (!src.includes(GEN_GUARD)) throw new Error('generation guard line missing from app.js — sensitivity test cannot run');

  // (a) cross-board race: with BOTH guards stripped, A's late payload lands
  // under B. Note the generation guard alone (identity guard stripped only)
  // would ALSO discard it now — switchBoard() bumped boardReqGen — so this
  // control must strip both to reproduce the round-2 bug.
  const both = src.replace(GUARD, '/* identity guard removed */').replace(GEN_GUARD, '/* generation guard removed */');
  const ctxA = makeContext(both);
  ctxA.__S.board = 'A';
  const pA = ctxA.loadBoard(true);
  await tick();
  await switchToB(ctxA);
  ctxA.__settle('board:A', boardPayload('A', 999));   // stale lands without the guards
  await pA.catch(() => {});
  await tick(); await tick();
  check('sensitivity: without both guards A overwrites B and restores A cursor',
    ctxA.__S.data && ctxA.__S.data.board === 'A' && ctxA.__S.wsCursor === 999,
    { board: ctxA.__S.data && ctxA.__S.data.board, cursor: ctxA.__S.wsCursor });

  // (b) same-board reverse order: strip ONLY the generation guard (identity
  // guard intact — it cannot catch this case) and the stale Running snapshot
  // must clobber Done while the cursor sits at the newer value.
  const genless = makeContext(src.replace(GEN_GUARD, '/* generation guard removed */'));
  genless.__S.board = 'A';
  const pPoll = genless.loadBoard(true);
  await tick();
  const pAct = genless.loadBoard(true);
  await tick();
  genless.__settleOcc('board:A', 1, boardPayload('A', 10, 'done'));
  await pAct;
  await tick();
  genless.__settleOcc('board:A', 0, boardPayload('A', 5, 'running'));
  await pPoll.catch(() => {});
  await tick(); await tick();
  check('sensitivity: without the generation guard the older Running snapshot clobbers Done',
    genless.__S.data && genless.__S.data.columns[0].tasks[0].status === 'running',
    genless.__S.data && genless.__S.data.columns[0].tasks[0].status);
}

(async () => {
  await testRace();
  await testNormalRefresh();
  await testSameBoardReverseOrder();
  await testGuardlessIsDetected();
  console.log(failures === 0 ? '\nALL PASS' : `\n${failures} FAILURE(S)`);
  process.exit(failures === 0 ? 0 : 1);
})();
