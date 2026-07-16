"""Immutable execution identity persisted for restart-safe Run recovery."""

from __future__ import annotations

import hashlib
import json
from itertools import permutations
from typing import Any, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agentcell.agents import AgentSpec
from agentcell.providers import ModelSpecDefinition


def _canonical_json_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _canonical_sha256(value: BaseModel) -> str:
    return _canonical_json_sha256(value.model_dump(mode="json"))


def _matches_legacy_agent_hash(agent_spec: AgentSpec, expected: str) -> bool:
    """Accept v1 hashes whose only instability was frozenset serialization order."""

    snapshot = agent_spec.model_dump(mode="json")
    raw_capabilities = snapshot.get("capabilities")
    if not isinstance(raw_capabilities, list):
        return False
    capability_values = cast(list[object], raw_capabilities)
    if not all(isinstance(item, str) for item in capability_values):
        return False
    capabilities = cast(list[str], capability_values)
    for ordering in permutations(capabilities):
        candidate = {**snapshot, "capabilities": list(ordering)}
        if _canonical_json_sha256(candidate) == expected:
            return True
    return False


class RunExecutionIdentity(BaseModel):
    """Versioned Agent and model snapshot that a resumed Run must still match."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1, strict=True)
    user_id: UUID
    agent_spec: AgentSpec
    agent_spec_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_ref: str = Field(min_length=1)
    model_spec: ModelSpecDefinition
    model_spec_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @classmethod
    def capture(
        cls,
        *,
        user_id: UUID,
        agent_spec: AgentSpec,
        model_spec: ModelSpecDefinition,
    ) -> RunExecutionIdentity:
        """Create hashes from the exact validated definitions used for a new Run."""

        return cls(
            schema_version=2,
            user_id=user_id,
            agent_spec=agent_spec,
            agent_spec_sha256=_canonical_sha256(agent_spec),
            model_ref=agent_spec.model_ref,
            model_spec=model_spec,
            model_spec_sha256=_canonical_sha256(model_spec),
        )

    @model_validator(mode="after")
    def validate_snapshot(self) -> RunExecutionIdentity:
        if self.agent_spec.model_ref != self.model_ref:
            raise ValueError("agent_spec model_ref does not match execution identity")
        agent_hash_matches = _canonical_sha256(self.agent_spec) == self.agent_spec_sha256
        if not agent_hash_matches and not (
            self.schema_version == 1
            and _matches_legacy_agent_hash(self.agent_spec, self.agent_spec_sha256)
        ):
            raise ValueError("agent_spec snapshot hash does not match")
        if _canonical_sha256(self.model_spec) != self.model_spec_sha256:
            raise ValueError("model_spec snapshot hash does not match")
        return self

    def matches_current(self, *, model_spec: BaseModel) -> bool:
        """Return whether the configured model definition is byte-stably equivalent."""

        return _canonical_sha256(model_spec) == self.model_spec_sha256

    def event_fields(self) -> dict[str, Any]:
        """Return the non-sensitive identity summary safe for lifecycle events."""

        return {
            "model_ref": self.model_ref,
            "provider": self.model_spec.provider.value,
            "model": self.model_spec.model,
            "agent_spec_sha256": self.agent_spec_sha256,
            "model_spec_sha256": self.model_spec_sha256,
        }
