"""Stage 1 — company name -> official website, with hybrid verification.

Flow:
1. Serper (Google search) returns candidates; a blacklist removes social /
   directory / marketplace results.
2. The LLM picks the official site and reports a confidence level.
3. HYBRID CHECK: cheap heuristic first (company-name token in the domain
   boosts confidence to high); anything still below "high" gets a homepage
   content check — we fetch the page and ask the LLM whether it actually
   belongs to the company.

Why: a wrong domain is the most expensive failure in the whole pipeline —
every downstream Lusha credit spent on it is wasted. Verification cost is
paid only on the uncertain minority, not on every row.
"""
from __future__ import annotations

import json
import os
import re
import time

import requests

from .llm import LLM
from .utils import clean_domain, domain_base

UA = {"User-Agent": "Mozilla/5.0 (compatible; LeadWhop/0.1)"}


class WebsiteFinder:
    def __init__(self, llm: LLM, settings: dict):
        self.llm = llm
        self.serper_key = os.environ["SERPER_API_KEY"]
        self.url = settings["search"]["serper_url"]
        self.n_results = settings["search"]["results_count"]
        self.blacklist = set(settings.get("domain_blacklist", []))
        self.sleep = settings["rate_limits"]["sleep_between_calls"]
        ver = settings.get("verification", {})
        self.fetch_timeout = ver.get("fetch_timeout", 10)

    # ---- search ----------------------------------------------------------

    def _search(self, query: str) -> list[dict]:
        resp = requests.post(
            self.url,
            headers={"X-API-KEY": self.serper_key, "Content-Type": "application/json"},
            data=json.dumps({"q": query, "num": self.n_results}),
            timeout=20,
        )
        resp.raise_for_status()
        return resp.json().get("organic", [])

    def _is_candidate(self, link: str) -> bool:
        domain = clean_domain(link)
        return bool(domain) and not any(b in domain for b in self.blacklist)

    # ---- verification ------------------------------------------------------

    @staticmethod
    def _name_token_in_domain(company: str, domain: str) -> bool:
        """Cheap heuristic: a distinctive company-name token inside the domain."""
        base = domain_base(domain)
        tokens = [t for t in re.split(r"[^a-z0-9]+", str(company).lower())
                  if len(t) >= 4 and t not in ("group", "company", "corp",
                                               "inc", "gmbh", "ltd")]
        return any(t in base for t in tokens)

    def _homepage_text(self, website: str) -> str:
        resp = requests.get(website, headers=UA, timeout=self.fetch_timeout)
        resp.raise_for_status()
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(resp.text, "html.parser")
            title = soup.title.get_text(strip=True) if soup.title else ""
            body = soup.get_text(" ", strip=True)
        except Exception:
            title, body = "", re.sub(r"<[^>]+>", " ", resp.text)
        return (title + " | " + body)[:1500]

    def _verify_by_content(self, company: str, website: str) -> str:
        """Returns 'verified', 'rejected' or 'unreachable'."""
        try:
            text = self._homepage_text(website)
        except requests.RequestException:
            return "unreachable"
        verdict = self.llm.json_call(
            system=("You verify whether a homepage belongs to a specific company. "
                    "Return strict JSON: {\"belongs\": \"Yes|No\"}."),
            user=f"Company: {company}\nHomepage excerpt:\n{text}",
            max_tokens=20,
        )
        return "verified" if verdict.get("belongs") == "Yes" else "rejected"

    # ---- main ------------------------------------------------------------

    def find(self, company: str, country: str = "") -> dict:
        """Returns {website, domain, confidence, needs_review}.

        confidence: high | verified | medium | low | none_found | error
        needs_review is True whenever a human should double-check before
        spending enrichment credits.
        """
        query = f'"{company}" {country} official website'.strip()
        try:
            results = self._search(query)
        except requests.RequestException as exc:
            return {"website": "", "domain": "", "confidence": "error",
                    "needs_review": True, "error_detail": str(exc)}

        candidates = [
            {"link": r.get("link", ""), "title": r.get("title", ""),
             "snippet": r.get("snippet", "")}
            for r in results if self._is_candidate(r.get("link", ""))
        ]
        if not candidates:
            return {"website": "", "domain": "", "confidence": "none_found",
                    "needs_review": True}

        verdict = self.llm.json_call(
            system=("You identify a company's official website from search results. "
                    "Return strict JSON: {\"website\": url-or-empty, "
                    "\"confidence\": \"high|medium|low\"}. "
                    "Prefer the company's own domain over resellers or press."),
            user=f"Company: {company}\nCountry: {country}\nCandidates:\n"
                 + json.dumps(candidates, ensure_ascii=False),
        )
        time.sleep(self.sleep)
        website = str(verdict.get("website", "")).strip()
        if not website:
            return {"website": "", "domain": "", "confidence": "none_found",
                    "needs_review": True}

        confidence = verdict.get("confidence", "low")
        domain = clean_domain(website)

        # Heuristic boost: distinctive name token in domain -> trust it.
        if confidence != "high" and self._name_token_in_domain(company, domain):
            confidence = "high"

        # Hybrid content check for whatever is still uncertain.
        needs_review = False
        if confidence != "high":
            outcome = self._verify_by_content(company, website)
            if outcome == "verified":
                confidence = "verified"
            elif outcome == "rejected":
                return {"website": "", "domain": "", "confidence": "rejected",
                        "needs_review": True}
            else:  # unreachable — keep the guess but flag it
                needs_review = True

        return {"website": website, "domain": domain,
                "confidence": confidence, "needs_review": needs_review}
