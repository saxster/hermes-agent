"""CLI handlers for Hermes patterns, contexts, strategies, and ingestion."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Iterable


def _print_json(data: Any) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False, default=str))


def _variables(pairs: Iterable[str] | None) -> Dict[str, str]:
    values: Dict[str, str] = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise SystemExit(f"Invalid --variable {pair!r}; expected key=value")
        key, value = pair.split("=", 1)
        key = key.strip()
        if not key:
            raise SystemExit("Invalid --variable; key cannot be empty")
        values[key] = value
    return values


def _stdin_or_arg(value: str | None) -> str:
    if value is not None:
        return value
    if not sys.stdin.isatty():
        return sys.stdin.read()
    return ""


def _notify(title: str, body: str) -> None:
    if sys.platform != "darwin":
        return
    script = f'display notification "{body.replace(chr(34), chr(39))}" with title "{title.replace(chr(34), chr(39))}"'
    subprocess.run(["osascript", "-e", script], check=False, capture_output=True, text=True)


def cmd_patterns(args) -> None:
    from hermes_cli.config import load_config
    from patterns import PatternLibrary

    library = PatternLibrary()
    action = getattr(args, "patterns_action", None)
    if action == "list":
        _print_json([pattern.to_summary() for pattern in library.list_patterns()])
        return
    if action == "favorites":
        config = load_config()
        patterns_config = config.get("patterns", {}) or {}
        _print_json({"favorite_patterns": list(patterns_config.get("favorite_patterns", []) or [])})
        return
    if action == "show":
        _print_json(library.get_pattern(args.name).to_dict())
        return
    if action == "import-fabric":
        _print_json(library.import_fabric_starter_pack(force=getattr(args, "force", False)))
        return
    if action == "run":
        config = load_config()
        input_text = _stdin_or_arg(getattr(args, "input", None))
        rendered = library.render_pattern(
            args.name,
            input_text,
            context=getattr(args, "context", None) or (config.get("patterns", {}) or {}).get("default_context"),
            strategy=getattr(args, "strategy", None),
            variables=_variables(getattr(args, "variable", None)),
            input_ref=getattr(args, "input_ref", None),
            config=config,
            explicit_model=getattr(args, "model", None),
            active_model=config.get("model", ""),
        )
        if getattr(args, "render_only", False):
            _print_json(rendered.to_dict())
            return

        from agent.core import AIAgent
        from gateway.run import _resolve_runtime_agent_kwargs

        runtime = _resolve_runtime_agent_kwargs()
        agent = AIAgent(
            model=rendered.resolved_model or config.get("model", ""),
            api_key=runtime.get("api_key"),
            base_url=runtime.get("base_url"),
            provider=runtime.get("provider"),
            api_mode=runtime.get("api_mode"),
            command=runtime.get("command"),
            args=runtime.get("args"),
            credential_pool=runtime.get("credential_pool"),
            quiet_mode=not getattr(args, "stream", False),
            ephemeral_system_prompt=rendered.system_prompt,
            platform="cli",
        )
        result = agent.run_conversation(user_message=rendered.input_text)
        final = result.get("final_response") or result.get("error") or ""
        output = getattr(args, "output", None)
        if output:
            Path(output).expanduser().write_text(final, encoding="utf-8")
        else:
            print(final)
        if getattr(args, "notify", False):
            _notify("Hermes pattern complete", f"{args.name} finished")
        return
    print("Usage: hermes patterns list|show|run|import-fabric|favorites ...", file=sys.stderr)


def cmd_contexts(args) -> None:
    from patterns import PatternLibrary

    library = PatternLibrary()
    action = getattr(args, "contexts_action", None)
    if action == "list":
        _print_json(library.list_contexts())
    elif action == "show":
        print(library.get_context(args.name))
    elif action == "create":
        content = _stdin_or_arg(getattr(args, "content", None))
        print(library.create_context(args.name, content).name)
    elif action == "delete":
        library.delete_context(args.name)
        print(f"Deleted context: {args.name}")
    else:
        print("Usage: hermes contexts list|show|create|delete ...", file=sys.stderr)


def cmd_strategies(args) -> None:
    from patterns import PatternLibrary

    library = PatternLibrary()
    action = getattr(args, "strategies_action", None)
    if action == "list":
        _print_json([strategy.to_dict() for strategy in library.list_strategies()])
    elif action == "show":
        _print_json(library.get_strategy(args.name).to_dict())
    else:
        print("Usage: hermes strategies list|show ...", file=sys.stderr)


def cmd_ingest(args) -> None:
    from patterns import PatternLibrary

    library = PatternLibrary()
    action = getattr(args, "ingest_action", None)
    if action == "file":
        artifact = library.ingest_file(Path(args.path))
    elif action == "url":
        artifact = library.ingest_url(args.url)
    elif action == "youtube":
        artifact = library.ingest_youtube(args.url, timestamps=getattr(args, "timestamps", False))
    elif action == "clipboard":
        artifact = library.ingest_clipboard()
    elif action == "text":
        artifact = library.ingest_text(_stdin_or_arg(getattr(args, "content", None)))
    else:
        print("Usage: hermes ingest youtube|url|file|clipboard|text ...", file=sys.stderr)
        return
    _print_json(artifact.to_dict())
    if getattr(args, "notify", False):
        _notify("Hermes ingest complete", artifact.title or artifact.id[:12])
