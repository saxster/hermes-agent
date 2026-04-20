# Capability metadata contract

This document is the source of truth for the `capabilities` block returned by
`GET /health` on the gateway's OpenAI-compatible API server
(`gateway/platforms/api_server.py`, default port `8642`). Any change that
alters the wire shape must land in the same PR as an update here.

The contract has three downstream consumers:

| Consumer | Language | Binding |
|---|---|---|
| `hermes-companion` | Swift | `Codable` struct |
| `mission-control` | TypeScript | typed fetcher |
| `hermes-webui` | Python | in-process dict access |

All three treat this payload as the single probe that tells them what this
hermes-agent instance can do.

---

## Schema versioning

```
CAPABILITIES_SCHEMA_VERSION: Final[int] = 2
```

Clients must read `capabilities.schema_version` and decide what to do when
it differs from the version they were built against:

- **Equal** — proceed as normal.
- **Greater** — this hermes-agent is newer than the client. Treat unknown
  keys as forward-compatible; render what you recognise, log a one-line
  warning that some features may be hidden, keep working.
- **Less** — this hermes-agent is older than the client expects. Optional
  keys may be missing; degrade gracefully or show a banner recommending
  an upgrade.

### Bump policy

| Change type | Bump? | Example |
|---|---|---|
| Add a new top-level key | No | `cron.failing_jobs: int` |
| Add a new nested key inside an existing block | No | `tool_gateway.features[*].cost_tier` |
| Rename a key | Yes | `enabled_toolsets` → `toolsets_enabled` |
| Remove a key | Yes | drop `endpoints` |
| Change a key's type | Yes | `cron.available: bool` → `cron.state: str` |

Additive changes do not bump because downstream consumers tolerate unknown
keys. Breaking changes bump and update the history table at the bottom of
this document.

---

## Payload shape

Formal definition lives in `CapabilityPayload` (TypedDict) in
`gateway/platforms/api_server.py`. The wire contract follows.

```jsonc
{
  "schema_version": 2,                 // int — see versioning above
  "configured_model": "nous/hermes-4", // str — what _resolve_gateway_model() returned
  "enabled_toolsets": ["web", "..."],  // sorted list[str] of toolset keys enabled for api_server
  "endpoints": [                       // list[str] — endpoints this build exposes
    "/health",
    "/v1/chat/completions",
    "/v1/responses",
    "/v1/runs",
    "/v1/runs/{run_id}/events",
    "/v1/models",
    "/api/jobs",
    "/api/memory/search",
    "/v1/approvals",
    "/v1/media/synthesize",
    "/v1/media/bundles",
    "/v1/media/bundle/{bundle_id}"
  ],
  "providers": { /* from hermes_cli.status._collect_status_snapshot */ },
  "messaging": {
    "platforms": [ /* telegram/discord/slack/etc readiness rows */ ]
  },
  "gateway": { /* runtime_state, service_running, pid, health_url, … */ },
  "cron": {
    "available": true,   // bool — croniter + cron module importable
    "jobs_total": 17,    // int — every job file under ~/.hermes/cron/
    "jobs_active": 12    // int — jobs with enabled: true
  },
  "tool_gateway": {
    "available": true,        // bool — Nous auth present OR subscription active
    "provider_is_nous": true, // bool — the configured LLM provider is Nous Portal
    "features": [
      {
        "key": "web",
        "label": "Web Search",
        "available": true,
        "active": true,
        "managed_by_nous": true,
        "direct_override": false,
        "toolset_enabled": true,
        "current_provider": "nous"
      }
    ]
  },
  "surfaces": {
    "classic_cli":   { "available": true,  "command": "hermes chat" },
    "tui":           { "available": true,  "command": "npm run start",
                       "path": "/…/ui-tui" },
    "web_dashboard": { "available": true,  "command": "hermes dashboard",
                       "source_path": "/…/web",
                       "dist_path":   "/…/hermes_cli/web_dist",
                       "default_url": "http://127.0.0.1:9119" }
  },
  "hermes_home": "/Users/alice/.hermes",  // str — active HERMES_HOME (respects profiles)
  "errors": []                             // list[str] — advisory, not fatal
}
```

### Semantics of individual keys

| Key | Semantics |
|---|---|
| `schema_version` | See versioning above. Clients MUST read this before interpreting anything else. |
| `configured_model` | Model string the gateway will use by default. Falls back to `"auto"` if resolution fails. |
| `enabled_toolsets` | Sorted list of toolset keys available to `api_server` callers. Empty list means none or resolution failed (check `errors`). |
| `endpoints` | Static list of HTTP endpoints this build serves. Clients can use it to feature-detect newer routes. |
| `providers` | Readiness rows for each LLM provider — whether credentials are configured, whether the provider has been reached successfully. Opaque to downstream: read keys you know, ignore the rest. |
| `messaging` | Messaging-platform readiness (Telegram, Discord, Slack, WhatsApp, Signal, SMS). Shape mirrors `hermes_cli.status`. |
| `gateway` | Runtime state of the gateway process (running/stopped, pid, health URL). Shape mirrors `hermes_cli.status`. |
| `cron` | Cron scheduler health. `available` is false if croniter is missing or the cron module failed to import. |
| `tool_gateway` | Nous-managed tool gateway state. `available` reflects *auth posture*, not per-call availability. Check each feature's `active` field for per-feature routing. |
| `surfaces` | User-facing surfaces and how to launch each one. `available` may be `true` for a path-bound surface even when it is not currently running. |
| `hermes_home` | Absolute path of the active `HERMES_HOME` (respects profile overrides). Empty string on resolution failure. |
| `errors` | One string per probe that raised while collecting. Purely advisory — the `/health` response itself remains `200 OK` unless the transport fails. |

### Error semantics

The gateway NEVER lets a probe failure turn `/health` red:

- Each try/except block in `_collect_capability_metadata` swallows the
  exception and appends a human-readable entry to `errors`.
- Consumers that need to act on partial state should log the errors list
  and render what they can, never block on it.

### Privacy posture

`/health` is **not** authenticated (unlike `/v1/models`, `/v1/chat/completions`, etc.). That matters because the payload exposes:

- Absolute filesystem paths: `hermes_home` (reveals username on multi-user
  systems), `surfaces.tui.path`, `surfaces.web_dashboard.source_path` /
  `dist_path`.
- Runtime state: `gateway.pid`, the enabled toolset list (attack-surface
  hint), and all configured messaging platforms.

For the default single-user LAN-bound deployment this is the same information the user already has on their own machine, so it's acceptable. **If you ever bind the gateway to a public interface** (`0.0.0.0`, a reverse proxy, a tunnel), add authentication in front of `/health` or redact the path-bearing fields. The current shape is deliberately conservative for the common case, not for exposure.

Treat the combination of `hermes_home` + `gateway.pid` + toolset list as enough for an attacker to build a local-file-inclusion attempt against other endpoints. Don't expose `/health` unauthenticated to untrusted networks.

---

## How to verify

Locally:

```bash
hermes gateway run &
sleep 2
curl -s http://127.0.0.1:8642/health | jq .capabilities.schema_version   # → 2
curl -s http://127.0.0.1:8642/health | jq '.capabilities | keys | sort'
```

From a test:

```python
import json
from gateway.platforms.api_server import (
    APIServerAdapter,
    CAPABILITIES_SCHEMA_VERSION,
    CapabilityPayload,
)
from gateway.config import PlatformConfig

adapter = APIServerAdapter(PlatformConfig(enabled=True))
payload: CapabilityPayload = adapter._collect_capability_metadata()
assert payload["schema_version"] == CAPABILITIES_SCHEMA_VERSION
```

---

## History

| Version | Shipped | Breaking change summary |
|---|---|---|
| v1 | pre-2026-04 | Initial shape: `configured_model`, `enabled_toolsets`, `endpoints`. |
| v2 | 2026-04-20 | Added `tool_gateway`, `surfaces`, `cron`. Reorganised `providers` / `messaging` / `gateway` to wrap the output of `hermes_cli.status._collect_status_snapshot`. |
