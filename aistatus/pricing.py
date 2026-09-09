# input: httpx model search API responses, filesystem cache files, and provider/model token counts
# output: pricing lookups and cost estimates with in-memory plus atomic file-cache persistence
# pos: shared pricing layer for SDK usage accounting and gateway cost attribution
# >>> 一旦我被更新，务必更新我的开头注释，以及所属文件夹的 CLAUDE.md <<<

"""Pricing lookup and cost estimation for usage tracking."""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx

BASE_URL = "https://aistatus.cc"
CACHE_TTL_SECONDS = 3600


def _standard_cost(
    pricing: dict[str, float | None] | None, input_tokens: int, output_tokens: int
) -> float:
    """Price plain input/output tokens. Unknown pricing is billed as 0 rather than raising."""
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


def _cache_cost(
    pricing: dict[str, float | None] | None,
    input_tokens: int,
    output_tokens: int,
    cache_creation_input_tokens: int,
    cache_read_input_tokens: int,
) -> float:
    """Price a response that used the prompt cache, falling back to the usual multipliers."""
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
        write_price = (
            cache_write_per_million
            if cache_write_per_million is not None
            else (input_per_million * 1.25)
        )
        cost += (max(cache_creation_input_tokens, 0) / 1_000_000) * write_price
        # Cache read: use fetched price, fallback to 0.10x input price
        read_price = (
            cache_read_per_million
            if cache_read_per_million is not None
            else (input_per_million * 0.10)
        )
        cost += (max(cache_read_input_tokens, 0) / 1_000_000) * read_price
    if output_per_million is not None:
        cost += (max(output_tokens, 0) / 1_000_000) * output_per_million
    return round(cost, 8)


class CostCalculator:
    def __init__(self, base_url: str = BASE_URL, ttl_seconds: int = CACHE_TTL_SECONDS):
        self._base_url = base_url.rstrip("/")
        self._ttl_seconds = ttl_seconds
        self._memory_cache: dict[str, dict[str, Any]] = {}
        self._pending_refreshes: dict[str, asyncio.Future[None]] = {}
        self._cache_path = Path.home() / ".aistatus" / "usage" / "pricing-cache.json"

    def calculate_cost(self, provider: str, model: str, input_tokens: int, output_tokens: int) -> float:
        return _standard_cost(self.get_pricing(provider, model), input_tokens, output_tokens)

    async def calculate_cost_async(
        self, provider: str, model: str, input_tokens: int, output_tokens: int
    ) -> float:
        """Async twin of :meth:`calculate_cost` that never blocks the event loop.

        Also waits out a cold cache instead of silently pricing the first request of every model at
        zero, which is what the sync path does when it is called from async code.
        """
        pricing = await self._get_pricing_after_refresh(provider, model)
        return _standard_cost(pricing, input_tokens, output_tokens)

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
        return _cache_cost(
            self.get_pricing(provider, model),
            input_tokens,
            output_tokens,
            cache_creation_input_tokens,
            cache_read_input_tokens,
        )

    async def calculate_cost_with_cache_async(
        self,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cache_creation_input_tokens: int,
        cache_read_input_tokens: int,
    ) -> float:
        """Async twin of :meth:`calculate_cost_with_cache`."""
        pricing = await self._get_pricing_after_refresh(provider, model)
        return _cache_cost(
            pricing,
            input_tokens,
            output_tokens,
            cache_creation_input_tokens,
            cache_read_input_tokens,
        )

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

    async def _get_pricing_after_refresh(
        self, provider: str, model: str
    ) -> dict[str, float | None] | None:
        """Return cached pricing, awaiting one shared refresh when the cache is cold or stale."""
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

        await self._refresh_pricing(cache_key, provider, model)
        entry = self._memory_cache.get(cache_key)
        return entry["pricing"] if self._is_fresh(entry, time.time()) else None

    def _refresh_pricing(self, cache_key: str, provider: str, model: str) -> asyncio.Future[None]:
        """Single-flight refresh: concurrent callers for one model share a single upstream fetch.

        A busy gateway sees many simultaneous requests for the same model, and without this each one
        would issue its own lookup against the pricing API.
        """
        pending = self._pending_refreshes.get(cache_key)
        if pending is not None and not pending.done():
            return pending

        task = asyncio.ensure_future(self._fetch_and_cache_pricing(cache_key, provider, model))
        task.add_done_callback(lambda _task: self._pending_refreshes.pop(cache_key, None))
        self._pending_refreshes[cache_key] = task
        return task

    async def _fetch_and_cache_pricing(self, cache_key: str, provider: str, model: str) -> None:
        pricing = await self._afetch_pricing(provider, model)
        if pricing is None:
            return
        entry = {"ts": time.time(), "pricing": pricing}
        self._memory_cache[cache_key] = entry
        file_cache = self._read_file_cache()
        file_cache[cache_key] = entry
        self._write_file_cache(file_cache)

    async def _afetch_pricing(self, provider: str, model: str) -> dict[str, float | None] | None:
        provider_slug, model_name = self._split_model(provider, model)
        models: list[dict[str, Any]] = []
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                for query in self._candidate_queries(model_name):
                    response = await client.get(
                        f"{self._base_url}/api/models", params={"q": query}
                    )
                    response.raise_for_status()
                    models = response.json().get("models") or []
                    if models:
                        break
        except Exception:  # noqa: BLE001 — pricing is best-effort; a lookup failure must not bubble
            return None
        return self._build_pricing(provider_slug, model_name, models)

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

        return self._build_pricing(provider_slug, model_name, models)

    def _build_pricing(
        self, provider_slug: str, model_name: str, models: list[dict[str, Any]]
    ) -> dict[str, float | None] | None:
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
        fd, tmp_name = tempfile.mkstemp(prefix="pricing-cache-", suffix=".json", dir=self._cache_path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(cache, fh, ensure_ascii=False, indent=2)
            os.replace(tmp_name, self._cache_path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)

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
