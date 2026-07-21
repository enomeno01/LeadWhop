"""Stage 2 — ICP qualification via product web research.

Returns four fields per company:
  - is_fit          : Yes / No  — do their products suit glass packaging?
  - company_type    : Manufacturer / Co-packer / Brand Owner / Distributor / Unknown
  - ai_note         : one short English sentence — what they make / do
  - error_detail    : set only on API failure

Company type definitions used in the prompt:
  Manufacturer  — owns production facilities, makes the product start to finish
  Co-packer     — fills / packages other brands' products (contract filler)
  Brand Owner   — owns the brand but outsources all production
  Distributor   — only distributes / imports, no production involvement
"""
from __future__ import annotations

import json
import os
import time

import requests

from .llm import LLM
from . import status


class Qualifier:
    def __init__(self, llm: LLM, settings: dict):
        self.llm = llm
        self.serper_key = os.environ["SERPER_API_KEY"]
        self.url = settings["search"]["serper_url"]
        self.gl  = settings["search"].get("gl", "us")
        self.hl  = settings["search"].get("hl", "en")
        self.icp = settings["icp"]
        self.sleep = settings["rate_limits"]["sleep_between_calls"]

    def _product_snippets(self, company: str, country: str) -> str:
        query = f'"{company}" {country} products OR catalog'.strip()
        resp = requests.post(
            self.url,
            headers={"X-API-KEY": self.serper_key,
                     "Content-Type": "application/json"},
            data=json.dumps({"q": query, "gl": self.gl, "hl": self.hl}),
            timeout=20,
        )
        if not resp.ok:
            status.warn(status.classify_api_error("Serper", resp.status_code, resp.text))
            resp.raise_for_status()
        organic = resp.json().get("organic", [])[:4]
        return "\n".join(
            f"- {r.get('title','')}: {r.get('snippet','')}" for r in organic
        )

    def qualify(self, company: str, country: str = "", website: str = "", custom_instructions: str = "") -> dict:
        """Returns {is_fit, company_type, ai_note, error_detail?}."""
        try:
            snippets = self._product_snippets(company, country)
        except requests.RequestException as exc:
            return {"is_fit": "Unknown", "company_type": "Unknown",
                    "ai_note": "", "error_detail": str(exc)}

        additional_instructions = (
            f"\n\nADDITIONAL INSTRUCTIONS FROM USER:\n{custom_instructions}"
            if custom_instructions and custom_instructions.strip() else ""
        )

        prompt = f"""
You are an intelligent B2B Sales Analyst for a glass packaging manufacturer.

Your task:
Analyze whether this company is a good potential customer for glass packaging such as jars and bottles.

Company Name: '{company}'
Country: {country}
Website: {website}

Search Snippets:
{snippets}

Decision logic:
- CamPotansiyeli = "Yes" if the company produces products that are generally or traditionally packaged in glass jars or glass bottles.
- Examples for "Yes": jams, honey, sauces, spirits, vodka, whiskey, rum, pasta sauces, pesto, mayonnaise, pickles, olives, olive oil, vinegar, fruit juices, syrups, cold brew coffee, kombucha, wine, beer, premium beverages, cosmetics in glass, food preserves.
- CamPotansiyeli = "No" if the company mainly produces products that are almost never packaged in glass.
- Examples for "No": frozen foods, raw meat, poultry, seafood, fresh bakery, dry snacks in plastic pouches, chips, paper products, bulk grains, fresh produce, machinery, services, packaging distributors without own food/beverage production.

OVERRIDE RULES (these beat category norms — check first):
1. If the snippets clearly show the company's ACTUAL packaging format (e.g. "canned water", "in pouches", "tetra pak", "aluminum cans"), judge by that actual format, NOT by category norms.
2. Companies that MANUFACTURE glass packaging itself (glass bottle / jar producers) = No. They SELL glass — they are competitors or suppliers, not buyers.

COMPANY TYPE — default is "Manufacturer":
- Wineries, distilleries, breweries, farms, food processors = Manufacturer, always.
- "Co-packer" ONLY if snippets explicitly mention contract filling / co-packing / private label FOR OTHER BRANDS.
- "Brand Owner" ONLY if snippets explicitly mention outsourced production.
- "Distributor" ONLY if snippets show importing/distributing with no own production.
- Absence of facility info is NOT evidence of outsourcing → "Manufacturer".

Important:
- Do not require the word "glass" to appear.
- If the company makes products that are commonly sold in jars or bottles, answer "Yes".
- If search results are weak but the product category clearly fits glass, answer based on product logic.
- If there is not enough information, use your best judgment from the snippets.{additional_instructions}

Return ONLY valid JSON. All text must be in English.

JSON keys:
- "GlassFit": exactly "Yes" or "No"
- "CompanyType": exactly one of: Manufacturer / Co-packer / Brand Owner / Distributor / Unknown
- "AINote": exactly one short English sentence explaining the product fit or why it does not fit.
"""
        verdict = self.llm.json_call(
            system=("You are a precise B2B sales analyst. Output strict JSON only. "
                    "No markdown, no explanation outside the JSON."),
            user=prompt,
            max_tokens=100,
        )
        time.sleep(self.sleep)

        return {
            "is_fit":       verdict.get("GlassFit", "Unknown"),
            "company_type": verdict.get("CompanyType", "Unknown"),
            "ai_note":      verdict.get("AINote", ""),
        }
