/* Hermes Kanban POC front-end. Talks to /api/kanban (the real plugin API). */
'use strict';
window.addEventListener('error', e => { document.title = 'ERR:' + (e.message || e.error) .toString().slice(0,180); });


const API = "/api/kanban";
// Auth: HttpOnly session cookie set by POST /login. The JS never holds a
// credential; every fetch() adds the X-Kanban-UI marker header (the server
// rejects non-GET api calls without it — cross-site pages cannot set custom
// headers, and SameSite=strict keeps the cookie off any cross-site request).

const COLS = ["triage", "todo", "scheduled", "ready", "running", "blocked", "review", "done"];
// POST /tasks accepts only `triage: bool` (no general status field), so those two
// columns are the only ones the create API can honor. Other columns get no "+" CTA.
const CREATEABLE_COLS = new Set(["triage", "todo"]);
const COL_LABEL = { triage: "Triage", todo: "Todo", scheduled: "Scheduled", ready: "Ready", running: "Running", blocked: "Blocked", review: "Review", done: "Done", archived: "Archived" };

const S = {
  board: localStorage.getItem("kboard") || document.body.dataset.board || "default",
  data: null,           // last /board payload
  boards: [],
  assignees: [],        // from /assignees
  modelOpts: null,
  cfg: {},
  search: "",
  tenant: "",
  assignee: "",
  lanes: true,
  archived: false,
  sel: new Set(),       // bulk selection ids
  drawerId: null,
  drawerData: null,
  mobileCol: localStorage.getItem("mcol") || null,
  ws: null, wsCursor: 0, wsBackoff: 1000,
  workersTimer: null,
};

/* ---------------- api ---------------- */
async function api(path, { method = "GET", body, board = true, raw } = {}) {
  const url = new URL(API + path, location.origin);
  if (board && S.board) url.searchParams.set("board", S.board);
  const opts = { method, headers: {}, credentials: "same-origin" };
  if (body !== undefined && !raw) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
  opts.headers["X-Kanban-UI"] = "1";
  const res = await fetch(url, { ...opts, headers: { ...opts.headers } });
  if (res.status === 401) { showLogin(); throw new Error("session expired — log in again"); }
  if (!res.ok) {
    let detail = res.statusText;
    try { const j = await res.json(); detail = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail || j); } catch {}
    throw new Error(detail);
  }
  if (res.status === 204) return null;
  return normalizeTaskDetail(await res.json());
}
// PR#13 review finding 4319617504: the pinned plugin's _links_for() returns
// links.parents / links.children as bare task-ID strings, but both
// existingDepsBox() and renderDrawer() read {id,title,status} fields off
// each entry (blank labels, /tasks/undefined on click-through, and
// parent_id=undefined on removal). Normalize task-detail link entries at
// this shared response boundary into the object shape the consumers expect,
// enriched from the link_tasks rows that ship in the same payload. Gated to
// task-detail responses only (d.task + d.links — no other endpoint shape
// matches); object entries pass through untouched so a future upstream
// change to objects needs no client change.
function normalizeTaskDetail(d) {
  if (!d || typeof d !== "object" || typeof d.task !== "object" || d.task === null
      || typeof d.links !== "object" || d.links === null) return d;
  const rows = new Map();
  for (const r of (Array.isArray(d.link_tasks) ? d.link_tasks : [])) {
    if (r && typeof r === "object" && typeof r.id === "string") rows.set(r.id, r);
  }
  const norm = (list) => (Array.isArray(list) ? list : [])
    .map(x => (typeof x === "string" ? { id: x, ...(rows.get(x) || {}) } : x))
    .filter(x => x && typeof x === "object" && x.id != null);
  d.links = { ...d.links, parents: norm(d.links.parents), children: norm(d.links.children) };
  return d;
}
const qs = () => { const b = S.board ? `board=${encodeURIComponent(S.board)}` : ""; return b ? "?" + b : ""; };

/* ---------------- helpers ---------------- */
const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];
function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "html") el.innerHTML = v;
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v);
  }
  for (const kid of kids.flat(2)) {
    if (kid == null || kid === false) continue;
    el.append(kid.nodeType ? kid : document.createTextNode(kid));
  }
  return el;
}
function ago(ts, now) {
  if (!ts) return "";
  const d = Math.max(0, (now || Math.floor(Date.now() / 1000)) - ts);
  if (d < 60) return d + "s";
  if (d < 3600) return Math.floor(d / 60) + "m";
  if (d < 86400) return Math.floor(d / 3600) + "h";
  if (d < 86400 * 30) return Math.floor(d / 86400) + "d";
  return Math.floor(d / 86400 / 30) + "mo";
}
function fmtTime(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" }) + " " +
         d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}
function fmtBytes(n) { if (!n) return "0 B"; const u = ["B","KB","MB","GB"]; let i = 0; while (n >= 1024 && i < 3) { n /= 1024; i++; } return n.toFixed(i ? 1 : 0) + " " + u[i]; }
/* Markdown helpers live in md.js (window.KanbanMd) so the XSS-critical
 * renderer is unit-testable in plain node: tests/test_md_renderer.js.
 * They are also re-bound here with a hard load-time self-check: if the
 * renderer ever rejects a hostile link label (regression), md() degrades
 * to plain escaped text instead of throwing mid-render. */
const { esc, unent, textEsc, safeUrl } = window.KanbanMd;
const md = (src) => {
  try {
    const out = window.KanbanMd.md(src);
    if (out.indexOf("KanbanMdLabelEsc(") !== -1) throw new Error("label escape failed");
    return out;
  } catch (e) {
    return esc(unent(src)).replace(/\n/g, "<br>");
  }
};
function toast(msg, kind = "") {
  const t = h("div", { class: "toast " + kind }, msg);
  $("#toasts").append(t);
  setTimeout(() => { t.style.opacity = "0"; t.style.transition = "opacity .3s"; setTimeout(() => t.remove(), 320); }, kind === "err" ? 6000 : 3200);
}
async function act(promise, okMsg) {
  try { const r = await promise; if (okMsg) toast(okMsg, "ok"); return r; }
  catch (e) { toast(e.message || String(e), "err"); throw e; }
}

/* ---------------- data loads ---------------- */
async function loadBoard(partialOk = false) {
  if (document.hidden) return;   // visibility handler catches up on return
  // Identity guard (PR#13 review 4301554435): capture the requested board at
  // request time. api() reads S.board when building the URL, so if switchBoard()
  // flips the selection while this fetch is in flight, the awaited payload
  // belongs to the OLD board. Without the guard its assignments would render
  // A's tasks under B's name and Math.max() A's event cursor into place (the
  // WS identity guard only covers socket messages, not HTTP responses).
  const requestedBoard = S.board;
  try {
    const d = await api(`/board?include_archived=${S.archived}`);
    if (S.board !== requestedBoard) return;   // selection changed while awaiting — discard
    S.data = d;
    S.wsCursor = Math.max(S.wsCursor, S.data.latest_event_id || 0);
    render();
  } catch (e) { if (!partialOk) toast("Board load failed: " + e.message, "err"); }
}
async function loadBoards() {
  try { const r = await api("/boards?include_archived=1", { board: false }); S.boards = r.boards || []; } catch {}
  const b = S.boards.find(x => x.slug === S.board);
  $("#boardName").textContent = (b && (b.name || b.slug)) || S.board;
  $("#boardIcon").textContent = (b && b.icon) || "▦";
}
async function loadAssignees() {
  try { S.assignees = (await api("/assignees")).assignees || []; } catch {}
}
async function loadCfg() {
  try {
    S.cfg = await api("/config");
    S.lanes = !!S.cfg.lane_by_profile;
    $("#lanesChk").checked = S.lanes;
    $("#archivedChk").checked = !!S.cfg.include_archived_by_default;
    S.archived = !!S.cfg.include_archived_by_default;
    if (S.cfg.default_tenant) S.tenant = S.cfg.default_tenant;
  } catch {}
}
async function loadModelOpts() {
  if (S.modelOpts) return S.modelOpts;
  try { S.modelOpts = await api("/model-options"); } catch { S.modelOpts = { providers: [] }; }
  return S.modelOpts;
}

/* ---------------- filtering ---------------- */
function visibleTasks(list) {
  const q = S.search.trim().toLowerCase();
  return list.filter(t => {
    if (S.tenant && (t.tenant || "") !== S.tenant) return false;
    if (S.assignee && (t.assignee || "") !== S.assignee) return false;
    if (q && !(t.title.toLowerCase().includes(q) || (t.body || "").toLowerCase().includes(q) || (t.id || "").includes(q) || (t.assignee || "").toLowerCase().includes(q))) return false;
    return true;
  });
}

/* ---------------- render: board ---------------- */
function render() {
  if (!S.data) return;
  try { renderInner(); } catch (e) { console.error(e); toast("render: " + e.message, "err"); }
}

/* Background-refresh render: DOM nodes persist across refreshes (keyed by task id)
   so scroll positions, text selection and focus survive live updates. When the
   payload signature is unchanged the DOM is not touched at all. */
function fp(x, r) { return JSON.stringify(x, r); }
function cardSig(t) {
  // Stale-threshold bucket, NOT raw age: age ticks every fetch but the stale
  // class in applyCard only flips when started_age_seconds crosses 3600. A
  // boolean keeps the signature stable between ticks while forcing a
  // re-render at the exact crossing (see tickAges fast path).
  const stale = !!(t.status === "running" && t.age && t.age.started_age_seconds > 3600);
  return fp([t.title, t.status, t.assignee, t.priority, t.progress, t.link_counts, t.comment_count,
    t.warnings, t.tenant, t.created_at, t.completed_at, t.latest_summary, S.sel.has(t.id), stale]);
}
function findTask(id) {
  for (const c of ((S.data || {}).columns) || []) for (const t of c.tasks || []) if (t.id === id) return t;
  return null;
}
function reconcile(parent, desired) {
  const want = new Set(desired);
  for (const c of [...parent.children]) if (!want.has(c)) c.remove();
  let i = 0;
  for (const node of desired) {
    if (parent.children[i] !== node) parent.insertBefore(node, parent.children[i] || null);
    i++;
  }
}
const tabEls = new Map();  // col name -> {el, n}
const colEls = new Map();  // col name -> {el, count, body, cards:Map(id->el), lanes:Map(name->el), empty}
let boardSig = "";

function renderInner() {
  const now = Math.floor(Date.now() / 1000);
  const cols = S.data.columns || [];
  const byName = Object.fromEntries(cols.map(c => [c.name, c]));
  const names = cols.map(c => c.name).filter(n => COLS.includes(n) || n === "archived");
  if (names.length && !names.includes(S.mobileCol)) { // unset, or column no longer present (e.g. Archived hidden)
    const first = names.find(n => visibleTasks((byName[n] || {}).tasks || []).length) || names[0];
    S.mobileCol = first;
  }
  const vis = Object.fromEntries(names.map(n => [n, visibleTasks((byName[n] || {}).tasks || [])]));

  if (S.remount) { // board switch: drop all cached nodes
    S.remount = false;
    $("#board").replaceChildren(); $("#colTabs").replaceChildren();
    colEls.clear(); tabEls.clear(); boardSig = "";
  }
  const sig = fp([names.map(n => [n, vis[n].map(cardSig)]), S.lanes, S.mobileCol, S.search, S.tenant, S.assignee, S.archived]);
  if (sig === boardSig) { tickAges(now); return; }
  boardSig = sig;

  const tabDes = [];
  for (const n of names) {
    let rec = tabEls.get(n);
    if (!rec) {
      const nEl = h("span", { class: "n" });
      const el = h("button", { class: "coltab" }, COL_LABEL[n] || n, nEl);
      el.addEventListener("click", () => { S.mobileCol = n; localStorage.setItem("mcol", n); render(); });
      rec = { el, n: nEl }; tabEls.set(n, rec);
    }
    rec.n.textContent = String(vis[n].length);
    rec.el.classList.toggle("on", n === S.mobileCol);
    tabDes.push(rec.el);
  }
  reconcile($("#colTabs"), tabDes);

  const colDes = [];
  for (const name of names) {
    let rec = colEls.get(name);
    if (!rec) {
      const count = h("span", { class: "col-count" });
      const el = h("div", { class: `column col-${name}`, "data-col": name });
      el.append(h("div", { class: "col-head" },
        h("span", { class: "col-dot" }), COL_LABEL[name] || name, count,
        ...(CREATEABLE_COLS.has(name)
          ? [h("button", { class: "col-add", title: "Create task in this column", onclick: () => openCreateModal({ column: name }) }, "＋")]
          : [])));
      const body = h("div", { class: "col-body" }); el.append(body);
      el.addEventListener("dragover", e => { e.preventDefault(); el.classList.add("drag-over"); });
      el.addEventListener("dragleave", e => { if (!el.contains(e.relatedTarget)) el.classList.remove("drag-over"); });
      el.addEventListener("drop", async e => {
        e.preventDefault(); el.classList.remove("drag-over");
        const ids = JSON.parse(e.dataTransfer.getData("text/plain") || "[]");
        if (ids.length) await moveTasks(ids, name);
      });
      rec = { el, count, body, cards: new Map(), lanes: new Map(), empty: h("div", { class: "col-empty" }, "Nothing here") };
      colEls.set(name, rec);
    }
    rec.el.classList.toggle("mobile-active", name === S.mobileCol);
    const tasks = vis[name];
    rec.count.textContent = String(tasks.length);

    const desired = []; const ids = new Set();
    if (!tasks.length) {
      rec.lanes.clear();
      desired.push(rec.empty);
    } else if (S.lanes) {
      const lanes = {};
      for (const t of tasks) (lanes[t.assignee || "unassigned"] ||= []).push(t);
      const laneSet = new Set(Object.keys(lanes));
      for (const l of [...rec.lanes.keys()]) if (!laneSet.has(l)) rec.lanes.delete(l);
      for (const [lane, list] of Object.entries(lanes)) {
        let lab = rec.lanes.get(lane);
        if (!lab) { lab = h("div", { class: "lane-label" }, lane); rec.lanes.set(lane, lab); }
        desired.push(lab);
        for (const t of list) { ids.add(t.id); desired.push(recCard(rec, t, now)); }
      }
    } else {
      rec.lanes.clear();
      for (const t of tasks) { ids.add(t.id); desired.push(recCard(rec, t, now)); }
    }
    for (const [id2, el2] of [...rec.cards]) if (!ids.has(id2)) rec.cards.delete(id2);
    reconcile(rec.body, desired);
    colDes.push(rec.el);
  }
  reconcile($("#board"), colDes);

  renderStats();
  renderBulkBar();
  syncFilterOptions();
}

function recCard(rec, t, now) {
  let el = rec.cards.get(t.id);
  if (!el) { el = makeCard(t.id); rec.cards.set(t.id, el); }
  applyCard(el, t, now);
  return el;
}

function tickAges(now) {
  for (const el of $$("#board .card")) {
    const chip = el.querySelector(".chip.time");
    if (chip && el._ageTs != null) chip.textContent = "◷ " + ago(el._ageTs, now);
  }
}

function makeCard(id) {
  // PR#13 review finding 4331497750 (keyboard operability) as corrected by
  // the round-8 finding (W3C ACT rule 307n5z / WAI-ARIA 1.2 button children
  // are presentational): the card wrapper itself must NOT be role="button"
  // or a native button, because the focusable bulk checkbox lives inside it
  // and would become a silent focus stop / lose its checkbox semantics.
  // This wrapper is non-interactive (drag + context menu only). The two
  // focusable controls are built in applyCard as siblings: a dedicated
  // native <button type="button" class="card-open"> that opens the drawer
  // (native Enter/Space activation, labelled) and the bulk-selection
  // checkbox with its own label. Mouse clicks anywhere on the card body
  // still open the drawer; the open control stops propagation on its own
  // clicks (so native keyboard activation cannot double-open via bubbling)
  // and the checkbox stops propagation so selection never opens the drawer.
  const el = h("div", { class: "card", draggable: "true", "data-id": id });
  el.addEventListener("click", e => {
    if (e.target.classList.contains("card-check")) return;
    openDrawer(id);
  });
  el.addEventListener("contextmenu", e => {
    e.preventDefault();
    const t = findTask(id); if (t) openCardMenu(t, e.clientX, e.clientY);
  });
  el.addEventListener("dragstart", e => {
    const ids = S.sel.has(id) ? [...S.sel] : [id];
    e.dataTransfer.setData("text/plain", JSON.stringify(ids));
    el.classList.add("dragging");
    $("#trashZone").hidden = false;
  });
  el.addEventListener("dragend", () => { el.classList.remove("dragging"); $("#trashZone").hidden = true; $("#trashZone").classList.remove("over"); });
  return el;
}

function applyCard(el, t, now) {
  if (el._sig !== cardSig(t)) {
    el._sig = cardSig(t);
    const label = t.title || t.id;
    const checked = S.sel.has(t.id);
    // Round-8 finding (W3C ACT 307n5z / WAI-ARIA 1.2 button children are
    // presentational): the bulk checkbox and the task-open control are
    // SIBLING focusable controls directly under the non-interactive card
    // wrapper — the checkbox is never a descendant of a button-role/node,
    // so it keeps its checkbox role/name/state and its own tab stop. The
    // open control is a native <button type="button"> (free Enter/Space
    // activation); its spans (not divs) keep the button content model
    // phrasing-valid. Each control stops its own click propagation: the
    // button's stop prevents the wrapper click handler from double-opening
    // after native activation bubbles, the checkbox's stop keeps selection
    // toggling from ever opening the drawer.
    const chk = h("input", { type: "checkbox", class: "card-check", "aria-label": "Select task for bulk actions: " + label, title: "Select for bulk actions", ...(checked ? { checked: "" } : {}), onclick: e => { e.stopPropagation(); if (e.target.checked) S.sel.add(t.id); else S.sel.delete(t.id); render(); } });
    const open = h("button", { type: "button", class: "card-open", "aria-label": "Open task: " + label, onclick: e => { e.stopPropagation(); openDrawer(t.id); } },
      h("span", { class: "card-top" }, h("span", { class: "card-title" }, t.title)),
      ...(t.latest_summary ? [h("span", { class: "card-summary" }, t.latest_summary)] : []),
      cardMeta(t, now));
    el.replaceChildren(chk, open);
  }
  el.classList.toggle("selected", S.sel.has(t.id));
  el.classList.toggle("running", t.status === "running");
  el.classList.toggle("prio-high", (t.priority || 0) >= 2);
  el.classList.toggle("stale", t.status === "running" && t.age && t.age.started_age_seconds > 3600);
  el._ageTs = t.status === "done" ? t.completed_at : t.created_at;
  const chip = el.querySelector(".chip.time");
  if (chip) chip.textContent = "◷ " + ago(el._ageTs, now);
}

function cardMeta(t, now) {
  // Round-8 finding: this renders inside the native <button class="card-open">
  // so it must be a <span> (phrasing content) — the .card-meta rule already
  // sets display:flex, so layout is unchanged.
  const meta = h("span", { class: "card-meta" });
  if (t.status === "running") meta.append(h("span", { class: "spark" }));
  if (t.assignee) meta.append(h("span", { class: "chip assignee" }, "@" + t.assignee));
  if ((t.priority || 0) !== 0) meta.append(h("span", { class: "chip prio" }, "P" + t.priority));
  if (t.progress && t.progress.total) meta.append(h("span", { class: "chip prog" }, `✓ ${t.progress.done}/${t.progress.total}`));
  const lc = t.link_counts || {};
  if ((lc.parents || 0) + (lc.children || 0) > 0) meta.append(h("span", { class: "chip", title: "dependencies" }, "⛓ " + (lc.parents || 0) + "↑" + (lc.children || 0) + "↓"));
  if ((t.comment_count || 0) > 0) meta.append(h("span", { class: "chip" }, "💬 " + t.comment_count));
  if (t.warnings && t.warnings.count) meta.append(h("span", { class: "chip warn", title: Object.entries(t.warnings.kinds || {}).map(([k, v]) => k + " ×" + v).join(", ") + (t.warnings.highest_severity ? " (" + t.warnings.highest_severity + ")" : "") }, "⚠ " + t.warnings.count));
  if (t.tenant) meta.append(h("span", { class: "chip" }, "◈ " + t.tenant));
  meta.append(h("span", { class: "chip time", title: "age" }, "◷ " + ago(t.status === "done" ? t.completed_at : t.created_at, now)));
  return meta;
}

function renderStats() {
  const strip = $("#statsStrip");
  if (!S.data) return;
  const cols = S.data.columns || [];
  const total = cols.reduce((n, c) => n + (c.tasks || []).length, 0);
  const run = (cols.find(c => c.name === "running") || {}).tasks || [];
  const blocked = (cols.find(c => c.name === "blocked") || {}).tasks || [];
  const ready = (cols.find(c => c.name === "ready") || {}).tasks || [];
  const stale = ready.length && S.data.now ? ago(Math.min(...ready.map(t => t.created_at)), S.data.now) : "0s";
  strip.replaceChildren(
    h("span", { class: "stat-chip" }, h("b", {}, String(total)), " tasks"),
    h("span", { class: "stat-chip" }, h("b", {}, String(run.length)), " running"),
    h("span", { class: "stat-chip" }, h("b", {}, String(ready.length)), " ready"),
    h("span", { class: "stat-chip" }, h("b", {}, String(blocked.length)), " blocked"),
    h("span", { class: "stat-chip", title: "oldest ready task age" }, "oldest ready: ", h("b", {}, stale)),
  );
}

/* ---------------- filters ---------------- */
function syncFilterOptions() {
  const tSel = $("#tenantSel"), aSel = $("#assigneeSel");
  const tenants = S.data ? (S.data.tenants || []) : [];
  const assignees = [...new Set([...(S.data ? S.data.assignees || [] : []), ...(S.assignees || []).map(a => a.name)])];
  const opt = (v, label, cur) => h("option", { value: v, ...(v === cur ? { selected: "" } : {}) }, label);
  tSel.replaceChildren(opt("", "All", S.tenant), ...tenants.map(t => opt(t, t, S.tenant)));
  aSel.replaceChildren(opt("", "All", S.assignee), ...assignees.map(a => opt(a, "@" + a, S.assignee)));
  $("#filterBtn").classList.toggle("on", !!(S.tenant || S.assignee || S.search || S.archived));
}

/* ---------------- actions ---------------- */
async function moveTasks(ids, target) {
  // PR#13 review finding 4319617397: dragging a RUNNING card to Ready
  // reaches _set_status_direct, which releases the worker claim without
  // signalling the host worker (duplicate-dispatch hazard). server.py
  // refuses the transition at the shared chokepoint (PATCH 400 / bulk
  // per-id refusal); the UI refuses it too so there is no misleading
  // half-success toast. Reclaim belongs on the Hermes host.
  if (target === "ready") {
    const running = ids.filter(id => { const t = findTask(id); return t && t.status === "running"; });
    if (running.length) {
      toast("running card(s) cannot move to Ready from the WebUI: the claim release would not signal the host worker; run `hermes kanban reclaim <id>` on the Hermes host (" + running.join(", ") + ")", "err");
      if (running.length === ids.length) return;
      ids = ids.filter(id => !running.includes(id));
    }
  }
  // single PATCH per task via bulk endpoint for status
  const body = { ids, status: target === "archived" ? undefined : target, archive: target === "archived" };
  try {
    const r = await api("/tasks/bulk" + qs(), { method: "POST", body });
    const fails = (r.results || []).filter(x => x.error);
    if (fails.length) toast(fails.map(f => f.id + ": " + f.error).join(" · "), "err");
    await loadBoard(true);
  } catch (e) { toast(e.message, "err"); }
}

function cardMenuItems(t) {
  const items = [];
  const move = (status, label, extra = {}) => items.push({ label, fn: () => moveTasks(S.sel.has(t.id) ? [...S.sel] : [t.id], status), ...extra });
  if (t.status !== "done") {
    if (t.status === "blocked" || t.status === "scheduled") items.push({ label: "Unblock → Ready", fn: () => act(api(`/tasks/${t.id}` + qs(), { method: "PATCH", body: { status: "ready" } }).then(loadBoard), "Unblocked") });
    else if (t.status !== "running") items.push({ label: "✓ Complete", fn: () => openCompleteModal(t) });
    if (t.status !== "blocked") items.push({ label: "⛔ Block…", fn: () => promptReason("Block reason", r => act(api(`/tasks/${t.id}` + qs(), { method: "PATCH", body: { status: "blocked", block_reason: r } }).then(loadBoard), "Blocked")) });
    if (t.status !== "scheduled") items.push({ label: "⏰ Schedule…", fn: () => promptReason("Schedule note", r => act(api(`/tasks/${t.id}` + qs(), { method: "PATCH", body: { status: "scheduled", block_reason: r } }).then(loadBoard), "Scheduled")) });
    // PR#13 review finding 4301554389: worker-control ops (reclaim / run
    // terminate / reclaim-first reassign) are refused server-side in
    // server.py -- a container-side reclaim releases the claim without
    // signalling the host worker, so the dispatcher could spawn a
    // duplicate beside a still-running worker. The control is removed
    // here too (defense-in-depth + no misleading affordance); run
    // `hermes kanban reclaim <id>` on the Hermes host instead.
  }
  if (t.status === "triage") items.push({ label: "✒ Specify (expand w/ LLM)", fn: () => act(api(`/tasks/${t.id}/specify` + qs(), { method: "POST", body: {} }).then(loadBoard), "Specified") });
  if (t.status === "triage" || t.status === "todo") items.push({ label: "🌱 Decompose", fn: () => act(api(`/tasks/${t.id}/decompose` + qs(), { method: "POST", body: {} }).then(loadBoard), "Decomposed") });
  move("triage", "→ Triage");
  move("todo", "→ Todo");
  // PR#13 review finding 4319617397: running cards cannot be requeued to
  // Ready here (server-side chokepoint guard in server.py refuses; the
  // item stays visible-but-disabled until host-side worker control lands).
  // PR#13 review finding 4321367961: the flag must be a real boolean --
  // the renderer below tests it for truthiness, and an empty string left
  // this no-callback item clickable (onclick then threw on it.fn).
  if (t.status === "running") {
    items.push({ label: "→ Ready (disabled in the WebUI)", disabled: true, title: "Requeuing a running task would release the worker claim without signalling the host worker; run `hermes kanban reclaim <id>` on the Hermes host instead" });
  } else {
    move("ready", "→ Ready");
  }
  items.push({ label: "Reassign…", fn: () => openReassignModal(t) });
  items.push({ label: "🔮 Estimate", fn: async () => { try { const r = await api(`/tasks/${t.id}/estimate` + qs(), { method: "POST" }); toast(r.ok ? `est ~${r.est_tokens} tokens · ${r.complexity} (${r.model})` : ("estimate: " + (r.reason || "n/a")), r.ok ? "ok" : "err"); } catch (e) { toast(e.message, "err"); } } });
  items.push({ label: "⤴ Add dependency…", fn: () => openDepModal(t) });
  items.push({ sep: true });
  items.push({ label: "🗑 Archive", danger: true, fn: () => moveTasks([t.id], "archived") });
  if (t.status === "archived") items.push({ label: "✕ Delete permanently", danger: true, fn: () => confirmModal("Delete task " + t.id + " for good?", () => act(api(`/tasks/${t.id}` + qs(), { method: "DELETE" }).then(loadBoard), "Deleted")) });
  return items;
}

/* PR#13 review finding 4321368019: the drawer footer renders the first five
 * cardMenuItems, so for a RUNNING task the slice includes the no-callback
 * Ready item. The footer used to ignore the model's boolean `disabled` flag
 * and `title` explanation and call `it.fn()` unguarded (TypeError). Render
 * through the same contract as openCardMenu: real HTML disabled attribute +
 * tooltip for flagged items, and an inert click guard so a missing callback
 * can never be invoked here either. */
function footerActionButtons(t) {
  return cardMenuItems(t).filter(i => !i.sep).slice(0, 5).map(it => h("button", {
    class: "btn btn-ghost btn-sm" + (it.danger ? " danger" : ""),
    ...(it.disabled ? { disabled: "" } : {}), ...(it.title ? { title: it.title } : {}),
    onclick: () => { if (it.disabled || typeof it.fn !== "function") return; it.fn(); }
  }, it.label));
}

function openCardMenu(t, x, y) {
  closeMenus();
  const menu = h("div", { class: "modal", style: `position:fixed;left:${Math.min(x, innerWidth - 260)}px;top:${Math.min(y, innerHeight - 360)}px;width:250px;padding:6px;z-index:var(--z-menu)` });
  for (const it of cardMenuItems(t)) {
    if (it.sep) { menu.append(h("hr", { style: "border:none;border-top:1px solid var(--line);margin:4px 0" })); continue; }
    menu.append(h("button", {
      class: "btn btn-quiet" + (it.danger ? " danger" : ""), style: "width:100%;justify-content:flex-start;border-radius:8px",
      ...(it.disabled ? { disabled: "" } : {}), ...(it.title ? { title: it.title } : {}),
      // PR#13 review finding 4321367961: no-callback items must be inert.
      // The disabled flag now carries a real boolean, but guard the call
      // too so a future falsy flag can never reach `it.fn()` and throw.
      onclick: () => { if (it.disabled || typeof it.fn !== "function") return; closeMenus(); it.fn(); }
    }, it.label));
  }
  document.body.append(menu);
  const scrim = h("div", { class: "scrim", style: "z-index:var(--z-menu-scrim)", onclick: closeMenus });
  document.body.append(scrim);
  window.__menuClose = () => { menu.remove(); scrim.remove(); window.__menuClose = null; };
}
function closeMenus() { if (window.__menuClose) window.__menuClose(); }

/* ---------------- modals ---------------- */
function modalShell(title, body, actions, opts = {}) {
  const scrim = h("div", { class: "modal-wrap", onclick: e => { if (e.target === scrim) close(); } });
  const box = h("div", { class: "modal" }, h("h2", {}, title), body, actions ? h("div", { class: "modal-actions" }, ...actions) : null);
  scrim.append(box);
  $("#modalRoot").append(scrim);
  let closed = false;
  // close() is what the scrim and Escape paths invoke (see onKey below), NOT the
  // returned m.close. Cleanup registered via opts.onClose therefore fires on
  // every close path (button, scrim, Escape), guarded once so repeated calls are safe.
  function close() { if (closed) return; closed = true; scrim.remove(); document.removeEventListener("keydown", onKey); if (opts.onClose) opts.onClose(); }
  function onKey(e) { if (e.key === "Escape") close(); }
  document.addEventListener("keydown", onKey);
  return { close, box };
}
function promptReason(label, cb) {
  const ta = h("textarea", { placeholder: label });
  const m = modalShell(label, ta, [
    h("button", { class: "btn btn-quiet", onclick: () => m.close() }, "Cancel"),
    h("button", { class: "btn btn-accent", onclick: () => { m.close(); cb(ta.value.trim() || label); } }, "Apply"),
  ]);
  setTimeout(() => ta.focus(), 30);
}
function confirmModal(text, cb) {
  const m = modalShell("Are you sure?", h("p", { class: "md" }, text), [
    h("button", { class: "btn btn-quiet", onclick: () => m.close() }, "Cancel"),
    h("button", { class: "btn btn-accent", style: "background:var(--danger);box-shadow:none", onclick: () => { m.close(); cb(); } }, "Confirm"),
  ]);
}

async function openCreateModal({ column } = {}) {
  column = column || "todo";
  if (!CREATEABLE_COLS.has(column)) column = "todo"; // mobile fab / keyboard-n pass S.mobileCol (any column); create only honors triage|todo
  await loadAssignees();
  const title = h("input", { type: "text", placeholder: "Task title" });
  const body = h("textarea", { placeholder: "Description / brief (markdown ok)" });
  const assignee = selControl(assigneeOptions(), column === "triage" ? "" : "");
  const prio = h("input", { type: "number", value: "0", min: "-10", max: "100" });
  const tenant = h("input", { type: "text", placeholder: "optional" });
  const workspaceKind = selControl([["scratch", "scratch"], ["dir", "dir (fixed path)"], ["worktree", "git worktree"]], "scratch");
  const workspacePath = h("input", { type: "text", placeholder: "absolute path (dir/worktree)" });
  const parents = h("input", { type: "text", placeholder: "parent task ids, comma-separated" });
  const modelSel = await modelSelect(null);
  const triageChk = h("input", { type: "checkbox", ...(column === "triage" ? { checked: "" } : {}) });

  const m = modalShell("New task", h("div", {},
    h("label", {}, "Title"), title,
    h("label", {}, "Body"), body,
    h("button", { class: "btn btn-ghost btn-sm", title: "Rough token/complexity estimate before creating", onclick: async () => {
      try {
        const r = await api("/estimate", { method: "POST", body: { title: title.value, body: body.value }, board: false });
        toast(r.ok ? `est ~${r.est_tokens} tokens · ${r.complexity} (${r.model})` : ("estimate: " + (r.reason || "n/a")), r.ok ? "ok" : "err");
      } catch (e) { toast(e.message, "err"); }
    } }, "🔮 Estimate"),
    h("div", { class: "grid2" },
      h("div", {}, h("label", {}, "Assignee"), assignee),
      h("div", {}, h("label", {}, "Priority"), prio),
      h("div", {}, h("label", {}, "Tenant"), tenant),
      h("div", {}, h("label", {}, "Workspace"), workspaceKind),
      h("div", { style: "grid-column:1/-1" }, h("label", {}, "Workspace path"), workspacePath),
      h("div", { style: "grid-column:1/-1" }, h("label", {}, "Parent task(s)"), parents),
      h("div", {}, h("label", {}, "Model override"), modelSel),
      h("div", {}, h("label", {}, "Start in Triage"), h("div", { style: "padding:6px 0" }, triageChk)),
    ),
  ), [
    h("button", { class: "btn btn-quiet", onclick: () => m.close() }, "Cancel"),
    h("button", { class: "btn btn-accent", onclick: async () => {
      if (!title.value.trim()) { toast("Title required", "err"); return; }
      const payload = {
        title: title.value.trim(),
        body: body.value.trim() || null,
        assignee: assignee.value || null,
        priority: parseInt(prio.value || "0", 10),
        tenant: tenant.value.trim() || null,
        workspace_kind: workspaceKind.value,
        workspace_path: workspacePath.value.trim() || null,
        parents: parents.value.split(/[,\s]+/).map(s => s.trim()).filter(Boolean),
        triage: triageChk.checked,
      };
      const mo = modelSel.value ? modelSel.value.split("::") : [];
      if (mo[1]) { payload.model_override = mo[1]; payload.provider_override = mo[0]; }
      try {
        const r = await api("/tasks" + qs(), { method: "POST", body: payload });
        m.close();
        toast("Created " + (r.task ? r.task.id : ""), "ok");
        await loadBoard(true);
        if (r.task) openDrawer(r.task.id);
      } catch (e) { toast(e.message, "err"); }
    } }, "Create"),
  ]);
  setTimeout(() => title.focus(), 30);
}

async function openCompleteModal(t) {
  const result = h("textarea", { placeholder: "Result (what was done)" });
  const summary = h("textarea", { placeholder: "Handoff summary (goes to reviewers/children)" });
  const m = modalShell("Complete " + t.id, h("div", {},
    h("label", {}, "Result"), result,
    h("label", {}, "Summary"), summary,
  ), [
    h("button", { class: "btn btn-quiet", onclick: () => m.close() }, "Cancel"),
    h("button", { class: "btn btn-accent", onclick: async () => {
      try {
        await api(`/tasks/${t.id}` + qs(), { method: "PATCH", body: { status: "done", result: result.value.trim() || null, summary: summary.value.trim() || null } });
        m.close(); toast("Completed", "ok"); await loadBoard(true);
        if (S.drawerId === t.id) openDrawer(t.id);
      } catch (e) { toast(e.message, "err"); }
    } }, "Complete"),
  ]);
}

async function openReassignModal(t) {
  await loadAssignees();
  const sel = selControl(assigneeOptions(), t.assignee || "");
  // PR#13 review finding 4301554389: reclaim-first reassign calls
  // reclaim_task() server-side, which would release the claim WITHOUT
  // signalling the host worker (the container is not the worker's host).
  // The server rejects reclaim_first=true with 403; the checkbox stays
  // visible but DISABLED so the affordance is explained, not hidden, and
  // the Apply call below hard-codes reclaim_first: false regardless.
  const reclaim = h("input", { type: "checkbox", disabled: "", ...(t.status === "running" ? { checked: "" } : {}) });
  const m = modalShell("Reassign " + t.id, h("div", {},
    h("label", {}, "Profile"), sel,
    h("label", { class: "fchk", style: "margin-top:10px", title: "Worker control (reclaim) is only available on the Hermes host: hermes kanban reassign <id> <profile> --reclaim" }, reclaim, "Reclaim first (release running worker) (disabled in the WebUI)"),
  ), [
    h("button", { class: "btn btn-quiet", onclick: () => m.close() }, "Cancel"),
    h("button", { class: "btn btn-accent", onclick: async () => {
      try {
        await api(`/tasks/${t.id}/reassign` + qs(), { method: "POST", body: { profile: sel.value || null, reclaim_first: false, reason: "dashboard reassign" } });
        m.close(); toast("Reassigned", "ok"); await loadBoard(true);
        if (S.drawerId === t.id) openDrawer(t.id);
      } catch (e) { toast(e.message, "err"); }
    } }, "Apply"),
  ]);
}

function openDepModal(t) {
  const kind = selControl([["child", "This task is a child of…"], ["parent", "This task is a parent of…"]], "child");
  const other = h("input", { type: "text", placeholder: "other task id (t_…)" });
  const m = modalShell("Dependencies — " + t.id, h("div", {}, kind, h("div", { style: "height:8px" }, ""), other,
    existingDepsBox(t)), [
    h("button", { class: "btn btn-quiet", onclick: () => m.close() }, "Close"),
    h("button", { class: "btn btn-accent", onclick: async () => {
      const o = other.value.trim();
      if (!o) { toast("Other task id required", "err"); return; }
      const link = kind.value === "child" ? { parent_id: o, child_id: t.id } : { parent_id: t.id, child_id: o };
      try { await api("/links" + qs(), { method: "POST", body: link }); toast("Linked", "ok"); other.value = ""; m.close(); await loadBoard(true); if (S.drawerId === t.id) openDrawer(t.id); }
      catch (e) { toast(e.message, "err"); }
    } }, "Add link"),
  ]);
}
function existingDepsBox(t) {
  const box = h("div", { style: "margin-top:10px" });
  const render = async () => {
    try {
      const d = await api(`/tasks/${t.id}` + qs());
      const parents = (d.links && d.links.parents) || [];
      const children = (d.links && d.links.children) || [];
      const line = (rel, x) => {
        const row = h("div", { class: "wrow" },
          h("span", { class: "chip" }, rel),
          h("span", { class: "t" }, x.title || x.id),
          h("span", { class: "badge neutral" }, x.status || ""),
          h("button", { class: "btn btn-sm btn-quiet", onclick: async () => {
            const link = rel === "parent" ? { parent_id: x.id, child_id: t.id } : { parent_id: t.id, child_id: x.id };
            await act(api(`/links?parent_id=${link.parent_id}&child_id=${link.child_id}&board=${encodeURIComponent(S.board)}`, { method: "DELETE" }), "Unlinked");
            await loadBoard(true);
            await render();
          } }, "Remove dependency"));
        return row;
      };
      const rows = [...parents.map(p => line("parent", p)), ...children.map(c => line("child", c))];
      if (!rows.length) rows.push(h("div", { class: "col-empty" }, "no dependencies"));
      box.replaceChildren(...rows);
    } catch {}
  };
  render();
  return box;
}

function assigneeOptions() {
  const names = new Set((S.assignees || []).map(a => a.name));
  if (S.data) for (const a of S.data.assignees || []) names.add(a);
  return [["", "— unassigned —"], ...[...names].sort().map(n => [n, "@" + n])];
}
function selControl(opts, value) {
  const s = h("select", {}, ...opts.map(([v, label]) => h("option", { value: v, ...(v === value ? { selected: "" } : {}) }, label)));
  return s;
}
async function modelSelect(current) {
  const mo = await loadModelOpts();
  const opts = [["", current ? "— inherited —" : "Auto (profile default)"]];
  for (const p of (mo.providers || [])) for (const mm of (p.models || [])) {
    const name = typeof mm === "string" ? mm : (mm.id || mm.name || "");
    if (!name) continue;
    opts.push([`${p.slug}::${name}`, `${p.label || p.slug} · ${name}`]);
  }
  const cur = current ? (opts.find(([v]) => v.endsWith("::" + current)) || ["", ""])[0] : "";
  return selControl(opts, cur);
}

/* ---------------- settings / boards / workers ---------------- */
async function openSettings() {
  let orch = {};
  try { orch = await api("/orchestration"); } catch {}
  const orchSel = selControl([["", "(auto)"], ...(S.assignees || []).map(a => [a.name, "@" + a.name])], orch.orchestrator_profile || "");
  const defSel = selControl([["", "(auto)"], ...(S.assignees || []).map(a => [a.name, "@" + a.name])], orch.default_assignee || "");
  const autoDecomp = h("input", { type: "checkbox", ...(orch.auto_decompose ? { checked: "" } : {}) });
  const autoPromote = h("input", { type: "checkbox", ...(orch.auto_promote_children ? { checked: "" } : {}) });
  const profBox = h("div", {});
  (async () => {
    try {
      const r = await api("/profiles");
      for (const p of r.profiles || []) {
        const desc = h("input", { type: "text", value: p.description || "", placeholder: "what this profile is good at (used for routing)" });
        profBox.append(h("div", { style: "margin-bottom:10px" },
          h("label", {}, `${p.name}${p.is_default ? " (default)" : ""} — ${p.model || "no model"}${p.skill_count ? ` · ${p.skill_count} skills` : ""}`),
          h("div", { style: "display:flex;gap:6px" }, desc,
            h("button", { class: "btn btn-sm btn-quiet", onclick: async () => act(api(`/profiles/${p.name}`, { method: "PATCH", body: { description: desc.value }, board: false }), "Saved") }, "Save"),
            h("button", { class: "btn btn-sm btn-quiet", title: "auto-describe via LLM", onclick: async () => { try { await api(`/profiles/${p.name}/describe-auto`, { method: "POST", body: { overwrite: true }, board: false }); toast("Described"); openSettings._dirty = 1; } catch (e) { toast(e.message, "err"); } } }, "✨"))));
      }
    } catch (e) { profBox.append(h("div", { class: "col-empty" }, "profiles unavailable: " + e.message)); }
  })();

  const tabs = h("div", { style: "display:flex;gap:6px;margin-bottom:12px" });
  const pageOrch = h("div", {},
    h("label", {}, "Orchestrator profile"), orchSel,
    h("label", {}, "Default assignee"), defSel,
    h("label", { class: "fchk", style: "margin:10px 0" }, autoDecomp, "Auto-decompose triage tasks (dispatcher LLM)"),
    h("label", { class: "fchk", style: "margin-bottom:10px" }, autoPromote, "Auto-promote children when parents complete"),
    h("button", { class: "btn btn-accent btn-sm", onclick: async () => {
      await act(api("/orchestration", { method: "PUT", body: {
        orchestrator_profile: orchSel.value || null, default_assignee: defSel.value || null,
        auto_decompose: autoDecomp.checked, auto_promote_children: autoPromote.checked,
      }, board: false }), "Settings saved");
    } }, "Save orchestration"));
  const pageProf = h("div", { hidden: true }, profBox);
  const pageBoard = h("div", { hidden: true }, boardsPage(() => m.close()));
  const mkTab = (label, page) => {
    const b = h("button", { class: "btn btn-ghost btn-sm" }, label);
    b.onclick = () => { [pageOrch, pageProf, pageBoard].forEach(p => p.hidden = p !== page); $$(".btn", tabs).forEach(x => x.classList.remove("on")); b.classList.add("on"); };
    return b;
  };
  tabs.append(mkTab("Orchestration", pageOrch), mkTab("Profiles", pageProf), mkTab("Boards", pageBoard));
  const m = modalShell("Settings", h("div", {}, tabs, pageOrch, pageProf, pageBoard),
    [h("button", { class: "btn btn-quiet", onclick: () => m.close() }, "Close")]);
}

function boardsPage(closeCb) {
  const list = h("div", {});
  const slug = h("input", { type: "text", placeholder: "new-board-slug" });
  const name = h("input", { type: "text", placeholder: "Display name" });
  const icon = h("input", { type: "text", placeholder: "icon (emoji or glyph)" });
  const workdir = h("input", { type: "text", placeholder: "default project directory (absolute)" });
  const refresh = async () => {
    await loadBoards();
    list.replaceChildren(...S.boards.map(b => h("div", {
      class: "board-list-item" + (b.slug === S.board ? " current" : ""),
      // PR#13 review finding 4301554450: b.is_current is the GLOBAL Hermes
      // board pointer (e.g. what the CLI last selected), not this tab's
      // view. Keying clickability off it meant a board that was globally
      // current but not shown here could not be clicked into. Compare the
      // slug with S.board, exactly like the row highlight above.
      onclick: async () => { if (b.slug !== S.board) { await switchBoard(b.slug); if (closeCb) closeCb(); } },
    },
      h("span", { class: "board-icon" }, b.icon || "▦"),
      h("span", { class: "bname" }, b.name || b.slug),
      b.archived ? h("span", { class: "badge bad" }, "archived") : null,
      h("span", { class: "bmeta" }, `${b.total ?? "?"} tasks`, h("br"), b.slug),
      b.slug !== S.board && !b.archived ? h("button", { class: "btn btn-sm btn-quiet danger", onclick: async (e) => {
        e.stopPropagation();
        if (!confirm(`Archive board ${b.slug}? Its DB stays on disk.`)) return;
        await act(api(`/boards/${b.slug}`, { method: "DELETE", board: false }), "Board archived");
        refresh(); loadBoards();
      } }, "✕") : null,
    )));
  };
  refresh();
  return h("div", {},
    list,
    h("label", {}, "New board slug"), slug,
    h("div", { class: "grid2" }, h("div", {}, h("label", {}, "Display name"), name), h("div", {}, h("label", {}, "Icon"), icon)),
    h("label", {}, "Default project directory"), workdir,
    h("div", { style: "margin-top:10px" }, h("button", { class: "btn btn-accent btn-sm", onclick: async () => {
      if (!slug.value.trim()) { toast("Slug required", "err"); return; }
      try {
        await api("/boards", { method: "POST", body: { slug: slug.value.trim(), name: name.value.trim() || null, icon: icon.value.trim() || null, default_workdir: workdir.value.trim() || null, switch: false }, board: false });
        toast("Board created", "ok"); refresh(); loadBoards();
      } catch (e) { toast(e.message, "err"); }
    } }, "Create board")));
}

async function switchBoard(slug) {
  try {
    await api(`/boards/${slug}/switch`, { method: "POST", board: false });
  } catch (e) { /* switch is global; UI works purely via ?board= anyway */ }
  // Each board has its own SQLite event sequence, so the old board's cursor is
  // meaningless on the new one. Tear the socket down and zero the cursor BEFORE
  // loading the board: loadBoard() would otherwise Math.max() the stale cursor
  // into place, and the WS would then request events after a sequence the new
  // board never had (live refresh dead until it caught up). Reset backoff too.
  // close() only starts the closing handshake (CLOSING, not CLOSED), so an
  // already-queued old-board message could still fire afterwards: clear BOTH
  // onmessage and onclose before closing so no stale cursor can be restored.
  if (S.ws) { try { S.ws.onmessage = null; S.ws.onclose = null; S.ws.close(); } catch {} S.ws = null; }
  S.wsCursor = 0; S.wsBackoff = 1000; S.wsStale = false;
  S.board = slug; localStorage.setItem("kboard", slug);
  S.sel.clear(); closeDrawer(); S.remount = true;
  await Promise.all([loadBoard(), loadBoards(), loadAssignees()]);
  connectWS();
  toast("Board: " + slug, "ok");
}

function openBoardSwitcher() {
  const page = boardsPage(() => m.close());
  const m = modalShell("Switch kanban board", page, [h("button", { class: "btn btn-quiet", onclick: () => m.close() }, "Close")]);
}

function openWorkers() {
  const box = h("div", {});
  const refresh = async () => {
    try {
      const r = await api("/workers/active");
      box.replaceChildren();
      if (!r.count) { box.append(h("div", { class: "col-empty" }, "no live workers")); }
      for (const w of r.workers) {
        const now = Math.floor(Date.now() / 1000);
        box.append(h("div", { class: "wrow" },
          h("span", { class: "chip assignee" }, "@" + (w.profile || "?")),
          h("span", { class: "t", title: w.task_title }, w.task_title),
          h("span", { class: "chip time" }, "pid " + w.worker_pid),
          h("span", { class: "chip time", title: "heartbeat" }, "♥ " + ago(w.last_heartbeat_at, now)),
          h("span", { class: "chip time" }, ago(w.started_at, now)),
          h("button", { class: "btn btn-sm btn-quiet", onclick: () => inspectRun(w.run_id) }, "inspect"),
          // PR#13 review finding 4301554389: run termination routes
          // through reclaim_task(), which inside this container releases
          // the claim without signalling the host worker (duplicate-
          // dispatch hazard). Server rejects it with 403; the button is
          // disabled so the UI does not advertise a dead control.
          h("button", { class: "btn btn-sm btn-quiet danger", disabled: "", title: "Worker control is only available on the Hermes host: hermes kanban reclaim " + w.task_id }, "✕")),
        );
      }
    } catch (e) { box.replaceChildren(h("div", { class: "col-empty" }, e.message)); }
  };
  refresh();
  const timer = setInterval(refresh, 5000);
  // onClose fires from modalShell's internal close() for every path (Close
  // button, scrim click, Escape), so the 5s poll can no longer outlive the modal.
  const m = modalShell("Active workers", box, [h("button", { class: "btn btn-quiet", onclick: () => m.close() }, "Close")], { onClose: () => clearInterval(timer) });
}
async function inspectRun(runId) {
  try {
    const r = await api(`/runs/${runId}/inspect`);
    const rows = Object.entries(r).filter(([k]) => !["run_id"].includes(k)).map(([k, v]) => h("div", { class: "wrow" }, h("span", { class: "chip" }, k), h("span", { class: "t" }, String(Array.isArray(v) ? v.join(" ") : v))));
    const m = modalShell("Run " + runId, h("div", {}, ...rows), [h("button", { class: "btn btn-quiet", onclick: () => m.close() }, "Close")]);
  } catch (e) { toast(e.message, "err"); }
}

/* ---------------- drawer ---------------- */
let drawerSig = "";
/* PR#13 review finding 4319617532: renderDrawer() awaits /model-options
 * (uncached path) *after* its last caller-side staleness check, so opening
 * task A then task B can leave both requests pending and let a late A
 * response replace B's rendered drawer with A's edit controls while
 * S.drawerId still points to B. Mirror the board-refresh guard already used
 * in loadBoard(): each render captures the selected task id plus a
 * monotonic sequence at entry, and rechecks both right after the await,
 * before any drawer DOM is replaced. */
let _drawerRenderSeq = 0;
let _drawerRenderId = null;
function drawerIsStale(myId, mySeq) {
  // User switched cards while this render was in flight: the card now
  // selected owns the drawer.
  if (S.drawerId !== myId) return true;
  // Still viewing this task, but a newer render of THIS task has started
  // (e.g. refreshDrawer raced the open): the newest one wins. A late render
  // started for a *different* task must not discard this one — that render
  // is dropped by the selected-task check above instead, otherwise the
  // drawer would be left stuck on "Loading…".
  return _drawerRenderId === myId && mySeq !== _drawerRenderSeq;
}
/* PR#13 review finding 4321368000: the typing guard in refreshDrawer() only
 * ran *before* its task-detail request, and renderDrawer() suspends again on
 * the uncached /model-options fetch, so text typed into the description or
 * comment box while either request was in flight was destroyed by the
 * drawer.replaceChildren() swap. Every code path that is about to replace
 * the drawer must recheck editing state *after* its final await and defer
 * via S.drawerPending instead of clobbering unsaved text. A short retry
 * timer re-runs the refresh even if no focusout ever fires (tab parked in
 * the editor), so a deferred update cannot be lost. */
function drawerEditorActive() {
  const a = document.activeElement;
  return !!(a && $("#drawer").contains(a) &&
    (/input|textarea/i.test(a.tagName) || a.isContentEditable));
}
let _drawerApplyTimer = null;
function deferDrawerApply(id) {
  if (S.drawerId !== id) return;   // user moved on; the new card owns the drawer
  S.drawerPending = true;
  clearTimeout(_drawerApplyTimer);
  _drawerApplyTimer = setTimeout(() => {
    _drawerApplyTimer = null;
    if (S.drawerPending && S.drawerId === id) refreshDrawer();
  }, 2000);
}
async function openDrawer(id) {
  S.drawerId = id;
  drawerSig = "";
  $("#drawerScrim").hidden = false;
  const drawer = $("#drawer");
  drawer.hidden = false;
  drawer.replaceChildren(h("div", { class: "drawer-body" }, "Loading…"));
  try {
    const d = await api(`/tasks/${id}` + qs());
    if (S.drawerId !== id) return;              // user switched cards mid-fetch
    // PR#13 review finding 4321368000: the user may have started editing a
    // freshly-rendered field while THIS request was in flight. Defer; never
    // replace the drawer under an active editor.
    if (drawerEditorActive()) { deferDrawerApply(id); return; }  // typing began mid-fetch
    // Commit the signature/data only once the render actually landed (see
    // renderDrawer's return). A render dropped by drawerIsStale() or deferred
    // by the editor check must leave drawerSig behind, so a later refresh
    // still sees the payload as changed and retries.
    const applied = await renderDrawer(d).catch(() => false);
    if (applied) { S.drawerData = d; drawerSig = drawerSigOf(d); }
    else deferDrawerApply(id);
  } catch (e) {
    // PR#13 review finding 4321367982: same selected-task check as the
    // success path above. If this request was abandoned (user opened task
    // B, or closed the drawer — closeDrawer() nulls S.drawerId) while it
    // was in flight, its failure must not replace the drawer currently on
    // screen. Discard it; the task that owns the drawer owns its state.
    if (S.drawerId !== id) return;              // stale failure for an abandoned task
    drawer.replaceChildren(h("div", { class: "drawer-body" }, "Failed: " + e.message));
  }
}
function closeDrawer() {
  S.drawerId = null; S.drawerData = null; drawerSig = "";
  $("#drawer").hidden = true; $("#drawerScrim").hidden = true;
}
/* Background refresh: no "Loading…" flash. Zero DOM work when the payload is
   byte-identical; when it changed we preserve scroll + caret unless the user is
   actively typing, in which case we defer until they leave the field. */
function drawerSigOf(d) {
  // task.age ticks every fetch; it only feeds the age chips which render from
  // the same timestamps client-side, so exclude it from the change signature.
  return fp(d, (k, v) => k === "age" ? undefined : v);
}
async function refreshDrawer() {
  if (!S.drawerId) return;
  if (document.hidden) return;   // visibility handler catches up on return
  const id = S.drawerId;
  const drawer = $("#drawer");
  if (drawerEditorActive()) { S.drawerPending = true; return; }
  try {
    const d = await api(`/tasks/${id}` + qs());
    if (S.drawerId !== id) return;
    // PR#13 review finding 4321368000: recheck after the detail request —
    // the user may have started editing a description or comment while it
    // was in flight. renderDrawer() rechecks again after its own final
    // await (model options) and returns false when it drops the swap.
    if (drawerEditorActive()) { deferDrawerApply(id); return; }
    const sig = drawerSigOf(d);
    if (sig === drawerSig) { S.drawerData = d; return; }
    const body = drawer.querySelector(".drawer-body");
    const keep = { scrollTop: body ? body.scrollTop : 0, left: body ? body.scrollLeft : 0 };
    const af = document.activeElement;
    const all = [...drawer.querySelectorAll("*")];
    const focusIdx = af && af !== document.body && drawer.contains(af) ? all.indexOf(af) : -1;
    const applied = await renderDrawer(d).catch(() => false);
    if (!applied) { deferDrawerApply(id); return; }
    // Commit only once the render actually landed, so a dropped or deferred
    // render leaves drawerSig behind and a later refresh retries the change.
    S.drawerData = d; drawerSig = sig;
    const nb = drawer.querySelector(".drawer-body");
    if (nb) { nb.scrollTop = keep.scrollTop; nb.scrollLeft = keep.left; }
    if (focusIdx >= 0) {
      const na = [...drawer.querySelectorAll("*")][focusIdx];
      if (na && na.tagName === (all[focusIdx] || {}).tagName && na.focus) { try { na.focus(); } catch {} }
    }
  } catch {}
}
/* pick up deferred refreshes once the user leaves an editor */
document.addEventListener("focusout", e => {
  if (S.drawerPending && e.target && $("#drawer").contains(e.target)) {
    setTimeout(() => {
      if (!S.drawerPending) return;
      const a = document.activeElement;
      if (a && $("#drawer").contains(a) && (/input|textarea/i.test(a.tagName) || a.isContentEditable)) return;
      S.drawerPending = false;
      clearTimeout(_drawerApplyTimer); _drawerApplyTimer = null;
      refreshDrawer();
    }, 0);
  }
}, true);

async function renderDrawer(d) {
  // Returns true only when the drawer DOM was actually replaced. Callers
  // commit drawerSig/S.drawerData on true and defer on false (PR#13 review
  // finding 4321368000) so a dropped swap stays retryable.
  const t = d.task;
  const myId = t.id;
  const mySeq = ++_drawerRenderSeq;   // PR#13 finding 4319617532, see drawerIsStale
  _drawerRenderId = myId;
  const now = Math.floor(Date.now() / 1000);
  const drawer = $("#drawer");

  const titleEl = h("div", { class: "drawer-title", contenteditable: "true", spellcheck: "false" }, t.title);
  titleEl.addEventListener("blur", () => { const v = titleEl.textContent.trim(); if (v && v !== t.title) act(api(`/tasks/${t.id}` + qs(), { method: "PATCH", body: { title: v } }).then(loadBoard), "Title updated"); else if (!v) titleEl.textContent = t.title; });

  const head = h("div", { class: "drawer-head" },
    h("div", { class: "row1" },
      h("span", { class: "drawer-id" }, t.id),
      h("span", { class: "badge " + statusBadge(t.status) }, t.status),
      t.model_override ? h("span", { class: "chip" }, "🧠 " + t.model_override) : null,
      t.reasoning_effort ? h("span", { class: "chip" }, "💭 " + t.reasoning_effort) : null,
      h("button", { class: "btn drawer-close", title: "Close (Esc)", onclick: closeDrawer }, "✕")),
    titleEl);

  // fields grid
  const assignSel = selControl(assigneeOptions(), t.assignee || "");
  // PR#13 review finding 4301554389: never send reclaim_first from the
  // WebUI (server-side reject; see openReassignModal). For a RUNNING
  // task the plain reassign now surfaces upstream's 409 "still running"
  // refusal as a toast instead of silently releasing the claim beside a
  // live host worker.
  assignSel.addEventListener("change", () => act(api(`/tasks/${t.id}/reassign` + qs(), { method: "POST", body: { profile: assignSel.value || null, reclaim_first: false, reason: "dashboard" } }).then(() => { loadBoard(true); refreshDrawer(); }), "Assignee updated"));
  const prioInput = h("input", { type: "number", value: String(t.priority ?? 0) });
  prioInput.addEventListener("change", () => act(api(`/tasks/${t.id}` + qs(), { method: "PATCH", body: { priority: parseInt(prioInput.value || "0", 10) } }).then(loadBoard), "Priority updated"));
  const modelSel = await modelSelect(t.model_override);
  // PR#13 review finding 4319617532: the await above (uncached
  // /model-options fetch) is the last suspension point before the drawer
  // contents are replaced, and every caller-side check ran before it. If
  // the user switched cards (or a newer render of this card started) while
  // the fetch was in flight, drop this render entirely — the newer one
  // owns the drawer.
  if (drawerIsStale(myId, mySeq)) return false;
  // PR#13 review finding 4321368000: final recheck after the last await —
  // the user may have focused the description or comment box (or the
  // title) while the model-options fetch was in flight. Report the swap as
  // not applied so the caller defers instead of discarding unsaved text.
  if (drawerEditorActive()) return false;
  modelSel.addEventListener("change", async () => {
    if (!modelSel.value) { await act(api(`/tasks/${t.id}` + qs(), { method: "PATCH", body: { clear_model_override: true } }).then(loadBoard), "Model override cleared"); }
    else { const [prov, name] = modelSel.value.split("::"); await act(api(`/tasks/${t.id}` + qs(), { method: "PATCH", body: { model_override: name, provider_override: prov } }).then(loadBoard), "Model override set"); }
    refreshDrawer();
  });

  const kv = h("div", { class: "kv-grid" },
    kvBox("Assignee", assignSel),
    kvBox("Status", statusControl(t)),
    kvBox("Priority", prioInput),
    kvBox("Model", modelSel),
    kvBox("Tenant", h("span", {}, t.tenant || "—")),
    kvBox("Workspace", h("span", { title: t.workspace_path || "" }, `${t.workspace_kind}${t.workspace_path ? " · …" + t.workspace_path.slice(-28) : ""}`)),
    kvBox("Created", h("span", {}, `${ago(t.created_at, now)} ago · by ${t.created_by || "?"}`)),
    kvBox("Started", h("span", {}, t.started_at ? ago(t.started_at, now) + " ago" : "—")),
    kvBox("Completed", h("span", {}, t.completed_at ? ago(t.completed_at, now) + " ago" : "—")),
    kvBox("Goal mode", h("span", {}, t.goal_mode ? `on (${t.goal_max_turns || "?"} turns)` : "off")),
  );

  // body (editable)
  const bodyWrap = h("div", {});
  const renderBody = () => {
    bodyWrap.replaceChildren(
      h("div", { style: "display:flex;align-items:center" },
        h("button", { class: "btn btn-sm btn-quiet", style: "margin-left:auto", onclick: () => { editing = !editing; renderBody(); if (editing) setTimeout(() => ta.focus(), 20); } }, editing ? "Cancel" : "Edit description")),
      editing ? h("div", { class: "body-edit" }, ta,
        h("div", { style: "text-align:right;margin-top:6px" },
          h("button", { class: "btn btn-accent btn-sm", onclick: async () => { await act(api(`/tasks/${t.id}` + qs(), { method: "PATCH", body: { body: ta.value } }).then(loadBoard), "Description saved"); editing = false; renderBody(); } }, "Save")
        )) :
      h("div", { class: "md", html: md(t.body || "*(no description)*") })
    );
  };
  let editing = false;
  const ta = h("textarea", {}, t.body || "");
  renderBody();

  // diagnostics
  const diagBox = h("div", {});
  for (const dg of (t.diagnostics || [])) {
    diagBox.append(h("div", { class: "diag" },
      h("b", {}, dg.kind + " ×" + (dg.count || 1)), ` (${dg.severity || "warn"})`,
      h("div", { class: "md", html: textEsc(dg.summary || JSON.stringify(dg).slice(0, 300)) })));
  }

  // result
  const resultBox = (t.result || t.latest_summary) ? h("div", {},
    h("div", { class: "dsec" }, "Final Result (run summary)"),
    h("div", { class: "md", html: md([t.latest_summary, t.result && t.result !== t.latest_summary ? "result: " + t.result : ""].filter(Boolean).join("\n")) }) ) : null;

  // child results
  const childBox = (d.child_results || []).length ? h("div", {},
    h("div", { class: "dsec" }, "Child Results"),
    ...d.child_results.map(c => h("div", { class: "run", onclick: () => openDrawer(c.id), style: "cursor:pointer" },
      h("div", { class: "rrow" }, h("b", {}, c.title), h("span", { class: "badge " + statusBadge(c.status) }, c.status), h("span", { class: "drawer-id" }, c.id)),
      c.latest_summary ? h("div", { class: "summary" }, c.latest_summary) : null))) : null;

  // dependencies
  const depBox = h("div", {}, h("div", { class: "dsec" }, "Dependencies",
    h("button", { class: "btn btn-sm btn-quiet", onclick: () => openDepModal(t) }, "＋ link")));
  const deps = [...((d.links && d.links.parents) || []).map(x => ["parent", x]), ...((d.links && d.links.children) || []).map(x => ["child", x])];
  if (!deps.length) depBox.append(h("div", { class: "col-empty" }, "no dependencies"));
  for (const [rel, x] of deps) {
    depBox.append(h("div", { class: "wrow" },
      h("span", { class: "chip" }, rel),
      h("span", { class: "t", style: "cursor:pointer", onclick: () => openDrawer(x.id), title: x.id }, x.title || x.id),
      x.status ? h("span", { class: "badge " + statusBadge(x.status) }, x.status) : null,
      h("button", { class: "btn btn-sm btn-quiet", title: "Remove dependency", onclick: () => act(api(`/links?parent_id=${rel === "parent" ? x.id : t.id}&child_id=${rel === "parent" ? t.id : x.id}&board=${encodeURIComponent(S.board)}`, { method: "DELETE" }).then(() => { loadBoard(true); refreshDrawer(); }), "Unlinked") }, "✕")));
  }

  // comments
  const cmts = h("div", {}, ...(d.comments || []).slice().reverse().map(c => h("div", { class: "comment" },
    h("div", { class: "who" }, c.author || "?", h("time", {}, fmtTime(c.created_at))),
    h("p", { html: md(c.body) }))),
    h("div", { class: "comment-form" },
      (() => { const i = h("textarea", { placeholder: "Comment…", rows: 1 }); const btn = h("button", { class: "btn btn-accent", onclick: async () => { if (!i.value.trim()) return; btn.disabled = true; try { await api(`/tasks/${t.id}/comments` + qs(), { method: "POST", body: { body: i.value.trim(), author: "dashboard" } }); i.value = ""; refreshDrawer(); } catch (e) { toast(e.message, "err"); } finally { btn.disabled = false; } } }, "Comment"); return [i, btn]; })()));

  // attachments
  const attBox = h("div", {}, ...(d.attachments || []).map(a => h("div", { class: "wrow" },
    h("span", { class: "chip" }, "📎"),
    h("a", { href: API + "/attachments/" + a.id + "?board=" + encodeURIComponent(S.board), target: "_blank", class: "t" }, a.filename),
    h("span", { class: "chip time" }, fmtBytes(a.size)),
    h("button", { class: "btn btn-sm btn-quiet", title: "Remove attachment", onclick: () => act(api(`/attachments/${a.id}` + qs(), { method: "DELETE" }).then(refreshDrawer), "Removed") }, "✕"))),
    (() => {
      const f = h("input", { type: "file", hidden: "" });
      const up = h("button", { class: "btn btn-sm btn-quiet", onclick: () => f.click() }, "Upload file");
      f.addEventListener("change", async () => {
        if (!f.files[0]) return;
        const fd = new FormData(); fd.append("file", f.files[0]);
        try {
          const res = await fetch(`${API}/tasks/${t.id}/attachments?board=${encodeURIComponent(S.board)}`, { method: "POST", headers: { "X-Kanban-UI": "1" }, credentials: "same-origin", body: fd });
          if (res.status === 401) { showLogin(); throw new Error("session expired — log in again"); }
          if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
          toast("Uploaded", "ok"); refreshDrawer();
        } catch (e) { toast(e.message, "err"); }
      });
      return h("div", { style: "margin-top:6px" }, up, f);
    })());

  // runs
  const runs = h("div", {}, ...(d.runs || []).slice().reverse().map(r => h("div", { class: "run" },
    h("div", { class: "rrow" },
      h("b", {}, "run " + r.id),
      h("span", { class: "chip assignee" }, "@" + (r.profile || "?")),
      h("span", { class: "badge " + runBadge(r) }, r.outcome || r.status),
      r.worker_pid ? h("span", { class: "chip time" }, "pid " + r.worker_pid) : null,
      h("span", { class: "chip time" }, fmtTime(r.started_at) + (r.ended_at ? " → " + fmtTime(r.ended_at) : " → …")),
      r.ended_at === null ? h("button", { class: "btn btn-sm btn-quiet", onclick: () => inspectRun(r.id) }, "inspect") : null,
      (d.runs.length > 3 && (d.runs.slice().reverse().indexOf(r) > 5)) ? null : null),
    r.summary ? h("div", { class: "summary" }, r.summary) : null,
    r.error ? h("div", { class: "summary", style: "color:var(--danger)" }, r.error) : null)));
  let showAllRuns = false;

  // worker log
  const logBox = h("div", {});
  const loadLog = async () => {
    try {
      const r = await api(`/tasks/${t.id}/log` + qs());
      logBox.replaceChildren(
        h("div", { style: "display:flex;gap:8px;align-items:center;margin-bottom:6px" },
          h("span", { class: "drawer-id" }, r.path || ""),
          r.truncated ? h("span", { class: "chip warn" }, "tail only") : null,
          h("button", { class: "btn btn-sm btn-quiet", style: "margin-left:auto", onclick: loadLog }, "Refresh log")),
        r.exists ? h("pre", { class: "logbox" }, r.content || "(empty)") : h("div", { class: "col-empty" }, "no worker log yet"));
    } catch (e) { logBox.replaceChildren(h("div", { class: "col-empty" }, e.message)); }
  };

  // events
  const evts = h("div", { style: "max-height:220px;overflow:auto" }, ...(d.events || []).slice().reverse().slice(0, 50).map(e => h("div", { class: "event" },
    h("time", {}, fmtTime(e.created_at)),
    h("span", { class: "ek" }, e.kind),
    h("span", {}, (() => { try { return e.payload ? JSON.stringify(e.payload).slice(0, 140) : ""; } catch { return String(e.payload || "").slice(0, 140); } })()))));

  // notify toggles
  const notifyBox = h("div", {});
  (async () => {
    try {
      const r = await api(`/home-channels?task_id=${t.id}&board=${encodeURIComponent(S.board)}`);
      const chans = r.home_channels || [];
      if (!chans.length) { notifyBox.append(h("div", { class: "col-empty" }, "no home channels configured")); return; }
      notifyBox.append(h("div", { class: "md", style: "color:var(--text-3);font-size:.8rem;margin-bottom:4px" }, "Sending updates to home channels on completion/block"));
      for (const ch of chans) {
        const tg = h("button", { class: "btn btn-sm " + (ch.subscribed ? "btn-accent" : "btn-quiet"), onclick: async () => {
          try {
            await api(`/tasks/${t.id}/home-subscribe/${ch.platform}` + qs(), { method: ch.subscribed ? "DELETE" : "POST" });
            toast(ch.subscribed ? "Unsubscribed" : "Subscribed", "ok"); refreshDrawer();
          } catch (e) { toast(e.message, "err"); }
        } }, `${ch.platform}${ch.name ? " · " + ch.name : ""} ${ch.subscribed ? "✓" : ""}`);
        notifyBox.append(tg);
      }
    } catch (e) { notifyBox.append(h("div", { class: "col-empty" }, e.message)); }
  })();

  // actions footer (rendered via footerActionButtons so disabled menu-model
  // items keep their state here too — PR#13 finding 4321368019)
  const actions = h("div", { class: "drawer-actions" },
    ...footerActionButtons(t),
    h("button", { class: "btn btn-ghost btn-sm", style: "margin-left:auto", onclick: () => openCardMenu(t, innerWidth - 260, innerHeight - 320) }, "⋯ More"));

  drawer.replaceChildren(head,
    h("div", { class: "drawer-body" },
      kv,
      diagBox.children.length ? h("div", {}, h("div", { class: "dsec" }, "Diagnostics"), diagBox) : null,
      h("div", { class: "dsec" }, "Description"), bodyWrap,
      resultBox, childBox, depBox,
      h("div", { class: "dsec" }, "Comments", h("span", { class: "chip" }, String((d.comments || []).length))), cmts,
      h("div", { class: "dsec" }, "Attachments"), attBox,
      h("div", { class: "dsec" }, "Notify home channels"), notifyBox,
      h("div", { class: "dsec" }, "Runs", h("span", { class: "chip" }, String((d.runs || []).length))), runs,
      h("div", { class: "dsec" }, "Worker log"), logBox,
      h("div", { class: "dsec" }, "Events"), evts),
    actions);

  loadLog();
  return true;   // swap applied (see return-contract note at the function top)
}
function kvBox(k, vNode) { return h("div", { class: "kv" }, h("div", { class: "k" }, k), h("div", { class: "v" }, vNode)); }
function statusBadge(s) { return ["done"].includes(s) ? "ok" : ["blocked", "archived"].includes(s) ? "bad" : ["running", "scheduled", "review"].includes(s) ? "warn" : "neutral"; }
function runBadge(r) { const o = (r.outcome || "").toLowerCase(); if (["completed", "done", "success"].includes(o)) return "ok"; if (["failed", "crashed", "gave_up", "error", "timeout"].includes(o)) return "bad"; if (r.ended_at === null) return "warn"; return "neutral"; }
function statusControl(t) {
  // PR#13 review finding 4319617397: for a RUNNING card the "→ ready"
  // option is visible-but-disabled (the PATCH would be refused server-side
  // at _set_status_direct; reclaim belongs on the Hermes host). Every
  // other target stays available.
  const opts = [[ "", t.status], ...["triage", "todo", "scheduled", "ready", "blocked", "review", "done"]
    .filter(s => s !== t.status)
    .map(s => [s, "→ " + s + (t.status === "running" && s === "ready" ? " (disabled in the WebUI)" : "")])];
  const sel = selControl(opts, "");
  if (t.status === "running") {
    const readyOpt = [...sel.options].find(o => o.value === "ready");
    if (readyOpt) { readyOpt.disabled = true; readyOpt.title = "Requeuing a running task would release the worker claim without signalling the host worker; run `hermes kanban reclaim <id>` on the Hermes host instead"; }
  }
  sel.addEventListener("change", async () => {
    if (!sel.value) return;
    if (sel.value === "done") { openCompleteModal(t); sel.value = ""; return; }
    if (sel.value === "blocked") { promptReason("Block reason", r => act(api(`/tasks/${t.id}` + qs(), { method: "PATCH", body: { status: "blocked", block_reason: r } }).then(() => { loadBoard(true); refreshDrawer(); }), "Blocked")); sel.value = ""; return; }
    await act(api(`/tasks/${t.id}` + qs(), { method: "PATCH", body: { status: sel.value } }).then(() => { loadBoard(true); refreshDrawer(); }));
    sel.value = "";
  });
  return sel;
}

/* ---------------- bulk bar ---------------- */
function renderBulkBar() {
  const existing = $("#bulkBar");
  if (existing) existing.remove();
  if (!S.sel.size) return;
  const bar = h("div", { class: "bulkbar", id: "bulkBar" },
    h("span", { class: "chip" }, String(S.sel.size) + " selected"),
    h("button", { class: "btn btn-sm btn-quiet", onclick: () => { const ids = [...S.sel]; S.sel.clear(); moveTasks(ids, "todo").then(render); } }, "→ Todo"),
    h("button", { class: "btn btn-sm btn-quiet", onclick: () => { const ids = [...S.sel]; S.sel.clear(); moveTasks(ids, "ready").then(render); } }, "→ Ready"),
    h("button", { class: "btn btn-sm btn-quiet", onclick: () => { const ids = [...S.sel]; S.sel.clear(); moveTasks(ids, "blocked").then(render); } }, "→ Blocked"),
    h("button", { class: "btn btn-sm btn-quiet", onclick: () => { const ids = [...S.sel]; S.sel.clear(); moveTasks(ids, "done").then(render); } }, "→ Done"),
    h("button", { class: "btn btn-sm btn-quiet danger", onclick: () => { const ids = [...S.sel]; S.sel.clear(); moveTasks(ids, "archived").then(render); } }, "Archive"),
    // PR#13 review finding 4323915007: the container-local dispatcher
    // pass is refused server-side (POST /api/kanban/dispatch -> 403):
    // dispatch_once() from this container clears expired HOST worker
    // claims without signalling the host worker, inviting a duplicate
    // dispatch. The button stays visible but disabled (same contract as
    // the worker-terminate ✕) and constructs NO /dispatch fetch; run
    // `hermes kanban dispatch` on the Hermes host instead.
    h("button", { class: "btn btn-sm btn-quiet", disabled: "", title: "Dispatcher passes are only available on the Hermes host: hermes kanban dispatch (disabled in the WebUI)" }, "⇢ dispatch"),
    h("button", { class: "btn btn-sm btn-quiet", onclick: () => { S.sel.clear(); render(); } }, "Clear"));
  document.body.append(bar);
}

/* ---------------- auth (session cookie; no credential in JS) ---------- */
/* PR#13 review finding 4331497777 (round 6): the overlay used an inline
 * z-index 60 — below the drawer scrim (70) and drawer (80), and level with
 * the bulk bar — so a 401 arriving while a drawer, modal, or context menu
 * was open buried the re-login form (worst case: mobile, full-width
 * drawer). It now carries the .auth-overlay class, which pins it to
 * var(--z-auth), the TOPMOST tier of the documented stacking scale in
 * app.css. Accessibility: the overlay is a labelled modal dialog; focus
 * moves into the token field when it appears and returns to the previously
 * focused element when reauth succeeds. Success is a SOFT dismiss (remove
 * the overlay, catch-up refresh) instead of location.reload(), so the
 * previous view — an open drawer, an open modal — survives reauthenticating. */
function authOverlayOpen(box) {
  const previous = document.activeElement;
  const overlay = h("div", {
    id: "loginOverlay", class: "scrim auth-overlay",
    role: "dialog", "aria-modal": "true", "aria-label": "Re-authentication required",
  }, box);
  overlay.hidden = false;
  document.body.append(overlay);
  return previous;
}
function authOverlayDismiss(previous) {
  const overlay = document.getElementById("loginOverlay");
  if (overlay) overlay.remove();
  // Restore focus to whatever had it before the 401 surfaced, if it is
  // still in the document; otherwise fall back to the body.
  const target = previous && document.contains(previous) ? previous : document.body;
  if (typeof target.focus === "function") target.focus();
}
/* PR#13 review round-9 blocking finding (t_d7bdedae): server.py gates every
 * /api/ route on the session cookie, so a cold unauthenticated visit burns
 * loadCfg/loadBoards/loadAssignees/loadBoard and the WS connect on 401s, and
 * the old three-call soft dismiss (loadBoard/refreshDrawer/pollWorkers only)
 * left the selector/config state empty until a manual page reload — the
 * exact reload the soft dismiss was supposed to remove. Successful login
 * therefore re-runs the FULL protected bootstrap the boot IIFE would have
 * done with a session: config, board list, assignees, board payload, worker
 * pill, model-option cache (a pre-login 401 poisons it with an empty
 * provider list), and an explicit re-connect of the event socket (the boot
 * socket was opened without a session cookie). Nothing here touches the
 * drawer/modal underneath: loadBoard(true) renders columns only, and
 * refreshDrawer() no-ops unless a drawer is already open. */
async function postLoginBootstrap() {
  S.modelOpts = null;                        // drop any 401-poisoned cache
  await Promise.all([loadCfg(), loadBoards(), loadAssignees()]);
  await loadBoard(true);
  refreshDrawer();
  pollWorkers();
  S.wsBackoff = 1000;
  connectWS();                               // replaces the pre-login socket
}
function showLogin() {
  if (document.getElementById("loginOverlay")) return;
  // Round-3 finding 1: never render a credential form on a plaintext
  // origin. The server also refuses /login without the HTTPS proxy's
  // X-Forwarded-Proto, but refusing client-side means the token can never
  // even be typed and submitted to an http:// origin by accident.
  if (location.protocol !== "https:" && !/^127\.0\.0\.1$|^localhost$/.test(location.hostname)) {
    const box = h("div", { class: "panel", style: "width:min(340px,90vw);padding:18px" },
      h("div", { class: "dsec" }, "Hermes Kanban — login"),
      h("div", { class: "chip err", style: "min-height:18px;margin-bottom:8px" }, "plaintext origin: use the https endpoint"),
      h("a", { class: "btn btn-accent", style: "width:100%", href: "https://" + location.host + location.pathname }, "Continue over HTTPS"));
    authOverlayOpen(box);
    return;
  }
  const input = h("input", { type: "password", placeholder: "Access token", autocomplete: "current-password", style: "width:100%;margin-bottom:8px" });
  const err = h("div", { class: "chip err", style: "min-height:18px;margin-bottom:8px" }, "");
  const btn = h("button", { class: "btn btn-accent", style: "width:100%" }, "Log in");
  const submit = async () => {
    const v = input.value;
    if (!v) return;
    btn.disabled = true;
    try {
      const r = await fetch("/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ token: v }), credentials: "same-origin" });
      if (!r.ok) { const j = await r.json().catch(() => ({})); err.textContent = typeof j.detail === "string" ? j.detail : "login failed"; btn.disabled = false; input.value = ""; input.focus(); return; }
      // Soft dismiss: the new session cookie is set; drop the overlay and
      // run the full authenticated bootstrap (config/boards/assignees/board/
      // workers/WS), not just the three-call visibility catch-up. No reload
      // — the drawer/modal underneath stays exactly where it was. Every
      // loader inside catches its own failures, so a post-login hiccup can
      // never re-show an error on the (already removed) overlay.
      authOverlayDismiss(previous);
      await postLoginBootstrap();
    } catch (e) { err.textContent = String(e); btn.disabled = false; }
  };
  btn.onclick = submit;
  input.addEventListener("keydown", e => { if (e.key === "Enter") submit(); });
  const box = h("div", { class: "panel", style: "width:min(340px,90vw);padding:18px" },
    h("div", { class: "dsec" }, "Hermes Kanban — login"), input, err, btn);
  const previous = authOverlayOpen(box);
  input.focus();
}

/* ---------------- live events ---------------- */
function connectWS() {
  if (S.ws) { try { S.ws.onmessage = null; S.ws.onclose = null; S.ws.close(); } catch {} S.ws = null; }
  if (document.hidden) { S.wsStale = true; return; }   // reconnect on visibility
  const proto = location.protocol === "https:" ? "wss" : "ws";
  // Same-origin WS upgrade authenticates with the HttpOnly session cookie;
  // no credential in the URL (browser history / access logs never see one).
  const url = proto + "://" + location.host + API + "/events?since=" + S.wsCursor + "&board=" + encodeURIComponent(S.board);
  const ws = new WebSocket(url);
  S.ws = ws;
  ws.onmessage = e => {
    // Identity guard: if this socket is no longer the current one (board was
    // switched / socket replaced), its events are stale and must not touch
    // global cursor or trigger refreshes for the new board.
    if (S.ws !== ws) return;
    try {
      const msg = JSON.parse(e.data);
      if (msg.cursor) S.wsCursor = Math.max(S.wsCursor, msg.cursor);
      if (msg.events && msg.events.length) scheduleLiveRefresh();
    } catch {}
  };
  ws.onclose = () => { if (S.ws === ws) { setTimeout(connectWS, S.wsBackoff); S.wsBackoff = Math.min(S.wsBackoff * 1.7, 30000); } };
  ws.onopen = () => { S.wsBackoff = 1000; };
}
let liveTimer = null;
function scheduleLiveRefresh() {
  clearTimeout(liveTimer);
  liveTimer = setTimeout(async () => {
    await loadBoard(true);
    if (S.drawerId) refreshDrawer();
    pollWorkers();
  }, 600);
}
async function pollWorkers() {
  if (document.hidden) return;
  try {
    const r = await api("/workers/active");
    $("#workersDot").hidden = r.count === 0;
    const pill = $("#workersCount");
    pill.hidden = !r.count; pill.textContent = r.count;
  } catch {}
}

/* ---------------- wiring ---------------- */
function wire() {
  $("#search").addEventListener("input", e => { S.search = e.target.value; render(); });
  $("#tenantSel").addEventListener("change", e => { S.tenant = e.target.value; render(); });
  $("#assigneeSel").addEventListener("change", e => { S.assignee = e.target.value; render(); });
  $("#lanesChk").addEventListener("change", e => { S.lanes = e.target.checked; render(); });
  $("#archivedChk").addEventListener("change", async e => { S.archived = e.target.checked; await loadBoard(); });
  $("#clearFilters").addEventListener("click", () => { S.tenant = ""; S.assignee = ""; S.search = ""; S.archived = false; $("#search").value = ""; $("#archivedChk").checked = false; loadBoard(); });
  $("#filterBtn").addEventListener("click", () => { const b = $("#filterBar"); b.hidden = !b.hidden; });
  $("#newTaskBtn").addEventListener("click", () => openCreateModal({ column: S.mobileCol }));
  $("#settingsBtn").addEventListener("click", openSettings);
  $("#workersBtn").addEventListener("click", openWorkers);
  $("#boardBtn").addEventListener("click", openBoardSwitcher);
  // PR#13 review finding 4323915007: the topbar "Nudge dispatcher"
  // control triggered the container-local pass (POST /api/kanban/dispatch
  // -> dispatch_once), which from this container clears expired HOST
  // worker claims without signalling the host worker (duplicate-dispatch
  // hazard). Server refuses it with 403; the control is disabled here and
  // NO click handler is registered at all, so no /dispatch fetch can ever
  // be constructed. Dispatch belongs on the Hermes host: run
  // `hermes kanban dispatch` there, or let the gateway's embedded
  // dispatcher tick.
  const nudgeBtn = $("#nudgeBtn");
  nudgeBtn.disabled = true;
  nudgeBtn.title = "Dispatcher passes are only available on the Hermes host: hermes kanban dispatch (disabled in the WebUI)";
  const theme = localStorage.getItem("ktheme") || "dark";
  document.documentElement.dataset.theme = theme;
  $("#themeBtn").addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    localStorage.setItem("ktheme", next);
  });
  $("#drawerScrim").addEventListener("click", closeDrawer);
  document.addEventListener("keydown", e => {
    if (e.key === "Escape" && S.drawerId && !$("#modalRoot").children.length) closeDrawer();
    if (e.key === "n" && !/input|textarea|select/i.test(e.target.tagName) && !e.target.isContentEditable && !S.drawerId) { e.preventDefault(); openCreateModal({ column: S.mobileCol }); }
  });
  // trash zone (drag-to-archive)
  const trash = $("#trashZone");
  trash.addEventListener("dragover", e => { e.preventDefault(); trash.classList.add("over"); });
  trash.addEventListener("dragleave", () => trash.classList.remove("over"));
  trash.addEventListener("drop", async e => {
    e.preventDefault(); trash.classList.remove("over"); trash.hidden = true;
    const ids = JSON.parse(e.dataTransfer.getData("text/plain") || "[]");
    if (ids.length) await moveTasks(ids, "archived");
  });
  setInterval(() => { if (!document.hidden) loadBoard(true); }, 30000); // safety-net poll (WS is primary)
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) {
      if (S.wsStale || !S.ws || S.ws.readyState > 1) { S.wsStale = false; connectWS(); }
      loadBoard(true); refreshDrawer(); pollWorkers();   // catch up on what we missed
    }
  });
}

(async function boot() {
  wire();
  await Promise.all([loadCfg(), loadBoards(), loadAssignees()]);
  await loadBoard();
  connectWS();
  pollWorkers();
  setInterval(pollWorkers, 20000);
})();
