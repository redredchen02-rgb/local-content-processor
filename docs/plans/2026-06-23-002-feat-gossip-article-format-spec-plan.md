---
title: "feat: 七/八條款文章格式規範 — 吃瓜內容 LLM 生成品質提升"
type: feat
status: completed
date: 2026-06-23
---

# feat: 七/八條款文章格式規範 — 吃瓜內容 LLM 生成品質提升

## Overview

將操作手冊「七、标题与正文撰写规范」和「八、标签与关键词规范」的格式要求，落地到現有 LLM 生成流水線（assembler + copywriter + lint）中。爬取並確認主題後，LLM 產出的文章草稿必須符合該規範的結構與長度約束，不符合的草稿自動路由到 `NEEDS_REVISION`，由人工核查。

## Problem Frame

gossip_scraper → lcp 批次注入後，lcp 呼叫 assembler + copywriter 生成草稿。目前的問題：

1. **Assembler 輸出無結構**：`assemble()` 將 LLM 回傳的全部文本塞入 `event_body`，`intro` 僅取第一行。七條款要求 `intro` 80–120 字、`event_body` 100–200 字，但現行結構無法精確分開，也無法 lint 驗證。
2. **Lint 缺少長度/數量規則**：現行 `lint_rules.py` 只驗證 title 長度和 tags 數量。intro/event_body/summary/FAQ/quick_facts 的字數與數量限制均未受 lint 管控。
3. **Copywriter 提示詞缺乏規範細節**：QUICKFACT 沒有七條款要求的 7 個固定字段標籤；TITLE 候選沒有「人物/平台 + 事件關鍵詞 + 核心看點」結構指導；FAQ 沒有「3–5 題、結合本文」要求。
4. **無 Eatmelon 品牌模板**：`config.example.yaml` 的 `templates:` 區塊為空，operator 沒有現成的 Eatmelon 語氣模板可用。

## Requirements Trace

- R1. 標題 25–35 字，純文字，結構：人物/平台 + 事件關鍵詞 + 核心看點 ← lint title_min/max_chars 已達標；copywriter prompt 補結構說明
- R2. 開頭簡介 80–120 字，直接入題，未證實資訊加限定詞 ← 新 lint intro 長度規則 + assembler 結構化輸出
- R3. 一分鐘快速看懂 5–7 項（有明確資訊才填，不編造），7 個固定字段 ← 新 lint quick_facts 數量規則 + copywriter prompt 更新
- R4. 事件經過 100–200 字，按時間順序，不添加素材中不存在的資訊 ← 新 lint event_body 長度規則 + assembler 結構化輸出
- R5. FAQ 3–5 題，結合當前文章，不能每篇複製同一套問答 ← 新 lint faq 數量規則 + copywriter prompt 更新
- R6. 結尾總結約 80 字，不重複開頭和前文 ← 新 lint summary 長度規則
- R7. 標籤 3–5 個，客觀詞，禁止營銷詞 ← 現行 tag_min/max_count 已達標；擴充 hype_words

## Scope Boundaries

- 不改動 Draft 資料模型（8 段結構已完整）
- 不改動狀態機（沒有新狀態）
- 不做「排版相似度」外部比對（需要外部爬蟲，超出本期）
- 不實作「跨文章歷史比較」防重複（需要額外 storage + 歷史查詢，後期可加）；Unit 3 的 copywriter prompt 中的「防重複提醒」是**每篇文章內的 prompt 語言指示**（告知 LLM 根據當前素材自然調整，勿套固定模板），不做跨 job 歷史比對，與此 scope 邊界無矛盾
- 不修改 CLI/GUI（無新 operator 操作；這是 LLM 生成品質升級）

## Context & Research

### Relevant Code and Patterns

- `src/lcp/core/rules/lint_rules.py` — `LintConfig` / `lint_draft()` / `DEFAULT_HYPE_WORDS`；新欄位照現有 dataclass 模式添加（frozen dataclass，有型別默認值）
- `src/lcp/core/config.py:ContentConfig` — operator 可調的 lint 參數在此鏡像；`build_lint_config()` 在 `draft_linter.py` 做投影
- `src/lcp/adapters/processor/draft_linter.py:build_lint_config()` — 將 `ContentConfig` 投影到 `LintConfig`；新欄位遵循現有 fallback 模式（0/空 → 使用規則預設值）
- `src/lcp/adapters/llm/assembler.py` — `build_system_prompt()` 目前為英文常數；`assemble()` 將 LLM 全文放入 `event_body`
- `src/lcp/adapters/llm/copywriter.py` — line-prefix 協議（`KEY: value` 一行一條）；`_parse()` 做 prefix 分流；新 prefix 照現有模式擴充
- `tests/rules/test_lint_rules.py` — 現有 lint 測試模式，用純值構造 `Draft` + `LintConfig`
- `tests/llm/test_assembler.py` — `FakeClient` 模式（無網路），script `ChatResult`
- `tests/llm/test_copywriter.py` — 同上 FakeClient 模式

### Institutional Learnings

- `docs/solutions/` — 「fail-closed before human」：任何 LLM 輸出格式不符都路由到 NEEDS_REVISION，不靜默通過（U2 的 no-marker fallback 遵循此原則）
- `CLAUDE.md`「functional core / imperative shell」：lint 規則純邏輯放 `core/rules/lint_rules.py`，wiring 在 adapter `draft_linter.py`，prompt 邏輯在 `adapters/llm/`

## Key Technical Decisions

- **Assembler 使用 `INTRO:` / `EVENT:` line-prefix 協議**，與 copywriter 既有 prefix 協議一致，且**嚴格單行語義**（prompt 指示 LLM 把 INTRO/EVENT 各壓成一行，與 copywriter 的 `_parse()` 邏輯完全平行）；無 marker 時路由到 `NEEDS_REVISION`（fail-closed），不靜默回退到舊行為（舊行為讓 lint 永遠無法驗證長度，等同靜默通過）。
- **Assembler system prompt 聚焦在 intro + event_body**，移除對 quick_facts/FAQ/summary 的指示（這些由 copywriter 負責）；避免模型分裂注意力產生格式混亂。
- **LintConfig 新欄位全部有合理預設值**，operator 可在 `content:` 區塊覆蓋；0 / 空 → 使用規則預設值（延續現有 hype_words/min_copy_chars 的 fallback 模式）。
- **quick_facts_min_count=3**（非 5）：七條款允許「無明確資訊可刪除對應項目」，3 條以上即表示 LLM 有在跟隨格式。**1-2 條 = lint error（NEEDS_REVISION）**，視為 LLM 格式執行不到位（非素材稀少的合規 case）；operator 可人工判斷是否重跑或接受。素材確實稀少導致合規輸出 < 3 條的邊界 case 屬可接受誤殺率，由 operator review 決定。兩個錯誤訊息語義不同：空 list → "missing required section: 一分鐘快速看懂"，1-2 條 → "too few quick_facts items"，方便診斷。

## Open Questions

### Resolved During Planning

- **標題字數計算**：Python `len(title)` 以 Unicode code point 計，中文每字 = 1 point，符合「25–35 字」定義。現行 lint 已正確實作，無需改動。
- **quick_facts 的 "7 個字段標籤" 如何處理**：copywriter 指示 LLM 在 QUICKFACT 的值中攜帶字段名（如 `QUICKFACT: 人物/主體：某某`），label 保存在 `quick_facts` 的 string 值內，不拆分為 key/value。lint 的 count 規則針對列表長度，不解析標籤名稱，保持純度。
- **summary 長度規則**：規範說「約 80 字」，以 `summary_max_chars=100` 為警告上限（給模型彈性），`summary_max_chars_error=150` 為錯誤上限（超過則 NEEDS_REVISION）；上限由 LintConfig 新增兩個欄位控制。

### Deferred to Implementation

- 「同一寫手連續發布時小標題/結構不得完全相同」跨文章防重複檢測 — 需要跨 job 的歷史資料比對，非純 lint；實作時評估是否加到 dedup gate 或另立 gate。
- 「發布前檢查排版是否與參考站點高度相似」外部相似度比對 — 超出本期，需要獨立 scraper 作比對基線。

## High-Level Technical Design

> *此圖說明預期的方案架構，為審查方向性引導，不是實作規格。實作時應參考，而非逐字複製。*

```
爬取完成的 source_text
       │
       ▼
 [assemble()]──────────────────────────────────────────────┐
 │  system: "只生成 INTRO: + EVENT: 兩段，各有字數限制"      │
 │  user:   <delimiter>source_text</delimiter>             │
 │  LLM 回傳:                                              │
 │    INTRO: 引言內容（80–120 字）                           │
 │    EVENT: 事件時間線（100–200 字）                        │
 │  _parse_sections() → intro, event_body 各自獨立           │
 │  若無 marker → DraftStatus.NEEDS_REVISION               │
 └──────────────────────────────────────────────────────────┘
       │ Draft(intro=..., event_body=..., quick_facts=[], faq=[], ...)
       ▼
 [generate_structural_copy()] ─────────────────────────────┐
 │  TITLE:      候選標題（25–35字，結構格式）                 │
 │  QUICKFACT:  人物/主體：xxx（最多 7 條）                   │
 │  FAQ_Q/A:    3–5 對，本文具體問答                          │
 │  SUMMARY:    結尾（約 80 字）                              │
 │  TAG:        3–5 個客觀詞                                  │
 └──────────────────────────────────────────────────────────┘
       │ enriched Draft
       ▼
 [lint_draft()] ───────────────────────────────────────────┐
 │  NEW: intro 80–120 字                                    │
 │  NEW: event_body 100–200 字                              │
 │  NEW: summary ≤100 字（警告）/ ≤150 字（錯誤）             │
 │  NEW: faq 3–5 項                                         │
 │  NEW: quick_facts 3–7 項                                 │
 │  EXISTING: title 25–35 字、tags 3–5 個、無 hype 詞        │
 └──────────────────────────────────────────────────────────┘
```

## Implementation Units

- [ ] **Unit 1: 新增 lint 長度/數量規則 + 配置接線**

**Goal:** `lint_draft()` 驗證 intro / event_body / summary / FAQ / quick_facts 的格式約束，不符合路由 NEEDS_REVISION。

**Requirements:** R2, R3, R4, R5, R6, R7

**Dependencies:** None（純 core 層，無外部依賴）

**Files:**
- Modify: `src/lcp/core/rules/lint_rules.py`
- Modify: `src/lcp/core/config.py` (`ContentConfig`)
- Modify: `src/lcp/adapters/processor/draft_linter.py` (`build_lint_config()`)
- Modify: `config.example.yaml` （新增可調參數說明）
- Test: `tests/rules/test_lint_rules.py`
- Test: `tests/processor/test_lint_config_wiring.py`

**Approach:**
- `LintConfig` 新增欄位（全部有預設值，符合 frozen dataclass 模式）：
  - `intro_min_chars: int = 80`, `intro_max_chars: int = 120`
  - `event_body_min_chars: int = 100`, `event_body_max_chars: int = 200`
  - `summary_warn_chars: int = 100`, `summary_error_chars: int = 150`
  - `faq_min_count: int = 3`, `faq_max_count: int = 5`
  - `quick_facts_min_count: int = 3`, `quick_facts_max_count: int = 7`
  - **注意**：`summary` 的雙閾值（warn/error）是 `LintConfig` 裡第一個此類設計，無現成模式可複製。實作 `lint_draft()` 時需確保：warning 路徑只 append 到 `warnings`（不影響 status）；error 路徑 append 到 `errors`（觸發 NEEDS_REVISION）。兩者都用 `len(summary)` 計算，非 `strip()` 後長度（與 title 檢查一致）。
- `DEFAULT_HYPE_WORDS` 補充八條款禁用詞：`"顶级"`, `"頂級"`, `"最好看"`, `"刺激到不行"` （繁簡各一，因為 lint 是 lowercased 比對）
- `lint_draft()` 添加區塊（順序在 required sections 檢查之後）：
  - intro 長度（short/long → error）
  - event_body 長度（short/long → error）
  - summary 長度（warn_chars → warning；error_chars → error）
  - faq count（low → error；high → error）
  - quick_facts count（如 quick_facts 非空：low/high → error；空列表由 required-section 規則已處理）
- `ContentConfig` 新增對應欄位（預設值 0 / 空 = 使用 rule 預設，延續現有 hype_words/min_copy_chars 的 fallback 慣例）
- `build_lint_config()` 投影新欄位，0 fallback 到 LintConfig 預設值；在投影 `summary_warn_chars` / `summary_error_chars` 後加交叉驗證：`warn >= error → InputValidationError`（frozen dataclass 無 `__post_init__` 驗證，需在投影點捕捉）
- `config.example.yaml` 在 `content:` 區塊加新參數說明（與現有 hype_words/min_copy_chars 格式一致）

**Patterns to follow:**
- `LintConfig` frozen dataclass 欄位定義：`src/lcp/core/rules/lint_rules.py:123`
- `build_lint_config()` 的 0-fallback 模式：`src/lcp/adapters/processor/draft_linter.py:61`

**Test scenarios:**
- Happy path: intro=100字, event_body=150字, summary=80字, faq=4項, quick_facts=6項 → PASS
- Intro too short: intro=50字 → error "intro too short"
- Intro too long: intro=130字 → error "intro too long"
- Event body too short: event_body=80字 → error "事件經過 too short"
- Event body too long: event_body=210字 → error "事件經過 too long"
- Summary warning zone: summary=110字 → warning "結尾偏長", no error
- Summary error zone: summary=160字 → error "結尾過長"
- Too few FAQ: faq=2項 → error "too few faq items"
- Too many FAQ: faq=6項 → error "too many faq items"
- Quick_facts empty list: quick_facts=[] → handled by existing required-section rule (已有), not the count rule
- Quick_facts non-empty too few: quick_facts=["x", "y"] → error
- Quick_facts non-empty too many: quick_facts=["x"]*8 → error
- New hype word "顶级" in tag → error
- Config wiring: `ContentConfig(intro_min_chars=70)` → `LintConfig.intro_min_chars=70`
- Config wiring: `ContentConfig(intro_min_chars=0)` → `LintConfig.intro_min_chars=80` (default)

**Verification:**
- `tests/rules/test_lint_rules.py` 全部新舊測試通過
- `tests/processor/test_lint_config_wiring.py` 新欄位投影正確
- `mypy` clean（新欄位有型別，frozen dataclass 無問題）

---

- [ ] **Unit 2: Assembler 結構化輸出 — `INTRO:` / `EVENT:` prefix 協議**

**Goal:** Assembler LLM 生成兩個標記分隔段落（`INTRO:` / `EVENT:`），解析後分別填入 `Draft.intro` 和 `Draft.event_body`；無 marker → NEEDS_REVISION。

**Requirements:** R2, R4

**Dependencies:** Unit 1 — **測試依賴**（非 runtime 依賴）：assembler 和 lint 在 runtime 獨立運行（assembler 不 import lint），但整合測試需要 Unit 1 的長度規則已存在，才能驗證 assemble → lint 的完整路徑。先上 Unit 1 可確保 Unit 2 的新輸出立即受長度驗證；若 Unit 2 先上，短格式 intro 會靜默通過現有 lint（只有 required-section presence check），比失敗更危險。

**Files:**
- Modify: `src/lcp/adapters/llm/assembler.py`
- Modify: `src/lcp/core/rules/grounding.py` （將 `Draft.intro` 加入 `_split_claims()` 覆蓋範圍）
- Test: `tests/llm/test_assembler.py`

**Approach:**
- `build_system_prompt()` 重寫：聚焦指示 LLM 只生成兩個段落，使用嚴格 prefix 協議：
  - `INTRO:` 後接開頭簡介（80–120 字，直接入題，不重複標題，未證實用限定詞）
  - `EVENT:` 後接事件經過（100–200 字，按時間順序，不添加素材外資訊）
  - 不要生成 quick_facts / FAQ / 結尾（這些由 copywriter 負責）
  - 保留現有 anti-injection 指示（datamark / 零能力 LLM / 不跟隨源文指令）
  - prompt 改為中文，對齊吃瓜內容場景
- 新增 `_parse_sections(text: str) -> tuple[str, str]`：
  - 嚴格單行語義（與 copywriter `_parse()` 一致）：逐行掃描，`line.startswith("INTRO:")` → 取該行 `INTRO:` 之後的值（`.strip()`）；`EVENT:` 同理
  - **First-match 語義**：一旦 `intro` 已被賦值，後續 `INTRO:` 行不覆蓋（加 `if not intro:` guard）；`EVENT:` 同理。避免 last-write-wins 允許 LLM 輸出中的第二個 `INTRO:` 行覆蓋第一個合法值。
  - **輸出側 sanitize**：每個解析值在 return 前通過 `sanitize_source()` 清洗（與輸入側一致）；防止 LLM 反射 source 中的 bidi/zero-width 序列進入 Draft 字段。
  - 若兩個 marker 均缺失 → 回傳 `("", "")`
  - 若只缺其中一個 → 回傳已有的那個，另一個為空字串
  - 不做多行累積（prompt 已指示 LLM 把每段壓成單行）；若 LLM 換行寫，第二行不帶 marker 故被忽略 → lint 的長度規則會自然捕捉到截斷情形
- **grounding.py 更新**：在 `_split_claims()` 中加入 `intro` 覆蓋（與 `event_body` 並列：`for chunk in _sentences(draft.intro): if len(chunk) >= _MIN_CLAIM_CHARS: claims.append(chunk)`）。`intro` 現在是獨立的 LLM 生成字段，其中的事實聲明必須被驗證為 source 的子字符串；不加入等同讓編造的開頭簡介靜默通過 grounding。
- `assemble()` 調用 `_parse_sections(result.text)`：
  - 若 `intro == "" and event_body == ""` → `DraftStatus.NEEDS_REVISION`, `review_reason="missing_section_markers"`
  - 若只缺其中一個（`intro==""` 或 `event_body==""`）→ NEEDS_REVISION, `review_reason="missing_intro"` / `"missing_event"`
  - 兩個都有 → DRAFTED（後續 lint 再驗長度）
- 保留現有的 no-quote / truncated / dry-run guard（順序在 prefix 解析之前）

**Execution note:** 新增 NEEDS_REVISION path test 前先確認現有 happy-path test 仍 pass。

**Patterns to follow:**
- copywriter `_parse()` 的 line-prefix 解析模式：`src/lcp/adapters/llm/copywriter.py:104`
- assembler 現有 NEEDS_REVISION gate 模式：`src/lcp/adapters/llm/assembler.py:199`

**Technical design:**

```
# 方向性 pseudo-code（單行語義，與 copywriter._parse() 一致），非實作規格
def _parse_sections(text: str) -> tuple[str, str]:
    intro, event = "", ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("INTRO:"):
            intro = stripped[len("INTRO:"):].strip()
        elif stripped.startswith("EVENT:"):
            event = stripped[len("EVENT:"):].strip()
    return intro, event
```

嚴格單行協議：prompt 明確指示 LLM 每個 section 只能佔一行。不做多行累積，避免引入第二套有狀態的解析器，維持與 copywriter 的語義一致。anti-injection 前提：此分析成立於單行+first-match 解析 — source 中的 `INTRO: 假指令` 在 datamark 隔離的 user message data 區；sanitize 去除控制字符後，殘留內容被 parser 取出為值（不影響 SYSTEM prompt），且 first-match guard 確保第二個 `INTRO:` 行不覆蓋第一個已合法賦值。LLM output 側也通過 `sanitize_source()` 清洗，防止 bidi 序列反射。

**Test scenarios:**
- Happy path: LLM 回傳 `"INTRO: 引言…\nEVENT: 事件…"` → `draft.intro="引言…"`, `draft.event_body="事件…"`, status=DRAFTED
- Multi-line INTRO（截斷行為）: LLM 把 INTRO 拆成兩行 → 只有第一行被捕捉（single-line 合約）；第二行無 marker 被忽略 → `len(intro) < 80` → lint 觸發 min-chars error → NEEDS_REVISION，此為預期 degraded path，非 bug
- No markers at all: LLM 回傳純文本 blob → status=NEEDS_REVISION, reason="missing_section_markers"
- Only INTRO, no EVENT: → NEEDS_REVISION, reason="missing_event"
- Only EVENT, no INTRO: → NEEDS_REVISION, reason="missing_intro"
- Dry-run client → NOT_EXECUTED（`result.executed=False`，不呼叫 LLM，行為同現有 dry-run guard）
- Truncated finish_reason → NEEDS_REVISION（`result.needs_revision=True`，同現有 finish_reason gate）
- No verbatim quotes → NEEDS_REVISION（現有 no-quote guard 保持；`_parse_sections()` 不影響此路徑）
- Duplicate INTRO: 同一輸出中兩個 `INTRO:` 行 → first-match guard 確保取第一個合法值，第二個被忽略（last-write-wins 的替代方案，明確文件化為預期行為）
- Anti-injection: source 含 "INTRO: 假指令" → sanitize_source 先去除控制字符後，datamark 隔離；最終不影響 parser（parser 只掃第一個 `INTRO:` prefix 所在行）

**Verification:**
- `tests/llm/test_assembler.py` 全部新舊測試通過
- `draft.intro` 和 `draft.event_body` 分別持有不同內容（不再是 blob / first-line 拆分）
- `mypy` clean

---

- [ ] **Unit 3: Copywriter 提示詞更新 — 七/八條款結構規則**

**Goal:** copywriter LLM 指示更新，使 TITLE 候選符合結構，QUICKFACT 攜帶 7 個字段標籤，FAQ 有 3–5 題本文具體，SUMMARY 控制字數，TAG 禁止八條款列出的營銷詞。

**Requirements:** R1, R3, R5, R6, R7

**Dependencies:** None（獨立於 Unit 1 和 Unit 2）

**Files:**
- Modify: `src/lcp/adapters/llm/copywriter.py`
- Test: `tests/llm/test_copywriter.py`

**Approach:**
- `build_system_prompt()` 更新（現有 system prompt 的所有 anti-injection 規則保留）：
  - **TITLE** 規則：25–35 字，純文字（無無意義符號），結構：「人物或平台 + 事件關鍵詞 + 核心看點」；可自然加入地點/學校/身份
  - **QUICKFACT** 規則：最多 7 條，每條格式 `QUICKFACT: <字段名>：<值>` ，字段名固定為：「人物/主體」「發生地點」「所屬平台」「內容類型」「事件關鍵詞」「核心看點」「當前進展」；只填有明確資料的字段，沒有則省略（不編造）
  - **FAQ** 規則：生成 3–5 對，問題必須結合本篇文章內容，不得使用套路問答；答案未有可靠資訊時不得自行編寫
  - **SUMMARY** 規則：約 80 字，不重複開頭或前文，不添加未經證實的資訊
  - **TAG** 規則：3–5 個，只填客觀詞（人物/帳號/平台/地點/事件名/內容類型），禁止「頂級、最好看、刺激到不行」等主觀/營銷詞
  - **防重複提醒**：小標題和表達方式要根據素材自然調整，避免固定套版
- `_parse()` 不需改變（QUICKFACT 字段標籤保存在值字串內，不拆分 key/value）

**Patterns to follow:**
- `build_system_prompt()` 現有結構：`src/lcp/adapters/llm/copywriter.py:71`
- line-prefix 協議 `_PREFIXES` 字典：`src/lcp/adapters/llm/copywriter.py:33`

**Test scenarios:**
- Happy path (FakeClient returns): `TITLE: 某博主疑似逃漏稅遭爆料，金額據傳高達千萬\nQUICKFACT: 人物/主體：某博主\nFAQ_Q: 這件事是真的嗎？\nFAQ_A: 目前資訊來自網路傳播，尚未得到當事人確認\nSUMMARY: 某博主被爆疑似逃漏稅，相關單位尚未表態\nTAG: 博主` → `copy.title_candidates=["某博主..."]`, `copy.quick_facts=["人物/主體：某博主"]`, `copy.faq=[FaqItem(...)]`, `copy.summary="某博主被..."`, `copy.tags=["博主"]`
- system prompt contains title structure rule keyword → assert "人物" in build_system_prompt()
- system prompt contains quickfact field labels → assert "人物/主體" in build_system_prompt()
- system prompt contains faq count range → assert "3" in build_system_prompt() and "5" in build_system_prompt()
- _clean_tags drops new hype words (顶级) → assert "顶级" not in result after clean

**Verification:**
- `tests/llm/test_copywriter.py` 全部測試通過
- system prompt 包含七條款關鍵結構說明（可做字串 assert）
- `mypy` clean

---

- [ ] **Unit 4: `config.example.yaml` 新增 Eatmelon 吃瓜 operator 模板**

**Goal:** 在 `config.example.yaml` 的 `templates:` 區塊提供一個現成可用的 Eatmelon 吃瓜語氣模板，讓 operator 複製到 `config.yaml` 後可直接使用。

**Requirements:** R1 (品牌模板)

**Dependencies:** None

**Files:**
- Modify: `config.example.yaml`

**Approach:**
- 在 `config.example.yaml` 末尾加 `templates:` 區塊（`Config` 中已有此欄位）
- 模板 key 對齊現有 `categories:` 的一個 key（如 `今日吃瓜`）
- 模板文字只使用 `ALLOWED_SLOTS`（`{category}`, `{title}`, `{tags}`，`{keywords}`）
- 模板做為 developer_block 發給 LLM，主旨為：語氣說明 + 防重複提醒 + 品牌語調（Eatmelon 吃瓜風格）
- 模板文字需先通過 `validate_template()` lint（實作時用 Python REPL 驗証，確保 format_map 安全）
- 加上大型注釋說明如何使用模板功能

**Test expectation:** none — 純 YAML 配置，無行為改動。可透過整合測試 (`lcp run --dry-run`) 手動驗証。

**Verification:**
- `config.example.yaml` YAML 語法有效（`python -c "import yaml; yaml.safe_load(open('config.example.yaml'))"` 不報錯）
- 模板字串手動傳入 `validate_template()` 不報 `InputValidationError`

---

## System-Wide Impact

- **Interaction graph:** lint gate 是 Stage-2 的終態決策者；新規則會讓更多 LLM 輸出路由到 `NEEDS_REVISION`（預期是早期校準期的正常現象）
- **Error propagation:** 格式不符 → `LintStatus.NEEDS_REVISION` → `JobState.NEEDS_HUMAN_REVIEW`（現有路由，無變化）
- **State lifecycle risks:** 無新狀態；NEEDS_REVISION 仍可由 operator 重新跑 processing 或人工清除（現有操作路徑）
- **API surface parity:** 無新 CLI/GUI 操作；assembler/copywriter 的公開函數簽名不變
- **Integration coverage:** assembler `_parse_sections()` 新邏輯需整合覆蓋（FakeClient + 真實 LLM 的假輸出）
- **Unchanged invariants:** `needs_human_review=True` 和 `constrained_rewrite=True` 在所有 Draft 路徑保持不變；dry-run 行為不受影響

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| LLM 不遵循 `INTRO:` / `EVENT:` marker 格式 | fail-closed → `NEEDS_REVISION`，`review_reason="missing_section_markers"` 是唯一診斷信號；校準方式：在 audit log 中查詢 `event=LINT_GATE, status=needs_revision` 的頻率（`review_reason` 記錄在 audit extra.review_reason 欄位）；若持續偏高，校準入口是 `assembler.build_system_prompt()` 的 prompt 文字 |
| `ai_copy=False` + 新 assembler | 新 assembler 只輸出 intro/event_body；quick_facts/faq/summary 均為空 → lint 必然 NEEDS_REVISION（missing required section）。此為正確 fail-closed 行為，非退化，但 operator 需了解 `--no-ai-copy` 在本版本後等同強制 NEEDS_REVISION |
| 字數限制過嚴，正常輸出被拒 | 初期可在 config.yaml 調寬 `intro_max_chars` / `event_body_max_chars` 做校準 |
| 新 hype_words 誤殺正常標籤 | hype_words 是 operator-tunable，config.yaml 可個別排除 |
| QUICKFACT 字段標籤前綴（如「人物/主體：」）佔 quick_fact 文字比例較高，可能導致 grounding shingle overlap < 0.6 → NEEDS_HUMAN_REVIEW | copywriter prompt 指示每個 QUICKFACT 值需包含完整資訊句（「人物/主體：某某據傳逃漏稅」），確保 source-derived ngrams 主導 overlap 計算；初期校準時若 grounding 誤殺過多，可調整 quick_facts 的 `_MIN_CLAIM_CHARS` 下限或單獨排除標籤前綴部分 |
| 現有測試因 system prompt 改變而 assert 失敗 | test_assembler.py / test_copywriter.py 的 prompt 內容 assert 需更新 |

## Sources & References

- 七、标题与正文撰写规范（本期規格輸入）
- 八、标签与关键词规范（本期規格輸入）
- Related code: `src/lcp/core/rules/lint_rules.py`, `src/lcp/adapters/llm/assembler.py`, `src/lcp/adapters/llm/copywriter.py`
- Related plans: gossip pipeline plan `docs/plans/2026-06-22-001-feat-gossip-pipeline-core-plan.md`
