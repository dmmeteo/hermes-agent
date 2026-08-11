"""Projects ACP ``session/update`` events into Hermes' typed turn surfaces.

The Claude Code adapter reports a turn as typed protocol events, so projection
is a *mapping* job rather than the text-parsing job the earlier direct-CLI
experiment failed at (spike §8.3). This module owns that mapping and nothing
else: it takes one ``session/update`` payload at a time and returns a small
typed record. It never touches Hermes' agent object, never fires a callback,
and never touches the filesystem — ``agent/claude_code_runtime.py`` owns the
bridge from these records to Hermes' display callbacks, and
``agent/claude_acp_session.py`` owns the wire.

Event map (ACP ``sessionUpdate`` discriminator → projection):

  agent_message_chunk    → ``text``          (assistant reply; streamed *and*
                                              accumulated into the response)
  agent_thought_chunk    → ``thought``       (reasoning surface only)
  tool_call              → ``tool_started``
  tool_call_update       → ``tool_updated`` while the status is non-terminal,
                           ``tool_completed`` on ``completed`` / ``failed``
  usage_update           → ``usage``         (context occupancy + cost)
  everything else        → ``ignored``

Alongside those per-event projections the projector accumulates the turn's tool
activity as canonical **transcript rows** (``projected_messages``, Packet 5
slice D): one ``{role: "assistant", content: None, tool_calls: [...]}`` /
``{role: "tool", tool_call_id, content}`` pair per completed tool call, the
shape ``agent/transports/codex_event_projector.py`` already produces and
``agent/curator.py`` / ``session_search`` / a later native-model turn already
read. Display and persistence stay two disjoint consumers of one projector: a
row fires no callback and a callback writes no row.

Two rules make the "no raw payload in the reply" invariant structural rather
than incidental:

* **assistant text is an allow-list of exactly one event kind.** Anything new
  the adapter starts emitting — a replayed ``user_message_chunk``, a plan, a
  slash-command catalogue — is ignored by default, not appended to the reply.
* **every *display* field that leaves this module is credential-scrubbed**,
  because tool payloads are the one place a real key legitimately appears (a
  ``Bash`` command line, an edited ``.env``) and a projected display field ends
  up in a transcript or a chat message. The two exceptions are named and narrow:
  ``PermissionRequest.declared_command`` / ``.declared_paths`` are raw and are
  never rendered — the unconditional approval floor has to measure what the agent
  declared, not a redacted rendering of it. Assistant reply text is likewise
  accumulated raw (``_project_text``), because the response Hermes returns must
  be byte-identical to what the model said; the streaming surfaces apply their
  own scrubbers downstream.

Scrubbing runs before truncation: cutting first can split a secret into a
prefix the redactor no longer recognises.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

__all__ = [
    "ClaudeAcpEventProjector",
    "MODEL_CONFIG_OPTION_ID",
    "PERSISTED_RESULT_MAX_CHARS",
    "PermissionRequest",
    "ProjectedEvent",
    "ToolActivity",
    "UNREPORTED_TOOL_RESULT",
    "UsageSnapshot",
    "deterministic_call_id",
    "extract_available_mode_ids",
    "extract_current_mode_id",
    "extract_current_model_id",
    "extract_model_options",
    "has_model_config_option",
    "project_permission_request",
]

#: Statuses that end a tool call. Anything else is progress, not an outcome.
TERMINAL_TOOL_STATUSES = frozenset({"completed", "failed"})

#: The only update kind whose text is the assistant's reply.
ASSISTANT_TEXT_UPDATE = "agent_message_chunk"

#: The ``id`` the pinned adapter gives its model selector in ``configOptions``
#: (``MODEL_CONFIG_ID`` in ``dist/acp-agent.js``). ACP has no dedicated
#: "current model" field: the adapter reports it as this option's
#: ``currentValue`` on ``session/new``, ``session/load`` AND every later
#: ``config_option_update`` — including one it sends on its own, unprompted,
#: after an SDK-side refusal fallback (``syncModelAfterRefusalFallback``
#: re-uses the exact same ``config_option_update`` path a user-driven switch
#: takes). One extractor, shared with the wire client's ``current_model_id``
#: (``agent/transports/claude_acp.py``), covers the initial identity and every
#: live change including that automatic one.
MODEL_CONFIG_OPTION_ID = "model"


def extract_current_model_id(config_options: Any) -> Optional[str]:
    """The model selector a ``configOptions`` list reports, or ``None``.

    ``config_options`` is the raw, un-typed ``SessionConfigOption`` list ACP
    carries on ``session/new``'s and ``session/load``'s responses and on every
    ``config_option_update`` — identical shape in all three, so one function
    reads it everywhere it can appear. Returns ``None`` for anything that
    is not a well-formed match: an older adapter that never reports a model
    option, a malformed entry, or the list missing entirely.

    **This is the picker's selected value, not necessarily a model id.** The
    adapter maps only ``ModelInfo.value`` onto the wire and drops
    ``resolvedModel`` (the canonical id an alias resolves to), and it always
    prepends a ``"default"`` row it also selects when the user set no model —
    so ``"default"`` and family aliases like ``"sonnet"`` are ordinary live
    values here. ``agent/claude_code_runtime.py`` is where that distinction is
    interpreted (display name vs accounting key); this function only reads.
    """
    option = _model_config_option(config_options)
    if option is None:
        return None
    value = option.get("currentValue")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _model_config_option(config_options: Any) -> Optional[Dict[str, Any]]:
    """The "model" entry of a ``configOptions`` list, or ``None``."""
    if not isinstance(config_options, list):
        return None
    for option in config_options:
        if isinstance(option, dict) and option.get("id") == MODEL_CONFIG_OPTION_ID:
            return option
    return None


def has_model_config_option(config_options: Any) -> bool:
    """Whether this response carried a model selector at all.

    The companion question to :func:`extract_model_options`, and a different
    one: an empty list means "this session advertises no selectable model",
    while a missing entry means "this response says nothing about models".
    A reconnect must not read the second as the first — the previous adapter's
    list is not evidence about the one now answering (see
    ``ClaudeAcpSession._capture_model_state``).
    """
    return _model_config_option(config_options) is not None


def extract_model_options(config_options: Any) -> List[Dict[str, str]]:
    """Every model this session advertises as selectable, in the agent's order.

    The write-side companion to :func:`extract_current_model_id`, reading the
    same "model" entry's ``options`` instead of its ``currentValue`` (Packet 4
    slice B). The adapter builds that list from ``models.availableModels``,
    which the user's own Claude ``availableModels`` allowlist already filtered
    — so treating it as the whole truth is what keeps a Hermes-side pick inside
    the adapter's policy instead of second-guessing it.

    ACP allows the list to be *grouped* (``SessionConfigSelectOptions``), so
    groups are flattened exactly as the adapter flattens them when validating a
    requested value. Returns ``[]`` for an agent that advertises no selectable
    model at all — the only gate available, since ACP publishes no capability
    for this and adapter 0.66.0 advertises none.

    ``name`` is ``""`` for a row that carries none, rather than the value copied
    into it: "this row has no human-readable name" is the fact a display surface
    needs (``_option_label`` names the token instead of showing it bare), and a
    name substituted here is one it cannot tell apart from a real one. Like the
    rest of this module, this function reads the wire and interprets nothing.
    """
    option = _model_config_option(config_options)
    if option is None:
        return []
    raw = option.get("options")
    if not isinstance(raw, list):
        return []
    flattened: List[Any] = []
    for entry in raw:
        if isinstance(entry, dict) and isinstance(entry.get("options"), list):
            flattened.extend(entry["options"])
        else:
            flattened.append(entry)
    out: List[Dict[str, str]] = []
    seen: set = set()
    for entry in flattened:
        if not isinstance(entry, dict):
            continue
        value = entry.get("value")
        if not isinstance(value, str) or not value.strip():
            continue
        value = value.strip()
        if value in seen:
            continue
        seen.add(value)
        name = entry.get("name")
        description = entry.get("description")
        out.append({
            "value": value,
            "name": name.strip() if isinstance(name, str) else "",
            "description": (
                description.strip() if isinstance(description, str) else ""
            ),
        })
    return out


def extract_current_mode_id(modes: Any) -> Optional[str]:
    """The permission mode a ``modes`` block reports as current, or ``None``.

    *modes* is the raw ``SessionModeState`` ACP carries on ``session/new``'s and
    ``session/load``'s responses (``{currentModeId, availableModes}``; adapter
    0.66.0's ``createSession`` always sends one). Unlike the model, the mode has
    a dedicated ACP field and a dedicated change notification
    (``current_mode_update``) — so this reads a real protocol shape rather than
    the generic ``configOptions`` list, which also carries a "mode" entry the
    adapter leaves inert.

    ``None`` for anything that is not a well-formed answer, which is the fact a
    caller needs: a response that says nothing about modes must not be read as
    one that says the session has none (see :func:`extract_available_mode_ids`).
    """
    if not isinstance(modes, dict):
        return None
    value = modes.get("currentModeId")
    return value.strip() if isinstance(value, str) and value.strip() else None


def extract_available_mode_ids(modes: Any) -> List[str]:
    """Every permission mode a ``modes`` block advertises, in the agent's order.

    Reads ``availableModes[].id``. Returns ``[]`` for a block that advertises
    none *and* for one that is malformed — the caller distinguishes "no block at
    all" from "an empty block" by whether the response carried ``modes``, not by
    this list (``ClaudeAcpClient.available_mode_ids`` is where that split lives).
    """
    if not isinstance(modes, dict):
        return []
    raw = modes.get("availableModes")
    if not isinstance(raw, list):
        return []
    out: List[str] = []
    for entry in raw:
        mode_id: Any = None
        if isinstance(entry, dict):
            mode_id = entry.get("id")
        elif isinstance(entry, str):
            mode_id = entry
        if not isinstance(mode_id, str) or not mode_id.strip():
            continue
        mode_id = mode_id.strip()
        if mode_id not in out:
            out.append(mode_id)
    return out


# Display bounds. A single tool result can be a whole file; a messaging
# platform must not receive it, and neither must a progress bubble.
_MAX_RESULT_CHARS = 4000
_MAX_ARG_CHARS = 2000
_MAX_PREVIEW_CHARS = 120
#: Guards against a pathological nesting depth in `rawInput` / `rawOutput`.
_MAX_SCRUB_DEPTH = 6
#: How many declared tool locations an approval prompt may name. A tool that
#: touches 400 files must not turn the prompt into a wall of paths.
_MAX_LOCATIONS = 8

#: Bound on a tool result *as persisted in a transcript row*. A chatty Claude
#: Code turn can produce dozens of these into a session DB that is already
#: hundreds of megabytes, and a single result can be a whole file. Deliberately
#: its own constant rather than a reuse of the display bound: a display bubble
#: is thrown away, so cutting it with a bare ellipsis costs nothing, while a
#: transcript row is read later by the curator, by ``session_search`` and
#: possibly by a real API call — and a reader who cannot tell a complete result
#: from a truncated one has been told the wrong thing. The byte-exact output
#: always remains in Claude Code's own transcript, whose uuid is persisted on
#: the Hermes session (``agent/claude_acp_binding.py``).
PERSISTED_RESULT_MAX_CHARS = 4000

#: What a persisted row says for a tool call the adapter never reported an
#: outcome for — the turn was cancelled, errored, or stopped mid-tool. The row
#: exists rather than being dropped because an assistant ``tool_calls`` row with
#: no matching ``tool`` row is what breaks ``repair_message_sequence`` and what
#: a later native-model turn on the same session would send to a real API.
UNREPORTED_TOOL_RESULT = "[no result recorded: turn ended before the tool reported]"

#: Marks a failed tool in its transcript row. The display surface carries
#: ``is_error`` as structured data; a row has only its text, and "the tool ran
#: and returned this" reads as success without it.
_FAILED_RESULT_PREFIX = "[failed]"


def deterministic_call_id(tool_call_id: str) -> str:
    """Stable Hermes-side id for an ACP tool call.

    Derived from the adapter's own ``toolCallId`` so a live tool card and the
    same call seen again after a ``session/load`` replay carry one identity.
    Namespaced so it can never collide with a Hermes tool-call id.
    """
    return f"claude_acp_{tool_call_id}" if tool_call_id else "claude_acp_unknown"


def _scrub_text(text: str) -> str:
    """Credential-scrub one display string. Never raises."""
    if not text:
        return ""
    try:
        from agent.redact import redact_sensitive_text

        return redact_sensitive_text(text, force=True)
    except Exception:
        # Redaction failing must not turn into an unscrubbed leak: drop the
        # value rather than surfacing it raw.
        logger.debug("redaction of a projected ACP field failed", exc_info=True)
        return "«redacted»"


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def _scrub_value(value: Any, depth: int = 0) -> Any:
    """Recursively scrub a raw ACP payload for display.

    Structure is preserved — the display shows *which* arguments a tool got,
    with the secret-shaped values masked, rather than dropping the field (which
    would read as "the agent passed nothing").
    """
    if depth >= _MAX_SCRUB_DEPTH:
        return "«nested»"
    if isinstance(value, str):
        return _truncate(_scrub_text(value), _MAX_ARG_CHARS)
    if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
        return value
    if isinstance(value, dict):
        return {str(k): _scrub_value(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub_value(v, depth + 1) for v in value]
    return _truncate(_scrub_text(str(value)), _MAX_ARG_CHARS)


def _as_args(raw_input: Any) -> Dict[str, Any]:
    """Scrubbed argument dict for the tool-activity surfaces."""
    if isinstance(raw_input, dict):
        return _scrub_value(raw_input)
    if raw_input is None:
        return {}
    return {"input": _scrub_value(raw_input)}


def _content_text(blocks: Any) -> str:
    """Flatten ACP ``ToolCallContent`` blocks into display text."""
    if not isinstance(blocks, list):
        return ""
    parts: List[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "content":
            inner = block.get("content")
            if isinstance(inner, dict) and isinstance(inner.get("text"), str):
                parts.append(inner["text"])
        elif kind == "diff":
            path = block.get("path") or ""
            parts.append(f"[diff] {path}")
        elif kind == "terminal":
            parts.append(f"[terminal] {block.get('terminalId') or ''}")
    return "\n".join(part for part in parts if part)


def _scrubbed_result_text(payload: Dict[str, Any]) -> str:
    """Scrubbed — but NOT yet bounded — outcome text for a finished tool call.

    Unbounded because the display surface and the transcript row want different
    bounds, and scrubbing has to happen once, before either cut: truncating
    first can split a secret into a prefix the redactor no longer recognises.
    """
    raw_output = payload.get("rawOutput")
    if isinstance(raw_output, str):
        text = raw_output
    elif raw_output is not None:
        try:
            text = json.dumps(raw_output, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            text = repr(raw_output)
    else:
        text = _content_text(payload.get("content"))
    return _scrub_text(text)


def _result_text(payload: Dict[str, Any]) -> str:
    """Scrubbed, display-bounded outcome text for a finished tool call."""
    return _truncate(_scrubbed_result_text(payload), _MAX_RESULT_CHARS)


def _persisted_result_text(text: str) -> str:
    """Bound *text* for a transcript row, naming what was left out.

    Unlike :func:`_truncate`, the marker carries a count: a reader who sees one
    knows both that the result is partial and how much of it is missing, and can
    go to Claude Code's own transcript for the rest.
    """
    if len(text) <= PERSISTED_RESULT_MAX_CHARS:
        return text
    omitted = len(text) - PERSISTED_RESULT_MAX_CHARS
    return (
        text[:PERSISTED_RESULT_MAX_CHARS] + f"… [+{omitted} chars truncated]"
    )


def _transcript_pair(
    activity: "ToolActivity", result: str
) -> List[Dict[str, Any]]:
    """One tool call as the canonical ``(assistant tool_calls, tool)`` pair.

    The shape is ``codex_event_projector``'s, verbatim, because the readers are
    the same: ``agent/curator.py``, external-memory review, background-review
    snapshots, ``session_search``, and — if this session is ever switched to a
    native model — a real OpenAI-shaped API call.

    ``activity.args`` is already scrubbed and per-value bounded (``_scrub_value``
    caps every string at ``_MAX_ARG_CHARS``), so the arguments carried here are
    the same masked values the tool bubble showed. The tool is named exactly as
    the adapter named it (``Bash``, ``Write``) — honestly not-a-Hermes-tool, and
    the same name its approval prompt already used.
    """
    try:
        arguments = json.dumps(activity.args, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):  # pragma: no cover - args are scrubbed JSON
        arguments = "{}"
    return [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": activity.call_id,
                "type": "function",
                "function": {"name": activity.name, "arguments": arguments},
            }],
        },
        {
            "role": "tool",
            "tool_call_id": activity.call_id,
            # Both spellings, matching what Hermes' own tool loop writes
            # (`agent/tool_dispatch_helpers.py`): `name` is the wire field,
            # `tool_name` is the SessionDB column.
            "name": activity.name,
            "tool_name": activity.name,
            "content": result,
        },
    ]


def _declared_tool_name(payload: Dict[str, Any]) -> str:
    """The adapter's own tool name from ``_meta.claudeCode.toolName``, or ``""``.

    Shared by tool-activity projection and permission-request projection so a
    tool is named the same way in an approval prompt as in a progress bubble.
    """
    meta = payload.get("_meta")
    if isinstance(meta, dict):
        claude_meta = meta.get("claudeCode")
        if isinstance(claude_meta, dict):
            name = claude_meta.get("toolName")
            if isinstance(name, str) and name.strip():
                return _truncate(_scrub_text(name.strip()), _MAX_PREVIEW_CHARS)
    return ""


def _location_pairs(payload: Dict[str, Any]) -> List[tuple]:
    """``(declared path, display path)`` for every location on a tool call.

    Two views of the same list, produced together so they stay index-aligned.
    The display path is scrubbed and bounded by the *display* budget, not the
    argument budget: it becomes approval-prompt text, and on a messaging
    platform that text leaves the machine. The declared path is kept verbatim
    because that is what a workspace-root check has to measure — a truncated or
    redacted path resolves to something the agent never asked about.

    The list itself is deliberately NOT truncated here. Identity (the approval
    signature) has to see every path a tool declared — two asks that agree on
    their first few paths but differ later must not share one remembered grant.
    Bounding the list for *display* is :func:`project_permission_request`'s job.
    """
    raw = payload.get("locations")
    if not isinstance(raw, list):
        return []
    pairs: List[tuple] = []
    for entry in raw:
        if isinstance(entry, dict):
            path = entry.get("path")
        else:
            path = entry
        if isinstance(path, str) and path.strip():
            declared = path.strip()
            pairs.append(
                (declared, _truncate(_scrub_text(declared), _MAX_PREVIEW_CHARS))
            )
    return pairs


@dataclass
class PermissionRequest:
    """One ``session/request_permission``, as a human approval surface sees it.

    Every *display* field is already scrubbed and bounded: this is the record
    that becomes the text of a CLI prompt or a Discord/Telegram approval
    message, so nothing rendered from it may carry a credential or an unbounded
    payload. The protocol ``options`` are deliberately NOT here — answering the
    wire is the session's job, and the approval surface has no business choosing
    an ``optionId``.

    Two fields (``declared_command``, ``declared_paths``) are deliberately
    **raw**, and are never rendered. They exist for the unconditional policy
    floor in ``tools/approval.py``, which has to measure what the agent declared
    rather than a redacted, truncated rendering of it — the same reason the
    workspace-root check uses the declared path (see :func:`_location_pairs`). A
    surface that prints this record wholesale would defeat that; print
    :attr:`description` instead, which is what the prompt is for.
    """

    tool_name: str
    title: str = ""
    kind: str = ""
    args: Dict[str, Any] = field(default_factory=dict)
    #: The paths shown to the user — at most ``_MAX_LOCATIONS`` of them.
    locations: List[str] = field(default_factory=list)
    #: How many further paths the tool declared but the prompt does not list.
    #: Reported rather than dropped: a user approving "affects a.py, b.py" when
    #: 392 more files are in scope has been shown the wrong action.
    omitted_locations: int = 0
    #: Declared paths that fall outside the session's negotiated workspace roots
    #: (display form, and only the ones shown). Empty when no roots were
    #: supplied: with nothing to compare against there is no basis for the
    #: claim, and labelling everything "outside" trains the user to ignore it.
    outside_roots: tuple = ()
    #: How many *further* declared paths fall outside the roots but sit past the
    #: displayed slice. Since containment is marked rather than refused, the
    #: marker is the whole protection: a bulk edit whose ninth path leaves the
    #: workspace must not be presented as staying inside it.
    outside_roots_hidden: int = 0
    #: Opaque stable-per-request identity for Hermes' allowlist grain: two
    #: requests that describe the same action share it, so ``[a]lways`` cannot
    #: blanket a different action on the same tool. A digest of the *declared*
    #: values, so it neither collides on two actions that merely display alike
    #: nor carries the material it was computed from.
    signature: str = ""
    #: Raw declared shell command, when the tool declared one. Policy input
    #: only — never displayed.
    declared_command: str = ""
    #: Every raw declared path, in declaration order. Policy input only — never
    #: displayed.
    declared_paths: tuple = ()

    @property
    def description(self) -> str:
        """One line naming what is being asked, for the approval prompt."""
        parts: List[str] = []
        if self.title:
            parts.append(self.title)
        elif self.kind:
            parts.append(f"{self.kind} operation")
        if self.locations:
            affects = "affects " + ", ".join(self.locations)
            if self.omitted_locations:
                affects += f" (+{self.omitted_locations} more)"
            parts.append(affects)
        elif self.omitted_locations:
            parts.append(f"affects {self.omitted_locations} paths")
        if self.outside_roots:
            # The workspace is what the session negotiated; a path outside it is
            # the difference between "edit my project" and "edit my home
            # directory", and the user cannot weigh that from the path alone.
            marker = "outside the workspace: " + ", ".join(self.outside_roots)
            if self.outside_roots_hidden:
                marker += f" (+{self.outside_roots_hidden} more outside the workspace)"
            parts.append(marker)
        elif self.outside_roots_hidden:
            # None of the *shown* paths is outside, but some of the ones the
            # prompt had to omit are. Saying nothing would present a write that
            # leaves the workspace as one that stays inside it.
            parts.append(
                f"{self.outside_roots_hidden} more outside the workspace"
            )
        return "; ".join(parts) or f"{self.tool_name} requested permission"


def _declared_command(tool_call: Dict[str, Any], kind_raw: str) -> str:
    """The raw shell command a tool declared, or ``""``.

    ``Bash`` carries it in ``rawInput.command``; the adapter also copies it
    verbatim into ``title`` (``dist/tools.js``, ``case "Bash"``), which is the
    fallback when a future tool declares a command without naming the field.
    The title is only trusted for the ``execute`` kind — a ``Write``'s title is
    prose ("Write a.py"), not a command, and feeding prose to a shell-syntax
    detector invites blocks nobody can explain.

    An argv list (``["rm", "-rf", "/"]``) is joined rather than ignored. The
    pinned adapter always sends a string, so this is about the failure mode: a
    shape the extractor does not recognise returns ``""`` and the policy floor
    then silently measures nothing.
    """
    raw_input = tool_call.get("rawInput")
    if isinstance(raw_input, dict):
        command = raw_input.get("command")
        if isinstance(command, str) and command.strip():
            return command
        if isinstance(command, (list, tuple)) and all(
            isinstance(part, str) for part in command
        ):
            joined = " ".join(command).strip()
            if joined:
                return joined
    if kind_raw == "execute":
        title = tool_call.get("title")
        if isinstance(title, str) and title.strip():
            return title
    return ""


def project_permission_request(
    params: Dict[str, Any],
    roots: Optional[List[str]] = None,
    tool_name_hint: str = "",
) -> PermissionRequest:
    """Project ``session/request_permission`` params into an approval record.

    Every *display* field is credential-scrubbed and bounded before truncation,
    exactly as for a projected update — an approval prompt is user-visible text,
    and on a messaging platform it leaves the machine. The record additionally
    carries the raw declared command/paths for the policy floor; see
    :class:`PermissionRequest`.

    ``roots`` are the session's negotiated workspace directories. When supplied,
    **every** declared path is classified against them so the prompt can say
    which ones fall outside — including the ones the prompt has no room to list,
    which are reported as a count. Classification only: nothing here opens,
    reads, or creates a path, and a path outside the roots is still shown to the
    user rather than silently dropped.

    ``tool_name_hint`` is the tool name the adapter declared on the ``tool_call``
    this request refers to. Adapter 0.66.0 stamps ``_meta.claudeCode.toolName``
    on a permission request only for a *parented* (sub-agent) call, so for an
    ordinary top-level Bash/Write there is no name in these params at all — but
    the adapter always emits the referenced ``tool_call`` first, and that update
    does carry it. Passing it in is how the prompt names the tool that is
    actually running instead of the ACP *kind* ("execute", "edit"). Absent both,
    the kind is what can honestly be said.
    """
    tool_call = params.get("toolCall")
    if not isinstance(tool_call, dict):
        tool_call = {}
    title_raw = tool_call.get("title")
    title_raw = title_raw.strip() if isinstance(title_raw, str) else ""
    title = (
        _truncate(_scrub_text(title_raw), _MAX_PREVIEW_CHARS) if title_raw else ""
    )
    kind_value = tool_call.get("kind")
    kind_raw = kind_value.strip() if isinstance(kind_value, str) else ""
    kind = _truncate(_scrub_text(kind_raw), _MAX_PREVIEW_CHARS) if kind_raw else ""
    hint = (
        _truncate(_scrub_text(tool_name_hint.strip()), _MAX_PREVIEW_CHARS)
        if isinstance(tool_name_hint, str) and tool_name_hint.strip()
        else ""
    )
    name = _declared_tool_name(tool_call) or hint or kind or "tool"
    raw_input = tool_call.get("rawInput")
    args = _as_args(raw_input)
    pairs = _location_pairs(tool_call)
    declared_raw = tuple(raw for raw, _display in pairs)
    declared = [display for _raw, display in pairs]
    # Display is bounded; classification and identity are not. A tool touching
    # 400 files must not turn the prompt into a wall of paths, but every path it
    # declared still has to be measured against the roots and reach the
    # signature — otherwise a mass edit's ninth path could leave the workspace
    # unremarked, and two different mass edits sharing their first eight paths
    # would share one remembered grant.
    shown = declared[:_MAX_LOCATIONS]
    outside: tuple = ()
    outside_hidden = 0
    if roots:
        from agent.claude_acp_workspace import is_within_roots

        outside_all: List[Tuple[int, str]] = [
            (index, display)
            for index, (raw, display) in enumerate(pairs)
            if not is_within_roots(raw, roots)
        ]
        outside = tuple(
            display for index, display in outside_all if index < _MAX_LOCATIONS
        )
        outside_hidden = sum(
            1 for index, _display in outside_all if index >= _MAX_LOCATIONS
        )
    # Identity is derived from WHAT is being asked, never from the adapter's
    # per-call ``toolCallId``: a remembered grant must apply to the same action
    # asked again, and must not apply to a different one. It is computed over
    # the DECLARED values — the display view is scrubbed and truncated, so two
    # sibling files under a long common prefix, or two paths differing only
    # inside a secret-shaped segment, render identically and would share one
    # grant. The result is a digest, so the raw material it distinguishes on is
    # not carried around by the record.
    try:
        material = json.dumps(
            {"tool": name, "kind": kind_raw, "title": title_raw,
             "locations": list(declared_raw), "args": raw_input},
            ensure_ascii=False, sort_keys=True, default=str,
        )
    except (TypeError, ValueError):
        material = f"{name}|{kind_raw}|{title_raw}|{declared_raw}|{raw_input!r}"
    signature = hashlib.sha256(material.encode("utf-8", "replace")).hexdigest()
    return PermissionRequest(
        tool_name=name,
        title=title,
        kind=kind,
        args=args,
        locations=shown,
        omitted_locations=len(declared) - len(shown),
        outside_roots=outside,
        outside_roots_hidden=outside_hidden,
        signature=signature,
        declared_command=_declared_command(tool_call, kind_raw),
        declared_paths=declared_raw,
    )


@dataclass
class ToolActivity:
    """One Claude Code tool call, as Hermes' display surfaces see it."""

    call_id: str
    name: str
    args: Dict[str, Any] = field(default_factory=dict)
    preview: Optional[str] = None
    kind: str = ""
    status: str = ""
    result: str = ""
    is_error: bool = False
    duration: Optional[float] = None
    #: True when this activity was first seen on a terminal ``tool_call_update``
    #: with no preceding ``tool_call``. The bridge owes it an opening event, or
    #: a structured tool card never opens for it.
    needs_start: bool = False
    started_at: float = 0.0


@dataclass
class UsageSnapshot:
    """Context occupancy and cost from a ``usage_update``.

    ``used`` is how much of the context window the session now occupies — NOT
    this turn's token spend. Summing it per event (a real turn emits several)
    would inflate session accounting by orders of magnitude, so this is a
    last-wins snapshot and the per-turn token counts come from the
    ``session/prompt`` reply's ``usage`` block instead.
    """

    context_used_tokens: int = 0
    context_window_tokens: int = 0
    cost_amount: Optional[float] = None
    cost_currency: str = ""


@dataclass
class ProjectedEvent:
    """One projected event. ``kind == "ignored"`` means "nothing to surface"."""

    kind: str
    text: str = ""
    tool: Optional[ToolActivity] = None
    usage: Optional[UsageSnapshot] = None
    #: Set only for ``kind == "model_changed"`` — the model value
    #: ``config_option_update`` just reported (an id or a picker selector; see
    #: ``extract_current_model_id``). State capture only: this has no
    #: display surface of its own (see ``claude_code_runtime.py``'s event
    #: bridge), it exists so ``ClaudeAcpSession`` can keep its identity current.
    model_id: str = ""
    #: Set only for ``kind == "mode_changed"`` — the permission mode a
    #: ``current_mode_update`` just reported (Packet 5 slice A). State capture
    #: only, for exactly the reasons ``model_id`` is: the posture is a config
    #: decision and a mid-session change is a fact the session records, never a
    #: chat bubble.
    mode_id: str = ""


_IGNORED = ProjectedEvent(kind="ignored")


class ClaudeAcpEventProjector:
    """Stateful projector consuming one turn's ``session/update`` payloads.

    Not thread-safe: the ACP client pumps updates on the turn's own thread, so
    one projector serves one turn, in arrival order.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._text_parts: List[str] = []
        self._tools: Dict[str, ToolActivity] = {}
        #: ``toolCallId`` → the tool name the adapter declared for it. Kept
        #: separately from ``_tools`` because it is only ever the adapter's OWN
        #: name, never a kind fallback — a permission prompt asking about the
        #: wrong tool is worse than one that names the kind honestly.
        self._declared_names: Dict[str, str] = {}
        #: Transcript rows for the tool calls that reached a terminal update, in
        #: completion order (Packet 5 slice D). Written on outcomes only, never
        #: per progress event — a status change is not a thing that happened.
        self._transcript_rows: List[Dict[str, Any]] = []
        #: ``toolCallId``s already represented in :attr:`_transcript_rows`, so
        #: the orphan repair in :meth:`projected_messages` can tell an
        #: unfinished call from a finished one.
        self._recorded_calls: set = set()
        self.tool_iterations = 0
        self.usage: Optional[UsageSnapshot] = None
        #: Last model id a ``config_option_update`` reported during THIS turn,
        #: or ``None`` if none arrived. ``ClaudeAcpSession`` is what carries
        #: identity ACROSS turns — a fresh projector starting at ``None`` here
        #: just means "no change was reported this turn", not "no model known".
        self.model_id: Optional[str] = None
        #: Last permission mode a ``current_mode_update`` reported during THIS
        #: turn, on the same terms as :attr:`model_id`: per-turn, and the
        #: session is what carries the posture across turns.
        self.mode_id: Optional[str] = None

    # ---------- accumulated turn state ----------

    @property
    def assistant_text(self) -> str:
        """The reply, assembled from assistant text chunks only."""
        return "".join(self._text_parts)

    @property
    def projected_messages(self) -> List[Dict[str, Any]]:
        """This turn's tool activity as canonical transcript rows.

        One ``(assistant tool_calls, tool)`` pair per tool call, in completion
        order, with every call that never reached a terminal update repaired at
        the end (:data:`UNREPORTED_TOOL_RESULT`). The repair is computed on read
        rather than flushed by a caller, so there is no "someone forgot to call
        flush" failure mode: **every** exit path — success, a non-``end_turn``
        stop reason, a cancel mid-tool — reads a list with no orphan
        ``tool_calls`` row in it, and reading twice gives the same list.

        Fires no callback and touches no display surface. The runtime splices
        this into ``messages`` once, at turn end
        (``agent/claude_code_runtime.py``); the event sink never sees it.
        """
        rows = list(self._transcript_rows)
        for call_id, activity in self._tools.items():
            if call_id in self._recorded_calls:
                continue
            rows.extend(_transcript_pair(activity, UNREPORTED_TOOL_RESULT))
        return rows

    def declared_tool_name(self, tool_call_id: str) -> str:
        """The tool name the adapter declared for ``tool_call_id``, or ``""``.

        The permission path's honest answer to a request that carries no
        ``_meta`` of its own: the adapter emits the referenced ``tool_call``
        before it asks (``ensureToolCallEmitted``), and that update always
        carries the name. Nothing is invented for a call never seen.
        """
        return self._declared_names.get(str(tool_call_id or ""), "")

    # ---------- projection ----------

    def project(self, update: Dict[str, Any]) -> ProjectedEvent:
        if not isinstance(update, dict):
            return _IGNORED
        kind = update.get("sessionUpdate")
        if kind == ASSISTANT_TEXT_UPDATE:
            return self._project_text(update)
        if kind == "agent_thought_chunk":
            text = self._chunk_text(update)
            return ProjectedEvent(kind="thought", text=text) if text else _IGNORED
        if kind == "tool_call":
            return self._project_tool_call(update)
        if kind == "tool_call_update":
            return self._project_tool_call_update(update)
        if kind == "usage_update":
            return self._project_usage(update)
        if kind == "config_option_update":
            return self._project_config_option_update(update)
        if kind == "current_mode_update":
            return self._project_current_mode_update(update)
        # Deliberately ignored, and deliberately the default: user_message_chunk
        # (our own prompt echoed back, or history a session/load replayed),
        # plan/plan_update, available_commands_update, session_info_update,
        # and anything a future adapter version adds. None of it is the
        # assistant's reply.
        return _IGNORED

    # ---------- per-kind ----------

    @staticmethod
    def _chunk_text(update: Dict[str, Any]) -> str:
        content = update.get("content")
        if not isinstance(content, dict):
            return ""
        if content.get("type") != "text":
            # Non-text blocks (image, audio, resource) have no place in a text
            # transcript; the tool surfaces carry the structured view.
            return ""
        text = content.get("text")
        return text if isinstance(text, str) else ""

    def _project_text(self, update: Dict[str, Any]) -> ProjectedEvent:
        text = self._chunk_text(update)
        if not text:
            return _IGNORED
        # Accumulated raw: this is the assistant's own reply, and the response
        # Hermes returns must be byte-identical to what the model said. The
        # streaming surface applies its own scrubbers downstream.
        self._text_parts.append(text)
        return ProjectedEvent(kind="text", text=text)

    def _tool_name(
        self,
        payload: Dict[str, Any],
        existing: Optional[ToolActivity],
        call_id: str = "",
    ) -> str:
        """Display name for a tool call.

        Prefers the adapter's own ``_meta.claudeCode.toolName`` (``Read``,
        ``Bash``, ``Edit``, …) — that is the tool that actually ran, and naming
        it honestly beats mapping it onto a Hermes tool that did not. A declared
        name is also remembered against its ``toolCallId`` so an approval prompt
        for the same call can name the tool even though the permission request
        itself carries no ``_meta`` (see :meth:`declared_tool_name`).
        """
        declared = _declared_tool_name(payload)
        if declared:
            if call_id:
                self._declared_names[call_id] = declared
            return declared
        if existing is not None and existing.name:
            return existing.name
        kind = payload.get("kind")
        if isinstance(kind, str) and kind.strip():
            return _truncate(_scrub_text(kind.strip()), _MAX_PREVIEW_CHARS)
        return "tool"

    def _preview(self, payload: Dict[str, Any], args: Dict[str, Any]) -> Optional[str]:
        """Short human-readable line for the progress bubble."""
        title = payload.get("title")
        if isinstance(title, str) and title.strip():
            return _truncate(_scrub_text(title.strip()), _MAX_PREVIEW_CHARS)
        if args:
            try:
                return _truncate(
                    json.dumps(args, ensure_ascii=False, sort_keys=True),
                    _MAX_PREVIEW_CHARS,
                )
            except (TypeError, ValueError):
                return None
        return None

    def _project_tool_call(self, payload: Dict[str, Any]) -> ProjectedEvent:
        call_id = str(payload.get("toolCallId") or "")
        args = _as_args(payload.get("rawInput"))
        activity = ToolActivity(
            call_id=deterministic_call_id(call_id),
            name=self._tool_name(payload, None, call_id),
            args=args,
            preview=self._preview(payload, args),
            kind=str(payload.get("kind") or ""),
            status=str(payload.get("status") or "pending"),
            started_at=self._clock(),
        )
        if call_id:
            self._tools[call_id] = activity
        if activity.status in TERMINAL_TOOL_STATUSES:
            # A tool that arrives already finished (a fast call, or a coalesced
            # replay). It still owes the display an opening event, or a
            # structured tool card closes without ever having opened.
            activity.needs_start = True
            return self._finish(activity, payload)
        return ProjectedEvent(kind="tool_started", tool=activity)

    def _project_tool_call_update(self, payload: Dict[str, Any]) -> ProjectedEvent:
        call_id = str(payload.get("toolCallId") or "")
        activity = self._tools.get(call_id)
        status = str(payload.get("status") or "")
        if activity is None:
            # No preceding tool_call: the adapter may coalesce, and a replayed
            # session can start mid-call. Synthesize the activity so the update
            # is not silently dropped.
            activity = ToolActivity(
                call_id=deterministic_call_id(call_id),
                name=self._tool_name(payload, None, call_id),
                kind=str(payload.get("kind") or ""),
                status=status or "in_progress",
                started_at=self._clock(),
                needs_start=True,
            )
            if call_id:
                self._tools[call_id] = activity
        # An update carries only the fields that changed.
        if payload.get("rawInput") is not None:
            activity.args = _as_args(payload.get("rawInput"))
        if isinstance(payload.get("title"), str) or not activity.preview:
            preview = self._preview(payload, activity.args)
            if preview:
                activity.preview = preview
        if isinstance(payload.get("kind"), str) and payload["kind"]:
            activity.kind = payload["kind"]
        if status:
            activity.status = status
        if activity.status in TERMINAL_TOOL_STATUSES:
            return self._finish(activity, payload)
        return ProjectedEvent(kind="tool_updated", tool=activity)

    def _finish(self, activity: ToolActivity, payload: Dict[str, Any]) -> ProjectedEvent:
        scrubbed = _scrubbed_result_text(payload)
        activity.result = _truncate(scrubbed, _MAX_RESULT_CHARS)
        activity.is_error = activity.status == "failed"
        activity.duration = max(self._clock() - activity.started_at, 0.0)
        # One tick per completed tool call, mirroring the chat-completions
        # loop's per-iteration tick that this delegated path bypasses.
        self.tool_iterations += 1
        # ...and one transcript pair per completed tool call, from the SAME
        # event, so the row count and the iteration count can never disagree
        # about how many tools ran. Bounded from the unbounded scrubbed text
        # rather than from `activity.result`, whose bare-ellipsis display cut
        # carries no count of what it dropped.
        call_id = str(payload.get("toolCallId") or "")
        if call_id not in self._recorded_calls:
            # First outcome wins. A second terminal update for the same call
            # (`completed` then `failed`) is not something adapter 0.66.0 sends,
            # but if one arrived, appending a second pair would put two rows in
            # the transcript under one `tool_call_id` — and a pair matched by id
            # is exactly what `repair_message_sequence` and a later native-model
            # turn rely on. The display event below is unchanged: what a surface
            # does with a second completion is its own existing behaviour.
            persisted = _persisted_result_text(scrubbed)
            if activity.is_error:
                persisted = f"{_FAILED_RESULT_PREFIX} {persisted}".strip()
            self._recorded_calls.add(call_id)
            self._transcript_rows.extend(_transcript_pair(activity, persisted))
        return ProjectedEvent(kind="tool_completed", tool=activity)

    def _project_usage(self, payload: Dict[str, Any]) -> ProjectedEvent:
        cost = payload.get("cost")
        amount: Optional[float] = None
        currency = ""
        if isinstance(cost, dict):
            raw_amount = cost.get("amount")
            if isinstance(raw_amount, (int, float)) and not isinstance(raw_amount, bool):
                amount = float(raw_amount)
            raw_currency = cost.get("currency")
            if isinstance(raw_currency, str):
                currency = raw_currency
        snapshot = UsageSnapshot(
            context_used_tokens=_coerce_int(payload.get("used")),
            context_window_tokens=_coerce_int(payload.get("size")),
            cost_amount=amount,
            cost_currency=currency,
        )
        # Last-wins: several of these arrive per turn and each reports the
        # session's occupancy, so the final one is the current truth.
        self.usage = snapshot
        return ProjectedEvent(kind="usage", usage=snapshot)

    def _project_config_option_update(self, payload: Dict[str, Any]) -> ProjectedEvent:
        """State capture only (Packet 4 slice A) — never a display event.

        ``config_option_update`` fires on ANY session config change (mode,
        effort, Fast mode, model) and always restates the full list, so a
        change unrelated to the model still carries the model's current
        (unchanged) value here. Deduping against ``self.model_id`` collapses
        those restatements *within a turn*.

        The dedupe is per-turn only, and deliberately so: a projector is built
        per turn and starts knowing nothing, so the FIRST update of a turn
        always projects, even when it restates the model the session was
        already running. That costs nothing — ``model_changed`` has no display
        surface and the session writes back the value it already held — and
        seeding the projector from the session would trade a real coupling for
        no behavioural gain.
        """
        model_id = extract_current_model_id(payload.get("configOptions"))
        if not model_id or model_id == self.model_id:
            return _IGNORED
        self.model_id = model_id
        return ProjectedEvent(kind="model_changed", model_id=model_id)

    def _project_current_mode_update(self, payload: Dict[str, Any]) -> ProjectedEvent:
        """State capture only (Packet 5 slice A) — never a display event.

        The permission posture can move mid-session: the user changes it inside
        Claude Code, or the adapter drops out of a mode it cannot sustain. ACP
        announces that with its own notification kind rather than through
        ``config_option_update``, so this is a separate handler with the same
        discipline — deduped within a turn, no display surface, and read back by
        ``ClaudeAcpSession`` so ``effective_permission_mode`` describes what is
        actually in force rather than what Hermes last asked for.

        Deliberately does NOT re-negotiate. A mode Hermes did not request is a
        fact to record; answering it with another ``set_mode`` would fight the
        user's own live change, mid-turn, on the turn thread.
        """
        mode_id = extract_current_mode_id(payload)
        if not mode_id or mode_id == self.mode_id:
            return _IGNORED
        self.mode_id = mode_id
        return ProjectedEvent(kind="mode_changed", mode_id=mode_id)


def _coerce_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(value, 0)
    if isinstance(value, float):
        return max(int(value), 0)
    if isinstance(value, str):
        try:
            return max(int(value), 0)
        except ValueError:
            return 0
    return 0
