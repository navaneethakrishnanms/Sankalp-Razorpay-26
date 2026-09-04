"""
Obligation and AcceptanceCriterion — the root of the hash chain.

IMMUTABILITY CONTRACT
---------------------
Both models are frozen (Pydantic v2 model_config frozen=True).  Any
"update" produces a new object.  The hash anchors the state at bind time;
anything downstream that references obligation_hash is anchored to a
specific, unmodifiable Obligation.

HASH LIFECYCLE
--------------
1. Compiler creates criteria; binder calls Obligation(**fields, hash="").
2. Binder computes hash via _compute_obligation_hash().
3. Binder calls obligation.model_copy(update={"hash": h}).
4. model_copy re-runs _validate_hash(), which verifies the hash matches.
5. The bound Obligation is the canonical, immutable record.

The empty string is the "not yet bound" sentinel.  It is the only value
that bypasses the hash check.  A bound obligation with hash="" cannot
exist — the binder always sets it before returning.

FIELD REGISTRY ENFORCEMENT
--------------------------
AcceptanceCriterion.field must be a path registered in core/models/fields.py.
The binder validates every criterion against the registry and raises
BindError on an unknown path.  This means the compiler can never produce
a criterion that silently ABSTAINs because the constraint verifier cannot
resolve its field.

INFERRED-CRITERION LABELLING METRIC
-------------------------------------
The eval harness tracks two rates against ground truth:
  stated-labelled-as-inferred: compiler hides a hard constraint as advisory.
  inferred-labelled-as-stated: compiler over-enforces an advisory criterion.
The first is the dangerous direction; a rising rate means the compiler is
gaming the headline catch rate.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4
from typing import Annotated, Any

from pydantic import BaseModel, Field, model_validator

from core.models.enums import CriterionOperator, CriterionSource, EvidenceClass


# ── Canonical serialisation ────────────────────────────────────────────────

def _canonical_json(data: object) -> str:
    """
    Deterministic JSON for hashing.  sort_keys ensures field order does not
    affect the hash.  default=str catches Decimal, datetime, and enums that
    Pydantic's mode='json' may not fully flatten.
    """
    return json.dumps(data, sort_keys=True, default=str, ensure_ascii=True)


# ── AcceptanceCriterion ────────────────────────────────────────────────────

class AcceptanceCriterion(BaseModel):
    """
    A single checkable predicate on the Cart.

    Fields
    ------
    field    : Registered path from core/models/fields.py.  The binder
               validates this; an unregistered path is a hard bind failure.
    operator : How to compare the resolved value against `value`.
    value    : The threshold.  For 'in_set', this is a list.  The constraint
               verifier casts to the field's registered python_type before
               comparison.
    source   : How the criterion was produced.  Determines enforcement:
                 stated    → hard block on FAIL
                 inferred  → band escalation; accumulates toward CLARIFY
                 defaulted → logged, never blocks
    confidence : Compiler's confidence that this criterion was correctly
                 extracted.  Advisory; does not affect enforcement level
                 (source does that).

    Enforcement semantics are implemented in core/clearing/aggregator.py,
    not here — the model is pure data.
    """
    model_config = {"frozen": True}

    id:         str = Field(default_factory=lambda: str(uuid4()))
    field:      str
    operator:   CriterionOperator
    value:      str | int | float | list[Any]
    source:     CriterionSource
    confidence: Annotated[float, Field(ge=0.0, le=1.0)]


# ── Supporting models ──────────────────────────────────────────────────────

class MerchantScope(BaseModel):
    """
    Constrains which merchant(s) may fulfil the obligation.

    If merchant_ids is empty and category is None, any merchant is acceptable.
    The constraint verifier checks merchant_ids membership when non-empty;
    category is used as a secondary filter.
    """
    model_config = {"frozen": True}

    merchant_ids: list[str] = Field(default_factory=list)
    category:     str | None = None


class DeliveryWindow(BaseModel):
    model_config = {"frozen": True}

    latest_by: datetime           # absolute deadline; constraint verifier compares
                                  # cart.fulfilment_eta <= latest_by
    tz:        str = "Asia/Kolkata"


# ── Obligation ─────────────────────────────────────────────────────────────

class Obligation(BaseModel):
    """
    Compiled, bound, immutable representation of a user's purchase intent.

    Everything downstream — evidence items, verifier outputs, clearing
    decisions, settlement instructions — carries obligation_hash, anchoring
    it to this specific, unmodifiable record.

    budget_ceiling
    --------------
    Optional.  None means the user stated no budget ceiling — not that the
    order is unconstrained.  An order under the mandate limit with no stated
    ceiling is CLEAN.  budget_ceiling is the stated user ceiling; it is
    always ≤ the mandate limit by definition, but SANKALP never reads the
    mandate limit — that is the authorisation layer's concern.

    prohibited
    ----------
    Verbatim ingredient/item names the user forbade.  The binder generates
    a corresponding AcceptanceCriterion(operator=excludes) for each entry.
    Both the raw prohibited list and the compiled criterion are stored so
    the audit trail shows what the user said and how it was interpreted.
    """
    model_config = {"frozen": True}

    id:                  str = Field(default_factory=lambda: str(uuid4()))
    raw_instruction:     str                       # verbatim user utterance; never modified
    user_id:             str
    acceptance_criteria: list[AcceptanceCriterion]
    prohibited:          list[str] = Field(default_factory=list)
    budget_ceiling:      Decimal | None = None     # None = no stated ceiling
    merchant_scope:      MerchantScope = Field(default_factory=MerchantScope)
    delivery_window:     DeliveryWindow | None = None
    admissibility_floor: EvidenceClass = EvidenceClass.REC  # config default, not magic number
    ambiguity_flags:     list[str] = Field(default_factory=list)
    created_at:          datetime = Field(
        default_factory=lambda: datetime.now(tz=timezone.utc)
    )
    hash:                str = Field(default="")   # "" = unbound sentinel

    @model_validator(mode="after")
    def _validate_hash(self) -> "Obligation":
        if self.hash == "":
            # Unbound: being constructed for the first time.
            # The binder will compute and stamp the hash via model_copy.
            return self
        expected = _compute_obligation_hash(self)
        if self.hash != expected:
            raise ValueError(
                f"Obligation hash integrity check failed.  "
                f"Stored: {self.hash!r}, Expected: {expected!r}.  "
                f"The obligation may have been tampered with after binding."
            )
        return self


# ── Hash computation ───────────────────────────────────────────────────────

def _compute_obligation_hash(obligation: Obligation) -> str:
    """
    SHA-256 over the canonical JSON of all fields except 'hash'.

    Excluding 'hash' from its own computation avoids the bootstrap cycle
    while allowing the validator to re-derive and verify the hash on every
    construction.
    """
    payload = obligation.model_dump(mode="json", exclude={"hash"})
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
