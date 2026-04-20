from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter


class FakeUpgradeScout:
    def status(self):
        return {
            "enabled": True,
            "cron_job_id": "job_1",
            "latest_report": {"id": "report_1", "risk": "medium"},
            "pending_approval_count": 1,
        }

    def run_report(self):
        return {
            "report": {"id": "report_1", "risk": "medium"},
            "brief": "# Brief\n",
        }

    def list_reports(self):
        return [{"id": "report_1", "risk": "medium"}]

    def get_report(self, report_id):
        return {"report": {"id": report_id}, "brief": "# Brief\n"}

    def approve_report(self, report_id):
        return {"id": "apply_1", "report_id": report_id, "status": "merge_ready"}

    def get_apply_run(self, run_id):
        return {"id": run_id, "status": "merge_ready"}


def test_path_param_supports_fastapi_and_aiohttp_shapes():
    adapter = APIServerAdapter(PlatformConfig(enabled=True))

    class FastAPIRequest:
        path_params = {"run_id": "run_fastapi"}

    class MatchInfo:
        def get(self, name, default=None):
            return {"run_id": "run_aiohttp"}.get(name, default)

    class AioHTTPRequest:
        match_info = MatchInfo()

    assert adapter._path_param(FastAPIRequest(), "run_id") == "run_fastapi"
    assert adapter._path_param(AioHTTPRequest(), "run_id") == "run_aiohttp"
    assert adapter._path_param(object(), "run_id", "missing") == "missing"


@pytest.mark.asyncio
async def test_health_includes_companion_capability_contract(monkeypatch):
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    monkeypatch.setattr(
        adapter,
        "_collect_capability_metadata",
        lambda: {
            "schema_version": 2,
            "configured_model": "nous/hermes-4",
            "enabled_toolsets": ["web", "image_gen"],
            "endpoints": ["/v1/chat/completions", "/api/jobs"],
            "providers": {"readiness": {"configured": True}},
            "messaging": {"platforms": [{"key": "telegram", "configured": False}]},
            "gateway": {"runtime_state": "running", "service_running": True},
            "cron": {"available": True, "jobs_total": 2, "jobs_active": 1},
            "tool_gateway": {
                "available": True,
                "features": [
                    {
                        "key": "web",
                        "label": "Web Search",
                        "available": True,
                        "active": True,
                        "managed_by_nous": True,
                        "direct_override": False,
                        "toolset_enabled": True,
                        "current_provider": "nous",
                    }
                ],
            },
            "surfaces": {
                "tui": {"available": True, "command": "npm run start"},
                "web_dashboard": {"available": True, "command": "hermes dashboard"},
            },
            "hermes_home": "/tmp/hermes",
            "errors": [],
        },
    )

    app = FastAPI()
    app.add_api_route("/health", adapter._handle_health, methods=["GET"])

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health")

    assert response.status_code == 200
    body = response.json()
    capabilities = body["capabilities"]
    assert body["version"] == "0.10.0+hermes.local"
    assert capabilities["schema_version"] == 2
    assert capabilities["tool_gateway"]["available"] is True
    assert capabilities["cron"]["jobs_active"] == 1
    assert capabilities["surfaces"]["web_dashboard"]["command"] == "hermes dashboard"


@pytest.mark.asyncio
async def test_upgrade_scout_gateway_contract(monkeypatch):
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    monkeypatch.setattr(adapter, "_upgrade_scout", lambda: FakeUpgradeScout())

    app = FastAPI()
    app.add_api_route("/api/upgrade-scout/status", adapter._handle_upgrade_scout_status, methods=["GET"])
    app.add_api_route("/api/upgrade-scout/run", adapter._handle_upgrade_scout_run, methods=["POST"])
    app.add_api_route("/api/upgrade-scout/reports", adapter._handle_upgrade_scout_reports, methods=["GET"])
    app.add_api_route("/api/upgrade-scout/reports/{report_id}", adapter._handle_upgrade_scout_report, methods=["GET"])
    app.add_api_route("/api/upgrade-scout/reports/{report_id}/approve", adapter._handle_upgrade_scout_approve, methods=["POST"])
    app.add_api_route("/api/upgrade-scout/apply-runs/{run_id}", adapter._handle_upgrade_scout_apply_run, methods=["GET"])

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        status = await client.get("/api/upgrade-scout/status")
        run = await client.post("/api/upgrade-scout/run")
        reports = await client.get("/api/upgrade-scout/reports")
        detail = await client.get("/api/upgrade-scout/reports/report_1")
        approval = await client.post("/api/upgrade-scout/reports/report_1/approve")
        apply_run = await client.get("/api/upgrade-scout/apply-runs/apply_1")

    assert status.status_code == 200
    assert status.json()["enabled"] is True
    assert run.status_code == 201
    assert reports.json()["reports"][0]["id"] == "report_1"
    assert detail.json()["report"]["id"] == "report_1"
    assert approval.status_code == 202
    assert apply_run.json()["status"] == "merge_ready"
