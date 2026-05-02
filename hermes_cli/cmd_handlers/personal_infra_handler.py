"""CLI handlers for Hermes personal infrastructure."""

from __future__ import annotations

import json
import sys


def _print_json(data) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False, default=str))


def cmd_personal(args) -> None:
    from hermes_personal import PersonalArtifactStore

    store = PersonalArtifactStore()
    action = getattr(args, "personal_action", None)
    if action == "init":
        _print_json(store.init_defaults())
    elif action == "doctor":
        _print_json(store.doctor())
    elif action == "sync-obsidian":
        _print_json(store.sync_obsidian(getattr(args, "vault", None)))
    else:
        print("Usage: hermes personal init|doctor|sync-obsidian", file=sys.stderr)


def cmd_context(args) -> None:
    subcommand = getattr(args, "context_action", None)
    if subcommand != "preview":
        print("Usage: hermes context preview [--json] [--tokens]", file=sys.stderr)
        return
    from agent.prompt_builder import build_context_files_prompt

    context = build_context_files_prompt()
    result = {
        "sections": [{"name": "Project Context", "content": context}] if context else [],
        "char_count": len(context),
        "estimated_tokens": max(1, len(context) // 4) if context else 0,
    }
    if getattr(args, "json", False):
        _print_json(result)
    else:
        suffix = f"\n\nEstimated tokens: {result['estimated_tokens']}" if getattr(args, "tokens", False) else ""
        print((context or "No context files found.") + suffix)


def cmd_events(args) -> None:
    from hermes_personal import HermesEventLog

    action = getattr(args, "events_action", None)
    if action != "tail":
        print("Usage: hermes events tail [--type TYPE] [--json]", file=sys.stderr)
        return
    events = HermesEventLog().read(
        event_type=getattr(args, "type", None),
        limit=getattr(args, "limit", 100),
        after=getattr(args, "after", None),
    )
    if getattr(args, "json", False):
        for event in events:
            print(json.dumps(event, ensure_ascii=False, default=str))
    else:
        for event in events:
            print(f"{event.get('timestamp')} {event.get('type')} {event.get('summary')}")


def cmd_feedback(args) -> None:
    from hermes_personal import FeedbackStore

    store = FeedbackStore()
    action = getattr(args, "feedback_action", None)
    if action == "rate":
        _print_json(
            store.rate(
                session_id=args.session,
                message_id=args.message,
                rating=args.rating,
                comment=getattr(args, "comment", "") or "",
                source="cli",
            )
        )
    elif action == "fail":
        _print_json(
            store.capture_failure(
                session_id=args.session,
                message_id=args.message,
                reason=getattr(args, "reason", "") or "",
                source="cli",
            )
        )
    else:
        print("Usage: hermes feedback rate|fail ...", file=sys.stderr)


def cmd_packs(args) -> None:
    from hermes_personal import PackManager

    manager = PackManager()
    action = getattr(args, "packs_action", None)
    if action == "inspect":
        _print_json(manager.inspect(args.path_or_url, dry_run=True))
    elif action == "install":
        _print_json(
            manager.install(
                args.path_or_url,
                dry_run=getattr(args, "dry_run", False),
                backup=getattr(args, "backup", False),
                source="cli",
            )
        )
    elif action == "list":
        _print_json(manager.list_installed())
    elif action == "remove":
        _print_json(manager.remove(args.path_or_url))
    else:
        print("Usage: hermes packs inspect|install|list|remove ...", file=sys.stderr)
