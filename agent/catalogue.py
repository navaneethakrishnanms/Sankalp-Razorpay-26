"""
Synthetic merchant catalogue.

Hand-authored, fixed data — not generated.  This is the "world" the eval
corpus generator builds forward from (see eval/generator.py): every seed
order's clean cart is assembled from real catalogue items, and every
violation is produced by mutating that cart, never by inventing a
criterion first and working backward.

Prices, ingredients and categories are deliberately realistic enough that
dietary and budget mutations have something genuine to bite on (e.g. a
beef item exists so an "excludes beef" criterion can actually fail against
it, not just against a synthetic placeholder).
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal


@dataclasses.dataclass(frozen=True)
class CatalogueItem:
    name:        str
    unit_price:  Decimal
    ingredients: tuple[str, ...] = ()
    category:    str | None = None


@dataclasses.dataclass(frozen=True)
class CatalogueMerchant:
    id:       str
    name:     str
    category: str
    items:    dict[str, CatalogueItem]   # keyed by item name for lookup


MERCHANTS: dict[str, CatalogueMerchant] = {
    "rest-biryani": CatalogueMerchant(
        id="rest-biryani",
        name="Biryani House",
        category="food_delivery",
        items={
            "chicken biryani": CatalogueItem(
                name="Chicken Biryani", unit_price=Decimal("280.00"),
                ingredients=("chicken", "rice", "spices"), category="non-veg",
            ),
            "mutton biryani": CatalogueItem(
                name="Mutton Biryani", unit_price=Decimal("380.00"),
                ingredients=("mutton", "rice", "spices"), category="non-veg",
            ),
            "beef biryani": CatalogueItem(
                name="Beef Biryani", unit_price=Decimal("340.00"),
                ingredients=("beef", "rice", "spices"), category="non-veg",
            ),
            "veg biryani": CatalogueItem(
                name="Veg Biryani", unit_price=Decimal("220.00"),
                ingredients=("mixed vegetables", "rice", "spices"), category="veg",
            ),
            "raita": CatalogueItem(
                name="Raita", unit_price=Decimal("60.00"),
                ingredients=("yogurt", "cucumber"), category="veg",
            ),
            "chicken 65": CatalogueItem(
                name="Chicken 65", unit_price=Decimal("240.00"),
                ingredients=("chicken", "chilli", "curry leaves"), category="non-veg",
            ),
            "gulab jamun": CatalogueItem(
                name="Gulab Jamun", unit_price=Decimal("90.00"),
                ingredients=("milk solids", "sugar syrup"), category="dessert",
            ),
        },
    ),
    "rest-pizza": CatalogueMerchant(
        id="rest-pizza",
        name="Pizza Point",
        category="food_delivery",
        items={
            "margherita": CatalogueItem(
                name="Margherita", unit_price=Decimal("249.00"),
                ingredients=("cheese", "tomato", "basil"), category="veg",
            ),
            "pepperoni": CatalogueItem(
                name="Pepperoni", unit_price=Decimal("399.00"),
                ingredients=("cheese", "tomato", "pepperoni", "pork"), category="non-veg",
            ),
            "chicken supreme": CatalogueItem(
                name="Chicken Supreme", unit_price=Decimal("449.00"),
                ingredients=("cheese", "chicken", "capsicum", "onion"), category="non-veg",
            ),
            "garlic bread": CatalogueItem(
                name="Garlic Bread", unit_price=Decimal("129.00"),
                ingredients=("bread", "garlic", "butter"), category="veg",
            ),
            "veggie delight": CatalogueItem(
                name="Veggie Delight", unit_price=Decimal("299.00"),
                ingredients=("cheese", "capsicum", "olives", "corn"), category="veg",
            ),
            "coke 500ml": CatalogueItem(
                name="Coke 500ml", unit_price=Decimal("60.00"),
                ingredients=("carbonated water", "sugar"), category="veg",
            ),
        },
    ),
    "rest-southindian": CatalogueMerchant(
        id="rest-southindian",
        name="Saravana Bhavan",
        category="food_delivery",
        items={
            "masala dosa": CatalogueItem(
                name="Masala Dosa", unit_price=Decimal("140.00"),
                ingredients=("rice batter", "potato", "spices"), category="veg",
            ),
            "idli": CatalogueItem(
                name="Idli", unit_price=Decimal("80.00"),
                ingredients=("rice batter", "urad dal"), category="veg",
            ),
            "filter coffee": CatalogueItem(
                name="Filter Coffee", unit_price=Decimal("40.00"),
                ingredients=("coffee", "milk", "sugar"), category="veg",
            ),
            # ingredients deliberately undeclared — used to construct
            # abstain_expected CONSTRAINT_VIOLATION records.
            "chef special curry": CatalogueItem(
                name="Chef Special Curry", unit_price=Decimal("260.00"),
                ingredients=(), category=None,
            ),
            "chicken chettinad": CatalogueItem(
                name="Chicken Chettinad", unit_price=Decimal("290.00"),
                ingredients=("chicken", "spices", "coconut"), category="non-veg",
            ),
        },
    ),
    "rest-punjabi": CatalogueMerchant(
        id="rest-punjabi",
        name="Punjabi Dhaba",
        category="food_delivery",
        items={
            "butter chicken": CatalogueItem(
                name="Butter Chicken", unit_price=Decimal("320.00"),
                ingredients=("chicken", "butter", "tomato", "cream"), category="non-veg",
            ),
            "dal makhani": CatalogueItem(
                name="Dal Makhani", unit_price=Decimal("210.00"),
                ingredients=("black lentils", "butter", "cream"), category="veg",
            ),
            "naan": CatalogueItem(
                name="Naan", unit_price=Decimal("45.00"),
                ingredients=("flour", "yogurt"), category="veg",
            ),
            "paneer tikka": CatalogueItem(
                name="Paneer Tikka", unit_price=Decimal("260.00"),
                ingredients=("paneer", "capsicum", "yogurt", "spices"), category="veg",
            ),
            "mutton curry": CatalogueItem(
                name="Mutton Curry", unit_price=Decimal("360.00"),
                ingredients=("mutton", "onion", "spices"), category="non-veg",
            ),
            # ingredients deliberately undeclared — clean-cart mutation fixture for
            # "undeclared ingredient with no dietary criterion in play" (still CLEAN).
            "chef thali": CatalogueItem(
                name="Chef Thali", unit_price=Decimal("230.00"),
                ingredients=(), category=None,
            ),
        },
    ),
    "grocery-freshmart": CatalogueMerchant(
        id="grocery-freshmart",
        name="FreshMart",
        category="grocery",
        items={
            "milk 1l": CatalogueItem(
                name="Milk 1L", unit_price=Decimal("68.00"),
                ingredients=("milk",), category="dairy",
            ),
            "eggs 12pk": CatalogueItem(
                name="Eggs 12pk", unit_price=Decimal("90.00"),
                ingredients=("eggs",), category="non-veg",
            ),
            "basmati rice 5kg": CatalogueItem(
                name="Basmati Rice 5kg", unit_price=Decimal("650.00"),
                ingredients=("rice",), category="staple",
            ),
            "chicken breast 1kg": CatalogueItem(
                name="Chicken Breast 1kg", unit_price=Decimal("280.00"),
                ingredients=("chicken",), category="non-veg",
            ),
            "toor dal 1kg": CatalogueItem(
                name="Toor Dal 1kg", unit_price=Decimal("165.00"),
                ingredients=("toor dal",), category="staple",
            ),
            "onions 1kg": CatalogueItem(
                name="Onions 1kg", unit_price=Decimal("40.00"),
                ingredients=("onion",), category="produce",
            ),
            "tomatoes 1kg": CatalogueItem(
                name="Tomatoes 1kg", unit_price=Decimal("45.00"),
                ingredients=("tomato",), category="produce",
            ),
            # ingredients deliberately undeclared.
            "surprise snack box": CatalogueItem(
                name="Surprise Snack Box", unit_price=Decimal("150.00"),
                ingredients=(), category=None,
            ),
        },
    ),
    "grocery-dailybasket": CatalogueMerchant(
        id="grocery-dailybasket",
        name="DailyBasket",
        category="grocery",
        items={
            "apples 1kg": CatalogueItem(
                name="Apples 1kg", unit_price=Decimal("180.00"),
                ingredients=("apple",), category="produce",
            ),
            "bread loaf": CatalogueItem(
                name="Bread Loaf", unit_price=Decimal("55.00"),
                ingredients=("wheat flour", "yeast"), category="staple",
            ),
            "paneer 200g": CatalogueItem(
                name="Paneer 200g", unit_price=Decimal("90.00"),
                ingredients=("milk",), category="dairy",
            ),
            "butter 500g": CatalogueItem(
                name="Butter 500g", unit_price=Decimal("240.00"),
                ingredients=("milk",), category="dairy",
            ),
            "mutton mince 500g": CatalogueItem(
                name="Mutton Mince 500g", unit_price=Decimal("310.00"),
                ingredients=("mutton",), category="non-veg",
            ),
            "eggs 6pk": CatalogueItem(
                name="Eggs 6pk", unit_price=Decimal("55.00"),
                ingredients=("eggs",), category="non-veg",
            ),
            # ingredients deliberately undeclared.
            "mystery hamper": CatalogueItem(
                name="Mystery Hamper", unit_price=Decimal("400.00"),
                ingredients=(), category=None,
            ),
        },
    ),
}


def merchant(merchant_id: str) -> CatalogueMerchant:
    return MERCHANTS[merchant_id]


def item(merchant_id: str, item_name: str) -> CatalogueItem:
    return MERCHANTS[merchant_id].items[item_name.lower()]


def other_merchant_id(merchant_id: str, category: str | None = None) -> str:
    """
    Deterministically pick a merchant other than `merchant_id`, optionally
    restricted to a given category.  Used to construct WRONG_MERCHANT
    mutations.  Selection is by sorted id order — no randomness — so the
    same inputs always yield the same output.
    """
    candidates = sorted(
        m.id for m in MERCHANTS.values()
        if m.id != merchant_id and (category is None or m.category == category)
    )
    if not candidates:
        raise ValueError(f"No alternative merchant found for {merchant_id!r} / {category!r}")
    return candidates[0]
