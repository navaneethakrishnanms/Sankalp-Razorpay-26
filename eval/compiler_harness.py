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
import os
import statistics
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from core.llm.client import (
    MODEL_PRICING,
    USD_TO_INR,
    LLMClient,
    default_client,
    default_model_for,
)
from core.models.enums import Verdict
from core.models.obligation import Obligation
from core.obligation.compiler import (
    DEFAULT_MAX_TOKENS,
    DEFAULT_PROMPT_VERSION,
    PROMPT_VERSIONS,
    CompilationResult,
    compile_obligation,
    resolve_prompt,
)
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
    return bool(block_causes(obligation, cart))


def block_causes(obligation: Obligation, cart) -> list[str]:
    """
    WHICH checks blocked this record, not just whether one did.

    A false-block rate tells you the compiler is wrong; the cause tells you
    where. Without this, an invented criterion and a mis-parsed budget ceiling
    look identical in the results and the only way to tell them apart is to
    re-run the pipeline by hand.
    """
    detail = evaluate_constraint_checks(obligation, cart)
    causes: list[str] = []
    by_id = {c.id: c for c in obligation.acceptance_criteria}
    for cid, verdict in detail.criterion_verdicts.items():
        if verdict == Verdict.FAIL:
            criterion = by_id.get(cid)
            if criterion is not None:
                causes.append(f"criterion:{criterion.field} {criterion.operator.value} {criterion.value!r}")
            else:
                causes.append(f"criterion:{cid}")
    if detail.budget_verdict == Verdict.FAIL:
        causes.append("budget_ceiling")
    if detail.merchant_scope_verdict == Verdict.FAIL:
        causes.append("merchant_scope")
    if detail.delivery_verdict == Verdict.FAIL:
        causes.append("delivery_window")
    if detail.total_arithmetic_verdict == Verdict.FAIL:
        causes.append("cart.total_arithmetic")
    return causes


def _criteria_dump(obligation: Obligation) -> list[dict[str, Any]]:
    return [
        {"field": c.field, "operator": c.operator.value, "value": c.value, "source": c.source.value}
        for c in obligation.acceptance_criteria
    ]


def _gold_criteria_dump(record: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"field": c["field"], "operator": c["operator"], "value": c["value"], "source": c["source"]}
        for c in record["obligation"]["acceptance_criteria"]
    ]


# ── Top-level run ─────────────────────────────────────────────────────────

def run_compiler_eval(
    *,
    client: LLMClient | None = None,
    cache_only: bool = False,
    limit_seeds: int | None = None,
    model: str | None = None,
    temperature: float = 0.0,
    effort: str = "medium",
    max_tokens: int = DEFAULT_MAX_TOKENS,
    prompt_version: str = DEFAULT_PROMPT_VERSION,
) -> dict[str, Any]:
    client = client or default_client(cache_only=cache_only)
    resolved_model = model or default_model_for(client.provider.name)
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
            model=resolved_model,
            temperature=temperature,
            effort=effort,
            max_tokens=max_tokens,
            prompt_version=prompt_version,
        )
        latency = time.perf_counter() - started
        compilations.append(evaluate_seed(record, result, latency=latency))
        compiled_by_seed[seed_id] = result.obligation

    provenance = {
        "note": (
            "A metric without its model identifier is not reproducible. Every number "
            "in this file was produced by exactly this configuration."
        ),
        "provider": client.provider.name,
        "model": resolved_model,
        "temperature": temperature,
        "reasoning_effort": effort,
        "max_tokens": max_tokens,
        "prompt_version": resolve_prompt(prompt_version)[1],
        "prompt_file": resolve_prompt(prompt_version)[0],
        "reference_date": REFERENCE_DATE.isoformat(),
        "cache_hits": client.hits,
        "cache_misses": client.misses,
        "pricing_verified": (
            MODEL_PRICING[resolved_model].verified if resolved_model in MODEL_PRICING else False
        ),
        "pricing_source": (
            MODEL_PRICING[resolved_model].source_note
            if resolved_model in MODEL_PRICING else "no pricing entry"
        ),
    }

    return _summarise(compilations, compiled_by_seed, train_records, seed_ids, provenance)


def _summarise(
    compilations: list[SeedCompilation],
    compiled_by_seed: dict[str, Obligation],
    train_records: list[dict[str, Any]],
    seed_ids: list[str],
    provenance: dict[str, Any],
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

    # ── Obligation-field extraction ────────────────────────────────────────
    # Criteria are only half the compiler's output. budget_ceiling,
    # merchant_scope and delivery_window drive four of the six violation
    # classes (BUDGET_BREACH, WRONG_MERCHANT, TIMING_MISS, and the clean-record
    # false blocks they cause), so scoring criteria alone leaves most of the
    # delta unexplained.
    field_scores: dict[str, dict[str, int]] = {
        name: {"correct": 0, "missed": 0, "spurious": 0, "wrong_value": 0}
        for name in ("budget_ceiling", "merchant_scope", "delivery_window")
    }
    field_mismatches: list[dict[str, Any]] = []

    def _equivalent(gold: Any, got: Any) -> bool:
        """
        Compare by VALUE, not by string form.

        `str(Decimal("1500.00")) != "1500"` even though both mean the same
        ceiling, and a naive string compare scored the compiler wrong on every
        budget it got exactly right. Same trap for datetimes ("+00:00" vs "Z")
        and for scope lists whose order is incidental.
        """
        if gold is None or got is None:
            return gold is None and got is None
        for parse in (Decimal, datetime.fromisoformat):
            try:
                return parse(str(gold)) == parse(str(got))   # type: ignore[operator]
            except (InvalidOperation, ValueError, TypeError):
                continue
        if isinstance(gold, list) or isinstance(got, list):
            return sorted(map(str, gold if isinstance(gold, list) else [gold])) == sorted(
                map(str, got if isinstance(got, list) else [got])
            )
        return str(gold) == str(got)

    def _score(name: str, seed_id: str, instruction: str, gold: Any, got: Any) -> None:
        if gold is None and got is None:
            field_scores[name]["correct"] += 1
            return
        if gold is not None and got is None:
            field_scores[name]["missed"] += 1
        elif gold is None and got is not None:
            field_scores[name]["spurious"] += 1
        elif _equivalent(gold, got):
            field_scores[name]["correct"] += 1
            return
        else:
            field_scores[name]["wrong_value"] += 1
        field_mismatches.append(
            {"seed_id": seed_id, "field": name, "gold": str(gold), "compiled": str(got),
             "instruction": instruction}
        )

    for c in compilations:
        gold_ob = c.gold_record["obligation"]
        got_ob = c.result.obligation

        _score("budget_ceiling", c.seed_id, c.instruction,
                gold_ob["budget_ceiling"],
                str(got_ob.budget_ceiling) if got_ob.budget_ceiling is not None else None)

        gold_scope = gold_ob["merchant_scope"]["merchant_ids"] or gold_ob["merchant_scope"]["category"]
        got_scope = list(got_ob.merchant_scope.merchant_ids) or got_ob.merchant_scope.category
        _score("merchant_scope", c.seed_id, c.instruction, gold_scope or None, got_scope or None)

        gold_deadline = (gold_ob["delivery_window"] or {}).get("latest_by")
        got_deadline = (
            got_ob.delivery_window.latest_by.isoformat() if got_ob.delivery_window else None
        )
        _score("delivery_window", c.seed_id, c.instruction, gold_deadline, got_deadline)

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
    false_block_attribution: dict[str, int] = {}
    false_block_examples: list[dict[str, Any]] = []

    for record in scoped:
        gold_obligation, cart = record_to_models(record)
        compiled_obligation = compiled_by_seed[record["generation"]["seed_id"]]
        is_clean = record["labels"]["violation_class"] == "CLEAN"

        if is_clean:
            n_clean += 1
            gold_blocked += int(blocked_by_composite(gold_obligation, cart))
            causes = block_causes(compiled_obligation, cart)
            if causes:
                comp_blocked += 1
                for cause in causes:
                    false_block_attribution[cause] = false_block_attribution.get(cause, 0) + 1
                if len(false_block_examples) < 10:
                    false_block_examples.append({
                        "order_id": record["order_id"],
                        "clean_mutation": record["generation"]["mutation"],
                        "causes": causes,
                    })
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
        "provenance": provenance,
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

        "obligation_field_extraction": {
            "note": (
                "budget_ceiling, merchant_scope and delivery_window are not "
                "AcceptanceCriteria but drive four of the six violation classes. "
                "'spurious' means the compiler invented a limit the user never set — "
                "the most expensive error here, because it blocks correct orders."
            ),
            "counts": field_scores,
            "accuracy": {
                name: rate(s["correct"], sum(s.values())).as_dict()
                for name, s in field_scores.items()
            },
            "mismatches": field_mismatches,
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
            "note": (
                "ESTIMATE, not spend. Computed from cached token counts at published "
                "rates; no live API calls occurred during THIS run if cache_misses is 0 "
                "below. This is what the recorded compilations would have cost, not a "
                "charge incurred by running the harness again."
            ),
            "is_estimate": True,
            "cache_hits_this_run": provenance["cache_hits"],
            "cache_misses_this_run": provenance["cache_misses"],
            "usd_to_inr": str(USD_TO_INR),
            "total_inr": str(sum(costs, Decimal("0"))),
            "mean_per_compilation_inr": str(
                (sum(costs, Decimal("0")) / len(costs)) if costs else Decimal("0")
            ),
        },

        "latency_seconds": (
            {
                "note": "NOT_MEASURED — this run replayed the cache (cache_misses=0); "
                         "cached lookups take microseconds and do not reflect live "
                         "provider latency. Re-run with cache misses to measure.",
                "p50": None, "p95": None,
            }
            if provenance["cache_misses"] == 0 else
            {
                "note": f"Measured live over {provenance['cache_misses']} uncached call(s) "
                         f"in this run. Cached hits are excluded from these percentiles.",
                "p50": round(statistics.median(latencies), 4) if latencies else None,
                "p95": round(sorted(latencies)[min(len(latencies) - 1, int(0.95 * (len(latencies) - 1)))], 4)
                if latencies else None,
            }
        ),

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
            "false_block_attribution": dict(
                sorted(false_block_attribution.items(), key=lambda kv: -kv[1])
            ),
            "false_block_examples": false_block_examples,
        },

        "per_seed": [
            {
                "seed_id": c.seed_id, "language": c.language,
                "instruction": c.instruction,
                "captured": c.captured, "invented": c.invented, "missed": c.missed,
                "gold_criteria": _gold_criteria_dump(c.gold_record),
                "compiled_criteria": _criteria_dump(c.result.obligation),
                "gold_budget_ceiling": c.gold_record["obligation"]["budget_ceiling"],
                "compiled_budget_ceiling": (
                    str(c.result.obligation.budget_ceiling)
                    if c.result.obligation.budget_ceiling is not None else None
                ),
                "gold_merchant_scope": c.gold_record["obligation"]["merchant_scope"],
                "compiled_merchant_scope": {
                    "merchant_ids": list(c.result.obligation.merchant_scope.merchant_ids),
                    "category": c.result.obligation.merchant_scope.category,
                },
                "compiled_delivery_latest_by": (
                    c.result.obligation.delivery_window.latest_by.isoformat()
                    if c.result.obligation.delivery_window is not None else None
                ),
                "compiled_prohibited": list(c.result.obligation.prohibited),
                "unresolvable_paths": c.result.unresolvable_paths,
                "dropped": c.result.dropped_criteria,
                "gold_ambiguous": c.gold_ambiguous, "predicted_ambiguous": c.predicted_ambiguous,
            }
            for c in compilations
        ],
    }


def write_results(metrics: dict[str, Any], out_dir: Path | None = None) -> Path:
    """
    Write per-prompt-version files. A v2 run must never overwrite v1's numbers:
    prompt iteration against a measured set is a form of fitting, and the only
    honest way to do it is to keep every version's results side by side.
    """
    out_dir = out_dir or RESULTS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    version = metrics["provenance"]["prompt_version"].rsplit("/", 1)[-1]
    stem = f"stage4_results_{version}"
    path = out_dir / f"{stem}.json"
    path.write_text(json.dumps(metrics, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8")
    (out_dir / f"{stem}.md").write_text(_render_markdown(metrics), encoding="utf-8")
    return path


def _fmt(v: dict) -> str:
    return f"{v['rate']:.1%} [{v['ci_low']:.1%}, {v['ci_high']:.1%}] (n={v['n']})"


def _render_markdown(m: dict[str, Any]) -> str:
    ex = m["criterion_extraction"]
    src = m["source_labelling"]
    d = m["delta_vs_gold_criteria"]
    amb = m["ambiguity_detection"]
    p = m["provenance"]
    lines = [
        "# SANKALP — Stage 4 Results (obligation compiler)",
        "",
        f"Scope: {m['scope']}. {m['seeds_compiled']} seeds compiled, "
        f"{m['train_records_in_scope']} train records in scope.",
        "",
        "## Provenance",
        "",
        p["note"],
        "",
        "| | |",
        "|---|---|",
        f"| provider | `{p['provider']}` |",
        f"| model | `{p['model']}` |",
        f"| temperature | {p['temperature']} |",
        f"| reasoning effort | {p['reasoning_effort']} |",
        f"| prompt version | `{p['prompt_version']}` ({p['prompt_file']}) |",
        f"| cache hits / misses | {p['cache_hits']} / {p['cache_misses']} |",
        f"| pricing verified | {'yes' if p['pricing_verified'] else '**NO** — ' + p['pricing_source']} |",
        "",
        "## Criterion extraction",
        "",
        f"- gold criteria: {ex['gold_criteria']}",
        f"- captured: {ex['captured']} | invented: {ex['invented']} | missed: {ex['missed']}",
        f"- recall: {_fmt(ex['recall'])}",
        f"- precision: {_fmt(ex['precision'])}",
        "",
        "## Obligation-field extraction",
        "",
        m["obligation_field_extraction"]["note"],
        "",
        "| field | correct | missed | spurious | wrong value | accuracy |",
        "|---|---|---|---|---|---|",
    ]
    for name, counts in m["obligation_field_extraction"]["counts"].items():
        acc = m["obligation_field_extraction"]["accuracy"][name]
        lines.append(
            f"| `{name}` | {counts['correct']} | {counts['missed']} | "
            f"{counts['spurious']} | {counts['wrong_value']} | {_fmt(acc)} |"
        )
    mismatches = m["obligation_field_extraction"]["mismatches"]
    if mismatches:
        lines += ["", "Mismatches:", ""]
        lines += [
            f"- `{mm['seed_id']}` **{mm['field']}** gold=`{mm['gold']}` compiled=`{mm['compiled']}`"
            for mm in mismatches[:15]
        ]
    lines += [
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
        "| | gold criteria | compiled criteria | delta |",
        "|---|---|---|---|",
        f"| recall | {_fmt(d['gold_recall'])} | {_fmt(d['compiled_recall'])} | {d['recall_delta']:+.1%} |",
        f"| false-block | {_fmt(d['gold_false_block'])} | {_fmt(d['compiled_false_block'])} | {d['false_block_delta']:+.1%} |",
        "",
        "### What caused the false blocks",
        "",
        "A false-block rate says the compiler is wrong; the cause says where.",
        "",
    ]
    if d["false_block_attribution"]:
        lines += ["| cause | clean records blocked |", "|---|---|"]
        lines += [f"| `{cause}` | {count} |" for cause, count in d["false_block_attribution"].items()]
    else:
        lines.append("_No clean record was blocked by the compiled criteria._")
    lines += [
        "",
        "Per-seed gold-vs-compiled criteria are in `stage4_results.json` under `per_seed`.",
        "",
        "## Cost and latency",
        "",
        f"- cost — {m['cost']['note']} Rs {m['cost']['total_inr']} "
        f"across {m['seeds_compiled']} compilations "
        f"(mean Rs {m['cost']['mean_per_compilation_inr']} each, at {m['cost']['usd_to_inr']} INR/USD)",
        f"- cache this run: {m['cost']['cache_hits_this_run']} hits / "
        f"{m['cost']['cache_misses_this_run']} misses",
        (
            f"- latency: {m['latency_seconds']['note']}"
            if m['latency_seconds']['p50'] is None else
            f"- latency p50 {m['latency_seconds']['p50']:.3f}s / p95 {m['latency_seconds']['p95']:.3f}s "
            f"— {m['latency_seconds']['note']}"
        ),
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
    parser.add_argument(
        "--model", default=None,
        help="Override the model string. Defaults to the provider's default "
             "(groq: openai/gpt-oss-120b).",
    )
    parser.add_argument(
        "--temperature", type=float, default=0.0,
        help="Sampling temperature. Defaults to 0 — anything higher makes the same "
             "instruction compile differently on each run, which makes both the cache "
             "and the metrics meaningless.",
    )
    parser.add_argument(
        "--effort", default="medium",
        help="Reasoning effort passed to the provider (default: medium).",
    )
    parser.add_argument(
        "--max-tokens", type=int, default=DEFAULT_MAX_TOKENS,
        help=f"Output token ceiling (default: {DEFAULT_MAX_TOKENS}). Providers reserve "
             f"(prompt + max_tokens) against a per-minute budget, so raising this can "
             f"push a request past a free-tier limit and get it rejected outright.",
    )
    parser.add_argument(
        "--prompt-version", default=DEFAULT_PROMPT_VERSION, choices=sorted(PROMPT_VERSIONS),
        help=f"Prompt version to compile with (default: {DEFAULT_PROMPT_VERSION}). Each "
             f"version writes its own eval/results/stage4_results_<version>.* files, so "
             f"iterating never overwrites an earlier version's numbers.",
    )
    parser.add_argument(
        "--pace-seconds", type=float, default=0.0,
        help="Minimum seconds between provider calls. 0 (default) self-tunes: pacing "
             "starts at zero and widens automatically the first time a token rate "
             "limit is hit. Set explicitly to skip the discovery round-trips.",
    )
    args = parser.parse_args(argv)

    if args.pace_seconds > 0:
        os.environ["SANKALP_GROQ_MIN_INTERVAL"] = str(args.pace_seconds)

    metrics = run_compiler_eval(
        cache_only=args.cache_only, limit_seeds=args.limit_seeds,
        model=args.model, temperature=args.temperature, effort=args.effort,
        max_tokens=args.max_tokens, prompt_version=args.prompt_version,
    )
    path = write_results(metrics)
    print(_render_markdown(metrics))
    print(f"\nWrote {path} and {path.with_suffix('.md')}")
    return 0


if __name__ == "__main__":   # pragma: no cover
    raise SystemExit(main())
