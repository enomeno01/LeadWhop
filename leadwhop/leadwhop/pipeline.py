"""Orchestrator — runs any subset of stages with checkpointing.

Usage:
    pipe = Pipeline.from_config("config/settings.yaml")
    df = pipe.run("companies.xlsx", stages=["websites", "qualify", "contacts"])
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import yaml
from dotenv import load_dotenv

from .contact_finder import ContactFinder
from .crm_exporter import CRMExporter
from .llm import LLM
from .phone_enricher import PhoneEnricher
from .qualifier import Qualifier
from .website_finder import WebsiteFinder

STAGES = ["websites", "qualify", "contacts", "phones", "export"]


class Pipeline:
    def __init__(self, settings: dict, tiers: list[dict], crm_mapping: dict | None):
        load_dotenv()
        self.settings = settings
        cache_dir = settings["pipeline"]["cache_dir"]
        self.llm = LLM(settings["models"]["llm"], cache_dir,
                       settings["models"]["temperature"],
                       settings["rate_limits"]["sleep_between_calls"])
        self.finder = WebsiteFinder(self.llm, settings)
        self.qualifier = Qualifier(self.llm, settings)
        self.contacts = ContactFinder(tiers, settings)
        self.phones = PhoneEnricher(settings)
        self.exporter = CRMExporter(self.llm, crm_mapping) if crm_mapping else None
        self.checkpoint_every = settings["pipeline"]["checkpoint_every"]
        self.output_dir = Path(settings["pipeline"]["output_dir"])
        self.output_dir.mkdir(exist_ok=True)

    @classmethod
    def from_config(cls, settings_path: str = "config/settings.yaml") -> "Pipeline":
        base = Path(settings_path).parent
        settings = yaml.safe_load(Path(settings_path).read_text(encoding="utf-8"))
        tiers = yaml.safe_load((base / "tiers.yaml").read_text(encoding="utf-8"))
        mapping_file = base / "crm_mapping.yaml"
        if not mapping_file.exists():
            mapping_file = base / "crm_mapping.example.yaml"
        mapping = yaml.safe_load(mapping_file.read_text(encoding="utf-8"))
        return cls(settings, tiers, mapping)

    # ------------------------------------------------------------------

    def _checkpoint(self, df: pd.DataFrame, name: str) -> None:
        df.to_excel(self.output_dir / f"checkpoint_{name}.xlsx", index=False)

    def run(self, input_path: str, stages: list[str],
            event_name: str = "", progress_cb=None) -> pd.DataFrame:
        df = pd.read_excel(input_path)
        if "Company" not in df.columns:
            df = df.rename(columns={df.columns[0]: "Company"})

        def report(i, n, stage):
            if progress_cb:
                progress_cb(stage, i + 1, n)

        if "websites" in stages:
            for col in ("Website", "Website_Confidence", "Needs_Review"):
                if col not in df.columns:
                    df[col] = "" if col != "Needs_Review" else False
            for i, row in df.iterrows():
                if str(df.at[i, "Website"]).strip() not in ("", "nan"):
                    df.at[i, "Website_Confidence"] = df.at[i, "Website_Confidence"] or "user_provided"
                    continue  # user already supplied a domain — trust it, skip search
                res = self.finder.find(str(row["Company"]), str(row.get("Country", "")))
                df.at[i, "Website"] = res.get("website", "")
                df.at[i, "Website_Confidence"] = res.get("confidence", "")
                df.at[i, "Needs_Review"] = bool(res.get("needs_review", False))
                report(i, len(df), "websites")
                if i % self.checkpoint_every == 0:
                    self._checkpoint(df, "websites")

        if "qualify" in stages:
            for col in ("ICP_Fit", "Is_Manufacturer", "AI_Note"):
                if col not in df.columns:
                    df[col] = ""
            for i, row in df.iterrows():
                if str(df.at[i, "AI_Note"]).strip() not in ("", "nan"):
                    continue
                res = self.qualifier.qualify(str(row["Company"]),
                                             str(row.get("Country", "")))
                df.at[i, "ICP_Fit"] = res["is_fit"]
                df.at[i, "Is_Manufacturer"] = res["is_manufacturer"]
                df.at[i, "AI_Note"] = res["ai_note"]
                report(i, len(df), "qualify")
                if i % self.checkpoint_every == 0:
                    self._checkpoint(df, "qualify")

        if "contacts" in stages:
            rows = []
            targets = df[df.get("ICP_Fit", "Yes") != "No"] if "ICP_Fit" in df.columns else df
            for i, (_, row) in enumerate(targets.iterrows()):
                found = self.contacts.find(str(row["Company"]),
                                           str(row.get("Website", "")))
                for c in found:
                    # name-based fallback hits are kept but flagged for review
                    flagged = bool(row.get("Needs_Review", False)) or (
                        c.get("match_method") == "company_name")
                    rows.append({**row.to_dict(), "Name": c["name"],
                                 "Title": c["title"], "Email": c["email"],
                                 "Contact_Tier": c["tier"],
                                 "Match_Method": c.get("match_method", ""),
                                 "Match_Score": c.get("match_score", ""),
                                 "Lusha_Domain": c.get("lusha_domain", ""),
                                 "Needs_Review": flagged})
                if not found:
                    rows.append({**row.to_dict(), "Name": "", "Title": "",
                                 "Email": "", "Contact_Tier": "",
                                 "Match_Method": "not_found", "Match_Score": "",
                                 "Lusha_Domain": "", "Needs_Review": True})
                report(i, len(targets), "contacts")
                if i % self.checkpoint_every == 0 and rows:
                    self._checkpoint(pd.DataFrame(rows), "contacts")
            df = pd.DataFrame(rows) if rows else df

        if "phones" in stages and "Email" in df.columns:
            df["Phones"] = ""
            for i, row in df.iterrows():
                first, last = "", ""
                if "Name" in df.columns:
                    from .utils import split_full_name
                    first, last = split_full_name(row.get("Name", ""))
                df.at[i, "Phones"] = self.phones.find_phones(
                    email=str(row.get("Email", "")), first_name=first,
                    last_name=last, website=str(row.get("Website", "")))
                report(i, len(df), "phones")
                if i % self.checkpoint_every == 0:
                    self._checkpoint(df, "phones")

        if "export" in stages and self.exporter:
            df = self.exporter.export(df, event_name or "Untitled Event")

        out_path = self.output_dir / "leadwhop_output.xlsx"
        df.to_excel(out_path, index=False)
        return df
