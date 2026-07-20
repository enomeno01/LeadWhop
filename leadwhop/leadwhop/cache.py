"""Persistent JSON-backed memoization so repeated LLM/API lookups are free.

Unlike an in-memory dict, this survives across runs: the second time the
pipeline sees the title "Purchasing Manager", it costs zero tokens — even
in a different session next month.
"""
from __future__ import annotations

import json
from pathlib import Path


class DiskCache:
    def __init__(self, cache_dir: str, name: str):
        self.path = Path(cache_dir) / f"{name}.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data: dict[str, str] = {}
        if self.path.exists():
            self._data = json.loads(self.path.read_text(encoding="utf-8"))

    def get(self, key: str) -> str | None:
        return self._data.get(key)

    def set(self, key: str, value: str) -> None:
        self._data[key] = value
        self.path.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=1), encoding="utf-8"
        )
