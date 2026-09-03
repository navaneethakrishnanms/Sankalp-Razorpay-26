"""
Settlement instruction — the terminal step of a clearing decision.

DELIBERATELY THIN. Stage 6 (exposure banding + the Razorpay test-mode rail) is
out of scope for this deadline; this module exists to give
`core.clearing.engine.ClearingOutcome` a real, hash-chained terminus rather
than stopping at an in-memory dataclass, and to document the one mapping that
actually matters — what HOLD means — honestly, before any rail exists to test
it against.

HOLD SEMANTICS (documented now, not deferred, because the README's credibility
claim about HOLD has to match code that actually exists)
-----------------------------------------------------------------------------
HOLD means the UPI Reserve Pay reservation is NOT converted to a debit.

  * Test mode (what this project has, and all it claims): a Razorpay order is
    created via the test-mode SDK and the capture API is deliberately never
    called. `emit()` below does not call Razorpay at all yet — it only
    produces the SettlementInstruction record. Wiring the actual test-mode
    API call is Stage 6 work, not done here.
  * Production (explicitly out of scope, never exercised, never claimed):
    the reservation already exists as part of the authorisation layer (the
    AP2-shaped mandate chain, in this project's positioning); SANKALP decides
    whether it converts to a debit. No production rail has been touched.

This file is the honest boundary: it is the last correct, tested thing this
project does before "actually move money" begins, and it stops exactly there.
"""

from __future__ import annotations

from core.clearing.engine import ClearingOutcome
from core.models.enums import Rail, SettlementAction
from core.models.settlement import SettlementInstruction, _compute_settlement_hash


def emit(outcome: ClearingOutcome, clearing_decision_hash: str, *, rail: Rail = Rail.RAZORPAY_TEST) -> SettlementInstruction:
    """
    Produce the hash-chained terminus of a clearing decision.

    Does not call any payment rail. That is Stage 6. This function's entire
    job is: given a decided outcome, produce the immutable record of what
    should happen next, correctly reason-coded, so a rail integration later
    has something real to act on rather than inventing the mapping under time
    pressure.
    """
    unbound = SettlementInstruction(
        clearing_decision_hash=clearing_decision_hash,
        action=outcome.action,
        rail=rail,
        reason_code=outcome.reason_code,
    )
    return unbound.model_copy(update={"hash": _compute_settlement_hash(unbound)})


def explain(action: SettlementAction) -> str:
    """Human-readable meaning of an action, for the console and for anyone
    reading a settlement record without the rest of this module's docstring."""
    return {
        SettlementAction.EXECUTE: "Proceed with debit capture. All admissible checks passed.",
        SettlementAction.HOLD: (
            "Keep the UPI Reserve Pay reservation; do not capture. In test mode, "
            "no capture call is made. See this module's docstring for the "
            "test/production distinction."
        ),
        SettlementAction.CLARIFY: "Route one bounded question to the user before deciding.",
        SettlementAction.ABORT: "Release the reservation; cancel the order.",
    }[action]
