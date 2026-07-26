"""The two companion sheets that ship alongside the CRM import.

Sheet 2 — Sub Category Maps : what the lead would buy, and how much of it.
Sheet 3 — Locations         : where the lead operates.

Both are keyed by Email so they can be joined to the CRM sheet later; Lead__c
is deliberately left blank for the Salesforce Ids to be filled in after import.

Cost note: the sub-category, the volume estimate and the site list all come
from a SINGLE GPT call per company, not three. City names are then turned into
Salesforce Ids by a local lookup, which costs nothing.
"""
from __future__ import annotations

import json
import time

import pandas as pd

from . import catalog
from .llm import LLM

SUB_CATEGORY_COLUMNS = [
    "Lead__c", "Email", "Product_Category__c", "Product_Sub_Category__c",
    "Units_Insight__c", "Unit_Insight_Type__c", "Packaging_Type__c",
]

LOCATION_COLUMNS = [
    "Lead__c", "Email", "City__c", "LocationNum__c", "Type__c",
]

# Units_Insight__c is a round number, never a raw model guess.
UNIT_STEPS = [500_000, 1_000_000, 2_000_000, 3_000_000, 5_000_000,
              10_000_000, 20_000_000, 50_000_000, 100_000_000]


def _round_units(value) -> int:
    try:
        v = int(float(str(value).replace(",", "").replace(" ", "")))
    except Exception:
        return 500_000
    if v < 500_000:            # floor: anything but a micro-producer buys 500k+
        return 500_000
    return min(UNIT_STEPS, key=lambda s: abs(s - v))


class CRMSheets:
    def __init__(self, llm: LLM, sleep: float = 0.2):
        self.llm = llm
        self.sleep = sleep

    # ── one call per company, cached ────────────────────────────────────────

    def profile(self, company: str, country: str, ai_note: str,
                website: str = "") -> dict:
        """-> {sub_category, units, locations:[{city,country,type}]}"""
        cache = self.llm._cache("crm_sheets")
        key = f"{company}|{country}".lower()
        hit = cache.get(key)
        if hit is not None:
            try:
                return json.loads(hit)
            except Exception:
                pass

        prompt = f"""You prepare CRM records for a glass packaging manufacturer.

Company: {company}
Country: {country}
Website: {website}
What they do: {ai_note or "unknown"}

Answer three things about this company.

1. PRODUCT SUB CATEGORY — which single glass pack would they buy most of?
Choose exactly one name from this list:
{chr(10).join('- ' + n for n in catalog.SUB_CATEGORY_NAMES)}
Pick the most specific one that fits their main product. Use "PACKAGE" only
when nothing else fits.

2. ANNUAL UNITS — estimated glass containers bought per year, as an integer.
Round to one of: 500000, 1000000, 2000000, 3000000, 5000000, 10000000,
20000000, 50000000, 100000000.
Guidance: a small artisan producer 500000; a regional producer 1000000 to
5000000; a national brand 10000000 to 20000000; a large multinational
50000000 or more. Never go below 500000 unless the company is a micro
producer.

3. LOCATIONS — the company's known sites. Only real, verifiable places you are
confident about: headquarters, factories, distribution centres. Give the city
and its country. Do not invent locations; if you only know the head office,
return just that one. Maximum 5.
Each location has a type, exactly one of:
{", ".join(catalog.LOCATION_TYPES)}

Return ONLY valid JSON:
{{
  "sub_category": "one name from the list above",
  "units": 1000000,
  "locations": [
    {{"city": "Parma", "country": "Italy", "type": "HQ"}},
    {{"city": "Modena", "country": "Italy", "type": "Manufacturing"}}
  ]
}}"""

        raw = self.llm.json_call(
            system=("You are a precise B2B market analyst. Return strict JSON "
                    "only, no markdown, no commentary."),
            user=prompt,
            max_tokens=400,
        )
        time.sleep(self.sleep)

        locations = []
        for loc in (raw.get("locations") or [])[:5]:
            if not isinstance(loc, dict):
                continue
            city = str(loc.get("city", "")).strip()
            if not city:
                continue
            ltype = str(loc.get("type", "")).strip()
            if ltype not in catalog.LOCATION_TYPES:
                ltype = "HQ" if not locations else "Manufacturing"
            locations.append({"city": city,
                              "country": str(loc.get("country", "")).strip(),
                              "type": ltype})

        result = {
            "sub_category": str(raw.get("sub_category", "")).strip(),
            "units": _round_units(raw.get("units")),
            "locations": locations,
        }
        if result["sub_category"]:
            cache.set(key, json.dumps(result))
        return result

    # ── sheet builders ──────────────────────────────────────────────────────

    def build(self, df: pd.DataFrame, progress_cb=None
              ) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Returns (sub_category_maps, locations) for rows that have an email."""
        sub_rows, loc_rows = [], []
        rows = list(df.iterrows())
        for n, (_, row) in enumerate(rows, start=1):
            email = str(row.get("Email") or "").strip()
            if "@" not in email:
                continue

            company = str(row.get("Company") or "").strip()
            country = str(row.get("Country") or "").strip()
            note    = str(row.get("AI_Note") or "").strip()
            site    = str(row.get("Website") or "").strip()

            prof = self.profile(company, country, note, site)
            sub_id, cat_id, _ = catalog.resolve_sub_category(prof["sub_category"])

            sub_rows.append({
                "Lead__c": "",
                "Email": email,
                "Product_Category__c": cat_id,
                "Product_Sub_Category__c": sub_id,
                "Units_Insight__c": prof["units"],
                "Unit_Insight_Type__c": catalog.UNIT_INSIGHT_TYPE,
                "Packaging_Type__c": catalog.PACKAGING_TYPE,
            })

            found = prof["locations"] or [{"city": "", "country": country,
                                           "type": "HQ"}]
            num = 0
            for loc in found:
                cid = catalog.city_id(loc["city"], loc["country"] or country)
                if not cid:
                    continue                     # unknown city -> skip the row
                num += 1
                loc_rows.append({
                    "Lead__c": "",
                    "Email": email,
                    "City__c": cid,
                    "LocationNum__c": num,
                    "Type__c": loc["type"],
                })

            if progress_cb:
                progress_cb(n, len(rows))

        return (pd.DataFrame(sub_rows, columns=SUB_CATEGORY_COLUMNS),
                pd.DataFrame(loc_rows, columns=LOCATION_COLUMNS))
