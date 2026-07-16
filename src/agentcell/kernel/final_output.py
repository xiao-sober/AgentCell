"""Provider-neutral rejection of unresolved tool protocol masquerading as final text."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import cast

from pydantic import BaseModel, ConfigDict

_DSML_START = re.compile(
    r"^\s*<[|｜]{1,2}\s*DSML\s*[|｜]{1,2}\s*(?:tool_calls?|invoke)",
    re.IGNORECASE,
)
_TOOL_TAG_START = re.compile(
    r"^\s*</?(?:invoke|tool_calls?|function_calls?|function)\b",
    re.IGNORECASE,
)
_ARTIFACT_CALL_START = re.compile(
    r"^\s*(?:call\s+)?artifact_list\s*(?:\(|\{|\[|$)",
    re.IGNORECASE,
)
_UNFINISHED_ARTIFACT_INTENT = re.compile(
    r"^\s*(?:i\s+)?(?:need|will|must|should|let\s+me)\s+to?\s*"
    r"(?:call|use|invoke)\b.{0,120}\bartifact_list\b",
    re.IGNORECASE | re.DOTALL,
)


class FinalOutputAssessment(BaseModel):
    """Stable, auditable classification without retaining rejected output text."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    accepted: bool
    reason: str | None = None


@dataclass(slots=True)
class FinalOutputState:
    """Mutable per-execution guard state shared across one bounded model retry."""

    rejections: int = 0
    force_no_tools: bool = False
    runtime_finalize_reason: str | None = None

    def reject(self) -> int:
        self.rejections += 1
        self.force_no_tools = True
        return self.rejections

    def finalize(self, reason: str) -> None:
        """Disable further tools after deterministic runtime evidence is sufficient."""

        self.runtime_finalize_reason = reason


class FinalOutputGuard:
    """Reject only outputs whose body itself is an unresolved tool invocation."""

    @staticmethod
    def assess(output: str) -> FinalOutputAssessment:
        candidate = output.strip()
        if not candidate:
            return FinalOutputAssessment(accepted=True)
        if _looks_like_function_call_json(candidate):
            return FinalOutputAssessment(
                accepted=False,
                reason="unresolved_function_call",
            )
        if _DSML_START.search(candidate):
            return FinalOutputAssessment(accepted=False, reason="dsml_tool_protocol")
        if _TOOL_TAG_START.search(candidate):
            return FinalOutputAssessment(
                accepted=False,
                reason="unresolved_tool_protocol",
            )
        if _ARTIFACT_CALL_START.search(candidate) or (
            len(candidate) <= 500 and _UNFINISHED_ARTIFACT_INTENT.search(candidate)
        ):
            return FinalOutputAssessment(
                accepted=False,
                reason="unresolved_artifact_list_intent",
            )
        return FinalOutputAssessment(accepted=True)


def _looks_like_function_call_json(candidate: str) -> bool:
    if not candidate.startswith(("{", "[")):
        return False
    try:
        value = cast(object, json.loads(candidate))
    except (TypeError, ValueError):
        return False
    if isinstance(value, list):
        items = cast(list[object], value)
        return bool(items) and all(_function_call_mapping(item) for item in items)
    if not isinstance(value, dict):
        return False
    mapping = cast(dict[str, object], value)
    if "tool_calls" in mapping:
        calls = mapping["tool_calls"]
        return isinstance(calls, list) and bool(cast(list[object], calls))
    return _function_call_mapping(mapping)


def _function_call_mapping(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    mapping = cast(dict[object, object], value)
    keys = {str(key).casefold() for key in mapping}
    has_name = bool(keys & {"name", "tool_name", "function"})
    has_arguments = bool(keys & {"arguments", "args", "parameters"})
    return has_name and has_arguments
