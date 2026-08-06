"""Stage 3 — decision-maker discovery (Lusha Prospecting API).

Birebir orijinal notebook mantığı — 3 arama stratejisi:

1. DOMAIN FILTER  → companies.include.domains ile arar (en kesin)
2. SEARCH TEXT    → domain'i searchText olarak arar (fallback 1)
3. COMPANY NAME   → şirket adıyla arar + company_name_match filtresi (fallback 2)

Domain araması sonuç verirse isim araması hiç yapılmaz.
İsim aramasında ltd/inc/corp/llc gibi ekler normalize edilir,
şirket adından herhangi bir token diğerinde geçiyorsa eşleşme kabul edilir,
yoksa SequenceMatcher >= 0.72 şartı aranır.
"""
from __future__ import annotations

import os
import re
import time
from difflib import SequenceMatcher

import requests

from .utils import clean_domain
from . import status

SEARCH_URL = "https://api.lusha.com/prospecting/contact/search"
ENRICH_URL = "https://api.lusha.com/v2/person"

SLEEP_BETWEEN_TIERS  = 1.0
SLEEP_BEFORE_ENRICH  = 1.0


# ── helpers ──────────────────────────────────────────────────────────────────

def _safe(val) -> str:
    if val is None:
        return ""
    try:
        import pandas as pd
        if pd.isna(val):
            return ""
    except Exception:
        pass
    return str(val).strip()


def _smart_wait(response_text: str) -> None:
    """Reads Lusha's 'Reset in N seconds' hint; falls back to 20s."""
    match = re.search(r"Reset in (\d+) seconds", response_text or "")
    wait = int(match.group(1)) + 10 if match else 20
    print(f"    ⏳ Rate limited — waiting {wait}s")
    time.sleep(wait)


def _normalize(text: str) -> str:
    """Strip legal suffixes and punctuation for name comparison."""
    REMOVE = {"llc","inc","co","company","corp","corporation",
               "ltd","limited","the","and","group","holding","holdings"}
    text = _safe(text).lower()
    text = text.replace("&", " and ")
    text = re.sub(r"[^a-z0-9 ]", " ", text)
    words = [w for w in text.split() if w and w not in REMOVE]
    return " ".join(words)


def _company_match(a: str, b: str) -> bool:
    """True if the two company names refer to the same company.

    Matches if:
    - either normalized name is a substring of the other, OR
    - SequenceMatcher ratio >= 0.72
    Unreadable names are treated as match (don't drop the contact).
    """
    a_n = _normalize(a)
    b_n = _normalize(b)
    if not a_n or not b_n:
        return True
    if a_n == b_n or a_n in b_n or b_n in a_n:
        return True
    return SequenceMatcher(None, a_n, b_n).ratio() >= 0.72


# ── field extractors (mirrors every key Lusha has ever used) ──────────────────

def _get_domain(person: dict) -> str:
    raw = (person.get("fqdn") or person.get("companyDomain")
           or person.get("company_domain") or person.get("domain")
           or person.get("website") or person.get("companyWebsite")
           or person.get("companyUrl") or person.get("url"))
    if raw:
        return clean_domain(raw) or "-"
    co = person.get("company") or {}
    raw = (co.get("fqdn") or co.get("website") or co.get("domain")
           or co.get("companyDomain") or co.get("url") or co.get("companyUrl"))
    return (clean_domain(raw) or "-") if raw else "-"


def _get_company_name(person: dict) -> str:
    val = (person.get("companyName") or person.get("company_name")
           or person.get("organizationName") or person.get("organization_name"))
    if val:
        return val
    co = person.get("company") or {}
    return (co.get("name") or co.get("companyName") or co.get("displayName") or "-")


def _get_person_id(person: dict) -> str:
    return (person.get("personId") or person.get("person_id")
            or person.get("id") or person.get("contactId")
            or person.get("contact_id") or person.get("lushaId") or "")


def _get_name(person: dict) -> str:
    first = person.get("firstName") or person.get("first_name") or ""
    last  = person.get("lastName")  or person.get("last_name")  or ""
    if first or last:
        return f"{first} {last}".strip()
    return (person.get("name") or person.get("fullName")
            or person.get("full_name") or "Unknown")


def _get_title(person: dict) -> str:
    val = person.get("jobTitle")
    if isinstance(val, dict):
        return val.get("title") or "-"
    return (val or person.get("title") or person.get("job_title")
            or person.get("position") or "-")


def _get_linkedin(person: dict) -> str:
    return (person.get("linkedinUrl") or person.get("linkedin_url")
            or person.get("linkedin") or person.get("linkedInUrl") or "-")


def _extract_results(data: dict) -> list:
    inner = data.get("data", [])
    if isinstance(inner, dict):
        return inner.get("results", [])
    if isinstance(inner, list):
        return inner
    return []


# ── Lusha API calls ───────────────────────────────────────────────────────────

def _run_payload(headers: dict, name: str, payload: dict) -> list:
    """POST to Lusha search; retries on 429 AND on network errors."""
    for attempt in range(3):
        try:
            resp = requests.post(SEARCH_URL, json=payload, headers=headers, timeout=20)
        except requests.RequestException as e:
            # Timeout / dropped connection / DNS blip: don't crash the whole
            # run — wait briefly and retry, then give up on THIS search only.
            print(f"    🌐 network error ({name}), retry {attempt+1}/3: {e}")
            time.sleep(5)
            continue
        if resp.ok:
            results = _extract_results(resp.json())
            if results:
                print(f"    ✅ {name} → {len(results)} contacts")
            return results
        if resp.status_code == 429:
            _smart_wait(resp.text)
            continue
        if resp.status_code in (401, 402, 403):
            status.warn(status.classify_api_error("Lusha", resp.status_code, resp.text))
            return []
        print(f"    ⚠️ {name} failed {resp.status_code}: {resp.text[:200]}")
        return []
    return []


def _search_by_domain(headers: dict, domain: str, keywords: list,
                      page: int = 0, size: int = 10) -> tuple[list, str]:
    """Strategy 1: companies.include.domains filter (most precise)."""
    payload = {
        "filters": {
            "contacts": {"include": {"jobTitles": keywords}},
            "companies": {"include": {"domains": [domain]}},
        },
        "pages": {"page": page, "size": size},
    }
    results = _run_payload(headers, "domain_filter", payload)
    if results:
        return results, "domain_filter"

    # Strategy 2: domain as searchText (fallback)
    payload2 = {
        "filters": {
            "contacts": {"include": {"jobTitles": keywords, "searchText": domain}}
        },
        "pages": {"page": page, "size": size},
    }
    results2 = _run_payload(headers, "domain_searchtext", payload2)
    return (results2, "domain_searchtext") if results2 else ([], "no_result")


def _search_by_name(headers: dict, company: str, keywords: list,
                    page: int = 0, size: int = 10) -> tuple[list, str]:
    """Strategy 3: company name search (used only when domain yields nothing)."""
    payload = {
        "filters": {
            "contacts": {"include": {"jobTitles": keywords, "companies": [{"names": [company]}]}}
        },
        "pages": {"page": page, "size": size},
    }
    results = _run_payload(headers, "name_filter", payload)
    return (results, "name_filter") if results else ([], "no_result")


def _enrich(headers: dict, person_id: str) -> tuple[str, str, bool]:
    """Returns (email, linkedin, credit_charged)."""
    if not person_id:
        return "-", "-", False
    params = {"personId": person_id, "revealPhones": "false", "revealEmails": "true"}
    for _ in range(3):
        try:
            resp = requests.get(ENRICH_URL, headers=headers, params=params, timeout=20)
        except requests.RequestException as e:
            print(f"    🌐 network error (enrich), retrying: {e}")
            time.sleep(5)
            continue
        if resp.ok:
            data = resp.json()
            contact = data.get("contact") or {}
            if contact.get("error"):
                return "-", "-", False
            contact_data = contact.get("data") or {}
            emails = contact_data.get("emailAddresses") or []
            email = emails[0].get("email", "-") if emails else "-"
            social = contact_data.get("socialLinks") or {}
            linkedin = social.get("linkedin") or social.get("linkedinUrl") or "-"
            return email, linkedin, contact.get("isCreditCharged", False)
        if resp.status_code == 429:
            _smart_wait(resp.text)
            continue
        if resp.status_code in (401, 402, 403):
            status.warn(status.classify_api_error("Lusha (enrich)", resp.status_code, resp.text))
            return "-", "-", False
        print(f"    ⚠️ Enrich error {resp.status_code}: {resp.text[:200]}")
        return "-", "-", False
    return "-", "-", False


def domain_variants(domain: str, country: str, tld_map: dict) -> list[str]:
    """Ordered list of domains to try on Lusha before name search.

    gaiawines.gr + Greece  -> [gaiawines.gr, gaiawines.com]
    acme.com     + Germany -> [acme.com, acme.de]
    Searches are free on Lusha; only enrich burns credits.
    """
    from .utils import domain_base
    domain = clean_domain(domain)
    if not domain:
        return []
    base = domain_base(domain)
    variants = [domain]

    def add(tld: str):
        cand = f"{base}.{tld}"
        if cand not in variants:
            variants.append(cand)

    add("com")
    country_tld = tld_map.get(str(country or "").strip().lower())
    if country_tld:
        add(country_tld)
    return variants


# ── main class ────────────────────────────────────────────────────────────────

def _rank_people_by_fit(llm, people: list[dict]) -> list[dict]:
    """Order candidates by how relevant their title is to buying GLASS packaging.

    The tier keywords already restrict *which* people come back, but a single
    tier still mixes very different buyers: a "Packaging Buyer" and an
    "IT Procurement Manager" both match Tier 1 on the word "procurement", yet
    only one of them ever purchases glass. Keyword rules cannot tell them
    apart; the model can.

    The model only REORDERS the list it is given — it never invents, drops or
    judges anyone as unsuitable. If the model is unavailable or errors, the
    original Lusha order is returned unchanged, so the pipeline never breaks.
    """
    if llm is None or len(people) <= 1:
        return people

    titles = []
    for idx, person in enumerate(people):
        titles.append(f"{idx}: {_get_title(person)}")

    prompt = (
        "You rank sales contacts for a company that sells EMPTY GLASS bottles "
        "and jars to food and beverage producers. Given the numbered job "
        "titles below, order them from MOST to LEAST likely to be the person "
        "who decides on or influences the purchase of glass packaging / "
        "bottles / jars / raw packaging materials.\n\n"
        "Rank higher: packaging buyers, procurement/purchasing for materials, "
        "sourcing, supply chain, operations, production, and — for small "
        "producers — owners or founders who run purchasing themselves.\n"
        "Rank lower: roles that buy unrelated things (IT, media, marketing, "
        "HR, facilities, real estate, travel) and purely financial or "
        "administrative roles.\n\n"
        "Titles:\n" + "\n".join(titles) + "\n\n"
        'Return ONLY strict JSON: {"order": [list of the indices, best first]}. '
        "Include every index exactly once."
    )
    try:
        res = llm.json_call(
            system="You are a precise B2B sales analyst. Return strict JSON only.",
            user=prompt,
            max_tokens=300,
        )
        order = res.get("order", [])
        seen, ranked = set(), []
        for i in order:
            if isinstance(i, int) and 0 <= i < len(people) and i not in seen:
                seen.add(i)
                ranked.append(people[i])
        # Append anyone the model forgot, preserving original order.
        for idx, person in enumerate(people):
            if idx not in seen:
                ranked.append(person)
        return ranked if ranked else people
    except Exception:
        return people


class ContactFinder:
    def __init__(self, tiers: list[dict], settings: dict, llm=None,
                 qualifier=None):
        self.tiers = tiers
        self.llm = llm
        # Used to verify companies found by NAME search (not domain search).
        # A name search can return a same-named but unrelated business, so the
        # company Lusha returned is re-checked against the ICP using its own
        # domain before any credit is spent on revealing an email.
        self.qualifier = qualifier
        self._verify_cache: dict[str, bool] = {}
        self.headers = {
            "accept": "application/json",
            "content-type": "application/json",
            "api_key": os.environ["LUSHA_API_KEY"],
        }
        rl = settings["rate_limits"]
        self.sleep_company  = rl["sleep_between_companies"]
        self.max_contacts   = settings["pipeline"]["max_contacts_per_company"]
        self.country_tlds   = {str(k).lower(): str(v)
                               for k, v in (settings.get("country_tlds") or {}).items()}

    def _verify_name_match(self, wanted_company: str, person: dict,
                           country: str = "") -> bool:
        """Is the company Lusha returned really the company we asked for?

        Only used for NAME searches. Runs the Stage-2 qualifier against the
        DOMAIN Lusha returned: the model sees that company's real products and
        decides whether it fits the ICP. This replaces fuzzy string matching,
        which cannot tell "Hero España" (jams) from "Hero MotoCorp"
        (motorcycles) — the names are nearly identical, the businesses are not.

        Costs no Lusha credits: the check is a Serper search plus one GPT call.
        Cached per domain so repeated tiers don't re-ask.
        """
        if self.qualifier is None:
            return True                      # no qualifier wired: don't block

        lusha_domain = _get_domain(person)
        if not lusha_domain or lusha_domain == "-":
            return True                      # nothing to verify against

        if lusha_domain in self._verify_cache:
            return self._verify_cache[lusha_domain]

        lusha_company = _get_company_name(person)
        try:
            verdict = self.qualifier.qualify(
                lusha_company if lusha_company != "-" else wanted_company,
                country, lusha_domain)
            ok = str(verdict.get("is_fit", "")).strip().lower() == "yes"
            note = str(verdict.get("ai_note", ""))[:70]
        except Exception as exc:
            print(f"    ⚠️ Doğrulama yapılamadı ({exc}) — kişi alınıyor")
            ok = True
            note = ""

        self._verify_cache[lusha_domain] = ok
        if ok:
            print(f"    ✅ Doğrulandı: {lusha_domain} — {note}")
        else:
            print(f"    ⛔ ICP dışı, şirket atlanıyor: {lusha_domain} — {note}")
        return ok

    def find_bulk(self, company: str, website: str = "", country: str = "",
                  target: int = 10, max_pages: int = 5) -> list[dict]:
        """Keep searching until `target` contacts with an email are collected.

        Same matching rules as find(): domain variants first, name search as a
        fallback, the same company-match guard. The difference is that find()
        stops at the first tier that produces anything, because it only wants a
        couple of decision-makers. Here we walk every tier and page on through
        the result set until the quota is met.

        Cost warning: Lusha searches are free but each email reveal burns a
        credit, so a target of N costs up to N credits per company.
        """
        domain = clean_domain(website) or ""
        variants = domain_variants(domain, country, self.country_tlds)
        found: list[dict] = []
        seen_ids: set[str] = set()
        top_company_name: str | None = None
        print(f"  📥 Bulk search — target {target} contacts")

        for tier in self.tiers:
            if len(found) >= target:
                break
            keywords = tier["keywords"]
            print(f"  🔎 {tier['name']}  ({len(found)}/{target})")

            for page in range(max_pages):
                if len(found) >= target:
                    break

                people, method = [], "no_result"
                for var in variants:
                    people, method = _search_by_domain(
                        self.headers, var, keywords, page=page, size=20)
                    if people:
                        if var != domain:
                            method = f"domain_variant:{var}"
                        break

                if not people and page == 0:
                    people, method = _search_by_name(
                        self.headers, company, keywords, page=page, size=20)

                if not people:
                    break            # no more results in this tier

                if top_company_name is None:
                    top_company_name = _get_company_name(people[0])
                    print(f"    🎯 Anchored to: {top_company_name}")

                people = _rank_people_by_fit(self.llm, people)

                # NAME search: verify the company BEFORE spending any credit.
                # Tier keywords already filtered the titles, so reaching here
                # means a relevant person exists — now check the company fits.
                if method == "name_filter":
                    if not self._verify_name_match(company, people[0], country):
                        break            # wrong / non-ICP company: skip it
                    method_note = "name_filter_verified"
                else:
                    method_note = method

                for person in people:
                    if len(found) >= target:
                        break
                    pid = _get_person_id(person)
                    if not pid or pid in seen_ids:
                        continue
                    seen_ids.add(pid)

                    lusha_company = _get_company_name(person)

                    name  = _get_name(person)
                    title = _get_title(person)
                    linkedin_search = _get_linkedin(person)

                    time.sleep(SLEEP_BEFORE_ENRICH)
                    email, linkedin_enrich, charged = _enrich(self.headers, pid)
                    linkedin = linkedin_enrich if linkedin_enrich != "-" else linkedin_search

                    if email and email != "-":
                        print(f"    🎯 {len(found)+1}/{target}  {email}")
                        found.append({
                            "name": name, "title": title, "email": email,
                            "linkedin": linkedin, "tier": tier["name"],
                            "match_method": method_note,
                            "lusha_company": lusha_company,
                            "lusha_domain": _get_domain(person),
                            "credit_charged": charged,
                        })

            if len(found) < target:
                time.sleep(SLEEP_BETWEEN_TIERS)

        print(f"  ✅ Bulk result: {len(found)}/{target}")
        time.sleep(self.sleep_company)
        return found

    def find(self, company: str, website: str = "",
             country: str = "") -> list[dict]:
        """Returns list of contact dicts; empty list if nothing found."""
        domain = clean_domain(website) or ""
        variants = domain_variants(domain, country, self.country_tlds)
        found: list[dict] = []
        seen_ids: set[str] = set()
        top_company_name: str | None = None

        for tier in self.tiers:
            keywords = tier["keywords"]
            tier_name = tier["name"]
            print(f"  🔎 {tier_name}")

            # ── pick search strategy: domain variants first ───────────────
            people, method = [], "no_result"
            for var in variants:
                people, method = _search_by_domain(self.headers, var, keywords)
                if people:
                    if var != domain:
                        method = f"domain_variant:{var}"
                        print(f"    🔀 TLD variant hit: {var}")
                    break

            if not people:
                print(f"    ↩️ Domain search empty — trying name search for '{company}'")
                people, method = _search_by_name(self.headers, company, keywords)

            if not people:
                print("    ❌ No contacts found.")
                time.sleep(SLEEP_BETWEEN_TIERS)
                continue

            # ── lock onto the first company returned ──────────────────────
            if top_company_name is None:
                top_company_name = _get_company_name(people[0])
                print(f"    🎯 Anchored to: {top_company_name}")

            # Reorder this tier's candidates so the best-fit title is tried
            # first — the glass buyer ahead of the IT buyer. Falls back to the
            # original Lusha order when no model is available.
            people = _rank_people_by_fit(self.llm, people)

            # NAME search: verify the company BEFORE spending a credit.
            # Domain search needs no check — Lusha matched the exact domain.
            if method == "name_filter":
                if not self._verify_name_match(company, people[0], country):
                    print("    ⏭️ Şirket doğrulanamadı — atlanıyor")
                    time.sleep(SLEEP_BETWEEN_TIERS)
                    continue
                method = "name_filter_verified"

            added = 0
            for person in people:
                if added >= self.max_contacts:
                    break

                pid = _get_person_id(person)
                if not pid or pid in seen_ids:
                    continue
                seen_ids.add(pid)

                lusha_company = _get_company_name(person)

                name   = _get_name(person)
                title  = _get_title(person)
                linkedin_search = _get_linkedin(person)
                print(f"    👤 {name} | {title} | {lusha_company} | id:{pid}")

                time.sleep(SLEEP_BEFORE_ENRICH)
                email, linkedin_enrich, charged = _enrich(self.headers, pid)
                linkedin = linkedin_enrich if linkedin_enrich != "-" else linkedin_search

                if email and email != "-":
                    print(f"    🎯 Email found: {email}")
                    found.append({
                        "name":         name,
                        "title":        title,
                        "email":        email,
                        "linkedin":     linkedin,
                        "tier":         tier_name,
                        "match_method": method,
                        "lusha_company":lusha_company,
                        "lusha_domain": _get_domain(person),
                        "credit_charged": charged,
                    })
                    added += 1
                else:
                    print(f"    ⚠️ No email: {name} | {title}")

            if found:
                break  # tier that produced results — don't burn credits on lower tiers

            print("    ⚠️ No emails in this tier — trying next tier")
            time.sleep(SLEEP_BETWEEN_TIERS)

        time.sleep(self.sleep_company)
        return found
