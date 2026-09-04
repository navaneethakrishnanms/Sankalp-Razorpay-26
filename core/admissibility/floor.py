"""
Floor enforcement.

Each Obligation declares an admissibility_floor.  A verifier whose effective
declared basis falls below that floor is excluded: weight = 0, verdict does
not count.  Its output is preserved in ClearingDecision.verifier_outputs
for audit but plays no part in the aggregate verdict.

COMBINATION RULES (§5.2)
------------------------
1. Provenance chain → meet (weakest link).  [in lattice.py]
2. Single verifier's declared basis → meet.  [verifier_basis_class()]
3. Aggregate basis across surviving verifiers → join.  [aggregate_surviving_basis()]
   Only computed AFTER floor enforcement.

Rule 3 is a join, not another meet.  The aggregate basis class is the
*strongest* class among surviving verifiers, which correctly reflects that
if a REC-based verifier and an ATT-based verifier both survive, the
decision rests on ATT-quality evidence.

NON-PROMOTION — the key invariant
----------------------------------
Sub-floor items are excluded BEFORE join is called.  The join operation
only receives the survivor set.  Therefore:

    join([SELF, SIGN]) = SIGN         (below REC floor)
    → both excluded by apply_floor    (floor = REC)
    → join is never called
    → aggregate_surviving_basis returns None

Two sub-floor items, even joined, do not clear the floor.  The join is
never the mechanism for clearing — exclusion happens first.
"""

from __future__ import annotations

from core.models.enums import EvidenceClass
from core.models.verifier import VerifierOutput
from core.admissibility.lattice import meet, join


def verifier_basis_class(
    output: VerifierOutput,
    evidence_index: dict[str, EvidenceClass],
) -> EvidenceClass:
    """
    Compute the effective admissibility class of a verifier's declared basis.

    The verifier declares the IDs of evidence items it consulted.  We look up
    each item's admissibility_class and take the meet — the weakest link in
    the verifier's evidence chain.

    Edge cases:
    - Empty declared_basis → SELF.  A verifier that cites no evidence is
      treated as relying on nothing provable.  This makes a verifier that
      forgets to declare its basis unconditionally sub-floor at REC floors,
      which is the safe failure mode.
    - Unknown evidence ID → SELF.  Cannot verify provenance of unknown items;
      treated as if they are self-reports.  This makes evidence-ID spoofing
      structurally useless: a forged ID claiming ATT contributes SELF.
    """
    if not output.declared_basis:
        return EvidenceClass.SELF

    classes: list[EvidenceClass] = []
    for item_id in output.declared_basis:
        if item_id not in evidence_index:
            # Unknown ID → conservative SELF; no benefit from forgery.
            classes.append(EvidenceClass.SELF)
        else:
            classes.append(evidence_index[item_id])

    return meet(classes)


def meets_floor(basis: EvidenceClass, floor: EvidenceClass) -> bool:
    """Return True iff basis is at or above the floor."""
    return basis >= floor


def apply_floor(
    verifier_outputs: list[VerifierOutput],
    floor: EvidenceClass,
    evidence_index: dict[str, EvidenceClass],
) -> tuple[list[VerifierOutput], list[VerifierOutput]]:
    """
    Partition verifier outputs into (survivors, excluded).

    survivors: verifiers whose effective declared basis meets the floor.
    excluded:  verifiers whose effective declared basis falls below the floor.

    Excluded verifiers have weight = 0.  Their verdicts are preserved in
    ClearingDecision.verifier_outputs for the audit trail (so an auditor
    can see that a semantic verifier returned PASS and why it was ignored)
    but they do not participate in the aggregate verdict.

    This partition is the mechanism that enforces the non-promotion invariant:
    join is only ever called on the survivors.
    """
    survivors: list[VerifierOutput] = []
    excluded:  list[VerifierOutput] = []

    for output in verifier_outputs:
        basis = verifier_basis_class(output, evidence_index)
        if meets_floor(basis, floor):
            survivors.append(output)
        else:
            excluded.append(output)

    return survivors, excluded


def aggregate_surviving_basis(
    survivors: list[VerifierOutput],
    evidence_index: dict[str, EvidenceClass],
) -> EvidenceClass | None:
    """
    Compute the aggregate basis class across surviving (post-floor) verifiers.

    This is join over each survivor's basis class.  Represents the strongest
    quality of evidence that contributed to the clearing decision.

    Returns None if survivors is empty — all verifiers were excluded by floor
    enforcement.  The caller (aggregator.py) must treat this as an ABSTAIN
    with no admissible basis, not as a pass.
    """
    if not survivors:
        return None

    classes = [verifier_basis_class(v, evidence_index) for v in survivors]
    return join(classes)
