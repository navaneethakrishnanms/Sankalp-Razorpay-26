"""Unit tests for core/settlement/instruction.py — deliberately thin (Stage 6 stub)."""

from __future__ import annotations

from decimal import Decimal

from core.clearing.aggregator import aggregate
from core.clearing.engine import build_evidence, clear
from core.models.cart import Cart, CartItem, Merchant
from core.models.enums import CriterionOperator, CriterionSource, EvidenceClass, Rail, SettlementAction, Verdict
from core.models.obligation import AcceptanceCriterion, Obligation
from core.models.verifier import VerifierOutput
from core.settlement.instruction import emit, explain
from datetime import datetime, timezone


def make_obligation() -> Obligation:
    return Obligation(
        id="obl-settle-test", raw_instruction="x", user_id="u1",
        acceptance_criteria=[AcceptanceCriterion(
            id="c1", field="quantity_sum", operator=CriterionOperator.gte,
            value=2, source=CriterionSource.stated, confidence=1.0)],
        admissibility_floor=EvidenceClass.REC,
        created_at=datetime(2026, 8, 28, tzinfo=timezone.utc),
    )


def make_cart() -> Cart:
    return Cart(
        items=[CartItem(name="X", quantity=2, unit_price=Decimal("100"))],
        merchant=Merchant(id="m1", name="M", category="food_delivery"),
        total=Decimal("200"), fulfilment_eta=datetime(2026, 8, 28, 20, 0, tzinfo=timezone.utc),
    )


class TestEmit:
    def test_produces_a_bound_instruction(self):
        obligation, cart = make_obligation(), make_cart()
        envelope = build_evidence(cart, "a" * 64)
        pass_out = VerifierOutput(role="constraint", verdict=Verdict.PASS, confidence=1.0,
                                   declared_basis=["ev-catalogue"])
        outcome = clear(obligation, cart, [pass_out], envelope)
        instruction = emit(outcome, "b" * 64)
        assert len(instruction.hash) == 64
        assert instruction.action == SettlementAction.EXECUTE

    def test_default_rail_is_test_mode(self):
        obligation, cart = make_obligation(), make_cart()
        envelope = build_evidence(cart, "a" * 64)
        pass_out = VerifierOutput(role="constraint", verdict=Verdict.PASS, confidence=1.0,
                                   declared_basis=["ev-catalogue"])
        outcome = clear(obligation, cart, [pass_out], envelope)
        instruction = emit(outcome, "b" * 64)
        assert instruction.rail == Rail.RAZORPAY_TEST

    def test_abort_action_is_carried_through(self):
        obligation, cart = make_obligation(), make_cart()
        envelope = build_evidence(cart, "a" * 64)
        fail_out = VerifierOutput(role="constraint", verdict=Verdict.FAIL, confidence=1.0,
                                   declared_basis=["ev-catalogue"])
        outcome = clear(obligation, cart, [fail_out], envelope)
        instruction = emit(outcome, "b" * 64)
        assert instruction.action == SettlementAction.ABORT
        assert instruction.reason_code == "STATED_CRITERION_FAILED"


class TestExplain:
    def test_every_action_has_an_explanation(self):
        for action in SettlementAction:
            text = explain(action)
            assert isinstance(text, str) and len(text) > 0

    def test_hold_explanation_matches_the_documented_test_mode_boundary(self):
        text = explain(SettlementAction.HOLD).lower()
        assert "test mode" in text or "capture" in text
