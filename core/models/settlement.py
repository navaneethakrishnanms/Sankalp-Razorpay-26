"""
SettlementInstruction — the terminal object in the hash chain.

This is the instruction emitted to the payment rail.  Its hash anchors the
entire clearing chain:

    Obligation → EvidenceItems → ClearingDecision → SettlementInstruction

HOLD semantics (from the spec ruling)
--------------------------------------
HOLD means the UPI Reserve Pay reservation is not converted to a debit.
In test mode: a Razorpay order is created and the capture API is
deliberately not called.  In production (out of scope per §4): the
reservation already exists as part of the authorisation layer; SANKALP
decides whether it converts to a debit.

This is stated plainly here because the README credibility claim about HOLD
requires the code to match.  We have not run this against production rails.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

from core.models.enums import Rail, SettlementAction


def _canonical_json(data: object) -> str:
    return json.dumps(data, sort_keys=True, default=str, ensure_ascii=True)


class SettlementInstruction(BaseModel):
    model_config = {"frozen": True}

    id:                     str = Field(default_factory=lambda: str(uuid4()))
    clearing_decision_hash: str
    action:                 SettlementAction
    rail:                   Rail = Rail.RAZORPAY_TEST
    # reason_code: short machine-readable string.  Corresponds to corpus
    # violation classes (e.g. "QUANTITY_MISMATCH", "CONSTRAINT_VIOLATION").
    reason_code:            str
    emitted_at:             datetime = Field(
        default_factory=lambda: datetime.now(tz=timezone.utc)
    )
    hash:                   str = Field(default="")

    @model_validator(mode="after")
    def _validate_hash(self) -> "SettlementInstruction":
        if self.hash == "":
            return self
        expected = _compute_settlement_hash(self)
        if self.hash != expected:
            raise ValueError(
                f"SettlementInstruction hash integrity check failed.  "
                f"Stored: {self.hash!r}, Expected: {expected!r}."
            )
        return self


def _compute_settlement_hash(inst: SettlementInstruction) -> str:
    payload = inst.model_dump(mode="json", exclude={"hash"})
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
