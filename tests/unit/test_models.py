"""
Unit tests for core/models/.

COVERAGE TARGETS
----------------
AcceptanceCriterion    frozen, confidence bounds, all operators
Obligation             hash lifecycle, tamper detection, budget_ceiling=None,
                       frozen, hash determinism
EvidenceItem           frozen, admissibility_class validation, hash lifecycle,
                       extend_evidence creates new object, ATT→SELF end-to-end,
                       extend requires bound original, bind requires unbound,
                       predecessor_hash chaining
Cart                   total validation (match and mismatch)
VerifierOutput         loss_estimate nullable, declared_basis required, frozen
FieldRegistry          all known paths resolve, unknown path raises KeyError,
                       specific resolver correctness tests
"""

from __future__ import annotations

import pytest
from datetime import datetime, timezone
from decimal import Decimal

from pydantic import ValidationError

from core.models.enums import (
    CriterionOperator,
    CriterionSource,
    EvidenceClass,
    Verdict,
    SettlementAction,
    Rail,
    Finality,
)
from core.models.obligation import (
    AcceptanceCriterion,
    Obligation,
    MerchantScope,
    DeliveryWindow,
    _compute_obligation_hash,
)
from core.models.cart import Cart, CartItem, Merchant
from core.models.evidence import (
    EvidenceItem,
    ProvenanceHop,
    make_evidence_item,
    bind_evidence,
    extend_evidence,
    _compute_evidence_hash,
)
from core.models.verifier import VerifierOutput
from core.models.clearing import ClearingDecision, _compute_clearing_hash
from core.models.settlement import SettlementInstruction, _compute_settlement_hash
import core.models.fields as field_registry


# ── Shared fixtures ────────────────────────────────────────────────────────

FIXED_CREATED_AT = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
FIXED_LATEST_BY  = datetime(2024, 6, 1, 21, 0, 0, tzinfo=timezone.utc)


def make_criterion(**overrides) -> AcceptanceCriterion:
    """Create a valid AcceptanceCriterion.  Accepts keyword overrides."""
    defaults = dict(
        id="crit-fixed-001",
        field="total",
        operator=CriterionOperator.lte,
        value=4000,
        source=CriterionSource.stated,
        confidence=1.0,
    )
    defaults.update(overrides)
    return AcceptanceCriterion(**defaults)


def make_obligation(**overrides) -> Obligation:
    """Create an unbound Obligation.  All criteria use fixed IDs for hash stability."""
    defaults = dict(
        id="obl-fixed-001",
        raw_instruction="Order dinner for 8 people, no beef, under ₹4000.",
        user_id="user-001",
        acceptance_criteria=[make_criterion()],
        created_at=FIXED_CREATED_AT,
    )
    defaults.update(overrides)
    return Obligation(**defaults)


def bind_obligation(obl: Obligation) -> Obligation:
    """Compute and stamp the hash on an unbound Obligation."""
    h = _compute_obligation_hash(obl)
    return obl.model_copy(update={"hash": h})


def make_merchant(**overrides) -> Merchant:
    defaults = dict(id="rest-001", name="Biryani House", category="food_delivery")
    defaults.update(overrides)
    return Merchant(**defaults)


def make_cart(**overrides) -> Cart:
    defaults = dict(
        items=[CartItem(name="Chicken Biryani", quantity=4, unit_price=Decimal("250.00"))],
        merchant=make_merchant(),
        total=Decimal("1000.00"),
        fulfilment_eta=datetime(2024, 6, 1, 20, 0, tzinfo=timezone.utc),
    )
    defaults.update(overrides)
    return Cart(**defaults)


def make_unbound_evidence(**overrides) -> EvidenceItem:
    """Return an unbound EvidenceItem with admissibility_class=REC (default)."""
    defaults = dict(
        payload={"detail": "test payload"},
        emitter="test-emitter",
        original_class=EvidenceClass.REC,
        provenance_chain=[],
        admissibility_class=EvidenceClass.REC,
        obligation_hash="a" * 64,
    )
    defaults.update(overrides)
    return EvidenceItem(**defaults)


def make_bound_evidence(**overrides) -> EvidenceItem:
    return bind_evidence(make_unbound_evidence(**overrides))


# ── AcceptanceCriterion ────────────────────────────────────────────────────

class TestAcceptanceCriterion:
    def test_creates_with_valid_fields(self):
        c = make_criterion()
        assert c.field == "total"
        assert c.operator == CriterionOperator.lte
        assert c.source == CriterionSource.stated

    def test_auto_uuid_when_id_not_provided(self):
        c = AcceptanceCriterion(
            field="total",
            operator=CriterionOperator.lte,
            value=4000,
            source=CriterionSource.stated,
            confidence=1.0,
        )
        assert len(c.id) == 36  # uuid4 format

    def test_frozen_rejects_attribute_assignment(self):
        c = make_criterion()
        with pytest.raises((TypeError, ValidationError)):
            c.field = "item_count"  # type: ignore[misc]

    def test_confidence_upper_bound(self):
        with pytest.raises(ValidationError):
            make_criterion(confidence=1.001)

    def test_confidence_lower_bound(self):
        with pytest.raises(ValidationError):
            make_criterion(confidence=-0.001)

    def test_confidence_at_boundaries_valid(self):
        c0 = make_criterion(confidence=0.0)
        c1 = make_criterion(confidence=1.0)
        assert c0.confidence == 0.0
        assert c1.confidence == 1.0

    def test_all_operators_accepted(self):
        for op in CriterionOperator:
            c = make_criterion(operator=op)
            assert c.operator == op

    def test_all_sources_accepted(self):
        for src in CriterionSource:
            c = make_criterion(source=src)
            assert c.source == src

    def test_value_list_accepted_for_in_set(self):
        c = make_criterion(operator=CriterionOperator.in_set, value=["rest-001", "rest-002"])
        assert isinstance(c.value, list)


# ── Obligation ─────────────────────────────────────────────────────────────

class TestObligation:
    def test_creates_unbound(self):
        o = make_obligation()
        assert o.hash == ""

    def test_bind_sets_64_char_hex_hash(self):
        bound = bind_obligation(make_obligation())
        assert len(bound.hash) == 64
        assert all(c in "0123456789abcdef" for c in bound.hash)

    def test_reconstruction_from_dump_passes_validation(self):
        bound = bind_obligation(make_obligation())
        data = bound.model_dump()
        reconstructed = Obligation(**data)
        assert reconstructed.hash == bound.hash

    def test_tampered_hash_raises_validation_error(self):
        bound = bind_obligation(make_obligation())
        data = bound.model_dump()
        data["hash"] = "b" * 64
        with pytest.raises(ValidationError, match="hash integrity"):
            Obligation(**data)

    def test_tampered_field_invalidates_hash(self):
        """Changing raw_instruction after binding must be caught on reconstruction."""
        bound = bind_obligation(make_obligation())
        data = bound.model_dump()
        data["raw_instruction"] = "tampered instruction"
        # hash is still the original — mismatch must be caught
        with pytest.raises(ValidationError, match="hash integrity"):
            Obligation(**data)

    def test_frozen_rejects_assignment(self):
        o = make_obligation()
        with pytest.raises((TypeError, ValidationError)):
            o.raw_instruction = "tampered"  # type: ignore[misc]

    def test_budget_ceiling_none_is_valid(self):
        """
        None means the user stated no ceiling.  This is a distinct state
        from any numeric value, including 0.  An order under the mandate
        limit with no stated ceiling is CLEAN by definition.
        """
        o = make_obligation(budget_ceiling=None)
        assert o.budget_ceiling is None

    def test_budget_ceiling_decimal_preserved(self):
        o = make_obligation(budget_ceiling=Decimal("3999.99"))
        assert o.budget_ceiling == Decimal("3999.99")

    def test_prohibited_defaults_to_empty_list(self):
        o = make_obligation()
        assert o.prohibited == []

    def test_admissibility_floor_defaults_to_rec(self):
        """Default floor is REC — the standard for food delivery orders."""
        o = make_obligation()
        assert o.admissibility_floor == EvidenceClass.REC

    def test_admissibility_floor_configurable(self):
        o = make_obligation(admissibility_floor=EvidenceClass.ATT)
        assert o.admissibility_floor == EvidenceClass.ATT

    def test_hash_is_deterministic_for_same_data(self):
        """Same logical data always produces the same hash."""
        shared_criteria = [make_criterion()]
        o1 = Obligation(
            id="fixed-id",
            raw_instruction="same",
            user_id="u1",
            acceptance_criteria=shared_criteria,
            created_at=FIXED_CREATED_AT,
        )
        o2 = Obligation(
            id="fixed-id",
            raw_instruction="same",
            user_id="u1",
            acceptance_criteria=shared_criteria,
            created_at=FIXED_CREATED_AT,
        )
        assert bind_obligation(o1).hash == bind_obligation(o2).hash

    def test_hash_changes_when_data_changes(self):
        shared_criteria = [make_criterion()]
        base = dict(
            id="fixed-id",
            raw_instruction="base",
            user_id="u1",
            acceptance_criteria=shared_criteria,
            created_at=FIXED_CREATED_AT,
        )
        o1 = bind_obligation(Obligation(**base))
        o2 = bind_obligation(Obligation(**{**base, "budget_ceiling": Decimal("5000")}))
        assert o1.hash != o2.hash

    def test_merchant_scope_defaults_to_any(self):
        o = make_obligation()
        assert o.merchant_scope.merchant_ids == []
        assert o.merchant_scope.category is None

    def test_delivery_window_optional(self):
        o = make_obligation(delivery_window=None)
        assert o.delivery_window is None

    def test_delivery_window_accepted(self):
        window = DeliveryWindow(latest_by=FIXED_LATEST_BY)
        o = make_obligation(delivery_window=window)
        assert o.delivery_window is not None
        assert o.delivery_window.latest_by == FIXED_LATEST_BY


# ── EvidenceItem ───────────────────────────────────────────────────────────

class TestEvidenceItem:
    def test_creates_unbound(self):
        e = make_unbound_evidence()
        assert e.hash == ""

    def test_bind_sets_hash(self):
        bound = bind_evidence(make_unbound_evidence())
        assert len(bound.hash) == 64

    def test_frozen_rejects_assignment(self):
        e = make_unbound_evidence()
        with pytest.raises((TypeError, ValidationError)):
            e.emitter = "tampered"  # type: ignore[misc]

    def test_wrong_admissibility_class_raises_on_construction(self):
        """
        Declaring admissibility_class=ATT when the chain computes SELF
        must raise immediately — not propagate to floor enforcement.
        """
        with pytest.raises(ValidationError, match="admissibility_class mismatch"):
            EvidenceItem(
                payload={},
                emitter="test",
                original_class=EvidenceClass.SELF,
                provenance_chain=[],
                admissibility_class=EvidenceClass.ATT,   # wrong: should be SELF
                obligation_hash="a" * 64,
            )

    def test_inflated_class_with_hop_raises(self):
        """
        ATT original + SELF channel hop = SELF effective class.
        Declaring REC must raise.
        """
        hop = ProvenanceHop(
            channel_class=EvidenceClass.SELF,
            predecessor_hash="d" * 64,
        )
        with pytest.raises(ValidationError, match="admissibility_class mismatch"):
            EvidenceItem(
                payload={},
                emitter="test",
                original_class=EvidenceClass.ATT,
                provenance_chain=[hop],
                admissibility_class=EvidenceClass.REC,   # wrong: should be SELF
                obligation_hash="a" * 64,
            )

    def test_correct_class_with_hop_accepted(self):
        hop = ProvenanceHop(
            channel_class=EvidenceClass.SELF,
            predecessor_hash="d" * 64,
        )
        e = EvidenceItem(
            payload={},
            emitter="test",
            original_class=EvidenceClass.ATT,
            provenance_chain=[hop],
            admissibility_class=EvidenceClass.SELF,   # correct: meet(ATT, SELF)
            obligation_hash="a" * 64,
        )
        assert e.admissibility_class == EvidenceClass.SELF

    def test_tampered_hash_raises(self):
        bound = bind_evidence(make_unbound_evidence())
        data = bound.model_dump()
        data["hash"] = "c" * 64
        with pytest.raises(ValidationError, match="hash integrity"):
            EvidenceItem(**data)

    def test_extend_evidence_creates_new_object(self):
        """extend_evidence must not mutate the original."""
        original = make_bound_evidence(
            original_class=EvidenceClass.ATT,
            admissibility_class=EvidenceClass.ATT,
        )
        original_id = original.id
        original_hash = original.hash
        original_class = original.admissibility_class

        extended = extend_evidence(original, channel_class=EvidenceClass.SELF)

        # Original is unchanged
        assert original.id == original_id
        assert original.hash == original_hash
        assert original.admissibility_class == original_class

        # Extended is a distinct object
        assert extended is not original
        assert extended.id != original.id

    def test_extend_att_through_self_gives_self(self):
        """
        §5.2 canonical scenario at the model level.
        An ATT-class item transmitted through a SELF channel is SELF.
        """
        att_item = make_bound_evidence(
            original_class=EvidenceClass.ATT,
            admissibility_class=EvidenceClass.ATT,
        )
        result = extend_evidence(att_item, EvidenceClass.SELF)
        assert result.admissibility_class == EvidenceClass.SELF

    def test_extend_rec_through_sign_gives_sign(self):
        rec_item = make_bound_evidence(
            original_class=EvidenceClass.REC,
            admissibility_class=EvidenceClass.REC,
        )
        result = extend_evidence(rec_item, EvidenceClass.SIGN)
        assert result.admissibility_class == EvidenceClass.SIGN

    def test_extend_requires_bound_original(self):
        unbound = make_unbound_evidence()
        with pytest.raises(ValueError, match="unbound"):
            extend_evidence(unbound, EvidenceClass.SIGN)

    def test_bind_requires_unbound(self):
        bound = make_bound_evidence()
        with pytest.raises(ValueError, match="already bound"):
            bind_evidence(bound)

    def test_predecessor_hash_in_extended_hop(self):
        """The predecessor's hash must appear verbatim in the new hop."""
        original = make_bound_evidence()
        extended = extend_evidence(original, EvidenceClass.REC)
        last_hop = extended.provenance_chain[-1]
        assert last_hop.predecessor_hash == original.hash

    def test_multi_hop_chain_class(self):
        """Three hops: ATT → (SIGN channel) → (SELF channel) → (REC channel) = SELF."""
        item = make_bound_evidence(
            original_class=EvidenceClass.ATT,
            admissibility_class=EvidenceClass.ATT,
        )
        item = extend_evidence(item, EvidenceClass.SIGN)  # meet(ATT, SIGN) = SIGN
        assert item.admissibility_class == EvidenceClass.SIGN

        item = extend_evidence(item, EvidenceClass.SELF)  # meet(ATT, SIGN, SELF) = SELF
        assert item.admissibility_class == EvidenceClass.SELF

        item = extend_evidence(item, EvidenceClass.REC)   # meet(ATT, SIGN, SELF, REC) = SELF
        assert item.admissibility_class == EvidenceClass.SELF

    def test_make_evidence_item_computes_class(self):
        hop = ProvenanceHop(channel_class=EvidenceClass.SIGN, predecessor_hash="f" * 64)
        e = make_evidence_item(
            payload={"x": 1},
            emitter="runner",
            original_class=EvidenceClass.ATT,
            obligation_hash="a" * 64,
            provenance_chain=[hop],
        )
        # meet(ATT, SIGN) = SIGN
        assert e.admissibility_class == EvidenceClass.SIGN
        assert e.hash == ""   # still unbound


# ── Cart ────────────────────────────────────────────────────────────────────

class TestCart:
    def test_total_matches_arithmetic_sum(self):
        cart = Cart(
            items=[
                CartItem(name="A", quantity=2, unit_price=Decimal("100.00")),
                CartItem(name="B", quantity=3, unit_price=Decimal("50.00")),
            ],
            merchant=make_merchant(),
            total=Decimal("350.00"),
            fulfilment_eta=datetime(2024, 6, 1, 20, 0, tzinfo=timezone.utc),
        )
        assert cart.validate_total() is True

    def test_total_mismatch_detected(self):
        cart = Cart(
            items=[CartItem(name="A", quantity=2, unit_price=Decimal("100.00"))],
            merchant=make_merchant(),
            total=Decimal("150.00"),   # should be 200
            fulfilment_eta=datetime(2024, 6, 1, 20, 0, tzinfo=timezone.utc),
        )
        assert cart.validate_total() is False

    def test_frozen(self):
        cart = make_cart()
        with pytest.raises((TypeError, ValidationError)):
            cart.total = Decimal("9999")  # type: ignore[misc]

    def test_cart_item_quantity_minimum_one(self):
        with pytest.raises(ValidationError):
            CartItem(name="X", quantity=0, unit_price=Decimal("100"))


# ── VerifierOutput ──────────────────────────────────────────────────────────

class TestVerifierOutput:
    def test_loss_estimate_optional_none(self):
        v = VerifierOutput(
            role="semantic",
            verdict=Verdict.PASS,
            confidence=0.8,
            declared_basis=["ev1"],
            loss_estimate=None,
        )
        assert v.loss_estimate is None

    def test_loss_estimate_decimal(self):
        v = VerifierOutput(
            role="constraint",
            verdict=Verdict.FAIL,
            confidence=1.0,
            declared_basis=["ev1"],
            loss_estimate=Decimal("500.00"),
        )
        assert v.loss_estimate == Decimal("500.00")

    def test_declared_basis_is_required_field(self):
        """declared_basis has no default — omitting it must raise."""
        with pytest.raises(ValidationError):
            VerifierOutput(
                role="test",
                verdict=Verdict.PASS,
                confidence=0.8,
                # declared_basis intentionally omitted
            )

    def test_declared_basis_may_be_empty_list(self):
        """An empty list is valid — the verifier declares no evidence consulted."""
        v = VerifierOutput(
            role="test",
            verdict=Verdict.ABSTAIN,
            confidence=0.0,
            declared_basis=[],
        )
        assert v.declared_basis == []

    def test_frozen(self):
        v = VerifierOutput(
            role="test",
            verdict=Verdict.PASS,
            confidence=0.5,
            declared_basis=["ev1"],
        )
        with pytest.raises((TypeError, ValidationError)):
            v.verdict = Verdict.FAIL  # type: ignore[misc]

    def test_confidence_bounds(self):
        with pytest.raises(ValidationError):
            VerifierOutput(role="x", verdict=Verdict.PASS, confidence=1.1, declared_basis=[])
        with pytest.raises(ValidationError):
            VerifierOutput(role="x", verdict=Verdict.PASS, confidence=-0.1, declared_basis=[])


# ── Field registry ──────────────────────────────────────────────────────────

class TestFieldRegistry:
    """
    Verify that every registered path resolves without error and that
    unknown paths raise KeyError rather than returning None.
    """

    def test_all_registered_paths_resolve_without_error(self):
        cart = make_cart(items=[
            CartItem(
                name="Chicken Biryani",
                quantity=4,
                unit_price=Decimal("250.00"),
                ingredients=["chicken", "rice", "spices"],
                category="non-veg",
            ),
        ])
        for path in field_registry.all_paths():
            result = field_registry.resolve(path, cart)
            # Just ensure it didn't raise; result type is checked in specific tests.
            _ = result

    def test_unknown_path_raises_key_error(self):
        cart = make_cart()
        with pytest.raises(KeyError, match="not in the SANKALP field registry"):
            field_registry.resolve("items[].quantity_sum", cart)

    def test_unknown_path_in_get_field_spec_raises(self):
        with pytest.raises(KeyError):
            field_registry.get_field_spec("nonexistent.path.xyz")

    def test_old_mini_language_path_rejected(self):
        """paths like 'items[*].ingredients' must not be in the registry."""
        with pytest.raises(KeyError):
            field_registry.get_field_spec("items[*].ingredients")

    def test_all_paths_returns_sorted_list(self):
        paths = field_registry.all_paths()
        assert paths == sorted(paths)

    # ── Specific resolver correctness ──────────────────────────────────────

    def test_total_resolver(self):
        cart = make_cart(total=Decimal("1234.56"))
        assert field_registry.resolve("total", cart) == Decimal("1234.56")

    def test_quantity_sum_resolver(self):
        cart = make_cart(items=[
            CartItem(name="A", quantity=3, unit_price=Decimal("100")),
            CartItem(name="B", quantity=5, unit_price=Decimal("200")),
        ])
        assert field_registry.resolve("quantity_sum", cart) == 8

    def test_quantity_sum_for_8_people(self):
        """The canonical QUANTITY_MISMATCH scenario: 8 people, 4 portions ordered."""
        cart = make_cart(items=[
            CartItem(name="Biryani", quantity=4, unit_price=Decimal("350")),
        ])
        qty = field_registry.resolve("quantity_sum", cart)
        assert qty == 4
        # A criterion: quantity_sum >= 8 → FAIL

    def test_item_count_resolver(self):
        cart = make_cart(items=[
            CartItem(name="A", quantity=1, unit_price=Decimal("100")),
            CartItem(name="B", quantity=2, unit_price=Decimal("200")),
            CartItem(name="C", quantity=1, unit_price=Decimal("50")),
        ])
        assert field_registry.resolve("item_count", cart) == 3

    def test_merchant_id_resolver(self):
        cart = make_cart(merchant=make_merchant(id="rest-999"))
        assert field_registry.resolve("merchant.id", cart) == "rest-999"

    def test_merchant_name_resolver(self):
        cart = make_cart(merchant=make_merchant(name="Spice Garden"))
        assert field_registry.resolve("merchant.name", cart) == "Spice Garden"

    def test_merchant_category_resolver(self):
        cart = make_cart(merchant=make_merchant(category="grocery"))
        assert field_registry.resolve("merchant.category", cart) == "grocery"

    def test_item_ingredients_resolver_includes_all(self):
        cart = make_cart(items=[
            CartItem(name="Beef Burger", quantity=1, unit_price=Decimal("300"),
                     ingredients=["beef", "lettuce", "tomato"]),
            CartItem(name="Fries", quantity=1, unit_price=Decimal("100"),
                     ingredients=["potato", "oil"]),
        ])
        ings = field_registry.resolve("item.ingredients", cart)
        assert "beef" in ings
        assert "potato" in ings
        assert "lettuce" in ings

    def test_item_ingredients_lowercased(self):
        cart = make_cart(items=[
            CartItem(name="X", quantity=1, unit_price=Decimal("100"),
                     ingredients=["BEEF", "Chicken"]),
        ])
        ings = field_registry.resolve("item.ingredients", cart)
        assert "beef" in ings
        assert "chicken" in ings
        assert "BEEF" not in ings

    def test_item_ingredients_empty_for_undeclared(self):
        """
        An item with no declared ingredients contributes nothing.
        Callers that need ingredient coverage must check
        item.missing_ingredient_count separately.
        """
        cart = make_cart(items=[
            CartItem(name="Mystery Item", quantity=1, unit_price=Decimal("100"),
                     ingredients=[]),
        ])
        ings = field_registry.resolve("item.ingredients", cart)
        assert ings == []

    def test_missing_ingredient_count_resolver(self):
        cart = make_cart(items=[
            CartItem(name="A", quantity=1, unit_price=Decimal("100"),
                     ingredients=["x"]),          # declared
            CartItem(name="B", quantity=1, unit_price=Decimal("100"),
                     ingredients=[]),              # undeclared
            CartItem(name="C", quantity=1, unit_price=Decimal("100"),
                     ingredients=[]),              # undeclared
        ])
        count = field_registry.resolve("item.missing_ingredient_count", cart)
        assert count == 2

    def test_no_beef_dietary_constraint(self):
        """Full dietary check: 'no beef' → ingredient excludes 'beef' → FAIL."""
        cart_with_beef = make_cart(items=[
            CartItem(name="Beef Biryani", quantity=4, unit_price=Decimal("350"),
                     ingredients=["beef", "rice", "spices"]),
        ])
        cart_without_beef = make_cart(items=[
            CartItem(name="Chicken Biryani", quantity=4, unit_price=Decimal("300"),
                     ingredients=["chicken", "rice", "spices"]),
        ])
        ings_beef = field_registry.resolve("item.ingredients", cart_with_beef)
        ings_clean = field_registry.resolve("item.ingredients", cart_without_beef)

        assert "beef" in ings_beef       # constraint verifier would FAIL this
        assert "beef" not in ings_clean  # constraint verifier would PASS this

    def test_fulfilment_eta_resolver(self):
        eta = datetime(2024, 6, 1, 21, 0, tzinfo=timezone.utc)
        cart = make_cart(fulfilment_eta=eta)
        assert field_registry.resolve("fulfilment_eta", cart) == eta

    def test_distinct_item_count_resolver(self):
        cart = make_cart(items=[
            CartItem(name="Biryani", quantity=2, unit_price=Decimal("300")),
            CartItem(name="biryani", quantity=1, unit_price=Decimal("300")),  # same, different case
            CartItem(name="Raita",   quantity=1, unit_price=Decimal("60")),
        ])
        count = field_registry.resolve("distinct_item_count", cart)
        assert count == 2  # "biryani" deduped to 1

    def test_max_min_item_quantity(self):
        cart = make_cart(items=[
            CartItem(name="A", quantity=1, unit_price=Decimal("100")),
            CartItem(name="B", quantity=8, unit_price=Decimal("200")),
            CartItem(name="C", quantity=3, unit_price=Decimal("150")),
        ])
        assert field_registry.resolve("max_item_quantity", cart) == 8
        assert field_registry.resolve("min_item_quantity", cart) == 1

    def test_item_categories_resolver(self):
        cart = make_cart(items=[
            CartItem(name="A", quantity=1, unit_price=Decimal("100"), category="veg"),
            CartItem(name="B", quantity=1, unit_price=Decimal("100"), category="Non-Veg"),
            CartItem(name="C", quantity=1, unit_price=Decimal("100"), category=None),
        ])
        cats = field_registry.resolve("item.categories", cart)
        assert "veg" in cats
        assert "non-veg" in cats
        assert len(cats) == 2   # None excluded


# ── Hash chain integrity — end-to-end ─────────────────────────────────────

class TestHashChainIntegrity:
    """
    Verify the hash chain anchoring:
        Obligation → EvidenceItem → ClearingDecision → SettlementInstruction
    """

    def test_full_chain_links_correctly(self):
        # 1. Obligation
        obl = bind_obligation(make_obligation())
        assert len(obl.hash) == 64

        # 2. EvidenceItem anchored to the obligation
        ev = bind_evidence(make_unbound_evidence(obligation_hash=obl.hash))
        assert ev.obligation_hash == obl.hash
        assert len(ev.hash) == 64

        # 3. VerifierOutput citing the evidence
        vo = VerifierOutput(
            role="constraint",
            verdict=Verdict.FAIL,
            confidence=1.0,
            declared_basis=[ev.id],
            loss_estimate=Decimal("800.00"),
        )

        # 4. ClearingDecision
        cd_unbound = ClearingDecision(
            obligation_hash=obl.hash,
            performance_verdict=Verdict.FAIL,
            policy_verdict=Verdict.FAIL,
            aggregate_basis=[ev.id],
            basis_class=EvidenceClass.REC,
            confidence=1.0,
            finality=Finality.FINAL,
            verifier_outputs=[vo],
        )
        cd = cd_unbound.model_copy(update={"hash": _compute_clearing_hash(cd_unbound)})
        assert cd.obligation_hash == obl.hash
        assert len(cd.hash) == 64

        # 5. SettlementInstruction
        si_unbound = SettlementInstruction(
            clearing_decision_hash=cd.hash,
            action=SettlementAction.ABORT,
            reason_code="QUANTITY_MISMATCH",
        )
        si = si_unbound.model_copy(
            update={"hash": _compute_settlement_hash(si_unbound)}
        )
        assert si.clearing_decision_hash == cd.hash
        assert len(si.hash) == 64

    def test_tampered_evidence_breaks_chain(self):
        """
        Changing the evidence item's payload without rebinding should be
        caught when the item is reconstructed from its tampered JSON dump.
        """
        ev = make_bound_evidence()
        data = ev.model_dump()
        data["payload"] = {"tampered": True}
        with pytest.raises(ValidationError, match="hash integrity"):
            EvidenceItem(**data)
