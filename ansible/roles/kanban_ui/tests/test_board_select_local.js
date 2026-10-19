#!/usr/bin/env node
/* Local-board-select regression tests for the kanban-ui boards page
 * (app.js boardsPage).
 *
 * Proves the PR#13 round-2 review finding (inline comment 4301554450) stays
 * fixed: "The API's is_current flag describes the global Hermes board
 * pointer, not this tab's S.board. If another tab or the CLI selects board A
 * while this tab displays B, clicking A does nothing because b.is_current is
 * true. Compare the slug with S.board, as the row highlighting already does,
 * so that board remains selectable locally."
 *
 * Method (same vm harness style as test_board_refresh_race.js): app.js is
 * loaded into a vm context with minimal DOM/fetch stubs and the boot IIFE
 * stripped. Element stubs capture click listeners so the board-list rows
 * built by boardsPage() can be clicked by the test.
 *
 * Scenario: this tab displays B (S.board = 'B') while the SERVER-side global
 * pointer is on A (board A carries is_current: true in /api/kanban/boards).
 *   1. Clicking A's row must switch this tab to A (switch POST + S.board='A'
 *      + close callback fired) even though A is globally current.
 *   2. Clicking B's row (the board this tab already shows) must be a no-op:
 *      no switch request, no close.
 *   3. Sensitivity: the pre-fix condition (!b.is_current) is substituted and
 *      the same click is asserted NOT to switch — proving the harness
 *      actually detects the bug the fix addresses.
 *
 * Stdlib node only (no npm install). Run from anywhere:
 *
 *     node ansible/roles/kanban_ui/tests/test_board_select_local.js
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
    nodeType: 1, // so h() appends elements as-is instead of wrapping in text nodes
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

const API_PATHS = {
  board: '/api/kanban/board',
  boards: '/api/kanban/boards',
  assignees: '/api/kanban/assignees',
};

// Global pointer is on A (is_current), while the tab displays B.
const BOARDS = {
  boards: [
    { slug: 'A', name: 'Board A', is_current: true },
    { slug: 'B', name: 'Board B' },
  ],
};
const ASSIGNEES = { assignees: [] };

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

  // Strip the boot IIFE so nothing auto-fires at load time; expose S for the
  // assertions (script lexical scope is invisible on the sandbox otherwise).
  const boot = src.indexOf('(async function boot()');
  if (boot === -1) throw new Error('boot IIFE not found — app.js shape changed, update harness');
  const code = src.slice(0, boot) + '\n;globalThis.__S = S;';
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

const boardPayload = (name, cursor) => ({
  board: name, latest_event_id: cursor,
  columns: [{ name: 'ready', tasks: [{ id: name + '-task-1', title: 'task on ' + name, body: '', assignee: null, tenant: '' }] }],
});

const tick = () => new Promise(r => setImmediate(r));

/* Build the boards page while the tab shows B, settle the row-list load,
 * and return { list, closed } with closeCb bookkeeping. */
async function openBoardsList(ctx) {
  let closed = false;
  const page = ctx.boardsPage(() => { closed = true; });
  const list = page.children[0]; // first kid of the wrapper div is `list`
  await tick();
  ctx.__settle(API_PATHS.boards, BOARDS); // boardsPage refresh() -> loadBoards()
  await tick();
  return { list, wasClosed: () => closed };
}

/* Settle the full switchBoard() request fan-out for target slug. */
async function settleSwitch(ctx, slug) {
  await tick();
  ctx.__settle('/api/kanban/boards/' + slug + '/switch', {});
  await tick();
  ctx.__settle('board:' + slug, boardPayload(slug, 10));
  ctx.__settle(API_PATHS.boards, BOARDS);
  ctx.__settle(API_PATHS.assignees, ASSIGNEES);
  await tick();
}

const switchCallCount = (ctx, slug) =>
  ctx.__calls.filter(c => c.includes('/api/kanban/boards/' + slug + '/switch')).length;

/* ---------------- test 1: globally-current A is clickable from tab on B ---------------- */
async function testGlobalCurrentBoardSelectable() {
  const ctx = makeContext(fs.readFileSync(APP_JS, 'utf8'));
  ctx.__S.board = 'B';

  const { list, wasClosed } = await openBoardsList(ctx);
  check('rows rendered for both boards', list.children.length === 2, list.children.length);
  // children order follows S.boards order: [A, B]
  check('row order is [A, B]', list.children[0] && list.children[1]);
  const rowA = list.children[0];

  const pClick = rowA.__listeners.click();
  await settleSwitch(ctx, 'A');

  check('click on globally-current A issued the switch POST',
    switchCallCount(ctx, 'A') === 1, ctx.__calls.filter(c => c.includes('/switch')));
  check('tab-local S.board switched to A', ctx.__S.board === 'A', ctx.__S.board);
  check('close callback fired after selecting A', wasClosed() === true);
  check('A board data loaded into the tab', ctx.__S.data && ctx.__S.data.board === 'A', ctx.__S.data && ctx.__S.data.board);
}

/* ---------------- test 2: clicking the board this tab already shows is a no-op ---------------- */
async function testCurrentTabBoardNoOp() {
  const ctx = makeContext(fs.readFileSync(APP_JS, 'utf8'));
  ctx.__S.board = 'B';

  const { list, wasClosed } = await openBoardsList(ctx);
  const rowB = list.children[1]; // B is the board this tab displays
  await rowB.__listeners.click();
  await tick(); await tick();

  check('click on shown board B issued no switch POST', switchCallCount(ctx, 'B') === 0, ctx.__calls.filter(c => c.includes('/switch')));
  check('S.board unchanged after clicking B', ctx.__S.board === 'B', ctx.__S.board);
  check('close callback not fired for no-op click', wasClosed() === false);
}

/* ---------------- test 3: sensitivity (pre-fix condition restored) ---------------- */
async function testGuardlessIsDetected() {
  const src = fs.readFileSync(APP_JS, 'utf8');
  const FIXED = 'onclick: async () => { if (b.slug !== S.board) { await switchBoard(b.slug); if (closeCb) closeCb(); } },';
  const BUGGY = 'onclick: async () => { if (!b.is_current) { await switchBoard(b.slug); if (closeCb) closeCb(); } },';
  if (!src.includes(FIXED)) throw new Error('fixed click condition missing from app.js — sensitivity test cannot run');
  const ctx = makeContext(src.replace(FIXED, BUGGY));
  ctx.__S.board = 'B';

  const { list, wasClosed } = await openBoardsList(ctx);
  const rowA = list.children[0];
  await rowA.__listeners.click();
  await tick(); await tick();

  // With the old !b.is_current gate, A is globally current so the click
  // silently does nothing — this is exactly the reported bug. If the fix
  // regressed, this check FAILs because the click starts a switch request.
  check('sensitivity: pre-fix click on globally-current A is the dead no-op',
    switchCallCount(ctx, 'A') === 0 && ctx.__S.board === 'B' && wasClosed() === false,
    { board: ctx.__S.board, closed: wasClosed(), switches: ctx.__calls.filter(c => c.includes('/switch')) });
}

(async () => {
  await testGlobalCurrentBoardSelectable();
  await testCurrentTabBoardNoOp();
  await testGuardlessIsDetected();
  console.log(failures === 0 ? '\nALL PASS' : `\n${failures} FAILURE(S)`);
  process.exit(failures === 0 ? 0 : 1);
})();
