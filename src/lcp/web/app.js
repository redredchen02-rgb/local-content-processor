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
const VIEWS = ["inbox", "job", "setup"];
const NAV = { inbox: "nav-inbox", setup: "nav-setup" };

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
  if (jobId !== currentJobId) { refreshInbox(); return; } // settled while elsewhere
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
  open.addEventListener("click", function () { openJob(job.job_id); });
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
  if (!isError(sumRes)) {
    const counts = sumRes.summary || {};
    const parts = Object.keys(counts).filter(function (k) { return k !== "total"; }).sort()
      .map(function (k) { return lexState(k).title + " " + counts[k]; });
    setText($("inbox-counts"), parts.join(" · "));
  } else {
    setText($("inbox-counts"), "");
  }
}

function loadingRow() {
  const p = el("p", "载入中…");
  p.className = "empty loading";
  return p;
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
    const btn = button(act.label, "btn-primary");
    btn.addEventListener("click", async function () {
      if (!a || pollers[currentJobId]) return;
      setBusy(btn, true);
      const kick = await a.process_async(currentJobId, title.value, dry.checked);
      setBusy(btn, false);
      if (isError(kick)) { renderError($("job-status"), kick); return; }
      enterProgress(currentJobId, dry.checked ? "process_dry" : "process");
    });
    row.appendChild(title); row.appendChild(dryL); row.appendChild(btn);
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
  refreshReadyPill();
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

function openCreate(jobId) {
  showView("job");
  disarmConfirm();
  $("job-create").hidden = false;
  clear($("job-banner")); clear($("job-actions")); clear($("job-packet")); clear($("job-status")); clear($("job-inflight"));
  setText($("job-title"), jobId ? "重新抓取 " + jobId : "新工作");
  if (jobId) $("create-job-id").value = jobId;
  setText($("create-status"), "");
}

function bindCreate() {
  $("create-mode-url").addEventListener("change", function () { $("create-url-row").hidden = false; $("create-dir-row").hidden = true; });
  $("create-mode-dir").addEventListener("change", function () { $("create-url-row").hidden = true; $("create-dir-row").hidden = false; });
  $("btn-create").addEventListener("click", async function () {
    const a = api(); if (!a) return;
    const jobId = $("create-job-id").value.trim();
    if (!jobId) { setText($("create-status"), "请先填工作 id。"); return; }
    const btn = $("btn-create");
    if (!READY.pipelineReady) { setText($("create-status"), "还没设定好模型 endpoint／金鑰——请先到「设定」。"); return; }
    if ($("create-mode-url").checked) {
      const url = $("create-url").value.trim();
      if (!url) { setText($("create-status"), "请填网址。"); return; }
      setBusy(btn, true);
      const kick = await a.create_and_crawl_async(jobId, url); // async -> no freeze
      setBusy(btn, false);
      if (isError(kick)) { setText($("create-status"), inlineError(kick)); return; }
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
      openJob(jobId);
      refreshInbox();
    }
  });
}

// --- SETUP: LLM settings + readiness pill (full readiness lands in P2.1) -----

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
  refreshReadyPill();
}

async function refreshReadyPill() {
  const a = api(); if (!a) return;
  const s = await a.get_settings();
  const r = await a.reviewers();
  READY.pipelineReady = !isError(s) && !!(s.base_url && s.model && s.api_key_set);
  READY.signoffReady = !isError(r) && !!(r.reviewers && r.reviewers.length > 0);
  const ready = READY.pipelineReady && READY.signoffReady;
  const pill = $("ready-pill");
  setText(pill, ready ? "● 就绪" : "⚠ 需设定");
  pill.className = "pill " + (ready ? "pill--ready" : "pill--block");
}

// --- wiring -----------------------------------------------------------------

function bind() {
  $("nav-inbox").addEventListener("click", function () { showView("inbox"); refreshInbox(); });
  $("nav-new").addEventListener("click", function () { $("create-job-id").value = ""; openCreate(null); });
  $("nav-setup").addEventListener("click", function () { showView("setup"); loadSettings(); });
  $("job-back").addEventListener("click", function () { showView("inbox"); refreshInbox(); });
  $("setup-back").addEventListener("click", function () { showView("inbox"); refreshInbox(); });
  $("refresh-inbox").addEventListener("click", refreshInbox);
  $("btn-save-settings").addEventListener("click", saveSettings);
  bindCreate();
}

async function init() {
  bind();
  showView("inbox");
  await refreshReadyPill();
  refreshInbox();
}

if (window.pywebview) {
  window.addEventListener("pywebviewready", init);
} else {
  window.addEventListener("DOMContentLoaded", init);
}
