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

    def qualify(self, company: str, country: str = "", website: str = "") -> dict:
        """Returns {is_fit, company_type, ai_note, error_detail?}."""
        try:
            snippets = self._product_snippets(company, country)
        except requests.RequestException as exc:
            return {"is_fit": "Unknown", "company_type": "Unknown",
                    "ai_note": "", "error_detail": str(exc)}

        prompt = f"""
You are a B2B Sales Analyst for a glass packaging (bottles and jars) manufacturer.
Analyze the company below and answer three questions.

Company: '{company}'
Country: {country}
Website: {website}

Search snippets:
{snippets}

─── QUESTION 1 — Glass fit ───────────────────────────────────────────────────
Does this company's product line typically use glass packaging?

YES if products are traditionally packaged in glass:
  spirits, wine, beer, water, juices, syrups, jams, honey, sauces, pesto,
  mayonnaise, pickles, olives, olive oil, vinegar, cosmetics/perfume in glass,
  pharmaceuticals in glass, baby food in jars, etc.
  (If they make jam or premium sauces, answer Yes even if "glass" isn't mentioned.)

NO if products almost never use glass:
  frozen foods, raw meat, fresh bakery, dry snacks, paper/textile/machinery,
  electronics, bulk grains, plastic products.

OVERRIDE RULES (these beat category norms):
1. If the snippets clearly show the company's ACTUAL packaging format
   (e.g. "canned water", "in pouches", "tetra pak", "aluminum cans"),
   judge by that actual format — NOT by what the category typically uses.
   A canned-water company is No even though premium water is often glass.
2. Companies that MANUFACTURE glass packaging itself (glass bottle / jar
   producers) = No. They SELL glass — they are competitors or suppliers,
   not glass buyers.

─── QUESTION 2 — Company type ────────────────────────────────────────────────
Classify the company as exactly one of:

  Manufacturer  — owns production facilities; makes the product start to finish.
  Co-packer     — fills or packages OTHER brands' products under contract
                  (contract filler, toll manufacturer, private-label producer).
                  They DO buy packaging (glass bottles/jars) in large quantities.
  Brand Owner   — owns a brand but OUTSOURCES all production to third parties.
                  They may influence packaging specs but don't buy packaging directly.
  Distributor   — only imports, distributes, or retails; no production involvement.
  Unknown       — not enough information.

─── QUESTION 3 — AI note ─────────────────────────────────────────────────────
Write ONE short English sentence describing what the company makes or does,
and (if glass fit = Yes) which products would use glass.

─── OUTPUT ───────────────────────────────────────────────────────────────────
Return ONLY valid JSON with these exact keys:
  "GlassFit"    : "Yes" or "No"
  "CompanyType" : one of Manufacturer / Co-packer / Brand Owner / Distributor / Unknown
  "AINote"      : one short English sentence
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
