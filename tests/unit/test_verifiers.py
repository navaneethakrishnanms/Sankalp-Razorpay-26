"""
Unit tests for core/verifiers/{constraint,receipt}.py.

Covers hand-built cases the corpus doesn't exercise (CriterionSource.inferred
/ defaulted — the corpus only authors `stated` criteria, see FAILURES.md /
ARCHITECTURE.md), plus the three-outcome (PASS/FAIL/ABSTAIN) contract, the
semantic-skip rule, and receipt independence.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal


from core.models.cart import Cart, CartItem, Merchant
from core.models.enums import CriterionOperator, CriterionSource, Verdict
from core.models.obligation import AcceptanceCriterion, DeliveryWindow, MerchantScope, Obligation
from core.verifiers.constraint import (
    CATALOGUE_EVIDENCE_ID,
    ConstraintVerifier,
    constraint_verify,
    evaluate_constraint_checks,
)
from core.verifiers.receipt import ReceiptVerifier, evaluate_receipt_checks, receipt_verify


def make_criterion(**overrides) -> AcceptanceCriterion:
    defaults = dict(id="c1", field="quantity_sum", operator=CriterionOperator.gte,
                     value=4, source=CriterionSource.stated, confidence=1.0)
    defaults.update(overrides)
    return AcceptanceCriterion(**defaults)


def make_obligation(**overrides) -> Obligation:
    defaults = dict(raw_instruction="test", user_id="u1", acceptance_criteria=[make_criterion()])
    defaults.update(overrides)
    return Obligation(**defaults)


def make_cart(**overrides) -> Cart:
    defaults = dict(
        items=[CartItem(name="Chicken Biryani", quantity=4, unit_price=Decimal("280.00"),
                         ingredients=["chicken", "rice"], category="non-veg")],
        merchant=Merchant(id="rest-biryani", name="Biryani House", category="food_delivery"),
        total=Decimal("1120.00"),
        fulfilment_eta=datetime(2026, 8, 28, 20, 0, tzinfo=timezone.utc),
    )
    defaults.update(overrides)
    return Cart(**defaults)


# ── Per-criterion evaluation ─────────────────────────────────────────────

class TestCriterionEvaluation:
    def test_gte_pass(self):
        detail = evaluate_constraint_checks(make_obligation(), make_cart())
        assert detail.criterion_verdicts["c1"] == Verdict.PASS

    def test_gte_fail(self):
        cart = make_cart(items=[CartItem(name="Chicken Biryani", quantity=2, unit_price=Decimal("280.00"))],
                          total=Decimal("560.00"))
        detail = evaluate_constraint_checks(make_obligation(), cart)
        assert detail.criterion_verdicts["c1"] == Verdict.FAIL

    def test_excludes_pass(self):
        obl = make_obligation(acceptance_criteria=[
            make_criterion(id="c2", field="item.ingredients", operator=CriterionOperator.excludes, value="beef"),
        ])
        detail = evaluate_constraint_checks(obl, make_cart())
        assert detail.criterion_verdicts["c2"] == Verdict.PASS

    def test_excludes_fail(self):
        obl = make_obligation(acceptance_criteria=[
            make_criterion(id="c2", field="item.ingredients", operator=CriterionOperator.excludes, value="chicken"),
        ])
        detail = evaluate_constraint_checks(obl, make_cart())
        assert detail.criterion_verdicts["c2"] == Verdict.FAIL

    def test_excludes_abstains_on_missing_ingredients(self):
        """The critical three-outcome case: undeclared ingredients -> ABSTAIN, never FAIL by luck."""
        cart = make_cart(items=[CartItem(name="Mystery", quantity=1, unit_price=Decimal("100.00"), ingredients=[])],
                          total=Decimal("100.00"))
        obl = make_obligation(acceptance_criteria=[
            make_criterion(id="c2", field="item.ingredients", operator=CriterionOperator.excludes, value="beef"),
        ])
        detail = evaluate_constraint_checks(obl, cart)
        assert detail.criterion_verdicts["c2"] == Verdict.ABSTAIN

    def test_excludes_abstains_even_if_declared_items_are_clean(self):
        """One item with missing data poisons the whole excludes-check, even if
        every OTHER item is fully declared and clean — we can't know what's in
        the undeclared item."""
        cart = make_cart(items=[
            CartItem(name="Veg Biryani", quantity=1, unit_price=Decimal("220.00"), ingredients=["rice", "veg"]),
            CartItem(name="Mystery", quantity=1, unit_price=Decimal("100.00"), ingredients=[]),
        ], total=Decimal("320.00"))
        obl = make_obligation(acceptance_criteria=[
            make_criterion(id="c2", field="item.ingredients", operator=CriterionOperator.excludes, value="beef"),
        ])
        detail = evaluate_constraint_checks(obl, cart)
        assert detail.criterion_verdicts["c2"] == Verdict.ABSTAIN

    def test_distinct_item_count_eq(self):
        obl = make_obligation(acceptance_criteria=[
            make_criterion(id="c3", field="distinct_item_count", operator=CriterionOperator.eq, value=1),
        ])
        detail = evaluate_constraint_checks(obl, make_cart())
        assert detail.criterion_verdicts["c3"] == Verdict.PASS

    def test_semantic_criterion_is_skipped_not_failed(self):
        obl = make_obligation(acceptance_criteria=[
            make_criterion(id="c4", field="item.categories", operator=CriterionOperator.semantic, value="not_spicy"),
        ])
        detail = evaluate_constraint_checks(obl, make_cart())
        assert "c4" not in detail.criterion_verdicts

    def test_in_set_operator(self):
        obl = make_obligation(acceptance_criteria=[
            make_criterion(id="c5", field="merchant.id", operator=CriterionOperator.in_set,
                           value=["rest-biryani", "rest-pizza"]),
        ])
        detail = evaluate_constraint_checks(obl, make_cart())
        assert detail.criterion_verdicts["c5"] == Verdict.PASS

    def test_contains_operator(self):
        obl = make_obligation(acceptance_criteria=[
            make_criterion(id="c6", field="item.names", operator=CriterionOperator.contains, value="chicken biryani"),
        ])
        detail = evaluate_constraint_checks(obl, make_cart())
        assert detail.criterion_verdicts["c6"] == Verdict.PASS


# ── Obligation-level checks ────────────────────────────────────────────────

class TestObligationLevelChecks:
    def test_budget_none_when_no_ceiling(self):
        detail = evaluate_constraint_checks(make_obligation(), make_cart())
        assert detail.budget_verdict is None

    def test_budget_pass(self):
        obl = make_obligation(budget_ceiling=Decimal("2000.00"))
        detail = evaluate_constraint_checks(obl, make_cart())
        assert detail.budget_verdict == Verdict.PASS

    def test_budget_fail(self):
        obl = make_obligation(budget_ceiling=Decimal("500.00"))
        detail = evaluate_constraint_checks(obl, make_cart())
        assert detail.budget_verdict == Verdict.FAIL

    def test_merchant_scope_by_ids_pass(self):
        obl = make_obligation(merchant_scope=MerchantScope(merchant_ids=["rest-biryani"]))
        detail = evaluate_constraint_checks(obl, make_cart())
        assert detail.merchant_scope_verdict == Verdict.PASS

    def test_merchant_scope_by_ids_fail(self):
        obl = make_obligation(merchant_scope=MerchantScope(merchant_ids=["rest-pizza"]))
        detail = evaluate_constraint_checks(obl, make_cart())
        assert detail.merchant_scope_verdict == Verdict.FAIL

    def test_merchant_scope_by_category_pass(self):
        obl = make_obligation(merchant_scope=MerchantScope(category="food_delivery"))
        detail = evaluate_constraint_checks(obl, make_cart())
        assert detail.merchant_scope_verdict == Verdict.PASS

    def test_merchant_scope_by_category_fail(self):
        obl = make_obligation(merchant_scope=MerchantScope(category="grocery"))
        detail = evaluate_constraint_checks(obl, make_cart())
        assert detail.merchant_scope_verdict == Verdict.FAIL

    def test_merchant_scope_none_when_unspecified(self):
        detail = evaluate_constraint_checks(make_obligation(), make_cart())
        assert detail.merchant_scope_verdict is None

    def test_delivery_window_pass(self):
        obl = make_obligation(delivery_window=DeliveryWindow(latest_by=datetime(2026, 8, 28, 21, 0, tzinfo=timezone.utc)))
        detail = evaluate_constraint_checks(obl, make_cart())
        assert detail.delivery_verdict == Verdict.PASS

    def test_delivery_window_fail(self):
        obl = make_obligation(delivery_window=DeliveryWindow(latest_by=datetime(2026, 8, 28, 19, 0, tzinfo=timezone.utc)))
        detail = evaluate_constraint_checks(obl, make_cart())
        assert detail.delivery_verdict == Verdict.FAIL

    def test_delivery_window_none_when_unspecified(self):
        detail = evaluate_constraint_checks(make_obligation(), make_cart())
        assert detail.delivery_verdict is None

    def test_total_arithmetic_pass(self):
        detail = evaluate_constraint_checks(make_obligation(), make_cart())
        assert detail.total_arithmetic_verdict == Verdict.PASS

    def test_total_arithmetic_fail(self):
        cart = make_cart(total=Decimal("999999.00"))
        detail = evaluate_constraint_checks(make_obligation(), cart)
        assert detail.total_arithmetic_verdict == Verdict.FAIL

    def test_total_arithmetic_always_evaluated_even_with_no_criteria(self):
        obl = make_obligation(acceptance_criteria=[])
        detail = evaluate_constraint_checks(obl, make_cart())
        assert detail.total_arithmetic_verdict == Verdict.PASS


# ── loss_estimate ────────────────────────────────────────────────────────

class TestLossEstimate:
    def test_none_when_nothing_fails(self):
        detail = evaluate_constraint_checks(make_obligation(), make_cart())
        assert detail.loss_estimate is None

    def test_quantity_shortfall_loss(self):
        cart = make_cart(items=[CartItem(name="Chicken Biryani", quantity=2, unit_price=Decimal("280.00"))],
                          total=Decimal("560.00"))
        detail = evaluate_constraint_checks(make_obligation(), cart)
        # shortfall = 4 - 2 = 2 units * 280 = 560
        assert detail.loss_estimate == Decimal("560.00")

    def test_budget_overage_loss(self):
        obl = make_obligation(budget_ceiling=Decimal("1000.00"))
        detail = evaluate_constraint_checks(obl, make_cart())   # total 1120
        assert detail.loss_estimate == Decimal("120.00")


# ── Source-based composite enforcement (not exercised by the corpus — see FAILURES.md) ──

class TestSourceEnforcement:
    def test_stated_fail_hard_blocks(self):
        obl = make_obligation(acceptance_criteria=[make_criterion(source=CriterionSource.stated)])
        cart = make_cart(items=[CartItem(name="X", quantity=1, unit_price=Decimal("10"))], total=Decimal("10"))
        out = constraint_verify(obl, cart)
        assert out.verdict == Verdict.FAIL
        assert out.confidence == 1.0

    def test_inferred_fail_alone_does_not_block(self):
        obl = make_obligation(acceptance_criteria=[make_criterion(source=CriterionSource.inferred)])
        cart = make_cart(items=[CartItem(name="X", quantity=1, unit_price=Decimal("10"))], total=Decimal("10"))
        out = constraint_verify(obl, cart)
        assert out.verdict == Verdict.PASS
        assert out.confidence < 1.0

    def test_defaulted_fail_never_affects_verdict(self):
        obl = make_obligation(acceptance_criteria=[make_criterion(source=CriterionSource.defaulted)])
        cart = make_cart(items=[CartItem(name="X", quantity=1, unit_price=Decimal("10"))], total=Decimal("10"))
        out = constraint_verify(obl, cart)
        assert out.verdict == Verdict.PASS
        assert out.confidence == 1.0

    def test_multiple_inferred_fails_still_do_not_block(self):
        obl = make_obligation(acceptance_criteria=[
            make_criterion(id="a", source=CriterionSource.inferred, value=100),
            make_criterion(id="b", source=CriterionSource.inferred, value=100),
        ])
        cart = make_cart(items=[CartItem(name="X", quantity=1, unit_price=Decimal("10"))], total=Decimal("10"))
        out = constraint_verify(obl, cart)
        assert out.verdict == Verdict.PASS

    def test_obligation_level_fail_always_hard_blocks_regardless_of_criteria(self):
        """budget/scope/delivery/arithmetic have no `source` — always hard-block on FAIL."""
        obl = make_obligation(acceptance_criteria=[], budget_ceiling=Decimal("1.00"))
        out = constraint_verify(obl, make_cart())
        assert out.verdict == Verdict.FAIL

    def test_abstain_composite_when_no_fail_present(self):
        cart = make_cart(items=[CartItem(name="Mystery", quantity=1, unit_price=Decimal("100"), ingredients=[])],
                          total=Decimal("100"))
        obl = make_obligation(acceptance_criteria=[
            make_criterion(id="c2", field="item.ingredients", operator=CriterionOperator.excludes,
                           value="beef", source=CriterionSource.stated),
        ])
        out = constraint_verify(obl, cart)
        assert out.verdict == Verdict.ABSTAIN

    def test_stated_fail_wins_over_abstain_elsewhere(self):
        cart = make_cart(items=[
            CartItem(name="Beef Biryani", quantity=1, unit_price=Decimal("340"), ingredients=["beef"]),
            CartItem(name="Mystery", quantity=1, unit_price=Decimal("100"), ingredients=[]),
        ], total=Decimal("440"))
        obl = make_obligation(acceptance_criteria=[
            make_criterion(id="c-beef", field="item.ingredients", operator=CriterionOperator.excludes,
                           value="beef", source=CriterionSource.stated),
            make_criterion(id="c-egg", field="item.ingredients", operator=CriterionOperator.excludes,
                           value="egg", source=CriterionSource.stated),
        ])
        detail = evaluate_constraint_checks(obl, cart)
        assert detail.criterion_verdicts["c-beef"] == Verdict.FAIL
        assert detail.criterion_verdicts["c-egg"] == Verdict.ABSTAIN
        out = constraint_verify(obl, cart)
        assert out.verdict == Verdict.FAIL   # the definite violation wins, not swallowed by the abstain


# ── declared_basis / VerifierOutput contract ───────────────────────────────

class TestVerifierOutputContract:
    def test_declared_basis_present(self):
        out = constraint_verify(make_obligation(), make_cart())
        assert out.declared_basis == [CATALOGUE_EVIDENCE_ID]

    def test_role_is_constraint(self):
        out = constraint_verify(make_obligation(), make_cart())
        assert out.role == "constraint"

    def test_class_wrapper_matches_module_function(self):
        obl, cart = make_obligation(), make_cart()
        via_class = ConstraintVerifier().verify(obl, cart)
        via_fn = constraint_verify(obl, cart)
        assert via_class.verdict == via_fn.verdict
        assert via_class.declared_basis == via_fn.declared_basis


# ── Receipt verifier ────────────────────────────────────────────────────────

class TestReceiptVerifier:
    def test_pass_on_real_catalogue_data(self):
        detail = evaluate_receipt_checks(make_cart())
        assert detail.merchant_exists and detail.items_exist and detail.prices_match

    def test_unknown_merchant_fails(self):
        cart = make_cart(merchant=Merchant(id="rest-does-not-exist", name="Fake", category="food_delivery"))
        detail = evaluate_receipt_checks(cart)
        assert detail.merchant_exists is False

    def test_unknown_item_fails(self):
        cart = make_cart(items=[CartItem(name="Nonexistent Dish", quantity=1, unit_price=Decimal("100.00"))],
                          total=Decimal("100.00"))
        detail = evaluate_receipt_checks(cart)
        assert detail.items_exist is False

    def test_price_mismatch_fails(self):
        cart = make_cart(items=[CartItem(name="Chicken Biryani", quantity=4, unit_price=Decimal("1.00"))],
                          total=Decimal("4.00"))
        detail = evaluate_receipt_checks(cart)
        assert detail.prices_match is False

    def test_receipt_verify_output(self):
        out = receipt_verify(make_obligation(), make_cart())
        assert out.verdict == Verdict.PASS
        assert out.role == "receipt"
        assert out.loss_estimate is None   # receipt verifier never authors a loss number

    def test_receipt_does_not_import_constraint_field_registry(self):
        """Independence guard: receipt.py must not import core.models.fields."""
        import core.verifiers.receipt as receipt_module
        source = open(receipt_module.__file__, encoding="utf-8").read()
        assert "core.models.fields" not in source
        assert "core.verifiers.constraint" not in source.replace(
            "from core.verifiers.constraint import CATALOGUE_EVIDENCE_ID", ""
        )

    def test_class_wrapper_matches_module_function(self):
        obl, cart = make_obligation(), make_cart()
        via_class = ReceiptVerifier().verify(obl, cart)
        via_fn = receipt_verify(obl, cart)
        assert via_class.verdict == via_fn.verdict
