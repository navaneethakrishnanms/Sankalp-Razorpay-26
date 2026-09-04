"""
Aggregator — combines verifier outputs into a verdict.

THE ORDER IS THE MECHANISM
---------------------------
    1. apply_floor partitions verifiers into survivors and excluded.
    2. join runs over SURVIVORS ONLY.
    3. the weighted verdict is computed over SURVIVORS ONLY.

Step 1 must precede steps 2 and 3, and this is not a stylistic preference. If
join ran over the unfiltered set, `join([SELF, REC])` would return REC and a
self-report-based PASS would be laundered into a REC-quality basis — the
non-promotion invariant would be gone and floor enforcement would be theatre.

`aggregate()` therefore never receives an unfiltered list beyond its own
entrypoint, and `_JoinAudit` records every call so a test can assert join was
only ever handed survivors. That test exists because this is the one ordering in
the system that cannot be allowed to drift, and a comment does not stop a
refactor.

ENFORCEMENT BY CRITERION SOURCE (core/models/enums.py)
-------------------------------------------------------
  stated    FAIL -> hard block. The only source that can produce FAIL.
  inferred  FAIL -> confidence reduction; accumulated weight past a threshold
                    routes to CLARIFY. Never FAIL, alone or in any number.
  defaulted FAIL -> logged only. Never affects the outcome.
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal

from core.admissibility.floor import apply_floor, verifier_basis_class
from core.admissibility.lattice import join
from core.models.enums import CriterionSource, EvidenceClass, Verdict
from core.models.obligation import Obligation
from core.models.verifier import VerifierOutput

# Accumulated inferred-failure weight above this routes to CLARIFY.
INFERRED_CLARIFY_THRESHOLD = 1.0

# How much a surviving verifier's vote is worth by the quality of its evidence.
# SELF is present for completeness only — it cannot survive a REC floor.
BASIS_WEIGHT: dict[EvidenceClass, float] = {
    EvidenceClass.SELF: 0.0,
    EvidenceClass.SIGN: 0.25,
    EvidenceClass.WIT:  0.50,
    EvidenceClass.REC:  1.00,
    EvidenceClass.ATT:  1.50,
}


@dataclasses.dataclass
class JoinAudit:
    """Records what join was called with, so ordering can be asserted, not assumed."""
    calls: list[list[EvidenceClass]] = dataclasses.field(default_factory=list)


@dataclasses.dataclass(frozen=True)
class AggregateResult:
    verdict:            Verdict
    confidence:          float
    survivors:            list[VerifierOutput]
    excluded:              list[VerifierOutput]
    basis_class:            EvidenceClass | None
    aggregate_basis:         list[str]
    loss_estimate:            Decimal | None
    clarify:                   bool
    reason_code:                str
    inferred_failure_weight:     float
    join_audit:                   JoinAudit


def _criterion_index(obligation: Obligation) -> dict[str, CriterionSource]:
    return {c.id: c.source for c in obligation.acceptance_criteria}


def aggregate(
    obligation: Obligation,
    verifier_outputs: list[VerifierOutput],
    evidence_index: dict[str, EvidenceClass],
    *,
    criterion_sources: dict[str, CriterionSource] | None = None,
    enforce_floor: bool = True,
) -> AggregateResult:
    """
    Combine verifier outputs into a verdict.

    `enforce_floor=False` exists ONLY to compute the no-floor counterfactual in
    eval/stage5_harness.py — the measurement of what floor enforcement is worth.
    It must never be used in a real clearing path: with it off, a SELF-class
    PASS votes, which is precisely the failure the architecture exists to
    prevent. It is a measurement instrument, not a configuration option.
    """
    audit = JoinAudit()

    # ── 1. Partition FIRST. Nothing below sees the unfiltered list. ──────
    if enforce_floor:
        survivors, excluded = apply_floor(
            verifier_outputs, obligation.admissibility_floor, evidence_index
        )
    else:
        survivors, excluded = list(verifier_outputs), []

    # ── 2. join over SURVIVORS ONLY. ─────────────────────────────────────
    basis_class: EvidenceClass | None = None
    if survivors:
        survivor_classes = [verifier_basis_class(v, evidence_index) for v in survivors]
        audit.calls.append(survivor_classes)
        basis_class = join(survivor_classes)

    aggregate_basis = sorted({eid for v in survivors for eid in v.declared_basis})

    # ── 3. Weighted verdict over SURVIVORS ONLY. ─────────────────────────
    sources = criterion_sources if criterion_sources is not None else _criterion_index(obligation)

    stated_fail = False
    inferred_weight = 0.0
    any_abstain = False
    total_weight = 0.0
    loss = Decimal("0")
    has_loss = False

    for verifier in survivors:
        klass = verifier_basis_class(verifier, evidence_index)
        weight = verifier.confidence * BASIS_WEIGHT.get(klass, 0.0)
        total_weight += weight

        if verifier.verdict == Verdict.ABSTAIN:
            any_abstain = True
            continue
        if verifier.verdict != Verdict.FAIL:
            continue

        if verifier.loss_estimate is not None:
            loss += verifier.loss_estimate
            has_loss = True

        # A verifier that names no criterion is reporting an obligation-level
        # or structural failure (budget, scope, arithmetic) — always hard-blocking.
        cited = [sources.get(cid) for cid in verifier.declared_basis if cid in sources]
        relevant = [s for s in cited if s is not None]
        if not relevant:
            stated_fail = True
            continue
        if CriterionSource.stated in relevant:
            stated_fail = True
        elif CriterionSource.inferred in relevant:
            inferred_weight += weight
        # defaulted: logged only, never affects the outcome.

    if not survivors:
        # Every verifier was excluded. That is not a pass — it is the absence of
        # any admissible basis, and the caller must treat it as such.
        return AggregateResult(
            Verdict.ABSTAIN, 0.0, survivors, excluded, None, aggregate_basis,
            None, True, "NO_ADMISSIBLE_BASIS", 0.0, audit,
        )

    if stated_fail:
        verdict, clarify, reason = Verdict.FAIL, False, "STATED_CRITERION_FAILED"
        confidence = 1.0
    elif inferred_weight > INFERRED_CLARIFY_THRESHOLD:
        # Never FAIL from inferred alone, at any accumulated weight.
        verdict, clarify, reason = Verdict.PASS, True, "INFERRED_FAILURES_ACCUMULATED"
        confidence = max(0.0, 1.0 - inferred_weight / (total_weight or 1.0))
    elif any_abstain:
        verdict, clarify, reason = Verdict.ABSTAIN, True, "EVIDENCE_INSUFFICIENT"
        confidence = 0.0
    elif inferred_weight > 0:
        verdict, clarify, reason = Verdict.PASS, False, "INFERRED_FAILURE_NOTED"
        confidence = max(0.0, 1.0 - inferred_weight / (total_weight or 1.0))
    else:
        verdict, clarify, reason = Verdict.PASS, False, "ALL_CHECKS_PASSED"
        confidence = 1.0

    return AggregateResult(
        verdict=verdict,
        confidence=round(confidence, 4),
        survivors=survivors,
        excluded=excluded,
        basis_class=basis_class,
        aggregate_basis=aggregate_basis,
        loss_estimate=loss if has_loss else None,
        clarify=clarify,
        reason_code=reason,
        inferred_failure_weight=round(inferred_weight, 4),
        join_audit=audit,
    )
