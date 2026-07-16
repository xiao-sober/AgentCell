"""PydanticAI Agent construction from stateless AgentCell declarations."""

from __future__ import annotations

from collections.abc import Sequence

from pydantic_ai import Agent, Tool
from pydantic_ai.models import Model
from pydantic_ai.output import OutputSpec

from agentcell.agents.models import AgentSpec
from agentcell.providers import ProviderFactory


class AgentFactory:
    """Build fresh PydanticAI Agents while ProviderFactory owns model lifecycles."""

    def __init__(self, providers: ProviderFactory) -> None:
        self._providers = providers

    async def create[DepsT, OutputT](
        self,
        spec: AgentSpec,
        *,
        deps_type: type[DepsT],
        output_type: OutputSpec[OutputT],
        tools: Sequence[Tool[DepsT]] = (),
        model: Model | None = None,
    ) -> Agent[DepsT, OutputT]:
        selected_model = model or await self._providers.build_model(spec.model_ref)
        agent = Agent(
            selected_model,
            deps_type=deps_type,
            output_type=output_type,
            instructions=spec.instructions,
            tools=tools,
            name=spec.id,
        )
        return agent
