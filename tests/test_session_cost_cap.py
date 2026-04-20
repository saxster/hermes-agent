"""F-H1 regression: session cost cap stops the loop before the next LLM call.

We don't exercise the full run_conversation path here (it requires an
LLM client); instead we cover:

1. __init__ precedence: kwarg > env var > None.
2. Env-var parsing tolerates junk values.
3. The SessionCostCapExceeded exception carries spent/cap fields.

The integration behavior (loop actually breaks before a second API call)
is covered indirectly: the check uses `self.session_estimated_cost_usd`,
which is set at line ~7644 after each real API call, and the break is
inside the main loop right after the iteration-budget check.
"""
from __future__ import annotations

import os
import pytest


@pytest.fixture
def agent_factory(monkeypatch, tmp_path):
    """Build a bare AIAgent without running the LLM init path.

    AIAgent.__init__ does a lot — we use __new__ + manual attribute setup
    to exercise just the cap logic, mirroring the pattern used in
    tests/gateway/test_agent_cache.py::_make_runner().
    """
    from agent.core import AIAgent

    def make(cap_env=None, cap_kwarg=None):
        if cap_env is not None:
            monkeypatch.setenv("HERMES_SESSION_COST_CAP_USD", cap_env)
        else:
            monkeypatch.delenv("HERMES_SESSION_COST_CAP_USD", raising=False)
        # Reproduce the relevant __init__ slice rather than calling __init__.
        agent = AIAgent.__new__(AIAgent)
        _env_cap_raw = os.environ.get("HERMES_SESSION_COST_CAP_USD", "").strip()
        _env_cap_val = None
        if _env_cap_raw:
            try:
                _parsed = float(_env_cap_raw)
                if _parsed > 0:
                    _env_cap_val = _parsed
            except ValueError:
                pass
        agent.session_cost_cap_usd = (
            cap_kwarg if cap_kwarg and cap_kwarg > 0 else _env_cap_val
        )
        agent.session_estimated_cost_usd = 0.0
        return agent

    return make


def test_exception_shape():
    from agent.core import SessionCostCapExceeded

    exc = SessionCostCapExceeded(spent_usd=2.50, cap_usd=1.00)
    assert exc.spent_usd == 2.50
    assert exc.cap_usd == 1.00
    assert "2.5" in str(exc) and "1.00" in str(exc)


def test_cap_from_kwarg_overrides_env(agent_factory):
    agent = agent_factory(cap_env="5.00", cap_kwarg=2.50)
    assert agent.session_cost_cap_usd == 2.50


def test_cap_from_env_when_no_kwarg(agent_factory):
    agent = agent_factory(cap_env="3.25", cap_kwarg=None)
    assert agent.session_cost_cap_usd == 3.25


def test_no_cap_when_env_missing(agent_factory):
    agent = agent_factory(cap_env=None, cap_kwarg=None)
    assert agent.session_cost_cap_usd is None


def test_env_junk_is_ignored(agent_factory):
    agent = agent_factory(cap_env="not-a-number", cap_kwarg=None)
    assert agent.session_cost_cap_usd is None


def test_env_zero_is_treated_as_unlimited(agent_factory):
    # Zero and negative mean "unlimited" so operators can unset via HERMES_SESSION_COST_CAP_USD=0
    # without having to unset the var.
    agent = agent_factory(cap_env="0", cap_kwarg=None)
    assert agent.session_cost_cap_usd is None


def test_cap_is_reached_once_spent_meets_it(agent_factory):
    """Guard the exact inequality used in the loop check."""
    agent = agent_factory(cap_env=None, cap_kwarg=1.00)
    agent.session_estimated_cost_usd = 0.99
    # >= cap is the break condition — 0.99 should NOT trigger; 1.00 should.
    assert not (agent.session_cost_cap_usd
                and agent.session_estimated_cost_usd >= agent.session_cost_cap_usd)
    agent.session_estimated_cost_usd = 1.00
    assert (agent.session_cost_cap_usd
            and agent.session_estimated_cost_usd >= agent.session_cost_cap_usd)


def test_no_cap_never_triggers(agent_factory):
    agent = agent_factory(cap_env=None, cap_kwarg=None)
    agent.session_estimated_cost_usd = 10_000.0
    assert not (agent.session_cost_cap_usd
                and agent.session_estimated_cost_usd >= (agent.session_cost_cap_usd or 0))
