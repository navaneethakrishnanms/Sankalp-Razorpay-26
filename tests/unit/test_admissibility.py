"""
Unit tests for core/admissibility/lattice.py and core/admissibility/floor.py.

These tests are the structural backbone of the admissibility system.  They
must all pass before any component that depends on admissibility is built.

CRITICAL TESTS
--------------
test_non_promotion_invariant
    Two sub-floor items do not clear the floor, even joined.  This is the
    architectural guarantee against LLM hallucination of a PASS verdict.

test_propagated_class_att_through_self
    §5.2 canonical: ATT evidence rebroadcast through a self-reporting channel
    is SELF.  Proves the channel-weakens rule holds at the function level.

TestFloorEnforcementFooledJudge
    The §5.2 "fooled judge" scenario, end-to-end:
      - Semantic LLM verifier reads agent self-report → returns PASS
      - Honestly declares basis as SELF-class evidence
      - Floor enforcement (floor=REC) excludes it
      - Constraint verifier's REC-based FAIL carries the decision
      - The excluded verifier's PASS verdict does not appear in survivors

    This test answers "what stops your LLM from hallucinating a pass?"
    with a structural guarantee: the LLM verifier is excluded by its
    own honest basis declaration, not by prompt engineering or outvoting.
"""

import pytest
from decimal import Decimal

from core.models.enums import EvidenceClass, Verdict
from core.models.verifier import VerifierOutput
from core.admissibility.lattice import meet, join, propagated_class
from core.admissibility.floor import (
    verifier_basis_class,
    meets_floor,
    apply_floor,
    aggregate_surviving_basis,
)

# Aliases for readability
SELF = EvidenceClass.SELF
SIGN = EvidenceClass.SIGN
WIT  = EvidenceClass.WIT
REC  = EvidenceClass.REC
ATT  = EvidenceClass.ATT


# ── meet ───────────────────────────────────────────────────────────────────

class TestMeet:
    def test_single_element_identity(self):
        for cls in EvidenceClass:
            assert meet([cls]) == cls

    def test_weakest_link_two(self):
        assert meet([ATT, SELF]) == SELF

    def test_weakest_link_five(self):
        assert meet([ATT, REC, WIT, SIGN, SELF]) == SELF

    def test_all_same(self):
        assert meet([REC, REC, REC]) == REC

    def test_adjacent_pair(self):
        assert meet([REC, ATT]) == REC

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="meet.*empty"):
            meet([])

    def test_order_independent(self):
        """meet must not depend on list order."""
        classes = [ATT, SELF, WIT, REC, SIGN]
        expected = SELF
        for _ in range(5):
            import random
            random.shuffle(classes)
            assert meet(classes) == expected


# ── join ───────────────────────────────────────────────────────────────────

class TestJoin:
    def test_single_element_identity(self):
        for cls in EvidenceClass:
            assert join([cls]) == cls

    def test_strongest_survivor_two(self):
        assert join([SELF, ATT]) == ATT

    def test_strongest_survivor_five(self):
        assert join([SELF, SIGN, WIT, REC, ATT]) == ATT

    def test_all_same(self):
        assert join([SIGN, SIGN]) == SIGN

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="join.*requires at least one"):
            join([])

    def test_order_independent(self):
        classes = [SELF, WIT, SIGN]
        expected = WIT
        for _ in range(5):
            import random
            random.shuffle(classes)
            assert join(classes) == expected


# ── meet and join are different operations ─────────────────────────────────

class TestMeetJoinDistinction:
    def test_meet_ne_join_when_mixed(self):
        classes = [SELF, REC]
        assert meet(classes) == SELF
        assert join(classes) == REC
        assert meet(classes) != join(classes)

    def test_meet_and_join_agree_on_singleton(self):
        for cls in EvidenceClass:
            assert meet([cls]) == join([cls])

    def test_meet_and_join_agree_on_homogeneous(self):
        for cls in EvidenceClass:
            assert meet([cls, cls]) == join([cls, cls]) == cls


# ── propagated_class ───────────────────────────────────────────────────────

class TestPropagatedClass:
    def test_att_through_self_is_self(self):
        """
        §5.2 canonical example.

        An ATT-class attestation rebroadcast via a self-reporting channel
        degrades to SELF.  Channels are the weakest link; a strong original
        cannot survive a weak channel.
        """
        assert propagated_class(ATT, SELF) == SELF

    def test_self_through_att_is_still_self(self):
        """
        A strong channel cannot promote weak evidence.
        SELF evidence reaching us via an ATT channel is still SELF.
        """
        assert propagated_class(SELF, ATT) == SELF

    def test_att_through_att_stays_att(self):
        assert propagated_class(ATT, ATT) == ATT

    def test_rec_through_sign_gives_sign(self):
        """REC evidence arriving via a SIGN channel is SIGN."""
        assert propagated_class(REC, SIGN) == SIGN

    def test_identity_for_same_class(self):
        for cls in EvidenceClass:
            assert propagated_class(cls, cls) == cls

    def test_propagated_class_equals_meet_of_two(self):
        """propagated_class is exactly meet([original, channel])."""
        for orig in EvidenceClass:
            for chan in EvidenceClass:
                assert propagated_class(orig, chan) == meet([orig, chan])


# ── meets_floor ────────────────────────────────────────────────────────────

class TestMeetsFloor:
    def test_equal_meets_floor(self):
        for cls in EvidenceClass:
            assert meets_floor(cls, cls) is True

    def test_above_meets_floor(self):
        assert meets_floor(ATT, REC) is True
        assert meets_floor(REC, SIGN) is True
        assert meets_floor(WIT, SELF) is True

    def test_below_does_not_meet_floor(self):
        assert meets_floor(SELF, REC) is False
        assert meets_floor(SIGN, REC) is False
        assert meets_floor(WIT, ATT) is False

    def test_everything_meets_self_floor(self):
        for cls in EvidenceClass:
            assert meets_floor(cls, SELF) is True

    def test_only_att_meets_att_floor(self):
        for cls in EvidenceClass:
            result = meets_floor(cls, ATT)
            assert result == (cls == ATT)


# ── Non-promotion invariant ────────────────────────────────────────────────

class TestNonPromotionInvariant:
    """
    Core architectural invariant: no combination of sub-floor items clears
    the floor.  Two LLM verifiers consulting the same self-report and both
    returning PASS do not contribute to the clearing decision when the floor
    is REC.

    This section tests the invariant at three levels:
      1. Pure lattice level (join of sub-floor classes does not meet floor)
      2. apply_floor level (both are excluded before join)
      3. aggregate_surviving_basis level (returns None, not a class)
    """

    def test_join_of_self_sign_does_not_meet_rec_floor(self):
        combined = join([SELF, SIGN])
        assert combined == SIGN
        assert not meets_floor(combined, REC)

    def test_join_of_three_self_does_not_meet_rec_floor(self):
        combined = join([SELF, SELF, SELF])
        assert not meets_floor(combined, REC)

    def test_join_of_self_wit_does_not_meet_rec_floor(self):
        """WIT < REC in our total order (named shortcut documented in enums.py)."""
        combined = join([SELF, WIT])
        assert combined == WIT
        assert not meets_floor(combined, REC)

    def test_apply_floor_excludes_both_subfloor_verifiers(self):
        evidence_index = {
            "ev-a": SELF,
            "ev-b": SIGN,
        }
        v1 = VerifierOutput(
            role="semantic",
            verdict=Verdict.PASS,
            confidence=0.9,
            declared_basis=["ev-a"],
        )
        v2 = VerifierOutput(
            role="semantic_2",
            verdict=Verdict.PASS,
            confidence=0.8,
            declared_basis=["ev-b"],
        )
        survivors, excluded = apply_floor([v1, v2], REC, evidence_index)
        assert len(survivors) == 0, "Both verifiers should be excluded at REC floor"
        assert len(excluded) == 2

    def test_aggregate_basis_none_when_all_excluded(self):
        """apply_floor that excludes all verifiers → aggregate_surviving_basis returns None."""
        basis = aggregate_surviving_basis([], {})
        assert basis is None

    def test_non_promotion_end_to_end(self):
        """
        Full pipeline: two SELF-class verifiers returning PASS are both
        excluded at a REC floor.  aggregate_surviving_basis returns None.
        The aggregate verdict must be ABSTAIN, not PASS.
        """
        evidence_index = {"ev1": SELF, "ev2": SELF}
        v1 = VerifierOutput(
            role="semantic",
            verdict=Verdict.PASS,
            confidence=1.0,
            declared_basis=["ev1"],
        )
        v2 = VerifierOutput(
            role="semantic_2",
            verdict=Verdict.PASS,
            confidence=1.0,
            declared_basis=["ev2"],
        )
        survivors, excluded = apply_floor([v1, v2], REC, evidence_index)
        basis = aggregate_surviving_basis(survivors, evidence_index)
        assert basis is None  # caller must emit ABSTAIN


# ── The fooled-judge scenario (§5.2) ──────────────────────────────────────

class TestFloorEnforcementFooledJudge:
    """
    §5.2 canonical scenario — the most persuasive test in this project.

    Setup:
      - evidence_index contains two items:
          "ev-self-report"  → SELF class (the agent's own claim the order is correct)
          "ev-catalogue"    → REC class  (the merchant's signed catalogue)

      - The semantic LLM verifier reads the self-report and returns PASS.
        It honestly declares its basis as "ev-self-report".

      - The constraint verifier reads the catalogue and detects a quantity
        violation.  It returns FAIL with basis "ev-catalogue".

      - The obligation floor is REC (standard for food orders).

    Expected outcome:
      - Fooled semantic verifier → excluded (SELF < REC floor).
      - Constraint verifier → survives (REC ≥ REC floor).
      - Aggregate basis = REC (join over one survivor).
      - The PASS verdict from the semantic verifier does NOT appear in survivors.
      - The FAIL verdict from the constraint verifier DOES appear in survivors.
      - The clearing engine's aggregate verdict must therefore be FAIL.

    This answers "what stops your LLM from hallucinating a pass?" with
    a structural guarantee: the LLM verifier is excluded by its own
    honest basis declaration, not by outvoting, prompt engineering, or
    temperature settings.
    """

    def setup_method(self):
        self.evidence_index = {
            "ev-self-report": SELF,
            "ev-catalogue":   REC,
        }
        self.fooled_semantic = VerifierOutput(
            role="semantic",
            verdict=Verdict.PASS,
            confidence=0.85,
            declared_basis=["ev-self-report"],
            loss_estimate=None,   # semantic verifier never returns a number
            reasoning=(
                "The agent reports the order is correct.  "
                "Based on this self-report, the order appears to match the intent."
            ),
        )
        self.honest_constraint = VerifierOutput(
            role="constraint",
            verdict=Verdict.FAIL,
            confidence=1.0,
            declared_basis=["ev-catalogue"],
            loss_estimate=Decimal("800.00"),
            reasoning="quantity_sum=4 does not satisfy gte 8 (criterion: 8 people).",
        )

    def test_fooled_verifier_is_excluded(self):
        survivors, excluded = apply_floor(
            [self.fooled_semantic, self.honest_constraint],
            REC,
            self.evidence_index,
        )
        assert self.fooled_semantic in excluded
        assert self.fooled_semantic not in survivors

    def test_constraint_verifier_survives(self):
        survivors, excluded = apply_floor(
            [self.fooled_semantic, self.honest_constraint],
            REC,
            self.evidence_index,
        )
        assert self.honest_constraint in survivors
        assert self.honest_constraint not in excluded

    def test_aggregate_basis_is_rec(self):
        survivors, _ = apply_floor(
            [self.fooled_semantic, self.honest_constraint],
            REC,
            self.evidence_index,
        )
        basis = aggregate_surviving_basis(survivors, self.evidence_index)
        assert basis == REC

    def test_pass_verdict_absent_from_survivors(self):
        """The LLM's PASS is structurally absent — not outvoted, absent."""
        survivors, _ = apply_floor(
            [self.fooled_semantic, self.honest_constraint],
            REC,
            self.evidence_index,
        )
        survivor_verdicts = {v.verdict for v in survivors}
        assert Verdict.PASS not in survivor_verdicts
        assert Verdict.FAIL in survivor_verdicts

    def test_excluded_verifier_basis_class_is_self(self):
        """Verify the basis computation that triggers exclusion."""
        basis = verifier_basis_class(self.fooled_semantic, self.evidence_index)
        assert basis == SELF

    def test_surviving_verifier_basis_class_is_rec(self):
        basis = verifier_basis_class(self.honest_constraint, self.evidence_index)
        assert basis == REC

    def test_high_confidence_does_not_override_floor(self):
        """
        Confidence = 0.99 on the fooled verifier must not save it.
        Floor enforcement is binary; confidence is irrelevant to exclusion.
        """
        high_confidence_fooled = VerifierOutput(
            role="semantic",
            verdict=Verdict.PASS,
            confidence=0.99,       # very confident — still excluded
            declared_basis=["ev-self-report"],
        )
        survivors, excluded = apply_floor(
            [high_confidence_fooled, self.honest_constraint],
            REC,
            self.evidence_index,
        )
        assert high_confidence_fooled in excluded


# ── verifier_basis_class edge cases ───────────────────────────────────────

class TestVerifierBasisClass:
    def test_empty_declared_basis_is_self(self):
        v = VerifierOutput(
            role="test",
            verdict=Verdict.ABSTAIN,
            confidence=0.0,
            declared_basis=[],
        )
        assert verifier_basis_class(v, {}) == SELF

    def test_unknown_evidence_id_is_self(self):
        """Spoofing a non-existent ATT-class item ID gives SELF, not ATT."""
        v = VerifierOutput(
            role="test",
            verdict=Verdict.PASS,
            confidence=0.9,
            declared_basis=["fabricated-att-id"],
        )
        assert verifier_basis_class(v, {}) == SELF

    def test_mix_of_known_and_unknown_takes_weakest(self):
        """
        If the verifier cites one real ATT item and one unknown ID,
        the unknown ID degrades to SELF, and meet(ATT, SELF) = SELF.
        ID spoofing cannot elevate the basis class.
        """
        v = VerifierOutput(
            role="test",
            verdict=Verdict.PASS,
            confidence=0.8,
            declared_basis=["known-att", "spoofed-att"],
        )
        index = {"known-att": ATT}
        assert verifier_basis_class(v, index) == SELF

    def test_multiple_known_takes_meet(self):
        v = VerifierOutput(
            role="test",
            verdict=Verdict.PASS,
            confidence=0.8,
            declared_basis=["ev-att", "ev-rec", "ev-sign"],
        )
        index = {"ev-att": ATT, "ev-rec": REC, "ev-sign": SIGN}
        # meet(ATT, REC, SIGN) = SIGN
        assert verifier_basis_class(v, index) == SIGN

    def test_single_att_item(self):
        v = VerifierOutput(
            role="receipt",
            verdict=Verdict.PASS,
            confidence=1.0,
            declared_basis=["ev-catalogue"],
        )
        index = {"ev-catalogue": ATT}
        assert verifier_basis_class(v, index) == ATT


# ── aggregate_surviving_basis ──────────────────────────────────────────────

class TestAggregateSurvivingBasis:
    def test_none_on_empty_survivors(self):
        assert aggregate_surviving_basis([], {}) is None

    def test_single_survivor_returns_its_class(self):
        v = VerifierOutput(
            role="constraint",
            verdict=Verdict.FAIL,
            confidence=1.0,
            declared_basis=["ev-rec"],
        )
        index = {"ev-rec": REC}
        basis = aggregate_surviving_basis([v], index)
        assert basis == REC

    def test_join_across_multiple_survivors(self):
        v_rec = VerifierOutput(
            role="constraint",
            verdict=Verdict.PASS,
            confidence=1.0,
            declared_basis=["ev-rec"],
        )
        v_att = VerifierOutput(
            role="receipt",
            verdict=Verdict.PASS,
            confidence=1.0,
            declared_basis=["ev-att"],
        )
        index = {"ev-rec": REC, "ev-att": ATT}
        basis = aggregate_surviving_basis([v_rec, v_att], index)
        # join(REC, ATT) = ATT
        assert basis == ATT
