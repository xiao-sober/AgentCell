"""Stage 5 CLI verifies the direct, offline RunService entry point."""

from __future__ import annotations

import json
import re
import sqlite3
from importlib import import_module
from pathlib import Path
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from agentcell.agents import (
    DelegationResult,
    DelegationStatus,
    HandoffResult,
    HandoffStage,
    software_team_spec,
)
from agentcell.budgets import BudgetTracker
from agentcell.cli.app import app

cli_module = import_module("agentcell.cli.app")


def test_dry_route_is_non_authoritative_and_creates_no_run(
    migrated_database_url: str,
    database_path: Path,
    tmp_path: Path,
) -> None:
    result = CliRunner().invoke(
        app,
        [
            "run",
            "分析项目结构并给出规划",
            "--dry-route",
            "--offline-fake",
            "--workspace",
            str(tmp_path),
            "--database-url",
            migrated_database_url,
        ],
    )

    assert result.exit_code == 0, result.output
    assert "route=single_agent:coordinator" in result.output
    assert "authoritative=false" in result.output
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM runs").fetchone() == (0,)


def test_offline_software_team_runs_four_stages_and_prints_ids(
    migrated_database_url: str,
    tmp_path: Path,
) -> None:
    result = CliRunner().invoke(
        app,
        [
            "run",
            "implement and review",
            "--team",
            "software",
            "--command-profile",
            "pytest",
            "--offline-fake",
            "--no-stream",
            "--workspace",
            str(tmp_path),
            "--database-url",
            migrated_database_url,
        ],
    )

    assert result.exit_code == 0, result.output
    assert "root_run_id=" in result.output
    assert "child_run_ids=" in result.output
    assert "route=team:software" in result.output
    assert "status=completed" in result.output
    assert result.output.count("Offline result: implement and review") == 1


def test_run_rejects_agent_and_team_override_together() -> None:
    result = CliRunner().invoke(
        app,
        [
            "run",
            "inspect",
            "--agent",
            "coder",
            "--team",
            "software",
            "--command-profile",
            "pytest",
            "--offline-fake",
        ],
    )

    assert result.exit_code == 1
    assert "--agent and --team are mutually exclusive" in result.output


def test_offline_software_team_json_is_structured(
    migrated_database_url: str,
    tmp_path: Path,
) -> None:
    result = CliRunner().invoke(
        app,
        [
            "run",
            "deliver",
            "--team",
            "software",
            "--command-profile",
            "pytest",
            "--offline-fake",
            "--json",
            "--workspace",
            str(tmp_path),
            "--database-url",
            migrated_database_url,
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["decision"]["team_id"] == "software"
    assert payload["decision"]["source"] == "override"
    assert payload["run"]["status"] == "completed"
    assert len(payload["child_run_ids"]) == 4
    assert payload["run"]["id"]
    assert payload["run"]["conversation_id"]


def test_failed_software_team_prints_stage_code_and_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root_run_id = uuid4()
    conversation_id = uuid4()
    child_run_id = uuid4()
    stage = DelegationResult(
        delegation_id=uuid4(),
        child_run_id=child_run_id,
        agent_id="coordinator",
        status=DelegationStatus.FAILED,
        error_code="invalid_final_output",
        error_message="Model returned unresolved tool protocol as its final response twice",
    )
    failed = HandoffResult(
        root_run_id=root_run_id,
        conversation_id=conversation_id,
        team_id="software",
        team_version=1,
        status=DelegationStatus.FAILED,
        stages=(stage,),
        budget=BudgetTracker(software_team_spec(model_ref="fake").default_budget).snapshot(),
        error_code=stage.error_code,
        error_message=stage.error_message,
        error_stage=HandoffStage.COORDINATOR,
    )

    async def return_failure(**_: object) -> tuple[HandoffResult, bool]:
        return failed, False

    monkeypatch.setattr(cli_module, "_run_once", return_failure)
    result = CliRunner().invoke(
        app,
        ["run", "repair tests", "--team", "software", "--no-stream"],
    )

    assert result.exit_code == 1
    assert "status=failed" in result.output
    assert "Team failed: stage=coordinator" in result.output
    assert "code=invalid_final_output" in result.output
    assert "message=Model returned" in result.output
    assert "unresolved tool protocol as its final response twice" in result.output


def test_offline_fake_run_persists_and_prints_json(
    migrated_database_url: str,
    tmp_path: Path,
) -> None:
    result = CliRunner().invoke(
        app,
        [
            "run",
            "inspect project",
            "--offline-fake",
            "--json",
            "--workspace",
            str(tmp_path),
            "--database-url",
            migrated_database_url,
            "--max-requests",
            "14",
            "--max-tool-calls",
            "35",
            "--max-input-tokens",
            "180000",
            "--max-total-tokens",
            "220000",
        ],
    )

    assert result.exit_code == 0, result.output
    assert '"status": "completed"' in result.output
    assert '"output": "Offline result: inspect project"' in result.output
    assert '"requests": 1' in result.output
    assert '"max_requests": 14' in result.output
    assert '"max_tool_calls": 35' in result.output
    assert '"max_input_tokens": 180000' in result.output
    assert '"max_output_tokens": 48000' in result.output
    assert '"agent_id": "coordinator"' in result.output
    assert '"max_total_tokens": 220000' in result.output
    assert '"cache_write_tokens": 0' in result.output
    assert '"cache_read_tokens": 0' in result.output

    run_id = re.search(r'"id": "([0-9a-f-]{36})"', result.output)
    assert run_id is not None
    inspected = CliRunner().invoke(
        app,
        [
            "inspect",
            run_id.group(1),
            "--database-url",
            migrated_database_url,
            "--json",
        ],
    )
    assert inspected.exit_code == 0, inspected.output
    assert '"event_count":' in inspected.output
    assert '"last_sequence":' in inspected.output


def test_offline_fake_human_output_includes_token_and_cache_usage(
    migrated_database_url: str,
    tmp_path: Path,
) -> None:
    result = CliRunner().invoke(
        app,
        [
            "run",
            "inspect project",
            "--offline-fake",
            "--workspace",
            str(tmp_path),
            "--database-url",
            migrated_database_url,
        ],
    )

    assert result.exit_code == 0, result.output
    assert "tokens input=" in result.output
    assert "output=" in result.output
    assert "total=" in result.output
    assert "cache_read=0" in result.output
    assert "cache_write=0" in result.output
    assert "cache_hit=0.0%" in result.output
    assert result.output.count("Offline result: inspect project") == 1

    no_stream = CliRunner().invoke(
        app,
        [
            "run",
            "inspect project",
            "--offline-fake",
            "--no-stream",
            "--workspace",
            str(tmp_path),
            "--database-url",
            migrated_database_url,
        ],
    )
    assert no_stream.exit_code == 0, no_stream.output
    assert no_stream.output.count("Offline result: inspect project") == 1
    assert "budget.updated source=" not in no_stream.output


def test_chat_continues_a_conversation_after_process_restart(
    migrated_database_url: str,
    tmp_path: Path,
) -> None:
    runner = CliRunner()
    first = runner.invoke(
        app,
        [
            "chat",
            "--offline-fake",
            "--workspace",
            str(tmp_path),
            "--database-url",
            migrated_database_url,
        ],
        input="first question\ny\n/exit\n",
    )
    assert first.exit_code == 0, first.output
    assert "Offline chat response" in first.output
    assert "model_ref=offline_fake" in first.output
    match = re.search(r"conversation_id=([0-9a-f-]{36})", first.output)
    assert match is not None

    second = runner.invoke(
        app,
        [
            "chat",
            "--conversation-id",
            match.group(1),
            "--offline-fake",
            "--database-url",
            migrated_database_url,
        ],
        input="follow-up question\ny\n/exit\n",
    )
    assert second.exit_code == 0, second.output
    assert f"conversation_id={match.group(1)}" in second.output
    assert "model_ref=offline_fake" in second.output
    assert "Offline chat response" in second.output
    assert "Continue with:" in second.output
    assert f"chat --conversation-id {match.group(1)}" in second.output


def test_chat_ordinary_question_uses_direct_tool_free_turn(
    migrated_database_url: str,
    tmp_path: Path,
) -> None:
    result = CliRunner().invoke(
        app,
        [
            "chat",
            "--offline-fake",
            "--workspace",
            str(tmp_path),
            "--database-url",
            migrated_database_url,
        ],
        input="Who are you?\n/exit\n",
    )

    assert result.exit_code == 0, result.output
    assert "Offline chat response" in result.output
    assert re.search(r"status=completed\s+requests=1\s+tool_calls=0", result.output)
    assert "Use single_agent" not in result.output


def test_run_supports_coder_permission_mode_and_conversation_footer(
    migrated_database_url: str,
    tmp_path: Path,
) -> None:
    result = CliRunner().invoke(
        app,
        [
            "run",
            "prepare a change",
            "--agent",
            "coder",
            "--permission-mode",
            "request",
            "--offline-fake",
            "--workspace",
            str(tmp_path),
            "--database-url",
            migrated_database_url,
        ],
    )

    assert result.exit_code == 0, result.output
    assert "conversation_id=" in result.output
    assert "status=completed" in result.output
    assert "Deprecated:" in result.output


def test_stage_922_new_options_and_legacy_json_compatibility(
    migrated_database_url: str,
    tmp_path: Path,
) -> None:
    runner = CliRunner()
    current = runner.invoke(
        app,
        [
            "run",
            "profile current",
            "--agent",
            "coder",
            "--approval-mode",
            "full",
            "--write-scope",
            "src",
            "--command-profile",
            "pytest",
            "--offline-fake",
            "--no-stream",
            "--workspace",
            str(tmp_path),
            "--database-url",
            migrated_database_url,
        ],
    )
    assert current.exit_code == 0, current.output
    assert "Deprecated:" not in current.output

    legacy_json = runner.invoke(
        app,
        [
            "run",
            "profile legacy",
            "--agent",
            "coder",
            "--permission-mode",
            "full",
            "--allow-write",
            "src",
            "--allow-command",
            "pytest",
            "--offline-fake",
            "--json",
            "--workspace",
            str(tmp_path),
            "--database-url",
            migrated_database_url,
        ],
    )
    assert legacy_json.exit_code == 0, legacy_json.output
    assert "Deprecated:" not in legacy_json.output
    assert json.loads(legacy_json.output)["run"]["status"] == "completed"


def test_stage_922_chat_uses_the_same_profile_options(
    migrated_database_url: str,
    tmp_path: Path,
) -> None:
    result = CliRunner().invoke(
        app,
        [
            "chat",
            "--agent",
            "coder",
            "--approval-mode",
            "request",
            "--write-scope",
            "src",
            "--command-profile",
            "pytest",
            "--offline-fake",
            "--workspace",
            str(tmp_path),
            "--database-url",
            migrated_database_url,
        ],
        input="shared profile\n/exit\n",
    )

    assert result.exit_code == 0, result.output
    assert "Offline chat response" in result.output
    assert "Deprecated:" not in result.output


def test_stage_922_profile_rejects_agent_capability_escalation(
    migrated_database_url: str,
    tmp_path: Path,
) -> None:
    runner = CliRunner()
    reviewer_write = runner.invoke(
        app,
        [
            "run",
            "must reject",
            "--agent",
            "reviewer",
            "--write-scope",
            ".",
            "--offline-fake",
            "--workspace",
            str(tmp_path),
            "--database-url",
            migrated_database_url,
        ],
    )
    coder_network = runner.invoke(
        app,
        [
            "run",
            "must reject",
            "--agent",
            "coder",
            "--network-domain",
            "example.com",
            "--offline-fake",
            "--workspace",
            str(tmp_path),
            "--database-url",
            migrated_database_url,
        ],
    )

    assert reviewer_write.exit_code == 1
    assert "does not permit --write-scope" in reviewer_write.output
    assert coder_network.exit_code == 1
    assert "does not permit --network-domain" in coder_network.output


def test_stage_922_help_hides_legacy_options_and_groups_advanced_capabilities() -> None:
    help_result = CliRunner().invoke(app, ["run", "--help"])

    assert help_result.exit_code == 0, help_result.output
    assert "--approval-mode" in help_result.output
    assert "--write-scope" in help_result.output
    assert "--command-profile" in help_result.output
    assert "Capabilities" in help_result.output
    assert "Advanced" in help_result.output
    assert "--permission-mode" not in help_result.output
    assert "--allow-write" not in help_result.output
    assert "--allow-command" not in help_result.output


def test_run_failure_prints_stable_run_conversation_status_and_error_code(
    migrated_database_url: str,
    tmp_path: Path,
) -> None:
    result = CliRunner().invoke(
        app,
        [
            "run",
            "cannot start",
            "--agent",
            "missing-agent",
            "--offline-fake",
            "--workspace",
            str(tmp_path),
            "--database-url",
            migrated_database_url,
        ],
    )

    assert result.exit_code == 1
    assert re.search(r"run_id=[0-9a-f-]{36}", result.output)
    assert re.search(r"conversation_id=[0-9a-f-]{36}", result.output)
    assert "status=failed" in result.output
    assert "error_code=agent_not_found" in result.output


def test_resume_requires_an_explicit_approval_decision() -> None:
    result = CliRunner().invoke(
        app,
        ["resume", str(uuid4()), "--approval-id", str(uuid4())],
    )

    assert result.exit_code == 1
    assert "requires an explicit --decision" in result.output


def test_run_json_events_is_stable_ndjson_without_human_footer(
    migrated_database_url: str,
    tmp_path: Path,
) -> None:
    result = CliRunner().invoke(
        app,
        [
            "run",
            "分析项目并给出规划",
            "--offline-fake",
            "--json-events",
            "--workspace",
            str(tmp_path),
            "--database-url",
            migrated_database_url,
        ],
    )

    assert result.exit_code == 0, result.output
    events = [json.loads(line) for line in result.output.splitlines() if line.strip()]
    assert events
    assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))
    assert any(event["event_type"] == "model.text_delta" for event in events)
    assert any(event["event_type"] == "run.completed" for event in events)
    assert "run_id=" not in result.output


def test_run_json_events_failure_is_one_machine_readable_record(
    migrated_database_url: str,
    tmp_path: Path,
) -> None:
    result = CliRunner().invoke(
        app,
        [
            "run",
            "cannot start",
            "--agent",
            "missing-agent",
            "--offline-fake",
            "--json-events",
            "--workspace",
            str(tmp_path),
            "--database-url",
            migrated_database_url,
        ],
    )

    assert result.exit_code == 1
    records = [json.loads(line) for line in result.output.splitlines() if line.strip()]
    assert len(records) == 1
    assert records[0]["status"] == "failed"
    assert records[0]["error_code"] == "agent_not_found"
    assert "Run failed" not in result.output


def test_resource_cli_json_and_error_exit_codes(
    migrated_database_url: str,
) -> None:
    runner = CliRunner()
    agents = runner.invoke(
        app,
        [
            "agents",
            "list",
            "--offline-fake",
            "--database-url",
            migrated_database_url,
            "--json",
        ],
    )
    assert agents.exit_code == 0, agents.output
    agent_values = json.loads(agents.output)
    assert any(item["id"] == "coordinator" for item in agent_values)
    assert any(item["id"] == "summarizer" for item in agent_values)
    coordinator = next(item for item in agent_values if item["id"] == "coordinator")
    assert coordinator["source"] == "builtin"
    assert coordinator["visibility"] == "public"
    assert coordinator["configured_model_ref"] == "offline_fake"
    assert coordinator["max_children"] == 0

    public_agents = runner.invoke(
        app,
        [
            "agents",
            "list",
            "--offline-fake",
            "--database-url",
            migrated_database_url,
        ],
    )
    assert public_agents.exit_code == 0, public_agents.output
    assert "coordinator" in public_agents.output
    assert "summarizer" not in public_agents.output
    assert "finalizer" not in public_agents.output
    assert "configured=offline_fake" in public_agents.output

    tools = runner.invoke(
        app,
        [
            "tools",
            "list",
            "--offline-fake",
            "--database-url",
            migrated_database_url,
            "--json",
        ],
    )
    assert tools.exit_code == 0, tools.output
    assert any(item["name"] == "workspace.read" for item in json.loads(tools.output))

    missing = runner.invoke(
        app,
        ["inspect", str(uuid4()), "--database-url", migrated_database_url],
    )
    assert missing.exit_code == 1
    assert "run" in missing.output.casefold()
