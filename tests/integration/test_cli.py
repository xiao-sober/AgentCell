"""Stage 5 CLI verifies the direct, offline RunService entry point."""

from __future__ import annotations

import json
import re
from pathlib import Path
from uuid import uuid4

from typer.testing import CliRunner

from agentcell.cli.app import app


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
    assert '"max_output_tokens": 40000' in result.output
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
        input="first question\n/exit\n",
    )
    assert first.exit_code == 0, first.output
    assert "Offline chat response" in first.output
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
        input="follow-up question\n/exit\n",
    )
    assert second.exit_code == 0, second.output
    assert f"conversation_id={match.group(1)}" in second.output
    assert "Offline chat response" in second.output
    assert "Continue with:" in second.output
    assert f"chat --conversation-id {match.group(1)}" in second.output


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


def test_run_json_events_is_stable_ndjson_without_human_footer(
    migrated_database_url: str,
    tmp_path: Path,
) -> None:
    result = CliRunner().invoke(
        app,
        [
            "run",
            "stream events",
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
    assert any(item["id"] == "coordinator" for item in json.loads(agents.output))

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
