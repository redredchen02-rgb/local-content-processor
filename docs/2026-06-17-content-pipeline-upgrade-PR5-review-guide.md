> **【更新 2026-06-17】本指南中关于「去水印 / Batch 2 / Units 6–9」的章节已被 CUT 取代** —— 该能力已从程式码移除（见 `docs/plans/2026-06-17-003-refactor-cut-dewatermark-pipeline-plan.md`）。其余（Batch 1 文案/封面、Batch 3 导入）仍有效。

# PR #5 审阅指南 — Content Pipeline Upgrade

> 一句话:四项 SOP 能力(加水印 / 封面校验 / 栏目模板+AI文案 / 去水印)在**不离开现有合规框**的前提下加厚;去水印**默认锁死 + 引擎默认缺席**,合并后无任何不安全行为会上线。

- **分支:** `feat/content-pipeline-upgrade` → `main`
- **规模:** 60 文件,~+5000 行(过半是测试)
- **测试:** 630 通过,mypy 整包干净(60 files)
- **来源:** `docs/plans/2026-06-17-002-feat-content-pipeline-upgrade-plan.md`

---

## 1. 改了什么(按批次)

### 批次 1 — 文案 + 封面(低风险、日常省时)
| Unit | 文件 | 作用 |
|---|---|---|
| U1 | `adapters/media/watermark.py`, `core/config.py:WatermarkConfig` | 加官方水印基元(logo/text,正文图+封面共用);RGBA→RGB,dry-run 不写 |
| U2 | `core/rules/asset_rules.py`, `adapters/media/cover_checks.py` | 封面安全区/边框/头重/拥挤 **建议性**检查(绝不 needs_revision)+ 安全区预览图 |
| U3 | `core/rules/template_lint.py`, `adapters/llm/templates.py`, `assembler.py` | 栏目提示词模板:受检对象 + `str.format_map` 白名单渲染进 **USER/DEVELOPER 子块,永不进 SYSTEM** |
| U4 | `adapters/llm/copywriter.py`, `grounding.py`, `review_packet.py`, `draft.py` | AI 图说/FAQ/小标题;**纳入 grounding + 冻结哈希**(冻结后偷改被 approve() 抓到;空值向后兼容) |
| U5 | `cli.py`, `gui.py`, `web/app.js`, `web/lex.js` | process-time 输入贯通:`--watermark/--template/--ai-copy` + GUI 选择器/预览(textContent,CSP 不破) |

### 批次 2 — 去水印(默认锁死,安全可插拔)
| Unit | 文件 | 作用 |
|---|---|---|
| U6 | `spikes/dewatermark/` | go/no-go 测量 harness(PSNR/残留/可发布率/延迟)+ 样本脚手架 + `make_mask.py` |
| U7 | `adapters/publisher/dewatermark.py` | **权责分离具结**:提交人 ≠ 独立复核者(归一化比对)+ 授权依据(仅哈希入 audit)+ DEWATERMARK_DISCLAIMER + 默认锁死 |
| U8 | `adapters/media/dewatermark_runner.py`, `mask.py`, `processor/dewatermark_gate.py` | 隔离子进程引擎(scrubbed env,重依赖不进主 venv,缺引擎→DependencyError)+ 仅具结、normalize 前、EXIF strip、失败→needs_revision、溯源 `AssetRef.watermark_removed` |
| U9 | `gui.py`, `cli.py`, `web/app.js` | 具结流程 GUI/CLI(`--dewatermark` + `dewatermark-request/-attest`)+ 提高 inpaint 轮询上限 |

### 批次 3 — 导入
| U10 | `adapters/crawler/ingest.py` | 混合素材包完整性报告(不支持/子目录→skipped、空媒体→FAILED、无静默丢弃);本地导入范围不变 |

---

## 2. 合规红线(全部保持)

- **零自动上架**:所有新动作只产待审包(R26)。
- **R16 受限改写未松绑**:AI 只生成结构件;模板渲染在 SYSTEM 之外。
- **R2 去水印**:这是对 R2 的**有界修订**——仅自有/授权 + 独立复核 + 授权依据,诚实披露"具结非证明"。
- **PII-free audit**:授权依据只存 sha256;去水印溯源只存 bool+hash。
- **R41 渲染纪律**:bridge 输出全 escape,app.js 仅 textContent,无 innerHTML。

---

## 3. 审阅结论(两个独立 reviewer + 全部修复)

**安全 review:无 P0/P1。** 四大高风险面(prompt 注入、子进程隔离、双人具结、渲染逃逸)通过。
**正确性 review:1 个 P0(已修+回归测试)。**

| 等级 | 问题 | 修复 commit |
|---|---|---|
| P0 | 封面在真实管线从没被打水印(`make_cover` 漏传 `watermark=`,单测掩盖) | `dda03ae` + 端到端回归测试 |
| P1 | `apply_copy_to_draft` 当 refs<captions 时静默丢图说 | `dda03ae` 改以 captions 驱动 |
| P2 | 空媒体白占 max_assets 名额 | `dda03ae` |
| P2安全 | 双人具结精确比对,`alice`/`Alice` 可绕 | `dda03ae` NFKC+casefold 归一化 |
| P2安全 | evidence 文件先写后 chmod | `dda03ae` os.open 原子 0600 |

---

## 4. ⚠️ 合并后仍待运营者决定(不阻塞合并)

去水印的 **BUILD/CUT** 是运营者的决定,需在**自有/授权样本 + 本机**上跑:
```bash
./.venv/bin/python spikes/dewatermark/run_eval.py --samples spikes/dewatermark/samples
```
看每桶可发布率 vs 验收线、残留、墙钟延迟 + GO/NO-GO,再决定装哪个引擎(MI-GAN-ONNX / static-ghost)还是砍掉 Batch 2。**没装引擎 = DependencyError;没具结 = 锁死**,所以合并是安全的。

---

## 5. Merge 前 checklist

- [x] 630 测试通过,mypy 整包干净
- [x] 安全 + 正确性 review 完成,P0/P1/P2 全部修复
- [x] 合规红线核对(零自动上架 / R16 / R2 修订诚实披露 / PII-free / R41)
- [x] 去水印默认锁死 + 引擎默认缺席(合并不引入风险)
- [ ] 人工抽审:`template_lint.py` 拒绝规则、`dewatermark.py` 双人逻辑、`media_checker` 水印顺序
- [ ] (合并后,运营者)跑去水印 spike 决定 BUILD/CUT
- [ ] (可选)合并后用真实 LLM 端点验证模板渲染 + AI 文案 grounding

---

## 6. 已知遗留(低风险,非阻塞)

- 模板/ai_copy 的选择**未单独入 audit**(但 AI 内容已冻进 `body_sha256`,可溯不可标)。
- 启用 logo 水印需**同时**配 body+cover 两个 logo 资产,否则媒体闸门 fail-loud(刻意,不静默)。
- 去水印 gate 幂等性依赖引擎本身幂等(同一图二次 inpaint)。
