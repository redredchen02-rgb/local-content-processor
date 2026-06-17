---
date: 2026-06-16
topic: frontend-ui-ux-deep-plan
---
# Local Content Processor — 操作者 GUI 深度 UI/UX 设计方案

> 单一事实来源 (single source of truth)。供 planner / implementer 直接落地。
> 适用文件：`src/lcp/web/index.html`（148 行）、`src/lcp/web/app.js`（307 行）、`src/lcp/web/app.css`（14 行）、后端只读 `src/lcp/gui.py`。
> **零新增后端 API**（除一条强烈建议的只读单行扩展，见「Onboarding & Settings」§6）；**零构建步骤、零框架、零 CDN、纯 vanilla JS + 外部 CSS**。

---

## Problem Frame（问题框定）

**结论先行：** 这个工具的后端（15 状态的状态机、fail-closed 合规闸、冻结保证、不自动发布）是正确且诚实的；但当前 GUI 把这套机器的**原始词汇**直接糊在操作者脸上，且**整页 7 个 section 同时摊开、所有动作按钮永远可见**。对一个**非技术的经营者（文科背景）**而言，这等于把一台精密但无标签的仪表盘交给完全不懂状态机的人——这是 #1 弃用风险。

**操作者是谁、为什么现在重要：**
- 操作者是单机、单人的**经营者**，不写代码、不读状态机、读中文。日常循环是「建 job → 跑处理 → 看 review packet → approve/reject → 手动上架后回填」。
- 一次性的技术设置（API key / base_url / 爬虫 allowlist / reviewer 白名单 / 权限加固，R33 左侧）由「装机的人」完成，**与日常操作明确分离**。
- README 显示桌面窗口**很可能从未被真人端到端跑过**（G7）——只有 headless 的 Api 有单测。所以本方案的首要可交付物是「**让它真的能跑起来且不吓跑人**」。

**当前被验证的七个缺口（G1–G7），本方案逐一关闭：**
- **G1 冻结窗口**：`app.js` 调用**同步**的 `create_and_crawl`/`process` 后只 `await`，长爬取/LLM 调用期间窗口卡死、无进度。异步后端（`create_and_crawl_async` / `process_async` + `job_status` 轮询）**已存在但 app.js 从未使用**。
- **G2 无状态驱动的可供性**：8 个 transition 按钮（`btn-crawl/btn-ingest/btn-process/btn-packet/btn-approve/btn-reject/btn-supersede/btn-backfill`）永远存在且启用；操作者必须**先懂状态机**才知道下一步按哪个。
- **G3 机器话（machine-speak）**：worklist 显示原始 `review_reason` 码（`risk|dedup|grounding`）；错误显示 `error (3): ...`。无人类解释、无下一步建议——这是 #1 弃用风险。
- **G4 缺失交互态**：无 empty / loading / success 态，终态与不可逆动作（reject / supersede / backfill-attest）无确认。
- **G5 无视觉设计**：14 行 CSS，系统字体、单一灰边框，无状态色彩语义、无字阶、无间距系统、无信任/合规视觉线索。
- **G6 无 onboarding**：settings 是裸表单，不提示运行前必备的前置条件；尤其**空的 reviewer 白名单会静默地让全部签核失效**，且 GUI 无写入路径。
- **G7 从未跑过**：整套需要在真实 pywebview 窗口端到端验证。

**核心矛盾，一句话：** 后端把「fail-closed = 替你拦下、停在那等你看」做对了；GUI 必须把这种「停下」读成「**已替你停好，等你判断（parked for your review）**」，而**绝不能**读成「失败 / error」。整套设计就是这句话的视觉与文案落地。

---

## Design Principles（设计原则，7 条）

1. **状态驱动的可供性（state-driven affordances，修 G2）。** 每个 job 的合法下一步**完全由它的 `state`（及 hold 时的 `review_reason`）从 `state.py` 的 `_TRANSITIONS` 表计算**。只渲染合法动作——非法动作**直接不渲染**（不是置灰），所以操作者永远不会问「为什么这个按钮是灰的」。绝不发明不存在的边（已删除两个伪造边：`crawled`→re-crawl、`crawl_failed`→`ingest_dir`）。
2. **机器说人话（machine-speaks-human，修 G3）。** 后端发出的一切闭合枚举（15 个 `JobState` + 3 个 `ReviewReason` + 5 个 `exit_code` 桶）经**一张静态查找表**翻译成「一句中文标题 + 平白的为什么 + 恰好一个下一步」。原始码作为小号灰色的「技术细节」保留，供技术助手与 CLI 对照，但操作者读的是句子。
3. **信任优先、合规可见（trust-first / compliance-visible，修 G5 的精神层）。** 「机器永不自动发布」「签核=署名，非身分验证」「查重仅代表本工具处理过、非全站」这三句诚实声明常驻可见，绝不被滚走、绝不被绿色「全部就绪」盖过。冻结的 review packet 是不可变的信任凭证，有专属视觉皮肤。
4. **安全是既定前提（security-as-given：textContent / CSP，硬红线 R41/R40）。** 所有用户面文本一律 `textContent` / `createElement` / `setAttribute`；**绝不** innerHTML / 模板字符串 HTML / eval / 内联 script / 内联 style。严格 CSP 不变。来源链接渲染为**惰性纯文本**（不可点、永不抓取）。loopback-only，`connect-src 'self'`。这不是一个可讨论的设计维度，而是每个组件的前置约束。
5. **CLI/GUI 对等（agent-native parity，R30）。** 每个 CLI transition 动作在 GUI 都可达；可**重组、隐藏至相关时再现**，但**绝不删除**。GUI 隐藏一个动作直到状态使其合法，正是 CLI 隐式所做的（对 `crawled` job 调 `approve` 会 raise `InputValidationError`）——同一保证，更友善的表面。
6. **无构建步骤的极简主义（no-build-step minimalism，YAGNI）。** 单一静态页、vanilla JS、外部 CSS、loopback 自带 HTTP server。无路由器、无框架、无主题引擎、无设计系统库。视图切换用 `element.hidden` + 顶栏 `<button>` 的 `addEventListener`，不是路由。增量约 ~230 行 CSS + ~180–230 行 JS。
7. **fail-closed 读作「已停好」而非「失败」（fail-closed reads as parked, not failed）。** 颜色与文案严格区分三类：**琥珀=需要你（needs YOU，可救）**、**红=死路（终态，到此为止）**、**靛蓝=已冻结（不可改，等人）**。`needs_human_review` 是琥珀不是红；`blocked/duplicate` 是红且**零按钮**（无「approve anyway」）。这条原则把后端的合规姿态翻译成情绪正确的视觉。

---

## Information Architecture（信息架构 + ASCII 线框）

### 核心决定：视图切换、worklist 为中心（view-switched, worklist-centric）

当前页把 7 个 section 堆在一条滚动上，并用一个共享的 `#job-id` 文本框串起所有动作。重设计改为**三个顶层视图、同时只显示一个、用 show/hide 切换**——无路由、无框架、无构建步骤。

| 视图 | 角色（类比） | 默认？ | 吸收旧 section |
|---|---|---|---|
| **INBOX**（`#view-inbox`） | 收件匣——日常落地页：什么需要我处理，按优先级排 | **是** | Summary(2) + Worklist(3)，合并重构 |
| **JOB**（`#view-job`） | 案件工作台——一个选中的 job 摊在桌上，只显示其状态允许的动作 | 否（从 Inbox 打开或 create 后进入） | Create&run(4) + Job detail(5) + Sign-off(6) + Backfill(7)，统一并状态门控 |
| **SETUP**（`#view-setup`） | 一次性技术设定房间——前置条件清单 + 3 个可编辑 LLM 字段 | 否（首次未就绪时自动打开） | LLM settings(1)，重构为 onboarding（G6） |

> **为什么不是改进单滚动？** 对这个操作者，失败模式是「我不知道这堆表单里哪个适用于我关心的 job」。单滚动无法回答「job X 下一步该做什么」而不读完所有 section。聚焦的 JOB 视图能从单一字段（`state`）算出答案并只显示那些按钮。
> **为什么 worklist 为中心、而非 job 为中心？** 不存在值得落地的「当前 job」概念——操作者到来是为了**分检一个队列**（被 hold 的、待签核的、已批准未回填的、以及**机器为合规拦下的**）。Inbox 即首页；job 只有被打开进 JOB 视图才成为「当前」。

### 持久外壳（始终在切换视图之上）

```
+==============================================================================+
|  Local Content Processor            [ Inbox ]  [ + New job ]  [ Setup ⚙ ]    |   <- 顶栏：3 个 nav 按钮
|  机器永不自动上架。签核=署名，非身分验证。               ● Ready  (或 ⚠ Setup needed) |   <- 诚实声明（常驻）+ 就绪 pill
+==============================================================================+
|                                                                              |
|   ( #view-inbox / #view-job / #view-setup 恰有一个显示在这里 )                |
|                                                                              |
+==============================================================================+
```

- 顶栏 3 个按钮：`[Inbox]`/`[Setup ⚙]` 切视图，`[+ New job]` 以 create 模式打开 JOB 视图。活动视图按钮加 `aria-current="page"`（`setAttribute`，CSS 着色，无内联 style）。
- 诚实声明（index.html 现第 19 行）逐字保留并 pin 在每个视图上（R26/R18/R25 信任姿态永不被滚走）。
- **就绪 pill** `● Ready` / `⚠ Setup needed` 在 init 时由 `get_settings()` + `reviewers()` 计算；点击跳到 SETUP。这是 G6 的全局化修复。
- 导航是纯 JS 状态变量 `currentView` + `showView(name)` 切三个视图根的 `.hidden`。无 URL、无 history API（桌面窗口无后退键需要尊重）。每次进入 INBOX 重跑 `refreshInbox()`，计数实时。

### INBOX 视图（默认落地页）

Inbox 把旧 Summary 表与 Worklist 合成一屏分检页，以「需要你处理」band 领头，并**将机器拦下（BLOCKED/DUPLICATE）的 job 展开显示**，绝不藏进折叠抽屉。

```
+-- #view-inbox --------------------------------------------------------------+
|  INBOX                                                       [ Refresh ]    |  <- 手动刷新；进入视图时也自动刷
|  +----------------------------------------------------------------------+  |
|  |  需要你处理 NEEDS YOUR ATTENTION  (3)                                 |  |  <- band 1：派生分组（非状态）
|  |  job-0042   已 hold：无法核对来源 (grounding) · 2h ago     [ Open > ] |  |
|  |  job-0039   等你签核 (review pending) · 5h ago             [ Open > ] |  |
|  |  job-0031   已签核——回填你的发布网址 (approved) · 1d ago   [ Open > ] |  |
|  +----------------------------------------------------------------------+  |
|  +----------------------------------------------------------------------+  |
|  |  被机器拦下 STOPPED BY THE MACHINE  (1)   (合规 fail-closed)          |  |  <- band 2：始终展开
|  |  job-0050   已拦下：触及风险红线，无法继续 (blocked) · 30m   [ Open > ]|  |
|  +----------------------------------------------------------------------+  |
|  +----------------------------------------------------------------------+  |
|  |  进行中 IN FLIGHT  (2)                                                |  |  <- band 3：new/crawled/processed
|  |  job-0048   可开始处理                                     [ Open > ] |  |
|  |  job-0051   刚建立——抓取中…                                [ Open > ] |  |
|  +----------------------------------------------------------------------+  |
|  +----------------------------------------------------------------------+  |
|  |  已结案 CLOSED  (11)                                       [ show v ] |  |  <- band 4：例行终态，折叠
|  +----------------------------------------------------------------------+  |
|  Counts: review_pending 1 · needs_human_review 1 · approved 1 ·            |  <- summary() 计数条，
|          blocked 1 · processed 1 · published_recorded 9 · rejected 2       |     'total' 已剥除
|  [ Filter: (all) v ]   <- 显眼控件；选定后把 band 折叠成单状态平铺表（对齐旧 worklist）|
+----------------------------------------------------------------------------+
```

**Inbox 分 band 逻辑（客户端，从 `list_jobs()` + `summary()` 计算，非新后端调用）。** 终态被**刻意拆成两组**：闸杀（BLOCKED/DUPLICATE）从「已结案」中拉出、单独展开成合规 band——对合规优先的工具，刚被拦下的 job 比例行的 review_pending 更值得注意，且非技术操作者绝不能让一个 job 静默消失进抽屉。

| Band | 包含状态 | 默认 | 为什么分组 |
|---|---|---|---|
| **需要你处理** | `needs_human_review`, `needs_revision`, `review_pending`, `approved`, `process_failed`, `crawl_failed` | 展开 | 每个「人是瓶颈」或「欠一次重试」的状态。inbox-zero 目标。 |
| **被机器拦下** | `blocked`, `duplicate` | **展开** | 合规 fail-closed（R20/R22）。终态——操作者必须看见并理解为什么；其 JOB 视图只读、无前进路径（无「approve anyway」）。 |
| **进行中** | `new`, `crawled`, `crawled_warn`, `processed` | 展开 | 机器就绪：操作者一个动作即推进的阶段。`new` 因 `crawl_failed -> new` 重试边可达而纳入，否则 NEW 态 job 会落空。 |
| **已结案** | `rejected`, `superseded`, `published_recorded` | 折叠 | 操作者已决定的例行终态，可收起。 |

`processing` 永不出现（transient、永不持久化）。14 个持久状态各映射到恰好一个 band；空 band 渲染 empty 态，不留空白。

### JOB 工作台（聚焦视图 + 状态门控）

吸收旧 4/5/6/7，**只显示当前状态合法的动作**。

> **打开一个 job 时，状态来自 worklist/job_status，而非 packet。** `get_packet()` 对所有 pre-draft 状态（`new/crawled/crawled_warn/crawl_failed/process_failed/needs_revision`）返回 `{error:"no draft for <id>"}`——正是需要 Process/Retry 按钮的那些状态。所以 `openJob()` 分离两件事：

```
openJob(jobId):
  currentJobId = jobId; showView('job')
  st = job_status(jobId)        // running -> 进度模式；done/idle -> 取 result.state/persisted；unknown -> "job 不存在"
  // 必要时回退到 list_jobs() 的 state；state 永远可解析
  renderStateBanner(state, review_reason)   // 人类化（见 State & Message Legibility）
  renderActions(state, review_reason)       // 状态门控（下表）
  pkt = get_packet(jobId)
    if pkt 有 "error"  -> 完全隐藏 packet 面板（pre-draft 正常，绝不渲染 error 卡）
    else               -> 逐字段渲染 packet（R18 framing）
```

```
+-- #view-job （含 packet，例如 review_pending） -----------------------------+
|  [ < Inbox ]                                                               |
|  JOB  job-0039                  [● 已凍結·待簽核 review_pending]  updated 5h |  <- 只读标题（取代 #job-id 输入框）+ 状态徽章
|  ┌── 这是什么意思 ──────────────────────────────────────────────────┐      |  <- 人类化 headline，始终在
|  │ 草稿已冻结，内文不可再改。看完后核可或退件。要改只能作废另开新 job。 │      |
|  └────────────────────────────────────────────────────────────────────┘    |
|  +-- 你现在可以做 WHAT YOU CAN DO NOW ------------------------------+        |  <- 状态门控动作区（G2）
|  |   Reviewer: [ 王小明 ▼ ]  (来自 reviewers()；空则替换为 §onboarding banner)|        |
|  |   ┏ ✓ 核可 Approve ┓   [ 退件 Reject… ]   [ 作废 Supersede… ]      |        |     只渲染合法按钮
|  |   (verbatim disclaimer 只读，呈现在决定当下)                        |        |
|  +------------------------------------------------------------------+        |
|  +-- REVIEW PACKET (机器产生 · 待人工校阅) --------------------------+        |  <- get_packet() 逐字段
|  |  标题/分类/一分钟快速看懂/事件经过/FAQ/结尾/Tags/Keywords          |        |
|  |  来源连结 (惰性纯文字，不可点、不会被开启): example.com/...        |        |  <- R41/R2 inert text
|  |  Provenance: model=… finish_reason=stop  ·  Frozen body hash: 3f9a… |        |     (hash 作信任徽章)
|  |  ── 查重仅代表本工具未处理过此内容，不代表站上不存在（advisory）── |        |  <- R36 静态诚实句，常驻
|  +------------------------------------------------------------------+        |
+----------------------------------------------------------------------------+
```

`[+ New job]` 以 create 模式打开 JOB 视图（内含 job-id + 「爬一个 URL / 匯入资料夾」二选一 + dry-run 勾选）。成功后**同一视图重渲染为该 id 的工作台**（无需二次输入 id），并立即进入进度模式（G1）。这把缺失的 CLI `run` 流程（parity gap P1）组合成视图内的引导序列：create → `crawled` 出现 Process → packet → 签核，每步都是同一工作台里的下一个合法动作。

### SETUP 视图（onboarding + R33 技术/日常拆分）

```
+-- #view-setup --------------------------------------------------------------+
|  [ < Inbox ]   SETUP（一次性技术设定——日常工作不需要这些）                  |
|  就绪清单 READINESS CHECKLIST                                               |
|   ✓  LLM endpoint  (base_url + model 已设)                                  |
|   ✓  API key       (api_key_set = true，存于 OS keyring)                    |
|   ✗  Reviewer 白名单 (0 reviewers — 签核被阻)  ⤷ 在 config.yaml，GUI 无法改   |
|   ◐  Crawler allowlist (config.yaml 只读；除非后端扩展，否则「无法确认」)     |
|  可在此编辑（3 个日常 LLM 字段）                                            |
|   Base URL [____]   Model [____]   API key [••••] (留空=不变，永不回显)      |
|   Allowed hosts (由 Base URL 自动推导，只读)                                 |
|   [ Save settings ]                                                        |
|  仅 config.yaml（GUI 无写入路径）：reviewers · crawler allowlist · 逾时/重试 · storage |
|  路径：{config_path}    [ Re-check ]                                        |
+----------------------------------------------------------------------------+
```

> **杀掉共享 `#job-id` 文本框。** 选择=「打开一个 job」（`openJob(job_id)` 设 `currentJobId` 模块变量并切到 JOB 视图），不是编辑文本框。job id 显示为只读标题，杜绝「签核中误把动作打到错 job」一整类错误，且是状态门控可供性的结构性使能（恰好一个 job 「打开」，视图才能算其合法动作）。`supersede` 的「新 job id」与 create 模式的 job-id 输入仍保留——那是操作者新撰写的 id，与选择现有 job 不同。

### 全应用导航流程图

```
                         init()
                           | get_settings + reviewers
                  ready? --+-- no --> [ SETUP ]  (自动打开，G7 首跑；base_url 字段聚焦)
                     | yes              | Save / 修 config
                     v                  v (ready)
                 [ INBOX ] <==============================================+
                   |  [Open >] 行            [+ New job]                  |
                   v                              |                       |
              openJob(id)                  [ JOB: create mode ]           |
                 state<-job_status/list_jobs      | create&fetch (async)  |
                 packet body 可选<-get_packet     v                       |
        +----> [ JOB: workspace ] <---------------+                       |
        |        | 状态门控动作                                            |
        |        | - process_async --> [JOB: progress] --done--+          |
        |        | - make_review_packet(freeze) / approve / reject* /     |
        |        |   supersede* / resolve(relint|override+reason)         |
        |        | - backfill*(URL+attest+reviewer)                       |
        |        | - blocked/duplicate -> 只读，无动作                     |
        |        +--------------------------------------------------------+
        |  [ < Inbox ] / 终态动作后 ----------------------------------------+ (Inbox 重渲染：实时计数)
   [ Setup ⚙ ] 顶栏任意时刻可达。   * = in-DOM 确认面板。
```

---

## Core Flows（核心流程）

### 流程一：日常循环（happy path + 现实分支）

```
CREATE (JOB create 模式)
  job-id + [爬 URL] 或 [用资料夹] + ☐dry-run
   └ create_and_crawl_async / ingest_dir  (异步 → 实时进度，修 G1；ingest 无 _async，同步+busy 锁)
        └ 轮询 job_status → 抓取结果落到 ↓ 其一
   ┌────────────┬──────────────┬───────────────┬────────────────┐
 CRAWLED     CRAWLED_WARN   NEEDS_REVISION   CRAWL_FAILED      (罕见 NEW，自动推进)
 [开始处理]   [仍可处理]      [重新处理/作废]   [重新抓取(同id, CRAWL_FAILED→NEW)]
   └────process_async────────┴───────────────┘
              PROCESSING (蓝色不确定 spinner + 静态阶段名 risk→dedup→assemble→lint→ground)
              └ 轮询 job_status → Stage-2 自动落到 ↓ 其一
   ┌──────────┬─────────┬──────────┬────────────────────┬─────────────┬──────────┐
 PROCESS_   BLOCKED   DUPLICATE  NEEDS_HUMAN_REVIEW    NEEDS_REVISION PROCESSED
 FAILED     (红终态)  (红终态)   (琥珀, reason: risk|   (琥珀)        [建立审阅包(冻结)]
 [重试处理]  无动作    无动作      dedup|grounding)                          │ make_review_packet
                                  └ HOLD 子流程(流程二)                      ▼
                                                              REVIEW_PENDING (靛蓝冻结)
                                                              [核可] [退件*] [作废*]
                                          approve(reviewer)──────┘
                                                              APPROVED (靛蓝；机器到此为止 R26)
                                                              「先手动上架，再回来回填」
                                          backfill(reviewer,url,attested=True)──┐ 需 URL+勾选+reviewer
                                                              PUBLISHED_RECORDED ✓ (绿终态)
```

三个对本工具具体的要点：
1. **G1 在两个长调用处修复**（`create_and_crawl_async`, `process_async`），经完整 6-shape `job_status` 解包 + 轮询上限（见 Interaction & Async States）。同步调用（`make_review_packet`/`approve`/`reject`/`resolve`/`backfill`/`supersede`）快、不轮询，但仍上 busy 锁防双击。
2. **PROCESSING 永不出现在 worklist**（transient），唯一可见处是操作者刚启动的那个 job 的实时 spinner。
3. **每个红终态显示零按钮**——「fail-closed、无 approve-anyway」的视觉体现。`blocked/duplicate` 连 supersede 都非法（其 `_TRANSITIONS` 为空 frozenset）。
4. **无「re-crawl」可供性**于 `crawled`/`crawled_warn`：`_TRANSITIONS` 只有 `CRAWLED→{CRAWLED_WARN,PROCESSING}` 与 `CRAWLED_WARN→{PROCESSING}`，无重爬边，且重爬会撞 R11（不可覆盖 job）。要重抓→走 `[+ New job]` 用新 id（文案引导）。

### 流程二：hold-resolution（NEEDS_HUMAN_REVIEW，最高风险分支）

`resolve()` 后端按 reason 分支，UI 同样分支。点「Review & resolve」把动作区换成 in-DOM 子面板（无 JS prompt）。若 `reviewers()` 为空，此处也以 onboarding banner 取代 reviewer 下拉与「清除 hold」按钮。

```
┌─ 处理这个 hold ─────────────────────────────────────────────────────┐
│ ── 若 review_reason == "grounding" ──                                │
│  「机器无法自动确认每句对回来源句子。grounding 是你的判断，机器不保证。」 │
│   ( ) 重新检查 (relint=true)  「核对过出处就选这个；lint 通过自动放行，不需理由」 │
│   ( ) 人工放行 (relint=false) 「需要你写一句理由」 理由:[______]      │
│ ── 若 risk / dedup ── (无 relint 选项，仅 override)                   │
│  risk:  「风险闸（诽谤/可识别个资/缺出处）拦下，要清除须写理由，入审计」 │
│  dedup: 「疑似重复。⚠ 『unique』只代表本工具没处理过——不代表站上没发过。请自行复查站点。」 │
│   理由(必填):[______________________]                                │
│   Reviewer:[ 王小明 ▼ ]  (空→onboarding banner)                       │
│   ┏ 清除 hold → PROCESSED ┓  → resolve(job,reviewer,relint,reason)    │
│   ── 其他出口 ── [ 退件… ]→reject  [ 作废… ]→supersede  [ 取消 ]      │
└──────────────────────────────────────────────────────────────────────┘
```

UI 镜像 `resolve()` 的校验（防止操作者撞到原始后端错误）：grounding+relint → reason 省略，lint 干净自动升 `processed`；grounding+override / risk / dedup → **「清除 hold」按钮在写入非空理由前保持禁用**（前端 + 后端双重防护）；dedup 永远显示 R36 诚实警语（强制，非可选）。

### 流程三：首跑（first-run，G6/G7）

```
launch → init() → computeReadiness()（get_settings + reviewers）
  ├ P1/P2 未设(含空默认串) → gate banner Variant A + #settings-base-url 聚焦；pipeline 按钮软禁用
  ├ 仅 config 项(P3/P4)缺   → Variant B：「需一次性技术设定」，列出缺项 + 指向 handoff 卡
  ├ P3 无法确认(后端未扩展) → Variant C-partial「大致就绪，一项无法核对」(绝不绿)
  ├ 四项全部正向确认       → Variant C 真绿(自动收成细条)
  └ get_settings/reviewers 报错 → Variant A + 逐字转义错误消息 + 友好桶(exit 2=输入问题/exit 3=设置问题)
  操作者填 LLM 面板 → Save → 再 computeReadiness → 重渲染 + applyGating
  (config 项：助手改 config.yaml → Re-check → 同上)
```

---

## State & Message Legibility（机器说人话 — 完整映射表）

**一个想法：一张 `lex.js`、三个渲染点。** 后端发出的一切是闭合枚举（15 + 3 + 5），所以一张静态字典可零风险翻译全部。三个渲染点：worklist Reason 单元格、workspace 状态横幅、inline 错误块。操作者可塑/攻击者可塑的尾巴（`error` 消息串、坏 `job_id`）保持转义，只作为引用的**「技术细节」**呈现，绝不作 headline。

> **检测错误：任何带 `error` 键的 dict** 即错误响应（无 HTTP 状态，这是 JS 桥）。switch on `exit_code`，**不** switch on 消息文本。唯一永不报错的方法是 `disclaimer()`。
> **「技术细节」披露机制（R41 pin 死）：** 一个 `<button>` + `addEventListener` 切换兄弟元素的 `.hidden`（或原生 `<details>`/`<summary>`）。**绝不**内联 `onclick=`（违反 `script-src 'self'`）、**绝不** innerHTML 切换；细节文本经 `createElement` + `textContent` 写入。

### A. STATE 映射（全部 15 个 `JobState`）— 标题 · 为什么 · 下一步 · tone

| state | 人类标题 (badge) | 为什么 (plain WHY) | 下一步 (→ Api) | tone |
|---|---|---|---|---|
| `new` | 刚建立 ● | 工作刚建好，素材还没抓进来。 | 抓取网址或匯入资料夾 → `create_and_crawl`/`ingest_dir` | neutral |
| `crawled` | 素材已就绪 ● | 素材抓干净了，可以开始处理。 | 点「开始处理」→ `process` | ready |
| `crawled_warn` | 素材已就绪·部分缺漏 ! | 抓到了，但有些图片/影片没抓全，仍可处理。 | 直接「开始处理」→ `process`（要补齐请开新 job） | caution |
| `processing` | 处理中… ⟳ | 正在跑风险/查重/改写/校验，这步要连 LLM，会花点时间。 | 不用动作，等进度跑完（画面不会卡，可离开）→ 轮询 `job_status` | busy |
| `process_failed` | 处理没跑完·可重试 ✕ | 处理中途出错，没留下半成品（可安全重跑）。 | 点「重试处理」→ `process` | retry |
| `crawl_failed` | 没抓到内容·可重试 ✕ | 整页都抓不到（网址错/被挡/站点没回应）。 | 检查网址后「重新抓取」→ `create_and_crawl` | retry |
| `processed` | 草稿完成·待冻结 ● | 草稿做好且通过检查，还没锁定成审阅包。 | 点「建立审阅包」锁定版本 → `make_review_packet` | ready |
| `blocked` | 风险封锁·终止 ✕ | 命中红线（如未成年/隐私/重大风险），不可发布。 | 不能放行；只能退件或作废。此为终点，仅供检视。 | stop |
| `duplicate` | 重复·终止 ✕ | 与既有内容重复，不应再发。 | 此为终点，仅供检视。⚠ 仅代表本工具处理过，非全站查重。 | stop |
| `needs_human_review` | 需人工判断 ⚑ | 某道关卡没把握，交给你判读（见原因标签）。**这是人工确认，不是失败。** | 依原因处理（见 B 表）→ `resolve`/`reject`/`supersede` | review |
| `needs_revision` | 内容需补正 ✎ | 缺了标题或内文等必填项（可修可重跑）。**若 LLM 被截断也落这里，内容可能不完整，建议重跑。** | 补齐后「重新处理」或「作废」→ `process`/`supersede`（无退件选项，见下注） | review |
| `review_pending` | 已冻结·待签核 (corner ribbon) | 审阅包已建立，草稿已**冻结**锁定，等你核可。 | 看完后核可／退件／作废 → `approve`/`reject`/`supersede`（核可后仍须你手动上架） | action(frozen) |
| `approved` | 已签核·待你手动上架 ✓ | 已通过签核——这是机器能到的最远一步。**机器不会自动发布。** | 你手动上架后，回来「回填网址」并勾确认 → `backfill` | action(frozen) |
| `rejected` | 已退件·终止 ✕ | 审阅者退回，不采用。 | 终点，仅供检视。要重做请开新工作。 | done |
| `superseded` | 已作废 ✕(struck) | 此版作废，由新工作取代。 | 终点，仅供检视。请改看接手的新 job id。 | done |
| `published_recorded` | 已上架并登记 ✓ | 你已手动上架、回填网址并具结确认，全程完成。 | 完成，仅供检视。 | done |
| *(未映射回退)* | 未知状态 ? | — | — | neutral |

注：`processing` 永不作 worklist 行（其行文案不用，仅 banner 用）；`approved`/`published_recorded` 文案不得暗示机器已发布（R26）；`duplicate` 内嵌 R36 诚实警语；`needs_revision` 的「为什么」携带 R34 截断交叉引用。

### B. HOLD-REASON 映射（3 个 `ReviewReason`，仅当 `state == needs_human_review`）

| review_reason | 原因标签 | 为什么停下 | 下一步（分支感知） | 红线提醒 |
|---|---|---|---|---|
| `risk` | 风险待判读 | 偵测可能风险（疑似诽谤/可识别个资/缺出处授权）。「不确定就停」交给你。 | 二选一：**人工放行**(需写理由)→`resolve(reason=…)`；或**退件/作废**。**无「强制通过」按钮。** | 不提供绕过；fail-closed 是刻意的 |
| `dedup` | 疑似重复待确认 | 查重觉得「可能」重复但不确定，没直接判定，留给你看。 | 二选一：**人工放行**(需写理由)→`resolve(reason=…)`；或**作废**。⚠ 查重仅基于本工具处理过的内容，不等于全站查过。 | 必须显示 dedup 可靠度低的诚实警语 |
| `grounding` | 可信度待确认 | 机器无法把叙述句对回出处句子，不敢替你掛保证。「叙述是否有出处依据」目前是**你的责任**，不是系统保证。 | 两条路：(1) 已核对出处→**重新检查**(relint)，通过自动放行→`resolve(relint=true)`；(2) 仍要放行→**人工放行**并写理由→`resolve(reason=…)`。 | 不可暗示机器已保证 grounding (R23) |

### C. ERROR / exit-code 映射（取代 `error (N): …`，5 桶）

| exit_code | 桶 | 人类标题 | 为什么 | 下一步 | framing |
|---|---|---|---|---|---|
| 1 | usage | 操作方式不对 | 指令用法/必填栏位有问题。GUI 一般不会走到这。 | 若出现，多半版本不符——重开程式或回报。 | 系统面，非你的错 |
| 2 | input | 你填的内容要修 | 输入某值不对：网址格式错/job id 不存在/网址不在允许清单。 | 照「技术细节」修正那一项再送一次。 + [跳到对应栏位] | 你的输入，可自行修 |
| 3 | dependency | 还没设定好 (setup needed) | 缺前置：LLM 金鑰没填，或缺 ffmpeg 等本机工具。 | LLM 类→开「Setup」填好存档；其他→找装机的人。 + [Go to Setup] | 一次性技术设定 (R33) |
| 4 | external | 外部服务暂时不通 | 连外部失败：LLM 逾时/5xx 或网路出错。**不是你的错。** | 稍等几分钟按同一颗按钮**重试**（重跑安全）。 + [Retry] | 外部问题，安全重试 (R29/R34) |
| 5 | internal | 程式出了状况 | 程式遇到没预期的错（我们的 bug，不是你的操作）。 | 把「技术细节」截图回报；通常重试/重开可继续。 | 我们的 bug，非你的错 |

**map 也拥有的特例（仍是闭合词汇）：**
- **backfill 缺勾选/缺 URL = ERROR DICT（`exit_code:2`），在落入通用 input 桶前先拦截。** `signoff.py` 在 `attested=false` 或 URL 空时 **raise** `InputValidationError`，跨桥为 `{error, exit_code:2}`，**绝不**返回 `state:"approved"` 的成功 dict。renderer 按服务器自有稳定短语拦截：消息含 `attestation required` → 「尚未完成：你没勾『上架内容＝已签核版本』，工作仍停在『已签核』。勾选后再送一次。」；含 `published URL is required` → 「请先填『已上架的网址』再送出。」**不要靠「state 仍 approved」检测——该 payload 永不到达。**
- **`finish_reason != "stop"`（R34 截断诚实）仅在 `get_packet()` 渲染点触发。** `process()` 成功 dict **不**携带 `finish_reason`；截断草稿正常落 `needs_revision`（该步显示 needs_revision 文案，其「为什么」已交叉引用截断）。仅当操作者后续打开一个非 `stop` 的 packet 时触发横幅：「草稿可能被截断 (finish_reason: <value>)：LLM 没正常收尾，内容可能不完整，建议重新处理。」
- **`reviewers()` 返回 `[]`（G6）：** 签核区显示「还不能签核：尚未设定审阅者名单。请在 config.yaml 的 publisher.reviewers 加入名字（一次性技术设定）。」无 GUI 写入路径，文案指向文件而非按钮。

### `lex.js` 工件（一文件 ~120 行，纯数据 no logic）

同源 JS 对象字面量（CSP `script-src 'self'` 已允许）。三个瘦 renderer 在 `app.js` 消费它，取代今日 `handleResult` 的 `"error (" + exit_code + "): "` 拼接（app.js 现 line 52）与两处 inline error 点。所有值流经既有 `setText`/`el` 收口 → 仍 `textContent` only。未知枚举回退到原始 token（前向兼容：新后端状态降级为其码，绝不空白或崩溃）。

### state → 可供性绑定（横幅显示哪些按钮，从 `_TRANSITIONS` 逐行验证）

| state | 横幅按钮 | 需 in-DOM 确认 |
|---|---|---|
| crawled / crawled_warn | [开始处理] | — |
| process_failed | [重试处理] | — |
| crawl_failed | [重新抓取] | — |
| needs_revision | [重新处理] [作废…] | 作废 |
| needs_human_review (risk/dedup) | [人工放行…(需理由)] [退件…] [作废…] | 放行/退件/作废 |
| needs_human_review (grounding) | [重新检查] [人工放行…] [退件…] [作废…] | 放行/退件/作废 |
| processed | [建立审阅包] | freeze（提醒：冻结后不能改，只能退件/作废） |
| review_pending | [核可] [退件…] [作废…] | 退件/作废 |
| approved | [回填网址并具结…] [作废…] | backfill-attest |
| blocked/duplicate/rejected/superseded/published_recorded | (无按钮，只读；worklist 用 [检视] 进入) | — |

> **守卫注（防实现陷阱）：** `needs_revision` 出口刻意是 `{重新处理→process, 作废→supersede}`，**故意排除退件**——`state.py` 无 `NEEDS_REVISION→REJECTED` 边。reject 仅合法于 `REVIEW_PENDING`/`APPROVED`/`NEEDS_HUMAN_REVIEW`。因 reject 出现在相邻 hold/pending 态，实现者**不得**把退件按钮复制到 `needs_revision`（会报错）。按钮集从上方 A/B 表每态导出一次，绝不手抄。

---

## Interaction & Async States（异步、进度、空/载入/成功、in-DOM 确认）

### 一个 in-flight job 的 UI transport 状态机（修 G1）

后端 15 状态之上叠加一个小的 transport 状态，纯由 `job_status(jobId)` 返回驱动。

```
点 "Create & crawl" / "Process" → 客户端校验(job-id 非空? url/dir 在?)
  NO → UI:INVALID (栏位下红字提示，无 spinner，无锁)
  YES → 调 *_async() → {status:"running"} → UI:SUBMITTING(<300ms乐观) → UI:RUNNING(每1500ms轮询)
        RUNNING 按 job_status 响应形态分支 → DONE / FAILED / SETTLED / LOST
```

### `job_status` 解包表 — **6 种形态**（本处 #1 bug 来源）

`job_status` 不返回统一 `{status:...}` 信封。**轮询器必须在 `switch` 前先守卫顶层 `{error}`，并有显式 `default:` fail-safe，否则 spinner 永转。**

| 响应形态 | `status` | 有 `result`？ | transport 态 | 读取 | 操作者看到 |
|---|---|---|---|---|---|
| `{error, exit_code}`（顶层，**无** status） | 缺 | 否 | FAILED | `resp.error`, `resp.exit_code` | 人类化错误 + Retry |
| `{status:"running"}` | running | 否 | RUNNING | — | spinner 续转，重新轮询 |
| `{status:"done", result:{…}}` | done | 是 | DONE（**先看 result**） | 若 `result.error` 在→当失败处理；否则 `result.state`/`result.notes` | 成功横幅 + 新状态徽章 |
| `{status:"error", result:{error,exit_code}}` | error | 是(嵌套) | FAILED | `result.error`, `result.exit_code` | 人类化错误 + Retry |
| `{status:"idle", state:…}` | idle | 否 | SETTLED | 顶层 `state`（来自 SQLite） | 「上次已完成」+ 反映状态 |
| `{status:"unknown"}` | unknown | 否 | LOST | — | 「此 job 已不在磁碟上」+ 重建 |
| *(其他/缺失，未来防御)* | 其他 | — | FAILED | — | 「与本机引擎失去联系——请重开程式。」 |

> `idle` 之所以存在：内存 `_status` 在 app 重启时丢失——上一会话完成的 job 报 `idle`（从 SQLite 读），**不是 `done`**。UI 把 `idle` 当**轮询的合法终点**：读 `state`、停止轮询，**绝不**当错误、绝不空转等 `done`。

### 轮询节奏、生命周期、再入规则

- **节奏：固定 1500ms `setTimeout` 链（非 `setInterval`）**——`setTimeout` 链（仅在当前轮询 resolve 后排下一次）防轮询堆叠卡死单线程 pywebview 桥。
- **每 job 一个轮询器**：模块级 `Map<jobId,{timer,kind,startedAt}>`，再点提交是 no-op（按钮也已禁用）——这是修复「操作者狂点冻结按钮」的真正守卫。
- **重启后恢复 = 对当前 `#job-id` 单次探测**（修正：不扫所有行——`PROCESSING` 永不持久化，旧的「扫 transient 行」predicate 选空集，是死代码）。`init()` 若 `currentJobId` 非空则探测一次：`running`→恢复轮询；`idle`→SETTLED（正常重启情形，从 SQLite 读静止态）；`unknown`/顶层错误→不 spinner，只反映 worklist。
- **有界轮询错误容忍**：**抛出的**桥错误（非 `{error}` dict）≤3 次静默重试（1500ms），再 FAILED「与本机引擎失去联系」。这是唯一需要 try/catch 处；业务错误都作为 dict 到达。
- **无与后端矛盾的客户端逾时**：LLM 调用拥有自己的 `timeout_seconds`；UI 只要 `running` 就续轮询，仅显示 elapsed `mm:ss` 让操作者判断。加一个**硬轮询上限**（约 120 次/~90s）：触顶则停 spinner，显示「还在处理——比平常久。让它继续，或稍后看 worklist。」spinner **永不无限转**。

### in-flight 视觉（操作者实际所见）

因 `PROCESSING` 永不入 list，in-flight 指示器挂在当前 `#job-id`/JOB 工作台进度模式上：

```
+-- #view-job (progress mode) ------------------------------------------------+
|  JOB  melon-2026-0042                                                      |
|  ◐ 正在请模型组装草稿…  01:12   (这会花点时间——视窗不会卡死，可继续操作)    |  ← aria-live=polite 活区
|     Running risk → dedup → assemble → lint → ground (静态阶段名，非真步进)   |
|     [ • • • ◦ ◦ ]  (不确定 spinner，绝非假百分比)                            |
+----------------------------------------------------------------------------+
```

- spinner = **CSS-only 动画**（`@keyframes`），无 GIF、无远程资源（`img-src 'self'`）。
- **reduced-motion 回退（CSP-clean）**：JS 读 `window.matchMedia('(prefers-reduced-motion: reduce)')`；匹配则不 CSS 旋转，改每个 poll tick 写 `span.textContent` 的循环字形 `◐◓◑◒`——纯 textContent 变更，留在 R41/`style-src 'self'` 内。
- **阶段标签诚实**：后端无 sub-progress 百分比，**无假进度条**；两个标签按 `kind`：crawl→「Fetching the page…」、process→「Asking the model to assemble the draft…」（dry_run→「Running a safe preview (no model call)…」，因 R32 dry-run 不连 LLM）。
- **显式「视窗不会卡死」安抚句**——直接对抗 G1 症状。面板非模态，可滚到 worklist/setup。

### EMPTY / LOADING / SUCCESS 态（其余 G4）

- **Empty（每面板）**：Inbox 空 band→「收件匣已清空——没有待办」（首跑→「建立第一个 job」指向 `[+ New job]`）；worklist 过滤无果→「<state> 没有 job。清除过滤看全部。」；reviewer `<select>` 为空→**onboarding-critical**：不可移除的禁用选项「⚠ 没有审阅者，无法签核。在 config.yaml 加 publisher.reviewers（一次性设定）」并禁用 Approve/Reject/Resolve/Backfill。
- **Loading**：每个 init 拉取的面板先插一行「Loading…」占位行（`createElement`），结果到时清除；`get_packet` 大 dict 显示「Loading packet…」；Refresh 按钮拉取期间禁用防叠加。
- **Success affirmation**：`handleResult` 重写为三变体横幅（`.ok`/`.warn`/`.err`，CSS 类无内联 style）。**来源纪律（修正）**：横幅只能断言原始调用 result dict 实际携带的字段。`process()` 返回 `{state, stopped_at, dry_run, notes[]}`，**不**携带 `blocking_reasons`/dedup 细节。所以终态闸杀（blocked/duplicate）横幅只命名闸（`stopped_at`）并显示 `notes[]`，不伪造它取不到的人类化拦截原因；更丰富的解释指向 worklist 行 `review_reason` / `get_packet().review_reason`。横幅用 `aria-live="polite"`，~6s 后视觉淡出但文本留 DOM（信息不丢）。

### in-DOM 确认（终态/不可逆，禁 `confirm()`）

`reject`/`supersede`/`backfill-attest` 加一个就地展开的两步确认条（非模态、无遮罩、无焦点陷阱）：首次点**arm**→显示确认条→第二次点**异标按钮**提交。Escape/Cancel 解除。**同时只可 arm 一个确认条。**

```
[ 退回 Reject ] ← 点
   ┌─ confirm-tray (红框) ──────────────────────────────────┐
   │ 退回后此 job 进入「已退回·终止」，无法复原（只能作废另开新）。│
   │ 理由(必填):[__________]  Reviewer:[王小明▾]               │
   │              [ 取消 ]   [ 确定退回 ]  ← .btn-danger.is-armed │
   └──────────────────────────────────────────────────────────┘
```

- arm 步**前端校验前置**（Reject 需非空理由 + reviewer；缺则显示提示而非提交键），不送操作者进必然报错。
- **backfill 无操作检测按返回的 `attested` 布尔分支，不按 `state=='approved'` 单独判断**——忘勾的结果和全新 approved job 都是 `state=='approved'`。规则：刚做过 backfill 且返回 `attested===false`→警告「未记录——你没勾选版本相符，job 仍 APPROVED 在等」；从未 backfill 的全新 approved→保持中性「已签核——先手动上架再回填」（不显示吓人警告）。
- backfill 确认条强制反过度宣称句：「你是在**具结**，不是证明。我们不会抓取或核对此 URL」（R37/R41 inert-link 纪律可见）。

### 文件增量（无构建步骤）

- **`app.js`**：`create_and_crawl`→`create_and_crawl_async`（~line 239）、`process`→`process_async`（~line 251）；新增 `pollUntilSettled`（顶层错误守卫 + `default:` fail-safe + 6-shape switch + 轮询 Map + init 单探测恢复 + 轮询上限）、`setBusy`、`mountSpinner`（matchMedia 分支）、inline-confirm `arm/commit/disarm` 三件套、empty-row helper，并把 `handleResult` 重写为 3 变体横幅。复用既有 `el/setText/clear/renderField/renderList`（textContent 纪律不动）。
- **`index.html`**：`#actions` 内加 `<div id="inflight">`(aria-live)，Reject/Supersede/Backfill/crawl 下加 `<div class="confirm" hidden>` 槽；无新 `<script>`、无内联 handler，CSP 不变。
- **`app.css`**：加 `@keyframes spin`、`.spin`、`.banner.ok/.warn/.err`、`.confirm`、`.empty`、`@media (prefers-reduced-motion: reduce)` 与禁用按钮样式。
- **后端零改动**：`create_and_crawl_async`/`process_async`/`job_status` 已存在。
- **注**：`ingest_dir` 无 `_async` 孪生（本地无网络，保持同步但仍上 busy 锁）。若实测慢，`ingest_dir_async` 是后端 follow-up（已标记，非假设）。

---

## Visual Design System（视觉系统 — 具体 tokens / 色彩语义 / 组件 / 信任语言，修 G5）

一个重写的 `app.css`（~230 行）：token 块 + 组件规则，静态同源文件。无新 HTML 结构必需；tokens 挂在既有 `section/table/button/.status` 选择器上，但假设兄弟 JS 维度设少量 class hook（`el.className`/`setAttribute('class',…)`）与 data 属性（`setAttribute('data-glyph',…)`）——皆 R41 允许（属性/属性写入，绝非 innerHTML、绝非内联 style）。

**三条为兄弟 JS 维度 pin 死的渲染纪律：**
1. **state→class** 从闭合 `JobState` 计算；JS map **必须有 default 分支**发 `.badge--unknown`/`.lane--unknown`，使未来枚举值可见地被标记、绝不静默无样式。
2. **字形仅装饰。** WCAG 1.4.1「颜色绝非唯一信号」的保证落在**人类文字标签**（未開始/需人工判斷/已凍結·待簽核/風險封鎖·終止/已作廢…，皆 `textContent`）。字形可在 WebKitGTK 上 tofu 而不破坏含义。
3. **show/hide 仅经原生 `hidden` 属性**——`el.hidden = true/false`（或 `removeAttribute('hidden')`），**绝不** `el.style.display`。CSS 提供 `[hidden]{display:none}` 作安全网，但属性是契约。

### 设计 tokens（粘贴在 `app.css` 顶部）

```css
:root {
  /* 字阶 (1.20 minor-third, 16px base；CJK + Latin 安全) */
  --fs-300:.78rem; --fs-400:.875rem; --fs-500:1rem; --fs-600:1.15rem; --fs-700:1.4rem; --fs-900:1.85rem;
  --fw-regular:400; --fw-medium:500; --fw-bold:700;
  --lh-tight:1.25; --lh-body:1.55;   /* CJK 正文读起来更松 */
  --font-ui: system-ui, -apple-system, "PingFang TC","PingFang SC","Microsoft JhengHei","Hiragino Sans GB", sans-serif;
  --font-mono: ui-monospace,"SF Mono",Menlo,Consolas,monospace;  /* hash, job-id */
  /* 间距 (4px base) */
  --sp-1:.25rem; --sp-2:.5rem; --sp-3:.75rem; --sp-4:1rem; --sp-5:1.5rem; --sp-6:2rem; --sp-7:3rem;
  --radius-sm:4px; --radius-md:8px; --radius-pill:999px;
  --border-w:1px; --border-w-strong:2px; --focus-w:3px;
  /* 中性暖灰 */
  --ink-900:#1a1d21; --ink-700:#3c424a; --ink-500:#5e6671;
  --paper:#fff; --paper-2:#f5f6f8; --paper-3:#eceef2; --line:#d4d8df; --line-2:#b9bfc8;
  /* 状态语义色（bg/border/text，皆 WCAG≥4.5:1 已验） */
  --c-neutral-bg:#eceef2; --c-neutral-bd:#b9bfc8; --c-neutral-tx:#3c424a;  /* GREY: new/crawled/processed/unknown */
  --c-progress-bg:#e4eefb; --c-progress-bd:#4f86d6; --c-progress-tx:#143a73; /* BLUE: processing/running */
  --c-attention-bg:#fdf3da; --c-attention-bd:#c8901b; --c-attention-tx:#6b4900; /* AMBER: 需要你（非失败） */
  --c-stop-bg:#fce6e4; --c-stop-bd:#c2342a; --c-stop-tx:#8a1209;  /* RED: 死路/硬停 */
  --c-go-bg:#e3f3e8; --c-go-bd:#2f8f4e; --c-go-tx:#145129;  /* GREEN: 安全推进/结案-good */
  --c-frozen-bg:#e9e7f7; --c-frozen-bd:#6a5acd; --c-frozen-tx:#342a78; /* INDIGO: review_pending 冻结 */
  --c-void-bg:#e6e7ea; --c-void-bd:#8b909a; --c-void-tx:#4a4f59;  /* SLATE: superseded 作废 */
  --focus-ring:#1f6fd6;
}
```

**为什么 6 个色族（非 5）：** REVIEW_PENDING（冻结）**绝不能**看起来像通用「进行中」蓝或「go」绿——冻结的 packet 是不可变信任凭证，专属靛蓝；SUPERSEDED 须读作「作废」（灰 + 删除线），区别于红「rejected」。这把冻结保证与 supersede 语义编进颜色。**琥珀=需要你（非 error）**，**红专留死路**——操作者绝不混淆「我得动手」（琥珀）与「到此为止」（红）。16px 正文底线（非 14px）因 CJK 字形需更多像素。

### state → 色 → 徽章映射 + 对比验证

徽章是 pill：小型大写**人类文字标签**（承载非颜色信号）+ **装饰性前导字形**。字形限于跨 webview 安全子集 `● ○ ! ✓ ✕ ✎ ⚑ ?`（弃用会在 WebKitGTK tofu 的 `⌀ ⊘ ◇ ⟳ 🔗 🔒`）；「frozen」用 CSS-drawn corner ribbon（文字「已凍結 FROZEN」）承载，非字形。三个 stop 态共享 `✕` 但靠文字标签消歧（·可重試 vs ·終止）。

```css
.badge { display:inline-flex; align-items:center; gap:var(--sp-1);
  font-size:var(--fs-300); font-weight:var(--fw-medium); line-height:var(--lh-tight);
  padding:2px var(--sp-2); border-radius:var(--radius-pill);
  border:var(--border-w) solid currentColor;  /* 强制 WCAG 1.4.11：边=自身文字色，不靠软 bg。不可删 */
  white-space:nowrap; }
.badge::before { content:attr(data-glyph); font-weight:var(--fw-bold); }  /* 装饰 only */
.badge--neutral,.badge--unknown{background:var(--c-neutral-bg);color:var(--c-neutral-tx);}
.badge--progress{background:var(--c-progress-bg);color:var(--c-progress-tx);}
.badge--attention{background:var(--c-attention-bg);color:var(--c-attention-tx);}
.badge--stop{background:var(--c-stop-bg);color:var(--c-stop-tx);}
.badge--go{background:var(--c-go-bg);color:var(--c-go-tx);}
.badge--frozen{background:var(--c-frozen-bg);color:var(--c-frozen-tx);}
.badge--void{background:var(--c-void-bg);color:var(--c-void-tx);text-decoration:line-through;}
```

文字-on-bg 对比皆 ≥7.4:1（AAA）。徽章边界（WCAG 1.4.11 非文本对比）：软 bg 与纯白 <3:1，故每个 `.badge` 用 `border:1px solid currentColor`（边=文字色，7.4:1+）——**强制非装饰**。

### 布局与节奏（套在既有 7-section 单滚动 / 三视图根上）

```css
*{box-sizing:border-box;}
body{font-family:var(--font-ui);font-size:var(--fs-500);line-height:var(--lh-body);
  color:var(--ink-900);background:var(--paper);margin:0 auto;padding:0 var(--sp-6) var(--sp-7);max-width:64rem;}
header{position:sticky;top:0;z-index:1;background:var(--paper);
  border-bottom:var(--border-w) solid var(--line);padding:var(--sp-3) 0;margin-bottom:var(--sp-5);}
section{border-top:var(--border-w) solid var(--line);padding:var(--sp-5) 0;margin-top:var(--sp-3);}
[hidden]{display:none;}   /* 视图切换/确认条/提示的安全网；JS 经 el.hidden 切换 */
:where(input,select,textarea,button,a):focus-visible{outline:var(--focus-w) solid var(--focus-ring);outline-offset:2px;}
```

### 组件（节选）

- **按钮三意图**：`.btn-primary`（实心绿/墨，白字，状态的唯一前进动作）；secondary（描边墨，非破坏）；`.btn-danger`（描边红；**仅在确认条内**填实心红 `.btn-danger.is-armed`）。`button[disabled]{opacity:.45;cursor:not-allowed}`（状态门控隐藏/禁用，G2）。
- **worklist 表**：「Why / next step」列是 G3 所在；每行 4px 左色条按 family 着色（`box-shadow:inset 4px 0 0 var(--c-*-bd)`，含 `.lane--unknown` 必需回退），操作者先按色带扫描再读字。
- **状态横幅**（载入/成功/错误，修 G1+G4）：`.banner` + `.banner--progress/--success/--error/--empty`，`border-left:2px solid currentColor`，进度条 CSS-only 不确定动画 `@keyframes slide`，`@media (prefers-reduced-motion:reduce)` 收为静态条。

### 信任 / 合规视觉语言（本维度核心）

- **常驻诚实声明 + 待校阅 framing（R18/R25）**：sticky header 让「署名非身分验证」句随滚动常在；每个 packet 卡带常驻「机器产生·待人工校阅」`.pending-ribbon`（琥珀）。
- **REVIEW_PENDING 冻结皮肤（冻结保证可视化）**：`.card.is-frozen` 2px 靛蓝边 + 霜色底 + **CSS-drawn corner ribbon「已凍結 FROZEN」**（文字非 emoji，WebKitGTK 安全）+ 「已冻结·不可再修改」说明 + `.hash-chip`（mono，`body_sha256` 作信任徽章）。**刻意无编辑可供性**，皮肤说明为什么：唯一出口是核可/退件/作废。
- **INERT 来源链接（R41/R2，微妙但承重）**：拆成不可选的可供性标签 `.inert-link__tag`（`user-select:none`，承载「來源(僅供查證,不可點擊)」+ 装饰 🔗）与**唯一可选的 URL span** `.inert-link__url`（mono、虚线边=「非活动控件」、`cursor:text`、`user-select:all`）——select-all 复制得干净可贴的 URL，无字形污染。（注：`get_packet().source_urls` 今为 `[]`，故无数据，但样式就绪并复用于同样 inert 的 `asset_ref`。）
- **dedup 诚实/fail-loud（R36）**：`.honesty-callout` 深琥珀 2px 边常驻：「查重仅代表本工具未处理过此内容，不代表站上不存在。」——静态固定句（后端无 `dedup_reliability` 字段可读），不过度承诺。
- **in-DOM 确认条（禁对话框）**：`.confirm-tray` 红框；`.confirm-tray[hidden]{display:none}` 仅安全网，JS 经 `el.hidden` 切换，绝不 `style=`。
- **onboarding 前置 chip（G6/R33）**：header LLM chip + 签核区 banner 以平白语言surface两个静默阻塞；`.precond.is-ready`（绿）/`.precond.is-block`（琥珀）。

### a11y & 韧性（约束，非可选）

颜色绝非唯一信号（文字标签是非颜色信号）；徽章边界 `currentColor` 强制；focus-visible 3px 永不移除；reduced-motion 收为静态；`prefers-color-scheme:dark` 出范围（YAGNI，单机 light 窗口；未来仅需重声明 `:root` token 块——无组件硬编码 hex）；静态骨架不依赖 JS（慢桥显示干净 empty 态，非未样式化 HTML）。

---

## Onboarding & Settings（前置条件清单、GUI-可编辑 vs config-only、校验）

**一个想法：** 把裸 3 字段表单换成 **Setup & readiness**，开头是 4 行就绪清单，让操作者在碰流水线前就看到哪些前置**已设 vs 缺失**；可就地编辑 2 个 GUI-可编辑的（LLM endpoint + API key）；对 2 个 config.yaml 技术任务（爬虫 allowlist、reviewer 白名单）给诚实、可复制的交接。任何前置缺失时，gate banner + 软禁用相关动作按钮（带一行提示），而非让操作者在流水线深处撞 `exit_code 3`。

### 四个前置条件与编辑归属

| # | 前置 | 为什么 gate | 后端信号 | GUI 可编辑？ |
|---|---|---|---|---|
| P1 | **LLM endpoint**（`base_url`+`model`） | `process` 连 LLM；空/无效→`DependencyError` exit 3 | `get_settings().base_url/.model`（空串⇒缺；默认皆 `""`） | **是**—`save_settings` |
| P2 | **LLM API key**（OS keyring） | 同上，无 key⇒assembly 时 exit 3 | `get_settings().api_key_set`（bool） | **是**—`save_settings`（仅 keyring，永不回显） |
| P3 | **Crawler allowlist**（`crawler.allow_domains`） | 空 allowlist 拒每个 crawl URL（R1）；默认 `[]` | **今未暴露**—`get_settings()` 不返回；需 §6 单行添加才成真信号 | **否**—config.yaml only（R33 左侧） |
| P4 | **Reviewer 白名单**（`publisher.reviewers`） | 空 list 静默阻塞全部签核 | `reviewers().reviewers[]`（空数组⇒缺） | **否**—config.yaml only（R33 左侧） |

**R33 拆分：** P1/P2 GUI-可编辑（日常相邻）；P3/P4 config.yaml-only（一次性技术设定）——它们是**安全/合规决定**（你合法能爬哪些站 R1、谁对签核负责 R25）。让非技术操作者从 GUI 文本框拓宽爬虫 allowlist 是合规 footgun（这是 SSRF/own-site 边界）。设计**不**让 P3/P4 GUI-可编辑，只让其**状态可见**并以文案交接「怎么做」。

### gate banner — 四个变体（杜绝假绿色）

载入时 + 每次 `save_settings`/`Re-check` 后由 `computeReadiness()` 计算，互斥四变体：
- **A**（P1 和/或 P2 缺，即字面首跑/新机器）：`⚠ SETUP NOT COMPLETE — N of 4 ready`，[跳到设置]，P1 缺时 `#settings-base-url` 聚焦。
- **B**（P1+P2 已设，仅 config 项 P3/P4 缺）：`⚠ 需一次性技术设定`，只列真缺项，指向 handoff 卡，诚实说明「by design 你无法在此修」。
- **C-partial**（P1+P2+P4 已设但 P3 **无法核对**，仅在后端未扩展时出现）：`◐ 大致就绪——一项无法核对`，**绝不**显示绿色「全部就绪」。流水线按钮启用（不知 allowlist 空，阻塞自身是 footgun），文案诚实警告「首次 crawl 是真正的测试」。
- **C**（四项全部正向确认，真绿）：仅当 §6 后端添加已落地且 `allow_domains.length>0`（落地前此变体不可达，显示 C-partial 代替）。自动收成细条。

> 精确选择：P1 或 P2 false→**A**；否则 config 项缺→**B**；否则 P3 是 `unknown`→**C-partial**；否则→**C**。`unknown` 视作「非绿」，永不当通过。

### Setup section body

- **就绪清单（4 行）**渲染进 `<tbody id="readiness-body">`（`createElement` only）：状态 pill（`● set` 绿 / `○ MISSING` 琥珀 / 仅 P3 在后端未扩展时 `◐ can't tell` 灰）+ 名称 + 平白后果 + 谁能修。
- **LLM endpoint 编辑器（P1/P2）**：同 3 输入，加**最佳努力的 advisory 内联校验（非 `validate_llm_base_url` 重实现）**——权威检查永远是服务器；advisory 在 `input` 事件触发，匹配服务器真实规则：空/非 http(s) scheme / `http://` 非 loopback（`localhost`/`127.x`/`::1` 字面）须 https / `value.replace(/\/+$/,"")` 不以 `/v1` 结尾（容忍尾随斜杠，镜像服务器 `s.rstrip('/').endswith('/v1')`）。分歧时服务器消息胜出（已转义，经 textContent，exit_code 映友好 framing）。**Key 字段语义**：明文「留空=保留现有 key」（`save_settings` 空 `api_key`=不变非清除）；存后从 DOM 清除（既有 app.js:222）、pill 重读 `api_key_set`。
- **config.yaml handoff 卡（P3/P4）**：渲染 `get_settings().config_path`（已转义）+ 要加的确切 YAML，作为 **inert、可选复制的 `<pre>`（`textContent`，无 innerHTML、无剪贴板 JS）**。文案陈述「空 list 的后果」并内嵌「署名非身分验证」framing（R25）。「Re-check」重跑 `computeReadiness()`。

### 逻辑：`computeReadiness()` + 软门控 `applyGating()`

```
async function computeReadiness(){
  const s=await api().get_settings(); const r=await api().reviewers();
  if(s.error||r.error) return {error:..., exit_code:..., config_path:s.config_path}; // 坏 YAML → A + 逐字消息
  return { p1_endpoint: !!(s.base_url && s.model), p2_key: s.api_key_set===true,
    p3_allowlist: ('allow_domains' in s) ? (s.allow_domains.length>0) : 'unknown', // §6 添加在? 真布尔:诚实 unknown→C-partial,绝不绿
    p4_reviewers: Array.isArray(r.reviewers)&&r.reviewers.length>0, config_path:s.config_path }; }
function applyGating(r){
  const pipelineReady=r.p1_endpoint&&r.p2_key; const signoffReady=r.p4_reviewers;  // P3 'unknown' 不阻塞（首爬是测试）
  setGroup(["btn-crawl","btn-ingest","btn-process","btn-packet"], pipelineReady, "setup-hint-pipeline");
  setGroup(["btn-approve","btn-reject","btn-supersede","btn-backfill"], signoffReady, "setup-hint-signoff"); }
function setGroup(ids,enabled,hintId){
  ids.forEach(id=>{const b=$(id); enabled?b.removeAttribute("disabled"):b.setAttribute("disabled","");}); // 仅属性，CSP-safe
  $(hintId).classList.toggle("hidden", enabled); }  // 提示节点静态预声明于 index.html，绝非 innerHTML
```

`applyGating` 在每次 `computeReadiness` 通过时调用（init / post-save / Re-check），**幂等**（既设又清 `disabled`，故陈旧禁用不会熬过完成的设置）。两个静态提示 span 预声明于 index.html（`#setup-hint-pipeline`「完成上方设置以启用」、`#setup-hint-signoff`「在 config.yaml 加 reviewer 以启用签核」），仅 show/hide，禁用按钮永不无提示。无轮询、无 timer（YAGNI）。

### 后端工作（按 brief 标记）— 强烈建议单行只读添加

| 添加 | 范围 | 为什么 | 风险 |
|---|---|---|---|
| **扩展 `get_settings()` 返回 `allow_domains`** | gui.py ~1 行：`"allow_domains":[escape_html(d) for d in c.config.crawler.allow_domains]` | 把 P3 从 `unknown`/C-partial 翻成真 set/MISSING；让绿色横幅诚实；删掉 C-partial 双路径 | 极小——只读、已转义、无写入路径、无 secret | 
| *(MVP 不建议)* GUI-可编辑 reviewers/allowlist | 新 bridge 方法 + IN-DOM UI | 会删掉 handoff 卡 | **合规风险**——allowlist 是 SSRF/own-site 边界；非技术操作者从文本框拓宽是 footgun。保持 config-only 是**刻意的 R33 拆分**。 |

**建议：** 做第一个（最干净地修假绿 bug），P3/P4 保持 config-only。落地前 C-partial 让横幅保持诚实。

---

## Constraints Honored（守护栏 — 实现必须保持）

- **R41 渲染纪律**：所有用户面文本经 `textContent`/`createElement`/`setAttribute`；**绝不** innerHTML / 模板字符串 HTML / eval / 内联 script / 内联 style。来源链接=惰性纯文本（不可点、永不抓取）。错误经 `{error, exit_code}` dict 跨桥，绝不抛异常。「技术细节」披露用 `<button>`+`addEventListener` 切 `.hidden` 或原生 `<details>`，绝非内联 `onclick`/innerHTML。复用既有 `el/setText/clear/renderField/renderList` 收口。
- **CSP（不变）**：`default-src 'none'; script-src 'self'; img-src 'self'; style-src 'self'; connect-src 'self'; object-src 'none'`。所有 CSS/JS/img 同源本地文件，无 CDN，无 web font（仅 OS 字体栈），无内联 style 属性。颜色一律经 class，spinner 经 `@keyframes`（无 GIF）。
- **Loopback-only（R40）**：127.0.0.1 自带 server；无外部 origin；无新网络调用，全经既有 js_api 桥。
- **浏览器对话框禁令**：无 JS `alert/confirm/prompt`（会阻塞 pywebview 桥）。所有确认（reject/supersede/backfill-attest/freeze）用 in-DOM 确认条（arm→commit→disarm），同时只 arm 一个。
- **CLI/GUI 对等（R30）**：每个 state-machine transition 动作两侧可达；只重组「何时可见」，不删动作。`run --until`（P1）由引导序列复现；`crawl --input` 文件（P2）GUI 走重复 NEW job 扇出（已知接受的缺口）。P3/P4 在 CLI/GUI 两侧皆 config-only。软禁用完全可逆。
- **平台**：pywebview 桌面窗口、静态同源文件、vanilla JS + 外部 CSS，**无构建步骤、无框架、无 CDN、无路由器、无主题引擎**。视图切换=`element.hidden`。增量约 ~230 行 CSS + ~180–230 行 JS，零新依赖。
- **R33 拆分**：P1/P2 GUI-可编辑（日常相邻）、P3/P4 config.yaml-only（一次性技术），并向操作者展示**原因**。
- **R19/secret 纪律**：api_key 永不回读（仅 `api_key_set`）；「留空=保留」；存后从 DOM 清除。
- **状态门控真实性（G2）**：动作集从 `state.py` `_TRANSITIONS` 逐边导出，不发明边（已删 `crawled`→re-crawl、`crawl_failed`→`ingest_dir`）；`blocked/duplicate` 零按钮；`needs_revision` 不含 reject。
- **诚实姿态**：fail-closed 读作「已停好」（琥珀）非「失败」（红）；查重静态诚实句常驻；新机器空/未知 allowlist 落 Variant A/C-partial，**绝不**绿；冻结/署名 framing 不过度宣称。

---

## Implementation Roadmap（分阶段、可交付）

> 阶段化排序原则：先让它**能跑且不吓跑人**（弃用修复），再让它**好看且流畅**，最后**打磨 onboarding**。每阶段单独可交付、可在真实 pywebview 窗口验证（直接攻 G7）。

### Phase 0（前置，~1 行后端，强烈建议先做）
- 扩展 `get_settings()` 返回 `allow_domains`（只读、已转义）。**关闭/简化**：让 P3 成真信号，删除 onboarding 的 C-partial 双路径与假绿风险。可与 P0 并行；不阻塞其余。

### Phase P0 — 让它跑 + 状态驱动可供性 + 易读性（弃用修复）
**关闭 G2、G3，并使 G7 首次真正可端到端走通。**
- **IA 骨架**：`index.html` 改为顶栏 `<nav>` 3 按钮 + 3 视图根 `<section hidden>`；`showView()`/`currentView`/`currentJobId`/`openJob()`；杀掉共享 `#job-id` 输入框。
- **状态门控动作区（G2）**：`STATE_ACTIONS` 表 + `renderActions(state, review_reason)`（含 `needs_human_review` 按 reason 分支、终态零按钮、无伪造边）。Inbox 4-band 分桶器（blocked/duplicate 单独展开 band、NEW 入「进行中」）。
- **易读性（G3）**：`lex.js`（15 state + 3 reason + 5 exit）+ 三 renderer；**重写** `handleResult`/错误路径为 `exit_code`-keyed 表（旧 `error (N): …` 分支删除，非复用）；worklist Reason 单元格人类化；backfill 错误特例（按稳定后端短语拦截 `attestation required`/`published URL is required`）。
- **空 reviewer 前置（G6 子集）**：reviewer 下拉为空时以 onboarding banner 取代签核区。
- 验证：在真实 pywebview 窗口跑一遍 crawl→process→packet→approve→backfill 全程，确认无 machine-speak、无非法按钮。

### Phase P1 — 异步/进度 + 视觉系统
**关闭 G1、G4、G5。**
- **异步（G1/G4）**：`create_and_crawl`→`_async`、`process`→`_async`；`pollUntilSettled`（6-shape 解包 + 顶层错误守卫 + `default:` fail-safe + 轮询 Map + init 单探测恢复 + 轮询上限）；JOB 进度模式 spinner（CSS-only + matchMedia reduced-motion 回退）；busy 锁（含同步调用防双击）。
- **交互态（G4）**：empty/loading/success 三态；3 变体成功横幅（按 result dict 实际字段，blocked/duplicate 只命名 `stopped_at`+notes）；in-DOM 确认条三件套（reject/supersede/backfill-attest，backfill 按 `attested` 布尔分支）。
- **视觉系统（G5）**：重写 `app.css`（~230 行 token + 组件）；6 色族徽章（含 `.badge--unknown`/`.lane--unknown` 必需回退、`currentColor` 边强制）；worklist 色带；状态横幅；冻结皮肤 + hash-chip；inert-link 拆分；honesty-callout；sticky 诚实声明 header。

### Phase P2 — onboarding + 打磨
**关闭 G6 全量、完成 G7 验证。**
- **SETUP 视图 + 就绪清单**：`computeReadiness()` + `applyGating()`（幂等软门控）；4 行清单 + 4 变体 gate banner（含 Phase 0 落地后的真绿/无 C-partial）；config.yaml handoff 卡（inert `<pre>` + 实时 `config_path`）；advisory 内联 base_url 校验；首跑自动打开 SETUP + 字段聚焦。
- **打磨**：成功/淡出动画时序、reduced-motion 全覆盖、最窄窗口下 sticky header 高度目检、WebKitGTK 字形渲染一次性目检（防 tofu）。
- **G7 收尾**：在真实窗口跑完整 onboarding（裸机→填 LLM→空 reviewer 提示→改 config.yaml→Re-check→全绿）+ 所有终态/不可逆确认路径。

---

## Success Criteria（可观察的成功标志）

1. **非技术操作者无需读状态机即可完成一次完整日常循环**（create→process→packet→approve→手动上架→backfill），全程不见任何 enum 值、reason 码或 `error (N): …`。
2. **窗口在长爬取/LLM 处理期间不冻结**：进度模式可见、elapsed 计时走动、可切回 Inbox，且 spinner 在任何 `job_status` 形态（含 idle/unknown/顶层 error）下都能正确终止——绝不无限转。
3. **操作者面对一个 hold（needs_human_review）时知道下一步**：grounding 看到「重新检查 vs 人工放行」、risk/dedup 看到「必须写理由」，且绝无「approve anyway」可点。
4. **被机器拦下的 job 永不静默消失**：blocked/duplicate 在 Inbox「被机器拦下」band 展开可见，其 JOB 视图只读、零按钮、有人类化「为什么」。
5. **首跑操作者立即看到 4 个前置的就绪状态**，缺失时被软门控 + 一行提示挡住、而非在流水线深处撞 exit 3；空 reviewer 白名单被显式解释（指向 config.yaml，非假装可在 GUI 修）。
6. **冻结的 review packet 在视觉上明确不可编辑**（靛蓝皮肤 + FROZEN ribbon + hash 徽章），操作者不尝试「edit-and-reprocess」。
7. **诚实声明（不自动发布 / 署名非验证 / 查重非全站）在每个视图常驻可见**，从未被滚走或被绿色「全部就绪」盖过。
8. **CLI 与 GUI 对同一 job 的同一动作给出一致的可达性**（无 GUI 删除的动作）。

---

## Resolve Before Planning（规划前需拍板）

> **状态：已全部拍板（2026-06-16）。** 规划无 open blocker，可直接进入 `/ce:plan`。

1. ~~是否在 Phase 0 先落地 `get_settings()` 返回 `allow_domains` 的单行只读添加？~~ **✅ 已决定：做。** Phase 0 纳入实现范围（只读、已转义、单行）。onboarding P3 因此成为真信号，删除 C-partial 双路径与「假绿色」风险。可与 P0 并行。
2. ~~轮询节奏 1500ms / 上限 ~90s 是否需先验证？~~ **✅ 已决定：降级为 P1 实测前置（不阻塞规划）。** 现行 1500ms / ~90s 作为默认值进规划；P1 第一项验证 = 真实 pywebview 窗口跑一次真实 `process`，实测耗时后校准这两个常数（确认 1500ms 不让 elapsed 滞后、~90s 不在合法慢调用时误触「taking longer than usual」）。

## Deferred to Planning（规划阶段处理）

1. **`get_packet().source_urls` 今为 `[]`（gui.py 硬编码）**，故 inert-source-link 视觉与 R2 attribution-visible 承诺当前无数据可显。视觉样式就绪并复用于 `asset_ref`；是否/何时让 GUI 喂 source_urls 是后端 feed 缺口，规划时决定是否纳入。
2. **`ingest_dir` 无 `_async` 孪生**：本地无网络故保持同步 + busy 锁。若实测大资料夹 ingest 显得冻结，`ingest_dir_async` 是后端 follow-up（已标记，非本期假设）。
3. **CLI parity gap P2（`crawl --input` URL-list 文件）**：GUI create 只取单 URL，走重复 NEW job 扇出。是否为批量 power-user 加「duplicate this job with a new id」或文件批量入口，post-MVP 决定。
4. **重启后仍真在处理且上一会话崩溃的 job**：因 PROCESSING 永不持久化，会报最后持久化的静止态而非「仍在跑」；操作者依赖 `.processing` marker / re-process 重试路径，而非 GUI 跨重启的实时 spinner。规划时决定是否需要更强的崩溃恢复 UI。
5. **Inbox 非实时**：刷新仅 on-entry + 手动（无 timer，YAGNI）。后台线程改了某 job 状态时，操作者坐在 Inbox 不会自动重新分 band，须点 [Refresh]。对单操作者可接受；规划时确认无需轻量定时刷新。
6. **`needs_revision` 不显示原始 `finish_reason` 值**（仅 `get_packet()` 暴露）：截断草稿若一直停在 needs_revision 未成 packet，操作者只见「可能不完整」交叉引用文案，看不到确切截断原因。MVP 诚实层可接受；规划时决定是否把 `finish_reason` 也加入 `process()`/`list_jobs` 返回。
7. **跨维度 tone 约定**：`lex.js` 的 `tone` 值交给视觉维度消费；规划时须确认 fail-closed 态（needs_human_review、crawled_warn）映射到 calm 琥珀（「已停好待审」）而非 alarming 红，否则会重新引入文案极力避免的「失败/error」感。
8. **WebKitGTK 字形 tofu 与最窄窗口 sticky header 高度**：受限子集字形（⚑ ✎）与两行 header 需在目标 Linux webview / 最小窗口尺寸做一次性目检（G7）；规划时把这两项列入视觉 QA 清单。