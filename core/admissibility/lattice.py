"""
Admissibility lattice: meet, join, propagated_class.

The lattice is a total order on EvidenceClass:
    SELF(0) < SIGN(1) < WIT(2) < REC(3) < ATT(4)

Three named operations with distinct semantics — not interchangeable:

meet(classes)
    Infimum (weakest element).  Used in two contexts:
    1. Computing the effective class of a provenance chain (weakest link).
    2. Computing a single verifier's declared basis (weakest of its evidence).
    These are the same mathematical operation but different semantic roles.

join(classes)
    Supremum (strongest element).  Used to aggregate the *basis* across
    surviving verifiers, computed AFTER floor enforcement has excluded weak
    ones.  join is never called on a mix of sub-floor and above-floor items.

propagated_class(original, channel)
    meet([original, channel]).  Named separately so call sites are
    self-documenting: "ATT evidence travelling through a SELF channel."

NON-PROMOTION INVARIANT
-----------------------
join of a set of sub-floor items does not clear the floor.  This is
structural, not a policy decision:

    join([SELF, SIGN]) = SIGN
    meets_floor(SIGN, REC) = False

Two LLM verifiers consulting the same self-report and both returning PASS
contribute exactly zero weight to the clearing decision when the floor is
REC.  The invariant is enforced by ensuring join is called only on the
post-floor survivor set (see floor.py).

This is the architectural answer to "what stops your LLM from
hallucinating a PASS?" — it is structural, not prompt-engineered.
"""

from core.models.enums import EvidenceClass


def meet(classes: list[EvidenceClass]) -> EvidenceClass:
    """
    Weakest-link infimum over the given evidence classes.

    Raises ValueError on empty input: the meet of the empty set is
    undefined in this bounded lattice (there is no bottom element that
    is strictly weaker than SELF in a useful way).  Callers that
    construct evidence chains must ensure at least the origin class
    is always present.
    """
    if not classes:
        raise ValueError(
            "meet() requires at least one EvidenceClass.  "
            "The meet of the empty set is undefined in this lattice."
        )
    return EvidenceClass(min(int(c) for c in classes))


def join(classes: list[EvidenceClass]) -> EvidenceClass:
    """
    Strongest-survivor supremum over the given evidence classes.

    Must only be called on the post-floor survivor set (see floor.py).
    Raises ValueError on empty input: the aggregator must not call join
    if all verifiers were excluded — that is a distinct failure mode
    (no admissible basis, clearing engine must emit ABSTAIN).
    """
    if not classes:
        raise ValueError(
            "join() requires at least one EvidenceClass.  "
            "If all verifiers were excluded by floor enforcement, "
            "the aggregator must emit Verdict.ABSTAIN rather than calling join()."
        )
    return EvidenceClass(max(int(c) for c in classes))


def propagated_class(
    original: EvidenceClass,
    channel: EvidenceClass,
) -> EvidenceClass:
    """
    Effective class of evidence after transmission through a single channel.

    propagated_class(ATT, SELF) == SELF
        An ATT-class attestation rebroadcast via an agent self-report is
        now SELF-class.  The channel is the weakest link.

    propagated_class(SELF, ATT) == SELF
        A strong channel cannot promote weak evidence.

    This is meet([original, channel]) expressed as a named function so
    that call sites are self-documenting at a glance.
    """
    return meet([original, channel])
