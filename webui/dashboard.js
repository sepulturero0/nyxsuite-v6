"use strict";

const HOST = location.hostname || "127.0.0.1";
const TOKEN = (typeof window !== "undefined" && window.__NYX_TOKEN__) || "";
function tokenHeaders() { return TOKEN ? { "X-Nyx-Token": TOKEN, "X-Nyxify-Token": TOKEN } : {}; }
function adspowerStateText(r) {
  if (!r || !r.adspower_open_profile_id) return "";
  if (r.adspower_open === true) return "Open";
  if (r.adspower_open === false) return "Closed";
  return "Checking";
}

const PRODUCTS = {
  nyx: {
    base: `http://${HOST}:8865`, key: "profile_id",
    tiles: ["pending", "running", "done", "failed"],
    columns: [
      { h: "AdsPower ID", f: r => r.profile_id || "", s: "" },
      { h: "AdsPower", f: adspowerStateText, s: "" },
      { h: "Username", f: r => (r.username || "").replace(/^\s*snapchat\s*:\s*/i, ""), s: "" },
      { h: "Model", f: r => r.model || "", s: "model" },
      { h: "Date Added", f: r => (r.created_at || "").slice(0, 19).replace("T", " "), s: "created_at" },
      { h: "Status", badge: true, f: r => r.status || "", s: "status" },
      { h: "Last Step", f: r => r.last_step || "", s: "" },
    ],
    queue: [["Rerun Failed", "/queue/rerun_failed", ""], ["Reset Stuck", "/queue/reset_stuck", ""],
            ["Flush", "/bot/finish_remaining", ""], ["Clear Completed", "/queue/prune_completed", ""],
            ["Remove Missing", "/queue/remove_missing_profile", ""]],
    row: [["Mark Done", "/queue/mark_done", ""], ["Relaunch", "/queue/relaunch", ""], ["Remove", "/queue/remove", "bad"]],
    group: [["Mark Done", "/queue/mark_done", ""], ["Relaunch", "/queue/relaunch", ""], ["Remove", "/queue/remove", "bad"]],
  },
  nyxify: {
    base: `http://${HOST}:8866`, key: "row_key",
    tiles: ["pending", "ready", "running", "done", "failed"],
    columns: [
      { h: "AdsPower ID", f: r => r.adspower_id || r.adspower_profile_id || "", s: "" },
      { h: "AdsPower", f: adspowerStateText, s: "" },
      { h: "Username", f: r => (r.username || "").replace(/^\s*snapchat\s*:\s*/i, ""), s: "" },
      { h: "Model", f: r => r.model || "", s: "model" },
      { h: "Date Added", f: r => (r.created_at || "").slice(0, 19).replace("T", " "), s: "created_at" },
      { h: "Proxy", f: r => r.proxy_address || "", s: "" },
      { h: "Status", badge: true, f: r => r.status || "", s: "status" },
      { h: "Last Step", f: r => r.last_step || "", s: "" },
      { h: "Error", f: r => r.error || "", s: "" },
    ],
    queue: [["Reset Failed", "/queue/reset_failed", ""], ["Clear Queue", "/queue/clear", "bad"]],
    row: [["Remove", "/queue/remove", "bad"]],
    group: [["Remove", "/queue/remove", "bad"]],
  },
};

// One merged queue table: Nyx (Bitmoji) + Nyxify (Signup) rows side by side.
// Every row is tagged with _product so actions dispatch to the right local API.
const SUITE = {
  tiles: ["pending", "ready", "running", "done", "failed"],
  columns: [
    { h: "Type", f: r => (r._product === "nyx" ? "Bitmoji" : "Signup"), s: "type" },
    { h: "AdsPower ID", f: r => r.profile_id || r.adspower_id || r.adspower_profile_id || "", s: "" },
    { h: "AdsPower", f: adspowerStateText, s: "" },
    { h: "Username", f: r => (r.username || "").replace(/^\s*snapchat\s*:\s*/i, ""), s: "" },
    { h: "Model", f: r => r.model || "", s: "model" },
    { h: "Date Added", f: r => (r.created_at || "").slice(0, 19).replace("T", " "), s: "created_at" },
    { h: "Proxy", f: r => r.proxy_address || "", s: "" },
    { h: "Status", badge: true, f: r => r.status || "", s: "status" },
    { h: "Last Step", f: r => r.last_step || "", s: "" },
    { h: "Error", f: r => r.error || "", s: "" },
  ],
};
const SUITE_QUEUE = [
  ["Rerun Failed", "nyx", "/queue/rerun_failed", ""],
  ["Reset Stuck", "nyx", "/queue/reset_stuck", ""],
  ["Flush", "nyx", "/bot/finish_remaining", ""],
  ["Clear Completed", "nyx", "/queue/prune_completed", ""],
  ["Remove Missing", "nyx", "/queue/remove_missing_profile", ""],
  null,
  ["Reset Failed", "nyxify", "/queue/reset_failed", ""],
  ["Clear Queue", "nyxify", "/queue/clear", "bad"],
];
const SUITE_ROW = [
  ["Mark Done", "nyx", "/queue/mark_done", ""],
  ["Relaunch", "nyx", "/queue/relaunch", ""],
  ["Remove", null, "/queue/remove", "bad"],
  ["Reset Failed", "nyxify", "/queue/reset_failed", ""],
];
const SUITE_GROUP = [
  ["Mark Done", "nyx", "/queue/mark_done", ""],
  ["Relaunch", "nyx", "/queue/relaunch", ""],
  ["Remove", null, "/queue/remove", "bad"],
  ["Reset Failed", "nyxify", "/queue/reset_failed", ""],
];

const state = {
  nyx: { rows: new Map(), counts: {}, bot: {}, usage: null, health: null, live: null, config: {}, advancedVisible: false, search: "", sort: "", dir: 1, statusFilter: "", checked: new Set() },
  nyxify: { rows: new Map(), counts: {}, bot: {}, usage: null, health: null, live: null, config: {}, fullautoVisible: false, proxyrankVisible: false, advancedVisible: false, bannedRows: [], proxyRankingRows: [], search: "", sort: "", dir: 1, statusFilter: "", checked: new Set() },
  suite: { search: "", sort: "", dir: 1, statusFilter: "", checked: new Set() },
  bridge: { settings: { transparent_tray_icon: false } },
  version: "",
  update: { checked: false, available: false, current: "", latest: "", latest_name: "", notes: "", backups: [], availableVersions: [] },
};
let active = "nyxify";
const selected = { suite: null, nyx: null, nyxify: null };

const el = id => document.getElementById(id);
const keyOf = (p, row) => String(row[PRODUCTS[p].key] || row.profile_id || row.row_key || "");

function applyProductSnapshot(p, snap) {
  if (!snap || snap.error) return;
  const m = new Map();
  (snap.rows || []).forEach(r => { const k = keyOf(p, r); if (k) m.set(k, r); });
  state[p].rows = m;
  state[p].counts = snap.counts || {};
  state[p].bot = snap.bot || {};
  state[p].usage = snap.adspower_usage || null;
  state[p].health = snap.adspower_health || null;
  state[p].live = snap.adspower_live || null;
  if (snap.config) state[p].config = snap.config;
  settleRunnerInFlight(p);
}

function applyUpdate(ev) {
  const p = ev.product; if (!state[p]) return;
  const m = state[p].rows;
  (ev.rows || []).forEach(r => { const k = keyOf(p, r); if (k) m.set(k, r); });
  (ev.removed || []).forEach(k => m.delete(k));
  if (ev.counts) state[p].counts = ev.counts;
  if (ev.bot) state[p].bot = ev.bot;
  if (ev.adspower_usage !== undefined && ev.adspower_usage !== null) state[p].usage = ev.adspower_usage;
  if (ev.adspower_health !== undefined) state[p].health = ev.adspower_health;
  if (ev.adspower_live !== undefined) state[p].live = ev.adspower_live;
  settleRunnerInFlight(p);
}

function settleRunnerInFlight(p) {
  // A Start/Stop latch is only released once the server confirms a real state.
  // While it's still in a transition (or the optimistic state is unconfirmed)
  // the runner buttons stay disabled so a duplicate click can't race a spawn.
  const st = String(((state[p] && state[p].bot) || {}).state || "").toLowerCase();
  if (st === "starting" || st === "stopping") return;
  if (runnerInFlight[p]) {
    runnerInFlight[p] = false;
    renderSuiteRunnerButtons();
    if (active === p) renderPanel(p);
  }
}

function onMessage(data) {
  let ev; try { ev = JSON.parse(data); } catch (e) { return; }
  if (ev.type === "snapshot") {
    const st = ev.status || {};
    state.version = (st.bridge || {}).version || "";
    state.bridge.settings = (st.bridge || {}).settings || state.bridge.settings;
    const prods = st.products || {};
    Object.keys(PRODUCTS).forEach(p => { if (prods[p]) applyProductSnapshot(p, prods[p]); });
  } else if (ev.type === "update") {
    applyUpdate(ev);
  }
  render();
}

let es = null, pollTimer = null;
function connect() {
  try {
    es = new EventSource(`${location.origin}/bridge/events?token=${encodeURIComponent(TOKEN)}`);
    es.onopen = () => { setConn(true); stopPolling(); };
    es.onmessage = e => onMessage(e.data);
    es.onerror = () => { setConn(false); startPolling(); };
  } catch (e) { setConn(false); startPolling(); }
}
function startPolling() {
  if (pollTimer) return;
  pollTimer = setInterval(async () => {
    try {
      const r = await fetch(`${location.origin}/bridge/status`);
      onMessage(JSON.stringify({ type: "snapshot", status: await r.json() }));
      setConn(true);
    } catch (e) { setConn(false); }
  }, 1500);
}
function stopPolling() { if (pollTimer) { clearInterval(pollTimer); pollTimer = null; } }
function setConn(ok) { const c = el("conn"); c.textContent = ok ? "live" : "offline"; c.className = "pill " + (ok ? "pill-ok" : "pill-bad"); }

async function callAction(p, path, payload) {
  try {
    const r = await fetch(PRODUCTS[p].base + path, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...tokenHeaders() },
      body: JSON.stringify({ ...(payload || {}), token: TOKEN }),
    });
    const d = await r.json().catch(() => ({}));
    toast(d.message || (d.ok !== false ? "Done." : (d.error || ("HTTP " + r.status))), d.ok !== false);
    return d;
  } catch (e) { toast("Request failed: " + e, false); return { ok: false, error: String(e) }; }
}

function updateReplaceBannedDashboardSummary(message, rows) {
  const summary = el("replace-banned-summary-nyxify");
  const removeBtn = el("remove-banned-nyxify");
  const warmupBtn = el("warmup-banned-nyxify");
  const ids = el("banned-adspower-ids-nyxify");
  if (Array.isArray(rows)) {
    state.nyxify.bannedRows = rows;
  }
  if (summary) summary.textContent = message || "";
  if (ids) ids.value = formatBannedAdspowerIds(state.nyxify.bannedRows);
  if (removeBtn) removeBtn.disabled = !state.nyxify.bannedRows.length;
  if (warmupBtn) warmupBtn.disabled = !state.nyxify.bannedRows.length;
  if (active === "banned") renderBannedPanel();
}

function formatBannedAdspowerIds(rows) {
  const seen = new Set();
  return (Array.isArray(rows) ? rows : [])
    .map(row => String((row && (row.adspower_id || row.adspower_profile_id || row.profile_id)) || "").trim())
    .filter(id => {
      if (!id || seen.has(id)) return false;
      seen.add(id);
      return true;
    })
    .join("\n");
}

async function scanBannedFromDashboard() {
  updateReplaceBannedDashboardSummary("Scanning latest SnapBoard snapshot...", []);
  try {
    const r = await fetch(PRODUCTS.nyxify.base + "/replace_banned/scan", { headers: tokenHeaders() });
    const d = await r.json().catch(() => ({}));
    if (!r.ok || d.ok === false) {
      updateReplaceBannedDashboardSummary(d.error || "Could not scan banned rows.", []);
      toast(d.error || "Could not scan banned rows.", false);
      return;
    }
    const rows = d.rows || [];
    updateReplaceBannedDashboardSummary(
      rows.length ? `Found ${rows.length} banned row(s).` : "No banned rows found.",
      rows
    );
    toast(d.message || "Banned scan complete.", true);
  } catch (e) {
    updateReplaceBannedDashboardSummary("Could not reach Nyxify local API.", []);
    toast("Banned scan failed: " + e, false);
  }
}

async function removeBannedFromDashboard() {
  const rows = state.nyxify.bannedRows || [];
  if (!rows.length) {
    updateReplaceBannedDashboardSummary("Scan banned rows first.", []);
    return;
  }
  const removeBtn = el("remove-banned-nyxify");
  if (removeBtn) removeBtn.disabled = true;
  updateReplaceBannedDashboardSummary(`Removing ${rows.length} banned row(s)...`, rows);
  const result = await callAction("nyxify", "/replace_banned/remove", { rows });
  if (result && result.ok !== false) {
    updateReplaceBannedDashboardSummary(result.message || "Remove banned finished.", []);
  } else {
    updateReplaceBannedDashboardSummary((result && (result.error || result.message)) || "Remove banned failed.", rows);
  }
}

async function warmupBannedFromDashboard() {
  const rows = state.nyxify.bannedRows || [];
  if (!rows.length) {
    updateReplaceBannedDashboardSummary("Scan banned rows first.", []);
    return;
  }
  const warmupBtn = el("warmup-banned-nyxify");
  if (warmupBtn) warmupBtn.disabled = true;
  updateReplaceBannedDashboardSummary(`Changing ${rows.length} banned row(s) to Warm Up...`, rows);
  const result = await callAction("nyxify", "/replace_banned/warmup", { rows });
  if (result && result.ok !== false) {
    updateReplaceBannedDashboardSummary(result.message || "Warm Up banned finished.", rows);
  } else {
    updateReplaceBannedDashboardSummary((result && (result.error || result.message)) || "Warm Up update failed.", rows);
  }
}

// Pull the live config straight from the product API so the Advanced form always
// reflects what is actually persisted. The SSE stream only carries `config` in
// the initial snapshot, so without this the form could render a stale config and
// a Save would write those stale values back — silently wiping real settings.
async function refreshConfig(p) {
  try {
    const r = await fetch(PRODUCTS[p].base + "/config", { headers: tokenHeaders() });
    const d = await r.json().catch(() => ({}));
    if (d && d.config) state[p].config = d.config;
  } catch (e) { /* keep whatever we have */ }
}

async function callBridge(action, payload) {
  try {
    const r = await fetch("/bridge/" + action, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...tokenHeaders() },
      body: JSON.stringify({ ...(payload || {}), token: TOKEN }),
    });
    return await r.json();
  } catch (e) { return { ok: false, error: String(e) }; }
}

function render() {
  el("version").textContent = state.version ? ("v" + state.version) : "v…";
  // The runner Start/Stop buttons live in the global sidebar dock, so keep them
  // current on every tick regardless of the active tab (cheap).
  renderSuiteRunnerButtons();
  if (active === "suite" || active === "nyx" || active === "nyxify") renderPanel(active);
  if (active === "banned") renderBannedPanel();
  // Settings is rendered once on tab open (see setActive); re-rendering on every
  // SSE tick would clobber in-progress edits in the config form.
}

function sortRows(rows, field, dir) {
  if (!field) return rows;
  const key = field === "type" ? "_product" : field;
  return [...rows].sort((a, b) => {
    let va = String(a[key] || "").toLowerCase();
    let vb = String(b[key] || "").toLowerCase();
    if (key === "created_at") { va = a[key] || ""; vb = b[key] || ""; }
    if (va < vb) return -1 * dir;
    if (va > vb) return 1 * dir;
    return 0;
  });
}

// Signature of the last rendered output. The dashboard re-runs renderPanel on
// every SSE message and on every 1.5s poll tick; rebuilding the whole table via
// innerHTML each time is the main source of lag (and it wiped text selections).
// When nothing that affects the table has changed we skip the rebuild entirely,
// so steady-state ticks become free.
const panelRenderSig = {};
// Separate memo for the status card + tiles so a row-only change (or a bot-only
// change) does not rebuild the table, and vice-versa. This is what makes
// steady-state SSE ticks free even though the runners update every ~0.5s.
const statusRenderSig = {};
// Guards against duplicate Start/Stop clicks while a request is in flight.
const runnerInFlight = {};
let updateCheckInFlight = false;
let updateApplyInFlight = false;
let updateApplyPollTimer = null;

function suiteRows() {
  const rows = [];
  ["nyx", "nyxify"].forEach(p => {
    state[p].rows.forEach(r => {
      const tagged = Object.assign({}, r);
      tagged._product = p;
      rows.push(tagged);
    });
  });
  return rows;
}

function suiteKey(r) { return r._product + ":" + keyOf(r._product, r); }

function suiteCounts() {
  const out = {};
  ["nyx", "nyxify"].forEach(p => {
    Object.entries(state[p].counts || {}).forEach(([k, v]) => {
      out[k] = (out[k] || 0) + (Number(v) || 0);
    });
  });
  return out;
}

function suiteBot() {
  const isActiveState = s => ["running", "paused", "waiting", "blocked"].includes(String(s || "").toLowerCase());
  const a = state.nyx.bot || {}, b = state.nyxify.bot || {};
  const primary = isActiveState(a.state) ? a : (isActiveState(b.state) ? b : (a.state ? a : b));
  return {
    state: primary.state || "stopped",
    detail: [a.detail, b.detail].filter(Boolean).join(" · "),
  };
}

function selectedSuiteProduct() {
  const k = selected.suite || "";
  const i = k.indexOf(":");
  return i === -1 ? "" : k.slice(0, i);
}

function dispatchSuiteAction(def, keys) {
  const product = def[1], path = def[2];
  keys.forEach(k => {
    const i = k.indexOf(":");
    if (i === -1) return;
    const prod = k.slice(0, i);
    if (product && product !== prod) return;
    const payload = { [PRODUCTS[prod].key]: k.slice(i + 1) };
    callAction(prod, path, payload);
  });
}

function renderPanel(p) {
  if (!p || p === "suite") { renderSuiteBody(); return; }
  renderProductBody(p);
}

function productStatusSignature(p, s) {
  return [
    (s.bot && s.bot.state) || "",
    (s.bot && s.bot.detail) || "",
    p === "nyxify" ? JSON.stringify(s.usage || {}) : "",
    p === "nyxify" ? JSON.stringify(s.live || {}) : "",
    JSON.stringify(s.counts || {}),
  ].join("##");
}

function productTableSignature(p, cfg, s) {
  const rawRows = [...s.rows.values()];
  return [
    s.statusFilter || "",
    (s.search || "").toLowerCase().trim(),
    s.sort || "",
    String(s.dir),
    selected[p] || "",
    [...s.checked].sort().join(","),
    rawRows.map(r => keyOf(p, r) + ":" + cfg.columns.map(c => String(c.f(r))).join("|")).join("\n"),
  ].join("##");
}

function updateProductStatus(p, cfg) {
  const s = state[p];
  const bs = (s.bot && s.bot.state) || "…";
  const stEl = el("state-" + p);
  if (stEl) { stEl.textContent = bs; stEl.className = "state " + bs; }
  const dEl = el("detail-" + p);
  if (dEl) dEl.textContent = (s.bot && s.bot.detail) || "";
  if (p === "nyxify") {
    const live = s.live || {};
    const uEl = el("usage-nyxify");
    if (uEl) uEl.textContent = live.ready ? ("Open AdsPower profiles: " + (live.open || 0)) : "";
  }
  const tiles = el("tiles-" + p); tiles.innerHTML = "";
  cfg.tiles.forEach(k => {
    const d = document.createElement("button"); d.className = "tile"; d.type = "button";
    const on = s.statusFilter === k;
    if (on) d.classList.add("tile-active");
    d.dataset.status = k;
    d.setAttribute("aria-pressed", on ? "true" : "false");
    d.setAttribute("aria-label", `Filter by ${k}: ${(s.counts && s.counts[k]) || 0}`);
    d.innerHTML = `<div class="n">${(s.counts && s.counts[k]) || 0}</div><div class="l">${k}</div>`;
    d.onclick = () => {
      if (s.statusFilter === k) s.statusFilter = "";
      else s.statusFilter = k;
      s.checked.clear();
      renderPanel(p);
    };
    tiles.appendChild(d);
  });
}

function updateProductTable(p, cfg) {
  const s = state[p];
  buildToolbar("queue-" + p, cfg.queue, p, false);

  const searchEl = el("search-" + p);
  if (searchEl) { searchEl.value = s.search || ""; }
  let rows = [...s.rows.values()];
  const q = (s.search || "").toLowerCase().trim();
  if (q) {
    rows = rows.filter(r => {
      const id = String(r.profile_id || r.adspower_id || r.row_key || "").toLowerCase();
      const uname = String(r.username || r.snapchat_username || "").toLowerCase();
      return id.includes(q) || uname.includes(q);
    });
  }
  if (s.statusFilter) {
    const sf = s.statusFilter.toUpperCase();
    rows = rows.filter(r => (r.status || "").toUpperCase() === sf);
  }
  rows = sortRows(rows, s.sort, s.dir);

  const colCount = cfg.columns.length + 1;
  const allChecked = rows.length > 0 && rows.every(r => s.checked.has(keyOf(p, r)));
  const arrow = f => s.sort === f ? (s.dir === 1 ? " &#9650;" : " &#9660;") : "";
  const ths = `<th class="th-cb"><input type="checkbox" id="cb-all-${p}" ${allChecked ? "checked" : ""}></th>`
    + cfg.columns.map(c => {
        if (c.s) return `<th class="th-sortable" data-sort="${c.s}">${c.h}${arrow(c.s)}</th>`;
        return `<th>${c.h}</th>`;
      }).join("");
  el("thead-" + p).innerHTML = "<tr>" + ths + "</tr>";
  const cbAll = el("cb-all-" + p);
  if (cbAll) {
    cbAll.onclick = e => {
      if (e.target.checked) rows.forEach(r => s.checked.add(keyOf(p, r)));
      else s.checked.clear();
      renderPanel(p);
    };
  }

  const tbody = el("tbody-" + p); tbody.innerHTML = "";
  if (!rows.length) {
    tbody.innerHTML = `<tr><td class="empty" colspan="${colCount}">No rows.</td></tr>`;
    buildGroupToolbar(p);
    return;
  }
  rows.forEach(r => {
    const k = keyOf(p, r);
    const tr = document.createElement("tr"); tr.dataset.key = k;
    if (selected[p] === k) tr.classList.add("sel");
    if (s.checked.has(k)) tr.classList.add("checked");
    tr.innerHTML = `<td class="td-cb"><input type="checkbox" class="cb-row" ${s.checked.has(k) ? "checked" : ""}></td>`
      + cfg.columns.map(c => {
          const v = c.f(r);
          return c.badge ? `<td><span class="badge ${escapeAttr(v)}">${escapeHtml(v)}</span></td>`
                         : `<td title="${escapeAttr(v)}">${escapeHtml(v)}</td>`;
        }).join("");
    const cb = tr.querySelector(".cb-row");
    cb.onclick = e => {
      e.stopPropagation();
      if (e.target.checked) s.checked.add(k); else s.checked.delete(k);
      renderPanel(p);
    };
    tr.onclick = e => {
      if (e.target.closest(".td-cb, .cb-row")) return;
      if (window.getSelection && String(window.getSelection()).length > 0) return;
      selected[p] = (selected[p] === k ? null : k);
      renderPanel(p);
    };
    tbody.appendChild(tr);
  });
  buildGroupToolbar(p);
}

function renderProductBody(p) {
  const cfg = PRODUCTS[p], s = state[p];

  // Status card + tiles: cheap, updated only when state/counts actually changed.
  const stSig = productStatusSignature(p, s);
  if (statusRenderSig[p] !== stSig) {
    statusRenderSig[p] = stSig;
    updateProductStatus(p, cfg);
  }

  // Table: rebuilt only when the row set or view state changed — a bot-only
  // SSE update (rows = []) no longer rebuilds the whole table.
  const tblSig = productTableSignature(p, cfg, s);
  const tbodyEl = el("tbody-" + p);
  if (panelRenderSig[p] === tblSig && tbodyEl && tbodyEl.children.length) {
    return;
  }
  panelRenderSig[p] = tblSig;
  updateProductTable(p, cfg);
}

function buildToolbar(id, defs, p, isRow) {
  const bar = el(id); bar.innerHTML = "";
  if (!defs || !defs.length) { bar.style.display = "none"; return; }
  // Row actions are contextual — only take space when a row is selected.
  if (isRow && !selected[p]) { bar.style.display = "none"; return; }
  bar.style.display = "flex";
  let group = null;
  defs.forEach(d => {
    if (d === null) { group = null; return; }
    const [label, path, cls] = d;
    if (!group) { group = document.createElement("div"); group.className = "btn-group"; bar.appendChild(group); }
    const b = document.createElement("button");
    b.className = "btn" + (cls ? (" " + cls) : "");
    b.textContent = label;
    if (isRow) {
      b.disabled = !selected[p];
      b.onclick = () => { const k = selected[p]; if (!k) return; const payload = {}; payload[PRODUCTS[p].key] = k; callAction(p, path, payload); };
    } else {
      b.onclick = () => callAction(p, path, {});
    }
    group.appendChild(b);
  });
}

function buildGroupToolbar(p) {
  const bar = el("group-" + p);
  if (!bar) return;
  const cfg = PRODUCTS[p];
  const defs = cfg.group || [];
  const checked = state[p].checked;
  if (!defs.length || !checked.size) { bar.classList.remove("visible"); return; }
  bar.classList.add("visible");
  bar.innerHTML = `<span class="badge-cb">${checked.size} selected</span>`;
  let group = null;
  defs.forEach(d => {
    if (d === null) { group = null; return; }
    const [label, path, cls] = d;
    if (!group) { group = document.createElement("div"); group.className = "btn-group"; bar.appendChild(group); }
    const b = document.createElement("button");
    b.className = "btn" + (cls ? (" " + cls) : "");
    b.textContent = label;
    b.onclick = () => {
      const keys = [...checked];
      const keyField = cfg.key;
      keys.forEach(k => {
        const payload = { [keyField]: k };
        callAction(p, path, payload);
      });
    };
    group.appendChild(b);
  });
}

function suiteStatusSignature() {
  const s = state.suite;
  return [
    JSON.stringify(suiteBot()),
    JSON.stringify(suiteCounts()),
    JSON.stringify(state.nyxify.usage || {}),
    JSON.stringify(state.nyxify.live || {}),
  ].join("##");
}

function suiteTableSignature() {
  const s = state.suite;
  const rows = suiteRows();
  return [
    s.statusFilter || "",
    (s.search || "").toLowerCase().trim(),
    s.sort || "",
    String(s.dir),
    selected.suite || "",
    [...s.checked].sort().join(","),
    rows.map(r => suiteKey(r) + ":" + SUITE.columns.map(c => String(c.f(r))).join("|")).join("\n"),
  ].join("##");
}

function updateSuiteStatus() {
  const s = state.suite;
  const bot = suiteBot();
  const stEl = el("state-suite");
  if (stEl) { stEl.textContent = bot.state; stEl.className = "state " + bot.state; }
  const dEl = el("detail-suite");
  if (dEl) dEl.textContent = bot.detail;
  const uEl = el("usage-suite");
  const live = state.nyxify.live || {};
  if (uEl) uEl.textContent = live.ready ? ("Open AdsPower profiles: " + (live.open || 0)) : "";

  const counts = suiteCounts();
  const tiles = el("tiles-suite"); tiles.innerHTML = "";
  SUITE.tiles.forEach(k => {
    const d = document.createElement("button"); d.className = "tile"; d.type = "button";
    const on = s.statusFilter === k;
    if (on) d.classList.add("tile-active");
    d.dataset.status = k;
    d.setAttribute("aria-pressed", on ? "true" : "false");
    d.setAttribute("aria-label", `Filter by ${k}: ${counts[k] || 0}`);
    d.innerHTML = `<div class="n">${counts[k] || 0}</div><div class="l">${k}</div>`;
    d.onclick = () => {
      if (s.statusFilter === k) s.statusFilter = "";
      else s.statusFilter = k;
      s.checked.clear();
      renderPanel();
    };
    tiles.appendChild(d);
  });
  renderSuiteRunnerButtons();
}

function updateSuiteTable() {
  const s = state.suite;
  buildSuiteToolbar("queue-suite", SUITE_QUEUE, "queue");

  const searchEl = el("search-suite");
  if (searchEl) { searchEl.value = s.search || ""; }
  let rows = suiteRows();
  const q = (s.search || "").toLowerCase().trim();
  if (q) {
    rows = rows.filter(r => {
      const id = String(r.profile_id || r.adspower_id || r.adspower_profile_id || r.row_key || "").toLowerCase();
      const uname = String(r.username || r.snapchat_username || "").toLowerCase();
      return id.includes(q) || uname.includes(q);
    });
  }
  if (s.statusFilter) {
    const sf = s.statusFilter.toUpperCase();
    rows = rows.filter(r => String(r.status || "").toUpperCase() === sf);
  }
  rows = sortRows(rows, s.sort, s.dir);

  const colCount = SUITE.columns.length + 1;
  const allChecked = rows.length > 0 && rows.every(r => s.checked.has(suiteKey(r)));
  const arrow = f => s.sort === f ? (s.dir === 1 ? " &#9650;" : " &#9660;") : "";
  const ths = `<th class="th-cb"><input type="checkbox" id="cb-all-suite" ${allChecked ? "checked" : ""}></th>`
    + SUITE.columns.map(c => {
        if (c.s) return `<th class="th-sortable" data-sort="${c.s}">${c.h}${arrow(c.s)}</th>`;
        return `<th>${c.h}</th>`;
      }).join("");
  el("thead-suite").innerHTML = "<tr>" + ths + "</tr>";
  const cbAll = el("cb-all-suite");
  if (cbAll) {
    cbAll.onclick = e => {
      if (e.target.checked) rows.forEach(r => s.checked.add(suiteKey(r)));
      else s.checked.clear();
      renderPanel();
    };
  }

  const tbody = el("tbody-suite"); tbody.innerHTML = "";
  if (!rows.length) {
    tbody.innerHTML = `<tr><td class="empty" colspan="${colCount}">No rows.</td></tr>`;
    buildSuiteGroupToolbar();
    return;
  }
  rows.forEach(r => {
    const k = suiteKey(r);
    const tr = document.createElement("tr"); tr.dataset.key = k;
    if (selected.suite === k) tr.classList.add("sel");
    if (s.checked.has(k)) tr.classList.add("checked");
    tr.innerHTML = `<td class="td-cb"><input type="checkbox" class="cb-row" ${s.checked.has(k) ? "checked" : ""}></td>`
      + SUITE.columns.map(c => {
          const v = c.f(r);
          return c.badge ? `<td><span class="badge ${escapeAttr(v)}">${escapeHtml(v)}</span></td>`
                         : `<td title="${escapeAttr(v)}">${escapeHtml(v)}</td>`;
        }).join("");
    const cb = tr.querySelector(".cb-row");
    cb.onclick = e => {
      e.stopPropagation();
      if (e.target.checked) s.checked.add(k); else s.checked.delete(k);
      renderPanel();
    };
    tr.onclick = e => {
      if (e.target.closest(".td-cb, .cb-row")) return;
      // Let the user highlight/copy cell text without the row re-render wiping it.
      if (window.getSelection && String(window.getSelection()).length > 0) return;
      selected.suite = (selected.suite === k ? null : k);
      renderPanel();
    };
    tbody.appendChild(tr);
  });
  buildSuiteGroupToolbar();
}

function renderSuiteBody() {
  // Status card + tiles + runner buttons: cheap, updated on state/counts change.
  const stSig = suiteStatusSignature();
  if (statusRenderSig.suite !== stSig) {
    statusRenderSig.suite = stSig;
    updateSuiteStatus();
  }
  // Table: rebuilt only when the row set or view state changed.
  const tblSig = suiteTableSignature();
  const tbodyEl = el("tbody-suite");
  if (panelRenderSig.suite === tblSig && tbodyEl && tbodyEl.children.length) {
    return;
  }
  panelRenderSig.suite = tblSig;
  updateSuiteTable();
}

function setRunnerTransition(product, action) {
  const s = state[product];
  if (!s) return;
  s.bot = Object.assign({}, s.bot || {}, {
    state: action === "start" ? "starting" : "stopping",
    detail: action === "start" ? `${product} is starting…` : `${product} is stopping…`,
  });
}

function runnerToggle(products, action) {
  products.forEach(p => {
    if (runnerInFlight[p]) return;  // ignore duplicate Start/Stop clicks
    runnerInFlight[p] = true;
    setRunnerTransition(p, action);      // show the request immediately
    renderPanelForRunner(p);
    callAction(p, "/bot/" + action, {}).then(ack => {
      // A failed/unreachable request must not leave the control stuck disabled.
      if (!ack || ack.ok === false) {
        runnerInFlight[p] = false;
        renderPanelForRunner(p);
      }
      // Otherwise the latch is released by settleRunnerInFlight once the SSE
      // stream confirms the real runner state.
    });
    // Safety net: never leave the button disabled forever even if SSE is down.
    setTimeout(() => {
      if (runnerInFlight[p]) { runnerInFlight[p] = false; renderPanelForRunner(p); }
    }, 5000);
  });
}

function renderPanelForRunner(p) {
  // The suite runner buttons live in the global sidebar dock; refresh them, and
  // refresh the currently-visible panel so the status card/tiles reflect the
  // requested transition immediately.
  renderSuiteRunnerButtons();
  if (active === p) renderPanel(p);
  else if (active === "suite") renderPanel("suite");
}

function renderSuiteRunnerButtons() {
  const bar = el("runner-suite");
  if (!bar) return;
  const stateOf = p => String(((state[p].bot || {}).state || "")).toLowerCase();
  const isActive = s => ["running", "paused", "waiting", "blocked"].includes(s);
  bar.innerHTML = "";
  const g = document.createElement("div"); g.className = "btn-group"; bar.appendChild(g);
  [
    { label: "Nyx", products: ["nyx"] },
    { label: "Nyxify", products: ["nyxify"] },
  ].forEach(def => {
    const anyActive = def.products.some(p => isActive(stateOf(p)));
    const inFlight = def.products.some(p => runnerInFlight[p]);
    const b = document.createElement("button");
    b.className = "btn" + (anyActive ? " bad" : " primary");
    b.classList.add("runner-start-stop");
    b.type = "button";
    b.disabled = !!inFlight;
    b.textContent = inFlight ? "Working…" : (anyActive ? `Stop ${def.label}` : `Start ${def.label}`);
    b.title = (inFlight ? "Working…" : (anyActive ? "Stop " : "Start ") + def.label + " runner");
    b.onclick = () => runnerToggle(def.products, anyActive ? "stop" : "start");
    g.appendChild(b);
  });
  bar.style.display = "flex";
}

function buildSuiteToolbar(id, defs, mode) {
  const bar = el(id); if (!bar) return;
  bar.innerHTML = "";
  if (!defs || !defs.length) { bar.style.display = "none"; return; }
  // Row actions are contextual — only take space when a row is selected.
  if (mode === "row" && !selected.suite) { bar.style.display = "none"; return; }
  bar.style.display = "flex";
  let group = null;
  defs.forEach(d => {
    if (d === null) { group = null; return; }
    const [label, product, path, cls] = d;
    if (mode === "row") {
      const prod = selectedSuiteProduct();
      if (product && prod && product !== prod) return;
    }
    if (!group) { group = document.createElement("div"); group.className = "btn-group"; bar.appendChild(group); }
    const b = document.createElement("button");
    b.className = "btn" + (cls ? (" " + cls) : "");
    b.textContent = label;
    if (mode === "queue") {
      b.onclick = () => callAction(product, path, {});
    } else if (mode === "row") {
      const k = selected.suite || "";
      b.disabled = !k;
      b.onclick = () => { if (k) dispatchSuiteAction(d, [k]); };
    }
    group.appendChild(b);
  });
}

function buildSuiteGroupToolbar() {
  const bar = el("group-suite");
  if (!bar) return;
  const checked = state.suite.checked;
  if (!SUITE_GROUP.length || !checked.size) { bar.classList.remove("visible"); return; }
  bar.classList.add("visible");
  bar.innerHTML = `<span class="badge-cb">${checked.size} selected</span>`;
  let group = null;
  SUITE_GROUP.forEach(d => {
    if (d === null) { group = null; return; }
    const [label, , , cls] = d;
    if (!group) { group = document.createElement("div"); group.className = "btn-group"; bar.appendChild(group); }
    const b = document.createElement("button");
    b.className = "btn" + (cls ? (" " + cls) : "");
    b.textContent = label;
    b.onclick = () => dispatchSuiteAction(d, [...checked]);
    group.appendChild(b);
  });
}

const escapeHtml = s => String(s).replace(/[&<>]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
const escapeAttr = s => String(s).replace(/"/g, "&quot;");

let toastTimer = null;
function toast(msg, ok) {
  const t = el("toast"); t.textContent = msg; t.className = "toast " + (ok ? "ok" : "bad");
  void t.offsetWidth;
  t.classList.add("show");
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.classList.remove("show"); }, 4000);
}

function setActive(p) {
  active = p;
  document.querySelectorAll(".tab").forEach(t => t.classList.toggle("active", t.dataset.tab === p));
  document.querySelectorAll(".panel").forEach(s => s.classList.toggle("hidden", s.dataset.product !== p && s.id !== ("panel-" + p)));
  document.body.classList.remove("sidebar-open");
  if (p === "settings") renderSettings();
  if (p === "bitmoji") loadBitmoji();
  if (p === "banned") renderBannedPanel();
  if (p === "nyx" || p === "nyxify") renderPanel(p);
  if (p === "proxyrank") renderProxyRanking();
  if (p === "fullauto") renderFullAuto();
  if (p === "nyxconfig") refreshConfig("nyx").then(renderNyxAdvanced);
  if (p === "nyxifyconfig") refreshConfig("nyxify").then(renderNyxifyAdvanced);
  if (p === "suite") {
    render();
  }
}

function renderBannedPanel() {
  const tbody = el("tbody-banned");
  if (!tbody) return;
  const rows = state.nyxify.bannedRows || [];
  if (!rows.length) {
    tbody.innerHTML = '<tr><td class="empty" colspan="4">No banned rows yet — press "Scan banned" to check the latest SnapBoard snapshot.</td></tr>';
    return;
  }
  tbody.innerHTML = rows.map(r => {
    const id = escapeHtml(String(r.adspower_id || r.adspower_profile_id || r.profile_id || ""));
    const uname = escapeHtml(String(r.username || "").replace(/^\s*snapchat\s*:\s*/i, ""));
    const model = escapeHtml(String(r.model || ""));
    const status = escapeHtml(String(r.status || r.last_step || "banned"));
    return `<tr><td>${id}</td><td>${uname}</td><td>${model}</td><td><span class="badge FAILED">${status}</span></td></tr>`;
  }).join("");
}

el("sidebar-toggle").addEventListener("click", () => document.body.classList.toggle("sidebar-open"));
el("sidebar-backdrop").addEventListener("click", () => document.body.classList.remove("sidebar-open"));

// Sidebar collapse / expand (desktop icon rail). Persists across reloads.
const collapseBtn = el("sidebar-collapse");
if (collapseBtn) {
  const applyMin = (on) => {
    document.body.classList.toggle("sidebar-min", on);
    collapseBtn.setAttribute("aria-label", on ? "Expand sidebar" : "Collapse sidebar");
    collapseBtn.title = on ? "Expand sidebar" : "Collapse sidebar";
  };
  try { applyMin(localStorage.getItem("nyx_sidebar_min") === "1"); } catch (e) {}
  collapseBtn.addEventListener("click", () => {
    const on = !document.body.classList.contains("sidebar-min");
    applyMin(on);
    try { localStorage.setItem("nyx_sidebar_min", on ? "1" : "0"); } catch (e) {}
  });
}

// ---------- Settings panel ----------
async function renderSettings() {
  const autostart = await callBridge("autostart");
  const asEl = el("autostart-toggle");
  if (autostart.ok) asEl.checked = autostart.enabled;

  const tray = await callBridge("tray_icon");
  if (tray.ok) {
    state.bridge.settings.transparent_tray_icon = !!tray.transparent;
  }
  renderTrayIconPreference();

  await refreshConfig("nyx");
  renderAdsPowerModeControls();
  renderUpdatesCard();
  // Refresh rollback targets: local snapshots plus every published version.
  const backups = await callBridge("list_backups");
  state.update.backups = backups.ok ? (backups.backups || []) : [];
  state.update.availableVersions = backups.ok ? (backups.available_versions || []) : [];
  renderUpdatesCard();
}

function renderTrayIconPreference() {
  const toggle = el("tray-transparent-toggle");
  if (toggle) {
    toggle.checked = !!(state.bridge.settings || {}).transparent_tray_icon;
  }
  const feedback = el("tray-transparent-feedback");
  if (feedback && !feedback.dataset.busy) {
    feedback.textContent = toggle && toggle.checked
      ? "Transparent on macOS; click target stays active."
      : "Current visible status dot.";
  }
}

function currentAdsPowerControlMode() {
  const mode = String((state.nyx.config || {}).adspower_control_mode || "auto").trim().toLowerCase();
  return ["auto", "api", "gui"].includes(mode) ? mode : "auto";
}

function renderAdsPowerModeControls() {
  const current = currentAdsPowerControlMode();
  document.querySelectorAll("[data-adspower-mode]").forEach(btn => {
    const active = btn.dataset.adspowerMode === current;
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-pressed", active ? "true" : "false");
  });
  const feedback = el("adspower-mode-feedback");
  if (feedback && !feedback.dataset.busy) {
    const labels = { auto: "Auto: use API first, then GUI when AdsPower blocks the API.", api: "API: Local API only; GUI fallback disabled.", gui: "GUI: use AdsPower desktop automation first on this device." };
    feedback.textContent = labels[current] || labels.auto;
  }
}

async function saveAdsPowerControlMode(mode) {
  const normalized = String(mode || "").trim().toLowerCase();
  if (!["auto", "api", "gui"].includes(normalized)) return;
  const feedback = el("adspower-mode-feedback");
  if (feedback) {
    feedback.dataset.busy = "1";
    feedback.textContent = "Saving AdsPower control mode...";
  }
  const result = await callAction("nyx", "/config", { adspower_control_mode: normalized });
  if (result && result.config) state.nyx.config = result.config;
  renderAdsPowerModeControls();
  if (feedback) {
    feedback.dataset.busy = "";
    feedback.textContent = result && result.ok !== false
      ? "AdsPower control mode saved: " + normalized.toUpperCase() + ". Restart running bots to apply immediately."
      : (result && (result.error || result.message)) || "Could not save AdsPower control mode.";
  }
}

// Reflect state.update into the Updates card: Apply only shows when an update
// is available; Roll Back only shows when a restore point exists.
function renderUpdateActionButtons() {
  const checkBtn = el("update-check-btn");
  const applyBtn = el("update-apply-btn");
  const checkBusy = updateCheckInFlight || updateApplyInFlight;

  if (checkBtn) {
    checkBtn.disabled = checkBusy;
    checkBtn.classList.toggle("is-loading", checkBusy);
    checkBtn.setAttribute("aria-busy", checkBusy ? "true" : "false");
    checkBtn.textContent = updateCheckInFlight ? "Checking…" : updateApplyInFlight ? "Waiting…" : "Check for Update";
  }
  if (applyBtn) {
    applyBtn.disabled = updateApplyInFlight || updateCheckInFlight;
    applyBtn.classList.toggle("is-loading", updateApplyInFlight);
    applyBtn.setAttribute("aria-busy", updateApplyInFlight ? "true" : "false");
    applyBtn.textContent = updateApplyInFlight ? "Applying…" : "Apply Update";
  }
}

function renderUpdatesCard() {
  const u = state.update;
  el("update-current").textContent = u.current || state.nyx.config.version || state.version || "—";
  const latestRow = el("update-latest-row");
  const latestEl = el("update-latest");
  const applyBtn = el("update-apply-btn");
  const notesEl = el("update-release-notes");
  const backupsEl = el("update-backups");
  if (u.available) {
    latestRow.style.display = "flex";
    latestEl.textContent = u.latest_name || u.latest;
    applyBtn.style.display = "";
    if (u.notes) {
      notesEl.style.display = "block";
      notesEl.innerHTML = "<details><summary>Release Notes</summary><pre>" + escapeHtml(u.notes) + "</pre></details>";
    } else {
      notesEl.style.display = "none";
    }
  } else {
    latestRow.style.display = "none";
    applyBtn.style.display = "none";
    notesEl.style.display = "none";
  }
  renderRollbackOptions();
  renderUpdateActionButtons();
}

// Populate the Roll Back picker with every version we can restore: any published
// release plus local-only snapshots (marked "offline" — they restore instantly
// without a download). The installed version is excluded. Falls back to the raw
// backups list if the release list couldn't be fetched.
function renderRollbackOptions() {
  const u = state.update;
  const row = el("update-rollback-row");
  const select = el("update-rollback-select");
  const backupsEl = el("update-backups");
  if (!row || !select) return;

  const current = String(u.current || state.version || "").replace(/^v/i, "");
  let options = Array.isArray(u.availableVersions) ? u.availableVersions.slice() : [];
  if (!options.length && u.backups && u.backups.length) {
    options = u.backups.map((v) => ({ version: String(v).replace(/^v/i, ""), local: true }));
  }
  options = options.filter((o) => o && o.version && o.version !== current);

  if (!options.length) {
    row.style.display = "none";
    if (backupsEl) backupsEl.textContent = "";
    return;
  }

  const prev = select.value;
  select.innerHTML = options.map((o) => {
    const label = "v" + o.version + (o.local ? " (offline)" : "");
    return '<option value="' + escapeHtml(o.version) + '">' + escapeHtml(label) + "</option>";
  }).join("");
  if (prev && options.some((o) => o.version === prev)) select.value = prev;

  row.style.display = "flex";
  const localCount = options.filter((o) => o.local).length;
  if (backupsEl) {
    backupsEl.textContent = localCount
      ? (localCount + " version(s) restore instantly offline; others download on demand.")
      : "Any released version can be downloaded and restored.";
  }
}

// ---------- Update check + indicator ----------
async function runUpdateCheck(showFeedback) {
  if (updateCheckInFlight || updateApplyInFlight) {
    return { ok: false, skipped: true };
  }

  updateCheckInFlight = true;
  renderUpdateActionButtons();
  try {
    const r = await callBridge("check_update");
    const u = state.update;
    u.checked = true;
    if (!r.ok) {
      u.available = false;
      if (r.current) u.current = r.current;
      if (showFeedback) el("update-feedback").textContent = r.message || "Check failed.";
    } else {
      u.current = r.current || u.current;
      u.available = !!r.update_available;
      u.latest = r.latest || "";
      u.latest_name = r.latest_name || "";
      u.notes = r.release_notes || "";
      if (showFeedback) {
        el("update-feedback").textContent = r.update_available
          ? (r.message || `Update ${r.latest} available.`)
          : (r.message || `Up to date (${r.current || "—"}).`);
      }
    }
    refreshUpdateIndicator();
    if (active === "settings") renderUpdatesCard();
    return r;
  } finally {
    updateCheckInFlight = false;
    renderUpdateActionButtons();
  }
}

function refreshUpdateIndicator() {
  const ind = el("update-indicator");
  if (!ind) return;
  if (state.update.available) {
    ind.style.display = "";
    ind.textContent = "update " + (state.update.latest || "ready");
  } else {
    ind.style.display = "none";
  }
}

// ---------- Advanced Config (Nyx section, upper-right) ----------
function renderNyxAdvanced() {
  const body = el("nyx-advanced-body");
  if (!body) return;
  const v = state.nyx.config || {};
  const outfitStyle = v.outfit_style === "mixed" ? "mix" : v.outfit_style;
  const opt = (val, label) => `<option value="${val}" ${outfitStyle === val ? "selected" : ""}>${label}</option>`;
  const mode = currentAdsPowerControlMode();
  const modeOpt = (val, label) => `<option value="${val}" ${mode === val ? "selected" : ""}>${label}</option>`;
  body.innerHTML = `
    <div class="adv-grid">
      <label class="adv-field"><span>Pending threshold</span><input id="cfg-pending_threshold" class="input" value="${escapeAttr(v.pending_threshold || 1)}"></label>
      <label class="adv-field"><span>Max parallel</span><input id="cfg-max_parallel_profiles" class="input" value="${escapeAttr(v.max_parallel_profiles || 5)}"></label>
      <label class="adv-field"><span>Automation speed (%)</span><input id="cfg-automation_speed" class="input" type="number" min="5" max="100" value="${escapeAttr(Math.round((Number(v.automation_speed) || 1) * 50))}"></label>
      <label class="adv-field"><span>Outfit style</span><select id="cfg-outfit_style" class="input">
        ${opt("default", "Default")}${opt("mix", "Mix")}${opt("casual", "Casual")}${opt("sexy", "Sexy")}${opt("custom", "Custom")}
      </select></label>
      <div class="adv-field toggle-row"><span class="toggle-text">Hair randomizer</span><label class="toggle-switch"><input id="cfg-hair_randomizer_enabled" type="checkbox" ${v.hair_randomizer_enabled ? "checked" : ""}><span class="toggle-slider"></span></label></div>

    </div>
    <div class="adv-section-label">AdsPower Local API</div>
    <p class="adv-note">NyxSuite connects to AdsPower automatically — just keep the AdsPower app open and logged in. No API key needed. The fields below are optional, only for a custom host/port or an AdsPower that requires a key.</p>
    <div class="adv-grid">
      <label class="adv-field"><span>Control mode</span><select id="cfg-adspower_control_mode" class="input">
        ${modeOpt("auto", "Auto")}${modeOpt("api", "API only")}${modeOpt("gui", "GUI first")}
      </select></label>
      <label class="adv-field"><span>API key (optional)</span><input id="cfg-adspower_api_key" class="input" type="password" autocomplete="off" placeholder="${v.adspower_api_key_set ? "•••••• (saved — leave blank to keep)" : "optional — only if AdsPower requires one"}"></label>
      <label class="adv-field"><span>Host (optional)</span><input id="cfg-adspower_host" class="input" value="${escapeAttr(v.adspower_host || "")}" placeholder="127.0.0.1"></label>
      <label class="adv-field"><span>Port (optional)</span><input id="cfg-adspower_port" class="input" value="${escapeAttr(v.adspower_port || "")}" placeholder="50325"></label>
    </div>
    <div class="adv-actions">
      <button id="cfg-adspower-test-btn" class="btn" type="button">Test AdsPower connection</button>
      <span id="cfg-adspower-test-result" class="muted"></span>
    </div>
    <button id="cfg-save-btn" class="btn primary" type="button">Save Config</button>
  `;
}

// ---------- Advanced Config (Nyxify section) ----------
function renderNyxifyAdvanced() {
  const body = el("nyxify-advanced-body");
  if (!body) return;
  const v = state.nyxify.config || {};
  const banned = Array.isArray(v.blocked_proxies) ? v.blocked_proxies
    : (Array.isArray(v.banned_proxies) ? v.banned_proxies : []);
  // Pre-fill the warm-up editor with the built-in list when no custom list has
  // been saved yet, so the sites are visible and can be edited/removed.
  const warmupSites = (Array.isArray(v.cookie_warmup_sites) && v.cookie_warmup_sites.length)
    ? v.cookie_warmup_sites
    : (Array.isArray(v.cookie_warmup_sites_default) ? v.cookie_warmup_sites_default : []);
  body.innerHTML = `
    <div class="adv-grid">
      <label class="adv-field"><span>Max parallel</span><input id="ncfg-max_parallel_profiles" class="input" value="${escapeAttr(v.max_parallel_profiles || 1)}"></label>
      <label class="adv-field"><span>Temporary name</span><input id="ncfg-temporary_profile_name" class="input" value="${escapeAttr(v.temporary_profile_name || "")}"></label>
      <label class="adv-field"><span>AdsPower group</span><input id="ncfg-adspower_group" class="input" value="${escapeAttr(v.adspower_group || "")}"></label>
      <label class="adv-field"><span>Extension category</span><input id="ncfg-extension_category" class="input" value="${escapeAttr(v.extension_category || "")}"></label>
      <label class="adv-field"><span>Tag 1</span><input id="ncfg-tag_one" class="input" value="${escapeAttr(v.tag_one || "")}"></label>
      <label class="adv-field"><span>Tag 2</span><input id="ncfg-tag_two" class="input" value="${escapeAttr(v.tag_two || "")}"></label>
      <div class="adv-field toggle-row"><span class="toggle-text">Apply AdsPower tags <span class="muted">(off = no tags on created profiles)</span></span><label class="toggle-switch"><input id="ncfg-adspower_tags_enabled" type="checkbox" ${v.adspower_tags_enabled === true ? "checked" : ""}><span class="toggle-slider"></span></label></div>
      <div class="adv-field toggle-row"><span class="toggle-text">Push AdsPower ID to SnapBoard</span><label class="toggle-switch"><input id="ncfg-push_adspower_id_enabled" type="checkbox" ${v.push_adspower_id_enabled !== false ? "checked" : ""}><span class="toggle-slider"></span></label></div>
      <div class="adv-field toggle-row"><span class="toggle-text">Proxy Blocker</span><label class="toggle-switch"><input id="ncfg-proxy_blocker_enabled" type="checkbox" ${v.proxy_blocker_enabled !== false ? "checked" : ""}><span class="toggle-slider"></span></label></div>
      <div class="adv-field toggle-row"><span class="toggle-text">Proxy Checker <span class="muted">(uses AdsPower check)</span></span><label class="toggle-switch"><input id="ncfg-proxy_checker_enabled" type="checkbox" ${v.proxy_checker_enabled !== false ? "checked" : ""}><span class="toggle-slider"></span></label></div>
      <div class="adv-field toggle-row"><span class="toggle-text">Full Auto Mode</span><label class="toggle-switch"><input id="ncfg-full_auto_mode_enabled" type="checkbox" ${v.full_auto_mode_enabled === true ? "checked" : ""}><span class="toggle-slider"></span></label></div>
      <div class="adv-field toggle-row"><span class="toggle-text">Continuous Mode <span class="muted">(send completed signups to Nyx)</span></span><label class="toggle-switch"><input id="ncfg-continuous_mode_enabled" type="checkbox" ${v.continuous_mode_enabled === true ? "checked" : ""}><span class="toggle-slider"></span></label></div>
      <div class="adv-field toggle-row"><span class="toggle-text">Disable extensions on create <span class="muted">(off = leave extensions on during signup)</span></span><label class="toggle-switch"><input id="ncfg-disable_extensions_enabled" type="checkbox" ${v.disable_extensions_enabled === true ? "checked" : ""}><span class="toggle-slider"></span></label></div>
      <div class="adv-field toggle-row"><span class="toggle-text">Cookie Warm-up <span class="muted">(browse sites before signup)</span></span><label class="toggle-switch"><input id="ncfg-cookie_warmup_enabled" type="checkbox" ${v.cookie_warmup_enabled !== false ? "checked" : ""}><span class="toggle-slider"></span></label></div>
      <div class="adv-field toggle-row"><span class="toggle-text">whox Trust Check <span class="muted">(deep-scan whox.com before warm-up)</span></span><label class="toggle-switch"><input id="ncfg-whox_check_enabled" type="checkbox" ${v.whox_check_enabled !== false ? "checked" : ""}><span class="toggle-slider"></span></label></div>
      <label class="adv-field"><span>whox min trust score <span class="muted">(below = delete + recreate)</span></span><input id="ncfg-whox_min_trust_score" class="input" type="number" min="1" max="100" value="${escapeAttr(v.whox_min_trust_score || 70)}"></label>
      <label class="adv-field"><span>whox URL</span><input id="ncfg-whox_url" class="input" value="${escapeAttr(v.whox_url || "https://whox.com/")}"></label>
    </div>
    <label class="adv-field adv-field-wide"><span>Cookie warm-up sites (one per line — edit or remove; clear all to restore the built-in list)</span><textarea id="ncfg-cookie_warmup_sites" class="input textarea-full" placeholder="https://wikipedia.org/&#10;https://cnn.com/">${escapeHtml(warmupSites.join("\n"))}</textarea></label>
    <label class="adv-field adv-field-wide"><span>Banned proxies (one per line)</span><textarea id="ncfg-banned_proxies" class="input textarea-full">${escapeHtml(banned.join("\n"))}</textarea></label>
    <button id="ncfg-save-btn" class="btn primary" type="button">Save Config</button>
  `;
}

el("autostart-toggle").addEventListener("change", async () => {
  const enabled = el("autostart-toggle").checked;
  const r = await callBridge("set_autostart", { enabled });
  el("autostart-feedback").textContent = r.ok ? (r.message || "Done.") : (r.error || "Failed.");
});

el("tray-transparent-toggle").addEventListener("change", async () => {
  const transparent = el("tray-transparent-toggle").checked;
  const feedback = el("tray-transparent-feedback");
  if (feedback) {
    feedback.dataset.busy = "1";
    feedback.textContent = "Saving menu bar icon setting...";
  }
  const r = await callBridge("set_tray_icon", { transparent });
  if (r.ok) {
    state.bridge.settings.transparent_tray_icon = !!r.transparent;
    el("tray-transparent-toggle").checked = !!r.transparent;
    if (feedback) feedback.textContent = r.message || "Menu bar icon setting saved.";
  } else {
    el("tray-transparent-toggle").checked = !transparent;
    if (feedback) feedback.textContent = r.error || "Could not save menu bar icon setting.";
  }
  if (feedback) feedback.dataset.busy = "";
});

el("clear-cache-logs-btn").addEventListener("click", async () => {
  const button = el("clear-cache-logs-btn");
  const feedback = el("clear-cache-logs-feedback");
  if (!button || button.disabled) return;
  if (!confirm("Clear NyxSuite logs and cached runtime data?")) return;
  button.disabled = true;
  button.textContent = "Clearing…";
  if (feedback) feedback.textContent = "Clearing disposable runtime data…";
  try {
    const result = await callBridge("clear_cache_logs");
    if (feedback) {
      feedback.textContent = result && result.ok !== false
        ? (result.message || "Logs and cache cleared.")
        : (result && result.error) || "Could not clear logs and cache.";
    }
  } finally {
    button.disabled = false;
    button.textContent = "Clear Logs & Cache";
  }
});

el("update-check-btn").addEventListener("click", async () => {
  if (updateCheckInFlight || updateApplyInFlight) return;
  el("update-feedback").textContent = "Checking…";
  await runUpdateCheck(true);
});

el("update-indicator").addEventListener("click", () => setActive("settings"));

el("update-apply-btn").addEventListener("click", async () => {
  if (updateApplyInFlight || updateCheckInFlight) return;
  const target = state.update.latest_name || state.update.latest || "the latest version";
  const preUpdateVersion = state.version;
  if (!confirm("Apply update to " + target + "?\n\nThe app will download, install, and restart. The browser extension is updated too — you'll need to reload it at chrome://extensions afterward.")) return;
  updateApplyInFlight = true;
  renderUpdateActionButtons();
  const fb = el("update-feedback");
  fb.textContent = "Updating… the dashboard will refresh automatically.";
  try {
    const r = await callBridge("apply_update");
    if (!r || !r.ok) {
      fb.textContent = (r && r.message) || "Update failed.";
      updateApplyInFlight = false;
      renderUpdateActionButtons();
      return;
    }

    // Keep Apply disabled until the bridge has restarted or the poll gives up.
    let elapsed = 0;
    const pollInterval = 1500;
    const maxWait = 60000;
    updateApplyPollTimer = setInterval(async () => {
      elapsed += pollInterval;
      try {
        const statusR = await fetch("/bridge/status");
        const status = await statusR.json();
        const newVersion = (status.bridge || {}).version || "";
        if (newVersion && newVersion !== preUpdateVersion) {
          clearInterval(updateApplyPollTimer);
          updateApplyPollTimer = null;
          fb.textContent = "Update applied! Refreshing…";
          // Show extension reload banner
          showExtensionReloadBanner(newVersion);
          setTimeout(() => location.reload(), 800);
          return;
        }
        if (newVersion && newVersion === preUpdateVersion) {
          // Bridge back but same version — also refresh
          clearInterval(updateApplyPollTimer);
          updateApplyPollTimer = null;
          fb.textContent = "Bridge restarted. Refreshing…";
          showExtensionReloadBanner(newVersion);
          setTimeout(() => location.reload(), 800);
          return;
        }
      } catch (e) {
        // Bridge still down — keep waiting
      }
      if (elapsed >= maxWait) {
        clearInterval(updateApplyPollTimer);
        updateApplyPollTimer = null;
        updateApplyInFlight = false;
        renderUpdateActionButtons();
        fb.innerHTML = "The bridge is taking longer than expected to restart."
          + ' <button id="manual-reload-btn" class="btn" type="button">Reload now</button>';
        el("manual-reload-btn").addEventListener("click", () => location.reload());
        showExtensionReloadBanner(preUpdateVersion);
      }
    }, pollInterval);
  } catch (e) {
    updateApplyInFlight = false;
    renderUpdateActionButtons();
    fb.textContent = "Update failed: " + (e && e.message || e);
  }
});

function showExtensionReloadBanner(version) {
  const existing = el("ext-reload-banner");
  if (existing) return;
  const banner = document.createElement("div");
  banner.id = "ext-reload-banner";
  banner.className = "ext-reload-banner";
  banner.innerHTML = `Extension updated to v${escapeHtml(version)} — `
    + `<button id="ext-reload-copy" class="btn btn-ghost" type="button">Copy chrome://extensions</button>`
    + ` and paste it into your address bar, then click the reload ↻ on the Nyx / Nyxify extension.`;
  document.querySelector(".content")?.prepend(banner);
  el("ext-reload-copy")?.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText("chrome://extensions");
      toast("Copied chrome://extensions — paste it in the address bar.", true);
    } catch (e) {
      toast("Could not copy. Manually type chrome://extensions in the address bar.", false);
    }
  });
}

el("update-rollback-btn").addEventListener("click", async () => {
  const select = el("update-rollback-select");
  const version = select ? select.value : "";
  if (!version) { el("update-feedback").textContent = "No version selected to roll back to."; return; }
  const offline = (state.update.availableVersions || []).some((o) => o.version === version && o.local);
  const note = offline
    ? "This restores that version's files and restarts the app."
    : "This version isn't stored offline, so it will be downloaded first, then the app restarts.";
  if (!confirm("Roll back to v" + version + "?\n\n" + note)) return;
  el("update-feedback").textContent = "Rolling back to v" + version + "…";
  const r = await callBridge("rollback", { version });
  el("update-feedback").textContent = r.message || (r.ok ? "Rolling back..." : "Rollback failed.");
});

document.addEventListener("click", async (e) => {
  if (e.target.id === "cfg-save-btn") {
    const speedPct = Math.max(5, Math.min(100, parseInt(el("cfg-automation_speed").value) || 50));
    const cfg = {
      pending_threshold: parseInt(el("cfg-pending_threshold").value) || 1,
      max_parallel_profiles: parseInt(el("cfg-max_parallel_profiles").value) || 5,
      automation_speed: Math.max(0.1, Math.min(2.0, Math.round((speedPct / 50) * 100) / 100)),
      outfit_style: el("cfg-outfit_style").value,
      hair_randomizer_enabled: el("cfg-hair_randomizer_enabled").checked,

      adspower_control_mode: el("cfg-adspower_control_mode") ? el("cfg-adspower_control_mode").value : "auto",
      adspower_host: el("cfg-adspower_host") ? el("cfg-adspower_host").value.trim() : "",
      adspower_port: el("cfg-adspower_port") ? el("cfg-adspower_port").value.trim() : "",
    };
    // Only send the API key when the user typed a new one — a blank masked field
    // means "keep the saved key", so we omit it entirely.
    const apiKeyInput = el("cfg-adspower_api_key");
    const typedKey = apiKeyInput ? apiKeyInput.value.trim() : "";
    if (typedKey) cfg.adspower_api_key = typedKey;
    const r = await callAction("nyx", "/config", cfg);
    if (r && r.config) state.nyx.config = r.config;
    renderAdsPowerModeControls();
    el("config-feedback").textContent = "Config saved.";
  }
  if (e.target && e.target.dataset && e.target.dataset.adspowerMode) {
    await saveAdsPowerControlMode(e.target.dataset.adspowerMode);
  }
  if (e.target.id === "cfg-adspower-test-btn") {
    const out = el("cfg-adspower-test-result");
    if (out) { out.textContent = "Testing…"; out.className = "muted"; }
    // Save first so the test uses any host/port/key just entered.
    if (e.target.dataset.busy === "1") return;
    e.target.dataset.busy = "1";
    try {
      const saveCfg = {
        adspower_host: el("cfg-adspower_host") ? el("cfg-adspower_host").value.trim() : "",
        adspower_port: el("cfg-adspower_port") ? el("cfg-adspower_port").value.trim() : "",
      };
      const apiKeyInput = el("cfg-adspower_api_key");
      const typedKey = apiKeyInput ? apiKeyInput.value.trim() : "";
      if (typedKey) saveCfg.adspower_api_key = typedKey;
      await callAction("nyx", "/config", saveCfg);
      const res = await callBridge("adspower_test");
      if (out) {
        out.textContent = res && res.ok ? "✓ AdsPower Local API is reachable." : (res && res.message) || "AdsPower test failed.";
        out.className = res && res.ok ? "ok-text" : "bad-text";
      }
    } catch (err) {
      if (out) { out.textContent = "Test failed: " + (err && err.message || err); out.className = "bad-text"; }
    } finally {
      e.target.dataset.busy = "";
    }
  }
  if (e.target.id === "ncfg-save-btn") {
    const cfg = {
      max_parallel_profiles: parseInt(el("ncfg-max_parallel_profiles").value) || 1,
      temporary_profile_name: el("ncfg-temporary_profile_name").value,
      adspower_group: el("ncfg-adspower_group").value,
      extension_category: el("ncfg-extension_category").value,
      tag_one: el("ncfg-tag_one").value,
      tag_two: el("ncfg-tag_two").value,
      adspower_tags_enabled: el("ncfg-adspower_tags_enabled").checked,
      push_adspower_id_enabled: el("ncfg-push_adspower_id_enabled").checked,
      proxy_blocker_enabled: el("ncfg-proxy_blocker_enabled").checked,
      proxy_checker_enabled: el("ncfg-proxy_checker_enabled").checked,
      full_auto_mode_enabled: el("ncfg-full_auto_mode_enabled").checked,
      continuous_mode_enabled: el("ncfg-continuous_mode_enabled").checked,
      disable_extensions_enabled: el("ncfg-disable_extensions_enabled").checked,
      cookie_warmup_enabled: el("ncfg-cookie_warmup_enabled").checked,
      cookie_warmup_sites: el("ncfg-cookie_warmup_sites").value.split(/\r?\n/).map(s => s.trim()).filter(Boolean),
      whox_check_enabled: el("ncfg-whox_check_enabled").checked,
      whox_min_trust_score: parseInt(el("ncfg-whox_min_trust_score").value) || 70,
      whox_url: el("ncfg-whox_url").value.trim(),
      banned_proxies: el("ncfg-banned_proxies").value.split(/\r?\n/).map(s => s.trim()).filter(Boolean),
      // This textarea is a deliberate full edit of the banned list, so allow it
      // to REPLACE the stored list (incidental config saves do not set this and
      // therefore can't wipe bans added from the Proxy Ranking "Ban" button).
      blocked_proxies_replace: true,
    };
    const res = await callAction("nyxify", "/config", cfg);
    // Keep local state in sync with what was persisted, so re-opening the panel
    // (or a later save) edits the saved config — not a stale snapshot.
    if (res && res.config) { state.nyxify.config = res.config; renderNyxifyAdvanced(); }
    el("nyxify-config-feedback").textContent = "Config saved.";
  }
});

// ---------- Full Auto editor ----------
let currentModels = [];
let selectedModel = "";

async function renderFullAuto() {
  const r = await fetch(`http://${HOST}:8866/models`, { headers: tokenHeaders() }).then(r => r.json()).catch(() => ({ models: [] }));
  currentModels = r.models || [];
  const sel = el("fullauto-model-select");
  sel.innerHTML = currentModels.map(m => `<option value="${escapeAttr(m)}"${m === selectedModel ? " selected" : ""}>${escapeHtml(m)}</option>`).join("");
  if (!selectedModel && currentModels.length) selectedModel = currentModels[0];
  loadFullAutoModel(selectedModel);
}

async function loadFullAutoModel(model) {
  if (!model) { el("fullauto-usernames").value = ""; el("fullauto-signup-names").value = ""; return; }
  const u = await fetch(`http://${HOST}:8866/usernames?model=${encodeURIComponent(model)}`, { headers: tokenHeaders() }).then(r => r.json()).catch(() => ({}));
  el("fullauto-usernames").value = (u.usernames || []).join("\n");
  const s = await fetch(`http://${HOST}:8866/signup_names?model=${encodeURIComponent(model)}`, { headers: tokenHeaders() }).then(r => r.json()).catch(() => ({}));
  el("fullauto-signup-names").value = (s.signup_names || []).join("\n");
}

el("fullauto-model-select").addEventListener("change", () => {
  selectedModel = el("fullauto-model-select").value;
  loadFullAutoModel(selectedModel);
});

el("fullauto-save-usernames").addEventListener("click", async () => {
  const model = el("fullauto-model-select").value;
  if (!model) return;
  const usernames = el("fullauto-usernames").value.split("\n").map(s => s.trim()).filter(Boolean);
  const r = await fetch(`http://${HOST}:8866/usernames`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...tokenHeaders() },
    body: JSON.stringify({ model, usernames, token: TOKEN }),
  }).then(r => r.json());
  toast(r.ok ? "Usernames saved." : (r.error || "Failed."), r.ok);
});

el("fullauto-save-signup").addEventListener("click", async () => {
  const model = el("fullauto-model-select").value;
  if (!model) return;
  const signup_names = el("fullauto-signup-names").value.split("\n").map(s => s.trim()).filter(Boolean);
  const r = await fetch(`http://${HOST}:8866/signup_names`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...tokenHeaders() },
    body: JSON.stringify({ model, signup_names, token: TOKEN }),
  }).then(r => r.json());
  toast(r.ok ? "Signup names saved." : (r.error || "Failed."), r.ok);
});

el("tabs").addEventListener("click", e => { const b = e.target.closest(".tab"); if (b && b.dataset.tab) setActive(b.dataset.tab); });

function applyTheme(dark) {
  document.body.classList.toggle("dark", dark);
  const label = el("theme-label");
  if (label) label.textContent = dark ? "Light Mode" : "Dark Mode";
}
let themeDark = false;
try { themeDark = localStorage.getItem("nyxsuite-theme") === "dark"; } catch (_) {}
applyTheme(themeDark);
el("theme-toggle").addEventListener("click", () => {
  const dark = !document.body.classList.contains("dark");
  applyTheme(dark);
  try { localStorage.setItem("nyxsuite-theme", dark ? "dark" : "light"); } catch (_) {}
});
el("scan-banned-nyxify").addEventListener("click", scanBannedFromDashboard);
el("remove-banned-nyxify").addEventListener("click", removeBannedFromDashboard);
el("warmup-banned-nyxify").addEventListener("click", warmupBannedFromDashboard);

// Sort on column header click
document.addEventListener("click", e => {
  const th = e.target.closest(".th-sortable");
  if (!th) return;
  const p = th.closest("[data-product]");
  if (!p) return;
  const prod = p.dataset.product;
  const s = state[prod];
  const field = th.dataset.sort;
  if (s.sort === field) s.dir *= -1;
  else { s.sort = field; s.dir = 1; }
  renderPanel(prod);
});

// ---------- Search ----------
["suite", "nyx", "nyxify"].forEach(p => {
  const inp = el("search-" + p);
  if (inp) {
    inp.addEventListener("input", () => {
      state[p].search = inp.value;
      render();
    });
  }
});

// ---------- Install Dependencies ----------
let installPollTimer = null;
el("install-deps-btn").addEventListener("click", async () => {
  const btn = el("install-deps-btn");
  const stateEl = el("install-deps-state");
  const outputEl = el("install-deps-output");
  btn.disabled = true;
  stateEl.textContent = "starting...";
  outputEl.style.display = "none";
  el("install-deps-feedback").textContent = "";
  const r = await fetch("/bridge/install_deps", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...tokenHeaders() },
    body: JSON.stringify({ token: TOKEN }),
  });
  const d = await r.json().catch(() => ({}));
  if (!d.ok) { stateEl.textContent = d.message || "Failed"; btn.disabled = false; return; }
  stateEl.textContent = "running...";
  // Poll for completion
  installPollTimer = setInterval(async () => {
    try {
      const r2 = await fetch("/bridge/install_deps_status", {
        method: "POST",
        headers: { "Content-Type": "application/json", ...tokenHeaders() },
        body: JSON.stringify({ token: TOKEN }),
      });
      const d2 = await r2.json().catch(() => ({}));
      if (d2.state === "running") { stateEl.textContent = "running..."; return; }
      if (installPollTimer) { clearInterval(installPollTimer); installPollTimer = null; }
      btn.disabled = false;
      outputEl.textContent = d2.output || "(no output)";
      outputEl.style.display = "block";
      if (d2.state === "done") {
        stateEl.textContent = "done";
        el("install-deps-feedback").textContent = "Dependencies installed successfully.";
      } else {
        stateEl.textContent = "failed";
        el("install-deps-feedback").textContent = "Some steps failed. Check output above.";
      }
    } catch (e) {
      if (installPollTimer) { clearInterval(installPollTimer); installPollTimer = null; }
      btn.disabled = false;
      stateEl.textContent = "error";
      el("install-deps-feedback").textContent = "Status check failed: " + e;
    }
  }, 1000);
});

// ---------- Proxy Ranking ----------
function proxyScoreClass(score) {
  if (score <= 0.34) return "score-good";
  if (score <= 1.0) return "score-mid";
  return "score-bad";
}

function proxyScoreNumber(row) {
  return Number(row && row.score || 0);
}

function badProxyRows(rows) {
  return (rows || []).filter(row => proxyScoreClass(proxyScoreNumber(row)) === "score-bad" && String(row.subnet || "").trim());
}

function updateProxyRankBulkButton(rows) {
  const btn = el("proxyrank-ban-red");
  if (!btn) return;
  const badRows = badProxyRows(rows);
  btn.disabled = !badRows.length;
  btn.textContent = badRows.length ? `Ban all red proxies (${badRows.length})` : "Ban all red proxies";
}

async function loadProxyRankingRows() {
  try {
    const r = await fetch(`http://${HOST}:8866/proxy_ranking`, { headers: tokenHeaders() })
      .then(r => r.json());
    const rows = (r && r.rows) || [];
    state.nyxify.proxyRankingRows = rows;
    return rows;
  } catch (e) {
    return state.nyxify.proxyRankingRows || [];
  }
}

function renderProxyRankSummary(rows) {
  const host = el("proxyrank-summary");
  if (!host) return;
  const list = rows || [];
  const counts = list.reduce((acc, row) => {
    const cls = proxyScoreClass(proxyScoreNumber(row));
    if (cls === "score-bad") acc.bad += 1;
    else if (cls === "score-mid") acc.mid += 1;
    else acc.good += 1;
    return acc;
  }, { good: 0, mid: 0, bad: 0 });
  host.innerHTML = `
    <span class="proxyrank-chip proxyrank-chip-good"><b>${counts.good}</b> Good</span>
    <span class="proxyrank-chip proxyrank-chip-mid"><b>${counts.mid}</b> Watch</span>
    <span class="proxyrank-chip proxyrank-chip-bad"><b>${counts.bad}</b> Red</span>
  `;
}

// Hand-rolled inline-SVG bar chart (no chart lib - the dashboard is an offline
// SPA). Worst subnets are shown first so problem ranges are visible immediately.
// Snapshot only: the store keeps running counters, not history.
function renderProxyRankChart(rows) {
  const host = el("proxyrank-chart");
  if (!host) return;
  if (!rows || !rows.length) { host.innerHTML = ""; return; }
  const data = rows.slice().sort((a, b) => proxyScoreNumber(b) - proxyScoreNumber(a)).slice(0, 14);
  const W = 720, rowH = 30, top = 12, padL = 112, padR = 74;
  const H = top * 2 + data.length * rowH;
  const barMax = W - padL - padR;
  const maxScore = Math.max(1, ...data.map(d => proxyScoreNumber(d)));
  const bars = data.map((d, i) => {
    const sc = proxyScoreNumber(d);
    const y = top + i * rowH;
    const bw = sc <= 0 ? 3 : Math.max(8, (sc / maxScore) * barMax);
    const cls = proxyScoreClass(sc).replace("score-", "prc-");
    const subnet = escapeHtml(String(d.subnet || ""));
    return `<text class="prc-label" x="0" y="${y + rowH / 2}" dominant-baseline="middle">${subnet}</text>`
      + `<rect class="prc-track" x="${padL}" y="${y + 5}" width="${barMax}" height="${rowH - 10}" rx="4"></rect>`
      + `<rect class="prc-bar ${cls}" x="${padL}" y="${y + 5}" width="${bw.toFixed(1)}" height="${rowH - 10}" rx="4"></rect>`
      + `<text class="prc-val" x="${padL + barMax + 6}" y="${y + rowH / 2}" dominant-baseline="middle">${sc.toFixed(2)}</text>`;
  }).join("");
  const more = rows.length > data.length
    ? `<div class="prc-more">+${rows.length - data.length} more subnets in the table below</div>` : "";
  host.innerHTML = `<div class="prc-cap">Worst subnets by score - higher is worse</div>`
    + `<svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}" role="img" aria-label="Proxy subnet score chart, lower is better">${bars}</svg>${more}`;
}

async function renderProxyRanking() {
  const tbody = el("proxyrank-tbody");
  if (!tbody) return;
  const rows = await loadProxyRankingRows();
  renderProxyRankSummary(rows);
  renderProxyRankChart(rows);
  updateProxyRankBulkButton(rows);
  if (!rows.length) {
    tbody.innerHTML = `<tr><td colspan="7" class="hint" style="text-align:center;padding:14px">No proxy usage recorded yet.</td></tr>`;
    return;
  }
  tbody.innerHTML = rows.map(row => {
    const subnet = escapeHtml(String(row.subnet || ""));
    const sc = proxyScoreNumber(row);
    const cls = proxyScoreClass(sc);
    return `<tr class="${cls}-row">
      <td>${subnet}</td>
      <td>${row.uses || 0}</td>
      <td>${row.retries || 0}</td>
      <td>${row.creation_fails || 0}</td>
      <td>${row.ban_hits || 0}</td>
      <td class="${cls}">${sc.toFixed(2)}</td>
      <td><button class="btn btn-sm proxyrank-ban" data-subnet="${escapeAttr(String(row.subnet || ""))}" type="button">Ban</button></td>
    </tr>`;
  }).join("");
  tbody.querySelectorAll(".proxyrank-ban").forEach(b => {
    b.addEventListener("click", async () => {
      const subnet = b.getAttribute("data-subnet");
      if (!subnet) return;
      b.disabled = true;
      const res = await fetch(`http://${HOST}:8866/proxy_ranking/ban`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...tokenHeaders() },
        body: JSON.stringify({ subnet, token: TOKEN }),
      }).then(r => r.json()).catch(() => ({ ok: false }));
      toast(res.ok ? `Subnet ${subnet} added to Proxy Blocker.` : (res.error || "Ban failed."), res.ok);
      if (res.ok && res.config) {
        state.nyxify.config = res.config;
        if (typeof renderNyxifyAdvanced === "function") renderNyxifyAdvanced();
      }
      renderProxyRanking();
    });
  });
}

async function banBadProxyRows() {
  const btn = el("proxyrank-ban-red");
  const rows = await loadProxyRankingRows();
  const badRows = badProxyRows(rows);
  const subnets = badRows.map(row => String(row.subnet || "").trim()).filter(Boolean);
  if (!subnets.length) {
    toast("No red proxy subnets to ban.", true);
    return;
  }
  if (btn) {
    btn.disabled = true;
    btn.textContent = "Banning red proxies...";
  }
  const res = await fetch(`http://${HOST}:8866/proxy_ranking/ban_many`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...tokenHeaders() },
    body: JSON.stringify({ subnets, token: TOKEN }),
  }).then(r => r.json()).catch(() => ({ ok: false }));
  toast(res.ok ? (res.message || `Banned ${subnets.length} red proxy subnet(s).`) : (res.error || "Bulk ban failed."), res.ok);
  if (res.ok && res.config) {
    state.nyxify.config = res.config;
    if (typeof renderNyxifyAdvanced === "function") renderNyxifyAdvanced();
  }
  renderProxyRanking();
}

el("proxyrank-refresh").addEventListener("click", () => renderProxyRanking());
el("proxyrank-ban-red").addEventListener("click", banBadProxyRows);

  // ---------- Configure Nyxmoji (editor) ----------
const bm = {
  catalog: {}, order: [], outfitOrder: [], groups: {}, models: {}, modelNames: [], presets: {}, renderParams: {},
  baseAvatar: "", current: "", active: "", previewPick: {}, featGroup: {}, openGroups: new Set(), loaded: false,
  optFilter: "", editorMode: "model", outfitConfig: {}, activeOutfit: "", outfitColorTarget: {},
};
const NYX_BASE = PRODUCTS.nyx.base;
const nyxmoji = globalThis.NyxmojiHelpers;
const BM_OUTFIT_FEATURES = ["tops", "bottoms", "dresses", "footwear"];
const BM_HINTS = {
  preset: m => `Using ${m}'s built-in look. Browse every option below — click one to make it a one-item Random pool.`,
  random: () => "Tick every option to allow — the bot picks one at random for each profile. Use Select all / Clear to build the pool fast.",
};

function bmIsOutfit(f) { return (bm.catalog[f] && bm.catalog[f].type) === "outfit"; }
function bmIsOutfitFeature(f) { return BM_OUTFIT_FEATURES.includes(f); }
function bmIsOutfitMode() { return bm.editorMode === "outfit"; }
function bmActiveFeature() { return bmIsOutfitMode() ? bm.activeOutfit : bm.active; }
function bmActiveOrder() { return bmIsOutfitMode() ? bm.outfitOrder : bm.order; }
// Fixed is no longer offered in the UI — a one-option Random pool does the same.
// Convert any legacy Fixed selections to that form so old configs keep working.
function normalizeFixedModes() {
  Object.values(bm.models || {}).forEach(cfg => {
    Object.values(cfg || {}).forEach(sel => {
      if (!sel || sel.mode !== "fixed") return;
      const id = String(sel.id || "");
      const pool = id ? [id] : [];
      sel.mode = "random";
      sel.pool = pool;
      if (sel.color) sel.colors = [sel.color];
      delete sel.id; delete sel.color;
    });
  });
}
function bmDefaultOutfitConfig() {
  return { active_custom_preset_id: "", custom_presets: {} };
}
function ensureOutfitPreset() {
  if (!bm.outfitConfig || typeof bm.outfitConfig !== "object") bm.outfitConfig = bmDefaultOutfitConfig();
  if (!bm.outfitConfig.custom_presets || typeof bm.outfitConfig.custom_presets !== "object") bm.outfitConfig.custom_presets = {};
  let active = bm.outfitConfig.active_custom_preset_id || Object.keys(bm.outfitConfig.custom_presets)[0] || "";
  if (!active || !bm.outfitConfig.custom_presets[active]) {
    active = "default";
    bm.outfitConfig.custom_presets[active] = {
      id: active,
      name: "Custom",
      tuck_in: true,
      features: {},
    };
  }
  bm.outfitConfig.active_custom_preset_id = active;
  const preset = bm.outfitConfig.custom_presets[active];
  if (!preset.features || typeof preset.features !== "object") preset.features = {};
  return preset;
}
function outfitCfg() { return ensureOutfitPreset().features; }
function activeCfg() { return bmIsOutfitMode() ? outfitCfg() : modelCfg(); }
function stripModelOutfitConfig(models) {
  if (!models || typeof models !== "object") return {};
  const dead = ["outfits", ...BM_OUTFIT_FEATURES, "outerwear", "sock"];
  Object.values(models).forEach(cfg => {
    if (!cfg || typeof cfg !== "object") return;
    dead.forEach(feature => delete cfg[feature]);
  });
  return models;
}
function bmColorTarget(feature, sel) {
  const pool = (sel && Array.isArray(sel.pool)) ? sel.pool.map(String) : [];
  const current = String(bm.outfitColorTarget[feature] || "");
  if (current && pool.includes(current)) return current;
  const first = pool[0] || "";
  if (first) bm.outfitColorTarget[feature] = first;
  return first;
}
function bmFeatureColors(feature, sel) {
  const feat = bm.catalog[feature];
  if (!feat || !sel || !bmIsOutfit(feature)) return [];
  if (sel.mode === "fixed") return nyxmoji.verifiedColorsForOption(feat, sel.id);
  if (sel.mode === "random") return nyxmoji.verifiedColorUnion(feat, sel.pool || []);
  return [];
}
function bmPruneFeatureColors(feature, sel) {
  if (!sel || !bmIsOutfit(feature)) return;
  const feat = bm.catalog[feature];
  if (bmIsOutfitMode()) {
    const pool = (sel.pool || []).map(String);
    const colorsByOption = sel.colors_by_option || {};
    const next = {};
    pool.forEach(optionId => {
      if (!nyxmoji.optionColorsAreVerified(feat, optionId)) {
        const kept = Array.isArray(colorsByOption[optionId]) ? colorsByOption[optionId].filter(Boolean) : [];
        if (kept.length) next[optionId] = kept;
        return;
      }
      const valid = nyxmoji.filterConfiguredColors(
        colorsByOption[optionId],
        nyxmoji.verifiedColorsForOption(feat, optionId),
      );
      if (valid.length) next[optionId] = valid;
    });
    if (Object.keys(next).length) sel.colors_by_option = next;
    else delete sel.colors_by_option;
    return;
  }
  const colors = bmFeatureColors(feature, sel);
  if (sel.mode === "fixed") {
    if (!nyxmoji.optionColorsAreVerified(feat, sel.id)) return;
    const valid = nyxmoji.filterConfiguredColors([sel.color], colors);
    if (valid.length) sel.color = valid[0]; else delete sel.color;
  } else if (sel.mode === "random") {
    const pool = sel.pool || [];
    if (!pool.every(optionId => nyxmoji.optionColorsAreVerified(feat, optionId))) return;
    const valid = nyxmoji.filterConfiguredColors(sel.colors, colors);
    if (valid.length) sel.colors = valid; else delete sel.colors;
  }
}
function bmColorsForPickedOption(feature, optionId, configuredColors) {
  const feat = bm.catalog[feature];
  if (!feat || !bmIsOutfit(feature)) return [];
  return nyxmoji.filterConfiguredColors(configuredColors, nyxmoji.verifiedColorsForOption(feat, optionId));
}

async function loadBitmoji() {
  el("bm-status").textContent = "Loading…";
  try {
    const [cat, mod, outfitData] = await Promise.all([
      fetch(NYX_BASE + "/bitmoji/catalog", { headers: tokenHeaders() }).then(r => r.json()),
      fetch(NYX_BASE + "/bitmoji/models", { headers: tokenHeaders() }).then(r => r.json()),
      fetch(NYX_BASE + "/bitmoji/outfits", { headers: tokenHeaders() }).then(r => r.json()).catch(() => ({})),
    ]);
    bm.catalog = (cat && cat.catalog) || {};
    const fullOrder = ((cat && cat.feature_order) || Object.keys(bm.catalog))
      .filter(f => bm.catalog[f] && bm.catalog[f].options && bm.catalog[f].options.length);
    bm.order = fullOrder.filter(f => !bmIsOutfit(f) && f !== "outfits" && f !== "sock");
    bm.outfitOrder = fullOrder.filter(f => bmIsOutfitFeature(f));
    bm.groups = (cat && cat.groups) || {};
    bm.baseAvatar = (cat && cat.base_avatar) || "";
    bm.renderParams = (cat && cat.render_params) || {};
    bm.models = stripModelOutfitConfig((mod && mod.models) || {});
    normalizeFixedModes();
    bm.modelNames = (mod && mod.model_names) || [];
    bm.presets = (mod && mod.model_presets) || {};
    bm.outfitConfig = (outfitData && outfitData.outfits) || bmDefaultOutfitConfig();
    ensureOutfitPreset();
    bm.loaded = true;
    if (!bm.current || !bm.modelNames.includes(bm.current)) bm.current = bm.modelNames[0] || "";
    if (!bm.active || !bm.order.includes(bm.active)) bm.active = bm.order[0] || "";
    if (!bm.activeOutfit || !bm.outfitOrder.includes(bm.activeOutfit)) bm.activeOutfit = bm.outfitOrder[0] || "";
    el("bm-status").textContent = "";
    renderBitmojiModels();
    renderBitmojiAll();
  } catch (e) {
    el("bm-status").textContent = "Could not load Nyxmoji config (is the runner up?).";
  }
}

function renderBitmojiModels() {
  el("bm-model").innerHTML = bm.modelNames.map(m =>
    `<option value="${escapeAttr(m)}"${m === bm.current ? " selected" : ""}>${escapeHtml(m)}</option>`).join("");
  document.querySelectorAll(".bm-editor-mode").forEach(btn => {
    btn.classList.toggle("active", btn.dataset.bmEditor === bm.editorMode);
  });
  const outfitMode = bmIsOutfitMode();
  el("bm-model").style.display = outfitMode ? "none" : "";
  const modelLabel = document.querySelector('label[for="bm-model"]');
  if (modelLabel) modelLabel.style.display = outfitMode ? "none" : "";
  ["bm-outfit-preset", "bm-outfit-add", "bm-outfit-rename", "bm-outfit-delete"].forEach(id => {
    const node = el(id);
    if (node) node.style.display = outfitMode ? "" : "none";
  });
  const tuckWrap = el("bm-outfit-tuck-wrap");
  if (tuckWrap) tuckWrap.style.display = outfitMode ? "" : "none";
  renderOutfitPresetSelect();
  renderOutfitTuckButton();
}

function renderOutfitTuckButton() {
  const input = el("bm-outfit-tuck");
  const label = el("bm-outfit-tuck-label");
  if (!input) return;
  ensureOutfitPreset();
  const tucked = bm.outfitConfig.custom_presets[bm.outfitConfig.active_custom_preset_id].tuck_in !== false;
  input.checked = tucked;
  if (label) label.textContent = tucked ? "Tucked" : "Not Tucked";
}

function renderOutfitPresetSelect() {
  const select = el("bm-outfit-preset");
  if (!select) return;
  ensureOutfitPreset();
  const presets = bm.outfitConfig.custom_presets || {};
  select.innerHTML = Object.keys(presets).map(id => {
    const preset = presets[id] || {};
    return `<option value="${escapeAttr(id)}"${id === bm.outfitConfig.active_custom_preset_id ? " selected" : ""}>${escapeHtml(preset.name || id)}</option>`;
  }).join("");
}

function modelCfg() {
  if (!bm.models[bm.current]) bm.models[bm.current] = {};
  return bm.models[bm.current];
}

function renderBitmojiAll() {
  if (!bm.loaded) return;
  const preset = ensureOutfitPreset();
  el("bm-model-tag").textContent = bmIsOutfitMode() ? `Outfit: ${preset.name || preset.id}` : bm.current;
  renderBitmojiModels();
  renderCatBar();
  renderSide();
  samplePreviewPicks();   // draw a real random sample so the preview isn't just pool[0]
  renderAvatar();
}


function renderCatBar() {
  const cfg = activeCfg();
  const order = bmActiveOrder();
  const grouped = (bm.groups && Object.keys(bm.groups).length) ? bm.groups : {};
  // Build feature→group lookup and store on bm for click handler
  const featGroup = {};
  if (bmIsOutfitMode()) {
    order.forEach(f => { featGroup[f] = "Outfit"; });
  } else {
    Object.keys(grouped).forEach(g => {
      (grouped[g] || []).filter(f => order.includes(f)).forEach(f => { featGroup[f] = g; });
    });
  }
  bm.featGroup = featGroup;
  // Auto-open the active feature's group
  const activeFeature = bmActiveFeature();
  const activeGroup = featGroup[activeFeature];
  if (activeGroup) bm.openGroups.add(activeGroup);
  let currentGroup = "";
  let html = "";
  order.forEach(f => {
    const feat = bm.catalog[f];
    if (!feat) return;
    const g = featGroup[f] || "";
    if (g && g !== currentGroup) {
      if (currentGroup) html += `</div>`;  // close previous group's feature container
      currentGroup = g;
      const open = bm.openGroups.has(g);
      html += `<button type="button" class="bm-cat-group bm-cat-group-btn${open ? " open" : ""}" data-group="${escapeAttr(g)}">`
        + `<span class="bm-group-arrow">${open ? "▾" : "▸"}</span>${escapeHtml(g)}</button>`
        + `<div class="bm-cat-features" style="${open ? "" : "display:none"}">`;
    }
    const configured = !!cfg[f];
    html += `<button type="button" class="bm-cat${f === activeFeature ? " active" : ""}${configured ? " has-config" : ""}" data-feature="${escapeAttr(f)}">
      <span class="bm-cat-dot"></span><span class="bm-cat-name">${escapeHtml(feat.label || f)}</span></button>`;
  });
  if (currentGroup) html += `</div>`;  // close last group
  el("bm-catbar").innerHTML = html;
}

// Resolve the model's baseline (preset) option id for a feature so the preset
// gallery can highlight "the one this model uses now". Colour features store
// the preset as a decimal tone; catalog option ids are #hex — normalise both.
function bmPresetOptionId(f) {
  const rp = bm.renderParams[f];
  const presetParams = bm.presets[bm.current] || {};
  if (!rp || presetParams[rp.param] == null) return null;
  return String(presetParams[rp.param]);
}
function bmOptionMatchesPreset(f, o, presetVal) {
  if (presetVal == null) return false;
  const rp = bm.renderParams[f];
  if (rp && rp.color) {
    const dec = parseInt(String(o.id).replace("#", ""), 16);
    return !isNaN(dec) && String(dec) === presetVal;
  }
  return String(o.id) === presetVal;
}

// One option cell — an image thumbnail, or a colour swatch for "color" features.
function bmOptCellHtml(feat, o, on, extraClass) {
  const cls = `bm-opt${feat.type === "color" ? " bm-opt-color" : ""}${on ? " sel" : ""}${extraClass ? " " + extraClass : ""}`;
  if (feat.type === "color") {
    return `<button type="button" class="${cls}" data-id="${escapeAttr(o.id)}" title="${escapeAttr(o.id)}" style="background:${escapeAttr(o.id)}"></button>`;
  }
  return `<button type="button" class="${cls}" data-id="${escapeAttr(o.id)}" title="${escapeAttr(o.id)}"><img loading="lazy" src="${escapeAttr(o.preview || "")}" alt=""></button>`;
}

function renderSide() {
  const f = bmActiveFeature(); const feat = bm.catalog[f];
  if (!feat) { el("bm-options").innerHTML = ""; renderOptTools(null, "preset"); return; }
  const cfg = activeCfg();
  if (bmIsOutfitMode() && !cfg[f]) cfg[f] = { mode: "random", pool: [] };
  const sel = cfg[f] || { mode: "preset" };
  if (sel.mode === "fixed") {  // legacy configs — Fixed is now a one-item Random pool
    sel.mode = "random";
    sel.pool = [String(sel.id || "")].filter(Boolean);
    if (sel.color) sel.colors = [sel.color];
    delete sel.id; delete sel.color;
  }
  const mode = sel.mode || "preset";
  bmPruneFeatureColors(f, sel);
  el("bm-feature-title").textContent = feat.label || f;
  el("bm-feature-count").textContent = feat.options.length + " options";
  let hintText = bmIsOutfitMode()
    ? "Shared outfit pool. Each profile picks a random item from this pool; colours are configured per selected item."
    : (BM_HINTS[mode] || (() => ""))(bm.current);
  el("bm-mode-hint").textContent = hintText;
  el("bm-clear-feature").style.display = (mode === "preset" && !bmIsOutfitMode()) ? "none" : "";
  el("bm-mode-group").style.display = bmIsOutfitMode() ? "none" : "";
  document.querySelectorAll("#bm-mode-group .bm-mode-btn").forEach(b =>
    b.classList.toggle("active", b.dataset.mode === mode));
  renderOptTools(feat, mode);
  const host = el("bm-options");
  host.className = "bm-options";
  host.innerHTML = renderOptions(f, feat, sel, mode);
  // Selection summary below the grid (skipped in preset — the gallery is the view).
  if (mode === "preset") return;
  const optById = {}; feat.options.forEach(o => { optById[String(o.id)] = o; });
  const thumb = id => {
    const o = optById[String(id)];
    if (feat.type === "color") {
      return `<span class="bm-pool-chip bm-pool-color" title="${escapeAttr(id)}" style="background:${escapeAttr(id)}"></span>`;
    }
    if (o && o.preview) {
      return `<span class="bm-pool-thumb" title="${escapeAttr(id)}"><img loading="lazy" src="${escapeAttr(o.preview)}" alt=""></span>`;
    }
    return `<span class="bm-pool-chip" title="${escapeAttr(id)}">${escapeHtml(id)}</span>`;
  };
  const poolThumbImg = id => {
    const o = optById[String(id)];
    if (o && o.preview) return `<img loading="lazy" src="${escapeAttr(o.preview)}" alt="">`;
    return `<span class="bm-pool-chip" title="${escapeAttr(id)}">${escapeHtml(id)}</span>`;
  };
  let info = `<div class="bm-selection-info">`;
  if (bmIsOutfitMode()) {
    const pool = sel.pool || [];
    const colorTarget = bmColorTarget(f, sel);
    info += `<div class="bm-pool-head">Shared random pool — <strong>${pool.length}</strong> selected</div>`;
    if (pool.length) {
      info += `<div class="bm-pool-grid">`;
      pool.forEach(itemId => {
        const colors = (sel.colors_by_option && Array.isArray(sel.colors_by_option[itemId])) ? sel.colors_by_option[itemId] : [];
        const verified = nyxmoji.filterConfiguredColors(colors, nyxmoji.verifiedColorsForOption(feat, itemId));
        const swatches = verified.length
          ? `<span class="bm-thumb-colors">${verified.map(c => `<i class="bm-thumb-color" title="${escapeAttr(c)}" style="background:${escapeAttr(c)}"></i>`).join("")}</span>`
          : "";
        info += `<span class="bm-pool-thumb" title="${escapeAttr(itemId)}">${poolThumbImg(itemId)}`
          + `<button type="button" class="bm-pool-remove" data-remove-id="${escapeAttr(itemId)}" title="Remove from pool">×</button>`
          + `<span class="bm-thumb-count" title="Colours configured for this item">${verified.length}</span>`
          + `${swatches}</span>`;
      });
      info += `</div>`;
    } else {
      info += `<div class="bm-pool-empty">Tick options above to add them to the shared outfit pool.</div>`;
    }
    if (colorTarget) {
      const selectedColors = (sel.colors_by_option && Array.isArray(sel.colors_by_option[colorTarget])) ? sel.colors_by_option[colorTarget] : [];
      info += `<div class="bm-pool-head">Colours for ${escapeHtml(colorTarget)} — <strong>${selectedColors.length}</strong></div>`;
      info += selectedColors.length
        ? `<div class="bm-pool-grid">${selectedColors.map(c => `<span class="bm-pool-chip bm-pool-color" title="${escapeAttr(c)}" style="background:${escapeAttr(c)}"></span>`).join("")}</div>`
        : `<div class="bm-pool-empty">No colours selected for this item.</div>`;
    }
  } else if (mode === "random") {
    const pool = sel.pool || [];
    info += `<div class="bm-pool-head">Random pool — <strong>${pool.length}</strong> selected</div>`;
    info += pool.length
      ? `<div class="bm-pool-grid">${pool.map(thumb).join("")}</div>`
      : `<div class="bm-pool-empty">Tick options above to add them to the pool.</div>`;
    const selectedColors = nyxmoji.filterConfiguredColors(sel.colors, bmFeatureColors(f, sel));
    if (selectedColors.length) {
      info += `<div class="bm-pool-head">Colors — <strong>${selectedColors.length}</strong></div>`;
      info += `<div class="bm-pool-grid">${selectedColors.map(c => `<span class="bm-pool-chip bm-pool-color" title="${escapeAttr(c)}" style="background:${escapeAttr(c)}"></span>`).join("")}</div>`;
    }
  }
  info += `</div>`;
  host.insertAdjacentHTML("beforeend", info);
}

// The sticky toolbar above the option grid: a filter box (all modes with options)
// plus Select all / Clear / Invert for the Random pool.
function renderOptTools(feat, mode) {
  const tools = el("bm-opt-tools"); if (!tools) return;
  if (!feat || !feat.options || !feat.options.length) { tools.style.display = "none"; return; }
  tools.style.display = "";
  const bulk = mode === "random"
    ? `<div class="bm-opt-bulk">
         <button type="button" class="btn btn-sm" data-bulk="all">Select all</button>
         <button type="button" class="btn btn-sm" data-bulk="clear">Clear</button>
         <button type="button" class="btn btn-sm" data-bulk="invert">Invert</button>
       </div>` : "";
  tools.innerHTML = `<input id="bm-opt-search" class="input bm-opt-search" type="text" placeholder="Filter ${feat.options.length} options by id…" value="${escapeAttr(bm.optFilter || "")}">${bulk}`;
}

function renderOptions(feature, feat, sel, mode) {
  const filter = String(bm.optFilter || "").trim().toLowerCase();
  const presetVal = mode === "preset" && !bmIsOutfitMode() ? bmPresetOptionId(feature) : null;
  const chosen = mode === "random" ? (sel.pool || []) : [];
  const chosenSet = new Set(chosen.map(String));
  const matches = o => !filter || String(o.id).toLowerCase().includes(filter);
  const shown = feat.options.filter(matches);
  let grid = shown.map(o => {
    const on = mode === "preset"
      ? bmOptionMatchesPreset(feature, o, presetVal)
      : chosenSet.has(String(o.id));
    const extra = mode === "preset" ? "bm-opt-preset" : "";
    return bmOptCellHtml(feat, o, on, extra);
  }).join("");
  if (!shown.length) grid = `<div class="bm-pool-empty">No option id matches “${escapeHtml(bm.optFilter || "")}”.</div>`;
  let html = `<div class="bm-opt-grid${feat.type === "color" ? " is-color" : ""}">${grid}</div>`;
  // Garment colours are catalogued per option. A verified empty list is an
  // intentional colourless garment, so it has no colour controls at all.
  if (feat.type === "outfit" && mode !== "preset") {
    const colorTarget = bmIsOutfitMode() ? bmColorTarget(feature, sel) : "";
    const colors = bmIsOutfitMode()
      ? (colorTarget ? nyxmoji.verifiedColorsForOption(feat, colorTarget) : [])
      : bmFeatureColors(feature, sel);
    const configuredColors = bmIsOutfitMode()
      ? ((sel.colors_by_option && colorTarget) ? sel.colors_by_option[colorTarget] : [])
      : sel.colors;
    const selectedColors = nyxmoji.filterConfiguredColors(configuredColors, colors);
    const poolColors = new Set(selectedColors.map(color => color.toLowerCase()));
    const label = bmIsOutfitMode() ? `Colours for ${colorTarget || "selected item"}` : "Colours (pool)";
    if (colors.length) {
      html += `<div class="bm-color-block"><div class="bm-color-head"><span class="bm-color-label">${label}</span>`;
      if (mode === "random") {
        html += `<span class="bm-color-actions"><button type="button" class="btn btn-sm bm-color-all">All</button><button type="button" class="btn btn-sm bm-color-none">Clear</button></span>`;
      }
      html += `</div><div class="bm-color-grid">`;
      html += colors.map(c => {
        const on = poolColors.has(c.toLowerCase());
        return `<button type="button" class="bm-opt bm-opt-color bm-opt-swatch${on ? " sel" : ""}" data-color="${escapeAttr(c)}" title="${escapeAttr(c)}" aria-label="Colour ${escapeAttr(c)}" style="background:${escapeAttr(c)}"></button>`;
      }).join("");
      html += `</div></div>`;
    }
  }
  return html;
}

const bmRandOf = arr => arr[Math.floor(Math.random() * arr.length)];

// Conservative sampler used on load / re-render: only re-rolls features the
// operator set to Random, drawing from THAT pool. Preset features keep
// the model's chosen/baseline look, so opening a model shows its clean preset.
function samplePreviewPicks() {
  const cfg = activeCfg();
  bmActiveOrder().forEach(f => {
    const rp = bm.renderParams[f];
    const feat = bm.catalog[f];
    if (!rp || !feat || !feat.options || !feat.options.length) return;
    const sel = cfg[f];
    if (!sel || sel.mode !== "random") { delete bm.previewPick[f]; delete bm.previewPick[f + ":color"]; return; }
    const pool = (sel.pool || []).map(String).filter(id => feat.options.some(o => String(o.id) === id));
    if (!pool.length) { delete bm.previewPick[f]; return; }
    const optionId = bmRandOf(pool);
    bm.previewPick[f] = optionId;
    const colors = bmIsOutfitMode()
      ? nyxmoji.filterConfiguredColors(
        (sel.colors_by_option && sel.colors_by_option[optionId]) || [],
        nyxmoji.verifiedColorsForOption(feat, optionId),
      )
      : bmColorsForPickedOption(f, optionId, sel.colors);
    if (colors.length) bm.previewPick[f + ":color"] = bmRandOf(colors);
    else delete bm.previewPick[f + ":color"];
  });
}

function bmRenderValue(feature, id) {
  const rp = bm.renderParams[feature];
  if (!rp) return null;
  if (rp.color) { const n = parseInt(String(id).replace("#", ""), 16); return isNaN(n) ? null : String(n); }
  return String(id);
}

function buildBaseAvatarUrl() {
  // The model's baseline look with NO per-feature overrides — always a valid
  // render, used as the preview's last-resort fallback so the stage never blanks.
  if (!bm.baseAvatar) return "";
  let url; try { url = new URL(bm.baseAvatar); } catch (e) { return ""; }
  const p = url.searchParams;
  p.set("scale", "1");
  const preset = bm.presets[bm.current] || {};
  Object.keys(preset).forEach(k => p.set(k, preset[k]));
  return url.toString();
}

function buildAvatarUrl() {
  if (!bm.baseAvatar) return "";
  let url; try { url = new URL(bm.baseAvatar); } catch (e) { return ""; }
  const p = url.searchParams;
  p.set("scale", "1");  // lighter render — preview re-fetches on every change
  const preset = bm.presets[bm.current] || {};
  Object.keys(preset).forEach(k => p.set(k, preset[k]));
  const cfg = activeCfg();
  const overrides = {};
  bmActiveOrder().forEach(f => {
    const rp = bm.renderParams[f]; const feat = bm.catalog[f]; const sel = cfg[f];
    if (!rp || !feat) return;
    let id = null, color = null;
    if (sel && sel.mode === "fixed") {
      id = sel.id;
      color = bmColorsForPickedOption(f, id, [sel.color])[0] || null;
    } else if (sel && sel.mode === "random") {
      const pool = (sel.pool || []).map(String).filter(optionId => nyxmoji.findOption(feat, optionId));
      if (pool.length) id = (bm.previewPick[f] && pool.includes(String(bm.previewPick[f]))) ? bm.previewPick[f] : pool[0];
      const colors = bmIsOutfitMode()
        ? nyxmoji.filterConfiguredColors(
          (sel.colors_by_option && sel.colors_by_option[id]) || [],
          nyxmoji.verifiedColorsForOption(feat, id),
        )
        : bmColorsForPickedOption(f, id, sel.colors);
      if (colors.length) color = (bm.previewPick[f + ":color"] && nyxmoji.filterConfiguredColors([bm.previewPick[f + ":color"]], colors).length)
        ? bm.previewPick[f + ":color"] : colors[0];
    } else if (bm.previewPick[f]) {
      // Preset/unconfigured feature rolled for preview — render that pick so the
      // preview reflects it. Cleared on model change / re-render.
      id = bm.previewPick[f];
      color = bmColorsForPickedOption(f, id, [bm.previewPick[f + ":color"]])[0] || null;
    }
    // Otherwise preset/unconfigured features inherit the model preset applied above.
    if (id) {
      const option = nyxmoji.findOption(feat, id);
      if (option && option.render && typeof option.render === "object") {
        Object.assign(overrides, nyxmoji.resolveOptionRender(feat, id, color));
      } else {
        // Older non-garment records can still render their feature id, but a
        // missing live render record never creates a made-up colour parameter.
        const value = bmRenderValue(f, id);
        if (value != null) overrides[rp.param] = value;
      }
    }
  });
  // Show the tuck state on the preview too: the renderer tucks the top in when
  // is_tucked=1, matching what the flow will do for a tuck_in preset.
  if (bmIsOutfitMode() && ensureOutfitPreset().tuck_in !== false) overrides.is_tucked = "1";
  return nyxmoji.applyAvatarParams(url.toString(), overrides) || url.toString();
}

function renderAvatar() {
  const img = el("bm-avatar");
  const note = el("bm-avatar-err");
  const u = buildAvatarUrl();
  if (!u) return;
  img.onload = () => { img.style.display = ""; if (note) note.style.display = "none"; bm.lastGoodSrc = u; };
  img.onerror = () => {
    // Never blank the stage: fall back to the last good preview, else the model
    // base look. A rare invalid param combo shows the base instead of nothing.
    const fallback = bm.lastGoodSrc || buildBaseAvatarUrl();
    img.onerror = null;  // don't loop if the fallback also fails
    if (fallback && img.src !== fallback) {
      if (note) { note.textContent = "Showing base look — that combination couldn't render."; note.style.display = ""; }
      img.style.display = "";
      img.src = fallback;
    }
  };
  img.style.display = "";
  img.src = u;
}

el("bm-model").addEventListener("change", e => { bm.current = e.target.value; bm.previewPick = {}; renderBitmojiAll(); });
el("bm-outfit-preset").addEventListener("change", e => {
  ensureOutfitPreset();
  const id = e.target.value;
  if (bm.outfitConfig.custom_presets[id]) bm.outfitConfig.active_custom_preset_id = id;
  bm.previewPick = {};
  renderBitmojiAll();
});

document.querySelectorAll(".bm-editor-mode").forEach(btn => {
  btn.addEventListener("click", () => {
    bm.editorMode = btn.dataset.bmEditor === "outfit" ? "outfit" : "model";
    bm.optFilter = "";
    bm.previewPick = {};
    bm.openGroups.clear();
    if (bmIsOutfitMode()) ensureOutfitPreset();
    renderBitmojiAll();
  });
});

function uniqueOutfitPresetId(name) {
  const base = String(name || "custom").trim().toLowerCase().replace(/[^a-z0-9_-]+/g, "_").replace(/^_+|_+$/g, "") || "custom";
  const presets = (bm.outfitConfig && bm.outfitConfig.custom_presets) || {};
  let id = base;
  let i = 2;
  while (presets[id]) {
    id = `${base}_${i}`;
    i += 1;
  }
  return id;
}

el("bm-outfit-add").addEventListener("click", () => {
  ensureOutfitPreset();
  const name = (prompt("Name this custom outfit preset:", "Custom Outfit") || "").trim();
  if (!name) return;
  const id = uniqueOutfitPresetId(name);
  bm.outfitConfig.custom_presets[id] = { id, name, tuck_in: true, features: {} };
  bm.outfitConfig.active_custom_preset_id = id;
  bm.previewPick = {};
  renderBitmojiAll();
});

el("bm-outfit-rename").addEventListener("click", () => {
  const preset = ensureOutfitPreset();
  const name = (prompt("Rename this custom outfit preset:", preset.name || preset.id) || "").trim();
  if (!name) return;
  preset.name = name;
  renderBitmojiAll();
});

el("bm-outfit-delete").addEventListener("click", () => {
  ensureOutfitPreset();
  const presets = bm.outfitConfig.custom_presets || {};
  const activeId = bm.outfitConfig.active_custom_preset_id;
  if (Object.keys(presets).length <= 1) {
    toast("Keep at least one custom outfit preset.", false);
    return;
  }
  if (!confirm("Delete this custom outfit preset?")) return;
  delete presets[activeId];
  bm.outfitConfig.active_custom_preset_id = Object.keys(presets)[0] || "";
  bm.previewPick = {};
  renderBitmojiAll();
});

el("bm-outfit-tuck").addEventListener("change", () => {
  const preset = ensureOutfitPreset();
  preset.tuck_in = el("bm-outfit-tuck").checked;
  renderOutfitTuckButton();
  renderAvatar();
  toast(preset.tuck_in ? "This outfit will tuck the top in." : "This outfit will not tuck the top in.", true);
});

el("bm-catbar").addEventListener("click", e => {
  const groupBtn = e.target.closest(".bm-cat-group-btn");
  if (groupBtn) {
    const g = groupBtn.dataset.group;
    if (bm.openGroups.has(g)) bm.openGroups.delete(g);
    else { bm.openGroups.clear(); bm.openGroups.add(g); }  // accordion: close others
    renderCatBar();
    return;
  }
  const cat = e.target.closest(".bm-cat"); if (!cat) return;
  if (bmIsOutfitMode()) bm.activeOutfit = cat.dataset.feature;
  else bm.active = cat.dataset.feature;
  bm.optFilter = "";   // a filter for one feature shouldn't leak into the next
  const g = bm.featGroup[bmActiveFeature()];
  if (g) { bm.openGroups.clear(); bm.openGroups.add(g); }
  renderCatBar(); renderSide();
});

document.getElementById("bm-mode-group").addEventListener("click", e => {
  const btn = e.target.closest(".bm-mode-btn"); if (!btn) return;
  if (bmIsOutfitMode()) return;
  const mode = btn.dataset.mode; const cfg = activeCfg(); const f = bmActiveFeature();
  if (mode === "preset") delete cfg[f];
  else if (mode === "fixed") cfg[f] = { mode: "random", pool: (cfg[f] && cfg[f].id) ? [String(cfg[f].id)] : [] };
  else cfg[f] = { mode: "random", pool: (cfg[f] && cfg[f].pool) || [] };
  bmPruneFeatureColors(f, cfg[f]);
  renderCatBar(); renderSide(); renderAvatar();
});

el("bm-options").addEventListener("click", e => {
  const poolRemove = e.target.closest(".bm-pool-remove");
  if (poolRemove) {
    const f = bmActiveFeature(); const sel = activeCfg()[f];
    if (!sel || !bmIsOutfitMode()) return;
    const itemId = poolRemove.dataset.removeId;
    sel.pool = (sel.pool || []).filter(optionId => String(optionId) !== String(itemId));
    if (sel.colors_by_option) delete sel.colors_by_option[itemId];
    if (String(bm.outfitColorTarget[f] || "") === String(itemId)) delete bm.outfitColorTarget[f];
    if (bm.previewPick[f] === itemId) { delete bm.previewPick[f]; delete bm.previewPick[f + ":color"]; }
    bmPruneFeatureColors(f, sel);
    renderSide(); renderAvatar();
    return;
  }
  const poolThumbClick = e.target.closest(".bm-pool-thumb");
  if (poolThumbClick && bmIsOutfitMode()) {
    const f = bmActiveFeature(); const sel = activeCfg()[f];
    if (!sel) return;
    const itemId = poolThumbClick.getAttribute("title");
    const inPool = (sel.pool || []).some(optionId => String(optionId) === String(itemId));
    if (!inPool) return;
    bm.outfitColorTarget[f] = itemId;
    renderSide();
    return;
  }
  const colorBulk = e.target.closest(".bm-color-all, .bm-color-none");
  if (colorBulk) {
    const f = bmActiveFeature(); const sel = activeCfg()[f];
    if (!sel || sel.mode !== "random") return;
    const targetId = bmIsOutfitMode() ? bmColorTarget(f, sel) : "";
    const colors = bmIsOutfitMode()
      ? (targetId ? nyxmoji.verifiedColorsForOption(bm.catalog[f], targetId) : [])
      : bmFeatureColors(f, sel);
    if (!colors.length) return;
    if (bmIsOutfitMode()) {
      sel.colors_by_option = sel.colors_by_option || {};
      sel.colors_by_option[targetId] = colorBulk.classList.contains("bm-color-all") ? colors.slice() : [];
      if (!sel.colors_by_option[targetId].length) delete sel.colors_by_option[targetId];
    } else {
      sel.colors = colorBulk.classList.contains("bm-color-all") ? colors.slice() : [];
    }
    bmPruneFeatureColors(f, sel);
    renderSide(); renderAvatar();
    return;
  }
  const swatchBtn = e.target.closest(".bm-opt-swatch");
  if (swatchBtn) {
    if (swatchBtn.disabled) return;   // preset shows the palette read-only
    const f = bmActiveFeature(); const color = swatchBtn.dataset.color; const sel = activeCfg()[f];
    if (!sel) return;
    const targetId = bmIsOutfitMode() ? bmColorTarget(f, sel) : "";
    const available = bmIsOutfitMode()
      ? (targetId ? nyxmoji.verifiedColorsForOption(bm.catalog[f], targetId) : [])
      : bmFeatureColors(f, sel);
    const selected = nyxmoji.filterConfiguredColors([color], available)[0];
    if (!selected) return;
    if (bmIsOutfitMode()) {
      sel.colors_by_option = sel.colors_by_option || {};
      const list = sel.colors_by_option[targetId] || [];
      const ci = list.findIndex(c => String(c).toLowerCase() === selected.toLowerCase());
      if (ci >= 0) list.splice(ci, 1); else list.push(selected);
      if (list.length) sel.colors_by_option[targetId] = list;
      else delete sel.colors_by_option[targetId];
    } else if (sel.mode === "random") {
      sel.colors = sel.colors || [];
      const ci = sel.colors.findIndex(c => String(c).toLowerCase() === selected.toLowerCase());
      if (ci >= 0) sel.colors.splice(ci, 1); else sel.colors.push(selected);
    }
    bmPruneFeatureColors(f, sel);
    renderSide(); renderAvatar();
    return;
  }
  const optBtn = e.target.closest(".bm-opt"); if (!optBtn || optBtn.classList.contains("bm-opt-swatch")) return;
  const f = bmActiveFeature(); const id = optBtn.dataset.id; const cfg = activeCfg(); let sel = cfg[f];
  if (bmIsOutfitMode()) {
    if (!sel) sel = cfg[f] = { mode: "random", pool: [] };
    sel.pool = sel.pool || [];
    const i = sel.pool.findIndex(optionId => String(optionId) === String(id));
    if (i >= 0) sel.pool.splice(i, 1); else sel.pool.push(id);
    bm.outfitColorTarget[f] = id;
    if (sel.colors_by_option && !sel.pool.includes(id)) delete sel.colors_by_option[id];
    bm.previewPick[f] = id;
    bmPruneFeatureColors(f, sel);
    renderCatBar(); renderSide(); renderAvatar();
    return;
  }
  if (!sel) {
    // Preset gallery: clicking any option sets a one-item Random pool — the
    // fast path from "just browsing" to "use this exact one".
    cfg[f] = { mode: "random", pool: [String(id)] };
    bmPruneFeatureColors(f, cfg[f]);
    renderCatBar(); renderSide(); renderAvatar();
    return;
  }
  if (sel.mode === "random") {
    sel.pool = sel.pool || [];
    const i = sel.pool.findIndex(optionId => String(optionId) === String(id));
    if (i >= 0) sel.pool.splice(i, 1); else sel.pool.push(id);
    bm.previewPick[f] = id;
    bmPruneFeatureColors(f, sel);
    renderCatBar(); renderSide();
  }
  renderAvatar();
});

// Filter box + Select all / Clear / Invert for the option grid.
el("bm-opt-tools").addEventListener("input", e => {
  if (e.target.id !== "bm-opt-search") return;
  bm.optFilter = e.target.value;
  const f = bmActiveFeature(); const feat = bm.catalog[f]; const sel = activeCfg()[f] || { mode: "preset" };
  const host = el("bm-options");
  const info = host.querySelector(".bm-selection-info");
  host.innerHTML = renderOptions(f, feat, sel, sel.mode || "preset");
  if (info) host.appendChild(info);   // keep the selection summary in place
});
el("bm-opt-tools").addEventListener("click", e => {
  const btn = e.target.closest("[data-bulk]"); if (!btn) return;
  const f = bmActiveFeature(); const feat = bm.catalog[f]; const sel = activeCfg()[f];
  if (!feat || !sel || sel.mode !== "random") return;
  const filter = String(bm.optFilter || "").trim().toLowerCase();
  const shownIds = feat.options.filter(o => !filter || String(o.id).toLowerCase().includes(filter)).map(o => String(o.id));
  const kind = btn.dataset.bulk;
  const cur = new Set((sel.pool || []).map(String));
  if (kind === "all") shownIds.forEach(id => cur.add(id));
  else if (kind === "clear") shownIds.forEach(id => cur.delete(id));
  else if (kind === "invert") shownIds.forEach(id => cur.has(id) ? cur.delete(id) : cur.add(id));
  sel.pool = Array.from(cur);
  if (bmIsOutfitMode() && sel.colors_by_option) {
    Object.keys(sel.colors_by_option).forEach(optionId => {
      if (!cur.has(String(optionId))) delete sel.colors_by_option[optionId];
    });
  }
  bmPruneFeatureColors(f, sel);
  renderCatBar(); renderSide(); renderAvatar();
});

el("bm-clear-feature").addEventListener("click", () => {
  const f = bmActiveFeature();
  delete activeCfg()[f];
  if (bmIsOutfitMode()) activeCfg()[f] = { mode: "random", pool: [] };
  delete bm.previewPick[f]; delete bm.previewPick[f + ":color"];
  renderCatBar(); renderSide(); renderAvatar();
});


el("bm-save").addEventListener("click", async () => {
  el("bm-status").textContent = "Saving…";
  try {
    ensureOutfitPreset();
    normalizeFixedModes();
    const [modelsRes, outfitsRes] = await Promise.all([
      fetch(NYX_BASE + "/bitmoji/models", {
        method: "POST",
        headers: { "Content-Type": "application/json", ...tokenHeaders() },
        body: JSON.stringify({ models: stripModelOutfitConfig(bm.models), token: TOKEN }),
      }),
      fetch(NYX_BASE + "/bitmoji/outfits", {
        method: "POST",
        headers: { "Content-Type": "application/json", ...tokenHeaders() },
        body: JSON.stringify({ outfits: bm.outfitConfig, token: TOKEN }),
      }),
    ]);
    const d = await modelsRes.json().catch(() => ({}));
    const outfitD = await outfitsRes.json().catch(() => ({}));
    if (d.ok && outfitD.ok) {
      bm.models = d.models || bm.models;
      bm.outfitConfig = outfitD.outfits || bm.outfitConfig;
      ensureOutfitPreset();
      el("bm-status").textContent = "Saved.";
      toast("Nyxmoji config saved.", true);
      renderBitmojiAll();
    }
    else {
      const error = d.error || outfitD.error || "Save failed.";
      el("bm-status").textContent = error;
      toast(error, false);
    }
  } catch (e) { el("bm-status").textContent = "Save failed: " + e; toast("Save failed.", false); }
});

el("bm-export").addEventListener("click", () => {
  ensureOutfitPreset();
  const payload = {
    version: 2,
    models: bm.models,
    outfits: bm.outfitConfig,
  };
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "nyxmoji_config.json";
  a.click();
  URL.revokeObjectURL(a.href);
  toast("Exported nyxmoji_config.json", true);
});

el("bm-import").addEventListener("click", () => el("bm-import-file").click());
el("bm-import-file").addEventListener("change", e => {
  const file = e.target.files && e.target.files[0]; if (!file) return;
  const reader = new FileReader();
  reader.onload = () => {
    try {
      const parsed = JSON.parse(reader.result);
      if (!parsed || typeof parsed !== "object") throw new Error("not an object");
      if (parsed.models || parsed.outfits) {
        if (parsed.models && typeof parsed.models === "object") { bm.models = parsed.models; normalizeFixedModes(); }
        if (parsed.outfits && typeof parsed.outfits === "object") bm.outfitConfig = parsed.outfits;
      } else {
        bm.models = parsed;
        normalizeFixedModes();
      }
      ensureOutfitPreset();
      bm.previewPick = {};
      renderBitmojiModels(); renderBitmojiAll();
      el("bm-status").textContent = "Imported — review and click Save to apply.";
      toast("Config imported. Click Save to apply.", true);
    } catch (err) { toast("Import failed: invalid JSON.", false); }
  };
  reader.readAsText(file);
  e.target.value = "";
});

connect();
render();
// Auto-check for updates on launch so the indicator lights up without the user
// having to open Settings. Best-effort — silent when offline or unconfigured.
setTimeout(() => { runUpdateCheck(false).catch(() => {}); }, 1200);

// Deep link: the extension's "Setup & Install" button opens the dashboard at
// #setup — jump to Settings and highlight the Setup & Install card.
function handleHashRoute() {
  // Legacy #nyx / #nyxify deep links land on the merged suite table.
  if (location.hash === "#nyx" || location.hash === "#nyxify" || location.hash === "#suite") { setActive("suite"); return; }
  if (location.hash === "#setup") {
    setActive("settings");
    setTimeout(() => {
      const card = el("card-setup");
      if (card) {
        card.scrollIntoView({ behavior: "smooth", block: "center" });
        card.classList.add("flash");
        setTimeout(() => card.classList.remove("flash"), 1800);
      }
    }, 120);
  }
}
window.addEventListener("hashchange", handleHashRoute);
handleHashRoute();
