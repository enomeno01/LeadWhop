"""Stage 2 — ICP qualification.

For each company, run a product-focused web search and ask the LLM a single
configurable ICP question (settings.yaml -> icp:). Output is a Yes/No verdict
plus a one-sentence "AI note" that downstream stages reuse for industry
classification — one research pass, several consumers.
"""
from __future__ import annotations

import json
import os
import time

import requests

from .llm import LLM


class Qualifier:
    def __init__(self, llm: LLM, settings: dict):
        self.llm = llm
        self.serper_key = os.environ["SERPER_API_KEY"]
        self.url = settings["search"]["serper_url"]
        self.icp = settings["icp"]
        self.sleep = settings["rate_limits"]["sleep_between_calls"]

    def _product_snippets(self, company: str, country: str) -> str:
        query = f'"{company}" {country} products OR catalog'.strip()
        resp = requests.post(
            self.url,
            headers={"X-API-KEY": self.serper_key, "Content-Type": "application/json"},
            data=json.dumps({"q": query}),
            timeout=20,
        )
        resp.raise_for_status()
        organic = resp.json().get("organic", [])[:5]
        return "\n".join(f"- {r.get('title','')}: {r.get('snippet','')}" for r in organic)

    def qualify(self, company: str, country: str = "") -> dict:
        """Returns {is_fit: Yes/No, is_manufacturer: Yes/No, ai_note: str}."""
        try:
            snippets = self._product_snippets(company, country)
        except requests.RequestException as exc:
            return {"is_fit": "Unknown", "is_manufacturer": "Unknown",
                    "ai_note": "", "error_detail": str(exc)}

        verdict = self.llm.json_call(
            system=("You are a B2B market analyst. Answer from the search snippets "
                    "only. Return strict JSON: {\"is_manufacturer\": \"Yes|No\", "
                    "\"is_fit\": \"Yes|No\", \"note\": \"one short sentence\"}."),
            user=(f"ICP question: {self.icp['question']}\n"
                  f"Note instruction: {self.icp['note_instruction']}\n\n"
                  f"Company: {company} ({country})\nSearch snippets:\n{snippets}"),
        )
        time.sleep(self.sleep)
        return {
            "is_fit": verdict.get("is_fit", "Unknown"),
            "is_manufacturer": verdict.get("is_manufacturer", "Unknown"),
            "ai_note": verdict.get("note", ""),
        }
