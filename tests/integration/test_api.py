"""Stage 9 FastAPI resources and restart-safe AG-UI/SSE integration."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from agentcell.api import create_app
from agentcell.application import build_application


@pytest.mark.asyncio
async def test_run_api_problem_details_and_agui_reconnect(
    migrated_database_url: str,
    tmp_path: Path,
) -> None:
    application = await build_application(
        database_url=migrated_database_url,
        offline_fake=True,
        fake_output="API streamed result",
    )
    api = create_app(application)
    async with api.router.lifespan_context(api):
        async with AsyncClient(
            transport=ASGITransport(app=api),
            base_url="http://test",
        ) as client:
            missing = await client.get(f"/api/runs/{uuid4()}")
            assert missing.status_code == 404
            assert missing.headers["content-type"].startswith("application/problem+json")
            assert missing.json()["code"] == "run_not_found"

            created = await client.post(
                "/api/runs",
                json={
                    "prompt": "stream this",
                    "workspace": str(tmp_path),
                },
            )
            assert created.status_code == 202, created.text
            run_id = created.json()["id"]

            current = await client.get(f"/api/runs/{run_id}")
            for _ in range(100):
                if current.json()["status"] == "completed":
                    break
                await asyncio.sleep(0.01)
                current = await client.get(f"/api/runs/{run_id}")
            assert current.json()["status"] == "completed"

            streamed = await client.get(f"/api/runs/{run_id}/events")
            assert streamed.status_code == 200
            assert '"type":"RUN_STARTED"' in streamed.text
            assert '"type":"TEXT_MESSAGE_CONTENT"' in streamed.text
            assert "API streamed result" in streamed.text
            assert '"type":"RUN_FINISHED"' in streamed.text

            event_ids = [
                line.removeprefix("id: ")
                for line in streamed.text.splitlines()
                if line.startswith("id: ")
            ]
            assert event_ids == sorted(
                event_ids,
                key=lambda value: tuple(int(part) for part in value.split(".")),
            )

            resumed = await client.get(
                f"/api/runs/{run_id}/events",
                headers={"Last-Event-ID": event_ids[-2]},
            )
            assert resumed.status_code == 200
            resumed_ids = [
                line.removeprefix("id: ")
                for line in resumed.text.splitlines()
                if line.startswith("id: ")
            ]
            assert resumed_ids == [event_ids[-1]]


@pytest.mark.asyncio
async def test_resources_hide_secrets_and_persist_agent_definitions(
    migrated_database_url: str,
) -> None:
    application = await build_application(
        database_url=migrated_database_url,
        offline_fake=True,
    )
    api = create_app(application)
    custom = {
        "id": "api-reviewer",
        "name": "API Reviewer",
        "description": "Read-only API-managed reviewer.",
        "model_ref": "offline_fake",
        "instructions": "Review without modifying files.",
        "tools": ["workspace.read", "workspace.search"],
        "capabilities": ["filesystem.read"],
        "max_steps": 8,
        "max_children": 0,
        "max_depth": 0,
    }
    async with api.router.lifespan_context(api):
        async with AsyncClient(
            transport=ASGITransport(app=api),
            base_url="http://test",
        ) as client:
            providers = await client.get("/api/providers")
            assert providers.status_code == 200
            serialized = json.dumps(providers.json()).casefold()
            assert "api_key" not in serialized
            assert "api_key_env" not in serialized

            created = await client.post("/api/agents", json={"spec": custom})
            assert created.status_code == 201, created.text
            duplicate = await client.post("/api/agents", json={"spec": custom})
            assert duplicate.status_code == 409
            assert duplicate.json()["code"] == "agent_registration_error"

            tools = await client.get("/api/tools")
            assert tools.status_code == 200
            assert any(item["name"] == "workspace.read" for item in tools.json())

    restarted = await build_application(
        database_url=migrated_database_url,
        offline_fake=True,
    )
    try:
        assert restarted.agents.get("api-reviewer").name == "API Reviewer"
    finally:
        await restarted.close()


@pytest.mark.asyncio
async def test_conversation_api_creates_fresh_runs_and_ordered_messages(
    migrated_database_url: str,
    tmp_path: Path,
) -> None:
    application = await build_application(
        database_url=migrated_database_url,
        offline_fake=True,
        fake_output="thread reply",
    )
    api = create_app(application)
    user_id = uuid4()
    async with api.router.lifespan_context(api):
        async with AsyncClient(
            transport=ASGITransport(app=api),
            base_url="http://test",
        ) as client:
            created = await client.post(
                "/api/conversations",
                json={"user_id": str(user_id), "workspace": str(tmp_path)},
            )
            assert created.status_code == 201, created.text
            conversation_id = created.json()["id"]

            bypass = await client.post(
                "/api/runs",
                json={
                    "prompt": "must not bypass history",
                    "workspace": str(tmp_path),
                    "conversation_id": conversation_id,
                },
            )
            assert bypass.status_code == 409
            assert bypass.json()["code"] == "conversation_conflict"

            run_ids: list[str] = []
            for prompt in ("first", "follow-up"):
                started = await client.post(
                    f"/api/conversations/{conversation_id}/runs",
                    json={"user_id": str(user_id), "prompt": prompt},
                )
                assert started.status_code == 202, started.text
                run_id = started.json()["id"]
                run_ids.append(run_id)
                current = await client.get(f"/api/runs/{run_id}")
                for _ in range(100):
                    if current.json()["status"] == "completed":
                        break
                    await asyncio.sleep(0.01)
                    current = await client.get(f"/api/runs/{run_id}")
                assert current.json()["status"] == "completed"

            messages = await client.get(
                f"/api/conversations/{conversation_id}/messages",
                params={"user_id": str(user_id)},
            )
            assert messages.status_code == 200, messages.text
            values = messages.json()
            assert [item["sequence"] for item in values] == list(range(1, len(values) + 1))
            assert {item["run_id"] for item in values} == set(run_ids)

            forbidden = await client.get(
                f"/api/conversations/{conversation_id}",
                params={"user_id": str(uuid4())},
            )
            assert forbidden.status_code == 403
            assert forbidden.json()["code"] == "conversation_scope_mismatch"
