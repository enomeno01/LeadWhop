"""Stage 3 — decision-maker discovery (Lusha Prospecting API).

Two-level cascade, mirroring how a careful human researcher works:

1. DOMAIN search (exact, safest) — tiered titles, Tier 1 first.
2. If the domain yields nothing (or there is no domain): COMPANY-NAME
   search, but a name-based hit is only accepted when it passes a fuzzy
   guard: the Lusha record's company name must be similar enough to ours
   (>= fuzzy_threshold) OR its domain must be a TLD variant of ours
   (acme.de vs acme.com). Anything below the bar is dropped and the row
   is flagged instead of silently importing a stranger's employees.

Tier ordering is the main cost lever — enrichment credits are never spent
on a Tier 2 contact when a Tier 1 contact exists.
"""
from __future__ import annotations

import os
import time

from .utils import clean_domain, domain_base, request_with_backoff, similarity

SEARCH_URL = "https://api.lusha.com/prospecting/contact/search"
ENRICH_URL = "https://api.lusha.com/v2/person"


class ContactFinder:
    def __init__(self, tiers: list[dict], settings: dict):
        self.tiers = tiers
        self.headers = {
            "accept": "application/json",
            "content-type": "application/json",
            "api_key": os.environ["LUSHA_API_KEY"],
        }
        rl = settings["rate_limits"]
        self.sleep_company = rl["sleep_between_companies"]
        self.max_retries = rl["max_retries"]
        self.max_contacts = settings["pipeline"]["max_contacts_per_company"]
        self.fuzzy_threshold = settings.get("verification", {}).get(
            "fuzzy_threshold", 0.6)

    # ---- Lusha calls -------------------------------------------------------

    def _search(self, company_filter: dict, keywords: list[str]) -> list[dict]:
        payload = {
            "pages": {"page": 0, "size": self.max_contacts * 3},
            "filters": {"contacts": {"include": {
                "jobTitles": keywords, "companies": [company_filter]}}},
        }
        resp = request_with_backoff("POST", SEARCH_URL, headers=self.headers,
                                    json=payload, max_retries=self.max_retries)
        if resp is None or not resp.ok:
            return []
        return resp.json().get("contacts", []) or []

    def _enrich(self, contact_id: str) -> dict:
        resp = request_with_backoff(
            "GET", ENRICH_URL, headers=self.headers,
            params={"contactId": contact_id, "revealEmails": "true",
                    "revealPhones": "false"},
            max_retries=self.max_retries,
        )
        if resp is None or not resp.ok:
            return {}
        return resp.json()

    # ---- fuzzy guard for name-based hits ------------------------------------

    def _passes_fuzzy_guard(self, contact: dict, company: str,
                            our_domain: str) -> tuple[bool, float, str]:
        """(accepted, score, lusha_domain) for a name-based search hit."""
        lusha_name = (contact.get("companyName")
                      or contact.get("company", {}).get("name", "") or "")
        lusha_domain = clean_domain(
            contact.get("fqdn") or contact.get("companyWebsite")
            or contact.get("company", {}).get("website", "") or "")
        score = similarity(lusha_name, company)
        tld_variant = (bool(our_domain) and bool(lusha_domain)
                       and domain_base(lusha_domain) == domain_base(our_domain))
        return (score >= self.fuzzy_threshold or tld_variant), score, lusha_domain

    # ---- search strategies ---------------------------------------------------

    def _tiered(self, company_filter: dict, validate=None) -> list[dict]:
        found: list[dict] = []
        for tier in self.tiers:
            for contact in self._search(company_filter, tier["keywords"]):
                extra: dict = {}
                if validate:
                    ok, score, lusha_domain = validate(contact)
                    if not ok:
                        continue
                    extra = {"match_score": round(score, 2),
                             "lusha_domain": lusha_domain}
                data = self._enrich(contact.get("id", ""))
                person = data.get("contact", {}).get("data", {}) or data.get("data", {})
                emails = [e.get("email") for e in person.get("emailAddresses", [])
                          if e.get("email")]
                if not emails:
                    continue
                found.append({
                    "name": person.get("fullName", contact.get("name", "")),
                    "title": person.get("jobTitle", contact.get("jobTitle", "")),
                    "email": emails[0],
                    "tier": tier["name"],
                    **extra,
                })
                if len(found) >= self.max_contacts:
                    return found
            if found:  # a higher tier delivered — don't burn credits on lower tiers
                return found
            time.sleep(1.0)
        return found

    # ---- main ------------------------------------------------------------

    def find(self, company: str, website: str = "") -> list[dict]:
        """Up to max_contacts dicts:
        {name, title, email, tier, match_method, match_score?, lusha_domain?}
        """
        domain = clean_domain(website)
        contacts: list[dict] = []

        # 1) exact domain search
        if domain:
            contacts = self._tiered({"domains": [domain]})
            for c in contacts:
                c["match_method"] = "domain"

        # 2) fallback: company-name search behind the fuzzy guard
        if not contacts:
            guard = lambda contact: self._passes_fuzzy_guard(contact, company, domain)
            contacts = self._tiered({"names": [company]}, validate=guard)
            for c in contacts:
                c["match_method"] = "company_name"

        time.sleep(self.sleep_company)
        return contacts
