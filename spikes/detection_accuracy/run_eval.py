#!/usr/bin/env python3
"""Unit 1 spike harness — measure the REAL deterministic detectors against a
labeled set, and print a decision table (strategy x precision/recall x
recommended fail-closed threshold).

This is the runnable MEASUREMENT harness the Unit 1 plan asks for. It does NOT
make the grounding-strength decision (substring-only vs +NLI/MiniCheck): that
decision needs a real labeled corpus and, for NLI, a model. What this proves now
is the MECHANICS — the detectors load, run, and are scored end to end — and it
gives an honest baseline number on the small synthetic set so the harness can't
silently rot.

Run:
    ./.venv/bin/python spikes/detection_accuracy/run_eval.py
    ./.venv/bin/python spikes/detection_accuracy/run_eval.py --labeled <path>

It imports the production detectors directly (they are pure + deterministic; the
company LLM is NOT needed):
  * grounding  -> lcp.core.rules.grounding.verify_grounding (+ SubstringOverlapStrategy)
  * risk       -> lcp.core.rules.risk_rules.assess_risk
  * dedup      -> lcp.core.rules.dedup_rules.assess_dedup

The grounding eval runs the substring/overlap BASELINE today. The seam where a
+NLI strategy plugs in is marked GROUNDING_STRATEGIES below: drop an object that
satisfies the GroundingStrategy Protocol there and it is scored alongside the
baseline — no heavy ML dependency is added here.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

# --- import path: spike lives outside the package; add src/ ------------------
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from lcp.core.draft import Draft, FaqItem, SourceQuote  # noqa: E402
from lcp.core.rules.dedup_rules import (  # noqa: E402
    DedupIndex,
    DedupStatus,
    IndexEntry,
    assess_dedup,
)
from lcp.core.rules.grounding import (  # noqa: E402
    GroundingStrategy,
    SubstringOverlapStrategy,
    verify_grounding,
)
from lcp.core.rules.risk_rules import (  # noqa: E402
    RiskInput,
    RiskStatus,
    assess_risk,
)

DEFAULT_LABELED = Path(__file__).resolve().parent / "sample_labeled.jsonl"


# --- generic binary metrics --------------------------------------------------


@dataclass(frozen=True)
class Metrics:
    """Binary classification metrics for one detector x one positive class.

    "positive" = the unsafe outcome we want the fail-closed gate to catch
    (ungrounded / flagged-or-blocked / duplicate-or-uncertain). A false negative
    is the dangerous error (an unsafe item slipped through as safe)."""

    tp: int = 0
    fp: int = 0
    tn: int = 0
    fn: int = 0

    @property
    def total(self) -> int:
        return self.tp + self.fp + self.tn + self.fn

    @property
    def precision(self) -> float:
        d = self.tp + self.fp
        return self.tp / d if d else 1.0

    @property
    def recall(self) -> float:
        d = self.tp + self.fn
        return self.tp / d if d else 1.0

    @property
    def false_positive_rate(self) -> float:
        d = self.fp + self.tn
        return self.fp / d if d else 0.0

    @property
    def false_negative_rate(self) -> float:
        d = self.fn + self.tp
        return self.fn / d if d else 0.0

    @property
    def accuracy(self) -> float:
        return (self.tp + self.tn) / self.total if self.total else 1.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "tp": self.tp,
            "fp": self.fp,
            "tn": self.tn,
            "fn": self.fn,
            "total": self.total,
            "precision": round(self.precision, 4),
            "recall": round(self.recall, 4),
            "false_positive_rate": round(self.false_positive_rate, 4),
            "false_negative_rate": round(self.false_negative_rate, 4),
            "accuracy": round(self.accuracy, 4),
        }


def _score(pairs: list[tuple[bool, bool]]) -> Metrics:
    """pairs = list of (predicted_positive, actual_positive)."""
    tp = fp = tn = fn = 0
    for pred, actual in pairs:
        if pred and actual:
            tp += 1
        elif pred and not actual:
            fp += 1
        elif not pred and not actual:
            tn += 1
        else:
            fn += 1
    return Metrics(tp=tp, fp=fp, tn=tn, fn=fn)


# --- dataset loading ---------------------------------------------------------


def load_labeled(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as e:  # pragma: no cover - bad-data guard
            raise ValueError(f"{path}:{i}: invalid JSON: {e}") from e
    return rows


def _rows_of_kind(rows: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [r for r in rows if r.get("kind") == kind]


# --- grounding eval ----------------------------------------------------------

# Each entry maps a strategy NAME -> a GroundingStrategy (Protocol) factory.
# The substring/overlap BASELINES run by default (zero deps, offline). The +NLI
# LLM entailment judge is REAL and opt-in: pass --with-nli to append it (see
# build_grounding_strategies + lcp.adapters.llm.nli_grounding). verify_grounding()
# and the scoring below DO NOT change — every strategy is scored the same way.
GROUNDING_STRATEGIES: dict[str, Callable[[], GroundingStrategy]] = {
    "substring_overlap_0.6": lambda: SubstringOverlapStrategy(overlap_threshold=0.6),
    # Sensitivity probe — same baseline, stricter overlap (more fail-closed).
    "substring_overlap_0.8": lambda: SubstringOverlapStrategy(overlap_threshold=0.8),
}


def _row_to_draft(d: dict[str, Any]) -> Draft:
    return Draft(
        event_body=d.get("event_body", ""),
        quotes=[SourceQuote(text=q["text"]) for q in d.get("quotes", [])],
        faq=[FaqItem(question=f.get("question", ""), answer=f["answer"]) for f in d.get("faq", [])],
    )


def eval_grounding(
    rows: list[dict[str, Any]],
    strategies: dict[str, Callable[[], GroundingStrategy]] | None = None,
) -> dict[str, Metrics]:
    """For each strategy, predicted_positive = "needs_human_review" (i.e. the
    gate flagged it as not-grounded). actual_positive = label == 'ungrounded'.

    `strategies` defaults to the substring baselines; pass an augmented dict (e.g.
    including the opt-in +NLI LLM judge) to score them head-to-head."""
    samples = _rows_of_kind(rows, "grounding")
    strategies = strategies or GROUNDING_STRATEGIES
    out: dict[str, Metrics] = {}
    for name, factory in strategies.items():
        strat = factory()
        pairs: list[tuple[bool, bool]] = []
        for r in samples:
            draft = _row_to_draft(r["draft"])
            result = verify_grounding(draft, r["source_text"], strategy=strat)
            predicted_ungrounded = result.needs_human_review
            actual_ungrounded = r["label"] == "ungrounded"
            pairs.append((predicted_ungrounded, actual_ungrounded))
        out[name] = _score(pairs)
    return out


# --- risk eval ---------------------------------------------------------------


def eval_risk(rows: list[dict[str, Any]]) -> dict[str, Metrics]:
    """Two views of the risk gate against the default KeywordRiskDetector:

    * "flag_any"  — positive = NOT pass (needs_human_review OR blocked). This is
      the fail-closed catch rate: did the gate route anything non-clean to a
      human / block?
    * "block_only" — positive = blocked. Precision of the hard-stop tier (a FP
      here means a redline block on content that was only review-worthy).
    """
    samples = _rows_of_kind(rows, "risk")
    flag_pairs: list[tuple[bool, bool]] = []
    block_pairs: list[tuple[bool, bool]] = []
    for r in samples:
        content = RiskInput(
            title=r.get("title", ""),
            body=r.get("body", ""),
            has_source=r.get("has_source", True),
            contains_serious_claim=r.get("contains_serious_claim", False),
        )
        result = assess_risk(content)
        predicted_flag = result.status != RiskStatus.PASS
        actual_flag = r["label"] != "pass"
        flag_pairs.append((predicted_flag, actual_flag))

        predicted_block = result.status == RiskStatus.BLOCKED
        actual_block = r["label"] == "blocked"
        block_pairs.append((predicted_block, actual_block))
    return {"flag_any": _score(flag_pairs), "block_only": _score(block_pairs)}


# --- dedup eval --------------------------------------------------------------


def eval_dedup(rows: list[dict[str, Any]]) -> dict[str, Metrics]:
    """positive = NOT unique (duplicate OR uncertain) — i.e. the gate did not
    confidently clear it. actual_positive = label != 'unique'. This matches R36:
    a confident 'unique' is the only "let it pass" outcome."""
    samples = _rows_of_kind(rows, "dedup")
    pairs: list[tuple[bool, bool]] = []
    for r in samples:
        index = DedupIndex(
            entries=tuple(
                IndexEntry(job_id=e["job_id"], title=e.get("title", ""), body=e.get("body", ""))
                for e in r.get("index", [])
            ),
            site_index_available=r.get("site_index_available", True),
        )
        result = assess_dedup(title=r.get("title", ""), body=r.get("body", ""), index=index)
        predicted_not_unique = result.status != DedupStatus.UNIQUE
        actual_not_unique = r["label"] != "unique"
        pairs.append((predicted_not_unique, actual_not_unique))
    return {"not_unique": _score(pairs)}


# --- decision table ----------------------------------------------------------


def _recommend_threshold(m: Metrics) -> str:
    """Honest, rule-of-thumb fail-closed recommendation from THIS run's numbers.

    The principle (plan Unit 1): missing an unsafe item (FN) is far worse than an
    extra human review (FP), so when recall < 1.0 the recommendation is to fail
    closed — route the reason to a human rather than auto-pass. This is a
    heuristic over a TINY synthetic set, NOT the production threshold decision."""
    if m.total == 0:
        return "n/a (no samples)"
    if m.false_negative_rate == 0.0 and m.precision >= 0.9:
        return "auto-gate OK (FN=0, high precision) — still confirm on real corpus"
    if m.false_negative_rate == 0.0:
        return "auto-gate catches all; tighten precision to cut human load"
    return "FAIL-CLOSED: recall<1.0 -> route this reason to human, do not auto-pass"


def build_report(
    rows: list[dict[str, Any]],
    strategies: dict[str, Callable[[], GroundingStrategy]] | None = None,
) -> dict[str, Any]:
    """Full machine-readable metrics structure (also what the harness test asserts)."""
    grounding = {k: v.as_dict() for k, v in eval_grounding(rows, strategies).items()}
    risk = {k: v.as_dict() for k, v in eval_risk(rows).items()}
    dedup = {k: v.as_dict() for k, v in eval_dedup(rows).items()}
    return {
        "sample_count": len(rows),
        "counts_by_kind": dict(
            sorted({k: len(_rows_of_kind(rows, k)) for k in {r.get("kind") for r in rows}}.items())
        ),
        "grounding": grounding,
        "risk": risk,
        "dedup": dedup,
    }


def _fmt_row(detector: str, strategy: str, m: Metrics) -> str:
    return (
        f"  {detector:<10} {strategy:<24} "
        f"P={m.precision:5.2f}  R={m.recall:5.2f}  "
        f"FP={m.fp:>2}  FN={m.fn:>2}  "
        f"FPR={m.false_positive_rate:4.2f}  FNR={m.false_negative_rate:4.2f}   "
        f"-> {_recommend_threshold(m)}"
    )


def print_decision_table(
    rows: list[dict[str, Any]],
    strategies: dict[str, Callable[[], GroundingStrategy]] | None = None,
) -> dict[str, Any]:
    counts = defaultdict(int)
    for r in rows:
        counts[r.get("kind")] += 1

    print("=" * 100)
    print("Unit 1 spike — detection accuracy on SYNTHETIC labeled set")
    print(f"  file: {len(rows)} rows  |  by kind: {dict(sorted(counts.items()))}")
    print("  NOTE: synthetic data validates MECHANICS only. The real grounding-strength")
    print("        decision (substring-only vs +NLI/MiniCheck) awaits a real labeled corpus.")
    print("=" * 100)
    print("  detector   strategy                 metrics                                      recommendation")
    print("-" * 100)

    g = eval_grounding(rows, strategies)
    for name, m in g.items():
        print(_fmt_row("grounding", name, m))
    r = eval_risk(rows)
    for name, m in r.items():
        print(_fmt_row("risk", name, m))
    d = eval_dedup(rows)
    for name, m in d.items():
        print(_fmt_row("dedup", name, m))
    print("-" * 100)
    print("  positive class = the UNSAFE outcome the fail-closed gate must catch")
    print("    grounding: positive = needs_human_review (not grounded)")
    print("    risk.flag_any: positive = blocked or needs_human_review (not clean)")
    print("    risk.block_only: positive = blocked (hard-stop tier)")
    print("    dedup.not_unique: positive = duplicate or uncertain (R36: only confident unique passes)")
    print("  FN (false negative) = unsafe item wrongly cleared — the dangerous error.")
    print("=" * 100)

    return build_report(rows, strategies)


# --- opt-in +NLI strategy (uses the company LLM; needs config + network) ------


def build_grounding_strategies(
    *, with_nli: bool, config_path: str | None
) -> dict[str, Callable[[], GroundingStrategy]]:
    """The grounding strategies to score. Always the substring baselines; with
    ``with_nli`` also the LLM entailment judge (+NLI), constructed from config.

    The +NLI judge is built ONCE and reused across samples (one LlmClient). It
    makes a real network call per grounding claim, so it is opt-in only — the
    default harness run stays zero-dependency and offline."""
    strategies = dict(GROUNDING_STRATEGIES)
    if not with_nli:
        return strategies

    from lcp.adapters.llm.client import LlmClient
    from lcp.adapters.llm.nli_grounding import LlmGroundingStrategy
    from lcp.core.config import load_config

    cfg = load_config(config_path)
    if not cfg.llm.base_url:
        raise SystemExit(
            "error: --with-nli needs an LLM endpoint; set llm.base_url in the "
            "config (and the api_key in the OS keyring / LCP_LLM_API_KEY)."
        )
    client = LlmClient(
        cfg,
        ca_bundle=cfg.llm.ca_bundle,
        allow_http_hosts=cfg.llm.allow_http_hosts,
    )
    strategy = LlmGroundingStrategy(client=client)
    # Reuse the SAME strategy object (one client) across the lambda's calls.
    strategies[f"nli_llm[{cfg.llm.model or 'llm'}]"] = lambda: strategy
    return strategies


# --- entrypoint --------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--labeled",
        type=Path,
        default=DEFAULT_LABELED,
        help="path to a labeled .jsonl set (defaults to the bundled synthetic sample)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the machine-readable report as JSON instead of the table",
    )
    parser.add_argument(
        "--with-nli",
        action="store_true",
        help="also score the opt-in +NLI LLM entailment judge (needs an LLM "
        "endpoint configured; makes one network call per grounding claim)",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="config path for --with-nli (defaults to ./config.yaml if present)",
    )
    args = parser.parse_args(argv)

    if not args.labeled.exists():
        print(f"error: labeled set not found: {args.labeled}", file=sys.stderr)
        return 2

    rows = load_labeled(args.labeled)
    if not rows:
        print(f"error: labeled set is empty: {args.labeled}", file=sys.stderr)
        return 2

    config_path = args.config
    if args.with_nli and config_path is None:
        default_cfg = _REPO_ROOT / "config.yaml"
        config_path = str(default_cfg) if default_cfg.exists() else None
    strategies = build_grounding_strategies(
        with_nli=args.with_nli, config_path=config_path
    )

    if args.json:
        print(json.dumps(build_report(rows, strategies), ensure_ascii=False, indent=2))
    else:
        print_decision_table(rows, strategies)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
