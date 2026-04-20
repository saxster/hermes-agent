"""Shared helpers for tool backend selection."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict

from hermes_constants import get_hermes_home
from utils import env_var_enabled

_DEFAULT_BROWSER_PROVIDER = "local"
_DEFAULT_MODAL_MODE = "auto"
_VALID_MODAL_MODES = {"auto", "direct", "managed"}


def managed_nous_tools_enabled() -> bool:
    """Return True when Nous Tool Gateway can be attempted.

    v0.10 moved this from a hidden feature flag toward subscription-backed
    availability. Keep the legacy flag as an override for existing local
    workflows, but also enable managed routes when a Hermes-owned Nous token is
    present. A dedicated disable flag gives users a predictable opt-out.
    """
    if env_var_enabled("HERMES_DISABLE_NOUS_MANAGED_TOOLS"):
        return False
    if env_var_enabled("HERMES_ENABLE_NOUS_MANAGED_TOOLS"):
        return True
    if os.getenv("TOOL_GATEWAY_USER_TOKEN", "").strip():
        return True

    try:
        auth_path = get_hermes_home() / "auth.json"
        if not auth_path.is_file():
            return False
        data = json.loads(auth_path.read_text())
        providers = data.get("providers", {})
        nous = providers.get("nous", {}) if isinstance(providers, dict) else {}
        return bool(
            isinstance(nous, dict)
            and (
                str(nous.get("access_token") or "").strip()
                or str(nous.get("refresh_token") or "").strip()
            )
        )
    except Exception:
        return False


def normalize_browser_cloud_provider(value: object | None) -> str:
    """Return a normalized browser provider key."""
    provider = str(value or _DEFAULT_BROWSER_PROVIDER).strip().lower()
    return provider or _DEFAULT_BROWSER_PROVIDER


def coerce_modal_mode(value: object | None) -> str:
    """Return the requested modal mode when valid, else the default."""
    mode = str(value or _DEFAULT_MODAL_MODE).strip().lower()
    if mode in _VALID_MODAL_MODES:
        return mode
    return _DEFAULT_MODAL_MODE


def normalize_modal_mode(value: object | None) -> str:
    """Return a normalized modal execution mode."""
    return coerce_modal_mode(value)


def has_direct_modal_credentials() -> bool:
    """Return True when direct Modal credentials/config are available."""
    return bool(
        (os.getenv("MODAL_TOKEN_ID") and os.getenv("MODAL_TOKEN_SECRET"))
        or (Path.home() / ".modal.toml").exists()
    )


def resolve_modal_backend_state(
    modal_mode: object | None,
    *,
    has_direct: bool,
    managed_ready: bool,
) -> Dict[str, Any]:
    """Resolve direct vs managed Modal backend selection.

    Semantics:
    - ``direct`` means direct-only
    - ``managed`` means managed-only
    - ``auto`` prefers managed when available, then falls back to direct
    """
    requested_mode = coerce_modal_mode(modal_mode)
    normalized_mode = normalize_modal_mode(modal_mode)
    managed_mode_blocked = (
        requested_mode == "managed" and not managed_nous_tools_enabled()
    )

    if normalized_mode == "managed":
        selected_backend = "managed" if managed_nous_tools_enabled() and managed_ready else None
    elif normalized_mode == "direct":
        selected_backend = "direct" if has_direct else None
    else:
        selected_backend = "managed" if managed_nous_tools_enabled() and managed_ready else "direct" if has_direct else None

    return {
        "requested_mode": requested_mode,
        "mode": normalized_mode,
        "has_direct": has_direct,
        "managed_ready": managed_ready,
        "managed_mode_blocked": managed_mode_blocked,
        "selected_backend": selected_backend,
    }


def resolve_openai_audio_api_key() -> str:
    """Prefer the voice-tools key, but fall back to the normal OpenAI key."""
    return (
        os.getenv("VOICE_TOOLS_OPENAI_KEY", "")
        or os.getenv("OPENAI_API_KEY", "")
    ).strip()
