# LCP Operator GUI — Visual Elevation Plan

**Date:** 2026-06-17
**Scope:** `src/lcp/web/app.css` (+ a few R41-legal `app.js` `createElement` wrappers)
**Source:** synthesis of five design-lens critiques (composition, typography, surface/depth, interaction, constraint/trust)

---

## Opening summary

Today the operator GUI reads as a "colored wireframe": correct semantics and a careful state-color
system, but **zero surface containment** — bands, rows, the form and the job workspace all float on
raw white with only 1px hairlines, the page title and section headings collapse to the same
`--fs-600` size, and the inbox counts render as one run-on `·`-joined grey string that looks like
console output. The fix is almost entirely **pure CSS on hooks the renderer already emits**: cards
+ elevation tokens for containment, re-binding the already-defined-but-unused `--fs-700`/`--fs-900`
type steps for hierarchy, a left family-accent on each band, a width cap to kill the dead-white
right/bottom, and hover/focus affordance so rows read as clickable. Only **two** changes touch
`app.js`, and both stay strictly `createElement` + `textContent` + `className` (R41): splitting the
counts string into per-state chips, and making the whole job row keyboard-focusable.

Nothing here adds a web font, inline style, `innerHTML`, CDN, dependency, build step, or router; the
strict CSP, the trust posture (always-visible honesty note, the six state-color meanings, the frozen
2px-indigo skin, inert source links, "parked ≠ failure"), and accessibility (≥4.5:1, badge
`currentColor` borders, `:focus-visible` rings, reduced-motion, text-label-on-every-color) are all
preserved verbatim.

---

## North star

> **Turn the colored wireframe into a built, trustworthy desktop tool — using only containment,
> type hierarchy, and depth, never new meaning.**

A region should announce *what kind of thing it is* (a group of work, a single job, a transient
decision, a config form) by its **surface and elevation**, and the eye should land in a clear
order **title → section → band → row → metadata** by **size**, not by hunting. Every lift is
decorative or structural — it must not invent a hue, recolor a state, weaken the honesty note, or
reduce contrast. When in doubt, prefer the pure-CSS form on an existing class hook over any DOM change.

### Ground-truth hooks (verified in the codebase)

These are confirmed in `src/lcp/web/` so every selector below is real:

- **Bands** are `<section class="band band--KEY">` where `KEY ∈ {attention, stopped, inflight, closed}`
  only (`app.js:318` BANDS def, className at `app.js:387`). ⚠️ Lens-3's `.band--frozen/.band--ready/.band--done`
  selectors **do not exist** and are corrected to the real four keys throughout this plan.
- **Band header** is `<div class="band-head"><strong>…（n）</strong></div>` (`app.js:388-390`).
- **Job rows** are `<div class="job-row lane--TONE">` with an inset 4px lane shadow (`app.js:347`, `app.css:91-98`),
  containing `.job-id` (mono), `.job-why`, `.job-when`, and a trailing `.btn-secondary "打开 ›"` (`app.js:359`).
- **Counts** is a single `<p id="inbox-counts" class="counts">` whose text is `parts.join(" · ")` (`app.js:423-425`).
- **Readiness rows** are `<div class="ready-row">` inside `<div class="ready-list">` (`app.js:901,929`).
- **Setup form** uses `<div class="row">` with `input{flex:1;min-width:11rem}` inside `<section id="settings">` (`index.html:84`).
- **Single `<main>`** wraps all three views (`index.html:32`). `<header>` is `position:sticky` with the honesty `.note` (`index.html:29`).
- Helpers `el()` / `setText()` / `clear()` are **textContent + createElement + removeChild only** (`app.js:30-40`) — R41-safe.
- The `@media (prefers-reduced-motion: reduce)` block exists at `app.css:178`; the 40rem narrow breakpoint at `app.css:162`.

---

## Roadmap

Legend — **pure-CSS?** Y = `app.css` only, no DOM change · N = needs an R41-legal `app.js` `createElement` edit.
Each item lists: file · pure-CSS? · concrete change · **impact / effort / risk**.

### P0 — highest impact-to-effort, lowest-risk quick wins (do these first)

These most directly kill the "colored wireframe" feel. All but P0-7 are pure CSS on existing hooks.

#### P0-1 — App canvas + capped paper surface (kills the floating column & dead white)
- **File:** `app.css` · **pure-CSS:** Y
- **Change:** Tint the window and float a contained surface on it. Add tokens at `:root`:
  `--canvas:#f3f4f7;` and the shadow tokens from P0-6. Set `body{ background:var(--canvas); }` and
  remove `max-width` from `body` so the tint goes edge-to-edge. Style the existing single `<main>`:
  `main{ display:block; max-width:60rem; margin:0 auto; background:var(--paper); border:1px solid var(--line);
  border-radius:var(--radius-md); padding:var(--sp-6); min-height:calc(100vh - 8rem); box-shadow:var(--shadow-md); }`.
  `min-height` converts the empty lower 60% into the card's own padding instead of raw void.
- **Why it lifts:** the white surfaces now read as *raised paper on a tinted desk* — the single biggest "wireframe → app" cue.
- **Impact:** high · **Effort:** S · **Risk:** low. Sticky header still works (main is in normal flow).
  `ink-900` on `#f3f4f7` ≈ 15:1, safe. Must degrade at 40rem (see P0-8).

#### P0-2 — Promote inbox bands to cards with a per-family left accent
- **File:** `app.css` · **pure-CSS:** Y
- **Change:** Replace the hairline-only `.band{border-top:…}` with a contained card carrying its
  family accent:
  `.band{ background:var(--paper); border:1px solid var(--line); border-left:4px solid var(--line);
  border-radius:var(--radius-md); padding:var(--sp-4); margin-bottom:var(--sp-5); box-shadow:var(--shadow-sm); }`
  (drop `border-top`). Key the accent by the **real** band classes:
  `.band--attention{border-left-color:var(--c-attention-bd)}` ·
  `.band--stopped{border-left-color:var(--c-stop-bd)}` ·
  `.band--inflight{border-left-color:var(--c-progress-bd)}` ·
  `.band--closed{border-left-color:var(--c-void-bd)}`.
  Give the header presence:
  `.band-head{ margin:calc(-1*var(--sp-4)) calc(-1*var(--sp-4)) var(--sp-3); padding:var(--sp-2) var(--sp-4);
  background:var(--paper-2); border-bottom:1px solid var(--line); border-radius:var(--radius-md) var(--radius-md) 0 0; }`.
- **Why it lifts:** introduces surfaces, depth, AND structure-level family color in one move — the four bands stop blurring into one ribbon.
- **Impact:** high · **Effort:** S · **Risk:** low. Accent reuses existing `--c-*-bd` family tokens (no new hue, no remap);
  text count label stays so color is never the sole signal. `attention` keeps AMBER (parked ≠ failure).

#### P0-3 — Activate the type scale: title › heading › band › row
- **File:** `app.css` · **pure-CSS:** Y
- **Change:** Re-bind the already-defined-but-**unused** `--fs-700`/`--fs-900` tokens (no new tokens, no JS):
  `.topbar h1{ font-size:var(--fs-700); letter-spacing:-.01em; }` (persistent app anchor) ·
  `h2{ font-size:var(--fs-700); }` (per-view heading) ·
  `.band-head strong{ font-size:var(--fs-600); font-weight:var(--fw-bold); }` (a clear step above 1rem rows) ·
  `h1,h2,h3{ line-height:var(--lh-tight); }`. Reserve `--fs-900` only for an optional empty-state hero (P2).
- **Why it lifts:** the whole screen currently sits in a 0.875–1.15rem grey band with no anchor; this creates the missing 4-level scan order purely from existing tokens.
- **Impact:** high · **Effort:** S · **Risk:** low. Larger sticky `h1` slightly raises header height — keep `--fs-700` (not `--fs-900`)
  so `.topbar` flex-wrap doesn't push the pill to a second line in a ~700px-tall window.

#### P0-4 — Split job-id and why-phrase into two registers
- **File:** `app.css` · **pure-CSS:** Y
- **Change:** Demote the machine id, promote the human reason (both classes already exist on `jobRow()` output, `app.js:348/356`):
  `.job-id{ font-size:var(--fs-300); color:var(--ink-500); letter-spacing:.02em; }` (small, grey, tracked mono) ·
  `.job-why{ color:var(--ink-900); font-weight:var(--fw-medium); }` (primary readable element).
- **Why it lifts:** today the identifier and the reason share the same 1rem weight; now badge + why scan first, id + timestamp recede as metadata.
- **Impact:** high · **Effort:** S · **Risk:** low. `--ink-500` on white ≈ 6:1, safe. Why-phrase stays 1rem so it doesn't compete with the now-larger band header.

#### P0-5 — Restyle the counts line as a contained summary strip (pure-CSS now)
- **File:** `app.css` · **pure-CSS:** Y
- **Change:** Turn the debug-like run-on into a deliberate bottom-anchored strip:
  `.counts{ margin-top:var(--sp-6); padding:var(--sp-3) var(--sp-4); background:var(--paper-2);
  border:1px solid var(--line); border-radius:var(--radius-md); color:var(--ink-700);
  display:flex; flex-wrap:wrap; gap:var(--sp-1) var(--sp-3); line-height:var(--lh-tight); font-size:var(--fs-400); }`.
  The existing `" · "` join still renders but now reads as a summary footer, not console output.
- **Why it lifts:** removes the single most "unfinished" element in the lower half with zero JS risk; P1-1 then upgrades it to chips.
- **Impact:** medium · **Effort:** S · **Risk:** low. Nothing semantic changes — same string, same order, same state titles.

#### P0-6 — Add elevation tokens so containment reads as depth
- **File:** `app.css` · **pure-CSS:** Y
- **Change:** Add at `:root`:
  `--shadow-sm:0 1px 2px rgba(26,29,33,.06);` and
  `--shadow-md:0 1px 3px rgba(26,29,33,.07), 0 6px 16px rgba(26,29,33,.05);`.
  Apply `--shadow-sm` to `.band`, `.handoff`, `.packet`, `.banner`; `--shadow-md` to `main`.
- **Why it lifts:** borders alone read as wireframe outlines; low-alpha warm-ink shadows are the cue that separates "finished product" from "schematic".
- **Impact:** low–medium · **Effort:** S · **Risk:** low. Decorative-only, static (no reduced-motion impact), never a sole signal.
  Frozen packet keeps a slightly indigo-tinted shadow (P1-6) to reinforce "sealed artifact" without changing its border.

#### P0-7 — Make the whole job row a click + keyboard target
- **File:** `app.js` · **pure-CSS:** N (R41-legal)
- **Change:** In `jobRow()` (`app.js:344`) add to the row element:
  `row.setAttribute('role','button'); row.setAttribute('tabindex','0');
  row.addEventListener('click', function(){ openJob(job.job_id); });
  row.addEventListener('keydown', function(e){ if(e.key==='Enter'||e.key===' '){ e.preventDefault(); openJob(job.job_id); } });`.
  Keep the visible `"打开 ›"` cue text (color is not the only signal); in `app.css` add
  `.job-row{ cursor:pointer; } .job-row:focus-visible{ outline:3px solid var(--focus-ring); outline-offset:2px; }`.
- **Why it lifts:** rows currently look like static debug output with one tiny right-side button; this makes the whole card the affordance and gives keyboard users row context.
- **Impact:** high · **Effort:** M · **Risk:** low. The inner `"打开 ›"` button calls the same idempotent `openJob`, so the
  nested control double-fire is harmless (just navigates). Uses only `setAttribute`/`addEventListener` — no `innerHTML`. Verify focus order reaches each row once.

#### P0-8 — Narrow-window degradation for every width cap (guards P0-1/P1-3)
- **File:** `app.css` · **pure-CSS:** Y
- **Change:** Extend the existing `@media (max-width:40rem)` block (`app.css:162`):
  `main{ border:none; border-radius:0; box-shadow:none; padding:var(--sp-4); min-height:0; }` ·
  `#settings .row{ grid-template-columns:1fr; }` (if the P1-3 grid lands) ·
  `#settings input[type=text], #settings input[type=password]{ max-width:none; }`.
- **Why it lifts:** keeps the current edge-to-edge stacked WKWebView path intact; the desktop card treatment is additive only on wide windows.
- **Impact:** medium (correctness) · **Effort:** S · **Risk:** low. This is the one item that *must* ship alongside P0-1 and P1-3.

---

### P1 — structure & interaction (after P0 lands)

#### P1-1 — Counts as per-state chips (the one legitimate inbox JS change)
- **File:** `app.js` + `app.css` · **pure-CSS:** N (R41-legal)
- **Change:** In `refreshInbox()` (`app.js:423-425`) replace the `join`/`setText` with a `clear()` + `createElement` loop using the existing `el()` helper:
  `const c=$('inbox-counts'); clear(c); Object.keys(counts).filter(k=>k!=='total').sort().forEach(function(k){ const chip=el('span', lexState(k).title+' '+counts[k]); chip.className='count-chip'; c.appendChild(chip); });`.
  In `app.css`: `.count-chip{ display:inline-flex; gap:var(--sp-1); align-items:baseline; padding:1px var(--sp-2);
  border:1px solid var(--line); border-radius:var(--radius-pill); background:var(--paper); font-size:var(--fs-300); }`.
- **Why it lifts:** numbers become scannable pills instead of a run-on string; this is the only thing CSS truly cannot do (can't split one text node).
- **Impact:** medium–high · **Effort:** M · **Risk:** low. Must stay `el()`+`textContent` (never `innerHTML`); same `lexState(k).title`
  strings and same sort order, so semantics are untouched. Keep chips visually subordinate (neutral grey) so they don't imply actionable state.

#### P1-2 — Job rows read as hoverable inset surfaces
- **File:** `app.css` · **pure-CSS:** Y
- **Change:** Give rows a surface + quiet separation inside the band card:
  `.job-row{ background:var(--paper); border:1px solid transparent; border-radius:var(--radius-sm);
  padding:var(--sp-2) var(--sp-3); transition:box-shadow .12s, border-color .12s; }`.
  Preserve the lane bar by **threading the lane color through a custom prop** so hover stays DRY:
  set `--lane:var(--c-neutral-bd)` on `.job-row` and `--lane:var(--c-stop-bd)` etc. in each existing `.lane--*` rule,
  then `box-shadow:inset 4px 0 0 var(--lane), var(--shadow-sm);` once. Hover:
  `.job-row:hover{ border-color:var(--ink-500); box-shadow:inset 4px 0 0 var(--lane), var(--shadow-md); }`.
  `.job-row + .job-row{ /* optional hairline */ }` only if rows need separation.
- **Why it lifts:** rows feel interactive (pairs with P0-7) without restating the dual shadow per lane.
- **Impact:** medium · **Effort:** M · **Risk:** medium. The custom-prop approach avoids the verbose "restate both shadows per lane"
  trap; verify all 7 `.lane--*` set `--lane`. Add `.job-row{ transition:none; }` under the reduced-motion block (P1-7).

#### P1-3 — Constrain the SETUP form to a readable measure on its own card
- **File:** `app.css` · **pure-CSS:** Y
- **Change:** Card the existing `#settings` and cap field width:
  `#settings{ max-width:34rem; border:1px solid var(--line); border-radius:var(--radius-md); background:var(--paper-2);
  padding:var(--sp-5); margin-top:var(--sp-4); }` ·
  `#settings input[type=text], #settings input[type=password]{ max-width:22rem; }`.
  Optional clean rhythm scoped so it can't touch the create-job `.row`:
  `#settings .row{ display:grid; grid-template-columns:10rem 1fr; align-items:center; gap:var(--sp-3); margin:var(--sp-3) 0; }`.
- **Why it lifts:** today inputs stretch to ~1024px, the widest emptiest line in the app; this caps them to a scannable measure.
- **Impact:** medium · **Effort:** S · **Risk:** low. `max-width(22rem) > min-width(11rem)` so fields never clip on narrow; collapses to one column at 40rem (P0-8). No trust wording touched.

#### P1-4 — Nav as a segmented control with a real active state
- **File:** `app.css` · **pure-CSS:** Y
- **Change:** Style the existing `.nav` container (no DOM change):
  `.nav{ background:var(--paper-2); border:1px solid var(--line); border-radius:var(--radius-md); padding:2px; gap:0; }` ·
  `.nav button{ border:1px solid transparent; background:none; border-radius:var(--radius-sm); padding:var(--sp-1) var(--sp-3); }` ·
  `.nav button:hover{ background:var(--paper); }`. Replace the underline active rule with a raised pill:
  `.nav button[aria-current="page"]{ background:var(--paper); border-color:var(--line); box-shadow:var(--shadow-sm);
  font-weight:var(--fw-bold); text-decoration:none; }`.
- **Why it lifts:** the three nav buttons stop reading as identical default buttons; the active tab gets a non-color cue (raised fill + bold).
- **Impact:** medium · **Effort:** S · **Risk:** low. Drops `text-decoration:underline` but `aria-current="page"` (kept) + bold + raised fill
  are sufficient non-color cues; `:focus-visible` ring stays. **No brand accent hue** — chrome stays neutral paper to avoid a state collision (see rejected ideas).

#### P1-5 — Differentiate the button system with hover/active states
- **File:** `app.css` · **pure-CSS:** Y
- **Change:** Shared `button{ transition:background-color .12s, border-color .12s, box-shadow .12s; }`. Then:
  `.btn-primary{ box-shadow:0 1px 2px rgba(20,81,41,.25); } .btn-primary:hover{ background:var(--c-go-tx); border-color:var(--c-go-tx); } .btn-primary:active{ box-shadow:none; }` ·
  `.btn-secondary{ background:var(--paper-2); } .btn-secondary:hover{ background:var(--paper); border-color:var(--ink-500); }` ·
  `.btn-danger:hover{ background:var(--c-stop-bg); } .btn-danger.is-armed:hover{ background:var(--c-stop-tx); }` ·
  `.link-toggle:hover{ text-decoration:underline; }`.
- **Why it lifts:** the four button families are currently visually indistinguishable browser defaults; states give primary lead and secondary/link recede.
- **Impact:** medium–high · **Effort:** S · **Risk:** low. Primary hover green-bd→green-tx (#145129) on white stays >7:1.
  `button[disabled]{opacity:.45}` still wins (independent). Danger armed keeps white-on-red. Color/shadow only → no reduced-motion concern.

#### P1-6 — Subtle depth on banners, packet, and frozen trust skin
- **File:** `app.css` · **pure-CSS:** Y
- **Change:** `.banner, .packet, .handoff, .confirm-tray, .hold-panel{ box-shadow:var(--shadow-sm); }`. For the frozen review packet,
  keep its 2px indigo border but let elevation reinforce "sealed":
  `.packet.is-frozen{ box-shadow:0 2px 10px rgba(106,90,205,.18); }` and `.frozen-ribbon{ box-shadow:var(--shadow-sm); }`.
- **Why it lifts:** the in-DOM decision/trust surfaces read as built panels; the frozen skin gains weight without any semantic change.
- **Impact:** medium · **Effort:** S · **Risk:** low. Frozen shadow uses the indigo family tint only — border color/weight and immutability semantics untouched.
  The honesty `.note` is **not** elevated (must not imply dismissibility).

#### P1-7 — Reduced-motion + density calibration
- **File:** `app.css` · **pure-CSS:** Y
- **Change:** Extend the existing `@media (prefers-reduced-motion:reduce)` block (`app.css:178`):
  add `.job-row, .btn-secondary, .btn-primary, button{ transition:none; }`. Calibrate rhythm:
  `.view-head{ margin-bottom:var(--sp-5); padding-bottom:var(--sp-3); border-bottom:1px solid var(--line); }` ·
  `.band-body{ margin-top:0; display:flex; flex-direction:column; gap:var(--sp-2); }` (then `.job-row{ margin:0; }` to avoid double-spacing) ·
  `.ready-row{ padding:var(--sp-2) 0; }`.
- **Why it lifts:** replaces uniform cramped `sp-1` micro-spacing with deliberate tiers; honors constraint 6 for all the new hover transitions.
- **Impact:** medium · **Effort:** S · **Risk:** low. Pick gap **or** margin (not both). Verify new padding doesn't overflow at 40rem.

---

### P2 — polish (nice-to-have, after P0+P1)

#### P2-1 — Empty/loading states get a contained, finished feel + spinner reuse
- **File:** `app.css` (+ optional `app.js`) · **pure-CSS:** Y for the box, N for the spinner glyph
- **Change:** `.empty{ color:var(--ink-500); font-style:normal; background:var(--paper-2); border:1px dashed var(--line);
  border-radius:var(--radius-md); padding:var(--sp-5); text-align:center; }`. Optional R41-legal: in `loadingRow()` (`app.js:431`)
  build the existing `.spin` glyph via `el('span')`+`className='spin'` prepended to the "载入中…" text (spinner already has a reduced-motion off-switch).
- **Why it lifts:** "没有待办。" / "载入中…" stop looking like a failed page in the big white lower half; the cleared-inbox copy stays untouched.
- **Impact:** medium · **Effort:** S · **Risk:** low. Keep the "+ 新工作" wording verbatim. Pure-CSS box alone suffices; spinner reuse is the optional upgrade.

#### P2-2 — Readiness checklist + pill as a contained status panel
- **File:** `app.css` · **pure-CSS:** Y
- **Change:** `.ready-list{ background:var(--paper); border:1px solid var(--line); border-radius:var(--radius-md);
  box-shadow:var(--shadow-sm); padding:var(--sp-2) var(--sp-4); }` ·
  `.ready-row{ display:flex; align-items:baseline; gap:var(--sp-2); padding:var(--sp-3) 0; border-bottom:1px solid var(--line); }`
  `.ready-row:last-child{ border-bottom:none; }` · `.pill{ box-shadow:var(--shadow-sm); }`.
- **Why it lifts:** the SETUP checklist reads as a status panel, not plain text lines.
- **Impact:** medium · **Effort:** S · **Risk:** low. The per-row status pills are `.badge--go/--neutral/--caution` (`app.js:903-905`) — leave their `currentColor` borders intact (WCAG 1.4.11).

#### P2-3 — Attention band dominant, closed band recedes (type weight)
- **File:** `app.css` · **pure-CSS:** Y
- **Change:** `.band--attention .band-head strong{ font-size:var(--fs-700); color:var(--c-attention-tx); }` (loudest) ·
  `.band--closed .band-head strong{ font-weight:var(--fw-medium); color:var(--ink-500); }` (recede).
- **Why it lifts:** "需要你处理" finally outranks "已结案" in visual priority.
- **Impact:** medium · **Effort:** S · **Risk:** low. `--c-attention-tx` (AMBER text, ~7:1) keeps parked semantics; `--ink-500` on white ~6:1.
  Don't tint closed below 4.5:1. Text labels remain so color is never the sole signal.

#### P2-4 — Cap the honesty note / job workspace measure; optional `--fs-900` hero
- **File:** `app.css` · **pure-CSS:** Y
- **Change:** `.note{ max-width:52rem; }` (cap measure, never hide) · `#view-job{ max-width:46rem; margin:0 auto; }` ·
  `#job-actions:not(:empty){ border:1px solid var(--line); border-radius:var(--radius-md); background:var(--paper-2); padding:var(--sp-4); margin-top:var(--sp-4); }`.
  Optionally use the still-unused `--fs-900` on the cleared-inbox empty-state hero only.
- **Why it lifts:** the single-job view stops floating in a 1024px void; honesty line gets a comfortable measure without ever hiding.
- **Impact:** low–medium · **Effort:** S · **Risk:** low. `:not(:empty)` guard avoids an empty bordered box (actions are `clear()`ed between renders). Honesty note stays always-visible.

#### P2-5 — Subtle reduced-motion-aware reveal for confirm tray / hold panel
- **File:** `app.css` · **pure-CSS:** Y
- **Change:** `@keyframes tray-in{ from{opacity:0; transform:translateY(-4px);} to{opacity:1; transform:none;} }`
  `.confirm-tray, .hold-panel{ box-shadow:var(--shadow-md); animation:tray-in .14s ease; }`, then add
  `.confirm-tray, .hold-panel{ animation:none; }` to the reduced-motion block. `.confirm-tray{ border-width:2px; }` so danger feels weightier.
- **Why it lifts:** transient decision surfaces get a tactile reveal + elevation; the danger tray outweighs the hold panel.
- **Impact:** low–medium · **Effort:** S · **Risk:** low. Animate opacity+translate only (no layout thrash); reduced-motion users get the instant version. Tray wording (set in app.js) untouched.

---

## Rejected (constraint-violating) ideas

- **Any web font / `@font-face` / Google Fonts `<link>`** — there is **no `font-src`** in the CSP (`index.html:12`), so it is network-blocked. Stick to the existing `system-ui / PingFang / Microsoft JhengHei` stack. *(A self-hosted font would require adding a file to `web/` **and** a `font-src 'self'` CSP edit — explicitly out of scope; default NO.)*
- **A new brand/accent hue for nav-active or buttons (e.g. Lens-3's `--accent:#3f5bd9`)** — the six color families are **semantic**; a new hue (or even reusing progress-blue as chrome) risks reading as a new state. Chrome stays neutral paper/elevation (P1-4). Corollary: **white text on `--c-progress-bd` (#4f86d6) is ~3.6:1 and FAILS 4.5:1**, so blue is legal only as a border/text/hover on white, never a filled button — primary buttons keep the GREEN family which passes.
- **Any inline `style="…"` or inline `<style>`** for a shadow/accent/one-off — `style-src 'self'` forbids both; every visual ships as a class in `app.css`.
- **Building a card wrapper or the counts chips with `innerHTML` / template-HTML strings** — violates R41. The counts chips (P1-1) and row affordance (P0-7) use only `el()`/`createElement` + `textContent` + `className`/`setAttribute`.
- **Remote `normalize.css`, an icon font, or an SVG sprite from a CDN** — `default-src 'none'` blocks it; no dependency/CDN/build step is allowed. Decorative glyphs stay as the existing `::before`/text glyphs.
- **Lens-3's `.band--frozen / .band--ready / .band--done` band selectors** — these band keys **do not exist** (`BANDS` keys are only `attention/stopped/inflight/closed`, `app.js:318`). Corrected to the real four keys in P0-2.
- **Restating the dual `box-shadow` (lane bar + elevation) per `.lane--*` rule** — verbose and easy to miss one (silently dropping elevation). Replaced by the DRY `--lane` custom-prop approach in P1-2.
- **Making the row a `<button>` / removing the visible "打开 ›" cue** — would make color/elevation the only affordance signal. P0-7 keeps the row as `role="button"` *and* the visible text cue.
- **Elevating or animating the honesty `.note` so it could read as dismissible** — trust posture requires it stay always-visible and static; only its measure is capped (P2-4).
- **`--fs-900` on the persistent `.topbar h1`** — a 1.85rem title inflates the sticky header in a ~700px-tall resizable pywebview window. Title caps at `--fs-700` (P0-3); `--fs-900` is reserved for an opt-in empty-state hero only.
- **Recoloring any lane/badge or remapping a state family (e.g. tinting amber toward red, or "fail-closed" toward RED)** — fail-closed must keep reading as AMBER "parked", dedup/backfill/署名 wording stays untouched; only neutral surfaces and elevation are added.

---

## Constraint conformance (one-line audit)

CSP: ✅ no font-src use, no inline style/script, no remote asset — all class rules in the one linked `app.css`.
R41: ✅ the two JS edits (P0-7, P1-1) are `createElement`/`textContent`/`setAttribute` only via the existing `el()` helper — no `innerHTML`.
Trust: ✅ honesty note always-visible & static; six families keep meaning; frozen 2px-indigo skin + ribbon intact; inert links untouched; parked ≠ failure.
A11y: ✅ ≥4.5:1 maintained (canvas/paper-2 backgrounds, `--ink-500`~6:1, `--c-attention-tx`~7:1); badge `currentColor` borders kept; `:focus-visible` rings kept and **added** to focusable rows; reduced-motion extended; every color keeps its text label.
Desktop: ✅ designed for ~900–1200px (60rem capped paper on a tinted canvas) and degrades at the existing 40rem narrow breakpoint (P0-8).
