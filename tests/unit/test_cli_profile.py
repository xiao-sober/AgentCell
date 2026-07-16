"""Stage 9.2.2 CLI profiles preserve old behavior without widening AgentSpec."""

from __future__ import annotations

import pytest

from agentcell.agents import coder_spec, researcher_spec, reviewer_spec
from agentcell.cli.profile import CliRunProfile, CommandProfile
from agentcell.policy import PermissionMode


def test_new_and_legacy_options_build_the_same_lease() -> None:
    spec = coder_spec(model_ref="fake")
    current = CliRunProfile.resolve(
        spec,
        approval_mode=PermissionMode.AUTO,
        permission_mode=None,
        write_scopes=["src", "tests"],
        legacy_write_scopes=None,
        commands=None,
        legacy_commands=None,
        command_profiles=[CommandProfile.RUFF],
        network_domains=None,
    )
    legacy = CliRunProfile.resolve(
        spec,
        approval_mode=None,
        permission_mode=PermissionMode.AUTO,
        write_scopes=None,
        legacy_write_scopes=["src", "tests"],
        commands=None,
        legacy_commands=["ruff"],
        command_profiles=None,
        network_domains=None,
    )

    assert current.lease == legacy.lease
    assert current.approval_mode is legacy.approval_mode is PermissionMode.AUTO
    assert legacy.deprecated_options == ("--permission-mode", "--allow-write", "--allow-command")


def test_coder_defaults_to_workspace_write_but_no_command() -> None:
    profile = CliRunProfile.resolve(
        coder_spec(model_ref="fake"),
        approval_mode=None,
        permission_mode=None,
        write_scopes=None,
        legacy_write_scopes=None,
        commands=None,
        legacy_commands=None,
        command_profiles=None,
        network_domains=None,
    )

    assert profile.lease.filesystem_write == (".",)
    assert profile.lease.commands == frozenset()
    assert profile.lease.can_delegate is False


def test_profile_rejects_capabilities_outside_the_selected_agent() -> None:
    with pytest.raises(ValueError, match="reviewer.*--write-scope"):
        CliRunProfile.resolve(
            reviewer_spec(model_ref="fake"),
            approval_mode=None,
            permission_mode=None,
            write_scopes=["."],
            legacy_write_scopes=None,
            commands=None,
            legacy_commands=None,
            command_profiles=None,
            network_domains=None,
        )
    with pytest.raises(ValueError, match="coder.*--network-domain"):
        CliRunProfile.resolve(
            coder_spec(model_ref="fake"),
            approval_mode=None,
            permission_mode=None,
            write_scopes=None,
            legacy_write_scopes=None,
            commands=None,
            legacy_commands=None,
            command_profiles=None,
            network_domains=["example.com"],
        )


def test_command_profile_and_network_domain_are_exact_and_explicit() -> None:
    coder = CliRunProfile.resolve(
        coder_spec(model_ref="fake"),
        approval_mode=PermissionMode.FULL,
        permission_mode=None,
        write_scopes=None,
        legacy_write_scopes=None,
        commands=None,
        legacy_commands=None,
        command_profiles=[CommandProfile.PYTEST],
        network_domains=None,
    )
    researcher = CliRunProfile.resolve(
        researcher_spec(model_ref="fake"),
        approval_mode=None,
        permission_mode=None,
        write_scopes=None,
        legacy_write_scopes=None,
        commands=None,
        legacy_commands=None,
        command_profiles=None,
        network_domains=["docs.example.com"],
    )

    assert coder.lease.commands == frozenset({"pytest"})
    assert "python" not in coder.lease.commands
    assert "uv" not in coder.lease.commands
    assert researcher.lease.network_domains == ("docs.example.com",)


def test_approval_modes_change_only_policy_not_authority() -> None:
    leases = [
        CliRunProfile.resolve(
            coder_spec(model_ref="fake"),
            approval_mode=mode,
            permission_mode=None,
            write_scopes=["src"],
            legacy_write_scopes=None,
            commands=None,
            legacy_commands=None,
            command_profiles=[CommandProfile.PYTEST],
            network_domains=None,
        ).lease
        for mode in PermissionMode
    ]

    assert all(lease == leases[0] for lease in leases)


def test_new_and_legacy_names_cannot_be_mixed() -> None:
    with pytest.raises(ValueError, match="cannot be used together"):
        CliRunProfile.resolve(
            coder_spec(model_ref="fake"),
            approval_mode=PermissionMode.REQUEST,
            permission_mode=PermissionMode.AUTO,
            write_scopes=None,
            legacy_write_scopes=None,
            commands=None,
            legacy_commands=None,
            command_profiles=None,
            network_domains=None,
        )
