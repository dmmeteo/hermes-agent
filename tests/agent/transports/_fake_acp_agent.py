"""A minimal fake ACP agent used by the ClaudeAcpClient tests.

Spawned as a real subprocess so the tests exercise real stdio framing,
real threading, and real server-initiated request round-trips rather than
mocks. Deliberately NOT named ``test_*`` so pytest does not collect it.

Behaviour is driven entirely by the methods the client sends:

``initialize``      -> protocolVersion 1 + a small capability set
``session/new``     -> a fixed session id, plus a ``configOptions`` "model"
                       entry (Packet 4 slice A: the authoritative model id)
                       carrying two selectable ``options`` (slice B), plus a
                       ``modes`` block (Packet 5 slice A: the permission mode
                       the session is in and the ones it advertises)
``session/set_config_option``
                    -> switches the "model" entry's ``currentValue`` and
                       replies with the whole refreshed ``configOptions``
                       list, rejecting an unknown option id or a value the
                       list never advertised — the same three outcomes
                       adapter 0.66.0's ``setSessionConfigOption`` produces
``session/prompt``  -> streams two ``session/update`` notifications, then
                       issues an ``fs/read_text_file`` request back at the
                       client, echoes whatever the client answered as a
                       third update, and finally answers ``end_turn``
``session/set_mode``
                    -> moves the session's current mode and echoes it, or
                       errors for a mode it never advertised
``session/cancel``  -> (notification) makes the in-flight prompt answer
                       ``cancelled`` instead of ``end_turn``
``boom``            -> replies with a JSON-RPC error, for error mapping
"""

from __future__ import annotations

import json
import sys
import threading

_cancelled = threading.Event()
_pending_client_replies: dict[int, dict] = {}
_replies_lock = threading.Lock()
_reply_ready = threading.Condition(_replies_lock)

# The session's model state (Packet 4 slice A/B). ACP carries it inside the
# generic `configOptions` list — `currentValue` is the authoritative model id,
# `options` the models this session advertises as selectable.
_MODEL_OPTIONS = [
    {"value": "claude-sonnet-5-20260815", "name": "Sonnet 5"},
    {"value": "claude-haiku-4-5-20251001", "name": "Haiku 4.5"},
]
_current_model = _MODEL_OPTIONS[0]["value"]

# The session's permission mode (Packet 5 slice A). Unlike the model, ACP gives
# this a first-class `modes` block on the session response, so it is reported
# there rather than through `configOptions`. The starting mode is the real
# adapter's own self-approving default, which is why a client negotiates at all.
_AVAILABLE_MODES = [
    {"id": "auto", "name": "Auto"},
    {"id": "default", "name": "Default"},
    {"id": "plan", "name": "Plan"},
]
_current_mode = "auto"


def _modes() -> dict:
    return {"currentModeId": _current_mode, "availableModes": list(_AVAILABLE_MODES)}


def _config_options() -> list[dict]:
    return [{
        "id": "model",
        "name": "Model",
        "type": "select",
        "currentValue": _current_model,
        "options": list(_MODEL_OPTIONS),
    }]


def _send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _notify(method: str, params: dict) -> None:
    _send({"jsonrpc": "2.0", "method": method, "params": params})


def _ask_client(request_id: int, method: str, params: dict) -> dict:
    """Send a server-initiated request and block until the client answers."""
    _send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
    with _reply_ready:
        while request_id not in _pending_client_replies:
            _reply_ready.wait(timeout=10)
        return _pending_client_replies.pop(request_id)


def _handle_prompt(msg: dict) -> None:
    session_id = (msg.get("params") or {}).get("sessionId")

    _notify("session/update", {
        "sessionId": session_id,
        "update": {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": "hello "},
        },
    })
    _notify("session/update", {
        "sessionId": session_id,
        "update": {
            "sessionUpdate": "tool_call",
            "toolCallId": "tc1",
            "title": "Read notes.txt",
            "kind": "read",
            "status": "pending",
        },
    })

    answer = _ask_client(9001, "fs/read_text_file", {
        "sessionId": session_id,
        "path": (msg.get("params") or {}).get("_probePath", "/nonexistent"),
    })
    echoed = (answer.get("result") or {}).get("content")
    if echoed is None:
        echoed = f"error:{(answer.get('error') or {}).get('code')}"

    _notify("session/update", {
        "sessionId": session_id,
        "update": {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": str(echoed)},
        },
    })

    stop = "cancelled" if _cancelled.is_set() else "end_turn"
    _send({"jsonrpc": "2.0", "id": msg["id"], "result": {"stopReason": stop}})


def main() -> None:
    global _current_model, _current_mode
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        method = msg.get("method")

        # A reply from the client to one of our server-initiated requests.
        if method is None and "id" in msg:
            with _reply_ready:
                _pending_client_replies[msg["id"]] = msg
                _reply_ready.notify_all()
            continue

        if method == "session/cancel":
            _cancelled.set()
            continue

        if method == "initialize":
            _send({"jsonrpc": "2.0", "id": msg["id"], "result": {
                "protocolVersion": 1,
                "agentCapabilities": {"loadSession": True, "promptCapabilities": {"image": True}},
                "authMethods": [],
            }})
        elif method == "session/new":
            _send({"jsonrpc": "2.0", "id": msg["id"], "result": {
                "sessionId": "sess-1",
                # Mirrors the real adapter's `configOptions`, which always
                # carries a "model" entry (Packet 4 slice A) — the only place
                # ACP exposes the session's authoritative model id.
                "configOptions": _config_options(),
                # ...and the `modes` block, which is where the permission mode
                # genuinely lives (Packet 5 slice A).
                "modes": _modes(),
            }})
        elif method == "session/set_config_option":
            params = msg.get("params") or {}
            config_id = params.get("configId")
            value = params.get("value")
            if config_id != "model":
                _send({"jsonrpc": "2.0", "id": msg["id"], "error": {
                    "code": -32603,
                    "message": f"Unknown config option: {config_id}",
                }})
                continue
            if value not in [o["value"] for o in _MODEL_OPTIONS]:
                _send({"jsonrpc": "2.0", "id": msg["id"], "error": {
                    "code": -32603,
                    "message": f"Invalid value for config option model: {value}",
                }})
                continue
            _current_model = value
            # The real adapter answers the RPC with the refreshed list and
            # does NOT also notify — `config_option_update` is reserved for
            # changes the client did not ask for (the refusal fallback).
            _send({"jsonrpc": "2.0", "id": msg["id"], "result": {
                "configOptions": _config_options(),
            }})
        elif method == "session/set_mode":
            mode_id = (msg.get("params") or {}).get("modeId")
            if mode_id not in [m["id"] for m in _AVAILABLE_MODES]:
                _send({"jsonrpc": "2.0", "id": msg["id"], "error": {
                    "code": -32603, "message": f"Unknown mode: {mode_id}",
                }})
                continue
            _current_mode = mode_id
            # Echo the requested mode so the client can assert on it.
            _send({"jsonrpc": "2.0", "id": msg["id"], "result": {
                "modeId": mode_id,
            }})
        elif method == "session/prompt":
            # Run on a worker so the reader loop can still receive our
            # fs/read_text_file reply while the prompt is in flight.
            threading.Thread(target=_handle_prompt, args=(msg,), daemon=True).start()
        elif method == "boom":
            _send({"jsonrpc": "2.0", "id": msg["id"], "error": {
                "code": -32000, "message": "kaboom", "data": {"detail": "x"},
            }})
        else:
            _send({"jsonrpc": "2.0", "id": msg["id"], "error": {
                "code": -32601, "message": f"unknown method {method}",
            }})


if __name__ == "__main__":
    main()
