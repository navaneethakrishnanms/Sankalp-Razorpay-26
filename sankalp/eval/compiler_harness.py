"""
Stage 4 evaluation harness — the obligation compiler.

TRAIN ONLY. The holdout seeds are not touched at this stage; touching them
while iterating on a prompt is how a holdout stops being a holdout.

WHAT IS MEASURED, AND AGAINST WHAT
------------------------------------
Ground truth is the corpus's hand-authored `obligation` block — the criteria a
human derived from `instruction_text` before any model existed, with the
traceability assertion in eval/generator.py guaranteeing every `stated`
criterion is literally recoverable from the instruction. The compiler never
saw those criteria.

Compilation is done ONCE PER SEED, not once per record: the instruction is a
property of the seed, and 45 seeds expand to 1,359 records. Compiling per
record would multiply cost ~30x and measure the same 45 instructions
repeatedly, making the CIs meaningless (the samples would not be independent).

THE DELTA IS THE INTERESTING NUMBER
-------------------------------------
Extraction accuracy answers "did the compiler produce the criteria a human
would have?". The delta answers "what does using the compiler instead of a
human actually cost at the point of decision?" — recall and false-block from
the Stage 3 verifiers, run twice over the same train records: once with gold
obligations, once with compiled ones. A compiler can score well on extraction
and still be expensive here (e.g. by mislabelling `stated` as `inferred`,
which leaves the criterion present but unenforceable).

CAUGHT, DEFINED ID-INDEPENDENTLY
----------------------------------
The Stage 3 harness decides "caught" by looking up gold criterion IDs. Compiled
criteria have different IDs, so that definition cannot compare the two. Here,
"caught" means the composite constraint verdict is FAIL (or ABSTAIN where the
record's ground truth says ABSTAIN is correct). Both sides of the delta use
this same definition, so the comparison is like-for-like.
"""

from __future__ import annotations

import dataclasses
import json
import statistics
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from core.llm.client import USD_TO_INR, LLMClient, default_client
from core.models.enums import Verdict
from core.models.obligation import Obligation
from core.obligation.compiler import CompilationResult, compile_obligation
from core.verifiers.constraint import evaluate_constraint_checks
from eval.harness import load_records, load_split, record_to_models
from eval.stats import rate

RESULTS_DIR = Path(__file__).resolve().parent / "results"
REFERENCE_DATE = datetime(2026, 8, 28, tzinfo=timezone.utc)


# ── Criterion matching ────────────────────────────────────────────────────

def _criterion_signature(field: str, operator: str, value: Any) -> tuple[str, str, str]:
    """
    Identity of a criterion for matching purposes: (field, operator, value).

    Value is normalised to a string so that gold's `4` and a compiled `4`
    match, and gold's `"beef"` matches a compiled `"Beef"`. Deliberately does
    NOT include `source` — source is scored separately, on the pairs that
    matched, so that a source mislabel shows up as a source error rather than
    silently becoming a "missed" criterion and double-counting.

    SEMANTIC CRITERIA ARE MATCHED ON (field, operator) ONLY
    --------------------------------------------------------
    A `semantic` criterion's value is a hand-authored label in the corpus
    ("not_too_spicy"), not a checkable value. The compiler cannot reproduce
    that string without authoring a value it never read in the user's
    instruction — the exact thing core/guards/output_validator.py forbids. It
    can only quote what the user actually said ("nothing too spicy").

    Comparing those two strings would score every semantic criterion as
    simultaneously missed AND invented, penalising the compiler twice for
    obeying the rule that keeps it safe. So the value is collapsed for
    semantic criteria and the match is on field+operator. This is a real
    limitation of the ground truth, not a leniency knob: it is stated in the
    results output so a reader knows semantic extraction is measured more
    coarsely than the rest.
    """
    normalised_op = operator.strip().lower()
    if normalised_op == "semantic":
        return (field.strip().lower(), normalised_op, "<semantic:value-not-compared>")
    return (field.strip().lower(), normalised_op, str(value).strip().lower())


def _gold_signatures(record: dict[str, Any]) -> dict[tuple[str, str, str], dict]:
    out: dict[tuple[str, str, str], dict] = {}
    for c in record["obligation"]["acceptance_criteria"]:
        out[_criterion_signature(c["field"], c["operator"], c["value"])] = c
    return out


def _compiled_signatures(obligation: Obligation) -> dict[tuple[str, str, str], Any]:
    out: dict[tuple[str, str, str], Any] = {}
    for c in obligation.acceptance_criteria:
        out[_criterion_signature(c.field, c.operator.value, c.value)] = c
    return out


@dataclasses.dataclass
class SeedCompilation:
    seed_id:          str
    language:          str
    instruction:        str
    gold_record:         dict[str, Any]
    result:               CompilationResult
    captured:              int
    invented:               int
    missed:                  int
    source_confusion:         dict[str, int]
    latency_seconds:           float
    cost_inr:                   Decimal
    gold_ambiguous:              bool
    predicted_ambiguous:          bool


def evaluate_seed(record: dict[str, Any], result: CompilationResult, *,
                   latency: float) -> SeedCompilation:
    gold = _gold_signatures(record)
    compiled = _compiled_signatures(result.obligation)

    matched = set(gold) & set(compiled)
    captured = len(matched)
    invented = len(set(compiled) - set(gold))
    missed = len(set(gold) - set(compiled))

    # Source labelling, scored only on criteria that matched — an unmatched
    # criterion has no gold source to compare against.
    confusion: dict[str, int] = {
        "correct": 0,
        "stated_labelled_as_inferred": 0,
        "inferred_labelled_as_stated": 0,
        "other_mismatch": 0,
    }
    for signature in matched:
        gold_source = gold[signature]["source"]
        compiled_source = compiled[signature].source.value
        if gold_source == compiled_source:
            confusion["correct"] += 1
        elif gold_source == "stated" and compiled_source == "inferred":
            confusion["stated_labelled_as_inferred"] += 1
        elif gold_source == "inferred" and compiled_source == "stated":
            confusion["inferred_labelled_as_stated"] += 1
        else:
            confusion["other_mismatch"] += 1

    return SeedCompilation(
        seed_id=record["generation"]["seed_id"],
        language=record["language"],
        instruction=record["instruction_text"],
        gold_record=record,
        result=result,
        captured=captured,
        invented=invented,
        missed=missed,
        source_confusion=confusion,
        latency_seconds=latency,
        cost_inr=result.llm_response.cost_inr(),
        gold_ambiguous=record["labels"]["instruction_ambiguous"],
        predicted_ambiguous=result.clarify,
    )


# ── ID-independent "caught" ───────────────────────────────────────────────

def caught_by_composite(record: dict[str, Any], obligation: Obligation, cart) -> bool:
    """
    Did the deterministic verifier flag this record, using only the obligation
    supplied? Works identically for gold and compiled obligations.
    """
    detail = evaluate_constraint_checks(obligation, cart)
    any_fail = (
        any(v == Verdict.FAIL for v in detail.criterion_verdicts.values())
        or detail.budget_verdict == Verdict.FAIL
        or detail.merchant_scope_verdict == Verdict.FAIL
        or detail.delivery_verdict == Verdict.FAIL
        or detail.total_arithmetic_verdict == Verdict.FAIL
    )
    if record["labels"]["abstain_expected"]:
        return any(v == Verdict.ABSTAIN for v in detail.criterion_verdicts.values())
    return any_fail


def blocked_by_composite(obligation: Obligation, cart) -> bool:
    """Would this record be blocked? Used for the false-block side of the delta."""
    detail = evaluate_constraint_checks(obligation, cart)
    return (
        any(v == Verdict.FAIL for v in detail.criterion_verdicts.values())
        or detail.budget_verdict == Verdict.FAIL
        or detail.merchant_scope_verdict == Verdict.FAIL
        or detail.delivery_verdict == Verdict.FAIL
        or detail.total_arithmetic_verdict == Verdict.FAIL
    )


# ── Top-level run ─────────────────────────────────────────────────────────

def run_compiler_eval(
    *,
    client: LLMClient | None = None,
    cache_only: bool = False,
    limit_seeds: int | None = None,
) -> dict[str, Any]:
    client = client or default_client(cache_only=cache_only)
    records = load_records()
    split = load_split()

    train_records = [r for r in records if split[r["order_id"]] == "train"]
    if not train_records:
        raise RuntimeError("No train records found — check eval/corpus/split.json.")

    # One representative record per seed (they share instruction + obligation).
    by_seed: dict[str, dict[str, Any]] = {}
    for record in train_records:
        by_seed.setdefault(record["generation"]["seed_id"], record)

    seed_ids = sorted(by_seed)
    if limit_seeds is not None:
        seed_ids = seed_ids[:limit_seeds]

    compilations: list[SeedCompilation] = []
    compiled_by_seed: dict[str, Obligation] = {}

    for seed_id in seed_ids:
        record = by_seed[seed_id]
        started = time.perf_counter()
        result = compile_obligation(
            record["instruction_text"],
            client=client,
            user_id="eval",
            reference_date=REFERENCE_DATE,
        )
        latency = time.perf_counter() - started
        compilations.append(evaluate_seed(record, result, latency=latency))
        compiled_by_seed[seed_id] = result.obligation

    return _summarise(compilations, compiled_by_seed, train_records, seed_ids)


def _summarise(
    compilations: list[SeedCompilation],
    compiled_by_seed: dict[str, Obligation],
    train_records: list[dict[str, Any]],
    seed_ids: list[str],
) -> dict[str, Any]:
    total_captured = sum(c.captured for c in compilations)
    total_invented = sum(c.invented for c in compilations)
    total_missed = sum(c.missed for c in compilations)
    total_gold = total_captured + total_missed
    total_emitted = total_captured + total_invented

    confusion_total: dict[str, int] = {
        "correct": 0, "stated_labelled_as_inferred": 0,
        "inferred_labelled_as_stated": 0, "other_mismatch": 0,
    }
    for c in compilations:
        for key, value in c.source_confusion.items():
            confusion_total[key] += value
    matched_total = sum(confusion_total.values())

    unresolvable = [p for c in compilations for p in c.result.unresolvable_paths]
    emitted_including_invalid = total_emitted + len(unresolvable)

    # Ambiguity precision/recall against a hand-assigned, intuition-based label.
    tp = sum(1 for c in compilations if c.predicted_ambiguous and c.gold_ambiguous)
    fp = sum(1 for c in compilations if c.predicted_ambiguous and not c.gold_ambiguous)
    fn = sum(1 for c in compilations if not c.predicted_ambiguous and c.gold_ambiguous)

    latencies = [c.latency_seconds for c in compilations]
    costs = [c.cost_inr for c in compilations]

    # ── The delta ────────────────────────────────────────────────────────
    scoped = [r for r in train_records if r["generation"]["seed_id"] in set(seed_ids)]
    gold_caught = gold_blocked = comp_caught = comp_blocked = 0
    n_violations = n_clean = 0

    for record in scoped:
        gold_obligation, cart = record_to_models(record)
        compiled_obligation = compiled_by_seed[record["generation"]["seed_id"]]
        is_clean = record["labels"]["violation_class"] == "CLEAN"

        if is_clean:
            n_clean += 1
            gold_blocked += int(blocked_by_composite(gold_obligation, cart))
            comp_blocked += int(blocked_by_composite(compiled_obligation, cart))
        else:
            n_violations += 1
            gold_caught += int(caught_by_composite(record, gold_obligation, cart))
            comp_caught += int(caught_by_composite(record, compiled_obligation, cart))

    gold_recall = rate(gold_caught, n_violations)
    comp_recall = rate(comp_caught, n_violations)
    gold_fb = rate(gold_blocked, n_clean)
    comp_fb = rate(comp_blocked, n_clean)

    per_language: dict[str, Any] = {}
    for lang in ("en", "hinglish"):
        subset = [c for c in compilations if c.language == lang]
        if not subset:
            continue
        cap = sum(c.captured for c in subset)
        mis = sum(c.missed for c in subset)
        inv = sum(c.invented for c in subset)
        per_language[lang] = {
            "seeds": len(subset),
            "extraction_recall": rate(cap, cap + mis).as_dict(),
            "extraction_precision": rate(cap, cap + inv).as_dict(),
        }

    return {
        "stage": 4,
        "scope": "train split only — holdout untouched at Stage 4",
        "seeds_compiled": len(compilations),
        "train_records_in_scope": len(scoped),

        "criterion_extraction": {
            "note": (
                "Matched on (field, operator, value). SEMANTIC criteria are matched on "
                "(field, operator) only — their corpus value is a hand-authored label "
                "('not_too_spicy') the compiler cannot reproduce without authoring a "
                "value, which the output guard forbids. Semantic extraction is therefore "
                "measured more coarsely than the rest."
            ),
            "gold_criteria": total_gold,
            "captured": total_captured,
            "invented": total_invented,
            "missed": total_missed,
            "recall": rate(total_captured, total_gold).as_dict(),
            "precision": rate(total_captured, total_emitted).as_dict(),
        },

        "source_labelling": {
            "note": (
                "Scored only on criteria that matched gold by (field, operator, value). "
                "stated_labelled_as_inferred is the dangerous direction: the criterion "
                "survives but can no longer block, so violations clear while extraction "
                "accuracy still looks clean."
            ),
            "matched_criteria": matched_total,
            "counts": confusion_total,
            "accuracy": rate(confusion_total["correct"], matched_total).as_dict(),
            "stated_labelled_as_inferred_rate": rate(
                confusion_total["stated_labelled_as_inferred"], matched_total
            ).as_dict(),
            "inferred_labelled_as_stated_rate": rate(
                confusion_total["inferred_labelled_as_stated"], matched_total
            ).as_dict(),
        },

        "unresolvable_paths": {
            "note": "Field paths the model emitted that are not in core/models/fields.py.",
            "count": len(unresolvable),
            "paths": sorted(set(unresolvable)),
            "rate": rate(len(unresolvable), max(emitted_including_invalid, 1)).as_dict(),
        },

        "ambiguity_detection": {
            "note": (
                "instruction_ambiguous was assigned per-seed by hand during corpus "
                "authoring from an intuitive reading, not a written-down rule — it is "
                "the noisiest label in the corpus. Read these as agreement with the "
                "author's intuition, not as ground truth."
            ),
            "true_positives": tp, "false_positives": fp, "false_negatives": fn,
            "precision": rate(tp, tp + fp).as_dict(),
            "recall": rate(tp, tp + fn).as_dict(),
        },

        "per_language": per_language,

        "cost": {
            "usd_to_inr": str(USD_TO_INR),
            "total_inr": str(sum(costs, Decimal("0"))),
            "mean_per_compilation_inr": str(
                (sum(costs, Decimal("0")) / len(costs)) if costs else Decimal("0")
            ),
        },

        "latency_seconds": {
            "p50": round(statistics.median(latencies), 4) if latencies else 0.0,
            "p95": round(sorted(latencies)[min(len(latencies) - 1, int(0.95 * (len(latencies) - 1)))], 4)
            if latencies else 0.0,
            "note": "Cached responses report near-zero latency; re-record to measure live latency.",
        },

        "delta_vs_gold_criteria": {
            "note": (
                "Stage 3 verifiers run over the same train records twice — once with "
                "hand-authored obligations, once with compiled ones. This is the "
                "compiler's true cost at the point of decision."
            ),
            "n_violations": n_violations,
            "n_clean": n_clean,
            "gold_recall": gold_recall.as_dict(),
            "compiled_recall": comp_recall.as_dict(),
            "recall_delta": round(comp_recall.rate - gold_recall.rate, 4),
            "gold_false_block": gold_fb.as_dict(),
            "compiled_false_block": comp_fb.as_dict(),
            "false_block_delta": round(comp_fb.rate - gold_fb.rate, 4),
        },

        "per_seed": [
            {
                "seed_id": c.seed_id, "language": c.language,
                "captured": c.captured, "invented": c.invented, "missed": c.missed,
                "unresolvable_paths": c.result.unresolvable_paths,
                "dropped": c.result.dropped_criteria,
                "gold_ambiguous": c.gold_ambiguous, "predicted_ambiguous": c.predicted_ambiguous,
            }
            for c in compilations
        ],
    }


def write_results(metrics: dict[str, Any], out_dir: Path | None = None) -> Path:
    out_dir = out_dir or RESULTS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "stage4_compiler_results.json"
    path.write_text(json.dumps(metrics, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8")
    (out_dir / "stage4_compiler_results.md").write_text(_render_markdown(metrics), encoding="utf-8")
    return path


def _fmt(v: dict) -> str:
    return f"{v['rate']:.1%} [{v['ci_low']:.1%}, {v['ci_high']:.1%}] (n={v['n']})"


def _render_markdown(m: dict[str, Any]) -> str:
    ex = m["criterion_extraction"]
    src = m["source_labelling"]
    d = m["delta_vs_gold_criteria"]
    amb = m["ambiguity_detection"]
    lines = [
        "# SANKALP — Stage 4 Results (obligation compiler)",
        "",
        f"Scope: {m['scope']}. {m['seeds_compiled']} seeds compiled, "
        f"{m['train_records_in_scope']} train records in scope.",
        "",
        "## Criterion extraction",
        "",
        f"- gold criteria: {ex['gold_criteria']}",
        f"- captured: {ex['captured']} | invented: {ex['invented']} | missed: {ex['missed']}",
        f"- recall: {_fmt(ex['recall'])}",
        f"- precision: {_fmt(ex['precision'])}",
        "",
        "## Source labelling (the metric that hides failure)",
        "",
        src["note"],
        "",
        f"- matched criteria scored: {src['matched_criteria']}",
        f"- accuracy: {_fmt(src['accuracy'])}",
        f"- **stated-labelled-as-inferred: {_fmt(src['stated_labelled_as_inferred_rate'])}** (dangerous direction)",
        f"- inferred-labelled-as-stated: {_fmt(src['inferred_labelled_as_stated_rate'])}",
        "",
        "## Unresolvable paths",
        "",
        f"- count: {m['unresolvable_paths']['count']} | rate: {_fmt(m['unresolvable_paths']['rate'])}",
        f"- paths: {m['unresolvable_paths']['paths']}",
        "",
        "## Ambiguity detection",
        "",
        amb["note"],
        "",
        f"- precision: {_fmt(amb['precision'])} | recall: {_fmt(amb['recall'])}",
        "",
        "## Delta vs hand-authored criteria — the compiler's true cost",
        "",
        d["note"],
        "",
        f"| | gold criteria | compiled criteria | delta |",
        f"|---|---|---|---|",
        f"| recall | {_fmt(d['gold_recall'])} | {_fmt(d['compiled_recall'])} | {d['recall_delta']:+.1%} |",
        f"| false-block | {_fmt(d['gold_false_block'])} | {_fmt(d['compiled_false_block'])} | {d['false_block_delta']:+.1%} |",
        "",
        "## Cost and latency",
        "",
        f"- total: Rs {m['cost']['total_inr']} across {m['seeds_compiled']} compilations "
        f"(mean Rs {m['cost']['mean_per_compilation_inr']} each, at {m['cost']['usd_to_inr']} INR/USD)",
        f"- latency p50 {m['latency_seconds']['p50']:.3f}s / p95 {m['latency_seconds']['p95']:.3f}s "
        f"— {m['latency_seconds']['note']}",
        "",
        "## Per-language",
        "",
        "| language | seeds | extraction recall | extraction precision |",
        "|---|---|---|---|",
    ]
    for lang, v in sorted(m["per_language"].items()):
        lines.append(
            f"| {lang} | {v['seeds']} | {_fmt(v['extraction_recall'])} | {_fmt(v['extraction_precision'])} |"
        )
    lines.append("")
    return "\n".join(lines)


# ── CLI ───────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Run the Stage 4 obligation-compiler evaluation (train split only)."
    )
    parser.add_argument(
        "--cache-only", action="store_true",
        help="Replay the committed eval/llm_cache/ and never call a provider. "
             "Fails loudly on a cache miss. Use in CI.",
    )
    parser.add_argument(
        "--limit-seeds", type=int, default=None,
        help="Compile only the first N train seeds — use for a cheap smoke run before "
             "spending on the full set.",
    )
    args = parser.parse_args(argv)

    metrics = run_compiler_eval(cache_only=args.cache_only, limit_seeds=args.limit_seeds)
    path = write_results(metrics)
    print(_render_markdown(metrics))
    print(f"\nWrote {path} and {path.with_name('stage4_compiler_results.md')}")
    return 0


if __name__ == "__main__":   # pragma: no cover
    raise SystemExit(main())
