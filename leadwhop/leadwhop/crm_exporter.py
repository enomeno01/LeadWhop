"""Stage 5 — CRM-ready export.

Classifies every lead on four axes (business function, industry,
sub-industry, seniority) and maps text values to CRM record IDs from
config/crm_mapping.yaml.

Rules-first design: deterministic keyword rules resolve the obvious cases
("CEO" -> C-Suite) at zero cost; only ambiguous inputs reach the LLM, and
every LLM verdict is cached on disk.
"""
from __future__ import annotations

import difflib

import pandas as pd

from .llm import LLM
from .utils import split_full_name

SENIORITY_LEVELS = ["Board Level", "C-Suite Level", "Upper Managerial Level",
                    "Manager Level", "Mid Level", "Entry Level"]

# Deterministic seniority rules — checked in order, first hit wins.
SENIORITY_RULES = [
    ("Board Level", ["board", "chairman", "chairwoman"]),
    ("C-Suite Level", ["ceo", "cfo", "coo", "cto", "cmo", "cpo", "chief",
                        "president", "founder", "owner"]),
    ("Upper Managerial Level", ["vice president", "vp", "general manager",
                                 "director", "head of"]),
    ("Manager Level", ["manager", "supervisor", "lead"]),
    ("Entry Level", ["assistant", "junior", "intern", "trainee"]),
]


class CRMExporter:
    def __init__(self, llm: LLM, mapping: dict):
        self.llm = llm
        self.m = mapping

    # ---- classification -------------------------------------------------

    def seniority(self, title: str) -> str:
        t = str(title or "").lower()
        if not t.strip() or t.strip() == "-":
            return "Mid Level"
        for level, words in SENIORITY_RULES:
            if any(w in t for w in words):
                return level
        return self.llm.choose(
            "seniority",
            system=("Classify the job title into exactly one of: "
                    + ", ".join(SENIORITY_LEVELS) + ". Reply with the level only."),
            user=f"Title: {title}", options=SENIORITY_LEVELS, default="Mid Level",
        )

    def business_function(self, title: str) -> str:
        options = list(self.m["business_functions"].keys())
        default = options[0]
        if not str(title or "").strip():
            return default
        return self.llm.choose(
            "business_function",
            system=("Assign the job title to exactly one of these business "
                    "functions: " + ", ".join(options) +
                    ". Reply with the function name only, verbatim."),
            user=f"Title: {title}", options=options, default=default,
        )

    def industry(self, ai_note: str) -> str:
        options = list(self.m["industries"].keys())
        default = options[-1]
        if not str(ai_note or "").strip():
            return default
        return self.llm.choose(
            "industry",
            system=("From the company description choose exactly one industry: "
                    + ", ".join(options) + ". Reply with the name only."),
            user=f"Description: {ai_note}", options=options, default=default,
        )

    def sub_industry(self, ai_note: str) -> str:
        options = list(self.m["sub_industries"].keys())
        default = "Other Manufacturing" if "Other Manufacturing" in options else options[-1]
        if not str(ai_note or "").strip():
            return default
        return self.llm.choose(
            "sub_industry",
            system=("From the company description choose exactly one sub-industry: "
                    + ", ".join(options) + ". Reply with the name only, verbatim."),
            user=f"Description: {ai_note}", options=options, default=default,
        )

    # ---- country / territory --------------------------------------------

    def country_id(self, raw: str) -> str:
        if not str(raw or "").strip():
            return ""
        key = str(raw).strip().lower()
        aliases = self.m.get("country_aliases", {})
        countries = self.m.get("countries", {})
        if key in aliases:
            return countries.get(aliases[key], "")
        for name, cid in countries.items():
            if key == name.lower():
                return cid
        close = difflib.get_close_matches(key, [c.lower() for c in countries],
                                          n=1, cutoff=0.5)
        if close:
            for name, cid in countries.items():
                if name.lower() == close[0]:
                    return cid
        return ""

    def territory_id(self, country_id: str) -> str:
        home = self.m.get("countries", {}).get(self.m.get("home_country", ""), None)
        key = "domestic" if country_id and country_id == home else "export"
        return self.m.get("territories", {}).get(key, "")

    # ---- main ------------------------------------------------------------

    def export(self, df: pd.DataFrame, event_name: str) -> pd.DataFrame:
        """Expects columns: Company, Website, Email, Title, Name, Country, AI_Note."""
        out = pd.DataFrame()
        out["Company"] = df["Company"]
        out["Website"] = df.get("Website", "")
        out["Email"] = df.get("Email", "")
        out["Title"] = df.get("Title", "")
        names = df.get("Name", pd.Series([""] * len(df))).apply(
            lambda x: pd.Series(split_full_name(x)))
        out["FirstName"], out["LastName"] = names[0], names[1]

        out["Address_Country__c"] = df.get("Country", "").apply(self.country_id)
        out["Address_Territory__c"] = out["Address_Country__c"].apply(self.territory_id)

        out["Level__c"] = df.get("Title", "").apply(self.seniority)
        out["Business_Function__c"] = (
            df.get("Title", "").apply(self.business_function)
            .map(self.m["business_functions"]))
        out["Industry__c"] = (
            df.get("AI_Note", "").apply(self.industry).map(self.m["industries"]))
        out["Sub_Industry__c"] = (
            df.get("AI_Note", "").apply(self.sub_industry)
            .map(self.m["sub_industries"]))

        out["Current Packaging Strategy"] = df.get("AI_Note", "")
        out["Event_Name__c"] = event_name
        out["RecordTypeID"] = self.m.get("record_type_id", "")
        return out
