"""Pricing lookup and cost estimation for usage tracking."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

import httpx

BASE_URL = "https://aistatus.cc"
CACHE_TTL_SECONDS = 3600


class CostCalculator:
    def __init__(self, base_url: str = BASE_URL, ttl_seconds: int = CACHE_TTL_SECONDS):
        self._base_url = base_url.rstrip("/")
        self._ttl_seconds = ttl_seconds
        self._memory_cache: dict[str, dict[str, Any]] = {}
        self._cache_path = Path.home() / ".aistatus" / "usage" / "pricing-cache.json"

    def calculate_cost(self, provider: str, model: str, input_tokens: int, output_tokens: int) -> float:
        pricing = self.get_pricing(provider, model)
        if not pricing:
            return 0.0

        input_per_million = pricing.get("input_per_million")
        output_per_million = pricing.get("output_per_million")
        if input_per_million is None and output_per_million is None:
            return 0.0

        cost = 0.0
        if input_per_million is not None:
            cost += (max(input_tokens, 0) / 1_000_000) * input_per_million
        if output_per_million is not None:
            cost += (max(output_tokens, 0) / 1_000_000) * output_per_million
        return round(cost, 8)

    def calculate_cost_with_cache(
        self,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cache_creation_input_tokens: int,
        cache_read_input_tokens: int,
    ) -> float:
        """Calculate cost including prompt cache tokens."""
        pricing = self.get_pricing(provider, model)
        if not pricing:
            return 0.0

        input_per_million = pricing.get("input_per_million")
        output_per_million = pricing.get("output_per_million")
        cache_read_per_million = pricing.get("input_cache_read_per_million")
        cache_write_per_million = pricing.get("input_cache_write_per_million")

        if input_per_million is None and output_per_million is None:
            return 0.0

        cost = 0.0
        if input_per_million is not None:
            cost += (max(input_tokens, 0) / 1_000_000) * input_per_million
            # Cache creation: use fetched price, fallback to 1.25x input price
            write_price = cache_write_per_million if cache_write_per_million is not None else (input_per_million * 1.25)
            cost += (max(cache_creation_input_tokens, 0) / 1_000_000) * write_price
            # Cache read: use fetched price, fallback to 0.10x input price
            read_price = cache_read_per_million if cache_read_per_million is not None else (input_per_million * 0.10)
            cost += (max(cache_read_input_tokens, 0) / 1_000_000) * read_price
        if output_per_million is not None:
            cost += (max(output_tokens, 0) / 1_000_000) * output_per_million
        return round(cost, 8)

    def get_pricing(self, provider: str, model: str) -> dict[str, float | None] | None:
        cache_key = self._normalize_key(provider, model)
        now = time.time()

        mem_entry = self._memory_cache.get(cache_key)
        if self._is_fresh(mem_entry, now):
            return mem_entry["pricing"]

        file_cache = self._read_file_cache()
        file_entry = file_cache.get(cache_key)
        if self._is_fresh(file_entry, now):
            self._memory_cache[cache_key] = file_entry
            return file_entry["pricing"]

        pricing = self._fetch_pricing(provider, model)
        if pricing is None:
            return None

        entry = {"ts": now, "pricing": pricing}
        self._memory_cache[cache_key] = entry
        file_cache[cache_key] = entry
        self._write_file_cache(file_cache)
        return pricing

    def _fetch_pricing(self, provider: str, model: str) -> dict[str, float | None] | None:
        provider_slug, model_name = self._split_model(provider, model)
        queries = self._candidate_queries(model_name)
        models: list[dict[str, Any]] = []

        try:
            with httpx.Client(timeout=3.0) as client:
                for query in queries:
                    response = client.get(
                        f"{self._base_url}/api/models",
                        params={"q": query},
                    )
                    response.raise_for_status()
                    data = response.json()
                    models = data.get("models") or []
                    if models:
                        break
        except Exception:
            return None

        match = self._pick_model_match(provider_slug, model_name, models)
        if not match:
            return None

        pricing = match.get("pricing") or {}
        prompt = self._to_float(pricing.get("prompt"))
        completion = self._to_float(pricing.get("completion"))
        if prompt is None and completion is None:
            return None

        cache_read = self._to_float(pricing.get("input_cache_read"))
        cache_write = self._to_float(pricing.get("input_cache_write"))

        return {
            "input_per_million": None if prompt is None else prompt * 1_000_000,
            "output_per_million": None if completion is None else completion * 1_000_000,
            "input_cache_read_per_million": None if cache_read is None else cache_read * 1_000_000,
            "input_cache_write_per_million": None if cache_write is None else cache_write * 1_000_000,
        }

    def _pick_model_match(self, provider: str, model: str, models: list[dict[str, Any]]) -> dict[str, Any] | None:
        target_full = self._normalize_model_id(f"{provider}/{model}")
        target_name = self._normalize_model_id(model)

        for item in models:
            candidate = self._normalize_model_id(str(item.get("id", "")))
            if candidate == target_full:
                return item
        for item in models:
            candidate = self._normalize_model_id(str(item.get("id", "")))
            if candidate.endswith(f"/{target_name}"):
                return item
        for item in models:
            candidate = self._normalize_model_id(str(item.get("id", "")))
            if target_name in candidate:
                return item
        return models[0] if models else None

    def _read_file_cache(self) -> dict[str, dict[str, Any]]:
        try:
            if not self._cache_path.exists():
                return {}
            data = json.loads(self._cache_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        return {}

    def _write_file_cache(self, cache: dict[str, dict[str, Any]]) -> None:
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")

    def _normalize_key(self, provider: str, model: str) -> str:
        provider_slug, model_name = self._split_model(provider, model)
        return f"{provider_slug}/{model_name}"

    @staticmethod
    def _split_model(provider: str, model: str) -> tuple[str, str]:
        if "/" in model:
            provider_slug, model_name = model.split("/", 1)
            return provider_slug, model_name
        return provider, model

    def _is_fresh(self, entry: dict[str, Any] | None, now: float) -> bool:
        if not entry:
            return False
        ts = self._to_float(entry.get("ts"))
        return ts is not None and (now - ts) < self._ttl_seconds

    @staticmethod
    def _normalize_model_id(value: str) -> str:
        value = value.lower().strip()
        value = re.sub(r"(?<=\d)-(?=\d)", ".", value)
        return value

    def _candidate_queries(self, model_name: str) -> list[str]:
        variants = [model_name]
        normalized = self._normalize_model_id(model_name)
        if normalized != model_name:
            variants.append(normalized)
        versions = self._version_aliases(model_name)
        variants.extend(versions)
        variants.extend(version.replace(".", "-") for version in versions)
        variants.append(normalized.replace(".", "-"))
        variants.append(normalized.replace("-", " "))

        deduped: list[str] = []
        seen: set[str] = set()
        for variant in variants:
            variant = variant.strip()
            if not variant or variant in seen:
                continue
            seen.add(variant)
            deduped.append(variant)
        return deduped

    @classmethod
    def _version_aliases(cls, model_name: str) -> list[str]:
        match = re.fullmatch(r"(.+?)-(\d+)-(\d+)-(\d{8})", model_name.lower().strip())
        if not match:
            return []
        prefix, major, minor, _date = match.groups()
        return [f"{prefix}-{major}.{minor}"]

    @staticmethod
    def _to_float(value: Any) -> float | None:
        try:
            if value is None:
                return None
            return float(value)
        except (TypeError, ValueError):
            return None
