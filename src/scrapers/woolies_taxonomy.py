"""Woolworths' own product hierarchy -> the app's category labels.

The live browse API never sets ``Department`` (0 of 1,817 half-price tiles on
2026-10-02), so every Woolworths item seen only through the live feed was
written 'Uncategorised' — 47% of that week's Woolworths half-price set. Each
tile does carry Woolworths' internal merchandise hierarchy in
``AdditionalAttributes`` (``sapcategoryname`` / ``sapsubcategoryname``: one
value per product, stable) and its customer-facing aisles
(``piescategorynamesjson``: a list that also holds promotional groupings such
as "Halloween" or "Lunch Box", so it is only a fallback).

Labels come from the existing vocabulary (``data/hotprices_categories.json``),
so the app's browse groups keep working; "Vitamins & Supplements" is the one
addition. The same comparison showed the hotprices code labels misfile some
Woolworths lines (vitamins as Confectionery, potato chips as Frozen Chips,
food storage as Baby Accessories), so a mapped label here is authoritative
for Woolworths (see ``bulk_writer``). Anything unmapped stays
'Uncategorised' — honestly unknown rather than guessed into a shelf.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

UNCATEGORISED = "Uncategorised"

# (sap category, sap subcategory) -> label, checked before the category default.
_BY_SUBCATEGORY: dict[tuple[str, str], str] = {
    ("TOILETRIES", "COSMETICS"): "Cosmetics",
    ("TOILETRIES", "SKIN CARE"): "Skin Care",
    ("TOILETRIES", "SUN CARE"): "Skin Care",
    ("TOILETRIES", "HAIR CARE"): "Hair Care",
    ("TOILETRIES", "HAIR COLOUR"): "Hair Care",
    ("TOILETRIES", "HAIR ACCESSORIES"): "Hair Care",
    ("TOILETRIES", "ORAL CARE"): "Oral Care",
    ("TOILETRIES", "DEODORANT & TALC"): "Personal Care & Hygiene",
    ("TOILETRIES", "PERSONAL WASH"): "Personal Care & Hygiene",
    ("TOILETRIES", "PERIOD AND CONTINENCE CARE"): "Period Care",
    ("TOILETRIES", "WOMENS HAIR REMOVAL"): "Women's Hair Removal",
    ("HEALTH CARE", "VITAMINS"): "Vitamins & Supplements",
    ("BISCUITS", "MUESLI BARS"): "Healthy Snacks & Foods",
    ("BABY NEEDS", "BABY - NAPPIES"): "Nappies & Wipes",
    ("CLEANSING", "INSECTICIDES"): "Pest Control",
    ("CLEANSING", "LAUNDRY - CAPSULES"): "Laundry",
    ("CLEANSING", "LAUNDRY - FABRIC CARE"): "Laundry",
    ("CLEANSING", "LAUNDRY - LIQUIDS"): "Laundry",
    ("CLEANSING", "LAUNDRY - POWDERS"): "Laundry",
    ("CLEANSING", "LAUNDRY - SOAKERS & BLEACH"): "Laundry",
    ("DELI CONVENIENCE", "BISCUITS - SPECIALTY DELI CRACKERS"): "Biscuits & Crackers",
    ("DELI CONVENIENCE", "DAIRY - SMALLGOODS"): "Dips & Cold Packaged Meats",
    ("DELI SERVICE", "CHEESE BULK"): "Cheese",
    ("DELI SERVICE", "SMALLGOODS BULK"): "Dips & Cold Packaged Meats",
    ("DOMESTICWARE", "BBQ"): "Outdoor Living",
    ("FREEZER - VEGETABLES", "FREEZER - POTATO"): "Frozen Chips, Wedges & Potatoes",
    ("FROZEN MEALS", "FREEZER - SNACKING"): "Frozen Party Food",
    ("LIFESTYLE/WATER NON CARBONATED", "SOFT DRINKS - ENERGY"): "Energy Drinks",
    ("LIFESTYLE/WATER NON CARBONATED", "SOFT DRINKS - MINERAL WATER"): "Mineral Water",
    ("LIFESTYLE/WATER NON CARBONATED", "SOFT DRINKS - SPORTS"): "Sports Drinks",
    ("PAPERGOODS", "FOOD WRAPS BAGS AND STORAGE"): "Kitchenware & Storage",
    ("PASTA / RICE", "RICE"): "Rice & Grains",
    ("PET FOOD", "PET NEEDS - CAT FOOD DRY"): "Cat & Kitten",
    ("PET FOOD", "PET NEEDS - CAT FOOD WET"): "Cat & Kitten",
    ("PET FOOD", "PET NEEDS - CAT TREATS"): "Cat & Kitten",
    ("PET FOOD", "PET NEEDS - DOG FOOD DRY"): "Dog & Puppy",
    ("PET FOOD", "PET NEEDS - DOG FOOD WET"): "Dog & Puppy",
    ("PET FOOD", "PET NEEDS - DOG TREATS"): "Dog & Puppy",
    ("PROPRIETARY BAKERY", "RESALE CAKE"): "Cakes, Muffins & Pastries",
}

# sap category -> label when the subcategory has no specific entry.
_BY_CATEGORY: dict[str, str] = {
    "APPAREL": "Clothing & Accessories",
    "BABY NEEDS": "Baby",
    "BEVERAGES": "Drinks",
    "BISCUITS": "Biscuits & Crackers",
    "BREAKFAST FOODS": "Breakfast & Spreads",
    "CARBONATED SOFT DRINKS": "Soft Drinks",
    "CHEESE ENTERTAINING": "Cheese",
    "CLEANSING": "Cleaning Goods",
    "CONDIMENTS": "Condiments & Dressings",
    "CONFECTIONERY": "Confectionery",
    "COOKING NEEDS": "Cooking & Baking Needs",
    "CORDIAL / DRINK BASES": "Cordials",
    "DAIRY - YOGHURT": "Yogurt",
    "DOMESTICWARE": "Kitchenware & Storage",
    "ELECTRICAL": "Hardware",
    "ETHNIC / GOURMET FOOD": "International Foods",
    "FREEZER - DESSERTS & PASTRY": "Ice Cream & Frozen Desserts",
    "FREEZER - FISH": "Frozen Seafood",
    "FREEZER - VEGETABLES": "Frozen Vegetables & Fruit",
    "FROZEN MEALS": "Frozen Meals",
    "GARDEN AIDS/SEEDS/BULBS": "Garden & Outdoors",
    "HARDWARE": "Hardware",
    "HEALTH CARE": "First Aid & Medicinal",
    "HEALTH FOODS": "Healthy Snacks & Foods",
    "HOUSEHOLD CLEANING": "Cleaning Goods",
    "LIFESTYLE/WATER NON CARBONATED": "Drinks",
    "LONGLIFE JUICE / DRINKS": "Juice",
    "MANCHESTER": "Manchester & Bedding",
    "NEWSAGENCY": "Books & Magazines",
    "PAPERGOODS": "Toilet Paper, Tissues & Paper Towels",
    "PASTA / RICE": "Pasta & Noodles",
    "PET FOOD": "Pet",
    "PREPARED FOODS": "Packaged Meals",
    "PROPRIETARY BAKERY": "Bakery",
    "SNACKS": "Savoury Snacks",
    "STATIONERY": "Stationery & Office Supplies",
    "TOILETRIES": "Beauty & Personal Care",
    "TOYS": "Toys & Games",
}

# Every label a Woolworths aisle name may match exactly (the fallback).
_KNOWN_LABELS: frozenset[str] = frozenset(
    json.loads(
        (Path(__file__).resolve().parent.parent / "data" / "hotprices_categories.json")
        .read_text(encoding="utf-8")
    ).values()
) | frozenset(_BY_SUBCATEGORY.values()) | frozenset(_BY_CATEGORY.values())


def _aisles(attrs: dict[str, Any]) -> list[str]:
    try:
        names = json.loads(attrs.get("piescategorynamesjson") or "[]")
    except (TypeError, ValueError):
        return []
    return [n for n in names if isinstance(n, str)]


def category_from_attributes(attrs: dict[str, Any] | None) -> str:
    """The app category for one browse-API tile's ``AdditionalAttributes``."""
    if not attrs:
        return UNCATEGORISED
    cat = (attrs.get("sapcategoryname") or "").strip().upper()
    sub = (attrs.get("sapsubcategoryname") or "").strip().upper()
    label = _BY_SUBCATEGORY.get((cat, sub)) or _BY_CATEGORY.get(cat)
    if label:
        return label
    for aisle in _aisles(attrs):
        if aisle in _KNOWN_LABELS and aisle != UNCATEGORISED:
            return aisle
    return UNCATEGORISED
