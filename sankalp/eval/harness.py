"""
Stage 3 evaluation harness. Zero LLM calls — criteria come straight from
the corpus's hand-authored obligations; the compiler is bypassed entirely.

Runs core/verifiers/{constraint,receipt}.py over every record in
eval/corpus/records.jsonl and computes every metric from
eval/PRE_REGISTERED.md that is tagged *Stage: 3* (constraint-verifier-only)
or reachable with a deterministic proxy. Everything else is reported as
the literal string "NOT_YET_EXERCISED" — never a bare 0, which would read
as a measured failure of a mechanism that doesn't exist yet.

ON THE 1% / 2% / 5% FALSE-BLOCK SWEEP
---------------------------------------
PRE_REGISTERED.md's headline is "recall at a fixed 2% false-block rate,"
swept across 1/2/5% for the curve. That sweep presumes a continuous,
tunable decision signal to trade recall against false-block rate. The
constraint verifier built this stage is a deterministic, zero-noise
predicate evaluator: on a CLEAN record it is either right (because the
record genuinely satisfies every criterion — proven by
tests/unit/test_generator.py's TestLabelIntegrity) or it isn't, and there
is no threshold to slide that changes that. Sweeping a fake threshold over
a binary signal would produce three identical numbers dressed up as a
curve, which is worse than reporting one honest number.

So Stage 3 reports the single ACHIEVED (recall, false-block) operating
point, with its Wilson CI, and explicitly labels it as a proxy for the
Stage 6 headline (per PRE_REGISTERED.md's stage-gating for that metric).
The 1/2/5% sweep becomes meaningful once Stage 4's compiler and Stage 5's
semantic verifier introduce real, continuous-confidence error into the
pipeline — that is where recall/false-block actually trade off.
"""

from __future__ import annotations

import dataclasses
import json
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

from core.models.cart import Cart, CartItem, Merchant
from core.models.enums import CriterionOperator, CriterionSource, Verdict
from core.models.obligation import AcceptanceCriterion, DeliveryWindow, MerchantScope, Obligation
from core.verifiers.constraint import ConstraintCheckDetail, ConstraintVerifier, evaluate_constraint_checks
from core.verifiers.receipt import ReceiptVerifier
from eval.stats import RateEstimate, rate

CORPUS_DIR = Path(__file__).resolve().parent / "corpus"
NOT_YET_EXERCISED = "NOT_YET_EXERCISED"


# ── Corpus loading / model reconstruction ─────────────────────────────────

def load_records(path: Path | None = None) -> list[dict[str, Any]]:
    path = path or (CORPUS_DIR / "records.jsonl")
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def load_split(path: Path | None = None) -> dict[str, str]:
    path = path or (CORPUS_DIR / "split.json")
    return json.loads(path.read_text(encoding="utf-8"))["assignments"]


def record_to_models(record: dict[str, Any]) -> tuple[Obligation, Cart]:
    ob = record["obligation"]
    criteria = [
        AcceptanceCriterion(
            id=c["id"], field=c["field"], operator=CriterionOperator(c["operator"]),
            value=c["value"], source=CriterionSource(c["source"]), confidence=c["confidence"],
        )
        for c in ob["acceptance_criteria"]
    ]
    obligation = Obligation(
        raw_instruction=record["instruction_text"],
        user_id="corpus",
        acceptance_criteria=criteria,
        prohibited=ob["prohibited"],
        budget_ceiling=Decimal(ob["budget_ceiling"]) if ob["budget_ceiling"] is not None else None,
        merchant_scope=MerchantScope(
            merchant_ids=ob["merchant_scope"]["merchant_ids"],
            category=ob["merchant_scope"]["category"],
        ),
        delivery_window=(
            DeliveryWindow(latest_by=ob["delivery_window"]["latest_by"], tz=ob["delivery_window"]["tz"])
            if ob["delivery_window"] is not None else None
        ),
    )

    cd = record["cart"]
    cart = Cart(
        items=[
            CartItem(name=i["name"], quantity=i["quantity"], unit_price=Decimal(i["unit_price"]),
                      ingredients=i["ingredients"], category=i["category"])
            for i in cd["items"]
        ],
        merchant=Merchant(**cd["merchant"]),
        total=Decimal(cd["total"]),
        fulfilment_eta=cd["fulfilment_eta"],
    )
    return obligation, cart


# ── Per-record evaluation ──────────────────────────────────────────────────

@dataclasses.dataclass
class RecordResult:
    order_id:              str
    violation_class:         str
    subpopulation:             str | None   # e.g. "QUANTITY_MISMATCH:catchable"; None for CLEAN
    verifier_catchable:          bool
    abstain_expected:             bool
    self_report_deceptive:         bool
    language:                       str
    split:                           str
    detail:                           ConstraintCheckDetail
    composite_verdict:                 Verdict
    receipt_verdict:                     Verdict
    cart_total:                            Decimal
    latency_seconds:                       float
    caught:                                  bool | None   # None only for CLEAN
    mislabel:                                  bool | None   # True iff a catchable=False record was actually caught


_INGREDIENT_EXCLUDE_FIELD = "item.ingredients"


def _subpopulation_key(record: dict[str, Any]) -> str | None:
    vc = record["labels"]["violation_class"]
    if vc == "CLEAN":
        return None
    if vc == "QUANTITY_MISMATCH":
        return f"{vc}:{'catchable' if record['labels']['verifier_catchable'] else 'uncatchable'}"
    if vc == "CONSTRAINT_VIOLATION":
        if record["labels"]["abstain_expected"]:
            return f"{vc}:abstain"
        return f"{vc}:{'catchable' if record['labels']['verifier_catchable'] else 'uncatchable'}"
    return vc


def evaluate_record(record: dict[str, Any], split_assignment: str) -> RecordResult:
    obligation, cart = record_to_models(record)

    t0 = time.perf_counter()
    detail = evaluate_constraint_checks(obligation, cart)
    composite = ConstraintVerifier().verify(obligation, cart)
    receipt = ReceiptVerifier().verify(obligation, cart)
    latency = time.perf_counter() - t0

    labels = record["labels"]
    vc = labels["violation_class"]
    caught: bool | None = None
    mislabel: bool | None = None

    if vc == "CLEAN":
        pass

    elif vc == "QUANTITY_MISMATCH":
        if labels["verifier_catchable"]:
            cid = labels["violating_criterion_ids"][0]
            caught = detail.criterion_verdicts.get(cid) == Verdict.FAIL
        else:
            qty_ids = [c["id"] for c in record["obligation"]["acceptance_criteria"] if c["field"] == "quantity_sum"]
            actually_caught = any(detail.criterion_verdicts.get(cid) == Verdict.FAIL for cid in qty_ids)
            caught = False
            mislabel = actually_caught

    elif vc == "CONSTRAINT_VIOLATION":
        if labels["abstain_expected"]:
            excl_ids = [
                c["id"] for c in record["obligation"]["acceptance_criteria"]
                if c["field"] == _INGREDIENT_EXCLUDE_FIELD and c["operator"] == "excludes"
            ]
            caught = bool(excl_ids) and all(detail.criterion_verdicts.get(i) == Verdict.ABSTAIN for i in excl_ids)
        elif labels["verifier_catchable"]:
            cid = labels["violating_criterion_ids"][0]
            caught = detail.criterion_verdicts.get(cid) == Verdict.FAIL
        else:
            # Uncatchable by construction: operator=semantic criteria are never
            # evaluated by this verifier at all, so "caught" is structurally False.
            caught = False
            mislabel = False

    elif vc == "BUDGET_BREACH":
        caught = detail.budget_verdict == Verdict.FAIL
    elif vc == "WRONG_MERCHANT":
        caught = detail.merchant_scope_verdict == Verdict.FAIL
    elif vc == "TIMING_MISS":
        caught = detail.delivery_verdict == Verdict.FAIL
    elif vc == "TOTAL_MISDECLARED":
        caught = detail.total_arithmetic_verdict == Verdict.FAIL

    return RecordResult(
        order_id=record["order_id"], violation_class=vc, subpopulation=_subpopulation_key(record),
        verifier_catchable=labels["verifier_catchable"], abstain_expected=labels["abstain_expected"],
        self_report_deceptive=labels["self_report_deceptive"], language=record["language"],
        split=split_assignment, detail=detail, composite_verdict=composite.verdict,
        receipt_verdict=receipt.verdict, cart_total=cart.total,
        latency_seconds=latency, caught=caught, mislabel=mislabel,
    )


# ── Aggregate metrics ────────────────────────────────────────────────────

def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(len(s) - 1, max(0, round(p * (len(s) - 1))))
    return s[idx]


def _recall_over(results: list[RecordResult], predicate) -> RateEstimate:
    subset = [r for r in results if predicate(r)]
    caught = sum(1 for r in subset if r.caught)
    return rate(caught, len(subset))


def compute_metrics(results: list[RecordResult]) -> dict[str, Any]:
    violations = [r for r in results if r.violation_class != "CLEAN"]
    clean = [r for r in results if r.violation_class == "CLEAN"]

    def is_structurally_catchable(r: RecordResult) -> bool:
        """Everything except records the generator deliberately built to be
        invisible to this verifier (quantity_uncatchable_swap, semantic-only
        criteria)."""
        return (
            r.verifier_catchable or r.abstain_expected
            or r.violation_class in ("BUDGET_BREACH", "WRONG_MERCHANT", "TIMING_MISS", "TOTAL_MISDECLARED")
        )

    structurally_catchable = [r for r in violations if is_structurally_catchable(r)]

    # ── HEADLINE ────────────────────────────────────────────────────────────
    # The denominator is ALL violations — including the ~52 the corpus
    # deliberately built to be invisible to deterministic verification.
    # Removing known misses from a recall denominator turns "recall" into
    # "recall over the things we already knew we could catch", which is not
    # recall. The uncatchable records are misses and are counted as misses.
    headline_excl_total = _recall_over(violations, lambda r: r.violation_class != "TOTAL_MISDECLARED")
    headline_incl_total = _recall_over(violations, lambda r: True)

    # ── SECONDARY DIAGNOSTIC ────────────────────────────────────────────────
    # "Recall over violations within deterministic expressive power" — the
    # catchable-only figure. Useful for answering "is the deterministic layer
    # working correctly within its reach?", never useful as a headline,
    # because its denominator is chosen by the thing being measured.
    within_power_excl_total = _recall_over(
        structurally_catchable, lambda r: r.violation_class != "TOTAL_MISDECLARED"
    )
    within_power_incl_total = _recall_over(structurally_catchable, lambda r: True)

    per_subpopulation: dict[str, list[RecordResult]] = {}
    for r in violations:
        key = r.subpopulation or "UNKNOWN"   # violations always have a subpopulation; defensive only
        per_subpopulation.setdefault(key, []).append(r)
    per_subpopulation_recall = {
        key: rate(sum(1 for r in recs if r.caught), len(recs)).as_dict()
        for key, recs in per_subpopulation.items()
    }

    # False-block proxy (Stage 6 owns the real metric; see module docstring).
    false_blocked = [r for r in clean if r.composite_verdict == Verdict.FAIL]
    false_block_proxy = rate(len(false_blocked), len(clean))

    # Over-abstention: CLEAN records where ANY criterion abstained.
    over_abstained = [r for r in clean if any(v == Verdict.ABSTAIN for v in r.detail.criterion_verdicts.values())]
    over_abstention_rate = rate(len(over_abstained), len(clean))

    # ABSTAIN accuracy on records where it's expected.
    abstain_expected_records = [r for r in violations if r.abstain_expected]
    abstain_correct = sum(1 for r in abstain_expected_records if r.caught)
    abstain_accuracy = rate(abstain_correct, len(abstain_expected_records))

    # verifier_catchable mislabel audit.
    catchable_false_records = [r for r in violations if not r.verifier_catchable]
    mislabelled = [r for r in catchable_false_records if r.mislabel]

    # Misses table.
    misses = []
    for r in violations:
        if r.caught:
            continue
        expected = not r.verifier_catchable
        misses.append({
            "order_id": r.order_id, "violation_class": r.violation_class,
            "subpopulation": r.subpopulation, "type": "expected" if expected else "unexpected",
            "hypothesis": (
                "structurally uncatchable by design (see eval/generator.py's verifier_catchable tag)"
                if expected else
                "UNEXPECTED — catchable record was missed; investigate the verifier or the record"
            ),
        })

    latencies = [r.latency_seconds for r in results]

    def _split_summary(side: str) -> dict:
        subset_all = [r for r in results if r.split == side]
        subset_viol = [r for r in subset_all if r.violation_class != "CLEAN"]
        subset_within_power = [r for r in subset_viol if is_structurally_catchable(r)]
        return {
            "record_count": len(subset_all),
            # Headline denominators: all violations on this side.
            "recall_excl_total_misdeclared": _recall_over(
                subset_viol, lambda r: r.violation_class != "TOTAL_MISDECLARED"
            ).as_dict(),
            "recall_incl_total_misdeclared": rate(
                sum(1 for r in subset_viol if r.caught), len(subset_viol)
            ).as_dict(),
            "within_expressive_power_excl_total_misdeclared": _recall_over(
                subset_within_power, lambda r: r.violation_class != "TOTAL_MISDECLARED"
            ).as_dict(),
        }

    language_summary: dict[str, dict] = {}
    for lang in ("en", "hinglish"):
        subset_viol = [r for r in violations if r.language == lang]
        language_summary[lang] = {
            "record_count": sum(1 for r in results if r.language == lang),
            "recall_excl_total_misdeclared": _recall_over(
                subset_viol, lambda r: r.violation_class != "TOTAL_MISDECLARED"
            ).as_dict(),
            "within_expressive_power_excl_total_misdeclared": _recall_over(
                [r for r in subset_viol if is_structurally_catchable(r)],
                lambda r: r.violation_class != "TOTAL_MISDECLARED",
            ).as_dict(),
        }

    return {
        "record_count": len(results),
        "clean_count": len(clean),
        "violation_count": len(violations),
        "base_rate_clean": round(len(clean) / len(results), 4) if results else 0.0,

        "headline": {
            "note": (
                "Recall over ALL violations — structurally-uncatchable records are IN "
                "the denominator and counted as misses. Single achieved deterministic "
                "operating point; see eval/harness.py module docstring for why the "
                "1/2/5% false-block sweep is not yet meaningful."
            ),
            "denominator": "all violations",
            "recall_excl_total_misdeclared": headline_excl_total.as_dict(),
            "recall_incl_total_misdeclared": headline_incl_total.as_dict(),
            "false_block_rate_proxy": false_block_proxy.as_dict(),
        },

        "secondary_diagnostic_within_expressive_power": {
            "note": (
                "Recall over violations within deterministic expressive power — "
                "denominator EXCLUDES the structurally-uncatchable records. This is a "
                "diagnostic answering 'is the deterministic layer correct within its "
                "reach?', NOT a headline: its denominator is chosen by the component "
                "being measured. Always read it next to the headline figure above."
            ),
            "denominator": "violations within deterministic expressive power",
            "recall_excl_total_misdeclared": within_power_excl_total.as_dict(),
            "recall_incl_total_misdeclared": within_power_incl_total.as_dict(),
        },

        "per_subpopulation_recall": per_subpopulation_recall,

        "abstain_accuracy": {
            "abstain_expected_correct": abstain_accuracy.as_dict(),
            "over_abstention_on_clean": over_abstention_rate.as_dict(),
        },

        "verifier_catchable_audit": {
            "catchable_false_count": len(catchable_false_records),
            "mislabelled_count": len(mislabelled),
            "mislabelled_order_ids": [r.order_id for r in mislabelled],
        },

        "false_block": {
            "note": "Stage-3 proxy only (FAIL-rate on CLEAN records) — real metric is Stage 6.",
            "count": len(false_blocked),
            "rate": false_block_proxy.as_dict(),
            "rupee_cost": str(sum((r.cart_total for r in false_blocked), Decimal("0"))),
        },

        "latency_seconds": {
            "p50": round(_percentile(latencies, 0.50), 6),
            "p95": round(_percentile(latencies, 0.95), 6),
        },

        "split": {"train": _split_summary("train"), "holdout": _split_summary("holdout")},
        "language": language_summary,

        "misses": misses,
        "misses_summary": {
            "expected": sum(1 for m in misses if m["type"] == "expected"),
            "unexpected": sum(1 for m in misses if m["type"] == "unexpected"),
        },

        "not_yet_exercised": [
            "recall_at_2pct_false_block (Stage 6 — requires settlement instruction)",
            "clarification_precision (Stage 6)",
            "deceptive_self_report_catch_rate (Stage 5 — requires semantic verifier + floor enforcement)",
            "band_distribution (Stage 6 — requires exposure scorer)",
            "cost_per_clearing (Stage 4/6 — requires compiler / LLM calls; $0 this stage by construction)",
            "compiler_metrics (Stage 4)",
        ],
    }


# ── Top-level run ─────────────────────────────────────────────────────────

def run_harness(records: list[dict[str, Any]] | None = None, split: dict[str, str] | None = None) -> dict[str, Any]:
    records = records if records is not None else load_records()
    split = split if split is not None else load_split()

    results = [evaluate_record(record, split[record["order_id"]]) for record in records]
    return compute_metrics(results)


def write_results(out_dir: Path | None = None) -> dict[str, Any]:
    out_dir = out_dir or (Path(__file__).resolve().parent / "results")
    out_dir.mkdir(parents=True, exist_ok=True)

    metrics = run_harness()
    baselines = _baselines_summary()

    payload = {"stage": 3, "constraint_and_receipt_verifiers": metrics, "baselines": baselines}
    (out_dir / "stage3_results.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True), encoding="utf-8"
    )
    (out_dir / "stage3_results.md").write_text(_render_markdown(metrics, baselines), encoding="utf-8")
    return payload


def _baselines_summary() -> dict[str, Any]:
    from eval.baselines import evaluate_baselines
    return evaluate_baselines(load_records())


def _fmt(v: dict) -> str:
    return f"{v['rate']:.1%} [{v['ci_low']:.1%}, {v['ci_high']:.1%}] (n={v['n']})"


def _render_markdown(metrics: dict[str, Any], baselines: dict[str, Any]) -> str:
    h = metrics["headline"]
    d = metrics["secondary_diagnostic_within_expressive_power"]
    lines = [
        "# SANKALP — Stage 3 Results (deterministic verifiers, zero LLM calls)",
        "",
        f"Corpus: {metrics['record_count']} records "
        f"({metrics['clean_count']} CLEAN, {metrics['violation_count']} violations, "
        f"{metrics['base_rate_clean']:.1%} base rate).",
        "",
        "## Headline — recall over ALL violations",
        "",
        h["note"],
        "",
        f"- **Recall (excl. TOTAL_MISDECLARED): {_fmt(h['recall_excl_total_misdeclared'])}** ← headline",
        f"- Recall (incl. TOTAL_MISDECLARED): {_fmt(h['recall_incl_total_misdeclared'])}",
        f"- False-block proxy: {_fmt(h['false_block_rate_proxy'])}",
        "",
        "## Secondary diagnostic — within deterministic expressive power",
        "",
        d["note"],
        "",
        f"- Recall (excl. TOTAL_MISDECLARED): {_fmt(d['recall_excl_total_misdeclared'])}",
        f"- Recall (incl. TOTAL_MISDECLARED): {_fmt(d['recall_incl_total_misdeclared'])}",
        "",
        "## Baselines",
        "",
        f"- block-nothing: {baselines['block_nothing']['recall']:.1%} recall, "
        f"{baselines['block_nothing']['false_block_rate']:.1%} false-block",
        f"- block-everything: {baselines['block_everything']['recall']:.1%} recall, "
        f"{baselines['block_everything']['false_block_rate']:.1%} false-block",
        f"- SANKALP constraint+receipt: {h['recall_excl_total_misdeclared']['rate']:.1%} recall, "
        f"{h['false_block_rate_proxy']['rate']:.1%} false-block",
        "",
        "## Per-subpopulation recall",
        "",
        "| Subpopulation | Recall | 95% CI | n |",
        "|---|---|---|---|",
    ]
    for key, v in sorted(metrics["per_subpopulation_recall"].items()):
        lines.append(f"| {key} | {v['rate']:.1%} | [{v['ci_low']:.1%}, {v['ci_high']:.1%}] | {v['n']} |")

    a = metrics["abstain_accuracy"]
    lines += [
        "",
        "## ABSTAIN accuracy",
        "",
        f"- Correct on abstain_expected records: {a['abstain_expected_correct']['rate']:.1%} "
        f"[{a['abstain_expected_correct']['ci_low']:.1%}, {a['abstain_expected_correct']['ci_high']:.1%}] "
        f"(n={a['abstain_expected_correct']['n']})",
        f"- Over-abstention on CLEAN records: {a['over_abstention_on_clean']['rate']:.1%} "
        f"[{a['over_abstention_on_clean']['ci_low']:.1%}, {a['over_abstention_on_clean']['ci_high']:.1%}] "
        f"(n={a['over_abstention_on_clean']['n']})",
        "",
        "## verifier_catchable audit",
        "",
        f"- catchable=False records checked: {metrics['verifier_catchable_audit']['catchable_false_count']}",
        f"- mislabelled (actually caught): {metrics['verifier_catchable_audit']['mislabelled_count']}",
        "",
        "## Misses",
        "",
        f"- expected (uncatchable by design): {metrics['misses_summary']['expected']}",
        f"- unexpected (catchable but missed): {metrics['misses_summary']['unexpected']}",
        "",
        "## Split (all denominators are all-violations, per the headline convention)",
        "",
        f"- train: {metrics['split']['train']['record_count']} records, "
        f"recall (excl. TOTAL_MISDECLARED) "
        f"{_fmt(metrics['split']['train']['recall_excl_total_misdeclared'])}",
        f"- holdout: {metrics['split']['holdout']['record_count']} records, "
        f"recall (excl. TOTAL_MISDECLARED) "
        f"{_fmt(metrics['split']['holdout']['recall_excl_total_misdeclared'])} "
        f"— low resolution, aggregate only, draw no per-class conclusions here",
        "",
        "## Language",
        "",
        f"- en: recall (excl. TOTAL_MISDECLARED) {_fmt(metrics['language']['en']['recall_excl_total_misdeclared'])}",
        f"- hinglish: recall (excl. TOTAL_MISDECLARED) {_fmt(metrics['language']['hinglish']['recall_excl_total_misdeclared'])}",
        "",
        "## Latency",
        "",
        f"- p50: {metrics['latency_seconds']['p50']*1000:.3f} ms",
        f"- p95: {metrics['latency_seconds']['p95']*1000:.3f} ms",
        "",
    ]
    return "\n".join(lines)
