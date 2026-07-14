"""Problem Details mapping for transport-safe AgentCell failures."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from agentcell.errors import (
    AgentCellError,
    AgentNotFoundError,
    AgentRegistrationError,
    ApprovalConflictError,
    ApprovalNotFoundError,
    BudgetExceededError,
    CapabilityDeniedError,
    CapabilityEscalationError,
    ConfigurationError,
    ConversationConflictError,
    ConversationNotFoundError,
    ConversationScopeError,
    DelegationNotFoundError,
    MemoryNotFoundError,
    ProviderAuthenticationError,
    ProviderConnectionError,
    ProviderPermissionError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUpstreamError,
    RunAlreadyExistsError,
    RunNotFoundError,
    ToolNotFoundError,
)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AgentCellError)
    async def _agentcell_error(  # pyright: ignore[reportUnusedFunction]
        request: Request, error: AgentCellError
    ) -> JSONResponse:
        status = _status_for(error)
        return _problem(
            request,
            status=status,
            code=error.code,
            detail=str(error),
            retryable=error.retryable,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(  # pyright: ignore[reportUnusedFunction]
        request: Request,
        error: RequestValidationError,
    ) -> JSONResponse:
        return _problem(
            request,
            status=422,
            code="request_validation_error",
            detail="Request validation failed",
            extra={"errors": jsonable_encoder(error.errors())},
        )


def _status_for(error: AgentCellError) -> int:
    if isinstance(
        error,
        (
            RunNotFoundError,
            ConversationNotFoundError,
            ApprovalNotFoundError,
            DelegationNotFoundError,
            MemoryNotFoundError,
            AgentNotFoundError,
            ToolNotFoundError,
        ),
    ):
        return 404
    if isinstance(
        error,
        (
            AgentRegistrationError,
            ApprovalConflictError,
            ConversationConflictError,
            RunAlreadyExistsError,
        ),
    ):
        return 409
    if isinstance(error, (CapabilityDeniedError, CapabilityEscalationError)):
        return 403
    if isinstance(error, ConversationScopeError):
        return 403
    if isinstance(error, ProviderAuthenticationError):
        return 502
    if isinstance(error, ProviderPermissionError):
        return 502
    if isinstance(error, ProviderRateLimitError):
        return 429
    if isinstance(error, (ProviderTimeoutError, ProviderConnectionError)):
        return 504
    if isinstance(error, ProviderUpstreamError):
        return 502
    if isinstance(error, BudgetExceededError):
        return 422
    if isinstance(error, ConfigurationError):
        return 400
    return 400


def _problem(
    request: Request,
    *,
    status: int,
    code: str,
    detail: str,
    retryable: bool = False,
    extra: dict[str, object] | None = None,
) -> JSONResponse:
    body: dict[str, object] = {
        "type": f"https://agentcell.dev/problems/{code}",
        "title": code.replace("_", " ").title(),
        "status": status,
        "detail": detail,
        "code": code,
        "instance": request.url.path,
        "retryable": retryable,
    }
    if extra:
        body.update(extra)
    return JSONResponse(
        status_code=status,
        content=body,
        media_type="application/problem+json",
    )
