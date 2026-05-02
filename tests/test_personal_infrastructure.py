import json
import sys
import zipfile
from pathlib import Path

import pytest

from hermes_personal import (
    FeedbackStore,
    HermesEventLog,
    HermesPaths,
    PackManager,
    PersonalArtifactStore,
)


@pytest.fixture()
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def test_paths_init_creates_personal_infrastructure(hermes_home):
    paths = HermesPaths().ensure()

    assert paths.home == hermes_home
    assert (hermes_home / "system").is_dir()
    assert (hermes_home / "user" / "telos").is_dir()
    assert (hermes_home / "user" / "skill-customizations").is_dir()
    assert (hermes_home / "events").is_dir()
    assert (hermes_home / "learning" / "signals").is_dir()
    assert (hermes_home / "packs" / "installed.json").is_file()


def test_personal_artifact_init_write_read_tree_and_overlay(hermes_home):
    store = PersonalArtifactStore()
    init_result = store.init_defaults()

    assert "user/telos/GOALS.md" in init_result["created"]
    written = store.write_content("user/USER.md", "# User\n\nPrefers terse answers.\n")
    read_back = store.read_content("user/USER.md")
    tree = store.tree()

    assert written["path"] == "user/USER.md"
    assert read_back["content"].startswith("# User")
    assert any(item["path"] == "user/USER.md" for item in tree["files"])

    overlay = hermes_home / "user" / "skill-customizations" / "research" / "PREFERENCES.md"
    overlay.parent.mkdir(parents=True)
    overlay.write_text("Always include source dates.\n", encoding="utf-8")
    content, label = store.apply_skill_overlay("research", "# Research Skill\n")

    assert label == "user/skill-customizations/research/PREFERENCES.md"
    assert "User Skill Customization Override" in content
    assert "Always include source dates." in content


def test_event_append_survives_db_indexing_failure(hermes_home, monkeypatch):
    from hermes_state import SessionDB

    def fail_index(self, event, log_path, byte_offset):
        raise RuntimeError("boom")

    if not hasattr(SessionDB, "index_event"):
        pytest.skip("SessionDB.index_event not available in this version")

    monkeypatch.setattr(SessionDB, "index_event", fail_index)

    event = HermesEventLog().append(
        "session.start",
        source="cli",
        actor="system",
        summary="Started test session",
        session_id="s1",
    )

    log_path = hermes_home / "events" / "events.jsonl"
    rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert event["id"] == rows[0]["id"]
    assert rows[0]["type"] == "session.start"


def test_ratings_and_failure_capsules_create_files(hermes_home):
    feedback = FeedbackStore()
    rating = feedback.rate(session_id="s1", message_id="m1", rating=8, comment="good", source="test")
    failure = feedback.capture_failure(
        session_id="s1",
        message_id="m1",
        reason="missed constraint",
        source="test",
        rating_id=rating["id"],
        rating=2,
        comment="bad",
    )

    ratings_log = hermes_home / "learning" / "signals" / "ratings.jsonl"
    capsule = Path(failure["capsule_path"])

    assert json.loads(ratings_log.read_text(encoding="utf-8").splitlines()[0])["rating"] == 8
    assert (capsule / "CONTEXT.md").is_file()
    assert (capsule / "transcript.jsonl").is_file()
    assert (capsule / "tool-calls.json").is_file()
    assert json.loads((capsule / "metadata.json").read_text(encoding="utf-8"))["status"] == "open"


def test_pack_inspect_dry_run_install_backup_and_traversal_guard(hermes_home, tmp_path, monkeypatch):
    monkeypatch.setattr(PackManager, "_security_scan", lambda self, pack_dir, metadata: {"allowed": True, "findings": []})
    paths = HermesPaths().ensure()
    existing = paths.user_dir / "pack-test" / "file.txt"
    existing.parent.mkdir(parents=True)
    existing.write_text("old\n", encoding="utf-8")

    pack = tmp_path / "SamplePack"
    (pack / "src" / "prefs").mkdir(parents=True)
    (pack / "README.md").write_text("# Sample\n", encoding="utf-8")
    (pack / "INSTALL.md").write_text("# Install\n", encoding="utf-8")
    (pack / "VERIFY.md").write_text("# Verify\n", encoding="utf-8")
    (pack / "pack.yaml").write_text(
        "\n".join(
            [
                "id: sample-pack",
                "name: Sample Pack",
                "version: 1.0.0",
                "type: preferences",
                "description: Test pack",
                "contents:",
                "  - source: prefs",
                "    destination: user/pack-test",
                "post_install_verify:",
                "  - safe: true",
                "    command:",
                f"      - {json.dumps(sys.executable)}",
                "      - -c",
                "      - \"print('verified')\"",
            ]
        ),
        encoding="utf-8",
    )
    (pack / "src" / "prefs" / "file.txt").write_text("new\n", encoding="utf-8")

    manager = PackManager(paths)
    inspected = manager.inspect(str(pack), dry_run=True)
    installed = manager.install(str(pack), backup=True)

    assert inspected["ok"] is True
    assert inspected["conflicts"][0]["destination"] == str(existing)
    assert existing.read_text(encoding="utf-8") == "new\n"
    assert installed["installed"]["backup_path"]
    assert installed["installed"]["verification"]["ok"] is True
    assert installed["installed"]["verification"]["results"][0]["stdout"].strip() == "verified"
    backup_file = Path(installed["installed"]["backup_path"]) / "user" / "pack-test" / "file.txt"
    assert backup_file.read_text(encoding="utf-8") == "old\n"

    bad_pack = tmp_path / "BadPack"
    (bad_pack / "src").mkdir(parents=True)
    for name in ("README.md", "INSTALL.md", "VERIFY.md"):
        (bad_pack / name).write_text("# x\n", encoding="utf-8")
    (bad_pack / "pack.yaml").write_text(
        "id: bad\ncontents:\n  - source: .\n    destination: ../outside\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError):
        manager.inspect(str(bad_pack), dry_run=True)


def test_pack_zip_extraction_rejects_path_traversal(hermes_home, tmp_path):
    manager = PackManager(HermesPaths().ensure())
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("../escape.txt", "no")

    with pytest.raises(ValueError):
        manager._extract_zip_safely(archive, tmp_path / "extract")
