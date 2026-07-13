"""Risk, capability, ToolPolicy, and immutable Run lease models."""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterable
from enum import StrEnum
from pathlib import PurePosixPath
from typing import cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agentcell.errors import CapabilityEscalationError

_ENV_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_COMMAND_RE = re.compile(r"^[A-Za-z0-9_.+-]+$")
_DOMAIN_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_BLOCKED_NETWORK_NAMES = frozenset({"localhost", "metadata.google.internal"})


class RiskLevel(StrEnum):
    """Stable tool risk classification used by approval policy."""

    SAFE = "safe"
    GUARDED = "guarded"
    DANGEROUS = "dangerous"
    FORBIDDEN = "forbidden"


class Capability(StrEnum):
    """Coarse capabilities checked before tool-specific scope checks."""

    FILESYSTEM_READ = "filesystem.read"
    FILESYSTEM_WRITE = "filesystem.write"
    NETWORK_REQUEST = "network.request"
    SHELL_EXECUTE = "shell.execute"
    AGENT_DELEGATE = "agent.delegate"


class ToolPolicy(BaseModel):
    """Execution boundaries declared by every registered tool."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    risk: RiskLevel
    requires_approval: bool
    idempotent: bool
    timeout_seconds: float = Field(gt=0, le=3600, allow_inf_nan=False)
    max_output_bytes: int = Field(ge=1, le=16 * 1024 * 1024, strict=True)
    capabilities: frozenset[Capability] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_risk_rules(self) -> ToolPolicy:
        if self.risk is RiskLevel.DANGEROUS and not self.requires_approval:
            raise ValueError("dangerous tools must require approval")
        return self


class CapabilityLease(BaseModel):
    """Default-deny, workspace-relative authority assigned to one Run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    filesystem_read: tuple[str, ...] = ()
    filesystem_write: tuple[str, ...] = ()
    network_domains: tuple[str, ...] = ()
    commands: frozenset[str] = frozenset()
    can_delegate: bool = False
    max_child_depth: int = Field(default=0, ge=0, strict=True)

    @field_validator("filesystem_read", "filesystem_write", mode="before")
    @classmethod
    def normalize_path_scopes(cls, value: object) -> tuple[str, ...]:
        if not isinstance(value, (list, tuple, set, frozenset)):
            raise ValueError("filesystem scopes must be a collection")
        items = cast(Iterable[object], value)
        return tuple(sorted({_normalize_scope(item) for item in items}))

    @field_validator("network_domains", mode="before")
    @classmethod
    def normalize_network_domains(cls, value: object) -> tuple[str, ...]:
        if not isinstance(value, (list, tuple, set, frozenset)):
            raise ValueError("network_domains must be a collection")
        items = cast(Iterable[object], value)
        return tuple(sorted({_normalize_domain(item) for item in items}))

    @field_validator("commands", mode="before")
    @classmethod
    def normalize_commands(cls, value: object) -> frozenset[str]:
        if not isinstance(value, (list, tuple, set, frozenset)):
            raise ValueError("commands must be a collection")
        normalized: set[str] = set()
        for item in cast(Iterable[object], value):
            if not isinstance(item, str) or not _COMMAND_RE.fullmatch(item):
                raise ValueError("commands must contain executable names without arguments")
            normalized.add(item.casefold())
        return frozenset(normalized)

    @model_validator(mode="after")
    def validate_delegation_depth(self) -> CapabilityLease:
        if self.can_delegate and self.max_child_depth == 0:
            raise ValueError("can_delegate requires max_child_depth greater than zero")
        if not self.can_delegate and self.max_child_depth != 0:
            raise ValueError("max_child_depth must be zero when delegation is disabled")
        return self

    def allows(self, capability: Capability) -> bool:
        """Return whether this lease grants a coarse capability."""

        grants = {
            Capability.FILESYSTEM_READ: bool(self.filesystem_read),
            Capability.FILESYSTEM_WRITE: bool(self.filesystem_write),
            Capability.NETWORK_REQUEST: bool(self.network_domains),
            Capability.SHELL_EXECUTE: bool(self.commands),
            Capability.AGENT_DELEGATE: self.can_delegate and self.max_child_depth > 0,
        }
        return grants[capability]

    def ensure_child_subset(self, child: CapabilityLease) -> None:
        """Reject any child lease that widens scopes or delegation depth."""

        _ensure_scopes_subset(
            child.filesystem_read,
            self.filesystem_read,
            capability=Capability.FILESYSTEM_READ,
        )
        _ensure_scopes_subset(
            child.filesystem_write,
            self.filesystem_write,
            capability=Capability.FILESYSTEM_WRITE,
        )
        for domain in child.network_domains:
            if not any(_domain_is_within(domain, parent) for parent in self.network_domains):
                raise CapabilityEscalationError(Capability.NETWORK_REQUEST)
        if not child.commands.issubset(self.commands):
            raise CapabilityEscalationError(Capability.SHELL_EXECUTE)
        if child.can_delegate:
            if not self.can_delegate or child.max_child_depth > self.max_child_depth - 1:
                raise CapabilityEscalationError(Capability.AGENT_DELEGATE)


def _normalize_scope(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("filesystem scope must be a string")
    normalized = value.strip().replace("\\", "/")
    if not normalized:
        raise ValueError("filesystem scope cannot be empty")
    if normalized.startswith("/") or normalized.startswith("//") or _ENV_DRIVE_RE.match(normalized):
        raise ValueError("filesystem scope must be workspace-relative")
    path = PurePosixPath(normalized)
    if ".." in path.parts:
        raise ValueError("filesystem scope cannot contain parent traversal")
    parts = tuple(part for part in path.parts if part not in {"", "."})
    return "." if not parts else "/".join(parts)


def _normalize_domain(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("network domain must be a string")
    domain = value.strip().casefold().rstrip(".")
    if not domain or "://" in domain or "/" in domain or ":" in domain:
        raise ValueError("network domain must not include scheme, path, or port")
    if domain in _BLOCKED_NETWORK_NAMES:
        raise ValueError("local and cloud metadata domains are forbidden")
    try:
        address = ipaddress.ip_address(domain)
    except ValueError:
        labels = domain.split(".")
        if len(labels) < 2 or any(not _DOMAIN_LABEL_RE.fullmatch(label) for label in labels):
            raise ValueError("network domain is invalid") from None
    else:
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
            or address.is_unspecified
        ):
            raise ValueError("local, private, metadata, and reserved addresses are forbidden")
    return domain


def _ensure_scopes_subset(
    child_scopes: tuple[str, ...],
    parent_scopes: tuple[str, ...],
    *,
    capability: Capability,
) -> None:
    for child in child_scopes:
        if not any(_scope_is_within(child, parent) for parent in parent_scopes):
            raise CapabilityEscalationError(capability)


def _scope_is_within(child: str, parent: str) -> bool:
    return parent == "." or child == parent or child.startswith(f"{parent}/")


def _domain_is_within(child: str, parent: str) -> bool:
    return child == parent or child.endswith(f".{parent}")
