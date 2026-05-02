import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from hermes_personal import HermesEventLog, PersonalArtifactStore


def _create_app(adapter: APIServerAdapter) -> FastAPI:
    app = FastAPI()
    app.add_api_route("/api/context/preview", adapter._handle_context_preview, methods=["GET"])
    app.add_api_route("/api/events", adapter._handle_events, methods=["GET"])
    app.add_api_route("/api/feedback/rating", adapter._handle_feedback_rating, methods=["POST"])
    app.add_api_route("/api/feedback/failure", adapter._handle_feedback_failure, methods=["POST"])
    app.add_api_route("/api/failures", adapter._handle_failures, methods=["GET"])
    app.add_api_route("/api/failures/{failure_id}", adapter._handle_failure_update, methods=["PATCH"])
    app.add_api_route("/api/personal/content", adapter._handle_personal_content, methods=["GET", "PUT"])
    app.add_api_route("/api/personal/tree", adapter._handle_personal_tree, methods=["GET"])
    return app


@pytest.fixture()
def personal_api(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    return _create_app(adapter)


@pytest.mark.asyncio
async def test_context_preview_events_and_personal_content(personal_api):
    PersonalArtifactStore().write_content("user/USER.md", "# User\n\nPrefers explicit plans.\n", source="test")
    HermesEventLog().append("agent.step", source="tool", summary="step captured", payload={"n": 1})

    async with AsyncClient(transport=ASGITransport(app=personal_api), base_url="http://test") as client:
        preview = await client.get("/api/context/preview")
        events = await client.get("/api/events", params={"type": "agent.step", "limit": "10"})
        tree = await client.get("/api/personal/tree")
        content = await client.get("/api/personal/content", params={"path": "user/USER.md"})
        blocked = await client.put("/api/personal/content", json={"path": "../outside.md", "content": "no"})

    assert preview.status_code == 200
    assert "Prefers explicit plans" in preview.text
    assert events.status_code == 200
    assert events.json()["events"][0]["type"] == "agent.step"
    assert tree.status_code == 200
    assert any(item["path"] == "user/USER.md" for item in tree.json()["files"])
    assert content.status_code == 200
    assert content.json()["content"].startswith("# User")
    assert blocked.status_code == 400


@pytest.mark.asyncio
async def test_feedback_rating_failure_and_validation(personal_api):
    async with AsyncClient(transport=ASGITransport(app=personal_api), base_url="http://test") as client:
        invalid = await client.post(
            "/api/feedback/rating",
            json={"session_id": "s1", "message_id": "m1", "rating": 11},
        )
        rating = await client.post(
            "/api/feedback/rating",
            json={"session_id": "s1", "message_id": "m1", "rating": 8, "comment": "useful"},
        )
        failure = await client.post(
            "/api/feedback/failure",
            json={"session_id": "s1", "message_id": "m1", "reason": "missed detail"},
        )
        reviewed = await client.patch(
            f"/api/failures/{failure.json()['failure']['id']}",
            json={"status": "reviewed"},
        )
        failures = await client.get("/api/failures")

    assert invalid.status_code == 400
    assert rating.status_code == 201
    assert rating.json()["rating"]["rating"] == 8
    assert failure.status_code == 201
    assert failure.json()["failure"]["status"] == "open"
    assert reviewed.status_code == 200
    assert reviewed.json()["failure"]["status"] == "reviewed"
    assert failures.status_code == 200
    assert failures.json()["failures"][0]["reason"] == "missed detail"
