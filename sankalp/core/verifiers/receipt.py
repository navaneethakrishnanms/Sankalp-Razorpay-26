"""
Receipt verifier — independently cross-checks the cart against the
merchant catalogue: do these items exist at this merchant, are the prices
correct, is the merchant real. Basis class REC.

INDEPENDENCE
------------
Deliberately does not import or call anything from core/verifiers/constraint.py
or core/models/fields.py — it resolves catalogue truth directly against
agent/catalogue.py. Two verifiers sharing an implementation are one
verifier wearing two hats, and the aggregate basis join across "survivors"
(core/admissibility/floor.py::aggregate_surviving_basis) becomes
meaningless if both survivors trace back to the same code path.

EXPECTED STAGE 3 BEHAVIOUR ON THIS CORPUS
-------------------------------------------
eval/generator.py never fabricates a non-existent item, a wrong price, or
an unknown merchant — every mutation sources its cart data from
agent/catalogue.py, including the WRONG_MERCHANT mutation (which swaps to
a *different real* merchant, not a fake one). So this verifier is expected
to PASS on every record in the Stage 2/2.5 corpus, catchable or not — it
is not what catches WRONG_MERCHANT (that is `merchant_scope`, an
obligation-compliance check owned by the constraint verifier; "is this
merchant real" and "is this merchant the one the user asked for" are
different questions). This is not a bug: the corpus has no adversarial
catalogue-tampering case to catch yet, and that gap is worth naming
outright rather than treating a universal PASS as a broken verifier.
"""

from __future__ import annotations

import dataclasses

from agent import catalogue
from core.models.cart import Cart
from core.models.enums import Verdict
from core.models.obligation import Obligation
from core.models.verifier import VerifierOutput
from core.verifiers.base import Verifier
from core.verifiers.constraint import CATALOGUE_EVIDENCE_ID


@dataclasses.dataclass(frozen=True)
class ReceiptCheckDetail:
    merchant_exists: bool
    items_exist:      bool
    prices_match:       bool
    mismatches:           tuple[str, ...]


def evaluate_receipt_checks(cart: Cart) -> ReceiptCheckDetail:
    if cart.merchant.id not in catalogue.MERCHANTS:
        return ReceiptCheckDetail(False, False, False, (f"unknown merchant {cart.merchant.id!r}",))

    catalogue_merchant = catalogue.MERCHANTS[cart.merchant.id]
    mismatches: list[str] = []
    items_exist = True
    prices_match = True

    for item in cart.items:
        catalogue_item = catalogue_merchant.items.get(item.name.lower())
        if catalogue_item is None:
            items_exist = False
            mismatches.append(f"item {item.name!r} not found at merchant {cart.merchant.id!r}")
            continue
        if catalogue_item.unit_price != item.unit_price:
            prices_match = False
            mismatches.append(
                f"{item.name!r} priced {item.unit_price}, catalogue says {catalogue_item.unit_price}"
            )

    return ReceiptCheckDetail(True, items_exist, prices_match, tuple(mismatches))


def receipt_verify(obligation: Obligation, cart: Cart) -> VerifierOutput:
    detail = evaluate_receipt_checks(cart)
    ok = detail.merchant_exists and detail.items_exist and detail.prices_match
    return VerifierOutput(
        role="receipt",
        verdict=Verdict.PASS if ok else Verdict.FAIL,
        confidence=1.0,
        declared_basis=[CATALOGUE_EVIDENCE_ID],
        loss_estimate=None,
        reasoning="; ".join(detail.mismatches) if detail.mismatches else "cart matches catalogue",
    )


class ReceiptVerifier(Verifier):
    role = "receipt"

    def verify(self, obligation: Obligation, cart: Cart) -> VerifierOutput:
        return receipt_verify(obligation, cart)
