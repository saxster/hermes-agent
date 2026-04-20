"""Shared external-ID namespace for knowledge stores (F-D1 Phase 1).

Before Hermes had a knowledge router, each of the three stores
(``tools/kb_tool.py`` markdown wiki, ``agent/graph_manager.py`` Graphiti
+ Kuzu context graph, ``agent/knowledge_manager.py`` Obsidian + SQLite
structured records) generated its own identifiers: the wiki used slug
paths, the graph used UUIDs, SQLite used integer primary keys. The
same fact could land in two or three stores under three different
IDs with no cheap way to join across them.

This module introduces a stable *external ID* every store can carry
as an append-only extra attribute. The ID is a deterministic hash of
the normalized (kind, name) pair, so any caller can compute it from
user input without first having to write to any store:

    >>> knowledge_external_id("person", "Priya Sharma")
    'prsn_4b39e5e2a1b7'
    >>> knowledge_external_id("person", "  priya   sharma  ")
    'prsn_4b39e5e2a1b7'   # whitespace-normalized, case-folded

The 4-character kind prefix (``prsn``, ``prjt``, ``dcsn``, ``cncp``,
``rfrn``, ``epsd``, ``note``) keeps IDs readable at a glance when they
appear in logs, wiki frontmatter, or graph node attributes. The 12-hex
suffix is the first 12 chars of ``sha256(normalized)`` — billions of
distinct names before collision risk matters.

This is Phase 1 of F-D1 (the knowledge-routing design doc at
``docs/plans/2026-04-20-knowledge-routing-design.md``). Phase 2 adds
the ``KnowledgeRouter`` on top; Phase 3 integrates an agent-facing
``knowledge`` tool; Phase 4 adds a promotion cron. This helper stays
usable even if Phases 2–4 are never shipped — writing the ID into each
store's schema now makes any future join cheap, without requiring a
migration.
"""

from __future__ import annotations

import hashlib
import re
from typing import Final

__all__ = [
    "KNOWLEDGE_KINDS",
    "knowledge_external_id",
    "normalize_knowledge_name",
]


# Four-character prefixes make the kind visible in logs and in each
# store's schema without requiring a separate column. Picked for rough
# pronounceability: prsn-4b39.., prjt-a71e.., etc.
_KIND_PREFIXES: Final[dict[str, str]] = {
    "person": "prsn",
    "project": "prjt",
    "decision": "dcsn",
    "concept": "cncp",
    "reference": "rfrn",
    "episode": "epsd",
    "note": "note",
}

# The ordered list of kinds the router understands. Callers can check
# membership before dispatching into ``knowledge_external_id`` so
# unknown kinds fail fast instead of silently producing an ID.
KNOWLEDGE_KINDS: Final[tuple[str, ...]] = tuple(_KIND_PREFIXES.keys())


_WHITESPACE_RUN = re.compile(r"\s+")


def normalize_knowledge_name(name: str) -> str:
    """Normalize an entity name for hashing.

    Rules:
      - lowercase (fold case across scripts Python supports it for)
      - collapse internal whitespace runs to single spaces
      - strip leading/trailing whitespace
      - leave other punctuation intact (so "Dr. Foo" and "Dr Foo"
        deliberately hash differently)

    Kept deliberately minimal — aggressive normalization (stripping
    titles, accent folding, etc.) would make collisions more likely
    without clearly making the right identity calls in mixed scripts.
    """
    if not isinstance(name, str):
        raise TypeError(f"normalize_knowledge_name expects str, got {type(name).__name__}")
    folded = name.casefold()
    collapsed = _WHITESPACE_RUN.sub(" ", folded)
    return collapsed.strip()


def knowledge_external_id(kind: str, name: str) -> str:
    """Compute a stable cross-store ID for a knowledge entity.

    Deterministic: same (kind, name) pair always produces the same ID
    regardless of which store is being written to or when. Callers must
    pass a ``kind`` from :data:`KNOWLEDGE_KINDS`; unknown kinds raise
    ``ValueError`` so typos don't silently produce non-joinable IDs.

    The output is URL-, filename-, and slug-safe — wiki frontmatter,
    graph node attributes, and SQLite columns can all carry it without
    escaping.
    """
    if kind not in _KIND_PREFIXES:
        raise ValueError(
            f"Unknown knowledge kind {kind!r}. "
            f"Valid kinds: {', '.join(sorted(_KIND_PREFIXES))}"
        )

    normalized = normalize_knowledge_name(name)
    if not normalized:
        raise ValueError("knowledge_external_id requires a non-empty name")

    digest = hashlib.sha256(
        f"{kind}:{normalized}".encode("utf-8")
    ).hexdigest()[:12]
    return f"{_KIND_PREFIXES[kind]}_{digest}"
