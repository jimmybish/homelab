#!/usr/bin/env node
/* Regression tests for the re-login overlay stacking + focus contract
 * (app.js showLogin/authOverlayOpen/authOverlayDismiss, app.css scale).
 *
 * PR#13 round-6 inline comment 4331497777 (verbatim):
 *   "When an API request returns 401 with a drawer open, this login overlay
 *    uses z-index 60, below the drawer scrim (70) and drawer (80). On mobile
 *    the full-width drawer hides the re-login form; existing modals and menus
 *    also cover it. Place the authentication overlay above those layers so
 *    users can reauthenticate without first dismissing them or reloading."
 *
 * The accepted shape:
 *   1. app.css carries a DOCUMENTED stacking scale (CSS custom properties,
 *      comment block in :root) and EVERY z-index in the stylesheet — and the
 *      two inline menu z-indexes in app.js — references a scale token. No
 *      one-off magic integers anywhere.
 *   2. The auth overlay is TOPMOST: --z-auth is strictly greater than every
 *      other tier (drawer scrim 70, drawer 80, modal 90, menu scrim 95,
 *      menu 100, toasts 120), delivered via the .auth-overlay class, with
 *      NO inline z-index on the overlay element.
 *   3. The overlay is a labelled modal dialog (role=dialog, aria-modal,
 *      aria-label); focus moves into the token input when it appears.
 *   4. Successful reauth is a SOFT dismiss: overlay removed, no
 *      location.reload(), focus restored to the previously focused element,
 *      and a FULL authenticated bootstrap (postLoginBootstrap: config,
 *      boards, assignees, board, drawer, workers, model-option cache reset,
 *      WebSocket reconnect) — so an open drawer/modal survives AND an
 *      unauthenticated cold boot recovers every protected load without a
 *      page reload (PR#13 round-9 blocking finding t_d7bdedae: server.py
 *      gates every /api/ route on the session cookie, so the old three-call
 *      catch-up left selector/config state empty until a manual reload).
 *   5. Non-401 stacking is UNCHANGED: the scale values reproduce the exact
 *      pre-fix numbers for topbar/trash/bulkbar/scrim/drawer/modal/menu/
 *      toasts (a silent renumbering of those layers would be a regression).
 *
 * Method (same vm harness style as test_card_keyboard_access.js): app.js is
 * loaded into a vm context with minimal DOM/fetch stubs; the boot IIFE is
 * stripped so nothing auto-fires. Element stubs RECORD parent links,
 * attributes, style strings, focus calls and real removal from parents, so
 * the assertions run the SHIPPED showLogin code end to end rather than
 * grepping it. Two sensitivity probes prove the suite detects the original
 * bug shape and a renumbered-down auth tier.
 *
 * Stdlib node only (no npm install). Run from anywhere:
 *
 *     node ansible/roles/kanban_ui/tests/test_auth_overlay_stacking.js
 *
 * Exit 0 = all pass. Any failure prints and exits 1.
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const APP_JS = path.join(__dirname, '..', 'files', 'app', 'static', 'app.js');
const APP_CSS = path.join(__dirname, '..', 'files', 'app', 'static', 'app.css');
const MD_JS = path.join(__dirname, '..', 'files', 'app', 'static', 'md.js');

const appJs = fs.readFileSync(APP_JS, 'utf8');
const appCss = fs.readFileSync(APP_CSS, 'utf8');

let failures = 0;
function check(name, cond, detail) {
  if (cond) {
    console.log('PASS  ' + name);
  } else {
    failures += 1;
    console.log('FAIL  ' + name + (detail !== undefined ? '  -> got: ' + JSON.stringify(detail) : ''));
  }
}

/* ---------------- static CSS scale checks ---------------- */

function parseScale(css) {
  const scale = {};
  for (const m of css.matchAll(/(--z-[a-z-]+)\s*:\s*(\d+)/g)) scale[m[1]] = Number(m[2]);
  return scale;
}

const EXPECTED = {
  '--z-topbar': 40, '--z-trash': 55, '--z-bulkbar': 60, '--z-scrim': 70,
  '--z-drawer': 80, '--z-modal': 90, '--z-menu-scrim': 95, '--z-menu': 100,
  '--z-toasts': 120,
};

const scale = parseScale(appCss);
check('scale declares every documented tier',
  Object.keys(EXPECTED).concat('--z-auth').every(k => k in scale), scale);
check('non-auth tiers keep their exact pre-fix values (no silent renumbering)',
  Object.entries(EXPECTED).every(([k, v]) => scale[k] === v), scale);
check('--z-auth is TOPMOST (strictly above toasts, the highest old layer)',
  scale['--z-auth'] > scale['--z-toasts'], scale);
{
  const vals = Object.entries(scale).filter(([k]) => k !== '--z-auth');
  check('ordering is strict: every lower tier < --z-auth',
    vals.every(([, v]) => v < scale['--z-auth']), scale);
}
check('scale is documented (comment block explains the stacking order)',
  /stacking scale/i.test(appCss) && /TOPMOST/i.test(appCss));

// No one-off magic z-index integers left in the stylesheet.
const rawZ = [...appCss.matchAll(/z-index:\s*(\d+)/g)].map(m => m[1]);
check('every z-index in app.css references a var(--z-*) token',
  rawZ.length === 0, rawZ);
// And every var(--z-*) used is declared.
const declared = new Set(Object.keys(scale));
for (const m of appCss.matchAll(/--z-[a-z-]+\s*:/g)) declared.add(m[0].replace(/\s*:$/, ''));
const usedTokens = [...appCss.matchAll(/var\(\s*(--z-[a-z-]+)/g)].map(m => m[1]);
check('every var(--z-*) in app.css is declared', usedTokens.every(t => declared.has(t)), usedTokens);

const authRule = appCss.match(/\.auth-overlay\s*\{([^}]*)\}/);
check('.auth-overlay rule exists in app.css', authRule !== null);
if (authRule) {
  check('.auth-overlay pins z-index to var(--z-auth)',
    /z-index:\s*var\(\s*--z-auth\s*\)/.test(authRule[1]), authRule[1]);
  check('.auth-overlay centres the panel (old inline flex moves here)',
    /display:\s*flex/.test(authRule[1]) && /align-items:\s*center/.test(authRule[1]) && /justify-content:\s*center/.test(authRule[1]), authRule[1]);
}

/* ---------------- static app.js checks ---------------- */

const showStart = appJs.indexOf('function showLogin()');
const showEnd = appJs.indexOf('function connectWS()');
check('showLogin region locatable in app.js', showStart !== -1 && showEnd > showStart);
const loginRegion = appJs.slice(appJs.indexOf('function authOverlayOpen'), showEnd);
check('no inline z-index anywhere in the auth overlay code', !/z-index/.test(loginRegion));
check('menu overlay code uses scale tokens, not raw integers',
  appJs.includes('z-index:var(--z-menu)') && appJs.includes('z-index:var(--z-menu-scrim)')
  && !/z-index:\s*\d+/.test(appJs.slice(appJs.indexOf('function openCardMenu'), appJs.indexOf('/* ---------------- modals'))));
check('reauth success path no longer hard-reloads (whole file; only prose may mention it)',
  appJs.split('location.reload(').length - 1 - (appJs.match(/\* .*location\.reload\(\)/g) || []).length === 0,
  (appJs.match(/.*location\.reload\(.*/g) || []));

/* ---------------- vm harness ---------------- */

function elStub(tag) {
  const el = {
    nodeType: 1,
    tag: tag || 'div',
    children: [], style: {}, dataset: {},
    parent: null,
    classList: { _s: new Set(), add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); }, contains(c) { return this._s.has(c); } },
    attrs: {},
    listeners: {},
    append(...k) { this.children.push(...k); for (const c of k) if (c && typeof c === 'object') c.parent = this; },
    replaceChildren(...k) { this.children = k; for (const c of k) if (c && typeof c === 'object') c.parent = this; },
    addEventListener(ev, fn) { (this.listeners[ev] || (this.listeners[ev] = [])).push(fn); },
    setAttribute(name, value) { this.attrs[name] = String(value); },
    remove() {
      const p = this.parent;
      if (p) { p.children = p.children.filter(c => c !== this); this.parent = null; }
    },
    focusCalls: 0,
    focus() { el._doc.activeElement = this; this.focusCalls += 1; },
    contains() { return false; },
    querySelector() { return elStub(); }, querySelectorAll() { return [] },
    textContent: '', innerHTML: '', hidden: false, value: '', checked: false,
  };
  return el;
}

function descendantById(root, id) {
  for (const c of root.children || []) {
    if (!c || typeof c !== 'object') continue;
    if (c.attrs && c.attrs.id === id) return c;
    const hit = descendantById(c, id);
    if (hit) return hit;
  }
  return null;
}

function makeContext(opts = {}) {
  // Round-9 cold-login harness (t_b73b3189): keepBoot:true runs the real
  // boot IIFE against a fetch stub we control, so a cold unauthenticated
  // visit really burns its protected loads on 401s and the login submit has
  // to recover them. Default (keepBoot:false) keeps the original
  // strip-the-boot behaviour of this suite. src overrides the app.js text
  // (sensitivity probes feed a mutated build through the same harness).
  const keepBoot = !!opts.keepBoot;
  const src = typeof opts.src === 'string' ? opts.src : appJs;
  const win = { addEventListener() {} };
  const body = elStub('body');
  const doc = {
    hidden: false, title: '', activeElement: null,
    addEventListener() {},
    createElement: (tag) => elStub(tag),
    createTextNode: t => ({ nodeType: 3, text: String(t) }),
    body, documentElement: { dataset: {} },
    querySelector: () => elStub(),
    querySelectorAll: () => [],
    getElementById: (id) => descendantById(body, id),
    contains: (node) => {
      for (let p = node; p; p = p.parent) if (p === body) return true;
      return false;
    },
  };
  // focus() needs the doc backlink for activeElement bookkeeping.
  const link = (el) => { el._doc = doc; return el; };
  const origCreate = doc.createElement;
  doc.createElement = (tag) => link(origCreate(tag));
  body._doc = doc;
  link(body);

  const ctx = {
    window: win,
    console: { error() {}, log() {} },
    URL, Set, Map, WeakMap, Object, Array, JSON, Math, Date, String, Number, Boolean, Error, Promise, RegExp,
    parseInt, parseFloat, isNaN, encodeURIComponent, decodeURIComponent,
    innerWidth: 1600, innerHeight: 900,
    setTimeout: () => 0, clearTimeout: () => {}, setInterval: () => 0, clearInterval: () => {},
    localStorage: { getItem: () => null, setItem() {} },
    location: { origin: 'https://kanban.example.internal', protocol: 'https:', host: 'kanban.example.internal', hostname: 'kanban.example.internal', pathname: '/', reloadCalls: 0, reload() { ctx.location.reloadCalls += 1; } },
    WebSocket: function WS(url) { this.close = () => {}; this.readyState = 1; },
    document: doc,
    fetch: () => new Promise(() => {}),
  };
  win.KanbanMd = require(MD_JS);

  // Round-9 cold-login harness (t_b73b3189): authed:false makes every
  // /api/ fetch answer 401 exactly like server.py's auth_mw before a
  // session cookie exists; POST /login flips authed and answers 200.
  const state = { authed: false, ws: 0, fetchLog: [] };
  ctx.__state = state;

  const boot = src.indexOf('(async function boot()');
  if (boot === -1) throw new Error('boot IIFE not found — app.js shape changed, update harness');
  const code = src.slice(0, keepBoot ? src.length : boot)
    + '\n;globalThis.__S = S;'
    + (keepBoot ? '\n;render = () => { globalThis.__renders = (globalThis.__renders || 0) + 1; };' : '');

  if (keepBoot) {
    // The boot IIFE suspends at its FIRST await fetch(), which happens
    // synchronously inside runInContext — the session-gated stub and the
    // socket-counting WebSocket must therefore be installed BEFORE eval.
    ctx.fetch = (url, opts = {}) => {
      const u = String(url);
      state.fetchLog.push((opts.method || 'GET') + ' ' + u);
      if (u === '/login') {
        state.authed = true;
        return Promise.resolve({ ok: true, status: 200, statusText: 'OK', json: async () => ({ ok: true }) });
      }
      if (!state.authed) {
        return Promise.resolve({ ok: false, status: 401, statusText: 'Unauthorized', json: async () => ({ detail: 'unauthorized' }) });
      }
      const p = new URL(u).pathname;
      const payload = p === '/api/kanban/config' ? { lane_by_profile: true, include_archived_by_default: false }
        : p === '/api/kanban/boards' ? { boards: [{ slug: 'default', name: 'Default Board' }] }
        : p === '/api/kanban/assignees' ? { assignees: ['alice', 'virtuajimmy'] }
        : p === '/api/kanban/board' ? { board: 'default', latest_event_id: 5, columns: [{ name: 'ready', tasks: [{ id: 't_1', title: 'task one', body: '', assignee: 'alice', tenant: '' }] }] }
        : p === '/api/kanban/workers/active' ? { count: 2 }
        : {};
      return Promise.resolve({ ok: true, status: 200, statusText: 'OK', json: async () => payload });
    };
    const WSBase = ctx.WebSocket;
    ctx.WebSocket = function WS(url) { state.ws += 1; state.fetchLog.push('ws:' + String(url)); return new WSBase(url); };
  }

  vm.createContext(ctx);
  vm.runInContext(code, ctx, { filename: 'app.js' });

  if (keepBoot) return ctx;   // real loaders run; no spies installed

  // Spies resolved on the context global at call time (same trick as
  // test_card_keyboard_access.js): the post-login bootstrap must hit ALL of
  // these — the full protected catch-up (round-9 finding t_d7bdedae), not
  // just the three visibility-refresh calls.
  ctx.__refresh = { board: 0, drawer: 0, workers: 0, cfg: 0, boards: 0, assignees: 0, ws: 0 };
  ctx.loadBoard = async () => { ctx.__refresh.board += 1; };
  ctx.refreshDrawer = async () => { ctx.__refresh.drawer += 1; };
  ctx.pollWorkers = async () => { ctx.__refresh.workers += 1; };
  ctx.loadCfg = async () => { ctx.__refresh.cfg += 1; };
  ctx.loadBoards = async () => { ctx.__refresh.boards += 1; };
  ctx.loadAssignees = async () => { ctx.__refresh.assignees += 1; };
  ctx.connectWS = () => { ctx.__refresh.ws += 1; };
  return ctx;
}

function hasCls(node, cls) {
  if (!node) return false;
  if ((node.className || '').split(/\s+/).includes(cls)) return true;
  return !!(node.classList && node.classList._s && node.classList._s.has(cls));
}

async function main() {
  /* ---- 1. BEHAVIOR: 401 while a drawer is open -> overlay above all ---- */
  {
    const ctx = makeContext();
    // Simulate focus sitting on a drawer control before the 401 arrives.
    const drawerBtn = ctx.document.createElement('button');
    drawerBtn.attrs['aria-label'] = 'Open task: Demo';
    ctx.document.body.append(drawerBtn);
    drawerBtn.focus();
    ctx.__fetchMode = 'ok';
    ctx.fetch = (url) => {
      ctx.__lastFetchUrl = String(url);
      return Promise.resolve({ ok: true, status: 200, json: async () => ({}) });
    };

    ctx.showLogin();  // what api() calls on res.status === 401

    const overlay = ctx.document.getElementById('loginOverlay');
    check('401 with drawer open renders #loginOverlay', overlay !== null);
    check('overlay carries scrim + auth-overlay classes (topmost tier via CSS)',
      hasCls(overlay, 'scrim') && hasCls(overlay, 'auth-overlay'), overlay && overlay.className);
    check('overlay sets NO inline z-index (scale token comes from CSS)',
      overlay && Object.keys(overlay.style).length === 0, overlay && overlay.style);
    check('overlay is a labelled modal dialog (a11y)',
      overlay && overlay.attrs.role === 'dialog' && overlay.attrs['aria-modal'] === 'true'
      && typeof overlay.attrs['aria-label'] === 'string' && overlay.attrs['aria-label'].length > 0,
      overlay && overlay.attrs);

    // Focus landed in the token input.
    const input = (overlay.children[0].children || []).find(c => c && c.tag === 'input');
    check('token input exists inside the panel', input !== undefined && input !== null);
    check('focus moved into the re-login token field', input && ctx.document.activeElement === input,
      ctx.document.activeElement && ctx.document.activeElement.tag);
    check('input is a password field (no plaintext credential echo)',
      input && input.attrs.type === 'password', input && input.attrs);

    // Submit the token -> soft dismiss, focus restored, catch-up refresh.
    const btn = (overlay.children[0].children || []).find(c => c && c.tag === 'button');
    input.value = 'a-token';
    await btn.onclick();

    check('successful reauth removes the overlay', ctx.document.getElementById('loginOverlay') === null);
    check('successful reauth does NOT reload the page (drawer view survives)',
      ctx.location.reloadCalls === 0, ctx.location.reloadCalls);
    check('focus returns to the previously focused drawer control',
      ctx.document.activeElement === drawerBtn, ctx.document.activeElement && ctx.document.activeElement.tag);
    check('successful reauth runs the FULL protected bootstrap (cfg/boards/assignees/board/drawer/workers/WS)',
      ctx.__refresh.board === 1 && ctx.__refresh.drawer === 1 && ctx.__refresh.workers === 1 &&
      ctx.__refresh.cfg === 1 && ctx.__refresh.boards === 1 && ctx.__refresh.assignees === 1 &&
      ctx.__refresh.ws === 1,
      ctx.__refresh);
    check('login POST goes to /login only', ctx.__lastFetchUrl === '/login', ctx.__lastFetchUrl);
  }

  /* ---- 2. BEHAVIOR: failed login keeps overlay, keeps focus, no refresh ---- */
  {
    const ctx = makeContext();
    const opener = ctx.document.createElement('button');
    ctx.document.body.append(opener);
    opener.focus();
    ctx.fetch = () => Promise.resolve({ ok: false, status: 401, json: async () => ({ detail: 'bad token' }) });

    ctx.showLogin();
    const overlay = ctx.document.getElementById('loginOverlay');
    const panel = overlay.children[0];
    const input = (panel.children || []).find(c => c && c.tag === 'input');
    const btn = (panel.children || []).find(c => c && c.tag === 'button');
    input.value = 'nope';
    await btn.onclick();

    check('failed reauth keeps the overlay up', ctx.document.getElementById('loginOverlay') !== null);
    check('failed reauth shows the server detail',
      (panel.children || []).some(c => c && String(c.textContent) === 'bad token'));
    check('failed reauth refocuses the input, restores the button',
      ctx.document.activeElement === input && btn.disabled === false,
      { active: ctx.document.activeElement && ctx.document.activeElement.tag, disabled: btn.disabled });
    check('failed reauth runs NO catch-up refresh',
      ctx.__refresh.board === 0 && ctx.__refresh.drawer === 0, ctx.__refresh);
  }

  /* ---- 3. BEHAVIOR: overlay dismissal when the old focus is gone ---- */
  {
    const ctx = makeContext();
    // Focus on an element that will be detached (e.g. card re-rendered away).
    const doomed = ctx.document.createElement('button');
    ctx.document.body.append(doomed);
    doomed.focus();
    ctx.fetch = () => Promise.resolve({ ok: true, status: 200, json: async () => ({}) });
    ctx.showLogin();
    const overlay = ctx.document.getElementById('loginOverlay');
    const btn = (overlay.children[0].children || []).find(c => c && c.tag === 'button');
    const inp = (overlay.children[0].children || []).find(c => c && c.tag === 'input');
    inp.value = 'a-token';
    doomed.remove();   // detached while the overlay was open
    await btn.onclick();
    check('detached previous focus falls back to body (no crash, focus lands somewhere)',
      ctx.document.activeElement === ctx.document.body,
      ctx.document.activeElement && ctx.document.activeElement.tag);
  }

  /* ---- 4. BEHAVIOR: idempotent — second 401 while overlay open is a no-op ---- */
  {
    const ctx = makeContext();
    ctx.showLogin();
    const first = ctx.document.getElementById('loginOverlay');
    ctx.showLogin();
    const second = ctx.document.getElementById('loginOverlay');
    check('concurrent 401s render exactly one overlay', first !== null && first === second);
  }

  /* ---- 5. BEHAVIOR: COLD unauthenticated boot -> login recovers ALL
   * protected state without location.reload() (round-9 finding t_d7bdedae) ---- */
  async function coldBootAndLogin(src, opts = {}) {
    const ctx = makeContext({ keepBoot: true, ...(opts.breakSrc ? { src } : {}) });
    const tick = () => new Promise(r => setImmediate(r));
    await tick(); await tick(); await tick();   // let the boot IIFE settle on 401s

    const pre = {
      boards: (ctx.__S.boards || []).length,
      assignees: (ctx.__S.assignees || []).length,
      cfgKeys: Object.keys(ctx.__S.cfg || {}).length,
      data: ctx.__S.data,
      ws: ctx.__state.ws,
    };
    const overlay = ctx.document.getElementById('loginOverlay');
    // The pre-login assertions are the round-9 finding itself; sensitivity
    // probes skip them but must still run the real login submit below, or
    // their "bug reproduced" checks would pass vacuously.
    if (opts.assert !== false) {
      check('cold boot: protected loads landed as 401 (cfg/boards/assignees/board all empty)',
        pre.boards === 0 && pre.assignees === 0 && pre.cfgKeys === 0 && pre.data === null, pre);
      check('cold boot: boot still opened one (unauthenticated) event socket', pre.ws === 1, pre.ws);
      check('cold boot: 401 surfaced the login overlay', overlay !== null);
    }
    // Simulate a pre-login 401 poisoning the model-option cache (a create
    // modal opened before authenticating caches {providers: []} on failure).
    ctx.__S.modelOpts = { providers: ['stale-pre-login'] };
    const panel = overlay.children[0];
    const input = (panel.children || []).find(c => c && c.tag === 'input');
    const btn = (panel.children || []).find(c => c && c.tag === 'button');
    input.value = 'a-token';
    await btn.onclick();
    await tick(); await tick(); await tick();

    return { ctx, pre, overlay };
  }
  {
    const { ctx, pre } = await coldBootAndLogin(appJs);
    const S = ctx.__S;
    check('cold login: overlay removed (soft dismiss)', ctx.document.getElementById('loginOverlay') === null);
    check('cold login: NO location.reload()', ctx.location.reloadCalls === 0, ctx.location.reloadCalls);
    check('cold login: /login POST happened', ctx.__state.fetchLog.some(l => l === 'POST /login'), ctx.__state.fetchLog);
    check('cold login: protected config recovered',
      S.cfg && S.cfg.lane_by_profile === true, S.cfg);
    check('cold login: board list recovered',
      Array.isArray(S.boards) && S.boards.some(b => b.slug === 'default'), S.boards);
    check('cold login: assignees recovered',
      Array.isArray(S.assignees) && S.assignees.length === 2, S.assignees);
    check('cold login: board payload recovered and rendered',
      S.data && S.data.board === 'default' && (ctx.__renders || 0) >= 1, { data: S.data && S.data.board, renders: ctx.__renders });
    check('cold login: model-option cache reset (no poisoned empty provider list survives)',
      S.modelOpts === null, S.modelOpts);
    const wsIdx = ctx.__state.fetchLog.reduce((last, l, i) => l.startsWith('ws:') ? i : last, -1);
    check('cold login: authenticated event socket connected (2nd socket, after authed POST)',
      ctx.__state.ws === 2 && wsIdx > ctx.__state.fetchLog.indexOf('POST /login'),
      ctx.__state.fetchLog.filter(l => l.startsWith('ws:')));
    check('cold login: protected GETs re-issued AFTER the login POST',
      ctx.__state.fetchLog.lastIndexOf('GET https://kanban.example.internal/api/kanban/config?board=default')
      > ctx.__state.fetchLog.indexOf('POST /login'), ctx.__state.fetchLog);

    /* ---- 6. SENSITIVITY C: the old three-call catch-up must be detected ---- */
    const brokenSrc = appJs.replace(
      'await postLoginBootstrap();',
      'loadBoard(true); refreshDrawer(); pollWorkers();');
    check('SENSITIVITY C source probe: old three-call line is a real substitution',
      brokenSrc !== appJs && /loadBoard\(true\); refreshDrawer\(\); pollWorkers\(\);/.test(
        brokenSrc.slice(brokenSrc.indexOf('const submit = async'), brokenSrc.indexOf('btn.onclick'))));
    {
      const { ctx: bctx, pre: bpre } = await coldBootAndLogin(brokenSrc, { breakSrc: true, assert: false });
      // Re-run assertions manually on the broken build: cfg/boards/assignees
      // must STAY empty — that is the regression the round-9 finding named.
      check('SENSITIVITY C: three-call catch-up leaves config/boards/assignees empty (old bug reproduced)',
        Object.keys(bctx.__S.cfg || {}).length === bpre.cfgKeys && bctx.__S.boards.length === 0 && bctx.__S.assignees.length === 0,
        { cfg: bctx.__S.cfg, boards: bctx.__S.boards, assignees: bctx.__S.assignees });
      check('SENSITIVITY C: three-call catch-up never reopens the event socket',
        bctx.__state.ws === 1, bctx.__state.ws);
    }
  }

  /* ---- 7. SENSITIVITY A: original bug shape must FAIL the suite ---- */
  {
    const brokenCss = appCss
      .replace(/\.auth-overlay\s*\{[^}]*\}/, '.auth-overlay { z-index: 60; display: flex; }')
      .replace(/--z-auth:\s*130/, '--z-auth: 60');
    const bScale = parseScale(brokenCss);
    check('SENSITIVITY A: auth tier dropped to 60 (old bug) is detected',
      !(bScale['--z-auth'] > bScale['--z-toasts']), bScale);
    check('SENSITIVITY A: raw integer z-index in CSS is detected',
      /\.auth-overlay\s*\{[^}]*z-index:\s*\d+/.test(brokenCss));
  }

  /* ---- 6. SENSITIVITY B: old inline z-index:60 overlay shape fails ---- */
  {
    const brokenJs = appJs.replace(
      'id: "loginOverlay", class: "scrim auth-overlay",',
      'id: "loginOverlay", class: "scrim", style: "display:flex;align-items:center;justify-content:center;z-index:60",');
    const region = brokenJs.slice(brokenJs.indexOf('function authOverlayOpen'), brokenJs.indexOf('function connectWS()'));
    check('SENSITIVITY B: inline z-index:60 overlay shape is detected', /z-index/.test(region));
    check('SENSITIVITY B: missing auth-overlay class is detected',
      !/class: "scrim auth-overlay"/.test(region));
  }

  console.log(failures === 0 ? '\nALL PASS (' + 'auth overlay stacking)' : '\n' + failures + ' FAILURE(S)');
  process.exit(failures === 0 ? 0 : 1);
}

main().catch(e => { console.error(e); process.exit(1); });
