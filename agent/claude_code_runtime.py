"""Claude Code ACP runtime — one turn, delegated to a Claude Code session.

Called from ``run_conversation()`` when ``agent.api_mode == "claude_code_acp"``
(see ``agent/conversation_loop.py``). The adapter subprocess owns the model
loop and the native tools; Hermes owns the durable conversation, the
transcript, and the turn lease.

Packet 1 scope — deliberately narrow (see
``docs/development/claude-code-acp-runtime-plan.md``):

* one adapter process **per turn**, reaped on success, error, and interrupt
* only the **new user turn** is sent — Hermes never replays transcript
  history, which would break the adapter's own prompt cache
* ``agent_message_chunk`` text is assembled into the normal Hermes response;
  tool and thought events are consumed but not projected

Session reuse, rich tool projection, and permission UX are Packets 2 and 3.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# The adapter drives its own agentic loop, so a turn can legitimately run for
# a long time. Matches the copilot ACP client's ceiling.
_TURN_TIMEOUT_SECONDS = 900.0

# Permission mode Hermes negotiates for every session. NOT the adapter's
# default (`auto`), which self-approves via a classifier.
_PERMISSION_MODE = "default"

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


class ClaudeCodeAuthRequired(RuntimeError):
    """The adapter has no usable Claude credential."""


def _diagnostics(turn: Optional["ClaudeAcpTurn"]) -> str:
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


class ClaudeAcpTurn:
    """One in-flight ACP turn. Owns the adapter process for its lifetime."""

    def __init__(self, argv: list[str], cwd: str) -> None:
        self._argv = argv
        self._cwd = cwd
        self._client = None
        self._session_id: Optional[str] = None
        self._cancel_requested = threading.Event()
        self._lock = threading.Lock()
        self.text_parts: list[str] = []
        self.denied_permissions = 0
        # Captured before the client is closed so failure paths can surface
        # bounded, scrubbed diagnostics.
        self.stderr_tail: list[str] = []

    # -- cancellation -----------------------------------------------------

    def request_cancel(self) -> None:
        """Ask the adapter to abort. Safe to call from another thread.

        Cancellation is a protocol notification, so the pending
        ``session/prompt`` still settles — with ``stopReason == "cancelled"``.
        """
        self._cancel_requested.set()
        with self._lock:
            client, session_id = self._client, self._session_id
        if client is not None and session_id:
            try:
                client.cancel(session_id)
            except Exception:
                logger.debug("session/cancel failed", exc_info=True)

    @property
    def cancel_requested(self) -> bool:
        return self._cancel_requested.is_set()

    def _reissue_cancel_if_pending(self) -> None:
        """Re-send a cancel once the prompt is genuinely in flight.

        A `session/cancel` delivered before any prompt existed is something a
        real adapter may legitimately ignore — there was no turn to cancel.
        Without this the user's stop would be silently dropped and the turn
        would run to natural completion. Safe to send twice: cancellation is
        idempotent, and the turn still settles through the pending prompt
        reply.
        """
        if not self._cancel_requested.is_set():
            return
        with self._lock:
            client, session_id = self._client, self._session_id
        if client is not None and session_id:
            try:
                client.cancel(session_id)
            except Exception:
                logger.debug("cancel re-issue failed", exc_info=True)

    # -- the turn ---------------------------------------------------------

    def run(self, user_message: str) -> Dict[str, Any]:
        """Spawn, handshake, run one prompt, and always reap the child."""
        from agent.transports.claude_acp import ClaudeAcpClient

        client = ClaudeAcpClient(command=self._argv, cwd=self._cwd)
        with self._lock:
            self._client = client
        try:
            client.initialize()
            # An adapter with no usable credential advertises the auth methods
            # it wants completed first. Without this check the user just sees
            # session/new fail with an opaque ACP error — the most likely first
            # failure on a headless host.
            if client.auth_methods:
                raise ClaudeCodeAuthRequired(
                    "Claude Code is not authenticated. Run `claude` once to log "
                    "in, or on a headless host create a subscription token with "
                    "`claude setup-token` and set CLAUDE_CODE_OAUTH_TOKEN."
                )
            session_id = client.session_new()
            with self._lock:
                self._session_id = session_id

            # SECURITY: the adapter starts in `auto`, where a classifier
            # approves writes and shell commands without ever asking Hermes.
            # Move it to `default` before any prompt runs so every decision
            # comes back to us as session/request_permission. Until the rich
            # approval UX lands (Packet 3) we answer every one of them with a
            # denial, so an unattended turn cannot mutate the workspace.
            client.set_mode(session_id, _PERMISSION_MODE)

            # A cancel that landed during the handshake must not be lost.
            if self._cancel_requested.is_set():
                client.cancel(session_id)

            try:
                result = client.prompt(
                    session_id,
                    [{"type": "text", "text": user_message}],
                    on_update=self._on_update,
                    on_request=self._on_request,
                    on_dispatched=self._reissue_cancel_if_pending,
                    timeout=_TURN_TIMEOUT_SECONDS,
                )
            except Exception:
                # Tell the adapter to stop before we tear the pipes down, so
                # it can wind its own session down rather than being killed
                # mid-tool.
                try:
                    client.cancel(session_id)
                except Exception:
                    logger.debug("cancel-on-teardown failed", exc_info=True)
                raise
            return {
                "text": "".join(self.text_parts),
                "stop_reason": result.get("stopReason"),
            }
        finally:
            try:
                self.stderr_tail = client.stderr_tail()
            except Exception:
                self.stderr_tail = []
            # Deterministic reaping: no adapter outlives its turn, on any
            # path — success, protocol error, cancel, or unexpected raise.
            try:
                client.close()
            except Exception:
                logger.debug("ACP client close failed", exc_info=True)
            with self._lock:
                self._client = None

    def _on_update(self, update: Dict[str, Any]) -> None:
        """Collect assistant text only.

        ``tool_call`` / ``tool_call_update`` / ``agent_thought_chunk`` carry
        structured payloads that must never reach the user-visible response —
        leaking them as text was exactly the direct-CLI failure this runtime
        replaces. Packet 3 projects them into Hermes' tool-activity display.
        """
        if update.get("sessionUpdate") != "agent_message_chunk":
            return
        content = update.get("content") or {}
        if content.get("type") == "text":
            self.text_parts.append(content.get("text", ""))

    def _on_request(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Answer agent-initiated requests. Permissions are denied outright.

        Packet 1 has no approval UX, and "no UX" must mean *deny*, never
        *silently allow*. Returning a reject option (rather than letting the
        request go unanswered) also keeps the turn moving: an unanswered
        agent request deadlocks the prompt.
        """
        from agent.transports.claude_acp import deny_permission_response

        if method == "session/request_permission":
            self.denied_permissions += 1
            logger.info(
                "Claude Code ACP: denied a permission request (no approval UX "
                "in this runtime yet)"
            )
            return deny_permission_response(params.get("options") or [])
        raise PermissionError(f"client does not handle {method}")


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

    turn = ClaudeAcpTurn(argv=argv, cwd=cwd)
    # Published so AIAgent.interrupt() can reach the in-flight turn.
    agent._claude_acp_turn = turn
    # An interrupt that landed BEFORE this publication had nothing to cancel:
    # AIAgent.interrupt() looks up `_claude_acp_turn`, finds None, and the
    # request is lost. The adapter would then run to the full turn timeout
    # with the user believing they stopped it. Fold that already-latched flag
    # into the turn so the cancel is delivered as soon as a session exists.
    if getattr(agent, "_interrupt_requested", False):
        turn.request_cancel()

    try:
        outcome = turn.run(user_message)
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
        return _failure_result(agent, messages, f"{exc}{_diagnostics(turn)}")
    except Exception as exc:
        logger.exception("Claude Code ACP turn failed")
        return _failure_result(
            agent, messages,
            f"Claude Code ACP turn failed: {exc}{_diagnostics(turn)}",
        )
    finally:
        agent._claude_acp_turn = None

    final_text = outcome["text"]
    stop_reason = outcome["stop_reason"]
    cancelled = stop_reason == "cancelled" or turn.cancel_requested

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

    # The inbound user message was already appended by run_conversation()
    # before the early return, so only the reply is added here.
    if final_text:
        messages.append({"role": "assistant", "content": final_text})
        _persist(agent, messages)

    agent.session_api_calls = getattr(agent, "session_api_calls", 0) + 1
    _run_improvement_hooks(
        agent,
        original_user_message=original_user_message,
        final_text=final_text,
        messages=messages,
        should_review_memory=should_review_memory,
        succeeded=not cancelled and error is None,
    )

    return {
        "final_response": final_text,
        "messages": messages,
        "api_calls": 1,
        "completed": not cancelled and error is None,
        "partial": cancelled or error is not None,
        "interrupted": _user_interrupted,
        **({"interrupt_message": _interrupt_message} if _interrupt_message else {}),
        "error": error,
        # We flushed the assistant message ourselves above, so the gateway
        # must not write it again — append_message has no dedup and a second
        # write would duplicate the already-flushed user turn (#860/#42039).
        "agent_persisted": True,
    }


def _run_improvement_hooks(
    agent,
    *,
    original_user_message: Any,
    final_text: str,
    messages: List[Dict[str, Any]],
    should_review_memory: bool,
    succeeded: bool,
) -> None:
    """Delegated-runtime parity for the agent-improvement loop.

    The codex app-server runtime performs exactly these four steps after a
    turn (``agent/codex_runtime.py``). Without them, selecting Claude Code
    silently disables skill nudges, external-memory sync, and background
    review for the whole session. This runtime owns one adapter-side
    iteration per turn, so the skill counter advances by one.
    """
    agent._iters_since_skill = getattr(agent, "_iters_since_skill", 0) + 1

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
        "error": message,
    }
