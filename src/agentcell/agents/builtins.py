"""Built-in Agent declarations with explicit least-authority roles."""

from __future__ import annotations

from agentcell.agents.models import AgentSpec
from agentcell.policy import Capability


def coordinator_spec(*, model_ref: str, collaborative: bool = False) -> AgentSpec:
    """Return a coordinator; delegation remains opt-in for existing callers."""

    tools = ["workspace.list", "workspace.read", "workspace.search"]
    capabilities = {Capability.FILESYSTEM_READ}
    if collaborative:
        tools.append("agent.delegate")
        capabilities.add(Capability.AGENT_DELEGATE)

    return AgentSpec(
        id="coordinator",
        name="Coordinator",
        description="Plans and completes one local software-project task.",
        model_ref=model_ref,
        instructions=(
            "Work only inside the supplied workspace. Inspect strategically: begin at the "
            "workspace root, prefer search when you know a filename or symbol, read only files "
            "that materially affect the answer, and avoid duplicate or exhaustive directory "
            "traversal. Keep enough tool-call budget to synthesize the result. Never claim to "
            "have modified files. Return a concise final result."
        ),
        tools=tuple(tools),
        capabilities=frozenset(capabilities),
        max_steps=20,
        max_children=3 if collaborative else 0,
        max_depth=2 if collaborative else 0,
    )


def coder_spec(*, model_ref: str) -> AgentSpec:
    return AgentSpec(
        id="coder",
        name="Coder",
        description="Implements scoped code changes and runs approved checks.",
        model_ref=model_ref,
        instructions=(
            "Implement only the assigned task inside the supplied workspace. Reuse existing "
            "components and tests. Report changed files and verification accurately."
        ),
        tools=(
            "workspace.list",
            "workspace.read",
            "workspace.search",
            "workspace.write",
            "workspace.patch",
            "workspace.delete",
            "shell.test",
        ),
        capabilities=frozenset(
            {
                Capability.FILESYSTEM_READ,
                Capability.FILESYSTEM_WRITE,
                Capability.SHELL_EXECUTE,
            }
        ),
        max_steps=30,
    )


def reviewer_spec(*, model_ref: str) -> AgentSpec:
    """Reviewer is structurally read-only, independent of its Run lease."""

    return AgentSpec(
        id="reviewer",
        name="Reviewer",
        description="Performs independent read-only correctness and security review.",
        model_ref=model_ref,
        instructions=(
            "Review the assigned changes without modifying files or executing commands. Return "
            "specific findings, regression risks, and a clear pass or changes-needed decision."
        ),
        tools=("workspace.list", "workspace.read", "workspace.search"),
        capabilities=frozenset({Capability.FILESYSTEM_READ}),
        max_steps=20,
    )


def researcher_spec(*, model_ref: str) -> AgentSpec:
    return AgentSpec(
        id="researcher",
        name="Researcher",
        description="Collects read-only workspace and approved network evidence.",
        model_ref=model_ref,
        instructions="Collect evidence, cite its origin, and do not modify the workspace.",
        tools=("workspace.list", "workspace.read", "workspace.search", "http.request"),
        capabilities=frozenset({Capability.FILESYSTEM_READ, Capability.NETWORK_REQUEST}),
        max_steps=20,
    )


def summarizer_spec(*, model_ref: str) -> AgentSpec:
    return AgentSpec(
        id="summarizer",
        name="Summarizer",
        description="Produces a bounded low-cost task summary without tools.",
        model_ref=model_ref,
        instructions="Summarize facts and outcomes concisely. Do not invent actions or results.",
        max_steps=5,
    )


def finalizer_spec(*, model_ref: str) -> AgentSpec:
    return AgentSpec(
        id="finalizer",
        name="Finalizer",
        description="Synthesizes coordinator, coder, and reviewer results.",
        model_ref=model_ref,
        instructions=(
            "Produce the final handoff from the supplied stage results. Do not modify files and "
            "do not claim checks that are not present in the evidence."
        ),
        tools=("workspace.list", "workspace.read", "workspace.search"),
        capabilities=frozenset({Capability.FILESYSTEM_READ}),
        max_steps=15,
    )
