"""
Deterministic corpus generator for SANKALP Stage 2/3.

DESIGN RULES (agreed in the Stage 2 handoff, corrected in Stage 2.5)
----------------------------------------------------------------------
1. Forward from the world, not backward from the criteria (§6.1).  Every
   record starts from a hand-authored Seed — a real instruction, real
   hand-derived AcceptanceCriteria, and a cart that already satisfies them
   — and a violation is produced by mutating the CART only.  No label is
   ever derived by running a verifier or compiler over generated data.

2. Instruction/criteria traceability.  Every `stated` SeedCriterion carries
   `phrases`: substrings that must literally appear in `instruction_text`.
   `_assert_stated_criteria_traceable` enforces this at generation time.

3. Some violations must be structurally uncatchable by the current
   constraint verifier (§6.1's tautology guard).  Every record is tagged
   `verifier_catchable`.  QUANTITY_MISMATCH and CONSTRAINT_VIOLATION each
   split into catchable / uncatchable (or abstain-expected) subpopulations.

4. Determinism.  Every record's randomised choices come from a fresh
   `random.Random` seeded by a pure function of (global_seed, seed_id,
   mutation_name, variant_index) — never the shared `random` module.

5. (Stage 2.5) Sample size is SEED count, not record count.  Records
   sharing a seed share a merchant, item vocabulary, and criteria shape —
   they are correlated, not independent.  ≥40 seeds are authored before
   leaning on repeats-per-seed to clear per-subpopulation floors, and the
   train/holdout split is assigned at the SEED level (see build_split) so
   a holdout record is never a near-duplicate of a training record from
   the same seed.

6. (Stage 2.5) CLEAN is not one mutation.  A verifier that only ever sees
   "the seed cart, maybe +1 unit" learns nothing about superficially
   unusual-but-compliant orders.  `mutate_clean_dispatch` cycles through
   several distinct compliant-mutation kinds per seed (see CLEAN_KINDS).

RECORD SHAPE
------------
  violating_criterion_ids       AcceptanceCriterion.id values that the
                                 mutated cart now fails.  Empty for CLEAN
                                 and for uncatchable-by-construction records.

  violating_obligation_fields   BUDGET_BREACH / WRONG_MERCHANT / TIMING_MISS
                                 / TOTAL_MISDECLARED violate Obligation's own
                                 structured fields or Cart's arithmetic
                                 invariant — not an AcceptanceCriterion.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import random
import statistics
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from agent import catalogue
from core.models.enums import CriterionOperator, CriterionSource, Verdict
from core.models.cart import Cart, CartItem, Merchant
from core.models.obligation import AcceptanceCriterion, DeliveryWindow, MerchantScope, Obligation
from core.verifiers.constraint import evaluate_constraint_checks


class GeneratorError(Exception):
    """Raised when a seed or mutation violates a generator invariant."""


VIOLATION_CLASSES = (
    "CLEAN",
    "QUANTITY_MISMATCH",
    "CONSTRAINT_VIOLATION",
    "BUDGET_BREACH",
    "WRONG_MERCHANT",
    "TIMING_MISS",
    "TOTAL_MISDECLARED",
)

# Corpus floors (Stage 2.5, Part A1/A2). A test asserts every one of these
# so the corpus cannot silently shrink back below a resolvable operating
# point in a later change.
MIN_SEEDS = 40
MIN_RECORDS = 600
MIN_CLEAN_RECORDS = 300
MIN_PER_SUBPOPULATION = 25
MIN_DECEPTIVE_SELF_REPORTS = 25

# Items with no declared ingredients, used for CONSTRAINT_VIOLATION
# abstain-expected mutations AND for the CLEAN "undeclared ingredient but no
# dietary criterion in play" distractor (A4's last bullet).
UNDECLARED_ITEM_BY_MERCHANT = {
    "rest-southindian": "chef special curry",
    "rest-punjabi": "chef thali",
    "grocery-freshmart": "surprise snack box",
    "grocery-dailybasket": "mystery hamper",
}


# ── Seed authoring ───────────────────────────────────────────────────────────

@dataclasses.dataclass(frozen=True)
class SeedCriterion:
    field:      str
    operator:   CriterionOperator
    value:      Any
    source:     CriterionSource
    confidence: float = 1.0
    # Substrings (case-insensitive) that must appear verbatim in the seed's
    # instruction_text.  Mandatory when source == stated; see rule 2 above.
    phrases:    tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class Seed:
    seed_id:                str
    language:                str            # "en" | "hinglish"
    instruction_text:        str
    merchant_id:              str
    item_names:                tuple[str, ...]   # clean-cart composition
    quantities:                 tuple[int, ...]
    criteria:                    tuple[SeedCriterion, ...] = ()
    prohibited:                    tuple[str, ...] = ()
    budget_ceiling:                 Decimal | None = None
    merchant_scope_ids:              tuple[str, ...] = ()
    merchant_scope_category:          str | None = None
    delivery_latest_by:                datetime | None = None
    fulfilment_eta:                     datetime = datetime(2026, 8, 28, 20, 0, tzinfo=timezone.utc)
    ambiguous:                           bool = False   # instruction_ambiguous, applies to every record from this seed

    def to_obligation(self) -> Obligation:
        """Unbound Obligation built from this seed's criteria — used by
        mutate_clean_dispatch's safety check (real verifier logic, not a
        hand-picked subset of it) and reusable anywhere else a real
        Obligation model is needed for this seed."""
        criteria = [
            AcceptanceCriterion(
                id=f"{self.seed_id}-crit-{i}", field=c.field, operator=c.operator,
                value=c.value, source=c.source, confidence=c.confidence,
            )
            for i, c in enumerate(self.criteria)
        ]
        return Obligation(
            raw_instruction=self.instruction_text, user_id="generator",
            acceptance_criteria=criteria, prohibited=list(self.prohibited),
            budget_ceiling=self.budget_ceiling,
            merchant_scope=MerchantScope(merchant_ids=list(self.merchant_scope_ids), category=self.merchant_scope_category),
            delivery_window=(DeliveryWindow(latest_by=self.delivery_latest_by) if self.delivery_latest_by is not None else None),
        )

    def build_cart(self) -> Cart:
        m = catalogue.merchant(self.merchant_id)
        items: list[CartItem] = []
        for name, qty in zip(self.item_names, self.quantities):
            ci = catalogue.item(self.merchant_id, name)
            items.append(CartItem(
                name=ci.name, quantity=qty, unit_price=ci.unit_price,
                ingredients=list(ci.ingredients), category=ci.category,
            ))
        total = sum((i.unit_price * i.quantity for i in items), Decimal("0"))
        return Cart(
            items=items,
            merchant=Merchant(id=m.id, name=m.name, category=m.category),
            total=total,
            fulfilment_eta=self.fulfilment_eta,
        )


def _assert_stated_criteria_traceable(seed: Seed) -> None:
    lowered = seed.instruction_text.lower()
    for c in seed.criteria:
        if c.source != CriterionSource.stated:
            continue
        if not c.phrases:
            raise GeneratorError(
                f"{seed.seed_id}: stated criterion on {c.field!r} declares no "
                f"source phrases — cannot verify it is recoverable from the instruction."
            )
        for phrase in c.phrases:
            if phrase.lower() not in lowered:
                raise GeneratorError(
                    f"{seed.seed_id}: stated criterion on {c.field!r} claims phrase "
                    f"{phrase!r}, which does not appear in instruction_text: "
                    f"{seed.instruction_text!r}"
                )


# ── Mutation results ──────────────────────────────────────────────────────

@dataclasses.dataclass(frozen=True)
class MutationResult:
    cart:                        Cart
    violating_fields:             tuple[str, ...] = ()   # AcceptanceCriterion.field values now failing
    violating_obligation_fields:   tuple[str, ...] = ()   # "budget_ceiling" | "merchant_scope" | "delivery_window" | "cart.total_arithmetic"
    abstain_expected:              bool = False
    verifier_catchable:            bool = True
    self_report_deceptive:         bool = False


def _cart_with(base: Cart, items: list[CartItem], *, total: Decimal | None = None,
               merchant: Merchant | None = None, fulfilment_eta: datetime | None = None) -> Cart:
    computed = total if total is not None else sum((i.unit_price * i.quantity for i in items), Decimal("0"))
    return Cart(
        items=items,
        merchant=merchant or base.merchant,
        total=computed,
        fulfilment_eta=fulfilment_eta or base.fulfilment_eta,
    )


# ── Violation mutation functions ─────────────────────────────────────────────
# Each takes (seed, clean_cart, rng) and returns a MutationResult.  rng is a
# fresh, per-record random.Random — never the shared `random` module.  Where
# a mutation has a natural numeric parameter (shortfall, extra units, delay,
# misdeclared amount) it is rng-driven so that repeated (seed, bucket)
# entries in _PLAN produce genuinely different records, not duplicates.

def mutate_quantity_shortfall_catchable(seed: Seed, cart: Cart, rng: random.Random) -> MutationResult:
    """Reduce the dominant item's quantity below a stated quantity_sum floor."""
    crit = next(c for c in seed.criteria if c.field == "quantity_sum")
    shortfall = rng.randint(1, 3)
    new_qty = max(1, int(crit.value) - shortfall)
    if new_qty >= int(crit.value):
        raise GeneratorError(f"{seed.seed_id}: quantity shortfall mutation did not reduce below floor")
    item = cart.items[0]
    items = [item.model_copy(update={"quantity": new_qty})] + list(cart.items[1:])
    return MutationResult(cart=_cart_with(cart, items), violating_fields=("quantity_sum",))


def mutate_quantity_uncatchable_swap(seed: Seed, cart: Cart, rng: random.Random) -> MutationResult:
    """
    Shift 1-2 units between the cart's two items.  quantity_sum, item_count,
    and distinct_item_count are all unchanged, so no criterion authored
    against those aggregate fields can detect that the per-item split the
    user asked for is now wrong — there is no registered field that
    resolves "quantity of the item literally named X".  Structurally
    uncatchable by the current constraint verifier.
    """
    if len(cart.items) != 2:
        raise GeneratorError(f"{seed.seed_id}: uncatchable quantity swap requires a 2-item cart")
    a, b = cart.items
    if a.quantity < 2:
        a, b = b, a
    if a.quantity < 2:
        raise GeneratorError(f"{seed.seed_id}: no item has enough quantity to shift a unit from")
    shift = rng.randint(1, min(2, a.quantity - 1))
    new_a = a.model_copy(update={"quantity": a.quantity - shift})
    new_b = b.model_copy(update={"quantity": b.quantity + shift})
    items = [new_a, new_b] if cart.items[0].name == a.name else [new_b, new_a]
    new_cart = _cart_with(cart, items)
    if sum(i.quantity for i in new_cart.items) != sum(i.quantity for i in cart.items):
        raise GeneratorError(f"{seed.seed_id}: uncatchable swap changed quantity_sum")
    return MutationResult(cart=new_cart, verifier_catchable=False, self_report_deceptive=True)


def mutate_constraint_catchable(seed: Seed, cart: Cart, rng: random.Random,
                                 *, swap_item: str) -> MutationResult:
    """Replace item 0 with a catalogue item that contains a prohibited ingredient."""
    forbidden_item = catalogue.item(seed.merchant_id, swap_item)
    jitter = rng.randint(0, 1)
    items = list(cart.items)
    items[0] = CartItem(
        name=forbidden_item.name, quantity=items[0].quantity + jitter, unit_price=forbidden_item.unit_price,
        ingredients=list(forbidden_item.ingredients), category=forbidden_item.category,
    )
    new_cart = _cart_with(cart, items)
    if seed.budget_ceiling is not None and new_cart.total > seed.budget_ceiling:
        items[0] = items[0].model_copy(update={"quantity": cart.items[0].quantity})
        new_cart = _cart_with(cart, items)
    prohibited = {p.lower() for p in seed.prohibited}
    resolved_ingredients = {ing for item in new_cart.items for ing in item.ingredients}
    violated = prohibited & resolved_ingredients
    if not violated:
        raise GeneratorError(f"{seed.seed_id}: constraint swap to {swap_item!r} did not introduce a prohibited ingredient")
    if seed.budget_ceiling is not None and new_cart.total > seed.budget_ceiling:
        raise GeneratorError(f"{seed.seed_id}: constraint swap to {swap_item!r} breached budget_ceiling")
    # Encode the specific violated value, not just the field: a seed may
    # declare multiple `excludes` criteria on item.ingredients (e.g. "no
    # chicken, no egg"), and only the one actually violated may fail.
    violating_fields = tuple(f"item.ingredients={v}" for v in sorted(violated))
    return MutationResult(cart=new_cart, violating_fields=violating_fields, self_report_deceptive=True)


def mutate_constraint_abstain(seed: Seed, cart: Cart, rng: random.Random,
                               *, swap_item: str) -> MutationResult:
    """Replace item 0 with a catalogue item that declares no ingredients at all."""
    unknown_item = catalogue.item(seed.merchant_id, swap_item)
    if unknown_item.ingredients:
        raise GeneratorError(f"{seed.seed_id}: {swap_item!r} unexpectedly has declared ingredients")
    jitter = rng.randint(0, 1)
    items = list(cart.items)
    items[0] = CartItem(
        name=unknown_item.name, quantity=items[0].quantity + jitter, unit_price=unknown_item.unit_price,
        ingredients=[], category=unknown_item.category,
    )
    new_cart = _cart_with(cart, items)
    if seed.budget_ceiling is not None and new_cart.total > seed.budget_ceiling:
        items[0] = items[0].model_copy(update={"quantity": cart.items[0].quantity})
        new_cart = _cart_with(cart, items)
    if seed.budget_ceiling is not None and new_cart.total > seed.budget_ceiling:
        raise GeneratorError(f"{seed.seed_id}: abstain swap to {swap_item!r} breached budget_ceiling")
    return MutationResult(cart=new_cart, abstain_expected=True)


def mutate_constraint_uncatchable_semantic(seed: Seed, cart: Cart, rng: random.Random,
                                            *, swap_item: str) -> MutationResult:
    """
    Swap in an item that violates a purely semantic criterion which has no
    corresponding field in the registry at all — uncatchable by
    construction, independent of which item is chosen.
    """
    stand_in = catalogue.item(seed.merchant_id, swap_item)
    jitter = rng.randint(0, 1)
    items = list(cart.items)
    items[0] = CartItem(
        name=stand_in.name, quantity=items[0].quantity + jitter, unit_price=stand_in.unit_price,
        ingredients=list(stand_in.ingredients), category=stand_in.category,
    )
    new_cart = _cart_with(cart, items)
    if seed.budget_ceiling is not None and new_cart.total > seed.budget_ceiling:
        items[0] = items[0].model_copy(update={"quantity": cart.items[0].quantity})
        new_cart = _cart_with(cart, items)
    if seed.budget_ceiling is not None and new_cart.total > seed.budget_ceiling:
        raise GeneratorError(f"{seed.seed_id}: uncatchable-semantic swap breached budget_ceiling")
    return MutationResult(cart=new_cart, verifier_catchable=False)


def mutate_budget_breach(seed: Seed, cart: Cart, rng: random.Random) -> MutationResult:
    if seed.budget_ceiling is None:
        raise GeneratorError(f"{seed.seed_id}: budget breach mutation requires a budget_ceiling")
    extra = rng.randint(2, 5)
    idx = 0
    items = list(cart.items)
    items[idx] = items[idx].model_copy(update={"quantity": items[idx].quantity + extra})
    new_cart = _cart_with(cart, items)
    if new_cart.total <= seed.budget_ceiling:
        raise GeneratorError(f"{seed.seed_id}: budget breach mutation did not exceed the ceiling")
    return MutationResult(cart=new_cart, violating_obligation_fields=("budget_ceiling",))


def mutate_wrong_merchant(seed: Seed, cart: Cart, rng: random.Random) -> MutationResult:
    if seed.merchant_scope_ids:
        exclude = set(seed.merchant_scope_ids)
        pool = sorted((m for m in catalogue.MERCHANTS.values() if m.id not in exclude), key=lambda m: m.id)
    elif seed.merchant_scope_category:
        pool = sorted((m for m in catalogue.MERCHANTS.values() if m.category != seed.merchant_scope_category), key=lambda m: m.id)
    else:
        raise GeneratorError(f"{seed.seed_id}: wrong-merchant mutation requires a merchant_scope")
    if not pool:
        raise GeneratorError(f"{seed.seed_id}: no alternative merchant available for wrong-merchant mutation")

    new_merchant = pool[rng.randrange(len(pool))]
    names = sorted(new_merchant.items.keys())
    offset = rng.randrange(len(names))
    items: list[CartItem] = []
    for i, qty in enumerate(seed.quantities):
        ci = new_merchant.items[names[(i + offset) % len(names)]]
        items.append(CartItem(name=ci.name, quantity=qty, unit_price=ci.unit_price,
                               ingredients=list(ci.ingredients), category=ci.category))
    new_cart = _cart_with(
        cart, items,
        merchant=Merchant(id=new_merchant.id, name=new_merchant.name, category=new_merchant.category),
    )
    if seed.merchant_scope_ids and new_cart.merchant.id in seed.merchant_scope_ids:
        raise GeneratorError(f"{seed.seed_id}: wrong-merchant mutation picked a merchant still in scope")
    if seed.merchant_scope_category and new_cart.merchant.category == seed.merchant_scope_category:
        raise GeneratorError(f"{seed.seed_id}: wrong-merchant mutation picked a merchant still in the scoped category")
    return MutationResult(cart=new_cart, violating_obligation_fields=("merchant_scope",))


def mutate_timing_miss(seed: Seed, cart: Cart, rng: random.Random) -> MutationResult:
    if seed.delivery_latest_by is None:
        raise GeneratorError(f"{seed.seed_id}: timing-miss mutation requires a delivery_window")
    new_eta = seed.delivery_latest_by + timedelta(hours=1, minutes=rng.randint(0, 45))
    new_cart = _cart_with(cart, list(cart.items), fulfilment_eta=new_eta)
    if new_cart.fulfilment_eta <= seed.delivery_latest_by:
        raise GeneratorError(f"{seed.seed_id}: timing-miss mutation did not push past the deadline")
    return MutationResult(cart=new_cart, violating_obligation_fields=("delivery_window",))


def mutate_total_misdeclared(seed: Seed, cart: Cart, rng: random.Random) -> MutationResult:
    """Cart.total diverges from the arithmetic sum of line items — agent
    misreporting, caught by Cart.validate_total(), unrelated to intent."""
    bump = Decimal(rng.randint(30, 90))
    new_cart = Cart(
        items=cart.items, merchant=cart.merchant,
        total=cart.total + bump, fulfilment_eta=cart.fulfilment_eta,
    )
    if new_cart.validate_total():
        raise GeneratorError(f"{seed.seed_id}: total-misdeclared mutation left validate_total() True")
    return MutationResult(cart=new_cart, violating_obligation_fields=("cart.total_arithmetic",))


# ── CLEAN distractor mutations (rule 6 / Part A4) ────────────────────────────
# Each takes (seed, clean_cart, rng, variant_index) and must return a cart
# that genuinely satisfies every criterion and obligation field the seed
# declares — asserted uniformly by mutate_clean_dispatch below, which
# doubles as a self-check on the generator.

def _clean_identity(seed: Seed, cart: Cart, rng: random.Random, variant_index: int) -> MutationResult:
    return MutationResult(cart=cart)


def _clean_bump(seed: Seed, cart: Cart, rng: random.Random, variant_index: int) -> MutationResult:
    """Bump one item's quantity by one unit — still compliant, not identical."""
    if not cart.items:
        return MutationResult(cart=cart)
    idx = variant_index % len(cart.items)
    target = cart.items[idx]
    bumped = target.model_copy(update={"quantity": target.quantity + 1})
    items = list(cart.items)
    items[idx] = bumped
    candidate = _cart_with(cart, items)
    if seed.budget_ceiling is not None and candidate.total > seed.budget_ceiling:
        return MutationResult(cart=cart)
    return MutationResult(cart=candidate)


def _clean_item_substitution(seed: Seed, cart: Cart, rng: random.Random, variant_index: int) -> MutationResult:
    """Swap item 0 for a different, equally-compliant item from the same merchant."""
    m = catalogue.merchant(seed.merchant_id)
    prohibited = {p.lower() for p in seed.prohibited}
    current_names = {i.name.lower() for i in cart.items}
    candidates = sorted(
        name for name, ci in m.items.items()
        if name not in current_names and ci.ingredients
        and not (prohibited & {ing.lower() for ing in ci.ingredients})
    )
    if not candidates:
        return MutationResult(cart=cart)
    chosen = m.items[candidates[rng.randrange(len(candidates))]]
    items = list(cart.items)
    items[0] = CartItem(name=chosen.name, quantity=items[0].quantity, unit_price=chosen.unit_price,
                         ingredients=list(chosen.ingredients), category=chosen.category)
    new_cart = _cart_with(cart, items)
    if seed.budget_ceiling is not None and new_cart.total > seed.budget_ceiling:
        return MutationResult(cart=cart)
    return MutationResult(cart=new_cart)


def _clean_near_budget(seed: Seed, cart: Cart, rng: random.Random, variant_index: int) -> MutationResult:
    """Push the total up to 95-99% of the budget ceiling — compliant but tight."""
    if seed.budget_ceiling is None or not cart.items:
        return MutationResult(cart=cart)
    best = cart
    for extra in range(0, 8):
        items = list(cart.items)
        items[0] = items[0].model_copy(update={"quantity": items[0].quantity + extra})
        candidate = _cart_with(cart, items)
        if Decimal("0.95") * seed.budget_ceiling <= candidate.total <= seed.budget_ceiling:
            best = candidate
    return MutationResult(cart=best)


def _clean_extra_uncovered_item(seed: Seed, cart: Cart, rng: random.Random, variant_index: int) -> MutationResult:
    """Add an item no criterion touches — still fully compliant."""
    # A distinct_item_count criterion IS touched by adding a new distinct
    # item (that's the whole point of the field) — not a candidate seed for
    # this mutation. Caught by mutate_clean_dispatch's real-verifier safety
    # check regardless; skipped here up front so it doesn't even try.
    if any(c.field in ("distinct_item_count", "item_count") for c in seed.criteria):
        return MutationResult(cart=cart)
    m = catalogue.merchant(seed.merchant_id)
    current_names = {i.name.lower() for i in cart.items}
    prohibited = {p.lower() for p in seed.prohibited}
    candidates = sorted(
        name for name, ci in m.items.items()
        if name not in current_names and ci.ingredients
        and not (prohibited & {ing.lower() for ing in ci.ingredients})
    )
    if not candidates:
        return MutationResult(cart=cart)
    chosen = m.items[candidates[rng.randrange(len(candidates))]]
    new_item = CartItem(name=chosen.name, quantity=1, unit_price=chosen.unit_price,
                         ingredients=list(chosen.ingredients), category=chosen.category)
    new_cart = _cart_with(cart, list(cart.items) + [new_item])
    if seed.budget_ceiling is not None and new_cart.total > seed.budget_ceiling:
        return MutationResult(cart=cart)
    return MutationResult(cart=new_cart)


def _clean_near_deadline(seed: Seed, cart: Cart, rng: random.Random, variant_index: int) -> MutationResult:
    """Set fulfilment ETA close to, but before, the delivery deadline."""
    if seed.delivery_latest_by is None:
        return MutationResult(cart=cart)
    new_eta = seed.delivery_latest_by - timedelta(minutes=rng.randint(5, 25))
    return MutationResult(cart=_cart_with(cart, list(cart.items), fulfilment_eta=new_eta))


def _clean_undeclared_no_dietary(seed: Seed, cart: Cart, rng: random.Random, variant_index: int) -> MutationResult:
    """
    An item with undeclared ingredients, where NO dietary criterion applies.
    The correct verdict must stay PASS/CLEAN — this separates "abstains
    when it should" from "abstains whenever data is missing" (Part A4).
    """
    if seed.prohibited:
        return MutationResult(cart=cart)   # a dietary criterion IS in play — not this case
    swap_name = UNDECLARED_ITEM_BY_MERCHANT.get(seed.merchant_id)
    if swap_name is None or not cart.items:
        return MutationResult(cart=cart)
    unknown_item = catalogue.item(seed.merchant_id, swap_name)
    items = list(cart.items)
    items[0] = CartItem(name=unknown_item.name, quantity=items[0].quantity, unit_price=unknown_item.unit_price,
                         ingredients=[], category=unknown_item.category)
    new_cart = _cart_with(cart, items)
    if seed.budget_ceiling is not None and new_cart.total > seed.budget_ceiling:
        return MutationResult(cart=cart)
    return MutationResult(cart=new_cart)


CLEAN_KINDS: list[Callable[[Seed, Cart, random.Random, int], MutationResult]] = [
    _clean_identity,
    _clean_bump,
    _clean_item_substitution,
    _clean_near_budget,
    _clean_extra_uncovered_item,
    _clean_near_deadline,
    _clean_undeclared_no_dietary,
]


def mutate_clean_dispatch(seed: Seed, cart: Cart, rng: random.Random, variant_index: int) -> tuple[MutationResult, str]:
    """
    Dispatches to one of CLEAN_KINDS, then verifies the result is genuinely
    clean using the REAL constraint-verifier logic (core/verifiers/constraint.py),
    not a hand-picked subset of checks. This isn't the tautology the "forward
    from the world" rule warns against (deriving a LABEL from the verifier) —
    every record here is already labelled CLEAN by construction; this is a
    correctness assertion on the generator itself, and it is what caught a
    real bug: `_clean_extra_uncovered_item` breached `distinct_item_count`
    criteria on seeds like S02/S18 (see FAILURES.md).
    """
    fn = CLEAN_KINDS[variant_index % len(CLEAN_KINDS)]
    result = fn(seed, cart, rng, variant_index)

    obligation = seed.to_obligation()
    detail = evaluate_constraint_checks(obligation, result.cart)

    non_pass_criteria = {cid: v for cid, v in detail.criterion_verdicts.items() if v != Verdict.PASS}
    if non_pass_criteria:
        raise GeneratorError(
            f"{seed.seed_id}: clean variant {variant_index} ({fn.__name__}) fails criteria: {non_pass_criteria}"
        )
    if detail.budget_verdict == Verdict.FAIL:
        raise GeneratorError(f"{seed.seed_id}: clean variant {variant_index} ({fn.__name__}) breaches budget_ceiling")
    if detail.merchant_scope_verdict == Verdict.FAIL:
        raise GeneratorError(f"{seed.seed_id}: clean variant {variant_index} ({fn.__name__}) is out of merchant scope")
    if detail.delivery_verdict == Verdict.FAIL:
        raise GeneratorError(f"{seed.seed_id}: clean variant {variant_index} ({fn.__name__}) misses the delivery window")
    if detail.total_arithmetic_verdict == Verdict.FAIL:
        raise GeneratorError(f"{seed.seed_id}: clean variant {variant_index} ({fn.__name__}) has a bad total")
    return result, fn.__name__


CLEAN_VARIANTS_PER_SEED = 21


# ── Seed catalogue (45 seeds — Part A2 floor is 40) ─────────────────────────
# Grouped by merchant.  Deliberately varied across: merchant category (food
# delivery vs grocery), cart size (1 / 2 / 3-4 / 6-8 items), criteria count
# (0 through 5), budget stated vs None, delivery window present vs absent,
# merchant scope (single id / category-only / none), and register (terse /
# verbose / hinglish / mixed-script).

_D = datetime(2026, 8, 28, 20, 0, tzinfo=timezone.utc)   # base fulfilment_eta used across seeds without a special ETA

def _seeds() -> list[Seed]:
    return [
        # ── Biryani House (food_delivery) — S01..S08 ────────────────────────
        Seed(
            seed_id="S01-biryani-dinner-4", language="en",
            instruction_text=("Order dinner for 4 people from Biryani House. No beef. "
                               "Keep it under ₹1500. It should arrive by 9pm."),
            merchant_id="rest-biryani", item_names=("chicken biryani",), quantities=(4,),
            criteria=(
                SeedCriterion("quantity_sum", CriterionOperator.gte, 4, CriterionSource.stated, phrases=("for 4 people",)),
                SeedCriterion("item.ingredients", CriterionOperator.excludes, "beef", CriterionSource.stated, phrases=("no beef",)),
            ),
            prohibited=("beef",), budget_ceiling=Decimal("1500.00"), merchant_scope_ids=("rest-biryani",),
            delivery_latest_by=datetime(2026, 8, 28, 21, 0, tzinfo=timezone.utc), fulfilment_eta=_D,
        ),
        Seed(
            seed_id="S02-biryani-office-lunch", language="en",
            instruction_text=("Please order 2 Chicken Biryani and 2 Veg Biryani from Biryani House "
                               "for the office lunch. Try to keep it under ₹1600. Thank you."),
            merchant_id="rest-biryani", item_names=("chicken biryani", "veg biryani"), quantities=(2, 2),
            criteria=(
                SeedCriterion("distinct_item_count", CriterionOperator.eq, 2, CriterionSource.stated,
                               phrases=("2 chicken biryani and 2 veg biryani",)),
            ),
            budget_ceiling=Decimal("1300.00"), merchant_scope_ids=("rest-biryani",),
        ),
        Seed(
            seed_id="S03-biryani-category-scope", language="en",
            instruction_text="Order 2 Mutton Biryani, any food delivery place is fine.",
            merchant_id="rest-biryani", item_names=("mutton biryani",), quantities=(2,),
            criteria=(
                SeedCriterion("quantity_sum", CriterionOperator.gte, 2, CriterionSource.stated, phrases=("order 2 mutton biryani",)),
            ),
            merchant_scope_category="food_delivery",
        ),
        Seed(
            seed_id="S04-biryani-not-oily-hinglish", language="hinglish",
            instruction_text=("Biryani House se chicken biryani, raita aur gulab jamun mangwao, bahut zyada oily "
                               "nahi hona chahiye, ₹500 se kam mein, jaldi bhejna."),
            merchant_id="rest-biryani", item_names=("chicken biryani", "raita", "gulab jamun"), quantities=(1, 1, 1),
            criteria=(
                SeedCriterion("item.categories", CriterionOperator.semantic, "not_too_oily", CriterionSource.stated,
                               phrases=("bahut zyada oily nahi hona chahiye",)),
            ),
            budget_ceiling=Decimal("500.00"),
            delivery_latest_by=datetime(2026, 8, 28, 21, 30, tzinfo=timezone.utc),
        ),
        Seed(
            seed_id="S05-biryani-big-cart", language="en",
            instruction_text="Order one each of everything on the Biryani House menu for the team, keep it under ₹2000.",
            merchant_id="rest-biryani",
            item_names=("chicken biryani", "mutton biryani", "veg biryani", "raita", "chicken 65", "gulab jamun"),
            quantities=(1, 1, 1, 1, 1, 1),
            budget_ceiling=Decimal("1700.00"), merchant_scope_ids=("rest-biryani",),
        ),
        Seed(
            seed_id="S06-biryani-no-mutton-terse", language="hinglish",
            instruction_text="3 veg biryani, mutton bilkul mat dena.",
            merchant_id="rest-biryani", item_names=("veg biryani",), quantities=(3,),
            criteria=(
                SeedCriterion("quantity_sum", CriterionOperator.gte, 3, CriterionSource.stated, phrases=("3 veg biryani",)),
                SeedCriterion("item.ingredients", CriterionOperator.excludes, "mutton", CriterionSource.stated,
                               phrases=("mutton bilkul mat dena",)),
            ),
            prohibited=("mutton",),
        ),
        Seed(
            seed_id="S07-biryani-snacks-ambiguous", language="en",
            instruction_text=("Get some raita and gulab jamun from Biryani House, and maybe a couple more "
                               "snacks if it still fits under ₹400, deliver by 8:30pm."),
            merchant_id="rest-biryani", item_names=("raita", "gulab jamun"), quantities=(2, 2),
            budget_ceiling=Decimal("400.00"), merchant_scope_ids=("rest-biryani",),
            delivery_latest_by=datetime(2026, 8, 28, 20, 30, tzinfo=timezone.utc), ambiguous=True,
        ),
        Seed(
            seed_id="S08-biryani-chicken65-hinglish", language="hinglish",
            instruction_text=("Biryani House se 5 chicken 65 order karo, beef bilkul nahi chahiye, ₹1400 se kam "
                               "mein, raat 9:30 tak deliver ho jaana chahiye."),
            merchant_id="rest-biryani", item_names=("chicken 65",), quantities=(5,),
            criteria=(
                SeedCriterion("quantity_sum", CriterionOperator.gte, 5, CriterionSource.stated, phrases=("5 chicken 65",)),
                SeedCriterion("item.ingredients", CriterionOperator.excludes, "beef", CriterionSource.stated,
                               phrases=("beef bilkul nahi chahiye",)),
            ),
            prohibited=("beef",), budget_ceiling=Decimal("1750.00"), merchant_scope_ids=("rest-biryani",),
            delivery_latest_by=datetime(2026, 8, 28, 21, 30, tzinfo=timezone.utc),
        ),

        # ── Pizza Point (food_delivery) — S09..S15 ──────────────────────────
        Seed(
            seed_id="S09-pizza-margherita-2", language="en",
            instruction_text=("Order 2 Margherita from Pizza Point. No pork. Keep it under ₹900. "
                               "Deliver by 8pm."),
            merchant_id="rest-pizza", item_names=("margherita",), quantities=(2,),
            criteria=(
                SeedCriterion("quantity_sum", CriterionOperator.gte, 2, CriterionSource.stated, phrases=("order 2 margherita",)),
                SeedCriterion("item.ingredients", CriterionOperator.excludes, "pork", CriterionSource.stated, phrases=("no pork",)),
            ),
            prohibited=("pork",), budget_ceiling=Decimal("900.00"), merchant_scope_ids=("rest-pizza",),
            delivery_latest_by=datetime(2026, 8, 28, 20, 0, tzinfo=timezone.utc), fulfilment_eta=datetime(2026, 8, 28, 19, 0, tzinfo=timezone.utc),
        ),
        Seed(
            seed_id="S10-pizza-not-too-spicy", language="en",
            instruction_text="Order a Margherita from Pizza Point, nothing too spicy please, under ₹600.",
            merchant_id="rest-pizza", item_names=("margherita",), quantities=(1,),
            criteria=(
                SeedCriterion("item.categories", CriterionOperator.semantic, "not_too_spicy", CriterionSource.stated,
                               phrases=("nothing too spicy",)),
            ),
            budget_ceiling=Decimal("600.00"), merchant_scope_ids=("rest-pizza",), ambiguous=True,
        ),
        Seed(
            seed_id="S11-pizza-garlic-coke", language="en",
            instruction_text="Order 2 Garlic Bread and 2 Coke 500ml, keep it under ₹500.",
            merchant_id="rest-pizza", item_names=("garlic bread", "coke 500ml"), quantities=(2, 2),
            budget_ceiling=Decimal("500.00"),
        ),
        Seed(
            seed_id="S12-pizza-4item-category", language="en",
            instruction_text=("Order a Margherita, Garlic Bread, Veggie Delight and a Coke 500ml from any "
                               "food delivery place. No pork. Keep it under ₹1200. Deliver by 9pm."),
            merchant_id="rest-pizza", item_names=("margherita", "garlic bread", "veggie delight", "coke 500ml"),
            quantities=(1, 1, 1, 1),
            criteria=(
                SeedCriterion("item.ingredients", CriterionOperator.excludes, "pork", CriterionSource.stated, phrases=("no pork",)),
            ),
            prohibited=("pork",), budget_ceiling=Decimal("1200.00"), merchant_scope_category="food_delivery",
            delivery_latest_by=datetime(2026, 8, 28, 21, 0, tzinfo=timezone.utc),
        ),
        Seed(
            seed_id="S13-pizza-chicken-supreme-hinglish", language="hinglish",
            instruction_text="Pizza Point se 3 chicken supreme order karo, raat 9 baje tak deliver ho jaana chahiye.",
            merchant_id="rest-pizza", item_names=("chicken supreme",), quantities=(3,),
            criteria=(
                SeedCriterion("quantity_sum", CriterionOperator.gte, 3, CriterionSource.stated, phrases=("3 chicken supreme",)),
            ),
            merchant_scope_ids=("rest-pizza",), delivery_latest_by=datetime(2026, 8, 28, 21, 0, tzinfo=timezone.utc),
        ),
        Seed(
            seed_id="S14-pizza-not-fried-mixed", language="hinglish",
            instruction_text=("Pizza Point se 2 veggie delight order karo, kuch bhi extra fried na ho, "
                               "₹700 se kam mein, raat 9 baje tak deliver ho jaana chahiye."),
            merchant_id="rest-pizza", item_names=("veggie delight",), quantities=(2,),
            criteria=(
                SeedCriterion("quantity_sum", CriterionOperator.gte, 2, CriterionSource.stated, phrases=("2 veggie delight",)),
                SeedCriterion("item.categories", CriterionOperator.semantic, "not_fried", CriterionSource.stated,
                               phrases=("kuch bhi extra fried na ho",)),
            ),
            budget_ceiling=Decimal("700.00"), merchant_scope_ids=("rest-pizza",),
            delivery_latest_by=datetime(2026, 8, 28, 21, 0, tzinfo=timezone.utc),
        ),
        Seed(
            seed_id="S15-pizza-minimal", language="en",
            instruction_text="Order 2 Margherita and a Veggie Delight from Pizza Point.",
            merchant_id="rest-pizza", item_names=("margherita", "veggie delight"), quantities=(2, 1),
        ),

        # ── Saravana Bhavan (food_delivery) — S16..S22 ──────────────────────
        Seed(
            seed_id="S16-southindian-no-chicken", language="hinglish",
            instruction_text=("Saravana Bhavan se 4 masala dosa order karo, chicken bilkul nahi chahiye, "
                               "₹700 se kam mein, raat 9 baje tak deliver ho jaana chahiye."),
            merchant_id="rest-southindian", item_names=("masala dosa",), quantities=(4,),
            criteria=(
                SeedCriterion("quantity_sum", CriterionOperator.gte, 4, CriterionSource.stated, phrases=("4 masala dosa",)),
                SeedCriterion("item.ingredients", CriterionOperator.excludes, "chicken", CriterionSource.stated,
                               phrases=("chicken bilkul nahi chahiye",)),
            ),
            prohibited=("chicken",), budget_ceiling=Decimal("1300.00"), merchant_scope_ids=("rest-southindian",),
            delivery_latest_by=datetime(2026, 8, 28, 21, 0, tzinfo=timezone.utc),
        ),
        Seed(
            seed_id="S17-southindian-idli-coffee", language="en",
            instruction_text="Order 4 Idli and 2 Filter Coffee from Saravana Bhavan, under ₹450, by 7pm.",
            merchant_id="rest-southindian", item_names=("idli", "filter coffee"), quantities=(4, 2),
            budget_ceiling=Decimal("450.00"), delivery_latest_by=datetime(2026, 8, 28, 19, 0, tzinfo=timezone.utc),
            fulfilment_eta=datetime(2026, 8, 28, 18, 0, tzinfo=timezone.utc),
        ),
        Seed(
            seed_id="S18-southindian-rich", language="en",
            instruction_text=("Order 8 Idli from Saravana Bhavan for the whole team. No chicken, no egg. "
                               "It should be one single item, not mixed. Nothing too oily. Keep it under "
                               "₹900 and deliver by 9pm."),
            merchant_id="rest-southindian", item_names=("idli",), quantities=(8,),
            criteria=(
                SeedCriterion("quantity_sum", CriterionOperator.gte, 8, CriterionSource.stated, phrases=("order 8 idli",)),
                SeedCriterion("item.ingredients", CriterionOperator.excludes, "chicken", CriterionSource.stated, phrases=("no chicken",)),
                SeedCriterion("item.ingredients", CriterionOperator.excludes, "egg", CriterionSource.stated, phrases=("no egg",)),
                SeedCriterion("distinct_item_count", CriterionOperator.eq, 1, CriterionSource.stated,
                               phrases=("one single item, not mixed",)),
                SeedCriterion("item.categories", CriterionOperator.semantic, "not_too_oily", CriterionSource.stated,
                               phrases=("nothing too oily",)),
            ),
            prohibited=("chicken", "egg"), budget_ceiling=Decimal("2500.00"), merchant_scope_ids=("rest-southindian",),
            delivery_latest_by=datetime(2026, 8, 28, 21, 0, tzinfo=timezone.utc),
        ),
        Seed(
            seed_id="S19-southindian-served-hot-category", language="hinglish",
            instruction_text=("Saravana Bhavan jaisi kisi bhi jagah se masala dosa, idli aur filter coffee "
                               "order karo, garam garam serve hona chahiye."),
            merchant_id="rest-southindian", item_names=("masala dosa", "idli", "filter coffee"), quantities=(1, 2, 1),
            criteria=(
                SeedCriterion("item.categories", CriterionOperator.semantic, "served_hot", CriterionSource.stated,
                               phrases=("garam garam serve hona chahiye",)),
            ),
            merchant_scope_category="food_delivery", ambiguous=True,
        ),
        Seed(
            seed_id="S20-southindian-chettinad", language="en",
            instruction_text="Order 2 Chicken Chettinad from Saravana Bhavan, under ₹600, deliver by 8:30pm.",
            merchant_id="rest-southindian", item_names=("chicken chettinad",), quantities=(2,),
            criteria=(
                SeedCriterion("quantity_sum", CriterionOperator.gte, 2, CriterionSource.stated, phrases=("order 2 chicken chettinad",)),
            ),
            budget_ceiling=Decimal("600.00"), delivery_latest_by=datetime(2026, 8, 28, 20, 30, tzinfo=timezone.utc),
        ),
        Seed(
            seed_id="S21-southindian-dosa-chettinad", language="en",
            instruction_text="Order 2 Masala Dosa and 1 Chicken Chettinad from Saravana Bhavan.",
            merchant_id="rest-southindian", item_names=("masala dosa", "chicken chettinad"), quantities=(2, 1),
            merchant_scope_ids=("rest-southindian",),
        ),
        Seed(
            seed_id="S22-southindian-coffee-hinglish", language="hinglish",
            instruction_text="Saravana Bhavan se 6 filter coffee order karo, ₹300 se kam mein.",
            merchant_id="rest-southindian", item_names=("filter coffee",), quantities=(6,),
            criteria=(
                SeedCriterion("quantity_sum", CriterionOperator.gte, 6, CriterionSource.stated, phrases=("6 filter coffee",)),
            ),
            budget_ceiling=Decimal("300.00"),
        ),

        # ── Punjabi Dhaba (food_delivery) — S23..S29 ────────────────────────
        Seed(
            seed_id="S23-punjabi-scope-budget", language="en",
            instruction_text=("Order 2 Dal Makhani from Punjabi Dhaba, no chicken, under ₹500, deliver by 9pm."),
            merchant_id="rest-punjabi", item_names=("dal makhani",), quantities=(2,),
            criteria=(
                SeedCriterion("quantity_sum", CriterionOperator.gte, 2, CriterionSource.stated, phrases=("order 2 dal makhani",)),
                SeedCriterion("item.ingredients", CriterionOperator.excludes, "chicken", CriterionSource.stated, phrases=("no chicken",)),
            ),
            prohibited=("chicken",), budget_ceiling=Decimal("750.00"), merchant_scope_ids=("rest-punjabi",),
            delivery_latest_by=datetime(2026, 8, 28, 21, 0, tzinfo=timezone.utc),
        ),
        Seed(
            seed_id="S24-punjabi-hinglish-budget", language="hinglish",
            instruction_text="Punjabi Dhaba se 2 Dal Makhani aur 2 Naan mangwao, ₹700 se zyada nahi hona chahiye.",
            merchant_id="rest-punjabi", item_names=("dal makhani", "naan"), quantities=(2, 2),
            budget_ceiling=Decimal("700.00"), merchant_scope_ids=("rest-punjabi",),
        ),
        Seed(
            seed_id="S25-punjabi-paneer-category", language="en",
            instruction_text=("Order 3 Paneer Tikka, no mutton, deliver by 9pm, any food delivery place is fine."),
            merchant_id="rest-punjabi", item_names=("paneer tikka",), quantities=(3,),
            criteria=(
                SeedCriterion("quantity_sum", CriterionOperator.gte, 3, CriterionSource.stated, phrases=("order 3 paneer tikka",)),
                SeedCriterion("item.ingredients", CriterionOperator.excludes, "mutton", CriterionSource.stated, phrases=("no mutton",)),
            ),
            prohibited=("mutton",), merchant_scope_category="food_delivery",
            delivery_latest_by=datetime(2026, 8, 28, 21, 0, tzinfo=timezone.utc),
        ),
        Seed(
            seed_id="S26-punjabi-big-cart", language="en",
            instruction_text="Order one of everything from Punjabi Dhaba for the team dinner, under ₹1800.",
            merchant_id="rest-punjabi",
            item_names=("butter chicken", "dal makhani", "naan", "paneer tikka", "mutton curry", "chef thali"),
            quantities=(1, 1, 1, 1, 1, 1),
            budget_ceiling=Decimal("1800.00"), merchant_scope_ids=("rest-punjabi",),
        ),
        Seed(
            seed_id="S27-punjabi-naan-hinglish", language="hinglish",
            instruction_text="Punjabi Dhaba se 10 naan order karo, ₹500 se kam mein.",
            merchant_id="rest-punjabi", item_names=("naan",), quantities=(10,),
            criteria=(
                SeedCriterion("quantity_sum", CriterionOperator.gte, 10, CriterionSource.stated, phrases=("10 naan",)),
            ),
            budget_ceiling=Decimal("500.00"),
        ),
        Seed(
            seed_id="S28-punjabi-no-colour-ambiguous", language="en",
            instruction_text=("Order 1 Paneer Tikka and 3 Naan from Punjabi Dhaba, nothing with artificial "
                               "colour, under ₹450, deliver by 8pm."),
            merchant_id="rest-punjabi", item_names=("paneer tikka", "naan"), quantities=(1, 3),
            criteria=(
                SeedCriterion("item.categories", CriterionOperator.semantic, "no_artificial_colour", CriterionSource.stated,
                               phrases=("nothing with artificial colour",)),
            ),
            budget_ceiling=Decimal("600.00"), delivery_latest_by=datetime(2026, 8, 28, 20, 0, tzinfo=timezone.utc),
            fulfilment_eta=datetime(2026, 8, 28, 19, 0, tzinfo=timezone.utc), ambiguous=True,
        ),
        Seed(
            seed_id="S29-punjabi-mutton-curry", language="en",
            instruction_text=("Order 2 Mutton Curry from Punjabi Dhaba, no chicken, under ₹800, deliver by 9pm."),
            merchant_id="rest-punjabi", item_names=("mutton curry",), quantities=(2,),
            criteria=(
                SeedCriterion("quantity_sum", CriterionOperator.gte, 2, CriterionSource.stated, phrases=("order 2 mutton curry",)),
                SeedCriterion("item.ingredients", CriterionOperator.excludes, "chicken", CriterionSource.stated, phrases=("no chicken",)),
            ),
            prohibited=("chicken",), budget_ceiling=Decimal("800.00"), merchant_scope_ids=("rest-punjabi",),
            delivery_latest_by=datetime(2026, 8, 28, 21, 0, tzinfo=timezone.utc),
        ),

        # ── FreshMart (grocery) — S30..S37 ──────────────────────────────────
        Seed(
            seed_id="S30-freshmart-no-eggs", language="en",
            instruction_text=("Order Milk 1L and Basmati Rice 5kg from FreshMart, no eggs, under ₹900, "
                               "must arrive by 6pm."),
            merchant_id="grocery-freshmart", item_names=("milk 1l", "basmati rice 5kg"), quantities=(2, 1),
            criteria=(
                SeedCriterion("item.ingredients", CriterionOperator.excludes, "eggs", CriterionSource.stated, phrases=("no eggs",)),
            ),
            prohibited=("eggs",), budget_ceiling=Decimal("1000.00"), merchant_scope_ids=("grocery-freshmart",),
            delivery_latest_by=datetime(2026, 8, 28, 18, 0, tzinfo=timezone.utc), fulfilment_eta=datetime(2026, 8, 28, 16, 0, tzinfo=timezone.utc),
            ambiguous=True,
        ),
        Seed(
            seed_id="S31-freshmart-fresh-not-frozen", language="en",
            instruction_text="Order 3 Eggs 12pk from FreshMart only, must be fresh not frozen, under ₹400.",
            merchant_id="grocery-freshmart", item_names=("eggs 12pk",), quantities=(3,),
            criteria=(
                SeedCriterion("quantity_sum", CriterionOperator.gte, 3, CriterionSource.stated, phrases=("3 eggs 12pk",)),
                SeedCriterion("item.categories", CriterionOperator.semantic, "fresh_not_frozen", CriterionSource.stated,
                               phrases=("must be fresh not frozen",)),
            ),
            budget_ceiling=Decimal("400.00"), merchant_scope_ids=("grocery-freshmart",),
        ),
        Seed(
            seed_id="S32-freshmart-no-chicken-hinglish", language="hinglish",
            instruction_text=("FreshMart se 2 toor dal 1kg order karo, chicken bilkul nahi chahiye, raat 9 baje "
                               "tak deliver ho jaana chahiye."),
            merchant_id="grocery-freshmart", item_names=("toor dal 1kg",), quantities=(2,),
            criteria=(
                SeedCriterion("quantity_sum", CriterionOperator.gte, 2, CriterionSource.stated, phrases=("2 toor dal 1kg",)),
                SeedCriterion("item.ingredients", CriterionOperator.excludes, "chicken", CriterionSource.stated,
                               phrases=("chicken bilkul nahi chahiye",)),
            ),
            prohibited=("chicken",), delivery_latest_by=datetime(2026, 8, 28, 21, 0, tzinfo=timezone.utc),
        ),
        Seed(
            seed_id="S33-freshmart-big-cart", language="en",
            instruction_text="Order one unit of everything from FreshMart for the pantry restock, under ₹1800.",
            merchant_id="grocery-freshmart",
            item_names=("milk 1l", "eggs 12pk", "basmati rice 5kg", "chicken breast 1kg", "toor dal 1kg",
                        "onions 1kg", "tomatoes 1kg", "surprise snack box"),
            quantities=(1, 1, 1, 1, 1, 1, 1, 1),
            budget_ceiling=Decimal("1550.00"), merchant_scope_ids=("grocery-freshmart",),
        ),
        Seed(
            seed_id="S34-freshmart-produce", language="en",
            instruction_text="Order 2kg Onions and 2kg Tomatoes from FreshMart, under ₹200.",
            merchant_id="grocery-freshmart", item_names=("onions 1kg", "tomatoes 1kg"), quantities=(2, 2),
            budget_ceiling=Decimal("200.00"),
        ),
        Seed(
            seed_id="S35-freshmart-rice-category", language="en",
            instruction_text=("Order 2 Basmati Rice 5kg from any grocery store, no eggs, under ₹1500, deliver by 6pm."),
            merchant_id="grocery-freshmart", item_names=("basmati rice 5kg",), quantities=(2,),
            criteria=(
                SeedCriterion("quantity_sum", CriterionOperator.gte, 2, CriterionSource.stated, phrases=("order 2 basmati rice 5kg",)),
                SeedCriterion("item.ingredients", CriterionOperator.excludes, "eggs", CriterionSource.stated, phrases=("no eggs",)),
            ),
            prohibited=("eggs",), budget_ceiling=Decimal("1500.00"), merchant_scope_category="grocery",
            delivery_latest_by=datetime(2026, 8, 28, 18, 0, tzinfo=timezone.utc), fulfilment_eta=datetime(2026, 8, 28, 16, 0, tzinfo=timezone.utc),
        ),
        Seed(
            seed_id="S36-freshmart-sparse-hinglish", language="hinglish",
            instruction_text="FreshMart se milk, toor dal aur onions le aana, sham 6 baje tak.",
            merchant_id="grocery-freshmart", item_names=("milk 1l", "toor dal 1kg", "onions 1kg"), quantities=(1, 1, 1),
            delivery_latest_by=datetime(2026, 8, 28, 18, 0, tzinfo=timezone.utc), fulfilment_eta=datetime(2026, 8, 28, 16, 0, tzinfo=timezone.utc),
            ambiguous=True,
        ),
        Seed(
            seed_id="S37-freshmart-chicken-breast", language="en",
            instruction_text=("Order 2kg Chicken Breast from FreshMart, no eggs, under ₹600, deliver by 6pm."),
            merchant_id="grocery-freshmart", item_names=("chicken breast 1kg",), quantities=(2,),
            criteria=(
                SeedCriterion("quantity_sum", CriterionOperator.gte, 2, CriterionSource.stated, phrases=("order 2kg chicken breast",)),
                SeedCriterion("item.ingredients", CriterionOperator.excludes, "eggs", CriterionSource.stated, phrases=("no eggs",)),
            ),
            prohibited=("eggs",), budget_ceiling=Decimal("600.00"), merchant_scope_ids=("grocery-freshmart",),
            delivery_latest_by=datetime(2026, 8, 28, 18, 0, tzinfo=timezone.utc), fulfilment_eta=datetime(2026, 8, 28, 16, 0, tzinfo=timezone.utc),
        ),

        # ── DailyBasket (grocery) — S38..S45 ────────────────────────────────
        Seed(
            seed_id="S38-dailybasket-no-mutton", language="en",
            instruction_text=("Order 2kg Apples and 2 Bread Loaf from DailyBasket, no mutton, under ₹500, "
                               "deliver by 7pm."),
            merchant_id="grocery-dailybasket", item_names=("apples 1kg", "bread loaf"), quantities=(2, 2),
            criteria=(
                SeedCriterion("item.ingredients", CriterionOperator.excludes, "mutton", CriterionSource.stated, phrases=("no mutton",)),
            ),
            prohibited=("mutton",), budget_ceiling=Decimal("1000.00"), merchant_scope_ids=("grocery-dailybasket",),
            delivery_latest_by=datetime(2026, 8, 28, 19, 0, tzinfo=timezone.utc), fulfilment_eta=datetime(2026, 8, 28, 17, 0, tzinfo=timezone.utc),
        ),
        Seed(
            seed_id="S39-dailybasket-no-eggs-hinglish", language="hinglish",
            instruction_text="DailyBasket se 4 paneer 200g order karo, eggs bilkul nahi chahiye, ₹500 se kam mein.",
            merchant_id="grocery-dailybasket", item_names=("paneer 200g",), quantities=(4,),
            criteria=(
                SeedCriterion("quantity_sum", CriterionOperator.gte, 4, CriterionSource.stated, phrases=("4 paneer 200g",)),
                SeedCriterion("item.ingredients", CriterionOperator.excludes, "eggs", CriterionSource.stated,
                               phrases=("eggs bilkul nahi chahiye",)),
            ),
            prohibited=("eggs",), budget_ceiling=Decimal("1700.00"), merchant_scope_ids=("grocery-dailybasket",),
        ),
        Seed(
            seed_id="S40-dailybasket-unsalted-category", language="en",
            instruction_text=("Order 3 Butter 500g from any grocery store, must be unsalted, deliver by 7pm."),
            merchant_id="grocery-dailybasket", item_names=("butter 500g",), quantities=(3,),
            criteria=(
                SeedCriterion("quantity_sum", CriterionOperator.gte, 3, CriterionSource.stated, phrases=("order 3 butter 500g",)),
                SeedCriterion("item.categories", CriterionOperator.semantic, "unsalted", CriterionSource.stated,
                               phrases=("must be unsalted",)),
            ),
            merchant_scope_category="grocery", delivery_latest_by=datetime(2026, 8, 28, 19, 0, tzinfo=timezone.utc),
            fulfilment_eta=datetime(2026, 8, 28, 17, 0, tzinfo=timezone.utc), ambiguous=True,
        ),
        Seed(
            seed_id="S41-dailybasket-big-cart", language="en",
            instruction_text="Order one unit of everything from DailyBasket for the week, under ₹1500.",
            merchant_id="grocery-dailybasket",
            item_names=("apples 1kg", "bread loaf", "paneer 200g", "butter 500g", "mutton mince 500g",
                        "eggs 6pk", "mystery hamper"),
            quantities=(1, 1, 1, 1, 1, 1, 1),
            budget_ceiling=Decimal("1500.00"), merchant_scope_ids=("grocery-dailybasket",),
        ),
        Seed(
            seed_id="S42-dailybasket-mince-eggs", language="en",
            instruction_text="Order 1kg Mutton Mince and a pack of Eggs 6pk from DailyBasket, under ₹1000.",
            merchant_id="grocery-dailybasket", item_names=("mutton mince 500g", "eggs 6pk"), quantities=(2, 1),
            budget_ceiling=Decimal("1000.00"),
        ),
        Seed(
            seed_id="S43-dailybasket-apples-no-mutton", language="en",
            instruction_text=("Order 5kg Apples from DailyBasket, no mutton, under ₹1000, deliver by 7pm."),
            merchant_id="grocery-dailybasket", item_names=("apples 1kg",), quantities=(5,),
            criteria=(
                SeedCriterion("quantity_sum", CriterionOperator.gte, 5, CriterionSource.stated, phrases=("order 5kg apples",)),
                SeedCriterion("item.ingredients", CriterionOperator.excludes, "mutton", CriterionSource.stated, phrases=("no mutton",)),
            ),
            prohibited=("mutton",), budget_ceiling=Decimal("2100.00"), merchant_scope_ids=("grocery-dailybasket",),
            delivery_latest_by=datetime(2026, 8, 28, 19, 0, tzinfo=timezone.utc), fulfilment_eta=datetime(2026, 8, 28, 17, 0, tzinfo=timezone.utc),
        ),
        Seed(
            seed_id="S44-dailybasket-4item-hinglish", language="hinglish",
            instruction_text="DailyBasket se bread, paneer, butter aur apples le aana, sham 7 baje tak.",
            merchant_id="grocery-dailybasket", item_names=("bread loaf", "paneer 200g", "butter 500g", "apples 1kg"),
            quantities=(1, 1, 1, 1), merchant_scope_ids=("grocery-dailybasket",),
            delivery_latest_by=datetime(2026, 8, 28, 19, 0, tzinfo=timezone.utc), fulfilment_eta=datetime(2026, 8, 28, 17, 0, tzinfo=timezone.utc),
        ),
        Seed(
            seed_id="S45-dailybasket-eggs-no-mutton", language="en",
            instruction_text="Order 2 Eggs 6pk from DailyBasket, no mutton, under ₹300.",
            merchant_id="grocery-dailybasket", item_names=("eggs 6pk",), quantities=(2,),
            criteria=(
                SeedCriterion("quantity_sum", CriterionOperator.gte, 2, CriterionSource.stated, phrases=("order 2 eggs 6pk",)),
                SeedCriterion("item.ingredients", CriterionOperator.excludes, "mutton", CriterionSource.stated, phrases=("no mutton",)),
            ),
            prohibited=("mutton",), budget_ceiling=Decimal("900.00"),
        ),
    ]


# ── Generation plan ──────────────────────────────────────────────────────────
# (seed_id, violation_class, mutation_fn, mutation_kwargs).  Each row is
# generated twice (see _REPEAT_COUNT below) using rng-driven parameter
# diversity (see the mutation functions above) so repeats are genuinely
# different records, not duplicates.  CLEAN is generated separately.

MutationFn = Callable[..., MutationResult]

_PLAN: list[tuple[str, str, MutationFn, dict[str, Any]]] = [
    ("S01-biryani-dinner-4", "QUANTITY_MISMATCH", mutate_quantity_shortfall_catchable, {}),
    ("S01-biryani-dinner-4", "CONSTRAINT_VIOLATION", mutate_constraint_catchable, {"swap_item": "beef biryani"}),
    ("S01-biryani-dinner-4", "BUDGET_BREACH", mutate_budget_breach, {}),
    ("S01-biryani-dinner-4", "WRONG_MERCHANT", mutate_wrong_merchant, {}),
    ("S01-biryani-dinner-4", "TIMING_MISS", mutate_timing_miss, {}),
    ("S01-biryani-dinner-4", "TOTAL_MISDECLARED", mutate_total_misdeclared, {}),

    ("S02-biryani-office-lunch", "QUANTITY_MISMATCH", mutate_quantity_uncatchable_swap, {}),
    ("S02-biryani-office-lunch", "QUANTITY_MISMATCH", mutate_quantity_uncatchable_swap, {}),
    ("S02-biryani-office-lunch", "BUDGET_BREACH", mutate_budget_breach, {}),
    ("S02-biryani-office-lunch", "WRONG_MERCHANT", mutate_wrong_merchant, {}),
    ("S02-biryani-office-lunch", "TOTAL_MISDECLARED", mutate_total_misdeclared, {}),

    ("S03-biryani-category-scope", "QUANTITY_MISMATCH", mutate_quantity_shortfall_catchable, {}),
    ("S03-biryani-category-scope", "WRONG_MERCHANT", mutate_wrong_merchant, {}),
    ("S03-biryani-category-scope", "TOTAL_MISDECLARED", mutate_total_misdeclared, {}),

    ("S04-biryani-not-oily-hinglish", "CONSTRAINT_VIOLATION", mutate_constraint_uncatchable_semantic, {"swap_item": "chicken 65"}),
    ("S04-biryani-not-oily-hinglish", "CONSTRAINT_VIOLATION", mutate_constraint_uncatchable_semantic, {"swap_item": "chicken 65"}),
    ("S04-biryani-not-oily-hinglish", "BUDGET_BREACH", mutate_budget_breach, {}),
    ("S04-biryani-not-oily-hinglish", "TIMING_MISS", mutate_timing_miss, {}),
    ("S04-biryani-not-oily-hinglish", "TOTAL_MISDECLARED", mutate_total_misdeclared, {}),

    ("S05-biryani-big-cart", "BUDGET_BREACH", mutate_budget_breach, {}),
    ("S05-biryani-big-cart", "WRONG_MERCHANT", mutate_wrong_merchant, {}),
    ("S05-biryani-big-cart", "TOTAL_MISDECLARED", mutate_total_misdeclared, {}),

    ("S06-biryani-no-mutton-terse", "QUANTITY_MISMATCH", mutate_quantity_shortfall_catchable, {}),
    ("S06-biryani-no-mutton-terse", "CONSTRAINT_VIOLATION", mutate_constraint_catchable, {"swap_item": "mutton biryani"}),
    ("S06-biryani-no-mutton-terse", "TOTAL_MISDECLARED", mutate_total_misdeclared, {}),

    ("S07-biryani-snacks-ambiguous", "QUANTITY_MISMATCH", mutate_quantity_uncatchable_swap, {}),
    ("S07-biryani-snacks-ambiguous", "BUDGET_BREACH", mutate_budget_breach, {}),
    ("S07-biryani-snacks-ambiguous", "WRONG_MERCHANT", mutate_wrong_merchant, {}),
    ("S07-biryani-snacks-ambiguous", "TIMING_MISS", mutate_timing_miss, {}),
    ("S07-biryani-snacks-ambiguous", "TOTAL_MISDECLARED", mutate_total_misdeclared, {}),

    ("S08-biryani-chicken65-hinglish", "QUANTITY_MISMATCH", mutate_quantity_shortfall_catchable, {}),
    ("S08-biryani-chicken65-hinglish", "CONSTRAINT_VIOLATION", mutate_constraint_catchable, {"swap_item": "beef biryani"}),
    ("S08-biryani-chicken65-hinglish", "WRONG_MERCHANT", mutate_wrong_merchant, {}),
    ("S08-biryani-chicken65-hinglish", "TIMING_MISS", mutate_timing_miss, {}),
    ("S08-biryani-chicken65-hinglish", "TOTAL_MISDECLARED", mutate_total_misdeclared, {}),

    ("S09-pizza-margherita-2", "QUANTITY_MISMATCH", mutate_quantity_shortfall_catchable, {}),
    ("S09-pizza-margherita-2", "CONSTRAINT_VIOLATION", mutate_constraint_catchable, {"swap_item": "pepperoni"}),
    ("S09-pizza-margherita-2", "BUDGET_BREACH", mutate_budget_breach, {}),
    ("S09-pizza-margherita-2", "WRONG_MERCHANT", mutate_wrong_merchant, {}),
    ("S09-pizza-margherita-2", "TIMING_MISS", mutate_timing_miss, {}),
    ("S09-pizza-margherita-2", "TOTAL_MISDECLARED", mutate_total_misdeclared, {}),

    ("S10-pizza-not-too-spicy", "CONSTRAINT_VIOLATION", mutate_constraint_uncatchable_semantic, {"swap_item": "chicken supreme"}),
    ("S10-pizza-not-too-spicy", "CONSTRAINT_VIOLATION", mutate_constraint_uncatchable_semantic, {"swap_item": "chicken supreme"}),
    ("S10-pizza-not-too-spicy", "BUDGET_BREACH", mutate_budget_breach, {}),
    ("S10-pizza-not-too-spicy", "WRONG_MERCHANT", mutate_wrong_merchant, {}),
    ("S10-pizza-not-too-spicy", "TOTAL_MISDECLARED", mutate_total_misdeclared, {}),

    ("S11-pizza-garlic-coke", "QUANTITY_MISMATCH", mutate_quantity_uncatchable_swap, {}),
    ("S11-pizza-garlic-coke", "BUDGET_BREACH", mutate_budget_breach, {}),
    ("S11-pizza-garlic-coke", "TOTAL_MISDECLARED", mutate_total_misdeclared, {}),

    ("S12-pizza-4item-category", "CONSTRAINT_VIOLATION", mutate_constraint_catchable, {"swap_item": "pepperoni"}),
    ("S12-pizza-4item-category", "BUDGET_BREACH", mutate_budget_breach, {}),
    ("S12-pizza-4item-category", "WRONG_MERCHANT", mutate_wrong_merchant, {}),
    ("S12-pizza-4item-category", "TIMING_MISS", mutate_timing_miss, {}),
    ("S12-pizza-4item-category", "TOTAL_MISDECLARED", mutate_total_misdeclared, {}),

    ("S13-pizza-chicken-supreme-hinglish", "QUANTITY_MISMATCH", mutate_quantity_shortfall_catchable, {}),
    ("S13-pizza-chicken-supreme-hinglish", "WRONG_MERCHANT", mutate_wrong_merchant, {}),
    ("S13-pizza-chicken-supreme-hinglish", "TIMING_MISS", mutate_timing_miss, {}),
    ("S13-pizza-chicken-supreme-hinglish", "TOTAL_MISDECLARED", mutate_total_misdeclared, {}),

    ("S14-pizza-not-fried-mixed", "QUANTITY_MISMATCH", mutate_quantity_shortfall_catchable, {}),
    ("S14-pizza-not-fried-mixed", "CONSTRAINT_VIOLATION", mutate_constraint_uncatchable_semantic, {"swap_item": "garlic bread"}),
    ("S14-pizza-not-fried-mixed", "CONSTRAINT_VIOLATION", mutate_constraint_uncatchable_semantic, {"swap_item": "garlic bread"}),
    ("S14-pizza-not-fried-mixed", "BUDGET_BREACH", mutate_budget_breach, {}),
    ("S14-pizza-not-fried-mixed", "WRONG_MERCHANT", mutate_wrong_merchant, {}),
    ("S14-pizza-not-fried-mixed", "TIMING_MISS", mutate_timing_miss, {}),
    ("S14-pizza-not-fried-mixed", "TOTAL_MISDECLARED", mutate_total_misdeclared, {}),

    ("S15-pizza-minimal", "QUANTITY_MISMATCH", mutate_quantity_uncatchable_swap, {}),
    ("S15-pizza-minimal", "TOTAL_MISDECLARED", mutate_total_misdeclared, {}),

    ("S16-southindian-no-chicken", "QUANTITY_MISMATCH", mutate_quantity_shortfall_catchable, {}),
    ("S16-southindian-no-chicken", "CONSTRAINT_VIOLATION", mutate_constraint_catchable, {"swap_item": "chicken chettinad"}),
    ("S16-southindian-no-chicken", "CONSTRAINT_VIOLATION", mutate_constraint_abstain, {"swap_item": "chef special curry"}),
    ("S16-southindian-no-chicken", "CONSTRAINT_VIOLATION", mutate_constraint_abstain, {"swap_item": "chef special curry"}),
    ("S16-southindian-no-chicken", "WRONG_MERCHANT", mutate_wrong_merchant, {}),
    ("S16-southindian-no-chicken", "TIMING_MISS", mutate_timing_miss, {}),
    ("S16-southindian-no-chicken", "TOTAL_MISDECLARED", mutate_total_misdeclared, {}),

    ("S17-southindian-idli-coffee", "QUANTITY_MISMATCH", mutate_quantity_uncatchable_swap, {}),
    ("S17-southindian-idli-coffee", "BUDGET_BREACH", mutate_budget_breach, {}),
    ("S17-southindian-idli-coffee", "TIMING_MISS", mutate_timing_miss, {}),
    ("S17-southindian-idli-coffee", "TOTAL_MISDECLARED", mutate_total_misdeclared, {}),

    ("S18-southindian-rich", "QUANTITY_MISMATCH", mutate_quantity_shortfall_catchable, {}),
    ("S18-southindian-rich", "CONSTRAINT_VIOLATION", mutate_constraint_catchable, {"swap_item": "chicken chettinad"}),
    ("S18-southindian-rich", "CONSTRAINT_VIOLATION", mutate_constraint_abstain, {"swap_item": "chef special curry"}),
    ("S18-southindian-rich", "CONSTRAINT_VIOLATION", mutate_constraint_uncatchable_semantic, {"swap_item": "filter coffee"}),
    ("S18-southindian-rich", "WRONG_MERCHANT", mutate_wrong_merchant, {}),
    ("S18-southindian-rich", "TIMING_MISS", mutate_timing_miss, {}),
    ("S18-southindian-rich", "TOTAL_MISDECLARED", mutate_total_misdeclared, {}),

    ("S19-southindian-served-hot-category", "CONSTRAINT_VIOLATION", mutate_constraint_uncatchable_semantic, {"swap_item": "chicken chettinad"}),
    ("S19-southindian-served-hot-category", "WRONG_MERCHANT", mutate_wrong_merchant, {}),
    ("S19-southindian-served-hot-category", "TOTAL_MISDECLARED", mutate_total_misdeclared, {}),

    ("S20-southindian-chettinad", "QUANTITY_MISMATCH", mutate_quantity_shortfall_catchable, {}),
    ("S20-southindian-chettinad", "BUDGET_BREACH", mutate_budget_breach, {}),
    ("S20-southindian-chettinad", "TIMING_MISS", mutate_timing_miss, {}),
    ("S20-southindian-chettinad", "TOTAL_MISDECLARED", mutate_total_misdeclared, {}),

    ("S21-southindian-dosa-chettinad", "QUANTITY_MISMATCH", mutate_quantity_uncatchable_swap, {}),
    ("S21-southindian-dosa-chettinad", "WRONG_MERCHANT", mutate_wrong_merchant, {}),
    ("S21-southindian-dosa-chettinad", "TOTAL_MISDECLARED", mutate_total_misdeclared, {}),

    ("S22-southindian-coffee-hinglish", "QUANTITY_MISMATCH", mutate_quantity_shortfall_catchable, {}),
    ("S22-southindian-coffee-hinglish", "BUDGET_BREACH", mutate_budget_breach, {}),
    ("S22-southindian-coffee-hinglish", "TOTAL_MISDECLARED", mutate_total_misdeclared, {}),

    ("S23-punjabi-scope-budget", "QUANTITY_MISMATCH", mutate_quantity_shortfall_catchable, {}),
    ("S23-punjabi-scope-budget", "CONSTRAINT_VIOLATION", mutate_constraint_catchable, {"swap_item": "butter chicken"}),
    ("S23-punjabi-scope-budget", "CONSTRAINT_VIOLATION", mutate_constraint_abstain, {"swap_item": "chef thali"}),
    ("S23-punjabi-scope-budget", "BUDGET_BREACH", mutate_budget_breach, {}),
    ("S23-punjabi-scope-budget", "WRONG_MERCHANT", mutate_wrong_merchant, {}),
    ("S23-punjabi-scope-budget", "TIMING_MISS", mutate_timing_miss, {}),
    ("S23-punjabi-scope-budget", "TOTAL_MISDECLARED", mutate_total_misdeclared, {}),

    ("S24-punjabi-hinglish-budget", "QUANTITY_MISMATCH", mutate_quantity_uncatchable_swap, {}),
    ("S24-punjabi-hinglish-budget", "BUDGET_BREACH", mutate_budget_breach, {}),
    ("S24-punjabi-hinglish-budget", "WRONG_MERCHANT", mutate_wrong_merchant, {}),
    ("S24-punjabi-hinglish-budget", "TOTAL_MISDECLARED", mutate_total_misdeclared, {}),

    ("S25-punjabi-paneer-category", "QUANTITY_MISMATCH", mutate_quantity_shortfall_catchable, {}),
    ("S25-punjabi-paneer-category", "CONSTRAINT_VIOLATION", mutate_constraint_catchable, {"swap_item": "mutton curry"}),
    ("S25-punjabi-paneer-category", "CONSTRAINT_VIOLATION", mutate_constraint_abstain, {"swap_item": "chef thali"}),
    ("S25-punjabi-paneer-category", "WRONG_MERCHANT", mutate_wrong_merchant, {}),
    ("S25-punjabi-paneer-category", "TIMING_MISS", mutate_timing_miss, {}),
    ("S25-punjabi-paneer-category", "TOTAL_MISDECLARED", mutate_total_misdeclared, {}),

    ("S26-punjabi-big-cart", "BUDGET_BREACH", mutate_budget_breach, {}),
    ("S26-punjabi-big-cart", "WRONG_MERCHANT", mutate_wrong_merchant, {}),
    ("S26-punjabi-big-cart", "TOTAL_MISDECLARED", mutate_total_misdeclared, {}),

    ("S27-punjabi-naan-hinglish", "QUANTITY_MISMATCH", mutate_quantity_shortfall_catchable, {}),
    ("S27-punjabi-naan-hinglish", "BUDGET_BREACH", mutate_budget_breach, {}),
    ("S27-punjabi-naan-hinglish", "TOTAL_MISDECLARED", mutate_total_misdeclared, {}),

    ("S28-punjabi-no-colour-ambiguous", "QUANTITY_MISMATCH", mutate_quantity_uncatchable_swap, {}),
    ("S28-punjabi-no-colour-ambiguous", "CONSTRAINT_VIOLATION", mutate_constraint_uncatchable_semantic, {"swap_item": "mutton curry"}),
    ("S28-punjabi-no-colour-ambiguous", "BUDGET_BREACH", mutate_budget_breach, {}),
    ("S28-punjabi-no-colour-ambiguous", "TIMING_MISS", mutate_timing_miss, {}),
    ("S28-punjabi-no-colour-ambiguous", "TOTAL_MISDECLARED", mutate_total_misdeclared, {}),

    ("S29-punjabi-mutton-curry", "QUANTITY_MISMATCH", mutate_quantity_shortfall_catchable, {}),
    ("S29-punjabi-mutton-curry", "CONSTRAINT_VIOLATION", mutate_constraint_catchable, {"swap_item": "butter chicken"}),
    ("S29-punjabi-mutton-curry", "CONSTRAINT_VIOLATION", mutate_constraint_abstain, {"swap_item": "chef thali"}),
    ("S29-punjabi-mutton-curry", "BUDGET_BREACH", mutate_budget_breach, {}),
    ("S29-punjabi-mutton-curry", "WRONG_MERCHANT", mutate_wrong_merchant, {}),
    ("S29-punjabi-mutton-curry", "TIMING_MISS", mutate_timing_miss, {}),
    ("S29-punjabi-mutton-curry", "TOTAL_MISDECLARED", mutate_total_misdeclared, {}),

    ("S30-freshmart-no-eggs", "QUANTITY_MISMATCH", mutate_quantity_uncatchable_swap, {}),
    ("S30-freshmart-no-eggs", "CONSTRAINT_VIOLATION", mutate_constraint_catchable, {"swap_item": "eggs 12pk"}),
    ("S30-freshmart-no-eggs", "CONSTRAINT_VIOLATION", mutate_constraint_abstain, {"swap_item": "surprise snack box"}),
    ("S30-freshmart-no-eggs", "WRONG_MERCHANT", mutate_wrong_merchant, {}),
    ("S30-freshmart-no-eggs", "TIMING_MISS", mutate_timing_miss, {}),
    ("S30-freshmart-no-eggs", "TOTAL_MISDECLARED", mutate_total_misdeclared, {}),

    ("S31-freshmart-fresh-not-frozen", "QUANTITY_MISMATCH", mutate_quantity_shortfall_catchable, {}),
    ("S31-freshmart-fresh-not-frozen", "CONSTRAINT_VIOLATION", mutate_constraint_uncatchable_semantic, {"swap_item": "milk 1l"}),
    ("S31-freshmart-fresh-not-frozen", "CONSTRAINT_VIOLATION", mutate_constraint_uncatchable_semantic, {"swap_item": "milk 1l"}),
    ("S31-freshmart-fresh-not-frozen", "BUDGET_BREACH", mutate_budget_breach, {}),
    ("S31-freshmart-fresh-not-frozen", "WRONG_MERCHANT", mutate_wrong_merchant, {}),
    ("S31-freshmart-fresh-not-frozen", "TOTAL_MISDECLARED", mutate_total_misdeclared, {}),

    ("S32-freshmart-no-chicken-hinglish", "QUANTITY_MISMATCH", mutate_quantity_shortfall_catchable, {}),
    ("S32-freshmart-no-chicken-hinglish", "CONSTRAINT_VIOLATION", mutate_constraint_catchable, {"swap_item": "chicken breast 1kg"}),
    ("S32-freshmart-no-chicken-hinglish", "CONSTRAINT_VIOLATION", mutate_constraint_abstain, {"swap_item": "surprise snack box"}),
    ("S32-freshmart-no-chicken-hinglish", "TIMING_MISS", mutate_timing_miss, {}),
    ("S32-freshmart-no-chicken-hinglish", "TOTAL_MISDECLARED", mutate_total_misdeclared, {}),

    ("S33-freshmart-big-cart", "BUDGET_BREACH", mutate_budget_breach, {}),
    ("S33-freshmart-big-cart", "WRONG_MERCHANT", mutate_wrong_merchant, {}),
    ("S33-freshmart-big-cart", "TOTAL_MISDECLARED", mutate_total_misdeclared, {}),

    ("S34-freshmart-produce", "QUANTITY_MISMATCH", mutate_quantity_uncatchable_swap, {}),
    ("S34-freshmart-produce", "BUDGET_BREACH", mutate_budget_breach, {}),
    ("S34-freshmart-produce", "TOTAL_MISDECLARED", mutate_total_misdeclared, {}),

    ("S35-freshmart-rice-category", "QUANTITY_MISMATCH", mutate_quantity_shortfall_catchable, {}),
    ("S35-freshmart-rice-category", "CONSTRAINT_VIOLATION", mutate_constraint_catchable, {"swap_item": "eggs 12pk"}),
    ("S35-freshmart-rice-category", "BUDGET_BREACH", mutate_budget_breach, {}),
    ("S35-freshmart-rice-category", "WRONG_MERCHANT", mutate_wrong_merchant, {}),
    ("S35-freshmart-rice-category", "TIMING_MISS", mutate_timing_miss, {}),
    ("S35-freshmart-rice-category", "TOTAL_MISDECLARED", mutate_total_misdeclared, {}),

    ("S36-freshmart-sparse-hinglish", "TIMING_MISS", mutate_timing_miss, {}),
    ("S36-freshmart-sparse-hinglish", "TOTAL_MISDECLARED", mutate_total_misdeclared, {}),

    ("S37-freshmart-chicken-breast", "QUANTITY_MISMATCH", mutate_quantity_shortfall_catchable, {}),
    ("S37-freshmart-chicken-breast", "CONSTRAINT_VIOLATION", mutate_constraint_catchable, {"swap_item": "eggs 12pk"}),
    ("S37-freshmart-chicken-breast", "CONSTRAINT_VIOLATION", mutate_constraint_abstain, {"swap_item": "surprise snack box"}),
    ("S37-freshmart-chicken-breast", "BUDGET_BREACH", mutate_budget_breach, {}),
    ("S37-freshmart-chicken-breast", "WRONG_MERCHANT", mutate_wrong_merchant, {}),
    ("S37-freshmart-chicken-breast", "TIMING_MISS", mutate_timing_miss, {}),
    ("S37-freshmart-chicken-breast", "TOTAL_MISDECLARED", mutate_total_misdeclared, {}),

    ("S38-dailybasket-no-mutton", "QUANTITY_MISMATCH", mutate_quantity_uncatchable_swap, {}),
    ("S38-dailybasket-no-mutton", "CONSTRAINT_VIOLATION", mutate_constraint_catchable, {"swap_item": "mutton mince 500g"}),
    ("S38-dailybasket-no-mutton", "CONSTRAINT_VIOLATION", mutate_constraint_abstain, {"swap_item": "mystery hamper"}),
    ("S38-dailybasket-no-mutton", "WRONG_MERCHANT", mutate_wrong_merchant, {}),
    ("S38-dailybasket-no-mutton", "TIMING_MISS", mutate_timing_miss, {}),
    ("S38-dailybasket-no-mutton", "TOTAL_MISDECLARED", mutate_total_misdeclared, {}),

    ("S39-dailybasket-no-eggs-hinglish", "QUANTITY_MISMATCH", mutate_quantity_shortfall_catchable, {}),
    ("S39-dailybasket-no-eggs-hinglish", "CONSTRAINT_VIOLATION", mutate_constraint_catchable, {"swap_item": "eggs 6pk"}),
    ("S39-dailybasket-no-eggs-hinglish", "CONSTRAINT_VIOLATION", mutate_constraint_abstain, {"swap_item": "mystery hamper"}),
    ("S39-dailybasket-no-eggs-hinglish", "WRONG_MERCHANT", mutate_wrong_merchant, {}),
    ("S39-dailybasket-no-eggs-hinglish", "TOTAL_MISDECLARED", mutate_total_misdeclared, {}),

    ("S40-dailybasket-unsalted-category", "QUANTITY_MISMATCH", mutate_quantity_shortfall_catchable, {}),
    ("S40-dailybasket-unsalted-category", "CONSTRAINT_VIOLATION", mutate_constraint_uncatchable_semantic, {"swap_item": "paneer 200g"}),
    ("S40-dailybasket-unsalted-category", "CONSTRAINT_VIOLATION", mutate_constraint_uncatchable_semantic, {"swap_item": "paneer 200g"}),
    ("S40-dailybasket-unsalted-category", "WRONG_MERCHANT", mutate_wrong_merchant, {}),
    ("S40-dailybasket-unsalted-category", "TIMING_MISS", mutate_timing_miss, {}),
    ("S40-dailybasket-unsalted-category", "TOTAL_MISDECLARED", mutate_total_misdeclared, {}),

    ("S41-dailybasket-big-cart", "BUDGET_BREACH", mutate_budget_breach, {}),
    ("S41-dailybasket-big-cart", "WRONG_MERCHANT", mutate_wrong_merchant, {}),
    ("S41-dailybasket-big-cart", "TOTAL_MISDECLARED", mutate_total_misdeclared, {}),

    ("S42-dailybasket-mince-eggs", "QUANTITY_MISMATCH", mutate_quantity_uncatchable_swap, {}),
    ("S42-dailybasket-mince-eggs", "BUDGET_BREACH", mutate_budget_breach, {}),
    ("S42-dailybasket-mince-eggs", "TOTAL_MISDECLARED", mutate_total_misdeclared, {}),

    ("S43-dailybasket-apples-no-mutton", "QUANTITY_MISMATCH", mutate_quantity_shortfall_catchable, {}),
    ("S43-dailybasket-apples-no-mutton", "CONSTRAINT_VIOLATION", mutate_constraint_catchable, {"swap_item": "mutton mince 500g"}),
    ("S43-dailybasket-apples-no-mutton", "CONSTRAINT_VIOLATION", mutate_constraint_abstain, {"swap_item": "mystery hamper"}),
    ("S43-dailybasket-apples-no-mutton", "WRONG_MERCHANT", mutate_wrong_merchant, {}),
    ("S43-dailybasket-apples-no-mutton", "TIMING_MISS", mutate_timing_miss, {}),
    ("S43-dailybasket-apples-no-mutton", "TOTAL_MISDECLARED", mutate_total_misdeclared, {}),

    ("S44-dailybasket-4item-hinglish", "WRONG_MERCHANT", mutate_wrong_merchant, {}),
    ("S44-dailybasket-4item-hinglish", "TIMING_MISS", mutate_timing_miss, {}),
    ("S44-dailybasket-4item-hinglish", "TOTAL_MISDECLARED", mutate_total_misdeclared, {}),

    ("S45-dailybasket-eggs-no-mutton", "QUANTITY_MISMATCH", mutate_quantity_shortfall_catchable, {}),
    ("S45-dailybasket-eggs-no-mutton", "CONSTRAINT_VIOLATION", mutate_constraint_catchable, {"swap_item": "mutton mince 500g"}),
    ("S45-dailybasket-eggs-no-mutton", "CONSTRAINT_VIOLATION", mutate_constraint_abstain, {"swap_item": "mystery hamper"}),
    ("S45-dailybasket-eggs-no-mutton", "TOTAL_MISDECLARED", mutate_total_misdeclared, {}),
]

# Every _PLAN row is generated this many times (rng-driven, so repeats
# differ — see mutation function docstrings).  Pushes per-subpopulation
# counts comfortably past the Part A1 floor of 25 without requiring 25+
# distinct seeds for every bucket (Part A2's ask is seed count for overall
# corpus independence, not per-bucket seed count).
_REPEAT_COUNT = 2


# ── Deterministic per-record RNG ─────────────────────────────────────────────

def _record_seed(global_seed: int, *parts: str) -> int:
    digest = hashlib.sha256("::".join(parts).encode("utf-8")).hexdigest()
    return global_seed ^ int(digest[:16], 16)


def _canonical_json(data: object) -> str:
    return json.dumps(data, sort_keys=True, default=str, ensure_ascii=True)


# ── Record assembly ──────────────────────────────────────────────────────────

def _criteria_payload(seed: Seed) -> list[dict[str, Any]]:
    return [
        {
            "id": f"{seed.seed_id}-crit-{i}",
            "field": c.field,
            "operator": c.operator.value,
            "value": c.value,
            "source": c.source.value,
            "confidence": c.confidence,
        }
        for i, c in enumerate(seed.criteria)
    ]


def _resolve_violating_criterion_ids(seed: Seed, violating_fields: tuple[str, ...]) -> list[str]:
    """
    Each entry in violating_fields is either "field" (matches any criterion
    on that field — safe when a seed has at most one criterion per field)
    or "field=value" (matches by field AND value — required when a seed
    declares multiple criteria on the same field, e.g. two `excludes`
    criteria on item.ingredients for "no chicken, no egg").
    """
    ids = []
    for key in violating_fields:
        if "=" in key:
            field, _, value = key.partition("=")
            matches = [i for i, c in enumerate(seed.criteria) if c.field == field and str(c.value).lower() == value.lower()]
        else:
            matches = [i for i, c in enumerate(seed.criteria) if c.field == key]
        if not matches:
            raise GeneratorError(f"{seed.seed_id}: no authored criterion for violating field {key!r}")
        ids.extend(f"{seed.seed_id}-crit-{i}" for i in matches)
    return ids


def _obligation_payload(seed: Seed) -> dict[str, Any]:
    return {
        "acceptance_criteria": _criteria_payload(seed),
        "prohibited": list(seed.prohibited),
        "budget_ceiling": str(seed.budget_ceiling) if seed.budget_ceiling is not None else None,
        "merchant_scope": {"merchant_ids": list(seed.merchant_scope_ids), "category": seed.merchant_scope_category},
        "delivery_window": (
            {"latest_by": seed.delivery_latest_by.isoformat(), "tz": "Asia/Kolkata"}
            if seed.delivery_latest_by is not None else None
        ),
        "admissibility_floor": "REC",
    }


def _cart_payload(cart: Cart) -> dict[str, Any]:
    return {
        "items": [
            {
                "name": i.name, "quantity": i.quantity, "unit_price": str(i.unit_price),
                "ingredients": list(i.ingredients), "category": i.category,
            }
            for i in cart.items
        ],
        "merchant": {"id": cart.merchant.id, "name": cart.merchant.name, "category": cart.merchant.category},
        "total": str(cart.total),
        "fulfilment_eta": cart.fulfilment_eta.isoformat(),
    }


def _make_record(seed: Seed, global_seed: int, *, bucket: str, variant_index: int,
                  mutation_name: str, result: MutationResult) -> dict[str, Any]:
    order_id = f"{seed.seed_id}::{bucket}::{variant_index}"
    rseed = _record_seed(global_seed, seed.seed_id, bucket, mutation_name, str(variant_index))

    if bucket == "CLEAN":
        violating_criterion_ids: list[str] = []
    else:
        violating_criterion_ids = _resolve_violating_criterion_ids(seed, result.violating_fields)

    labels = {
        "violation_class": bucket,
        "violating_criterion_ids": violating_criterion_ids,
        "violating_obligation_fields": list(result.violating_obligation_fields),
        "abstain_expected": result.abstain_expected,
        "instruction_ambiguous": seed.ambiguous,
        "self_report_deceptive": result.self_report_deceptive,
        "verifier_catchable": result.verifier_catchable,
    }

    return {
        "order_id": order_id,
        "language": seed.language,
        "instruction_text": seed.instruction_text,
        "obligation": _obligation_payload(seed),
        "cart": _cart_payload(result.cart),
        "labels": labels,
        "self_report": (
            {"emitter": "agent", "claim": _SELF_REPORT_CLAIM} if result.self_report_deceptive else None
        ),
        "generation": {
            "seed_id": seed.seed_id, "mutation": mutation_name,
            "variant_index": variant_index, "record_seed": rseed,
        },
    }


_SELF_REPORT_CLAIM = "All items match your request. No substitutions, everything as ordered."


# ── Corpus assembly ──────────────────────────────────────────────────────────

def build_corpus(global_seed: int) -> list[dict[str, Any]]:
    """
    Deterministically build the full record list.  Calling this twice with
    the same global_seed must produce an identical list.
    """
    seeds_by_id = {s.seed_id: s for s in _seeds()}
    for seed in seeds_by_id.values():
        _assert_stated_criteria_traceable(seed)

    records: list[dict[str, Any]] = []

    for seed in seeds_by_id.values():
        cart = seed.build_cart()
        for variant_index in range(CLEAN_VARIANTS_PER_SEED):
            rseed = _record_seed(global_seed, seed.seed_id, "CLEAN", "clean", str(variant_index))
            rng = random.Random(rseed)
            result, kind_name = mutate_clean_dispatch(seed, cart, rng, variant_index)
            records.append(_make_record(
                seed, global_seed, bucket="CLEAN", variant_index=variant_index,
                mutation_name=kind_name, result=result,
            ))

    bucket_counters: dict[tuple[str, str], int] = {}
    for seed_id, bucket, fn, kwargs in _PLAN:
        seed = seeds_by_id[seed_id]
        for _ in range(_REPEAT_COUNT):
            variant_index = bucket_counters.get((seed_id, bucket), 0)
            bucket_counters[(seed_id, bucket)] = variant_index + 1

            cart = seed.build_cart()
            rseed = _record_seed(global_seed, seed_id, bucket, fn.__name__, str(variant_index))
            rng = random.Random(rseed)
            result = fn(seed, cart, rng, **kwargs)
            records.append(_make_record(
                seed, global_seed, bucket=bucket, variant_index=variant_index,
                mutation_name=fn.__name__, result=result,
            ))

    records.sort(key=lambda r: r["order_id"])
    return records


def _subpopulation_key(record: dict[str, Any]) -> str | None:
    """None for CLEAN (not stratified — every seed contributes CLEAN records,
    so it needs no balancing check)."""
    vc = record["labels"]["violation_class"]
    if vc == "CLEAN":
        return None
    if vc == "QUANTITY_MISMATCH":
        return f"{vc}:{'catchable' if record['labels']['verifier_catchable'] else 'uncatchable'}"
    if vc == "CONSTRAINT_VIOLATION":
        if record["labels"]["abstain_expected"]:
            return f"{vc}:abstain"
        return f"{vc}:{'catchable' if record['labels']['verifier_catchable'] else 'uncatchable'}"
    return str(vc)


def build_split(records: list[dict[str, Any]], global_seed: int, *,
                 holdout_fraction: float = 0.3) -> dict[str, str]:
    """
    Deterministic, SEED-level train/holdout split, stratified so every
    violation subpopulation has at least one contributing seed on each
    side (Part A3).  Splitting at the record level would put near-
    duplicate records — same merchant, same criteria shape, differing only
    in which mutation fired — on both sides, which is leakage, not
    generalisation.

    Every record inherits its seed's assignment.  The record-level holdout
    fraction will not land exactly on `holdout_fraction` once stratification
    moves seeds around — that is expected; assert a tolerance band, not an
    exact figure.
    """
    seed_ids = sorted({r["generation"]["seed_id"] for r in records})
    rng = random.Random(global_seed ^ 0x5A1_17_D0)
    shuffled = list(seed_ids)
    rng.shuffle(shuffled)
    n_holdout = round(len(shuffled) * holdout_fraction)
    holdout_seeds = set(shuffled[:n_holdout])

    subpop_seeds: dict[str, set[str]] = {}
    for r in records:
        key = _subpopulation_key(r)
        if key is None:
            continue
        subpop_seeds.setdefault(key, set()).add(r["generation"]["seed_id"])

    for _ in range(100):
        changed = False
        for key, seeds_for_key in subpop_seeds.items():
            train_seeds = sorted(s for s in seeds_for_key if s not in holdout_seeds)
            hold_seeds = sorted(s for s in seeds_for_key if s in holdout_seeds)
            if not train_seeds and hold_seeds:
                holdout_seeds.discard(hold_seeds[0])
                changed = True
            elif not hold_seeds and train_seeds:
                holdout_seeds.add(train_seeds[0])
                changed = True
        if not changed:
            break
    else:
        raise GeneratorError("split stratification did not converge")

    return {r["order_id"]: ("holdout" if r["generation"]["seed_id"] in holdout_seeds else "train") for r in records}


def sha256_of(obj: object) -> str:
    return hashlib.sha256(_canonical_json(obj).encode("utf-8")).hexdigest()


def records_jsonl(records: list[dict[str, Any]]) -> str:
    """Canonical, line-stable serialisation: one sort-keyed JSON object per line."""
    return "\n".join(_canonical_json(r) for r in records) + "\n"


# ── Disk I/O ──────────────────────────────────────────────────────────────

DEFAULT_GLOBAL_SEED = 20260828   # fixed at first commit; changing it changes the corpus

def write_corpus(out_dir: Path, global_seed: int = DEFAULT_GLOBAL_SEED) -> dict[str, Any]:
    """
    Generate the corpus and write records.jsonl, split.json, and
    CORPUS_LOCK.json to out_dir.  Returns the lock dict.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    records = build_corpus(global_seed)
    split = build_split(records, global_seed)

    records_text = records_jsonl(records)
    split_payload = {"holdout_fraction": 0.3, "global_seed": global_seed, "assignments": split}
    split_text = _canonical_json(split_payload)

    lock = {
        "global_seed": global_seed,
        "records_sha256": hashlib.sha256(records_text.encode("utf-8")).hexdigest(),
        "split_sha256": hashlib.sha256(split_text.encode("utf-8")).hexdigest(),
        "record_count": len(records),
        "seed_count": len({r["generation"]["seed_id"] for r in records}),
    }

    (out_dir / "records.jsonl").write_text(records_text, encoding="utf-8")
    (out_dir / "split.json").write_text(split_text, encoding="utf-8")
    (out_dir / "CORPUS_LOCK.json").write_text(_canonical_json(lock), encoding="utf-8")
    return lock
