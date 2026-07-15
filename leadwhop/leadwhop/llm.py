"""Thin wrapper around the OpenAI client with caching and strict-choice prompts."""
from __future__ import annotations

import os
import time

from .cache import DiskCache


class LLM:
    def __init__(self, model: str, cache_dir: str, temperature: float = 0.0,
                 sleep: float = 0.2):
        from openai import OpenAI  # lazy: package imports fine without openai installed
        self.client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
        self.model = model
        self.temperature = temperature
        self.sleep = sleep
        self._caches: dict[str, DiskCache] = {}
        self._cache_dir = cache_dir

    def _cache(self, namespace: str) -> DiskCache:
        if namespace not in self._caches:
            self._caches[namespace] = DiskCache(self._cache_dir, namespace)
        return self._caches[namespace]

    def choose(self, namespace: str, system: str, user: str,
               options: list[str], default: str) -> str:
        """Ask the model to pick exactly one option; cache by user prompt.

        Rules-first callers should only reach this for ambiguous inputs.
        """
        cache = self._cache(namespace)
        cached = cache.get(user)
        if cached is not None:
            return cached
        try:
            resp = self.client.chat.completions.create(
                model=self.model,
                temperature=self.temperature,
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": user}],
            )
            answer = (resp.choices[0].message.content or "").strip()
        except Exception:
            return default
        if answer not in options:
            answer = default
        cache.set(user, answer)
        time.sleep(self.sleep)
        return answer

    def json_call(self, system: str, user: str, max_tokens: int = 120) -> dict:
        """Strict-JSON call for qualification verdicts."""
        import json as _json
        try:
            resp = self.client.chat.completions.create(
                model=self.model,
                temperature=self.temperature,
                max_tokens=max_tokens,
                response_format={"type": "json_object"},
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": user}],
            )
            return _json.loads(resp.choices[0].message.content or "{}")
        except Exception:
            return {}
