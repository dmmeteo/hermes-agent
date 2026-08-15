"""The Hermes operating context a Claude Code ACP session is created with.

Packets 0–5 left one gap, and it is the largest one in the runtime: a Claude
Code ACP turn ran with **zero** Hermes-authored context. ``agent/turn_context.py``
builds (and persists) a full system prompt for every turn *including* this
runtime's, and ``agent/conversation_loop.py`` then hands the turn to
``agent/claude_code_runtime.py``, which sends exactly one thing on the wire: the
user's message. The model answering did not know it was Hermes, did not know who
the user is, did not know it was talking on Discord rather than a terminal, and
had never seen the profile's ``SOUL.md``.

This module is the policy and the composition for closing that gap. It is
modelled on ``agent/claude_acp_permissions.py`` — one config key, an explicit
allow-list, every rejection resolving to something usable, and the decision
stated rather than implied.

**The seam is the adapter's, not one we invented.** The pinned adapter clamps
``params._meta.systemPrompt`` back to ``{type: "preset", preset: "claude_code"}``
and honours only ``append``, so the object form *adds* to Claude Code's own
instructions. The **string form replaces them**, which is how the pre-ACP
direct-CLI experiment failed, and :func:`system_prompt_meta` is the one place
that shape is built so a regression there is a single test away.

Four rules shape everything below:

* **Curated, not verbatim.** The default is *not* ``agent._cached_system_prompt``.
  That prompt carries ``MEMORY_GUIDANCE`` / ``SKILLS_GUIDANCE`` /
  ``STEER_CHANNEL_NOTE`` and the skills index, all of which instruct the model
  to call Hermes tools that **do not exist in this session** — the same
  tool-vocabulary hazard ``agent/claude_acp_skills.py`` measured for bridged
  skills, an order of magnitude harder. It also carries a ``Model:``/``Provider:``
  line that is actively false here, and the AGENTS.md/CLAUDE.md context files
  the adapter already loads through its own ``settingSources``. ``mode: full``
  is the opt-in for anyone who wants exactly that text and accepts the risk.
* **Sent once per session, never per turn.** ``ClaudeAcpSession._connect()`` is
  the only caller, so the context rides in the session-creation call and
  ``session/prompt`` still carries only the user's turn. That is structural, not
  conventional: turns 2..N return early from ``_connect()``.
* **Bounded and deterministic.** ``max_chars`` is enforced by dropping *whole*
  sections in a fixed order, never ``identity``, never the preamble. Composing
  twice yields identical bytes, which is what makes the digest meaningful.
* **Fail-open.** A composition fault costs one WARNING and a session with no
  ``_meta`` — never the user's turn. An identity blob must never cost a message.

**Privacy.** ``SOUL.md``, ``USER.md`` and the memory snapshot are Hermes-owned
personal context, and after this packet they are sent to the adapter and thence
to the model endpoint. That endpoint already receives every message of the
conversation — there is no new network peer and no new credential — but the
*class* of content is new, so ``mode: soul`` (identity only) and ``mode: off``
(nothing at all) exist and are one word each. Nothing here is scrubbed on the
way out: this is user-authored configuration, the same class as the user's own
message text, and redacting it would silently mangle a ``SOUL.md``. **Logs and
displays carry the digest and the char count only — never the text.**
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: Where the profile owner configures this. Per-profile by construction —
#: ``config.yaml`` is per-``HERMES_HOME``.
CONFIG_KEYS = ("model", "claude_code_acp", "system_prompt")

#: Nothing is sent; byte-identical to the pre-packet behaviour. The rollback.
MODE_OFF = "off"
#: Identity only — the narrowest value that still makes the agent Hermes.
MODE_SOUL = "soul"
#: The curated Hermes operating context. The default.
MODE_HERMES = "hermes"
#: ``agent._cached_system_prompt`` verbatim, framed. Opt-in, and it accepts the
#: tool-vocabulary hazard the default set exists to avoid.
MODE_FULL = "full"

#: What every rejection below resolves to, and what an absent key means.
DEFAULT_MODE = MODE_HERMES

MODES: Tuple[str, ...] = (MODE_OFF, MODE_SOUL, MODE_HERMES, MODE_FULL)

#: The section vocabulary, in composition order. Also the config allow-list.
SECTIONS: Tuple[str, ...] = (
    "identity",
    "environment",
    "profile",
    "platform",
    "caller_instructions",
    "memory",
    "user_profile",
    "external_memory",
    "skills_index",
    "context_files",
    "coding",
    "tool_guidance",
)

#: What each mode composes when ``sections`` is left empty. ``full`` ignores the
#: section vocabulary by definition, and ``off`` composes nothing at all.
MODE_SECTIONS: Dict[str, Tuple[str, ...]] = {
    MODE_OFF: (),
    MODE_SOUL: ("identity",),
    MODE_HERMES: (
        "identity",
        "environment",
        "profile",
        "platform",
        "caller_instructions",
        "memory",
        "user_profile",
        "external_memory",
    ),
    MODE_FULL: (),
}

#: The order sections are dropped in when the composition exceeds ``max_chars``:
#: least identity-bearing first. ``identity`` is deliberately absent — it is the
#: one block whose loss would make the whole append pointless, and it is already
#: capped by ``load_soul_md``'s context-length-scaled truncation.
DROP_ORDER: Tuple[str, ...] = (
    "tool_guidance",
    "coding",
    "context_files",
    "skills_index",
    "external_memory",
    "memory",
    "user_profile",
    "caller_instructions",
    "platform",
    "profile",
    "environment",
)

#: Roughly 1.5–2.5k tokens for a default composition on a real profile.
DEFAULT_MAX_CHARS = 24000
MIN_MAX_CHARS = 1000
MAX_MAX_CHARS = 120000

#: Frozen text, versioned with this module so the digest means something. It
#: states the precedence explicitly, because the ``claude_code`` preset says
#: "you are Claude Code" and a ``SOUL.md`` says "you are Hermes", and an
#: unresolved conflict there is the one failure mode this whole packet can
#: introduce.
PREAMBLE = """[Hermes operating context]
This Claude Code session is the execution engine for Hermes, a personal agent the
user talks to through Hermes' own surfaces. Claude Code's instructions, tools and
safety rules continue to apply as written. The blocks below add who you are to
this user, who this user is, and where the conversation is happening.
Where they disagree about HOW TO USE TOOLS, Claude Code's instructions win — the
tools here are Claude Code's, not Hermes'. Where they disagree about IDENTITY,
TONE, or the user's standing preferences, the blocks below win.
Hermes owns the transcript and delivers your reply on the surface named below;
the text you produce is the message the user receives."""

#: Appended when sections had to be dropped for length. Named rather than
#: silent: a truncation the model cannot see is a truncation the user cannot
#: debug.
_OMITTED_TEMPLATE = (
    "[Hermes operating context: the following blocks were omitted for length "
    "and are not part of this session's context: %s]"
)


@dataclass(frozen=True)
class SystemPromptConfig:
    """The resolved policy. Always usable — :func:`resolve_system_prompt_config`
    never raises and never returns something the composer has to re-validate."""

    mode: str = DEFAULT_MODE
    sections: Tuple[str, ...] = MODE_SECTIONS[DEFAULT_MODE]
    max_chars: int = DEFAULT_MAX_CHARS


@dataclass(frozen=True)
class ComposedSystemPrompt:
    """What one ACP session will be created with, and how to describe it.

    ``text`` is never logged. ``digest`` and ``chars`` are what every log line,
    binding field and accessor carries instead.
    """

    text: str
    mode: str

    @property
    def digest(self) -> str:
        return prompt_digest(self.text)

    @property
    def chars(self) -> int:
        return len(self.text)

    def meta(self) -> Optional[Dict[str, Any]]:
        return system_prompt_meta(self.text)


def prompt_digest(text: str) -> str:
    """The sha256 of the exact bytes sent. The only thing we ever log."""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def system_prompt_meta(text: Optional[str]) -> Optional[Dict[str, Any]]:
    """The ``_meta.systemPrompt`` value, or ``None`` to send no ``_meta``.

    SECURITY-adjacent correctness: this MUST be the object form. The adapter
    accepts a bare string too, and a string **replaces** the ``claude_code``
    preset — deleting Claude Code's own tool instructions and safety rules for
    the whole session. The object form is clamped by the adapter back to
    ``{type: "preset", preset: "claude_code"}``, so ``append`` can only ever
    add. Building it in exactly one place is what keeps that one test away.
    """
    if not text or not text.strip():
        return None
    return {"type": "preset", "preset": "claude_code", "append": text}


def vocabulary_note() -> str:
    """The Hermes→Claude Code tool mapping, rendered deterministically.

    Rendered from ``agent/claude_acp_skills.py``'s ``HERMES_TO_CLAUDE_TOOLS``
    rather than a second hand-written table — the skills bridge already owns
    that translation and a duplicate would drift. Sorted, so the bytes (and the
    digest with them) do not move when the table gains an entry.
    """
    from agent.claude_acp_skills import HERMES_TO_CLAUDE_TOOLS

    lines = [
        "[Tool vocabulary]",
        "Hermes' own documents name Hermes tools. In this session the tools are "
        "Claude Code's, and the Hermes names map like this:",
    ]
    for name in sorted(HERMES_TO_CLAUDE_TOOLS):
        lines.append(f"  {name} -> {HERMES_TO_CLAUDE_TOOLS[name]}")
    lines.append(
        "Never call a Hermes tool name directly. When a Hermes document asks for "
        "one that has no equivalent here, say so plainly instead of pretending "
        "it ran."
    )
    return "\n".join(lines)


# --------------------------------------------------------------- config policy


def configured_system_prompt() -> Any:
    """The raw ``system_prompt`` node from config.yaml, or ``None``.

    Never raises and never validates — an absent key and an unreadable config
    are the same answer here, and :func:`resolve_system_prompt_config` owns the
    single place where a value becomes a policy.
    """
    try:
        from hermes_cli.config import load_config

        node: Any = load_config()
        for key in CONFIG_KEYS:
            if not isinstance(node, dict):
                return None
            node = node.get(key)
        return node
    except Exception:
        logger.debug(
            "reading the Claude Code ACP system-prompt config failed", exc_info=True
        )
        return None


def resolve_system_prompt_config(raw: Any) -> SystemPromptConfig:
    """Turn a raw config node into a usable policy. Never raises.

    Unlike ``permission_mode``, no value here can widen a security boundary:
    every value only changes what *text* Hermes sends into a session whose
    permission mode, workspace roots and approval gate are decided elsewhere
    and are untouched by this module. The direction that matters is privacy,
    and ``soul`` / ``off`` are the narrow values — so an absent key resolving
    to the full default set is a deliberate choice, not an oversight, and it
    logs nothing (it is the intended experience, not a rejection).
    """
    if raw is None:
        return SystemPromptConfig()
    if not isinstance(raw, dict):
        logger.warning(
            "Ignoring model.claude_code_acp.system_prompt %r: expected a "
            "mapping; using mode %r with its default sections",
            raw, DEFAULT_MODE,
        )
        return SystemPromptConfig()

    mode = _resolve_mode(raw.get("mode"))
    max_chars = _resolve_max_chars(raw.get("max_chars"))

    if mode == MODE_OFF:
        return SystemPromptConfig(mode=mode, sections=(), max_chars=max_chars)

    raw_sections = raw.get("sections")
    if mode == MODE_FULL:
        if raw_sections:
            logger.warning(
                "Ignoring model.claude_code_acp.system_prompt.sections under "
                "mode %r: 'full' sends the built Hermes system prompt verbatim, "
                "so there are no sections to select",
                MODE_FULL,
            )
        return SystemPromptConfig(mode=mode, sections=(), max_chars=max_chars)

    return SystemPromptConfig(
        mode=mode,
        sections=_resolve_sections(raw_sections, mode),
        max_chars=max_chars,
    )


def _resolve_mode(raw: Any) -> str:
    if raw is None:
        return DEFAULT_MODE
    if isinstance(raw, str) and raw.strip() in MODES:
        return raw.strip()
    logger.warning(
        "Ignoring model.claude_code_acp.system_prompt.mode %r: expected one of "
        "%s; using %r",
        raw, list(MODES), DEFAULT_MODE,
    )
    return DEFAULT_MODE


def _resolve_sections(raw: Any, mode: str) -> Tuple[str, ...]:
    default = MODE_SECTIONS.get(mode, MODE_SECTIONS[DEFAULT_MODE])
    if raw is None or raw == []:
        return default
    if not isinstance(raw, (list, tuple)):
        logger.warning(
            "Ignoring model.claude_code_acp.system_prompt.sections %r: expected "
            "a list of section ids from %s; using the %r defaults",
            raw, list(SECTIONS), mode,
        )
        return default

    kept: List[str] = []
    unknown: List[Any] = []
    for entry in raw:
        name = entry.strip() if isinstance(entry, str) else entry
        if name in SECTIONS:
            if name not in kept:
                kept.append(name)
        else:
            unknown.append(entry)
    if unknown:
        logger.warning(
            "Ignoring unknown model.claude_code_acp.system_prompt.sections "
            "entries %r: known ids are %s",
            unknown, list(SECTIONS),
        )
    if not kept:
        logger.warning(
            "model.claude_code_acp.system_prompt.sections named no known "
            "section; using the %r defaults", mode,
        )
        return default
    # Composition order is SECTIONS' order, not the order the user listed them:
    # the bytes must not depend on how the list was typed.
    return tuple(name for name in SECTIONS if name in kept)


def _resolve_max_chars(raw: Any) -> int:
    if raw is None:
        return DEFAULT_MAX_CHARS
    # bool is an int subclass and `max_chars: true` is a typo, not a size.
    if isinstance(raw, bool) or not isinstance(raw, int):
        logger.warning(
            "Ignoring model.claude_code_acp.system_prompt.max_chars %r: expected "
            "an integer between %d and %d; using %d",
            raw, MIN_MAX_CHARS, MAX_MAX_CHARS, DEFAULT_MAX_CHARS,
        )
        return DEFAULT_MAX_CHARS
    clamped = max(MIN_MAX_CHARS, min(MAX_MAX_CHARS, raw))
    if clamped != raw:
        logger.warning(
            "Clamping model.claude_code_acp.system_prompt.max_chars %d to %d "
            "(allowed range %d-%d)",
            raw, clamped, MIN_MAX_CHARS, MAX_MAX_CHARS,
        )
    return clamped


# ---------------------------------------------------------------- composition


def _section_texts(agent: Any, wanted: Tuple[str, ...]) -> Dict[str, str]:
    """Render every wanted section. A section that fails is simply absent.

    Every source is wrapped: these are the same helpers the canonical builder
    calls, but the canonical builder runs inside a path that is allowed to fail
    the turn and this one is not.
    """
    from agent.system_prompt import (
        build_active_profile_hint,
        build_identity_block,
        prompt_context_length,
        resolve_platform_hint_block,
    )

    out: Dict[str, str] = {}
    ctx_len = _guard(lambda: prompt_context_length(agent), None)
    soul_loaded = False

    if "identity" in wanted:
        identity, soul_loaded = _guard(
            lambda: build_identity_block(agent, ctx_len), ("", False)
        )
        if identity:
            out["identity"] = identity

    if "environment" in wanted:
        out["environment"] = _guard(lambda: _ra().build_environment_hints(), "")

    if "profile" in wanted:
        out["profile"] = _guard(build_active_profile_hint, "")

    if "platform" in wanted:
        out["platform"] = _guard(lambda: resolve_platform_hint_block(agent), "")

    if "caller_instructions" in wanted:
        # Captured beside the cached-prompt build in ``agent/turn_context.py``.
        # Read only here, and only at the first connect, so it inherits the
        # cached prompt's own "built once, first turn wins" semantics — a cron
        # job's or a delegation's ``system_message`` would otherwise be
        # silently dropped on this provider alone.
        caller = getattr(agent, "_active_system_message", None)
        out["caller_instructions"] = caller if isinstance(caller, str) else ""

    if "memory" in wanted:
        out["memory"] = _guard(lambda: _memory_block(agent, "memory"), "")

    if "user_profile" in wanted:
        out["user_profile"] = _guard(lambda: _memory_block(agent, "user"), "")

    if "external_memory" in wanted:
        out["external_memory"] = _guard(lambda: _external_memory_block(agent), "")

    if "skills_index" in wanted:
        out["skills_index"] = _guard(lambda: _skills_index(agent), "")

    if "context_files" in wanted:
        out["context_files"] = _guard(
            lambda: _context_files(agent, ctx_len, soul_loaded), ""
        )

    if "coding" in wanted:
        out["coding"] = _guard(lambda: _coding_block(agent), "")

    if "tool_guidance" in wanted:
        out["tool_guidance"] = _guard(_tool_guidance, "")

    return {key: value.strip() for key, value in out.items() if value and value.strip()}


def _ra():
    import run_agent

    return run_agent


def _guard(fn, default):
    try:
        return fn()
    except Exception:
        logger.debug("a Claude Code ACP context section failed to render", exc_info=True)
        return default


def _memory_block(agent: Any, target: str) -> str:
    store = getattr(agent, "_memory_store", None)
    if store is None:
        return ""
    enabled = (
        getattr(agent, "_memory_enabled", True) if target == "memory"
        else getattr(agent, "_user_profile_enabled", True)
    )
    if not enabled:
        return ""
    return store.format_for_system_prompt(target) or ""


def _external_memory_block(agent: Any) -> str:
    manager = getattr(agent, "_memory_manager", None)
    if manager is None:
        return ""
    return manager.build_system_prompt() or ""


def _skills_index(agent: Any) -> str:
    """Opt-in only. The default set excludes it deliberately.

    The index instructs ``skill_view`` / ``skills_list``, which do not exist in
    a Claude Code session — the skills *bridge* (``agent/claude_acp_skills.py``)
    is the right mechanism there, and it works through the adapter's own
    progressive disclosure. It is reachable here because an explicit
    ``sections:`` allow-list should be able to name every block, not because it
    is a good idea.
    """
    tools = list(getattr(agent, "valid_tool_names", None) or [])
    if not tools:
        return ""
    _r = _ra()
    toolsets = {
        toolset
        for toolset in (_r.get_toolset_for_tool(name) for name in tools)
        if toolset
    }
    return _r.build_skills_system_prompt(
        available_tools=tools, available_toolsets=toolsets
    ) or ""


def _context_files(agent: Any, ctx_len: Optional[int], soul_loaded: bool) -> str:
    """Opt-in only: Claude Code already loads project memory through its own
    ``settingSources``, so shipping AGENTS.md/CLAUDE.md here double-loads it —
    and puts repo-authored prose into the *system* block, where the adapter's
    own trust posture for project memory no longer applies."""
    if getattr(agent, "skip_context_files", False):
        return ""
    from agent.runtime_cwd import resolve_context_cwd

    return _ra().build_context_files_prompt(
        cwd=resolve_context_cwd(),
        skip_soul=soul_loaded,
        context_length=ctx_len,
        allow_install_tree_fallback=getattr(agent, "platform", "") in ("cli", "tui"),
    ) or ""


def _coding_block(agent: Any) -> str:
    from agent.coding_context import coding_system_prompt_parts
    from agent.runtime_cwd import resolve_context_cwd

    prefix, workspace, trailing = coding_system_prompt_parts(
        platform=getattr(agent, "platform", ""),
        cwd=resolve_context_cwd(),
        model=getattr(agent, "model", ""),
    )
    return "\n\n".join(
        part.strip()
        for part in list(prefix) + list(workspace) + list(trailing)
        if part and part.strip()
    )


def _tool_guidance() -> str:
    """Opt-in only, and the single biggest reason the default set is curated.

    These blocks steer *Hermes'* tool loop and name Hermes' tools. In a Claude
    Code session they instruct behaviour against tools that are not there.
    """
    from agent.prompt_builder import (
        PARALLEL_TOOL_CALL_GUIDANCE,
        TASK_COMPLETION_GUIDANCE,
        TOOL_USE_ENFORCEMENT_GUIDANCE,
    )

    return "\n\n".join(
        (TASK_COMPLETION_GUIDANCE, PARALLEL_TOOL_CALL_GUIDANCE,
         TOOL_USE_ENFORCEMENT_GUIDANCE)
    )


def _assemble(
    rendered: Dict[str, str],
    order: Tuple[str, ...],
    dropped: List[str],
    *,
    with_vocabulary: bool,
) -> str:
    blocks: List[str] = [PREAMBLE]
    blocks.extend(rendered[name] for name in order if name in rendered)
    if dropped:
        blocks.append(_OMITTED_TEMPLATE % ", ".join(dropped))
    if with_vocabulary:
        blocks.append(vocabulary_note())
    return "\n\n".join(blocks)


def compose_system_prompt(
    agent: Any, config: Optional[SystemPromptConfig] = None
) -> str:
    """The exact text this session's ``append`` will carry.

    Deterministic: composing twice against unchanged inputs returns identical
    bytes, which is what lets the digest stand in for the text everywhere else.
    """
    config = config or SystemPromptConfig()

    if config.mode == MODE_OFF:
        return ""

    if config.mode == MODE_FULL:
        cached = getattr(agent, "_cached_system_prompt", None)
        body = cached.strip() if isinstance(cached, str) else ""
        text = "\n\n".join(
            part for part in (PREAMBLE, vocabulary_note(), body) if part
        )
        return _clamp_verbatim(text, config.max_chars)

    wanted = config.sections or MODE_SECTIONS[DEFAULT_MODE]
    rendered = _section_texts(agent, wanted)
    order = tuple(name for name in SECTIONS if name in wanted)
    # ``soul`` is identity and framing alone: with no Hermes-authored prose that
    # could name a Hermes tool beyond SOUL.md itself, the vocabulary note would
    # be most of the append.
    with_vocabulary = config.mode != MODE_SOUL

    dropped: List[str] = []
    text = _assemble(rendered, order, dropped, with_vocabulary=with_vocabulary)
    for name in DROP_ORDER:
        if len(text) <= config.max_chars:
            break
        if name not in rendered:
            continue
        rendered.pop(name)
        dropped.append(name)
        text = _assemble(rendered, order, dropped, with_vocabulary=with_vocabulary)
    if dropped:
        logger.warning(
            "Claude Code ACP: the Hermes operating context exceeded max_chars "
            "(%d); omitted these sections: %s",
            config.max_chars, ", ".join(dropped),
        )
    if len(text) > config.max_chars and with_vocabulary:
        # Everything droppable is gone. The note goes before identity does.
        text = _assemble(rendered, order, dropped, with_vocabulary=False)
    return _clamp_verbatim(text, config.max_chars)


def _clamp_verbatim(text: str, max_chars: int) -> str:
    """The last-resort bound. Named in the text, and logged once.

    Reached only when the preamble plus a single un-droppable section already
    exceeds ``max_chars`` (or in ``full`` mode, where there are no sections to
    drop). Truncating is worse than dropping, which is why every other path
    drops whole blocks first — but an unbounded append is worse than both.
    """
    if len(text) <= max_chars:
        return text
    logger.warning(
        "Claude Code ACP: the Hermes operating context is %d chars and does not "
        "fit max_chars=%d even with every optional section dropped; truncating",
        len(text), max_chars,
    )
    marker = "\n[Hermes operating context: truncated]"
    keep = max(0, max_chars - len(marker))
    return text[:keep] + marker


def compose_for_agent(agent: Any) -> Optional[ComposedSystemPrompt]:
    """Compose this agent's operating context, or ``None`` to send no ``_meta``.

    Fail-open by construction (D7): a config read that explodes, a section
    source that explodes, an ``agent`` shaped differently than expected — each
    costs one WARNING and a session created exactly the way it is created
    today. The turn always runs.
    """
    try:
        config = resolve_system_prompt_config(configured_system_prompt())
        if config.mode == MODE_OFF:
            return None
        text = compose_system_prompt(agent, config)
        if not text or not text.strip():
            return None
        return ComposedSystemPrompt(text=text, mode=config.mode)
    except Exception:
        logger.warning(
            "Claude Code ACP: composing the Hermes operating context failed; "
            "this session is created without one",
            exc_info=True,
        )
        return None


def describe_composition(composed: ComposedSystemPrompt) -> str:
    """One line for the connect log. Digest and size only — never the text.

    Says *sent*, never *applied*: there is no capability for "the agent honoured
    ``_meta.systemPrompt``", and ``model.claude_code_acp.command`` can point at
    any ACP agent. What Hermes knows is what Hermes put on the wire.
    """
    return (
        "Claude Code ACP: sent the Hermes operating context (mode=%s, %d chars, "
        "sha256=%s…)" % (composed.mode, composed.chars, composed.digest[:12])
    )


__all__ = [
    "CONFIG_KEYS",
    "DEFAULT_MAX_CHARS",
    "DEFAULT_MODE",
    "DROP_ORDER",
    "MAX_MAX_CHARS",
    "MIN_MAX_CHARS",
    "MODES",
    "MODE_FULL",
    "MODE_HERMES",
    "MODE_OFF",
    "MODE_SECTIONS",
    "MODE_SOUL",
    "PREAMBLE",
    "SECTIONS",
    "ComposedSystemPrompt",
    "SystemPromptConfig",
    "compose_for_agent",
    "compose_system_prompt",
    "configured_system_prompt",
    "describe_composition",
    "prompt_digest",
    "resolve_system_prompt_config",
    "system_prompt_meta",
    "vocabulary_note",
]
