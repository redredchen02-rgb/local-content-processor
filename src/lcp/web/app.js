/*
 * GUI renderer. HARD INVARIANT (R41, redline 3):
 *   - render ONLY via textContent / createElement / setAttribute,
 *   - never the markup-injecting DOM sink, never template-literal HTML, no eval,
 *   - source links are rendered as INERT text (never a live link, never fetched).
 *
 * Data from window.pywebview.api.* is ALREADY sanitized server-side. We still
 * only ever assign it to textContent — defence in depth behind the strict CSP.
 *
 * Three-view shell (INBOX / JOB / SETUP), state-gated actions (STATE_ACTIONS,
 * lex.js), machine->human text (LEX, lex.js). No router/framework/build step:
 * views switch via the native `hidden` attribute. lex.js loads first and exposes
 * the globals LEX and STATE_ACTIONS.
 *
 * P1: long crawl/process calls run via the *_async bridge + job_status polling
 * so the window never freezes (G1). Terminal/irreversible actions use in-DOM
 * confirm trays (no alert/confirm/prompt). Spinner is CSS-only with a
 * reduced-motion textContent fallback.
 */
"use strict";

// --- primitives (single textContent choke point) ---------------------------

function api() {
  return (window.pywebview && window.pywebview.api) || null;
}
function $(id) {
  return document.getElementById(id);
}
function setText(node, value) {
  node.textContent = value == null ? "" : String(value);
}
function el(tag, text) {
  const node = document.createElement(tag);
  if (text !== undefined) setText(node, text);
  return node;
}
function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}
function button(label, cls) {
  const b = el("button", label);
  b.setAttribute("type", "button");
  if (cls) b.className = cls;
  return b;
}
function textInput(placeholder) {
  const i = el("input");
  i.type = "text";
  if (placeholder) i.setAttribute("placeholder", placeholder);
  return i;
}
function checkbox() {
  const c = el("input");
  c.type = "checkbox";
  return c;
}
function labeled(label, node) {
  const wrap = el("label");
  wrap.appendChild(el("span", label));
  wrap.appendChild(node);
  return wrap;
}
function setBusy(btn, on) {
  if (on) btn.setAttribute("disabled", "");
  else btn.removeAttribute("disabled");
}

// --- lex lookups (pure; unknown enum/code -> explicit fallback) -------------

function lexState(state) {
  return (LEX.state && LEX.state[state]) || LEX.fallback.state;
}
function lexReason(reason) {
  return (LEX.reason && LEX.reason[reason]) || null;
}
function lexExit(code) {
  return (LEX.exit && LEX.exit[String(code)]) || LEX.fallback.exit;
}
function stateActions(state) {
  return STATE_ACTIONS[state] || [];
}
function isError(res) {
  return !!(res && res.error);
}

const HOLD_STATES = ["blocked", "duplicate", "needs_human_review", "needs_revision"];

// --- view navigation (no router) -------------------------------------------

let currentView = "inbox";
let currentJobId = null;
const VIEWS = ["inbox", "dashboard", "job", "setup"];
const NAV = { inbox: "nav-inbox", dashboard: "nav-dashboard", setup: "nav-setup" };

function showView(name) {
  currentView = name;
  VIEWS.forEach(function (v) {
    $("view-" + v).hidden = v !== name;
  });
  Object.keys(NAV).forEach(function (v) {
    const btn = $(NAV[v]);
    if (name === v) btn.setAttribute("aria-current", "page");
    else btn.removeAttribute("aria-current");
  });
}

// --- error + success framing (lex; replaces the old "error (N): " concat) ---

const BACKFILL_PHRASES = [
  { needle: "attestation required", msg: "尚未完成：你没勾「上架内容＝已签核版本」，工作仍停在「已签核」。勾选后再送一次。" },
  { needle: "published URL is required", msg: "请先填「已上架的网址」再送出。" },
];

function specialMessage(res) {
  const raw = String(res.error || "");
  for (let i = 0; i < BACKFILL_PHRASES.length; i++) {
    if (raw.indexOf(BACKFILL_PHRASES[i].needle) !== -1) return BACKFILL_PHRASES[i].msg;
  }
  return null;
}

function inlineError(res) {
  const lx = lexExit(res.exit_code);
  return specialMessage(res) || lx.title + "：" + lx.why;
}

function renderError(container, res) {
  clear(container);
  const lx = lexExit(res.exit_code);
  const box = el("div");
  box.className = "banner banner--error";
  box.appendChild(el("strong", lx.title));
  const special = specialMessage(res);
  box.appendChild(el("p", special || lx.why));
  if (!special && lx.next) box.appendChild(el("p", "下一步：" + lx.next));
  const toggle = button("技术细节 ▾", "link-toggle");
  const detail = el("div");
  detail.className = "tech-detail";
  detail.hidden = true;
  detail.appendChild(el("code", "exit " + res.exit_code + ": " + res.error));
  toggle.addEventListener("click", function () { detail.hidden = !detail.hidden; });
  box.appendChild(toggle);
  box.appendChild(detail);
  container.appendChild(box);
}

function renderBanner_(container, variant, title, detail) {
  clear(container);
  const box = el("div");
  box.className = "banner banner--" + variant;
  box.appendChild(el("strong", title));
  if (detail) box.appendChild(el("span", " " + detail));
  container.appendChild(box);
  return box;
}
function renderSuccess(container, title, detail) {
  const box = renderBanner_(container, "success", title, detail);
  box.setAttribute("aria-live", "polite");
}
function renderInfo(container, title, detail) {
  renderBanner_(container, "attention", title, detail);
}

// --- async transport: poll job_status until settled, fix G1 freeze ----------

const POLL_MS = 1500;
const POLL_CAP = 120; // ~90s; the spinner must never spin forever
const pollers = {}; // jobId -> {kind, ticks, startedAt, errors, box, timer}

function clearPoller(jobId) {
  const p = pollers[jobId];
  if (p && p.timer) clearTimeout(p.timer);
  delete pollers[jobId];
}

function stageLabel(kind) {
  if (kind === "crawl") return "正在抓取页面…";
  if (kind === "process_dry") return "正在跑安全预览（不连模型）…";
  if (kind === "process") return "正在请模型组装草稿…";
  return "处理中…";
}

function mountSpinner(kind) {
  const c = $("job-inflight");
  clear(c);
  const reduce = !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  const box = el("div");
  box.className = "inflight";
  const head = el("div");
  head.className = "inflight-head";
  const glyph = el("span", reduce ? "◐" : "");
  glyph.className = reduce ? "spin-glyph" : "spin";
  const elapsed = el("span", "00:00");
  elapsed.className = "elapsed";
  head.appendChild(glyph);
  head.appendChild(el("span", " " + stageLabel(kind)));
  head.appendChild(elapsed);
  box.appendChild(head);
  box.appendChild(el("p", "这会花点时间——视窗不会卡死，可切回收件匣。"));
  c.appendChild(box);
  return { box: box, elapsed: elapsed, glyph: glyph, reduce: reduce };
}

function updateSpinner(p) {
  if (!p.ui) return;
  const secs = Math.floor((p.ticks * POLL_MS) / 1000);
  const mm = String(Math.floor(secs / 60));
  const ss = String(secs % 60);
  setText(p.ui.elapsed, (mm.length < 2 ? "0" + mm : mm) + ":" + (ss.length < 2 ? "0" + ss : ss));
  if (p.ui.reduce) {
    const g = ["◐", "◓", "◑", "◒"];
    setText(p.ui.glyph, g[p.ticks % 4]);
  }
}

function enterProgress(jobId, kind) {
  currentJobId = jobId;
  showView("job");
  $("job-create").hidden = true;
  setText($("job-title"), "工作 " + jobId);
  clear($("job-banner"));
  clear($("job-actions"));
  clear($("job-packet"));
  clear($("job-status"));
  startPoll(jobId, kind);
}

function startPoll(jobId, kind) {
  clearPoller(jobId);
  const ui = mountSpinner(kind);
  pollers[jobId] = { kind: kind, ticks: 0, errors: 0, ui: ui, timer: null };
  pollTick(jobId);
}

function schedule(jobId) {
  const p = pollers[jobId];
  if (!p) return;
  p.timer = setTimeout(function () { pollTick(jobId); }, POLL_MS);
}

async function pollTick(jobId) {
  const p = pollers[jobId];
  if (!p) return;
  const a = api();
  if (!a) return;
  let resp;
  try {
    resp = await a.job_status(jobId);
    p.errors = 0;
  } catch (e) {
    p.errors += 1;
    if (p.errors > 3) { settle(jobId, { t: "FAILED", error: "与本机引擎失去联系——请重开程式。", exit_code: 5 }); return; }
    schedule(jobId);
    return;
  }
  // top-level {error} guard FIRST (idle-fallback raise; has no status key)
  if (resp && resp.error && resp.status === undefined) {
    settle(jobId, { t: "FAILED", error: resp.error, exit_code: resp.exit_code });
    return;
  }
  switch (resp && resp.status) {
    case "running":
      p.ticks += 1;
      updateSpinner(p);
      if (p.ticks >= POLL_CAP) { capReached(jobId); return; }
      schedule(jobId);
      return;
    case "done": {
      const r = resp.result || {};
      if (r.error) { settle(jobId, { t: "FAILED", error: r.error, exit_code: r.exit_code }); return; }
      const held = HOLD_STATES.indexOf(r.state) !== -1;
      settle(jobId, { t: held ? "PARKED" : "DONE", state: r.state });
      return;
    }
    case "error": {
      const r = resp.result || {};
      settle(jobId, { t: "FAILED", error: r.error, exit_code: r.exit_code });
      return;
    }
    case "idle":
      settle(jobId, { t: "SETTLED", state: resp.state });
      return;
    case "unknown":
      settle(jobId, { t: "LOST" });
      return;
    default:
      settle(jobId, { t: "FAILED", error: "未知的处理状态。", exit_code: 5 });
      return;
  }
}

function capReached(jobId) {
  clearPoller(jobId);
  renderInfo($("job-inflight"), "还在处理——比平常久", "让它继续，或稍后回收件匣看结果。");
}

async function settle(jobId, outcome) {
  clearPoller(jobId);
  // Only re-render the workspace if the operator is actually viewing this job.
  // If they navigated to the inbox (or another job) while it ran, don't yank
  // them back — just refresh the inbox so the new state shows there.
  if (!(currentView === "job" && jobId === currentJobId)) { refreshInbox(); return; }
  clear($("job-inflight"));
  if (outcome.t === "FAILED") { renderError($("job-status"), { error: outcome.error, exit_code: outcome.exit_code }); refreshInbox(); return; }
  if (outcome.t === "LOST") { renderError($("job-status"), { error: "此工作已不在磁碟上：" + jobId, exit_code: 2 }); refreshInbox(); return; }
  // DONE / PARKED / SETTLED -> re-render the workspace from persisted state,
  // then a transport note. PARKED is NOT a green success (it is "stopped for you").
  await openJob(jobId);
  const title = outcome.state ? lexState(outcome.state).title : "";
  if (outcome.t === "PARKED") renderInfo($("job-status"), "已替你停下（见上方说明）", title ? "→ " + title : "");
  else if (outcome.t === "SETTLED") renderInfo($("job-status"), "上次已完成", title ? "→ " + title : "");
  else renderSuccess($("job-status"), "处理完成", title ? "→ " + title : "");
}

// --- INBOX: counts + worklist in 4 derived bands ---------------------------

const BANDS = [
  { key: "attention", label: "需要你处理", collapsed: false,
    states: ["needs_human_review", "needs_revision", "review_pending", "approved"] },
  { key: "stopped", label: "被机器拦下（合规）", collapsed: false,
    states: ["blocked", "duplicate"] },
  { key: "inflight", label: "进行中", collapsed: false,
    states: ["new", "crawled", "crawled_warn", "processed", "crawl_failed", "process_failed"] },
  { key: "closed", label: "已结案", collapsed: true,
    states: ["rejected", "superseded", "published_recorded"] },
];

function bandFor(state) {
  for (let i = 0; i < BANDS.length; i++) {
    if (BANDS[i].states.indexOf(state) !== -1) return BANDS[i].key;
  }
  return "inflight";
}

function badgeFor(state) {
  const lx = lexState(state);
  const b = el("span", lx.glyph + " " + lx.title);
  b.className = "badge badge--" + (lx.tone || "neutral");
  b.setAttribute("data-glyph", lx.glyph);
  return b;
}

function jobRow(job) {
  const lx = lexState(job.state);
  const row = el("div");
  row.className = "job-row lane--" + (lx.tone || "neutral");
  // whole row is the affordance (P0-7): clickable + keyboard-focusable. The
  // visible "打开 ›" cue stays, so colour/elevation is never the only signal.
  row.setAttribute("role", "button");
  row.setAttribute("tabindex", "0");
  row.addEventListener("click", function () { openJob(job.job_id); });
  row.addEventListener("keydown", function (e) {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openJob(job.job_id); }
  });
  const id = el("span", job.job_id); id.className = "job-id";
  row.appendChild(id);
  row.appendChild(badgeFor(job.state));
  let whyShort = lx.title;
  if (job.state === "needs_human_review" && job.review_reason) {
    const r = lexReason(job.review_reason);
    if (r) whyShort = r.label;
  }
  const why = el("span", whyShort); why.className = "job-why";
  row.appendChild(why);
  if (job.updated_at) { const w = el("span", job.updated_at); w.className = "job-when"; row.appendChild(w); }
  const open = button("打开 ›", "btn-secondary");
  open.addEventListener("click", function (e) { e.stopPropagation(); openJob(job.job_id); });
  row.appendChild(open);
  return row;
}

async function refreshInbox() {
  const a = api();
  if (!a) return;
  const bandsEl = $("inbox-bands");
  clear(bandsEl);
  setText($("inbox-counts"), "载入中…");
  bandsEl.appendChild(loadingRow());

  const listRes = await a.list_jobs(null);
  clear(bandsEl);
  if (isError(listRes)) { renderError(bandsEl, listRes); setText($("inbox-counts"), ""); return; }
  const jobs = listRes.jobs || [];

  const buckets = {};
  BANDS.forEach(function (b) { buckets[b.key] = []; });
  jobs.forEach(function (j) { buckets[bandFor(j.state)].push(j); });

  let totalShown = 0;
  BANDS.forEach(function (b) {
    const rows = buckets[b.key];
    totalShown += rows.length;
    const section = el("section");
    section.className = "band band--" + b.key;
    const head = el("div");
    head.className = "band-head";
    head.appendChild(el("strong", b.label + "（" + rows.length + "）"));
    const body = el("div");
    body.className = "band-body";
    let collapsed = b.collapsed && rows.length > 0;
    body.hidden = collapsed;
    if (b.key === "closed" && rows.length > 0) {
      const toggle = button(collapsed ? "展开" : "收起", "link-toggle");
      toggle.addEventListener("click", function () {
        collapsed = !collapsed; body.hidden = collapsed; setText(toggle, collapsed ? "展开" : "收起");
      });
      head.appendChild(toggle);
    }
    section.appendChild(head);
    if (rows.length === 0) {
      const empty = el("p", b.key === "attention" ? "没有待办。" : "（无）");
      empty.className = "empty";
      body.appendChild(empty);
    } else {
      rows.forEach(function (j) { body.appendChild(jobRow(j)); });
    }
    section.appendChild(body);
    bandsEl.appendChild(section);
  });

  if (totalShown === 0) {
    const empty = el("p", "收件匣已清空——没有待办。建立第一个工作？点上方「+ 新工作」。");
    empty.className = "empty";
    bandsEl.appendChild(empty);
  }

  const sumRes = await a.summary();
  const countsEl = $("inbox-counts");
  clear(countsEl);
  if (!isError(sumRes)) {
    const counts = sumRes.summary || {};
    Object.keys(counts).filter(function (k) { return k !== "total"; }).sort().forEach(function (k) {
      const chip = el("span", lexState(k).title + " " + counts[k]);
      chip.className = "count-chip";
      countsEl.appendChild(chip);
    });
  }
}

function loadingRow() {
  const p = el("p", "载入中…");
  p.className = "empty loading";
  return p;
}

// --- DASHBOARD: accumulated metrics (read-only) -----------------------------

function lexDash() {
  return LEX.dashboard || {};
}
function gateLabel(gate) {
  const d = lexDash();
  return (d.gate && d.gate[gate]) || gate;
}
function pct(rate) {
  return rate == null ? "—" : Math.round(rate * 100) + "%";
}

function dashSection(title) {
  const sec = el("section");
  sec.className = "dash-section";
  sec.appendChild(el("h3", title));
  return sec;
}

function statTable(headers, rows) {
  const table = el("table");
  table.className = "dash-table";
  const thead = el("tr");
  headers.forEach(function (h) { thead.appendChild(el("th", h)); });
  table.appendChild(thead);
  rows.forEach(function (cells) {
    const tr = el("tr");
    cells.forEach(function (c) { tr.appendChild(el("td", c)); });
    table.appendChild(tr);
  });
  return table;
}

function renderDashboardEmpty(container) {
  const d = lexDash();
  const box = el("div");
  box.className = "dash-empty";
  box.appendChild(el("strong", d.empty_title || "还没有累积"));
  box.appendChild(el("p", d.empty_body || ""));
  container.appendChild(box);
}

function renderDashboard(container, res) {
  const d = lexDash();
  clear(container);
  const lead = el("p", d.subtitle || "");
  lead.className = "dash-subtitle";
  container.appendChild(lead);

  // empty state: never a wall of zeros on first run (the whole point of the
  // feature is to feel accumulated, not blank).
  if (!res.has_jobs) { renderDashboardEmpty(container); return; }

  // states
  const counts = res.summary || {};
  const stateRows = Object.keys(counts).filter(function (k) { return k !== "total"; }).sort()
    .map(function (k) { return [lexState(k).title, String(counts[k])]; });
  if (stateRows.length) {
    const sec = dashSection(d.section_states || "目前各状态");
    sec.appendChild(statTable(["", ""], stateRows));
    container.appendChild(sec);
  }

  // gate intercept rates
  const gates = res.gates || [];
  if (gates.length) {
    const sec = dashSection(d.section_gates || "各关卡拦截率");
    const rows = gates.map(function (g) {
      return [gateLabel(g.gate), String(g.reached), String(g.intercepted), pct(g.rate)];
    });
    sec.appendChild(statTable(
      ["", d.col_reached || "到达", d.col_intercepted || "拦下", d.col_rate || "拦截率"], rows
    ));
    container.appendChild(sec);
  } else {
    const sec = dashSection(d.section_gates || "各关卡拦截率");
    const p = el("p", d.no_intercepts || ""); p.className = "empty";
    sec.appendChild(p);
    container.appendChild(sec);
  }

  // review reasons
  const reasons = res.review_reasons || {};
  const reasonKeys = Object.keys(reasons);
  if (reasonKeys.length) {
    const sec = dashSection(d.section_reasons || "人工处理原因");
    const rows = reasonKeys.sort().map(function (k) {
      const r = lexReason(k);
      return [r ? r.label : k, String(reasons[k])];
    });
    sec.appendChild(statTable(["", d.col_count || "次数"], rows));
    container.appendChild(sec);
  }

  // gate intervals (labelled: includes operator wait; NOT compute time)
  const intervals = res.gate_intervals || [];
  if (intervals.length) {
    const sec = dashSection(d.section_intervals || "关卡间隔（含等待）");
    const caveat = el("p", d.intervals_caveat || ""); caveat.className = "dash-caveat";
    sec.appendChild(caveat);
    const rows = intervals.map(function (it) {
      return [it.transition, String(it.count), String(Math.round(it.avg_seconds)), String(Math.round(it.max_seconds))];
    });
    sec.appendChild(statTable(
      ["", d.col_count || "次数", d.col_avg || "平均(秒)", d.col_max || "最长(秒)"], rows
    ));
    container.appendChild(sec);
  }

  // daily throughput
  const daily = res.daily_jobs || {};
  const days = Object.keys(daily);
  if (days.length) {
    const sec = dashSection(d.section_daily || "每日产量");
    const rows = days.sort().map(function (day) { return [day, String(daily[day])]; });
    sec.appendChild(statTable(["", d.col_count || "次数"], rows));
    container.appendChild(sec);
  }
}

async function openDashboard() {
  showView("dashboard");
  setText($("dashboard-title"), lexDash().title || "总览");
  const body = $("dashboard-body");
  clear(body);
  body.appendChild(loadingRow());
  const a = api();
  if (!a) return;
  const res = await a.dashboard_stats();
  clear(body);
  if (isError(res)) { renderError(body, res); return; }
  renderDashboard(body, res);
}

// --- JOB workspace ----------------------------------------------------------

async function openJob(jobId) {
  const a = api();
  if (!a) return;
  currentJobId = jobId;
  showView("job");
  $("job-create").hidden = true;
  disarmConfirm();
  clear($("job-status"));
  setText($("job-title"), "工作 " + jobId);

  // transport check: if a background task is in flight, resume progress mode.
  const ts = await a.job_status(jobId);
  if (ts && ts.status === "running") {
    const kind = (pollers[jobId] && pollers[jobId].kind) || "process";
    enterProgress(jobId, kind);
    return;
  }

  clear($("job-banner"));
  $("job-banner").appendChild(loadingRow());
  const listRes = await a.list_jobs(null);
  let rec = null;
  if (!isError(listRes)) {
    (listRes.jobs || []).forEach(function (j) { if (j.job_id === jobId) rec = j; });
  }
  if (rec === null) {
    renderError($("job-banner"), { error: "找不到这个工作：" + jobId, exit_code: 2 });
    clear($("job-actions")); clear($("job-packet"));
    return;
  }
  renderBanner(rec.state, rec.review_reason);
  const reviewers = await a.reviewers();
  renderActions(rec.state, rec.review_reason, reviewers);
  await renderPacket(jobId);
}

function renderBanner(state, reason) {
  const c = $("job-banner");
  clear(c);
  const lx = lexState(state);
  const box = el("div");
  box.className = "banner banner--state tone--" + (lx.tone || "neutral");
  const head = el("div"); head.className = "banner-head";
  head.appendChild(badgeFor(state));
  box.appendChild(head);
  box.appendChild(el("p", lx.why));
  if (lx.next) box.appendChild(el("p", "你可以：" + lx.next));
  if (state === "needs_human_review" && reason) {
    const r = lexReason(reason);
    if (r) {
      const rb = el("div"); rb.className = "reason-note";
      rb.appendChild(el("strong", r.label + "："));
      rb.appendChild(el("span", r.why));
      box.appendChild(rb);
    }
  }
  c.appendChild(box);
}

function reviewerSelect(reviewers) {
  const sel = el("select");
  sel.className = "reviewer";
  (reviewers.reviewers || []).forEach(function (name) {
    const opt = el("option", name);
    opt.value = name;
    sel.appendChild(opt);
  });
  return sel;
}
function templateSelect() {
  // A <select> of 栏目 templates. Default empty option = no template (the
  // assemble path runs exactly as before). Populated async from a.templates();
  // names arrive pre-escaped from the bridge and are assigned via textContent.
  const sel = el("select");
  sel.className = "template-pick";
  const none = el("option", "（不套用模板）");
  none.value = "";
  sel.appendChild(none);
  const a = api();
  if (a && a.templates) {
    Promise.resolve(a.templates()).then(function (res) {
      if (!res || isError(res) || !res.categories) return;
      res.categories.forEach(function (name) {
        const opt = el("option", name);
        opt.value = name;
        sel.appendChild(opt);
      });
    });
  }
  return sel;
}
function reviewersEmpty(reviewers) {
  return !reviewers || isError(reviewers) || !(reviewers.reviewers && reviewers.reviewers.length);
}
function reviewerOnboarding(container) {
  const box = el("div"); box.className = "banner banner--attention";
  box.appendChild(el("strong", "还不能签核"));
  box.appendChild(el("p", "尚未设定审阅者名单。请在 config.yaml 的 publisher.reviewers 加入名字（一次性技术设定）——这是合规归属决定，不在此 GUI 修改。"));
  container.appendChild(box);
}

// --- in-DOM confirm tray (arm -> commit -> disarm; one at a time) -----------

let armedTray = null;
function disarmConfirm() {
  if (armedTray) { armedTray.hidden = true; armedTray = null; }
}
function confirmTray(triggerLabel, triggerCls, buildBody, commitLabel, onCommit, preCheck) {
  const wrap = el("div");
  wrap.className = "confirm-wrap";
  const trigger = button(triggerLabel, triggerCls);
  const body = el("div");
  body.className = "confirm-tray";
  body.hidden = true;
  buildBody(body);
  const acts = el("div"); acts.className = "confirm-actions";
  const cancel = button("取消", "btn-secondary");
  const commit = button(commitLabel, triggerCls + " is-armed");
  cancel.addEventListener("click", function () { body.hidden = true; armedTray = null; });
  commit.addEventListener("click", async function () {
    if (preCheck && !preCheck()) return;
    await onCommit();
  });
  acts.appendChild(cancel);
  acts.appendChild(commit);
  body.appendChild(acts);
  trigger.addEventListener("click", function () { disarmConfirm(); body.hidden = false; armedTray = body; });
  wrap.appendChild(trigger);
  wrap.appendChild(body);
  return wrap;
}

async function renderActions(state, reason, reviewers) {
  const c = $("job-actions");
  clear(c);
  disarmConfirm();
  const actions = stateActions(state);
  if (actions.length === 0) return; // terminal / processing: zero buttons (fail-closed)

  const SIGNOFF = { approve: 1, reject: 1, backfill: 1, openHold: 1 };
  const needsReviewer = actions.some(function (act) { return SIGNOFF[act.method]; });
  if (needsReviewer && reviewersEmpty(reviewers)) {
    reviewerOnboarding(c);
    if (actions.some(function (act) { return act.method === "supersede"; })) c.appendChild(supersedeRow());
    return;
  }
  actions.forEach(function (act) {
    const row = buildActionRow(act, reviewers, reason);
    if (row) c.appendChild(row);
  });
}

function supersedeRow() {
  const row = el("div"); row.className = "action-row";
  const inp = textInput("接手的新 job id（可留空）");
  const tray = confirmTray("作废 Supersede", "btn-danger",
    function (body) {
      body.appendChild(el("p", "作废后此版本停用、由新工作取代，无法复原。"));
      body.appendChild(labeled("新 job id：", inp));
    },
    "确定作废",
    async function () { const a = api(); if (!a) return; afterAction(await a.supersede(currentJobId, inp.value || null), "已作废"); }
  );
  row.appendChild(el("span", "作废："));
  row.appendChild(tray);
  return row;
}

function buildActionRow(act, reviewers, reason) {
  const row = el("div");
  row.className = "action-row";
  const a = api();

  if (act.method === "process") {
    const title = textInput("标题（可留空）");
    const dry = checkbox();
    const dryL = el("label"); dryL.appendChild(dry); dryL.appendChild(el("span", " 安全预览"));
    // process-time inputs (Unit 5): watermark toggle, 栏目 template, AI 文案
    const wm = checkbox();
    const wmL = el("label"); wmL.appendChild(wm); wmL.appendChild(el("span", " 打官方水印"));
    const tmpl = templateSelect();
    const ai = checkbox();
    const aiL = el("label"); aiL.appendChild(ai); aiL.appendChild(el("span", " AI 图说/FAQ/小标题（待审）"));
    const btn = button(act.label, "btn-primary");
    btn.addEventListener("click", async function () {
      if (!a || pollers[currentJobId]) return;
      setBusy(btn, true);
      // watermark: null = follow config; checkbox makes it an explicit true/false
      const kick = await a.process_async(
        currentJobId, title.value, dry.checked, wm.checked, tmpl.value || null, ai.checked
      );
      setBusy(btn, false);
      if (isError(kick)) { renderError($("job-status"), kick); return; }
      enterProgress(currentJobId, dry.checked ? "process_dry" : "process");
    });
    row.appendChild(title);
    row.appendChild(labeled("栏目模板：", tmpl));
    row.appendChild(wmL);
    row.appendChild(aiL);
    row.appendChild(dryL);
    row.appendChild(btn);
    return row;
  }

  if (act.method === "make_review_packet") {
    const tray = confirmTray(act.label, "btn-primary",
      function (body) { body.appendChild(el("p", "冻结后内文不能再改，只能退件或作废。确定建立审阅包？")); },
      "确定冻结",
      async function () { if (!a) return; afterAction(await a.make_review_packet(currentJobId), "已建立审阅包"); }
    );
    row.appendChild(tray);
    return row;
  }

  if (act.method === "openCreate") {
    const btn = button(act.label, "btn-primary");
    btn.addEventListener("click", function () { openCreate(currentJobId); });
    row.appendChild(btn);
    return row;
  }

  if (act.method === "openHold") {
    return holdPanel(reviewers, reason);
  }

  if (act.method === "approve") {
    const sel = reviewerSelect(reviewers);
    const btn = button("核可 Approve", "btn-primary");
    btn.addEventListener("click", async function () { if (!a) return; afterAction(await a.approve(currentJobId, sel.value), "已核可"); });
    row.appendChild(el("span", "审阅者："));
    row.appendChild(sel);
    row.appendChild(btn);
    row.appendChild(disclaimerNote());
    return row;
  }

  if (act.method === "reject") {
    const sel = reviewerSelect(reviewers);
    const reason = textInput("退件理由（必填）");
    const tray = confirmTray("退件 Reject", "btn-danger",
      function (body) {
        body.appendChild(el("p", "退回后此工作进入「已退件·终止」，无法复原（只能作废另开新）。"));
        body.appendChild(labeled("审阅者：", sel));
        body.appendChild(reason);
      },
      "确定退回",
      async function () { if (!a) return; afterAction(await a.reject(currentJobId, sel.value, reason.value), "已退件"); },
      function () { if (!reason.value.trim()) { setText($("job-status"), "请先填退件理由。"); return false; } return true; }
    );
    row.appendChild(tray);
    return row;
  }

  if (act.method === "supersede") {
    return supersedeRow();
  }

  if (act.method === "backfill") {
    const sel = reviewerSelect(reviewers);
    const url = textInput("已上架的网址");
    const attest = checkbox();
    const tray = confirmTray("回填网址并具结", "btn-primary",
      function (body) {
        body.appendChild(labeled("审阅者：", sel));
        body.appendChild(labeled("已上架网址：", url));
        const al = el("label"); al.appendChild(attest); al.appendChild(el("span", " 我确认上架内容＝已签核版本"));
        body.appendChild(al);
        const h = el("p", LEX.honesty.backfill_attest); h.className = "honesty-callout";
        body.appendChild(h);
      },
      "确定回填",
      // no-op detection keys on the returned ERROR dict (signoff raises on
      // no-tick / empty-url), NOT on an `attested` boolean — afterAction renders
      // that error via the stable backfill phrases.
      async function () { if (!a) return; afterAction(await a.backfill(currentJobId, sel.value, url.value, attest.checked), "已登记上架"); }
    );
    row.appendChild(tray);
    return row;
  }

  return null;
}

function disclaimerNote() {
  const box = el("div"); box.className = "disclaimer";
  const a = api();
  if (a) a.disclaimer().then(function (res) { if (res && res.disclaimer) setText(box, res.disclaimer); });
  return box;
}

function holdPanel(reviewers, reason) {
  // the hold reason is on the banner already; here we build the resolution panel
  const panel = el("div");
  panel.className = "hold-panel";
  const sel = reviewerSelect(reviewers);
  let relint = false;

  if (reason === "grounding") {
    const a1 = el("label"); const r1 = el("input"); r1.type = "radio"; r1.name = "hold-mode"; r1.checked = true;
    a1.appendChild(r1); a1.appendChild(el("span", " 重新检查（核对过出处就选这个，通过自动放行）"));
    const a2 = el("label"); const r2 = el("input"); r2.type = "radio"; r2.name = "hold-mode";
    a2.appendChild(r2); a2.appendChild(el("span", " 人工放行（须写理由）"));
    panel.appendChild(a1); panel.appendChild(a2);
    relint = true;
    r1.addEventListener("change", function () { relint = r1.checked; reasonInput.hidden = relint; });
    r2.addEventListener("change", function () { relint = !r2.checked; reasonInput.hidden = relint; });
  }
  const reasonInput = textInput("理由");
  reasonInput.hidden = relint;
  if (reason === "dedup" || reason === "risk") {
    const note = el("p", lexReason(reason).note);
    note.className = "honesty-callout";
    panel.appendChild(note);
  }
  const btn = button("清除 hold → 草稿完成", "btn-primary");
  btn.addEventListener("click", async function () {
    const a = api(); if (!a) return;
    if (!relint && !reasonInput.value.trim()) { setText($("job-status"), "人工放行须写理由。"); return; }
    afterAction(await a.resolve(currentJobId, sel.value, relint, relint ? null : reasonInput.value), "已处理 hold");
  });
  panel.appendChild(el("span", "审阅者："));
  panel.appendChild(sel);
  panel.appendChild(reasonInput);
  panel.appendChild(btn);
  return panel;
}

async function afterAction(res, okLabel) {
  if (isError(res)) { renderError($("job-status"), res); return; }
  const newState = res.state ? lexState(res.state).title : "完成";
  renderSuccess($("job-status"), okLabel, "→ " + newState);
  computeReadiness();
  if (currentJobId) {
    const note = $("job-status").firstChild;
    await openJob(currentJobId);
    if (note) $("job-status").appendChild(note); // preserve the success line under the re-rendered view
  }
}

async function renderPacket(jobId) {
  const a = api();
  if (!a) return;
  const view = $("job-packet");
  clear(view);
  const res = await a.get_packet(jobId);
  if (isError(res)) return; // pre-draft states have no packet — hide, never show an error card
  const card = el("div");
  card.className = "packet" + (res.state === "review_pending" ? " is-frozen" : "");
  if (res.state === "review_pending") {
    const ribbon = el("span", "已凍結 FROZEN"); ribbon.className = "frozen-ribbon"; card.appendChild(ribbon);
  }
  card.appendChild(el("h3", "审阅包（机器产生 · 待人工校阅）"));
  packetField(card, "标题", res.title);
  packetField(card, "分类", res.category);
  packetField(card, "一分钟看懂", res.intro);
  packetList(card, "快速事实", res.quick_facts);
  packetField(card, "事件经过", res.event_body);
  if (res.faq && res.faq.length) {
    card.appendChild(el("strong", "FAQ："));
    res.faq.forEach(function (item) {
      card.appendChild(el("div", "Q: " + item.question));
      card.appendChild(el("div", "A: " + item.answer));
    });
  }
  packetField(card, "结尾", res.summary);
  packetList(card, "Tags", res.tags);
  inertLinks(card, res.source_urls);
  packetField(card, "Model", res.model);
  packetField(card, "Finish reason", res.finish_reason);
  if (res.body_sha256) {
    const chip = el("span", "frozen hash " + res.body_sha256); chip.className = "hash-chip"; card.appendChild(chip);
  }
  view.appendChild(card);
  await renderCoverReport(jobId, view);
}

async function renderCoverReport(jobId, view) {
  const a = api();
  if (!a || !a.cover_report) return;
  const res = await a.cover_report(jobId);
  if (isError(res) || !res || !res.has_report || !res.cover) return;
  const box = el("div"); box.className = "cover-report";
  box.appendChild(el("h3", "封面检查（建议性 · 不拦截）"));
  packetField(box, "封面", res.cover);
  if (res.cover_preview) packetField(box, "安全区预览图", res.cover_preview);
  const geo = res.geometry || [], aes = res.aesthetic || [];
  if (!geo.length && !aes.length) {
    box.appendChild(el("p", "没有封面警告。"));
  } else {
    if (geo.length) { box.appendChild(el("strong", "几何警告：")); geo.forEach(function (g) { box.appendChild(el("div", "• " + g)); }); }
    if (aes.length) { box.appendChild(el("strong", "美学建议：")); aes.forEach(function (s) { box.appendChild(el("div", "• " + s)); }); }
  }
  view.appendChild(box);
}

function packetField(container, label, value) {
  if (value === undefined || value === null || value === "") return;
  const wrap = el("div"); wrap.className = "field";
  wrap.appendChild(el("strong", label + "："));
  wrap.appendChild(el("span", value));
  container.appendChild(wrap);
}
function packetList(container, label, items) {
  if (!items || !items.length) return;
  container.appendChild(el("strong", label + "："));
  const ul = el("ul");
  items.forEach(function (it) { ul.appendChild(el("li", it)); });
  container.appendChild(ul);
}
function inertLinks(container, items) {
  if (!items || !items.length) return;
  const wrap = el("div"); wrap.className = "inert-link";
  const tag = el("span", "来源（仅供查证，不可点击）："); tag.className = "inert-link__tag";
  wrap.appendChild(tag);
  items.forEach(function (u) {
    const url = el("span", u); url.className = "inert-link__url";
    wrap.appendChild(url);
  });
  container.appendChild(wrap);
}

// --- create mode ------------------------------------------------------------

// raw (unescaped) source_ref keyed by saved-source id — ONLY ever assigned to
// an input .value (never a markup sink), so the original URL/path is re-submitted
// through the normal, re-validated create path.
const savedSourceRaw = {};

function createModeUrl() {
  return $("create-mode-url").checked;
}
function setCreateMode(isUrl) {
  $("create-mode-url").checked = isUrl;
  $("create-mode-dir").checked = !isUrl;
  $("create-url-row").hidden = !isUrl;
  $("create-dir-row").hidden = isUrl;
}

function applySavedSource(id) {
  const raw = savedSourceRaw[id];
  if (raw == null) return;
  // URL-ish -> url mode + url field; otherwise a local dir path.
  const isUrl = /^[a-z][a-z0-9+.-]*:\/\//i.test(raw);
  setCreateMode(isUrl);
  if (isUrl) $("create-url").value = raw;
  else $("create-dir").value = raw;
}

async function loadSavedSources() {
  const a = api();
  const row = $("create-reuse-row");
  const pick = $("create-source-pick");
  if (!a) { row.hidden = true; return; }
  const res = await a.saved_sources();
  Object.keys(savedSourceRaw).forEach(function (k) { delete savedSourceRaw[k]; });
  clear(pick);
  const sources = (res && res.sources) || [];
  if (isError(res) || sources.length === 0) { row.hidden = true; return; }
  const placeholder = el("option", "— 选一个已存来源 —");
  placeholder.value = "";
  pick.appendChild(placeholder);
  sources.forEach(function (s) {
    savedSourceRaw[s.id] = s.source_ref_raw;
    const opt = el("option", s.label + "（" + s.source_ref + "）");
    opt.value = s.id;
    pick.appendChild(opt);
  });
  row.hidden = false;
}

function openCreate(jobId) {
  showView("job");
  disarmConfirm();
  $("job-create").hidden = false;
  clear($("job-banner")); clear($("job-actions")); clear($("job-packet")); clear($("job-status")); clear($("job-inflight"));
  setText($("job-title"), jobId ? "重新抓取 " + jobId : "新工作");
  if (jobId) $("create-job-id").value = jobId;
  $("create-save-source").checked = false;
  $("create-save-label").value = "";
  setText($("create-status"), "");
  loadSavedSources();
}

async function maybeSaveSource(ref) {
  if (!$("create-save-source").checked) return;
  const a = api();
  if (!a || !ref) return;
  const label = $("create-save-label").value.trim() || ref;
  await a.add_saved_source(label, ref); // best-effort; failure must not block the job
}

function bindCreate() {
  $("create-mode-url").addEventListener("change", function () { $("create-url-row").hidden = false; $("create-dir-row").hidden = true; });
  $("create-mode-dir").addEventListener("change", function () { $("create-url-row").hidden = true; $("create-dir-row").hidden = false; });
  $("create-source-pick").addEventListener("change", function () { applySavedSource(this.value); });
  $("create-source-del").addEventListener("click", async function () {
    const a = api(); if (!a) return;
    const id = $("create-source-pick").value;
    if (!id) return;
    await a.delete_saved_source(id);
    await loadSavedSources();
  });
  $("btn-create").addEventListener("click", async function () {
    const a = api(); if (!a) return;
    const jobId = $("create-job-id").value.trim();
    if (!jobId) { setText($("create-status"), "请先填工作 id。"); return; }
    const btn = $("btn-create");
    // Re-check readiness LIVE against the backend, not the module flag: that flag
    // is only refreshed on init/save/open-setup, so a bridge-not-ready-at-init
    // race can leave it stale-false even after settings are correctly saved.
    setText($("create-status"), "检查设定…");
    const ready = await computeReadiness();
    if (!ready || ready.error || !ready.pipelineReady) { setText($("create-status"), "还没设定好模型 endpoint／金鑰——请先到「设定」。"); return; }
    setText($("create-status"), "");
    if ($("create-mode-url").checked) {
      const url = $("create-url").value.trim();
      if (!url) { setText($("create-status"), "请填网址。"); return; }
      setBusy(btn, true);
      const kick = await a.create_and_crawl_async(jobId, url); // async -> no freeze
      setBusy(btn, false);
      if (isError(kick)) { setText($("create-status"), inlineError(kick)); return; }
      await maybeSaveSource(url);
      enterProgress(jobId, "crawl");
      refreshInbox();
    } else {
      const dir = $("create-dir").value.trim();
      if (!dir) { setText($("create-status"), "请填资料夹路径。"); return; }
      // ingest_dir has no async twin (local, no network). Sync + busy + notice.
      setBusy(btn, true);
      setText($("create-status"), "正在匯入资料夹…可能需要一点时间（视窗不会卡死的话）。");
      const res = await a.ingest_dir(jobId, dir);
      setBusy(btn, false);
      if (isError(res)) { setText($("create-status"), inlineError(res)); return; }
      await maybeSaveSource(dir);
      openJob(jobId);
      refreshInbox();
    }
  });
}

// --- SETUP: LLM settings + readiness checklist + soft gating (P2.1) ----------

const READY = { pipelineReady: false, signoffReady: false };

function setKeyState(isSet) { setText($("settings-key-state"), isSet ? "key：已设" : "key：未设"); }

async function loadSettings() {
  const a = api(); if (!a) return;
  const res = await a.get_settings();
  if (isError(res)) { setText($("settings-status"), inlineError(res)); return; }
  $("settings-base-url").value = res.base_url || "";
  $("settings-model").value = res.model || "";
  setKeyState(res.api_key_set);
}

async function saveSettings() {
  const a = api(); if (!a) return;
  const res = await a.save_settings($("settings-base-url").value, $("settings-model").value, $("settings-api-key").value);
  $("settings-api-key").value = "";
  if (isError(res)) { setText($("settings-status"), inlineError(res)); return; }
  setKeyState(res.api_key_set);
  setText($("settings-status"), "已储存" + (res.key_saved ? "（api_key 已更新）" : ""));
  renderReadiness(await computeReadiness());
}

// readiness: P1 endpoint, P2 key (GUI-editable); P3 allowlist, P4 reviewers
// (config.yaml-only, R33). Phase 0 exposes allow_domains so P3 is a real bool;
// 'unknown' is a defensive fallback only if a backend ever drops the key.
async function computeReadiness() {
  const a = api();
  if (!a) return { error: true };
  const s = await a.get_settings();
  const r = await a.reviewers();
  if (isError(s)) { applyPill(false, false); return { error: true, exit_code: s.exit_code, msg: s.error, config_path: s.config_path }; }
  const p1 = !!(s.base_url && s.model);
  const p2 = s.api_key_set === true;
  const p3 = ("allow_domains" in s) ? (s.allow_domains.length > 0) : "unknown";
  const p4 = !isError(r) && !!(r.reviewers && r.reviewers.length > 0);
  applyPill(p1 && p2, p4);
  return { p1: p1, p2: p2, p3: p3, p4: p4, config_path: s.config_path, pipelineReady: p1 && p2, signoffReady: p4 };
}

function applyPill(pipelineReady, signoffReady) {
  READY.pipelineReady = pipelineReady;
  READY.signoffReady = signoffReady;
  const ready = pipelineReady && signoffReady;
  const pill = $("ready-pill");
  setText(pill, ready ? "● 就绪" : "⚠ 需设定");
  pill.className = "pill " + (ready ? "pill--ready" : "pill--block");
}

function readyRow(label, status, consequence, fixer) {
  const row = el("div"); row.className = "ready-row";
  const pill = el("span");
  if (status === true) { setText(pill, "● 已设"); pill.className = "badge badge--go"; }
  else if (status === "unknown") { setText(pill, "◐ 无法确认"); pill.className = "badge badge--neutral"; }
  else { setText(pill, "○ 缺"); pill.className = "badge badge--caution"; }
  row.appendChild(pill);
  row.appendChild(el("strong", " " + label));
  row.appendChild(el("span", " — " + consequence));
  const who = el("span", "（" + fixer + "）"); who.className = "hint";
  row.appendChild(who);
  return row;
}

function renderReadiness(r) {
  const c = $("setup-readiness");
  clear(c);
  if (r.error) { renderError(c, { error: r.msg || "读取设定失败", exit_code: r.exit_code }); return; }

  // gate banner — variant A / B / C. C-partial (p3 'unknown') only pre-Phase-0;
  // never shows green when something is unconfirmed.
  let variant, title;
  const n = [r.p1, r.p2, r.p3 === true, r.p4].filter(Boolean).length;
  if (!r.p1 || !r.p2) { variant = "attention"; title = "⚠ 设定未完成 — " + n + "/4 就绪"; }
  else if (r.p3 === "unknown") { variant = "attention"; title = "◐ 大致就绪——允许清单无法核对（首次抓取才是真测试）"; }
  else if (r.p3 !== true || !r.p4) { variant = "attention"; title = "⚠ 还需一次性技术设定（见下方交接）"; }
  else { variant = "success"; title = "● 全部就绪"; }
  renderBanner_(c, variant, title, "");

  const list = el("div"); list.className = "ready-list";
  list.appendChild(readyRow("模型 endpoint", r.p1, "process 需要它；没设处理时会报「还没设定好」", "可在此编辑"));
  list.appendChild(readyRow("模型 API key", r.p2, "同上；存在 OS keyring", "可在此编辑"));
  list.appendChild(readyRow("爬虫允许清单 allow_domains", r.p3, "空清单会拒绝每个抓取网址", "config.yaml only（合规边界）"));
  list.appendChild(readyRow("审阅者白名单 reviewers", r.p4, "空名单会让全部签核被阻", "config.yaml only（签核归属）"));
  c.appendChild(list);

  if (r.p3 !== true || !r.p4) {
    const card = el("div"); card.className = "handoff";
    card.appendChild(el("strong", "在 config.yaml 加入（一次性技术设定，GUI 不改这些）："));
    const path = el("p"); path.appendChild(el("span", "档案："));
    path.appendChild(el("code", r.config_path || "config.yaml")); card.appendChild(path);
    const pre = el("pre");
    pre.textContent = "crawler:\n  allow_domains:\n    - your-allowlisted-site.example\npublisher:\n  reviewers:\n    - 你的名字";
    card.appendChild(pre);
    card.appendChild(el("p", "改完点「重新检查」。署名＝署名，非身分验证。"));
    const recheck = button("重新检查", "btn-secondary");
    recheck.addEventListener("click", async function () { renderReadiness(await computeReadiness()); });
    card.appendChild(recheck);
    c.appendChild(card);
  }
}

// advisory base_url check — NON-authoritative; server's validate_llm_base_url wins
function advisoryBaseUrl() {
  const v = $("settings-base-url").value.trim();
  const out = $("base-url-advisory");
  if (!v) { setText(out, ""); return; }
  let msg = "";
  const m = v.match(/^([a-z]+):\/\//i);
  if (!m) msg = "需以 http(s):// 开头";
  else {
    const scheme = m[1].toLowerCase();
    if (scheme !== "http" && scheme !== "https") msg = "scheme 须是 http 或 https";
    else if (scheme === "http" && !/^https?:\/\/(localhost|127\.|\[::1\])/i.test(v)) msg = "http 仅限本机 loopback，其余须用 https";
    else if (!v.replace(/\/+$/, "").endsWith("/v1")) msg = "须以 /v1 结尾";
  }
  setText(out, msg ? "⚠ " + msg + "（存档时以服务器为准）" : "✓ 看起来没问题");
}

async function openSetup() {
  showView("setup");
  await loadSettings();
  renderReadiness(await computeReadiness());
}

// --- wiring -----------------------------------------------------------------

function bind() {
  $("nav-inbox").addEventListener("click", function () { showView("inbox"); refreshInbox(); });
  $("nav-dashboard").addEventListener("click", openDashboard);
  $("refresh-dashboard").addEventListener("click", openDashboard);
  $("nav-new").addEventListener("click", function () { $("create-job-id").value = ""; openCreate(null); });
  $("nav-setup").addEventListener("click", openSetup);
  $("job-back").addEventListener("click", function () { showView("inbox"); refreshInbox(); });
  $("setup-back").addEventListener("click", function () { showView("inbox"); refreshInbox(); });
  $("refresh-inbox").addEventListener("click", refreshInbox);
  $("btn-save-settings").addEventListener("click", saveSettings);
  $("settings-base-url").addEventListener("input", advisoryBaseUrl);
  bindCreate();
}

async function init() {
  bind();
  const r = await computeReadiness();
  if (r && !r.error && !r.pipelineReady) {
    // first run / unconfigured: open SETUP and focus the first thing to fix
    await loadSettings();
    renderReadiness(r);
    showView("setup");
    const f = $("settings-base-url");
    if (f && f.focus) f.focus();
  } else {
    showView("inbox");
  }
  refreshInbox();
}

// Bootstrap. In pywebview the window.pywebview object + .api are injected AFTER
// the page loads and announced by the `pywebviewready` event — so a parse-time
// `if (window.pywebview)` check ALWAYS fails and would silently never wire the
// bridge. We instead: (1) always subscribe to pywebviewready (covers "not yet
// fired"), (2) immediately boot if the bridge is already present (covers
// "already fired" / fast inject), and (3) fall back to a plain DOM boot for the
// CLI/GUI-parity browser case where pywebview never appears. A run-once guard
// makes every path idempotent. CSP/R41-safe: external JS, event wiring only.
let _booted = false;
function boot() {
  if (_booted) return;
  _booted = true;
  init();
}
function whenDom(fn) {
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", fn, { once: true });
  } else {
    fn();
  }
}
// (1) bridge may arrive later — always listen.
window.addEventListener("pywebviewready", function () { whenDom(boot); }, { once: true });
if (window.pywebview && window.pywebview.api) {
  // (2) bridge already injected (event may have fired before this script ran).
  whenDom(boot);
} else if (typeof window.pywebview === "undefined") {
  // (3) No bridge at parse time. This is EITHER real pywebview whose bridge is
  //     not injected yet (the COMMON case — inject happens after load and fires
  //     pywebviewready, handled by (1)) OR a plain browser where the event never
  //     fires (CLI/GUI parity). Danger: a 0ms fallback runs on DOMContentLoaded,
  //     BEFORE pywebviewready, and would boot init() against a not-yet-ready
  //     bridge — reproducing the exact bug we are fixing. So this timer is a LAST
  //     RESORT only: long enough that a real bridge always wins via (1) first
  //     (boot() is idempotent, so that makes this a harmless no-op). It only
  //     actually boots when pywebview never appears at all (real browser).
  whenDom(function () {
    setTimeout(boot, 3000);
  });
}
