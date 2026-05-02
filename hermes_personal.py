"""Personal infrastructure primitives for Hermes.

Canonical human-readable artifacts live under HERMES_HOME.  This module keeps
path rules, atomic artifact writes, event logging, feedback capture, failure
capsules, Obsidian mirroring, and pack installs in one import-safe place so CLI,
gateway, cron, tools, and tests do not drift.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
import urllib.request
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

import yaml

from hermes_constants import get_hermes_home
from utils import atomic_json_write

logger = logging.getLogger(__name__)

EVENT_SCHEMA_VERSION = 1
EVENT_TYPES = {
    "session.start",
    "session.end",
    "agent.step",
    "tool.call",
    "tool.result",
    "approval.requested",
    "approval.resolved",
    "memory.updated",
    "skill.installed",
    "pack.installed",
    "cron.started",
    "cron.completed",
    "rating.captured",
    "failure.captured",
    "work.created",
    "work.updated",
    "personal.updated",
    "obsidian.sync",
}

TELOS_FILES = (
    "README.md",
    "GOALS.md",
    "BELIEFS.md",
    "PROJECTS.md",
    "CHALLENGES.md",
    "WISDOM.md",
    "FRAMES.md",
)

TELOS_TEMPLATES = {
    "README.md": "# TELOS\n\nStable personal context for Hermes. Edit these files directly or through Companion.\n",
    "GOALS.md": "# Goals\n\n- \n",
    "BELIEFS.md": "# Beliefs\n\n- \n",
    "PROJECTS.md": "# Projects\n\n- \n",
    "CHALLENGES.md": "# Challenges\n\n- \n",
    "WISDOM.md": "# Wisdom\n\n- \n",
    "FRAMES.md": "# Frames\n\n- \n",
}

APPROVED_PACK_DESTINATIONS = ("skills", "user", "system", "workflows", "packs")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slugify(value: str, fallback: str = "item") -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return slug[:64] or fallback


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def atomic_text_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.stem}_",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


@dataclass(frozen=True)
class HermesPaths:
    home: Path | None = None

    def __post_init__(self) -> None:
        if self.home is None:
            object.__setattr__(self, "home", get_hermes_home())

    @property
    def system_dir(self) -> Path:
        return self.home / "system"

    @property
    def user_dir(self) -> Path:
        return self.home / "user"

    @property
    def telos_dir(self) -> Path:
        return self.user_dir / "telos"

    @property
    def skill_customizations_dir(self) -> Path:
        return self.user_dir / "skill-customizations"

    @property
    def projects_dir(self) -> Path:
        return self.user_dir / "projects"

    @property
    def events_dir(self) -> Path:
        return self.home / "events"

    @property
    def events_log(self) -> Path:
        return self.events_dir / "events.jsonl"

    @property
    def learning_dir(self) -> Path:
        return self.home / "learning"

    @property
    def ratings_log(self) -> Path:
        return self.learning_dir / "signals" / "ratings.jsonl"

    @property
    def failures_dir(self) -> Path:
        return self.learning_dir / "failures"

    @property
    def work_dir(self) -> Path:
        return self.home / "work"

    @property
    def packs_dir(self) -> Path:
        return self.home / "packs"

    @property
    def installed_packs_json(self) -> Path:
        return self.packs_dir / "installed.json"

    @property
    def pack_backups_dir(self) -> Path:
        return self.home / "backups" / "packs"

    def ensure(self) -> "HermesPaths":
        for path in (
            self.home,
            self.system_dir,
            self.user_dir,
            self.telos_dir,
            self.skill_customizations_dir,
            self.projects_dir,
            self.events_dir,
            self.learning_dir / "signals",
            self.failures_dir,
            self.work_dir,
            self.packs_dir,
            self.pack_backups_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)
        if not self.installed_packs_json.exists():
            atomic_json_write(self.installed_packs_json, {"schema_version": 1, "packs": []})
        return self

    def safe_home_relative(self, rel_path: str | Path) -> Path:
        rel = Path(rel_path)
        if rel.is_absolute() or ".." in rel.parts:
            raise ValueError("Path must be relative and stay inside HERMES_HOME")
        resolved = (self.home / rel).resolve()
        resolved.relative_to(self.home.resolve())
        return resolved

    def safe_user_relative(self, rel_path: str | Path) -> Path:
        rel = Path(rel_path)
        if rel.is_absolute() or ".." in rel.parts:
            raise ValueError("Path must be relative and stay inside user/")
        resolved = (self.user_dir / rel).resolve()
        resolved.relative_to(self.user_dir.resolve())
        return resolved

    def relative_to_home(self, path: Path) -> str:
        return str(path.resolve().relative_to(self.home.resolve()))


class PersonalArtifactStore:
    def __init__(self, paths: HermesPaths | None = None):
        self.paths = (paths or HermesPaths()).ensure()

    def init_defaults(self) -> dict[str, Any]:
        created: list[str] = []
        for name in TELOS_FILES:
            target = self.paths.telos_dir / name
            if not target.exists():
                atomic_text_write(target, TELOS_TEMPLATES[name])
                created.append(self.paths.relative_to_home(target))
        projects = self.paths.projects_dir / "PROJECTS.md"
        if not projects.exists():
            atomic_text_write(projects, "# Projects\n\n- \n")
            created.append(self.paths.relative_to_home(projects))
        self._index_personal_documents()
        HermesEventLog(self.paths).append(
            "personal.updated",
            source="cli",
            actor="system",
            summary="Initialized personal context files" if created else "Personal context already initialized",
            payload={"created": created},
        )
        return {"created": created, "home": str(self.paths.home)}

    def doctor(self) -> dict[str, Any]:
        self.paths.ensure()
        required = [
            self.paths.system_dir,
            self.paths.user_dir,
            self.paths.telos_dir,
            self.paths.skill_customizations_dir,
            self.paths.projects_dir,
            self.paths.events_dir,
            self.paths.learning_dir / "signals",
            self.paths.failures_dir,
            self.paths.work_dir,
            self.paths.packs_dir,
        ]
        missing = [str(path) for path in required if not path.exists()]
        telos_missing = [name for name in TELOS_FILES if not (self.paths.telos_dir / name).exists()]
        return {
            "ok": not missing and not telos_missing,
            "home": str(self.paths.home),
            "missing_directories": missing,
            "missing_telos_files": telos_missing,
        }

    def read_content(self, rel_path: str) -> dict[str, Any]:
        path = self.paths.safe_home_relative(rel_path)
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(rel_path)
        text = path.read_text(encoding="utf-8")
        return {
            "path": self.paths.relative_to_home(path),
            "content": text,
            "hash": _sha256_text(text),
            "updated_at": path.stat().st_mtime,
        }

    def write_content(self, rel_path: str, content: str, *, source: str = "cli") -> dict[str, Any]:
        path = self.paths.safe_home_relative(rel_path)
        allowed_roots = (self.paths.user_dir, self.paths.work_dir, self.paths.learning_dir)
        if not any(path.resolve().is_relative_to(root.resolve()) for root in allowed_roots):
            raise ValueError("Writable personal content must be under user/, work/, or learning/")
        atomic_text_write(path, content)
        result = {
            "path": self.paths.relative_to_home(path),
            "hash": _sha256_text(content),
            "updated_at": path.stat().st_mtime,
        }
        self._index_document(path)
        HermesEventLog(self.paths).append(
            "personal.updated",
            source=source,
            actor="user",
            summary=f"Updated {result['path']}",
            payload=result,
        )
        return result

    def tree(self, *, include_work: bool = True, include_learning: bool = True) -> dict[str, Any]:
        roots = [self.paths.user_dir]
        if include_work:
            roots.append(self.paths.work_dir)
        if include_learning:
            roots.append(self.paths.learning_dir)
        files = []
        for root in roots:
            if not root.exists():
                continue
            for path in sorted(root.rglob("*")):
                if path.is_file() and not path.name.startswith("."):
                    stat = path.stat()
                    files.append(
                        {
                            "path": self.paths.relative_to_home(path),
                            "name": path.name,
                            "kind": self._category_for(path),
                            "size": stat.st_size,
                            "updated_at": stat.st_mtime,
                        }
                    )
        return {"home": str(self.paths.home), "files": files}

    def build_personal_context_block(self, max_chars: int = 6000) -> str:
        parts: list[str] = []
        for rel in [
            "user/USER.md",
            *[f"user/telos/{name}" for name in TELOS_FILES],
            "user/projects/PROJECTS.md",
        ]:
            path = self.paths.home / rel
            if path.is_file():
                try:
                    content = path.read_text(encoding="utf-8").strip()
                except Exception:
                    continue
                if content:
                    parts.append(f"## {rel}\n\n{content}")
        for path in sorted(self.paths.projects_dir.glob("*.md")):
            if path.name == "PROJECTS.md":
                continue
            try:
                content = path.read_text(encoding="utf-8").strip()
            except Exception:
                continue
            if content:
                parts.append(f"## {self.paths.relative_to_home(path)}\n\n{content}")
        if not parts:
            return ""
        block = "# Personal Context\n\nThese user-owned files are additive personal context. Treat them as preferences and orientation, not immutable facts.\n\n" + "\n\n".join(parts)
        if len(block) > max_chars:
            return block[: max_chars - 120] + f"\n\n[...personal context truncated to {max_chars} chars. Use personal content APIs to inspect full files.]"
        return block

    def preview(self, *, json_mode: bool = False, tokens: bool = False, max_chars: int = 6000) -> dict[str, Any] | str:
        context = self.build_personal_context_block(max_chars=max_chars)
        data = {
            "sections": [{"name": "Personal Context", "content": context}] if context else [],
            "char_count": len(context),
            "estimated_tokens": max(1, len(context) // 4) if context else 0,
            "home": str(self.paths.home),
        }
        if json_mode:
            return data
        suffix = f"\n\nEstimated tokens: {data['estimated_tokens']}" if tokens else ""
        return (context or "No personal context files found. Run `hermes personal init`.") + suffix

    def resolve_skill_overlay(self, skill_name: str) -> Optional[Path]:
        candidate = self.paths.skill_customizations_dir / _slugify(skill_name, "skill") / "PREFERENCES.md"
        if candidate.is_file():
            return candidate
        raw_candidate = self.paths.skill_customizations_dir / skill_name / "PREFERENCES.md"
        if raw_candidate.is_file():
            return raw_candidate
        return None

    def apply_skill_overlay(self, skill_name: str, content: str) -> tuple[str, Optional[str]]:
        overlay = self.resolve_skill_overlay(skill_name)
        if not overlay:
            return content, None
        overlay_text = overlay.read_text(encoding="utf-8").strip()
        if not overlay_text:
            return content, None
        label = self.paths.relative_to_home(overlay)
        block = (
            "\n\n---\n\n"
            "## User Skill Customization Override\n\n"
            f"Source: `{label}`\n\n"
            "These user-owned preferences override or specialize the base skill. "
            "Do not edit the upstream skill file to satisfy these preferences.\n\n"
            f"{overlay_text}\n"
        )
        return content.rstrip() + block, label

    def sync_obsidian(self, vault_path: str | None = None) -> dict[str, Any]:
        service = ObsidianMirrorService(self.paths, vault_path=vault_path)
        return service.sync()

    def _category_for(self, path: Path) -> str:
        try:
            rel = path.resolve().relative_to(self.paths.home.resolve())
        except ValueError:
            return "external"
        parts = rel.parts
        if len(parts) >= 3 and parts[0] == "user" and parts[1] == "telos":
            return "telos"
        if len(parts) >= 2 and parts[0] == "user" and parts[1] == "projects":
            return "project"
        if parts and parts[0] == "work":
            return "work"
        if parts and parts[0] == "learning":
            return "learning"
        if parts and parts[0] == "user":
            return "personal"
        return "system"

    def _index_personal_documents(self) -> None:
        for item in self.tree(include_work=True, include_learning=True)["files"]:
            try:
                self._index_document(self.paths.home / item["path"])
            except Exception:
                logger.debug("Failed to index personal document %s", item["path"], exc_info=True)

    def _index_document(self, path: Path, obsidian_path: str | None = None, sync_status: str = "local") -> None:
        try:
            from hermes_state import SessionDB

            content = path.read_text(encoding="utf-8") if path.exists() else ""
            SessionDB().upsert_personal_document(
                {
                    "id": self.paths.relative_to_home(path),
                    "relative_path": self.paths.relative_to_home(path),
                    "title": path.stem,
                    "category": self._category_for(path),
                    "content_hash": _sha256_text(content),
                    "canonical_path": str(path),
                    "obsidian_path": obsidian_path,
                    "sync_status": sync_status,
                    "updated_at": path.stat().st_mtime if path.exists() else time.time(),
                    "metadata": {},
                }
            )
        except Exception:
            logger.debug("personal_documents indexing failed for %s", path, exc_info=True)


class HermesEventLog:
    def __init__(self, paths: HermesPaths | None = None):
        self.paths = (paths or HermesPaths()).ensure()

    def append(
        self,
        event_type: str,
        *,
        source: str,
        actor: str = "system",
        summary: str = "",
        session_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if event_type not in EVENT_TYPES:
            logger.debug("Unknown Hermes event type %s; recording anyway", event_type)
        event = {
            "schema_version": EVENT_SCHEMA_VERSION,
            "id": str(uuid.uuid4()),
            "timestamp": utc_now_iso(),
            "type": event_type,
            "source": source,
            "session_id": session_id,
            "actor": actor,
            "summary": summary,
            "payload": payload or {},
        }
        self.paths.events_log.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
        offset = self.paths.events_log.stat().st_size if self.paths.events_log.exists() else 0
        with self.paths.events_log.open("a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())
        self._index_best_effort(event, offset)
        return event

    def read(self, *, event_type: str | None = None, limit: int = 100, after: int | None = None) -> list[dict[str, Any]]:
        if not self.paths.events_log.exists():
            return []
        events: list[dict[str, Any]] = []
        with self.paths.events_log.open("r", encoding="utf-8") as f:
            if after is not None and after >= 0:
                f.seek(after)
            for line in f:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event_type and event.get("type") != event_type:
                    continue
                events.append(event)
        return events[-max(1, min(limit, 1000)) :]

    def tail_lines(self, *, event_type: str | None = None, limit: int = 100) -> Iterable[dict[str, Any]]:
        yield from self.read(event_type=event_type, limit=limit)

    def _index_best_effort(self, event: dict[str, Any], offset: int) -> None:
        try:
            from hermes_state import SessionDB

            db = SessionDB()
            if hasattr(db, "index_event"):
                db.index_event(event, str(self.paths.events_log), offset)
        except Exception:
            logger.debug("event_index insert failed for event %s", event.get("id"), exc_info=True)


class FeedbackStore:
    def __init__(self, paths: HermesPaths | None = None):
        self.paths = (paths or HermesPaths()).ensure()

    def rate(
        self,
        *,
        session_id: str,
        message_id: str,
        rating: int,
        comment: str = "",
        source: str = "cli",
    ) -> dict[str, Any]:
        rating = int(rating)
        if rating < 1 or rating > 10:
            raise ValueError("rating must be between 1 and 10")
        item = {
            "id": str(uuid.uuid4()),
            "session_id": session_id,
            "message_id": message_id,
            "rating": rating,
            "comment": comment,
            "source": source,
            "created_at": utc_now_iso(),
        }
        self.paths.ratings_log.parent.mkdir(parents=True, exist_ok=True)
        with self.paths.ratings_log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")
            f.flush()
            os.fsync(f.fileno())
        try:
            from hermes_state import SessionDB

            SessionDB().save_feedback_rating(item)
        except Exception:
            logger.debug("feedback_ratings insert failed", exc_info=True)
        HermesEventLog(self.paths).append(
            "rating.captured",
            source=source,
            actor="user",
            session_id=session_id,
            summary=f"Captured rating {rating}/10",
            payload={"rating_id": item["id"], "message_id": message_id, "rating": rating},
        )
        return item

    def capture_failure(
        self,
        *,
        session_id: str,
        message_id: str,
        reason: str = "",
        source: str = "cli",
        rating_id: str | None = None,
        rating: int | None = None,
        comment: str = "",
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        capsule_id = str(uuid.uuid4())
        slug = f"{now.strftime('%Y%m%d-%H%M%S')}_{_slugify(reason or message_id or session_id, 'failure')}"
        capsule_dir = self.paths.failures_dir / now.strftime("%Y-%m") / slug
        capsule_dir.mkdir(parents=True, exist_ok=True)

        messages = []
        actions = []
        try:
            from hermes_state import SessionDB

            db = SessionDB()
            messages = db.get_messages(session_id)
            actions = db.get_action_log(session_id=session_id, mutates_only=False, limit=200)
        except Exception:
            logger.debug("Failed to load transcript/tool calls for failure capsule", exc_info=True)

        nearby = messages[-12:] if messages else []
        last_assistant = next((m for m in reversed(messages) if m.get("role") == "assistant"), {})
        metadata = {
            "id": capsule_id,
            "session_id": session_id,
            "message_id": message_id,
            "rating_id": rating_id,
            "rating": rating,
            "comment": comment,
            "reason": reason,
            "status": "open",
            "created_at": utc_now_iso(),
            "capsule_path": str(capsule_dir),
            "last_assistant_preview": (last_assistant.get("content") or "")[:1000],
        }
        context_md = (
            f"# Failure Capsule\n\n"
            f"- Session: `{session_id}`\n"
            f"- Message: `{message_id}`\n"
            f"- Created: {metadata['created_at']}\n"
            f"- Rating: {rating if rating is not None else 'n/a'}\n\n"
            f"## User Reason\n\n{reason or '_No reason provided._'}\n\n"
            f"## Last Assistant Response\n\n{last_assistant.get('content') or '_Not available._'}\n"
        )
        atomic_text_write(capsule_dir / "CONTEXT.md", context_md)
        with (capsule_dir / "transcript.jsonl").open("w", encoding="utf-8") as f:
            for msg in nearby:
                f.write(json.dumps(msg, ensure_ascii=False, default=str) + "\n")
        atomic_json_write(capsule_dir / "tool-calls.json", actions, default=str)
        atomic_json_write(capsule_dir / "metadata.json", metadata, default=str)
        try:
            from hermes_state import SessionDB

            SessionDB().save_failure_capsule(metadata)
        except Exception:
            logger.debug("failure_capsules insert failed", exc_info=True)
        HermesEventLog(self.paths).append(
            "failure.captured",
            source=source,
            actor="user",
            session_id=session_id,
            summary="Captured manual failure capsule",
            payload={"failure_id": capsule_id, "path": str(capsule_dir), "message_id": message_id},
        )
        return metadata

    def list_failures(self, *, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        failures: list[dict[str, Any]] = []
        for metadata_path in sorted(self.paths.failures_dir.rglob("metadata.json"), reverse=True):
            try:
                item = json.loads(metadata_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if status and item.get("status") != status:
                continue
            failures.append(item)
            if len(failures) >= limit:
                break
        return failures

    def update_failure_status(self, failure_id: str, *, status: str, source: str = "cli") -> dict[str, Any]:
        status = status.strip().lower()
        if status not in {"open", "reviewed", "archived"}:
            raise ValueError("status must be open, reviewed, or archived")
        for metadata_path in self.paths.failures_dir.rglob("metadata.json"):
            try:
                item = json.loads(metadata_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if item.get("id") != failure_id:
                continue
            item["status"] = status
            if status == "reviewed":
                item["reviewed_at"] = utc_now_iso()
            item["updated_at"] = utc_now_iso()
            atomic_json_write(metadata_path, item, default=str)
            try:
                from hermes_state import SessionDB

                SessionDB().save_failure_capsule(item)
            except Exception:
                logger.debug("failure_capsules status update failed", exc_info=True)
            HermesEventLog(self.paths).append(
                "failure.captured",
                source=source,
                actor="user",
                session_id=item.get("session_id"),
                summary=f"Marked failure capsule {status}",
                payload={"failure_id": failure_id, "status": status},
            )
            return item
        raise FileNotFoundError(failure_id)


class WorkLedger:
    def __init__(self, paths: HermesPaths | None = None):
        self.paths = (paths or HermesPaths()).ensure()

    def start(self, title: str, *, session_id: str | None = None, goal: str = "", constraints: str = "", source: str = "cli") -> dict[str, Any]:
        created = datetime.now(timezone.utc)
        slug = f"{created.strftime('%Y%m%d-%H%M%S')}_{_slugify(title, 'work')}"
        work_path = self.paths.work_dir / slug / "WORK.md"
        content = (
            f"# {title}\n\n"
            f"status: open\n"
            f"created_at: {created.isoformat()}\n"
            f"updated_at: {created.isoformat()}\n"
            f"session_id: {session_id or ''}\n\n"
            f"## Goal\n\n{goal or title}\n\n"
            f"## Constraints\n\n{constraints or '- '}\n\n"
            "## Checklist\n\n- [ ] Define acceptance criteria\n\n"
            "## Decisions\n\n- \n\n"
            "## Validation\n\n- \n\n"
            "## Next Steps\n\n- \n"
        )
        atomic_text_write(work_path, content)
        item = self._metadata_for(work_path, session_id=session_id, status="open")
        try:
            from hermes_state import SessionDB

            SessionDB().upsert_work_item(item)
        except Exception:
            logger.debug("work_items insert failed", exc_info=True)
        HermesEventLog(self.paths).append(
            "work.created",
            source=source,
            actor="user",
            session_id=session_id,
            summary=f"Created work item {title}",
            payload=item,
        )
        return item

    def list(self, *, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for path in sorted(self.paths.work_dir.rglob("WORK.md"), reverse=True):
            item = self._metadata_for(path)
            if status and item.get("status") != status:
                continue
            items.append(item)
            if len(items) >= limit:
                break
        return items

    def _metadata_for(self, path: Path, *, session_id: str | None = None, status: str | None = None) -> dict[str, Any]:
        content = path.read_text(encoding="utf-8") if path.exists() else ""
        title = path.parent.name
        for line in content.splitlines():
            if line.startswith("# "):
                title = line[2:].strip()
                break
        if status is None:
            match = re.search(r"^status:\s*(.+)$", content, re.MULTILINE)
            status = match.group(1).strip() if match else "open"
        if session_id is None:
            match = re.search(r"^session_id:\s*(.+)$", content, re.MULTILINE)
            session_id = match.group(1).strip() if match else None
        stat = path.stat() if path.exists() else None
        return {
            "id": self.paths.relative_to_home(path),
            "slug": path.parent.name,
            "title": title,
            "status": status,
            "session_id": session_id,
            "work_path": str(path),
            "progress_summary": "",
            "created_at": stat.st_ctime if stat else time.time(),
            "updated_at": stat.st_mtime if stat else time.time(),
            "metadata": {},
        }


class ObsidianMirrorService:
    def __init__(self, paths: HermesPaths | None = None, vault_path: str | None = None):
        self.paths = (paths or HermesPaths()).ensure()
        self.vault_path = Path(vault_path).expanduser() if vault_path else self._configured_vault_path()

    def sync(self) -> dict[str, Any]:
        if not self.vault_path:
            return {"ok": False, "error": "No Obsidian vault path configured"}
        root = self.vault_path / "Hermes"
        root.mkdir(parents=True, exist_ok=True)
        mirrored: list[dict[str, Any]] = []
        conflicts: list[dict[str, Any]] = []
        store = PersonalArtifactStore(self.paths)
        for item in store.tree(include_work=True, include_learning=True)["files"]:
            src = self.paths.home / item["path"]
            dest = root / item["path"]
            dest.parent.mkdir(parents=True, exist_ok=True)
            src_text = src.read_text(encoding="utf-8")
            hermes_id = item["path"]
            mirrored_text = self._with_frontmatter(src_text, hermes_id)
            if dest.exists():
                existing = dest.read_text(encoding="utf-8")
                existing_body = self._strip_frontmatter(existing)
                if existing_body.strip() != src_text.strip() and _sha256_text(existing) != _sha256_text(mirrored_text):
                    conflicts.append({"path": item["path"], "vault_path": str(dest), "type": "divergent_vault_edit"})
                    continue
            atomic_text_write(dest, mirrored_text)
            store._index_document(src, obsidian_path=str(dest), sync_status="synced")
            mirrored.append({"path": item["path"], "vault_path": str(dest)})
        HermesEventLog(self.paths).append(
            "obsidian.sync",
            source="cli",
            actor="system",
            summary=f"Mirrored {len(mirrored)} Hermes files to Obsidian",
            payload={"vault_path": str(self.vault_path), "mirrored": mirrored, "conflicts": conflicts},
        )
        return {"ok": not conflicts, "vault_path": str(self.vault_path), "mirrored": mirrored, "conflicts": conflicts}

    def _configured_vault_path(self) -> Optional[Path]:
        config_path = self.paths.home / "config.yaml"
        if not config_path.exists():
            return None
        try:
            cfg = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
            vault = (cfg.get("knowledge") or {}).get("vault_path") or ""
            return Path(vault).expanduser() if vault else None
        except Exception:
            return None

    def _with_frontmatter(self, text: str, hermes_id: str) -> str:
        if text.startswith("---") and "hermes_id:" in text.split("---", 2)[1]:
            return text
        return f"---\nhermes_id: {json.dumps(hermes_id)}\n---\n\n{text}"

    def _strip_frontmatter(self, text: str) -> str:
        if text.startswith("---"):
            parts = text.split("---", 2)
            if len(parts) == 3:
                return parts[2].lstrip()
        return text


class PackManager:
    def __init__(self, paths: HermesPaths | None = None):
        self.paths = (paths or HermesPaths()).ensure()

    def inspect(self, path_or_url: str, *, dry_run: bool = True) -> dict[str, Any]:
        pack_dir = self._resolve_local_pack(path_or_url)
        pack_yaml = pack_dir / "pack.yaml"
        errors = []
        for required in ("README.md", "INSTALL.md", "VERIFY.md", "pack.yaml", "src"):
            if not (pack_dir / required).exists():
                errors.append(f"Missing {required}")
        metadata = {}
        if pack_yaml.exists():
            metadata = yaml.safe_load(pack_yaml.read_text(encoding="utf-8")) or {}
        pack_id = metadata.get("id") or _slugify(pack_dir.name, "pack")
        plan = []
        conflicts = []
        backups = []
        if not errors:
            for entry in self._content_entries(metadata, pack_dir):
                src = (pack_dir / "src" / entry["source"]).resolve()
                dest = self._safe_pack_destination(entry["destination"])
                files = [src] if src.is_file() else [p for p in sorted(src.rglob("*")) if p.is_file()]
                for file_path in files:
                    rel_under_src = file_path.relative_to(src) if src.is_dir() else Path(file_path.name)
                    final_dest = dest / rel_under_src
                    final_dest.relative_to(self.paths.home.resolve())
                    row = {
                        "source": str(file_path),
                        "destination": str(final_dest),
                        "exists": final_dest.exists(),
                    }
                    plan.append(row)
                    if final_dest.exists():
                        conflicts.append(row)
                        backups.append(str(final_dest))
        scan = self._security_scan(pack_dir, metadata)
        return {
            "ok": not errors and scan.get("allowed", True),
            "dry_run": dry_run,
            "pack_dir": str(pack_dir),
            "pack": metadata,
            "pack_id": pack_id,
            "readme_summary": self._read_pack_excerpt(pack_dir / "README.md"),
            "install_summary": self._read_pack_excerpt(pack_dir / "INSTALL.md"),
            "verify_summary": self._read_pack_excerpt(pack_dir / "VERIFY.md"),
            "errors": errors,
            "plan": plan,
            "conflicts": conflicts,
            "required_backups": backups,
            "security_scan": scan,
        }

    def install(self, path_or_url: str, *, dry_run: bool = False, backup: bool = True, source: str = "cli") -> dict[str, Any]:
        inspected = self.inspect(path_or_url, dry_run=True)
        if dry_run or not inspected["ok"]:
            return inspected
        pack = inspected["pack"]
        pack_id = inspected["pack_id"]
        backup_dir = None
        if backup and inspected["required_backups"]:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            backup_dir = self.paths.pack_backups_dir / pack_id / stamp
            for dest_str in inspected["required_backups"]:
                dest = Path(dest_str)
                backup_target = backup_dir / dest.relative_to(self.paths.home)
                backup_target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(dest, backup_target)
        installed_files = []
        for row in inspected["plan"]:
            src = Path(row["source"])
            dest = Path(row["destination"])
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            installed_files.append(str(dest))
        verify_result = self._run_declared_verification(pack, installed_files)
        registry = self._load_installed_registry()
        registry["packs"] = [p for p in registry.get("packs", []) if p.get("id") != pack_id]
        install_record = {
            "id": str(uuid.uuid4()),
            "pack_id": pack_id,
            "name": pack.get("name") or pack_id,
            "version": str(pack.get("version") or ""),
            "status": "installed" if verify_result.get("ok", True) else "verify_failed",
            "source_path": inspected["pack_dir"],
            "installed_at": utc_now_iso(),
            "verified_at": utc_now_iso() if verify_result else None,
            "backup_path": str(backup_dir) if backup_dir else None,
            "files": installed_files,
            "verification": verify_result,
        }
        registry["packs"].append(install_record)
        atomic_json_write(self.paths.installed_packs_json, registry, default=str)
        try:
            from hermes_state import SessionDB

            SessionDB().save_pack_install(install_record)
        except Exception:
            logger.debug("pack_installs insert failed", exc_info=True)
        HermesEventLog(self.paths).append(
            "pack.installed",
            source=source,
            actor="user",
            summary=f"Installed Hermes Pack {pack_id}",
            payload=install_record,
        )
        return {"ok": True, "installed": install_record, "plan": inspected["plan"]}

    def list_installed(self) -> dict[str, Any]:
        return self._load_installed_registry()

    def remove(self, pack_id: str) -> dict[str, Any]:
        registry = self._load_installed_registry()
        remaining = []
        removed = None
        for item in registry.get("packs", []):
            if item.get("pack_id") == pack_id or item.get("id") == pack_id:
                removed = item
            else:
                remaining.append(item)
        if not removed:
            return {"ok": False, "error": f"Pack {pack_id} is not installed"}
        registry["packs"] = remaining
        atomic_json_write(self.paths.installed_packs_json, registry, default=str)
        return {"ok": True, "removed": removed}

    def _resolve_local_pack(self, path_or_url: str) -> Path:
        if re.match(r"^https?://", path_or_url):
            return self._resolve_url_pack(path_or_url)
        pack_dir = Path(path_or_url).expanduser()
        if not pack_dir.is_absolute():
            pack_dir = Path.cwd() / pack_dir
        pack_dir = pack_dir.resolve()
        if not pack_dir.is_dir():
            raise FileNotFoundError(path_or_url)
        return pack_dir

    def _resolve_url_pack(self, url: str) -> Path:
        cache_root = self.paths.home / "cache" / "packs" / hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
        source_root = cache_root / "source"
        if source_root.exists():
            found = self._find_pack_root(source_root)
            if found:
                return found
            shutil.rmtree(source_root)
        cache_root.mkdir(parents=True, exist_ok=True)
        if url.endswith(".zip") or "/archive/" in url:
            archive_path = cache_root / "pack.zip"
            urllib.request.urlretrieve(url, archive_path)
            source_root.mkdir(parents=True, exist_ok=True)
            self._extract_zip_safely(archive_path, source_root)
            found = self._find_pack_root(source_root)
            if found:
                return found
            raise FileNotFoundError("Downloaded pack archive does not contain pack.yaml")

        subprocess.run(
            ["git", "clone", "--depth", "1", url, str(source_root)],
            cwd=str(cache_root),
            text=True,
            capture_output=True,
            timeout=120,
            check=True,
        )
        found = self._find_pack_root(source_root)
        if found:
            return found
        raise FileNotFoundError("Cloned pack repository does not contain pack.yaml")

    def _find_pack_root(self, root: Path) -> Optional[Path]:
        if (root / "pack.yaml").exists():
            return root
        for pack_yaml in sorted(root.rglob("pack.yaml")):
            return pack_yaml.parent
        return None

    def _extract_zip_safely(self, archive_path: Path, dest_root: Path) -> None:
        dest_root_resolved = dest_root.resolve()
        with zipfile.ZipFile(archive_path) as archive:
            for member in archive.infolist():
                target = (dest_root / member.filename).resolve()
                target.relative_to(dest_root_resolved)
            archive.extractall(dest_root)

    def _content_entries(self, metadata: dict[str, Any], pack_dir: Path) -> list[dict[str, str]]:
        contents = metadata.get("contents") or []
        if not contents:
            return [{"source": ".", "destination": f"packs/{metadata.get('id') or _slugify(pack_dir.name, 'pack')}/src"}]
        entries = []
        for item in contents:
            if isinstance(item, str):
                entries.append({"source": item, "destination": item})
            elif isinstance(item, dict):
                entries.append({"source": str(item.get("source", "")), "destination": str(item.get("destination", ""))})
        return entries

    def _safe_pack_destination(self, destination: str) -> Path:
        rel = Path(destination)
        if rel.is_absolute() or ".." in rel.parts:
            raise ValueError("Pack destination must be a safe relative HERMES_HOME path")
        if not rel.parts or rel.parts[0] not in APPROVED_PACK_DESTINATIONS:
            raise ValueError(f"Pack destination must start with one of: {', '.join(APPROVED_PACK_DESTINATIONS)}")
        dest = (self.paths.home / rel).resolve()
        dest.relative_to(self.paths.home.resolve())
        return dest

    def _security_scan(self, pack_dir: Path, metadata: dict[str, Any]) -> dict[str, Any]:
        try:
            from tools.skills_guard import format_scan_report, scan_skill, should_allow_install

            findings = []
            allowed = True
            for entry in self._content_entries(metadata, pack_dir):
                dest = Path(entry["destination"])
                src = pack_dir / "src" / entry["source"]
                if dest.parts and dest.parts[0] == "skills" and src.exists():
                    scan_root = src if src.is_dir() else src.parent
                    result = scan_skill(scan_root, source="community")
                    ok, reason = should_allow_install(result)
                    allowed = allowed and ok
                    findings.append({"path": str(scan_root), "allowed": ok, "reason": reason, "report": format_scan_report(result)})
            return {"allowed": allowed, "findings": findings}
        except Exception as exc:
            return {"allowed": False, "findings": [], "error": str(exc)}

    def _read_pack_excerpt(self, path: Path, limit: int = 1600) -> str:
        if not path.exists():
            return ""
        try:
            text = path.read_text(encoding="utf-8").strip()
        except Exception:
            return ""
        return text[:limit]

    def _run_declared_verification(self, pack: dict[str, Any], installed_files: list[str]) -> dict[str, Any]:
        checks = pack.get("post_install_verify") or []
        safe_checks: list[dict[str, Any]] = []
        for check in checks:
            if isinstance(check, dict) and check.get("safe") is True and check.get("command"):
                safe_checks.append(check)
        if not safe_checks:
            return {"ok": True, "skipped": True, "reason": "No safe verification commands declared"}
        results = []
        ok = True
        for check in safe_checks:
            command = check["command"]
            argv = command if isinstance(command, list) else shlex.split(str(command))
            if not argv:
                results.append({"command": command, "ok": False, "error": "Empty command"})
                ok = False
                continue
            try:
                completed = subprocess.run(
                    argv,
                    cwd=str(self.paths.home),
                    text=True,
                    capture_output=True,
                    timeout=int(check.get("timeout_seconds") or 30),
                    check=False,
                )
                row = {
                    "command": command,
                    "ok": completed.returncode == 0,
                    "returncode": completed.returncode,
                    "stdout": completed.stdout[-4000:],
                    "stderr": completed.stderr[-4000:],
                }
                results.append(row)
                ok = ok and row["ok"]
            except Exception as exc:
                results.append({"command": command, "ok": False, "error": str(exc)})
                ok = False
        return {"ok": ok, "skipped": False, "results": results, "installed_files": installed_files}

    def _load_installed_registry(self) -> dict[str, Any]:
        if not self.paths.installed_packs_json.exists():
            return {"schema_version": 1, "packs": []}
        try:
            data = json.loads(self.paths.installed_packs_json.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("schema_version", 1)
                data.setdefault("packs", [])
                return data
        except Exception:
            pass
        return {"schema_version": 1, "packs": []}
