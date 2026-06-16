---
title: "feat: Local Content Processor — MVP (Crawl → Process → Review packet)"
type: feat
status: active
date: 2026-06-16
deepened: 2026-06-16
origin: docs/brainstorms/2026-06-16-local-content-processor-requirements.md
---

# feat: Local Content Processor — MVP

## Overview

建一個**本地執行**的內容流水線 `local-content-processor`（lcp）：抓取公開且可合法引用的來源 →
標準化媒體 → 風控/查重閘門 → 用公司自有 LLM 做**受限改寫** → 產出**可審核的草稿包**，
由非技術經營者透過**最小 GUI** 審核後**手動上架**。MVP **不自動發布**（見 origin Scope Boundaries）。

三條貫穿全案的架構紅線（前兩條來自外部研究，第三條為信心檢查的安全審查補入）：

1. **重寫 LLM 全程零能力**：content_assembler 呼叫 LLM 時**無工具、無外網、無寫入**，只回文字。
   打破 prompt-injection 的「致命三要素（lethal trifecta）」——被注入的最壞結果降級為「文字變垃圾」。
2. **所有自動判斷皆 advisory，人審才是真正閘門**：唯一不可繞過的硬閘門是「未經人工 approve 不得有任何上架路徑」。
3. **三要素不可在下游被重建**：LLM 在那站零能力還不夠——**攻擊者可塑造的文字（scraped/LLM 輸出）在任何後續環節
   都不得重獲能力**。具體：(a) GUI 一律以 `textContent`/轉義渲染、禁 `innerHTML`、上 CSP、來源連結惰性化；
   (b) linter/grounding 只比對本地字串、**MUST NOT 解析任何 URL**；(c) 子進程以最小化 env 啟動。
   （否則 webview 的 js_api 橋會讓 XSS 直達 core，read/write/network 三條腿一次補齊——信心檢查的 Critical 發現。）

## Problem Frame

非技術經營者目前手動把外部素材整理成站內文章，流程零散、無紀錄、品質不一、易誤觸法律紅線。
需要可重跑、可追蹤、合規優先的本地 pipeline，把「找料→加工→待審」自動化到「只差人按一下審核」。
（完整背景見 origin: `docs/brainstorms/2026-06-16-local-content-processor-requirements.md`）

## Requirements Trace

覆蓋 origin R1–R40，並由信心檢查（架構/安全審查）新增 R41–R44。關鍵對應：

- **定位/合規** R1–R5 → Unit 1/4/6/7a
- **介面** R33 → Unit 2（分層）/ Unit 9（GUI）
- **Stage 1** R6–R11, R40 → Unit 4
- **Stage 2 媒體** R12–R15 → Unit 5
- **內容組裝** R16–R19, R34–R35 → Unit 7a；**lint/grounding** R23 → Unit 7b（強度依 Unit 1 spike）
- **風控/查重** R20–R22, R36 → Unit 6
- **審核/簽核** R24–R26, R37 → Unit 8
- **基礎設施/安全** R27–R32, R38–R40 → Unit 3（+橫貫）
- **R41（新，源自安全審查 A）**：輸出端轉義/淨化 + CSP + 惰性連結 + linter 禁解析 URL → Unit 7b/9
- **R42（右整為 MVP 範圍）**：MVP = best-effort unlink + 明文標註「不保證 SSD 抹除」+ PII-free SQLite/manifest + 祕密遮罩 + 敏感資料與 audit 分離 + PII 清單；**落盤加密 + crypto-shredding 移 post-MVP**（經使用者確認無具名法定抹除義務）→ Unit 3
- **R43（新，源自安全審查 B5）**：PII 清單 / 資料流圖為 **gating requirement** → Unit 3（前置）
- **R44（新，源自安全審查 C）**：OS 衛生（子進程最小 env、umask 先於 spawn、temp 在 job 目錄、禁 core dump、PyInstaller 排除祕密、spikes/ gitignore） → Unit 2/4/5

## Scope Boundaries

- **不做**（origin，post-MVP）：Stage 5 後台自動建草稿、Stage 6 自動發布 + 版本 hash 強制比對、
  Playwright JS 重頁抓取、image perceptual-hash 查重、stage rollback 撤銷機制、完整桌面 App。
- **不做**：跨機/雲端同步、多租戶、真認證（簽核僅 attribution）。
- **不做（post-MVP，無具名抹除義務）**：落盤加密 + crypto-shredding（MVP 採 best-effort unlink + 誠實標註）、版本 hash 自動比對（Stage 6）。
- **保留接縫**：crawler / assembler / publisher 以**明確 adapter 契約**預留（見 Unit 4 / Key Decisions），未來可接 Playwright / 後台 API。crawler 契約刻意輕量（一個 base class），屬 future-facing 保險。

## Context & Research

### Relevant Code and Patterns

Greenfield（已驗證：repo 內僅有 origin 文件、非 git repo、無程式碼）。無本地 pattern，全部 pattern 來自外部研究。

### External References（2026 研究綜述，決策導向）

**函式庫選型（鎖版本）**
- **Scrapy 2.16.0**（`>=2.16,<3`）：**每 job 開獨立 subprocess** 跑爬蟲（繞 `ReactorNotRestartable` + 崩潰隔離 + cron 契合）。`ROBOTSTXT_OBEY=True`、AutoThrottle、`allowed_domains` + downloader 層 `OffsiteMiddleware`、`ImagesPipeline`/`FilesPipeline` 必設 `IMAGES_STORE`/`FILES_STORE`。
- **Pillow 12.2.0**（`>=12.2,<13`）：`exif_transpose → thumbnail((800,big),LANCZOS) → save`；封面 `ImageOps.fit` 拼 1300×640；`ANTIALIAS` 已刪用 `Resampling.LANCZOS`；解壓炸彈 warning 升 error、勿設 `MAX_IMAGE_PIXELS=None`。
- **ffmpeg/ffprobe**：`subprocess`+argv（無 wrapper、無 `shell=True`）；`-nostdin`、`start_new_session=True`+`os.killpg` 殺整樹、`-t` 限量；幀率是 rational 字串；black/silence 在 stderr。
- **openai SDK v2.x**（`>=2,<3`）：`base_url` 含 `/v1`、Chat Completions、讀 `finish_reason`（非 `stop` 即需人工審）、timeout/retry 調短。
- **GUI：pywebview 6.x + `js_api`**：內建 server **僅綁 127.0.0.1**（off-host 不可達，消除網路來源 CSRF/CORS/DNS-rebinding）；PyInstaller 單視窗。**注意**：loopback-only 只防網路攻擊者，**不代表 DOM 內容可信**——js_api 橋無 origin/auth 檢查，故 R41 輸出轉義為必須。（inline `html=` serverless 模式無法載入外部 app.js/cover.jpg，不採用。）

**最佳實務（標註 真有效 / 裝飾）**
- **冪等**：temp 檔 + 原子 `os.replace` + **內容雜湊跳過**（檔案存在/mtime＝裝飾）。**checksum 冪等只適用確定性 stage**；LLM 草稿非確定、不參與 checksum 跳過，靠「進 review 後凍結」（見 Key Decisions）。
- **狀態**：SQLite 索引（**僅存 PII-free 欄位**）+ folder-per-job blob（**0600 明文；落盤加密=post-MVP**，見 R42）。
- **audit × GDPR 刪除**（**此為 crypto-shred 的前提；MVP 無抹除義務、不做加密，改 best-effort unlink + 誠實標註，見 R42**）：完整性鏈/引用留 append-only log；**敏感 payload 必須真的加密**才能 crypto-shred（毀金鑰）；裸 SHA-256 對低熵 PII（電話/姓名）可暴破＝**裝飾**，改 keyed-HMAC 或只 hash 高熵 artifact 內容；刪除記 `ERASURE` 事件。誠實限制：本地對 root 只能 tamper-evident、非 tamper-proof。
- **prompt injection**：最高槓桿＝破 lethal trifecta（紅線 1&3）；輸入端清隱形 payload + datamarking/spotlighting（非 base64）；偵測分類器只當 triage、**不可當主牆**；**輸出端**轉義是 A 段 Critical 的根治（紅線 3）。
- **extractive ≠ faithful**（~30% 純抽取仍不忠實）：claim 級 NLI（MiniCheck）+ 逐字子字串驗證；「有引用」≠忠實（57% 事後合理化）。
- **PII/祕密**：api_key 用 **OS keyring**；**建檔前 `os.umask(0o077)`**；log/crash 遮罩；**子進程最小 env**（避免解析攻擊者媒體的 ffmpeg 繼承祕密）；temp 一律在 job 目錄非 `/tmp`；啟動 `RLIMIT_CORE=0` 禁 core dump；SSD 覆寫刪除無效 → **MVP 誠實標 best-effort、不宣稱抹除**；加密 + crypto-shredding 為 post-MVP 的真抹除手段。
- **SSRF**：驗 **DNS 解析後 IP** 的 `is_global`、**釘字面 IP 連線**（防 rebinding/TOCTOU）、關 redirect 跟隨、scheme allowlist、`safehttpx`。（hostname 字串黑名單＝裝飾）
- **path traversal**：`resolve()`+`is_relative_to`；解壓逐成員驗證、tarfile `filter='data'`。
- **不可信媒體**：`defusedxml`、Pillow 炸彈護欄、解析放 subprocess+`setrlimit`、串流算大小別信 `Content-Length`。
- **dedup**：便宜優先級聯（標題正規化 hash → MinHash/LSH + 精確 Jaccard → 選配 embedding）；**advisory + 邊界送人審、只 auto-pass 明確非重複、絕不 auto-reject**；門檻自家語料校準（cosine 硬門檻＝危險）。
- **CLI-core + thin-GUI**：**三層**——純核（models/state 轉移/判斷函式，零框架零 I/O）/ I/O adapters（crawl_runner/llm_client/ffprobe/job_store/audit）/ UI 殼（cli/gui）。pydantic models 單一真相。
- **reviewer 署名**：attribution 非 authentication——`pwd.getpwuid(os.getuid()).pw_name`（比信 env 的 `getpass.getuser()` 難偽造）+ 白名單 + **逐字免責聲明**。

## Key Technical Decisions

- **每 job subprocess 跑 Scrapy**：繞 `ReactorNotRestartable` + 崩潰隔離。
- **crawler 抽象契約**（輸入：source spec + job dir；輸出：raw_job_bundle + per-asset 狀態），**Scrapy 為第一個實作**而非唯一形狀——讓 post-MVP Playwright 真能插入而非重構（架構審查 7）。
- **狀態：SQLite 索引（PII-free）+ folder-per-job 0600 明文 blob（落盤加密=post-MVP）**。
- **冪等 = checksum 只覆蓋確定性 stage**；LLM 草稿靠「進 REVIEW_PENDING（review packet 雜湊產生）後凍結原地重跑」而非 checksum 相同（架構審查 1c/8）。
- **重做用 `SUPERSEDED` 終態**：可被 supersede 的狀態（含 APPROVED）→ `SUPERSEDED`（作廢舊簽核、audit 記 `SIGNOFF_INVALIDATED`/`SUPERSEDED`、反向連結 new_job_id）。supersede 是一級操作非註腳（架構審查 1a/1b）。
- **PROCESSING 為瞬態、不寫 SQLite**；崩潰偵測僅依 `.processing` 標記檔；失敗落 `PROCESS_FAILED`（可重跑）（架構審查 2c/9）。
- **PII 落地治理（R42，已右整為 MVP 範圍）**：經使用者確認**無具名法定抹除義務**，MVP **不做落盤加密/crypto-shredding**——這同時避開 over-scope（origin 本將加密列 P2）與技術矛盾（Scrapy 寫明文、隨機 nonce 破壞 checksum 冪等、DEK 給不給不可信子進程、加密機制延後阻擋 Unit 3）。MVP 採：**best-effort unlink + 明文標註「不保證 SSD 抹除」**、PII-free SQLite/manifest、祕密遮罩、敏感資料與 audit 分離、PII 清單（R43）。audit = append-only + 雜湊鏈（**誠實 tamper-evident、非 tamper-proof**，本地 root 不可防）、PII 不入 audit、只 hash 高熵 artifact 內容、不雜湊裸識別碼。**落盤加密 + crypto-shred 列 post-MVP**，未來有抹除義務再啟用。
- **輸出端轉義為硬不變式（R41）**：所有攻擊者可塑造字串（scraped title/text、LLM draft、source URL、review_message）一律 `textContent`/轉義渲染、禁 `innerHTML`；`index.html` 上嚴格 CSP（`script-src 'self'`、無 inline、`object-src 'none'`）；來源連結渲染為**惰性文字**（不自動 fetch）。linter/grounding **MUST NOT 解析任何 URL**（安全審查 A/A4/A5）。
- **LLM 受限改寫 + 多層驗證**：datamarking 包裹、逐字引用子字串驗證、claim 級 entailment（強度由 Unit 1 spike 決定）、非 `stop` 即需人工審。
- **error 自帶 exit_code 屬性**：CLI 殼只讀不判斷，避免業務邏輯洩漏到殼（架構審查 10）。
- **單一 `NEEDS_HUMAN_REVIEW` 帶 `review_reason`（risk/dedup/grounding）**：依 reason 給不同 allowed action 與分桶量測。
- **GUI = pywebview + 內建 server 僅綁 127.0.0.1**（off-host 不可達，防網路攻擊者；不採 inline html= 以免與外部 app.js/cover.jpg/CSP 衝突）+ R41 輸出轉義（防內容注入）二者並用才完整。
- **reviewer = attribution 非 auth**：附逐字免責聲明。

## Open Questions

### Resolved During Planning
- GUI 技術選型：pywebview 6.x + 127.0.0.1-only server。爬蟲多 job：subprocess-per-job。狀態：SQLite 索引 + folder-per-job 0600 明文 blob（加密 post-MVP）。
- 冪等對 LLM 語義：結構/可審等價 + 進 review 後凍結 + SUPERSEDED 重做。
- allowlist 可行性（origin 唯一產品 blocker）：使用者已確認有可合法引用來源 ✅。
- **R42 抹除義務**：使用者確認**無具名法定抹除義務** ✅ → 落盤加密/crypto-shred 降 post-MVP，MVP 採 best-effort unlink + 誠實標註。

### Deferred to Implementation
- **公司 LLM 端點確切規格**（base_url 後綴、模型名、認證 header、串流/限流）：向內部團隊取得；Unit 7a 以 adapter 隔離。
- **目標機 ffmpeg filter 預設值**：落地前跑 `ffmpeg -h filter=blackdetect` 對齊。
- **模糊/黑屏/dedup 門檻數值**：Unit 1 spike + 自家語料校準。
- **grounding 自動驗證強度**（子字串 only vs +NLI/MiniCheck）：由 Unit 1 spike 可達準確度決定（origin Deferred）。
- **（已右整）落盤加密/crypto-shred 移 post-MVP**（使用者確認無具名抹除義務）；MVP 採 best-effort unlink + 誠實標註。未來若出現抹除義務，再做加密機制（file-AES vs SQLCipher）+ DEK 邊界（誰持有、Scrapy 明文窗）+ nonce-vs-checksum 的 spike。
- **目標 OS**：R44 多項硬化（umask/RLIMIT_CORE/start_new_session/killpg）為 POSIX-only；MVP 主目標訂 **macOS/Linux**，Windows 對等（ACL/DPAPI/Job Object）列 post-MVP。

## High-Level Technical Design

> *以下為方向性說明，供審閱驗證設計形狀，非實作規格。*

**三層分層（functional core / I/O adapters / UI shells）** — 純判斷可純測、I/O 由 pipeline 注入：

```
src/lcp/
  core/        # 純核：零框架、零 I/O
    models.py(pydantic 單一真相) · state.py(JobState + transition table + 純轉移驗證) · errors.py(各 error 自帶 exit_code)
    rules/      # 純判斷函式：dedup 評分 · lint 規則 · grounding 子字串比對 · risk 規則（吃資料→回 decision）
  adapters/    # 有副作用的 I/O（非 UI 殼）
    storage/   job_store.py(SQLite,PII-free,WAL) · manifest.py(原子提交+確定性 checksum) · audit_log.py(append-only+雜湊鏈,誠實 tamper-evident) 〔crypto.py 落盤加密=post-MVP〕
    crawler/   base.py(Crawler 抽象契約) · scrapy_impl.py · crawl_runner.py(subprocess/job,最小 env) · ingest.py · net_guard.py(SSRF/path) · source_registry.py(allowlist+授權依據)
    media/     ffprobe.py · normalizer.py(Pillow)
    llm/       client.py(openai-compat) · assembler.py(datamarking 受限改寫)
  pipeline.py  # 編排：注入 adapters、呼叫純核、管理狀態轉移、冪等
  cli.py       # click 薄殼（只讀 error.exit_code）
  gui.py + web/  # pywebview js_api 薄殼 + index.html(CSP)/app.js(textContent 渲染)
data/jobs/<job_id>/{raw,processed,review}/(0600,明文,best-effort 刪除) + manifest.json(PII-free) + audit.jsonl ; data/lcp.db(SQLite,PII-free)
```

**Job 狀態機 transition table（已補架構審查的 SUPERSEDED / retry 出邊 / 拆分矛盾轉移）** —
不變式：唯一上架路徑 `REVIEW_PENDING →approve→ APPROVED →回填→ PUBLISHED_RECORDED`；BLOCKED/DUPLICATE 無上架路徑。

| 狀態 | 事件 | 新狀態 | 觸發者 | 副作用 |
|------|------|--------|--------|--------|
| NEW | crawl ok | CRAWLED | 機 | 寫 raw_job_bundle（0600） |
| NEW | 整頁抽取失敗 | CRAWL_FAILED | 機 | audit retriable=true |
| CRAWL_FAILED | retry | NEW | 人/batch | 可複用已下載 asset |
| CRAWLED | 部分 asset 失敗 | CRAWLED_WARN | 機 | manifest per-asset 狀態 |
| CRAWLED / CRAWLED_WARN | process | PROCESSING | 機 | （PROCESSING 不寫 SQLite，瞬態） |
| PROCESSING | LLM/工具失敗 | PROCESS_FAILED | 機 | audit retriable=true |
| PROCESS_FAILED | retry | PROCESSING | 人/batch | 重跑確定性產物復用 |
| PROCESSING | risk 紅線 | BLOCKED | 機 | 終態；預設不可覆寫 |
| PROCESSING | dedup duplicate | DUPLICATE | 機 | 不得上架（與 BLOCKED 分離以利量測） |
| PROCESSING | risk/dedup/grounding 不確定 | NEEDS_HUMAN_REVIEW(reason) | 機 | 帶 review_reason |
| PROCESSING | lint needs_revision | NEEDS_REVISION | 機 | 列修正點 |
| PROCESSING | 全 pass | PROCESSED | 機 | 寫 processed bundle（0600） |
| NEEDS_HUMAN_REVIEW | clear（依 reason） | PROCESSED | 人 | 裁決寫 audit；grounding 放行後重跑 lint |
| NEEDS_HUMAN_REVIEW | reject | REJECTED | 人 | — |
| NEEDS_REVISION | 原地重跑 | PROCESSING | 人 | 凍結由 **edge-absence** 強制（不存在 REVIEW_PENDING→PROCESSING 邊），非靠 guard |
| NEEDS_REVISION | supersede | SUPERSEDED | 人 | 建 new job、反向連結 |
| PROCESSED | 產 review packet（`review-packet` 指令） | REVIEW_PENDING | 人 | **draft body + cover + title** hash（凍結點） |
| REVIEW_PENDING | approve | APPROVED | 人(reviewer) | 簽核寫 audit（綁 **draft body + cover + title** hash，非僅 title/cover） |
| REVIEW_PENDING | reject | REJECTED | 人 | — |
| REVIEW_PENDING / APPROVED | supersede（要改稿） | SUPERSEDED | 人 | 作廢舊簽核、audit `SIGNOFF_INVALIDATED`、建 new job |
| APPROVED | 回填 published_url+勾選 | PUBLISHED_RECORDED | 人 | 責任閉環完結 |
| REJECTED | （終態） | — | — | 可另開 new job 重做 |
| BLOCKED / DUPLICATE / SUPERSEDED / PUBLISHED_RECORDED | （終態） | — | — | — |

**受限改寫 + 雙向淨化資料流**：
`來源 → [輸入端]清隱形 payload → datamarking 包裹(user)+零能力規則(system) → LLM → 讀 finish_reason → 逐字引用子字串驗證 → claim 級 grounding 驗證 → [輸出端]轉義/CSP/惰性連結 → 人審`。

## Implementation Units

```mermaid
graph TB
  U1[U1 Spike: 偵測/grounding 準確度]
  U2[U2 Scaffold + 三層分層 + config + OS 衛生]
  U3[U3 Job store + 狀態機 + 0600落盤/best-effort刪除 + audit + PII 清單]
  U4[U4 Stage1 crawl/ingest + crawler 契約 + net guards]
  U5[U5 Stage2 asset 驗證 + 媒體標準化]
  U6[U6 Stage2 risk + dedup 閘門]
  U7a[U7a LLM 受限改寫 + 注入防護 + 失敗路徑]
  U7b[U7b draft linter + grounding + 輸出端淨化]
  U8[U8 Stage4 review packet + 簽核 + queue]
  U9[U9 最小 GUI pywebview + 輸出轉義/CSP]
  U2 --> U3 --> U4 --> U5 --> U6 --> U7a --> U7b --> U8 --> U9
  U1 --> U6
  U1 --> U7b
```
（U1→U6/U7b 為**硬依賴**：偵測/grounding 強度是這兩個 unit 的介面決策輸入，非軟參考。U6/U7b 可先實作 advisory 骨架 + pluggable 偵測介面，強度待 U1 結論接入。）

### MVP 優先級（承接 origin 分層，deepening 後重整）

- **P0 — 核心價值路徑（缺了 MVP 不成立）**：U2 骨架、U3（狀態機+job store+audit，**不含加密**）、U4（crawl+基線 SSRF+net_guard）、U5 媒體標準化、U6（risk hard-stop+advisory dedup）、U7a 受限改寫、U7b（lint+grounding 子字串底線+**R41 輸出轉義**）、U8（review packet+簽核）、U9 最小 GUI。
  - ⚠️ **R41 輸出端轉義是 P0，不可砍**——它與加密（R42）是**不同 cost/benefit 的兩條線**，別綁一起評估（product/scope 審查強調）。
- **P1 — 重要、可短暫延後或簡化**：U1 的 +NLI 升級（子字串為底線）、深度 SSRF/DNS-rebinding（基線 SSRF 屬 P0）、PII 清單 R43（cheap，建議仍早做）、crawler 抽象契約（future-facing 保險）、audit 雜湊鏈（基本 append-only 為底線）。
- **post-MVP**：落盤加密 + crypto-shred（R42）、Stage 5/6 自動建草稿/發布、版本 hash 自動比對、Playwright、perceptual-hash 查重、rollback。

### Phase 0 — De-risking spike

- [~] **Unit 1: 合規偵測 / grounding 準確度 spike** — harness 已建並可跑（`spikes/detection_accuracy/`：評測真實確定性偵測器、NLI seam 就緒、合成樣本驗證 mechanics）；**準確度「決策」（substring-only vs +NLI）仍待真實語料 + 模型**。

**Goal:** 量測誹謗/隱私偵測與 grounding 驗證在本地可達的誤判/漏判率，定 R4/R16/R23 偵測策略強度（純規則 vs +NLI/MiniCheck）+ golden set + 閾值。

**Requirements:** R4, R16, R23, Success Criteria（閘門準確度可量測）

**Dependencies:** None（origin 指定為 planning 第一個任務）

**Files:**
- Create: `spikes/detection_accuracy/README.md`、`spikes/detection_accuracy/run_eval.py`
- Create: `spikes/detection_accuracy/golden_set/`（**去識別化**樣本；**禁入版控**，見 R44）
- Test: 不適用（spike 產出為報告與閾值）

**Approach:**
- 30–60 篇代表性樣本，人工標註誹謗/隱私/grounding。比較：(a) 規則詞表；(b) +claim 級 NLI（MiniCheck/SummaC）；(c) 逐字引用子字串驗證。量測 precision/recall，定 fail-closed 保守門檻。
- **明確 outcome 分支**：準確度達標 → 自動閘門接入 U6/U7b；**準確度過低 → MVP 退為 advisory-only + 該 reason 一律 route-to-human（fail-closed），接受較高人審量**，並回報 origin 的定位張力（不卡住整條 pipeline）。
- **依賴影響**：若結論選 (b) +NLI，會引入 MiniCheck/SummaC + transformer runtime（重量級）；應作為**可選 dependency group**，並評估 PyInstaller 打包與離線模型；子字串路徑保持零額外依賴（feasibility 審查）。
- **golden_set 視同 PII**：去識別化、`spikes/` 入 `.gitignore`、處理流程比照 R44。

**Execution note:** 探索性 spike，目標是決策數據；結論回填本計畫 Deferred 項。

**Test scenarios:** Test expectation: none — spike 產出為評測報告與閾值建議，非 feature-bearing。

**Verification:** 產出「策略 × precision/recall × 建議門檻」表；明確結論 grounding 用「子字串 only」還是「+NLI」；golden_set 確認未進版控。

### Phase 1 — Foundation

- [x] **Unit 2: 專案骨架 + 三層分層 + config + OS 衛生基線**

**Goal:** 建立 三層（純核/adapters/UI 殼）骨架、pydantic 模型單一真相、config 與祕密載入、CLI 入口殼、OS 衛生基線。

**Requirements:** R19, R30–R33, R39, R44

**Dependencies:** None

**Files:**
- Create: `pyproject.toml`（鎖版本）、`config.example.yaml`、`.gitignore`（含 `.env`、`data/`、**`spikes/`**）
- Create: `src/lcp/core/models.py`、`src/lcp/core/errors.py`（**各 error 自帶 `exit_code`**）
- Create: `src/lcp/core/config.py`（pydantic-settings；api_key 走 `keyring`；base_url/allowlist/分類/reviewer 白名單由 config）
- Create: `src/lcp/cli.py`（click 薄殼，只讀 `error.exit_code`）
- Create: `src/lcp/runtime_hardening.py`（啟動：`os.umask(0o077)`、`RLIMIT_CORE=0`、log 祕密遮罩 filter、最小 env helper）
- Test: `tests/test_config.py`、`tests/test_cli_skeleton.py`、`tests/test_hardening.py`

**Approach:**
- core 不 import click/pywebview/scrapy；UI 殼只解析→呼叫 core→格式化。
- 全域旗標 `--config --dry-run --json --verbose --quiet --job-id --output-dir`；exit code 0–5（R31）。
- **OS 衛生（R44）**：`os.umask(0o077)` 在任何寫檔/spawn 前；`RLIMIT_CORE=0` 禁 core dump；log 祕密遮罩；提供 `minimal_env()` 給所有 subprocess 用。

**Patterns to follow:** functional core / imperative shell；pydantic-settings；error 自帶 exit_code。

**Test scenarios:**
- Happy path：合法 config → 型別正確設定物件；`--help` 列出指令與旗標。
- Edge case：缺 api_key（keyring/env 皆無）→ exit 3，訊息**不含祕密**。
- Error path：config 未知欄位/格式錯 → exit 2 並指出欄位。
- Integration：`--verbose` 下傳含假 api_key 字串到 log → 斷言被遮罩；斷言啟動後 `RLIMIT_CORE`=0。
- Integration：`minimal_env()` 產出的 env **不含** 既有祕密環境變數。

**Verification:** core 無框架 import（grep/測試斷言）；umask/core-dump/遮罩生效；`.gitignore` 含 spikes/。

- [x] **Unit 3: Job store + 狀態機 + 0600 落盤/best-effort 刪除 + audit + PII 清單**

**Goal:** 持久層：SQLite 索引（PII-free）、folder-per-job **0600 明文 blob（落盤加密=post-MVP）**、原子提交 manifest、append-only audit（雜湊鏈，誠實 tamper-evident）、完整 transition table（含 SUPERSEDED/retry）、**PII 清單為 gating**。

**Requirements:** R10, R11, R27–R29, R32, R38, R42, R43

**Dependencies:** Unit 2

**Files:**
- Create: `src/lcp/core/state.py`（`JobState` enum + transition table + 純轉移驗證 + `review_reason`/SUPERSEDED）
- Create: `src/lcp/adapters/storage/job_store.py`（SQLite，**僅 PII-free 欄位**：job_id/state/hash/timestamp；**PII-free by construction，故不需 secure_delete**；**WAL 模式 + 每執行緒/進程獨立連線 + busy_timeout**（CLI/GUI/背景 thread 並發）；PROCESSING 不入庫）
- Create: `src/lcp/adapters/storage/manifest.py`（temp+`os.replace` 原子提交；**確定性 stage 用內容 checksum 冪等**）
- Create: `src/lcp/adapters/storage/audit_log.py`（append-only JSONL；雜湊鏈（**誠實 tamper-evident**）；**PII 不入 audit、只 hash 高熵 artifact 內容、不雜湊裸識別碼**；刪除記 `ERASURE` 事件）
- Create: `docs/security/pii-inventory.md`（**gating**：列舉每個 PII sink — raw/processed/review/SQLite 欄位/manifest/audit/log/temp/keyring — 各自「可證 PII-free」或「best-effort 刪除涵蓋」；**Unit 3 程式碼合併前須完成並經審閱**）。SQLite **允許欄位**：job_id/state/各 hash/timestamp/error_code；**禁止**：title/正文/來源 URL/作者/網域/review_reason 文字（reason 以 enum code 存，非文字）。
- _(post-MVP)_ `crypto.py`（per-job DEK 加密落盤 + crypto-shred）——MVP 不做（無抹除義務）；MVP 刪除為 best-effort unlink、job 目錄 0600
- Test: `tests/test_state_machine.py`、`tests/test_job_store.py`、`tests/test_manifest_idempotency.py`、`tests/test_audit_log.py`、`tests/test_best_effort_deletion.py`（unlink + ERASURE 事件 + **不宣稱密碼學抹除**斷言）

**Approach:**
- 狀態機依 transition table；非法轉移拋錯；SUPERSEDED 與 retry 出邊齊全。
- 冪等：**僅確定性 stage**用內容 checksum 跳過；LLM 草稿不參與（靠凍結）。`.processing` 標記檔偵測崩潰。
- **PII 落地（R42，MVP 右整版）**：job 目錄 0600、temp 在 job 目錄內（非 `/tmp`，R44）；刪除為 **best-effort unlink + 明文標註不保證 SSD 抹除**；PII-free SQLite/manifest、敏感與 audit 分離。**落盤加密/crypto-shred 移 post-MVP**。
- **PII 清單（R43）**：先產 `pii-inventory.md` 再實作儲存；每個 sink 可證 PII-free 或由 best-effort 刪除涵蓋。

**Execution note:** 先寫狀態機非法轉移失敗測試與 SQLite/manifest PII-free 斷言測試（test-first）。

**Test scenarios:**
- Happy path：job 全合法轉移 NEW→…→PUBLISHED_RECORDED，每步寫 audit。
- Edge case（冪等）：重跑確定性 stage（內容未變）→ checksum 命中跳過、不覆蓋。
- Edge case（凍結）：進 REVIEW_PENDING 後嘗試原地重跑 Stage 2 → 拒絕，要求 supersede。
- Edge case（supersede）：APPROVED 改稿 → SUPERSEDED + `SIGNOFF_INVALIDATED` + new job 反向連結。
- Edge case（retry）：CRAWL_FAILED→NEW、PROCESS_FAILED→PROCESSING 合法且復用既有產物。
- Edge case（崩潰）：留 `.processing`+半寫 temp → 重啟重做該 stage，無壞檔；PROCESSING 不在 SQLite。
- Error path：非法轉移（BLOCKED→APPROVED）→ 拋錯。覆寫既有 raw（R11）→ 拒絕。
- Integration（刪除）：刪 job → best-effort unlink；audit 記 `ERASURE` 事件、雜湊鏈仍可驗證；**MVP 不宣稱密碼學抹除**（誠實標註）。
- Integration（audit 防竄改）：竄改中間一筆 → 雜湊鏈偵測（誠實 tamper-evident；本地 root 不可防，已標明）。
- Integration（PII-free）：掃 SQLite/manifest 欄位 → 斷言無原始 PII（只有 hash/狀態）。

**Verification:** 所有旁支有出邊；冪等僅限確定性 stage；**刪除為 best-effort 並誠實標註**；SQLite/manifest PII-free；pii-inventory.md 完成且每 sink 有歸屬。

### Phase 2 — Stage 1 Crawl / Ingest

- [x] **Unit 4: Crawl（Scrapy subprocess）+ crawler 抽象契約 + 本地匯入 + 網路/路徑護欄**

**Goal:** 三種輸入 → raw_job_bundle（0600）；allowlist、限速、robots、SSRF/path-traversal、per-asset 狀態、不繞反爬；**定義 crawler 抽象契約（Scrapy 為第一實作）**。

**Requirements:** R1, R2, R6–R11, R40, R44

**Dependencies:** Unit 3

**Files:**
- Create: `src/lcp/adapters/crawler/base.py`（**Crawler 抽象契約**：輸入 source spec + job dir；輸出 raw_job_bundle + per-asset 狀態）
- Create: `src/lcp/adapters/crawler/scrapy_impl.py`、`crawl_runner.py`（subprocess-per-job + 逾時 + **`minimal_env()`**）
- Create: `src/lcp/adapters/crawler/net_guard.py`（SSRF：解析後 IP `is_global` + 釘 IP 連線 + 關 redirect + scheme allowlist；path：resolve+`is_relative_to`）
- Create: `src/lcp/adapters/crawler/source_registry.py`（allow_domains + 每來源授權依據）、`ingest.py`
- Test: `tests/test_net_guard.py`、`tests/test_crawl_runner.py`、`tests/test_ingest.py`、`tests/crawler/test_contract.py`

**Approach:**
- Scrapy 設定：`ROBOTSTXT_OBEY=True`、AutoThrottle、`allowed_domains`、`RETRY_*`、`DOWNLOAD_TIMEOUT`、`IMAGES_STORE`/`FILES_STORE` 指向該 job 目錄（0600）。
- subprocess：`subprocess.run([sys.executable,"-m",...], timeout=..., env=minimal_env())`；**umask 須在 spawn 前已設**（R44 / 架構審查 C2），下載媒體落盤 0600。
- net_guard 前置驗 URL；命中反爬即記錄並跳過（R8）。必要欄位門檻（R9）：標題或正文缺→needs_revision；整頁失敗→CRAWL_FAILED；per-asset 狀態寫 manifest。

**Execution note:** 先寫 net_guard SSRF/path-traversal 失敗測試（安全關鍵，test-first）。

**Patterns to follow:** subprocess-per-job + minimal env；`is_global`+釘 IP；`resolve()+is_relative_to`；crawler 契約。

**Test scenarios:**
- Happy path：allowlist 內單 URL → raw_job_bundle（0600）+ sha256；本地資料夾匯入無需網路。
- Edge case：重複 URL 跳過；部分 asset 失敗 → CRAWLED_WARN + per-asset 狀態。
- Edge case：標題有正文空 → needs_revision；整頁失敗 → CRAWL_FAILED retriable。
- Error path（SSRF）：`169.254.169.254`/`127.0.0.1`/`10.x`/十進位 IP/`*.nip.io` → 拒絕。
- Error path（rebinding）：驗證時公網、連線時內網 → 釘 IP 擋下。
- Error path（redirect）：302 跳內網 → 擋下。
- Error path（path）：清單檔/資料夾含 `../`/symlink 出 base → 拒絕。
- Error path：domain 不在 allowlist → 拒絕記 audit。
- Integration：命中 robots/反爬 → 記錄跳過不繞過；subprocess env 不含祕密；落盤 0600。
- Integration（契約）：以 fake crawler 實作契約 → pipeline 不需改動即可替換（證明接縫真實）。

**Verification:** raw_job_bundle（0600）不覆蓋既有 job；SSRF/path 全綠；per-asset 狀態入 manifest；crawler 契約可替換。

### Phase 3 — Stage 2 Process / Normalize

- [x] **Unit 5: 素材驗證 + 媒體標準化（Pillow + ffprobe）**

**Goal:** 素材完整性（含模糊/黑屏/解壓炸彈護欄）、圖片 800px + 1300×640 封面、影片規格檢查。純判斷與 I/O 分離。

**Requirements:** R12–R15, R44

**Dependencies:** Unit 4

**Files:**
- Create: `src/lcp/core/rules/asset_rules.py`（**純判斷**：是否過小/模糊門檻、規格是否合規）
- Create: `src/lcp/adapters/media/normalizer.py`（Pillow I/O：exif_transpose→thumbnail(LANCZOS)→save；封面 ImageOps.fit）
- Create: `src/lcp/adapters/media/ffprobe.py`（subprocess argv + `minimal_env()` + json 解析 + rational 幀率 + black/silence + process-group 逾時）
- Test: `tests/rules/test_asset_rules.py`、`tests/media/test_normalizer.py`、`tests/media/test_ffprobe.py`

**Approach:**
- 圖片：`MAX_IMAGE_PIXELS` 調低 + warning 升 error；exif_transpose 必先；正文 800px 等比、封面 1300×640 固定切版（1/2/3/4）。
- 影片：先廉價 ffprobe（在 allowlist + 是媒體）再決定跑貴的 blackdetect/silencedetect；argv list、`-nostdin`、`start_new_session`+killpg、`-t` 限量、`minimal_env()`。
- 命中（模糊/黑屏/壞檔）預設 needs_revision；per-asset 粒度寫 manifest。**純門檻判斷在 `asset_rules`，I/O 在 normalizer/ffprobe**（架構審查 4）。

**Patterns to follow:** Pillow exif→thumbnail→save；ffprobe subprocess 硬化；純判斷/IO 分離。

**Test scenarios:**
- Happy path：正常圖 → 800px 等比；4 張 → 1300×640 封面無變形；合規影片 → ffprobe 解析正確。
- Edge case：EXIF 方向圖 → 方向正確；幀率 `30000/1001` → 正確解析；1 張來源 → 封面退化規則正確。
- Error path（炸彈）：超大像素圖 → DecompressionBombError 進 report，不 OOM。
- Error path（惡意媒體）：卡死媒體 → process-group 逾時殺光、無殭屍。
- Error path：缺 ffmpeg/ffprobe → exit 3。
- Integration：含黑屏影片 → blackdetect 從 stderr 抓區間。
- Unit（純）：asset_rules 門檻判斷可不碰 I/O 純測。

**Verification:** 尺寸/規格正確；不可信媒體有資源/逾時護欄；純判斷可純測。

- [x] **Unit 6: 風控 + 查重閘門**

**Goal:** hard-stop 風控（紅線 + 日常誹謗/隱私/著作權，fail-closed）+ advisory/fail-loud 查重；輸出帶 `review_reason`。純判斷與索引 I/O 分離。

**Requirements:** R3–R5, R20–R22, R36

**Dependencies:** Unit 3, Unit 5；**硬依賴 Unit 1**（偵測強度）

**Files:**
- Create: `src/lcp/core/rules/risk_rules.py`、`src/lcp/core/rules/dedup_rules.py`（**純判斷**：紅線比對、MinHash 評分、reliability 計算）
- Create: `src/lcp/adapters/processor/risk_checker.py`、`dedup_checker.py`（I/O 編排：載入索引、呼叫純判斷）
- Test: `tests/rules/test_risk_rules.py`、`tests/rules/test_dedup_rules.py`、`tests/processor/test_dedup_index.py`

**Approach:**
- risk：紅線→BLOCKED（終態不可覆寫）；日常偵測採 Unit 1 結論；不確定/不可用→fail-closed needs_human_review(reason=risk)；校園分類預設停用。
- dedup：便宜優先級聯；兩組查重詞（R21）；duplicate→DUPLICATE；uncertain→needs_human_review(reason=dedup)；**無站內索引→`dedup_reliability=low`+警告、只 auto-pass 明確非重複、絕不 auto-reject**（R36）。
- **pluggable 偵測介面**：U1 未定前實作 advisory 骨架 + 介面，強度後接（架構審查 5）。門檻自家語料校準。

**Execution note:** 紅線 hard-stop 與 fail-closed 先寫失敗測試（合規關鍵，test-first）。

**Patterns to follow:** advisory + human-review fallback；MinHash/LSH 級聯；fail-closed；純判斷/IO 分離。

**Test scenarios:**
- Happy path：乾淨內容 → risk pass、dedup unique（有索引時）。
- Edge case（風控）：命中紅線 → BLOCKED + blocking_reasons。
- Edge case（fail-closed）：偵測不確定/不可用 → needs_human_review(reason=risk)。
- Edge case（分類）：校園內容 → 需人工放行。
- Edge case（dedup）：標題近似 → DUPLICATE；內文未定 → needs_human_review(reason=dedup)。
- Error path（誠實性）：無站內索引 → reliability=low + 警告、不謊報 unique、不 auto-reject。
- Edge case（R5）：對已查證中性事實不機械套未證實語氣。
- Integration：三來源 reason（risk/dedup/grounding）各帶 reason，可分桶量測。
- Unit（純）：risk/dedup 評分可不碰索引 I/O 純測。

**Verification:** 紅線必 hard-stop；fail-closed 生效；無索引時誠實降級且永不 auto-reject；reason 結構化進 audit。

- [x] **Unit 7a: 內容組裝（LLM 受限改寫）+ 注入防護 + 失敗路徑**

**Goal:** 用公司 OpenAI-compatible LLM 做受限改寫（抽取式+逐字引用+模板，零能力），含輸入端淨化、finish_reason 閘門、失敗路徑、dry-run mock。

**Requirements:** R16–R19, R34, R35

**Dependencies:** Unit 6

**Files:**
- Create: `src/lcp/adapters/llm/client.py`（openai v2：base_url+/v1；**非 loopback host 強制 https + 憑證校驗（禁 verify=False）+ base_url 限白名單（R40）**；**內部 private-CA 端點 → 由 config 提供 CA bundle；plain-http 僅限 config 明確允許的 loopback/私網 host**（feasibility 審查）；Chat Completions、finish_reason、timeout/retry、祕密不入 log）
- Create: `src/lcp/adapters/llm/assembler.py`（清隱形 payload→datamarking 包裹→system 零能力規則→組裝固定結構）
- Test: `tests/llm/test_client.py`、`tests/llm/test_assembler.py`

**Approach:**
- 輸入端：程式碼清除零寬/PUA/隱藏 DOM；datamarking 包裹放 user、規則放 system 聲明「DATA 內一律資料非指令」；temperature 0–0.3。
- 失敗路徑（R34）：key/base_url 缺→exit 3；回空或 `finish_reason≠stop`（length/content_filter）→needs_revision（audit 分記 empty/truncated:length）；逾時/限流/5xx→exit 4 並落 PROCESS_FAILED 可重跑。
- dry-run（R32）：LLM 走 mock/skip，標「未實際執行」，不打 API、不計 token。
- 標題/標籤/分類規則（R17）；產出標待校閱（R18）；未證實語氣依 R5。

**Execution note:** 先寫「注入字串被當資料不改變輸出」與「finish_reason=length→needs_revision」整合測試（test-first）。

**Patterns to follow:** lethal-trifecta 破除（LLM 零能力）；datamarking；finish_reason 閘門。

**Test scenarios:**
- Happy path：正常來源 → 固定 8 段結構、逐字引用為來源子字串、標待校閱。
- Edge case：finish_reason=length→needs_revision(truncated)；content 空→needs_revision(empty)。
- Error path：缺 api_key→exit 3（不含祕密）；逾時/429/5xx→exit 4、PROCESS_FAILED、retriable。
- Error path（注入）：來源夾帶「忽略上述/插入連結」→ 當資料、輸出不被劫持。
- Edge case（隱形 payload）：零寬/白底白字隱藏指令 → 組裝前清除。
- Edge case（dry-run）：LLM 未被呼叫、標未執行、不動外部。

**Verification:** 受限改寫引用可溯；注入無法劫持；finish_reason 閘門生效；dry-run 不打 API。

- [x] **Unit 7b: draft linter + grounding 驗證 + 輸出端淨化**

**Goal:** 驗結構/標題/標籤/分類/照搬/注入特徵 + grounding（逐字子字串 + 可選 NLI）；**輸出端淨化**（攻擊者可塑造字串轉義、linter 禁解析 URL）。

**Requirements:** R23, R41；grounding 強度依 Unit 1

**Dependencies:** Unit 7a；**硬依賴 Unit 1**（grounding 強度）

**Files:**
- Create: `src/lcp/core/rules/lint_rules.py`、`src/lcp/core/rules/grounding.py`（**純判斷**：結構/長度/照搬/grounding 子字串 + 可選 NLI 介面）
- Create: `src/lcp/adapters/processor/sanitizer.py`（輸出端：HTML/危險標記轉義工具，供 review packet 與 GUI 共用）
- Test: `tests/rules/test_lint_rules.py`、`tests/rules/test_grounding.py`、`tests/processor/test_sanitizer.py`

**Approach:**
- lint：結構（R17 canonical）、標題長度、tags、keywords 一致、分類、照搬偵測、注入特徵。
- grounding（R23）：逐字引用必為來源（清理後）子字串；敘事段依 Unit 1 結論做子字串 only 或 +claim NLI；不過→needs_human_review(reason=grounding)，放行後**重跑 lint**（架構審查 2d）。
- **輸出端淨化（R41）**：sanitizer 把攻擊者可塑造字串（draft/title/scraped/URL）轉成安全可渲染形式；**linter/grounding MUST NOT 解析任何 URL**（純字串比對，加負向測試釘死，安全審查 A5）。

**Execution note:** 先寫「draft/title 含 `<script>`/`onerror` → 經 sanitizer 後惰性」與「linter 不對任何 URL 發出網路請求」測試（安全關鍵，test-first）。

**Patterns to follow:** 純判斷/IO 分離；輸出端轉義；逐字子字串 + 可選 NLI。

**Test scenarios:**
- Happy path：合規草稿 → lint pass。
- Edge case（lint）：標題超長/tags 數不符/分類不在 config/照搬來源段落 → needs_revision/blocked。
- Edge case（grounding）：出現來源沒有的指控 → needs_human_review(reason=grounding)；放行後重跑 lint。
- Error path（輸出注入）：draft/title 含 `<script>`/`<img onerror>` → sanitizer 轉義為惰性文字。
- Error path（SSRF via linter）：draft/source 含 URL → linter/grounding **不發任何網路請求**（負向斷言）。
- Integration：完整 Stage 2（驗證→閘門→組裝→lint→淨化）產 processed bundle（0600；draft.md + reports）。
- Unit（純）：lint/grounding 判斷可不碰 I/O 純測。

**Verification:** lint/grounding 正確；輸出端轉義生效；linter 絕不解析 URL（測試釘死）。

### Phase 4 — Stage 4 Review & Sign-off

- [x] **Unit 8: Review packet + 簽核 + 上架責任閉環 + queue/list**

**Goal:** 人面向審核包（**淨化後**）、approve/reject（reviewer attribution）、published_url 回填責任閉環、batch↔人工閘門銜接（待辦清單 + summary）。

**Requirements:** R24–R26, R37, R41；解 flow gap G5/G6/G7

**Dependencies:** Unit 7b

**Files:**
- Create: `src/lcp/adapters/publisher/review_packet.py`（cover.jpg、title.txt、review_message.txt、**來源連結以惰性純文字**；**經 sanitizer**；review_manifest.json + **draft body/title/cover hash**；**0600 job 目錄、best-effort 刪除**）
- Create: `src/lcp/adapters/publisher/signoff.py`（approve/reject；reviewer 白名單 + `pwd.getpwuid` 觀測值 + 逐字免責；published_url 回填）
- Modify: `src/lcp/cli.py`（新增 `review-packet`/`approve`/`reject`/`list`/`run`）
- Create: `src/lcp/pipeline.py`（`run --until draft|review` 編排；batch summary counts-by-state；注入 adapters）
- Test: `tests/publisher/test_review_packet.py`、`tests/publisher/test_signoff.py`、`tests/test_pipeline_batch.py`

**Approach:**
- review_packet：review_message.txt 由模板+草稿關鍵欄位組出（非 linter 原始輸出）、**所有攻擊者可塑造欄位經 sanitizer**、**來源連結以惰性純文字呈現（不可點/不自動 fetch）**、優先在 GUI 內消費（R41，含檔案讀取路徑——安全審查 A）；記 **draft body + title + cover hash**；packet 在 0600 job 目錄、刪除 best-effort（安全審查 B3）。
- 簽核：reviewer 須在白名單；audit 記 `reviewer_stated`+`observed_os_user`+**綁 draft body/title/cover hash**+seq；附逐字免責（attribution 非 auth）。
- 責任閉環（R37）：approve 後回填 published_url + 勾選「上架=已簽核版本」→ PUBLISHED_RECORDED；未回填不完結；CLI 提供對等 backfill 指令。
- batch↔人工（G5/G7）：`list --state pending|blocked|needs-review|duplicate` 待辦；batch 結束輸出 counts-by-state summary；MVP 拉取式（無 push）。

**Patterns to follow:** attribution + 免責；artifact SHA-256 綁簽核版本；輸出端淨化。

**Test scenarios:**
- Happy path：PROCESSED → 完整 review packet（含 hash、已淨化）；approve→APPROVED；回填+勾選→PUBLISHED_RECORDED。
- Edge case：reviewer 不在白名單 → 拒絕記 audit；approve 後未回填 → 停 APPROVED、list 可見、summary 計未完結。
- Edge case（supersede）：APPROVED 改稿 → SUPERSEDED + SIGNOFF_INVALIDATED + new job。
- Error path：對 BLOCKED/DUPLICATE/needs_review 直接 approve → 拒絕（狀態機）。
- Error path（輸出注入）：含 `<script>` 的 title 進 review packet → 已轉義。
- Integration（batch）：多 job → summary 正確分類 counts。
- Integration（CLI/GUI 對等）：複查裁決/重跑/resume/回填皆有 CLI 對等指令。

**Verification:** 未經 approve 無上架路徑；簽核綁 draft body/title/cover hash；packet 已淨化且在 0600 job 目錄；batch 後有待辦清單與 summary。

### Phase 5 — Interface

- [x] **Unit 9: 最小 GUI（pywebview serverless）+ 輸出轉義/CSP**

**Goal:** 不開埠的桌面視窗，完成建/跑/看/approve/reject/複查裁決/重跑/resume/回填，與 CLI 共用 core，顯示狀態計數/待辦；**所有渲染輸出端轉義 + CSP**。

**Requirements:** R33, R25, R37, R41；解 flow gap G6/G7

**Dependencies:** Unit 8

**Files:**
- Create: `src/lcp/gui.py`（pywebview `create_window`+`js_api`，長任務丟背景 thread + 狀態檔輪詢）
- Create: `src/lcp/web/index.html`（**嚴格 CSP：`script-src 'self'`、`img-src 'self'`、無 inline、`object-src 'none'`**）、`src/lcp/web/app.js`（**一律 `textContent`/轉義、禁 `innerHTML`；來源連結惰性文字**）
- Test: `tests/test_gui_api.py`、`tests/web/test_render_escaping.py`

**Approach:**
- **載入模型（feasibility 審查更正）**：用 pywebview 內建 HTTP server **僅綁 127.0.0.1**（loopback、off-host 不可達）——可正常載入外部 `app.js`/`cover.jpg` 並滿足 CSP `script-src 'self'`。**不採 inline `html=`**：其 serverless 模式無法存取檔案系統，會與「外部 app.js / 顯示 cover.jpg / no-inline CSP」三者同時衝突。CSP 需含 `img-src 'self'`。GUI 與 CLI 共用 core 與 sanitizer（R41）；js_api handler 各執行緒**各自開 SQLite 連線**。
- **輸出轉義（R41，安全審查 A）**：攻擊者可塑造字串一律 `textContent`/經 sanitizer；CSP 縱深防禦；來源連結渲染為不可自動 fetch 的惰性文字（防 A4 SSRF/beacon）。
- GUI×CLI 能力矩陣（G6）：建/跑/看/approve/reject/**複查裁決(依 reason)**/重跑/resume/回填 全部可做。
- reviewer 白名單下拉 + 顯示免責；首頁狀態計數 + 待辦清單（G7）。長任務背景 thread + 輪詢，不卡 UI。

**Execution note:** GUI 為薄殼，business `if` 在 core；js_api 只「解析→呼叫 core→回傳」。先寫渲染轉義測試（安全關鍵）。

**Patterns to follow:** pywebview js_api serverless；functional core 共用；textContent 渲染 + CSP。

**Test scenarios:**
- Happy path：js_api `create_job/run/get_packet/approve` → 正確呼叫 core 並回傳。
- Edge case：needs_human_review(reason=dedup) → GUI 提供對應裁決動作。
- Edge case：長任務進行 → UI 不凍結，輪詢更新。
- Error path（XSS）：draft/title/scraped 含 `<script>`/`<img onerror>` → 以 textContent 渲染、**js_api 未被呼叫**（核心安全斷言）。
- Error path（惰性連結）：來源連結不自動 fetch。
- Error path：core 拋錯 → GUI 顯示可讀錯誤，不洩漏祕密/堆疊。
- Integration：CLI approve 後 GUI 狀態計數即時反映（共用 SQLite）。

**Verification:** GUI server **僅綁 127.0.0.1**（測試斷言非 0.0.0.0、off-host 不可達）；攻擊者文字無法執行/呼叫 js_api；CSP 生效；CLI 能做的 GUI 皆可做。

## System-Wide Impact

- **Interaction graph:** pipeline.py 注入 adapters、編排 stage、管理狀態轉移；CLI 與 GUI 兩殼共用 core + sanitizer + SQLite；crawler/媒體解析以 subprocess + minimal env 隔離；LLM 為唯一外呼且零能力。
- **Error propagation:** error 自帶 exit_code；stage 失敗落可重跑持久狀態（非停 PROCESSING）；外部失敗標 retriable，內部錯 exit 5。
- **State lifecycle risks:** 非確定 LLM → 進 REVIEW_PENDING 後凍結、改稿走 SUPERSEDED；部分成功用 per-asset 狀態；崩潰只從磁碟最後原子提交重建。
- **Security boundary（信心檢查重點）:** 三要素須在**全鏈**不被重建——LLM 零能力 + 輸出端轉義/CSP/惰性連結（含檔案讀取路徑）+ linter 禁解析 URL + 子進程 minimal env。PII 在 0600 job 目錄、PII-free 索引、刪除為 best-effort（誠實標註，不宣稱密碼學抹除；加密 post-MVP）。
- **API surface parity:** 每個 CLI 動作須有 GUI 對等（Unit 9 矩陣）；core 單一真相避免漂移。
- **Integration coverage:** 端到端 pipeline + 安全路徑（XSS/SSRF/炸彈/注入/best-effort 刪除）需整合測試（mock 證不了）。
- **Unchanged invariants:** MVP 機器零寫入外部系統、不自動發布、不繞反爬。

## Risks & Dependencies

| Risk | 真有效/裝飾 標記 | Mitigation |
|------|------------------|------------|
| **輸出端 XSS → js_api → core（Critical，信心檢查新增）** | 輸出轉義+CSP+惰性連結 = **真有效**；只靠「不開埠」= **裝飾** | R41：textContent/轉義、CSP、惰性連結、linter 禁解析 URL、子進程 minimal env |
| PII 落地刪除（右整為 MVP 範圍） | best-effort unlink + 誠實標註 = **真有效（不裝飾）**；明文+宣稱抹除 = **裝飾** | R42 右整：MVP 不做加密、**不宣稱抹除**；PII-free SQLite + 分離 + PII 清單；加密/crypto-shred 移 post-MVP（經確認無抹除義務） |
| **PII 無清單 → shred 不可驗（High，新增）** | PII 清單 gating = **真有效** | R43：pii-inventory.md 列每個 sink，要嘛在 shred 邊界要嘛可證 PII-free |
| Prompt injection 劫持 LLM | 破 lethal trifecta = **真有效** | LLM 零能力 + 輸入清洗 + datamarking + 人審；偵測器僅 triage |
| LLM 杜撰/不忠實 | NLI/子字串 = **真有效**；「有引用」= **裝飾** | 逐字子字串 + claim NLI（強度依 U1）+ 人審 |
| 查重無索引時失效 | fail-loud advisory = **真有效**；靜默 unique = **裝飾** | reliability=low、永不 auto-reject、只 auto-pass 明確非重複 |
| audit 殘留可識別資訊（新增） | PII 不入 audit + 只 hash 高熵內容 = **真有效**；雜湊裸識別碼 = **裝飾** | PII 不入 audit；只 hash 高熵 artifact；雜湊鏈誠實 tamper-evident（本地 root 不可防，已標明） |
| api_key 外洩（commit/log/crash/subprocess） | keyring+遮罩+minimal env+禁 core dump = **真有效**；.gitignore 事後 = **裝飾** | R44：keyring、log/crash 遮罩、minimal env、RLIMIT_CORE=0、洩漏要輪換 |
| SSRF / DNS-rebinding | 釘 IP+is_global = **真有效**；hostname 黑名單 = **裝飾** | 解析→驗 IP→連字面 IP、關 redirect、scheme allowlist、safehttpx；**連結渲染惰性化防下游 SSRF** |
| 不可信媒體炸彈/RCE | subprocess+rlimit+炸彈護欄+minimal env = **真有效** | Pillow 護欄、ffprobe process-group 逾時、defusedxml、資源上限、最小 env |
| temp/core dump/swap PII 殘留（新增） | temp 在 job 0600 目錄 + 禁 core dump = **真有效** | R44：temp 非 /tmp、RLIMIT_CORE=0；swap PII 殘留為已知殘餘風險，文件建議操作者啟用 OS 層加密 swap（MVP 不處理） |
| 簽核可冒名（單機無 auth） | 誠實標 attribution = **真有效**（認清限制） | pwd.getpwuid + 白名單 + 逐字免責；多人/法律約束時升真認證 |
| 合規偵測準確度過低 | spike 先量 = **真有效** | Unit 1 spike；過低回報定位張力 |
| 公司 LLM 介面不相容 | adapter 隔離 = **真有效** | llm/client adapter；最低共同分母參數；先取規格 |
| crawler 接縫變空話 | 抽象契約 = **真有效** | Unit 4 定 Crawler 契約，Scrapy 為第一實作，fake 實作測試釘死 |
| golden_set 真 PII 誤入版控（新增） | spikes/ gitignore + 視同 PII = **真有效** | R44：`.gitignore` 含 spikes/、去識別化、README 規範 |

## Documentation / Operational Notes

- **config.example.yaml** 含 allow_domains（+授權依據）、媒體/內容參數、risk hard_stop、reviewer 白名單；api_key 走 keyring 不入檔。
- **docs/security/pii-inventory.md** 為 gating 交付（R43），落地前須完成。
- **桌面打包**：PyInstaller 出 pywebview 單視窗；spec **排除真 config / 祕密 / spikes/**（R44）；註明 Win 需 WebView2、各平台 webview backend 各自測。
- **首次安裝 = 一次性技術 setup**（api_key 入 keyring / allowlist+授權依據 / reviewer 白名單 / 權限），與日常非技術操作分開。allowlist 擴充與 reviewer 編輯雖偶發但屬技術操作，需在文件標明由誰負責（adversarial 審查）。
- **批次/cron**：拉取式待辦（`list` + summary），MVP 無 push 通知。
- **PII at-rest 誠實揭露**：`data/jobs/` 為**明文、僅 0600 保護**；操作者文件須明寫此事實 + GUI 刪除動作標「**best-effort，SSD 上不保證抹除**」，並建議操作者自行啟用 OS 全碟加密（安全審查）。
- **R42 抹除義務確認**：本計畫的「無具名法定抹除義務」結論應由 PM/法務**書面確認**並記於 `pii-inventory.md`，使此假設可稽核（非僅對話）（adversarial 審查）。
- **操作者自助 vs 技術歸屬**（待 PM 決）：allowlist 擴充、reviewer 白名單編輯是否進 GUI（提高 Unit 9 範圍）讓非技術操作者自助，或指派具名技術負責人——MVP 前須擇一。

## Sources & References

- **Origin document:** [docs/brainstorms/2026-06-16-local-content-processor-requirements.md](docs/brainstorms/2026-06-16-local-content-processor-requirements.md)
- **信心檢查（deepen）**：架構整合性審查（SUPERSEDED/retry 出邊/三層分層/Unit 7 拆分/crawler 契約/冪等界定）；安全威脅模型審查（輸出端 XSS→js_api Critical、crypto-shredding 加密層 Critical、PII 清單、OS 衛生）。
- 外部研究（節錄）：Scrapy/Pillow/ffmpeg/openai/pywebview 官方文件；lethal trifecta（simonwillison.net）；Spotlighting（arxiv 2403.14720）；Extractive is not Faithful（arxiv 2209.03549）；MiniCheck；OWASP SSRF Cheat Sheet；safehttpx；datasketch MinHash；crypto-shredding（NIST SP 800-88 Purge）；functional core/imperative shell。
