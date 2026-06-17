# Operator GUI — Phase 0.5 + P0 真窗口 QA 清单

> 这些步骤需要**真实桌面显示器**（pywebview 窗口），无法在 headless 环境跑。
> 自动化部分（pytest + mypy + JS 语法）已在 CI/本地绿；这里是只能由人在窗口里确认的 G7 部分。

启动窗口（装好 gui extra 后）：

```sh
./.venv/bin/pip install -e ".[crawl,media,llm,dedup,gui,dev]"
./.venv/bin/python -c "from lcp.gui import launch; launch()"
```

## Phase 0.5 — 平台烟雾切片（先做，验证平台假设）

- [ ] 窗口能开，无崩溃；`app.css` / `app.js` / `lex.js` 都加载（无 CSP 报错——开 devtools console 看）
- [ ] 顶栏中文 + 安全字形（`● ○ ! ✓ ✕ ✎ ⚑ ?`）不出现豆腐块（□）
- [ ] sticky header 不与内容重叠；基本布局成立
- [ ] 用 dry-run 走一遍最短闭环：建工作 → `process` 勾「安全预览」→ 建立审阅包 → 核可（需先在 config.yaml 设 reviewers）
- [ ] 若任何一项崩裂，记录下来——P0 设计可能需据此调整（这正是前置它的目的）

## P0 — 弃用关键验证（SC1–SC4 的真窗口部分）

- [ ] **SC1 无 machine-speak**：全程看不到 `new`/`needs_human_review`/`risk` 等原始枚举值，也看不到 `error (2): …`；看到的是中文标题 + 为什么 + 下一步
- [ ] **SC2（部分）**：注意 P0 仍是**同步**调用——真实 crawl/LLM 会**冻结窗口**（已知，G1 留到 P1）；故本轮只用 dry-run / 短路径验证，不要拿真实 LLM 跑
- [ ] **G2 状态门控**：
  - [ ] `review_pending` 工作只显示「核可 / 退件 / 作废」
  - [ ] `approved` 只显示「回填网址并具结 / 作废」
  - [ ] `blocked` / `duplicate` 工作**零按钮**、只读，有人话「为什么」
  - [ ] `needs_revision` 只显示「重新处理 / 作废」（**无退件**）
- [ ] **SC3 hold**：`needs_human_review`
  - [ ] reason=`grounding` → 见「重新检查 / 人工放行」两条路
  - [ ] reason=`risk`/`dedup` → 只见「人工放行（须写理由）」，**无** approve-anyway
  - [ ] dedup 永远显示「查重仅代表本工具处理过、非全站」诚实警语
- [ ] **SC4 Inbox**：blocked/duplicate 在「被机器拦下」band **展开可见**、不静默消失；「已结案」band 可折叠
- [ ] **G6 空 reviewer**：把 config.yaml 的 `publisher.reviewers` 清空 → 签核区被「还不能签核…去 config.yaml 加名字」banner 取代（不是灰按钮）；「就绪」pill 显示「⚠ 需设定」
- [ ] **导航**：顶栏「收件匣 / + 新工作 / 设定」切换正常；选择工作 = 打开（不再有共享 #job-id 文本框）；终态动作后回 Inbox 计数实时更新
- [ ] **错误人话**：故意填坏网址 / 不存在的 job → 看到「你填的内容要修」+ 可展开「技术细节」，不见 `error (N)`
- [ ] **回填特例**：approved 工作不勾「我确认…」点回填 → 「尚未完成：你没勾…工作仍停在已签核」（非成功、非原始错误）

## P1 — 异步/交互/视觉（G1/G4/G5 的真窗口部分）

- [ ] **G1 不再冻结**：用**真实**模型 endpoint 跑 `process`（不勾安全预览）→ 出现进度 spinner + elapsed `mm:ss` 走动 + 「视窗不会卡死」字样；此时能点「‹ 收件匣」切走、再「打开」该工作回到进度——spinner 仍在转
- [ ] **spinner 永不卡死**：处理中关掉模型/断网造成失败 → spinner 停、显示人话错误 + Retry；超长（>~90s）→ 显示「还在处理——比平常久」并停转
- [ ] **reduced-motion**：系统开「减少动态效果」→ spinner 变成 `◐◓◑◒` 字形循环（无旋转）
- [ ] **parked 不报成功**：让一个 job 被 `process` 判成 `needs_human_review`/`blocked` → 落地后**不是绿色成功横幅**，而是琥珀「已替你停下」+ 上方状态横幅是对应 tone（绝不绿）
- [ ] **in-DOM 确认条**：点退件/作废/回填/建立审阅包 → 就地展开红框确认条（**不是**系统弹窗）；只能同时展开一个；取消可收起；退件缺理由时「确定退回」拦下
- [ ] **回填特例**：approved 工作勾确认 + 填网址 → 成功；不勾 → 「尚未完成…仍停在已签核」
- [ ] **视觉**：徽章 6 色族正确（琥珀=需要你、红=死路、靛蓝=冻结）；`review_pending` 包卡有靛蓝皮肤 + FROZEN 角标 + hash chip；窄窗口（拖到很窄）worklist 行折成堆叠卡片

## P2 — onboarding（G6 的真窗口部分）

- [ ] **首跑自动开 SETUP**：清空 LLM 设定（或全新机器）启动 → 自动落在「设定」页、base_url 聚焦、就绪清单显示缺项
- [ ] **4 变体 gate banner 无假绿**：只设 base_url/key（allowlist/reviewers 空）→ banner 显示「还需一次性技术设定」+ 列出缺项 + config.yaml 交接卡（可整段复制的 `<pre>`）；**绝不**显示「全部就绪」绿条
- [ ] **真绿仅四项齐**：config.yaml 补上 allow_domains + reviewers → 点「重新检查」→ banner 变绿「全部就绪」，顶栏 pill 变「● 就绪」
- [ ] **软门控**：未就绪时「+ 新工作」的建立按钮被挡 + 一行提示（而非深处撞 exit 3）
- [ ] **advisory 校验**：base_url 输入框边打边给提示（如缺 `/v1`、http 非 loopback）；存档时仍以服务器为准

## 已自动验证（无需你做）

- pytest 全套 492 passed（含 Phase 0 的 `get_settings` 4 例、lex/STATE_ACTIONS 完整性 13 例、既有 GUI 静态守卫：无 innerHTML / 严格 CSP / 无 onclick / settings IDs）
- mypy 两段式类型门：49 文件 0 error
- `node --check` app.js + lex.js 语法 OK
- CLI/GUI 对等：16 个操作者 Api 方法在 app.js 全部可达
