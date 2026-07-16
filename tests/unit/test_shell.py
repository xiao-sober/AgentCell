"""Structured shell.test evidence distinguishes execution from inspection-only commands."""

from __future__ import annotations

import pytest

from agentcell.tools import (
    ShellRunResult,
    ShellTestResult,
    assess_test_execution,
    is_successful_test_result,
)


def _result(
    command: tuple[str, ...],
    *,
    exit_code: int,
    stdout: str,
) -> ShellRunResult:
    return ShellRunResult(
        command=command,
        cwd=".",
        exit_code=exit_code,
        stdout=stdout,
        stderr="",
        output_bytes=len(stdout.encode("utf-8")),
    )


@pytest.mark.parametrize("flag", ["--collect-only", "--co", "--collect-only=true"])
def test_pytest_collection_success_is_not_test_execution(flag: str) -> None:
    result = _result(
        ("pytest", "tests/", flag),
        exit_code=0,
        stdout="48 tests collected in 0.07s",
    )
    evidence = assess_test_execution(result)

    assert evidence.collected_only is True
    assert evidence.executed is False
    assert evidence.successful is False
    assert (
        is_successful_test_result(
            ShellTestResult(**result.model_dump(), test_execution=evidence).model_dump()
        )
        is False
    )


def test_executed_passing_pytest_run_is_structured_success() -> None:
    result = _result(
        ("pytest", "tests/", "-x", "--tb=short"),
        exit_code=0,
        stdout="================ 44 passed, 4 skipped in 1.72s ================",
    )
    evidence = assess_test_execution(result)

    assert evidence.executed is True
    assert evidence.successful is True
    assert evidence.collected_only is False
    assert evidence.summary == "================ 44 passed, 4 skipped in 1.72s ================"
    assert (
        is_successful_test_result(
            ShellTestResult(**result.model_dump(), test_execution=evidence).model_dump()
        )
        is True
    )


def test_failed_or_unknown_test_output_never_enables_fast_finalization() -> None:
    failed = _result(("python", "-m", "pytest", "tests/"), exit_code=1, stdout="1 failed")
    unknown = _result(("custom-test",), exit_code=0, stdout="all checks passed")

    failed_evidence = assess_test_execution(failed)
    unknown_evidence = assess_test_execution(unknown)

    assert failed_evidence.executed is True
    assert failed_evidence.successful is False
    assert unknown_evidence.executed is False
    assert unknown_evidence.successful is False
