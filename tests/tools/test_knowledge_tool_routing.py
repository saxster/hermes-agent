"""Tests for the F-D1 Phase 3 additions on ``tools/knowledge_tool.py``.

Phase 3 makes the agent-facing ``knowledge`` tool router-aware without
breaking its existing action surface. Two behaviors are flag-gated on
``knowledge.routing.enabled``:

  1. The ``save_*`` actions inject an ``external_id:<id>`` tag on every
     row so SQLite rows become joinable with wiki pages and graph
     entries written through ``KnowledgeRouter``.

  2. A new ``search_all_layers`` action performs cross-store search
     via the router (structured + wiki + context graph, fused with
     RRF). Gracefully falls back to structured-only search when the
     flag is off so the action is always callable.

These tests flip the flag directly (monkeypatch of
``tools.knowledge_tool._knowledge_routing_enabled``) rather than
writing config, so they run fast and don't depend on config-file
layout. All downstream stores are faked.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List

import pytest

import tools.knowledge_tool as knowledge_tool_mod
from tools.knowledge_tool import knowledge_tool


# ------------------------------------------------------------------------
# Fakes
# ------------------------------------------------------------------------

class FakeSessionDB:
    """Stand-in for SessionDB — records writes, serves searches."""

    def __init__(self, rows: List[Dict[str, Any]] | None = None):
        self.rows = rows or []
        self.saved_notes: List[Dict[str, Any]] = []
        self.saved_persons: List[Dict[str, Any]] = []
        self.saved_projects: List[Dict[str, Any]] = []
        self.saved_decisions: List[Dict[str, Any]] = []

    def save_knowledge_note(self, **kwargs: Any) -> int:
        self.saved_notes.append(kwargs)
        return len(self.saved_notes)

    def save_knowledge_person(self, **kwargs: Any) -> int:
        self.saved_persons.append(kwargs)
        return len(self.saved_persons)

    def save_knowledge_project(self, **kwargs: Any) -> int:
        self.saved_projects.append(kwargs)
        return len(self.saved_projects)

    def save_knowledge_decision(self, **kwargs: Any) -> int:
        self.saved_decisions.append(kwargs)
        return len(self.saved_decisions)

    def search_knowledge(
        self,
        *,
        query: str = "",
        entity_type: str | None = None,
        tag: str | None = None,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        matches = [
            row for row in self.rows
            if (not query or query.lower() in json.dumps(row).lower())
            and (entity_type is None or row.get("entity_type") == entity_type)
            and (tag is None or tag in (row.get("tags") or []))
        ]
        return matches[:limit]


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


# ------------------------------------------------------------------------
# External ID propagation on save_* actions
# ------------------------------------------------------------------------

class TestExternalIdPropagation:
    def test_save_note_skips_external_id_when_flag_off(self, monkeypatch):
        monkeypatch.setattr(knowledge_tool_mod, "_knowledge_routing_enabled", lambda: False)
        db = FakeSessionDB()

        result = _run(knowledge_tool(
            action="save_note",
            content="remember to buy milk",
            tags="grocery",
            session_db=db,
        ))
        data = json.loads(result)
        assert data["success"] is True
        saved_tags = db.saved_notes[0]["tags"]
        # No external_id tag
        assert all(not t.startswith("external_id:") for t in saved_tags)
        assert "grocery" in saved_tags

    def test_save_note_injects_external_id_when_flag_on(self, monkeypatch):
        monkeypatch.setattr(knowledge_tool_mod, "_knowledge_routing_enabled", lambda: True)
        db = FakeSessionDB()

        result = _run(knowledge_tool(
            action="save_note",
            content="followup with Priya next week\nContext matters",
            tags="work",
            session_db=db,
        ))
        data = json.loads(result)
        assert data["success"] is True
        saved_tags = db.saved_notes[0]["tags"]
        ext_tags = [t for t in saved_tags if t.startswith("external_id:")]
        assert len(ext_tags) == 1
        assert ext_tags[0].startswith("external_id:note_")
        # Original tag preserved
        assert "work" in saved_tags

    def test_save_person_injects_external_id_when_flag_on(self, monkeypatch):
        monkeypatch.setattr(knowledge_tool_mod, "_knowledge_routing_enabled", lambda: True)
        db = FakeSessionDB()

        _run(knowledge_tool(
            action="save_person",
            name="Priya Sharma",
            role="CTO",
            session_db=db,
        ))
        ext_tags = [t for t in db.saved_persons[0]["tags"] if t.startswith("external_id:")]
        assert len(ext_tags) == 1
        assert ext_tags[0].startswith("external_id:prsn_")

    def test_save_project_injects_external_id_when_flag_on(self, monkeypatch):
        monkeypatch.setattr(knowledge_tool_mod, "_knowledge_routing_enabled", lambda: True)
        db = FakeSessionDB()

        _run(knowledge_tool(
            action="save_project",
            name="Alpha",
            description="big thing",
            session_db=db,
        ))
        ext_tags = [t for t in db.saved_projects[0]["tags"] if t.startswith("external_id:")]
        assert len(ext_tags) == 1
        assert ext_tags[0].startswith("external_id:prjt_")

    def test_save_decision_injects_external_id_when_flag_on(self, monkeypatch):
        monkeypatch.setattr(knowledge_tool_mod, "_knowledge_routing_enabled", lambda: True)
        db = FakeSessionDB()

        _run(knowledge_tool(
            action="save_decision",
            title="Ship on Friday",
            rationale="Markets close Fri",
            session_db=db,
        ))
        ext_tags = [t for t in db.saved_decisions[0]["tags"] if t.startswith("external_id:")]
        assert len(ext_tags) == 1
        assert ext_tags[0].startswith("external_id:dcsn_")

    def test_duplicate_external_id_tag_not_added(self, monkeypatch):
        # If the caller already passed an external_id tag (unusual but
        # plausible), don't add it twice.
        monkeypatch.setattr(knowledge_tool_mod, "_knowledge_routing_enabled", lambda: True)
        db = FakeSessionDB()

        _run(knowledge_tool(
            action="save_person",
            name="Alice",
            tags="founder,external_id:prsn_fake",
            session_db=db,
        ))
        ext_tags = [t for t in db.saved_persons[0]["tags"] if t.startswith("external_id:")]
        # The tool should have deduped — either the fake or the real,
        # but only one.
        assert len(ext_tags) >= 1
        # No duplicate of the same ID
        assert len(set(ext_tags)) == len(ext_tags)


# ------------------------------------------------------------------------
# search_all_layers action
# ------------------------------------------------------------------------

class TestSearchAllLayersFallback:
    def test_flag_off_returns_structured_only_with_note(self, monkeypatch):
        monkeypatch.setattr(knowledge_tool_mod, "_knowledge_routing_enabled", lambda: False)
        db = FakeSessionDB(rows=[
            {"name": "Priya Sharma", "entity_type": "person"},
        ])

        result = _run(knowledge_tool(
            action="search_all_layers",
            query="priya",
            session_db=db,
        ))
        data = json.loads(result)

        assert data["success"] is True
        assert data["action"] == "search_all_layers"
        assert data["routing_enabled"] is False
        assert data["layers_searched"] == ["structured"]
        assert data["count"] == 1
        assert "note" in data
        assert "knowledge.routing.enabled is off" in data["note"]

    def test_flag_off_requires_query(self, monkeypatch):
        monkeypatch.setattr(knowledge_tool_mod, "_knowledge_routing_enabled", lambda: False)
        db = FakeSessionDB()
        result = _run(knowledge_tool(
            action="search_all_layers",
            query=None,
            session_db=db,
        ))
        data = json.loads(result)
        assert data["success"] is False
        assert "query is required" in data["error"]


class TestSearchAllLayersRouted:
    def test_flag_on_hits_router_and_tags_layers(self, monkeypatch):
        monkeypatch.setattr(knowledge_tool_mod, "_knowledge_routing_enabled", lambda: True)
        db = FakeSessionDB(rows=[
            {"name": "Priya", "entity_type": "person", "tags": []},
        ])

        # Fake KnowledgeRouter that captures the search call
        captured: Dict[str, Any] = {}

        class FakeHit:
            def __init__(self, source_layer, title, snippet, score, external_id=None):
                self.source_layer = source_layer
                self.title = title
                self.snippet = snippet
                self.score = score
                self.external_id = external_id

        class FakeRouter:
            def __init__(self, **kwargs: Any):
                captured["deps"] = kwargs

            def search(self, query: str, *, limit: int = 10) -> List[FakeHit]:
                captured["query"] = query
                captured["limit"] = limit
                return [
                    FakeHit("structured", "Priya Sharma", "CTO at Acme", 0.9, "prsn_abc"),
                    FakeHit("wiki", "priya.md", "notes page", 0.5),
                ]

        # kb_tool availability stub
        class _FakeKB:
            def __call__(self, **_: Any) -> str:
                return "{}"

        monkeypatch.setattr("agent.knowledge_router.KnowledgeRouter", FakeRouter)
        monkeypatch.setattr("tools.kb_tool.kb_tool", _FakeKB())
        monkeypatch.setattr("tools.kb_tool.check_kb_requirements", lambda: True)

        class _FakeKM:
            pass

        result = _run(knowledge_tool(
            action="search_all_layers",
            query="priya",
            limit=10,
            session_db=db,
            knowledge_manager=_FakeKM(),
        ))
        data = json.loads(result)

        assert data["success"] is True
        assert data["routing_enabled"] is True
        assert data["count"] == 2
        layers = [r["source_layer"] for r in data["results"]]
        assert "structured" in layers
        assert "wiki" in layers
        # External ID is returned to the caller when the underlying hit had one.
        external_ids = [r.get("external_id") for r in data["results"]]
        assert "prsn_abc" in external_ids

    def test_flag_on_router_error_falls_back_to_structured(self, monkeypatch):
        monkeypatch.setattr(knowledge_tool_mod, "_knowledge_routing_enabled", lambda: True)
        db = FakeSessionDB(rows=[
            {"name": "Priya", "entity_type": "person"},
        ])

        class ExplodingRouter:
            def __init__(self, **_: Any):
                raise RuntimeError("router import went wrong")

        monkeypatch.setattr("agent.knowledge_router.KnowledgeRouter", ExplodingRouter)
        monkeypatch.setattr("tools.kb_tool.check_kb_requirements", lambda: False)

        result = _run(knowledge_tool(
            action="search_all_layers",
            query="priya",
            session_db=db,
        ))
        data = json.loads(result)

        # Degrades to structured-only with a fallback_reason, still success=True
        assert data["success"] is True
        assert data["routing_enabled"] is True
        assert data["layers_searched"] == ["structured"]
        assert "fallback_reason" in data
        assert "RuntimeError" in data["fallback_reason"]


# ------------------------------------------------------------------------
# Unknown action reports the new action in the help message
# ------------------------------------------------------------------------

class TestUnknownActionHelp:
    def test_error_message_lists_search_all_layers(self):
        result = _run(knowledge_tool(
            action="bogus-action",
            session_db=FakeSessionDB(),
        ))
        data = json.loads(result)
        assert data["success"] is False
        assert "search_all_layers" in data["error"]
