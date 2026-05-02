from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from patterns import HERMES_PATTERN_MODEL_PREFIX, PatternError, PatternLibrary, pattern_name_from_model_id


def write_pattern(home: Path, name: str, system: str, metadata: dict | None = None) -> None:
    pattern_dir = home / "patterns" / name
    pattern_dir.mkdir(parents=True)
    (pattern_dir / "system.md").write_text(system, encoding="utf-8")
    (pattern_dir / "metadata.yaml").write_text(
        yaml.safe_dump({"name": name, **(metadata or {})}, sort_keys=False),
        encoding="utf-8",
    )


def test_pattern_render_replaces_input_and_variables(tmp_path):
    write_pattern(
        tmp_path,
        "brief",
        "Analyze {{topic}}:\n\n{{input}}",
        {"description": "Brief", "tags": ["analysis"]},
    )
    rendered = PatternLibrary(tmp_path).render_pattern(
        "brief",
        "raw material",
        variables={"topic": "claims"},
    )

    assert "Analyze claims" in rendered.system_prompt
    assert "raw material" in rendered.system_prompt
    assert "{{topic}}" not in rendered.system_prompt


def test_pattern_without_input_placeholder_appends_input(tmp_path):
    write_pattern(tmp_path, "append", "Summarize this.")

    rendered = PatternLibrary(tmp_path).render_pattern("append", "hello")

    assert rendered.system_prompt == "Summarize this.\n\nhello"


def test_context_strategy_pattern_order(tmp_path):
    write_pattern(tmp_path, "ordered", "PATTERN {{input}}", {"default_strategy": "careful"})
    (tmp_path / "contexts").mkdir()
    (tmp_path / "contexts" / "research.md").write_text("CONTEXT", encoding="utf-8")
    (tmp_path / "strategies").mkdir()
    (tmp_path / "strategies" / "careful.json").write_text(
        json.dumps({"name": "careful", "description": "", "prompt": "STRATEGY"}),
        encoding="utf-8",
    )

    rendered = PatternLibrary(tmp_path).render_pattern("ordered", "INPUT", context="research")

    assert rendered.system_prompt.split("\n\n") == ["CONTEXT", "STRATEGY", "PATTERN INPUT"]


def test_model_resolution_precedence(tmp_path):
    write_pattern(tmp_path, "modelled", "Use input {{input}}", {"default_model": "metadata-model"})
    library = PatternLibrary(tmp_path)
    pattern = library.get_pattern("modelled")

    assert library.resolve_model(pattern, explicit_model="cli-model", active_model="active") == "cli-model"
    assert library.resolve_model(
        pattern,
        config={"patterns": {"model_overrides": {"modelled": "override-model"}}},
        active_model="active",
    ) == "override-model"
    assert library.resolve_model(pattern, active_model="active") == "metadata-model"


def test_ingest_artifact_and_input_ref(tmp_path):
    source = tmp_path / "source.md"
    source.write_text("artifact content", encoding="utf-8")
    library = PatternLibrary(tmp_path)
    artifact = library.ingest_file(source)
    write_pattern(tmp_path, "summarize", "SUM {{input}}")

    rendered = library.render_pattern("summarize", input_ref=artifact.id)

    assert artifact.id
    assert "artifact content" in rendered.system_prompt


def test_text_ingest_artifact(tmp_path):
    library = PatternLibrary(tmp_path)

    artifact = library.ingest_text("shared text", title="Share")

    assert artifact.source_type == "text"
    assert artifact.title == "Share"
    assert "shared text" in Path(artifact.content_path).read_text(encoding="utf-8")


def test_fabric_import_uses_mocked_fetcher_and_preserves_existing(tmp_path):
    library = PatternLibrary(tmp_path)
    write_pattern(tmp_path, "summarize", "LOCAL")

    def fetcher(url: str) -> bytes:
        return f"Fetched from {url}".encode()

    result = library.import_fabric_starter_pack(fetcher=fetcher, names=["summarize", "extract_wisdom"])

    assert result["skipped"] == ["summarize"]
    assert result["imported"] == ["extract_wisdom"]
    imported = library.get_pattern("extract_wisdom")
    assert imported.metadata.source == "fabric"
    assert imported.metadata.license == "MIT"


def test_virtual_pattern_model_parser():
    assert pattern_name_from_model_id(f"{HERMES_PATTERN_MODEL_PREFIX}summarize") == "summarize"
    assert pattern_name_from_model_id("hermes-agent") is None


def test_invalid_names_are_rejected(tmp_path):
    with pytest.raises(PatternError):
        PatternLibrary(tmp_path).get_pattern("../secret")
