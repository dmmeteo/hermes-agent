"""Claude Code ACP runtime — one turn, delegated to a Claude Code session.

Called from ``run_conversation()`` when ``agent.api_mode == "claude_code_acp"``
(see ``agent/conversation_loop.py``). The adapter subprocess owns the model
loop and the native tools; Hermes owns the durable conversation, the
transcript, and the turn lease.

Scope through Packet 2 (see
``docs/development/claude-code-acp-runtime-plan.md``):

* one adapter process **per Hermes session**, reused across turns and reaped
  on close, retirement, idle eviction, and interpreter exit
  (``agent/claude_acp_session.py`` owns that lifecycle)
* only the **new user turn** is sent — Hermes never replays transcript
  history, which would break the adapter's own prompt cache. The single
  exception is a bounded recap after a reseed, when the Claude-side session
  could not be reattached at all
* ``agent_message_chunk`` text is assembled into the normal Hermes response

Packet 3 slice 3A adds typed event projection on top of that: tool activity
reaches the same display callbacks Hermes' own tool loop fires, assistant text
also streams, thoughts reach the reasoning surface, and token usage reaches
Hermes' usage accounting.

Slice 3B adds the permission bridge: a ``session/request_permission`` is asked
of Hermes' existing approval flow (``tools/approval.py``) instead of being
denied outright, so a write raises a real prompt on whichever surface the
session is running on. Deny is still what happens whenever that flow cannot
produce a human answer.
"""

from __future__ import annotations

import hashlib
import logging
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

from agent.claude_acp_binding import BindingStore, TurnLeaseConflict
from agent.claude_acp_session import ClaudeAcpSession, ClaudeCodeAuthRequired

if TYPE_CHECKING:  # display projection types; no import-time cost
    from agent.transports.claude_acp_event_projector import (
        PermissionRequest,
        ProjectedEvent,
        ToolActivity,
    )

logger = logging.getLogger(__name__)

__all__ = [
    "ClaudeCodeAuthRequired",
    "RESEED_MARKER",
    "RUNTIME_LABEL",
    "make_claude_acp_event_bridge",
    "make_claude_acp_permission_bridge",
    "resolve_adapter_argv",
    "run_claude_code_acp_turn",
]

# The one user-visible marker a reseed is allowed to produce. Shown once, on
# the turn that lost its Claude-side session — silently starting over would
# make the assistant look like it forgot the conversation for no reason.
RESEED_MARKER = (
    "↻ Claude Code's session for this conversation had to be restarted; "
    "continuing from a short recap."
)

# Recap bounds. The recap exists so a reseeded session is not amnesiac — it is
# NOT a transcript replay, and it must stay small enough that it can never
# become one.
_RECAP_MAX_MESSAGES = 8
_RECAP_MAX_CHARS = 2000
_RECAP_MESSAGE_CHARS = 300

# Stop reasons that mean "the model finished normally". Anything else
# (refusal, max_tokens, max_turn_requests, ...) is surfaced as a failure.
_SUCCESS_STOP_REASONS = frozenset({"end_turn"})

# How many adapter stderr lines a failure message may carry. Bounded so a
# chatty adapter cannot flood the transcript or a messaging platform.
_STDERR_DIAGNOSTIC_LINES = 5
# ...and how much of each line. A line count alone is not a bound: adapters
# routinely emit a whole JSON blob on ONE line, which would then land intact in
# a Discord/Telegram message. The head of a diagnostic carries the signal.
_STDERR_DIAGNOSTIC_LINE_CHARS = 500


def _diagnostics(turn: Any) -> str:
    """Bounded, credential-scrubbed adapter stderr for a failure message.

    Adapter stderr is the only signal for a whole class of setup failures, but
    it is also where a misconfigured adapter prints keys — so every line goes
    through the same redaction as any other user-facing text.
    """
    lines = list(getattr(turn, "stderr_tail", None) or [])
    if not lines:
        return ""
    from agent.redact import redact_sensitive_text

    cleaned = []
    for line in lines[-_STDERR_DIAGNOSTIC_LINES:]:
        text = line.strip()
        if not text:
            continue
        try:
            text = redact_sensitive_text(text, force=True)
        except Exception:
            # Never surface an unscrubbed line if redaction itself failed.
            continue
        # Truncate AFTER redaction: cutting first could split a secret into a
        # prefix the redactor no longer recognises.
        if len(text) > _STDERR_DIAGNOSTIC_LINE_CHARS:
            text = text[:_STDERR_DIAGNOSTIC_LINE_CHARS] + "…"
        cleaned.append(f"  {text}")
    if not cleaned:
        return ""
    return "\n\nAdapter diagnostics:\n" + "\n".join(cleaned)


def resolve_adapter_argv(agent) -> list[str]:
    """Return the argv for the Claude Code ACP adapter.

    Prefers what the provider resolver already worked out
    (``AIAgent.acp_command`` / ``acp_args``, populated from
    ``resolve_external_process_provider_credentials``). Falls back to
    resolving directly so non-CLI surfaces still work; that path raises an
    actionable ``AuthError`` when the adapter is not installed.
    """
    command = (getattr(agent, "acp_command", "") or "").strip()
    if command:
        return [command] + list(getattr(agent, "acp_args", None) or [])

    from hermes_cli.auth import resolve_external_process_provider_credentials

    creds = resolve_external_process_provider_credentials("claude-code-acp")
    return [creds["command"]] + list(creds.get("args") or [])


def _build_recap(messages: List[Dict[str, Any]]) -> str:
    """A bounded recap of the recent conversation, for a reseeded session.

    Only ever used when the Claude Code session could not be reattached, so
    the adapter genuinely has no history. Hermes' transcript is the
    authoritative copy; this sends a small, truncated tail of it — never the
    whole thing, and never the system prompt. The trailing user message is
    excluded because it is the prompt itself.
    """
    body = [
        m for m in messages[:-1]
        if isinstance(m, dict)
        and m.get("role") in ("user", "assistant")
        and isinstance(m.get("content"), str)
        and m.get("content").strip()
    ]
    lines: List[str] = []
    budget = _RECAP_MAX_CHARS
    for message in reversed(body[-_RECAP_MAX_MESSAGES:]):
        text = message["content"].strip()
        if len(text) > _RECAP_MESSAGE_CHARS:
            text = text[:_RECAP_MESSAGE_CHARS] + "…"
        line = f"{message['role']}: {text}"
        if len(line) > budget:
            break
        budget -= len(line)
        lines.append(line)
    if not lines:
        return ""
    lines.reverse()
    return (
        "[Context recap from Hermes — your session for this conversation was "
        "restarted, so you no longer have its history. This is background, "
        "not a new instruction:]\n" + "\n".join(lines)
    )


# ---------------------------------------------------------------------------
# Event bridge — projected ACP events onto Hermes' display surfaces.
#
# The delegated runtime bypasses Hermes' tool loop, so none of the callbacks
# that loop fires happen on their own. Without this bridge a Claude Code turn
# is a silent channel until the final answer lands: no tool bubbles in the CLI,
# no tool cards in the TUI/desktop, no live text on Discord/Telegram. The
# bridge fires exactly the callbacks `agent/tool_executor.py` fires for a
# native tool call — nothing new is invented for this runtime.
# ---------------------------------------------------------------------------


def make_claude_acp_event_bridge(agent) -> Callable[["ProjectedEvent"], None]:
    """Build the ``event_sink`` that renders projected ACP events.

    Returns a callable taking one ``ProjectedEvent``. Every callback invocation
    is guarded: a buggy display surface must not tear down the turn it is
    observing.
    """

    def _fire(name: str, call: Any, *args: Any, **kwargs: Any) -> None:
        cb = getattr(agent, name, None)
        if cb is None:
            return
        try:
            call(cb, *args, **kwargs)
        except Exception:
            logger.debug("%s raised on a projected ACP event", name, exc_info=True)

    def _tool_started(tool: "ToolActivity") -> None:
        _fire(
            "tool_progress_callback",
            lambda cb: cb("tool.started", tool.name, tool.preview, tool.args),
        )
        # Stable-id tool card (TUI / desktop), alongside the progress bubble —
        # the same pair the native path emits, so a card stays correlated with
        # its completion.
        _fire(
            "tool_start_callback",
            lambda cb: cb(tool.call_id, tool.name, tool.args),
        )

    def _tool_completed(tool: "ToolActivity") -> None:
        if tool.needs_start:
            # Never seen as a `tool_call`: open the card before closing it.
            _tool_started(tool)
        _fire(
            "tool_progress_callback",
            lambda cb: cb(
                "tool.completed", tool.name, None, None,
                duration=tool.duration, is_error=tool.is_error, result=tool.result,
            ),
        )
        _fire(
            "tool_complete_callback",
            lambda cb: cb(tool.call_id, tool.name, tool.args, tool.result),
        )

    def sink(event: "ProjectedEvent") -> None:
        kind = event.kind
        if kind == "text":
            _fire("_fire_stream_delta", lambda cb: cb(event.text))
        elif kind == "thought":
            _fire("_fire_reasoning_delta", lambda cb: cb(event.text))
        elif kind == "tool_started":
            _tool_started(event.tool)
        elif kind == "tool_completed":
            _tool_completed(event.tool)
        # "tool_updated" (a non-terminal status change) and "usage" have no
        # live Hermes surface: usage is recorded once at turn end, and Hermes'
        # tool display is a started/completed pair.

    return sink


# ---------------------------------------------------------------------------
# Permission bridge — an ACP permission request onto Hermes' approval flow.
#
# The adapter owns its own tools, so Hermes' tool loop (and with it
# `check_all_command_guards`) never sees the write. What Hermes does see is the
# adapter asking, because `session/set_mode` → `default` keeps the adapter's
# self-approving classifier out of the decision. This bridge takes that request
# to the SAME gate a dangerous shell command goes through, which is what makes
# the prompt appear on whichever surface the session is running on — CLI panel,
# Discord/Telegram buttons, TUI/desktop prompt — with no per-surface work here.
#
# Nothing about the policy lives in this module: mode, allowlists, timeout, and
# the fail-closed rule are all `tools/approval.py`'s.
# ---------------------------------------------------------------------------

#: How the runtime names itself in an approval prompt.
RUNTIME_LABEL = "Claude Code"


def make_claude_acp_permission_bridge(
    before_prompt: Optional[Callable[[], None]] = None,
) -> Callable[["PermissionRequest"], bool]:
    """Build the ``permission_handler`` that asks the user about one action.

    Returns a callable taking a projected ``PermissionRequest`` and returning
    True only when the human gate approved it. Called synchronously on the turn
    thread while the adapter waits, which is what lets the gate resolve
    per-thread and per-context state on its own — the CLI approval callback, the
    gateway session key, the interrupt flag. Takes no agent for exactly that
    reason: there is nothing about this decision that Hermes' approval flow does
    not already resolve from the ambient session.

    ``before_prompt`` runs immediately before the user is asked. It exists for
    one job — releasing text the streaming scrubbers are still holding — and is
    a bare callable rather than an agent handle so that job cannot grow into
    "the bridge knows about the agent".
    """

    def handler(request: "PermissionRequest") -> bool:
        if before_prompt is not None:
            # A prompt drawn while the scrubber holds a trailing partial tag
            # asks the user about text they have not been shown; the tail then
            # lands after their answer, glued to whatever came next.
            try:
                before_prompt()
            except Exception:
                logger.debug("pre-approval stream flush raised", exc_info=True)

        from tools.approval import request_delegated_runtime_approval

        decision = request_delegated_runtime_approval(
            RUNTIME_LABEL,
            request.tool_name,
            request.description,
            # Grain the allowlist on WHAT was asked, so an `[a]lways` answer
            # covers this action asked again and nothing else.
            rule_key=hashlib.sha256(
                request.signature.encode("utf-8")
            ).hexdigest()[:16],
            # The unconditional floor — the hardline blocklist and the user's
            # `approvals.deny` rules — is measured on what the runtime DECLARED,
            # not on the scrubbed/truncated prompt text. Without these two
            # arguments the gate's yolo bypass would auto-approve a command
            # Hermes' own shell tool refuses outright.
            declared_command=request.declared_command,
            declared_paths=request.declared_paths,
        )
        approved = bool(decision.get("approved"))
        if not approved:
            # The gate's message is written for the model, and the model here is
            # inside the adapter — which only ever learns "denied" from the
            # protocol. Log it so the refusal is at least diagnosable.
            logger.info(
                "Claude Code ACP: %s was not approved — %s",
                request.tool_name, decision.get("message") or "no reason given",
            )
        return approved

    return handler


def _flush_stream_surface(agent) -> None:
    """Release any text the streaming scrubbers are holding back.

    The scrubbers hold a trailing partial tag (a bare ``<`` that might open
    ``<think>``) until the next delta resolves it. On this path there is no
    next delta, and a surface that already streamed the reply does not
    re-render it — so an unflushed tail is text the user never sees.
    """
    flush = getattr(agent, "_reset_stream_delivery_tracking", None)
    if flush is None:
        return
    try:
        flush()
    except Exception:
        logger.debug("stream delivery flush raised", exc_info=True)


# ---------------------------------------------------------------------------
# Usage accounting.
#
# ACP reports usage twice over, and the two are NOT interchangeable:
#
# * the ``session/prompt`` reply carries ``usage`` — this turn's token counts
#   (input / output / cached read / cached write / total). Authoritative.
# * ``usage_update`` notifications carry ``used`` + ``size`` + ``cost`` —
#   how much of the context window the session now occupies. Several arrive per
#   turn and each restates occupancy, so summing them into token counters
#   would inflate session accounting by orders of magnitude.
#
# Tokens therefore come from the reply, occupancy is reported as its own pair
# of fields, and the projector keeps only the last occupancy snapshot.
# ---------------------------------------------------------------------------


def _coerce_usage_int(value: Any) -> int:
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


def _record_usage(agent, outcome: Any) -> Dict[str, Any]:
    """Project one turn's ACP usage into Hermes' accounting.

    Mirrors ``_record_codex_app_server_usage`` (session counters, cost
    estimation, the SessionDB token queue) with two deliberate differences:

    * ``session_api_calls`` is ticked by the caller, not here, so the count
      stays correct on the failure paths that never reach this function.
    * Hermes' **context compressor is not driven**. Claude Code owns its own
      context window and compacts it itself, and Hermes never replays its
      transcript into the adapter — so feeding the compressor would rewrite
      Hermes' past context (the one cache-invalidating operation we allow) to
      solve a problem that lives on the other side of the boundary. This
      resolves Packet 2's R5 by decision rather than leaving it implicit.
    """
    usage = getattr(outcome, "usage", None)
    context_usage = getattr(outcome, "context_usage", None)

    result: Dict[str, Any] = {}
    if context_usage is not None:
        result["context_used_tokens"] = context_usage.context_used_tokens
        result["context_window_tokens"] = context_usage.context_window_tokens
        if context_usage.cost_amount is not None:
            # Reported, never added to the session cost: the adapter's figure is
            # what the turn would have cost on API billing, while a
            # subscription turn costs the user nothing extra.
            result["adapter_reported_cost_usd"] = context_usage.cost_amount
            result["adapter_reported_cost_currency"] = context_usage.cost_currency

    if not isinstance(usage, dict) or not usage:
        # No usage for this turn: still persist the api-call tick so session
        # accounting does not lose the turn entirely.
        _queue_token_counts(agent, None, None)
        return result

    from agent.usage_pricing import CanonicalUsage, estimate_usage_cost

    canonical = CanonicalUsage(
        input_tokens=_coerce_usage_int(usage.get("inputTokens")),
        output_tokens=_coerce_usage_int(usage.get("outputTokens")),
        cache_read_tokens=_coerce_usage_int(usage.get("cachedReadTokens")),
        cache_write_tokens=_coerce_usage_int(usage.get("cachedWriteTokens")),
        reasoning_tokens=_coerce_usage_int(usage.get("thoughtTokens")),
        raw_usage=usage,
    )
    reported_total = _coerce_usage_int(usage.get("totalTokens"))
    prompt_tokens = canonical.prompt_tokens
    completion_tokens = canonical.output_tokens
    total_tokens = reported_total or canonical.total_tokens
    usage_dict = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "input_tokens": canonical.input_tokens,
        "output_tokens": canonical.output_tokens,
        "cache_read_tokens": canonical.cache_read_tokens,
        "cache_write_tokens": canonical.cache_write_tokens,
        "reasoning_tokens": canonical.reasoning_tokens,
    }

    agent.session_prompt_tokens += prompt_tokens
    agent.session_completion_tokens += completion_tokens
    agent.session_total_tokens += total_tokens
    agent.session_input_tokens += canonical.input_tokens
    agent.session_output_tokens += canonical.output_tokens
    agent.session_cache_read_tokens += canonical.cache_read_tokens
    agent.session_cache_write_tokens += canonical.cache_write_tokens
    agent.session_reasoning_tokens += canonical.reasoning_tokens

    cost_result = estimate_usage_cost(
        agent.model,
        canonical,
        provider=agent.provider,
        base_url=agent.base_url,
        api_key=getattr(agent, "api_key", ""),
    )
    if cost_result.amount_usd is not None:
        agent.session_estimated_cost_usd += float(cost_result.amount_usd)
    agent.session_cost_status = cost_result.status
    agent.session_cost_source = cost_result.source

    _queue_token_counts(agent, canonical, cost_result)

    result.update(usage_dict)
    result["last_prompt_tokens"] = prompt_tokens
    result["estimated_cost_usd"] = (
        float(cost_result.amount_usd) if cost_result.amount_usd is not None else None
    )
    result["cost_status"] = cost_result.status
    result["cost_source"] = cost_result.source
    return result


def _queue_token_counts(agent, canonical: Any, cost_result: Any) -> None:
    """Hand this turn's counts to the SessionDB background writer."""
    if not getattr(agent, "_session_db", None) or not getattr(agent, "session_id", ""):
        return
    try:
        if not agent._session_db_created:
            agent._ensure_db_session()
        if canonical is None:
            agent._session_db.queue_token_counts(
                agent.session_id,
                model=agent.model,
                billing_provider=agent.provider,
                billing_base_url=agent.base_url,
                billing_mode="subscription_included",
                api_call_count=1,
            )
            return
        agent._session_db.queue_token_counts(
            agent.session_id,
            input_tokens=canonical.input_tokens,
            output_tokens=canonical.output_tokens,
            cache_read_tokens=canonical.cache_read_tokens,
            cache_write_tokens=canonical.cache_write_tokens,
            reasoning_tokens=canonical.reasoning_tokens,
            estimated_cost_usd=float(cost_result.amount_usd)
            if cost_result.amount_usd is not None else None,
            cost_status=cost_result.status,
            cost_source=cost_result.source,
            billing_provider=agent.provider,
            billing_base_url=agent.base_url,
            billing_mode="subscription_included"
            if cost_result.status == "included" else None,
            model=agent.model,
            api_call_count=1,
        )
    except Exception as exc:
        logger.debug(
            "Claude Code ACP token persistence failed (session=%s): %s",
            getattr(agent, "session_id", None), exc,
        )


def _get_or_create_session(
    agent, argv: list[str], cwd: str, additional_directories: list[str]
) -> ClaudeAcpSession:
    """The one live ACP session for this Hermes conversation.

    Recreated only when the adapter argv, the workspace, or the negotiated
    workspace roots changed — each means the existing adapter is no longer the
    right process to be talking to (the roots go out on ``session/new``, so a
    live adapter cannot be re-scoped in place), and leaving it running would
    break the one-adapter-per-session invariant.
    """
    session = getattr(agent, "_claude_acp_session", None)
    if (
        session is not None
        and not session.closed
        and session.matches(argv, cwd, additional_directories)
    ):
        return session
    if session is not None:
        try:
            session.close()
        except Exception:
            logger.debug("closing the previous ACP session failed", exc_info=True)
    session = ClaudeAcpSession(
        argv=argv,
        cwd=cwd,
        additional_directories=additional_directories,
        store=BindingStore.for_agent(agent),
        # A persistence-isolated fork shares the parent's session_id, so
        # keying the registry on it would retire the user's live adapter out
        # from under the conversation. Forks get their own anonymous slot.
        hermes_session_id=(
            ""
            if getattr(agent, "_persist_disabled", False)
            else str(getattr(agent, "session_id", "") or "")
        ),
    )
    agent._claude_acp_session = session
    return session


def run_claude_code_acp_turn(
    agent,
    *,
    user_message: str,
    original_user_message: Any,
    messages: List[Dict[str, Any]],
    effective_task_id: str,
    should_review_memory: bool = False,
) -> Dict[str, Any]:
    """Run one Hermes turn through Claude Code over ACP.

    Returns the same dict shape as the chat_completions path.
    """
    from agent.runtime_cwd import resolve_agent_cwd

    cwd = getattr(agent, "session_cwd", None) or str(resolve_agent_cwd())

    try:
        argv = resolve_adapter_argv(agent)
    except Exception as exc:
        # Missing adapter is a setup problem, not a turn failure — surface the
        # actionable message rather than a stack trace or a hang.
        logger.warning("Claude Code ACP adapter unavailable: %s", exc)
        return _failure_result(agent, messages, str(exc))

    # Workspace containment is negotiated at session creation, never inferred:
    # the cwd, plus only the directories the user explicitly allowed in
    # config.yaml. See agent/claude_acp_workspace.py.
    from agent.claude_acp_workspace import resolve_additional_directories

    session = _get_or_create_session(
        agent, argv, cwd, resolve_additional_directories(cwd)
    )

    try:
        outcome = session.run_turn(
            user_message,
            recap_provider=lambda: _build_recap(messages),
            # An interrupt that landed BEFORE this turn started has nothing to
            # cancel yet: AIAgent.interrupt() finds no in-flight turn and the
            # request would be lost, leaving the adapter to run to the full
            # turn timeout while the user believes they stopped it. Carry the
            # latched flag into the turn so the cancel goes out as soon as a
            # session exists.
            cancel_pending=bool(getattr(agent, "_interrupt_requested", False)),
            event_sink=make_claude_acp_event_bridge(agent),
            # Replaces Packet 1's blanket denial. Absent this handler the
            # session still denies every request, so the fail-closed default
            # survives any future caller that forgets to pass it.
            permission_handler=make_claude_acp_permission_bridge(
                before_prompt=lambda: _flush_stream_surface(agent),
            ),
        )
    except TurnLeaseConflict as exc:
        # Refuse rather than spawning a second adapter against a session
        # another process is already driving — that is how one turn gets
        # delivered twice.
        logger.warning("Claude Code ACP turn lease conflict: %s", exc)
        return _failure_result(agent, messages, f"Claude Code: {exc}")
    except FileNotFoundError as exc:
        logger.warning("Claude Code ACP adapter could not be spawned: %s", exc)
        from hermes_cli.auth import CLAUDE_CODE_ACP_PACKAGE

        return _failure_result(
            agent,
            messages,
            f"Could not find the Claude Code ACP adapter '{argv[0]}'. "
            f"Install it with: npm install -g {CLAUDE_CODE_ACP_PACKAGE} "
            f"(or set model.claude_code_acp.command in config.yaml).",
        )
    except ClaudeCodeAuthRequired as exc:
        # A setup problem, not a turn failure: say exactly what to run.
        logger.warning("Claude Code ACP authentication required")
        return _failure_result(agent, messages, f"{exc}{_diagnostics(session)}")
    except Exception as exc:
        logger.exception("Claude Code ACP turn failed")
        return _failure_result(
            agent, messages,
            f"Claude Code ACP turn failed: {exc}{_diagnostics(session)}",
        )

    # Streaming ended with this turn: release whatever the scrubbers held back.
    _flush_stream_surface(agent)

    final_text = outcome.text
    stop_reason = outcome.stop_reason
    cancelled = stop_reason == "cancelled" or bool(
        getattr(agent, "_interrupt_requested", False)
    )

    # Mirror the conversation-loop finalizer's interrupt handoff: this early
    # return bypasses it. Key off Hermes' own latched flag, NOT the wire stop
    # reason — `session/cancel` is fire-and-forget, so a cancel that lands
    # while the adapter is already finishing returns `end_turn`. Keying off
    # the stop reason there would leave both `_interrupt_requested` and the
    # per-thread tool-abort flag latched, and the user's NEXT message would be
    # dropped before a single API call. (Matches codex_runtime, which keys off
    # its runtime-owned flag rather than the wire.)
    _user_interrupted = bool(getattr(agent, "_interrupt_requested", False))
    _interrupt_message = (
        getattr(agent, "_interrupt_message", None) if _user_interrupted else None
    )
    if _user_interrupted:
        agent.clear_interrupt()

    # A terminal-but-unsuccessful stop reason (refusal, max_tokens,
    # max_turn_requests, ...) must not be reported as success with an empty
    # body: run_conversation() already appended the user message, so returning
    # nothing leaves the transcript ending on a `user` row and the next turn
    # appends a second consecutive one. Always emit a real assistant message.
    failed_stop = stop_reason not in _SUCCESS_STOP_REASONS and not cancelled
    error: Optional[str] = None
    if failed_stop:
        error = f"Claude Code ended the turn with stopReason={stop_reason!r}"
        if not final_text:
            final_text = f"Claude Code ended the turn without a response ({stop_reason})."
    elif not final_text:
        # Includes the cancelled path: interrupting before the first
        # agent_message_chunk arrives would otherwise append nothing, leaving
        # the transcript ending on the user row that run_conversation() already
        # added — and the next turn would append a second consecutive one.
        if cancelled:
            final_text = "(interrupted before Claude Code produced a response)"
        else:
            final_text = "Claude Code returned an empty response."
            error = "empty response from Claude Code"

    # Exactly one visible marker, on the turn whose Claude-side session was
    # rebuilt. It rides inside the assistant message rather than being a
    # message of its own, so role alternation is untouched.
    if outcome.reseeded:
        final_text = f"{RESEED_MARKER}\n\n{final_text}" if final_text else RESEED_MARKER

    # The inbound user message was already appended by run_conversation()
    # before the early return, so only the reply is added here.
    if final_text:
        messages.append({"role": "assistant", "content": final_text})
        _persist(agent, messages)

    # A fork lives for exactly one turn, so its adapter is per-turn state:
    # reap it now rather than holding a Claude Code process open until exit.
    if getattr(agent, "_persist_disabled", False):
        try:
            session.close()
        except Exception:
            logger.debug("closing a forked ACP session failed", exc_info=True)
        agent._claude_acp_session = None

    agent.session_api_calls = getattr(agent, "session_api_calls", 0) + 1
    usage_result = _record_usage(agent, outcome)
    _run_improvement_hooks(
        agent,
        original_user_message=original_user_message,
        final_text=final_text,
        messages=messages,
        should_review_memory=should_review_memory,
        succeeded=not cancelled and error is None,
        tool_iterations=outcome.tool_iterations,
    )

    return {
        "final_response": final_text,
        "messages": messages,
        "api_calls": 1,
        "completed": not cancelled and error is None,
        "partial": cancelled or error is not None,
        "interrupted": _user_interrupted,
        **({"interrupt_message": _interrupt_message} if _interrupt_message else {}),
        **_steer_handoff(agent),
        "error": error,
        # We flushed the assistant message ourselves above, so the gateway
        # must not write it again — append_message has no dedup and a second
        # write would duplicate the already-flushed user turn (#860/#42039).
        "agent_persisted": True,
        **usage_result,
    }


def _run_improvement_hooks(
    agent,
    *,
    original_user_message: Any,
    final_text: str,
    messages: List[Dict[str, Any]],
    should_review_memory: bool,
    succeeded: bool,
    tool_iterations: int = 0,
) -> None:
    """Delegated-runtime parity for the agent-improvement loop.

    The codex app-server runtime performs exactly these four steps after a
    turn (``agent/codex_runtime.py``). Without them, selecting Claude Code
    silently disables skill nudges, external-memory sync, and background
    review for the whole session.

    The skill counter advances by the number of tool calls the adapter actually
    made (codex does the same with its own ``tool_iterations``), falling back to
    one for a turn that used no tools — the nudge gate is "iterations since the
    last skill review", and a 30-tool turn is not one iteration of work.
    """
    agent._iters_since_skill = (
        getattr(agent, "_iters_since_skill", 0) + max(tool_iterations, 1)
    )

    should_review_skills = False
    try:
        if (
            getattr(agent, "_skill_nudge_interval", 0) > 0
            and agent._iters_since_skill >= agent._skill_nudge_interval
            and "skill_manage" in getattr(agent, "valid_tool_names", ())
        ):
            should_review_skills = True
            agent._iters_since_skill = 0
    except Exception:
        logger.debug("skill nudge check raised", exc_info=True)

    # Skipped on interrupt/error so partial transcripts never reach memory.
    if succeeded:
        try:
            agent._sync_external_memory_for_turn(
                original_user_message=original_user_message,
                final_response=final_text,
                interrupted=False,
                messages=messages,
            )
        except Exception:
            logger.debug("external memory sync raised", exc_info=True)

    if final_text and succeeded and (should_review_memory or should_review_skills):
        try:
            agent._spawn_background_review(
                messages_snapshot=list(messages),
                review_memory=should_review_memory,
                review_skills=should_review_skills,
            )
        except Exception:
            logger.debug("background review spawn raised", exc_info=True)


def _steer_handoff(agent) -> Dict[str, Any]:
    """Hand a pending ``/steer`` back to the caller, or ``{}``.

    ``AIAgent.steer()`` returns True and parks the text in ``_pending_steer``,
    and every surface reports that as accepted. On the chat-completions path the
    text is drained onto the next tool result; on THIS path there is no next tool
    result — the adapter owns its own loop — and all three drains
    (``conversation_loop``, the tool-result applier, ``turn_finalizer``) sit
    behind the delegated early return. So the guidance would be dropped after
    the user was told it landed.

    ``result["pending_steer"]`` is the finalizer's own answer for exactly this
    shape ("a steer with no tool batch left to drain into"), and the CLI, the
    gateway and the TUI already deliver that key as the next user turn. Reusing
    it keeps role alternation intact — the text becomes a real next turn, never
    a synthetic message spliced into this one.
    """
    try:
        leftover = agent._drain_pending_steer()
    except Exception:
        logger.debug("draining a pending steer raised", exc_info=True)
        return {}
    return {"pending_steer": leftover} if leftover else {}


def _persist(agent, messages: List[Dict[str, Any]]) -> None:
    """Flush projected messages; this path bypasses the loop's persistence."""
    if getattr(agent, "_session_db", None) is None:
        return
    try:
        if agent._flush_messages_to_session_db(messages) is False:
            logger.warning(
                "Claude Code ACP turn was delivered but could NOT be persisted "
                "to the session DB (session=%s) — it will be missing after "
                "restart/resume",
                getattr(agent, "session_id", None),
            )
    except Exception:
        logger.warning("Claude Code ACP message flush failed", exc_info=True)


def _failure_result(
    agent, messages: List[Dict[str, Any]], message: str
) -> Dict[str, Any]:
    """Uniform failure shape that also clears a latched interrupt."""
    # A failure can land after some text already streamed; release whatever the
    # scrubbers are still holding so the surface is not left mid-delta.
    _flush_stream_surface(agent)
    _user_interrupted = bool(getattr(agent, "_interrupt_requested", False))
    _interrupt_message = (
        getattr(agent, "_interrupt_message", None) if _user_interrupted else None
    )
    if _user_interrupted:
        agent.clear_interrupt()
    return {
        "final_response": message,
        "messages": messages,
        "api_calls": 0,
        "completed": False,
        "partial": True,
        "interrupted": _user_interrupted,
        **({"interrupt_message": _interrupt_message} if _interrupt_message else {}),
        # A setup/protocol failure must not eat the user's guidance either.
        **_steer_handoff(agent),
        "error": message,
    }
