---
date: 2026-06-22
topic: gossip-pipeline-core
---

# Gossip Pipeline Core — Resource Acquisition & Processing

## Problem Frame

Eatmelon 是一個吃瓜內容帳號。目前的缺口：

- `gossip_scraper` 只爬 Weibo + 知乎熱榜**元數據**（標題、熱度），無文章正文、無封面圖
- `lcp` 有完整的 Scrapy 爬蟲 + 處理流水線，但設計為**止步於人工審核**、不自動發布，且沒有批次注入入口
- 兩者完全斷開，中間沒有連接橋

目標：讓 gossip_scraper 作為 Stage 1 資源採集層，擴展到更多平台並抓取深層內容（正文 + 圖片），再批次注入 lcp，由 lcp 完成處理（封面 + 文案 + 水印）後**全自動推送**到自建發布後台，中間無需人工點擊。

## 架構總覽

```
Stage 1: gossip_scraper（資源採集層）
┌─────────────────────────────────────────────┐
│ 熱榜抓取  Weibo / 知乎 / 抖音 / 百度 / B站    │
│       ↓                                     │
│ 跨平台去重 + 評分（cross_platform_count/score）│
│       ↓                                     │
│ Top-N 篩選（operator 設定每次送幾條進 lcp）    │
└──────────────────┬──────────────────────────┘
                   │ GossipItem list（含 URL）
                   ↓ 批次注入
Stage 2: lcp（處理層）
┌─────────────────────────────────────────────┐
│ Scrapy 深度爬取（正文 + 圖片）                │
│       ↓                                     │
│ 封面生成 → LLM 文案改寫 → 水印               │
└──────────────────┬──────────────────────────┘
                   │ 完成包
                   ↓ 全自動（R12–R13）
Stage 3: 自建發布後台
```

## Requirements

**Stage 1 — 熱榜來源擴展**

- R1. Bilibili 和 Toutiao（今日頭條）scraper 已在 codebase 中實作；本期新增**抖音熱榜**和**百度熱搜**兩個 scraper，並驗證現有 Bilibili / Toutiao 覆蓋率是否足夠（Toutiao ≠ Douyin，兩者是不同產品）。
- R2. **擴展現有** dedup 和 ranking 模組以涵蓋 R1 新增的平台，不需建新模組。`GossipItem` 的 `score`、`cross_platform_count`、`merged_from` 三個欄位已定義。
- R3. Top-N 篩選後，對每條 GossipItem 的 `url` 執行深度爬取，拿到文章正文（主要文本段落，≥300 字元為「成功」）
- R4. 深度爬取時同步抓取頁面圖片 URL 列表，作為封面候選素材；這些 URL 在進入 lcp 前必須通過現有 SSRF guard（scheme allowlist + DNS `is_global` check），不得新增繞過路徑
- R5. 深度爬取使用 Scrapy（與 lcp 現有 crawler adapter 架構一致，不引入第三套抓取框架）

**Stage 1 → Stage 2 — 注入橋接**

- R6. 新增批次注入入口（CLI 指令 or API）：接受 `GossipItem` 列表，在 lcp 中為每條創建一個 job，job 的來源 URL 設為 `GossipItem.url`
- R7. Top-N 篩選邏輯位於 gossip_scraper 側（在交出前裁剪），避免無效 job 進 lcp 佔空間

**Stage 2 — 處理流程**

- R9. **確認**現有 lcp 封面生成能力可正常應用於 gossip-injected jobs，並配置 Eatmelon 品牌模板（Logo + 色彩規範）。**純文字模板封面為必要交付物**（非可選 fallback），用於無可用圖片時的降級路徑。
- R10. **確認**現有 lcp copywriter（`--ai-copy`）可正常應用於 gossip-injected jobs；擴展 prompt 以符合 Eatmelon 語氣的固定格式。
- R11. **確認**現有 lcp watermark-ADD 可正常應用於 gossip-injected jobs，無需改動。

**Stage 3 — 發布**

- R12. 新增 publisher adapter：job 完成後，自動以 JSON POST 推送到自建後台 API（endpoint 由 config.yaml 配置）。**必須強制 HTTPS**（config load 時拒絕 http:// endpoint）；publisher auth token 存入 OS keyring，不放 config.yaml。
- R13. 全自動發布流：pipeline 完成後直接觸發發布，**不需人工點擊**。`dry_run` 模式下仍然跳過實際發布（現有保證不動）
- R14. 草稿模式：加 `--draft` flag。**語義**：完整跑 LLM / 封面 / 水印，只跳過 publish POST。**與 `--dry-run` 正交**：`--dry-run` 跳 LLM 不調用 publisher；`--draft` 跑 LLM 不調用 publisher；`--draft --dry-run` 兩者都跳（等同現有 `--dry-run` 行為）。
- R15. 平台 scraper 健康監控：每次 run 記錄各平台的抓取成功 / 失敗數；若某平台連續 7 天成功率低於 60%，記錄 `ERROR` 等級 log。

## Success Criteria

- 一次 pipeline run：從跑 gossip_scraper 到內容出現在發布後台，無任何手動步驟（R6, R12, R13）
- 新增平台後，熱榜覆蓋率提升（單次 run 能有 ≥50 條跨平台去重後的候選）（R1, R2, R7）（注：此為目標值，取決於平台間事件重合度；不達標不等於 pipeline 故障）
- 深度爬取正文成功率 ≥ 80%（正文 ≥300 字元為「成功」；失敗的 job 進 `BLOCKED` hold，不靜默跳過）（R3, R5）
- 封面圖片至少 1 張可用素材：失敗時降級為純文字模板封面，不卡流水線（R4, R9）

## Scope Boundaries

- 不做 CMS 直接整合（小紅書、WeChat 等）— 由自建後台負責最後一哩
- 不做去水印（de-watermark 已在 2026-06-17 的 PR 中明確 CUT，不恢復）
- lcp 作為獨立工具繼續可用，gossip pipeline 只是新增的使用路徑，不破壞現有行為
- 不做圖片 NCII 風險掃描（超出本期範圍；後台可自行加後置審核）

## Key Decisions

- **gossip_scraper 作為 Stage 1，lcp 作為 Stage 2+**：最大化復用 lcp 現有的 Scrapy crawler、處理閘道、狀態機；gossip_scraper 保持輕量，只負責熱榜聚合 + 去重 + 評分
- **全自動發布**：移除「機器不寫 CMS」的人工閘道設計，改為「機器推到自建後台，後台再決定」——把合規責任下移到後台，lcp 的防護重心從「不發布」改為「不寫未知 CMS」
- **Top-N 篩選在 gossip_scraper 側**：避免 lcp 積累大量低質量 job 佔用 SQLite 空間
- **Scrapy 統一深層爬取**：不在 gossip_scraper 裡引入 httpx/playwright，深層頁面統一交給 lcp 的 crawl_runner

## Dependencies / Assumptions

- 自建後台接受標準 JSON POST（需發布時提供具體 endpoint + auth scheme）
- lcp 的 `allow_domains` open-crawl 模式（空列表 = 允許任意公開域名）已可覆蓋新平台，**不需修改 config.example.yaml 預設值**（避免影響非 gossip 用途的 lcp 部署）；planning 需逐一確認各目標域名（weibo.com、bilibili.com 等）在部署環境中 DNS 解析回傳 `is_global=True` IP

## Outstanding Questions

### Resolve Before Planning

- [R12][User decision] 自建後台 API 的具體 endpoint、認證方式（Token? Basic Auth?）以及期望的 JSON 格式 — 這決定 publisher adapter 的實作介面。**同時須確認**：後台是否有自己的人工審核佇列？若有，「無任何手動步驟」的成功標準需修正定義。
- [R1, R13][Architecture decision] **lcp 全自動發布的狀態機路徑**：現有狀態機中 `APPROVED → PUBLISHED_RECORDED` 需要人工 attestation（`backfill --attest`），無機器可達路徑。自動發布需要：(a) 新增 state（如 `AUTO_PUBLISHED`）+ 新 terminal edge；或 (b) 重寫 `backfill` 中的 attestation 邏輯。這是狀態機手術，不是 config 變更，須在 planning 前決定。
- [R1–R5][Feasibility spike needed] **平台深度爬取可行性驗證**：在進入 planning 前，需以真實請求驗證抖音（重度 JS SPA）、百度熱搜的熱榜 + 文章頁是否可通過 Scrapy（無 JS 引擎）取得正文和圖片。若驗證失敗，R1–R5 的技術路線需重新評估（可能需 headless browser 或限制平台範圍）。
- [R3][URL structure assumption] **熱榜 item URL 是 topic 頁還是文章頁**：Weibo / Baidu 熱搜 item 的 URL 通常指向 trending topic 頁（搜索結果列表），而非單一文章正文。R3「對 GossipItem.url 深度爬取拿正文」若需要額外一跳（topic 頁 → 代表性文章），整個 Stage 1 → Stage 2 的 URL 流轉設計需調整。須在 planning 前確認現有 gossip_scraper 回傳的是哪種 URL。
- [R3, R6][Architecture decision] **批次注入後由 lcp 自行爬取 vs. gossip_scraper 預爬取後傳 payload**：前者更乾淨（符合 lcp subprocess 隔離模型），但需確認 R4 圖片 URL 如何在 crawl 結果中儲存並傳遞給封面生成步驟。此為 R3/R4/R5/R6 的架構前提，影響 injection interface 設計。
- [R10, R13][Legal decision] **LLM 改寫第三方內容的版權立場**：R10 以爬取的微博/知乎/B站/百度正文為 LLM 輸入改寫後自動發布。LLM 改寫不消除原始著作權。須在 planning 前確認：(a) 改寫內容是否標注來源；(b) 是否有 operator 確認此為可接受風險的明確決策記錄。
- [R13][BLOCKED accumulation] **BLOCKED job 清理策略**：成功率 80% + 每次 ≥50 jobs → 每次 run 新增 ≥10 個永久 BLOCKED job（lcp 設計無自動 exit）。須在 planning 前決定清理方案（TTL auto-supersede？定期手動清理？降低 Top-N 以控制 BLOCKED 積累速率？）。

### Deferred to Planning

- [R6][Technical] 批次注入 CLI 指令放在哪個入口點 — `lcp ingest-gossip` 或 `gossip_scraper --pipe-to-lcp`；若在 lcp 側，CLAUDE.md 要求同步實作 gui.py 的對應 API（cli/gui mirror invariant）
- [R9][Technical] 封面圖片挑選算法：v1 採「第一張可用圖片」啟發式（最低成本），後期可優化
- [R8 → resolved] allow_domains 無需額外設定，planning 確認各域名 is_global 解析即可
- [R12][Technical] publisher adapter 的重試策略和冪等性：POST 失敗時（5xx/timeout）如何重試？重試需冪等（避免重複發布）；是否需要對應 lcp 的 `PROCESS_FAILED` 模式新增 `PUBLISH_FAILED` hold state？

## Next Steps

→ 解決「Resolve Before Planning」後，可進入 `/ce:plan`
