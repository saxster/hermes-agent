"""Knowledge router (F-D1 Phase 2, Proposal A).

Single write + query surface over the three existing knowledge stores:

  * ``tools/kb_tool.py``       — markdown wiki (human-readable prose)
  * ``agent/graph_manager.py`` — Graphiti + Kuzu context graph (relations)
  * ``agent/knowledge_manager.py`` — Obsidian + SQLite (typed records)

The router does not replace any store. Each store keeps its native API
intact for callers that know they want a specific layer. What the
router adds is:

  1. A single ``add(kind, payload, metadata)`` entry point that picks
     the right write target (and, for some kinds, a secondary projection
     into the graph). This removes the "which tool do I call?" question
     from every agent turn.
  2. A single ``search(query, layers)`` entry point that scatter-gathers
     across all three stores with reciprocal-rank fusion, returning a
     single ranked list with source-layer tags.
  3. A shared external-ID namespace (:mod:`agent.knowledge_external_id`)
     that every write carries, so cross-store joins become cheap without
     a migration.

Phase 2 scaffolding only. The router is off by default
(``knowledge.routing.enabled: false`` in ``DEFAULT_CONFIG``). No
agent-facing tool invokes it yet — that integration lands in Phase 3
once the in-flight F-L1 refactor on ``agent/core.py`` stabilizes.
Phase 4 will add the promotion cron on top.

Design notes from ``docs/plans/2026-04-20-knowledge-routing-design.md``:

  * Proposal A ("router facade, stores unchanged") was picked because
    it is minimally invasive and allows us to migrate later to
    Proposal B ("KnowledgeManager canonical") *from* A without a
    redesign. See the recommendation section of that doc.

  * The promotion cron from Proposal C is a separate, additive
    artifact — it will land in its own commit under F-D1 Phase 4.

  * Writes are single-homed for record kinds ("person", "project",
    "decision") with a *secondary projection* into the graph so the
    graph's semantic search surfaces them. This is not replication:
    the structured row stays canonical; the graph episode is a derived
    hook for search only.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from agent.knowledge_external_id import (
    KNOWLEDGE_KINDS,
    knowledge_external_id,
)

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------------
# Result shapes
# ------------------------------------------------------------------------

@dataclass
class KnowledgeWriteResult:
    """Outcome of a ``KnowledgeRouter.add`` call.

    ``external_id`` is always set (the caller can rely on it for cross-
    store joins). ``primary_layer`` names the store that took ownership
    of the write; ``secondary_layers`` is every additional store the
    router also notified (graph episodes projected off a structured
    write, for example).

    ``errors`` carries per-layer failures without short-circuiting the
    whole write. The caller can decide whether a partial write is
    acceptable; for most kinds it is (graph ingestion is best-effort).
    """

    external_id: str
    primary_layer: str
    secondary_layers: List[str] = field(default_factory=list)
    payload_summary: str = ""
    errors: Dict[str, str] = field(default_factory=dict)


@dataclass
class KnowledgeSearchHit:
    """One row of a ``KnowledgeRouter.search`` result.

    ``source_layer`` tells the caller which store produced the hit.
    ``score`` is the fused reciprocal-rank-fusion score (higher =
    better); it is comparable across source layers by construction of
    the fusion. ``external_id`` is populated when the underlying store
    carries one; Phase 1 stores don't, so most hits will have it as
    ``None`` until each store's schema catches up.
    """

    source_layer: str
    title: str
    snippet: str
    score: float
    external_id: Optional[str] = None
    raw: Any = None


# ------------------------------------------------------------------------
# Router
# ------------------------------------------------------------------------

# Layer-name constants — keep in sync with the underlying modules.
LAYER_WIKI = "wiki"
LAYER_GRAPH = "graph"
LAYER_STRUCTURED = "structured"
ALL_LAYERS: tuple[str, ...] = (LAYER_WIKI, LAYER_GRAPH, LAYER_STRUCTURED)


class KnowledgeRouter:
    """Facade over the three knowledge stores.

    Injected with the already-initialized dependencies so the router
    itself stays free of environment concerns (config loading, vault
    path resolution, Graphiti setup) — those live in the caller that
    builds the router. That keeps the router unit-testable with simple
    fakes and matches the dependency-injection pattern used elsewhere
    in ``agent/``.

    Any of the three collaborators can be ``None`` when the containing
    deployment has disabled that store. The router degrades gracefully:
    writes targeting a disabled layer raise a clear ``RuntimeError``;
    searches over a disabled layer are silently skipped.
    """

    def __init__(
        self,
        *,
        knowledge_manager: Any = None,
        graph_manager: Any = None,
        kb_tool_fn: Any = None,
    ) -> None:
        self._km = knowledge_manager
        self._gm = graph_manager
        self._kb = kb_tool_fn

    # --------------------------------------------------------------
    # Writes
    # --------------------------------------------------------------

    def add(
        self,
        kind: str,
        payload: Dict[str, Any],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> KnowledgeWriteResult:
        """Route a write to the right store(s) for this kind.

        Routing policy (from the F-D1 design doc):

        ==========   =======================================   =====================
        kind         primary store                             secondary projection
        ==========   =======================================   =====================
        decision     KnowledgeManager.save_decision            GraphManager episode
        person       KnowledgeManager.save_person              GraphManager episode
        project      KnowledgeManager.save_project             GraphManager episode
        concept      kb_tool(action="file")                    GraphManager episode
        reference    kb_tool(action="file")                    GraphManager episode
        episode      GraphManager.add_episode                  (none)
        note         KnowledgeManager.save_note                (none)
        ==========   =======================================   =====================

        Every payload gets a shared external ID so downstream joins
        across stores are cheap. When a store rejects the secondary
        projection (graph ingestion can be slow and may fail
        transiently), the router captures the error and continues —
        the primary write is what the caller's contract depends on.
        """
        if kind not in KNOWLEDGE_KINDS:
            raise ValueError(
                f"Unknown knowledge kind {kind!r}. "
                f"Valid kinds: {', '.join(sorted(KNOWLEDGE_KINDS))}"
            )

        metadata = dict(metadata or {})
        name = _extract_name(kind, payload)
        external_id = knowledge_external_id(kind, name)
        metadata.setdefault("external_id", external_id)

        dispatch = _ROUTING_TABLE[kind]
        primary_layer = dispatch["primary"]
        secondary_layers = list(dispatch.get("secondary", ()))
        errors: Dict[str, str] = {}

        # Primary write — must succeed, or we surface the failure
        # immediately. The caller's contract depends on the primary
        # layer acknowledging the write.
        try:
            self._write_to_layer(primary_layer, kind, payload, metadata, external_id)
        except Exception as exc:
            errors[primary_layer] = f"{type(exc).__name__}: {exc}"
            logger.error(
                "KnowledgeRouter primary write failed: kind=%s layer=%s error=%s",
                kind, primary_layer, exc,
            )
            return KnowledgeWriteResult(
                external_id=external_id,
                primary_layer=primary_layer,
                secondary_layers=[],
                payload_summary=_summarize_payload(kind, payload),
                errors=errors,
            )

        # Secondary projections — best-effort. Collect errors, keep going.
        for layer in secondary_layers:
            try:
                self._write_to_layer(layer, kind, payload, metadata, external_id)
            except Exception as exc:
                errors[layer] = f"{type(exc).__name__}: {exc}"
                logger.warning(
                    "KnowledgeRouter secondary projection failed: "
                    "kind=%s layer=%s error=%s",
                    kind, layer, exc,
                )

        return KnowledgeWriteResult(
            external_id=external_id,
            primary_layer=primary_layer,
            secondary_layers=[l for l in secondary_layers if l not in errors],
            payload_summary=_summarize_payload(kind, payload),
            errors=errors,
        )

    def _write_to_layer(
        self,
        layer: str,
        kind: str,
        payload: Dict[str, Any],
        metadata: Dict[str, Any],
        external_id: str,
    ) -> None:
        """Dispatch to one concrete store. Internal; ``add`` wraps error handling."""
        if layer == LAYER_STRUCTURED:
            self._write_structured(kind, payload, metadata, external_id)
            return
        if layer == LAYER_GRAPH:
            self._write_graph(kind, payload, metadata, external_id)
            return
        if layer == LAYER_WIKI:
            self._write_wiki(kind, payload, metadata, external_id)
            return
        raise ValueError(f"Unknown knowledge layer: {layer!r}")

    def _write_structured(
        self,
        kind: str,
        payload: Dict[str, Any],
        metadata: Dict[str, Any],
        external_id: str,
    ) -> None:
        if self._km is None:
            raise RuntimeError(
                f"knowledge layer {LAYER_STRUCTURED!r} is not available; "
                "KnowledgeManager was not provided to KnowledgeRouter"
            )
        tags = list(payload.get("tags") or [])
        # The external_id rides along in tags until each store's schema
        # grows a dedicated column (additive migration in a later phase).
        tags.append(f"external_id:{external_id}")

        if kind == "person":
            self._km.save_person(
                name=payload["name"],
                role=payload.get("role"),
                organization=payload.get("organization"),
                details=payload.get("details"),
                tags=tags,
            )
        elif kind == "project":
            self._km.save_project(
                name=payload["name"],
                description=payload.get("description"),
                status=payload.get("status", "active"),
                tags=tags,
            )
        elif kind == "decision":
            self._km.save_decision(
                title=payload["title"],
                rationale=payload.get("rationale"),
                status=payload.get("status", "active"),
                tags=tags,
            )
        elif kind == "note":
            self._km.save_note(
                content=payload["content"],
                tags=tags,
                session_id=metadata.get("session_id"),
            )
        else:
            raise ValueError(f"Kind {kind!r} does not route to structured layer")

    def _write_graph(
        self,
        kind: str,
        payload: Dict[str, Any],
        metadata: Dict[str, Any],
        external_id: str,
    ) -> None:
        if self._gm is None:
            raise RuntimeError(
                f"knowledge layer {LAYER_GRAPH!r} is not available; "
                "GraphManager was not provided to KnowledgeRouter"
            )
        content = _render_payload_as_episode_text(kind, payload)
        name = _extract_name(kind, payload)[:80]
        # GraphManager.add_episode is async. Run it on a dedicated event
        # loop if the caller isn't already in one — matches the pattern
        # used by memory_curator and other sync callers of the graph.
        enriched_metadata = dict(metadata)
        enriched_metadata["external_id"] = external_id
        enriched_metadata["kind"] = kind
        _run_async(
            self._gm.add_episode(
                content=content,
                source_type="text",
                name=name,
                metadata=enriched_metadata,
                group_id=metadata.get("group_id", "personal"),
            )
        )

    def _write_wiki(
        self,
        kind: str,
        payload: Dict[str, Any],
        metadata: Dict[str, Any],
        external_id: str,
    ) -> None:
        if self._kb is None:
            raise RuntimeError(
                f"knowledge layer {LAYER_WIKI!r} is not available; "
                "kb_tool function was not provided to KnowledgeRouter"
            )
        title = payload.get("title") or payload.get("name") or external_id
        content = payload.get("content") or payload.get("body") or ""
        if not content:
            raise ValueError(
                f"wiki write requires a 'content' or 'body' field in payload (kind={kind!r})"
            )
        tag_csv = ",".join(list(payload.get("tags") or []) + [f"external_id:{external_id}"])
        self._kb(
            action="file",
            title=title,
            content=content,
            page_type=kind,
            tags=tag_csv,
        )

    # --------------------------------------------------------------
    # Search
    # --------------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        layers: Sequence[str] = ALL_LAYERS,
        limit: int = 10,
    ) -> List[KnowledgeSearchHit]:
        """Cross-layer search with reciprocal-rank fusion.

        Runs the query against every enabled layer in ``layers``, then
        fuses per-layer ranking into a single score via RRF (k=60, the
        standard choice — see Cormack et al. 2009). Missing stores are
        silently skipped so search degrades gracefully when a layer is
        disabled.
        """
        if not query or not query.strip():
            return []
        query = query.strip()

        per_layer_hits: Dict[str, List[KnowledgeSearchHit]] = {}

        if LAYER_WIKI in layers and self._kb is not None:
            try:
                per_layer_hits[LAYER_WIKI] = self._search_wiki(query, limit)
            except Exception as exc:
                logger.warning("KnowledgeRouter wiki search failed: %s", exc)

        if LAYER_STRUCTURED in layers and self._km is not None:
            try:
                per_layer_hits[LAYER_STRUCTURED] = self._search_structured(query, limit)
            except Exception as exc:
                logger.warning("KnowledgeRouter structured search failed: %s", exc)

        if LAYER_GRAPH in layers and self._gm is not None:
            try:
                per_layer_hits[LAYER_GRAPH] = self._search_graph(query, limit)
            except Exception as exc:
                logger.warning("KnowledgeRouter graph search failed: %s", exc)

        return _reciprocal_rank_fusion(per_layer_hits, limit=limit)

    def _search_wiki(self, query: str, limit: int) -> List[KnowledgeSearchHit]:
        # kb_tool returns a JSON string; we parse opportunistically.
        import json as _json

        raw = self._kb(action="search", query=query, max_results=limit)
        try:
            parsed = _json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            return []
        if not isinstance(parsed, dict):
            return []
        matches = parsed.get("matches") or parsed.get("results") or []
        hits: List[KnowledgeSearchHit] = []
        for match in matches[:limit]:
            if not isinstance(match, dict):
                continue
            hits.append(
                KnowledgeSearchHit(
                    source_layer=LAYER_WIKI,
                    title=str(match.get("title") or match.get("page") or match.get("slug") or ""),
                    snippet=str(match.get("snippet") or match.get("excerpt") or ""),
                    score=0.0,  # filled in by RRF from position
                    external_id=None,
                    raw=match,
                )
            )
        return hits

    def _search_structured(self, query: str, limit: int) -> List[KnowledgeSearchHit]:
        # KnowledgeManager delegates to the underlying SessionDB search.
        db = getattr(self._km, "db", None)
        if db is None or not hasattr(db, "search_knowledge"):
            return []
        rows = db.search_knowledge(query=query, limit=limit) or []
        hits: List[KnowledgeSearchHit] = []
        for row in rows[:limit]:
            if not isinstance(row, dict):
                continue
            hits.append(
                KnowledgeSearchHit(
                    source_layer=LAYER_STRUCTURED,
                    title=str(row.get("name") or row.get("title") or ""),
                    snippet=str(row.get("details") or row.get("description") or row.get("content") or ""),
                    score=0.0,
                    external_id=row.get("external_id"),
                    raw=row,
                )
            )
        return hits

    def _search_graph(self, query: str, limit: int) -> List[KnowledgeSearchHit]:
        result = _run_async(self._gm.search(query=query, limit=limit))
        edges = (result or {}).get("edges") or []
        hits: List[KnowledgeSearchHit] = []
        for edge in edges[:limit]:
            if not isinstance(edge, dict):
                continue
            hits.append(
                KnowledgeSearchHit(
                    source_layer=LAYER_GRAPH,
                    title=str(edge.get("name") or edge.get("predicate") or ""),
                    snippet=str(edge.get("fact") or edge.get("summary") or ""),
                    score=0.0,
                    external_id=edge.get("external_id"),
                    raw=edge,
                )
            )
        return hits


# ------------------------------------------------------------------------
# Routing table — kept as module data so tests can introspect it.
# ------------------------------------------------------------------------

_ROUTING_TABLE: Dict[str, Dict[str, Any]] = {
    "person":    {"primary": LAYER_STRUCTURED, "secondary": (LAYER_GRAPH,)},
    "project":   {"primary": LAYER_STRUCTURED, "secondary": (LAYER_GRAPH,)},
    "decision":  {"primary": LAYER_STRUCTURED, "secondary": (LAYER_GRAPH,)},
    "note":      {"primary": LAYER_STRUCTURED, "secondary": ()},
    "concept":   {"primary": LAYER_WIKI,       "secondary": (LAYER_GRAPH,)},
    "reference": {"primary": LAYER_WIKI,       "secondary": (LAYER_GRAPH,)},
    "episode":   {"primary": LAYER_GRAPH,      "secondary": ()},
}


# ------------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------------

def _extract_name(kind: str, payload: Dict[str, Any]) -> str:
    """Pick the ID-seed field from a payload by kind.

    Centralized so the external ID is always computed over the same
    field regardless of caller. Raises on missing field so routing
    mistakes surface immediately rather than silently collapsing to
    empty-string IDs.
    """
    if kind in ("person", "project"):
        name = payload.get("name")
    elif kind == "decision":
        name = payload.get("title")
    elif kind in ("concept", "reference"):
        name = payload.get("title") or payload.get("name")
    elif kind == "note":
        content = payload.get("content") or ""
        name = content.strip().splitlines()[0][:80] if content.strip() else None
    elif kind == "episode":
        name = payload.get("name") or payload.get("title") or (payload.get("content") or "")[:80]
    else:
        name = payload.get("name") or payload.get("title")

    if not name:
        raise ValueError(
            f"payload for kind {kind!r} is missing the identifying field "
            f"(expected one of: name/title/content)"
        )
    return str(name)


def _render_payload_as_episode_text(kind: str, payload: Dict[str, Any]) -> str:
    """Render a structured payload into the prose the graph ingests.

    GraphManager's entity/relation extractor runs an LLM over a text
    episode — we give it a concise rendered form rather than a JSON
    blob so the extraction actually produces useful edges.
    """
    if kind == "person":
        parts = [f"Person: {payload.get('name', '')}"]
        if payload.get("role"):
            parts.append(f"Role: {payload['role']}")
        if payload.get("organization"):
            parts.append(f"Organization: {payload['organization']}")
        if payload.get("details"):
            parts.append(str(payload["details"]))
        return "\n".join(parts)

    if kind == "project":
        parts = [f"Project: {payload.get('name', '')}"]
        parts.append(f"Status: {payload.get('status', 'active')}")
        if payload.get("description"):
            parts.append(str(payload["description"]))
        return "\n".join(parts)

    if kind == "decision":
        parts = [f"Decision: {payload.get('title', '')}"]
        parts.append(f"Status: {payload.get('status', 'active')}")
        if payload.get("rationale"):
            parts.append(f"Rationale: {payload['rationale']}")
        return "\n".join(parts)

    # concept / reference / episode — body is the whole point.
    return str(
        payload.get("content")
        or payload.get("body")
        or payload.get("description")
        or ""
    )


def _summarize_payload(kind: str, payload: Dict[str, Any]) -> str:
    """Short human-readable label for logs and write-result introspection."""
    try:
        return f"{kind}:{_extract_name(kind, payload)[:60]}"
    except Exception:
        return kind


def _reciprocal_rank_fusion(
    per_layer_hits: Dict[str, List[KnowledgeSearchHit]],
    *,
    limit: int,
    k: int = 60,
) -> List[KnowledgeSearchHit]:
    """Fuse per-layer ranked results into a single list via RRF.

    RRF score = sum over layers of 1 / (k + rank_in_layer). The k=60
    default is the value used in the original Cormack et al. RRF paper
    and is what GraphManager's own fusion step uses internally — keeping
    them aligned means fusion across the router behaves consistently
    with single-layer fusion inside the graph.
    """
    scored: Dict[tuple[str, str], KnowledgeSearchHit] = {}
    for layer_hits in per_layer_hits.values():
        for rank, hit in enumerate(layer_hits):
            key = (hit.source_layer, hit.title or id(hit))  # type: ignore[arg-type]
            contrib = 1.0 / (k + rank + 1)
            if key in scored:
                scored[key].score += contrib
            else:
                new_hit = KnowledgeSearchHit(
                    source_layer=hit.source_layer,
                    title=hit.title,
                    snippet=hit.snippet,
                    score=contrib,
                    external_id=hit.external_id,
                    raw=hit.raw,
                )
                scored[key] = new_hit

    ordered = sorted(scored.values(), key=lambda h: h.score, reverse=True)
    return ordered[:limit]


def _run_async(coro: Any) -> Any:
    """Execute an async coroutine from sync code.

    Matches the pattern used by ``agent/memory_curator.py`` and other
    sync callers of the graph: if we're inside a running event loop,
    wrap the await in a thread to avoid the deadlock; otherwise
    ``asyncio.run`` works.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    # Already inside a loop — schedule on a dedicated thread so we can
    # block on the result without reentering the caller's loop.
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(asyncio.run, coro)
        return future.result()
