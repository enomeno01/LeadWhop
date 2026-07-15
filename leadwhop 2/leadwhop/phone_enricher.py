"""Stage 4 (optional) — add phone numbers to existing leads.

Looks a person up by email when available (exact match, cheapest path),
falling back to name + company domain. Emails are not re-revealed, so no
credits are wasted on data you already have.
"""
from __future__ import annotations

import os

from .utils import clean_domain, request_with_backoff

ENRICH_URL = "https://api.lusha.com/v2/person"


class PhoneEnricher:
    def __init__(self, settings: dict):
        self.headers = {"accept": "application/json",
                        "api_key": os.environ["LUSHA_API_KEY"]}
        self.max_retries = settings["rate_limits"]["max_retries"]

    def find_phones(self, email: str = "", first_name: str = "",
                    last_name: str = "", website: str = "") -> str:
        params: dict = {"revealPhones": "true", "revealEmails": "false"}
        if email and "@" in email:
            params["email"] = email
        elif first_name:
            params.update({"firstName": first_name, "lastName": last_name,
                           "domain": clean_domain(website)})
        else:
            return ""

        resp = request_with_backoff("GET", ENRICH_URL, headers=self.headers,
                                    params=params, max_retries=self.max_retries)
        if resp is None or not resp.ok:
            return ""
        data = resp.json()
        person = data.get("contact", {}).get("data", {}) or {}
        numbers = []
        for p in person.get("phoneNumbers", []):
            num = p.get("number") or p.get("internationalNumber")
            if num:
                kind = p.get("phoneType", "phone")
                numbers.append(f"{num} ({kind})")
        return "; ".join(numbers)
