"""Knowledge-promotion cron pipeline (F-D1 Phase 4).

Periodic job that moves high-value content between the three
knowledge stores. Lifted out of inline memory-curator code so it is
callable from ``hermes cron`` independently of a live session and so
Steps 2 and 3 have a natural home when their dependencies land.

Design source: Proposal C in
``docs/plans/2026-04-20-knowledge-routing-design.md``.

Three steps, chained in order:

  Step 1  MEMORY.md → wiki
          For each entry in MEMORY.md that the ``memory_curator``
          pipeline flags as high-value, file a wiki page and remove
          the source entry. Implemented by delegation to the existing
          ``agent.memory_curator.curate_memory`` — it already has the
          scoring prompt, the wiki filing path, and the dedup check
          against existing wiki pages.

  Step 2  wiki → graph  (stub — pending Phase-4b dependency)
          For each wiki page that has been read ≥ k times in the
          last 30 days OR whose slug matches an existing graph
          entity, call ``GraphManager.add_episode``. Currently a
          stub: wiki read-count tracking is not yet implemented. A
          follow-up commit adds a read-count file under
          ``~/.hermes/wiki/.read_counts.json`` that ``kb_tool`` bumps
          on ``action="read"``; at that point this step activates.

  Step 3  graph → structured  (stub — pending Phase-4c dependency)
          For each graph entity with ≥ m outgoing relations AND a
          recognizable type (Person/Project/Decision), auto-create a
          ``KnowledgeManager`` structured row tagged with the graph
          entity's external ID. Currently a stub: Graphiti's Kuzu
          schema does not yet expose a cheap "entities by type with
          degree ≥ m" query and we do not want to scan the whole
          edge set nightly. A follow-up commit adds a degree index
          (written by ``GraphManager`` at ingestion time); at that
          point this step activates.

Cron recipe. Register once with::

    hermes cron add "0 3 * * *" \
        "Run the Hermes knowledge-promotion pipeline (MEMORY → wiki → \
         graph → structured)." \
        --skills knowledge-promotion \
        --labels "[SILENT]"

Output lands under
``~/.hermes/cron/output/<job_id>/`` following the existing cron
convention. ``[SILENT]`` suppresses chat notifications when the run
has nothing to report (no promotions, no errors).

The entry point ``run_knowledge_promotion`` is designed to be called
from either the cron scheduler (via the cron job's `script` field
— see ``reference_cron_script_injection_automation.md`` in memory)
or directly in tests. It never raises: errors are captured into the
return dict so a nightly job can degrade gracefully.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class PromotionResult:
    """Structured summary of a single promotion run.

    Fields are filled in step-by-step so a failed Step 1 still
    surfaces Step-2/Step-3 status (they will say ``skipped: True``
    with a reason) rather than leaving the caller to guess which
    step crashed.
    """

    step1_memory_to_wiki: Dict[str, Any] = field(default_factory=dict)
    step2_wiki_to_graph: Dict[str, Any] = field(default_factory=dict)
    step3_graph_to_structured: Dict[str, Any] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step1_memory_to_wiki": self.step1_memory_to_wiki,
            "step2_wiki_to_graph": self.step2_wiki_to_graph,
            "step3_graph_to_structured": self.step3_graph_to_structured,
            "errors": list(self.errors),
        }

    def brief(self) -> str:
        """One-line summary for cron ``final_response`` / ``[SILENT]`` output."""
        step1_promoted = self.step1_memory_to_wiki.get("promoted", 0)
        step2_promoted = self.step2_wiki_to_graph.get("promoted", 0)
        step3_promoted = self.step3_graph_to_structured.get("promoted", 0)
        total = step1_promoted + step2_promoted + step3_promoted
        error_count = len(self.errors)
        if total == 0 and error_count == 0:
            return "knowledge promotion: no items promoted; no errors"
        return (
            f"knowledge promotion: {total} promoted "
            f"(memory→wiki={step1_promoted}, wiki→graph={step2_promoted}, "
            f"graph→structured={step3_promoted}); {error_count} error(s)"
        )


def run_knowledge_promotion(
    *,
    memory_store: Any = None,
    auxiliary_client: Any = None,
    auxiliary_model: Optional[str] = None,
    session_db: Any = None,
    graph_manager: Any = None,
    knowledge_manager: Any = None,
    dry_run: bool = False,
) -> PromotionResult:
    """Run the three-step promotion pipeline end to end.

    Parameters are all optional so the cron caller can build the
    dependencies it has available and pass ``None`` for ones that
    are not configured in the current environment. Missing
    dependencies cause the relevant step to be skipped with a
    reason, not crash.

    ``dry_run`` is threaded into Step 1 so callers can preview what
    would be promoted without mutating state. Steps 2 and 3 are
    stubs today, so ``dry_run`` does not yet affect them.
    """
    result = PromotionResult()

    # --- Step 1 ---------------------------------------------------
    try:
        step1 = _step1_memory_to_wiki(
            memory_store=memory_store,
            auxiliary_client=auxiliary_client,
            auxiliary_model=auxiliary_model,
            session_db=session_db,
            dry_run=dry_run,
        )
        result.step1_memory_to_wiki = step1
    except Exception as exc:
        logger.exception("knowledge_promotion step 1 failed")
        msg = f"step1_memory_to_wiki: {type(exc).__name__}: {exc}"
        result.errors.append(msg)
        result.step1_memory_to_wiki = {"skipped": True, "reason": msg}

    # --- Step 2 (stub) --------------------------------------------
    try:
        result.step2_wiki_to_graph = _step2_wiki_to_graph(
            graph_manager=graph_manager,
            dry_run=dry_run,
        )
    except Exception as exc:
        logger.exception("knowledge_promotion step 2 failed")
        msg = f"step2_wiki_to_graph: {type(exc).__name__}: {exc}"
        result.errors.append(msg)
        result.step2_wiki_to_graph = {"skipped": True, "reason": msg}

    # --- Step 3 (stub) --------------------------------------------
    try:
        result.step3_graph_to_structured = _step3_graph_to_structured(
            graph_manager=graph_manager,
            knowledge_manager=knowledge_manager,
            dry_run=dry_run,
        )
    except Exception as exc:
        logger.exception("knowledge_promotion step 3 failed")
        msg = f"step3_graph_to_structured: {type(exc).__name__}: {exc}"
        result.errors.append(msg)
        result.step3_graph_to_structured = {"skipped": True, "reason": msg}

    return result


# ------------------------------------------------------------------------
# Step 1 — MEMORY → wiki
# ------------------------------------------------------------------------

def _step1_memory_to_wiki(
    *,
    memory_store: Any,
    auxiliary_client: Any,
    auxiliary_model: Optional[str],
    session_db: Any,
    dry_run: bool,
) -> Dict[str, Any]:
    """Delegate to the existing memory_curator pipeline.

    ``curate_memory`` already handles the scoring LLM call, the
    duplicate/low-value removal, and the dedup-aware wiki filing
    (via the ``_promote_to_wiki`` helper inside ``memory_curator``).
    The cron wrapper's job is simply to orchestrate the preconditions
    (verify that a memory store exists, that an auxiliary LLM is
    configured, etc.) and translate the curator's result shape into
    the promotion result shape.
    """
    if memory_store is None:
        return {"skipped": True, "reason": "no memory_store provided"}
    if auxiliary_client is None or auxiliary_model is None:
        return {"skipped": True, "reason": "no auxiliary LLM configured"}

    from agent.memory_curator import curate_memory

    curator_result = curate_memory(
        memory_store=memory_store,
        auxiliary_client=auxiliary_client,
        auxiliary_model=auxiliary_model,
        session_db=session_db,
        session_id=None,
        dry_run=dry_run,
    )

    # curate_memory reports ``total_promoted`` (wiki + knowledge DB
    # combined). The cron wrapper distinguishes the wiki half so its
    # brief() output is accurate.
    promoted = int(curator_result.get("total_promoted", 0) or 0)
    removed = int(curator_result.get("total_removed", 0) or 0)
    return {
        "skipped": False,
        "dry_run": bool(dry_run),
        "promoted": promoted,
        "removed": removed,
        "targets_curated": curator_result.get("targets_curated", []),
    }


# ------------------------------------------------------------------------
# Step 2 — wiki → graph (stub)
# ------------------------------------------------------------------------

def _step2_wiki_to_graph(
    *,
    graph_manager: Any,
    dry_run: bool,
) -> Dict[str, Any]:
    """Promote "frequently-read" wiki pages into graph episodes.

    The design doc calls for wiki pages read ≥ k times in the last
    30 days OR whose slug matches an existing graph entity to be
    promoted into graph episodes. Wiki read-count tracking does not
    yet exist (``kb_tool`` has no ``action="read"`` counter). The
    placeholder returns ``skipped=True`` until Phase-4b adds the
    read-count file under ``~/.hermes/wiki/.read_counts.json``.

    The ``graph_manager`` argument is accepted now so the function
    signature is forward-compatible — when read-count tracking lands,
    only the *body* of this function changes.
    """
    return {
        "skipped": True,
        "reason": (
            "Phase-4b pending: wiki read-count tracking not yet implemented. "
            "Will activate when kb_tool records read counts under "
            "~/.hermes/wiki/.read_counts.json."
        ),
        "promoted": 0,
    }


# ------------------------------------------------------------------------
# Step 3 — graph → structured (stub)
# ------------------------------------------------------------------------

def _step3_graph_to_structured(
    *,
    graph_manager: Any,
    knowledge_manager: Any,
    dry_run: bool,
) -> Dict[str, Any]:
    """Create structured rows for graph entities with enough edges.

    The design doc calls for graph entities with ≥ m outgoing
    relations AND a recognizable type (Person/Project/Decision) to
    spawn a structured row tagged with the entity's external ID.
    Graphiti's Kuzu schema does not yet expose a cheap "entities by
    type with degree ≥ m" query, and scanning the full edge set
    nightly is wasteful. The placeholder returns ``skipped=True``
    until Phase-4c adds a degree index maintained at ingestion time
    by ``GraphManager.add_episode``.

    Both ``graph_manager`` and ``knowledge_manager`` are accepted now
    so callers can start wiring the dependency without waiting for
    the activation commit.
    """
    return {
        "skipped": True,
        "reason": (
            "Phase-4c pending: graph degree index not yet implemented. "
            "Will activate when GraphManager maintains an entity-degree "
            "index at ingestion time so this step can enumerate "
            "high-degree entities cheaply."
        ),
        "promoted": 0,
    }
