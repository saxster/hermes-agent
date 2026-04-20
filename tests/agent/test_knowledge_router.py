"""Tests for ``agent/knowledge_router.py`` (F-D1 Phase 2, Proposal A).

The router is a facade with no persistence of its own — its contract
is "route the right kind to the right store, with a shared external
ID, degrading gracefully when a store is unavailable." These tests
use fakes for the three collaborators so we can assert on the exact
call shapes without spinning up SQLite, Graphiti, or the wiki.

What each test pins down:

  * Routing table: each kind lands in its documented primary store and
    optional graph projection.
  * External ID: every write carries one, always the same shape, and
    propagates into the store's tags/metadata.
  * Primary-write failure: short-circuits, returns an error result,
    does *not* project secondary layers.
  * Secondary-write failure: captured in ``errors`` but does not mask
    the primary success.
  * Disabled layer: raises a clear RuntimeError for writes; silently
    skipped for searches.
  * Search: scatter-gathers across enabled layers, fuses via RRF, and
    returns a single ranked list with source-layer tags.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest

from agent.knowledge_router import (
    ALL_LAYERS,
    KnowledgeRouter,
    KnowledgeSearchHit,
    KnowledgeWriteResult,
    LAYER_GRAPH,
    LAYER_STRUCTURED,
    LAYER_WIKI,
)
from agent.knowledge_external_id import knowledge_external_id


# ------------------------------------------------------------------------
# Fakes
# ------------------------------------------------------------------------

class FakeKnowledgeManager:
    """Records every save_* call for assertions."""

    def __init__(self, *, db: Any = None, fail_on: str | None = None):
        self.db = db
        self.persons: List[Dict[str, Any]] = []
        self.projects: List[Dict[str, Any]] = []
        self.decisions: List[Dict[str, Any]] = []
        self.notes: List[Dict[str, Any]] = []
        self.fail_on = fail_on

    def _maybe_fail(self, method: str) -> None:
        if self.fail_on == method:
            raise RuntimeError(f"simulated failure in {method}")

    def save_person(self, **kwargs: Any) -> int:
        self._maybe_fail("save_person")
        self.persons.append(kwargs)
        return len(self.persons)

    def save_project(self, **kwargs: Any) -> int:
        self._maybe_fail("save_project")
        self.projects.append(kwargs)
        return len(self.projects)

    def save_decision(self, **kwargs: Any) -> int:
        self._maybe_fail("save_decision")
        self.decisions.append(kwargs)
        return len(self.decisions)

    def save_note(self, **kwargs: Any) -> int:
        self._maybe_fail("save_note")
        self.notes.append(kwargs)
        return len(self.notes)


class FakeGraphManager:
    """Records every add_episode / search call."""

    def __init__(self, *, fail_episodes: bool = False):
        self.episodes: List[Dict[str, Any]] = []
        self.search_queries: List[str] = []
        self.search_result: Dict[str, Any] = {"edges": []}
        self.fail_episodes = fail_episodes

    async def add_episode(
        self,
        *,
        content: str,
        source_type: str = "text",
        name: str = "",
        metadata: Dict[str, Any] | None = None,
        group_id: str = "personal",
    ) -> Dict[str, Any]:
        if self.fail_episodes:
            raise RuntimeError("graph ingestion failed")
        episode = {
            "content": content,
            "source_type": source_type,
            "name": name,
            "metadata": metadata or {},
            "group_id": group_id,
        }
        self.episodes.append(episode)
        return {"episode_id": f"ep-{len(self.episodes)}"}

    async def search(self, *, query: str, limit: int = 10) -> Dict[str, Any]:
        self.search_queries.append(query)
        return self.search_result


class FakeDB:
    """Stand-in for SessionDB's search_knowledge."""

    def __init__(self, rows: List[Dict[str, Any]] | None = None):
        self.rows = rows or []

    def search_knowledge(self, query: str, limit: int = 10, **_: Any) -> List[Dict[str, Any]]:
        return [r for r in self.rows if query.lower() in str(r).lower()][:limit]


def make_fake_kb(pages: List[Dict[str, Any]] | None = None):
    """Return a kb_tool-compatible function that records writes + serves searches."""
    filed: List[Dict[str, Any]] = []
    pages = pages or []

    def _kb_tool(action: str, **kwargs: Any) -> str:
        if action == "file":
            filed.append(kwargs)
            return json.dumps({"success": True, "title": kwargs.get("title")})
        if action == "search":
            query = kwargs.get("query", "").lower()
            matches = [p for p in pages if query in json.dumps(p).lower()]
            return json.dumps({"matches": matches})
        return json.dumps({"error": f"unexpected action {action}"})

    _kb_tool.filed = filed  # type: ignore[attr-defined]
    return _kb_tool


# ------------------------------------------------------------------------
# Routing policy
# ------------------------------------------------------------------------

class TestRoutingPolicy:
    def _make_router(self, **overrides):
        return KnowledgeRouter(
            knowledge_manager=overrides.get("km", FakeKnowledgeManager()),
            graph_manager=overrides.get("gm", FakeGraphManager()),
            kb_tool_fn=overrides.get("kb", make_fake_kb()),
        )

    def test_person_writes_structured_primary_and_graph_secondary(self):
        km = FakeKnowledgeManager()
        gm = FakeGraphManager()
        router = self._make_router(km=km, gm=gm)

        result = router.add("person", {"name": "Priya Sharma", "role": "CTO"})

        assert isinstance(result, KnowledgeWriteResult)
        assert result.primary_layer == LAYER_STRUCTURED
        assert LAYER_GRAPH in result.secondary_layers
        assert result.errors == {}
        assert result.external_id == knowledge_external_id("person", "Priya Sharma")
        assert len(km.persons) == 1
        assert km.persons[0]["name"] == "Priya Sharma"
        assert any("external_id:" in t for t in km.persons[0]["tags"])
        assert len(gm.episodes) == 1
        assert gm.episodes[0]["metadata"]["external_id"] == result.external_id

    def test_project_writes_structured_primary_and_graph_secondary(self):
        km = FakeKnowledgeManager()
        gm = FakeGraphManager()
        router = self._make_router(km=km, gm=gm)

        result = router.add("project", {"name": "Alpha", "description": "big thing"})
        assert result.primary_layer == LAYER_STRUCTURED
        assert LAYER_GRAPH in result.secondary_layers
        assert len(km.projects) == 1
        assert len(gm.episodes) == 1

    def test_decision_writes_structured_primary_and_graph_secondary(self):
        km = FakeKnowledgeManager()
        gm = FakeGraphManager()
        router = self._make_router(km=km, gm=gm)

        router.add("decision", {"title": "Ship on Friday", "rationale": "Markets close Fri"})
        assert len(km.decisions) == 1
        assert len(gm.episodes) == 1

    def test_note_writes_structured_only(self):
        km = FakeKnowledgeManager()
        gm = FakeGraphManager()
        router = self._make_router(km=km, gm=gm)

        result = router.add("note", {"content": "Look into Y later"})
        assert result.primary_layer == LAYER_STRUCTURED
        assert result.secondary_layers == []
        assert len(km.notes) == 1
        assert gm.episodes == []

    def test_concept_writes_wiki_primary_and_graph_secondary(self):
        gm = FakeGraphManager()
        kb = make_fake_kb()
        router = self._make_router(gm=gm, kb=kb)

        result = router.add(
            "concept",
            {"title": "Reciprocal Rank Fusion", "content": "RRF is..."},
        )
        assert result.primary_layer == LAYER_WIKI
        assert LAYER_GRAPH in result.secondary_layers
        assert len(kb.filed) == 1  # type: ignore[attr-defined]
        assert kb.filed[0]["title"] == "Reciprocal Rank Fusion"  # type: ignore[attr-defined]
        assert len(gm.episodes) == 1

    def test_episode_writes_graph_only(self):
        km = FakeKnowledgeManager()
        gm = FakeGraphManager()
        router = self._make_router(km=km, gm=gm)

        result = router.add("episode", {"name": "turn-42", "content": "agent ran X tool"})
        assert result.primary_layer == LAYER_GRAPH
        assert result.secondary_layers == []
        assert km.persons == [] and km.notes == []
        assert len(gm.episodes) == 1

    def test_unknown_kind_raises(self):
        router = self._make_router()
        with pytest.raises(ValueError, match="Unknown knowledge kind"):
            router.add("not-a-kind", {"name": "foo"})

    def test_missing_identifying_field_raises(self):
        router = self._make_router()
        # person requires 'name'
        with pytest.raises(ValueError, match="missing the identifying field"):
            router.add("person", {"role": "CTO"})


# ------------------------------------------------------------------------
# Failure modes
# ------------------------------------------------------------------------

class TestFailureSemantics:
    def test_primary_failure_short_circuits(self):
        km = FakeKnowledgeManager(fail_on="save_person")
        gm = FakeGraphManager()
        router = KnowledgeRouter(
            knowledge_manager=km,
            graph_manager=gm,
            kb_tool_fn=make_fake_kb(),
        )

        result = router.add("person", {"name": "Priya"})
        assert result.primary_layer == LAYER_STRUCTURED
        assert LAYER_STRUCTURED in result.errors
        assert result.secondary_layers == []
        assert gm.episodes == []  # no secondary projection when primary fails

    def test_secondary_failure_captured_but_primary_succeeds(self):
        km = FakeKnowledgeManager()
        gm = FakeGraphManager(fail_episodes=True)
        router = KnowledgeRouter(
            knowledge_manager=km,
            graph_manager=gm,
            kb_tool_fn=make_fake_kb(),
        )

        result = router.add("person", {"name": "Priya"})
        assert len(km.persons) == 1  # primary did succeed
        assert LAYER_GRAPH in result.errors
        # LAYER_GRAPH must NOT appear in secondary_layers when it errored
        assert LAYER_GRAPH not in result.secondary_layers

    def test_disabled_structured_layer_raises_on_structured_write(self):
        router = KnowledgeRouter(
            knowledge_manager=None,
            graph_manager=FakeGraphManager(),
            kb_tool_fn=make_fake_kb(),
        )
        result = router.add("person", {"name": "Priya"})
        assert LAYER_STRUCTURED in result.errors
        assert "not available" in result.errors[LAYER_STRUCTURED]

    def test_disabled_wiki_layer_raises_on_wiki_write(self):
        router = KnowledgeRouter(
            knowledge_manager=FakeKnowledgeManager(),
            graph_manager=FakeGraphManager(),
            kb_tool_fn=None,
        )
        result = router.add("concept", {"title": "X", "content": "..."})
        assert LAYER_WIKI in result.errors

    def test_disabled_graph_layer_silent_secondary(self):
        km = FakeKnowledgeManager()
        router = KnowledgeRouter(
            knowledge_manager=km,
            graph_manager=None,
            kb_tool_fn=make_fake_kb(),
        )
        result = router.add("person", {"name": "Priya"})
        # Primary succeeded; secondary errored loud (we didn't quietly swallow).
        assert len(km.persons) == 1
        assert LAYER_GRAPH in result.errors
        assert "not available" in result.errors[LAYER_GRAPH]


# ------------------------------------------------------------------------
# Search
# ------------------------------------------------------------------------

class TestSearch:
    def test_empty_query_returns_empty(self):
        router = KnowledgeRouter(
            knowledge_manager=FakeKnowledgeManager(db=FakeDB([{"name": "x"}])),
            graph_manager=FakeGraphManager(),
            kb_tool_fn=make_fake_kb(),
        )
        assert router.search("") == []
        assert router.search("   ") == []

    def test_fuses_results_from_all_layers(self):
        # Each layer surfaces a different hit for "alpha"; RRF should merge them.
        km = FakeKnowledgeManager(db=FakeDB([
            {"name": "Alpha project", "description": "from SQLite"},
        ]))
        gm = FakeGraphManager()
        gm.search_result = {"edges": [{"name": "alpha-edge", "fact": "from graph"}]}
        kb = make_fake_kb(pages=[{"title": "Alpha primer", "snippet": "from wiki"}])

        router = KnowledgeRouter(
            knowledge_manager=km,
            graph_manager=gm,
            kb_tool_fn=kb,
        )

        hits = router.search("alpha")
        layers_seen = {h.source_layer for h in hits}
        assert layers_seen == set(ALL_LAYERS)
        # all scores > 0, sorted descending
        scores = [h.score for h in hits]
        assert all(s > 0 for s in scores)
        assert scores == sorted(scores, reverse=True)

    def test_scoped_to_subset_of_layers(self):
        km = FakeKnowledgeManager(db=FakeDB([{"name": "Alpha project"}]))
        gm = FakeGraphManager()
        gm.search_result = {"edges": [{"name": "alpha-edge"}]}
        kb = make_fake_kb(pages=[{"title": "Alpha primer"}])

        router = KnowledgeRouter(
            knowledge_manager=km, graph_manager=gm, kb_tool_fn=kb,
        )

        hits = router.search("alpha", layers=(LAYER_WIKI,))
        assert all(h.source_layer == LAYER_WIKI for h in hits)
        assert gm.search_queries == []  # graph not queried

    def test_disabled_layer_silently_skipped_in_search(self):
        # graph_manager=None — should not raise, just drop that layer.
        km = FakeKnowledgeManager(db=FakeDB([{"name": "Alpha"}]))
        router = KnowledgeRouter(
            knowledge_manager=km, graph_manager=None, kb_tool_fn=make_fake_kb(),
        )

        hits = router.search("alpha")
        assert all(h.source_layer != LAYER_GRAPH for h in hits)

    def test_search_hit_carries_source_layer_tag(self):
        km = FakeKnowledgeManager(db=FakeDB([{"name": "Alpha project", "description": "x"}]))
        router = KnowledgeRouter(
            knowledge_manager=km, graph_manager=None, kb_tool_fn=None,
        )
        hits = router.search("alpha")
        assert hits
        assert hits[0].source_layer == LAYER_STRUCTURED
        assert isinstance(hits[0], KnowledgeSearchHit)


# ------------------------------------------------------------------------
# External-ID propagation
# ------------------------------------------------------------------------

class TestExternalIdPropagation:
    def test_structured_write_tags_carry_external_id(self):
        km = FakeKnowledgeManager()
        router = KnowledgeRouter(
            knowledge_manager=km, graph_manager=None, kb_tool_fn=None,
        )
        router.add("person", {"name": "Alice", "tags": ["founder"]})
        tags = km.persons[0]["tags"]
        assert "founder" in tags
        assert any(t.startswith("external_id:prsn_") for t in tags)

    def test_graph_episode_metadata_carries_external_id_and_kind(self):
        km = FakeKnowledgeManager()
        gm = FakeGraphManager()
        router = KnowledgeRouter(
            knowledge_manager=km, graph_manager=gm, kb_tool_fn=None,
        )
        router.add("person", {"name": "Alice"})
        assert len(gm.episodes) == 1
        md = gm.episodes[0]["metadata"]
        assert md["external_id"].startswith("prsn_")
        assert md["kind"] == "person"

    def test_wiki_write_tags_carry_external_id(self):
        kb = make_fake_kb()
        router = KnowledgeRouter(
            knowledge_manager=None, graph_manager=None, kb_tool_fn=kb,
        )
        router.add("concept", {"title": "RRF", "content": "..."})
        assert kb.filed  # type: ignore[attr-defined]
        tags = kb.filed[0]["tags"]  # type: ignore[attr-defined]
        assert "external_id:cncp_" in tags
