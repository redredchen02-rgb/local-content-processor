/*
 * lex.js — the PURE-DATA UI layer (no logic): the machine->human translation
 * table (LEX) + the state->action map (STATE_ACTIONS). Consumed by app.js.
 *
 * WHY TWO PLAIN OBJECTS, JSON-SHAPED:
 *   The backend speaks closed enums (16 JobState incl. transient `processing`,
 *   3 ReviewReason, exit-code buckets). A non-technical operator must never see
 *   those raw tokens. These objects are the single source of the human strings
 *   and the legal-action sets.
 *   They are written as valid JSON (double-quoted, no trailing commas, no
 *   comments inside the literals) so `tests/test_lex_completeness.py` can EXTRACT
 *   and STRUCTURALLY validate them from Python — proving every enum has a real
 *   (non-empty, not-the-raw-token) entry, without a JS runtime. Keep them so.
 *
 * RENDER DISCIPLINE (R41): app.js consumes these via textContent only. These are
 * data, never markup. Do not add HTML here.
 *
 * exit-code note: only 2/3/4/5 are reachable across the Api bridge (exit 1 USAGE
 * is raised in cli.py only). Unknown enum / unknown code -> LEX.fallback.
 */
"use strict";

const LEX = {
  "state": {
    "new": {"title": "刚建立", "glyph": "○", "why": "工作刚建好，素材还没抓进来。", "next": "抓取网址或匯入资料夹。", "tone": "neutral"},
    "crawled": {"title": "素材已就绪", "glyph": "●", "why": "素材抓干净了，可以开始处理。", "next": "点「开始处理」。", "tone": "ready"},
    "crawled_warn": {"title": "素材已就绪·部分缺漏", "glyph": "!", "why": "抓到了，但有些图片或影片没抓全，仍可处理。", "next": "可直接「开始处理」；要补齐请另开新 job。", "tone": "caution"},
    "processing": {"title": "处理中", "glyph": "◐", "why": "正在跑风险、查重、改写、校验，这步会连模型、花点时间。", "next": "不用动作，等进度跑完（画面不会卡，可离开）。", "tone": "busy"},
    "process_failed": {"title": "处理没跑完·可重试", "glyph": "✕", "why": "处理中途出错，没留下半成品（可安全重跑）。", "next": "点「重试处理」。", "tone": "retry"},
    "crawl_failed": {"title": "没抓到内容·可重试", "glyph": "✕", "why": "整页都抓不到（网址错、被挡、或站点没回应）。", "next": "检查网址后「重新抓取」。", "tone": "retry"},
    "processed": {"title": "草稿完成·待冻结", "glyph": "●", "why": "草稿做好且通过检查，还没锁定成审阅包。", "next": "点「建立审阅包」锁定版本。", "tone": "ready"},
    "blocked": {"title": "风险封锁", "glyph": "✕", "why": "命中红线（如未成年、隐私、重大风险），不可发布。", "next": "不能放行。若确属误判，可经二次确认作废（红线覆写，留痕）后另开新工作重做。", "tone": "stop"},
    "duplicate": {"title": "重复", "glyph": "✕", "why": "与既有内容重复，不应再发。", "next": "查重仅代表本工具处理过、非全站。若确属误判，可作废后另开新工作重做。", "tone": "stop"},
    "needs_human_review": {"title": "需人工判断", "glyph": "⚑", "why": "某道关卡没把握，交给你判读（见原因标签）。这是人工确认，不是失败。", "next": "依原因处理（见下方）。", "tone": "review"},
    "needs_revision": {"title": "内容需补正", "glyph": "✎", "why": "缺了标题或内文等必填项（可修可重跑）。若模型被截断也会落这里，内容可能不完整、建议重跑。", "next": "补齐后「重新处理」，或「作废」。", "tone": "review"},
    "review_pending": {"title": "已冻结·待签核", "glyph": "●", "why": "审阅包已建立，草稿已冻结锁定，等你核可。", "next": "看完后核可／退件／作废（核可后仍须你手动上架）。", "tone": "frozen"},
    "approved": {"title": "已签核·待你手动上架", "glyph": "✓", "why": "已通过签核——这是机器能到的最远一步。机器不会自动发布。", "next": "你手动上架后，回来「回填网址」并勾确认。", "tone": "frozen"},
    "rejected": {"title": "已退件·终止", "glyph": "✕", "why": "审阅者退回，不采用。", "next": "终点，仅供检视。要重做请开新工作。", "tone": "done"},
    "superseded": {"title": "已作废", "glyph": "✕", "why": "此版作废，由新工作取代。", "next": "终点，仅供检视。请改看接手的新 job id。", "tone": "void"},
    "published_recorded": {"title": "已上架并登记", "glyph": "✓", "why": "你已手动上架、回填网址并具结确认，全程完成。", "next": "完成，仅供检视。", "tone": "done"}
  },
  "reason": {
    "risk": {"label": "风险待判读", "why": "偵测到可能风险（疑似诽谤、可识别个资、或缺出处授权）。「不确定就停」交给你。", "next": "二选一：人工放行（须写理由），或退件／作废。没有「强制通过」。", "note": "fail-closed 是刻意的，不提供绕过。"},
    "dedup": {"label": "疑似重复待确认", "why": "查重觉得「可能」重复但不确定，没直接判定，留给你看。", "next": "二选一：人工放行（须写理由），或作废。", "note": "查重仅基于本工具处理过的内容，不等于全站查过。"},
    "grounding": {"label": "可信度待确认", "why": "机器无法把叙述句对回出处句子，不敢替你掛保证。「叙述是否有出处依据」目前是你的责任。", "next": "两条路：已核对出处→「重新检查」（通过自动放行）；仍要放行→人工放行并写理由。", "note": "不暗示机器已保证 grounding。"}
  },
  "exit": {
    "2": {"title": "你填的内容要修", "why": "输入某个值不对：网址格式错、job id 不存在、或网址不在允许清单。", "next": "照「技术细节」修正那一项再送一次。", "framing": "你的输入，可自行修。"},
    "3": {"title": "还没设定好", "why": "缺前置：模型金鑰没填，或缺 ffmpeg 等本机工具。", "next": "模型类→开「设定」填好存档；其他→找装机的人。", "framing": "一次性技术设定。"},
    "4": {"title": "外部服务暂时不通", "why": "连外部失败：模型逾时／5xx，或网路出错。不是你的错。", "next": "稍等几分钟，按同一颗按钮重试（重跑安全）。", "framing": "外部问题，安全重试。"},
    "5": {"title": "程式出了状况", "why": "程式遇到没预期的错（我们的 bug，不是你的操作）。", "next": "把「技术细节」截图回报；通常重试或重开可继续。", "framing": "我们的 bug，非你的错。"}
  },
  "fallback": {
    "state": {"title": "未知状态", "glyph": "?", "why": "出现了这个版本还不认得的状态。", "next": "更新程式，或回报这个状态码。", "tone": "neutral"},
    "exit": {"title": "出了点状况", "why": "发生未预期的情况。", "next": "把「技术细节」截图回报；通常重试或重开可继续。", "framing": "系统面。"}
  },
  "honesty": {
    "dedup": "查重仅代表本工具处理过此内容、不代表站上不存在；请自行复查站点。",
    "backfill_attest": "你是在具结，不是证明，我们不会抓取或核对这个网址。"
  },
  "dashboard": {
    "title": "累积总览",
    "subtitle": "这条流水线到目前为止帮你处理了多少、拦下多少。",
    "empty_title": "还没有累积",
    "empty_body": "这些数字会随你处理 job 慢慢累积——建立第一个工作后再回来看。",
    "section_states": "目前各状态",
    "section_gates": "各关卡拦截率",
    "section_reasons": "人工处理原因",
    "section_intervals": "关卡间隔（含等待）",
    "section_daily": "每日产量",
    "intervals_caveat": "此为相邻关卡的时间差，包含等你处理的等待时间，不是机器计算耗时——仅供参考。",
    "col_reached": "到达",
    "col_intercepted": "拦下",
    "col_rate": "拦截率",
    "col_count": "次数",
    "col_avg": "平均(秒)",
    "col_max": "最长(秒)",
    "no_intercepts": "目前没有关卡拦截纪录。",
    "gate": {
      "RISK_GATE": "风险",
      "DEDUP_GATE": "查重",
      "LINT_GATE": "结构检查",
      "GROUNDING_GATE": "出处核对",
      "MEDIA_GATE": "媒体检查"
    }
  }
};

const STATE_ACTIONS = {
  "new": [{"label": "抓取网址 / 匯入资料夹", "method": "openCreate", "confirm": false}],
  "crawled": [{"label": "开始处理", "method": "process", "confirm": false}],
  "crawled_warn": [{"label": "仍开始处理", "method": "process", "confirm": false}],
  "processing": [],
  "process_failed": [{"label": "重试处理", "method": "process", "confirm": false}],
  "crawl_failed": [{"label": "重新抓取", "method": "openCreate", "confirm": false}],
  "processed": [{"label": "建立审阅包", "method": "make_review_packet", "confirm": true}],
  "blocked": [{"label": "误判？红线作废（需二次确认）", "method": "supersedeRedline", "confirm": true}],
  "duplicate": [{"label": "误判？作废重做", "method": "supersede", "confirm": true}],
  "needs_human_review": [{"label": "处理这个 hold", "method": "openHold", "confirm": false}, {"label": "退件", "method": "reject", "confirm": true}, {"label": "作废", "method": "supersede", "confirm": true}],
  "needs_revision": [{"label": "重新处理", "method": "process", "confirm": false}, {"label": "作废", "method": "supersede", "confirm": true}],
  "review_pending": [{"label": "核可", "method": "approve", "confirm": false}, {"label": "退件", "method": "reject", "confirm": true}, {"label": "作废", "method": "supersede", "confirm": true}],
  "approved": [{"label": "回填网址并具结", "method": "backfill", "confirm": true}, {"label": "作废", "method": "supersede", "confirm": true}],
  "rejected": [],
  "superseded": [],
  "published_recorded": []
};
