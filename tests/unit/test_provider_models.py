"""Unit coverage for typed Provider configuration and normalized output data."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.usage import RequestUsage, RunUsage

from agentcell.config import AgentCellSettings
from agentcell.providers import (
    BailianModelSpec,
    DeepSeekModelSpec,
    ModelCompleted,
    ModelTextDelta,
    ModelToolCall,
    ModelUsage,
    model_response_events,
)


def test_project_toml_loads_provider_specific_specs() -> None:
    settings = AgentCellSettings.from_toml(Path("agentcell.toml"))

    qwen = settings.models["qwen_plus"]
    deepseek = settings.models["deepseek_pro"]
    assert isinstance(qwen, BailianModelSpec)
    assert qwen.thinking_budget == 12_000
    assert isinstance(deepseek, DeepSeekModelSpec)
    assert deepseek.reasoning_effort == "high"


def test_provider_config_stores_environment_name_not_secret_value() -> None:
    spec = BailianModelSpec(
        model="qwen3.7-plus",
        api_key_env="DASHSCOPE_API_KEY",
    )

    dumped = spec.model_dump_json()
    assert "DASHSCOPE_API_KEY" in dumped
    assert "sk-super-secret" not in dumped


def test_vendor_thinking_options_are_validated() -> None:
    with pytest.raises(ValidationError, match="thinking_budget requires thinking=true"):
        BailianModelSpec(
            model="qwen3.7-plus",
            api_key_env="DASHSCOPE_API_KEY",
            thinking=False,
            thinking_budget=100,
        )

    with pytest.raises(ValidationError, match="does not support temperature"):
        DeepSeekModelSpec(
            model="deepseek-v4-pro",
            api_key_env="DEEPSEEK_API_KEY",
            thinking=True,
            temperature=0.2,
        )

    with pytest.raises(ValidationError, match="URL scheme should be 'https'"):
        BailianModelSpec.model_validate(
            {
                "model": "qwen3.7-plus",
                "api_key_env": "DASHSCOPE_API_KEY",
                "base_url": "http://insecure.example.com/v1",
            }
        )


def test_usage_normalizes_request_and_run_counters() -> None:
    request = ModelUsage.from_pydantic_ai(
        RequestUsage(input_tokens=3, cache_read_tokens=2, output_tokens=5)
    )
    run = ModelUsage.from_pydantic_ai(
        RunUsage(requests=2, tool_calls=1, input_tokens=7, output_tokens=11)
    )

    assert request.total_tokens == 8
    assert request.requests == 0
    assert run.requests == 2
    assert run.tool_calls == 1
    assert run.total_tokens == 18


def test_complete_response_is_mapped_to_normalized_events() -> None:
    response = ModelResponse(
        parts=[
            TextPart("hello"),
            ToolCallPart("lookup", {"query": "AgentCell"}, "call-1"),
        ],
        usage=RequestUsage(input_tokens=2, output_tokens=3),
        finish_reason="tool_call",
    )

    events = model_response_events(response)

    assert events == (
        ModelTextDelta(delta="hello"),
        ModelToolCall(
            tool_name="lookup",
            arguments={"query": "AgentCell"},
            tool_call_id="call-1",
        ),
        ModelCompleted(
            usage=ModelUsage(input_tokens=2, output_tokens=3),
            finish_reason="tool_call",
        ),
    )
