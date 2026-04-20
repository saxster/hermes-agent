"""Tests for ``agent/knowledge_external_id.py`` (F-D1 Phase 1).

The external-ID helper is the foundation of the Phase-2 router and of
any future cross-store join. These tests lock in the invariants the
rest of the pipeline depends on:

  * determinism: same (kind, name) → same ID across calls and processes
  * normalization: whitespace + case are collapsed before hashing so
    slightly-different user inputs collide into one ID
  * kind enforcement: unknown kinds raise rather than producing a
    non-joinable ID
  * slug safety: IDs are URL/filename-safe so every store can embed
    them without escaping
"""

from __future__ import annotations

import re

import pytest

from agent.knowledge_external_id import (
    KNOWLEDGE_KINDS,
    knowledge_external_id,
    normalize_knowledge_name,
)


class TestNormalization:
    def test_strips_and_casefolds(self):
        assert normalize_knowledge_name("  Priya Sharma  ") == "priya sharma"

    def test_collapses_internal_whitespace(self):
        assert normalize_knowledge_name("priya    sharma") == "priya sharma"
        assert normalize_knowledge_name("priya\tsharma") == "priya sharma"
        assert normalize_knowledge_name("priya\n\nsharma") == "priya sharma"

    def test_leaves_punctuation_intact(self):
        # Deliberately: "Dr. Foo" and "Dr Foo" should hash differently.
        assert normalize_knowledge_name("Dr. Foo") == "dr. foo"
        assert normalize_knowledge_name("Dr Foo") == "dr foo"
        assert normalize_knowledge_name("Dr. Foo") != normalize_knowledge_name("Dr Foo")

    def test_rejects_non_str(self):
        with pytest.raises(TypeError):
            normalize_knowledge_name(123)  # type: ignore[arg-type]


class TestExternalId:
    def test_deterministic(self):
        a = knowledge_external_id("person", "Priya Sharma")
        b = knowledge_external_id("person", "Priya Sharma")
        assert a == b

    def test_whitespace_and_case_collide(self):
        canonical = knowledge_external_id("person", "Priya Sharma")
        for variant in (
            "priya sharma",
            "PRIYA SHARMA",
            "  Priya   Sharma  ",
            "priya\tsharma",
        ):
            assert knowledge_external_id("person", variant) == canonical, variant

    def test_kind_prefix_visible(self):
        assert knowledge_external_id("person", "alice").startswith("prsn_")
        assert knowledge_external_id("project", "alpha").startswith("prjt_")
        assert knowledge_external_id("decision", "ship it").startswith("dcsn_")
        assert knowledge_external_id("concept", "calendrical").startswith("cncp_")
        assert knowledge_external_id("reference", "rfc 9110").startswith("rfrn_")
        assert knowledge_external_id("episode", "turn 42").startswith("epsd_")
        assert knowledge_external_id("note", "followup").startswith("note_")

    def test_different_kinds_produce_different_ids(self):
        person_id = knowledge_external_id("person", "Alpha")
        project_id = knowledge_external_id("project", "Alpha")
        assert person_id != project_id

    def test_slug_safe(self):
        # Must be URL/filename-safe: [a-z0-9_]+ only.
        pattern = re.compile(r"^[a-z]+_[a-f0-9]+$")
        for kind in KNOWLEDGE_KINDS:
            value = knowledge_external_id(kind, "Test Entity")
            assert pattern.match(value), f"non-slug-safe: {value}"

    def test_rejects_unknown_kind(self):
        with pytest.raises(ValueError, match="Unknown knowledge kind"):
            knowledge_external_id("not-a-kind", "whatever")

    def test_rejects_empty_name(self):
        with pytest.raises(ValueError, match="non-empty name"):
            knowledge_external_id("person", "")
        with pytest.raises(ValueError, match="non-empty name"):
            knowledge_external_id("person", "   ")

    def test_all_declared_kinds_are_usable(self):
        # KNOWLEDGE_KINDS should match what knowledge_external_id actually accepts.
        for kind in KNOWLEDGE_KINDS:
            assert isinstance(knowledge_external_id(kind, "x"), str)
