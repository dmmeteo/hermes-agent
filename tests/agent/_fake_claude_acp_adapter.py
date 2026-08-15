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
    ``permission-sudo-stdin``
                      like ``permission``, but the tool call is a ``Bash``
                      command piping a guessed password into ``sudo -S`` —
                      shaped as adapter 0.66.0 shapes one (the command verbatim
                      in ``rawInput.command`` and copied into ``title``). The
                      unconditional floor must refuse it AND answer the request,
                      with no human consulted
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
    ``cancel-mid-tool``
                      open a ``tool_call`` and never report its outcome, then
                      block until cancelled and answer ``cancelled`` — the one
                      shape that leaves a tool call with no terminal update, and
                      therefore the shape that decides whether a transcript ends
                      up with an orphan ``tool_calls`` row (Packet 5 slice D)
    ``partial-tag``   answer with text whose last character could start a
                      ``<think>`` tag, so the streaming scrubber holds it back
                      until the turn is flushed
    ``model-fallback``
                      like ``normal``, but emits a ``config_option_update``
                      part-way through the turn switching the "model"
                      ``configOptions`` entry from ``FAKE_ACP_MODEL_ID`` to
                      ``FAKE_ACP_FALLBACK_MODEL_ID`` — models the adapter's own
                      SDK-side refusal fallback (``syncModelAfterRefusalFallback``
                      in ``dist/acp-agent.js``), which is unprompted and carries
                      no separate "fallback happened" event of its own
    ``mode-switch``   like ``normal``, but emits a ``current_mode_update``
                      part-way through the turn moving the session's permission
                      mode to ``FAKE_ACP_SWITCH_MODE`` — the notification a real
                      adapter sends when the mode changes out from under the
                      client (Packet 5 slice A)
    ``usage-once``    report context occupancy on the session's FIRST turn and
                      never again. A real adapter emits ``usage_update`` when it
                      has something to say, not on a schedule, so a quiet turn
                      (no tool use, nothing that moves the window) carries none
                      — the shape that makes an occupancy display flicker
                      (Packet 5 slice B)

``FAKE_ACP_MODEL_ID`` / ``FAKE_ACP_FALLBACK_MODEL_ID`` (Packet 4 slice A):
override the model id ``session/new`` / ``session/load`` report (and, in
``model-fallback`` mode, the id switched to mid-turn). Every response that
carries ``configOptions`` includes a "model" entry — this is the ONLY place
ACP exposes a session's authoritative model, mirroring the pinned adapter
(0.66.0), which always sends one.

``FAKE_ACP_MODEL_OPTIONS`` / ``FAKE_ACP_NO_MODEL_OPTIONS`` (Packet 4 slice B):
the comma-separated model ids this session advertises as *selectable* (the
"model" entry's ``options``), and a switch that makes it advertise none at all
— the older/allowlist-empty adapter Hermes must offer nothing for.
``session/set_config_option`` moves the live model between them.

``FAKE_ACP_LOAD_NO_CONFIG_OPTIONS`` (Packet 4): answer ``session/load`` with no
``configOptions`` key at all, so a test can rebind against an adapter build
that reports its session config only on ``session/new``.

``FAKE_ACP_HUGE_TOOL_OUTPUT`` (Packet 5 slice D): how many filler characters the
``tools`` turn's first tool reports as its output. A persisted tool row is
bounded, so the only way to prove the bound *and* its truncation marker is a
result that genuinely exceeds it.

The record also carries ``skills_dir_listings`` (Packet 5 slice C): one entry per
``session/new`` / ``session/load``, listing this child's own
``<CLAUDE_CONFIG_DIR or $HOME/.claude>/skills`` **at that moment**. The real
adapter builds its SDK query and awaits ``initializationResult()`` inside those
two calls, so a link written after one of them is invisible for the life of that
session — recording the listing there is what makes an ordering assertion
possible at all.

``FAKE_ACP_MODES`` / ``FAKE_ACP_NO_MODES`` / ``FAKE_ACP_CURRENT_MODE`` /
``FAKE_ACP_REJECT_SET_MODE`` (Packet 5 slice A): the permission modes
``session/new`` and ``session/load`` advertise (``modes.availableModes``), the
mode they report as current before the client negotiates anything
(``modes.currentModeId``, defaulting to the real adapter's self-approving
``auto``), a switch that omits the ``modes`` key entirely — the older-adapter
case a client must not read as "inherit whatever is there" — and a switch that
makes every ``session/set_mode`` fail.

``FAKE_ACP_ECHO_SYSTEM_PROMPT`` (Packet 6): when set, every reply this adapter
produces is prefixed with ``sysprompt[<sha8>:<chars>] `` describing the
``_meta.systemPrompt.append`` it received on ``session/new`` / ``session/load``
(``sysprompt[none] `` when it received none). This is what turns "Hermes wrote
the field into a params dict" into "the agent side actually received it": an
assertion against ``session_new_params`` only proves the former, and the former
is not the claim the feature makes.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
import time

_MODE = os.getenv("FAKE_ACP_MODE", "normal")
# Packet 4 slice A: the model id every session/new, session/load and
# config_option_update reports via the "model" configOptions entry — the only
# place ACP exposes the session's authoritative model.
_MODEL_ID = os.getenv("FAKE_ACP_MODEL_ID", "claude-sonnet-5-20260815")
_FALLBACK_MODEL_ID = os.getenv(
    "FAKE_ACP_FALLBACK_MODEL_ID", "claude-haiku-4-5-20251001"
)
#: The model this adapter process is currently running. Starts at the id
#: `session/new` reports and moves when `session/set_config_option` switches it
#: (Packet 4 slice B) or the SDK's own refusal fallback fires (slice A).
_current_model_id = _MODEL_ID
#: The models this session advertises as SELECTABLE (Packet 4 slice B) — the
#: real adapter builds this from `availableModels`, i.e. the user's own Claude
#: settings allowlist, so Hermes may only offer what is listed here.
_MODEL_OPTIONS = [
    value.strip()
    for value in os.getenv(
        "FAKE_ACP_MODEL_OPTIONS",
        f"{_MODEL_ID},{_FALLBACK_MODEL_ID},claude-opus-4-1-20250805",
    ).split(",")
    if value.strip()
]
_MODEL_OPTION_NAMES = {
    "claude-sonnet-5-20260815": "Sonnet 5",
    "claude-haiku-4-5-20251001": "Haiku 4.5",
    "claude-opus-4-1-20250805": "Opus 4.1",
    # Picker *selectors* rather than model ids. The real adapter always
    # prepends a "default" row (`applyAvailableModelsAllowlist` in
    # `dist/acp-agent.js`) and `resolveModelPreference` accepts family
    # aliases, so `currentValue` is routinely one of these and NOT an
    # authoritative id — the display name is the only thing that names a
    # model there.
    "default": "Default",
    "sonnet": "Sonnet 5",
    "opus": "Opus 4.1",
}
#: Models an adapter that reports a current model but advertises no selectable
#: list at all — nothing for Hermes to offer, and nothing it may switch to.
_NO_MODEL_OPTIONS = os.getenv("FAKE_ACP_NO_MODEL_OPTIONS", "") == "1"
#: Reject every ``session/set_config_option`` even for an advertised value —
#: the real adapter does this when the SDK query stream already ended, or when
#: its own model state drifted from the list it last advertised.
_REJECT_SET_CONFIG = os.getenv("FAKE_ACP_REJECT_SET_CONFIG", "") == "1"
#: Answer ``session/load`` with no ``configOptions`` key at all — an adapter
#: build that reports its session config on ``session/new`` only. Distinct from
#: ``FAKE_ACP_NO_MODEL_OPTIONS`` (an entry that advertises nothing): one says
#: "this response carries no model state", the other "the model state is empty",
#: and a rebind must not treat either as "keep whatever the last adapter said".
_LOAD_NO_CONFIG_OPTIONS = os.getenv("FAKE_ACP_LOAD_NO_CONFIG_OPTIONS", "") == "1"
#: Packet 5 slice A: the permission modes this session advertises, in the
#: adapter's own order. The real 0.66.0 adapter answers `session/new` with
#: `modes: {currentModeId, availableModes}` alongside `configOptions`.
_MODES = [
    value.strip()
    for value in os.getenv(
        "FAKE_ACP_MODES",
        "auto,default,acceptEdits,plan,dontAsk,bypassPermissions",
    ).split(",")
    if value.strip()
]
_MODE_NAMES = {
    "auto": "Auto",
    "default": "Default",
    "acceptEdits": "Accept edits",
    "plan": "Plan",
    "dontAsk": "Don't ask",
    "bypassPermissions": "Bypass permissions",
}
#: Omit the ``modes`` key from every session response — an adapter build that
#: reports no mode state at all. Distinct from advertising an empty list, and a
#: client must not read either as "leave the mode alone".
_NO_MODES = os.getenv("FAKE_ACP_NO_MODES", "") == "1"
#: The mode this session is in before the client negotiates anything. The real
#: adapter's own default is the self-approving `auto`, which is exactly why
#: Hermes negotiates a mode at all.
_current_mode_id = os.getenv("FAKE_ACP_CURRENT_MODE", "auto")
#: Fail every ``session/set_mode`` — an adapter that refuses the negotiation.
_REJECT_SET_MODE = os.getenv("FAKE_ACP_REJECT_SET_MODE", "") == "1"
#: The mode the ``mode-switch`` turn moves to mid-turn.
_SWITCH_MODE = os.getenv("FAKE_ACP_SWITCH_MODE", "plan")
_RECORD_PATH = os.getenv("FAKE_ACP_RECORD", "")
_RECORD_DIR = os.getenv("FAKE_ACP_RECORD_DIR", "")
_STORE_DIR = os.getenv("FAKE_ACP_STORE", "")
#: Packet 6: echo a digest of the `_meta.systemPrompt.append` this session was
#: created with, so a test can assert on RECEIPT rather than on a params dict
#: Hermes itself wrote.
_ECHO_SYSTEM_PROMPT = os.getenv("FAKE_ACP_ECHO_SYSTEM_PROMPT", "") == "1"
#: acp session id -> the append it was created/reloaded with ("" for none).
_received_system_prompts: dict = {}
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
    # Packet 4 slice B evidence: every session/set_config_option this adapter
    # was asked to apply, in order — including ones it rejected.
    "set_config_option_calls": [],
    # Replies to the agent-initiated requests this adapter sent, so a test can
    # assert HOW the client refused rather than only that the turn survived.
    "client_request_replies": [],
    # Packet 5 slice C evidence. One entry per session/new and session/load,
    # dumped from inside the adapter AT THAT MOMENT. The real adapter builds its
    # SDK query and awaits initializationResult() inside those two calls, so the
    # personal-skill scan happens exactly then — a link written afterwards is
    # invisible for the life of that session. Recording the listing here is what
    # makes the E2E an assertion about ORDERING rather than about files merely
    # existing somewhere by the end of the turn.
    "skills_dir_listings": [],
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


def _system_prompt_echo(session_id: str) -> str:
    """``sysprompt[<sha8>:<chars>] `` for the append this session was created
    with, or ``""`` when the echo is off."""
    if not _ECHO_SYSTEM_PROMPT:
        return ""
    with _record_lock:
        append = _received_system_prompts.get(session_id)
    if not append:
        return "sysprompt[none] "
    digest = hashlib.sha256(append.encode("utf-8")).hexdigest()
    return f"sysprompt[{digest[:8]}:{len(append)}] "


def _capture_system_prompt(session_id: str, params: dict) -> None:
    """Remember the ``append`` this session was created (or reloaded) with."""
    meta = params.get("_meta")
    prompt = (meta or {}).get("systemPrompt") if isinstance(meta, dict) else None
    append = prompt.get("append") if isinstance(prompt, dict) else None
    with _record_lock:
        _received_system_prompts[session_id] = append if isinstance(append, str) else ""


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

# Filler for the oversized tool result (``FAKE_ACP_HUGE_TOOL_OUTPUT``). One
# repeated character so a test can assert on the truncated length and the
# marker without the payload's own structure getting in the way.
HUGE_OUTPUT_FILLER = "z"
_HUGE_TOOL_OUTPUT_CHARS = 0
try:
    _HUGE_TOOL_OUTPUT_CHARS = int(os.getenv("FAKE_ACP_HUGE_TOOL_OUTPUT", "0") or 0)
except ValueError:
    _HUGE_TOOL_OUTPUT_CHARS = 0

# The tool call ``cancel-mid-tool`` opens and never finishes.
UNFINISHED_TOOL_CALL_ID = "tc-unfinished"


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
    read_output = f"42 (token {TOOL_OUTPUT_SECRET})"
    if _HUGE_TOOL_OUTPUT_CHARS > 0:
        # A whole file's worth of output. What lands in a transcript row has to
        # be bounded; what the display bubble showed already was.
        read_output = HUGE_OUTPUT_FILLER * _HUGE_TOOL_OUTPUT_CHARS
    _update(session_id, {
        "sessionUpdate": "tool_call_update",
        "toolCallId": "tc-read",
        "status": "completed",
        "rawOutput": {"content": read_output},
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

#: The command the ``permission-sudo-stdin`` mode asks permission for: an
#: LLM-authored password guess piped into ``sudo -S``. Hermes' own shell tool
#: refuses this unconditionally, ahead of every bypass, so the delegated floor
#: has to as well.
SUDO_STDIN_PASSWORD_GUESS = "echo hunter2 | sudo -S rm -rf /var/log"

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
    if _MODE == "permission-sudo-stdin":
        tool_name = "Bash"
        call = {
            "toolCallId": "tc-bash",
            # `dist/tools.js` copies a Bash command verbatim into the title, so
            # this shape carries it in both fields, exactly as 0.66.0 does.
            "title": SUDO_STDIN_PASSWORD_GUESS,
            "kind": "execute",
            "rawInput": {"command": SUDO_STDIN_PASSWORD_GUESS},
            "locations": [],
        }
    else:
        tool_name = "Write"
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
                "toolName": tool_name,
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
                "toolName": tool_name, "parentToolUseId": "tc-task",
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


def _model_config_options(model_id: str = "") -> list:
    """The ``configOptions`` shape the pinned adapter always includes.

    Real shape (``buildConfigOptions`` in ``dist/acp-agent.js``): a "mode"
    entry plus this "model" one, whose ``currentValue`` is the authoritative
    model id (Packet 4 slice A) and whose ``options`` are the models this
    session advertises as selectable (slice B). "mode" is included for shape
    fidelity and left inert.

    ``model_id`` defaults to whatever this adapter process currently runs, so
    a switch made through ``session/set_config_option`` is reflected in every
    later response.
    """
    current = model_id or _current_model_id
    model_option: dict = {
        "id": "model", "name": "Model", "type": "select",
        "currentValue": current,
    }
    if not _NO_MODEL_OPTIONS:
        model_option["options"] = [
            {"value": value, "name": _MODEL_OPTION_NAMES.get(value, value)}
            for value in _MODEL_OPTIONS
        ]
    return [
        {"id": "mode", "name": "Mode", "type": "select", "currentValue": "default",
         "options": [{"value": "default", "name": "Default"}]},
        model_option,
    ]


def _session_modes() -> dict:
    """The ``modes`` block ``session/new`` / ``session/load`` carry.

    Real shape (``createSession`` in ``dist/acp-agent.js``):
    ``{currentModeId, availableModes: [{id, name}, …]}``. The current id moves
    when the client negotiates one with ``session/set_mode``.
    """
    return {
        "currentModeId": _current_mode_id,
        "availableModes": [
            {"id": mode, "name": _MODE_NAMES.get(mode, mode)} for mode in _MODES
        ],
    }


def _handle_set_mode(msg: dict) -> None:
    """Answer ``session/set_mode``: record it, then honour or refuse it."""
    global _current_mode_id
    mode_id = (msg.get("params") or {}).get("modeId")
    with _record_lock:
        _record["set_mode_calls"].append(mode_id)
    _flush_record()
    if _REJECT_SET_MODE:
        _send({"jsonrpc": "2.0", "id": msg["id"], "error": {
            "code": -32603, "message": f"Cannot set mode: {mode_id}",
        }})
        return
    _current_mode_id = mode_id
    _send({"jsonrpc": "2.0", "id": msg["id"], "result": {}})


def _handle_set_config_option(msg: dict) -> None:
    """Answer ``session/set_config_option`` the way adapter 0.66.0 does.

    Three outcomes, all of them load-bearing for the client: an unknown option
    id and a value the option never advertised are both errors that leave the
    session's model untouched, and a valid pick replies with the whole
    refreshed ``configOptions`` list — never a partial patch, and (unlike the
    SDK's own refusal fallback) never an accompanying ``config_option_update``
    notification.
    """
    global _current_model_id
    params = msg.get("params") or {}
    config_id = params.get("configId")
    value = params.get("value")
    with _record_lock:
        _record["set_config_option_calls"].append(
            {"configId": config_id, "value": value,
             "sessionId": params.get("sessionId")}
        )
    _flush_record()
    if config_id != "model":
        _send({"jsonrpc": "2.0", "id": msg["id"], "error": {
            "code": -32603, "message": f"Unknown config option: {config_id}",
        }})
        return
    if _REJECT_SET_CONFIG:
        _send({"jsonrpc": "2.0", "id": msg["id"], "error": {
            "code": -32603, "message": "Session has ended",
        }})
        return
    if _NO_MODEL_OPTIONS or value not in _MODEL_OPTIONS:
        _send({"jsonrpc": "2.0", "id": msg["id"], "error": {
            "code": -32603,
            "message": f"Invalid value for config option model: {value}",
        }})
        return
    _current_model_id = value
    _send({"jsonrpc": "2.0", "id": msg["id"],
           "result": {"configOptions": _model_config_options()}})


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


def _record_skills_dir(call: str) -> None:
    """Dump this child's own personal-skill directory, as it looks right now.

    Resolved the way the pinned SDK resolves it —
    ``process.env.CLAUDE_CONFIG_DIR ?? homedir()/.claude`` — so the path the
    test asserts about is the path the real adapter would read. ``usable`` is
    the subset the child can actually load through: a link that lists but whose
    ``SKILL.md`` cannot be read is a skill the model would fail on.
    """
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(
        os.environ.get("HOME", ""), ".claude"
    )
    skills_dir = os.path.join(config_dir, "skills")
    try:
        entries = sorted(
            name for name in os.listdir(skills_dir) if not name.startswith(".")
        )
    except OSError:
        entries = []
    usable = []
    for name in entries:
        try:
            with open(os.path.join(skills_dir, name, "SKILL.md"), "rb") as handle:
                handle.read(1)
            usable.append(name)
        except OSError:
            pass
    with _record_lock:
        _record["skills_dir_listings"].append({
            "call": call,
            "home": os.environ.get("HOME", ""),
            "dir": skills_dir,
            "entries": entries,
            "usable": usable,
        })
    _flush_record()


def _handle_load(msg: dict) -> None:
    """Rebind an existing session, replaying its history first."""
    _record_skills_dir("session/load")
    params = msg.get("params") or {}
    session_id = params.get("sessionId") or ""
    with _record_lock:
        _record["session_load_calls"].append(session_id)
        _record["session_load_params"].append(params)
    _flush_record()
    _capture_system_prompt(session_id, params)

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
    load_result: dict = {}
    if not _LOAD_NO_CONFIG_OPTIONS:
        load_result["configOptions"] = _model_config_options()
    if not _NO_MODES:
        load_result["modes"] = _session_modes()
    _send({"jsonrpc": "2.0", "id": msg["id"], "result": load_result})
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
        _text(
            session_id,
            _system_prompt_echo(session_id)
            + "recall[" + " | ".join(prior) + "]",
        )
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

    if _MODE in (
        "permission", "permission-no-once", "permission-subagent",
        "permission-sudo-stdin",
    ):
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

    if _MODE == "cancel-mid-tool":
        # A tool that is still running when the user stops the turn. The adapter
        # never sends a terminal `tool_call_update` for it — there was no
        # outcome — so the client is left holding an opened call that never
        # closed. That is the only shape that can leave an assistant
        # `tool_calls` row with no matching tool result behind it.
        _text(session_id, "starting the search ")
        _update(session_id, {
            "sessionUpdate": "tool_call",
            "toolCallId": UNFINISHED_TOOL_CALL_ID,
            "title": "Grep the whole tree",
            "kind": "search",
            "status": "in_progress",
            "rawInput": {"pattern": "TODO", "token": TOOL_INPUT_SECRET},
            "_meta": {"claudeCode": {"toolName": "Grep"}},
        })
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

    if _MODE == "model-fallback":
        # The SDK swapped the live model out from under the session — the
        # adapter never announces this as its own event kind, it just re-sends
        # configOptions (config_option_update) with the new value, exactly as
        # a user-driven /model switch would. Unprompted and mid-turn.
        global _current_model_id
        _text(session_id, "Let me think. ")
        _current_model_id = _FALLBACK_MODEL_ID
        _update(session_id, {
            "sessionUpdate": "config_option_update",
            "configOptions": _model_config_options(),
        })
        _text(session_id, "Falling back now.")
        _send({"jsonrpc": "2.0", "id": msg["id"], "result": {"stopReason": "end_turn"}})
        return

    if _MODE == "mode-switch":
        # The session's permission mode moved mid-turn. ACP announces this as
        # its own notification kind (unlike a model change, which rides
        # config_option_update), and it carries no display meaning at all.
        global _current_mode_id
        _text(session_id, "Switching mode. ")
        _current_mode_id = _SWITCH_MODE
        _update(session_id, {
            "sessionUpdate": "current_mode_update",
            "currentModeId": _SWITCH_MODE,
        })
        _text(session_id, "Switched.")
        _send({"jsonrpc": "2.0", "id": msg["id"], "result": {"stopReason": "end_turn"}})
        return

    if _MODE == "usage-once":
        # Occupancy is a standing fact about the session's window, and the
        # adapter states it only when it changes. The second turn here is the
        # quiet one: same session, same window, no `usage_update`.
        with _record_lock:
            first_turn = len(_record["prompts"]) == 1
        if first_turn:
            _update(session_id, {"sessionUpdate": "usage_update",
                                 "used": CONTEXT_USED_TOKENS,
                                 "size": CONTEXT_WINDOW_TOKENS,
                                 "cost": {"amount": TURN_COST_USD,
                                          "currency": "USD"}})
        _text(session_id, "Loud." if first_turn else "Quiet.")
        _send({"jsonrpc": "2.0", "id": msg["id"],
               "result": {"stopReason": "end_turn", "usage": dict(TURN_USAGE)}})
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
            _record_skills_dir("session/new")
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
            _new_id = _new_session_id()
            _capture_system_prompt(_new_id, msg.get("params") or {})
            new_result: dict = {
                "sessionId": _new_id,
                "configOptions": _model_config_options(),
            }
            if not _NO_MODES:
                new_result["modes"] = _session_modes()
            _send({"jsonrpc": "2.0", "id": msg["id"], "result": new_result})
        elif method == "session/set_config_option":
            _handle_set_config_option(msg)
        elif method == "session/set_mode":
            _handle_set_mode(msg)
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
