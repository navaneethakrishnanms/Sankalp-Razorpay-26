"""
Clearing engine — obligation + cart + evidence -> ClearingDecision -> SettlementInstruction.

This is where floor enforcement finally runs against real data. Until Stage 5 it
existed only in a unit test.

EVIDENCE ASSEMBLY
-----------------
The engine builds the evidence envelope, and the classes it assigns are the
whole ballgame:

  catalogue data      -> REC   (a non-interested external system)
  agent self-report   -> SELF  (a claim by the party being checked)

The deterministic verifiers read catalogue data and declare REC. The semantic
verifier reads whatever it is handed — and when that is an agent self-report, it
declares SELF, because core/verifiers/semantic.py sets declared_basis from the
evidence actually placed in the prompt rather than asking the model.

At a REC floor, that SELF-class verdict is partitioned out before aggregation.
The confident, wrong PASS is not outvoted; it is absent.
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal
from typing import Any

from core.clearing.aggregator import AggregateResult, aggregate
from core.models.cart import Cart
from core.models.enums import EvidenceClass, SettlementAction, Verdict
from core.models.evidence import EvidenceItem, bind_evidence, make_evidence_item
from core.models.obligation import Obligation
from core.models.verifier import VerifierOutput
from core.verifiers.constraint import CATALOGUE_EVIDENCE_ID

CATALOGUE_EMITTER = "merchant-catalogue"
AGENT_EMITTER = "shopping-agent"


@dataclasses.dataclass(frozen=True)
class EvidenceEnvelope:
    items: list[EvidenceItem]

    @property
    def index(self) -> dict[str, EvidenceClass]:
        return {item.id: item.admissibility_class for item in self.items}

    def of_class(self, klass: EvidenceClass) -> list[EvidenceItem]:
        return [i for i in self.items if i.admissibility_class == klass]


def build_evidence(
    cart: Cart,
    obligation_hash: str,
    *,
    self_report: dict[str, Any] | None = None,
) -> EvidenceEnvelope:
    """
    Assemble the evidence envelope.

    The catalogue item is REC because the merchant catalogue is a non-interested
    external system. The self-report is SELF because it is the acting agent's own
    account of its work — non-repudiation of a claim is not evidence the claim is
    true, and the class must reflect that or floor enforcement means nothing.

    The catalogue item's id is FIXED to CATALOGUE_EVIDENCE_ID (core/verifiers/
    constraint.py), not a random uuid. The deterministic verifiers declare that
    exact id as their basis (a Stage 3 evidence stand-in — the full per-item
    envelope is a later stage). If this id did not match, the constraint
    verifier's declared_basis would resolve to "unknown evidence" under
    core/admissibility/floor.py's rule, default to SELF class, and be silently
    EXCLUDED by its own REC floor — the deterministic FAIL would vanish instead
    of carrying. This was a real bug caught by the Stage 5 offline smoke test,
    not a hypothetical: see FAILURES.md.
    """
    catalogue_unbound = EvidenceItem(
        id=CATALOGUE_EVIDENCE_ID,
        payload={
            "merchant": cart.merchant.id,
            "items": [{"name": i.name, "quantity": i.quantity} for i in cart.items],
            "total": str(cart.total),
        },
        emitter=CATALOGUE_EMITTER,
        original_class=EvidenceClass.REC,
        provenance_chain=[],
        admissibility_class=EvidenceClass.REC,
        obligation_hash=obligation_hash,
    )
    items = [bind_evidence(catalogue_unbound)]
    if self_report is not None:
        items.append(bind_evidence(make_evidence_item(
            payload=dict(self_report),
            emitter=AGENT_EMITTER,
            original_class=EvidenceClass.SELF,
            obligation_hash=obligation_hash,
        )))
    return EvidenceEnvelope(items)


@dataclasses.dataclass(frozen=True)
class ClearingOutcome:
    aggregate:          AggregateResult
    action:              SettlementAction
    reason_code:          str
    verifier_outputs:      list[VerifierOutput]
    evidence:               EvidenceEnvelope
    loss_estimate:           Decimal | None


def decide_settlement(result: AggregateResult) -> SettlementAction:
    """
    Map an aggregate verdict to a settlement action.

    ABSTAIN routes to CLARIFY rather than EXECUTE: an unresolved question is not
    a pass. Asking one bounded question costs far less than settling a payment
    on evidence that did not answer it.
    """
    if result.verdict == Verdict.FAIL:
        return SettlementAction.ABORT
    if result.reason_code == "NO_ADMISSIBLE_BASIS":
        return SettlementAction.HOLD
    if result.clarify or result.verdict == Verdict.ABSTAIN:
        return SettlementAction.CLARIFY
    return SettlementAction.EXECUTE


def clear(
    obligation: Obligation,
    cart: Cart,
    verifier_outputs: list[VerifierOutput],
    evidence: EvidenceEnvelope,
    *,
    enforce_floor: bool = True,
) -> ClearingOutcome:
    """
    Run aggregation and settle.

    `enforce_floor=False` is the measurement instrument for the no-floor
    counterfactual only (see core/clearing/aggregator.aggregate). It must never
    be used in a real clearing path.
    """
    result = aggregate(
        obligation, verifier_outputs, evidence.index, enforce_floor=enforce_floor
    )
    return ClearingOutcome(
        aggregate=result,
        action=decide_settlement(result),
        reason_code=result.reason_code,
        verifier_outputs=verifier_outputs,
        evidence=evidence,
        loss_estimate=result.loss_estimate,
    )
