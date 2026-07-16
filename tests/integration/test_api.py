"""Stage 9 FastAPI resources and restart-safe AG-UI/SSE integration."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from agentcell.agents import AgentSource
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

            terminal_resume = await client.post(
                f"/api/runs/{run_id}/resume",
                json={},
            )
            assert terminal_resume.status_code == 409
            problem = terminal_resume.json()
            assert problem["code"] == "approval_conflict"
            assert problem["run_id"] == run_id
            assert problem["conversation_id"] == current.json()["conversation_id"]
            assert problem["run_status"] == "completed"

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
async def test_task_router_preview_and_authoritative_task_api(
    migrated_database_url: str,
    tmp_path: Path,
) -> None:
    application = await build_application(
        database_url=migrated_database_url,
        offline_fake=True,
        fake_output="routed API result",
    )
    api = create_app(application)
    root_run_id = uuid4()
    payload = {
        "task": "分析项目结构并给出规划",
        "workspace": str(tmp_path),
        "root_run_id": str(root_run_id),
    }
    async with api.router.lifespan_context(api):
        async with AsyncClient(
            transport=ASGITransport(app=api),
            base_url="http://test",
        ) as client:
            preview = await client.post("/api/task-routes", json=payload)
            assert preview.status_code == 200, preview.text
            assert preview.json()["authoritative"] is False
            assert preview.json()["run"] is None
            assert preview.json()["decision"]["agent_id"] == "coordinator"
            assert (await client.get(f"/api/runs/{root_run_id}")).status_code == 404

            created = await client.post("/api/tasks", json=payload)
            assert created.status_code == 202, created.text
            body = created.json()
            assert body["authoritative"] is True
            assert body["run"]["id"] == str(root_run_id)
            assert body["run"]["agent_id"] == "task-router"

            current = await client.get(f"/api/runs/{root_run_id}")
            for _ in range(100):
                if current.json()["status"] == "completed":
                    break
                await asyncio.sleep(0.01)
                current = await client.get(f"/api/runs/{root_run_id}")
            assert current.json()["status"] == "completed"

            streamed = await client.get(f"/api/runs/{root_run_id}/events")
            assert streamed.status_code == 200
            assert "routed API result" in streamed.text


@pytest.mark.asyncio
async def test_auto_conversation_routes_fresh_turns_and_preserves_binding(
    migrated_database_url: str,
    tmp_path: Path,
) -> None:
    application = await build_application(
        database_url=migrated_database_url,
        offline_fake=True,
        fake_output="auto conversation reply",
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
                json={
                    "user_id": str(user_id),
                    "workspace": str(tmp_path),
                    "routing_mode": "auto",
                },
            )
            assert created.status_code == 201, created.text
            conversation = created.json()
            assert conversation["agent_id"] == "task-router"
            assert conversation["routing_policy_version"] == "9.4.1-v1"

            turn = await client.post(
                f"/api/conversations/{conversation['id']}/runs",
                json={
                    "prompt": "分析项目结构并给出规划",
                    "user_id": str(user_id),
                },
            )
            assert turn.status_code == 202, turn.text
            run_id = turn.json()["id"]
            current = await client.get(f"/api/runs/{run_id}")
            for _ in range(100):
                current = await client.get(f"/api/runs/{run_id}")
                if current.json()["status"] == "completed":
                    break
                await asyncio.sleep(0.01)
            assert current.json()["status"] == "completed"

            messages = await client.get(
                f"/api/conversations/{conversation['id']}/messages",
                params={"user_id": str(user_id)},
            )
            for _ in range(100):
                messages = await client.get(
                    f"/api/conversations/{conversation['id']}/messages",
                    params={"user_id": str(user_id)},
                )
                if len(messages.json()) == 2:
                    break
                await asyncio.sleep(0.01)
            assert messages.status_code == 200
            assert [item["kind"] for item in messages.json()] == ["request", "response"]

            direct = await client.post(
                f"/api/conversations/{conversation['id']}/runs",
                json={"prompt": "你是谁？", "user_id": str(user_id)},
            )
            assert direct.status_code == 202, direct.text
            assert direct.json()["agent_id"] == "assistant"
            direct_run_id = direct.json()["id"]
            direct_current = await client.get(f"/api/runs/{direct_run_id}")
            for _ in range(100):
                direct_current = await client.get(f"/api/runs/{direct_run_id}")
                if direct_current.json()["status"] == "completed":
                    break
                await asyncio.sleep(0.01)
            assert direct_current.json()["status"] == "completed"

            direct_events = await client.get(f"/api/runs/{direct_run_id}/events")
            assert "task.route_proposed" not in direct_events.text
            assert "agent.child_started" not in direct_events.text


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
            public_agents = await client.get("/api/agents")
            assert public_agents.status_code == 200
            assert "summarizer" not in {item["id"] for item in public_agents.json()}
            all_agents = await client.get(
                "/api/agents",
                params={"include_internal": "true"},
            )
            assert all_agents.status_code == 200
            assert "summarizer" in {item["id"] for item in all_agents.json()}

            providers = await client.get("/api/providers")
            assert providers.status_code == 200
            serialized = json.dumps(providers.json()).casefold()
            assert "api_key" not in serialized
            assert "api_key_env" not in serialized

            created = await client.post("/api/agents", json={"spec": custom})
            assert created.status_code == 201, created.text
            assert application.agents.get_entry("api-reviewer").source is AgentSource.PERSISTED
            duplicate = await client.post("/api/agents", json={"spec": custom})
            assert duplicate.status_code == 409
            assert duplicate.json()["code"] == "agent_registration_error"

            tools = await client.get("/api/tools")
            assert tools.status_code == 200
            assert any(item["name"] == "workspace.read" for item in tools.json())

            coordinator = next(item for item in all_agents.json() if item["id"] == "coordinator")
            coordinator["name"] = "Persisted Coordinator Override"
            overridden = await client.put(
                "/api/agents/coordinator",
                json={"spec": coordinator},
            )
            assert overridden.status_code == 200, overridden.text
            assert application.agents.get_entry("coordinator").source is AgentSource.OVERRIDE

    restarted = await build_application(
        database_url=migrated_database_url,
        offline_fake=True,
    )
    try:
        assert restarted.agents.get("api-reviewer").name == "API Reviewer"
        assert restarted.agents.get("coordinator").name == "Persisted Coordinator Override"
        assert restarted.agents.get_entry("coordinator").source is AgentSource.OVERRIDE
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
            assert created.json()["model_ref"] == "offline_fake"

            model_drift = await client.post(
                f"/api/conversations/{conversation_id}/runs",
                json={
                    "user_id": str(user_id),
                    "prompt": "change the bound model",
                    "model_ref": "different_model",
                },
            )
            assert model_drift.status_code == 409
            assert model_drift.json()["code"] == "conversation_model_binding"

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
