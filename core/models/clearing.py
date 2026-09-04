"""
ClearingDecision — the output of the clearing engine, immutable once signed.

SIGNATURE CLASS
---------------
signature is the clearing engine's own signature over this record.  Its
admissibility class is SIGN — non-repudiation evidence that this engine
produced this decision at this time.  It is NOT ATT.  Non-repudiation
proves the engine made the decision; it is not proof of the decision's
correctness.

This distinction matters for any auditor that tries to use the signature
as evidence of correctness: the signature is SIGN-class evidence and will
be below any REC floor, so it cannot be used to override a REC-based FAIL.

HASH CHAINING
-------------
hash is computed over all fields except itself (same pattern as Obligation
and EvidenceItem).  The chain:

    Obligation.hash
        ↓ obligation_hash in EvidenceItems
    EvidenceItem.hash (multiple)
        ↓ declared_basis in VerifierOutputs
    VerifierOutput (preserved in verifier_outputs)
        ↓ aggregate_basis in ClearingDecision
    ClearingDecision.hash
        ↓ clearing_decision_hash in SettlementInstruction
    SettlementInstruction.hash

scripts/verify_chain.py walks this chain end to end.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

from core.models.enums import EvidenceClass, Finality, Verdict
from core.models.verifier import VerifierOutput


def _canonical_json(data: object) -> str:
    return json.dumps(data, sort_keys=True, default=str, ensure_ascii=True)


class ClearingDecision(BaseModel):
    model_config = {"frozen": True}

    id:                  str = Field(default_factory=lambda: str(uuid4()))
    obligation_hash:     str
    performance_verdict: Verdict       # did the cart satisfy the obligation?
    policy_verdict:      Verdict       # does the cart comply with policy?
    fault_assignment:    str | None = None  # "agent" | "merchant" | "ambiguous" | None
    # aggregate_basis: evidence item IDs that the surviving (post-floor)
    # verifiers relied on.  Union of declared_basis across survivors.
    aggregate_basis:     list[str]
    # basis_class: join of surviving verifiers' basis classes (post-floor).
    # Represents the strongest quality of evidence in the decision.
    basis_class:         EvidenceClass
    confidence:          float = Field(ge=0.0, le=1.0)
    finality:            Finality = Finality.PROVISIONAL
    # verifier_outputs: ALL verifier outputs, including excluded ones.
    # Excluded verifiers are clearly identified in the audit trail.
    # Their presence here does not imply they contributed to the verdict.
    verifier_outputs:    list[VerifierOutput]
    # signature: SIGN-class evidence (non-repudiation, not correctness).
    # "" until the engine signs the decision.
    signature:           str = ""
    decided_at:          datetime = Field(
        default_factory=lambda: datetime.now(tz=timezone.utc)
    )
    hash:                str = Field(default="")

    @model_validator(mode="after")
    def _validate_hash(self) -> "ClearingDecision":
        if self.hash == "":
            return self
        expected = _compute_clearing_hash(self)
        if self.hash != expected:
            raise ValueError(
                f"ClearingDecision hash integrity check failed.  "
                f"Stored: {self.hash!r}, Expected: {expected!r}."
            )
        return self


def _compute_clearing_hash(decision: ClearingDecision) -> str:
    payload = decision.model_dump(mode="json", exclude={"hash"})
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
