"""Tests for ``agent/knowledge_promotion.py`` (F-D1 Phase 4).

The promotion cron is a thin orchestrator over ``memory_curator``
plus two stubs that will activate in Phase 4b / 4c. The tests pin:

  * The orchestrator never raises — every exception is captured into
    ``errors`` so a nightly cron can degrade gracefully.
  * Missing dependencies (memory_store, auxiliary_client) skip Step 1
    with a clear reason, not a crash.
  * Step 1 delegates to ``curate_memory`` with the expected kwargs
    (memory_store, auxiliary_client, auxiliary_model, session_db,
    session_id=None, dry_run).
  * Stub Steps 2 and 3 return ``skipped=True`` with a reason string
    pointing at their activation commit.
  * ``PromotionResult.brief()`` produces a deterministic one-line
    summary suitable for ``[SILENT]`` cron output.
  * ``dry_run`` is threaded into Step 1.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from agent.knowledge_promotion import (
    PromotionResult,
    run_knowledge_promotion,
)


class FakeMemoryStore:
    """Minimal memory_store stand-in; never actually called by the orchestrator."""


class FakeAuxClient:
    """Stand-in for an OpenAI-compatible client; never called."""


# ------------------------------------------------------------------------
# Dependency handling
# ------------------------------------------------------------------------

class TestDependencyHandling:
    def test_missing_memory_store_skips_step1(self):
        result = run_knowledge_promotion()
        assert result.step1_memory_to_wiki["skipped"] is True
        assert "memory_store" in result.step1_memory_to_wiki["reason"]

    def test_missing_aux_client_skips_step1(self):
        result = run_knowledge_promotion(memory_store=FakeMemoryStore())
        assert result.step1_memory_to_wiki["skipped"] is True
        assert "auxiliary" in result.step1_memory_to_wiki["reason"].lower()

    def test_missing_aux_model_skips_step1(self):
        result = run_knowledge_promotion(
            memory_store=FakeMemoryStore(),
            auxiliary_client=FakeAuxClient(),
        )
        assert result.step1_memory_to_wiki["skipped"] is True

    def test_no_deps_still_returns_structured_result(self):
        # The whole point of returning PromotionResult: a no-op run
        # still produces a stable shape for the cron caller to inspect.
        result = run_knowledge_promotion()
        assert isinstance(result, PromotionResult)
        d = result.to_dict()
        for key in (
            "step1_memory_to_wiki",
            "step2_wiki_to_graph",
            "step3_graph_to_structured",
            "errors",
        ):
            assert key in d


# ------------------------------------------------------------------------
# Step 1 delegation
# ------------------------------------------------------------------------

class TestStep1Delegation:
    def test_step1_delegates_to_curate_memory(self, monkeypatch):
        call_log: Dict[str, Any] = {}

        def fake_curate_memory(**kwargs: Any) -> Dict[str, Any]:
            call_log.update(kwargs)
            return {
                "total_promoted": 3,
                "total_removed": 5,
                "targets_curated": [{"target": "memory", "entries_before": 10}],
            }

        monkeypatch.setattr("agent.memory_curator.curate_memory", fake_curate_memory)

        memory = FakeMemoryStore()
        client = FakeAuxClient()
        result = run_knowledge_promotion(
            memory_store=memory,
            auxiliary_client=client,
            auxiliary_model="gpt-4o-mini",
            session_db="fake-session-db",
        )

        # Verify the kwargs curate_memory was called with
        assert call_log["memory_store"] is memory
        assert call_log["auxiliary_client"] is client
        assert call_log["auxiliary_model"] == "gpt-4o-mini"
        assert call_log["session_db"] == "fake-session-db"
        assert call_log["session_id"] is None
        assert call_log["dry_run"] is False

        # Verify result translation
        step1 = result.step1_memory_to_wiki
        assert step1["skipped"] is False
        assert step1["dry_run"] is False
        assert step1["promoted"] == 3
        assert step1["removed"] == 5
        assert len(step1["targets_curated"]) == 1

    def test_step1_threads_dry_run(self, monkeypatch):
        call_log: Dict[str, Any] = {}

        def fake_curate_memory(**kwargs: Any) -> Dict[str, Any]:
            call_log.update(kwargs)
            return {"total_promoted": 0, "total_removed": 0, "targets_curated": []}

        monkeypatch.setattr("agent.memory_curator.curate_memory", fake_curate_memory)

        result = run_knowledge_promotion(
            memory_store=FakeMemoryStore(),
            auxiliary_client=FakeAuxClient(),
            auxiliary_model="m",
            dry_run=True,
        )
        assert call_log["dry_run"] is True
        assert result.step1_memory_to_wiki["dry_run"] is True

    def test_step1_curator_crash_captured_in_errors(self, monkeypatch):
        def fake_curate_memory(**_: Any) -> Dict[str, Any]:
            raise RuntimeError("LLM unavailable")

        monkeypatch.setattr("agent.memory_curator.curate_memory", fake_curate_memory)

        result = run_knowledge_promotion(
            memory_store=FakeMemoryStore(),
            auxiliary_client=FakeAuxClient(),
            auxiliary_model="m",
        )
        # Error captured, other steps still run
        assert any("step1_memory_to_wiki" in e for e in result.errors)
        assert any("RuntimeError" in e for e in result.errors)
        # step1 marked skipped with reason
        assert result.step1_memory_to_wiki["skipped"] is True
        # Other steps still produced output
        assert "skipped" in result.step2_wiki_to_graph
        assert "skipped" in result.step3_graph_to_structured


# ------------------------------------------------------------------------
# Stubbed steps
# ------------------------------------------------------------------------

class TestStubbedSteps:
    def test_step2_is_skipped_with_phase_4b_reason(self):
        result = run_knowledge_promotion()
        step2 = result.step2_wiki_to_graph
        assert step2["skipped"] is True
        assert step2["promoted"] == 0
        assert "Phase-4b" in step2["reason"]

    def test_step3_is_skipped_with_phase_4c_reason(self):
        result = run_knowledge_promotion()
        step3 = result.step3_graph_to_structured
        assert step3["skipped"] is True
        assert step3["promoted"] == 0
        assert "Phase-4c" in step3["reason"]


# ------------------------------------------------------------------------
# Result shape + brief()
# ------------------------------------------------------------------------

class TestResultShape:
    def test_brief_when_nothing_promoted(self):
        result = run_knowledge_promotion()
        assert result.brief() == "knowledge promotion: no items promoted; no errors"

    def test_brief_counts_across_steps(self, monkeypatch):
        def fake_curate_memory(**_: Any) -> Dict[str, Any]:
            return {"total_promoted": 4, "total_removed": 0, "targets_curated": []}

        monkeypatch.setattr("agent.memory_curator.curate_memory", fake_curate_memory)

        result = run_knowledge_promotion(
            memory_store=FakeMemoryStore(),
            auxiliary_client=FakeAuxClient(),
            auxiliary_model="m",
        )
        brief = result.brief()
        assert "4 promoted" in brief
        assert "memory→wiki=4" in brief
        assert "wiki→graph=0" in brief
        assert "graph→structured=0" in brief
        assert "0 error" in brief

    def test_to_dict_shape(self):
        result = run_knowledge_promotion()
        d = result.to_dict()
        assert set(d.keys()) == {
            "step1_memory_to_wiki",
            "step2_wiki_to_graph",
            "step3_graph_to_structured",
            "errors",
        }
        assert isinstance(d["errors"], list)
