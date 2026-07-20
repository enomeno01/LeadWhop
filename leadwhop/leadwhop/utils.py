"""Shared helpers: domain cleaning, name splitting, throttled requests."""
from __future__ import annotations

import time
from urllib.parse import urlparse

import pandas as pd
import requests


def clean_domain(url: str | None) -> str:
    """`https://www.Acme.com/about` -> `acme.com`."""
    if url is None or (isinstance(url, float) and pd.isna(url)) or not str(url).strip():
        return ""
    url = str(url).strip().lower()
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    domain = urlparse(url).netloc
    return domain[4:] if domain.startswith("www.") else domain


def split_full_name(full_name: str) -> tuple[str, str]:
    """'Maria de la Cruz' -> ('Maria de la', 'Cruz'). Last token = last name."""
    full_name = str(full_name or "").strip()
    if not full_name or full_name == "-":
        return "Unknown", "Unknown"
    parts = full_name.rsplit(" ", 1)
    return (parts[0], parts[1]) if len(parts) == 2 else (parts[0], "Unknown")


def request_with_backoff(method: str, url: str, *, max_retries: int = 3,
                         base_wait: float = 15.0, **kwargs) -> requests.Response | None:
    """HTTP call that respects 429s. Reads 'Reset in N seconds' hints when present."""
    import re
    for attempt in range(max_retries):
        try:
            resp = requests.request(method, url, timeout=20, **kwargs)
        except requests.RequestException:
            time.sleep(base_wait)
            continue
        if resp.status_code != 429:
            return resp
        match = re.search(r"Reset in (\d+) seconds", resp.text or "")
        wait = int(match.group(1)) + 5 if match else base_wait * (attempt + 1)
        print(f"  rate limited — sleeping {wait}s")
        time.sleep(wait)
    return None


def domain_base(domain: str) -> str:
    """`acme.com` / `acme.de` / `acme.com.tr` -> `acme`.

    Used to recognize TLD variants of the same company (a real-world Lusha
    quirk: your search says acme.com, their record says acme.de).
    """
    domain = clean_domain(domain)
    return domain.split(".")[0] if domain else ""


def similarity(a: str, b: str) -> float:
    """Case-insensitive fuzzy ratio between two names (0-1)."""
    from difflib import SequenceMatcher
    a = str(a or "").strip().lower()
    b = str(b or "").strip().lower()
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()
