"""
Tests for mcp_server/server.py.

`clear_intent` itself calls the real obligation compiler and, for semantic
criteria, the real semantic verifier — both are live LLM calls, same as
core/obligation/compiler.py and core/verifiers/semantic.py always have been.
There is no cassette for arbitrary free-text MCP input, so this file tests
the deterministic parts directly: cart parsing (the most likely place for a
malformed external payload to break something) and verifier-row
serialisation (the shape a caller actually receives). Exercising
`clear_intent` end-to-end requires a live GROQ_API_KEY/ANTHROPIC_API_KEY —
run it manually via `python -m mcp_server.server` against an MCP client, or
call `clear_intent(...)` directly in a REPL with a key configured.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

from core.models.enums import EvidenceClass, Verdict
from core.models.verifier import VerifierOutput
from mcp_server.server import CartIn, CartItemIn, _parse_cart, _parse_decimal, _verifier_row


def _cart_in(**overrides) -> CartIn:
    defaults = dict(
        merchant_id="ext-merchant-1", merchant_name="Test Kitchen", merchant_category="food_delivery",
        items=[CartItemIn(name="Paneer Tikka", quantity=2, unit_price="180.00",
                            ingredients=["paneer", "spices"], category="veg")],
        total="360.00", fulfilment_eta="2026-09-05T20:30:00+05:30",
    )
    defaults.update(overrides)
    return CartIn(**defaults)


class TestParseDecimal:
    def test_parses_a_valid_amount(self):
        assert _parse_decimal("280.00", "total") == Decimal("280.00")

    def test_rejects_a_non_numeric_string(self):
        with pytest.raises(ValueError, match="total"):
            _parse_decimal("not-a-number", "total")


class TestParseCart:
    def test_builds_a_real_cart_from_typed_input(self):
        cart = _parse_cart(_cart_in())
        assert cart.merchant.id == "ext-merchant-1"
        assert cart.items[0].name == "Paneer Tikka"
        assert cart.items[0].unit_price == Decimal("180.00")
        assert cart.items[0].quantity == 2
        assert cart.total == Decimal("360.00")
        assert isinstance(cart.fulfilment_eta, datetime)

    def test_lower_cases_ingredients(self):
        cart = _parse_cart(_cart_in(items=[
            CartItemIn(name="X", quantity=1, unit_price="10", ingredients=["CHICKEN", "Rice"]),
        ]))
        assert cart.items[0].ingredients == ["chicken", "rice"]

    def test_bad_decimal_raises_value_error(self):
        with pytest.raises(ValueError):
            _parse_cart(_cart_in(total="not-money"))

    def test_bad_datetime_raises_value_error(self):
        with pytest.raises(ValueError):
            _parse_cart(_cart_in(fulfilment_eta="not-a-date"))

    def test_cart_total_arithmetic_is_checkable_downstream(self):
        # Deliberately mismatched total: _parse_cart must not silently correct
        # it — that mismatch is exactly what the constraint verifier's
        # total_arithmetic check exists to catch.
        cart = _parse_cart(_cart_in(total="999.00"))
        assert cart.validate_total() is False


class TestVerifierRow:
    def test_reports_basis_class_from_evidence_index(self):
        v = VerifierOutput(role="constraint", verdict=Verdict.PASS, confidence=1.0,
                            declared_basis=["ev-catalogue"])
        row = _verifier_row(v, {"ev-catalogue": EvidenceClass.REC}, survived=True)
        assert row["basis_class"] == "REC"
        assert row["survived_floor"] is True
        assert row["verdict"] == "PASS"

    def test_unknown_basis_id_reports_self(self):
        v = VerifierOutput(role="semantic", verdict=Verdict.PASS, confidence=0.9,
                            declared_basis=["unregistered-id"])
        row = _verifier_row(v, {}, survived=False)
        assert row["basis_class"] == "SELF"
        assert row["survived_floor"] is False

    def test_empty_declared_basis_reports_self(self):
        v = VerifierOutput(role="constraint", verdict=Verdict.FAIL, confidence=1.0, declared_basis=[])
        row = _verifier_row(v, {"ev-catalogue": EvidenceClass.REC}, survived=False)
        assert row["basis_class"] == "SELF"
