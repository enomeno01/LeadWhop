"""Stage 4 (optional) — add phone numbers to existing leads.

Mirrors the original Personal_Phone_Finder notebook exactly:
1. If email present → look up by email (exact match, cheapest)
2. If no email → look up by firstName + lastName + domain
3. Collects ALL phone numbers with their type labels (Mobile, Work, etc.)
4. Falls back to company phone if no personal numbers found
5. Deduplicates and joins with " / "
"""
from __future__ import annotations

import os
import re
import time

import requests

from .utils import clean_domain

ENRICH_URL = "https://api.lusha.com/v2/person"


def _smart_wait(text: str) -> None:
    match = re.search(r"Reset in (\d+) seconds", text or "")
    wait = int(match.group(1)) + 5 if match else 15
    print(f"    ⏳ Rate limited — waiting {wait}s")
    time.sleep(wait)


class PhoneEnricher:
    def __init__(self, settings: dict):
        self.headers = {
            "accept": "application/json",
            "api_key": os.environ["LUSHA_API_KEY"],
        }
        self.max_retries = settings["rate_limits"]["max_retries"]

    def find_phones(self, email: str = "", first_name: str = "",
                    last_name: str = "", website: str = "") -> str:
        """Returns phone numbers as 'number (Type) / number (Type)' string."""
        params: dict = {"revealPhones": "true", "revealEmails": "false"}

        # Strategy 1: email exact match
        if email and email not in ("nan", "-") and "@" in email:
            params["email"] = email
        # Strategy 2: name + domain
        elif first_name and first_name not in ("nan", "-"):
            params["firstName"] = first_name
            params["lastName"]  = last_name if last_name not in ("nan", "-") else ""
            domain = clean_domain(website)
            if domain:
                params["domain"] = domain
        else:
            return ""

        for _ in range(self.max_retries):
            try:
                resp = requests.get(ENRICH_URL, headers=self.headers,
                                    params=params, timeout=15)
                if resp.ok:
                    data = resp.json()
                    found: list[str] = []

                    # A. Personal phone numbers
                    contact_data = (data.get("contact") or {}).get("data") or {}
                    for p in contact_data.get("phoneNumbers") or []:
                        num = p.get("number") or p.get("internationalNumber")
                        if not num:
                            continue
                        label = str(p.get("label") or p.get("type") or "Phone").capitalize()
                        found.append(f"{num} ({label})")

                    # B. Fallback: company phone
                    if not found:
                        co = data.get("company") or {}
                        c_phone = (co.get("phone")
                                   or (co.get("location") or {}).get("phone"))
                        if c_phone:
                            found.append(f"{c_phone} (Company)")

                    # Deduplicate and join
                    return " / ".join(list(dict.fromkeys(found)))

                elif resp.status_code == 429:
                    _smart_wait(resp.text)
                    continue
                elif resp.status_code in (401, 402, 403):
                    print(f"    ⚠️ Lusha auth/credit error {resp.status_code}")
                    return ""
                else:
                    return ""

            except Exception as exc:
                print(f"    ⚠️ Connection error: {exc}")
                time.sleep(2)

        return ""
