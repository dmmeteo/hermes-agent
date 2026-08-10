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

Two rules make the "no raw payload in the reply" invariant structural rather
than incidental:

* **assistant text is an allow-list of exactly one event kind.** Anything new
  the adapter starts emitting — a replayed ``user_message_chunk``, a plan, a
  slash-command catalogue — is ignored by default, not appended to the reply.
* **every field that leaves this module is credential-scrubbed**, because tool
  payloads are the one place a real key legitimately appears (a ``Bash``
  command line, an edited ``.env``) and every projected field is display-bound.

Scrubbing runs before truncation: cutting first can split a secret into a
prefix the redactor no longer recognises.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "ClaudeAcpEventProjector",
    "PermissionRequest",
    "ProjectedEvent",
    "ToolActivity",
    "UsageSnapshot",
    "deterministic_call_id",
    "project_permission_request",
]

#: Statuses that end a tool call. Anything else is progress, not an outcome.
TERMINAL_TOOL_STATUSES = frozenset({"completed", "failed"})

#: The only update kind whose text is the assistant's reply.
ASSISTANT_TEXT_UPDATE = "agent_message_chunk"

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


def _result_text(payload: Dict[str, Any]) -> str:
    """Scrubbed, bounded outcome text for a finished tool call."""
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
    return _truncate(_scrub_text(text), _MAX_RESULT_CHARS)


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

    Display-bound and already scrubbed: this is the record that becomes the text
    of a CLI prompt or a Discord/Telegram approval message, so nothing on it may
    carry a credential or an unbounded payload. The protocol ``options`` are
    deliberately NOT here — answering the wire is the session's job, and the
    approval surface has no business choosing an ``optionId``.
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
    #: Stable-per-request identity for Hermes' allowlist grain: two requests that
    #: describe the same action share it, so ``[a]lways`` cannot blanket a
    #: different action on the same tool.
    signature: str = ""

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
            parts.append(
                "outside the workspace: " + ", ".join(self.outside_roots)
            )
        return "; ".join(parts) or f"{self.tool_name} requested permission"


def project_permission_request(
    params: Dict[str, Any], roots: Optional[List[str]] = None
) -> PermissionRequest:
    """Project ``session/request_permission`` params into a display record.

    Every field is credential-scrubbed and bounded before truncation, exactly as
    for a projected update — an approval prompt is user-visible text, and on a
    messaging platform it leaves the machine.

    ``roots`` are the session's negotiated workspace directories. When supplied,
    declared paths are classified against them so the prompt can say which ones
    fall outside. Classification only: nothing here opens, reads, or creates a
    path, and a path outside the roots is still shown to the user rather than
    silently dropped.
    """
    tool_call = params.get("toolCall")
    if not isinstance(tool_call, dict):
        tool_call = {}
    title_raw = tool_call.get("title")
    title = (
        _truncate(_scrub_text(title_raw.strip()), _MAX_PREVIEW_CHARS)
        if isinstance(title_raw, str) and title_raw.strip()
        else ""
    )
    kind_raw = tool_call.get("kind")
    kind = (
        _truncate(_scrub_text(kind_raw.strip()), _MAX_PREVIEW_CHARS)
        if isinstance(kind_raw, str) and kind_raw.strip()
        else ""
    )
    name = _declared_tool_name(tool_call) or kind or "tool"
    args = _as_args(tool_call.get("rawInput"))
    pairs = _location_pairs(tool_call)
    declared = [display for _raw, display in pairs]
    # Display is bounded; identity is not. A tool touching 400 files must not
    # turn the prompt into a wall of paths, but the paths it does not show still
    # have to reach the signature — otherwise two different mass edits sharing
    # their first eight paths would share one remembered grant.
    shown = declared[:_MAX_LOCATIONS]
    outside: tuple = ()
    if roots:
        from agent.claude_acp_workspace import is_within_roots

        outside = tuple(
            display for raw, display in pairs[:_MAX_LOCATIONS]
            if not is_within_roots(raw, roots)
        )
    # Identity is derived from WHAT is being asked, never from the adapter's
    # per-call ``toolCallId``: a remembered grant must apply to the same action
    # asked again, and must not apply to a different one.
    try:
        signature = json.dumps(
            {"tool": name, "kind": kind, "title": title,
             "locations": declared, "args": args},
            ensure_ascii=False, sort_keys=True, default=str,
        )
    except (TypeError, ValueError):
        signature = f"{name}|{kind}|{title}|{declared}"
    return PermissionRequest(
        tool_name=name,
        title=title,
        kind=kind,
        args=args,
        locations=shown,
        omitted_locations=len(declared) - len(shown),
        outside_roots=outside,
        signature=signature,
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
        self.tool_iterations = 0
        self.usage: Optional[UsageSnapshot] = None

    # ---------- accumulated turn state ----------

    @property
    def assistant_text(self) -> str:
        """The reply, assembled from assistant text chunks only."""
        return "".join(self._text_parts)

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
        # Deliberately ignored, and deliberately the default: user_message_chunk
        # (our own prompt echoed back, or history a session/load replayed),
        # plan/plan_update, available_commands_update, session_info_update,
        # current_mode_update, config_option_update, and anything a future
        # adapter version adds. None of it is the assistant's reply.
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

    def _tool_name(self, payload: Dict[str, Any], existing: Optional[ToolActivity]) -> str:
        """Display name for a tool call.

        Prefers the adapter's own ``_meta.claudeCode.toolName`` (``Read``,
        ``Bash``, ``Edit``, …) — that is the tool that actually ran, and naming
        it honestly beats mapping it onto a Hermes tool that did not.
        """
        declared = _declared_tool_name(payload)
        if declared:
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
            name=self._tool_name(payload, None),
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
                name=self._tool_name(payload, None),
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
        activity.result = _result_text(payload)
        activity.is_error = activity.status == "failed"
        activity.duration = max(self._clock() - activity.started_at, 0.0)
        # One tick per completed tool call, mirroring the chat-completions
        # loop's per-iteration tick that this delegated path bypasses.
        self.tool_iterations += 1
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
