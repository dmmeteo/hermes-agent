"""The permission posture of a Claude Code ACP session — one config decision.

Packets 1–3 hardcoded ``session/set_mode`` → ``default`` on every connect. That
was the right default and the wrong *policy*: it also silently overwrote the
mode the profile owner had already chosen in the adapter's own
``~/.claude/settings.json`` (``permissions.defaultMode``), which the adapter
honours at session creation. Hermes never wrote the model at session start —
only read it — so a user who set both saw one setting take effect and the other
vanish. This module is that asymmetry's fix: the posture becomes
``model.claude_code_acp.permission_mode``, read from ``config.yaml`` alone.

Three rules shape everything below, and they are the same ones
``agent/claude_acp_workspace.py`` states for workspace roots:

* **Explicit only.** One config key, never an env var, never an inference from
  what the adapter happens to advertise. A self-approving mode is reachable
  only by a profile owner naming it (AGENTS.md §Adding Configuration).
* **Every rejection narrows.** An absent key, an unreadable config, a value
  that is not a mode, a mode the live adapter never advertised, and an adapter
  that reports no mode state at all all resolve to ``default`` — today's
  behaviour, where Hermes sees every permission request. Nothing here can
  *widen* a session's posture by accident.
* **Not deciding is a decision you have to make.** ``inherit`` is the one value
  that sends no ``session/set_mode`` at all, leaving whatever the adapter
  derived from its own settings in force. It is spelled out rather than
  reachable by omission, because "Hermes did not negotiate a mode" and "Hermes
  could not tell what the adapter supports" must never be the same state.

**What the mode does and does not change.** It changes *how many*
``session/request_permission`` calls exist — in a self-approving mode the
adapter's own classifier answers most of them and Hermes never sees them. It
changes nothing about what happens to a request that does arrive: that still
goes through ``ClaudeAcpSession._decide_permission`` →
``tools/approval.py::request_delegated_runtime_approval``, with the same
unconditional floor and the same deny-by-default. Widening the posture is
therefore a decision about how much of the session Hermes can see, and the
audit WARNING ``ClaudeAcpSession._connect()`` logs says so on every connect.
"""

from __future__ import annotations

import logging
from typing import Any, Optional, Sequence

logger = logging.getLogger(__name__)

#: Where the profile owner names the posture. Per-profile by construction —
#: ``config.yaml`` is per-``HERMES_HOME``.
CONFIG_KEYS = ("model", "claude_code_acp", "permission_mode")

#: What Hermes negotiates when nothing else can be justified. Byte-identical to
#: the pre-Packet-5 behaviour, and the answer to every rejection below.
DEFAULT_PERMISSION_MODE = "default"

#: Modes in which the adapter asks the client about tool use, so Hermes' own
#: approval flow is the gate the user actually sees.
HERMES_GATED_MODES = frozenset({"default", "plan"})

#: Modes in which the adapter decides for itself — its classifier (``auto``),
#: its in-workspace edit policy (``acceptEdits``), or no asking at all. Hermes'
#: approval gate still handles anything it *is* asked about, but in these modes
#: most actions never produce a request, so the unconditional floor
#: (hardline blocklist, sudo-stdin guard, ``approvals.deny``) never measures
#: them either. Enumerated opt-in only.
SELF_APPROVING_MODES = frozenset(
    {"auto", "acceptEdits", "dontAsk", "bypassPermissions"}
)

#: Not a mode: the instruction to send no ``session/set_mode`` at all.
INHERIT = "inherit"

#: Every value the key accepts other than :data:`INHERIT`.
KNOWN_MODES = HERMES_GATED_MODES | SELF_APPROVING_MODES


def configured_permission_mode() -> str:
    """The raw ``permission_mode`` from config.yaml, or ``default``.

    Never raises and never validates: an absent key, an unreadable config and a
    non-string value all read as :data:`DEFAULT_PERMISSION_MODE`, which is the
    narrow answer. Whether a *named* value is usable against the session that is
    actually running is :func:`resolve_permission_mode`'s question, and it is
    deliberately asked in one place rather than two.
    """
    try:
        from hermes_cli.config import load_config

        node: Any = load_config()
        for key in CONFIG_KEYS:
            if not isinstance(node, dict):
                return DEFAULT_PERMISSION_MODE
            node = node.get(key)
    except Exception:
        logger.debug(
            "reading the Claude Code ACP permission mode failed", exc_info=True
        )
        return DEFAULT_PERMISSION_MODE
    if not isinstance(node, str) or not node.strip():
        if node is not None:
            logger.warning(
                "Ignoring model.claude_code_acp.permission_mode %r: expected one "
                "of %s or %r; negotiating %r",
                node, sorted(KNOWN_MODES), INHERIT, DEFAULT_PERMISSION_MODE,
            )
        return DEFAULT_PERMISSION_MODE
    return node.strip()


def resolve_permission_mode(
    configured: Optional[str], advertised: Optional[Sequence[str]] = None
) -> Optional[str]:
    """The mode to negotiate, or ``None`` to negotiate none.

    *configured* is what the profile asked for (:func:`configured_permission_mode`);
    *advertised* is what the **live** session said it supports —
    ``ClaudeAcpClient.available_mode_ids``, where ``None`` means the response
    carried no ``modes`` block at all and ``[]`` means it advertised none. Those
    are two different facts and neither is evidence that an opt-in mode is
    available, so both narrow.

    Returns ``None`` only for :data:`INHERIT`. Every other unusable answer
    resolves to :data:`DEFAULT_PERMISSION_MODE` and logs one WARNING naming the
    value and the fallback — silently downgrading a posture the profile owner
    wrote down is how a config setting becomes folklore.

    ``default`` itself is sent even when the session advertised no modes: it is
    the mode Hermes has always negotiated and the narrowest one available, and
    withholding it would leave the adapter in its own self-approving ``auto``,
    which is the opposite of narrowing.
    """
    mode = (configured or "").strip() or DEFAULT_PERMISSION_MODE
    if mode == INHERIT:
        return None
    if mode not in KNOWN_MODES:
        logger.warning(
            "Ignoring model.claude_code_acp.permission_mode %r: not one of %s or "
            "%r; negotiating %r instead",
            mode, sorted(KNOWN_MODES), INHERIT, DEFAULT_PERMISSION_MODE,
        )
        return DEFAULT_PERMISSION_MODE
    if mode == DEFAULT_PERMISSION_MODE:
        return DEFAULT_PERMISSION_MODE
    if not advertised:
        # No mode state on the wire (an older adapter build) or a session that
        # advertises none. Either way this session never said it could honour
        # the configured mode, and asking anyway would be a containment claim
        # nothing backs.
        logger.warning(
            "Claude Code ACP: this session advertises no permission modes, so "
            "the configured mode %r cannot be negotiated; using %r",
            mode, DEFAULT_PERMISSION_MODE,
        )
        return DEFAULT_PERMISSION_MODE
    if mode not in advertised:
        logger.warning(
            "Claude Code ACP: this session does not advertise the configured "
            "permission mode %r (it offers %s); using %r",
            mode, sorted(advertised), DEFAULT_PERMISSION_MODE,
        )
        return DEFAULT_PERMISSION_MODE
    return mode


def describe_posture(negotiated: Optional[str], configured: str) -> str:
    """One audit line for what this connect leaves in force.

    Returned rather than logged so the caller decides the level and the session
    it names. ``negotiated is None`` is the ``inherit`` case, where the sentence
    has to be about the adapter's own settings and not about a mode Hermes
    chose.
    """
    if negotiated is None:
        return (
            "Claude Code ACP: permission_mode is %r, so Hermes negotiated no "
            "mode — whatever the adapter derived from its own settings "
            "(~/.claude/settings.json permissions.defaultMode) is in force, and "
            "Hermes' approval gate will only see the requests that mode "
            "produces" % (configured,)
        )
    return (
        "Claude Code ACP: negotiated the self-approving permission mode %r from "
        "model.claude_code_acp.permission_mode — the adapter approves most tool "
        "use itself, so Hermes' approval gate and its unconditional floor will "
        "see few or no requests for this session" % (negotiated,)
    )
