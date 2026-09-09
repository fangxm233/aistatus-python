# input:  a request path and the gateway's mode-aware endpoint table
# output: the endpoint, upstream path, billing mode and metadata the request routes to
# pos:    Gateway URL routing, shared by HTTP proxying and WebSocket upgrades
# >>> 一旦我被更新，务必更新我的开头注释与所属文件夹 CLAUDE.md <<<

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import unquote

from .config import EndpointConfig


@dataclass
class ProxyRoute:
    """Where a request path points.

    ``kind`` is ``route`` when it resolved, or ``unknown-mode`` / ``not-found`` when it did not —
    kept apart because they are different status codes to the caller.
    """

    kind: str
    ep_name: str = ""
    path: str = ""
    mode: str = ""
    metadata: dict[str, str] | None = None


def parse_url_metadata(segment: str) -> dict[str, str]:
    """Parse a `key=value,key=value` URL segment. Malformed pairs are skipped, not fatal."""
    metadata: dict[str, str] = {}
    for pair in segment.split(","):
        index = pair.find("=")
        if index > 0:
            metadata[unquote(pair[:index])] = unquote(pair[index + 1 :])
    return metadata


def resolve_proxy_route(
    pathname: str, endpoint_modes: dict[str, dict[str, EndpointConfig]]
) -> ProxyRoute:
    """Resolve a request path against the endpoint table, without touching request state.

    Three shapes are accepted::

        /m/{mode}/{metadata}/{endpoint}/{path}
        /m/{mode}/{endpoint}/{path}
        /{endpoint}/{path}

    The four-segment form is ambiguous with the three-segment one — `/m/plan/anthropic/v1/messages`
    could read either way — so it is only taken when its third segment really names an endpoint in
    that mode.

    This is a pure function so that HTTP requests and WebSocket upgrades resolve identically;
    duplicating the logic is how the two paths drift apart on mode and metadata attribution.
    """
    pathname = pathname.lstrip("/")

    if pathname.startswith("m/"):
        parts = pathname[2:].split("/", 3)
        if len(parts) < 3:
            return ProxyRoute(kind="not-found")

        mode = parts[0]
        mode_endpoints = endpoint_modes.get(mode)
        if not mode_endpoints:
            return ProxyRoute(kind="unknown-mode", mode=mode)

        if len(parts) >= 4 and parts[2] in mode_endpoints:
            return ProxyRoute(
                kind="route", ep_name=parts[2], path=parts[3], mode=mode,
                metadata=parse_url_metadata(parts[1]),
            )

        return ProxyRoute(kind="route", ep_name=parts[1], path="/".join(parts[2:]), mode=mode)

    ep_name, _, path = pathname.partition("/")
    if not ep_name:
        return ProxyRoute(kind="not-found")
    return ProxyRoute(kind="route", ep_name=ep_name, path=path)
