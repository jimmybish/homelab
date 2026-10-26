#!/usr/bin/env node
/* Local-board-select + keyboard-accessibility regression tests for the
 * kanban-ui boards page (app.js boardsPage).
 *
 * Proves two PR#13 review findings stay fixed, verbatim.
 *
 * Round-2 inline comment 4301554450: "The API's is_current flag describes
 * the global Hermes board pointer, not this tab's S.board. If another tab or
 * the CLI selects board A while this tab displays B, clicking A does nothing
 * because b.is_current is true. Compare the slug with S.board, as the row
 * highlighting already does, so that board remains selectable locally."
 *
 * Round-9 inline comment 4369296886 (keyboard operability, W3C ACT 307n5z
 * — same rule the card fix used): "The board selector renders each
 * selectable board as a `div` with only a click listener. Keyboard users
 * can open the switcher with `#boardBtn`, but cannot focus or activate any
 * board row, so they cannot switch boards. Add a labelled native
 * board-selection button, keeping the existing archive button as a separate
 * sibling rather than nesting it inside that button."
 * The accepted shape: row wrapper div > native <button type="button"
 * class="board-select" aria-label=...> carrying the row content, and the
 * archive <button> as its SIBLING (never nested: button descendants of a
 * button are invalid and lose their own tab stop/focus ring).
 *
 * Method (same vm harness style as test_board_refresh_race.js): app.js is
 * loaded into a vm context with minimal DOM/fetch stubs and the boot IIFE
 * stripped. Element stubs capture tag, className, setAttribute and click
 * listeners so the board-list rows built by boardsPage() can be clicked AND
 * structurally asserted (ancestor checks like test_card_keyboard_access.js).
 *
 * Scenarios: this tab displays B (S.board = 'B') while the SERVER-side
 * global pointer is on A (board A carries is_current: true):
 *   1. Clicking A's select button must switch this tab to A (switch POST +
 *      S.board='A' + close callback fired) even though A is globally current.
 *   2. Clicking B's row (the board this tab already shows) must be a no-op.
 *   3. STRUCTURE (finding 4369296886): each row is a wrapper div containing
 *      a native <button type="button"> board-select control with an
 *      aria-label, plus the archive button as a SIBLING outside it.
 *   4. Sensitivity: the pre-fix condition (!b.is_current) is substituted and
 *      the same click is asserted NOT to switch; and the archive button is
 *      re-nested inside the select button and asserted to be detected.
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
// Tag, className and attributes are RECORDED (and parent links) so the
// finding-4369296886 structural assertions can walk the real tree the
// renderer builds: the selectable row must be a native <button> and the
// archive control must not sit inside it.
function elStub(tag) {
  const el = {
    nodeType: 1, // so h() appends elements as-is instead of wrapping in text nodes
    tag: tag || 'div',
    className: '',
    children: [], style: {}, dataset: {}, classList: { add() {}, remove() {}, toggle() {} },
    __listeners: {}, attrs: {},
    parent: null,
    append(...k) { this.children.push(...k); for (const c of k) if (c && typeof c === 'object') c.parent = this; },
    replaceChildren(...k) { this.children = k; for (const c of k) if (c && typeof c === 'object') c.parent = this; },
    addEventListener(ev, fn) { this.__listeners[ev] = fn; },
    setAttribute(name, value) { this.attrs[name] = String(value); },
    remove() {}, focus() {}, contains() { return false; },
    querySelector() { return elStub(); }, querySelectorAll() { return []; },
    textContent: '', innerHTML: '', hidden: false, value: '', checked: false, disabled: false,
  };
  return el;
}

// h() assigns className directly; class membership check for both paths.
function hasCls(node, cls) {
  if (!node) return false;
  return (node.className || '').split(/\s+/).includes(cls);
}

// The labelled native board-selection button inside a row wrapper.
function selectBtnOf(row) {
  const btn = (row.children || []).find(c => c && c.tag === 'button' && hasCls(c, 'board-select'));
  if (!btn) throw new Error('board-select button missing — boardsPage shape changed, update harness');
  return btn;
}

function archiveBtnOf(row) {
  return (row.children || []).find(c => c && c.tag === 'button' && hasCls(c, 'danger')) || null;
}

function buttonAncestor(node) {
  for (let p = node && node.parent; p; p = p.parent) if (p.tag === 'button' || p.attrs.role === 'button') return p;
  return null;
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
      addEventListener() {}, createElement: (tag) => elStub(tag), createTextNode: t => ({ t }),
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

  const selA = selectBtnOf(rowA);
  const pClick = selA.__listeners.click();
  await settleSwitch(ctx, 'A');

  check('activating globally-current A issued the switch POST',
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
  await selectBtnOf(rowB).__listeners.click();
  await tick(); await tick();

  check('activating shown board B issued no switch POST', switchCallCount(ctx, 'B') === 0, ctx.__calls.filter(c => c.includes('/switch')));
  check('S.board unchanged after clicking B', ctx.__S.board === 'B', ctx.__S.board);
  check('close callback not fired for no-op click', wasClosed() === false);
  // The wrapper div itself must NOT carry a click handler anymore — keyboard
  // focus/activation lives on the native button (finding 4369296886).
  check('row wrapper is a plain non-interactive div', rowB.tag === 'div' && !rowB.__listeners.click, { tag: rowB.tag, hasClick: !!rowB.__listeners.click });
}

/* ---------------- test 3: STRUCTURE (finding 4369296886) ---------------- */
async function testRowStructure() {
  const ctx = makeContext(fs.readFileSync(APP_JS, 'utf8'));
  ctx.__S.board = 'B';

  const { list } = await openBoardsList(ctx);
  const rowA = list.children[0]; // not current, not archived -> has archive sibling
  const rowB = list.children[1]; // current board -> no archive button

  const selA = selectBtnOf(rowA);
  check('select control is a native <button type="button">', selA.tag === 'button' && selA.attrs.type === 'button', { tag: selA.tag, type: selA.attrs.type });
  check('select control carries an accessible name naming the board',
    typeof selA.attrs['aria-label'] === 'string' && selA.attrs['aria-label'].includes('Board A'), selA.attrs);
  check('current board select control announces itself as current',
    (selectBtnOf(rowB).attrs['aria-label'] || '').startsWith('Current board:'), selectBtnOf(rowB).attrs);
  const arch = archiveBtnOf(rowA);
  check('archive control exists for a non-current non-archived board', !!arch, rowA.children.map(c => c && c.tag));
  check('archive button is a SIBLING of the select button (not nested)', arch && arch.parent === rowA && selA.parent === rowA, arch && { parentTag: arch.parent && arch.parent.tag });
  check('ACT 307n5z: archive button has no button ancestor', arch && buttonAncestor(arch) === null, arch && !!buttonAncestor(arch));
  check('archive button has its own accessible name', arch && (arch.attrs['aria-label'] || '').includes('Archive board Board A'), arch && arch.attrs);
  check('row content spans live INSIDE the select button',
    selA.children.some(c => c && hasCls(c, 'bname')) && selA.children.some(c => c && hasCls(c, 'board-icon')),
    selA.children.map(c => c && c.className));
  check('current board row has no archive control', archiveBtnOf(rowB) === null, rowB.children.map(c => c && c.tag));
}

/* ---------------- test 4a: sensitivity (pre-fix condition restored) ---------------- */
async function testGuardlessIsDetected() {
  const src = fs.readFileSync(APP_JS, 'utf8');
  const FIXED = 'onclick: async () => { if (b.slug !== S.board) { await switchBoard(b.slug); if (closeCb) closeCb(); } },';
  const BUGGY = 'onclick: async () => { if (!b.is_current) { await switchBoard(b.slug); if (closeCb) closeCb(); } },';
  if (!src.includes(FIXED)) throw new Error('fixed click condition missing from app.js — sensitivity test cannot run');
  const ctx = makeContext(src.replace(FIXED, BUGGY));
  ctx.__S.board = 'B';

  const { list, wasClosed } = await openBoardsList(ctx);
  const rowA = list.children[0];
  await selectBtnOf(rowA).__listeners.click();
  await tick(); await tick();

  // With the old !b.is_current gate, A is globally current so the click
  // silently does nothing — this is exactly the reported bug. If the fix
  // regressed, this check FAILs because the click starts a switch request.
  check('sensitivity: pre-fix click on globally-current A is the dead no-op',
    switchCallCount(ctx, 'A') === 0 && ctx.__S.board === 'B' && wasClosed() === false,
    { board: ctx.__S.board, closed: wasClosed(), switches: ctx.__calls.filter(c => c.includes('/switch')) });
}

/* ---------------- test 4b: sensitivity (pre-fix div rows are detected) ---------------- */
async function testDivRowShapeIsDetected() {
  const src = fs.readFileSync(APP_JS, 'utf8');
  const FIXED = '      h("button", {\n        type: "button",\n        class: "board-select",';
  const PRE_FIX = '      h("div", {\n        class: "board-select",';
  if (!src.includes(FIXED)) throw new Error('native board-select button missing from app.js — fix regressed or was rewritten, update harness');
  const ctx = makeContext(src.replace(FIXED, PRE_FIX));
  ctx.__S.board = 'B';
  const { list } = await openBoardsList(ctx);
  let detected = false;
  try { selectBtnOf(list.children[0]); } catch { detected = true; }
  check('sensitivity: harness detects a div row with no native select button', detected === true);
}

/* ---------------- test 4c: sensitivity (nested archive is detected) ---------------- */
async function testNestedArchiveIsDetected() {
  const src = fs.readFileSync(APP_JS, 'utf8');
  const FIXED = 'h("span", { class: "bmeta" }, `${b.total ?? "?"} tasks`, h("br"), b.slug),\n      ),';
  const NESTED = 'h("span", { class: "bmeta" }, `${b.total ?? "?"} tasks`, h("br"), b.slug),\n        b.slug !== S.board && !b.archived ? h("button", { class: "btn btn-sm btn-quiet danger", onclick: async (e) => { e.stopPropagation(); } }, "x") : null,\n      ),';
  if (!src.includes(FIXED)) throw new Error('row structure changed — sensitivity test cannot run, update harness');
  const ctx = makeContext(src.replace(FIXED, NESTED));
  ctx.__S.board = 'B';
  const { list } = await openBoardsList(ctx);
  const selA = selectBtnOf(list.children[0]);
  const nested = (selA.children || []).find(c => c && c.tag === 'button' && hasCls(c, 'danger'));
  check('sensitivity: harness detects an archive button nested inside the select button',
    !!nested && buttonAncestor(nested) !== null, nested && { parentTag: nested.parent && nested.parent.tag });
}

(async () => {
  await testGlobalCurrentBoardSelectable();
  await testCurrentTabBoardNoOp();
  await testRowStructure();
  await testGuardlessIsDetected();
  await testDivRowShapeIsDetected();
  await testNestedArchiveIsDetected();
  console.log(failures === 0 ? '\nALL PASS' : `\n${failures} FAILURE(S)`);
  process.exit(failures === 0 ? 0 : 1);
})();
