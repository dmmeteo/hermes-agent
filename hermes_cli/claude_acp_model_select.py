"""What ``/model`` means for a live Claude Code (ACP) session.

Single owner of the Claude-Code half of ``/model``, called first by every
surface that has one — ``cli.py``'s ``_handle_model_switch`` and
``gateway/slash_commands.py``'s ``_handle_model_command`` — so the terminal,
Discord and Telegram cannot drift apart and no platform grows a Claude-Code
command of its own. Mirrors ``hermes_cli/codex_runtime_switch.py``, the
in-tree precedent for a slash-command behaviour shared across surfaces: this
module decides and phrases, the surface renders.

Why ``/model`` needs a branch here at all
-----------------------------------------

Hermes' ordinary ``/model`` resolves a *route* — provider, base_url,
credentials, api_mode — and rebuilds the agent around it. Claude Code's model
is not a route: it is state inside the ACP session Hermes is already talking
to, chosen from a list only that live session can report (the adapter builds it
from the user's own Claude ``availableModels`` allowlist). Sending a pick down
the route-switch path would resolve some *other* provider's endpoint and retire
the adapter, ACP session, and Claude-side prompt cache — so a pick this session
advertises is applied with one protocol call instead, and everything else about
``/model`` is left exactly as it was.

The fall-through rule is what keeps that narrow. :func:`handle_claude_acp_model_command`
returns ``None`` for anything it does not own — a different provider's agent, a
conversation with no live session yet, ``--provider`` (the way *out* of Claude
Code), and any name this session does not advertise (so ``/model gpt-5.5``
still switches provider from a Claude Code session). ``None`` means the caller
runs its normal path, unmodified.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional, Sequence, Tuple

from agent.claude_code_runtime import (
    ClaudeAcpModelOption,
    ClaudeAcpModelSelectionError,
    claude_acp_display_model,
    claude_acp_model_options,
    claude_acp_select_model,
    claude_acp_session,
    normalize_claude_model_label,
)

logger = logging.getLogger(__name__)

__all__ = [
    "ClaudeAcpModelReply",
    "format_claude_acp_model_list",
    "handle_claude_acp_model_command",
    "resolve_claude_acp_model",
]


@dataclass(frozen=True)
class ClaudeAcpModelReply:
    """A rendered answer for one ``/model`` invocation on a Claude Code session.

    ``text`` is surface-agnostic and already complete; a surface adds only its
    own decoration (the CLI's ``✓``/``✗`` and indent, the gateway's ``❌``).
    """

    text: str
    #: False for a refusal or a failure — the surface marks it as an error.
    success: bool = True
    #: True only when the session's model actually changed.
    switched: bool = False


def _normalized(value: str) -> str:
    """Fold a user-typed name for matching: case, spaces, dots, underscores."""
    folded = (value or "").strip().lower()
    for char in (" ", "_", "."):
        folded = folded.replace(char, "-")
    while "--" in folded:
        folded = folded.replace("--", "-")
    return folded.strip("-")


def _candidate_keys(option: ClaudeAcpModelOption) -> Tuple[str, ...]:
    """Every spelling of *option* a user could reasonably type.

    Blanks are dropped rather than carried: ``name`` is ``""`` for a row the
    agent never named, and an empty key must not become a prefix that matches
    everything.
    """
    keys = (
        _normalized(option.model_id),
        _normalized(option.label),
        _normalized(option.name),
    )
    return tuple(key for key in keys if key)


def resolve_claude_acp_model(
    options: Sequence[ClaudeAcpModelOption], target: str
) -> Tuple[Optional[ClaudeAcpModelOption], Tuple[ClaudeAcpModelOption, ...]]:
    """Match *target* against *options*. Returns ``(match, ambiguous)``.

    Accepts the exact authoritative id, the concise label Hermes displays, the
    agent's own display name, and an unambiguous prefix of any of them — the
    provider-like spellings a user of ``/model sonnet`` already expects. An exact
    match always beats a prefix, so a model whose id prefixes another's is still
    selectable by name.

    ``(None, ())`` means "not one of ours": the caller falls through to the
    cross-provider switch rather than guessing. ``(None, candidates)`` means the
    prefix matched several, which is asked about rather than resolved.
    """
    wanted = _normalized(target)
    if not wanted:
        return None, ()
    for option in options:
        if wanted in _candidate_keys(option):
            return option, ()
    prefixed = [
        option for option in options
        if any(key.startswith(wanted) for key in _candidate_keys(option))
    ]
    if len(prefixed) == 1:
        return prefixed[0], ()
    if prefixed:
        return None, tuple(prefixed)
    return None, ()


def format_claude_acp_model_list(
    options: Sequence[ClaudeAcpModelOption], current_label: str = ""
) -> str:
    """The model list for this session, current one marked.

    ``current_label`` is reported on its own line when the running model is not
    in the advertised list — a real state, not a defensive branch: the SDK's
    refusal fallback can move a session onto a model the user's allowlist
    excludes, and claiming one of the *listed* models is current would then be
    wrong.
    """
    lines = ["Claude Code models (this conversation):"]
    for option in options:
        marker = "→" if option.is_current else " "
        suffix = " (current)" if option.is_current else ""
        name = f" — {option.name}" if option.name and option.name != option.label else ""
        lines.append(f"  {marker} {option.label}{name}{suffix}")
    if current_label and not any(option.is_current for option in options):
        lines.append(
            f"  Currently running {current_label}, which this session does not "
            f"offer as a choice."
        )
    lines.append(
        "Pick one with /model <name> — it applies to this conversation "
        "immediately."
    )
    lines.append("Use /model <name> --provider <slug> to switch away from Claude Code.")
    return "\n".join(lines)


def _current_label(agent: Any, options: Sequence[ClaudeAcpModelOption]) -> str:
    """The label of the model running now, listed or not.

    Falls back to the identity every other surface displays before reading the
    session directly, so the listing, the footer and ``/usage`` cannot disagree
    about what is running — including when the session reports a picker
    selector rather than a model id.
    """
    for option in options:
        if option.is_current:
            return option.label
    displayed = claude_acp_display_model(agent)
    if displayed:
        return displayed
    session = claude_acp_session(agent)
    current = getattr(session, "current_model_id", "") or ""
    return normalize_claude_model_label(current) or current


def _handle_without_a_live_session(
    agent: Any, target: str
) -> Optional[ClaudeAcpModelReply]:
    """What ``/model <name>`` means when no live session can take a pick.

    Packet 2 retires an adapter that crashed, was replaced, or simply went idle;
    the session object survives and still remembers the list it advertised. A
    pick cannot be applied — the model is state inside the adapter — but a name
    that list contains must not be handed to the cross-provider resolver either:
    it would silently move the conversation, and its billing route, to a
    different provider. So a *recognised* name is refused with what to do next,
    and everything else falls through untouched.

    Before the very first turn nothing is remembered, so a Claude Code model is
    indistinguishable from any other name and ``/model`` behaves exactly as it
    did — the only non-guessing option available there. The same is true of a
    *live* session that reports no model state (an adapter build that does not
    advertise one, or an allowlist narrowed to nothing): a connected session
    only ever answers with what it advertises now, which is nothing, so every
    name falls through — the advertised list is the gate on both paths.
    """
    if not target:
        # A remembered list is not what this session offers *now*; presenting it
        # as live would be a lie, and the ordinary picker can still switch
        # provider, which is the useful thing to do with no adapter running.
        return None
    remembered = claude_acp_model_options(agent, require_connected=False)
    match, _ambiguous = resolve_claude_acp_model(remembered, target)
    if match is None:
        return None
    return ClaudeAcpModelReply(
        text=(
            f"{match.label} is a Claude Code model, but this conversation has no "
            f"running Claude Code session to apply it to. Send a message first, "
            f"then pick a model."
        ),
        success=False,
    )


def handle_claude_acp_model_command(
    agent: Any, request: Any
) -> Optional[ClaudeAcpModelReply]:
    """Answer ``/model`` for a live Claude Code session, or ``None``.

    ``request`` is a ``hermes_cli.model_switch.ModelSwitchRequest`` — the same
    parsed object every ``/model`` surface already has, so no surface re-parses
    anything for this path.

    ``None`` means "not mine, run your normal path". See the module docstring
    for the whole fall-through rule; the ordering below is deliberate — the
    advertised list is consulted *before* the flags, so a persistence flag on a
    name this session never offered still falls through to the provider switch
    that can honour it.
    """
    if getattr(request, "explicit_provider", ""):
        # `--provider` is how a user leaves Claude Code. Never intercept it.
        return None
    target = (getattr(request, "target", "") or "").strip()
    options = claude_acp_model_options(agent)
    if not options:
        return _handle_without_a_live_session(agent, target)

    if not target:
        return ClaudeAcpModelReply(
            text=format_claude_acp_model_list(options, _current_label(agent, options)),
        )

    match, ambiguous = resolve_claude_acp_model(options, target)
    if match is None:
        if ambiguous:
            names = ", ".join(option.label for option in ambiguous)
            return ClaudeAcpModelReply(
                text=(
                    f"{target!r} matches several Claude Code models: {names}. "
                    f"Name one of them."
                ),
                success=False,
            )
        # Not a model this session offers — the cross-provider resolver owns it.
        return None

    scope_flag = (
        "--global" if getattr(request, "is_global", False)
        else "--once" if getattr(request, "is_once", False)
        else ""
    )
    if scope_flag:
        # Refused by name rather than ignored: the pick lives inside the Claude
        # Code session, and Hermes has nowhere to persist it as a route (its own
        # `model.default` must stay the provider's pseudo-model, or provider
        # resolution and the gateway's agent-cache drift check both break).
        return ClaudeAcpModelReply(
            text=(
                f"{scope_flag} is not available for Claude Code: the Claude Code "
                f"session owns its model, so a pick applies to this conversation. "
                f"Run /model {match.label} on its own."
            ),
            success=False,
        )

    if match.is_current:
        return ClaudeAcpModelReply(text=f"Claude Code is already running {match.label}.")

    previous = _current_label(agent, options)
    try:
        identity = claude_acp_select_model(agent, match.model_id)
    except ClaudeAcpModelSelectionError as exc:
        return ClaudeAcpModelReply(
            text=f"Claude Code did not switch to {match.label}: {exc}",
            success=False,
        )
    except Exception as exc:
        logger.warning("Claude Code model switch failed unexpectedly: %s", exc)
        return ClaudeAcpModelReply(
            text=f"Claude Code did not switch to {match.label}: {exc}",
            success=False,
        )
    return ClaudeAcpModelReply(
        text=(
            f"Claude Code model switched: {previous} → {identity.label} "
            f"(this conversation)"
        ),
        switched=True,
    )
