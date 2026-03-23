"""Exceptions for aistatus SDK."""


class AIStatusError(Exception):
    """Base exception for all aistatus errors."""


class AllProvidersDown(AIStatusError):
    """Raised when no provider in the routing chain is available."""

    def __init__(self, tried: list[str]):
        self.tried = tried
        super().__init__(
            f"All providers unavailable. Tried: {', '.join(tried)}. "
            f"Check https://aistatus.cc for current status."
        )


class ProviderCallFailed(AIStatusError):
    """Raised when a provider API call fails (after status was operational)."""

    def __init__(self, provider: str, model: str, cause: Exception):
        self.provider = provider
        self.model = model
        self.cause = cause
        super().__init__(f"{provider} ({model}) call failed: {cause}")


class NoBudgetMatch(AIStatusError):
    """Raised when no available model fits the budget constraint."""

    def __init__(self, max_cost: float, tier: str):
        self.max_cost = max_cost
        super().__init__(
            f"No operational model in tier '{tier}' under ${max_cost}/M tokens."
        )


class ProviderNotInstalled(AIStatusError):
    """Raised when the required provider SDK is not installed."""

    def __init__(self, provider: str, package: str):
        self.provider = provider
        self.package = package
        super().__init__(
            f"Provider '{provider}' requires package '{package}'. "
            f"Install with: pip install aistatus[{provider}]"
        )


class ProviderNotConfigured(AIStatusError):
    """Raised when a provider's API key is not configured."""

    def __init__(self, provider: str, env_name: str | None = None):
        self.provider = provider
        self.env_name = env_name
        msg = f"Provider '{provider}' is not configured."
        if env_name:
            msg += f" Set the {env_name} environment variable."
        super().__init__(msg)


class CheckAPIUnreachable(AIStatusError):
    """Raised when aistatus.cc API itself is unreachable."""

    def __init__(self):
        super().__init__(
            "Could not reach aistatus.cc API. "
            "Proceeding with primary provider without status check."
        )
