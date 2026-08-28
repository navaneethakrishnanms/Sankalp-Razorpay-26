"""
Enumerations shared across all SANKALP models.

EvidenceClass implements a **total order** SELF < SIGN < WIT < REC < ATT.

ARCHITECTURE NOTE — total order vs. poset
------------------------------------------
The source specification (RAILS, arXiv 2606.08790) and our §5.2 observe
that WIT and REC rest on *different* trust assumptions and might better be
treated as incomparable in a partial order: a user's pre-debit confirmation
(WIT) is more authoritative than a merchant catalogue lookup (REC) when the
dispute is about intent, whereas a signed receipt (REC) is more authoritative
when the dispute is about what was delivered.

We keep a total order in v1 because a poset requires a lattice completion
step (computing meets and joins over an antichain) in every floor-enforcement
and aggregation call, which costs a day we do not have.  The consequence is
that REC is treated as strictly stronger than WIT, which is the *wrong*
answer in ambiguity-driven disputes.  This is a named, documented shortcut,
not an oversight.  A future v2 should replace IntEnum with a Hasse-diagram
representation and update floor.py's apply_floor accordingly.
"""

from enum import Enum, IntEnum


# StrEnum was added in Python 3.11.  We use the str,Enum mixin which gives
# identical behaviour on Python 3.10: enum values are plain strings and
# str(member) == member.value.  No logic changes required.
class _StrEnum(str, Enum):
    """Base for string enums compatible with Python >=3.10."""
    pass


class EvidenceClass(IntEnum):
    """
    Admissibility lattice.  Integer values define the total order; callers
    must use symbolic names, never raw integers.
    """
    SELF = 0   # agent's own self-report — weakest; zero epistemic weight
    SIGN = 1   # cryptographically signed by the acting agent (non-repudiation, not truth)
    WIT  = 2   # signed by a third party present to the action
               # Reachable only via explicit user pre-debit confirmation;
               # structurally absent in LOW and MODERATE exposure bands.
    REC  = 3   # signed receipt from a non-interested external system
               # (merchant catalogue, order confirmation API)
    ATT  = 4   # attestation from a hardened runner — strongest


class CriterionSource(_StrEnum):
    """
    Records how an AcceptanceCriterion came to exist.

    Enforcement consequences (implemented in core/clearing/aggregator.py):
      stated    → hard block on FAIL; the only source that can flip the
                  clearing decision to FAIL / ABORT.
      inferred  → contributes confidence reduction; accumulated failure weight
                  above a configured threshold routes clearing to CLARIFY,
                  never to FAIL.  A single inferred failure cannot block
                  settlement, nor can any number of inferred failures jointly.
      defaulted → logged for observability; never affects the outcome.

    Compiler metric: track stated-labelled-as-inferred and
    inferred-labelled-as-stated rates against ground truth in the eval
    harness.  An upward drift in stated-labelled-as-inferred means the
    compiler is labelling hard constraints as advisory to hide violations
    while keeping the headline catch rate clean.
    """
    stated    = "stated"
    inferred  = "inferred"
    defaulted = "defaulted"


class CriterionOperator(_StrEnum):
    """
    Operators supported in AcceptanceCriterion.

    'semantic' cannot be evaluated by the deterministic constraint verifier;
    it routes to the semantic verifier.  A criterion with operator=semantic
    and source=stated creates a hard dependency on the semantic path: if the
    semantic verifier ABSTAINs, the aggregator must not treat the criterion
    as satisfied.
    """
    eq       = "eq"
    neq      = "neq"
    lt       = "lt"
    lte      = "lte"
    gt       = "gt"
    gte      = "gte"
    contains = "contains"   # resolved list contains this element
    excludes = "excludes"   # resolved list must not contain this element
    in_set   = "in_set"     # value is a member of an allowed set (value is list)
    semantic = "semantic"   # cannot be reduced to a predicate; semantic verifier only


class Verdict(_StrEnum):
    PASS     = "PASS"
    FAIL     = "FAIL"
    ABSTAIN  = "ABSTAIN"    # verifier could not reach a conclusion
    DISPUTED = "DISPUTED"   # internal inconsistency detected between verifiers


class Finality(_StrEnum):
    PROVISIONAL = "PROVISIONAL"   # awaiting user confirmation (ELEVATED band)
    FINAL       = "FINAL"


class SettlementAction(_StrEnum):
    EXECUTE = "EXECUTE"   # proceed with debit capture
    HOLD    = "HOLD"      # keep UPI Reserve Pay reservation; do not capture
                          # In test mode: Razorpay order created, capture withheld.
                          # In production: maps to a UPI Reserve Pay reservation that
                          # is not converted to a debit until SANKALP re-evaluates.
    CLARIFY = "CLARIFY"   # route one bounded question to the user before deciding
    ABORT   = "ABORT"     # release reservation; cancel order


class ExposureBand(_StrEnum):
    LOW      = "LOW"       # deterministic verifiers only; target < 50 ms
    MODERATE = "MODERATE"  # deterministic + semantic verifier; target < 2 s
    ELEVATED = "ELEVATED"  # full mesh + user confirmation prompt; user-paced


class Rail(_StrEnum):
    RAZORPAY_TEST = "razorpay_test"
    # Production rails are explicitly out of scope per §4.
    # Adding a rail here without a corresponding implementation in
    # core/settlement/rails/ is a hard error.
