"""Approval-gated HTTPS tool with DNS pinning, redirect checks, and response bounds."""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import re
import socket
from typing import Protocol, cast
from urllib.parse import parse_qsl, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agentcell.errors import (
    HttpRequestDeniedError,
    HttpResponseTooLargeError,
    HttpToolError,
)
from agentcell.events import JsonValue
from agentcell.policy import Capability, RiskLevel, ToolPolicy
from agentcell.tools.models import ToolDefinition, ToolExecutionContext
from agentcell.tools.registry import ToolRegistry

_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"})
_REDIRECTS = frozenset({301, 302, 303, 307, 308})
_FORBIDDEN_REQUEST_HEADERS = frozenset(
    {
        "authorization",
        "cookie",
        "host",
        "proxy-authorization",
        "proxy-connection",
        "x-api-key",
        "connection",
        "transfer-encoding",
    }
)
_SENSITIVE_QUERY_NAMES = frozenset(
    {"api_key", "apikey", "key", "password", "secret", "signature", "token"}
)
_RESPONSE_HEADERS = frozenset(
    {"content-type", "content-length", "etag", "last-modified", "cache-control"}
)
_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")


class HostResolver(Protocol):
    async def resolve(self, host: str, port: int) -> tuple[str, ...]: ...


class _NetworkStream(Protocol):
    def get_extra_info(self, info: str) -> object: ...


class SystemHostResolver:
    async def resolve(self, host: str, port: int) -> tuple[str, ...]:
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            records = await asyncio.to_thread(
                socket.getaddrinfo,
                host,
                port,
                socket.AF_UNSPEC,
                socket.SOCK_STREAM,
            )
            return tuple(sorted({str(record[4][0]) for record in records}))
        return (str(address),)


class HttpRequestParams(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    method: str = Field(default="GET")
    url: str = Field(min_length=1, max_length=4096)
    headers: dict[str, str] = Field(default_factory=dict, max_length=32)
    json_body: JsonValue | None = None
    text_body: str | None = Field(default=None, max_length=1_048_576)
    max_response_bytes: int = Field(
        default=1024 * 1024,
        ge=1024,
        le=4 * 1024 * 1024,
        strict=True,
    )
    max_redirects: int = Field(default=3, ge=0, le=5, strict=True)

    @field_validator("method")
    @classmethod
    def normalize_method(cls, value: str) -> str:
        method = value.strip().upper()
        if method not in _METHODS:
            raise ValueError("HTTP method is not supported")
        return method

    @field_validator("headers")
    @classmethod
    def validate_headers(cls, value: dict[str, str]) -> dict[str, str]:
        normalized: dict[str, str] = {}
        for name, header_value in value.items():
            lowered = name.strip().casefold()
            if (
                not lowered
                or not _HEADER_NAME_RE.fullmatch(lowered)
                or lowered in _FORBIDDEN_REQUEST_HEADERS
                or len(lowered) > 128
                or len(header_value) > 4096
                or "\r" in header_value
                or "\n" in header_value
            ):
                raise ValueError("HTTP header is forbidden or invalid")
            normalized[lowered] = header_value
        return normalized

    @model_validator(mode="after")
    def validate_body(self) -> HttpRequestParams:
        if self.json_body is not None and self.text_body is not None:
            raise ValueError("HTTP request accepts only one body representation")
        if self.method in {"GET", "HEAD"} and (
            self.json_body is not None or self.text_body is not None
        ):
            raise ValueError("GET and HEAD requests cannot include a body")
        return self


class HttpResponseResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    url: str
    status_code: int = Field(ge=100, le=599)
    headers: dict[str, str]
    body: str
    body_encoding: str
    response_bytes: int = Field(ge=0)
    redirects_followed: int = Field(ge=0, le=5)


class HttpRequestHandler:
    def __init__(
        self,
        *,
        resolver: HostResolver | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._resolver = resolver or SystemHostResolver()
        self._transport = transport

    async def __call__(
        self,
        params: HttpRequestParams,
        context: ToolExecutionContext,
    ) -> HttpResponseResult:
        current = httpx.URL(params.url)
        method = params.method
        body, headers = _request_content(params)
        redirects = 0
        async with httpx.AsyncClient(
            transport=self._transport,
            follow_redirects=False,
            timeout=httpx.Timeout(30),
            trust_env=False,
        ) as client:
            while True:
                original_host, pinned_url, addresses = await self._validate_and_pin(
                    current,
                    context.lease.network_domains,
                )
                request_headers = dict(headers)
                request_headers["host"] = original_host
                request = client.build_request(
                    method,
                    pinned_url,
                    headers=request_headers,
                    content=body,
                )
                request.extensions["sni_hostname"] = current.host.encode("idna")
                response = await client.send(request, stream=True)
                try:
                    _verify_peer_if_available(response, addresses)
                    if response.status_code in _REDIRECTS:
                        location = response.headers.get("location")
                        if location is None:
                            raise HttpToolError("Redirect response omitted Location")
                        if redirects >= params.max_redirects:
                            raise HttpRequestDeniedError("redirect limit exceeded")
                        current = current.join(location)
                        redirects += 1
                        if response.status_code == 303 or (
                            response.status_code in {301, 302} and method == "POST"
                        ):
                            method = "GET"
                            body = None
                            headers.pop("content-type", None)
                        continue
                    content = await _read_response(response, params.max_response_bytes)
                    return _response_result(response, current, content, redirects)
                finally:
                    await response.aclose()

    async def _validate_and_pin(
        self,
        url: httpx.URL,
        allowed_domains: tuple[str, ...],
    ) -> tuple[str, httpx.URL, tuple[str, ...]]:
        parsed = urlsplit(str(url))
        if parsed.scheme.casefold() != "https":
            raise HttpRequestDeniedError("only HTTPS is allowed")
        if parsed.username is not None or parsed.password is not None:
            raise HttpRequestDeniedError("URL credentials are forbidden")
        host = parsed.hostname
        if host is None:
            raise HttpRequestDeniedError("URL host is missing")
        host = host.encode("idna").decode("ascii").casefold().rstrip(".")
        if parsed.port not in {None, 443}:
            raise HttpRequestDeniedError("only HTTPS port 443 is allowed")
        if not any(host == domain or host.endswith(f".{domain}") for domain in allowed_domains):
            raise HttpRequestDeniedError("host is outside the network lease")
        if any(name.casefold() in _SENSITIVE_QUERY_NAMES for name, _ in parse_qsl(parsed.query)):
            raise HttpRequestDeniedError("sensitive query parameters are forbidden")
        try:
            addresses = await self._resolver.resolve(host, 443)
        except OSError as error:
            raise HttpToolError("DNS resolution failed") from error
        if not addresses:
            raise HttpToolError("DNS resolution returned no addresses")
        for value in addresses:
            _require_public_address(value)
        selected = addresses[0]
        host_header = host
        pinned = url.copy_with(host=selected, port=None)
        return host_header, pinned, addresses


def _request_content(params: HttpRequestParams) -> tuple[bytes | None, dict[str, str]]:
    headers = dict(params.headers)
    if params.json_body is not None:
        body = json.dumps(params.json_body, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        headers.setdefault("content-type", "application/json")
    elif params.text_body is not None:
        body = params.text_body.encode("utf-8")
        headers.setdefault("content-type", "text/plain; charset=utf-8")
    else:
        body = None
    if body is not None and len(body) > 1024 * 1024:
        raise HttpRequestDeniedError("request body exceeds 1 MiB")
    return body, headers


async def _read_response(response: httpx.Response, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    used = 0
    async for chunk in response.aiter_bytes():
        used += len(chunk)
        if used > max_bytes:
            raise HttpResponseTooLargeError(max_bytes)
        chunks.append(chunk)
    return b"".join(chunks)


def _response_result(
    response: httpx.Response,
    original_url: httpx.URL,
    content: bytes,
    redirects: int,
) -> HttpResponseResult:
    media_type = response.headers.get("content-type", "").casefold()
    textual = (
        media_type.startswith("text/")
        or "json" in media_type
        or "xml" in media_type
        or "javascript" in media_type
    )
    if textual:
        body = content.decode("utf-8", errors="replace")
        encoding = "utf-8"
    else:
        body = base64.b64encode(content).decode("ascii")
        encoding = "base64"
    safe_headers = {
        name.casefold(): value
        for name, value in response.headers.items()
        if name.casefold() in _RESPONSE_HEADERS
    }
    return HttpResponseResult(
        url=str(original_url),
        status_code=response.status_code,
        headers=safe_headers,
        body=body,
        body_encoding=encoding,
        response_bytes=len(content),
        redirects_followed=redirects,
    )


def _require_public_address(value: str) -> None:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as error:
        raise HttpRequestDeniedError("DNS returned an invalid address") from error
    if not address.is_global or address.is_multicast:
        raise HttpRequestDeniedError("DNS resolved to a non-public address")


def _verify_peer_if_available(response: httpx.Response, addresses: tuple[str, ...]) -> None:
    stream = response.extensions.get("network_stream")
    if stream is None or not hasattr(stream, "get_extra_info"):
        return
    peer = cast(_NetworkStream, stream).get_extra_info("server_addr")
    if isinstance(peer, tuple) and peer:
        peer_items = cast(tuple[object, ...], peer)
        peer_address = str(peer_items[0])
        _require_public_address(peer_address)
        if peer_address not in addresses:
            raise HttpRequestDeniedError("connected peer differs from pinned DNS result")


def register_http_tools(
    registry: ToolRegistry,
    *,
    resolver: HostResolver | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    """Register a conservative request tool; every method requires per-Run approval."""

    registry.register(
        ToolDefinition(
            name="http.request",
            description=(
                "Send one bounded HTTPS request after domain, DNS, redirect, and user approval."
            ),
            params_model=HttpRequestParams,
            policy=ToolPolicy(
                risk=RiskLevel.GUARDED,
                requires_approval=True,
                idempotent=False,
                timeout_seconds=60,
                max_output_bytes=64 * 1024,
                capabilities=frozenset({Capability.NETWORK_REQUEST}),
            ),
            handler=HttpRequestHandler(resolver=resolver, transport=transport),
        )
    )
