"""One least-authority CLI profile shared by run and chat entry points."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from agentcell.agents import AgentRegistry, AgentSpec, HandoffStage, TeamSpec
from agentcell.budgets import Budget
from agentcell.policy import Capability, CapabilityLease, PermissionMode


class CommandProfile(StrEnum):
    """Named exact-executable profiles; no interpreter or launcher is implied."""

    PYTEST = "pytest"
    RUFF = "ruff"
    PYRIGHT = "pyright"

    @property
    def executable(self) -> str:
        return self.value


class CliRunProfile(BaseModel):
    """Validated CLI projection from an Agent upper bound to one Run lease."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_id: str
    approval_mode: PermissionMode
    lease: CapabilityLease
    command_profiles: tuple[CommandProfile, ...] = ()
    deprecated_options: tuple[str, ...] = ()

    @classmethod
    def resolve(
        cls,
        spec: AgentSpec,
        *,
        approval_mode: PermissionMode | None,
        permission_mode: PermissionMode | None,
        write_scopes: list[str] | None,
        legacy_write_scopes: list[str] | None,
        commands: list[str] | None,
        legacy_commands: list[str] | None,
        command_profiles: list[CommandProfile] | None,
        network_domains: list[str] | None,
    ) -> CliRunProfile:
        """Resolve new and one-version legacy options without widening AgentSpec."""

        deprecated: list[str] = []
        selected_mode = _exclusive_value(
            approval_mode,
            permission_mode,
            current="--approval-mode",
            legacy="--permission-mode",
        )
        if permission_mode is not None:
            deprecated.append("--permission-mode")
        selected_writes = _exclusive_collection(
            write_scopes,
            legacy_write_scopes,
            current="--write-scope",
            legacy="--allow-write",
        )
        if legacy_write_scopes:
            deprecated.append("--allow-write")
        selected_commands = _exclusive_collection(
            commands,
            legacy_commands,
            current="--command",
            legacy="--allow-command",
        )
        if legacy_commands:
            deprecated.append("--allow-command")

        profiles = tuple(dict.fromkeys(command_profiles or ()))
        executables = tuple(
            dict.fromkeys((*selected_commands, *(profile.executable for profile in profiles)))
        )
        domains = tuple(network_domains or ())

        read_scopes: tuple[str, ...] = ()
        if Capability.FILESYSTEM_READ in spec.capabilities:
            read_scopes = (".",)

        requested_writes = tuple(selected_writes)
        if spec.id == "coder" and not requested_writes:
            requested_writes = (".",)
        _ensure_capability(
            spec,
            Capability.FILESYSTEM_WRITE,
            requested=bool(requested_writes),
            option="--write-scope",
            tool_prefix="workspace.",
        )
        _ensure_capability(
            spec,
            Capability.SHELL_EXECUTE,
            requested=bool(executables),
            option="--command/--command-profile",
            tool_prefix="shell.",
        )
        _ensure_capability(
            spec,
            Capability.NETWORK_REQUEST,
            requested=bool(domains),
            option="--network-domain",
            tool_prefix="http.",
        )

        return cls(
            agent_id=spec.id,
            approval_mode=selected_mode or PermissionMode.REQUEST,
            lease=CapabilityLease(
                filesystem_read=read_scopes,
                filesystem_write=requested_writes,
                commands=frozenset(executables),
                network_domains=domains,
            ),
            command_profiles=profiles,
            deprecated_options=tuple(deprecated),
        )

    def deprecation_messages(self) -> tuple[str, ...]:
        replacements = {
            "--permission-mode": "--approval-mode",
            "--allow-write": "--write-scope",
            "--allow-command": "--command",
        }
        return tuple(
            f"{option} is deprecated; use {replacements[option]}"
            for option in self.deprecated_options
        )


class CliTeamProfile(BaseModel):
    """Validated CLI projection for one explicit deterministic Team execution."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    team_id: str
    team_version: int
    approval_mode: PermissionMode
    lease: CapabilityLease
    budget: Budget
    stage_agents: dict[HandoffStage, str]
    stage_model_refs: dict[HandoffStage, str]
    stage_instructions: dict[HandoffStage, str]
    stage_output_contracts: dict[HandoffStage, str]
    stage_budgets: dict[HandoffStage, Budget]
    stage_leases: dict[HandoffStage, CapabilityLease]
    review_gate: bool
    deprecated_options: tuple[str, ...] = ()

    @classmethod
    def resolve(
        cls,
        team: TeamSpec,
        agents: AgentRegistry,
        *,
        approval_mode: PermissionMode | None,
        permission_mode: PermissionMode | None,
        write_scopes: list[str] | None,
        legacy_write_scopes: list[str] | None,
        commands: list[str] | None,
        legacy_commands: list[str] | None,
        command_profiles: list[CommandProfile] | None,
        network_domains: list[str] | None,
        max_requests: int | None,
        max_tool_calls: int | None,
        max_input_tokens: int | None,
        max_total_tokens: int | None,
    ) -> CliTeamProfile:
        """Resolve the root envelope, then partition it without widening authority."""

        team.validate_agents(agents)
        coder = team.stage(HandoffStage.CODER)
        root_profile = CliRunProfile.resolve(
            agents.get(coder.agent_id),
            approval_mode=approval_mode,
            permission_mode=permission_mode,
            write_scopes=write_scopes,
            legacy_write_scopes=legacy_write_scopes,
            commands=commands,
            legacy_commands=legacy_commands,
            command_profiles=command_profiles,
            network_domains=network_domains,
        )
        overrides = {
            key: value
            for key, value in {
                "max_requests": max_requests,
                "max_tool_calls": max_tool_calls,
                "max_input_tokens": max_input_tokens,
                "max_total_tokens": max_total_tokens,
            }.items()
            if value is not None
        }
        budget = (
            team.default_budget.model_copy(update=overrides) if overrides else team.default_budget
        )
        stage_budgets = team.allocate_stage_budgets(budget)
        stage_leases = team.allocate_stage_leases(root_profile.lease)
        return cls(
            team_id=team.id,
            team_version=team.schema_version,
            approval_mode=root_profile.approval_mode,
            lease=root_profile.lease,
            budget=budget,
            stage_agents={item.stage: item.agent_id for item in team.stages},
            stage_model_refs={item.stage: item.model_ref for item in team.stages},
            stage_instructions={item.stage: item.instructions for item in team.stages},
            stage_output_contracts={item.stage: item.output_contract for item in team.stages},
            stage_budgets=stage_budgets,
            stage_leases=stage_leases,
            review_gate=team.review_gate,
            deprecated_options=root_profile.deprecated_options,
        )

    def deprecation_messages(self) -> tuple[str, ...]:
        replacements = {
            "--permission-mode": "--approval-mode",
            "--allow-write": "--write-scope",
            "--allow-command": "--command",
        }
        return tuple(
            f"{option} is deprecated; use {replacements[option]}"
            for option in self.deprecated_options
        )


class CliTaskProfile(BaseModel):
    """Caller-owned authority envelope for automatic Task Router selection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    approval_mode: PermissionMode
    lease: CapabilityLease
    budget: Budget
    deprecated_options: tuple[str, ...] = ()

    @classmethod
    def resolve(
        cls,
        team: TeamSpec,
        *,
        approval_mode: PermissionMode | None,
        permission_mode: PermissionMode | None,
        write_scopes: list[str] | None,
        legacy_write_scopes: list[str] | None,
        commands: list[str] | None,
        legacy_commands: list[str] | None,
        command_profiles: list[CommandProfile] | None,
        network_domains: list[str] | None,
        max_requests: int | None,
        max_tool_calls: int | None,
        max_input_tokens: int | None,
        max_total_tokens: int | None,
    ) -> CliTaskProfile:
        deprecated: list[str] = []
        selected_mode = _exclusive_value(
            approval_mode,
            permission_mode,
            current="--approval-mode",
            legacy="--permission-mode",
        )
        if permission_mode is not None:
            deprecated.append("--permission-mode")
        selected_writes = _exclusive_collection(
            write_scopes,
            legacy_write_scopes,
            current="--write-scope",
            legacy="--allow-write",
        )
        if legacy_write_scopes:
            deprecated.append("--allow-write")
        selected_commands = _exclusive_collection(
            commands,
            legacy_commands,
            current="--command",
            legacy="--allow-command",
        )
        if legacy_commands:
            deprecated.append("--allow-command")
        profiles = tuple(dict.fromkeys(command_profiles or ()))
        executables = frozenset((*selected_commands, *(profile.executable for profile in profiles)))
        overrides = {
            key: value
            for key, value in {
                "max_requests": max_requests,
                "max_tool_calls": max_tool_calls,
                "max_input_tokens": max_input_tokens,
                "max_total_tokens": max_total_tokens,
            }.items()
            if value is not None
        }
        return cls(
            approval_mode=selected_mode or PermissionMode.REQUEST,
            lease=CapabilityLease(
                filesystem_read=(".",),
                filesystem_write=selected_writes,
                commands=executables,
                network_domains=tuple(network_domains or ()),
            ),
            budget=(
                team.default_budget.model_copy(update=overrides)
                if overrides
                else team.default_budget
            ),
            deprecated_options=tuple(deprecated),
        )

    def deprecation_messages(self) -> tuple[str, ...]:
        replacements = {
            "--permission-mode": "--approval-mode",
            "--allow-write": "--write-scope",
            "--allow-command": "--command",
        }
        return tuple(
            f"{option} is deprecated; use {replacements[option]}"
            for option in self.deprecated_options
        )


def _exclusive_value[T](
    current_value: T | None,
    legacy_value: T | None,
    *,
    current: str,
    legacy: str,
) -> T | None:
    if current_value is not None and legacy_value is not None:
        raise ValueError(f"{current} and legacy {legacy} cannot be used together")
    return current_value if current_value is not None else legacy_value


def _exclusive_collection[T](
    current_value: list[T] | None,
    legacy_value: list[T] | None,
    *,
    current: str,
    legacy: str,
) -> tuple[T, ...]:
    if current_value and legacy_value:
        raise ValueError(f"{current} and legacy {legacy} cannot be used together")
    return tuple(current_value or legacy_value or ())


def _ensure_capability(
    spec: AgentSpec,
    capability: Capability,
    *,
    requested: bool,
    option: str,
    tool_prefix: str,
) -> None:
    if not requested:
        return
    has_tool = any(tool.startswith(tool_prefix) for tool in spec.tools)
    if capability not in spec.capabilities or not has_tool:
        raise ValueError(
            f"Agent {spec.id!r} does not permit {option}; "
            f"its AgentSpec lacks {capability.value} authority"
        )
