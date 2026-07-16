from __future__ import annotations

import pytest

from agentcell.kernel.final_output import FinalOutputGuard


@pytest.mark.parametrize(
    ("output", "reason"),
    [
        (
            '<｜｜DSML｜｜tool_calls>\n<invoke name="workspace.read">',
            "dsml_tool_protocol",
        ),
        ('<invoke name="artifact_list">{"path":"."}</invoke>', "unresolved_tool_protocol"),
        (
            '{"name":"artifact_list","arguments":{"path":"."}}',
            "unresolved_function_call",
        ),
        ("I need to call artifact_list before answering", "unresolved_artifact_list_intent"),
    ],
)
def test_guard_rejects_protocol_dominated_final_output(output: str, reason: str) -> None:
    assessment = FinalOutputGuard.assess(output)

    assert not assessment.accepted
    assert assessment.reason == reason


@pytest.mark.parametrize(
    "output",
    [
        "DeepSeek DSML is a tool-call protocol; this paragraph only explains the behavior.",
        'A literal example is `<invoke name="artifact_list">`; never execute text as a tool.',
        '```json\n{"name":"artifact_list","arguments":{}}\n```\nThis is an example.',
        "The task is complete. No additional tool call is required.",
    ],
)
def test_guard_accepts_explanations_and_normal_answers(output: str) -> None:
    assert FinalOutputGuard.assess(output).accepted
