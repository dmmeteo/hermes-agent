"""A fake `claude-agent-acp` adapter for the Packet 1 runtime tests.

Spawned as a real subprocess so the runtime is exercised over real stdio
framing. Deliberately NOT named ``test_*`` so pytest does not collect it.

Env vars:

``FAKE_ACP_RECORD``  path to write a JSON record of everything the adapter
                     observed — the ``session/prompt`` params, every
                     ``session/set_mode`` call, the Anthropic-related env vars
                     it inherited, and any permission decision it received.
``FAKE_ACP_MODE``
    ``normal``        stream text + a tool_call, then ``end_turn``
    ``error``         reply to ``session/prompt`` with a JSON-RPC error
    ``slow``          stream slowly until cancelled
    ``ignore-cancel`` ignore ``session/cancel`` and still answer ``end_turn``
    ``refusal``       answer ``{"stopReason": "refusal"}`` with no text
    ``permission``    ask the client for permission to write, honour the answer
    ``grandchild``    spawn a long-lived grandchild, then behave like ``normal``
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
_cancelled = threading.Event()

# Anthropic credential vars must NOT reach this child: the provider advertises
# subscription auth. The runtime strips them; the tests assert on this record.
_WATCHED_ENV_VARS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
)

_record: dict = {
    # Our own pid, so tests can assert on THIS adapter rather than pgrep-ing
    # for every fake adapter on the machine (test files run in parallel).
    "adapter_pid": os.getpid(),
    "prompt": None,
    "set_mode_calls": [],
    "inherited_env": {v: os.environ.get(v) for v in _WATCHED_ENV_VARS},
    "permission_outcome": None,
    "wrote_file": False,
    "grandchild_pid": None,
}
_record_lock = threading.Lock()

_pending_replies: dict = {}
_reply_ready = threading.Condition()


def _flush_record() -> None:
    if not _RECORD_PATH:
        return
    with _record_lock:
        snapshot = json.loads(json.dumps(_record))
    with open(_RECORD_PATH, "w", encoding="utf-8") as handle:
        json.dump(snapshot, handle)


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


def _handle_permission(session_id: str, msg: dict) -> None:
    target = os.path.join(os.getcwd(), "permission-probe.txt")
    answer = _ask_client(9101, "session/request_permission", {
        "sessionId": session_id,
        "toolCall": {"toolCallId": "tc-write", "title": "Write permission-probe.txt",
                     "kind": "edit"},
        "options": [
            {"optionId": "allow", "name": "Allow", "kind": "allow_once"},
            {"optionId": "deny", "name": "Deny", "kind": "reject_once"},
        ],
    })
    outcome = (answer.get("result") or {}).get("outcome") or {}
    with _record_lock:
        _record["permission_outcome"] = outcome
    allowed = outcome.get("outcome") == "selected" and outcome.get("optionId") == "allow"
    if allowed:
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("written despite policy")
        with _record_lock:
            _record["wrote_file"] = True
    _flush_record()
    _text(session_id, "Write was declined." if not allowed else "Wrote the file.")
    _send({"jsonrpc": "2.0", "id": msg["id"], "result": {"stopReason": "end_turn"}})


def _handle_prompt(msg: dict) -> None:
    params = msg.get("params") or {}
    session_id = params.get("sessionId")
    with _record_lock:
        _record["prompt"] = params
    _flush_record()

    if _MODE == "error":
        _send({"jsonrpc": "2.0", "id": msg["id"],
               "error": {"code": -32000, "message": "adapter exploded"}})
        return

    if _MODE == "refusal":
        # Terminal but unsuccessful: no text at all.
        _send({"jsonrpc": "2.0", "id": msg["id"], "result": {"stopReason": "refusal"}})
        return

    if _MODE == "permission":
        _handle_permission(session_id, msg)
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
            _send({"jsonrpc": "2.0", "id": msg["id"], "result": {
                "protocolVersion": 1,
                "agentCapabilities": {"loadSession": True},
                "authMethods": auth_methods,
            }})
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
            _send({"jsonrpc": "2.0", "id": msg["id"],
                   "result": {"sessionId": "fake-session-1"}})
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
