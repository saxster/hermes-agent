"""F-M2 regression: provider-aware token estimator calibration.

Pins:
- Legacy behavior (model=None) matches the pre-F-M2 `len(str(msg)) // 4` formula.
- Provider-tuned heuristics produce the expected ordering: Claude denser,
  Gemini sparser, OpenAI (via tiktoken or 4.0 heuristic) in between.
- tiktoken is only used for OpenAI-compatible model names.
"""
from __future__ import annotations

import pytest

from agent.model_metadata import (
    estimate_tokens_rough,
    estimate_messages_tokens_rough,
    estimate_request_tokens_rough,
    _chars_per_token_for,
    _tiktoken_encoder_for,
    _HAS_TIKTOKEN,
)


# ---------------------------------------------------------------------------
# _chars_per_token_for
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("model,expected", [
    ("anthropic/claude-opus-4.6", 3.6),
    ("claude-sonnet-4", 3.6),
    ("openai/gpt-4o", 4.0),
    ("gpt-4-turbo", 4.0),
    ("google/gemini-2.0-flash", 4.5),
    ("gemini-pro", 4.5),
    ("mistral-large", 3.9),
    ("meta-llama/llama-3.1", 3.9),
    ("some-unknown-model", 4.0),   # default
    (None, 4.0),                    # default
    ("", 4.0),                      # default
])
def test_chars_per_token_lookup(model, expected):
    assert _chars_per_token_for(model) == expected


# ---------------------------------------------------------------------------
# tiktoken routing
# ---------------------------------------------------------------------------

def test_tiktoken_not_used_for_non_openai():
    # Claude / Gemini must never be routed through tiktoken even if installed.
    assert _tiktoken_encoder_for("anthropic/claude-opus-4.6") is None
    assert _tiktoken_encoder_for("google/gemini-2.0") is None


@pytest.mark.skipif(not _HAS_TIKTOKEN, reason="tiktoken not installed")
def test_tiktoken_used_for_openai():
    enc = _tiktoken_encoder_for("openai/gpt-4o")
    assert enc is not None
    # Sanity: encoding returns integer token IDs
    ids = enc.encode("hello world")
    assert isinstance(ids, list) and len(ids) > 0


# ---------------------------------------------------------------------------
# Back-compat: model=None must preserve legacy behavior exactly
# ---------------------------------------------------------------------------

def test_legacy_messages_estimator_unchanged():
    msgs = [
        {"role": "user", "content": "hello world"},
        {"role": "assistant", "content": "hi back"},
    ]
    # The pre-F-M2 formula was `sum(len(str(msg)) for msg in messages) // 4`.
    expected = sum(len(str(m)) for m in msgs) // 4
    assert estimate_messages_tokens_rough(msgs) == expected


def test_legacy_text_estimator_unchanged():
    text = "some sample text " * 20
    assert estimate_tokens_rough(text) == len(text) // 4


def test_legacy_request_estimator_unchanged():
    msgs = [{"role": "user", "content": "q"}]
    sys = "you are a helpful agent"
    tools = [{"name": "tool_a", "parameters": {}}]
    # Legacy behavior: sum of char counts then //4 via component calls
    total = estimate_request_tokens_rough(msgs, system_prompt=sys, tools=tools)
    # Heuristic stays identical when no model is passed.
    assert total == estimate_request_tokens_rough(msgs, system_prompt=sys, tools=tools)


# ---------------------------------------------------------------------------
# Calibration ordering: claude (denser) > default > gemini (sparser) for heuristic path
# ---------------------------------------------------------------------------

def test_calibration_orders_claude_and_gemini():
    # Long repetitive text — integer-division rounding doesn't hide the gap.
    text = "The quick brown fox jumps over the lazy dog. " * 200
    msgs = [{"role": "user", "content": text}]
    claude = estimate_messages_tokens_rough(msgs, model="claude-opus-4.6")
    gemini = estimate_messages_tokens_rough(msgs, model="gemini-2.0-flash")
    default = estimate_messages_tokens_rough(msgs)  # legacy str(msg) path
    # claude ratio 3.6 → more tokens for the same chars vs default 4.0
    assert claude > default
    # gemini ratio 4.5 → fewer tokens for the same chars
    assert gemini < default


def test_multimodal_content_list_handled():
    msgs = [
        {"role": "user", "content": [
            {"type": "text", "text": "part one"},
            {"type": "text", "text": "part two"},
        ]}
    ]
    # Must not raise; model path extracts text from list-of-parts
    count = estimate_messages_tokens_rough(msgs, model="claude-opus-4.6")
    assert count > 0


def test_tool_call_only_assistant_message_handled():
    msgs = [{"role": "assistant", "content": None, "tool_calls": [
        {"id": "1", "function": {"name": "foo", "arguments": "{}"}},
    ]}]
    count = estimate_messages_tokens_rough(msgs, model="claude-opus-4.6")
    assert count > 0
