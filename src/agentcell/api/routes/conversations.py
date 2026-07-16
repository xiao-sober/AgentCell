"""Conversation thread and fresh-Run turn endpoints."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, status

from agentcell.api.dependencies import ApplicationDependency
from agentcell.api.schemas import (
    ConversationCreateRequest,
    ConversationMessageResponse,
    ConversationResponse,
    ConversationTurnRequest,
    RunResponse,
)
from agentcell.conversations import ConversationRoutingMode
from agentcell.routing import TaskRouteStatus

router = APIRouter(prefix="/conversations", tags=["conversations"])


@router.post("", response_model=ConversationResponse, status_code=status.HTTP_201_CREATED)
async def create_conversation(
    body: ConversationCreateRequest,
    application: ApplicationDependency,
) -> ConversationResponse:
    conversation = await application.conversations.create(**body.model_dump())
    return ConversationResponse.from_domain(conversation)


@router.get("", response_model=list[ConversationResponse])
async def list_conversations(
    user_id: Annotated[UUID, Query()],
    application: ApplicationDependency,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
) -> list[ConversationResponse]:
    conversations = await application.conversations.list(user_id, limit=limit)
    return [ConversationResponse.from_domain(item) for item in conversations]


@router.get("/{conversation_id}", response_model=ConversationResponse)
async def get_conversation(
    conversation_id: UUID,
    user_id: Annotated[UUID, Query()],
    application: ApplicationDependency,
) -> ConversationResponse:
    conversation = await application.conversations.get(conversation_id, user_id=user_id)
    return ConversationResponse.from_domain(conversation)


@router.get("/{conversation_id}/messages", response_model=list[ConversationMessageResponse])
async def list_messages(
    conversation_id: UUID,
    user_id: Annotated[UUID, Query()],
    application: ApplicationDependency,
    limit: Annotated[int, Query(ge=1, le=500)] = 500,
) -> list[ConversationMessageResponse]:
    messages = await application.conversations.messages(
        conversation_id, user_id=user_id, limit=limit
    )
    return [ConversationMessageResponse.from_domain(item) for item in messages]


@router.post(
    "/{conversation_id}/runs",
    response_model=RunResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_conversation_run(
    conversation_id: UUID,
    body: ConversationTurnRequest,
    application: ApplicationDependency,
) -> RunResponse:
    values = body.model_dump(exclude={"budget"})
    if body.budget is not None:
        values["budget"] = body.budget
    conversation = await application.conversations.get(
        conversation_id,
        user_id=body.user_id,
    )
    if (
        conversation.routing_mode is ConversationRoutingMode.AUTO
        or conversation.team_id is not None
    ):
        values["budget"] = body.budget or application.teams.get("software").default_budget
        if application.conversations.should_use_direct_turn(
            conversation,
            prompt=body.prompt,
            agent_id=body.agent_id,
            team_id=body.team_id,
        ):
            prepared = await application.conversations.prepare_direct_turn(
                conversation_id,
                prompt=body.prompt,
                user_id=body.user_id,
                permission_mode=body.permission_mode,
                budget=values["budget"],
                model_ref=body.model_ref,
                run_id=body.run_id,
            )
            run = await application.start_conversation_turn(prepared)
        else:
            prepared_route = await application.conversations.prepare_routed_turn(
                conversation_id,
                **values,
            )
            if prepared_route.decision.status is TaskRouteStatus.READY:
                run = await application.start_routed_conversation_turn(prepared_route)
            else:
                run = prepared_route.root
    else:
        values.pop("agent_id", None)
        values.pop("team_id", None)
        prepared = await application.conversations.prepare_turn(conversation_id, **values)
        run = await application.start_conversation_turn(prepared)
    return RunResponse.from_domain(run)
