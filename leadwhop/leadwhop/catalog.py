"""Salesforce reference data for the CRM export sheets.

Two things live here:

1. The product catalogue, filtered down to the food & beverage packaging rows
   that GCA actually sells. The full Salesforce list also covers pharma,
   cosmetics, drinkware, tableware and so on; feeding all of that to the model
   only makes the choice harder and the answer worse.

2. A city-name -> Salesforce City Id resolver. This is a plain dictionary
   lookup over a bundled CSV: it costs no API credits at all, neither search
   nor LLM. Only deciding WHICH cities a company operates in needs the model.

Every sub-category belongs to exactly one category, so the model is asked for
the sub-category alone and the category is filled in from this map. That makes
an inconsistent pair structurally impossible.
"""
from __future__ import annotations

import csv
import functools
import re
import unicodedata
from pathlib import Path

# ── Product_Category__c ──────────────────────────────────────────────────────
CATEGORY_IDS = {
    "Alcoholic Beverages":    "a3cWP0000001E7QYAU",
    "Food":                   "a3cWP0000001E7RYAU",
    "Non-Alcoholic Beverage": "a3cWP0000001E7SYAU",
    "JARS":                   "a3cWP00000076FKYAY",
    "BOTTLE":                 "a3cWP00000076FLYAY",
    "PACKAGE":                "a3cWP00000076FRYAY",
}

# ── Product_Sub_Category__c -> (Id, parent category name) ────────────────────
# Kept: packaging for food and drink. Dropped: pharma, cosmetics, decoration,
# home accessories, drinkware, kitchenware, serveware, tableware and the whole
# glassware range (tumblers, stemware, mugs, ashtrays...) — those are tableware
# products, not packaging a food producer would buy.
SUB_CATEGORIES: dict[str, tuple[str, str]] = {
    # Alcoholic Beverages
    "Wine Bottles":                 ("a3ZWP000000uM7o2AE", "Alcoholic Beverages"),
    "Beer Bottles":                 ("a3ZWP000000uM7p2AE", "Alcoholic Beverages"),
    "Spirits":                      ("a3ZWP000000uM7q2AE", "Alcoholic Beverages"),
    "Prosecco & Champagne Bottles": ("a3ZWP000000uM7r2AE", "Alcoholic Beverages"),
    "Ready to Drink":               ("a3ZWP000000uM7s2AE", "Alcoholic Beverages"),
    # Food
    "Oil & Vinegar Bottles":        ("a3ZWP000000uM7t2AE", "Food"),
    "Sauce Bottles":                ("a3ZWP000000uM7u2AE", "Food"),
    "Dairy Jars":                   ("a3ZWP000000uM7v2AE", "Food"),
    "Spread Jars":                  ("a3ZWP000000uM7z2AE", "Food"),
    "Spice Jars":                   ("a3ZWP000000uM802AE", "Food"),
    "Canned Food Jars":             ("a3ZWP000000uM832AE", "Food"),
    "Granular Coffee Jars":         ("a3ZWP000000uM842AE", "Food"),
    "Other Food Jars":              ("a3ZWP000000uM852AE", "Food"),
    "Baby Food Jars":               ("a3ZWP000000uM862AE", "Food"),
    "Jam Jars":                     ("a3ZWP00000187gT2AQ", "Food"),
    # Non-Alcoholic Beverage
    "Milk Bottles":                 ("a3ZWP000000uM872AE", "Non-Alcoholic Beverage"),
    "Water Bottles":                ("a3ZWP000000uM882AE", "Non-Alcoholic Beverage"),
    "Carbonated Beverages":         ("a3ZWP000000uM892AE", "Non-Alcoholic Beverage"),
    "Mineral Water Bottles":        ("a3ZWP000000uM8A2AU", "Non-Alcoholic Beverage"),
    "Juice Bottles":                ("a3ZWP000000uM8B2AU", "Non-Alcoholic Beverage"),
    "Other Cold Beverages":         ("a3ZWP000000uM8C2AU", "Non-Alcoholic Beverage"),
    "Sparkling Water Bottles":      ("a3ZWP0000015dBR2AY", "Non-Alcoholic Beverage"),
    # Generic fallbacks
    "JAR":                          ("a3ZWP000002nJBe2AM", "JARS"),
    "BOTTLE":                       ("a3ZWP000002nJBi2AM", "BOTTLE"),
    "PACKAGE":                       ("a3ZWP000007hVqv2AE", "PACKAGE"),
}

SUB_CATEGORY_NAMES = list(SUB_CATEGORIES)
DEFAULT_SUB_CATEGORY = "PACKAGE"

# ── Type__c (location role) ─────────────────────────────────────────────────
LOCATION_TYPES = ["HQ", "Manufacturing", "Storage", "Distribution",
                  "Sales", "Showroom", "Procurement"]

# ── Packaging_Type__c ───────────────────────────────────────────────────────
PACKAGING_TYPE = "Glass"      # GCA only sells glass
UNIT_INSIGHT_TYPE = "Unit"


def resolve_sub_category(name: str) -> tuple[str, str, str]:
    """Sub-category name -> (sub_category_id, category_id, canonical name).

    Falls back to PACKAGE when the model returns something unexpected, so the
    sheet never carries an Id that Salesforce would reject.
    """
    key = str(name or "").strip()
    if key not in SUB_CATEGORIES:
        for candidate in SUB_CATEGORIES:
            if candidate.lower() == key.lower():
                key = candidate
                break
        else:
            key = DEFAULT_SUB_CATEGORY
    sub_id, cat_name = SUB_CATEGORIES[key]
    return sub_id, CATEGORY_IDS[cat_name], key


# ── City resolution (no API cost) ───────────────────────────────────────────

def _norm(text: str) -> str:
    t = unicodedata.normalize("NFKD", str(text or "").strip().lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r"[^a-z0-9 ]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


@functools.lru_cache(maxsize=1)
def _city_index() -> tuple[dict, dict]:
    """(by_country_and_city, by_city_only) -> Salesforce City Id."""
    path = Path(__file__).parent / "data" / "cities.csv"
    by_pair: dict[tuple[str, str], str] = {}
    by_city: dict[str, str] = {}
    ambiguous: set[str] = set()
    if not path.exists():
        return by_pair, by_city
    with path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            cid, city = row.get("Id", ""), _norm(row.get("Name", ""))
            country, iso = _norm(row.get("Country", "")), _norm(row.get("ISO3", ""))
            if not cid or not city:
                continue
            by_pair.setdefault((country, city), cid)
            if iso:
                by_pair.setdefault((iso, city), cid)
            # A bare city name is only usable while it stays unique.
            if city in by_city and by_city[city] != cid:
                ambiguous.add(city)
            by_city.setdefault(city, cid)
    for city in ambiguous:
        by_city.pop(city, None)
    return by_pair, by_city


def city_id(city: str, country: str = "") -> str:
    """City name (+ country when known) -> Salesforce Id. '' when unknown.

    Pure lookup: no network call, no credits. Country is used first because
    564 city names in the Salesforce list exist in more than one country.
    """
    by_pair, by_city = _city_index()
    c, k = _norm(city), _norm(country)
    if not c:
        return ""
    if k and (k, c) in by_pair:
        return by_pair[(k, c)]
    # Try the English name of a Turkish country label ("Fransa" -> "France").
    if k:
        try:
            from .crm_exporter import match_country
            eng = _norm(match_country(country))
            if eng and (eng, c) in by_pair:
                return by_pair[(eng, c)]
        except Exception:
            pass
    return by_city.get(c, "")
