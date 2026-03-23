"""Gateway authentication: pure functions for API key validation."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class GatewayAuthConfig:
    """Configuration for gateway API key authentication."""
    enabled: bool = False
    keys: list[str] = field(default_factory=list)
    header: str = "authorization"
    public_paths: list[str] = field(default_factory=lambda: ["/health"])


def check_gateway_auth(
    auth_config: GatewayAuthConfig | None,
    pathname: str,
    headers: dict[str, str | list[str] | None],
) -> bool:
    """Check whether a request is authorized against the gateway auth config.

    Returns True if the request should be allowed through.
    """
    if not auth_config or not auth_config.enabled:
        return True

    # Check public paths
    public_paths = auth_config.public_paths
    for p in public_paths:
        if pathname == p or pathname.startswith(p + "/"):
            return True

    # Extract key from request
    header_name = (auth_config.header or "authorization").lower()
    raw_value = headers.get(header_name)
    if isinstance(raw_value, list):
        header_value = raw_value[0] if raw_value else ""
    else:
        header_value = raw_value or ""

    if header_name == "authorization":
        # Bearer scheme
        if header_value.lower().startswith("bearer "):
            provided_key = header_value[7:].strip()
        else:
            provided_key = header_value.strip()
    else:
        provided_key = header_value.strip()

    if not provided_key:
        return False

    # Check against configured keys
    return any(key == provided_key for key in auth_config.keys)
