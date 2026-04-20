#!/usr/bin/env python3
"""
Knowledge Tool — Structured personal knowledge store.

Provides CRUD for four entity types (notes, people, projects, decisions) with
tag-based cross-linking.  Tags are the cross-table connector: a note tagged
['sarah', 'acme', 'partnership'] links to Sarah's person entry and any Acme
project, enabling queries like "everything about Sarah" across all tables.

Storage: knowledge_* tables in ~/.hermes/state.db (schema v9+).
Thread-safe via SessionDB._execute_write (BEGIN IMMEDIATE + jitter retry).

Design mirrors memory_tool.py: schema + handler + self-registration.
"""

import json
import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Content scanning — delegates to consolidated guardrails module
# ---------------------------------------------------------------------------

from agent.guardrails import scan_content as _scan_content


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

_VALID_ENTITY_TYPES = {"note", "person", "project", "decision"}


# ---------------------------------------------------------------------------
# F-D1 Phase 3 — KnowledgeRouter integration (feature-flagged)
# ---------------------------------------------------------------------------
#
# When ``knowledge.routing.enabled`` is on in config, two new behaviors
# activate:
#   1. The ``search_all_layers`` action performs a cross-store search
#      via ``agent.knowledge_router.KnowledgeRouter`` (wiki + graph +
#      structured rows, fused with reciprocal-rank fusion).
#   2. The four ``save_*`` actions propagate a shared external ID
#      (via ``agent.knowledge_external_id.knowledge_external_id``) into
#      the saved row's tags, making the new row joinable with wiki
#      pages + graph nodes written through the router.
#
# Flag off: existing behavior is unchanged. No call path through the
# router, no external-ID tags, no schema difference. The flag is
# checked at every call so a config reload is picked up without
# restarting.

def _knowledge_routing_enabled() -> bool:
    """Return True when the F-D1 routing feature flag is on.

    Read the config each call (cheap) so flag flips during a running
    session take effect on the next tool invocation. Any exception
    (config file missing, malformed, etc.) degrades to ``False`` so
    the tool never crashes on a config-layer problem.
    """
    try:
        from hermes_cli.config import load_config

        config = load_config() or {}
        routing = ((config.get("knowledge") or {}).get("routing") or {})
        return bool(routing.get("enabled", False))
    except Exception:
        return False


def _external_id_tag(kind: str, name: str) -> Optional[str]:
    """Return an ``external_id:<id>`` tag string for a save_* action.

    ``kind`` must be one of the ``KNOWLEDGE_KINDS`` — for this tool we
    map ``save_note`` → ``"note"``, ``save_person`` → ``"person"``,
    ``save_project`` → ``"project"``, ``save_decision`` → ``"decision"``.
    ``name`` is the entity's identifying field. Returns ``None`` if the
    ID can't be computed (empty name, unknown kind, etc.) — callers
    should skip the tag injection rather than crash.
    """
    try:
        from agent.knowledge_external_id import knowledge_external_id

        ext_id = knowledge_external_id(kind, name)
        return f"external_id:{ext_id}"
    except Exception as exc:
        logger.debug("knowledge_external_id skipped: %s", exc)
        return None


async def knowledge_tool(
    action: str,
    content: str = None,
    name: str = None,
    title: str = None,
    role: str = None,
    organization: str = None,
    details: str = None,
    description: str = None,
    rationale: str = None,
    status: str = None,
    tags: str = None,
    query: str = None,
    entity_type: str = None,
    tag: str = None,
    limit: int = 20,
    session_db=None,
    session_id: str = None,
    knowledge_manager=None,
) -> str:
    """Single entry point for the knowledge tool. Returns JSON string."""
    if session_db is None:
        return json.dumps({
            "success": False,
            "error": "Knowledge store not available (session database not initialized)."
        })

    # Cap limit to prevent unbounded result sets
    limit = min(int(limit), 100)

    # Parse tags from comma-separated string
    tag_list = []
    if tags:
        tag_list = [t.strip().lower() for t in tags.split(",") if t.strip()]

    if action == "save_note":
        if not content:
            return json.dumps({"success": False, "error": "content is required for save_note"})
        threat = _scan_content(content)
        if threat:
            return json.dumps({"success": False, "error": threat})

        # F-D1 Phase 3: inject external_id tag when routing is on so
        # the new row is joinable with wiki/graph entries for the same
        # conceptual entity. The seed is the note's first line (what
        # knowledge_router uses).
        if _knowledge_routing_enabled():
            note_seed = (content.strip().splitlines()[0] if content.strip() else "")[:80]
            ext_tag = _external_id_tag("note", note_seed) if note_seed else None
            if ext_tag and ext_tag not in tag_list:
                tag_list = tag_list + [ext_tag]

        if knowledge_manager:
            note_id = knowledge_manager.save_note(
                content=content, tags=tag_list, session_id=session_id
            )
        else:
            note_id = session_db.save_knowledge_note(
                content=content, tags=tag_list,
                source="conversation", session_id=session_id,
            )

        return json.dumps({
            "success": True, "action": "save_note",
            "id": note_id, "tags": tag_list,
            "message": f"Note saved (id={note_id}) with tags: {tag_list}" if tag_list else f"Note saved (id={note_id})"
        })

    elif action == "save_person":
        if not name:
            return json.dumps({"success": False, "error": "name is required for save_person"})
        for field in [name, role, organization, details]:
            if field:
                threat = _scan_content(field)
                if threat:
                    return json.dumps({"success": False, "error": threat})

        if _knowledge_routing_enabled():
            ext_tag = _external_id_tag("person", name)
            if ext_tag and ext_tag not in tag_list:
                tag_list = tag_list + [ext_tag]

        if knowledge_manager:
            person_id = knowledge_manager.save_person(
                name=name, role=role, organization=organization,
                details=details, tags=tag_list
            )
        else:
            person_id = session_db.save_knowledge_person(
                name=name, role=role, organization=organization,
                details=details, tags=tag_list,
            )
            
        return json.dumps({
            "success": True, "action": "save_person",
            "id": person_id, "name": name,
            "message": f"Person '{name}' saved (id={person_id})"
        })

    elif action == "save_project":
        if not name:
            return json.dumps({"success": False, "error": "name is required for save_project"})
        for field in [name, description]:
            if field:
                threat = _scan_content(field)
                if threat:
                    return json.dumps({"success": False, "error": threat})

        if _knowledge_routing_enabled():
            ext_tag = _external_id_tag("project", name)
            if ext_tag and ext_tag not in tag_list:
                tag_list = tag_list + [ext_tag]

        if knowledge_manager:
            project_id = knowledge_manager.save_project(
                name=name, description=description,
                status=status or "active", tags=tag_list
            )
        else:
            project_id = session_db.save_knowledge_project(
                name=name, description=description,
                status=status or "active", tags=tag_list,
            )
            
        return json.dumps({
            "success": True, "action": "save_project",
            "id": project_id, "name": name,
            "message": f"Project '{name}' saved (id={project_id})"
        })

    elif action == "save_decision":
        if not title:
            return json.dumps({"success": False, "error": "title is required for save_decision"})
        for field in [title, rationale]:
            if field:
                threat = _scan_content(field)
                if threat:
                    return json.dumps({"success": False, "error": threat})

        if _knowledge_routing_enabled():
            ext_tag = _external_id_tag("decision", title)
            if ext_tag and ext_tag not in tag_list:
                tag_list = tag_list + [ext_tag]

        if knowledge_manager:
            decision_id = knowledge_manager.save_decision(
                title=title, rationale=rationale,
                status=status or "active", tags=tag_list
            )
        else:
            decision_id = session_db.save_knowledge_decision(
                title=title, rationale=rationale,
                status=status or "active", tags=tag_list,
            )
            
        return json.dumps({
            "success": True, "action": "save_decision",
            "id": decision_id, "title": title,
            "message": f"Decision '{title}' saved (id={decision_id})"
        })

    elif action == "search":
        if entity_type and entity_type not in _VALID_ENTITY_TYPES:
            return json.dumps({
                "success": False,
                "error": f"Invalid entity_type '{entity_type}'. Use: {', '.join(sorted(_VALID_ENTITY_TYPES))}"
            })
        results = session_db.search_knowledge(
            query=query, entity_type=entity_type,
            tag=tag, limit=limit,
        )
        return json.dumps({
            "success": True, "action": "search",
            "count": len(results), "results": results,
        })

    elif action == "search_all_layers":
        # F-D1 Phase 3: cross-store search via KnowledgeRouter. Hits
        # the wiki + context graph + structured store in parallel and
        # fuses results with reciprocal-rank fusion. When the routing
        # flag is off, gracefully falls back to the SQLite-only search
        # so the action is always callable — just with a layer-scoped
        # result set when routing is disabled.
        if not query:
            return json.dumps({
                "success": False,
                "error": "query is required for search_all_layers"
            })

        if not _knowledge_routing_enabled():
            # Flag off — degrade to structured-only search and tag the
            # response so the caller can see why the result set is narrower.
            results = session_db.search_knowledge(
                query=query, entity_type=None, tag=None, limit=limit,
            )
            return json.dumps({
                "success": True, "action": "search_all_layers",
                "routing_enabled": False,
                "layers_searched": ["structured"],
                "count": len(results),
                "results": results,
                "note": (
                    "knowledge.routing.enabled is off in config — "
                    "falling back to structured-only search. Enable routing to "
                    "include the wiki + context graph layers."
                ),
            })

        # Flag on — full cross-layer search.
        try:
            from agent.knowledge_router import KnowledgeRouter
            from tools.kb_tool import kb_tool as _kb_tool_fn, check_kb_requirements

            kb_callable = _kb_tool_fn if check_kb_requirements() else None

            graph_manager = None
            try:
                # GraphManager is optional; context_graph tool's
                # initialization does the heavy lifting when available.
                from tools.context_graph_tool import _get_graph_manager
                graph_manager = _get_graph_manager()
            except Exception as exc:
                logger.debug("GraphManager unavailable for search_all_layers: %s", exc)

            router = KnowledgeRouter(
                knowledge_manager=knowledge_manager,
                graph_manager=graph_manager,
                kb_tool_fn=kb_callable,
            )
            hits = router.search(query=query, limit=limit)
            layers_searched = [
                layer for layer, present in (
                    ("structured", knowledge_manager is not None),
                    ("graph", graph_manager is not None),
                    ("wiki", kb_callable is not None),
                ) if present
            ]
            return json.dumps({
                "success": True, "action": "search_all_layers",
                "routing_enabled": True,
                "layers_searched": layers_searched,
                "count": len(hits),
                "results": [
                    {
                        "source_layer": h.source_layer,
                        "title": h.title,
                        "snippet": h.snippet,
                        "score": round(h.score, 6),
                        "external_id": h.external_id,
                    }
                    for h in hits
                ],
            })
        except Exception as exc:
            logger.warning("search_all_layers failed, falling back: %s", exc)
            results = session_db.search_knowledge(
                query=query, entity_type=None, tag=None, limit=limit,
            )
            return json.dumps({
                "success": True, "action": "search_all_layers",
                "routing_enabled": True,
                "layers_searched": ["structured"],
                "count": len(results),
                "results": results,
                "fallback_reason": f"{type(exc).__name__}: {exc}",
            })

    elif action == "list":
        if not entity_type:
            return json.dumps({"success": False, "error": "entity_type is required for list"})
        if entity_type not in _VALID_ENTITY_TYPES:
            return json.dumps({
                "success": False,
                "error": f"Invalid entity_type '{entity_type}'. Use: {', '.join(sorted(_VALID_ENTITY_TYPES))}"
            })
        results = session_db.list_knowledge(
            entity_type=entity_type, tag=tag, limit=limit,
        )
        return json.dumps({
            "success": True, "action": "list",
            "entity_type": entity_type, "count": len(results),
            "results": results,
        })

    elif action == "ingest":
        if not knowledge_manager:
            return json.dumps({"success": False, "error": "Obsidian vault not configured."})
        
        from cron.obsidian_ingest import ObsidianIngest
        ingestor = ObsidianIngest(
            db=session_db,
            vault_path=str(knowledge_manager.vault_path),
            agent_prefix=knowledge_manager.agent_prefix
        )
        ingestor.ingest_all()
        return json.dumps({
            "success": True, "action": "ingest",
            "message": "Obsidian vault scanned and ingested successfully."
        })

    elif action == "sync":
        if not knowledge_manager:
            return json.dumps({"success": False, "error": "Obsidian vault not configured."})
        
        created, skipped = await knowledge_manager.sync_episodes(limit=limit)
        return json.dumps({
            "success": True, "action": "sync",
            "created": created, "skipped": skipped,
            "message": f"Synced {created} episodes to Obsidian (skipped {skipped})."
        })

    else:
        return json.dumps({
            "success": False,
            "error": f"Unknown action '{action}'. Use: save_note, save_person, save_project, save_decision, search, search_all_layers, list, ingest, sync"
        })


def check_knowledge_requirements() -> bool:
    """Knowledge tool requires session DB — always register, gate at runtime."""
    return True


# =============================================================================
# OpenAI Function-Calling Schema
# =============================================================================

KNOWLEDGE_SCHEMA = {
    "name": "knowledge",
    "description": (
        "Save and query structured personal knowledge. Mirrors all data to your "
        "Obsidian vault if configured, creating a compounding knowledge base.\n\n"
        "WHEN TO USE (proactively, don't wait to be asked):\n"
        "- User mentions a person: save_person with their name, role, org\n"
        "- User shares a meeting note or observation: save_note with tags\n"
        "- User starts or mentions a project: save_project\n"
        "- User makes or describes a decision: save_decision with rationale\n"
        "- User asks 'what do I know about X': search with query or tag\n\n"
        "ACTIONS:\n"
        "- save_note: Save a note (requires content, optional tags)\n"
        "- save_person: Save/update a person (requires name)\n"
        "- save_project: Save/update a project (requires name)\n"
        "- save_decision: Record a decision (requires title, rationale)\n"
        "- search: Cross-table search by query text and/or tag\n"
        "- search_all_layers: Cross-store search (structured + wiki + context graph). "
        "When knowledge.routing.enabled is on, this is the recommended search action "
        "for 'what do I know about X' questions — it hits all three stores and fuses "
        "results. With the flag off, gracefully degrades to structured-only search.\n"
        "- list: List entities of a specific type\n"
        "- ingest: Manually trigger ingestion of your personal Obsidian notes into Hermes\n"
        "- sync: Export recent agent episodes/learnings to Obsidian\n\n"
        "This is DIFFERENT from the memory tool: memory is for agent instructions and "
        "user preferences. Knowledge is for facts about the user's world. This is also "
        "different from the 'kb' wiki tool: knowledge is the structured fact store, while "
        "the wiki is for compiled markdown synthesis and durable research pages."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["save_note", "save_person", "save_project", "save_decision", "search", "search_all_layers", "list", "ingest", "sync"],
                "description": "The action to perform."
            },
            "content": {
                "type": "string",
                "description": "Note content (for save_note)."
            },
            "name": {
                "type": "string",
                "description": "Person or project name (for save_person, save_project)."
            },
            "title": {
                "type": "string",
                "description": "Decision title (for save_decision)."
            },
            "role": {
                "type": "string",
                "description": "Person's role (for save_person)."
            },
            "organization": {
                "type": "string",
                "description": "Person's organization (for save_person)."
            },
            "details": {
                "type": "string",
                "description": "Additional details as JSON (for save_person)."
            },
            "description": {
                "type": "string",
                "description": "Project description (for save_project)."
            },
            "rationale": {
                "type": "string",
                "description": "Why this decision was made (for save_decision)."
            },
            "status": {
                "type": "string",
                "description": "Entity status: 'active', 'completed', 'paused', 'superseded', 'reversed'."
            },
            "tags": {
                "type": "string",
                "description": "Comma-separated tags for cross-linking (e.g. 'sarah,acme,partnership')."
            },
            "query": {
                "type": "string",
                "description": "Search text (for search action). Searches across names, content, titles."
            },
            "entity_type": {
                "type": "string",
                "enum": ["note", "person", "project", "decision"],
                "description": "Filter by entity type (for search/list)."
            },
            "tag": {
                "type": "string",
                "description": "Filter by a single tag (for search/list)."
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of results to return (default 20, max 100).",
                "default": 20
            }
        },
        "required": ["action"],
    },
}


# --- Registry ---
from tools.registry import registry

registry.register(
    name="knowledge",
    toolset="knowledge",
    schema=KNOWLEDGE_SCHEMA,
    is_async=True,
    handler=lambda args, **kw: knowledge_tool(
        action=args.get("action", ""),
        content=args.get("content"),
        name=args.get("name"),
        title=args.get("title"),
        role=args.get("role"),
        organization=args.get("organization"),
        details=args.get("details"),
        description=args.get("description"),
        rationale=args.get("rationale"),
        status=args.get("status"),
        tags=args.get("tags"),
        query=args.get("query"),
        entity_type=args.get("entity_type"),
        tag=args.get("tag"),
        limit=args.get("limit", 20),
        session_db=kw.get("session_db"),
        session_id=kw.get("session_id"),
        knowledge_manager=kw.get("knowledge_manager"),
    ),
    check_fn=check_knowledge_requirements,
    emoji="📚",
    mutates=True,
)
