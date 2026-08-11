"""What model an agent is running, answered once for every gateway surface.

Two questions that look like one and are not, which is why this is two
functions rather than one with a flag:

* **What do we show a human?** ``display_model_for`` — the footer, ``/usage``,
  ``/context``, ``/agents``.
* **What do we look this agent's model up *by*?** ``metadata_model_for`` — the
  context-window table, pricing, a persisted usage row. Those are keyed on ids;
  a display label resolves in none of them.

For every provider but one the two answers are the same string, ``agent.model``,
and this module is the identity function. The exception is the Claude Code ACP
runtime, where ``agent.model`` is a static pseudo-model (``"claude-code"``) that
deliberately never changes — the gateway's post-turn agent cache evicts a
session whose ``model`` no longer matches config, so making the live model
visible by writing it there would rebuild the agent, and with it the prompt
cache, every time the user switched models. The live identity is captured
separately by ``agent/claude_code_runtime.py`` and read back through here.

The module exists because the messaging gateway needed the same answer the four
other surfaces already had, and the only implementation lived on two
staticmethods of a slash-command mixin, inside a module that imports the turn
runner. A footer cannot import that; a drifted second copy is what produced
``claude-code · 0%`` on Telegram while ``/usage`` in the same session said
``opus-5``. One function per question, imported by both.

Both are guarded: a display, or a metadata lookup key, must never be the thing
that breaks the command asking for it.
"""

from __future__ import annotations

from typing import Any


def display_model_for(agent: Any) -> str:
    """What to show as *agent*'s model on a user-facing surface.

    Answers ``agent.model`` for every agent that is not a Claude Code ACP one
    with a captured identity — including a Claude Code agent before its first
    completed turn, which genuinely has nothing better to report yet.
    """
    try:
        from agent.claude_code_runtime import claude_acp_display_model

        label = claude_acp_display_model(agent)
    except Exception:
        label = None
    return label or str(getattr(agent, "model", "") or "")


def metadata_model_for(agent: Any) -> str:
    """The model string to look *agent*'s model metadata up with.

    The display label is the wrong key for that — the metadata tables are keyed
    on ids — so this asks the runtime for its accounting id, which falls back to
    the pseudo-model when ACP reported only a picker selector (``"default"``,
    ``"sonnet"``): a row keyed on a selector is keyed on a fiction. See
    ``claude_acp_usage_model``.
    """
    try:
        from agent.claude_code_runtime import (
            claude_acp_model_identity,
            claude_acp_usage_model,
        )

        if claude_acp_model_identity(agent) is not None:
            return claude_acp_usage_model(agent)
    except Exception:
        pass
    return str(getattr(agent, "model", "") or "")
