"""Project-wide exception hierarchy for errors crossing AgentCell boundaries."""

from __future__ import annotations

from decimal import Decimal
from typing import ClassVar

type LimitValue = int | float | Decimal | None


class AgentCellError(Exception):
    """Base class for expected AgentCell failures."""

    code: ClassVar[str] = "agentcell_error"
    retryable: bool = False


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


class DomainError(AgentCellError):
    """Base class for invalid domain operations."""

    code = "domain_error"


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
