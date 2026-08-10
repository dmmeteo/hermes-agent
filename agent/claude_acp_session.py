"""One long-lived Claude Code ACP session per Hermes conversation.

Analogue of ``agent/transports/codex_app_server_session.py``: the delegated
runtime hands a turn to this object, and this object owns the adapter process,
the ACP session id, the permission-mode negotiation, cancellation, and
teardown. ``agent/claude_code_runtime.py`` stays the Hermes-facing seam.

What this adds over Packet 1's per-turn adapter:

* **Reuse.** One adapter process, one ACP session, many turns. Exactly one
  adapter per active Hermes session — a second one for the same session
  retires the first.
* **Rebind.** When the adapter is gone (killed, crashed, evicted, or simply a
  new Hermes process), the persisted binding is reattached with
  ``session/load``, capability-gated on the agent advertising ``loadSession``.
* **Reseed.** When load is unavailable or fails, a fresh session is created,
  the caller supplies a bounded recap, and the turn is marked so the user gets
  exactly one visible marker. Never a transcript replay: Hermes owns the
  history and Claude Code owns its own prompt cache.
* **Retirement.** A wedged or failed turn drops the adapter, so the next turn
  starts from a clean process rather than riding a broken one (the
  ``should_retire`` discipline the codex runtime already uses). Idle sessions
  are evicted the same way.

Security posture from Packet 1 is unchanged and re-applied on every rebind:
``session/set_mode`` → ``default`` before any prompt, so the adapter's
self-approving ``auto`` classifier never decides anything. What Packet 3B
changes is what happens to the request that mode produces: instead of a blanket
denial, it is offered to a ``permission_handler`` the runtime supplies (Hermes'
own approval flow). Deny remains the default on every path where that handler is
absent, unusable, or unsuccessful — see ``_decide_permission``.
"""

from __future__ import annotations

import atexit
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

from agent.claude_acp_binding import BindingStore, TurnLease

if TYPE_CHECKING:  # import-time cost stays off the hot path
    from agent.transports.claude_acp_event_projector import (
        PermissionRequest,
        ProjectedEvent,
        UsageSnapshot,
    )

logger = logging.getLogger(__name__)

#: The adapter drives its own agentic loop, so a turn can legitimately run for
#: a long time. Matches the copilot ACP client's ceiling.
TURN_TIMEOUT_SECONDS = 900.0

#: How long an idle adapter is kept before it is evicted. A Claude Code
#: session is a real process holding a model context open; keeping one alive
#: for a conversation nobody is having wastes a process per stale session. The
#: binding survives eviction, so the next turn rebinds transparently.
IDLE_TTL_SECONDS = 1800.0

#: Permission mode Hermes negotiates for every session. NOT the adapter's
#: default (``auto``), which self-approves via a classifier.
PERMISSION_MODE = "default"

#: Live sessions, keyed by Hermes session id (or by object identity when the
#: agent has no session id). The registry is what makes "exactly one adapter
#: per active Hermes session" true, and it is what the interpreter-exit sweep
#: walks — a long-lived adapter leads its own process group and does NOT die
#: with Hermes.
_SESSIONS: Dict[str, "ClaudeAcpSession"] = {}
_SESSIONS_LOCK = threading.Lock()
_ATEXIT_REGISTERED = False


class ClaudeCodeAuthRequired(RuntimeError):
    """The adapter has no usable Claude credential."""


@dataclass
class AcpTurnOutcome:
    """What one delegated turn produced."""

    text: str = ""
    stop_reason: Optional[str] = None
    #: The ACP session was rebuilt for this turn, so the caller must surface
    #: exactly one reseed marker.
    reseeded: bool = False
    #: Completed tool calls the adapter reported, for the skill-nudge counter
    #: the chat-completions loop ticks per tool iteration.
    tool_iterations: int = 0
    #: The ``usage`` block on the ``session/prompt`` reply — ACP's authoritative
    #: per-turn token counts. ``None`` when the agent reported none.
    usage: Optional[Dict[str, Any]] = None
    #: Latest ``usage_update``: context occupancy and cost, NOT per-turn tokens.
    context_usage: Optional["UsageSnapshot"] = None


@dataclass
class _TurnState:
    """Per-turn cancellation state, replaced on every turn.

    A fresh object per turn is what keeps a cancel from leaking across the
    boundary: between turns there is no state to cancel, so a stop that
    arrives while nothing is running cannot silently abort the next turn.
    """

    cancel: threading.Event = field(default_factory=threading.Event)
    session_id: Optional[str] = None


def _register(session: "ClaudeAcpSession") -> None:
    global _ATEXIT_REGISTERED
    with _SESSIONS_LOCK:
        previous = _SESSIONS.get(session.key)
        _SESSIONS[session.key] = session
        if not _ATEXIT_REGISTERED:
            atexit.register(close_all_sessions)
            _ATEXIT_REGISTERED = True
    if previous is not None and previous is not session:
        # One adapter per Hermes session. A rebuilt agent (gateway cache
        # eviction, /resume in a new process) must not leave the previous
        # adapter — and its tool children — running.
        logger.info(
            "Retiring the previous Claude Code adapter for session %s",
            session.key,
        )
        try:
            previous.close()
        except Exception:
            logger.debug("previous ACP session close failed", exc_info=True)


def _unregister(session: "ClaudeAcpSession") -> None:
    with _SESSIONS_LOCK:
        if _SESSIONS.get(session.key) is session:
            _SESSIONS.pop(session.key, None)


def close_all_sessions() -> None:
    """Reap every live adapter. Registered at interpreter exit."""
    with _SESSIONS_LOCK:
        sessions = list(_SESSIONS.values())
    for session in sessions:
        try:
            session.close()
        except Exception:
            logger.debug("ACP session close failed at exit", exc_info=True)


class ClaudeAcpSession:
    """One Claude Code session, owned by one Hermes conversation.

    Not thread-safe for concurrent turns — one caller drives it at a time,
    matching ``run_conversation()``. Cancellation is the one cross-thread
    entry point and is guarded by its own lock.
    """

    def __init__(
        self,
        *,
        argv: List[str],
        cwd: str,
        store: Optional[BindingStore] = None,
        hermes_session_id: str = "",
        additional_directories: Optional[List[str]] = None,
    ) -> None:
        self.argv = list(argv)
        self.cwd = cwd
        #: Extra roots the user explicitly allowed, already validated by
        #: ``agent/claude_acp_workspace.py``. Empty means "the workspace only".
        self.additional_directories = list(additional_directories or [])
        self.key = hermes_session_id or f"anon-{id(self):x}"
        self._store = store if store is not None else BindingStore()
        self._client: Optional[Any] = None
        self._acp_session_id: Optional[str] = None
        self._turn: Optional[_TurnState] = None
        self._lock = threading.Lock()
        self._last_used = time.monotonic()
        self._closed = False
        #: Captured before every teardown so failure paths can still surface
        #: bounded, scrubbed adapter diagnostics.
        self.stderr_tail: List[str] = []
        self.denied_permissions = 0
        self.granted_permissions = 0
        _register(self)

    # ---------- introspection ----------

    @property
    def acp_session_id(self) -> Optional[str]:
        return self._acp_session_id

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def adapter_pid(self) -> Optional[int]:
        client = self._client
        return None if client is None else client.pid

    def is_connected(self) -> bool:
        client = self._client
        return client is not None and client.is_alive()

    @property
    def workspace_roots(self) -> List[str]:
        """Every directory this session negotiated: the cwd, then the extras.

        This is the yardstick for anything the agent asks the client to touch.
        It is deliberately the same list that went out on ``session/new``, so a
        request cannot be checked against a wider scope than the one the session
        was created with.
        """
        return [self.cwd] + list(self.additional_directories)

    def matches(
        self,
        argv: List[str],
        cwd: str,
        additional_directories: Optional[List[str]] = None,
    ) -> bool:
        """Whether a live session is still the right one for these settings.

        The roots are part of the answer: they are negotiated on the wire at
        session creation, so a changed root list means the running adapter is
        scoped to something other than what the caller now wants.
        """
        return (
            self.argv == list(argv)
            and self.cwd == cwd
            and self.additional_directories == list(additional_directories or [])
        )

    # ---------- cancellation ----------

    def request_cancel(self) -> None:
        """Ask the adapter to abort the in-flight turn. Thread-safe.

        A no-op between turns: ``session/cancel`` with nothing running is at
        best ignored, and latching it here would cancel the *next* turn. A
        stop that lands before a turn starts is carried in by the caller as
        ``cancel_pending`` instead.
        """
        with self._lock:
            turn, client = self._turn, self._client
        if turn is None:
            return
        turn.cancel.set()
        if client is not None and turn.session_id:
            try:
                client.cancel(turn.session_id)
            except Exception:
                logger.debug("session/cancel failed", exc_info=True)

    @property
    def cancel_requested(self) -> bool:
        with self._lock:
            turn = self._turn
        return turn is not None and turn.cancel.is_set()

    # ---------- lifecycle ----------

    def close(self) -> None:
        """Reap the adapter process tree. Idempotent."""
        self._closed = True
        _unregister(self)
        self._drop_client()

    def _drop_client(self) -> None:
        """Reap the adapter, leaving the turn state alone.

        Deliberately does NOT clear ``_turn``: a reconnect happens *inside* a
        turn (the first connect, an idle respawn, a rebind), and clearing the
        turn there would unpublish the very turn a concurrent
        ``request_cancel`` needs to find — the user's stop would then be
        silently dropped for the whole turn. ``run_turn`` owns that field.
        """
        with self._lock:
            client, self._client = self._client, None
        self._acp_session_id = None
        if client is None:
            return
        try:
            self.stderr_tail = client.stderr_tail()
        except Exception:
            pass
        try:
            client.close()
        except Exception:
            logger.debug("ACP client close failed", exc_info=True)

    def _evict_if_idle(self) -> None:
        if self._client is None:
            return
        if (time.monotonic() - self._last_used) < IDLE_TTL_SECONDS:
            return
        logger.info(
            "Evicting an idle Claude Code adapter for session %s", self.key
        )
        self._drop_client()

    # ---------- connection ----------

    def _connect(self, turn: _TurnState) -> bool:
        """Ensure a live adapter bound to this conversation's ACP session.

        Returns True when the session had to be rebuilt from scratch, i.e. the
        caller owes the user a reseed marker and the adapter a recap.
        """
        if self.is_connected() and self._acp_session_id:
            return False
        self._drop_client()

        from agent.transports.claude_acp import ClaudeAcpClient

        client = ClaudeAcpClient(command=self.argv, cwd=self.cwd)
        with self._lock:
            self._client = client
        self._closed = False
        # A session reconnecting after close() must be back in the registry,
        # or the interpreter-exit sweep would not know to reap this adapter.
        _register(self)

        client.initialize()
        # An adapter with no usable credential advertises the auth methods it
        # wants completed first. Without this the user just sees session/new
        # fail with an opaque ACP error — the likeliest first failure on a
        # headless host.
        if client.auth_methods:
            raise ClaudeCodeAuthRequired(
                "Claude Code is not authenticated. Run `claude` once to log "
                "in, or on a headless host create a subscription token with "
                "`claude setup-token` and set CLAUDE_CODE_OAUTH_TOKEN."
            )

        binding = self._store.read()
        prior = str(binding.get("acp_session_id") or "")
        # The workspace is part of the session's identity: `session/load`
        # reattaches a session to a cwd, so a binding recorded elsewhere must
        # not be resumed against this one. Losing it still costs a reseed —
        # the conversation's Claude-side context is gone either way.
        bound = prior if str(binding.get("cwd") or "") == self.cwd else ""
        if prior and not bound:
            logger.info(
                "Discarding Claude Code ACP binding for session %s: workspace "
                "changed (%s -> %s)", self.key, binding.get("cwd"), self.cwd,
            )
        # Containment is negotiated, not intercepted (Packet 0, R4): the adapter
        # uses its own tools, so the only place Hermes can bound the workspace
        # is the session-creation call. Capability-gated — an agent that never
        # advertised the field gets the cwd alone, which is narrower.
        roots = self._negotiable_roots(client)

        reseeded = bool(prior) and not bound
        if bound and client.supports_load_session:
            try:
                client.session_load(bound, additional_directories=roots)
                # The agent replays its history as notifications before
                # answering. Hermes owns the transcript, so none of it may be
                # projected — and none of it may still be queued when the next
                # prompt starts pumping updates.
                client.drain_inbox()
                self._acp_session_id = bound
                logger.info(
                    "Rebound Claude Code ACP session %s for Hermes session %s",
                    bound, self.key,
                )
            except Exception:
                logger.warning(
                    "session/load failed for ACP session %s; starting a fresh "
                    "session and reseeding from Hermes' own transcript",
                    bound, exc_info=True,
                )
                reseeded = True
        elif bound:
            logger.info(
                "Adapter does not support session/load; reseeding Hermes "
                "session %s into a fresh Claude Code session", self.key,
            )
            reseeded = True

        if not self._acp_session_id:
            self._acp_session_id = client.session_new(additional_directories=roots)

        # SECURITY: the adapter starts in `auto`, where a classifier approves
        # writes and shell commands without ever asking Hermes. Move it to
        # `default` before any prompt runs — on every new session AND every
        # rebind, since the mode is session state the adapter re-derives.
        client.set_mode(self._acp_session_id, PERMISSION_MODE)
        self._store.bind(self._acp_session_id, self.cwd)
        turn.session_id = self._acp_session_id
        return reseeded

    def _negotiable_roots(self, client: Any) -> List[str]:
        """The extra roots this agent will actually accept on the wire."""
        if not self.additional_directories:
            return []
        if not client.supports_additional_directories:
            logger.warning(
                "The Claude Code adapter does not support additional workspace "
                "directories; session %s runs with %s only",
                self.key, self.cwd,
            )
            return []
        return list(self.additional_directories)

    # ---------- the turn ----------

    def run_turn(
        self,
        user_message: str,
        *,
        recap_provider: Optional[Callable[[], str]] = None,
        cancel_pending: bool = False,
        event_sink: Optional[Callable[["ProjectedEvent"], None]] = None,
        permission_handler: Optional[Callable[["PermissionRequest"], bool]] = None,
    ) -> AcpTurnOutcome:
        """Run one turn on this conversation's Claude Code session.

        The turn lease is taken before anything is spawned — a lease conflict
        must not start a second adapter against a session another process is
        already driving — and released on every exit path, success or not.

        ``event_sink`` receives each ``ProjectedEvent`` the projector produces,
        in arrival order. It is the runtime's seam onto Hermes' display
        surfaces; this class stays free of any knowledge of them.

        ``permission_handler`` is the same kind of seam for approvals: it is
        handed a projected ``PermissionRequest`` and returns True to permit that
        one action. It is called on this thread, synchronously, while the agent
        waits — which is exactly what lets Hermes' existing per-thread approval
        surfaces answer. Omitting it denies every request.
        """
        turn = _TurnState()
        if cancel_pending:
            # An interrupt that landed before this turn existed. Folding it in
            # here (rather than latching it on the session) keeps the stop
            # attached to the turn the user meant to stop.
            turn.cancel.set()
        # Publish BEFORE the lease write: acquiring the lease touches the
        # session store, and a stop arriving during that window would find no
        # turn to cancel and be silently dropped.
        with self._lock:
            self._turn = turn

        lease: Optional[TurnLease] = None
        try:
            lease = self._store.acquire_turn_lease()
        except Exception:
            with self._lock:
                self._turn = None
            raise

        outcome = AcpTurnOutcome()
        try:
            self._evict_if_idle()
            outcome.reseeded = self._connect(turn)
            session_id = self._acp_session_id or ""
            turn.session_id = session_id
            client = self._client
            assert client is not None

            blocks: List[Dict[str, Any]] = []
            if outcome.reseeded and recap_provider is not None:
                recap = (recap_provider() or "").strip()
                if recap:
                    blocks.append({"type": "text", "text": recap})
            blocks.append({"type": "text", "text": user_message})

            if turn.cancel.is_set():
                client.cancel(session_id)
            # Anything queued now predates our prompt (a load replay tail, a
            # stray notification) and must not be read as this turn's output.
            client.drain_inbox()

            from agent.transports.claude_acp_event_projector import (
                ClaudeAcpEventProjector,
            )

            # The projector owns the event→surface mapping and the strict
            # assistant-text allow-list: only `agent_message_chunk` becomes
            # reply text, so a structured payload (tool_call, thought, replayed
            # user text, plan) can never leak into it — the exact direct-CLI
            # failure this runtime replaces.
            projector = ClaudeAcpEventProjector()

            def _on_update(update: Dict[str, Any]) -> None:
                event = projector.project(update)
                if event.kind == "ignored" or event_sink is None:
                    return
                try:
                    event_sink(event)
                except Exception:
                    # A display surface must never break the turn it observes,
                    # and must never cost us the accumulated reply — which the
                    # projector already recorded before we got here.
                    logger.debug("ACP event sink raised", exc_info=True)

            try:
                result = client.prompt(
                    session_id,
                    blocks,
                    on_update=_on_update,
                    on_request=lambda method, request_params: self._on_request(
                        method,
                        request_params,
                        permission_handler=permission_handler,
                        # The projector has already seen the `tool_call` this
                        # permission request refers to (the adapter emits it
                        # first, and one stdio stream is ordered), so it knows
                        # the tool's declared name even though the request
                        # itself carries none for a top-level call.
                        name_lookup=projector.declared_tool_name,
                    ),
                    on_dispatched=lambda: self._reissue_cancel(turn),
                    timeout=TURN_TIMEOUT_SECONDS,
                )
            except Exception:
                # Tell the adapter to stop before the pipes go down, so it can
                # wind its own session down rather than being killed mid-tool.
                try:
                    client.cancel(session_id)
                except Exception:
                    logger.debug("cancel-on-teardown failed", exc_info=True)
                raise

            outcome.text = projector.assistant_text
            outcome.stop_reason = result.get("stopReason")
            outcome.tool_iterations = projector.tool_iterations
            outcome.context_usage = projector.usage
            reported_usage = result.get("usage")
            if isinstance(reported_usage, dict) and reported_usage:
                outcome.usage = reported_usage
            try:
                self.stderr_tail = client.stderr_tail()
            except Exception:
                pass
            return outcome
        except Exception:
            # Retire the adapter: a turn that failed at the protocol level
            # leaves a process we should not ride into the next turn. The
            # binding stays — the ACP session itself is usually still loadable.
            self._drop_client()
            raise
        finally:
            with self._lock:
                self._turn = None
            self._last_used = time.monotonic()
            self._store.release_turn_lease(lease)

    def _reissue_cancel(self, turn: _TurnState) -> None:
        """Re-send a cancel once the prompt is genuinely in flight.

        A ``session/cancel`` delivered before any prompt existed is something
        a real adapter may legitimately ignore — there was no turn to cancel.
        Without this the user's stop would be silently dropped and the turn
        would run to natural completion. Safe to send twice: cancellation is
        idempotent and the turn still settles through the pending reply.
        """
        if not turn.cancel.is_set():
            return
        with self._lock:
            client = self._client
        if client is not None and turn.session_id:
            try:
                client.cancel(turn.session_id)
            except Exception:
                logger.debug("cancel re-issue failed", exc_info=True)

    def _on_request(
        self,
        method: str,
        params: Dict[str, Any],
        *,
        permission_handler: Optional[Callable[[Any], bool]] = None,
        name_lookup: Optional[Callable[[str], str]] = None,
    ) -> Dict[str, Any]:
        """Answer agent-initiated requests. Only permissions are handled.

        Runs on the thread driving the turn (the wire pumps inside
        ``client.prompt``), which is what makes the Hermes-side decision see the
        right per-thread state: the CLI's approval callback, the gateway's
        session key contextvar, and the interrupt flag are all thread- or
        context-local.
        """
        if method == "session/request_permission":
            return self._decide_permission(
                params, permission_handler, name_lookup=name_lookup
            )
        if method.startswith("fs/"):
            return self._refuse_filesystem_request(method, params)
        raise PermissionError(f"client does not handle {method}")

    def _refuse_filesystem_request(
        self, method: str, params: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Refuse an ``fs/*`` request, out-of-root ones by name.

        Hermes advertises ``fs.readTextFile`` / ``fs.writeTextFile`` as false at
        ``initialize``, so any ``fs/*`` request is unsolicited whatever it names
        — serving one would hand the agent a capability it was told does not
        exist, on a thread that is meanwhile driving a turn.

        The containment check still runs first, and deliberately so. It is the
        one place a path the agent chose is measured against the roots the
        session negotiated, so the refusal says *why* — and if this client ever
        does advertise a filesystem capability, the check is already the gate
        rather than something a later change has to remember to add.
        """
        from agent.claude_acp_workspace import resolve_within_roots

        path = params.get("path")
        if isinstance(path, str) and path.strip():
            # Raises PermissionError naming the workspace, which the wire layer
            # turns into a JSON-RPC error reply — never silence, which would
            # deadlock the turn.
            resolve_within_roots(path, self.workspace_roots)
        raise PermissionError(
            f"client does not handle {method}: Hermes advertises no filesystem "
            "capability for this session"
        )

    def _decide_permission(
        self,
        params: Dict[str, Any],
        permission_handler: Optional[Callable[[Any], bool]],
        *,
        name_lookup: Optional[Callable[[str], str]] = None,
    ) -> Dict[str, Any]:
        """Decide one ``session/request_permission``. Default is deny.

        Protocol-level routing only — no approval policy lives here. The
        Hermes-side decision (mode, allowlists, timeout, which surface asks the
        user) belongs to ``tools/approval.py`` and reaches us through
        ``permission_handler``; everything this method adds is the reasons a
        request is refused *without* asking:

        * no handler wired (no approval surface at all),
        * the agent offered no single-shot allow option — permitting one would
          hand the adapter a standing grant Hermes could no longer see,
        * the turn is already cancelled: a user who pressed stop is not asked to
          approve more work,
        * the turn was cancelled *while* the handler was deciding — the answer
          was in flight when the user pressed stop, and a stop must not be
          answered with a write,
        * the handler raised, or returned anything other than approval.

        ``name_lookup`` maps a ``toolCallId`` to the tool name the adapter
        declared on the ``tool_call`` it emitted before asking. Adapter 0.66.0
        puts ``_meta.claudeCode.toolName`` on the permission request only for a
        sub-agent's (parented) call, so without this the prompt for an ordinary
        Bash/Write would name the ACP *kind* instead of the tool.

        Always answers. An unanswered agent request deadlocks the prompt until
        the turn timeout, which is strictly worse for the user than a denial.
        """
        from agent.transports.claude_acp import (
            allow_permission_response,
            deny_permission_response,
        )

        options = params.get("options") or []
        allow = allow_permission_response(options)

        def _deny(reason: str) -> Dict[str, Any]:
            self.denied_permissions += 1
            logger.info("Claude Code ACP: denied a permission request (%s)", reason)
            return deny_permission_response(options)

        if permission_handler is None:
            return _deny("no approval surface is wired for this turn")
        if allow is None:
            return _deny(
                "the agent offered no single-shot allow option; Hermes will not "
                "grant a standing adapter-side permission"
            )
        if self.cancel_requested:
            return _deny("the turn was already cancelled")

        from agent.transports.claude_acp_event_projector import (
            project_permission_request,
        )

        tool_call = params.get("toolCall")
        call_id = ""
        if isinstance(tool_call, dict):
            call_id = str(tool_call.get("toolCallId") or "")
        hint = ""
        if name_lookup is not None and call_id:
            try:
                hint = name_lookup(call_id) or ""
            except Exception:
                logger.debug("declared tool-name lookup raised", exc_info=True)

        try:
            approved = bool(permission_handler(project_permission_request(
                params, roots=self.workspace_roots, tool_name_hint=hint,
            )))
        except Exception:
            # A broken approval surface must not become an approval.
            logger.warning(
                "Claude Code ACP: the approval surface raised; denying",
                exc_info=True,
            )
            return _deny("the approval surface raised")

        if not approved:
            return _deny("the user did not approve it")
        if self.cancel_requested:
            # The stop landed while the human was deciding. The decision runs
            # synchronously on the turn thread, so this is where such a stop
            # necessarily arrives — and settling it as anything but a denial
            # would let `/stop` be answered with the write the user stopped.
            return _deny("the turn was cancelled while the approval was pending")
        self.granted_permissions += 1
        logger.info("Claude Code ACP: permission granted for one action")
        return allow
