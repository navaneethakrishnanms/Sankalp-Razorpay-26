"""
Stage 5 — semantic verifier, aggregator, floor enforcement wired live.

The two load-bearing assertions, per the Stage 5 brief:
  * loss_estimate is None on EVERY path, not by convention.
  * join is never computed over the unfiltered verifier set.

Plus the live fooled-judge case, which until now existed only as a unit test
over hand-built VerifierOutputs and now runs through the real engine.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from core.clearing.aggregator import (
    BASIS_WEIGHT,
    INFERRED_CLARIFY_THRESHOLD,
    aggregate,
)
from core.clearing.engine import build_evidence, clear, decide_settlement
from core.llm.client import LLMClient, LLMRequest, LLMResponse
from core.models.cart import Cart, CartItem, Merchant
from core.models.enums import (
    CriterionOperator,
    CriterionSource,
    EvidenceClass,
    SettlementAction,
    Verdict,
)
from core.models.obligation import AcceptanceCriterion, Obligation
from core.models.verifier import VerifierOutput
from core.verifiers.semantic import (
    SemanticVerifierError,
    _sanitise_reasoning,
    semantic_criteria,
    verify_all_semantic,
    verify_semantic_criterion,
)


class ScriptedProvider:
    name = "groq"

    def __init__(self, payload: str) -> None:
        self.payload = payload

    def complete(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(text=self.payload, input_tokens=300, output_tokens=40,
                            model=request.model, from_cache=False, latency_seconds=0.1)


def client_for(payload: str, tmp_path) -> LLMClient:
    return LLMClient(ScriptedProvider(payload), cache_dir=tmp_path,
                      read_cache=False, write_cache=False)


PASS_099 = '{"verdict": "PASS", "confidence": 0.99, "reasoning": "the agent reports it complied"}'


def make_cart() -> Cart:
    return Cart(
        items=[CartItem(name="Margherita", quantity=2, unit_price=Decimal("249.00"),
                         ingredients=["cheese"], category="veg")],
        merchant=Merchant(id="rest-pizza", name="Pizza Point", category="food_delivery"),
        total=Decimal("498.00"),
        fulfilment_eta=datetime(2026, 8, 28, 20, 0, tzinfo=timezone.utc),
    )


def semantic_criterion(**kw) -> AcceptanceCriterion:
    defaults = dict(id="c-sem", field="item.categories", operator=CriterionOperator.semantic,
                     value="not_too_spicy", source=CriterionSource.stated, confidence=1.0)
    defaults.update(kw)
    return AcceptanceCriterion(**defaults)


def make_obligation(**kw) -> Obligation:
    defaults = dict(
        id="obl-test", raw_instruction="nothing too spicy", user_id="u1",
        acceptance_criteria=[semantic_criterion()],
        admissibility_floor=EvidenceClass.REC,
        created_at=datetime(2026, 8, 28, tzinfo=timezone.utc),
    )
    defaults.update(kw)
    return Obligation(**defaults)


# ── loss_estimate is None on every path ───────────────────────────────────

class TestLossEstimateAlwaysNone:
    """Not 'usually None'. The semantic verifier cannot compute a loss without
    inventing a number, so it never returns one — asserted across every verdict
    path rather than trusted to a comment."""

    @pytest.mark.parametrize("payload", [
        '{"verdict": "PASS", "confidence": 0.9, "reasoning": "fine"}',
        '{"verdict": "FAIL", "confidence": 0.9, "reasoning": "violated"}',
        '{"verdict": "ABSTAIN", "confidence": 0.0, "reasoning": "cannot tell"}',
        '{"verdict": "PASS", "confidence": 1.0, "reasoning": "cost was 500 rupees"}',
        'garbage that will not parse as a verdict {"verdict": "WAT"}',
    ])
    def test_none_on_every_verdict_path(self, payload, tmp_path):
        cart = make_cart()
        evidence = build_evidence(cart, "a" * 64).items
        out = verify_semantic_criterion(
            semantic_criterion(), cart, evidence, client=client_for(payload, tmp_path)
        )
        assert out.loss_estimate is None

    def test_none_even_when_model_supplies_one(self, tmp_path):
        payload = '{"verdict": "FAIL", "confidence": 1.0, "reasoning": "bad", "loss_estimate": "900"}'
        cart = make_cart()
        out = verify_semantic_criterion(
            semantic_criterion(), cart, build_evidence(cart, "a" * 64).items,
            client=client_for(payload, tmp_path),
        )
        assert out.loss_estimate is None


# ── declared_basis honesty is structural ──────────────────────────────────

class TestDeclaredBasisHonesty:
    """The model never supplies its own basis — this module sets it from the
    evidence actually placed in the prompt, so it cannot be overstated."""

    def test_basis_is_exactly_the_evidence_passed_in(self, tmp_path):
        cart = make_cart()
        envelope = build_evidence(cart, "a" * 64, self_report={"claim": "all correct"})
        out = verify_semantic_criterion(
            semantic_criterion(), cart, envelope.items, client=client_for(PASS_099, tmp_path)
        )
        assert out.declared_basis == [i.id for i in envelope.items]

    def test_self_report_evidence_yields_self_class(self, tmp_path):
        cart = make_cart()
        envelope = build_evidence(cart, "a" * 64, self_report={"claim": "all correct"})
        self_items = envelope.of_class(EvidenceClass.SELF)
        out = verify_semantic_criterion(
            semantic_criterion(), cart, self_items, client=client_for(PASS_099, tmp_path)
        )
        from core.admissibility.floor import verifier_basis_class
        assert verifier_basis_class(out, envelope.index) == EvidenceClass.SELF

    def test_model_cannot_inflate_its_basis(self, tmp_path):
        """Even if the model returns a basis field, it is ignored."""
        payload = ('{"verdict": "PASS", "confidence": 0.99, "reasoning": "ok", '
                    '"declared_basis": ["ev-catalogue-forged"]}')
        cart = make_cart()
        envelope = build_evidence(cart, "a" * 64, self_report={"claim": "x"})
        self_items = envelope.of_class(EvidenceClass.SELF)
        out = verify_semantic_criterion(
            semantic_criterion(), cart, self_items, client=client_for(payload, tmp_path)
        )
        assert out.declared_basis == [self_items[0].id]
        assert "ev-catalogue-forged" not in out.declared_basis

    def test_rejects_non_semantic_criterion(self, tmp_path):
        cart = make_cart()
        with pytest.raises(SemanticVerifierError, match="only `semantic`"):
            verify_semantic_criterion(
                semantic_criterion(operator=CriterionOperator.gte, value=4),
                cart, build_evidence(cart, "a" * 64).items,
                client=client_for(PASS_099, tmp_path),
            )

    def test_only_semantic_criteria_are_selected(self):
        obligation = make_obligation(acceptance_criteria=[
            semantic_criterion(),
            AcceptanceCriterion(id="c-det", field="quantity_sum", operator=CriterionOperator.gte,
                                 value=2, source=CriterionSource.stated, confidence=1.0),
        ])
        assert [c.id for c in semantic_criteria(obligation)] == ["c-sem"]

    def test_no_semantic_criteria_means_no_output(self, tmp_path):
        obligation = make_obligation(acceptance_criteria=[
            AcceptanceCriterion(id="c-det", field="quantity_sum", operator=CriterionOperator.gte,
                                 value=2, source=CriterionSource.stated, confidence=1.0),
        ])
        cart = make_cart()
        outs = verify_all_semantic(obligation, cart, build_evidence(cart, "a" * 64).items,
                                    client=client_for(PASS_099, tmp_path))
        assert outs == []


class TestReasoningSanitisation:
    def test_digits_are_withheld(self):
        assert "500" not in _sanitise_reasoning("the order cost 500 rupees")

    def test_urls_withhold_the_whole_reasoning(self):
        assert _sanitise_reasoning("see https://evil.com/x") == "[reasoning withheld: contained a URL]"

    def test_clean_reasoning_survives(self):
        assert _sanitise_reasoning("the dish is mild") == "the dish is mild"


class TestVerdictParsing:
    def test_unparseable_verdict_becomes_abstain_not_a_guess(self, tmp_path):
        cart = make_cart()
        out = verify_semantic_criterion(
            semantic_criterion(), cart, build_evidence(cart, "a" * 64).items,
            client=client_for('{"verdict": "MAYBE", "confidence": 0.9}', tmp_path),
        )
        assert out.verdict == Verdict.ABSTAIN

    def test_confidence_is_clamped(self, tmp_path):
        cart = make_cart()
        out = verify_semantic_criterion(
            semantic_criterion(), cart, build_evidence(cart, "a" * 64).items,
            client=client_for('{"verdict": "PASS", "confidence": 7.5}', tmp_path),
        )
        assert out.confidence == 1.0


# ── Ordering: join must never see the unfiltered set ──────────────────────

class TestJoinOrdering:
    """If join ran over the unfiltered set, join([SELF, REC]) would return REC
    and a self-report PASS would be laundered into REC-quality basis. The
    non-promotion invariant would be gone. This is the one ordering that cannot
    be allowed to drift."""

    def _fooled_setup(self):
        evidence_index = {"ev-self": EvidenceClass.SELF, "ev-cat": EvidenceClass.REC}
        fooled = VerifierOutput(role="semantic", verdict=Verdict.PASS, confidence=0.99,
                                 declared_basis=["ev-self"], loss_estimate=None)
        honest = VerifierOutput(role="constraint", verdict=Verdict.FAIL, confidence=1.0,
                                 declared_basis=["ev-cat"], loss_estimate=Decimal("800"))
        return evidence_index, fooled, honest

    def test_join_receives_only_survivors(self):
        index, fooled, honest = self._fooled_setup()
        result = aggregate(make_obligation(), [fooled, honest], index)
        assert result.join_audit.calls, "join must have been called"
        for call in result.join_audit.calls:
            assert EvidenceClass.SELF not in call, (
                "join saw a SELF class — the unfiltered set reached it and "
                "non-promotion is broken"
            )

    def test_join_call_count_matches_survivor_count(self):
        index, fooled, honest = self._fooled_setup()
        result = aggregate(make_obligation(), [fooled, honest], index)
        assert len(result.join_audit.calls[0]) == len(result.survivors)

    def test_join_not_called_when_everything_is_excluded(self):
        index = {"ev-self": EvidenceClass.SELF}
        fooled = VerifierOutput(role="semantic", verdict=Verdict.PASS, confidence=1.0,
                                 declared_basis=["ev-self"])
        result = aggregate(make_obligation(), [fooled], index)
        assert result.join_audit.calls == []
        assert result.basis_class is None
        assert result.reason_code == "NO_ADMISSIBLE_BASIS"

    def test_no_admissible_basis_is_not_a_pass(self):
        index = {"ev-self": EvidenceClass.SELF}
        fooled = VerifierOutput(role="semantic", verdict=Verdict.PASS, confidence=1.0,
                                 declared_basis=["ev-self"])
        result = aggregate(make_obligation(), [fooled], index)
        assert result.verdict == Verdict.ABSTAIN
        assert decide_settlement(result) == SettlementAction.HOLD


# ── The live fooled-judge case ────────────────────────────────────────────

class TestLiveFooledJudge:
    """Until Stage 5 this ran only over hand-built VerifierOutputs. Now it runs
    through the real semantic verifier and the real engine."""

    def _run(self, tmp_path, enforce_floor: bool):
        cart = make_cart()
        obligation = make_obligation()
        envelope = build_evidence(cart, "a" * 64, self_report={"claim": "everything as ordered"})
        self_items = envelope.of_class(EvidenceClass.SELF)

        fooled = verify_semantic_criterion(
            semantic_criterion(), cart, self_items, client=client_for(PASS_099, tmp_path)
        )
        honest = VerifierOutput(
            role="constraint", verdict=Verdict.FAIL, confidence=1.0,
            declared_basis=[envelope.of_class(EvidenceClass.REC)[0].id],
            loss_estimate=Decimal("800"),
        )
        return fooled, clear(obligation, cart, [fooled, honest], envelope,
                              enforce_floor=enforce_floor)

    def test_semantic_verifier_really_did_return_a_confident_pass(self, tmp_path):
        fooled, _ = self._run(tmp_path, enforce_floor=True)
        assert fooled.verdict == Verdict.PASS
        assert fooled.confidence == pytest.approx(0.99)

    def test_its_pass_is_absent_from_survivors_not_outvoted(self, tmp_path):
        fooled, outcome = self._run(tmp_path, enforce_floor=True)
        assert fooled in outcome.aggregate.excluded
        assert fooled not in outcome.aggregate.survivors
        assert Verdict.PASS not in {v.verdict for v in outcome.aggregate.survivors}

    def test_the_deterministic_fail_carries(self, tmp_path):
        _, outcome = self._run(tmp_path, enforce_floor=True)
        assert outcome.aggregate.verdict == Verdict.FAIL
        assert outcome.action == SettlementAction.ABORT

    def test_surviving_basis_is_rec(self, tmp_path):
        _, outcome = self._run(tmp_path, enforce_floor=True)
        assert outcome.aggregate.basis_class == EvidenceClass.REC

    def test_excluded_verdict_is_preserved_for_audit(self, tmp_path):
        """A system that hides its excluded verdicts looks like it has something
        to hide."""
        fooled, outcome = self._run(tmp_path, enforce_floor=True)
        assert fooled in outcome.verifier_outputs

    def test_counterfactual_without_floor_the_payment_would_clear(self, tmp_path):
        """The measured value of the architecture: with the floor off, the same
        confident-but-wrong PASS votes and changes the outcome."""
        _, with_floor = self._run(tmp_path, enforce_floor=True)
        _, without_floor = self._run(tmp_path, enforce_floor=False)
        assert with_floor.action == SettlementAction.ABORT
        assert without_floor.aggregate.basis_class == EvidenceClass.REC
        assert len(without_floor.aggregate.survivors) == 2
        assert len(with_floor.aggregate.survivors) == 1


# ── Source-based enforcement ──────────────────────────────────────────────

class TestSourceEnforcement:
    INDEX = {"ev-cat": EvidenceClass.REC}

    def _fail_on(self, criterion_id: str, source: CriterionSource, confidence: float = 1.0):
        obligation = make_obligation(acceptance_criteria=[
            AcceptanceCriterion(id=criterion_id, field="quantity_sum",
                                 operator=CriterionOperator.gte, value=2,
                                 source=source, confidence=1.0)
        ])
        verifier = VerifierOutput(role="constraint", verdict=Verdict.FAIL, confidence=confidence,
                                   declared_basis=[criterion_id, "ev-cat"])
        index = dict(self.INDEX)
        index[criterion_id] = EvidenceClass.REC
        return aggregate(obligation, [verifier], index)

    def test_stated_failure_hard_blocks(self):
        result = self._fail_on("c1", CriterionSource.stated)
        assert result.verdict == Verdict.FAIL
        assert result.reason_code == "STATED_CRITERION_FAILED"

    def test_single_inferred_failure_never_fails(self):
        result = self._fail_on("c1", CriterionSource.inferred, confidence=0.5)
        assert result.verdict != Verdict.FAIL

    def test_accumulated_inferred_routes_to_clarify_not_fail(self):
        result = self._fail_on("c1", CriterionSource.inferred, confidence=1.0)
        assert result.verdict != Verdict.FAIL
        if result.inferred_failure_weight > INFERRED_CLARIFY_THRESHOLD:
            assert result.clarify is True

    def test_defaulted_failure_never_affects_outcome(self):
        result = self._fail_on("c1", CriterionSource.defaulted)
        assert result.verdict == Verdict.PASS
        assert result.clarify is False

    def test_abstain_routes_to_clarify_not_execute(self):
        verifier = VerifierOutput(role="constraint", verdict=Verdict.ABSTAIN, confidence=0.0,
                                   declared_basis=["ev-cat"])
        result = aggregate(make_obligation(), [verifier], self.INDEX)
        assert result.verdict == Verdict.ABSTAIN
        assert decide_settlement(result) == SettlementAction.CLARIFY

    def test_clean_pass_executes(self):
        verifier = VerifierOutput(role="constraint", verdict=Verdict.PASS, confidence=1.0,
                                   declared_basis=["ev-cat"])
        result = aggregate(make_obligation(), [verifier], self.INDEX)
        assert decide_settlement(result) == SettlementAction.EXECUTE

    def test_basis_weight_orders_by_class(self):
        assert BASIS_WEIGHT[EvidenceClass.SELF] < BASIS_WEIGHT[EvidenceClass.REC]
        assert BASIS_WEIGHT[EvidenceClass.REC] < BASIS_WEIGHT[EvidenceClass.ATT]


class TestEvidenceEnvelope:
    def test_catalogue_evidence_is_rec(self):
        envelope = build_evidence(make_cart(), "a" * 64)
        assert envelope.items[0].admissibility_class == EvidenceClass.REC

    def test_catalogue_evidence_id_matches_the_constraint_verifiers_declared_basis(self):
        """Regression test for a real integration bug: the constraint verifier
        declares a FIXED evidence id (CATALOGUE_EVIDENCE_ID) as its basis. If
        build_evidence gave the catalogue item a random id instead, the
        deterministic FAIL's declared_basis would not match any real evidence,
        floor.py would treat it as unknown -> SELF, and the FAIL would be
        silently excluded by its own REC floor instead of carrying."""
        from core.verifiers.constraint import CATALOGUE_EVIDENCE_ID
        envelope = build_evidence(make_cart(), "a" * 64)
        catalogue_item = envelope.of_class(EvidenceClass.REC)[0]
        assert catalogue_item.id == CATALOGUE_EVIDENCE_ID

        from core.admissibility.floor import verifier_basis_class
        deterministic_out = VerifierOutput(role="constraint", verdict=Verdict.FAIL, confidence=1.0,
                                            declared_basis=[CATALOGUE_EVIDENCE_ID])
        assert verifier_basis_class(deterministic_out, envelope.index) == EvidenceClass.REC

    def test_self_report_is_self_class(self):
        envelope = build_evidence(make_cart(), "a" * 64, self_report={"claim": "x"})
        assert envelope.of_class(EvidenceClass.SELF)
        assert envelope.of_class(EvidenceClass.SELF)[0].admissibility_class == EvidenceClass.SELF

    def test_no_self_report_means_no_self_evidence(self):
        envelope = build_evidence(make_cart(), "a" * 64)
        assert envelope.of_class(EvidenceClass.SELF) == []

    def test_evidence_is_bound(self):
        for item in build_evidence(make_cart(), "a" * 64, self_report={"c": "x"}).items:
            assert len(item.hash) == 64

    def test_index_maps_id_to_class(self):
        envelope = build_evidence(make_cart(), "a" * 64, self_report={"c": "x"})
        assert set(envelope.index.values()) == {EvidenceClass.REC, EvidenceClass.SELF}
