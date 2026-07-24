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
import re
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

    @staticmethod
    def _english_country(country: str) -> str:
        """Turkish / mixed country labels -> English, for English-language search.

        The input sheet often carries Turkish names ("Fransa", "Hollanda") or
        combined ones ("Hollanda/Belcika"). Sending those into an English
        Google query badly degrades the snippets, which is the single biggest
        cause of false "No" verdicts.
        """
        from .crm_exporter import match_country
        raw = str(country or "").strip()
        if not raw:
            return ""
        hit = match_country(raw)
        if hit:
            return hit
        # combined values: take the first part
        for sep in ("/", ",", "-", "&"):
            if sep in raw:
                first = raw.split(sep)[0].strip()
                hit = match_country(first)
                if hit:
                    return hit
        return raw

    def _search(self, query: str) -> list[dict]:
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
        return resp.json().get("organic", [])[:4]

    def _product_snippets(self, company: str, country: str,
                          website: str = "") -> str:
        from .utils import clean_domain
        country_en = self._english_country(country)
        domain = clean_domain(website) if website else ""

        # Search the DOMAIN first whenever we know it. The Company Name column
        # is frequently a retail banner, a private label, a holding company or
        # a parent brand ("Hacendado (Mercadona)"), which pulls back snippets
        # about the wrong business. The domain names exactly one operating
        # company, so it produces far better grounded evidence.
        if domain:
            primary  = f"{domain} products OR catalog"
            fallback = f'"{company}" {country_en} products'.strip()
        else:
            primary  = f'"{company}" {country_en} products OR catalog'.strip()
            fallback = f"{company} {country_en}".strip()

        organic = self._search(primary)
        if not organic:
            try:
                organic = self._search(fallback)
            except requests.RequestException:
                organic = []

        return "\n".join(
            f"- {r.get('title','')}: {r.get('snippet','')}" for r in organic
        )

    @staticmethod
    def _identity_key(company: str, website: str) -> str:
        """Stable identity for a company, independent of the Country column.

        The same company must always receive the same verdict. Keying on the
        domain (or, failing that, on a normalised legal-suffix-stripped name)
        means a duplicate row cannot contradict the first one, and costs no
        extra Serper or OpenAI calls.
        """
        from .utils import clean_domain, domain_base
        domain = clean_domain(website) if website else ""
        if domain:
            return f"d:{domain_base(domain)}"
        name = re.sub(r"[^a-z0-9 ]", " ", str(company).lower())
        name = re.sub(
            r"\b(s\s*r\s*l|srl|spa|s\s*p\s*a|bv|b\s*v|nv|gmbh|ltd|limited|"
            r"inc|llc|plc|sa|s\s*a|ag|as|oy|ab|aps|kft|sarl|co|company|"
            r"holding|holdings|group|the)\b", " ", name)
        return "n:" + re.sub(r"\s+", " ", name).strip()

    def qualify(self, company: str, country: str = "", website: str = "",
                custom_instructions: str = "") -> dict:
        """Returns {is_fit, company_type, ai_note, error_detail?}."""
        # One verdict per company, always. Cached across rows AND across runs.
        cache = self.llm._cache("qualify_verdict")
        # Cache files are already namespaced per model in LLM._cache().
        ckey = self._identity_key(company, website)
        if custom_instructions and custom_instructions.strip():
            ckey += "|" + str(hash(custom_instructions.strip()))
        cached = cache.get(ckey)
        if cached is not None:
            try:
                return json.loads(cached)
            except Exception:
                pass

        try:
            snippets = self._product_snippets(company, country, website)
        except requests.RequestException as exc:
            return {"is_fit": "Unknown", "company_type": "Unknown",
                    "ai_note": "", "error_detail": str(exc)}

        additional_instructions = (
            f"ADDITIONAL INSTRUCTIONS FROM USER:\n{custom_instructions}"
            if custom_instructions and custom_instructions.strip() else ""
        )

        prompt = f"""
You are a precise B2B Sales Analyst working for a manufacturer of empty glass
bottles and glass jars.

Your task is to determine whether the company is a plausible customer for glass
packaging used in food or beverage products.

A company may qualify if it manufactures, fills, bottles, packs, sources or
controls the packaging of relevant products. It does not need to manufacture
everything in-house.

CLASSIFICATION RULES

Set "GlassFit" to "Yes" when the company has a realistic need for empty glass
bottles or jars for food or beverage products.

Typical Yes categories include:

- Alcoholic beverages such as spirits, wine, beer, cider, liqueurs and cocktails
- Non-alcoholic beverages such as juices, premium soft drinks, mineral water,
  kombucha, cold brew coffee, bottled tea, syrups and beverage concentrates
- Sauces and condiments such as ketchup, mustard, mayonnaise, hot sauce,
  cooking sauces, pasta sauce, pesto, dressings, marinades and salsa
- Jams, honey, marmalade, fruit preserves, sweet spreads, nut butters and
  dessert toppings
- Pickles, olives, capers, fermented vegetables, antipasti and preserved foods
- Canned, jarred, bottled or otherwise preserved vegetables, legumes, pulses,
  tomatoes, passata, sweetcorn, artichokes, mushrooms, fruit and compotes
- Olive oil, edible oils, vinegar, lemon juice, extracts and liquid seasonings
- Jarred ready foods such as purees, soups, baby food, pâté, spreads, legumes
  and premium preserved products
- Other premium, artisanal, gourmet or organic food and beverage products for
  which glass provides preservation, resealability or premium presentation

Do not require the word "glass" to appear. Infer potential from the product
category, packaging evidence and business activity.

PROCESS IS NOT PACKAGING

The words "canned", "cannery", "conserve", "conserves", "conserven",
"conservas", "preserved", "preserving", "appertised" and "shelf-stable"
describe a PRESERVATION PROCESS, not a container. Companies described this way
routinely sell the same product in both metal cans and glass jars, and glass is
the premium format in this category.

Never return "No" merely because a product is called "canned" or because the
company is called a cannery or conserves producer. Treat these companies as
"Yes" unless the snippets explicitly show that every relevant line is sold only
in metal cans, cartons or pouches.

The same applies to frozen ranges: a company selling both frozen and preserved
products still qualifies through its preserved range.

Set "GlassFit" to "No" when:

- The company has no relevant food or beverage products suitable for glass
- The company's ENTIRE relevant range is a format that is never glass, such as
  exclusively frozen foods, raw meat, poultry, fresh seafood, fresh bakery,
  fresh produce, crisps, dry snacks or bulk grains.
  A company that also has a preserved, jarred, bottled or canned line does not
  belong here — classify it as "Yes".
- The company only resells finished products made and packaged by other
  companies and has no own-brand, filling, packing or packaging purchasing role
- The company is itself a RETAILER: a supermarket, hypermarket, discount
  chain, grocery store, convenience chain, food e-commerce site or retail
  banner, AND the website confirms a retail operation.
  This applies even when the retailer sells private-label products, because
  the glass is bought by the co-packer that fills them, not by the chain.
  IMPORTANT EXCEPTION: if the Company Name mentions a retailer or a private
  label but the WEBSITE belongs to a food or beverage PRODUCER, the producer
  is the real subject of this row. Classify the producer, not the retailer.
- The company mainly sells machinery, logistics, packaging materials or services
- The company manufactures empty glass bottles, jars or glass packaging itself
- The company operates mainly in cosmetics, personal care, pharmaceuticals,
  chemicals, cleaning products, candles or other non-food and non-beverage
  categories, unless additional instructions explicitly allow them

GLASS PRESENCE RULE

The presence of any commercially relevant product sold in a glass bottle or
glass jar is enough to return "GlassFit": "Yes".

This remains true even when:

- Most of the portfolio uses PET
- Most products use cans, cartons, pouches or plastic
- Glass represents only a small part of the product range
- Only selected premium, export, artisanal or specialty products use glass

Do not classify a company as "No" merely because PET or another packaging format
is dominant.

Return "No" because of packaging format only when the available evidence clearly
shows that the entire relevant portfolio is exclusively non-glass and there is
no glass product line or realistic glass packaging need.

DISTRIBUTOR AND BRAND OPERATOR RULE

Do not classify a company as "No" only because it is described as a distributor,
importer or wholesaler.

Return "GlassFit": "Yes" if the company also does any of the following:

- Owns or operates food or beverage brands
- Purchases empty packaging for its own brands
- Fills, bottles, packs or processes products
- Uses third-party contract fillers while controlling packaging decisions
- Offers private-label or own-brand products
- Has its own production, bottling, filling or packing facility
- Shows evidence of its own products sold in glass bottles or jars

Return "GlassFit": "No" only when the company appears to resell finished,
already-packaged products from other manufacturers and has no evidence of
own-brand activity, filling, packing or packaging purchasing responsibility.

RETAIL EXCEPTION: this rule does NOT rescue retailers. A supermarket or
grocery chain stays "No" even when it has a strong private-label range.

GLASS MANUFACTURER OVERRIDE

A company that manufactures empty glass bottles, glass jars or glass packaging
must always be classified as:

- "GlassFit": "No"

Such companies are competitors, suppliers or industry peers rather than buyers.

ACTUAL PACKAGING EVIDENCE

Actual packaging evidence is important, but it must be interpreted at portfolio
level.

Examples of non-glass packaging evidence include:

- Aluminum cans
- PET bottles
- Flexible pouches
- Cartons or Tetra Pak
- Plastic tubs or cups
- Bag-in-box

These formats do not automatically mean "No".

If any meaningful product line uses glass bottles or jars, return "Yes".

Only return "No" when the snippets clearly show that the full relevant product
portfolio is exclusively non-glass and there is no realistic glass opportunity.

COMPANY TYPE

Choose exactly one:

Manufacturer:
The company produces, processes, fills, bottles, brews, distills, ferments,
preserves or packs its own products.

Wineries, distilleries, breweries, food processors, farms producing finished
products and bottling companies should default to "Manufacturer" unless the
snippets explicitly state otherwise.

Co-packer:
Use only when the snippets explicitly mention contract manufacturing, contract
filling, co-packing, co-manufacturing or private-label production for other
brands.

Brand Owner:
Use when the company owns or markets its own brands but outsources manufacturing,
filling, bottling or packing.

Do not require the word "outsourced" if the snippets clearly show a brand-led
business with third-party manufacturing, but do not assume outsourcing only
because factory information is absent.

Distributor:
Use when importing, wholesaling or distribution is the company's main business,
even if it also owns brands or arranges filling.

A company may therefore be:

- "GlassFit": "Yes"
- "CompanyType": "Distributor"

when it distributes products but also controls own-brand packaging, filling or
glass-packaged product lines.

Unknown:
Use only when the business activity cannot reasonably be determined.

COMPANY TYPE AND GLASS FIT ARE SEPARATE

Do not force "GlassFit": "No" only because the CompanyType is "Distributor" or
"Brand Owner".

Evaluate:

1. Whether the company is connected to relevant glass-packaged food or beverage
   products
2. Whether it may purchase, specify, source or control empty glass packaging
3. Whether it owns, fills, packs or manages brands using glass

WEBSITE IS THE PRIMARY IDENTITY

When a Website is supplied, it is the strongest evidence of what this row
actually is, and it outranks the Company Name field.

Company Name values are often messy: they may contain a retail banner, a
private-label brand, a parent group, a holding company or a brand that is
merely a customer of the real business. The website points at one specific
operating company.

Therefore:

- If the website is a producer's own site (its own products, catalogue,
  factory, farm, distillery, winery or cannery), classify that producer, even
  when the Company Name suggests a supermarket, a brand or a holding.
- Only classify the row as a retailer when the WEBSITE itself is a retail site.
- Read the domain and the URL path as evidence: agrucapers, conservas,
  conserven, distillery, winery, oleificio, cantina, /jams, /sauces,
  /preserves, /our-products all indicate a producer.

DECISION PRIORITY

Apply the rules in this order:

1. Check whether the company manufactures glass packaging itself
2. Check whether any meaningful glass bottle or jar product exists
3. Check whether the company manufactures, fills, packs, owns or controls the
   packaging of relevant products
4. Identify the main product categories
5. Evaluate whether those products are suitable for glass bottles or jars
6. Determine the CompanyType separately from GlassFit
7. Write a short note based on the strongest evidence

When information is limited:

- Weak or empty search snippets are NOT evidence of a bad fit. Missing
  information must never by itself produce "GlassFit": "No".
- In that case, read the COMPANY NAME and the WEBSITE URL as evidence. Words
  in the name or domain such as conserve/conserves/conserven/conservas,
  preserved, preserves, jam, marmalade, confiture, sauce, pesto, olio, oil,
  miel/honey, pickle, antipasti, distillery, winery, brewery, bodega, cantina,
  juice, beverage, drinks or a URL path like /jams, /sauces, /preserves,
  /products/fruitpuree indicate a relevant food or beverage producer -> "Yes".
- Use the most likely conclusion from the snippets and product logic
- Do not invent unsupported products, facilities or packaging formats
- If the company clearly has suitable products, default to "Yes"
- If glass use is shown anywhere in a meaningful product line, return "Yes"
- If the company sells its own suitable products and outsourcing is not stated,
  default to "Manufacturer"
- Use "Unknown" only when the business activity truly cannot be determined,
  and never as a substitute for reading the company name and website

AINOTE

Write exactly one short English sentence.

The sentence must:

- Mention the product category or business activity behind the decision
- Explain why the company fits or does not fit glass packaging
- Avoid vague wording
- Avoid unsupported claims
- Remain concise

Good examples:

- "The company produces sauces and pickled vegetables that are suitable for glass jars."
- "The distillery produces whiskey and gin, making it a likely buyer of glass bottles."
- "The distributor operates own-brand beverages in glass and may control packaging purchases."
- "The company mainly uses PET but also sells a relevant glass-bottled product line."
- "The company only resells finished beverages and shows no packaging purchasing role."
- "The company manufactures glass bottles and is therefore a competitor rather than a buyer."
- "The company is a supermarket chain and does not purchase empty glass packaging itself."
- "The website belongs to a caper and pickled vegetable producer that packs in glass jars, despite the retail brand in the name."
- "The company cans vegetables and pulses, a category routinely sold in glass jars."
- "The company name and website indicate a preserved fruit and vegetable producer suited to glass jars."

{additional_instructions}

COMPANY INFORMATION

Company Name: "{company}"
Country: "{country}"
Website: "{website}"

Search Snippets:
{snippets}

OUTPUT

Return only one valid JSON object with exactly these keys:

{{
  "GlassFit": "Yes",
  "CompanyType": "Manufacturer",
  "AINote": "One short English sentence explaining the decision."
}}

Allowed values:

- "GlassFit": exactly "Yes" or "No"
- "CompanyType": exactly "Manufacturer", "Co-packer", "Brand Owner",
  "Distributor" or "Unknown"

Do not return markdown.
Do not return a code block.
Do not include any text outside the JSON.
Do not add extra JSON keys.
All output text must be in English.
"""

        verdict = self.llm.json_call(
            system=(
                "You are a precise B2B sales analyst for an empty glass packaging "
                "manufacturer. Apply the rules exactly and return strict valid JSON only."
            ),
            user=prompt,
            max_tokens=150,
        )
        time.sleep(self.sleep)

        result = {
            "is_fit":       verdict.get("GlassFit", "Unknown"),
            "company_type": verdict.get("CompanyType", "Unknown"),
            "ai_note":      verdict.get("AINote", ""),
        }
        # Only cache real verdicts — never cache an API failure.
        if result["is_fit"] in ("Yes", "No"):
            cache.set(ckey, json.dumps(result))
        return result
