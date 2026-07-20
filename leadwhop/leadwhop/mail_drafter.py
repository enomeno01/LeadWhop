"""Stage 6 — personalised outreach email drafts.

Runs independently of all other stages. Minimum input: just an Email column.
All other columns (Name, Company, AI_Note) are optional and add personalisation
when present.

Personalisation levels (additive):
  Email only       → Dear Team, generic hook, no company reference
  + Name           → Dear {FirstName},
  + Company        → company referenced in intro & closing line
  + AI_Note        → GPT 2-sentence personalised hook

Output columns: Email_Subject, Email_Draft
Only rows with a valid email address receive a draft.
"""
from __future__ import annotations

import os
import time

import pandas as pd

from .llm import LLM

SENDER_NAME    = "Enescan Demirpençe"
SENDER_COMPANY = "GCA"
SENDER_URL     = "https://www.gca.com.tr"
SENDER_INTRO   = (
    "My name is Enescan Demirpençe and I represent GCA, a flint glass "
    "packaging manufacturer and part of the Gürok Group."
)

SUBJECT_WITH_COMPANY = "Glass Packaging Partnership Opportunity — {company}"
SUBJECT_GENERIC      = "Glass Packaging Solutions — GCA Introduction"

GENERIC_HOOK = (
    "I believe your product line could be a great match for our glass "
    "packaging solutions, and I would love to explore potential synergies."
)

TEMPLATE = """\
Dear {salutation},

I hope you are doing well.

{sender_intro}

{intro_line}

{personalised_hook}

At GCA, we support producers with high-quality glass bottles, bottle decoration, and new design development through our design team.

We can offer:
\u2022 Flint glass bottle solutions
\u2022 Bottle decoration options to strengthen shelf presence
\u2022 New bottle design projects with our design team
\u2022 Competitive pricing and high service standards

{closing_line}

Would you be open to a short 30-minute introductory call in the coming days?

Kind regards,
{sender_name}
{sender_company}
{sender_url}"""


class MailDrafter:
    def __init__(self, llm: LLM):
        self.llm = llm

    # ── helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _empty(val) -> bool:
        return not val or str(val).strip() in ("", "nan", "-", "Unknown", "None")

    def _personalised_hook(self, company: str, ai_note: str) -> str:
        """Two sentences cached by company+note fingerprint."""
        cache_key = f"hook|{company}|{ai_note[:80]}"
        cache = self.llm._cache("mail_hook")
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

        result = self.llm.json_call(
            system=(
                "You write concise, professional B2B email copy for GCA, "
                "a flint glass packaging manufacturer. "
                "Return strict JSON: {\"hook\": \"two short sentences\"}. "
                "Sentence 1: what specifically caught GCA's attention about "
                "this company's products (reference actual products if known). "
                "Sentence 2: one concrete reason GCA's glass packaging or "
                "design capabilities could add value for them. "
                "No flattery, no buzzwords. Max 50 words total."
            ),
            user=f"Company: {company}\nWhat they produce: {ai_note}",
            max_tokens=120,
        )
        hook = str(result.get("hook", "")).strip() or GENERIC_HOOK
        cache.set(cache_key, hook)
        time.sleep(0.2)
        return hook

    # ── main draft logic ─────────────────────────────────────────────────

    def draft(self, row: dict) -> tuple[str, str]:
        """Return (subject, body) for one lead row."""
        name    = str(row.get("Name") or row.get("FirstName") or "").strip()
        company = str(row.get("Company") or "").strip()
        ai_note = str(row.get("AI_Note") or "").strip()

        has_name    = not self._empty(name)
        has_company = not self._empty(company)
        has_note    = not self._empty(ai_note)

        # Salutation
        salutation = name.split()[0] if has_name else "Team"

        # Intro line
        if has_company:
            intro_line = (f"I came across {company} and wanted to briefly "
                          f"introduce our glass packaging capabilities.")
        else:
            intro_line = ("I wanted to briefly introduce GCA's glass "
                          "packaging capabilities.")

        # Closing line
        closing_line = (
            f"I believe there could be a good opportunity to explore how we "
            f"can support {company} with future glass packaging needs."
            if has_company else
            "I believe there could be a good opportunity to explore how GCA "
            "can support your glass packaging needs."
        )

        # Hook
        if has_note and has_company:
            hook = self._personalised_hook(company, ai_note)
        else:
            hook = GENERIC_HOOK

        # Subject
        subject = (SUBJECT_WITH_COMPANY.format(company=company)
                   if has_company else SUBJECT_GENERIC)

        body = TEMPLATE.format(
            salutation=salutation,
            sender_intro=SENDER_INTRO,
            intro_line=intro_line,
            personalised_hook=hook,
            closing_line=closing_line,
            sender_name=SENDER_NAME,
            sender_company=SENDER_COMPANY,
            sender_url=SENDER_URL,
        )
        return subject, body

    # ── batch runner ─────────────────────────────────────────────────────

    def run(self, df: pd.DataFrame) -> pd.DataFrame:
        """Add Email_Subject and Email_Draft columns.
        Only rows with a valid email receive a draft.
        Minimum required column: Email.
        """
        df = df.copy()
        subjects, drafts = [], []
        total = 0
        for _, row in df.iterrows():
            email = str(row.get("Email") or "").strip()
            if email and email not in ("nan", "-") and "@" in email:
                subject, body = self.draft(row.to_dict())
                subjects.append(subject)
                drafts.append(body)
                total += 1
            else:
                subjects.append("")
                drafts.append("")
        df["Email_Subject"] = subjects
        df["Email_Draft"]   = drafts
        print(f"   \u2709\ufe0f {total} email drafts generated")
        return df
