/*
 * GUI renderer. HARD INVARIANT (R41, redline 3):
 *   - render ONLY via textContent / createElement / setAttribute,
 *   - never the markup-injecting DOM sink, never template-literal HTML, no eval,
 *   - source links are rendered as INERT text (never a live link, never fetched).
 *
 * Data from window.pywebview.api.* is ALREADY sanitized server-side
 * (escape_html / inert_link / sanitize_draft). We still only ever assign it to
 * textContent — defence in depth backed by the strict CSP.
 *
 * UI/UX rebuild: a worklist-centric three-view shell (INBOX / JOB / SETUP),
 * state-gated actions derived from STATE_ACTIONS (lex.js), and machine->human
 * translation via LEX (lex.js). No router, no framework, no build step: views
 * switch via the native `hidden` attribute. lex.js loads before this file and
 * exposes the globals LEX and STATE_ACTIONS.
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
  // exit_code is coarse (many conditions collapse onto 2); intercept the stable
  // server phrases that need a more specific next-step than the generic bucket.
  const raw = String(res.error || "");
  for (let i = 0; i < BACKFILL_PHRASES.length; i++) {
    if (raw.indexOf(BACKFILL_PHRASES[i].needle) !== -1) return BACKFILL_PHRASES[i].msg;
  }
  return null;
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
  // collapsible technical detail (button + addEventListener; never inline onclick)
  const toggle = button("技术细节 ▾", "link-toggle");
  const detail = el("div");
  detail.className = "tech-detail";
  detail.hidden = true;
  detail.appendChild(el("code", "exit " + res.exit_code + ": " + res.error));
  toggle.addEventListener("click", function () {
    detail.hidden = !detail.hidden;
  });
  box.appendChild(toggle);
  box.appendChild(detail);
  container.appendChild(box);
}

// concise one-line error for small status lines (<p>) — no banner, no form wipe
function inlineError(res) {
  const lx = lexExit(res.exit_code);
  return (specialMessage(res) || (lx.title + "：" + lx.why));
}

function renderSuccess(container, title, detail) {
  clear(container);
  const box = el("div");
  box.className = "banner banner--success";
  box.appendChild(el("strong", title));
  if (detail) box.appendChild(el("span", " " + detail));
  container.appendChild(box);
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
  return "inflight"; // unknown future state: keep it visible, not hidden
}

function jobRow(job) {
  const row = el("div");
  row.className = "job-row lane--" + (lexState(job.state).tone || "neutral");
  const lx = lexState(job.state);
  const badge = el("span", lx.glyph + " " + lx.title);
  badge.className = "badge badge--" + (lx.tone || "neutral");
  badge.setAttribute("data-glyph", lx.glyph);
  row.appendChild(el("span", job.job_id)).className = "job-id";
  row.appendChild(badge);
  // human "why short" — for a hold, show the reason label
  let whyShort = lx.title;
  if (job.state === "needs_human_review" && job.review_reason) {
    const r = lexReason(job.review_reason);
    if (r) whyShort = r.label;
  }
  row.appendChild(el("span", whyShort)).className = "job-why";
  if (job.updated_at) row.appendChild(el("span", job.updated_at)).className = "job-when";
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

  const listRes = await a.list_jobs(null);
  if (isError(listRes)) { renderError(bandsEl, listRes); setText($("inbox-counts"), ""); return; }
  const jobs = listRes.jobs || [];

  // bucket
  const buckets = {};
  BANDS.forEach(function (b) { buckets[b.key] = []; });
  jobs.forEach(function (j) { buckets[bandFor(j.state)].push(j); });

  BANDS.forEach(function (b) {
    const rows = buckets[b.key];
    const section = el("section");
    section.className = "band band--" + b.key;
    const head = el("div");
    head.className = "band-head";
    head.appendChild(el("strong", b.label + "（" + rows.length + "）"));
    const body = el("div");
    body.className = "band-body";
    // "closed" band starts collapsed; "stopped" never collapses.
    let collapsed = b.collapsed && rows.length > 0;
    body.hidden = collapsed;
    if (b.key === "closed" && rows.length > 0) {
      const toggle = button(collapsed ? "展开" : "收起", "link-toggle");
      toggle.addEventListener("click", function () {
        collapsed = !collapsed;
        body.hidden = collapsed;
        setText(toggle, collapsed ? "展开" : "收起");
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

  // counts line (summary), 'total' stripped
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

// --- JOB workspace ----------------------------------------------------------

async function openJob(jobId) {
  const a = api();
  if (!a) return;
  currentJobId = jobId;
  showView("job");
  $("job-create").hidden = true;
  setText($("job-status"), "");
  setText($("job-title"), "工作 " + jobId);

  // state comes from the worklist record, never the packet (pre-draft states
  // return an error from get_packet).
  const listRes = await a.list_jobs(null);
  let rec = null;
  if (!isError(listRes)) {
    (listRes.jobs || []).forEach(function (j) { if (j.job_id === jobId) rec = j; });
  }
  if (rec === null) {
    clear($("job-banner"));
    renderError($("job-banner"), { error: "找不到这个工作：" + jobId, exit_code: 2 });
    clear($("job-actions"));
    clear($("job-packet"));
    return;
  }
  renderBanner(rec.state, rec.review_reason);
  const reviewers = await a.reviewers();
  await renderActions(rec.state, rec.review_reason, reviewers);
  await renderPacket(jobId);
}

function renderBanner(state, reason) {
  const c = $("job-banner");
  clear(c);
  const lx = lexState(state);
  const box = el("div");
  box.className = "banner banner--state tone--" + (lx.tone || "neutral");
  const head = el("div");
  head.className = "banner-head";
  const badge = el("span", lx.glyph + " " + lx.title);
  badge.className = "badge badge--" + (lx.tone || "neutral");
  badge.setAttribute("data-glyph", lx.glyph);
  head.appendChild(badge);
  box.appendChild(head);
  box.appendChild(el("p", lx.why));
  if (lx.next) box.appendChild(el("p", "你可以：" + lx.next));
  if (state === "needs_human_review" && reason) {
    const r = lexReason(reason);
    if (r) {
      const rb = el("div");
      rb.className = "reason-note";
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

// empty reviewer whitelist -> replace sign-off actions with an onboarding banner
function reviewersEmpty(reviewers) {
  return !reviewers || isError(reviewers) || !(reviewers.reviewers && reviewers.reviewers.length);
}
function reviewerOnboarding(container) {
  const box = el("div");
  box.className = "banner banner--attention";
  box.appendChild(el("strong", "还不能签核"));
  box.appendChild(el("p", "尚未设定审阅者名单。请在 config.yaml 的 publisher.reviewers 加入名字（一次性技术设定）——这是合规归属决定，不在此 GUI 修改。"));
  container.appendChild(box);
}

async function renderActions(state, reason, reviewers) {
  const c = $("job-actions");
  clear(c);
  const actions = stateActions(state);
  if (actions.length === 0) return; // terminal / processing: no buttons (fail-closed)

  const SIGNOFF = { approve: 1, reject: 1, supersede: 0, backfill: 1, openHold: 1 };
  const needsReviewer = actions.some(function (act) { return SIGNOFF[act.method]; });
  if (needsReviewer && reviewersEmpty(reviewers)) {
    reviewerOnboarding(c);
    // supersede does not need a reviewer; still offer it so the operator is not stuck.
    if (actions.some(function (act) { return act.method === "supersede"; })) {
      c.appendChild(supersedeRow());
    }
    return;
  }

  actions.forEach(function (act) {
    const row = buildActionRow(act, reviewers);
    if (row) c.appendChild(row);
  });
}

function supersedeRow() {
  const row = el("div");
  row.className = "action-row";
  const inp = el("input");
  inp.type = "text";
  inp.setAttribute("placeholder", "接手的新 job id（可留空）");
  const btn = button("作废 Supersede", "btn-danger");
  btn.addEventListener("click", async function () {
    const a = api();
    if (!a) return;
    const res = await a.supersede(currentJobId, inp.value || null);
    afterAction(res, "已作废");
  });
  row.appendChild(el("label", "作废："));
  row.appendChild(inp);
  row.appendChild(btn);
  return row;
}

function buildActionRow(act, reviewers) {
  const row = el("div");
  row.className = "action-row";

  if (act.method === "process") {
    const title = el("input");
    title.type = "text";
    title.setAttribute("placeholder", "标题（可留空）");
    const dry = el("input");
    dry.type = "checkbox";
    const dryLabel = el("label");
    dryLabel.appendChild(dry);
    dryLabel.appendChild(el("span", " 安全预览"));
    const btn = button(act.label, "btn-primary");
    btn.addEventListener("click", async function () {
      const a = api();
      if (!a) return;
      const res = await a.process(currentJobId, title.value, dry.checked);
      afterAction(res, act.label);
    });
    row.appendChild(title);
    row.appendChild(dryLabel);
    row.appendChild(btn);
    return row;
  }

  if (act.method === "make_review_packet") {
    const note = el("span", "（冻结后不能改，只能退件／作废）");
    note.className = "hint";
    const btn = button(act.label, "btn-primary");
    btn.addEventListener("click", async function () {
      const a = api();
      if (!a) return;
      const res = await a.make_review_packet(currentJobId);
      afterAction(res, act.label);
    });
    row.appendChild(btn);
    row.appendChild(note);
    return row;
  }

  if (act.method === "openCreate") {
    const btn = button(act.label, "btn-primary");
    btn.addEventListener("click", function () { openCreate(currentJobId); });
    row.appendChild(btn);
    return row;
  }

  if (act.method === "openHold") {
    return holdPanel(reason, reviewers);
  }

  if (act.method === "approve") {
    const sel = reviewerSelect(reviewers);
    const btn = button("核可 Approve", "btn-primary");
    btn.addEventListener("click", async function () {
      const a = api();
      if (!a) return;
      const res = await a.approve(currentJobId, sel.value);
      afterAction(res, "已核可");
    });
    row.appendChild(el("label", "审阅者："));
    row.appendChild(sel);
    row.appendChild(btn);
    row.appendChild(disclaimerNote());
    return row;
  }

  if (act.method === "reject") {
    const sel = reviewerSelect(reviewers);
    const reason = el("input");
    reason.type = "text";
    reason.setAttribute("placeholder", "退件理由（必填）");
    const btn = button("退件 Reject", "btn-danger");
    btn.addEventListener("click", async function () {
      const a = api();
      if (!a) return;
      if (!reason.value.trim()) { setText($("job-status"), "请先填退件理由。"); return; }
      const res = await a.reject(currentJobId, sel.value, reason.value);
      afterAction(res, "已退件");
    });
    row.appendChild(sel);
    row.appendChild(reason);
    row.appendChild(btn);
    return row;
  }

  if (act.method === "supersede") {
    return supersedeRow();
  }

  if (act.method === "backfill") {
    const sel = reviewerSelect(reviewers);
    const url = el("input");
    url.type = "text";
    url.setAttribute("placeholder", "已上架的网址");
    const attest = el("input");
    attest.type = "checkbox";
    const attestLabel = el("label");
    attestLabel.appendChild(attest);
    attestLabel.appendChild(el("span", " 我确认上架内容＝已签核版本"));
    const btn = button("回填网址并具结", "btn-primary");
    btn.addEventListener("click", async function () {
      const a = api();
      if (!a) return;
      const res = await a.backfill(currentJobId, sel.value, url.value, attest.checked);
      afterAction(res, "已登记上架");
    });
    const honesty = el("p", LEX.honesty.backfill_attest);
    honesty.className = "hint";
    row.appendChild(el("label", "审阅者："));
    row.appendChild(sel);
    row.appendChild(url);
    row.appendChild(attestLabel);
    row.appendChild(btn);
    row.appendChild(honesty);
    return row;
  }

  return null;
}

function disclaimerNote() {
  const box = el("div");
  box.className = "disclaimer";
  const a = api();
  if (a) {
    a.disclaimer().then(function (res) {
      if (res && res.disclaimer) setText(box, res.disclaimer);
    });
  }
  return box;
}

// hold-resolution sub-panel (reason-branched), mirrors signoff.resolve()
function holdPanel(reason, reviewers) {
  const panel = el("div");
  panel.className = "hold-panel";
  const sel = reviewerSelect(reviewers);
  const lr = lexReason(reason);
  if (lr) panel.appendChild(el("p", lr.why));

  let relint = false;
  if (reason === "grounding") {
    const recheck = el("label");
    const rb = el("input"); rb.type = "radio"; rb.name = "hold-mode"; rb.checked = true;
    recheck.appendChild(rb); recheck.appendChild(el("span", " 重新检查（核对过出处就选这个，通过自动放行）"));
    const overrideL = el("label");
    const ob = el("input"); ob.type = "radio"; ob.name = "hold-mode";
    overrideL.appendChild(ob); overrideL.appendChild(el("span", " 人工放行（须写理由）"));
    panel.appendChild(recheck);
    panel.appendChild(overrideL);
    rb.addEventListener("change", function () { relint = rb.checked; updateReasonReq(); });
    ob.addEventListener("change", function () { relint = !ob.checked; updateReasonReq(); });
    relint = true;
  }

  const reasonInput = el("input");
  reasonInput.type = "text";
  reasonInput.setAttribute("placeholder", "理由");
  if (reason === "dedup" || reason === "risk") {
    const note = el("p", lexReason(reason).note);
    note.className = "honesty-callout";
    panel.appendChild(note);
  }
  function updateReasonReq() {
    reasonInput.hidden = relint; // grounding+recheck needs no reason
  }
  updateReasonReq();

  const btn = button("清除 hold → 草稿完成", "btn-primary");
  btn.addEventListener("click", async function () {
    const a = api();
    if (!a) return;
    if (!relint && !reasonInput.value.trim()) { setText($("job-status"), "人工放行须写理由。"); return; }
    const res = await a.resolve(currentJobId, sel.value, relint, relint ? null : reasonInput.value);
    afterAction(res, "已处理 hold");
  });

  panel.appendChild(el("label", "审阅者："));
  panel.appendChild(sel);
  panel.appendChild(reasonInput);
  panel.appendChild(btn);
  return panel;
}

async function afterAction(res, okLabel) {
  const status = $("job-status");
  if (isError(res)) {
    renderError(status, res);
    return;
  }
  const newState = res.state ? lexState(res.state).title : "完成";
  renderSuccess(status, okLabel, "→ " + newState);
  if (currentJobId) await openJob(currentJobId);
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
  packetList(card, "来源连结（惰性纯文字，不可点、不会被开启）", res.source_urls);
  packetField(card, "Model", res.model);
  packetField(card, "Finish reason", res.finish_reason);
  view.appendChild(card);
}

function packetField(container, label, value) {
  if (value === undefined || value === null || value === "") return;
  const wrap = el("div");
  wrap.className = "field";
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

// --- create mode ------------------------------------------------------------

function openCreate(jobId) {
  showView("job");
  $("job-create").hidden = false;
  clear($("job-banner"));
  clear($("job-actions"));
  clear($("job-packet"));
  setText($("job-title"), jobId ? "重新抓取 " + jobId : "新工作");
  if (jobId) $("create-job-id").value = jobId;
  setText($("create-status"), "");
}

function bindCreate() {
  $("create-mode-url").addEventListener("change", function () {
    $("create-url-row").hidden = false;
    $("create-dir-row").hidden = true;
  });
  $("create-mode-dir").addEventListener("change", function () {
    $("create-url-row").hidden = true;
    $("create-dir-row").hidden = false;
  });
  $("btn-create").addEventListener("click", async function () {
    const a = api();
    if (!a) return;
    const jobId = $("create-job-id").value.trim();
    if (!jobId) { setText($("create-status"), "请先填工作 id。"); return; }
    const useUrl = $("create-mode-url").checked;
    let res;
    if (useUrl) {
      const url = $("create-url").value.trim();
      if (!url) { setText($("create-status"), "请填网址。"); return; }
      res = await a.create_and_crawl(jobId, url);
    } else {
      const dir = $("create-dir").value.trim();
      if (!dir) { setText($("create-status"), "请填资料夹路径。"); return; }
      res = await a.ingest_dir(jobId, dir);
    }
    if (isError(res)) { setText($("create-status"), inlineError(res)); return; }
    openJob(jobId);
    refreshInbox();
  });
}

// --- SETUP: LLM settings + minimal readiness pill ---------------------------

function setKeyState(isSet) {
  setText($("settings-key-state"), isSet ? "key：已设" : "key：未设");
}

async function loadSettings() {
  const a = api();
  if (!a) return;
  const res = await a.get_settings();
  if (isError(res)) { setText($("settings-status"), inlineError(res)); return; }
  $("settings-base-url").value = res.base_url || "";
  $("settings-model").value = res.model || "";
  setKeyState(res.api_key_set);
}

async function saveSettings() {
  const a = api();
  if (!a) return;
  const res = await a.save_settings(
    $("settings-base-url").value,
    $("settings-model").value,
    $("settings-api-key").value
  );
  $("settings-api-key").value = ""; // clear the secret from the DOM regardless
  if (isError(res)) { setText($("settings-status"), inlineError(res)); return; }
  setKeyState(res.api_key_set);
  setText($("settings-status"), "已储存" + (res.key_saved ? "（api_key 已更新）" : ""));
  refreshReadyPill();
}

async function refreshReadyPill() {
  const a = api();
  if (!a) return;
  const s = await a.get_settings();
  const r = await a.reviewers();
  const pill = $("ready-pill");
  const ready = !isError(s) && !!(s.base_url && s.model && s.api_key_set) &&
    !isError(r) && (r.reviewers && r.reviewers.length > 0);
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

function init() {
  bind();
  showView("inbox");
  refreshReadyPill();
  refreshInbox();
}

if (window.pywebview) {
  window.addEventListener("pywebviewready", init);
} else {
  window.addEventListener("DOMContentLoaded", init);
}
