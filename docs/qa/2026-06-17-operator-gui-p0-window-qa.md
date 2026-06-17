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

## 已自动验证（无需你做）

- pytest 全套 492 passed（含 Phase 0 的 `get_settings` 4 例、lex/STATE_ACTIONS 完整性 13 例、既有 GUI 静态守卫：无 innerHTML / 严格 CSP / 无 onclick / settings IDs）
- mypy 两段式类型门：49 文件 0 error
- `node --check` app.js + lex.js 语法 OK
- CLI/GUI 对等：16 个操作者 Api 方法在 app.js 全部可达
