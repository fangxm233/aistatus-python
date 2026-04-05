# input: httpx sync/async clients plus provider/model query parameters
# output: cached public aistatus.cc API lookups for provider status, model checks, and search results
# pos: thin SDK HTTP client for status/model metadata with reusable sync and async transports
# >>> 一旦我被更新，务必更新我的开头注释，以及所属文件夹的 CLAUDE.md <<<

from __future__ import annotations

import atexit

import httpx

from ._defaults import extract_provider_slug, normalize_provider_slug
from .models import Alternative, CheckResult, ModelInfo, ProviderStatus, Status

BASE_URL = "https://aistatus.cc"


class StatusAPI:
    """Cached HTTP client for aistatus.cc."""

    def __init__(self, base_url: str = BASE_URL, timeout: float = 3.0):
        self._base = base_url.rstrip("/")
        self._timeout = timeout
        self._client: httpx.Client | None = None
        self._async_client: httpx.AsyncClient | None = None
        self._shutdown_registered = False

    def _get_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self._timeout)
        return self._client

    def _get_async_client(self) -> httpx.AsyncClient:
        if self._async_client is None:
            self._async_client = httpx.AsyncClient(timeout=self._timeout)
        if not self._shutdown_registered:
            atexit.register(self.close)
            self._shutdown_registered = True
        return self._async_client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    async def aclose(self) -> None:
        self.close()
        if self._async_client is not None:
            await self._async_client.aclose()
            self._async_client = None

    # ---- sync helpers ------------------------------------------------

    def _get(self, path: str, params: dict | None = None) -> dict:
        r = self._get_client().get(f"{self._base}{path}", params=params)
        r.raise_for_status()
        return r.json()

    async def _aget(self, path: str, params: dict | None = None) -> dict:
        r = await self._get_async_client().get(f"{self._base}{path}", params=params)
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
