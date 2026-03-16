"""Thin client for the aistatus.cc public API."""

from __future__ import annotations

import httpx

from .models import Alternative, CheckResult, ModelInfo, ProviderStatus, Status

BASE_URL = "https://aistatus.cc"


class StatusAPI:
    """Stateless HTTP client for aistatus.cc.  All methods are class-level."""

    def __init__(self, base_url: str = BASE_URL, timeout: float = 3.0):
        self._base = base_url.rstrip("/")
        self._timeout = timeout

    # ---- sync helpers ------------------------------------------------

    def _get(self, path: str, params: dict | None = None) -> dict:
        with httpx.Client(timeout=self._timeout) as c:
            r = c.get(f"{self._base}{path}", params=params)
            r.raise_for_status()
            return r.json()

    async def _aget(self, path: str, params: dict | None = None) -> dict:
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            r = await c.get(f"{self._base}{path}", params=params)
            r.raise_for_status()
            return r.json()

    # ---- public methods (sync) ----------------------------------------

    def check_provider(self, slug: str) -> CheckResult:
        """Pre-flight check for a provider.  GET /api/check?provider=..."""
        data = self._get("/api/check", {"provider": slug})
        return self._parse_check(data)

    def check_model(self, model_id: str) -> CheckResult:
        """Pre-flight check for a specific model.  GET /api/check?model=..."""
        data = self._get("/api/check", {"model": model_id})
        return self._parse_check(data)

    def providers(self) -> list[ProviderStatus]:
        """All provider statuses.  GET /api/providers"""
        data = self._get("/api/providers")
        return [
            ProviderStatus(
                slug=p["slug"],
                name=p["name"],
                status=Status(p["status"]),
                status_detail=p.get("statusDetail"),
                model_count=p.get("modelCount", 0),
            )
            for p in data.get("providers", [])
        ]

    def model(self, model_id: str) -> ModelInfo | None:
        """Get single model info.  GET /api/models/:provider/:model"""
        try:
            data = self._get(f"/api/models/{model_id}")
        except httpx.HTTPStatusError:
            return None
        return self._parse_model(data)

    def search_models(self, query: str) -> list[ModelInfo]:
        """Search models.  GET /api/models?q=..."""
        data = self._get("/api/models", {"q": query})
        return [self._parse_model(m) for m in data.get("models", [])]

    # ---- public methods (async) ----------------------------------------

    async def acheck_provider(self, slug: str) -> CheckResult:
        data = await self._aget("/api/check", {"provider": slug})
        return self._parse_check(data)

    async def acheck_model(self, model_id: str) -> CheckResult:
        data = await self._aget("/api/check", {"model": model_id})
        return self._parse_check(data)

    # ---- parsers -------------------------------------------------------

    @staticmethod
    def _parse_check(data: dict) -> CheckResult:
        return CheckResult(
            provider=data.get("provider", data.get("slug", "")),
            status=Status(data.get("status", "unknown")),
            status_detail=data.get("statusDetail"),
            model=data.get("model"),
            alternatives=[
                Alternative(
                    slug=a["slug"],
                    name=a["name"],
                    status=Status(a["status"]),
                    suggested_model=a.get("suggestedModel", ""),
                )
                for a in data.get("alternatives", [])
            ],
        )

    @staticmethod
    def _parse_model(data: dict) -> ModelInfo:
        pricing = data.get("pricing", {})
        prov = data.get("provider", {})
        return ModelInfo(
            id=data.get("id", ""),
            name=data.get("name", ""),
            provider_slug=prov.get("slug", "") if isinstance(prov, dict) else "",
            context_length=data.get("context_length", 0),
            modality=data.get("modality", "text->text"),
            prompt_price=float(pricing.get("prompt", 0)),
            completion_price=float(pricing.get("completion", 0)),
        )
