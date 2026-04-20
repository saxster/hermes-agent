"""Auxiliary client package (F-C3).

Holds the provider router used by side tasks (context compression,
title generation, vision, session search, web extraction, analyst
council, etc.). Historically a single ~1,930-line module at
``agent/auxiliary_client.py``; F-C3 splits it into smaller siblings
under this package.

  base.py        — shared machinery (provider resolution chain,
                   cache, call_llm, extract_content, timeouts).
  vision.py      — vision-specific auto-selection + client resolution
                   (extracted in step 2).
  providers.py   — Codex and Anthropic adapter classes that make
                   non-OpenAI providers look like chat.completions
                   (extracted in step 3).

All public symbols are re-exported here. Existing callers that do
``from agent.auxiliary_client import <name>`` keep working through
the ``agent/auxiliary_client.py`` shim, which re-exports this
package.
"""

from __future__ import annotations

from agent.auxiliary.base import *  # noqa: F401,F403
from agent.auxiliary.providers import *  # noqa: F401,F403
from agent.auxiliary.vision import *  # noqa: F401,F403
