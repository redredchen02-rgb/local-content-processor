/*
 * GUI renderer (Unit 9). HARD INVARIANT (plan R41, redline 3):
 *   - render ONLY via textContent / createElement / setAttribute,
 *   - never the markup-injecting DOM sink, never template-literal HTML, no eval,
 *   - source links are rendered as INERT text (never <a href>, never fetched).
 *
 * The data returned by window.pywebview.api.* is ALREADY sanitized server-side
 * (escape_html / inert_link / sanitize_draft). We still only ever assign it to
 * textContent — defence in depth: even a rendering bug cannot execute markup,
 * and the strict CSP (no inline, object-src 'none') backs this up.
 *
 * The 127.0.0.1-only built-in server load means there is no remote origin; but
 * loopback alone is decorative — this textContent discipline is the real fix.
 */
"use strict";

function api() {
  // window.pywebview.api is injected by the js_api bridge when running in the
  // desktop window. Guard so the page does not throw outside a window.
  return (window.pywebview && window.pywebview.api) || null;
}

function $(id) {
  return document.getElementById(id);
}

function setText(el, value) {
  // Single choke point: everything user-facing goes through textContent.
  el.textContent = value == null ? "" : String(value);
}

function el(tag, text) {
  const node = document.createElement(tag);
  if (text !== undefined) {
    setText(node, text);
  }
  return node;
}

function clear(node) {
  while (node.firstChild) {
    node.removeChild(node.firstChild);
  }
}

function showStatus(msg) {
  setText($("action-status"), msg);
}

function handleResult(result, okMsg) {
  if (result && result.error) {
    showStatus("error (" + result.exit_code + "): " + result.error);
    return false;
  }
  showStatus(okMsg + " -> " + (result && result.state ? result.state : "ok"));
  return true;
}

// --- Summary (home counts) -------------------------------------------------

async function refreshSummary() {
  const a = api();
  if (!a) return;
  const res = await a.summary();
  const body = $("summary-body");
  clear(body);
  if (res.error) {
    showStatus("error: " + res.error);
    return;
  }
  const counts = res.summary || {};
  Object.keys(counts).sort().forEach(function (state) {
    const tr = document.createElement("tr");
    tr.appendChild(el("td", state));
    tr.appendChild(el("td", counts[state]));
    body.appendChild(tr);
  });
}

// --- Worklist --------------------------------------------------------------

async function refreshJobs() {
  const a = api();
  if (!a) return;
  const state = $("state-filter").value || null;
  const res = await a.list_jobs(state);
  const body = $("jobs-body");
  clear(body);
  if (res.error) {
    showStatus("error: " + res.error);
    return;
  }
  (res.jobs || []).forEach(function (job) {
    const tr = document.createElement("tr");
    tr.appendChild(el("td", job.job_id));
    tr.appendChild(el("td", job.state));
    tr.appendChild(el("td", job.review_reason || ""));
    tr.appendChild(el("td", job.updated_at || ""));
    const pick = document.createElement("td");
    const btn = el("button", "select");
    btn.setAttribute("type", "button");
    btn.addEventListener("click", function () {
      setText($("job-id"), "");
      $("job-id").value = job.job_id;
      loadPacket();
    });
    pick.appendChild(btn);
    tr.appendChild(pick);
    body.appendChild(tr);
  });
}

// --- Packet / job detail (rendered field-by-field via textContent) ----------

function renderField(container, label, value) {
  if (value === undefined || value === null || value === "") return;
  const wrap = document.createElement("div");
  wrap.className = "field";
  const strong = el("strong", label + ": ");
  wrap.appendChild(strong);
  wrap.appendChild(el("span", value));
  container.appendChild(wrap);
}

function renderList(container, label, items) {
  if (!items || !items.length) return;
  container.appendChild(el("strong", label + ":"));
  const ul = document.createElement("ul");
  items.forEach(function (it) {
    ul.appendChild(el("li", it));
  });
  container.appendChild(ul);
}

async function loadPacket() {
  const a = api();
  if (!a) return;
  const jobId = $("job-id").value;
  const res = await a.get_packet(jobId);
  const view = $("packet-view");
  clear(view);
  if (res.error) {
    setText(view, "error (" + res.exit_code + "): " + res.error);
    return;
  }
  renderField(view, "Job", res.job_id);
  renderField(view, "State", res.state);
  renderField(view, "Title", res.title);
  renderField(view, "Category", res.category);
  renderField(view, "Intro", res.intro);
  renderList(view, "Quick facts", res.quick_facts);
  renderField(view, "Body", res.event_body);
  if (res.faq && res.faq.length) {
    view.appendChild(el("strong", "FAQ:"));
    res.faq.forEach(function (item) {
      view.appendChild(el("div", "Q: " + item.question));
      view.appendChild(el("div", "A: " + item.answer));
    });
  }
  renderField(view, "Summary", res.summary);
  renderList(view, "Tags", res.tags);
  // Source links: INERT plain text only — never an anchor, never fetched.
  renderList(view, "Source links (inert, not clickable)", res.source_urls);
  renderField(view, "Model", res.model);
  renderField(view, "Finish reason", res.finish_reason);
}

// --- Sign-off / actions ----------------------------------------------------

async function populateReviewers() {
  const a = api();
  if (!a) return;
  const res = await a.reviewers();
  const sel = $("reviewer");
  clear(sel);
  if (res.error) return;
  (res.reviewers || []).forEach(function (name) {
    const opt = document.createElement("option");
    opt.value = name;
    setText(opt, name);
    sel.appendChild(opt);
  });
}

async function loadDisclaimer() {
  const a = api();
  if (!a) return;
  const res = await a.disclaimer();
  $("disclaimer").value = res.disclaimer || "";
}

function bind() {
  $("refresh-summary").addEventListener("click", refreshSummary);
  $("refresh-jobs").addEventListener("click", refreshJobs);
  $("btn-load-packet").addEventListener("click", loadPacket);

  $("btn-crawl").addEventListener("click", async function () {
    const a = api(); if (!a) return;
    const res = await a.create_and_crawl($("job-id").value, $("crawl-url").value);
    handleResult(res, "crawled");
    refreshJobs(); refreshSummary();
  });

  $("btn-ingest").addEventListener("click", async function () {
    const a = api(); if (!a) return;
    const res = await a.ingest_dir($("job-id").value, $("ingest-dir").value);
    handleResult(res, "ingested");
    refreshJobs(); refreshSummary();
  });

  $("btn-process").addEventListener("click", async function () {
    const a = api(); if (!a) return;
    const res = await a.process($("job-id").value, $("proc-title").value, $("proc-dry").checked);
    handleResult(res, "processed");
    refreshJobs(); refreshSummary();
  });

  $("btn-packet").addEventListener("click", async function () {
    const a = api(); if (!a) return;
    const res = await a.make_review_packet($("job-id").value);
    handleResult(res, "review packet");
    refreshJobs(); refreshSummary();
  });

  $("btn-approve").addEventListener("click", async function () {
    const a = api(); if (!a) return;
    const res = await a.approve($("job-id").value, $("reviewer").value);
    handleResult(res, "approved");
    refreshJobs(); refreshSummary();
  });

  $("btn-reject").addEventListener("click", async function () {
    const a = api(); if (!a) return;
    const res = await a.reject($("job-id").value, $("reviewer").value, $("reject-reason").value);
    handleResult(res, "rejected");
    refreshJobs(); refreshSummary();
  });

  $("btn-supersede").addEventListener("click", async function () {
    const a = api(); if (!a) return;
    const res = await a.supersede($("job-id").value, $("new-job-id").value || null);
    handleResult(res, "superseded");
    refreshJobs(); refreshSummary();
  });

  $("btn-backfill").addEventListener("click", async function () {
    const a = api(); if (!a) return;
    const res = await a.backfill($("job-id").value, $("reviewer").value, $("published-url").value, $("attest").checked);
    handleResult(res, "recorded publish");
    refreshJobs(); refreshSummary();
  });
}

function init() {
  bind();
  populateReviewers();
  loadDisclaimer();
  refreshSummary();
  refreshJobs();
}

if (window.pywebview) {
  window.addEventListener("pywebviewready", init);
} else {
  window.addEventListener("DOMContentLoaded", init);
}
