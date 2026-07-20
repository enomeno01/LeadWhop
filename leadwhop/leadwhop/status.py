"""Run-time warning collector.

Modules append human-readable warnings here (credit exhausted, invalid key,
rate-limit walls). The Streamlit app displays them after the run so failures
are never silent.
"""
from __future__ import annotations

_warnings: list[str] = []


def warn(message: str) -> None:
    print(f"    ⚠️ {message}")
    if message not in _warnings:
        _warnings.append(message)


def get_warnings() -> list[str]:
    return list(_warnings)


def clear() -> None:
    _warnings.clear()


def classify_api_error(provider: str, status_code: int, body: str) -> str:
    """Turns an HTTP error into a user-friendly warning message."""
    body_l = (body or "").lower()
    if status_code == 401:
        return f"{provider}: API key invalid or expired (401)."
    if status_code == 402 or "credit" in body_l or "quota" in body_l or "insufficient" in body_l:
        return f"{provider}: OUT OF CREDITS — top up your account (HTTP {status_code})."
    if status_code == 403:
        return f"{provider}: Access denied (403) — key may lack this API permission or plan."
    if status_code == 429:
        return f"{provider}: Rate limited heavily (429) — try again later."
    return f"{provider}: API error {status_code}: {body[:120]}"
