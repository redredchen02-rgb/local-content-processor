---
date: 2026-06-17
topic: accumulation-dashboard
---

# lcp 累积与洞察:Dashboard + 输入复用

## Problem Frame

操作者希望 `lcp` 不再「每次打开都像开一个全新的服务」,而是能延续过去:打开就看到累积的处理纪录,能从中做资料洞察、找后续优化方向,并且把绑定/输入过的内容存下来避免重复操作。

关键事实:底层持久化**已经存在**。每个 job 已写入 `data/lcp.db`(SQLite,WAL)与 `data/jobs/audit.jsonl`(append-only,逐阶段记录 start/end/hold/error),关闭后重开纪录都还在。因此本需求的缺口不在「资料库」或「架构」,而在**呈现层**(没有累积总览/洞察视图)与**输入复用**(每次重填来源与设定)。

驱动力澄清:操作者想要「网页化」主要是觉得网页更好改、更现代(DX 偏好),**不是**远程访问、多设备或多人协作。因此不需要把桌面 app 改成对外的 web server。

## Requirements

**Dashboard 累积总览**
- R1. 新增一个 Dashboard 视图,作为打开后的预设落地页(或与 Inbox 并列的首页入口),让操作者第一眼就感受到「累积」而非「空白新服务」。
- R2. Dashboard 聚合并呈现产能指标:累计处理 job 数、各状态分布、各闸门(风险/查重/lint+grounding)拦截率、人工审查/处理耗时趋势。资料来源为既有的 `lcp.db` jobs 表与 `audit.jsonl`,不新增采集逻辑。
- R3. 呈现时间趋势(例如按日/周的产量与拦截率变化),让操作者能看出走势,而非只有当下快照。
- R4. Dashboard 上提供「后续优化方向」线索:例如高频拦截原因、重复来源、耗时最长的阶段等,帮助操作者判断该调整哪条规则或哪类来源。

**输入 / 来源复用**
- R5. 新增 SQLite 持久化的「已存来源/输入」表,让操作者把绑定/输入过的内容存下来,下次直接复用而不必重填。
- R6. 在既有 Setup / 新建 job 的入口提供「从已存来源选取」的复用路径(选取已存项 → 直接带入,无需重新键入)。
- R7. 复用表与既有 PII-free 设计原则一致:避免在索引层存放高风险明文(沿用现有 hash/枚举码做法),具体存什么栏位于规划阶段界定。

**架构与安全约束(保留,不回退)**
- R8. 维持现有 pywebview + 127.0.0.1 回环外壳;不改成对外可访问的 web server。
- R9. 维持现有 R41 安全加固:严格 CSP、bridge 净化、textContent-only 渲染、inert 来源连结。新视图的所有跨 bridge 资料须沿用既有 `escape_html` / 净化路径。
- R10. 新功能复用现有 pipeline / storage 适配器,不引入新框架、build step、CDN 或外部前端依赖。

## Success Criteria

- 操作者关闭并重开 app 后,第一眼即看到过去的累积纪录与产能趋势,而非空白 Inbox 或像新服务。
- 操作者能从 Dashboard 指出至少一个具体的优化方向(例如「某来源重复率高」「某阶段最耗时」)。
- 操作者可把一次输入的来源存下,在后续 session 直接复用,无需重新键入。
- 无任何安全回退:CSP、回环绑定、bridge 净化全部维持原样。

## Scope Boundaries

- 不做:把桌面 app 改成浏览器可访问的 web server(无远程/多设备/多人需求,且会使既有安全加固失效需重做)。
- 不做:更换前端框架、引入 build step 或 CDN。
- 不做:新增资料采集管线;Dashboard 只聚合既有 `lcp.db` 与 `audit.jsonl`。
- 不做:为复用表加密;沿用现有「OS 全碟加密 + 0600 权限 + PII-free 索引」模型。

## Key Decisions

- 保留 pywebview 外壳而非改 web server:pywebview 本就在渲染同一套 HTML/CSS/JS,改 web server 仅换来「可开 DevTools / 更现代」,却要重做整套 R41 安全加固(回环、CSP、SSRF、bridge 净化),carrying cost 不成比例。驱动力是 DX 偏好而非远程访问,故不值得。
- 持久化层不动:跨 session 的纪录已由 SQLite + audit.jsonl 提供;缺口是呈现层与输入复用,皆为加法功能。

## Dependencies / Assumptions

- 假设 `audit.jsonl` 已逐阶段记录足够推导产能/拦截/耗时的事件(start/end/hold/error + 时间戳)。规划阶段需验证现有事件粒度是否足以支撑 R2–R4 全部指标。
- 假设「输入/绑定的内容」主要指来源(URL/本地路径)与重复性设定,而非每次都变的一次性参数。

## Outstanding Questions

### Resolve Before Planning
- (无 — 产品决策已厘清,可进入规划)

### Deferred to Planning
- [Affects R2-R4][Technical] 现有 `audit.jsonl` 事件粒度与栏位,是否足以推导拦截率与各阶段耗时趋势?不足则需补记录。
- [Affects R1][Design] Dashboard 应作为「预设落地页」还是「与 Inbox 并列的第四个视图」?取决于既有三视图导航与状态门控的整合方式。
- [Affects R5-R7][Technical] 「已存来源/输入」表的 schema 与 PII 边界:存原文还是 hash + 标签?如何与既有 job 建立关联?
- [Affects R5][Needs research] 现有新建 job / Setup 入口的输入流程,确认最自然的「复用」接入点。

## Next Steps
→ `/ce:plan` 进行实作规划
