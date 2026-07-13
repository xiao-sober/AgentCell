"""Registry-based ProviderFactory with explicit model and HTTP client lifecycles."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping
from types import TracebackType

import httpx
from pydantic_ai.models import Model

from agentcell.errors import ProviderConfigurationError
from agentcell.providers.bailian import BailianProviderAdapter
from agentcell.providers.base import (
    EnvironmentSecretResolver,
    ProviderAdapter,
    SecretResolver,
    create_provider_http_client,
)
from agentcell.providers.deepseek import DeepSeekProviderAdapter
from agentcell.providers.fake import FakeProviderAdapter
from agentcell.providers.models import ModelSpec, NetworkModelSpec, ProviderName


class ProviderFactory:
    """Resolve model references through adapters without vendor conditionals in callers."""

    def __init__(
        self,
        models: Mapping[str, ModelSpec[ProviderName]],
        *,
        adapters: Iterable[ProviderAdapter] | None = None,
        secret_resolver: SecretResolver | None = None,
    ) -> None:
        self._models = dict(models)
        selected_adapters = tuple(adapters) if adapters is not None else _default_adapters()
        self._adapters: dict[ProviderName, ProviderAdapter] = {}
        for adapter in selected_adapters:
            if adapter.provider in self._adapters:
                raise ProviderConfigurationError(
                    f"Provider adapter {adapter.provider!r} is registered more than once"
                )
            self._adapters[adapter.provider] = adapter

        self._secret_resolver = secret_resolver or EnvironmentSecretResolver()
        self._cache: dict[str, Model] = {}
        self._owned_clients: list[httpx.AsyncClient] = []
        self._lock = asyncio.Lock()
        self._closed = False

    def model_spec(self, model_ref: str) -> ModelSpec[ProviderName]:
        """Return the validated specification behind a stable reference."""

        try:
            return self._models[model_ref]
        except KeyError as error:
            raise ProviderConfigurationError(f"Unknown model reference {model_ref!r}") from error

    async def build_model(
        self,
        model_ref: str,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> Model:
        """Build or reuse a model; injected clients remain owned by the caller."""

        async with self._lock:
            if self._closed:
                raise ProviderConfigurationError("ProviderFactory is closed")
            spec = self.model_spec(model_ref)
            try:
                adapter = self._adapters[spec.provider]
            except KeyError as error:
                raise ProviderConfigurationError(
                    f"No adapter is registered for Provider {spec.provider!r}",
                    provider=spec.provider,
                    model=spec.model,
                ) from error
            if http_client is None and adapter.cacheable and model_ref in self._cache:
                return self._cache[model_ref]

            api_key = self._resolve_api_key(adapter, spec)
            client = http_client
            owns_client = False
            if adapter.uses_http_client and client is None:
                if not isinstance(spec, NetworkModelSpec):
                    raise ProviderConfigurationError(
                        "Network Provider requires NetworkModelSpec",
                        provider=spec.provider,
                        model=spec.model,
                    )
                proxy = (
                    self._secret_resolver.resolve(spec.http.proxy_env)
                    if spec.http.proxy_env is not None
                    else None
                )
                client = create_provider_http_client(spec, proxy=proxy)
                owns_client = True

            try:
                model = adapter.build_model(spec, api_key=api_key, http_client=client)
            except BaseException:
                if owns_client and client is not None:
                    await client.aclose()
                raise

            if http_client is None and adapter.cacheable:
                self._cache[model_ref] = model
            if owns_client and client is not None:
                self._owned_clients.append(client)
            return model

    async def __aenter__(self) -> ProviderFactory:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        await self.aclose()

    async def aclose(self) -> None:
        """Close only clients created by this factory; safe to call repeatedly."""

        async with self._lock:
            if self._closed:
                return
            self._closed = True
            clients = tuple(self._owned_clients)
            self._owned_clients.clear()
            self._cache.clear()
        for client in clients:
            await client.aclose()

    def _resolve_api_key(
        self, adapter: ProviderAdapter, spec: ModelSpec[ProviderName]
    ) -> str | None:
        if not adapter.requires_api_key:
            return None
        if not isinstance(spec, NetworkModelSpec):
            raise ProviderConfigurationError(
                "Provider requires an API key environment variable",
                provider=spec.provider,
                model=spec.model,
            )
        return self._secret_resolver.resolve(spec.api_key_env)


def _default_adapters() -> tuple[ProviderAdapter, ...]:
    return (
        BailianProviderAdapter(),
        DeepSeekProviderAdapter(),
        FakeProviderAdapter(),
    )
