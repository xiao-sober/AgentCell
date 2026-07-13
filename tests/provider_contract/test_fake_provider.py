"""Deterministic Fake Provider contract for text, stream, tools, usage, and failures."""

from __future__ import annotations

import pytest
from pydantic_ai import Agent

from agentcell.errors import (
    ProviderAuthenticationError,
    ProviderConnectionError,
    ProviderContextLimitError,
    ProviderError,
    ProviderPermissionError,
    ProviderProtocolError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUpstreamError,
)
from agentcell.providers import (
    FakeFailureKind,
    FakeFailureStep,
    FakeModelSpec,
    FakeProviderAdapter,
    FakeScript,
    FakeTextStep,
    FakeToolCallStep,
    ModelUsage,
    ProviderFactory,
)


def _factory(script: FakeScript) -> ProviderFactory:
    spec = FakeModelSpec(model="contract-script")
    adapter = FakeProviderAdapter({spec.model: script})
    return ProviderFactory({"test": spec}, adapters=[adapter])


@pytest.mark.asyncio
async def test_fake_text_and_usage_are_deterministic() -> None:
    script = FakeScript(
        steps=(
            FakeTextStep(
                text="deterministic answer",
                usage=ModelUsage(input_tokens=4, output_tokens=2),
            ),
        )
    )

    async with _factory(script) as factory:
        result = await Agent(await factory.build_model("test")).run("question")

    assert result.output == "deterministic answer"
    assert result.usage.input_tokens == 4
    assert result.usage.output_tokens == 2


@pytest.mark.asyncio
async def test_each_fake_model_build_receives_a_fresh_script_cursor() -> None:
    script = FakeScript(steps=(FakeTextStep(text="repeatable"),))

    async with _factory(script) as factory:
        first_model = await factory.build_model("test")
        second_model = await factory.build_model("test")
        first = await Agent(first_model).run("one")
        second = await Agent(second_model).run("two")

    assert first_model is not second_model
    assert first.output == second.output == "repeatable"


@pytest.mark.asyncio
async def test_fake_stream_preserves_scripted_chunk_order() -> None:
    script = FakeScript(steps=(FakeTextStep(text="AgentCell", chunks=("Agent", "Cell")),))

    async with _factory(script) as factory:
        agent = Agent(await factory.build_model("test"))
        async with agent.run_stream("question") as result:
            chunks = [chunk async for chunk in result.stream_text(delta=True, debounce_by=None)]

    assert chunks == ["Agent", "Cell"]


@pytest.mark.asyncio
async def test_fake_supports_tool_call_and_multi_turn_result() -> None:
    script = FakeScript(
        steps=(
            FakeToolCallStep(
                tool_name="add",
                arguments={"left": 2, "right": 3},
                usage=ModelUsage(input_tokens=2, output_tokens=1),
            ),
            FakeTextStep(
                text="5",
                usage=ModelUsage(input_tokens=3, output_tokens=1),
            ),
        )
    )

    def add(left: int, right: int) -> int:
        return left + right

    async with _factory(script) as factory:
        agent = Agent(await factory.build_model("test"), tools=[add])
        result = await agent.run("add the numbers")

    assert result.output == "5"
    assert result.usage.requests == 2
    assert result.usage.tool_calls == 1
    assert result.usage.input_tokens == 5
    assert result.usage.output_tokens == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "expected_type", "retryable"),
    [
        (FakeFailureKind.AUTHENTICATION, ProviderAuthenticationError, False),
        (FakeFailureKind.PERMISSION, ProviderPermissionError, False),
        (FakeFailureKind.RATE_LIMIT, ProviderRateLimitError, True),
        (FakeFailureKind.TIMEOUT, ProviderTimeoutError, True),
        (FakeFailureKind.CONNECTION, ProviderConnectionError, True),
        (FakeFailureKind.CONTEXT_LIMIT, ProviderContextLimitError, False),
        (FakeFailureKind.UPSTREAM, ProviderUpstreamError, True),
        (FakeFailureKind.PROTOCOL, ProviderProtocolError, False),
    ],
)
async def test_fake_injects_classified_failures(
    kind: FakeFailureKind,
    expected_type: type[ProviderError],
    retryable: bool,
) -> None:
    script = FakeScript(steps=(FakeFailureStep(failure=kind),))

    async with _factory(script) as factory:
        agent = Agent(await factory.build_model("test"))
        with pytest.raises(expected_type) as caught:
            await agent.run("question")

    assert caught.value.retryable is retryable
