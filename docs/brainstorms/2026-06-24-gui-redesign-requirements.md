---
date: 2026-06-24
topic: gui-redesign-new-design-language
---

# LCP GUI 全新設計語言重設計

## Problem Frame

現有 GUI 的功能架構已完整（狀態機驅動可供性、異步輪詢、lex 人話翻譯、fail-closed 著色、CSRF 防護）——這些**全部保留**。問題在於**視覺語言**：整體用的是 `.row` 平鋪表單 + 最小化輸入框樣式 + 暖灰卡片，看起來像「工程師自己用的工具原型」，而不是一個非技術操作者每天打開的編輯室工具。

操作者需要的感受：**我打開這個 app，知道我在哪、下一步是什麼、這是一個認真的工具**。

硬性約束（不可動）：
- CSP: `style-src 'self'`，零內聯 style，零外部字型/icon font
- 所有文字 `textContent` only，零 innerHTML
- 零 build step、零框架、零 CDN
- 狀態語義色族（AMBER/RED/INDIGO/GREEN/BLUE/GREY/SLATE）的語義不可變（合規可視）
- 所有現有 API bridge 調用、lex 翻譯、狀態門控邏輯不動

---

## Requirements

**新色彩系統**
- R1. 主色改為深靛藍（`#1a1f36` 系）作為 sidebar/header 背景，取代暖灰 canvas。content area 保持白底。
- R2. 狀態語義色族保留語義但提升飽和度。格式為 `bg / border`（不是 text）：AMBER → `#f59e0b / #92400e`，RED → `#ef4444 / #7f1d1d`，GREEN → `#22c55e / #14532d`，INDIGO → `#6366f1 / #312e81`，BLUE → `#3b82f6 / #1e3a8a`。Badge text 繼續使用深色 ink on 淺色底（沿用現有機制達 ≥7:1，如 `--c-attention-tx: #6b4900` on `--c-attention-bg: #fdf3da`）；飽和色只用於 bg + border token，不用於直接接觸白底的文字。
- R3. 中性色換為更冷的藍灰系：canvas `#f1f5f9`，border `#cbd5e1`，ink-500 `#64748b`，ink-900 `#0f172a`。
- R4. 保留所有現有 CSS custom properties 名稱（`--c-attention-*` 等），只更新值——JavaScript 零改動。

**新排版系統**
- R5. 引入 display 字型尺寸：`--fs-display: 2rem`（Job 標題）和 `--fs-heading: 1.25rem`（section 標題），比現有 `--fs-700: 1.4rem` 更分明。
- R6. 正文行高從 1.55 改為 1.65（CJK 更易讀）。
- R7. 標籤文字（form label、badge）字重統一為 `500`（medium），不用 400。

**新導航：左側欄（sidebar）**
- R8. 桌面版（≥768px）：左側固定 sidebar（寬 220px），包含：品牌 logo 區 + 導航連結（收件匣、新工作、總覽、設定）+ 收件匣搜索框（見 R26）+ 底部就緒 pill。
- R9. 主內容區從 `body max-width: 62rem` 改為 `sidebar 220px + main flex:1 max-width: 52rem`，兩欄 flex 佈局。
- R10. 行動版（<768px）：sidebar 收起，top bar 顯示漢堡按鈕 + 當前頁面名稱，點漢堡展開側欄 overlay（`position: fixed; z-index: 100`）。
- R11. Sidebar 活躍連結：深色底 + 白色文字 + 左側 3px 亮色指示線（non-active: 透明 hover 態）。
- R12. 「機器永不自動上架」聲明移到 sidebar 底部（固定），不再佔 header 條帶。

**新輸入框 / 表單元素樣式**
- R13. 所有 `input[type="text"]`, `input[type="password"]`, `select` 高度統一 40px，`border-radius: 8px`，`border: 1.5px solid var(--border)`，focus 時 `border-color: var(--c-progress-bd)` + `box-shadow: 0 0 0 3px rgba(99,102,241,.15)`。
- R14. 取消現有 `.row` 的 `display: flex; align-items: center` 平鋪模式，改用垂直 stack 的 `.field-group`：`label` 在上（13px, ink-500）、`input` 在下（16px, ink-900），間距 6px。
- R15. 表單邏輯分組用 `.form-card`（白底、border、8px radius、`padding: 24px`、`margin-bottom: 16px`）包裹，每個 card 有 `<h3>` 分組標題。

**新建立工作 / 重新抓取表單（三步向導）**
- R16. 建立工作改為三步向導（Step 1/2/3），頂部顯示步驟指示器（數字 + 連線）：
  - Step 1「來源」：URL 或資料夾二選一（大型 radio card，非 inline radio），輸入框，已存來源下拉。「下一步」按鈕在 URL/路徑為空時 disabled；URL 格式錯誤在點「下一步」時 inline error，不在 blur。
  - Step 2「選項」：AI 文案開關、dry-run 開關、工作 ID（自動填充 + 可覆寫）。
  - Step 3「確認」：摘要顯示 label/value 對——來源類型（URL / 資料夾）、來源值（URL 全文；資料夾路徑顯示最後兩段，避免家目錄曝露）、AI 文案、dry-run、工作 ID（新建時可點「編輯」回到 Step 2）——+ 「建立並抓取」主按鈕。送出後向導整體進入 disabled，轉入 `enterProgress()` 後向導隱藏；送出失敗（API error）顯示 toast，向導維持在 Step 3（不退回 Step 1）。
- R17. Radio 選項改為大型選擇卡（card radio）：整個 `<label>` 成為可點區域，selected 時有 indigo 邊框 + 淡底色。
- R18. 步驟之間可「上一步」，向導狀態在 JS 模組變數維護（不改 HTML 結構，只改 `.hidden`）。
- R19. 重新抓取（re-crawl）進入向導時 Step 1 預填來源並鎖定工作 ID（Step 1 直接跳過，開啟於 Step 2）；Step 2 顯示「重新抓取 job-xyz」提示 banner。

**新工作工作台（Job Workspace）**
- R20. 工作台改為兩欄佈局（桌面）：左欄 60%（狀態 banner + workflow 步驟 + review packet），右欄 40%（actions 面板，sticky 跟隨捲動）。行動版降為單欄，actions 浮動在底部（`position: sticky; bottom: 0`）。
- R21. Workflow 步驟從垂直 `<ol>` 改為水平 stepper（桌面）或垂直 timeline（行動版）：每步一個圓形節點（✓完成/○待辦/✗封鎖），節點間橫線。
- R22. 狀態 banner 設計：更大圓角卡片（12px），左側 6px 色帶（tone 顏色），標題 18px bold，說明文字 14px，CTA hint 單獨一行帶箭頭圖示。
- R23. Actions 面板：獨立 card，標題「你現在可以做」（active 狀態），主要動作按鈕全寬（100%），危險動作保持 btn-danger 樣式。Terminal hold states（BLOCKED/DUPLICATE）：標題改為「此工作已停止」，只顯示 supersede 危險按鈕（含警告文字），redline 覆寫需二次確認（遵循現有 CLAUDE.md 規則）。完全 terminal 狀態（PUBLISHED_RECORDED/SUPERSEDED）：標題「已完成」，無操作按鈕，以 indigo 淡色調顯示歸檔說明。

**新收件匣**
- R24. 收件匣 band 頭部改為更視覺化的設計：左側色點（8px 圓）+ 粗體文字 + 右側數量 badge（pill 形，tone 顏色）。
- R25. 工作列改為更高的卡片行（56px 高），有更清晰的三欄資訊：左=工作 ID（monospace 小字）+ 狀態 badge，中=人話 reason 文字（粗體），右=時間 + 打開按鈕。Hover 時整行輕微上浮（`transform: translateY(-1px)` + 加深陰影）。
- R26. 收件匣搜索框移到 sidebar 頂部（在 "收件匣" 標題下方），始終可見。
- R27. 空收件匣顯示更大的 empty state：插圖區域（CSS 繪製的簡單幾何圖形）+ 標題 + 引導文字。

**新 Dashboard**
- R28. KPI 卡片改為更大底色塊（全底色，非左邊框）：數字 2.5rem，標籤 12px uppercase tracking-wider。
- R29. Dashboard section 標題有分隔線延伸（`h3::after { flex:1; border-bottom: 1px; }`）。

**新 Setup**
- R30. 設定表單放在更窄的居中卡片（max-width: 480px），有明確的「就緒清單」卡片（綠色邊框）和「設定」卡片分開。

**微互動 / 動效**
- R31. 所有真正的視圖切換改為 `opacity + translateX` 進入動畫（而非純 opacity）：新視圖從右側 20px 淡入（`@keyframes view-enter`）；in-place refresh（如 refreshInbox 在收件匣視圖內刷新）不觸發此動畫。`showView()` 需加 `requestAnimationFrame` 一幀延遲以跨越 `[hidden]{ display:none !important }` 限制（約 5 行 JS，含入預算 < 100 行）。
- R32. 工作列 hover：`translateY(-1px)` + `box-shadow` 加深，transition 150ms。
- R33. Toast 通知位置移到左下角（sidebar 上方），改為更圓潤的卡片樣式（12px radius，更大 padding）。
- R34. 按鈕 active 態：`scale(0.97)` 保留，新增 ripple 效果（pure CSS `::after` pseudo，`@keyframes ripple`，無 JS）。

---

## Success Criteria

- 視覺上：非技術操作者第一眼能識別「這是一個認真設計的工具」，而非開發者原型。
- 功能完整：所有現有 API 調用、lex 翻譯、狀態門控、安全邊界（CSP、textContent、CSRF）完全保留。
- 結構上：`app.js` 改動 < 100 行（限 sidebar toggle + wizard 步驟控制，所有 API 調用、lex 翻譯、狀態門控邏輯不動）；HTML 結構改動限於新增 sidebar 包裝層 + 新 `.form-card` / `.field-group` 結構 + 步驟向導 HTML；`lex.js` 零改動。
- 可維護：`app.css` 完整重寫（~400 行），所有舊 class 名稱若有新行為則保留相容層，不讓 JS 的 className 設定報錯。
- 響應式：≥768px sidebar，<768px top bar + overlay，所有功能在行動版可達。

---

## Scope Boundaries

- **不動**：lex.js、gui.py、webserver.py、pipeline.py、所有後端 API。
- **不做**：icon font / SVG sprite（CSP 限制），自訂字型，build step，JavaScript 框架。
- **不動**：狀態語義色族的語義（合規要求），只更新 token 值。
- **不做**：深色模式（`prefers-color-scheme: dark`）—— 本次不在範圍。
- **不做**：鍵盤快捷鍵的改動（command palette 已有）。

---

## Key Decisions

- **保留 lex.js 零改動**：lex 是純資料字典，與視覺系統完全解耦；新 CSS 透過同樣的 tone class 渲染。
- **app.js 最小改動原則**：所有新 HTML 結構（sidebar、form-card、field-group、wizard）加在 HTML 裡，JS 只需更新少量 `querySelector` 參照（sidebar toggle、wizard step 變數）。估計 JS 改動 < 100 行（sidebar toggle ~15 行 + wizard next/prev handlers + step indicator 更新 ~50 行 + bindCreate 接線 ~20 行；60 行估算偏緊，已修正）。
- **左側 sidebar**：比頂部 tab 給 content area 更多縱向空間，也讓品牌感更強；行動版收起不損失功能。
- **三步向導（Create form）**：把目前「一堆 row 擠在一起」拆成三個有順序的決策點，減少認知負擔。

---

## Dependencies / Assumptions

- 無外部依賴；所有資源必須來自 `src/lcp/web/`（`script-src 'self'`，`style-src 'self'`）。
- 假設 `webserver.py` 的 static 目錄設定不需改動（已 serve `web/` 目錄）。
- `index.html` 需要加入 sidebar HTML 結構，`app.css` 全量重寫，`app.js` 小幅修改（< 100 行）。
- app.js 動態設定的 CSS class names（CSS 重寫時不可改名）：`job-row`, `action-row`, `ready-row`, `skeleton-row`, `band`, `band-head`, `band-body`, `band--attention/stopped/inflight/closed`, `badge`, `badge--*`, `banner`, `banner--*`, `inflight`, `confirm-tray`, `confirm-backdrop`, `hold-panel`, `tech-detail`。

---

## Outstanding Questions

### Resolve Before Planning

（無阻塞問題——WCAG 色彩決策已寫入 R2；向導 Step 3 規格已寫入 R16；terminal 面板已寫入 R23；animation 決策已寫入 R31）

### Deferred to Planning

- [影響 R8/R10][Technical] sidebar toggle 的 JS：最小改動方式——`toggleClass` 事件綁定到漢堡按鈕 + `showView` 路徑關閉 overlay。
- [影響 R16-R18][Technical] 三步向導 HTML/JS 結構：3 個 step div + hidden 切換，JS wizard 步驟狀態變數，`bindCreate()` 接線最小改動量。re-crawl 路徑從 Step 2 開始（Step 1 跳過）。
- [影響 R20][Technical] 兩欄佈局 sticky actions：`job-cols` wrapper 不設 overflow；右欄 `align-self: stretch`；可行性已確認（`main` 無 overflow，sticky 正常生效）。
- [影響 R21][Technical] 水平 stepper 節點數 > 6 在 ~30rem 欄寬的 overflow 策略（建議：只顯示「已完成 N 步，當前：X」compact 模式，不試圖顯示全路徑）。
- [影響 R26][Technical] sidebar 搜索框在非收件匣視圖下的行為：選擇 (a) disabled/gray out，或 (b) 輸入後自動切換至收件匣視圖（後者需 ~5 行額外 JS）；mobile sidebar 收起時搜索替代入口（建議：top bar 增加搜索 icon，行動版觸發收件匣 overlay）。
- [影響 R29][Technical] `h3::after` flex separator scope 至 `.dash-section h3`（全域 h3 設 `display:flex` 會破壞 job workspace section heading）。
- [影響 R31][Technical] `prefers-reduced-motion` fallback：R31 translateX 過渡退為 opacity-only，R21 stepper 使用靜態節點；`@media (prefers-reduced-motion: reduce)` block，延續 app.js line 15 spinner fallback。
- [影響 R10/R12][Design] mobile top bar 就緒 pill 替代：top bar 右側保留小圓點（綠/紅），點擊跳至 Setup。
- [影響 R10][Technical] sidebar overlay 關閉機制：backdrop click、Escape 鍵、簡易 focus trap。

---

## Next Steps

→ `/ce:plan` 進行結構化實作規劃
