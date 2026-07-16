"""Project-wide exception hierarchy for errors crossing AgentCell boundaries."""

from __future__ import annotations

from decimal import Decimal
from typing import ClassVar

type LimitValue = int | float | Decimal | None


class AgentCellError(Exception):
    """Base class for expected AgentCell failures."""

    code: ClassVar[str] = "agentcell_error"
    retryable: bool = False
    model_correctable: bool = False


class ConfigurationError(AgentCellError):
    """Raised when configuration cannot be validated or resolved safely."""

    code = "configuration_error"


class ProviderConfigurationError(ConfigurationError):
    """Raised when a Provider or model reference cannot be built safely."""

    code = "provider_configuration_error"

    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        model: str | None = None,
    ) -> None:
        self.provider = provider
        self.model = model
        super().__init__(message)


class ProviderError(AgentCellError):
    """Base class for sanitized failures returned by a model Provider."""

    code = "provider_error"

    def __init__(
        self,
        provider: str,
        model: str,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool | None = None,
    ) -> None:
        self.provider = provider
        self.model = model
        self.status_code = status_code
        if retryable is not None:
            self.retryable = retryable
        super().__init__(f"Provider {provider!r} model {model!r}: {message}")


class ProviderAuthenticationError(ProviderError):
    """Raised when a Provider rejects its API credential."""

    code = "provider_authentication_error"


class ProviderPermissionError(ProviderError):
    """Raised when a valid credential lacks access to a Provider resource."""

    code = "provider_permission_error"


class ProviderModelNotFoundError(ProviderError):
    """Raised when the configured model name or endpoint does not exist."""

    code = "provider_model_not_found"


class ProviderRateLimitError(ProviderError):
    """Raised for Provider throttling that may succeed after backoff."""

    code = "provider_rate_limit_error"
    retryable = True


class ProviderTimeoutError(ProviderError):
    """Raised when a Provider connection or response exceeds its timeout."""

    code = "provider_timeout_error"
    retryable = True


class ProviderConnectionError(ProviderError):
    """Raised when a Provider cannot be reached due to a transport failure."""

    code = "provider_connection_error"
    retryable = True


class ProviderContextLimitError(ProviderError):
    """Raised when a request exceeds the model context window."""

    code = "provider_context_limit_error"


class ProviderUpstreamError(ProviderError):
    """Raised for a classified 5xx Provider response."""

    code = "provider_upstream_error"


class ProviderProtocolError(ProviderError):
    """Raised for invalid requests or unexpected Provider response data."""

    code = "provider_protocol_error"


class AgentRegistrationError(ConfigurationError):
    """Raised for duplicate or internally inconsistent Agent declarations."""

    code = "agent_registration_error"


class AgentNotFoundError(ConfigurationError):
    """Raised when a stable Agent identifier is not registered."""

    code = "agent_not_found"

    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id
        super().__init__(f"Agent {agent_id!r} is not registered")


class TeamNotFoundError(ConfigurationError):
    """Raised when a stable Team identifier is not registered."""

    code = "team_not_found"

    def __init__(self, team_id: str) -> None:
        self.team_id = team_id
        super().__init__(f"Team {team_id!r} is not registered")


class TeamRegistrationError(ConfigurationError):
    """Raised for duplicate or internally inconsistent Team declarations."""

    code = "team_registration_error"


class ToolError(AgentCellError):
    """Base class for safe, classified tool-system failures."""

    code = "tool_error"


class ToolRegistrationError(ToolError):
    """Raised when a ToolRegistry definition is invalid or duplicated."""

    code = "tool_registration_error"


class ToolNotFoundError(ToolError):
    """Raised when a requested tool name is not registered."""

    code = "tool_not_found"

    def __init__(self, tool_name: str) -> None:
        self.tool_name = tool_name
        super().__init__(f"Tool {tool_name!r} is not registered")


class ToolArgumentsError(ToolError):
    """Raised when structured tool arguments fail schema validation."""

    code = "tool_arguments_invalid"

    def __init__(self, tool_name: str) -> None:
        self.tool_name = tool_name
        super().__init__(f"Tool {tool_name!r} received invalid arguments")


class CapabilityDeniedError(ToolError):
    """Raised when a Run lease does not grant a required capability."""

    code = "capability_denied"

    def __init__(self, capability: str) -> None:
        self.capability = capability
        super().__init__(f"Capability {capability!r} is not granted")


class CapabilityEscalationError(ToolError):
    """Raised when a child lease would exceed its parent's authority."""

    code = "capability_escalation"
    model_correctable = True

    def __init__(self, capability: str) -> None:
        self.capability = capability
        super().__init__(f"Child lease expands capability {capability!r}")


class ToolApprovalRequiredError(ToolError):
    """Raised when a guarded or dangerous tool lacks explicit approval."""

    code = "tool_approval_required"

    def __init__(self, tool_name: str, *, preview: object | None = None) -> None:
        self.tool_name = tool_name
        self.preview = preview
        super().__init__(f"Tool {tool_name!r} requires approval")


class ToolCallDeferredError(ToolError):
    """Raised when a tool has durable external work that must resume later."""

    code = "tool_call_deferred"

    def __init__(self, metadata: dict[str, object]) -> None:
        self.metadata = metadata
        super().__init__("Tool call is waiting for durable external work")


class ToolForbiddenError(ToolError):
    """Raised when policy marks a tool as unconditionally forbidden."""

    code = "tool_forbidden"

    def __init__(self, tool_name: str) -> None:
        self.tool_name = tool_name
        super().__init__(f"Tool {tool_name!r} is forbidden")


class ToolTimeoutError(ToolError):
    """Raised when a tool exceeds its declared execution timeout."""

    code = "tool_timeout"

    def __init__(self, tool_name: str, timeout_seconds: float) -> None:
        self.tool_name = tool_name
        self.timeout_seconds = timeout_seconds
        super().__init__(f"Tool {tool_name!r} exceeded its {timeout_seconds:g}s timeout")


class ToolExecutionError(ToolError):
    """Raised when tool implementation code fails unexpectedly."""

    code = "tool_execution_error"

    def __init__(self, tool_name: str) -> None:
        self.tool_name = tool_name
        super().__init__(f"Tool {tool_name!r} failed during execution")


class ToolOutputTooLargeError(ToolError):
    """Raised when oversized output has no Artifact Store destination."""

    code = "tool_output_too_large"

    def __init__(self, tool_name: str, actual_bytes: int, max_bytes: int) -> None:
        self.tool_name = tool_name
        self.actual_bytes = actual_bytes
        self.max_bytes = max_bytes
        super().__init__(
            f"Tool {tool_name!r} output is {actual_bytes} bytes; limit is {max_bytes} bytes"
        )


class WorkspacePathError(ToolError):
    """Base class for workspace path and content safety failures."""

    code = "workspace_path_error"


class WorkspacePathDeniedError(WorkspacePathError):
    """Raised for absolute, escaping, sensitive, or unleased paths."""

    code = "workspace_path_denied"

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"Workspace path denied: {reason}")


class WorkspaceLeaseMismatchError(WorkspacePathDeniedError):
    """A safe relative path is outside the Run's declared filesystem scopes."""

    code = "workspace_lease_mismatch"
    model_correctable = True


class WorkspacePathNotFoundError(WorkspacePathError):
    """Raised when an allowed workspace path does not exist."""

    code = "workspace_path_not_found"

    def __init__(self, path: str) -> None:
        self.path = path
        super().__init__(f"Workspace path {path!r} was not found")


class WorkspacePathTypeError(WorkspacePathError):
    """Raised when a file operation receives a directory or vice versa."""

    code = "workspace_path_type_error"
    model_correctable = True

    def __init__(self, path: str, expected: str) -> None:
        self.path = path
        self.expected = expected
        super().__init__(f"Workspace path {path!r} must be a {expected}")


class WorkspaceBinaryFileError(WorkspacePathError):
    """Raised when a text-only workspace tool encounters binary content."""

    code = "workspace_binary_file"

    def __init__(self, path: str) -> None:
        self.path = path
        super().__init__(f"Workspace path {path!r} is not a UTF-8 text file")


class WorkspacePatchConflictError(WorkspacePathError):
    """Raised when a structured patch no longer matches the expected file state."""

    code = "workspace_patch_conflict"

    def __init__(self, path: str, expected: int, actual: int) -> None:
        self.path = path
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"Workspace patch for {path!r} expected {expected} matches but found {actual}"
        )


class WorkspaceStateConflictError(WorkspacePathError):
    """Raised when a file changed after the state used to prepare approval."""

    code = "workspace_state_conflict"

    def __init__(self, path: str) -> None:
        super().__init__(f"Workspace file {path!r} changed since it was read or approved")


class ChangeConflictError(WorkspacePathError):
    """Raised when safe reconciliation or rollback would overwrite newer state."""

    code = "change_conflict"

    def __init__(self, path: str) -> None:
        super().__init__(f"File change for {path!r} conflicts with the current workspace state")


class ShellError(ToolError):
    """Base class for bounded subprocess execution failures."""

    code = "shell_error"


class ShellCommandDeniedError(ShellError):
    code = "shell_command_denied"

    def __init__(self, command: str) -> None:
        super().__init__(f"Shell command {command!r} is not granted by the Run lease")


class ShellCommandLeaseMismatchError(ShellCommandDeniedError):
    code = "shell_command_lease_mismatch"
    model_correctable = True


class ShellOutputTooLargeError(ShellError):
    code = "shell_output_too_large"

    def __init__(self, max_bytes: int) -> None:
        super().__init__(f"Shell output exceeded the {max_bytes} byte capture limit")


class HttpToolError(ToolError):
    """Base class for bounded outbound HTTP failures."""

    code = "http_tool_error"


class HttpRequestDeniedError(HttpToolError):
    code = "http_request_denied"


class HttpDomainLeaseMismatchError(HttpRequestDeniedError):
    code = "http_domain_lease_mismatch"
    model_correctable = True

    def __init__(self, reason: str) -> None:
        super().__init__(f"HTTP request denied: {reason}")


class HttpResponseTooLargeError(HttpToolError):
    code = "http_response_too_large"

    def __init__(self, max_bytes: int) -> None:
        super().__init__(f"HTTP response exceeded the {max_bytes} byte limit")


class DomainError(AgentCellError):
    """Base class for invalid domain operations."""

    code = "domain_error"


class ConversationError(DomainError):
    """Base class for scoped multi-turn Conversation failures."""

    code = "conversation_error"


class ConversationNotFoundError(ConversationError):
    code = "conversation_not_found"

    def __init__(self, conversation_id: str) -> None:
        self.conversation_id = conversation_id
        super().__init__(f"Conversation {conversation_id!r} was not found")


class ConversationConflictError(ConversationError):
    code = "conversation_conflict"


class ConversationModelBindingError(ConversationConflictError):
    """Raised when a turn would change or guess a Conversation's bound model."""

    code = "conversation_model_binding"


class ConversationScopeError(ConversationError):
    code = "conversation_scope_mismatch"


class InvalidStateTransitionError(DomainError):
    """Raised when a Run attempts a transition outside the lifecycle table."""

    code = "invalid_state_transition"

    def __init__(self, current_status: str, target_status: str) -> None:
        self.current_status = current_status
        self.target_status = target_status
        super().__init__(f"Run cannot transition from {current_status!r} to {target_status!r}")


class InvalidRunTimestampError(DomainError):
    """Raised when a Run update would move its UTC timestamp backwards."""

    code = "invalid_run_timestamp"

    def __init__(self) -> None:
        super().__init__("Run updated_at cannot be earlier than its current value")


class RunExecutionError(AgentCellError):
    """Raised when a Run fails outside an already classified boundary."""

    code = "run_execution_error"

    def __init__(self, message: str = "Run execution failed") -> None:
        super().__init__(message)


class RunIdentityMismatchError(AgentCellError):
    """Raised when restart recovery cannot prove the original execution identity."""

    code = "run_identity_mismatch"

    def __init__(self, message: str) -> None:
        super().__init__(message)


class ModelOutputError(AgentCellError):
    """Raised when the model exhausts its bounded final-output retries."""

    code = "model_output_invalid"

    def __init__(self, attempts: int) -> None:
        self.attempts = attempts
        super().__init__(
            f"Model did not produce an acceptable final response after {attempts} attempts"
        )


class InvalidFinalOutputError(AgentCellError):
    """Raised after unresolved tool-protocol text fails its one guarded retry."""

    code = "invalid_final_output"

    def __init__(self) -> None:
        super().__init__("Model returned unresolved tool protocol as its final response twice")


class ApprovalError(DomainError):
    """Base class for persisted approval workflow failures."""

    code = "approval_error"


class ApprovalNotFoundError(ApprovalError):
    """Raised when an approval identifier is unknown."""

    code = "approval_not_found"

    def __init__(self, approval_id: str) -> None:
        super().__init__(f"Approval {approval_id!r} was not found")


class ApprovalConflictError(ApprovalError):
    """Raised when a decision conflicts with persisted approval state."""

    code = "approval_conflict"


class ReplayError(DomainError):
    """Raised when an event stream cannot be replayed deterministically."""

    code = "replay_error"


class ToolReplayBlockedError(ToolError):
    """Raised when an ambiguous non-idempotent execution cannot be repeated safely."""

    code = "tool_replay_blocked"

    def __init__(self, tool_name: str, provider_call_id: str) -> None:
        super().__init__(
            f"Non-idempotent tool {tool_name!r} call {provider_call_id!r} cannot be replayed"
        )


class ArtifactError(AgentCellError):
    """Base class for bounded Artifact persistence failures."""

    code = "artifact_error"


class ArtifactNotFoundError(ArtifactError):
    code = "artifact_not_found"

    def __init__(self, artifact_id: str) -> None:
        super().__init__(f"Artifact {artifact_id!r} was not found")


class ArtifactIntegrityError(ArtifactError):
    code = "artifact_integrity_error"


class ArtifactTooLargeError(ArtifactError):
    code = "artifact_too_large"


class MemoryError(AgentCellError):
    """Base class for memory policy, storage, and retrieval failures."""

    code = "memory_error"


class MemoryNotFoundError(MemoryError):
    code = "memory_not_found"


class MemoryApprovalRequiredError(MemoryError):
    code = "memory_approval_required"


class MemoryPolicyRejectedError(MemoryError):
    code = "memory_policy_rejected"


class BudgetError(DomainError):
    """Base class for invalid or exhausted Run budgets."""

    code = "budget_error"


class InvalidBudgetUsageError(BudgetError):
    """Raised when a caller reports an invalid resource usage delta."""

    code = "invalid_budget_usage"

    def __init__(self, resource: str, value: object) -> None:
        self.resource = resource
        self.value = value
        super().__init__(f"Budget usage for {resource!r} must be a finite non-negative value")


class BudgetExceededError(BudgetError):
    """Raised when a resource reservation or recorded usage exceeds its limit."""

    code = "budget_exceeded"

    def __init__(self, resource: str, limit: LimitValue, attempted: LimitValue) -> None:
        self.resource = resource
        self.limit = limit
        self.attempted = attempted
        super().__init__(
            f"Budget for {resource!r} exceeded: limit={limit!s}, attempted={attempted!s}"
        )


class EventPayloadTypeError(DomainError):
    """Raised when an event type is paired with an incompatible payload schema."""

    code = "event_payload_type_error"

    def __init__(self, event_type: str, expected: str, actual: str) -> None:
        self.event_type = event_type
        self.expected = expected
        self.actual = actual
        super().__init__(f"Event {event_type!r} requires payload {expected!r}, received {actual!r}")


class EventPayloadTooLargeError(DomainError):
    """Raised when inline event data must be stored as an Artifact instead."""

    code = "event_payload_too_large"

    def __init__(self, actual_bytes: int, max_bytes: int) -> None:
        self.actual_bytes = actual_bytes
        self.max_bytes = max_bytes
        super().__init__(
            f"Event payload is {actual_bytes} bytes; inline limit is {max_bytes} bytes"
        )


class StorageError(AgentCellError):
    """Base class for classified persistence failures."""

    code = "storage_error"


class ChangeNotFoundError(StorageError):
    """Raised when a persisted FileChange identifier is unknown."""

    code = "change_not_found"

    def __init__(self, change_id: str) -> None:
        super().__init__(f"File change {change_id!r} was not found")


class DelegationNotFoundError(StorageError):
    """Raised when a durable Agent delegation cannot be found."""

    code = "delegation_not_found"

    def __init__(self, delegation_id: str) -> None:
        super().__init__(f"Delegation {delegation_id!r} was not found")


class CheckpointNotFoundError(StorageError):
    """Raised when a Run has no recoverable checkpoint."""

    code = "checkpoint_not_found"

    def __init__(self, run_id: str) -> None:
        super().__init__(f"Run {run_id!r} has no recoverable checkpoint")


class StorageIntegrityError(StorageError):
    """Raised when persisted data would violate a database integrity rule."""

    code = "storage_integrity_error"


class RunNotFoundError(StorageError):
    """Raised when a requested Run does not exist in storage."""

    code = "run_not_found"

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        super().__init__(f"Run {run_id!r} was not found")


class RunAlreadyExistsError(StorageIntegrityError):
    """Raised when creating a Run whose identifier already exists."""

    code = "run_already_exists"

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        super().__init__(f"Run {run_id!r} already exists")


class StoredEventDataError(StorageError):
    """Raised when a stored event cannot be restored through its payload schema."""

    code = "stored_event_data_error"

    def __init__(self, event_id: str) -> None:
        self.event_id = event_id
        super().__init__(f"Stored event {event_id!r} contains invalid data")


class InvalidEventCursorError(DomainError):
    """Raised when an event query cursor is negative or otherwise invalid."""

    code = "invalid_event_cursor"

    def __init__(self, after_sequence: object) -> None:
        self.after_sequence = after_sequence
        super().__init__("after_sequence must be a non-negative integer")
