"""Filesystem-backed Hermes patterns, contexts, strategies, and ingest artifacts.

Patterns are lightweight prompt recipes.  Skills remain Hermes' richer
procedural capability format; this module only handles prompt assembly and
small reusable content artifacts.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional
from urllib.parse import urlparse

import yaml

from hermes_constants import get_hermes_home

HERMES_PATTERN_MODEL_PREFIX = "hermes-agent:pattern/"

FABRIC_STARTER_PACK = [
    "summarize",
    "extract_wisdom",
    "analyze_claims",
    "analyze_paper",
    "create_prd",
    "write_pull-request",
    "create_summary",
    "youtube_summary",
    "create_security_update",
    "create_stride_threat_model",
]

FABRIC_RAW_BASE_URL = "https://raw.githubusercontent.com/danielmiessler/Fabric/main/data/patterns"
FABRIC_REPO_URL = "https://github.com/danielmiessler/Fabric"

_PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_-]*)\s*\}\}")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_VTT_TIMESTAMP_RE = re.compile(r"^\d{2}:\d{2}:\d{2}\.\d{3}\s+-->\s+\d{2}:\d{2}:\d{2}\.\d{3}")


class PatternError(ValueError):
    """Raised when a pattern, context, strategy, or ingest artifact is invalid."""


@dataclass(frozen=True)
class PatternMetadata:
    name: str
    description: str = ""
    tags: List[str] = field(default_factory=list)
    default_model: Optional[str] = None
    default_strategy: Optional[str] = None
    source: str = "hermes"
    source_url: Optional[str] = None
    license: Optional[str] = None

    @classmethod
    def from_mapping(cls, name: str, data: Mapping[str, Any]) -> "PatternMetadata":
        tags = data.get("tags") or []
        if isinstance(tags, str):
            tags = [item.strip() for item in tags.split(",") if item.strip()]
        if not isinstance(tags, list):
            tags = []
        return cls(
            name=str(data.get("name") or name),
            description=str(data.get("description") or ""),
            tags=[str(tag) for tag in tags],
            default_model=_none_if_blank(data.get("default_model")),
            default_strategy=_none_if_blank(data.get("default_strategy")),
            source=str(data.get("source") or "hermes"),
            source_url=_none_if_blank(data.get("source_url")),
            license=_none_if_blank(data.get("license")),
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PatternRecord:
    name: str
    system: str
    metadata: PatternMetadata
    path: Path

    def to_summary(self) -> Dict[str, Any]:
        data = self.metadata.to_dict()
        data["name"] = self.name
        return data

    def to_dict(self) -> Dict[str, Any]:
        data = self.to_summary()
        data["system"] = self.system
        data["path"] = str(self.path)
        return data


@dataclass(frozen=True)
class StrategyRecord:
    name: str
    description: str
    prompt: str
    path: Path

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "prompt": self.prompt,
            "path": str(self.path),
        }


@dataclass(frozen=True)
class IngestArtifact:
    id: str
    source_type: str
    source: str
    title: str
    created_at: str
    content_path: str
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RenderedPattern:
    pattern: PatternRecord
    input_text: str
    system_prompt: str
    context_name: Optional[str] = None
    strategy_name: Optional[str] = None
    variables: Dict[str, str] = field(default_factory=dict)
    input_ref: Optional[str] = None
    resolved_model: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pattern": self.pattern.to_summary(),
            "input": self.input_text,
            "system_prompt": self.system_prompt,
            "context": self.context_name,
            "strategy": self.strategy_name,
            "variables": dict(self.variables),
            "input_ref": self.input_ref,
            "resolved_model": self.resolved_model,
        }


def is_pattern_model_id(model_id: str) -> bool:
    return isinstance(model_id, str) and model_id.startswith(HERMES_PATTERN_MODEL_PREFIX)


def pattern_name_from_model_id(model_id: str) -> Optional[str]:
    if not is_pattern_model_id(model_id):
        return None
    name = model_id[len(HERMES_PATTERN_MODEL_PREFIX):].strip()
    return name or None


def sync_builtin_assets(home: Optional[Path] = None) -> None:
    """Copy bundled seed patterns and strategies into HERMES_HOME if absent."""
    library = PatternLibrary(home=home)
    library.ensure_storage()
    seed_root = Path(__file__).parent / "seeds"
    if not seed_root.exists():
        return

    for source_dir, target_dir in (
        (seed_root / "patterns", library.patterns_dir),
        (seed_root / "contexts", library.contexts_dir),
        (seed_root / "strategies", library.strategies_dir),
    ):
        if not source_dir.exists():
            continue
        for source in source_dir.rglob("*"):
            if source.is_dir():
                continue
            relative = source.relative_to(source_dir)
            target = target_dir / relative
            if target.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)


class PatternLibrary:
    """HERMES_HOME-backed pattern library."""

    def __init__(self, home: Optional[Path] = None):
        self.home = Path(home) if home is not None else get_hermes_home()
        self.patterns_dir = self.home / "patterns"
        self.contexts_dir = self.home / "contexts"
        self.strategies_dir = self.home / "strategies"
        self.ingest_dir = self.home / "ingest"

    def ensure_storage(self) -> None:
        for directory in (self.patterns_dir, self.contexts_dir, self.strategies_dir, self.ingest_dir):
            directory.mkdir(parents=True, exist_ok=True)

    def list_patterns(self) -> List[PatternRecord]:
        self.ensure_storage()
        records: List[PatternRecord] = []
        for path in sorted(self.patterns_dir.iterdir(), key=lambda p: p.name):
            if path.is_dir() and (path / "system.md").exists():
                records.append(self.get_pattern(path.name))
        return records

    def get_pattern(self, name: str) -> PatternRecord:
        safe_name = _safe_name(name)
        path = self.patterns_dir / safe_name
        system_path = path / "system.md"
        if not system_path.exists():
            raise PatternError(f"Pattern not found: {name}")
        metadata_path = path / "metadata.yaml"
        metadata_data: Dict[str, Any] = {}
        if metadata_path.exists():
            loaded = yaml.safe_load(metadata_path.read_text(encoding="utf-8")) or {}
            if isinstance(loaded, Mapping):
                metadata_data = dict(loaded)
        metadata = PatternMetadata.from_mapping(safe_name, metadata_data)
        return PatternRecord(
            name=safe_name,
            system=system_path.read_text(encoding="utf-8").strip(),
            metadata=metadata,
            path=path,
        )

    def list_contexts(self) -> List[Dict[str, Any]]:
        self.ensure_storage()
        contexts = []
        for path in sorted(self.contexts_dir.glob("*.md"), key=lambda p: p.stem):
            contexts.append({
                "name": path.stem,
                "description": _first_non_empty_line(path.read_text(encoding="utf-8")),
                "path": str(path),
            })
        return contexts

    def get_context(self, name: str) -> str:
        path = self.contexts_dir / f"{_safe_name(name)}.md"
        if not path.exists():
            raise PatternError(f"Context not found: {name}")
        return path.read_text(encoding="utf-8").strip()

    def create_context(self, name: str, content: str = "") -> Path:
        self.ensure_storage()
        path = self.contexts_dir / f"{_safe_name(name)}.md"
        if path.exists():
            raise PatternError(f"Context already exists: {name}")
        path.write_text(content, encoding="utf-8")
        return path

    def delete_context(self, name: str) -> None:
        path = self.contexts_dir / f"{_safe_name(name)}.md"
        if not path.exists():
            raise PatternError(f"Context not found: {name}")
        path.unlink()

    def list_strategies(self) -> List[StrategyRecord]:
        self.ensure_storage()
        return [self.get_strategy(path.stem) for path in sorted(self.strategies_dir.glob("*.json"))]

    def get_strategy(self, name: str) -> StrategyRecord:
        path = self.strategies_dir / f"{_safe_name(name)}.json"
        if not path.exists():
            raise PatternError(f"Strategy not found: {name}")
        data = json.loads(path.read_text(encoding="utf-8"))
        return StrategyRecord(
            name=str(data.get("name") or path.stem),
            description=str(data.get("description") or ""),
            prompt=str(data.get("prompt") or ""),
            path=path,
        )

    def render_pattern(
        self,
        name: str,
        input_text: str = "",
        *,
        context: Optional[str] = None,
        strategy: Optional[str] = None,
        variables: Optional[Mapping[str, Any]] = None,
        input_ref: Optional[str] = None,
        config: Optional[Mapping[str, Any]] = None,
        explicit_model: Optional[str] = None,
        active_model: Optional[str] = None,
    ) -> RenderedPattern:
        self.ensure_storage()
        pattern = self.get_pattern(name)
        variables_text = {str(k): str(v) for k, v in (variables or {}).items()}
        resolved_input = self.resolve_input(input_text, input_ref=input_ref)

        pattern_text = pattern.system
        had_input_placeholder = bool(re.search(r"\{\{\s*input\s*\}\}", pattern_text))
        replace_values = dict(variables_text)
        replace_values["input"] = resolved_input
        pattern_text = _render_placeholders(pattern_text, replace_values)
        if not had_input_placeholder and resolved_input:
            pattern_text = f"{pattern_text.rstrip()}\n\n{resolved_input}".strip()

        selected_strategy = strategy or pattern.metadata.default_strategy
        sections: List[str] = []
        if context:
            sections.append(self.get_context(context))
        if selected_strategy:
            sections.append(self.get_strategy(selected_strategy).prompt)
        sections.append(pattern_text)

        return RenderedPattern(
            pattern=pattern,
            input_text=resolved_input,
            system_prompt="\n\n".join(section.strip() for section in sections if section and section.strip()),
            context_name=context,
            strategy_name=selected_strategy,
            variables=variables_text,
            input_ref=input_ref,
            resolved_model=self.resolve_model(pattern, config=config, explicit_model=explicit_model, active_model=active_model),
        )

    def resolve_model(
        self,
        pattern: PatternRecord,
        *,
        config: Optional[Mapping[str, Any]] = None,
        explicit_model: Optional[str] = None,
        active_model: Optional[str] = None,
    ) -> Optional[str]:
        if explicit_model:
            return explicit_model
        patterns_config = _config_get(config, "patterns", None)
        if patterns_config is not None:
            overrides = _config_get(patterns_config, "model_overrides", {}) or {}
            if isinstance(overrides, Mapping):
                override = _none_if_blank(overrides.get(pattern.name))
                if override:
                    return override
        if pattern.metadata.default_model:
            return pattern.metadata.default_model
        return active_model

    def resolve_input(self, input_text: str = "", *, input_ref: Optional[str] = None) -> str:
        if input_ref:
            artifact = self.get_ingest_artifact(input_ref)
            content_path = Path(artifact.content_path)
            if not content_path.exists():
                raise PatternError(f"Ingest artifact content missing: {input_ref}")
            content = content_path.read_text(encoding="utf-8")
            return content if not input_text else f"{content}\n\n{input_text}"
        return input_text or ""

    def ingest_file(self, path: Path) -> IngestArtifact:
        source_path = Path(path).expanduser()
        if not source_path.exists() or not source_path.is_file():
            raise PatternError(f"File not found: {path}")
        content = source_path.read_text(encoding="utf-8", errors="replace")
        return self._store_ingest_artifact(
            source_type="file",
            source=str(source_path),
            title=source_path.name,
            content=content,
            metadata={"size_bytes": source_path.stat().st_size},
        )

    def ingest_url(self, url: str, *, fetcher: Optional[Callable[[str], bytes]] = None) -> IngestArtifact:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise PatternError("URL ingestion supports http and https URLs")
        raw = fetcher(url) if fetcher else _default_fetcher(url)
        text = raw.decode("utf-8", errors="replace")
        title = _extract_html_title(text) or parsed.netloc or url
        content = _html_to_text(text)
        return self._store_ingest_artifact(
            source_type="url",
            source=url,
            title=title,
            content=content,
            metadata={"url": url},
        )

    def ingest_clipboard(self) -> IngestArtifact:
        if shutil.which("pbpaste") is None:
            raise PatternError("Clipboard ingestion is only available on macOS with pbpaste")
        result = subprocess.run(["pbpaste"], check=False, capture_output=True, text=True)
        if result.returncode != 0:
            raise PatternError(result.stderr.strip() or "Failed to read clipboard")
        return self._store_ingest_artifact(
            source_type="clipboard",
            source="clipboard",
            title="Clipboard",
            content=result.stdout,
            metadata={},
        )

    def ingest_text(self, content: str, *, title: str = "Shared Text", source: str = "text") -> IngestArtifact:
        if not content:
            raise PatternError("Text ingestion requires non-empty content")
        return self._store_ingest_artifact(
            source_type="text",
            source=source,
            title=title,
            content=content,
            metadata={"length": len(content)},
        )

    def ingest_youtube(self, url: str, *, timestamps: bool = False) -> IngestArtifact:
        if shutil.which("yt-dlp") is None:
            raise PatternError(
                "YouTube ingestion requires yt-dlp. Install it with `brew install yt-dlp` "
                "or `pipx install yt-dlp`, then retry."
            )
        with tempfile.TemporaryDirectory(prefix="hermes-youtube-") as tmp:
            tmp_dir = Path(tmp)
            metadata = self._youtube_metadata(url)
            cmd = [
                "yt-dlp",
                "--skip-download",
                "--write-auto-subs",
                "--write-subs",
                "--sub-langs",
                "en.*",
                "--sub-format",
                "vtt",
                "--output",
                str(tmp_dir / "%(id)s.%(ext)s"),
                url,
            ]
            result = subprocess.run(cmd, check=False, capture_output=True, text=True)
            if result.returncode != 0:
                raise PatternError(result.stderr.strip() or "yt-dlp failed to fetch transcript")
            vtt_files = sorted(tmp_dir.glob("*.vtt"))
            if not vtt_files:
                raise PatternError("No English transcript was available for this YouTube URL")
            transcript = _parse_vtt(vtt_files[0].read_text(encoding="utf-8", errors="replace"), timestamps=timestamps)
            title = str(metadata.get("title") or url)
            return self._store_ingest_artifact(
                source_type="youtube",
                source=url,
                title=title,
                content=transcript,
                metadata=metadata,
            )

    def _youtube_metadata(self, url: str) -> Dict[str, Any]:
        result = subprocess.run(
            ["yt-dlp", "--dump-json", "--skip-download", url],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return {"metadata_error": result.stderr.strip()}
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            return {}
        allowed = ("id", "title", "uploader", "duration", "webpage_url", "upload_date")
        return {key: data.get(key) for key in allowed if data.get(key) is not None}

    def _store_ingest_artifact(
        self,
        *,
        source_type: str,
        source: str,
        title: str,
        content: str,
        metadata: Mapping[str, Any],
    ) -> IngestArtifact:
        self.ensure_storage()
        digest = hashlib.sha256(f"{source_type}\0{source}\0{content}".encode("utf-8", errors="replace")).hexdigest()
        artifact_dir = self.ingest_dir / digest
        artifact_dir.mkdir(parents=True, exist_ok=True)
        content_path = artifact_dir / "content.md"
        artifact_path = artifact_dir / "artifact.json"
        content_path.write_text(content, encoding="utf-8")
        artifact = IngestArtifact(
            id=digest,
            source_type=source_type,
            source=source,
            title=title,
            created_at=datetime.now(timezone.utc).isoformat(),
            content_path=str(content_path),
            metadata=dict(metadata),
        )
        artifact_path.write_text(json.dumps(artifact.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
        return artifact

    def get_ingest_artifact(self, artifact_id: str) -> IngestArtifact:
        safe_id = _safe_digest(artifact_id)
        path = self.ingest_dir / safe_id / "artifact.json"
        if not path.exists():
            raise PatternError(f"Ingest artifact not found: {artifact_id}")
        data = json.loads(path.read_text(encoding="utf-8"))
        return IngestArtifact(
            id=str(data["id"]),
            source_type=str(data["source_type"]),
            source=str(data["source"]),
            title=str(data.get("title") or ""),
            created_at=str(data.get("created_at") or ""),
            content_path=str(data["content_path"]),
            metadata=dict(data.get("metadata") or {}),
        )

    def import_fabric_starter_pack(
        self,
        *,
        force: bool = False,
        fetcher: Optional[Callable[[str], bytes]] = None,
        names: Optional[Iterable[str]] = None,
    ) -> Dict[str, List[str]]:
        self.ensure_storage()
        fetcher = fetcher or _default_fetcher
        imported: List[str] = []
        skipped: List[str] = []
        failed: List[str] = []
        for name in names or FABRIC_STARTER_PACK:
            safe_name = _safe_name(name)
            target = self.patterns_dir / safe_name
            if target.exists() and not force:
                skipped.append(safe_name)
                continue
            url = f"{FABRIC_RAW_BASE_URL}/{safe_name}/system.md"
            try:
                prompt = fetcher(url).decode("utf-8", errors="replace").strip()
            except Exception:
                failed.append(safe_name)
                continue
            target.mkdir(parents=True, exist_ok=True)
            (target / "system.md").write_text(prompt, encoding="utf-8")
            metadata = PatternMetadata(
                name=safe_name,
                description=f"Imported Fabric pattern: {safe_name}",
                tags=["fabric"],
                source="fabric",
                source_url=f"{FABRIC_REPO_URL}/tree/main/data/patterns/{safe_name}",
                license="MIT",
            )
            (target / "metadata.yaml").write_text(yaml.safe_dump(metadata.to_dict(), sort_keys=False), encoding="utf-8")
            imported.append(safe_name)
        return {"imported": imported, "skipped": skipped, "failed": failed}


def import_fabric_starter_pack(**kwargs: Any) -> Dict[str, List[str]]:
    return PatternLibrary().import_fabric_starter_pack(**kwargs)


def _none_if_blank(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _config_get(config: Any, key: str, default: Any = None) -> Any:
    if config is None:
        return default
    if isinstance(config, Mapping):
        return config.get(key, default)
    getter = getattr(config, "get", None)
    if callable(getter):
        return getter(key, default)
    return getattr(config, key, default)


def _safe_name(name: str) -> str:
    text = str(name).strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", text):
        raise PatternError(f"Invalid name: {name!r}")
    return text


def _safe_digest(value: str) -> str:
    text = str(value).strip()
    if not re.fullmatch(r"[a-fA-F0-9]{64}", text):
        raise PatternError(f"Invalid ingest artifact id: {value!r}")
    return text.lower()


def _render_placeholders(text: str, values: Mapping[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        return values.get(key, match.group(0))

    return _PLACEHOLDER_RE.sub(replace, text)


def _first_non_empty_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip().lstrip("#").strip()
        if stripped:
            return stripped
    return ""


def _default_fetcher(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "Hermes-Agent/Patterns"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def _extract_html_title(text: str) -> Optional[str]:
    match = re.search(r"<title[^>]*>(.*?)</title>", text, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    return html.unescape(re.sub(r"\s+", " ", match.group(1)).strip())


def _html_to_text(text: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|li|h[1-6])>", "\n", text)
    text = _HTML_TAG_RE.sub(" ", text)
    text = html.unescape(text)
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+", " ", text)).strip()


def _parse_vtt(text: str, *, timestamps: bool = False) -> str:
    lines: List[str] = []
    seen: set[str] = set()
    current_timestamp: Optional[str] = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line == "WEBVTT" or line.startswith("NOTE"):
            continue
        if _VTT_TIMESTAMP_RE.match(line):
            current_timestamp = line.split("-->", 1)[0].strip()
            continue
        if line.isdigit() or line.startswith(("Kind:", "Language:")):
            continue
        clean = re.sub(r"<[^>]+>", "", line).strip()
        if not clean or clean in seen:
            continue
        seen.add(clean)
        if timestamps and current_timestamp:
            lines.append(f"[{current_timestamp}] {clean}")
        else:
            lines.append(clean)
    return "\n".join(lines).strip()
