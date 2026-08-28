"""
Closed registry of resolvable AcceptanceCriterion field paths.

WHY THIS EXISTS
---------------
The LLM compiler emits a field path for each AcceptanceCriterion it
creates.  A free-form dot-path string is a query mini-language, and the
compiler will invent paths the constraint verifier cannot resolve — silently
producing ABSTAIN verdicts that read as "no violation found."

The fix is structural: every path the compiler may emit is listed here,
each with a resolver function.  At bind time, the Obligation binder
validates every criterion's field against this registry.  An unresolvable
path is a hard bind failure — it never silently becomes an ABSTAIN.

ADDING A FIELD
--------------
1. Write a resolver function: (Cart) -> <python_type>
2. Create a FieldSpec and register it with _reg().
3. Add a test in tests/unit/test_models.py confirming the resolver.
4. Update the compiler prompt in core/obligation/compiler.py with the
   new path and its description.

Never add a path without a working resolver.  A path listed here that
returns None for a non-nullable field is as bad as a missing path.

COMPILER METRIC
---------------
Unresolvable-path rate is tracked in eval/harness.py separately from
the main accuracy metrics.  A rising rate means the compiler is emitting
paths that are not in this registry — the registry may need extension,
or the compiler prompt may need tightening.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime
from decimal import Decimal
from typing import Any, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from core.models.cart import Cart


@dataclasses.dataclass(frozen=True)
class FieldSpec:
    path:        str
    description: str
    python_type: type
    resolver:    Callable[["Cart"], Any]


# ── Internal registry ──────────────────────────────────────────────────────

_REGISTRY: dict[str, FieldSpec] = {}


def _reg(spec: FieldSpec) -> FieldSpec:
    """Register a FieldSpec and return it for assignment to a module-level name."""
    if spec.path in _REGISTRY:
        raise RuntimeError(
            f"Duplicate field path registration: {spec.path!r}. "
            "Each path must be registered exactly once."
        )
    _REGISTRY[spec.path] = spec
    return spec


# ── Resolver implementations ───────────────────────────────────────────────
#
# Each resolver is a pure function: Cart → Any.  No side effects; no I/O.
# Type annotations are advisory — the constraint verifier performs a runtime
# cast to the field's python_type before comparison.

def _total(cart: "Cart") -> Decimal:
    return cart.total


def _item_count(cart: "Cart") -> int:
    return len(cart.items)


def _quantity_sum(cart: "Cart") -> int:
    return sum(item.quantity for item in cart.items)


def _distinct_item_count(cart: "Cart") -> int:
    return len({item.name.lower() for item in cart.items})


def _max_item_quantity(cart: "Cart") -> int:
    return max((item.quantity for item in cart.items), default=0)


def _min_item_quantity(cart: "Cart") -> int:
    return min((item.quantity for item in cart.items), default=0)


def _merchant_id(cart: "Cart") -> str:
    return cart.merchant.id


def _merchant_name(cart: "Cart") -> str:
    return cart.merchant.name


def _merchant_category(cart: "Cart") -> str:
    return cart.merchant.category


def _item_names(cart: "Cart") -> list[str]:
    """Lower-cased list of all item names.  Use with contains / excludes."""
    return [item.name.lower() for item in cart.items]


def _item_ingredients(cart: "Cart") -> list[str]:
    """
    Flattened, lower-cased list of all declared ingredients across all items.

    An item with ingredients=[] contributes nothing to this list.
    Callers that need to detect missing ingredient declarations should check
    whether any CartItem.ingredients is empty and emit ABSTAIN rather than
    assuming a clean slate.  The constraint verifier does this.
    """
    result: list[str] = []
    for item in cart.items:
        result.extend(ing.lower() for ing in item.ingredients)
    return result


def _item_categories(cart: "Cart") -> list[str]:
    """Lower-cased item-level category tags for all items that declare one."""
    return [item.category.lower() for item in cart.items if item.category is not None]


def _fulfilment_eta(cart: "Cart") -> datetime:
    return cart.fulfilment_eta


def _items_with_missing_ingredients(cart: "Cart") -> int:
    """
    Count of line items that declare no ingredients.  Non-zero means the
    constraint verifier cannot make a definitive dietary judgement.
    """
    return sum(1 for item in cart.items if not item.ingredients)


# ── Field registrations ────────────────────────────────────────────────────

# Order-level
TOTAL = _reg(FieldSpec(
    path="total",
    description="Order total in INR as Decimal.",
    python_type=Decimal,
    resolver=_total,
))

ITEM_COUNT = _reg(FieldSpec(
    path="item_count",
    description="Number of distinct line items (Cart.items length).",
    python_type=int,
    resolver=_item_count,
))

QUANTITY_SUM = _reg(FieldSpec(
    path="quantity_sum",
    description="Sum of quantities across all line items.",
    python_type=int,
    resolver=_quantity_sum,
))

DISTINCT_ITEM_COUNT = _reg(FieldSpec(
    path="distinct_item_count",
    description="Count of unique item names (case-insensitive).",
    python_type=int,
    resolver=_distinct_item_count,
))

MAX_ITEM_QUANTITY = _reg(FieldSpec(
    path="max_item_quantity",
    description="Quantity of the single highest-quantity line item.",
    python_type=int,
    resolver=_max_item_quantity,
))

MIN_ITEM_QUANTITY = _reg(FieldSpec(
    path="min_item_quantity",
    description="Quantity of the single lowest-quantity line item.",
    python_type=int,
    resolver=_min_item_quantity,
))

# Merchant
MERCHANT_ID = _reg(FieldSpec(
    path="merchant.id",
    description="Merchant identifier string.",
    python_type=str,
    resolver=_merchant_id,
))

MERCHANT_NAME = _reg(FieldSpec(
    path="merchant.name",
    description="Human-readable merchant name.",
    python_type=str,
    resolver=_merchant_name,
))

MERCHANT_CATEGORY = _reg(FieldSpec(
    path="merchant.category",
    description="Merchant category string, e.g. 'food_delivery' or 'grocery'.",
    python_type=str,
    resolver=_merchant_category,
))

# Item content
ITEM_NAMES = _reg(FieldSpec(
    path="item.names",
    description=(
        "Lower-cased list of all item names in the cart.  "
        "Use with 'contains' or 'excludes'."
    ),
    python_type=list,
    resolver=_item_names,
))

ITEM_INGREDIENTS = _reg(FieldSpec(
    path="item.ingredients",
    description=(
        "Flattened, lower-cased list of all declared ingredients across all items.  "
        "Use with 'excludes' for dietary restrictions.  "
        "Items with no declared ingredients contribute nothing — "
        "the constraint verifier must ABSTAIN on dietary criteria if any item "
        "has empty ingredients."
    ),
    python_type=list,
    resolver=_item_ingredients,
))

ITEM_CATEGORIES = _reg(FieldSpec(
    path="item.categories",
    description=(
        "Lower-cased list of item-level category tags for items that declare one.  "
        "Use with 'contains' / 'excludes' / 'in_set'."
    ),
    python_type=list,
    resolver=_item_categories,
))

ITEMS_WITH_MISSING_INGREDIENTS = _reg(FieldSpec(
    path="item.missing_ingredient_count",
    description=(
        "Count of line items with no declared ingredients.  "
        "Use with 'eq' and value=0 to assert ingredient coverage is complete."
    ),
    python_type=int,
    resolver=_items_with_missing_ingredients,
))

# Fulfilment
FULFILMENT_ETA = _reg(FieldSpec(
    path="fulfilment_eta",
    description="Estimated delivery datetime.  Use with 'lte' against a deadline.",
    python_type=datetime,
    resolver=_fulfilment_eta,
))


# ── Public API ─────────────────────────────────────────────────────────────

def get_field_spec(path: str) -> FieldSpec:
    """
    Retrieve a FieldSpec by its registered path.

    Raises KeyError for unknown paths.  Callers (the binder) must treat this
    as a hard bind failure — never catch it silently and fall through.
    """
    if path not in _REGISTRY:
        raise KeyError(
            f"Field path {path!r} is not in the SANKALP field registry.  "
            f"Known paths: {sorted(_REGISTRY.keys())}"
        )
    return _REGISTRY[path]


def all_paths() -> list[str]:
    """Sorted list of all registered paths.  Used in the compiler prompt."""
    return sorted(_REGISTRY.keys())


def resolve(path: str, cart: "Cart") -> Any:
    """
    Resolve a field path against a Cart and return the raw value.

    Raises KeyError for unknown paths — never returns None for an unknown
    path, because a silent None would read as "no violation" to a caller
    doing an excludes check.
    """
    return get_field_spec(path).resolver(cart)
