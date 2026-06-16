---
date: 2026-06-16
topic: local-content-processor
---

# Local Content Processor — 需求文件（MVP）

> 一句話定位：這**不是自動發文器**，而是一條**本地內容流水線** ——
> 抓取素材 → 標準化處理 → 風控/查重 → 產出可審核的草稿包，**由人工貼進自家後台上架**。

## Problem Frame

**誰在用、解決什麼**
經營者（**非工程背景**）目前要從外部公開來源，手動把素材（圖、文、影片）整理成符合自家網站規格的
文章，過程零散、無紀錄、品質不一、也容易誤觸法律紅線。需要一條**可重跑、可追蹤**的本地 pipeline，
把「找料 → 加工 → 待審」自動化到「只差人按一下審核」的程度，且操作者**不必寫程式**即可使用。

**為什麼現在做**
內容量要規模化，但**合規與品質**不能規模化地崩壞。先把 job 狀態、audit、草稿、審核閘門做穩，
比一步到位做全自動發布重要得多。

**本次已敲定的框架性決策**（詳見 Key Decisions）
1. 定位＝**自營站 / 合規優先**（不是抓誰都改寫的聚合農場）。
2. MVP 範圍＝**做到 review packet 為止 + 最小 GUI 封裝**，人工手動上架。
3. 文章內容＝用**公司自有大模型 API**（config 設 key + base_url），但**收斂為受限改寫**、且機器產出**必經人工校閱**。
4. 介面＝**CLI 核心 + 最小 GUI / 一鍵封裝**，給非技術經營者操作。

## MVP Pipeline 與邊界

```
[最小 GUI / 一鍵封裝] ——— 包在 CLI 核心外，給非技術經營者操作 ———

INPUT              STAGE 1           STAGE 2 + 閘門                  STAGE 4         上架
─────              ───────           ─────────────                  ───────         ────
--url / --input →  crawl / ingest →  process → risk → dedup →       review     →   人工貼進
--dir              (Scrapy)          normalize  gate    gate        packet          自家後台
                   raw_job_bundle    content_assembler(公司 LLM)    封面+標題       (機器不碰
                                     受限改寫，非自由生成           +draft+來源     後台)
                        │                  │                            │
                        ▼                  ▼                            ▼
                   audit.jsonl ◄──── 每個 stage 都寫 audit ───────►  approve / reject
                                                                       (簽核 + 上架後回填 hash 校驗)

╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌ 以下整組移到 MVP 之後 ╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
STAGE 5  backend draft adapter（自動建草稿、dry-run、截圖為證）
STAGE 6  publish guard + 版本 hash 比對 + --confirm 才發布
其他    Playwright 抓取 adapter、rollback 撤銷機制、image perceptual-hash 查重
```

## Requirements

**內容定位與合規（Positioning & Compliance）**
- R1. 系統定位為**自營站內容 pipeline**：抓取來源僅限 allowlist 內、**公開且可合法引用**的網站。
- R2. 每篇產出**強制保留來源連結與署名/出處欄位**；不得以任何方式掩蓋權利人來源（不做去水印掩蓋）。
- R3. 分類設定中，`學生校園` 類（高機率涉及未成年/非公眾人物）**預設停用**，需經人工逐案放行才可啟用。
- R4. 風控除原有極端紅線（未成年、NCII、偷拍外流、涉政高風險、血腥暴力、侵犯人權）外，
  **新增三項日常風險檢查**：誹謗性陳述、可指認的私人隱私資訊、著作權來源缺漏。
  偵測不確定或不可用時一律 **fail-closed → status=needs_human_review**，不得預設放行。
- R5. **先判定**資訊是否未證實，**再對未證實部分**套用 `網傳 / 疑似 / 被曝 / 據傳` 等保留語氣；
  不得寫成既定事實，也**不得對已查證的中性事實機械套用**（誤套用反而可能構成不實）。

**介面與操作（Interface）**
- R33. 提供**最小 GUI / 一鍵封裝**（如本地網頁或桌面小視窗）包在 CLI 核心外，
  讓非技術經營者能不寫程式完成「建 job → 跑處理 → 看 review packet → approve/reject」；
  CLI 仍為自動化（batch/cron）的主要入口，GUI 與 CLI 共用同一核心邏輯。
  **初始設定**（api_key/base_url/allowlist/reviewer 白名單/權限硬化）為**一次性技術 setup**，與日常非技術操作分開
  （需求只承諾「日常操作不寫程式」，不承諾「零技術安裝」）。GUI 須提供 **reviewer 身分選擇/識別**，
  使 R25 簽核歸屬可成立；若採本地 web UI，**須綁定 127.0.0.1、不對外暴露**（見 R40）。

**Stage 1 — 抓取 / 匯入（Crawl / Ingest）**
- R6. 支援三種輸入：單一 URL、URL 清單檔、本地素材資料夾。
- R7. Crawler **MVP 以 Scrapy 為唯一抓取引擎**（極簡頁可退化用 requests+BS4），
  支援 allowlist domain、rate limit、retry/backoff、timeout、user-agent 設定、robots.txt respect、重複 URL 跳過。
- R8. 不繞過登入、驗證、付費牆或反爬機制（命中即視為不可抓取，記錄並跳過）。
- R9. 每個任務嘗試取得：source_url、標題、正文、發布時間、圖片、影片、作者/帳號、tags/分類、source_html，
  截圖為選配。**需明訂必要欄位門檻**：標題或正文任一缺失 → needs_revision；整頁抽取失敗 → CRAWL_FAILED。
- R10. 產出標準 `raw_job_bundle`（raw/ 目錄 + manifest.json + audit.jsonl），含 source html/text 的 sha256。
- R11. **不覆蓋既有 job**；同來源重抓需開新 job 或明確 resume。

**Stage 2 — 處理 / 標準化（Process / Normalize）**
- R12. 素材完整性檢查：圖片可否開啟、是否過小/嚴重模糊；影片可否播放、黑屏/無聲/損壞；外站網址/QR/廣告/明顯水印偵測。
  命中時預設 status=needs_revision（與 R14 一致）；blocked 僅保留給風控紅線（R20）。
- R13. 圖片標準化：正文圖 800px 寬等比、quality 90、檔名語意化；封面 1300×640、來源 1–4 張、safe area。
- R14. 影片規格檢查：以 ffprobe 驗 codec(H.264)、fps、bitrate、尺寸、檔案大小；不符規格標記為 needs_revision。
- R15. 產出 `processed_draft_bundle`：processed/ 目錄含 images、cover、videos、draft.md 與各項 report.json。
  （draft.json／程式化結構檔為 **Stage 5 預留**，MVP 以 draft.md + 必要 report 為主，避免維護兩套 schema。）

**內容組裝（Content Assembly via 公司 LLM）**
- R16. `content_assembler` 透過 **OpenAI-compatible adapter** 呼叫公司自有大模型（config 設 `api_key` + `base_url`），
  **收斂為受限改寫**：以抽取式摘要 ＋ 來源原句引用 ＋ 固定模板填充為主；敘事段（事件經過、FAQ）**須綁定來源句**，
  **不得自由杜撰來源未出現的指控或事實**。文章結構：標題、開頭簡介、一分鐘快速看懂、事件經過、圖片展示、影片介紹、FAQ、結尾。
- R17. 標題/標籤/關鍵詞/分類遵守規則（標題 25–35 字、tags 3–5 個且為客觀詞、禁用誇大詞、分類取自 config）。
  R16 的固定結構為**正式 schema（canonical）**，linter（R23）依此驗證。
- R18. **機器產出一律標記為「待人工校閱」**，且套用 R4/R5 的風控與未證實語氣規則；不確定分類時 status = needs_human_review。
- R19. API key / base_url **不得寫死在程式碼**，一律由 config/環境變數提供；
  所有日誌與 `audit.jsonl` 寫入前**強制遮罩機密欄位**（api_key、Authorization header、含 token 的 base_url），
  外部服務錯誤只記狀態碼與必要診斷，不記完整 request 標頭/body。
- R34. `content_assembler` **失敗路徑**明確化：key/base_url 缺漏 → exit 3（依賴錯）；
  LLM 回傳空（content 為 None/空）或**截斷**（finish_reason≠stop，如 length/content_filter）→ status=needs_revision，
  並在 audit 分別記錄 empty / truncated 原因；逾時/限流/5xx → exit 4（外部服務錯）並保留 job 可重跑。
- R35. 抓取內容**視為不受信任資料**：組裝前明確標記為「資料、非指令」以防 prompt injection；
  linter（R23）加入注入特徵檢查（如內文夾帶指令、隱藏連結、要求改變語氣/洩漏設定）。

**風控與查重閘門（Risk & Dedup Gates）**
- R20. Risk checker 實作 **hard stop**：命中紅線即 status=blocked、給 blocking_reasons，**不得進入後續流程**。
- R21. Dedup checker 比對「本地已處理 job index」「已發布 URL/title 索引」「source text 相似度」；
  至少兩組查重關鍵詞（人物/帳號＋事件詞、地點/平台/學校＋核心事件詞）。
- R22. Dedup 結果：duplicate→不得上架；uncertain→needs_human_review；unique→可繼續。
- R36. **Dedup 必須 fail-loud**：當缺少自家站內索引（或本地索引可能不完整，如換機/重裝/多人）時，
  須標記 `dedup_reliability=low` 並明確警告，**不得回傳過度自信的 unique**；此限制屬 Risk，非僅 Dependency。
  ⚠️ **MVP 已知限制**：無站內查詢 API 時，dedup 為 **advisory + 人工複查**——`unique` 自動放行僅代表
  「本工具未處理過」，**不代表站上未發布**；`dedup_reliability=high` 須接上站內 API 才成立。
  此限制必須在 GUI/輸出**明示**，不得讓操作者誤以為已完整查重。
- R23. Draft linter 檢查文章結構（依 R17 canonical schema）、標題長度、tags 數量、keywords 與正文一致、
  分類存在於 config、是否照搬來源段落、注入特徵（R35），輸出 pass / needs_revision / blocked。
  **同時檢查 grounding**：敘事段是否可對應到來源句，不可對應者標 needs_human_review。
  ⚠️ MVP 的 grounding 把關以**人工校閱為主**；自動句級對齊驗證列為 **planning spike**（見 Outstanding Questions），
  在 spike 落地前，「敘事段綁定來源句」是人工責任、非系統保證。

**Stage 4 — 審核包與簽核（Review Packet & Sign-off）**
- R24. 產出 review packet：cover.jpg、title.txt、review_message.txt、來源連結、review_manifest.json
  （含 title_hash、cover_hash、submitted_at、review_status=pending）。
  `review_message.txt` 為**人面向訊息**，由模板 + 草稿關鍵欄位組出（非 linter 原始輸出）。
- R25. 提供 `approve` / `reject` CLI/GUI 指令；reviewer **取自 config 白名單**，簽核決定與 reviewer 寫入 audit
  （作為人工上架前的責任紀錄，避免任意冒名）。
- R37. **上架責任閉環（輕量版）**：GUI 在 approve 後要求操作者回填 `published_url` 並**勾選確認**
  「上架內容＝已簽核版本」，連同 reviewer 寫入 audit；**job 未回填不算完結**（GUI 強制，避免靜默略過）。
  （真正的**版本 hash 自動比對**需要機器抓取上架後內容，與 Stage 6 同一能力，**移到 MVP 之後**——
  MVP 不在能力範圍內手算 live 頁 hash。此處只做「操作者具名確認」的責任紀錄，不宣稱密碼學級證據力。）
- R26. MVP **不自動建後台草稿、不自動發布**；上架由人工依 review packet 手動完成。

**基礎設施：Job / Audit / CLI / 安全（Infra / Security）**
- R27. 每個 job 維護狀態機（NEW→CRAWLED→PROCESSING→PROCESSED→…→APPROVED/REJECTED），
  並支援 BLOCKED、NEEDS_HUMAN_REVIEW、NEEDS_REVISION、CRAWL_FAILED 等旁支狀態。
- R28. 每個 job 有 **append-only `audit.jsonl`**，每個 stage 記 started/completed/failed/skipped/blocked/needs_human_review。
  MVP 為 per-job 檔（無中央日誌）；batch/cron 的彙總以掃描各 job 目錄為準。
- R29. 所有 stage **idempotent、可重跑**；輸出檔不覆蓋輸入。
  （**rollback 撤銷機制移到 MVP 之後**——MVP 不自動上架、無對外副作用，idempotent + 不覆蓋 + append-only audit 已足夠。）
- R30. CLI 支援 crawl / ingest / process / review-packet / approve / reject / run（`--until draft|review`），
  全域旗標含 `--config --dry-run --json --verbose --quiet --job-id --output-dir`，並支援 batch / cron 無人值守。
- R31. 失敗回傳明確 exit code（0 成功、1 用法錯、2 輸入驗證錯、3 依賴錯、4 外部服務錯、5 內部錯）。
- R32. 所有對外操作支援 `--dry-run`，且 dry-run 不修改任何外部系統或既有檔案。
- R38. **PII 落地檔治理**：raw_job_bundle / audit / review packet 含可指認隱私，須定義存放權限、保留期限與刪除流程；
  敏感資料與 append-only audit **分離存放**，以便履行合規刪除（被遺忘權）而不破壞稽核完整性。
- R39. **config / 憑證硬化**：config 檔權限收斂（如 600）、預設納入 `.gitignore`、支援環境分離（dev/prod）、金鑰可輪替。
- R40. **安全基線**：LLM 呼叫強制 TLS + 憑證校驗、base_url 限白名單；URL/清單檔/資料夾輸入做
  SSRF（封鎖內網/localhost/metadata IP）與路徑穿越防護。

### MVP 優先級（給 planning 排序用）

- **P0 必做閘門**（缺了 MVP 不成立）：R1–R5、R10、R16、R18、R20、R21、R22、R36、R24–R26、R28、R31、R33。
- **P1 核心**：R6–R9、R11–R15、R17、R19、R23、R27、R29–R30、R32、R34、R35、R37、R38、R39。
- **P2 可延後/可簡化**：R40 的進階項（深度 SSRF/DNS-rebinding/加密/媒體解析沙箱）、R13 封面 safe-area 細節、R14 影片規格嚴格度。

## Success Criteria

- 非技術經營者能透過**最小 GUI/一鍵封裝**、不寫一行程式，從 URL 或素材包跑出**可直接審核、來源可溯、合規無紅線**的草稿包。
- 能標準化圖片為 800px 寬、產出 1300×640 封面、用 ffprobe 驗影片規格。
- 能用公司 LLM 以**受限改寫**產出 draft.md，且機器產出皆標記待校閱、套未證實語氣、敘事段綁定來源句。
- 風控紅線能 hard stop；linter 能抓出結構/標題/標籤/照搬/注入問題。
- **查重誠實性**：有站內索引時能擋 duplicate；無索引時 dedup **明確標記不可靠並警告**，不謊報 unique。
- **閘門準確度可量測**：風控/查重/誹謗偵測須有**誤判率/漏判率**驗收標準（用一小組標註樣本），而非只測「會不會觸發」。
- 能產出 review packet、記錄 approve/reject 簽核（reviewer 來自白名單），**上架後可回填 hash 與簽核版本比對**。
- **未經簽核的內容沒有任何自動上架路徑**；CLI 可 batch 執行；dry-run 不動外部系統；每個 job 都有完整 audit。

## Scope Boundaries（MVP 不做）

- **不做** Stage 5 後台自動建草稿（backend / browser automation adapter）。
- **不做** Stage 6 自動發布、publish guard、版本 hash 強制比對。
- **不做** 完整桌面 App —— 只做**最小 GUI / 一鍵封裝**（薄殼包 CLI 核心）。
- **不做** `學生校園` 分類（預設停用，需人工放行）。
- **不做** 去水印掩蓋來源、繞過登入/付費牆/反爬。
- **不做** Playwright JS 重頁抓取（移到 MVP 後）、image perceptual hash 查重、stage rollback 撤銷機制。

## Key Decisions

- **定位＝自營站 / 合規優先**：抓取限公開可合法引用來源、強制署名、移除高風險分類。
  理由：carrying cost 最低、最可辯護，且與既有 non-goals 一致。
- **MVP 到 review packet 為止**：避開瀏覽器自動化發布這塊最貴、最脆弱、法律風險最高的部分；人工上架在合規優先前提下可接受。
- **介面＝CLI 核心 + 最小 GUI/一鍵封裝**：實際操作者為非技術經營者，成功標準「不寫程式即可用」需 GUI 才成立；
  CLI 仍是 batch/cron 與 GUI 共用的核心。
- **LLM 收斂為受限改寫（非自由生成）**：機器自由生成關於真實人物的全文是最大誹謗/幻覺風險，與「合規優先」相悖；
  改以抽取式摘要＋來源原句引用＋模板填充，敘事段綁定來源句，把人工校閱負擔降到可規模化。
- **核心張力（未解，需 planning 驗證）**：R16 grounding、R4/R5 風控、R36 查重在 MVP 最終都**部分依賴人工**，
  而「內容量規模化」正會給人工兜底加壓。能否規模化的關鍵，在於 planning 能否把 grounding/偵測的**自動準確度**
  做到足夠高（見 Resolve Before Planning）；若做不到，「合規優先 + 規模化」需重新權衡產量或定位。
- **rollback 延後**：Success Criteria 不要求撤銷、MVP 無對外副作用；以 idempotent + append-only + 不覆蓋取代，
  待 Stage 5/6 有真正撤銷需求再做。

## Dependencies / Assumptions

- 公司自有大模型提供 **OpenAI-compatible** 介面（`/chat/completions` 或相容），可用 base_url + key 呼叫（強制 TLS）。
- 本機已安裝 **ffmpeg / ffprobe**（影片檢查）與 Python 影像處理（Pillow）。
- Crawler **MVP 僅用 Scrapy**（極簡頁可 requests+BS4）；Playwright 移到 MVP 後。
- 查重的「站內查重」需要自家網站可被查詢的內容索引；MVP 先以**本地 job/已發布 URL 索引**為準，並依 R36 誠實標記可靠度。
- 最小 GUI 的技術選型（本地網頁 / 桌面框架）留待 planning 決定，但須與 CLI 共用同一核心。
- 文件中提到的「spec」指本次 brainstorm 的原始 dev-prompt；**R1–R40 為其具約束力的摘要**。
  建議 planning 前把完整 spec 存入 repo（如 `docs/spec/`），以免實作者無檔可依。

## Outstanding Questions

### Resolve Before Planning
- ✅ **已解決（2026-06-16）**：使用者確認**已有一批公開且可合法引用的來源**（含自有/授權內容）。
  planning 時須把這批來源寫入 `crawler.allow_domains`，並逐一保留其授權依據（呼應 R1/R2）。
  **無剩餘阻擋項，可進 planning。**

### Deferred to Planning
- [Affects R16/R34][Technical] 公司 LLM 介面的確切規格（端點路徑、認證 header、串流/逾時、速率限制）需在 planning 時取得。
- [Affects R4/R16/R23][Needs research] **合規偵測/grounding 可達準確度 spike（建議列為 planning 第一個任務）**：
  建立 golden set（誰標註、樣本量、可接受誤判/漏判閾值）並量測；grounding 自動驗證（敘事句對齊來源句）的可行作法。
  MVP 暫以人工校閱兜底；若可達準確度過低，「合規優先 + 規模化」定位需回頭權衡。
- [Affects R25/R33][Technical] GUI 的 reviewer 身分識別機制（人員選擇 / 簽核口令 / OS 帳號綁定），以真正達成「避免冒名」。
- [Affects R28/R37/R38][Technical] audit 防竄改（雜湊鏈/簽章）與 PII 分離存放後的引用/刪除對應模型。
- [Affects R21/R36][Technical] 自家網站是否有可程式查詢的搜尋/內容 API？有→可做即時站內查重；無→維持本地索引並依 R36 標記不可靠。
- [Affects R33][Technical] 最小 GUI 的具體形態（本地 web UI vs 桌面框架）與打包方式。
- [Affects R12][Needs research] 「圖片嚴重模糊 / 影片黑屏」的判定指標與門檻（如 Laplacian 變異數、亮度直方圖）。
- [Affects R4][Needs research] 誹謗性陳述與隱私資訊的本地偵測策略（規則詞表 vs LLM 判斷）、誤判處理與可達準確度。
- [Affects R38/R40][Needs research] PII 保留/刪除政策、媒體解析器漏洞、SSRF/路徑穿越防護的具體實作基線。
- [Affects Problem Frame][User decision] 現行手動流程的量化痛點（每篇耗時、每月篇數、過往合規事件），用於驗證投入是否成比例。

## Next Steps

→ **可進 planning**（blocker 已解）。執行 `/ce:plan`（按 Phase 1→4 順序 + 最小 GUI；Phase 5/6、版本 hash 比對、進階安全項標記為 MVP 後）。
  planning 的**第一個任務**：合規偵測 / grounding 可達準確度 spike（見 Deferred to Planning）。
