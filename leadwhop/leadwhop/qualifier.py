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

        prompt = f"""You are a B2B analyst for a GLASS packaging (bottles & jars) manufacturer.
Analyze this company and return a JSON verdict.

Company: '{company}'
Country: {country}
Website: {website}

Search snippets:
{snippets}

Follow this DECISION TREE in exact order. Stop at the first rule that applies.

STEP 1 — Is the company's PRODUCT packaging itself?
(glass/plastic bottle producers, can makers, closure/label/carton makers)
→ GlassFit = "No". They SELL packaging, they don't BUY it. STOP.

STEP 2 — Do the snippets show the company's ACTUAL packaging format?
→ Glass bottles / glass jars mentioned or clearly implied → "Yes". STOP.
→ Pouches, cans, tetra pak, plastic tubs, foil bags, sachets → "No". STOP.
(Actual evidence ALWAYS beats category norms. A baby-food brand selling
in pouches is "No" even though baby food is traditionally jarred.)

STEP 3 — No direct packaging evidence: judge by PRODUCT CATEGORY.
"Yes" categories (traditionally glass): spirits, gin, whisky, rum, vodka,
liqueurs, wine, beer, cider, premium/sparkling water, juices, cordials,
mixers, tonic, kombucha, jams, preserves, honey, sauces, mustard, pesto,
mayonnaise, pickles, olives, olive oil, vinegar, perfume, premium
cosmetics, pharmaceuticals.
"No" categories: crisps/snacks, frozen food, fresh bakery, raw meat,
powdered food/supplements, cereals, chocolate bars, textiles, machinery,
electronics, paper products.

CALIBRATION EXAMPLES:
- Craft gin distillery → Yes (spirits = glass)
- Jam producer → Yes (jars)
- Natural mineral water sold in glass → Yes
- Baby food brand selling in squeezable pouches → No (STEP 2 evidence)
- Crisps in foil bags → No
- Powdered meal replacement in plastic tubs → No
- Canned water brand → No (STEP 2 evidence)
- Glass bottle manufacturer → No (STEP 1)

COMPANY TYPE — default is "Manufacturer":
- Wineries, distilleries, breweries, farms, food processors = Manufacturer, always.
- "Co-packer" ONLY if snippets explicitly mention contract filling /
  co-packing / private label FOR OTHER BRANDS.
- "Brand Owner" ONLY if snippets explicitly mention outsourced production.
- "Distributor" ONLY if snippets show importing/distributing with no own production.
- Absence of facility info is NOT evidence of outsourcing → "Manufacturer".

Return ONLY valid JSON with exactly these keys:
"GlassFit": "Yes" or "No"
"CompanyType": Manufacturer / Co-packer / Brand Owner / Distributor / Unknown
"AINote": ONE short English sentence — what they make and (if Yes) which
products would use glass; (if No) their packaging format or why unfit.
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
