"""Stage 5 CLI verifies the direct, offline RunService entry point."""

from __future__ import annotations

from pathlib import Path

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
        ],
    )

    assert result.exit_code == 0, result.output
    assert '"status": "completed"' in result.output
    assert '"output": "Offline result: inspect project"' in result.output
    assert '"requests": 1' in result.output
