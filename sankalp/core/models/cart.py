"""
Cart and its constituent parts.

Cart is immutable once constructed.  The constraint verifier reads it;
nothing writes to it after the agent submits it.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field


class Merchant(BaseModel):
    model_config = {"frozen": True}

    id:       str
    name:     str
    category: str   # e.g. "food_delivery", "grocery"


class CartItem(BaseModel):
    model_config = {"frozen": True}

    name:        str
    quantity:    int = Field(ge=1)
    unit_price:  Decimal
    # ingredients must be provided by the synthetic catalogue for dietary checks.
    # An empty list means "unknown" — the receipt verifier treats this as
    # an ABSTAIN on any dietary constraint, not a pass.
    ingredients: list[str] = Field(default_factory=list)
    # Optional free-form category tag (e.g. "non-veg", "veg", "dessert").
    category:    str | None = None


class Cart(BaseModel):
    model_config = {"frozen": True}

    items:          list[CartItem]
    merchant:       Merchant
    # total is the agent-declared value.  validate_total() checks it against
    # the arithmetic sum.  A mismatch is a constraint violation in itself.
    total:          Decimal
    fulfilment_eta: datetime

    def validate_total(self) -> bool:
        """
        Return True iff the declared total equals the arithmetic sum of
        (unit_price × quantity) across all line items.

        Called by the constraint verifier; a mismatch is reported as a
        FAIL on the 'total' field criterion.
        """
        computed = sum(item.unit_price * item.quantity for item in self.items)
        return computed == self.total
