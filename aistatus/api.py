"""Thin client for the aistatus.cc public API."""

from __future__ import annotations

import httpx

from ._defaults import extract_provider_slug, normalize_provider_slug
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
        results = []
        for p in data.get("providers", []):
            ps = self._parse_provider_status(p)
            if ps:
                results.append(ps)
        return results

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
        results = []
        for m in data.get("models", []):
            mi = self._parse_model(m)
            if mi:
                results.append(mi)
        return results

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
        model = _as_string(data.get("model"))

        # Provider: try provider, slug, then extract from model
        provider = normalize_provider_slug(
            _as_string(data.get("provider"))
            or _as_string(data.get("slug"))
            or extract_provider_slug(model)
            or ""
        )

        # Status: try status, providerStatus, then available bool
        status = _parse_status(
            _as_string(data.get("status"))
            or _as_string(data.get("providerStatus"))
            or _available_to_status(data.get("available"))
        )

        # Status detail: try statusDetail, providerStatusDetail
        status_detail = (
            _as_string(data.get("statusDetail"))
            or _as_string(data.get("providerStatusDetail"))
        )

        alternatives = [
            alt for alt in (
                _parse_alternative(a) for a in data.get("alternatives", [])
            ) if alt is not None
        ]

        return CheckResult(
            provider=provider or "",
            status=status,
            status_detail=status_detail,
            model=model,
            alternatives=alternatives,
        )

    @staticmethod
    def _parse_provider_status(value: dict) -> ProviderStatus | None:
        if not isinstance(value, dict):
            return None
        return ProviderStatus(
            slug=normalize_provider_slug(_as_string(value.get("slug")) or ""),
            name=_as_string(value.get("name")) or _as_string(value.get("slug")) or "",
            status=_parse_status(_as_string(value.get("status"))),
            status_detail=_as_string(value.get("statusDetail")),
            model_count=_as_int(value.get("modelCount")),
        )

    @staticmethod
    def _parse_model(data: dict) -> ModelInfo | None:
        if not isinstance(data, dict):
            return None
        pricing = data.get("pricing", {})
        if not isinstance(pricing, dict):
            pricing = {}
        prov = data.get("provider", {})
        if not isinstance(prov, dict):
            prov = {}

        return ModelInfo(
            id=_as_string(data.get("id")) or "",
            name=_as_string(data.get("name")) or "",
            provider_slug=normalize_provider_slug(
                _as_string(prov.get("slug"))
                or extract_provider_slug(_as_string(data.get("id")) or "")
                or ""
            ),
            context_length=_as_int(data.get("context_length")),
            modality=_as_string(data.get("modality")) or "text->text",
            prompt_price=_as_float(pricing.get("prompt")),
            completion_price=_as_float(pricing.get("completion")),
        )


# ---- module-level helpers -----------------------------------------------

def _parse_alternative(value: dict) -> Alternative | None:
    if not isinstance(value, dict):
        return None

    suggested_model = (
        _as_string(value.get("suggestedModel"))
        or _as_string(value.get("model"))
        or _as_string(value.get("id"))
        or ""
    )
    slug = normalize_provider_slug(
        _as_string(value.get("slug"))
        or _as_string(value.get("provider"))
        or extract_provider_slug(suggested_model)
        or ""
    )

    return Alternative(
        slug=slug,
        name=_as_string(value.get("name")) or slug,
        status=_parse_status(
            _as_string(value.get("status"))
            or _as_string(value.get("providerStatus"))
            or _available_to_status(value.get("available"))
        ),
        suggested_model=suggested_model,
    )


def _available_to_status(value) -> str | None:
    if value is True:
        return Status.OPERATIONAL.value
    if value is False:
        return Status.DOWN.value
    return None


def _parse_status(value: str | None) -> Status:
    if value == Status.OPERATIONAL.value:
        return Status.OPERATIONAL
    if value == Status.DEGRADED.value:
        return Status.DEGRADED
    if value == Status.DOWN.value:
        return Status.DOWN
    return Status.UNKNOWN


def _as_string(value) -> str | None:
    return value if isinstance(value, str) else None


def _as_int(value) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value))
        except (ValueError, TypeError):
            pass
    return 0


def _as_float(value) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except (ValueError, TypeError):
            pass
    return 0.0
