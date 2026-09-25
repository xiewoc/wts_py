"""Shared HTTPS transport for the fixed, trusted endpoints of the core.

Every request keeps the three properties of the Rust core:

* redirects are followed manually and each hop is validated by the caller, so a
  downgrade to plain HTTP, a foreign host or embedded user information stops the
  request instead of being transparently followed;
* bodies are read in chunks with a hard byte ceiling, so a hostile or broken
  upstream cannot exhaust memory;
* no client is ever built with credential retention, and nothing is persisted.
"""

from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

import httpx

from .errors import ServiceError

JSON_ACCEPT = "application/json, text/plain, */*"

RedirectValidator = Callable[[httpx.URL, int], None]
ResponseReader = Callable[[httpx.Response], Awaitable[Any]]


def public_client(
    timeout_seconds: float, connect_timeout_seconds: float | None = None
) -> httpx.AsyncClient:
    """Build a client that never follows a redirect on its own."""
    timeout = (
        httpx.Timeout(timeout_seconds)
        if connect_timeout_seconds is None
        else httpx.Timeout(timeout_seconds, connect=connect_timeout_seconds)
    )
    return httpx.AsyncClient(timeout=timeout, follow_redirects=False)


async def send_with_redirects(
    client: httpx.AsyncClient,
    method: str,
    url: str | httpx.URL,
    *,
    max_redirects: int,
    validate: RedirectValidator,
    **kwargs: Any,
) -> httpx.Response:
    """Send a request, following at most ``max_redirects`` validated hops."""
    current = httpx.URL(str(url))
    for hop in range(max_redirects + 1):
        request = client.build_request(method, current, **kwargs)
        response = await client.send(request, follow_redirects=False)
        if not response.is_redirect:
            return response
        location = response.headers.get("location")
        status = response.status_code
        await response.aclose()
        if not location:
            raise ServiceError(f"重定向响应缺少 Location 头（HTTP {status}）。")
        target = current.join(location)
        validate(target, hop)
        current = target
    raise ServiceError("重定向次数过多。")


def reject_redirect(response: httpx.Response, label: str) -> None:
    """Fail a request whose upstream answered with a redirect."""
    if response.is_redirect:
        raise ServiceError(f"{label}返回了不受信任的重定向。")


async def read_limited(
    response: httpx.Response, maximum_bytes: int, label: str
) -> bytes:
    """Read a response body, refusing anything above ``maximum_bytes``."""
    declared = response.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > maximum_bytes:
                raise ServiceError(f"{label}响应过大。")
        except ValueError:
            pass
    body = bytearray()
    async for chunk in response.aiter_bytes():
        if len(chunk) > maximum_bytes - len(body):
            raise ServiceError(f"{label}响应过大。")
        body.extend(chunk)
    return bytes(body)


def parse_json(body: bytes, label: str) -> Any:
    try:
        return json.loads(body)
    except (ValueError, UnicodeDecodeError) as error:
        raise ServiceError(f"{label}返回了无法识别的数据：{error}") from error


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ServiceError(message)
