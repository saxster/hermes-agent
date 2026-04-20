"""Upgrade Scout service for scheduled upstream Hermes reviews.

The service intentionally separates review/report generation from applying
changes. Reports are deterministic JSON + Markdown artifacts. Approval creates
an apply-run record and, when the worktrees are clean, prepares local branches
and a ledger stub. It never merges or pushes.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

from hermes_constants import get_hermes_home
from utils import atomic_json_write


DEFAULT_UPSTREAM_URL = "https://github.com/nousresearch/hermes-agent"
DEFAULT_UPSTREAM_REF = "main"
UPGRADE_SCOUT_LABEL = "upgrade-scout"
UPGRADE_SCOUT_METADATA_TYPE = "upgrade_scout"


class UpgradeScoutError(RuntimeError):
    """Base error raised for expected Upgrade Scout failures."""


class StaleReportError(UpgradeScoutError):
    """Raised when a report no longer matches the current upstream SHA."""


class DirtyWorktreeError(UpgradeScoutError):
    """Raised when approval is blocked by local dirty worktrees."""


@dataclass(frozen=True)
class GitResult:
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[[Sequence[str], Path], GitResult]


def _default_runner(args: Sequence[str], cwd: Path) -> GitResult:
    proc = subprocess.run(
        list(args),
        cwd=str(cwd),
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )
    return GitResult(proc.returncode, proc.stdout.strip(), proc.stderr.strip())


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_now() -> str:
    return _utc_now().isoformat()


def _read_json(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default
    return default


def _atomic_text_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp_path.write_text(content, encoding="utf-8")
        os.replace(tmp_path, path)
    finally:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass


def _safe_report_id(upstream_sha: str) -> str:
    stamp = _utc_now().strftime("%Y%m%dT%H%M%SZ")
    short = (upstream_sha or "unknown")[:12]
    return f"usr_{stamp}_{short}"


def _short_sha(sha: Optional[str]) -> str:
    return (sha or "unknown")[:12]


class UpgradeScoutService:
    """Generate reports and approval records for upstream behavior-port reviews."""

    def __init__(
        self,
        *,
        hermes_home: Optional[Path] = None,
        agent_repo: Optional[Path] = None,
        workspace_root: Optional[Path] = None,
        upstream_url: str = DEFAULT_UPSTREAM_URL,
        upstream_ref: str = DEFAULT_UPSTREAM_REF,
        runner: Runner = _default_runner,
    ) -> None:
        self.hermes_home = Path(hermes_home) if hermes_home else get_hermes_home()
        self.agent_repo = Path(agent_repo) if agent_repo else Path(__file__).resolve().parents[1]
        self.workspace_root = Path(workspace_root) if workspace_root else self._infer_workspace_root(self.agent_repo)
        self.upstream_url = upstream_url
        self.upstream_ref = upstream_ref
        self.runner = runner
        self.base_dir = self.hermes_home / "upgrade-scout"
        self.reports_dir = self.base_dir / "reports"
        self.apply_runs_dir = self.base_dir / "apply-runs"
        self.state_path = self.base_dir / "state.json"

    @staticmethod
    def _infer_workspace_root(agent_repo: Path) -> Path:
        parent = agent_repo.parent
        if (parent / "hermes-companion").exists() or (parent / "start.sh").exists():
            return parent
        return agent_repo

    def status(self) -> Dict[str, Any]:
        state = self._load_state()
        reports = self.list_reports()
        latest_report = reports[0] if reports else None
        latest_apply_run = None
        last_apply_run_id = state.get("last_apply_run_id")
        if last_apply_run_id:
            latest_apply_run = self.get_apply_run(last_apply_run_id)

        cron_job = self._find_cron_job()
        pending = [
            report for report in reports
            if report.get("approval_status", "pending") == "pending"
        ]
        return {
            "enabled": bool(cron_job and cron_job.get("enabled", True)),
            "cron_job_id": cron_job.get("id") if cron_job else None,
            "schedule": cron_job.get("schedule_display") if cron_job else None,
            "last_reviewed_sha": state.get("last_reviewed_sha"),
            "last_approved_sha": state.get("last_approved_sha"),
            "latest_upstream_sha": state.get("latest_upstream_sha"),
            "latest_report": latest_report,
            "pending_approval_count": len(pending),
            "latest_apply_run": latest_apply_run,
            "state_dir": str(self.base_dir),
        }

    def run_report(self) -> Dict[str, Any]:
        self._ensure_dirs()
        state = self._load_state()
        upstream = self._resolve_upstream()
        upstream_sha = upstream["sha"]
        base_sha = state.get("last_reviewed_sha") or self._local_head(self.agent_repo)
        changed_paths = self._changed_paths(base_sha, upstream_sha)
        commits = self._commit_summaries(base_sha, upstream_sha)
        dirty = self._dirty_worktrees()
        classifications = self._classify_paths(changed_paths)
        recommendations = self._recommend(classifications, dirty)
        risk = self._risk_level(recommendations, dirty)
        report_id = _safe_report_id(upstream_sha)
        proposed_branch = f"codex/upgrade-scout-{_utc_now().strftime('%Y%m%d')}-{_short_sha(upstream_sha)}"
        proposed_agent_branch = f"codex/hermes-agent-upgrade-scout-{_utc_now().strftime('%Y%m%d')}-{_short_sha(upstream_sha)}"

        summary = self._summary(recommendations, risk, dirty)
        report = {
            "id": report_id,
            "created_at": _iso_now(),
            "upstream": upstream,
            "base_sha": base_sha,
            "last_reviewed_sha": state.get("last_reviewed_sha"),
            "strategy": "behavior-port",
            "approval_boundary": "merge_ready",
            "changed_paths": changed_paths,
            "commits": commits,
            "classifications": classifications,
            "recommendations": recommendations,
            "risk": risk,
            "summary": summary,
            "local_conflicts": dirty,
            "proposed_branch": proposed_branch,
            "proposed_agent_branch": proposed_agent_branch,
            "approval_status": "pending",
            "expected_tests": [
                ".venv/bin/python -m pytest tests/gateway tests/tools -q",
                "swift test --filter UpgradeScout",
                "swift test --filter DashboardViewRoutingTests",
            ],
        }
        brief = self._render_brief(report)

        report_dir = self.reports_dir / report_id
        report_dir.mkdir(parents=True, exist_ok=True)
        atomic_json_write(report_dir / "report.json", report)
        _atomic_text_write(report_dir / "brief.md", brief)

        state.update(
            {
                "last_report_id": report_id,
                "latest_upstream_sha": upstream_sha,
                "updated_at": _iso_now(),
            }
        )
        atomic_json_write(self.state_path, state)
        return {"report": report, "brief": brief}

    def list_reports(self) -> List[Dict[str, Any]]:
        self._ensure_dirs()
        reports: List[Dict[str, Any]] = []
        for report_json in self.reports_dir.glob("*/report.json"):
            report = _read_json(report_json, {})
            if not isinstance(report, dict) or not report.get("id"):
                continue
            reports.append(
                {
                    "id": report["id"],
                    "created_at": report.get("created_at"),
                    "upstream_sha": report.get("upstream", {}).get("sha"),
                    "risk": report.get("risk", "unknown"),
                    "summary": report.get("summary", ""),
                    "approval_status": report.get("approval_status", "pending"),
                    "proposed_branch": report.get("proposed_branch"),
                    "recommendation_counts": {
                        key: len(value)
                        for key, value in (report.get("recommendations") or {}).items()
                        if isinstance(value, list)
                    },
                }
            )
        return sorted(reports, key=lambda item: item.get("created_at") or "", reverse=True)

    def get_report(self, report_id: str) -> Dict[str, Any]:
        report_dir = self._report_dir(report_id)
        report = _read_json(report_dir / "report.json", None)
        if not isinstance(report, dict):
            raise FileNotFoundError(f"Report not found: {report_id}")
        brief_path = report_dir / "brief.md"
        brief = brief_path.read_text(encoding="utf-8") if brief_path.exists() else self._render_brief(report)
        return {"report": report, "brief": brief}

    def mark_report_reviewed(self, report_id: str) -> Dict[str, Any]:
        detail = self.get_report(report_id)
        report = detail["report"]
        report["approval_status"] = "reviewed"
        report["reviewed_at"] = _iso_now()
        atomic_json_write(self._report_dir(report_id) / "report.json", report)
        state = self._load_state()
        state["last_reviewed_sha"] = report.get("upstream", {}).get("sha")
        state["updated_at"] = _iso_now()
        atomic_json_write(self.state_path, state)
        return report

    def approve_report(self, report_id: str) -> Dict[str, Any]:
        detail = self.get_report(report_id)
        report = detail["report"]
        current_upstream = self._resolve_upstream()["sha"]
        report_sha = report.get("upstream", {}).get("sha")
        if current_upstream != report_sha:
            raise StaleReportError(
                f"Report {report_id} is stale: report={report_sha}, current={current_upstream}"
            )

        dirty = self._dirty_worktrees()
        if dirty["root"]["dirty"] or dirty["agent"]["dirty"]:
            raise DirtyWorktreeError("Approval requires clean root and hermes-agent worktrees")

        run_id = f"apply_{_utc_now().strftime('%Y%m%dT%H%M%SZ')}_{_short_sha(report_sha)}"
        run_dir = self.apply_runs_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        status = {
            "id": run_id,
            "report_id": report_id,
            "status": "running",
            "started_at": _iso_now(),
            "upstream_sha": report_sha,
            "root_branch": report.get("proposed_branch"),
            "agent_branch": report.get("proposed_agent_branch"),
            "commits": [],
            "validation": [],
            "blockers": [],
            "merge_ready": False,
        }
        atomic_json_write(run_dir / "status.json", status)

        try:
            self._prepare_branches(status)
            ledger_path = self._write_ledger_stub(report, status)
            self._commit_apply_artifacts(report, status, ledger_path)
            validation = self._run_apply_validation()
            status["validation"] = validation
            status["status"] = "merge_ready" if all(item["passed"] for item in validation) else "validation_failed"
            status["merge_ready"] = status["status"] == "merge_ready"
            status["finished_at"] = _iso_now()
            _atomic_text_write(run_dir / "validation.md", self._render_validation(status))
        except Exception as exc:
            status["status"] = "blocked"
            status["blockers"].append(str(exc))
            status["finished_at"] = _iso_now()
            _atomic_text_write(run_dir / "validation.md", self._render_validation(status))
        finally:
            atomic_json_write(run_dir / "status.json", status)

        report["approval_status"] = "approved" if status["merge_ready"] else "approval_blocked"
        report["apply_run_id"] = run_id
        atomic_json_write(self._report_dir(report_id) / "report.json", report)
        state = self._load_state()
        state["last_approved_sha"] = report_sha if status["merge_ready"] else state.get("last_approved_sha")
        state["last_apply_run_id"] = run_id
        state["updated_at"] = _iso_now()
        atomic_json_write(self.state_path, state)
        return status

    def get_apply_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        path = self.apply_runs_dir / run_id / "status.json"
        data = _read_json(path, None)
        return data if isinstance(data, dict) else None

    def ensure_cron_job(self) -> Dict[str, Any]:
        from cron.jobs import create_job, list_jobs, update_job

        existing = self._find_cron_job()
        if existing:
            if not existing.get("enabled", True):
                updated = update_job(existing["id"], {"enabled": True, "state": "scheduled"})
                return updated or existing
            return existing

        return create_job(
            prompt="Generate the Hermes upstream upgrade recommendation report.",
            schedule="every 14d",
            name="Hermes Upgrade Scout",
            deliver="local",
            labels=[UPGRADE_SCOUT_LABEL, "system"],
            metadata={
                "type": UPGRADE_SCOUT_METADATA_TYPE,
                "cadence": "fortnightly",
                "approval_boundary": "merge_ready",
            },
        )

    def pause_cron_job(self) -> Optional[Dict[str, Any]]:
        from cron.jobs import pause_job

        existing = self._find_cron_job()
        if not existing:
            return None
        return pause_job(existing["id"], reason="paused from Companion Upgrade Scout")

    def _ensure_dirs(self) -> None:
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        self.apply_runs_dir.mkdir(parents=True, exist_ok=True)

    def _load_state(self) -> Dict[str, Any]:
        self._ensure_dirs()
        data = _read_json(self.state_path, {})
        return data if isinstance(data, dict) else {}

    def _report_dir(self, report_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", report_id):
            raise ValueError("Invalid report id")
        return self.reports_dir / report_id

    def _git(self, args: Sequence[str], cwd: Optional[Path] = None) -> GitResult:
        return self.runner(["git", *args], cwd or self.agent_repo)

    def _resolve_upstream(self) -> Dict[str, Any]:
        remote_ref = f"refs/heads/{self.upstream_ref}"
        result = self._git(["ls-remote", self.upstream_url, remote_ref], self.agent_repo)
        sha = ""
        source = "ls-remote"
        error = None
        if result.returncode == 0 and result.stdout:
            sha = result.stdout.split()[0]
        else:
            local = self._git(["rev-parse", f"upstream/{self.upstream_ref}"], self.agent_repo)
            if local.returncode == 0 and local.stdout:
                sha = local.stdout.splitlines()[0]
                source = "local-upstream"
            else:
                head = self._local_head(self.agent_repo)
                sha = head or "unknown"
                source = "local-head-fallback"
                error = result.stderr or local.stderr or "Unable to resolve upstream"
        return {
            "url": self.upstream_url,
            "ref": self.upstream_ref,
            "sha": sha,
            "source": source,
            "error": error,
        }

    def _local_head(self, repo: Path) -> Optional[str]:
        result = self._git(["rev-parse", "HEAD"], repo)
        return result.stdout.splitlines()[0] if result.returncode == 0 and result.stdout else None

    def _changed_paths(self, base_sha: Optional[str], upstream_sha: str) -> List[str]:
        if not base_sha or not upstream_sha or upstream_sha == "unknown":
            return []
        result = self._git(["diff", "--name-only", f"{base_sha}..{upstream_sha}"], self.agent_repo)
        if result.returncode != 0:
            return []
        return sorted({line.strip() for line in result.stdout.splitlines() if line.strip()})

    def _commit_summaries(self, base_sha: Optional[str], upstream_sha: str) -> List[Dict[str, str]]:
        if not base_sha or not upstream_sha or upstream_sha == "unknown":
            return []
        result = self._git(["log", "--format=%H%x09%s", f"{base_sha}..{upstream_sha}", "--max-count=80"], self.agent_repo)
        if result.returncode != 0:
            return []
        commits = []
        for line in result.stdout.splitlines():
            if "\t" not in line:
                continue
            sha, subject = line.split("\t", 1)
            commits.append({"sha": sha, "subject": subject})
        return commits

    def _dirty_worktrees(self) -> Dict[str, Dict[str, Any]]:
        def status(repo: Path) -> Dict[str, Any]:
            result = self._git(["status", "--short"], repo)
            lines = [line for line in result.stdout.splitlines() if line.strip()]
            return {
                "path": str(repo),
                "dirty": bool(lines),
                "status": lines[:200],
                "error": result.stderr if result.returncode != 0 else None,
            }

        return {"root": status(self.workspace_root), "agent": status(self.agent_repo)}

    def _classify_paths(self, paths: Iterable[str]) -> Dict[str, List[str]]:
        buckets: Dict[str, List[str]] = {
            "gateway_api": [],
            "cron": [],
            "tools_security": [],
            "providers_models": [],
            "messaging": [],
            "tui_web": [],
            "companion_impact": [],
            "docs_tests": [],
            "other": [],
        }
        for path in paths:
            lower = path.lower()
            if path.startswith("gateway/platforms/") and "api_server" not in path:
                buckets["messaging"].append(path)
            elif path.startswith("gateway/") or "api_server" in path:
                buckets["gateway_api"].append(path)
            elif path.startswith("cron/") or "cron" in lower:
                buckets["cron"].append(path)
            elif path.startswith("tools/") or any(token in lower for token in ("security", "auth", "oauth", "approval", "guard", "ssrf", "sandbox", "credential")):
                buckets["tools_security"].append(path)
            elif any(token in lower for token in ("model", "provider", "runtime_provider", "bedrock", "gemini", "nvidia", "minimax", "kimi")):
                buckets["providers_models"].append(path)
            elif path.startswith("ui-tui/") or path.startswith("web/") or "web_server" in path:
                buckets["tui_web"].append(path)
            elif path.startswith("hermes-companion/") or "companion" in lower:
                buckets["companion_impact"].append(path)
            elif path.startswith("tests/") or path.startswith("docs/") or path.endswith(".md"):
                buckets["docs_tests"].append(path)
            else:
                buckets["other"].append(path)
        return buckets

    def _recommend(self, classifications: Dict[str, List[str]], dirty: Dict[str, Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
        recommendations: Dict[str, List[Dict[str, Any]]] = {
            "must_port": [],
            "should_port": [],
            "defer": [],
            "reject": [],
            "already_present": [],
            "needs_user_decision": [],
        }
        if dirty["root"]["dirty"] or dirty["agent"]["dirty"]:
            recommendations["needs_user_decision"].append(
                {
                    "cluster": "local-worktree",
                    "reason": "Local worktrees are dirty; approval is blocked until changes are committed or stashed.",
                    "paths": [],
                }
            )
        for cluster, paths in classifications.items():
            if not paths:
                continue
            item = {"cluster": cluster, "paths": paths[:80], "count": len(paths)}
            if cluster in {"tools_security", "gateway_api"}:
                item["reason"] = "Security or public API behavior can affect Companion and unattended cron execution."
                recommendations["must_port"].append(item)
            elif cluster in {"cron", "providers_models", "companion_impact"}:
                item["reason"] = "High-value behavior that directly improves reliability or local UX."
                recommendations["should_port"].append(item)
            elif cluster in {"messaging", "tui_web"}:
                item["reason"] = "Useful surface area, but should remain opt-in and disabled by default."
                recommendations["defer"].append(item)
            elif cluster == "docs_tests":
                item["reason"] = "Reference material and regression coverage; adopt only with touched behavior."
                recommendations["defer"].append(item)
            else:
                item["reason"] = "Unclassified upstream drift requires human review before porting."
                recommendations["needs_user_decision"].append(item)
        if not any(classifications.values()):
            recommendations["already_present"].append(
                {
                    "cluster": "upstream-delta",
                    "reason": "No changed paths could be resolved from the current local refs.",
                    "paths": [],
                }
            )
        return recommendations

    def _risk_level(self, recommendations: Dict[str, List[Dict[str, Any]]], dirty: Dict[str, Dict[str, Any]]) -> str:
        if dirty["root"]["dirty"] or dirty["agent"]["dirty"]:
            return "blocked"
        if len(recommendations["must_port"]) >= 3 or recommendations["needs_user_decision"]:
            return "high"
        if recommendations["must_port"] or recommendations["should_port"]:
            return "medium"
        return "low"

    def _summary(self, recommendations: Dict[str, List[Dict[str, Any]]], risk: str, dirty: Dict[str, Dict[str, Any]]) -> str:
        if risk == "blocked":
            return "Upgrade Scout found upstream drift, but approval is blocked by local dirty worktrees."
        must = len(recommendations["must_port"])
        should = len(recommendations["should_port"])
        if must or should:
            return f"Review recommends {must} must-port and {should} should-port cluster(s)."
        return "No urgent behavior-port work was identified."

    def _render_brief(self, report: Dict[str, Any]) -> str:
        upstream = report.get("upstream", {})
        lines = [
            "# Hermes Upgrade Scout Decision Brief",
            "",
            f"**Report ID:** {report.get('id')}",
            f"**Created:** {report.get('created_at')}",
            f"**Upstream:** `{upstream.get('url')}@{upstream.get('ref')}`",
            f"**Upstream SHA:** `{upstream.get('sha')}`",
            f"**Risk:** {report.get('risk')}",
            f"**Recommendation:** {report.get('summary')}",
            "",
            "## Recommendation Buckets",
        ]
        for bucket in ("must_port", "should_port", "defer", "reject", "already_present", "needs_user_decision"):
            items = report.get("recommendations", {}).get(bucket, [])
            lines.append(f"### {bucket.replace('_', ' ').title()} ({len(items)})")
            if not items:
                lines.append("- None")
                continue
            for item in items:
                lines.append(f"- **{item.get('cluster')}**: {item.get('reason')} ({item.get('count', 0)} paths)")
        lines.extend(["", "## Proposed Apply", f"- Root branch: `{report.get('proposed_branch')}`", f"- Agent branch: `{report.get('proposed_agent_branch')}`"])
        dirty = report.get("local_conflicts", {})
        lines.extend(["", "## Local Worktree Gate"])
        for key in ("root", "agent"):
            info = dirty.get(key, {})
            state = "dirty" if info.get("dirty") else "clean"
            lines.append(f"- {key}: {state} - `{info.get('path')}`")
        lines.extend(["", "## Expected Tests"])
        for test in report.get("expected_tests", []):
            lines.append(f"- `{test}`")
        return "\n".join(lines) + "\n"

    def _prepare_branches(self, status: Dict[str, Any]) -> None:
        root_branch = status.get("root_branch")
        agent_branch = status.get("agent_branch")
        if root_branch:
            result = self._git(["switch", "-c", root_branch], self.workspace_root)
            if result.returncode != 0:
                if "already exists" in result.stderr:
                    result = self._git(["switch", root_branch], self.workspace_root)
                if result.returncode != 0:
                    raise UpgradeScoutError(result.stderr or f"Failed to create branch {root_branch}")
        if agent_branch:
            result = self._git(["switch", "-c", agent_branch], self.agent_repo)
            if result.returncode != 0:
                if "already exists" in result.stderr:
                    result = self._git(["switch", agent_branch], self.agent_repo)
                if result.returncode != 0:
                    raise UpgradeScoutError(result.stderr or f"Failed to create branch {agent_branch}")

    def _write_ledger_stub(self, report: Dict[str, Any], status: Dict[str, Any]) -> Path:
        docs_dir = self.workspace_root / "docs"
        docs_dir.mkdir(parents=True, exist_ok=True)
        path = docs_dir / f"UpgradeScout-{report['id']}.md"
        content = [
            "# Upgrade Scout Apply Ledger",
            "",
            f"- Report ID: `{report['id']}`",
            f"- Upstream SHA: `{report.get('upstream', {}).get('sha')}`",
            f"- Root branch: `{status.get('root_branch')}`",
            f"- Agent branch: `{status.get('agent_branch')}`",
            f"- Strategy: `{report.get('strategy')}`",
            "",
            "## Selected Recommendation Buckets",
            "",
        ]
        for bucket, items in (report.get("recommendations") or {}).items():
            content.append(f"- {bucket}: {len(items)}")
        content.extend(["", "## Validation", "", "- Pending"])
        _atomic_text_write(path, "\n".join(content) + "\n")
        return path

    def _commit_apply_artifacts(self, report: Dict[str, Any], status: Dict[str, Any], ledger_path: Path) -> None:
        rel_ledger = ledger_path.relative_to(self.workspace_root)
        add = self._git(["add", str(rel_ledger)], self.workspace_root)
        if add.returncode != 0:
            raise UpgradeScoutError(add.stderr or "Failed to stage Upgrade Scout ledger")
        commit = self._git(
            ["commit", "-m", f"chore: prepare upgrade scout {report['id']}"],
            self.workspace_root,
        )
        if commit.returncode != 0:
            raise UpgradeScoutError(commit.stderr or "Failed to commit Upgrade Scout ledger")
        commit_sha = self._git(["rev-parse", "HEAD"], self.workspace_root)
        if commit_sha.returncode == 0 and commit_sha.stdout:
            status["commits"].append({"repo": "root", "sha": commit_sha.stdout.splitlines()[0], "type": "ledger"})

    def _run_apply_validation(self) -> List[Dict[str, Any]]:
        checks = [
            ("agent-status", ["git", "status", "--short"], self.agent_repo),
            ("root-status", ["git", "status", "--short"], self.workspace_root),
        ]
        results = []
        for name, cmd, cwd in checks:
            result = self.runner(cmd, cwd)
            results.append(
                {
                    "name": name,
                    "command": " ".join(cmd),
                    "passed": result.returncode == 0 and not result.stdout.strip(),
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                }
            )
        return results

    def _render_validation(self, status: Dict[str, Any]) -> str:
        lines = ["# Upgrade Scout Apply Validation", "", f"Status: {status.get('status')}", ""]
        for item in status.get("validation", []):
            mark = "PASS" if item.get("passed") else "FAIL"
            lines.append(f"- {mark}: `{item.get('command')}`")
        for blocker in status.get("blockers", []):
            lines.append(f"- BLOCKED: {blocker}")
        return "\n".join(lines) + "\n"

    def _find_cron_job(self) -> Optional[Dict[str, Any]]:
        try:
            from cron.jobs import list_jobs

            for job in list_jobs(include_disabled=True):
                metadata = job.get("metadata") or {}
                labels = job.get("labels") or []
                if metadata.get("type") == UPGRADE_SCOUT_METADATA_TYPE or UPGRADE_SCOUT_LABEL in labels:
                    return job
        except Exception:
            return None
        return None
