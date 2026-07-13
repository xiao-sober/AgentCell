"""Deterministic registry for typed, policy-bearing tool definitions."""

from __future__ import annotations

import re
from typing import cast

from pydantic import BaseModel

from agentcell.errors import ToolNotFoundError, ToolRegistrationError
from agentcell.tools.models import ToolDefinition

_TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_.-]*$")


class ToolRegistry:
    """Register immutable definitions without overwrites or hidden global state."""

    def __init__(self) -> None:
        self._definitions: dict[str, ToolDefinition[BaseModel]] = {}

    def register[ParamsT: BaseModel](self, definition: ToolDefinition[ParamsT]) -> None:
        """Register one definition after validating stable naming and strict arguments."""

        if not _TOOL_NAME_RE.fullmatch(definition.name):
            raise ToolRegistrationError(
                "Tool names must start with a letter and use only lowercase letters, "
                "digits, '.', '-', or '_'"
            )
        if not definition.description.strip():
            raise ToolRegistrationError("Tool description cannot be empty")
        if definition.params_model.model_config.get("extra") != "forbid":
            raise ToolRegistrationError(
                f"Tool {definition.name!r} parameter model must set extra='forbid'"
            )
        if definition.name in self._definitions:
            raise ToolRegistrationError(f"Tool {definition.name!r} is already registered")
        self._definitions[definition.name] = cast(ToolDefinition[BaseModel], definition)

    def get(self, tool_name: str) -> ToolDefinition[BaseModel]:
        """Return one definition or a classified not-found error."""

        try:
            return self._definitions[tool_name]
        except KeyError as error:
            raise ToolNotFoundError(tool_name) from error

    def list(self) -> tuple[ToolDefinition[BaseModel], ...]:
        """Return definitions in stable name order."""

        return tuple(self._definitions[name] for name in sorted(self._definitions))

    def parameter_schemas(self) -> dict[str, dict[str, object]]:
        """Return centralized JSON Schemas for future PydanticAI registration."""

        return {
            definition.name: definition.params_model.model_json_schema()
            for definition in self.list()
        }
