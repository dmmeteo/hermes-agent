"""A fake `claude-agent-acp` adapter for the Packet 1 runtime tests.

Spawned as a real subprocess so the runtime is exercised over real stdio
framing. Deliberately NOT named ``test_*`` so pytest does not collect it.

Env vars:

``FAKE_ACP_RECORD``  path to write a JSON record of everything the adapter
                     observed — the ``session/prompt`` params, every
                     ``session/set_mode`` call, the Anthropic-related env vars
                     it inherited, and any permission decision it received.
``FAKE_ACP_RECORD_DIR``
                     directory variant of the above: each adapter *process*
                     writes ``<dir>/<pid>.json``. Packet 2 spans several
                     adapter processes per conversation (restart, idle
                     eviction), and a single shared path would let the newest
                     adapter overwrite the evidence of the previous one.
``FAKE_ACP_STORE``   directory holding this adapter's own agent-side session
                     state, one ``<sessionId>.json`` per session. Models the
                     real adapter's ``~/.claude`` history: it survives the
                     adapter process, so ``session/load`` can genuinely recall
                     earlier turns. Hermes must never write here.
``FAKE_ACP_MODE``
    ``normal``        stream text + a tool_call, then ``end_turn``
    ``memory``        answer with the prompts this ACP session has already
                      seen (``recall[...]``), then remember the new one.
                      Requires ``FAKE_ACP_STORE``.
    ``memory-gated``  like ``memory``, but hold the turn open until the path
                      in ``FAKE_ACP_GATE`` exists
    ``hang``          accept the prompt and never answer it
    ``no-load``       like ``memory``, but advertise ``loadSession: false``
    ``load-fails``    like ``memory``, but fail every ``session/load``
    ``load-replays-late``
                      like ``memory``, but emit one history update AFTER the
                      ``session/load`` reply — the race that would otherwise
                      leak replayed history into the next turn's response
    ``error``         reply to ``session/prompt`` with a JSON-RPC error
    ``slow``          stream slowly until cancelled
    ``ignore-cancel`` ignore ``session/cancel`` and still answer ``end_turn``
    ``refusal``       answer ``{"stopReason": "refusal"}`` with no text
    ``permission``    ask the client for permission to write, honour the answer.
                      The request carries credential-shaped values in its
                      ``title`` / ``rawInput`` and reports a location OUTSIDE
                      the workspace, so the approval prompt can be asserted on
                      for scrubbing and containment
    ``permission-no-once``
                      like ``permission``, but offer only ``allow_always`` as
                      the permitting option — Hermes must refuse rather than
                      hand the adapter a standing grant
                      (``permission`` asks once per prompt, so a second turn on
                      the same session asks a second time)
    ``permission-subagent``
                      like ``permission``, but the tool call is attributed to a
                      parent (a sub-agent's tool use) — the ONLY shape in which
                      adapter 0.66.0 puts ``_meta.claudeCode.toolName`` on the
                      permission request itself
    ``permission-after-partial``
                      stream text whose last character could open a ``<think>``
                      tag (so the streaming scrubber holds it back), THEN ask
                      for permission — the ordering that decides whether the
                      held tail renders before or after the approval prompt
    ``fs-requests``   ask the client for ``fs/write_text_file`` inside the
                      workspace and ``fs/read_text_file`` outside it, record
                      both replies, then finish normally. Models an agent that
                      calls back into the client's filesystem regardless of the
                      capabilities the client advertised.
    ``no-additional-dirs``
                      like ``memory``, but advertise NO
                      ``sessionCapabilities.additionalDirectories`` — an older
                      adapter that cannot widen a session's roots
    ``grandchild``    spawn a long-lived grandchild, then behave like ``normal``
    ``cancel-grandchild``
                      spawn a long-lived grandchild, then block until cancelled
                      and answer ``cancelled`` — the shape a stopped turn leaves
                      behind if teardown only reaps the direct child
    ``cancel-empty``  block until cancelled and answer ``cancelled`` with NO text
    ``auth-required`` report an auth method from ``initialize``, then fail
                      ``session/new`` the way an unauthenticated adapter does
    ``stderr-noise``  emit diagnostics (and a secret) on stderr, then fail
    ``wedged``        never exit on stdin EOF — models an adapter that has to
                      be torn down rather than asked to stop
    ``cancel-after-prompt-only``
                      ignore any ``session/cancel`` that arrived before the
                      prompt was dispatched, and only honour one received
                      afterwards — what a real adapter may legitimately do
    ``tools``         emit the full typed-event zoo a real turn produces:
                      ``tool_call`` / ``tool_call_update`` (including a failed
                      one and an update-only one), credential-shaped values in
                      ``rawInput`` / ``rawOutput`` / ``title``,
                      ``user_message_chunk``, ``agent_thought_chunk``,
                      ``plan``, ``available_commands_update``,
                      ``usage_update``, and a ``usage`` block on the reply
    ``partial-tag``   answer with text whose last character could start a
                      ``<think>`` tag, so the streaming scrubber holds it back
                      until the turn is flushed
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time

_MODE = os.getenv("FAKE_ACP_MODE", "normal")
_RECORD_PATH = os.getenv("FAKE_ACP_RECORD", "")
_RECORD_DIR = os.getenv("FAKE_ACP_RECORD_DIR", "")
_STORE_DIR = os.getenv("FAKE_ACP_STORE", "")
_cancelled = threading.Event()
_session_counter = 0

# Anthropic credential vars must NOT reach this child: the provider advertises
# subscription auth. The runtime strips them; the tests assert on this record.
# ``FAKE_ACP_WATCH_ENV`` adds further names (comma-separated) so a test can
# assert about an unrelated provider/tool secret without this list growing one
# entry per provider.
_WATCHED_ENV_VARS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
) + tuple(
    name.strip()
    for name in os.getenv("FAKE_ACP_WATCH_ENV", "").split(",")
    if name.strip()
)

_record: dict = {
    # Our own pid, so tests can assert on THIS adapter rather than pgrep-ing
    # for every fake adapter on the machine (test files run in parallel).
    "adapter_pid": os.getpid(),
    "prompt": None,
    "set_mode_calls": [],
    "inherited_env": {v: os.environ.get(v) for v in _WATCHED_ENV_VARS},
    "permission_outcome": None,
    # Every permission outcome this adapter process received, in order. The
    # single-slot field above stays for the Packet 1 tests; a multi-turn
    # approval test needs the whole sequence.
    "permission_outcomes": [],
    "permission_requests": 0,
    "wrote_file": False,
    "grandchild_pid": None,
    # Packet 2 evidence: which sessions this adapter process created vs.
    # rebound, and every prompt it was given (so recap content is assertable).
    "session_new_calls": [],
    "session_load_calls": [],
    "prompts": [],
    # Packet 3C evidence. The workspace roots are negotiated in the params of
    # session/new and session/load, so the full params of both are recorded —
    # asserting on `cwd` + `additionalDirectories` is the whole containment
    # contract on this side of the wire.
    "session_new_params": [],
    "session_load_params": [],
    # What the client advertised it can do. `fs` / `terminal` here decide
    # whether the agent is entitled to call back into the client at all.
    "client_capabilities": None,
    # Every session/cancel this adapter received, in order.
    "cancel_calls": [],
    # Replies to the agent-initiated requests this adapter sent, so a test can
    # assert HOW the client refused rather than only that the turn survived.
    "client_request_replies": [],
}
_record_lock = threading.Lock()

_pending_replies: dict = {}
_reply_ready = threading.Condition()


def _flush_record() -> None:
    if not _RECORD_PATH and not _RECORD_DIR:
        return
    with _record_lock:
        snapshot = json.loads(json.dumps(_record))
    for path in (
        _RECORD_PATH,
        os.path.join(_RECORD_DIR, f"{os.getpid()}.json") if _RECORD_DIR else "",
    ):
        if not path:
            continue
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(snapshot, handle)


# ---------------------------------------------------------------- agent store


def _store_path(session_id: str) -> str:
    return os.path.join(_STORE_DIR, f"{session_id}.json")


def _store_read(session_id: str) -> dict:
    try:
        with open(_store_path(session_id), "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


def _store_write(session_id: str, data: dict) -> None:
    if not _STORE_DIR:
        return
    os.makedirs(_STORE_DIR, exist_ok=True)
    with open(_store_path(session_id), "w", encoding="utf-8") as handle:
        json.dump(data, handle)


def _prompt_text(params: dict) -> str:
    """Flatten the ACP content blocks of a prompt into plain text."""
    return " ".join(
        str(block.get("text") or "")
        for block in (params.get("prompt") or [])
        if isinstance(block, dict)
    ).strip()


def _send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _update(session_id: str, update: dict) -> None:
    _send({"jsonrpc": "2.0", "method": "session/update",
           "params": {"sessionId": session_id, "update": update}})


def _text(session_id: str, text: str) -> None:
    _update(session_id, {"sessionUpdate": "agent_message_chunk",
                         "content": {"type": "text", "text": text}})


def _ask_client(request_id: int, method: str, params: dict) -> dict:
    """Send a server-initiated request and block until the client answers."""
    _send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
    with _reply_ready:
        while request_id not in _pending_replies:
            _reply_ready.wait(timeout=15)
        return _pending_replies.pop(request_id)


# Credential-shaped values planted in tool payloads. The projector surfaces
# tool activity on Hermes' display callbacks, so every field it touches must be
# scrubbed; the tests assert these literals never reach a callback.
TOOL_INPUT_SECRET = "sk-ant-api03-FAKEINPUTSECRET0123456789"
TOOL_OUTPUT_SECRET = "ghp_FAKEOUTPUTSECRET0123456789abcd"

# Per-turn token usage on the `session/prompt` reply, which is where ACP
# reports authoritative counts (`usage_update` carries context occupancy and
# cost, not per-turn deltas). totalTokens is the sum of the four buckets.
TURN_USAGE = {
    "inputTokens": 120,
    "outputTokens": 30,
    "cachedReadTokens": 900,
    "cachedWriteTokens": 15,
    "thoughtTokens": 7,
    "totalTokens": 1065,
}

# Context occupancy reported mid-turn. `used` is NOT a per-turn token count —
# summing it into session token totals is the mistake the tests guard against.
CONTEXT_USED_TOKENS = 1234
CONTEXT_WINDOW_TOKENS = 200000
TURN_COST_USD = 0.42

# A path the agent reports as a tool location. Projection is display-only, so
# this file must never come into existence.
REPORTED_LOCATION = "reported-but-absent.txt"


def _handle_typed_events(session_id: str, msg: dict) -> None:
    """Emit the typed session/update zoo, then finish the turn."""
    if _MODE == "partial-tag":
        _text(session_id, "trailing bracket <")
        _send({"jsonrpc": "2.0", "id": msg["id"],
               "result": {"stopReason": "end_turn", "usage": dict(TURN_USAGE)}})
        return

    _text(session_id, "Reading. ")
    _update(session_id, {
        "sessionUpdate": "tool_call",
        "toolCallId": "tc-read",
        "title": f"Read notes.txt with key {TOOL_INPUT_SECRET}",
        "kind": "read",
        "status": "pending",
        "rawInput": {"file_path": "notes.txt", "api_key": TOOL_INPUT_SECRET},
        "locations": [{"path": os.path.join(os.getcwd(), REPORTED_LOCATION)}],
        "_meta": {"claudeCode": {"toolName": "Read"}},
    })
    _update(session_id, {"sessionUpdate": "tool_call_update",
                         "toolCallId": "tc-read", "status": "in_progress"})
    _update(session_id, {
        "sessionUpdate": "tool_call_update",
        "toolCallId": "tc-read",
        "status": "completed",
        "rawOutput": {"content": f"42 (token {TOOL_OUTPUT_SECRET})"},
    })
    # A tool that fails: the display must say so rather than reporting success.
    _update(session_id, {
        "sessionUpdate": "tool_call",
        "toolCallId": "tc-bash",
        "title": "Run the tests",
        "kind": "execute",
        "status": "pending",
        "rawInput": {"command": "pytest -x"},
        "_meta": {"claudeCode": {"toolName": "Bash"}},
    })
    _update(session_id, {
        "sessionUpdate": "tool_call_update",
        "toolCallId": "tc-bash",
        "status": "failed",
        "rawOutput": {"stderr": "1 failed"},
    })
    # A tool the client only ever sees as an update — the shape a replayed or
    # coalesced tool call arrives in, where there is no preceding tool_call.
    _update(session_id, {
        "sessionUpdate": "tool_call_update",
        "toolCallId": "tc-orphan",
        "status": "completed",
        "title": "Grep for TODO",
        "kind": "search",
        "rawInput": {"pattern": "TODO"},
        "rawOutput": "3 matches",
        "_meta": {"claudeCode": {"toolName": "Grep"}},
    })
    # A tool that is already finished when the client first sees it — the shape
    # a very fast call takes. It never gets its own opening event on the wire.
    _update(session_id, {
        "sessionUpdate": "tool_call",
        "toolCallId": "tc-instant",
        "title": "List the workspace",
        "kind": "search",
        "status": "completed",
        "rawInput": {"path": "."},
        "rawOutput": "2 entries",
        "_meta": {"claudeCode": {"toolName": "Glob"}},
    })
    # Everything below carries text that is NOT the assistant's reply. None of
    # it may reach the response or the streaming surface.
    _update(session_id, {"sessionUpdate": "user_message_chunk",
                         "content": {"type": "text", "text": "ECHOED-USER-PROMPT"}})
    _update(session_id, {"sessionUpdate": "agent_thought_chunk",
                         "content": {"type": "text", "text": "THOUGHT-not-response"}})
    _update(session_id, {"sessionUpdate": "plan", "entries": [
        {"content": "PLAN-ENTRY", "priority": "high", "status": "pending"}]})
    _update(session_id, {"sessionUpdate": "available_commands_update",
                         "availableCommands": [
                             {"name": "compact", "description": "COMMAND-DESC"}]})
    _update(session_id, {"sessionUpdate": "usage_update",
                         "used": CONTEXT_USED_TOKENS,
                         "size": CONTEXT_WINDOW_TOKENS,
                         "cost": {"amount": TURN_COST_USD, "currency": "USD"}})
    _text(session_id, "Done.")
    _send({"jsonrpc": "2.0", "id": msg["id"],
           "result": {"stopReason": "end_turn", "usage": dict(TURN_USAGE)}})


#: Paths the ``fs-requests`` mode asks the client to touch. The second one sits
#: outside any workspace the client could have negotiated, so a client that
#: served it would have escaped its own containment.
FS_REQUEST_INSIDE = "fs-request-inside-workspace.txt"
FS_REQUEST_OUTSIDE = "/hermes-acp-fs-request-outside-root-should-not-exist.txt"


def _handle_fs_requests(session_id: str, msg: dict) -> None:
    """Call back into the client's filesystem, inside and outside the workspace.

    Both are answered by the client one way or another — an unanswered agent
    request deadlocks the turn — so the turn finishing at all is part of what
    this mode proves.
    """
    inside = os.path.join(os.getcwd(), FS_REQUEST_INSIDE)
    for request_id, method, params in (
        (9200, "fs/write_text_file",
         {"sessionId": session_id, "path": inside, "content": "written by the agent"}),
        (9201, "fs/read_text_file",
         {"sessionId": session_id, "path": FS_REQUEST_OUTSIDE}),
    ):
        reply = _ask_client(request_id, method, params)
        with _record_lock:
            _record["client_request_replies"].append({
                "method": method,
                "path": params.get("path"),
                "error": reply.get("error"),
                "result": reply.get("result"),
            })
        _flush_record()
    _text(session_id, "Filesystem round-trip done.")
    _send({"jsonrpc": "2.0", "id": msg["id"], "result": {"stopReason": "end_turn"}})


def _spawn_grandchild() -> None:
    """A descendant that outlives the adapter unless the whole tree is killed."""
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(600)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    with _record_lock:
        _record["grandchild_pid"] = child.pid
    _flush_record()


#: A path the permission request reports as a write location while sitting
#: outside the adapter's workspace. Hermes only ever displays it, so it must
#: never come into existence — and the user must still be shown it.
OUTSIDE_WORKSPACE_LOCATION = "/hermes-acp-outside-workspace-should-not-exist.txt"

_permission_request_counter = 0


def _handle_permission(session_id: str, msg: dict) -> None:
    global _permission_request_counter
    target = os.path.join(os.getcwd(), "permission-probe.txt")
    if _MODE == "permission-no-once":
        # The only permitting option is a STANDING grant. Answering it would
        # move policy into the adapter, so Hermes must refuse instead.
        options = [
            {"optionId": "allow-always", "name": "Allow always", "kind": "allow_always"},
            {"optionId": "deny", "name": "Deny", "kind": "reject_once"},
        ]
    else:
        options = [
            {"optionId": "allow", "name": "Allow", "kind": "allow_once"},
            {"optionId": "allow-always", "name": "Allow always", "kind": "allow_always"},
            {"optionId": "deny", "name": "Deny", "kind": "reject_once"},
        ]
    _permission_request_counter += 1
    with _record_lock:
        _record["permission_requests"] = _permission_request_counter
    # Flush BEFORE blocking on the answer: a test that waits for "the adapter
    # has asked" must be able to see it while the request is still open.
    _flush_record()
    # Adapter 0.66.0 fidelity, part 1: `ensureToolCallEmitted` sends the
    # referenced `tool_call` BEFORE asking, so the client already has the call
    # a permission request points at (issue #851). That update carries
    # `_meta.claudeCode.toolName` unconditionally — unlike the request itself.
    call = {
        "toolCallId": "tc-write",
        # Credential-shaped noise in the human-facing fields: the approval
        # prompt is user-visible text and must be scrubbed like any other.
        "title": f"Write permission-probe.txt ({TOOL_OUTPUT_SECRET})",
        "kind": "edit",
        "rawInput": {"file_path": target, "token": TOOL_INPUT_SECRET},
        "locations": [{"path": target}, {"path": OUTSIDE_WORKSPACE_LOCATION}],
    }
    parented = _MODE == "permission-subagent"
    _update(session_id, {
        "sessionUpdate": "tool_call",
        "status": "pending",
        **call,
        "_meta": {
            "claudeCode": {
                "toolName": "Write",
                **({"parentToolUseId": "tc-task"} if parented else {}),
            }
        },
    })
    # Adapter 0.66.0 fidelity, part 2: on the *permission request* the
    # `claudeCode` meta block is emitted ONLY for a parented (sub-agent) tool
    # call — `...(parentToolUseId ? { _meta: ... } : {})` at both
    # `requestPermissionFromClient` sites. A top-level Bash/Write therefore
    # arrives with no declared tool name at all.
    answer = _ask_client(9100 + _permission_request_counter, "session/request_permission", {
        "sessionId": session_id,
        "toolCall": {
            **call,
            **({"_meta": {"claudeCode": {
                "toolName": "Write", "parentToolUseId": "tc-task",
            }}} if parented else {}),
        },
        "options": options,
    })
    outcome = (answer.get("result") or {}).get("outcome") or {}
    with _record_lock:
        _record["permission_outcome"] = outcome
        _record["permission_outcomes"].append(outcome)
    # A real adapter honours whichever permitting option was selected, so this
    # one does too: the "never hand over a standing grant" invariant is asserted
    # on the recorded optionId, not smuggled in as a refusal to write.
    allowed = outcome.get("outcome") == "selected" and outcome.get("optionId") in (
        "allow", "allow-always",
    )
    if allowed:
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("written despite policy")
        with _record_lock:
            _record["wrote_file"] = True
    _flush_record()
    _text(session_id, "Write was declined." if not allowed else "Wrote the file.")
    _send({"jsonrpc": "2.0", "id": msg["id"], "result": {"stopReason": "end_turn"}})


def _new_session_id() -> str:
    """A fresh session id, unique across adapter processes and sessions."""
    global _session_counter
    _session_counter += 1
    session_id = f"fake-session-{os.getpid()}-{_session_counter}"
    with _record_lock:
        _record["session_new_calls"].append(session_id)
    if _STORE_DIR:
        _store_write(session_id, {"prompts": []})
    _flush_record()
    return session_id


def _handle_load(msg: dict) -> None:
    """Rebind an existing session, replaying its history first."""
    params = msg.get("params") or {}
    session_id = params.get("sessionId") or ""
    with _record_lock:
        _record["session_load_calls"].append(session_id)
        _record["session_load_params"].append(params)
    _flush_record()

    if _MODE == "load-fails":
        _send({"jsonrpc": "2.0", "id": msg["id"], "error": {
            "code": -32002, "message": "session cannot be resumed",
        }})
        return

    stored = _store_read(session_id)
    if not stored:
        _send({"jsonrpc": "2.0", "id": msg["id"], "error": {
            "code": -32002, "message": f"unknown session {session_id}",
        }})
        return

    # A real adapter replays the session transcript as notifications before
    # answering. Hermes owns the transcript, so none of this may ever reach
    # the user-visible response.
    for prompt in stored.get("prompts") or []:
        _text(session_id, f"REPLAYED-HISTORY:{prompt}")
    _send({"jsonrpc": "2.0", "id": msg["id"], "result": {}})
    if _MODE == "load-replays-late":
        # The nastier shape of the same hazard: a replay notification emitted
        # AFTER the load reply, i.e. once the client has moved on. stdio is
        # ordered, so the client is guaranteed to see this before the reply to
        # whatever it sends next — draining before the prompt is what stops it
        # from being read as this turn's output.
        _text(session_id, "REPLAYED-HISTORY:late-arrival")


def _handle_prompt(msg: dict) -> None:
    params = msg.get("params") or {}
    session_id = params.get("sessionId")
    with _record_lock:
        _record["prompt"] = params
        _record["prompts"].append(params)
    _flush_record()

    if _MODE == "hang":
        # Never answers: models an adapter wedged inside a turn. The caller
        # has to hit its own turn timeout and retire the session.
        while True:
            time.sleep(0.2)

    if _MODE in (
        "memory", "no-load", "load-fails", "load-replays-late", "memory-gated",
        "no-additional-dirs",
    ):
        if _MODE == "memory-gated":
            # Hold the turn open until the test opens the gate, so the caller
            # can observe in-flight state (the turn lease) from another thread.
            gate = os.getenv("FAKE_ACP_GATE", "")
            while gate and not os.path.exists(gate):
                time.sleep(0.05)
        # Answer from this ACP session's own remembered history, then add the
        # new turn to it. The store outlives the process, so a reloaded
        # session recalls turns an earlier adapter served.
        stored = _store_read(session_id)
        prior = stored.get("prompts") or []
        _text(session_id, "recall[" + " | ".join(prior) + "]")
        prior.append(_prompt_text(params))
        _store_write(session_id, {"prompts": prior})
        _send({"jsonrpc": "2.0", "id": msg["id"], "result": {"stopReason": "end_turn"}})
        return

    if _MODE in ("tools", "partial-tag"):
        _handle_typed_events(session_id, msg)
        return

    if _MODE == "error":
        _send({"jsonrpc": "2.0", "id": msg["id"],
               "error": {"code": -32000, "message": "adapter exploded"}})
        return

    if _MODE == "refusal":
        # Terminal but unsuccessful: no text at all.
        _send({"jsonrpc": "2.0", "id": msg["id"], "result": {"stopReason": "refusal"}})
        return

    if _MODE in ("permission", "permission-no-once", "permission-subagent"):
        _handle_permission(session_id, msg)
        return

    if _MODE == "permission-after-partial":
        # The scrubber holds a trailing '<' until something resolves it, so the
        # approval prompt is drawn while text the user has not seen is still in
        # flight. Whether that tail arrives before or after the prompt is the
        # ordering under test.
        _text(session_id, "before the prompt <")
        _handle_permission(session_id, msg)
        return

    if _MODE == "fs-requests":
        _handle_fs_requests(session_id, msg)
        return

    if _MODE == "cancel-grandchild":
        _spawn_grandchild()
        _text(session_id, "working ")
        while not _cancelled.wait(timeout=0.05):
            pass
        _send({"jsonrpc": "2.0", "id": msg["id"], "result": {"stopReason": "cancelled"}})
        return

    if _MODE == "slow":
        _text(session_id, "starting ")
        while not _cancelled.wait(timeout=0.05):
            pass
        _send({"jsonrpc": "2.0", "id": msg["id"], "result": {"stopReason": "cancelled"}})
        return

    if _MODE == "cancel-empty":
        # Interrupted before the model emitted anything — no text at all.
        while not _cancelled.wait(timeout=0.05):
            pass
        _send({"jsonrpc": "2.0", "id": msg["id"], "result": {"stopReason": "cancelled"}})
        return

    if _MODE == "cancel-after-prompt-only":
        # A real adapter may legitimately ignore a session/cancel that arrived
        # before any prompt was in flight. Drop whatever we saw before now and
        # require a fresh one.
        _cancelled.clear()
        while not _cancelled.wait(timeout=0.05):
            pass
        _send({"jsonrpc": "2.0", "id": msg["id"], "result": {"stopReason": "cancelled"}})
        return

    if _MODE == "ignore-cancel":
        # Models the real race: cancel lands while the turn is already
        # finishing, so the adapter still reports a normal completion.
        _text(session_id, "done anyway.")
        time.sleep(0.4)
        _send({"jsonrpc": "2.0", "id": msg["id"], "result": {"stopReason": "end_turn"}})
        return

    if _MODE == "grandchild":
        _spawn_grandchild()

    _text(session_id, "The answer ")
    # A structured tool call. Its payload must NEVER appear in the assembled
    # response text — that was the direct-CLI failure mode this runtime fixes.
    _update(session_id, {
        "sessionUpdate": "tool_call",
        "toolCallId": "tc-1",
        "title": "Read notes.txt",
        "kind": "read",
        "status": "pending",
        "rawInput": {"file_path": "/tmp/notes.txt"},
    })
    _update(session_id, {
        "sessionUpdate": "tool_call_update",
        "toolCallId": "tc-1",
        "status": "completed",
        "rawOutput": "<tool_result>{\"secret\": \"leaked-payload\"}</tool_result>",
    })
    _update(session_id, {"sessionUpdate": "agent_thought_chunk",
                         "content": {"type": "text", "text": "thinking-should-not-leak"}})
    _text(session_id, "is 42.")
    time.sleep(0.02)
    _send({"jsonrpc": "2.0", "id": msg["id"], "result": {"stopReason": "end_turn"}})


def main() -> None:
    _flush_record()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        method = msg.get("method")

        # A reply from the client to one of our server-initiated requests.
        if method is None and "id" in msg:
            with _reply_ready:
                _pending_replies[msg["id"]] = msg
                _reply_ready.notify_all()
            continue

        if method == "session/cancel":
            with _record_lock:
                _record["cancel_calls"].append(
                    (msg.get("params") or {}).get("sessionId")
                )
            _flush_record()
            if _MODE != "ignore-cancel":
                _cancelled.set()
            continue
        if method == "initialize":
            # An adapter with no usable credential advertises an auth method
            # it wants the client to complete first.
            auth_methods = (
                [{"id": "claude-login", "name": "Log in with Claude"}]
                if _MODE == "auth-required" else []
            )
            # Mirrors the real adapter: widening a session's filesystem scope
            # is a session capability the client must be able to gate on.
            session_capabilities = (
                {} if _MODE == "no-additional-dirs"
                else {"additionalDirectories": {}}
            )
            with _record_lock:
                _record["client_capabilities"] = (
                    (msg.get("params") or {}).get("clientCapabilities")
                )
            _flush_record()
            _send({"jsonrpc": "2.0", "id": msg["id"], "result": {
                "protocolVersion": 1,
                "agentCapabilities": {
                    "loadSession": _MODE != "no-load",
                    "sessionCapabilities": session_capabilities,
                },
                "authMethods": auth_methods,
            }})
        elif method == "session/load":
            _handle_load(msg)
        elif method == "session/new":
            if _MODE == "auth-required":
                _send({"jsonrpc": "2.0", "id": msg["id"], "error": {
                    "code": -32000, "message": "Authentication required",
                }})
                continue
            if _MODE == "stderr-noise":
                # Realistic adapter diagnostics: a useful line next to one that
                # must never be echoed back to the user.
                for line in (
                    "adapter-diagnostic-marker: could not reach the model endpoint",
                    "using key sk-ant-SUPERSECRET-must-never-be-echoed",
                ):
                    sys.stderr.write(line + "\n")
                sys.stderr.flush()
                time.sleep(0.2)
                _send({"jsonrpc": "2.0", "id": msg["id"], "error": {
                    "code": -32001, "message": "session could not be created",
                }})
                continue
            with _record_lock:
                _record["session_new_params"].append(msg.get("params") or {})
            _send({"jsonrpc": "2.0", "id": msg["id"],
                   "result": {"sessionId": _new_session_id()}})
        elif method == "session/set_mode":
            with _record_lock:
                _record["set_mode_calls"].append((msg.get("params") or {}).get("modeId"))
            _flush_record()
            _send({"jsonrpc": "2.0", "id": msg["id"], "result": {}})
        elif method == "session/prompt":
            threading.Thread(target=_handle_prompt, args=(msg,), daemon=True).start()
        else:
            _send({"jsonrpc": "2.0", "id": msg["id"],
                   "error": {"code": -32601, "message": f"unknown {method}"}})

    if _MODE == "wedged":
        # stdin hit EOF and we deliberately do NOT exit: the caller has to tear
        # this process down. On Windows that is the case where killing the
        # .cmd shim first would orphan the real node child.
        time.sleep(600)


if __name__ == "__main__":
    main()
