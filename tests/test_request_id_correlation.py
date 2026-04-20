"""F-M4 regression: request-id contextvar + logging filter.

Pins:
- bind_request_id yields the id and resets on exit.
- Explicit id passes through; None generates a fresh 12-hex-char id.
- RequestIdFilter stamps `request_id` onto LogRecords (defaults to "-"
  outside any context so fixed-width log formats stay aligned).
- The format string in hermes_logging includes %(request_id)s.
"""
from __future__ import annotations

import logging
import re

import pytest

from hermes_logging import (
    bind_request_id,
    generate_request_id,
    get_request_id,
    RequestIdFilter,
    _LOG_FORMAT,
    _LOG_FORMAT_VERBOSE,
)


def test_default_id_is_empty():
    assert get_request_id() == ""


def test_bind_generates_id_when_none():
    with bind_request_id() as rid:
        assert re.fullmatch(r"[0-9a-f]{12}", rid)
        assert get_request_id() == rid
    assert get_request_id() == ""


def test_bind_accepts_explicit_id():
    with bind_request_id("my-custom-id") as rid:
        assert rid == "my-custom-id"
        assert get_request_id() == "my-custom-id"


def test_bind_handles_empty_string_as_none():
    with bind_request_id("") as rid:
        # Empty string should be treated as "generate fresh"
        assert rid != ""
        assert len(rid) == 12


def test_filter_stamps_active_id():
    rec = logging.LogRecord(
        name="t", level=logging.INFO, pathname="x", lineno=1,
        msg="m", args=(), exc_info=None,
    )
    with bind_request_id("abc-123"):
        assert RequestIdFilter().filter(rec) is True
        assert rec.request_id == "abc-123"


def test_filter_stamps_dash_outside_context():
    rec = logging.LogRecord(
        name="t", level=logging.INFO, pathname="x", lineno=1,
        msg="m", args=(), exc_info=None,
    )
    # No bind_request_id in effect
    assert RequestIdFilter().filter(rec) is True
    assert rec.request_id == "-"


def test_generate_returns_unique_ids():
    seen = {generate_request_id() for _ in range(50)}
    assert len(seen) == 50


def test_log_format_includes_request_id():
    assert "%(request_id)s" in _LOG_FORMAT
    assert "%(request_id)s" in _LOG_FORMAT_VERBOSE


def test_filter_survives_nested_binds():
    """Inner bind must shadow outer, then restore it on exit."""
    with bind_request_id("outer") as outer_rid:
        assert get_request_id() == "outer"
        with bind_request_id("inner") as inner_rid:
            assert get_request_id() == "inner"
        assert get_request_id() == "outer"
    assert get_request_id() == ""
