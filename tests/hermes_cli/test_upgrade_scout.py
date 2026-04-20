from pathlib import Path

import pytest

from hermes_cli.upgrade_scout import (
    DirtyWorktreeError,
    GitResult,
    StaleReportError,
    UpgradeScoutService,
)


def _runner(*, upstream_sha="b" * 40, dirty_root=False, dirty_agent=False, stale_sha=None):
    def run(args, cwd: Path):
        joined = " ".join(args)
        if args[:3] == ["git", "ls-remote", "https://github.com/nousresearch/hermes-agent"]:
            sha = stale_sha or upstream_sha
            return GitResult(0, f"{sha}\trefs/heads/main", "")
        if args[:3] == ["git", "rev-parse", "HEAD"]:
            return GitResult(0, "a" * 40, "")
        if args[:2] == ["git", "diff"]:
            return GitResult(
                0,
                "\n".join(
                    [
                        "gateway/platforms/api_server.py",
                        "cron/scheduler.py",
                        "tools/url_safety.py",
                        "web/src/App.tsx",
                    ]
                ),
                "",
            )
        if args[:2] == ["git", "log"]:
            return GitResult(0, f"{upstream_sha}\tGateway and cron fixes", "")
        if args[:3] == ["git", "status", "--short"]:
            cwd_text = str(cwd)
            if cwd_text.endswith("workspace") and dirty_root:
                return GitResult(0, " M start.sh", "")
            if cwd_text.endswith("agent") and dirty_agent:
                return GitResult(0, " M gateway/run.py", "")
            return GitResult(0, "", "")
        if args[:3] == ["git", "switch", "-c"]:
            return GitResult(0, "", "")
        if args[:2] == ["git", "add"]:
            return GitResult(0, "", "")
        if args[:2] == ["git", "commit"]:
            return GitResult(0, "[branch abc123] commit", "")
        if joined == "git rev-parse HEAD":
            return GitResult(0, "c" * 40, "")
        return GitResult(0, "", "")

    return run


def _service(tmp_path, runner):
    workspace = tmp_path / "workspace"
    agent = tmp_path / "agent"
    workspace.mkdir()
    agent.mkdir()
    return UpgradeScoutService(
        hermes_home=tmp_path / "home",
        workspace_root=workspace,
        agent_repo=agent,
        runner=runner,
    )


def test_run_report_writes_decision_brief_and_classifies_paths(tmp_path):
    service = _service(tmp_path, _runner())

    result = service.run_report()

    report = result["report"]
    assert report["risk"] == "medium"
    assert report["recommendations"]["must_port"]
    assert report["classifications"]["gateway_api"] == ["gateway/platforms/api_server.py"]
    assert "Hermes Upgrade Scout Decision Brief" in result["brief"]
    assert (tmp_path / "home" / "upgrade-scout" / "reports" / report["id"] / "report.json").exists()


def test_dirty_worktree_blocks_approval(tmp_path):
    service = _service(tmp_path, _runner(dirty_root=True))
    report = service.run_report()["report"]

    with pytest.raises(DirtyWorktreeError):
        service.approve_report(report["id"])


def test_stale_report_blocks_approval(tmp_path):
    upstream = "b" * 40
    current = {"sha": upstream}

    def run(args, cwd: Path):
        stale = current["sha"]
        return _runner(upstream_sha=upstream, stale_sha=stale)(args, cwd)

    service = _service(tmp_path, run)
    report = service.run_report()["report"]
    current["sha"] = "d" * 40

    with pytest.raises(StaleReportError):
        service.approve_report(report["id"])


def test_clean_approval_creates_apply_run_and_ledger_commit(tmp_path):
    service = _service(tmp_path, _runner())
    report = service.run_report()["report"]

    run = service.approve_report(report["id"])

    assert run["status"] == "merge_ready"
    assert run["merge_ready"] is True
    assert run["commits"][0]["repo"] == "root"
    assert (tmp_path / "workspace" / "docs" / f"UpgradeScout-{report['id']}.md").exists()
