"""Per-request budget and event instrumentation around a PydanticAI Model."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any, cast

from pydantic_ai import RunContext
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models import Model, ModelRequestParameters, StreamedResponse
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import RequestUsage

from agentcell.budgets import BudgetTracker
from agentcell.errors import ProviderError
from agentcell.events import (
    ErrorPayload,
    EventType,
    GenericEventPayload,
    JsonValue,
    ModelCompletedPayload,
    ModelRequestedPayload,
)
from agentcell.memory.compaction import ContextManager
from agentcell.providers import classify_provider_error
from agentcell.tools import ToolEventSink


class RunModel(WrapperModel):
    """Reserve and record every model request, including tool-loop follow-up calls."""

    def __init__(
        self,
        wrapped: Model,
        *,
        provider: str,
        model_name: str,
        budget: BudgetTracker,
        events: ToolEventSink,
        context_manager: ContextManager | None = None,
    ) -> None:
        super().__init__(wrapped)
        self._provider_name = provider
        self._configured_model_name = model_name
        self._budget = budget
        self._events = events
        self._context_manager = context_manager
        self._request_index = 0

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        messages = await self._prepare_messages(messages)
        await self._before_request()
        try:
            response = await self.wrapped.request(
                messages,
                model_settings,
                model_request_parameters,
            )
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            raise await self._record_failure(error) from error
        await self._record_completion(response.usage)
        return response

    @asynccontextmanager
    async def request_stream(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        run_context: RunContext[Any] | None = None,
    ) -> AsyncGenerator[StreamedResponse]:
        messages = await self._prepare_messages(messages)
        await self._before_request()
        try:
            async with self.wrapped.request_stream(
                messages,
                model_settings,
                model_request_parameters,
                run_context,
            ) as response_stream:
                yield response_stream
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            raise await self._record_failure(error) from error
        await self._record_completion(response_stream.usage)

    async def _before_request(self) -> None:
        usage = self._budget.reserve_model_request()
        self._request_index += 1
        await self._emit_budget(
            "model_request_reserved",
            cast(dict[str, JsonValue], usage.model_dump(mode="json")),
        )
        await self._events.emit(
            EventType.MODEL_REQUESTED,
            ModelRequestedPayload(
                provider=self._provider_name,
                model=self._configured_model_name,
                request_index=self._request_index,
            ),
        )

    async def _prepare_messages(self, messages: list[ModelMessage]) -> list[ModelMessage]:
        if self._context_manager is None:
            return messages
        return await self._context_manager.prepare(messages, events=self._events)

    async def _record_completion(self, usage: RequestUsage) -> None:
        recorded = self._budget.record_model_usage(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_write_tokens=usage.cache_write_tokens,
            cache_read_tokens=usage.cache_read_tokens,
        )
        await self._events.emit(
            EventType.MODEL_COMPLETED,
            ModelCompletedPayload(
                provider=self._provider_name,
                model=self._configured_model_name,
                request_index=self._request_index,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_write_tokens=usage.cache_write_tokens,
                cache_read_tokens=usage.cache_read_tokens,
            ),
        )
        await self._emit_budget(
            "model_usage_recorded",
            cast(dict[str, JsonValue], recorded.model_dump(mode="json")),
        )

    async def _record_failure(self, error: BaseException) -> ProviderError:
        classified = classify_provider_error(
            self._provider_name,
            self._configured_model_name,
            error,
        )
        await self._events.emit(
            EventType.MODEL_FAILED,
            ErrorPayload(
                code=classified.code,
                message=str(classified),
                retryable=classified.retryable,
            ),
        )
        return classified

    async def _emit_budget(self, source: str, usage: dict[str, JsonValue]) -> None:
        await self._events.emit(
            EventType.BUDGET_UPDATED,
            GenericEventPayload(
                data={
                    "source": source,
                    "request_index": self._request_index,
                    "usage": usage,
                }
            ),
        )
