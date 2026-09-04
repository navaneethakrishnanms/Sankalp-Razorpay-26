"""
VerifierOutput — what each verifier returns to the aggregator.

CONTRACT: declared_basis is mandatory
--------------------------------------
Every verifier must list the evidence item IDs it consulted.  This is not
optional hygiene — it is the mechanism by which floor enforcement works.
Floor enforcement looks up each item's admissibility_class, takes the meet,
and zeroes the verifier's weight if the result falls below the obligation's
floor.

A verifier that does not declare its basis gets SELF class (see floor.py
verifier_basis_class), which will be below the floor at REC or higher.  So
forgetting to declare basis means the verifier is structurally excluded,
not leniently included.  This is the correct failure mode.

loss_estimate
-------------
Optional[Decimal].  The semantic verifier MUST always return None here.
This is enforced by a test in tests/unit/test_models.py and documented in
core/verifiers/semantic.py.  A verifier that cannot compute a number without
authoring one must return None, not a guess.  The aggregator ignores nulls
when computing aggregate loss.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

from pydantic import BaseModel, Field

from core.models.enums import Verdict


class VerifierOutput(BaseModel):
    model_config = {"frozen": True}

    id:             str = Field(default_factory=lambda: str(uuid4()))
    role:           str        # "constraint" | "receipt" | "semantic"; extensible
    verdict:        Verdict
    confidence:     float = Field(ge=0.0, le=1.0)
    # declared_basis: the contract.  Must be present; may be empty list only
    # if the verifier genuinely consulted no evidence (which gives SELF class).
    declared_basis: list[str]  # evidence item IDs — no default; always required
    # loss_estimate: None for any verifier that cannot compute it without
    # authoring a number.  The semantic verifier must unconditionally return None.
    loss_estimate:  Decimal | None = None
    # reasoning is preserved for the audit trail.  It must not affect the
    # verdict — the verdict is computed deterministically by verifier logic.
    reasoning:      str = ""
