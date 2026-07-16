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
    """POST to Lusha search; retries on 429; prints status on errors."""
    for attempt in range(3):
        resp = requests.post(SEARCH_URL, json=payload, headers=headers, timeout=20)
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


def _search_by_domain(headers: dict, domain: str, keywords: list) -> tuple[list, str]:
    """Strategy 1: companies.include.domains filter (most precise)."""
    payload = {
        "filters": {
            "contacts": {"include": {"jobTitles": keywords}},
            "companies": {"include": {"domains": [domain]}},
        },
        "pages": {"page": 0, "size": 10},
    }
    results = _run_payload(headers, "domain_filter", payload)
    if results:
        return results, "domain_filter"

    # Strategy 2: domain as searchText (fallback)
    payload2 = {
        "filters": {
            "contacts": {"include": {"jobTitles": keywords, "searchText": domain}}
        },
        "pages": {"page": 0, "size": 10},
    }
    results2 = _run_payload(headers, "domain_searchtext", payload2)
    return (results2, "domain_searchtext") if results2 else ([], "no_result")


def _search_by_name(headers: dict, company: str, keywords: list) -> tuple[list, str]:
    """Strategy 3: company name search (used only when domain yields nothing)."""
    payload = {
        "filters": {
            "contacts": {"include": {"jobTitles": keywords, "companies": [{"names": [company]}]}}
        },
        "pages": {"page": 0, "size": 10},
    }
    results = _run_payload(headers, "name_filter", payload)
    return (results, "name_filter") if results else ([], "no_result")


def _enrich(headers: dict, person_id: str) -> tuple[str, str, bool]:
    """Returns (email, linkedin, credit_charged)."""
    if not person_id:
        return "-", "-", False
    params = {"personId": person_id, "revealPhones": "false", "revealEmails": "true"}
    for _ in range(3):
        resp = requests.get(ENRICH_URL, headers=headers, params=params, timeout=20)
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


# ── main class ────────────────────────────────────────────────────────────────

class ContactFinder:
    def __init__(self, tiers: list[dict], settings: dict):
        self.tiers = tiers
        self.headers = {
            "accept": "application/json",
            "content-type": "application/json",
            "api_key": os.environ["LUSHA_API_KEY"],
        }
        rl = settings["rate_limits"]
        self.sleep_company  = rl["sleep_between_companies"]
        self.max_contacts   = settings["pipeline"]["max_contacts_per_company"]

    def find(self, company: str, website: str = "") -> list[dict]:
        """Returns list of contact dicts; empty list if nothing found."""
        domain = clean_domain(website) or ""
        found: list[dict] = []
        seen_ids: set[str] = set()
        top_company_name: str | None = None

        for tier in self.tiers:
            keywords = tier["keywords"]
            tier_name = tier["name"]
            print(f"  🔎 {tier_name}")

            # ── pick search strategy ──────────────────────────────────────
            if domain:
                people, method = _search_by_domain(self.headers, domain, keywords)
            else:
                people, method = [], "no_result"

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

            added = 0
            for person in people:
                if added >= self.max_contacts:
                    break

                pid = _get_person_id(person)
                if not pid or pid in seen_ids:
                    continue
                seen_ids.add(pid)

                lusha_company = _get_company_name(person)

                # ── company match guard (name-search only really needs this) ─
                if (method == "name_filter"
                        and top_company_name and top_company_name != "-"
                        and lusha_company != "-"):
                    if not _company_match(top_company_name, lusha_company):
                        print(f"    ⛔ Rejected (company mismatch): "
                              f"{top_company_name} ≠ {lusha_company}")
                        continue

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
