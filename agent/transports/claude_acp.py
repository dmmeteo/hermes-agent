"""Agent Client Protocol (ACP) client over stdio — wire layer only.

Hermes is the ACP **client**; the child process is an ACP **agent**. For the
Claude Code runtime that agent is the `claude-agent-acp` adapter, which drives
a real Claude Code session (native tools, subscription auth) and reports back
as typed protocol events.

Protocol: newline-delimited JSON-RPC 2.0 over stdio, ACP protocolVersion 1.
Three message directions matter and all three are load-bearing:

1. client -> agent requests (`initialize`, `session/new`, `session/prompt`)
2. agent -> client notifications (`session/update` — the typed event stream)
3. **agent -> client requests** (`session/request_permission`,
   `fs/read_text_file`, `terminal/*`). These arrive *while* a prompt is still
   in flight and MUST be answered, otherwise the turn deadlocks. This is the
   single most common way a naive ACP client hangs.

Threading model mirrors ``codex_app_server.py``: one reader thread parses
stdout and routes replies to pending requests, while notifications and
agent-initiated requests land on a queue that the **calling** thread drains
inside :meth:`ClaudeAcpClient.prompt`. Handlers therefore never run on the
reader thread, so a slow handler (a human permission prompt) cannot stall the
wire. Intentionally synchronous — ``AIAgent.run_conversation()`` is
synchronous, and layering asyncio under it creates surprising interrupt
semantics.

Status: Packet 0 (feasibility spike). This module speaks the wire and nothing
else — session mapping, event projection, and the Hermes turn seam land in
later packets. See ``docs/development/claude-code-acp-runtime-plan.md``.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from tools.environments.local import hermes_subprocess_env

IS_WINDOWS = os.name == "nt"

# ACP protocol revision this client speaks. Bumping is a deliberate change:
# the agent echoes its own version in the initialize response and we compare.
ACP_PROTOCOL_VERSION = 1

# API-billing credentials stripped from the adapter's environment by default.
# The Claude Code provider advertises *subscription* auth, and the adapter
# would silently prefer an inherited API key — billing the API account at API
# rates while the UI says "your Claude subscription". Callers that genuinely
# want key auth pass ``strip_env_vars=()`` deliberately.
#
# CLAUDE_CODE_OAUTH_TOKEN is deliberately NOT here: it is the token minted by
# `claude setup-token` and IS the subscription credential — the documented way
# to authenticate Claude Code on a host with no interactive login (gateway,
# cron, containers). Stripping it would break the very auth this provider
# advertises. See website/docs/reference/environment-variables.md.
ANTHROPIC_CREDENTIAL_ENV_VARS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
)

# JSON-RPC reserved codes we emit back at the agent.
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INTERNAL_ERROR = -32603

_DEFAULT_REQUEST_TIMEOUT = 60.0
_DEFAULT_PROMPT_TIMEOUT = 900.0


class AcpError(RuntimeError):
    """A JSON-RPC ``error`` response from the agent."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(f"ACP error {code}: {message}")
        self.code = code
        self.message = message
        self.data = data


class AcpProtocolError(RuntimeError):
    """The connection is unusable — child exited, closed, or framing broke."""


def ensure_path_within_roots(path_text: str, roots: list[str]) -> Path:
    """Resolve ``path_text`` and require it to sit inside one of ``roots``.

    Workspace containment for agent-initiated filesystem requests. Uses
    resolved-parent comparison rather than string prefixes, so ``/ws-evil``
    does not pass a ``/ws`` root check and ``..`` traversal cannot escape.

    The rule itself lives in ``agent/claude_acp_workspace.py`` alongside root
    negotiation — the roots a request is checked against and the roots the
    session was created with have to be the same notion of "root", and one
    implementation is how that stays true.
    """
    from agent.claude_acp_workspace import resolve_within_roots

    return resolve_within_roots(path_text, roots)


def _terminate_process_tree(proc: Any, pgid: Optional[int] = None) -> None:
    """Best-effort kill of *proc* and every descendant, on both platforms.

    ``proc.kill()`` alone only reaps the direct child. The adapter spawns a
    real Claude Code session whose own tools fork further processes, and on
    Windows an npm-global install resolves to a ``.cmd`` shim whose ``node``
    grandchild survives a shim kill. Both are exactly the stale-subprocess
    class this runtime exists to prevent.

    Mirrors ``hermes_cli._subprocess_compat._kill_git_process_tree``: POSIX
    group-kill guarded on the child actually leading its own group (so a
    shared group is never blasted), ``taskkill /T /F`` on Windows. A process
    group id cannot be recycled while any member is alive, so signalling it
    after the direct child exited cannot hit an unrelated group. Every failure
    is swallowed — this only ever runs on a cleanup path.
    """
    if IS_WINDOWS:
        # MUST run before terminate()/kill(). The direct child is the npm
        # `.cmd` shim; once it is dead, `taskkill /T` can no longer enumerate
        # its node.exe descendant — which is the exact leak this exists to
        # prevent. (This is where the ported git-probe ordering was wrong: it
        # guards short-lived probes where the tree matters less.)
        try:
            from hermes_cli._subprocess_compat import windows_hide_flags

            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                timeout=5,
                check=False,
                creationflags=windows_hide_flags(),
            )
        except Exception:
            pass
    else:
        try:
            import signal as _signal

            # Only signal a group we know we lead. `pgid` is captured at spawn
            # time because os.getpgid() raises once the direct child is reaped,
            # which would leave surviving descendants unsignalled. When it is
            # None we never learned the group, so we do not guess: signalling a
            # recycled id could hit unrelated processes.
            if pgid is not None and pgid == proc.pid:
                os.killpg(pgid, _signal.SIGKILL)  # windows-footgun: ok — inside a `not IS_WINDOWS` gate
        except Exception:
            pass
    try:
        proc.kill()
    except Exception:
        pass


def allow_permission_response(options: list[dict]) -> Optional[dict]:
    """Build a reply permitting exactly the one action that was asked about.

    Only an ``allow_once`` option is ever selected. A standing adapter-side
    grant (``allow_always``) would move the policy out of Hermes' view — every
    later write would be self-approved by the adapter and Hermes would never see
    the request — and the user approved one action, not a class of them. Durable
    grants live in Hermes' own session/permanent allowlist instead, where the
    next request is still visible and still revocable.

    Returns ``None`` when the agent offered no single-shot allow option: there is
    then nothing Hermes can safely permit, and the caller must deny.
    """
    for option in options or []:
        if option.get("kind") == "allow_once" and option.get("optionId"):
            return {"outcome": {"outcome": "selected", "optionId": option["optionId"]}}
    return None


def deny_permission_response(options: list[dict]) -> dict:
    """Build a ``session/request_permission`` reply that refuses the action.

    Prefers an explicit reject option the agent offered (so it can distinguish
    "denied" from "the user went away"), and falls back to ``cancelled``.
    """
    for option in options or []:
        if option.get("kind") in ("reject_once", "reject_always"):
            return {"outcome": {"outcome": "selected", "optionId": option.get("optionId")}}
    return {"outcome": {"outcome": "cancelled"}}


class _Pending:
    __slots__ = ("box", "event", "method")

    def __init__(self, method: str) -> None:
        self.box: dict[str, Any] = {}
        self.event = threading.Event()
        self.method = method


class ClaudeAcpClient:
    """Minimal ACP client speaking JSON-RPC 2.0 over a child's stdio."""

    def __init__(
        self,
        command: list[str],
        cwd: str,
        env: Optional[dict[str, str]] = None,
        stderr_ring_size: int = 200,
        strip_env_vars: tuple = ANTHROPIC_CREDENTIAL_ENV_VARS,
    ) -> None:
        if not command:
            raise ValueError("command must be a non-empty argv list")

        # The adapter needs exactly ONE credential — the Claude subscription
        # token — so it is spawned with the narrow posture rather than the
        # provider-credential-inheriting one. `CLAUDE_CODE_OAUTH_TOKEN` survives
        # `inherit_credentials=False` because it is deliberately excluded from
        # the Tier-2 blocklist (`tools/environments/local.py`), which is the
        # exact pattern that helper's docstring recommends for a caller needing
        # one specific secret. Everything else Tier-2 covers — every other
        # provider key, every tool secret — is stripped: this child is a Node
        # process whose `Bash` tool executes model-authored commands, and a key
        # it never uses is only reachable from that shell.
        spawn_env = hermes_subprocess_env(inherit_credentials=False)
        if env:
            spawn_env.update(env)
        # Popped, not blanked: the adapter treats an empty-string key as
        # "unset" only by accident, and an inherited key would defeat the
        # subscription auth this provider advertises.
        for var in strip_env_vars or ():
            spawn_env.pop(var, None)

        from hermes_cli._subprocess_compat import windows_hide_flags

        self._command = list(command)
        self._cwd = cwd
        # POSIX: lead our own process group so close() can signal the whole
        # tree without ever touching Hermes' own group. Windows has no
        # equivalent here; teardown there goes through taskkill /T.
        group_kwargs = {} if IS_WINDOWS else {"process_group": 0}
        self._proc = subprocess.Popen(
            self._command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env=spawn_env,
            text=True,
            **group_kwargs,
            # JSON-RPC is UTF-8 by definition; without this the pipes decode
            # with the platform default (cp1252/cp936 on Windows) and any
            # non-ASCII payload corrupts or raises mid-turn.
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=windows_hide_flags(),
        )

        self._next_id = 1
        self._id_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._pending: dict[int, _Pending] = {}
        self._pending_lock = threading.Lock()
        # Notifications and agent-initiated requests, drained by the caller.
        self._inbox: queue.Queue = queue.Queue()
        self._stderr_ring: list[str] = []
        self._stderr_ring_size = stderr_ring_size
        self._stderr_lock = threading.Lock()
        self._closed = False
        # Captured now: after the child is reaped os.getpgid() raises, and the
        # descendant sweep in close() would silently become a no-op.
        # Left as None if the lookup fails: falling back to the pid would name
        # a group we may not own once that pid is recycled, and the
        # `pgid == proc.pid` guard would then pass trivially.
        self._pgid: Optional[int] = None
        if not IS_WINDOWS:
            try:
                self._pgid = os.getpgid(self._proc.pid)
            except Exception:
                self._pgid = None

        self.protocol_version: Optional[int] = None
        self.agent_capabilities: dict[str, Any] = {}
        self.auth_methods: list[dict] = []
        #: Raw ``configOptions`` from the most recent ``session/new`` /
        #: ``session/load`` response. ACP has no dedicated "current model"
        #: field (Packet 4 slice A) — the adapter reports it as this generic
        #: option list's "model" entry, so this is captured verbatim here and
        #: read back through :attr:`current_model_id`.
        self.session_config_options: Optional[list] = None

        self._reader = threading.Thread(
            target=self._read_stdout, name="acp-stdout", daemon=True
        )
        self._reader.start()
        self._stderr_reader = threading.Thread(
            target=self._read_stderr, name="acp-stderr", daemon=True
        )
        self._stderr_reader.start()

    # ---------- properties ----------

    @property
    def returncode(self) -> Optional[int]:
        return self._proc.returncode

    @property
    def pid(self) -> int:
        return self._proc.pid

    @property
    def supports_load_session(self) -> bool:
        return bool(self.agent_capabilities.get("loadSession"))

    @property
    def session_capabilities(self) -> dict[str, Any]:
        return self.agent_capabilities.get("sessionCapabilities") or {}

    @property
    def current_model_id(self) -> Optional[str]:
        """The agent-reported model selector for this session, or ``None``.

        Read from the "model" entry of the last ``session/new`` /
        ``session/load`` response's ``configOptions`` — an older adapter that
        never reports one, or a client that has not created a session yet,
        both read as ``None`` rather than a fabricated default.

        It is the *picker's* value: often a full model id, but ``"default"``
        or a family alias just as legitimately (see
        ``extract_current_model_id``).
        """
        from agent.transports.claude_acp_event_projector import (
            extract_current_model_id,
        )

        return extract_current_model_id(self.session_config_options)

    @property
    def available_model_options(self) -> Optional[list[dict[str, str]]]:
        """Every model the current session advertises as selectable.

        The write-side companion to :attr:`current_model_id`, read from the
        same captured ``configOptions`` (Packet 4 slice B). ``[]`` for an agent
        that advertises no model options — ACP publishes no capability for
        this, so an empty list is the only available "cannot pick a model here"
        signal.

        ``None`` is the *other* answer: this client's last session response
        carried no model entry at all (or it has created no session yet), so it
        reports nothing about models rather than reporting emptiness. A caller
        refreshing session state must keep those apart — see
        ``ClaudeAcpSession._capture_model_state``.
        """
        from agent.transports.claude_acp_event_projector import (
            extract_model_options,
            has_model_config_option,
        )

        if not has_model_config_option(self.session_config_options):
            return None
        return extract_model_options(self.session_config_options)

    @property
    def supports_additional_directories(self) -> bool:
        """Whether this agent can widen a session beyond its ``cwd``.

        Presence, not truthiness: ACP advertises sub-capabilities as (possibly
        empty) objects, and ``additionalDirectories: {}`` means supported.
        Sending the field to an agent that never advertised it is a containment
        claim we cannot back — some agents ignore an unknown field, others
        reject the whole request.
        """
        return "additionalDirectories" in self.session_capabilities

    def stderr_tail(self) -> list[str]:
        """Recent child stderr, for diagnostics. Never logged automatically."""
        with self._stderr_lock:
            return list(self._stderr_ring)

    def is_alive(self) -> bool:
        """Whether this connection can still carry a request.

        A long-lived client outlives a single turn, so callers must be able to
        ask before reusing it — the adapter can be killed, crash, or be reaped
        between turns, and writing to a dead child only surfaces as a broken
        pipe mid-request.
        """
        return not self._closed and self._proc.poll() is None

    def drain_inbox(self) -> int:
        """Discard queued notifications; return how many were dropped.

        Anything sitting in the inbox before a request is dispatched belongs
        to something that is *not* that request — most importantly the history
        ``session/load`` replays, which Hermes must never project (it owns the
        transcript). JSON-RPC over one stdio stream is ordered, so draining
        after a reply removes everything the agent emitted before it.

        The EOF sentinel is preserved: dropping it would hide a dead child
        from the request loop.
        """
        dropped = 0
        while True:
            try:
                msg = self._inbox.get_nowait()
            except queue.Empty:
                return dropped
            if msg is None:
                self._inbox.put(None)
                return dropped
            dropped += 1

    # ---------- reader threads ----------

    def _read_stdout(self) -> None:
        try:
            for line in self._proc.stdout:  # type: ignore[union-attr]
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    # Adapters occasionally emit non-JSON banner lines on
                    # stdout. Drop them rather than killing the connection.
                    continue
                self._dispatch(msg)
        except Exception:
            pass
        finally:
            # Unblock anyone waiting on a reply the child will never send.
            with self._pending_lock:
                pending = list(self._pending.values())
                self._pending.clear()
            for entry in pending:
                entry.box["error"] = {
                    "code": JSONRPC_INTERNAL_ERROR,
                    "message": "ACP connection closed before a reply arrived",
                }
                entry.event.set()
            self._inbox.put(None)

    def _dispatch(self, msg: dict) -> None:
        msg_id = msg.get("id")
        if msg_id is not None and "method" not in msg:
            with self._pending_lock:
                entry = self._pending.pop(msg_id, None)
            if entry is not None:
                entry.box.update(msg)
                entry.event.set()
            return
        # Notification, or an agent-initiated request. Both are handled by
        # the calling thread so handlers never stall the reader.
        self._inbox.put(msg)

    def _read_stderr(self) -> None:
        try:
            for line in self._proc.stderr:  # type: ignore[union-attr]
                with self._stderr_lock:
                    self._stderr_ring.append(line.rstrip())
                    if len(self._stderr_ring) > self._stderr_ring_size:
                        del self._stderr_ring[0]
        except Exception:
            pass

    # ---------- send ----------

    def _write(self, obj: dict) -> None:
        if self._closed or self._proc.stdin is None or self._proc.stdin.closed:
            raise AcpProtocolError("ACP connection is closed")
        payload = json.dumps(obj) + "\n"
        with self._write_lock:
            try:
                self._proc.stdin.write(payload)
                self._proc.stdin.flush()
            except (BrokenPipeError, ValueError) as exc:
                raise AcpProtocolError(f"ACP child is gone: {exc}") from exc

    def notify(self, method: str, params: Optional[dict] = None) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def respond(self, request_id: Any, result: dict) -> None:
        self._write({"jsonrpc": "2.0", "id": request_id, "result": result})

    def respond_error(self, request_id: Any, code: int, message: str) -> None:
        self._write({
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        })

    def request(
        self,
        method: str,
        params: Optional[dict] = None,
        timeout: float = _DEFAULT_REQUEST_TIMEOUT,
    ) -> dict:
        """Send a request and block for its reply.

        Use only for short control calls. ``session/prompt`` must go through
        :meth:`prompt`, which also pumps the inbox — a bare ``request`` would
        deadlock the moment the agent asked for a permission decision.
        """
        with self._id_lock:
            request_id = self._next_id
            self._next_id += 1

        entry = _Pending(method)
        with self._pending_lock:
            self._pending[request_id] = entry

        try:
            self._write({"jsonrpc": "2.0", "id": request_id, "method": method,
                         "params": params or {}})
        except Exception:
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise

        if not entry.event.wait(timeout):
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise AcpProtocolError(f"timed out after {timeout}s waiting for {method}")

        return self._unwrap(entry.box)

    @staticmethod
    def _unwrap(msg: dict) -> dict:
        if "error" in msg:
            err = msg["error"] or {}
            raise AcpError(
                err.get("code", JSONRPC_INTERNAL_ERROR),
                err.get("message", "unknown error"),
                err.get("data"),
            )
        return msg.get("result") or {}

    # ---------- protocol ----------

    def initialize(
        self,
        client_capabilities: Optional[dict] = None,
        timeout: float = 30.0,
    ) -> dict:
        """Perform the ACP handshake and cache the agent's capabilities.

        Default capabilities advertise nothing we cannot actually honour:
        filesystem and terminal support are opt-in, because advertising a
        capability we do not implement makes the agent send requests we would
        have to refuse mid-turn.
        """
        params = {
            "protocolVersion": ACP_PROTOCOL_VERSION,
            "clientCapabilities": client_capabilities or {
                "fs": {"readTextFile": False, "writeTextFile": False},
                "terminal": False,
            },
        }
        result = self.request("initialize", params, timeout=timeout)
        self.protocol_version = result.get("protocolVersion")
        self.agent_capabilities = result.get("agentCapabilities") or {}
        self.auth_methods = result.get("authMethods") or []
        if self.protocol_version != ACP_PROTOCOL_VERSION:
            raise AcpProtocolError(
                f"agent speaks ACP v{self.protocol_version}, "
                f"this client speaks v{ACP_PROTOCOL_VERSION}"
            )
        return result

    def session_new(
        self,
        cwd: Optional[str] = None,
        mcp_servers: Optional[list] = None,
        timeout: float = 60.0,
        additional_directories: Optional[list[str]] = None,
    ) -> str:
        """Create a session scoped to ``cwd`` plus any ``additional_directories``.

        The extra roots are the caller's containment decision and are sent only
        when they asked for some; capability-gating them is
        :attr:`supports_additional_directories`' job, one level up.
        """
        params: dict[str, Any] = {
            "cwd": cwd or self._cwd,
            "mcpServers": mcp_servers or [],
        }
        if additional_directories:
            params["additionalDirectories"] = list(additional_directories)
        result = self.request("session/new", params, timeout=timeout)
        session_id = result.get("sessionId")
        if not session_id:
            raise AcpProtocolError("session/new returned no sessionId")
        config_options = result.get("configOptions")
        if isinstance(config_options, list):
            self.session_config_options = config_options
        return session_id

    def set_mode(
        self,
        session_id: str,
        mode_id: str = "default",
        timeout: float = 30.0,
    ) -> dict:
        """Select the session's permission mode.

        The Claude Code adapter starts in ``auto``, where a classifier
        approves or denies tool use without ever asking the client. Hermes
        must move it to ``default`` so permission decisions come back to us as
        ``session/request_permission``.
        """
        return self.request(
            "session/set_mode",
            {"sessionId": session_id, "modeId": mode_id},
            timeout=timeout,
        )

    def set_config_option(
        self,
        session_id: str,
        config_id: str,
        value: str,
        timeout: float = 60.0,
    ) -> dict:
        """Set one of the session's config options; return the agent's reply.

        The protocol verb behind a model pick (Packet 4 slice B). The reply
        carries the whole refreshed ``configOptions`` list, so the local capture
        is replaced from it — the agent may canonicalise the value it was given
        (an alias resolving to a full model id) or clamp a dependent option, and
        the reply is the only place that lands.

        Raises :class:`AcpError` when the agent rejects the option or the value.
        Nothing local is updated on that path: a rejected pick must leave the
        captured identity exactly as it was.

        SECURITY: this verb is generic — the same call writes ``mode``, whose
        values include ``bypassPermissions`` and ``acceptEdits``. Packet 1's
        entire containment story is the ``default`` mode negotiated in
        ``ClaudeAcpSession._connect()``, so a permission mode must keep going
        through :meth:`set_mode` on that path and must never be reached for
        through this one.
        """
        result = self.request(
            "session/set_config_option",
            {"sessionId": session_id, "configId": config_id, "value": value},
            timeout=timeout,
        )
        config_options = result.get("configOptions")
        if isinstance(config_options, list):
            self.session_config_options = config_options
        return result

    def session_load(
        self,
        session_id: str,
        cwd: Optional[str] = None,
        mcp_servers: Optional[list] = None,
        on_update: Optional[Callable[[dict], None]] = None,
        timeout: float = 120.0,
        additional_directories: Optional[list[str]] = None,
    ) -> dict:
        """Rebind an existing agent session.

        The agent replays the session's history as ``session/update``
        notifications before replying, so this pumps the inbox exactly like
        :meth:`prompt` does.

        The roots are re-sent here for the same reason the permission mode is
        re-negotiated on a rebind: they are session state the agent re-derives
        for the reattached session, so a load that omitted them would silently
        run the rest of the conversation at a different scope.
        """
        params: dict[str, Any] = {
            "sessionId": session_id,
            "cwd": cwd or self._cwd,
            "mcpServers": mcp_servers or [],
        }
        if additional_directories:
            params["additionalDirectories"] = list(additional_directories)
        result = self._request_pumping(
            "session/load",
            params,
            on_update=on_update,
            on_request=None,
            timeout=timeout,
        )
        config_options = result.get("configOptions")
        if isinstance(config_options, list):
            self.session_config_options = config_options
        return result

    def cancel(self, session_id: str) -> None:
        """Ask the agent to abort the in-flight turn.

        Fire-and-forget by design: the turn still resolves through the pending
        ``session/prompt`` reply, with ``stopReason == "cancelled"``.
        """
        self.notify("session/cancel", {"sessionId": session_id})

    def prompt(
        self,
        session_id: str,
        blocks: list[dict],
        on_update: Optional[Callable[[dict], None]] = None,
        on_request: Optional[Callable[[str, dict], dict]] = None,
        on_dispatched: Optional[Callable[[], None]] = None,
        timeout: float = _DEFAULT_PROMPT_TIMEOUT,
    ) -> dict:
        """Run one turn, pumping typed updates and agent requests until it ends.

        ``on_dispatched`` fires once the request is on the wire but before we
        block on the reply — the only moment a caller can act on "the prompt is
        now genuinely in flight" (used to re-issue a cancel the agent may have
        ignored while no turn existed).

        ``on_update`` receives the inner ``update`` object of each
        ``session/update`` notification. ``on_request`` handles agent-initiated
        requests and returns the JSON-RPC ``result`` payload; raising, or
        having no handler at all, still produces an error reply so the agent
        is never left waiting.
        """
        return self._request_pumping(
            "session/prompt",
            {"sessionId": session_id, "prompt": blocks},
            on_update=on_update,
            on_request=on_request,
            timeout=timeout,
            on_dispatched=on_dispatched,
        )

    def _request_pumping(
        self,
        method: str,
        params: dict,
        on_update: Optional[Callable[[dict], None]],
        on_request: Optional[Callable[[str, dict], dict]],
        timeout: float,
        on_dispatched: Optional[Callable[[], None]] = None,
    ) -> dict:
        """Send a long-running request while servicing the inbox."""
        with self._id_lock:
            request_id = self._next_id
            self._next_id += 1

        entry = _Pending(method)
        with self._pending_lock:
            self._pending[request_id] = entry

        try:
            self._write({"jsonrpc": "2.0", "id": request_id, "method": method,
                         "params": params})
        except Exception:
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise

        if on_dispatched is not None:
            try:
                on_dispatched()
            except Exception:
                # A dispatch observer must never break the turn it observes.
                pass

        deadline = time.monotonic() + timeout
        while True:
            if entry.event.is_set():
                # Anything already queued arrived BEFORE this reply — one
                # ordered stdio stream, one reader thread — so it belongs to
                # this request and must still be handled. Returning straight
                # from the reply drops it: an `agent_message_chunk` emitted
                # immediately before `stopReason` would silently vanish from
                # the response, and a tool's completion event with it.
                self._drain_pending(on_update, on_request)
                return self._unwrap(entry.box)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                with self._pending_lock:
                    self._pending.pop(request_id, None)
                raise AcpProtocolError(f"timed out after {timeout}s waiting for {method}")
            try:
                msg = self._inbox.get(timeout=min(0.25, remaining))
            except queue.Empty:
                continue
            if msg is None:
                # Reader signalled EOF; the pending entry was completed with
                # an error, so loop once more to surface it.
                self._inbox.put(None)
                if entry.event.wait(1.0):
                    return self._unwrap(entry.box)
                raise AcpProtocolError("ACP child exited mid-turn")
            self._handle_inbox_message(msg, on_update, on_request)

    def _drain_pending(
        self,
        on_update: Optional[Callable[[dict], None]],
        on_request: Optional[Callable[[str, dict], dict]],
    ) -> None:
        """Handle every message queued right now, then return.

        Used when a reply lands: the tail of the request's own notifications
        can already be sitting in the inbox. The EOF sentinel is preserved —
        dropping it would hide a dead child from the next request loop.
        """
        while True:
            try:
                msg = self._inbox.get_nowait()
            except queue.Empty:
                return
            if msg is None:
                self._inbox.put(None)
                return
            self._handle_inbox_message(msg, on_update, on_request)

    def _handle_inbox_message(
        self,
        msg: dict,
        on_update: Optional[Callable[[dict], None]],
        on_request: Optional[Callable[[str, dict], dict]],
    ) -> None:
        method = msg.get("method")
        params = msg.get("params") or {}
        msg_id = msg.get("id")

        if msg_id is None:
            if method == "session/update" and on_update is not None:
                update = params.get("update") or {}
                try:
                    on_update(update)
                except Exception:
                    # An observer must never break the turn it is observing.
                    pass
            return

        # Agent-initiated request: always reply.
        if on_request is None:
            self.respond_error(
                msg_id, JSONRPC_METHOD_NOT_FOUND,
                f"client does not handle {method}",
            )
            return
        try:
            result = on_request(method, params)
        except PermissionError as exc:
            self.respond_error(msg_id, JSONRPC_INTERNAL_ERROR, str(exc))
        except Exception as exc:
            self.respond_error(msg_id, JSONRPC_INTERNAL_ERROR, f"handler failed: {exc}")
        else:
            self.respond(msg_id, result if result is not None else {})

    # ---------- lifecycle ----------

    def close(self, timeout: float = 5.0) -> None:
        """Close stdin, then reap the child, escalating to kill.

        Idempotent. Leaving a wedged adapter behind is the failure mode this
        method exists to prevent, so every path ends with the process reaped.
        """
        if self._closed:
            return
        self._closed = True
        try:
            if self._proc.stdin and not self._proc.stdin.closed:
                self._proc.stdin.close()
        except Exception:
            pass
        try:
            self._proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            if IS_WINDOWS:
                # The wedged path is exactly where the ordering matters: our
                # direct child is the npm `.cmd` shim, and `terminate()` here
                # would kill it before `taskkill /T` could enumerate the
                # node.exe underneath — orphaning the process this teardown
                # exists to reap. Go straight to the tree kill.
                _terminate_process_tree(self._proc, pgid=self._pgid)
                try:
                    self._proc.wait(timeout=timeout)
                except Exception:
                    pass
            else:
                # POSIX escalates gently first; the group SIGKILL below reaches
                # every descendant regardless of ordering.
                try:
                    self._proc.terminate()
                    self._proc.wait(timeout=timeout)
                except Exception:
                    pass
        except Exception:
            pass
        # Always sweep the tree, even after a clean exit: the adapter's own
        # descendants (Claude Code tool children, or the node grandchild of a
        # Windows .cmd shim) outlive the direct child otherwise.
        _terminate_process_tree(self._proc, pgid=self._pgid)
        try:
            self._proc.wait(timeout=timeout)
        except Exception:
            pass

    def __enter__(self) -> "ClaudeAcpClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
