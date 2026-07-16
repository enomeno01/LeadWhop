"""Stage 1 — company name -> official website, with hybrid verification.

Flow:
1. Serper (Google search) returns candidates; a blacklist removes social /
   directory / marketplace results.
2. Name-token heuristic: if ANY token from the company name appears in the
   domain, confidence is immediately set to "high" — no LLM call needed.
3. LLM pick: for remaining candidates, GPT chooses the best match.
4. HYBRID CHECK: only sites still below "high" after steps 2-3 get a
   homepage content check.

Key change from v1: token length minimum removed (was 4 chars), so short
company names like "GCA", "BWT", "Nio" now match correctly. Also GPT prompt
is more permissive — it used to over-reject valid candidates.
"""
from __future__ import annotations

import json
import os
import re
import time

import requests

from .llm import LLM
from .utils import clean_domain, domain_base
from . import status

UA = {"User-Agent": "Mozilla/5.0 (compatible; LeadWhop/0.1)"}


class WebsiteFinder:
    def __init__(self, llm: LLM, settings: dict):
        self.llm = llm
        self.serper_key = os.environ["SERPER_API_KEY"]
        self.url = settings["search"]["serper_url"]
        self.n_results = settings["search"]["results_count"]
        self.blacklist = set(settings.get("domain_blacklist", []))
        self.sleep = settings["rate_limits"]["sleep_between_calls"]
        self.settings = settings
        ver = settings.get("verification", {})
        self.fetch_timeout = ver.get("fetch_timeout", 10)

    # ---- search ----------------------------------------------------------

    def _search(self, query: str) -> list[dict]:
        resp = requests.post(
            self.url,
            headers={"X-API-KEY": self.serper_key,
                     "Content-Type": "application/json"},
            data=json.dumps({"q": query, "num": self.n_results, "gl": self.settings.get("search", {}).get("gl", "us"), "hl": self.settings.get("search", {}).get("hl", "en")}),
            timeout=20,
        )
        if not resp.ok:
            status.warn(status.classify_api_error("Serper", resp.status_code, resp.text))
            resp.raise_for_status()
        return resp.json().get("organic", [])

    def _is_candidate(self, link: str) -> bool:
        domain = clean_domain(link)
        return bool(domain) and not any(b in domain for b in self.blacklist)

    # ---- heuristic -------------------------------------------------------

    @staticmethod
    def _name_token_in_domain(company: str, domain: str) -> bool:
        """Any token from company name found in domain base = high confidence.

        No minimum length: 'GCA', 'BMW', 'BWT' all deserve to match.
        Generic stop-words are excluded so 'group.com' doesn't match
        'Acme Group Inc'.
        """
        STOPWORDS = {"group", "company", "corp", "inc", "gmbh", "ltd",
                     "llc", "srl", "spa", "bv", "as", "ab", "oy",
                     "holding", "international", "global"}
        base = domain_base(domain).lower()
        if not base:
            return False
        # all alphabetic tokens from company name, stop-words removed
        tokens = [t for t in re.split(r"[^a-z0-9]+",
                                       str(company).lower())
                  if t and t not in STOPWORDS]
        return any(t in base for t in tokens)

    # ---- verification ----------------------------------------------------

    def _homepage_text(self, website: str) -> str:
        resp = requests.get(website, headers=UA, timeout=self.fetch_timeout,
                            allow_redirects=True)
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
        try:
            text = self._homepage_text(website)
        except requests.RequestException:
            return "unreachable"
        verdict = self.llm.json_call(
            system=("You verify whether a homepage belongs to a specific "
                    "company. Be lenient — a partial name match or same "
                    "industry is enough. Return strict JSON: "
                    "{\"belongs\": \"Yes|No\"}."),
            user=f"Company: {company}\nHomepage excerpt:\n{text}",
            max_tokens=20,
        )
        return "verified" if verdict.get("belongs") == "Yes" else "rejected"

    # ---- main ------------------------------------------------------------

    def find(self, company: str, country: str = "") -> dict:
        """Returns {website, domain, confidence, needs_review, debug}.

        confidence: high | verified | medium | low | none_found | error
        debug: human-readable string explaining what happened — written to
               the Website_Debug column so failures are never silent.
        """
        query = f'"{company}" {country} official website'.strip()
        try:
            results = self._search(query)
        except requests.RequestException as exc:
            return {"website": "", "domain": "", "confidence": "error",
                    "needs_review": True,
                    "debug": f"Serper error: {exc}"}

        candidates = [
            {"link": r.get("link", ""), "title": r.get("title", ""),
             "snippet": r.get("snippet", "")}
            for r in results if self._is_candidate(r.get("link", ""))
        ]

        if not candidates:
            return {"website": "", "domain": "", "confidence": "none_found",
                    "needs_review": True,
                    "debug": f"Serper returned {len(results)} results, "
                             f"all blocked by domain blacklist or empty"}

        # --- heuristic pass: check every candidate before calling GPT ---
        for cand in candidates:
            domain = clean_domain(cand["link"])
            if self._name_token_in_domain(company, domain):
                return {"website": cand["link"], "domain": domain,
                        "confidence": "high", "needs_review": False,
                        "debug": f"heuristic match on '{domain}'"}

        # --- GPT pick (permissive prompt) --------------------------------
        verdict = self.llm.json_call(
            system=(
                "You identify a company's official website from search "
                "results. Be permissive — if any candidate is plausibly "
                "the company's own site, pick it. Only return an empty "
                "website if NONE of the candidates could possibly be the "
                "company's own domain. "
                "Return strict JSON: {\"website\": url-or-empty, "
                "\"confidence\": \"high|medium|low\", "
                "\"reason\": \"one short sentence\"}."
            ),
            user=(f"Company: {company}\nCountry: {country}\n"
                  f"Candidates:\n"
                  + json.dumps(candidates, ensure_ascii=False)),
        )
        time.sleep(self.sleep)

        website = str(verdict.get("website", "")).strip()
        reason = verdict.get("reason", "")

        if not website:
            return {"website": "", "domain": "", "confidence": "none_found",
                    "needs_review": True,
                    "debug": f"GPT rejected all candidates. Reason: {reason}. "
                             f"Candidates: "
                             + ", ".join(c["link"] for c in candidates[:3])}

        confidence = verdict.get("confidence", "low")
        domain = clean_domain(website)

        # second heuristic chance on GPT's pick
        if confidence != "high" and self._name_token_in_domain(company, domain):
            confidence = "high"
            return {"website": website, "domain": domain,
                    "confidence": confidence, "needs_review": False,
                    "debug": f"GPT pick '{domain}' confirmed by heuristic"}

        # content check for low/medium confidence
        if confidence != "high":
            outcome = self._verify_by_content(company, website)
            if outcome == "verified":
                return {"website": website, "domain": domain,
                        "confidence": "verified", "needs_review": False,
                        "debug": f"content-verified '{domain}'"}
            elif outcome == "rejected":
                return {"website": "", "domain": "",
                        "confidence": "rejected", "needs_review": True,
                        "debug": f"content check REJECTED '{domain}' "
                                 f"(GPT reason: {reason})"}
            else:
                return {"website": website, "domain": domain,
                        "confidence": confidence, "needs_review": True,
                        "debug": f"site unreachable for content check, "
                                 f"keeping GPT pick '{domain}'"}

        return {"website": website, "domain": domain,
                "confidence": confidence, "needs_review": False,
                "debug": f"GPT high-confidence pick '{domain}'"}
