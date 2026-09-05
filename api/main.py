"""
SANKALP demo API — a thin FastAPI layer over the real pipeline.

DESIGN FOR A LIVE DEMO, NOT A PRODUCT
---------------------------------------
Every scenario runs the REAL constraint verifier, REAL receipt verifier, REAL
aggregator (core/clearing/aggregator.py), and REAL engine
(core/clearing/engine.py) — nothing about the clearing mechanism is mocked.

The two scenarios that involve the semantic verifier ("Fooled Judge" and
"Semantic Caution") use a SCRIPTED semantic-verifier response instead of a
live or cached LLM call, and say so explicitly in their API response
(`"semantic_response_is_scripted": true`). This is a deliberate reliability
choice, not a shortcut: a live pitch cannot depend on a Groq call succeeding
or a cache entry matching exactly, and the two scripted scenarios reproduce
EXACTLY what this project already measured —
  * "Fooled Judge" reproduces tests/unit/test_stage5.py::TestLiveFooledJudge's
    proven scenario (a confident PASS on SELF-class evidence, excluded by a
    REC floor).
  * "Semantic Caution" reproduces the actual Stage 5 live-run result (the
    model abstained on all 16 records in the true fooled-judge population —
    see README's misses table and FAILURES.md).
Neither is a fabricated result; both are honestly-labelled replays of
findings already established elsewhere in this project, chosen so the demo
cannot fail from a network hiccup mid-pitch.

Every other scenario reads a REAL corpus record and runs it through the REAL
deterministic verifiers with ZERO scripting and ZERO network dependency.

Run:
    uvicorn api.main:app --reload --port 8000
Then open http://localhost:8000/
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse

from api.bank import router as bank_router

from core.clearing.engine import build_evidence, clear
from core.models.enums import EvidenceClass, Verdict
from core.models.verifier import VerifierOutput
from core.settlement.instruction import emit, explain
from core.verifiers.constraint import ConstraintVerifier
from core.verifiers.receipt import ReceiptVerifier
from eval.harness import load_records, load_split, record_to_models

app = FastAPI(title="SANKALP — Clearing Console")

# The React dev server (Vite, default port 5173) is a different origin from
# uvicorn (8000). Wide open only because every "user" and "wallet" here is a
# demo fixture with no real money or credentials behind it — see api/bank.py.
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)
app.include_router(bank_router)

# ── Corpus, loaded once at startup ─────────────────────────────────────────

_RECORDS = load_records()
_SPLIT = load_split()
_TRAIN = [r for r in _RECORDS if _SPLIT[r["order_id"]] == "train"]


def _pick(violation_class: str, **label_filters: Any) -> dict[str, Any]:
    """Deterministically pick the first train record matching, sorted by
    order_id — never hardcoded ids, so this works against any corpus state
    that satisfies the project's own floors (every subpopulation has >=25
    train records, guaranteed since Stage 2.5)."""
    candidates = sorted(
        (r for r in _TRAIN if r["labels"]["violation_class"] == violation_class
         and all(r["labels"].get(k) == v for k, v in label_filters.items())),
        key=lambda r: r["order_id"],
    )
    if not candidates:
        raise RuntimeError(
            f"No train record found for violation_class={violation_class!r} {label_filters}. "
            f"Corpus may be out of date — run `make corpus`."
        )
    return candidates[0]


# ── Serialisation helpers ──────────────────────────────────────────────────

def _verdict_to_dict(v: VerifierOutput, evidence_index: dict[str, str], survived: bool) -> dict[str, Any]:
    basis_class = evidence_index.get(v.declared_basis[0]) if v.declared_basis else "SELF"
    return {
        "role": v.role, "verdict": v.verdict.value, "confidence": v.confidence,
        "declared_basis": v.declared_basis, "basis_class": basis_class,
        "survived": survived, "loss_estimate": str(v.loss_estimate) if v.loss_estimate else None,
        "reasoning": v.reasoning,
    }


def _cart_dict(cart) -> dict[str, Any]:
    return {
        "merchant": {"id": cart.merchant.id, "name": cart.merchant.name, "category": cart.merchant.category},
        "items": [{"name": i.name, "quantity": i.quantity, "unit_price": str(i.unit_price),
                    "ingredients": i.ingredients, "category": i.category} for i in cart.items],
        "total": str(cart.total), "fulfilment_eta": cart.fulfilment_eta.isoformat(),
    }


def _obligation_dict(obligation) -> dict[str, Any]:
    return {
        "raw_instruction": obligation.raw_instruction,
        "acceptance_criteria": [
            {"field": c.field, "operator": c.operator.value, "value": c.value, "source": c.source.value}
            for c in obligation.acceptance_criteria
        ],
        "budget_ceiling": str(obligation.budget_ceiling) if obligation.budget_ceiling else None,
        "merchant_scope": {"merchant_ids": obligation.merchant_scope.merchant_ids,
                             "category": obligation.merchant_scope.category},
        "delivery_window": (obligation.delivery_window.latest_by.isoformat()
                              if obligation.delivery_window else None),
        "admissibility_floor": obligation.admissibility_floor.name,
    }


def _decision_dict(outcome, clearing_hash: str = "0" * 64) -> dict[str, Any]:
    instruction = emit(outcome, clearing_hash)
    return {
        "verdict": outcome.aggregate.verdict.value,
        "confidence": outcome.aggregate.confidence,
        "basis_class": outcome.aggregate.basis_class.name if outcome.aggregate.basis_class else None,
        "reason_code": outcome.aggregate.reason_code,
        "action": outcome.action.value,
        "action_explained": explain(outcome.action),
        "settlement_hash": instruction.hash,
    }


def _run_deterministic(record: dict[str, Any]) -> dict[str, Any]:
    """Runs the REAL constraint + receipt verifiers, REAL aggregator, REAL
    engine over a REAL corpus record. Zero scripting, zero network."""
    obligation, cart = record_to_models(record)
    envelope = build_evidence(cart, obligation.hash or "0" * 64)

    constraint_out = ConstraintVerifier().verify(obligation, cart)
    receipt_out = ReceiptVerifier().verify(obligation, cart)
    verifiers = [constraint_out, receipt_out]

    outcome = clear(obligation, cart, verifiers, envelope, enforce_floor=True)
    survivor_ids = {(v.role, v.verdict, v.confidence) for v in outcome.aggregate.survivors}

    return {
        "scenario": {
            "kind": "deterministic",
            "instruction_text": record["instruction_text"], "language": record["language"],
            "violation_class": record["labels"]["violation_class"], "order_id": record["order_id"],
        },
        "obligation": _obligation_dict(obligation),
        "cart": _cart_dict(cart),
        "self_report": None,
        "semantic_response_is_scripted": False,
        "verifiers": [
            _verdict_to_dict(v, {i.id: i.admissibility_class.name for i in envelope.items},
                              (v.role, v.verdict, v.confidence) in survivor_ids)
            for v in verifiers
        ],
        "decision": _decision_dict(outcome),
        "counterfactual": None,
    }


def _run_fooled_judge() -> dict[str, Any]:
    """
    SCRIPTED semantic response — see module docstring.

    Deliberately mirrors eval/stage5_harness.py's `run_deceptive_subset`
    "population B" construction EXACTLY: for a record where NO deterministic
    criterion covers the actual violation (a per-item quantity split — no
    field in the registry expresses "quantity of the item literally named
    X"), the ONLY verifier in play is the semantic one. This is deliberate,
    not an oversight: adding a second, unrelated verifier that happens to
    PASS (e.g. the receipt verifier, which checks pricing/catalogue
    correctness — genuinely irrelevant to this violation) would fill the
    survivor slot after the fooled PASS is excluded and incorrectly show
    EXECUTE either way, hiding exactly the mechanism this scenario exists to
    demonstrate.

    With the floor on: the SELF-basis PASS is excluded, nothing survives,
    the engine correctly HOLDs rather than clear a payment on unverified
    evidence. With the floor off: that same confident PASS is the sole
    survivor and the payment wrongly EXECUTEs. This is the real difference
    in ACTION (not just in which verifiers show as excluded) that makes the
    floor's value visible.
    """
    record = _pick("QUANTITY_MISMATCH", verifier_catchable=False, self_report_deceptive=True)
    obligation, cart = record_to_models(record)
    envelope = build_evidence(cart, obligation.hash or "0" * 64, self_report=record.get("self_report"))
    self_items = envelope.of_class(EvidenceClass.SELF)

    fooled = VerifierOutput(
        role="semantic", verdict=Verdict.PASS, confidence=0.99,
        declared_basis=[self_items[0].id] if self_items else [],
        reasoning="[scripted] the agent's self-report claims full compliance",
    )
    verifiers = [fooled]   # no deterministic backup — see docstring above

    with_floor = clear(obligation, cart, verifiers, envelope, enforce_floor=True)
    without_floor = clear(obligation, cart, verifiers, envelope, enforce_floor=False)
    survivor_ids_wf = {(v.role, v.verdict, v.confidence) for v in with_floor.aggregate.survivors}
    evidence_index = {i.id: i.admissibility_class.name for i in envelope.items}

    return {
        "scenario": {
            "kind": "fooled_judge_scripted",
            "instruction_text": record["instruction_text"], "language": record["language"],
            "violation_class": record["labels"]["violation_class"], "order_id": record["order_id"],
            "label": "Fooled Judge",
        },
        "obligation": _obligation_dict(obligation),
        "cart": _cart_dict(cart),
        "self_report": record.get("self_report"),
        "semantic_response_is_scripted": True,
        "scripted_note": (
            "The semantic verifier's PASS below is scripted, not a live model call — "
            "see api/main.py's module docstring. This record has NO deterministic backup "
            "(no field expresses the actual per-item quantity violation), so this is the "
            "true fooled-judge population, exactly as measured in Stage 5. Floor "
            "enforcement, the aggregator, and the engine below are the real, unmodified code."
        ),
        "verifiers": [
            _verdict_to_dict(v, evidence_index, (v.role, v.verdict, v.confidence) in survivor_ids_wf)
            for v in verifiers
        ],
        "decision": _decision_dict(with_floor),
        "counterfactual": _decision_dict(without_floor),
    }


def _run_semantic_caution() -> dict[str, Any]:
    """SCRIPTED semantic response reproducing the ACTUAL Stage 5 live-run
    result: the model abstained on every one of the 16 true fooled-judge
    records rather than producing a confident PASS. See README's misses
    table and FAILURES.md."""
    record = _pick("CONSTRAINT_VIOLATION", verifier_catchable=False, abstain_expected=False)
    obligation, cart = record_to_models(record)
    envelope = build_evidence(cart, obligation.hash or "0" * 64)
    catalogue_items = envelope.of_class(EvidenceClass.REC)

    abstained = VerifierOutput(
        role="semantic", verdict=Verdict.ABSTAIN, confidence=0.0,
        declared_basis=[i.id for i in catalogue_items],
        reasoning="[scripted, reproduces the measured Stage 5 result] insufficient evidence "
                   "to judge a subjective criterion from catalogue data alone",
    )
    constraint_out = ConstraintVerifier().verify(obligation, cart)
    verifiers = [abstained, constraint_out]

    outcome = clear(obligation, cart, verifiers, envelope, enforce_floor=True)
    survivor_ids = {(v.role, v.verdict, v.confidence) for v in outcome.aggregate.survivors}
    evidence_index = {i.id: i.admissibility_class.name for i in envelope.items}

    return {
        "scenario": {
            "kind": "semantic_caution_scripted",
            "instruction_text": record["instruction_text"], "language": record["language"],
            "violation_class": record["labels"]["violation_class"], "order_id": record["order_id"],
            "label": "Semantic Caution",
        },
        "obligation": _obligation_dict(obligation),
        "cart": _cart_dict(cart),
        "self_report": None,
        "semantic_response_is_scripted": True,
        "scripted_note": (
            "The semantic verifier's ABSTAIN below is scripted to reproduce the ACTUAL "
            "measured Stage 5 result (100% abstention on the 16-record true fooled-judge "
            "population, live, on this open-weights model) rather than repeat a live call "
            "during a demo. This is not a fabricated result — it is what the model did."
        ),
        "verifiers": [
            _verdict_to_dict(v, evidence_index, (v.role, v.verdict, v.confidence) in survivor_ids)
            for v in verifiers
        ],
        "decision": _decision_dict(outcome),
        "counterfactual": None,
    }


# ── Scenario registry ───────────────────────────────────────────────────────

SCENARIOS: dict[str, dict[str, Any]] = {
    "clean":              {"label": "Clean order",            "run": lambda: _run_deterministic(_pick("CLEAN"))},
    "budget_breach":      {"label": "Budget breach",           "run": lambda: _run_deterministic(_pick("BUDGET_BREACH"))},
    "wrong_merchant":     {"label": "Wrong merchant",           "run": lambda: _run_deterministic(_pick("WRONG_MERCHANT"))},
    "timing_miss":        {"label": "Delivery deadline missed",  "run": lambda: _run_deterministic(_pick("TIMING_MISS"))},
    "total_misdeclared":  {"label": "Total misdeclared",          "run": lambda: _run_deterministic(_pick("TOTAL_MISDECLARED"))},
    "dietary_violation":  {"label": "Dietary constraint violated", "run": lambda: _run_deterministic(_pick("CONSTRAINT_VIOLATION", verifier_catchable=True, abstain_expected=False))},
    "fooled_judge":       {"label": "Fooled Judge (floor enforcement, live)", "run": _run_fooled_judge},
    "semantic_caution":   {"label": "Semantic Caution (measured Stage 5 result)", "run": _run_semantic_caution},
}


@app.get("/api/scenarios")
def list_scenarios() -> JSONResponse:
    return JSONResponse([{"id": k, "label": v["label"]} for k, v in SCENARIOS.items()])


@app.get("/api/clear/{scenario_id}")
def run_scenario(scenario_id: str) -> JSONResponse:
    if scenario_id not in SCENARIOS:
        raise HTTPException(404, f"Unknown scenario {scenario_id!r}. See /api/scenarios.")
    try:
        result = SCENARIOS[scenario_id]["run"]()
    except Exception as exc:   # noqa: BLE001 — a demo endpoint must return an error, never a stack trace to the UI
        raise HTTPException(500, f"Scenario {scenario_id!r} failed: {type(exc).__name__}: {exc}") from exc
    return JSONResponse(result)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "corpus_records": str(len(_RECORDS))}


@app.get("/architecture", response_class=HTMLResponse)
def architecture_proof() -> str:
    """The architecture-proof console (the 8 scenarios above). The React app
    at web/ is the product demo; this stays reachable as its 'how it works'
    sidebar link, unbuilt and dependency-free."""
    from pathlib import Path
    return (Path(__file__).parent / "static" / "index.html").read_text(encoding="utf-8")


@app.get("/floorgate", response_class=HTMLResponse)
def floorgate() -> str:
    """The FloorGate one-page mechanism walkthrough, served from this app
    directly — not dependent on an externally-shared link's visibility
    setting. See README's 'Live demo' link."""
    from pathlib import Path
    return (Path(__file__).parent / "static" / "floorgate.html").read_text(encoding="utf-8")
