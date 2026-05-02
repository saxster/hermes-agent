from __future__ import annotations

import pytest
from fastapi.responses import JSONResponse

starlette_requests = pytest.importorskip("starlette.requests")
Request = starlette_requests.Request

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from patterns import HERMES_PATTERN_MODEL_PREFIX


def write_pattern(home, name="summarize"):
    pattern_dir = home / "patterns" / name
    pattern_dir.mkdir(parents=True, exist_ok=True)
    (pattern_dir / "system.md").write_text("SUM {{input}}", encoding="utf-8")
    (pattern_dir / "metadata.yaml").write_text(
        f"name: {name}\ndescription: Summary\ndefault_model: pattern-model\n",
        encoding="utf-8",
    )


def request(method: str = "GET") -> Request:
    return Request({"type": "http", "method": method, "headers": [], "path": "/"})


@pytest.mark.asyncio
async def test_models_includes_pattern_models_when_exposed(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    write_pattern(tmp_path, "summarize")
    adapter = APIServerAdapter(PlatformConfig(enabled=True))

    response = await adapter._handle_models(request())
    data = response.body.decode()

    assert "hermes-agent" in data
    assert f"{HERMES_PATTERN_MODEL_PREFIX}summarize" in data


def test_chat_virtual_model_renders_pattern_prompt(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    write_pattern(tmp_path, "summarize")
    adapter = APIServerAdapter(PlatformConfig(enabled=True))

    rendered = adapter._render_pattern_for_chat(
        {
            "model": f"{HERMES_PATTERN_MODEL_PREFIX}summarize",
            "hermes": {"variables": {}},
        },
        f"{HERMES_PATTERN_MODEL_PREFIX}summarize",
        "hello",
    )

    assert rendered is not None
    assert rendered.system_prompt == "SUM hello"
    assert rendered.resolved_model == "pattern-model"


@pytest.mark.asyncio
async def test_pattern_render_endpoint_returns_prompt(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    write_pattern(tmp_path, "summarize")
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    req = Request(
        {
            "type": "http",
            "method": "POST",
            "headers": [(b"content-type", b"application/json")],
            "path": "/api/patterns/summarize/render",
            "path_params": {"name": "summarize"},
        },
        receive=_json_receive(b'{"input":"hello"}'),
    )

    response = await adapter._handle_pattern_render(req)

    assert response.status_code == 200
    assert "SUM hello" in response.body.decode()


@pytest.mark.asyncio
async def test_pattern_run_endpoint_forwards_as_virtual_model(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    write_pattern(tmp_path, "summarize")
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    captured = {}

    async def fake_chat(req):
        captured.update(await req.json())
        return JSONResponse({"ok": True})

    adapter._handle_chat_completions = fake_chat
    req = Request(
        {
            "type": "http",
            "method": "POST",
            "headers": [(b"content-type", b"application/json")],
            "path": "/api/patterns/summarize/run",
            "path_params": {"name": "summarize"},
        },
        receive=_json_receive(b'{"input":"hello","context":"research","variables":{"tone":"short"}}'),
    )

    response = await adapter._handle_pattern_run(req)

    assert response.status_code == 200
    assert captured["model"] == f"{HERMES_PATTERN_MODEL_PREFIX}summarize"
    assert captured["messages"] == [{"role": "user", "content": "hello"}]
    assert captured["hermes"]["context"] == "research"
    assert captured["hermes"]["variables"] == {"tone": "short"}


@pytest.mark.asyncio
async def test_ingest_endpoint_accepts_shared_text(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    req = Request(
        {
            "type": "http",
            "method": "POST",
            "headers": [(b"content-type", b"application/json")],
            "path": "/api/ingest",
        },
        receive=_json_receive(b'{"source_type":"text","source":"shared text","title":"Share"}'),
    )

    response = await adapter._handle_ingest(req)

    assert response.status_code == 201
    assert '"source_type":"text"' in response.body.decode().replace(" ", "")


def _json_receive(body: bytes):
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return receive
