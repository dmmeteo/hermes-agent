"""Packet 3 slice 3B — a Claude Code ACP approval survives both gateway guards.

While a delegated turn is running, the session is *busy*, and the gateway has
two independent guards that hold inbound messages back from it:

1. the base platform adapter's active-session guard (``handle_message``), which
   parks a follow-up in ``_pending_messages`` instead of dispatching it, and
2. the runner's busy handler (``_handle_active_session_busy_message``), which
   steers or queues it.

A permission request raised *inside* the running turn can only be answered by a
message that gets past both — otherwise the approval waits out its timeout and
fails closed while the user's "yes" sits in a queue behind the very turn it
would unblock. Both guards already carry an approval bypass; these tests prove
it works for this runtime end to end, that the answer reaches the adapter, and
that the bypass stays narrow (ordinary text is still queued).

The agent side is a REAL fake-adapter subprocess over REAL stdio, blocked on a
REAL ``session/request_permission``; the gateway side calls the REAL guard code.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
)
from gateway.session import SessionSource, build_session_key

_FAKE_ADAPTER = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "agent", "_fake_claude_acp_adapter.py"
)

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="The fake ACP adapter's teardown assertions are POSIX-only.",
)


# --------------------------------------------------------------------------- #
# gateway harness
# --------------------------------------------------------------------------- #


class _StubAdapter(BasePlatformAdapter):
    """Concrete platform adapter with the abstract transport stubbed out."""

    async def connect(self, *, is_reconnect: bool = False):
        pass

    async def disconnect(self):
        pass

    async def send(self, chat_id, text, **kwargs):
        pass

    async def get_chat_info(self, chat_id):
        return {}


def _make_source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="chat-1",
        chat_type="dm",
        user_id="u1",
        user_name="tester",
    )


def _make_event(text: str) -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=_make_source(),
        message_id="m1",
    )


def _make_runner():
    """A ``GatewayRunner`` with only the attributes the guards touch."""
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")}
    )
    runner.adapters = {}
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._busy_ack_ts = {}
    runner._background_tasks = set()
    runner._voice_mode = {}
    runner._draining = False
    runner._busy_input_mode = "queue"
    runner._session_db = None
    runner.session_store = MagicMock()
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)
    runner._is_user_authorized = lambda source: True
    runner._set_session_env = lambda _context: None
    return runner


def _command_router(runner):
    """The adapter's message handler: real registry, real approval handlers.

    Stands in for ``GatewayRunner._handle_message``'s dispatcher only — the
    command resolution and both approval handlers are the shipped ones.
    """

    async def _handler(event: MessageEvent):
        from hermes_cli.commands import resolve_command

        cmd = event.get_command()
        resolved = resolve_command(cmd) if cmd else None
        canonical = resolved.name if resolved is not None else None
        if canonical == "approve":
            return await runner._handle_approve_command(event)
        if canonical == "deny":
            return await runner._handle_deny_command(event)
        return f"unrouted:{cmd}"

    return _handler


def _make_platform_adapter(runner, handler):
    adapter = _StubAdapter(
        PlatformConfig(enabled=True, token="***"), Platform.TELEGRAM
    )
    adapter._busy_text_mode = ""
    adapter.sent = []
    adapter._message_handler = handler

    async def _send_with_retry(chat_id, content, **kwargs):
        adapter.sent.append(content)
        return SimpleNamespace(success=True, message_id="sent-1")

    adapter._send_with_retry = _send_with_retry
    adapter.resume_typing_for_chat = MagicMock()
    adapter.pause_typing_for_chat = MagicMock()
    runner.adapters[Platform.TELEGRAM] = adapter
    return adapter


def _clear_approval_state():
    from tools import approval as mod

    mod._gateway_queues.clear()
    mod._gateway_notify_cbs.clear()
    mod._session_approved.clear()
    mod._permanent_approved.clear()
    mod._pending.clear()


# --------------------------------------------------------------------------- #
# agent side
# --------------------------------------------------------------------------- #


class _BlockedTurn:
    """A Claude Code ACP turn running on its own thread, blocked on approval.

    Mirrors what ``gateway/run.py`` does around ``agent.run_conversation``:
    binds the approval session key for this thread, registers the per-session
    notify callback, and marks the context as a gateway session.
    """

    def __init__(self, tmp_path, session_key, mode="permission"):
        self.tmp_path = tmp_path
        self.session_key = session_key
        self.mode = mode
        self.record = tmp_path / "record.json"
        self.notified = threading.Event()
        self.notifications = []
        self.result = {}
        self.error = None
        self.agent = None
        self._thread = None

    def notify(self, approval_data: dict) -> None:
        self.notifications.append(approval_data)
        self.notified.set()

    def _run(self):
        import run_agent
        from tools.approval import (
            register_gateway_notify,
            reset_current_session_key,
            set_current_session_key,
            unregister_gateway_notify,
        )

        os.environ["FAKE_ACP_MODE"] = self.mode
        os.environ["FAKE_ACP_RECORD"] = str(self.record)
        token = set_current_session_key(self.session_key)
        os.environ["HERMES_GATEWAY_SESSION"] = "1"
        register_gateway_notify(self.session_key, self.notify)
        agent = None
        try:
            agent = run_agent.AIAgent(
                api_key="claude-code-acp",
                base_url="acp://claude-code",
                provider="claude-code-acp",
                api_mode="claude_code_acp",
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
            )
            agent.acp_command = sys.executable
            agent.acp_args = [_FAKE_ADAPTER]
            agent.session_cwd = str(self.tmp_path)
            # Published before the turn so a test can stop it from outside —
            # `AIAgent.interrupt()` flags this thread, which is where the
            # approval wait is parked.
            self.agent = agent
            self.result = agent.run_conversation("please write a file")
        except Exception as exc:  # surfaced by the test, never swallowed
            self.error = exc
        finally:
            unregister_gateway_notify(self.session_key)
            reset_current_session_key(token)
            if agent is not None:
                try:
                    agent.close()
                except Exception:
                    pass

    #: Ceiling on "the turn got as far as asking". Generous on purpose: it
    #: covers building a whole AIAgent (provider probing, tool registry
    #: check_fns) plus spawning the adapter, on a box running a dozen test
    #: files at once. The wait returns the instant the request lands, so a
    #: green run pays nothing for the headroom — and a tighter budget turned
    #: this into a flake that failed only for the first test in the file.
    START_TIMEOUT_SECONDS = 180.0

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        assert self.notified.wait(timeout=self.START_TIMEOUT_SECONDS), (
            "the delegated turn never surfaced its permission request to the "
            "gateway notify callback"
        )
        return self

    def join(self, timeout=120.0):
        self._thread.join(timeout=timeout)
        assert not self._thread.is_alive(), "the delegated turn never finished"
        if self.error is not None:
            raise self.error
        return self.result

    @property
    def record_data(self) -> dict:
        return json.loads(self.record.read_text(encoding="utf-8"))

    @property
    def wrote_file(self) -> bool:
        return (self.tmp_path / "permission-probe.txt").exists()


@pytest.fixture
def approval_state(monkeypatch):
    from tools import approval as approval_module

    monkeypatch.setattr(approval_module, "_YOLO_MODE_FROZEN", False)
    monkeypatch.setattr(approval_module, "_get_approval_mode", lambda: "manual")
    monkeypatch.setattr(
        approval_module, "save_permanent_allowlist", lambda *a, **k: None
    )
    _clear_approval_state()
    yield approval_module
    _clear_approval_state()
    os.environ.pop("HERMES_GATEWAY_SESSION", None)


@pytest.fixture
def blocked_turn(tmp_path, approval_state, monkeypatch):
    """A live delegated turn parked on a real ACP permission request."""
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
    turns = []

    def _start(session_key, mode="permission"):
        turn = _BlockedTurn(tmp_path, session_key, mode=mode).start()
        turns.append(turn)
        return turn

    yield _start
    for turn in turns:
        # Never leave a blocked adapter behind: resolve, then reap.
        from tools.approval import resolve_gateway_approval

        resolve_gateway_approval(turn.session_key, "deny", resolve_all=True)
        if turn._thread is not None:
            turn._thread.join(timeout=120)


# --------------------------------------------------------------------------- #
# guard 1 — the base adapter's active-session guard
# --------------------------------------------------------------------------- #


class TestAdapterPendingMessagesGuard:
    """``/approve`` must reach the runner, not ``_pending_messages``."""

    @pytest.mark.asyncio
    async def test_slash_approve_bypasses_the_guard_and_unblocks_the_write(
        self, blocked_turn
    ):
        runner = _make_runner()
        source = _make_source()
        session_key = runner._session_key_for_source(source)
        turn = blocked_turn(session_key)

        adapter = _make_platform_adapter(runner, _command_router(runner))
        adapter._active_sessions[session_key] = asyncio.Event()
        runner._running_agents[session_key] = MagicMock()

        await adapter.handle_message(_make_event("/approve"))

        assert session_key not in adapter._pending_messages, (
            "the approval was parked behind the very turn it unblocks"
        )
        result = turn.join()
        assert turn.record_data["permission_outcome"] == {
            "outcome": "selected", "optionId": "allow",
        }
        assert turn.wrote_file is True
        assert result["completed"] is True

    @pytest.mark.asyncio
    async def test_slash_deny_blocks_the_write_and_the_turn_completes(
        self, blocked_turn
    ):
        runner = _make_runner()
        source = _make_source()
        session_key = runner._session_key_for_source(source)
        turn = blocked_turn(session_key)

        adapter = _make_platform_adapter(runner, _command_router(runner))
        adapter._active_sessions[session_key] = asyncio.Event()

        await adapter.handle_message(_make_event("/deny"))

        result = turn.join()
        assert turn.record_data["permission_outcome"] == {
            "outcome": "selected", "optionId": "deny",
        }
        assert turn.wrote_file is False
        assert result["completed"] is True
        assert result["error"] is None

    @pytest.mark.asyncio
    async def test_ordinary_text_is_still_queued_while_the_turn_runs(
        self, blocked_turn
    ):
        """The bypass must not widen into "anything goes while busy"."""
        runner = _make_runner()
        source = _make_source()
        session_key = runner._session_key_for_source(source)
        blocked_turn(session_key)

        adapter = _make_platform_adapter(runner, _command_router(runner))
        adapter._active_sessions[session_key] = asyncio.Event()

        await adapter.handle_message(_make_event("what are you up to?"))

        assert session_key in adapter._pending_messages, (
            "an ordinary follow-up must stay queued as the next turn"
        )


# --------------------------------------------------------------------------- #
# guard 2 — the runner's busy handler
# --------------------------------------------------------------------------- #


class TestRunnerBusyGuard:
    """A bare-word answer must route to the approval, not to the next turn."""

    @pytest.mark.asyncio
    async def test_plain_text_yes_resolves_the_blocking_approval(self, blocked_turn):
        from gateway.run import GatewayRunner

        runner = _make_runner()
        source = _make_source()
        session_key = runner._session_key_for_source(source)
        turn = blocked_turn(session_key)
        adapter = _make_platform_adapter(runner, AsyncMock())
        runner._running_agents[session_key] = MagicMock()

        handled = await GatewayRunner._handle_active_session_busy_message(
            runner, _make_event("yes"), session_key
        )

        assert handled is True
        assert session_key not in adapter._pending_messages
        result = turn.join()
        assert turn.record_data["permission_outcome"] == {
            "outcome": "selected", "optionId": "allow",
        }
        assert turn.wrote_file is True
        assert result["completed"] is True

    @pytest.mark.asyncio
    async def test_plain_text_no_denies_the_blocking_approval(self, blocked_turn):
        from gateway.run import GatewayRunner

        runner = _make_runner()
        source = _make_source()
        session_key = runner._session_key_for_source(source)
        turn = blocked_turn(session_key)
        _make_platform_adapter(runner, AsyncMock())
        runner._running_agents[session_key] = MagicMock()

        handled = await GatewayRunner._handle_active_session_busy_message(
            runner, _make_event("no"), session_key
        )

        assert handled is True
        result = turn.join()
        assert turn.record_data["permission_outcome"] == {
            "outcome": "selected", "optionId": "deny",
        }
        assert turn.wrote_file is False
        assert result["completed"] is True

    @pytest.mark.asyncio
    async def test_a_conversational_yes_is_not_an_approval_when_none_is_pending(
        self, approval_state
    ):
        """The disambiguator is a pending approval, not the word itself."""
        from gateway.run import GatewayRunner

        runner = _make_runner()
        source = _make_source()
        session_key = runner._session_key_for_source(source)
        adapter = _make_platform_adapter(runner, AsyncMock())
        runner._running_agents[session_key] = MagicMock()
        runner._peek_session_state = lambda _key: None
        runner._busy_text_mode = "queue"
        approve = AsyncMock()
        runner._handle_approve_command = approve

        handled = await GatewayRunner._handle_active_session_busy_message(
            runner, _make_event("yes"), session_key
        )

        approve.assert_not_called()
        assert handled is False, (
            "with nothing pending, 'yes' is ordinary conversation and must "
            "fall through to normal busy handling"
        )
        assert session_key not in adapter._pending_messages


# --------------------------------------------------------------------------- #
# what the user actually sees
# --------------------------------------------------------------------------- #


class TestTheUserSeesTheRequest:
    @pytest.mark.asyncio
    async def test_the_notification_names_the_action_and_hides_credentials(
        self, blocked_turn
    ):
        from tests.agent._fake_claude_acp_adapter import (
            TOOL_INPUT_SECRET,
            TOOL_OUTPUT_SECRET,
        )

        runner = _make_runner()
        session_key = runner._session_key_for_source(_make_source())
        turn = blocked_turn(session_key)
        try:
            payload = turn.notifications[0]
            text = json.dumps(payload)
            assert "Claude Code" in text
            # Qualified, so the assertion cannot be satisfied by the tool
            # *title* alone: adapter 0.66.0 omits the tool name on a top-level
            # permission request, and the name here is the one Hermes recovered
            # from the `tool_call` update the adapter sent first.
            assert "Claude Code: Write" in text
            assert "permission-probe.txt" in text
            assert TOOL_INPUT_SECRET not in text
            assert TOOL_OUTPUT_SECRET not in text
        finally:
            from tools.approval import resolve_gateway_approval

            resolve_gateway_approval(session_key, "deny")
            turn.join()

    @pytest.mark.asyncio
    async def test_a_timed_out_gateway_approval_denies(
        self, tmp_path, approval_state, monkeypatch
    ):
        """Nobody answers → the write is refused, and the turn still lands."""
        monkeypatch.setattr(approval_state, "_get_approval_timeout", lambda: 0)
        runner = _make_runner()
        session_key = runner._session_key_for_source(_make_source())
        turn = _BlockedTurn(tmp_path, session_key).start()

        result = turn.join()
        assert turn.record_data["permission_outcome"] == {
            "outcome": "selected", "optionId": "deny",
        }
        assert turn.wrote_file is False
        assert result["completed"] is True


# --------------------------------------------------------------------------- #
# slice 3C — a stop while the approval is pending
# --------------------------------------------------------------------------- #


class TestInterruptDuringApproval:
    """3B risk 6, closed by slice 3C's cancellation work.

    On a messaging surface the approval wait is a poll loop with a 300 s
    ceiling, and the delegated turn is parked inside it. If `/stop` did not
    reach that loop, the session would stay wedged for five minutes after the
    user asked it to stop — and the request would then have to settle on
    something other than the user's intent.
    """

    @pytest.mark.asyncio
    async def test_a_stop_denies_the_pending_approval_and_unwedges_the_turn(
        self, blocked_turn
    ):
        runner = _make_runner()
        session_key = runner._session_key_for_source(_make_source())
        turn = blocked_turn(session_key)

        assert turn.agent is not None, "the blocked turn never published its agent"
        started = time.time()
        turn.agent.interrupt("stop")
        result = turn.join(timeout=120)
        elapsed = time.time() - started

        assert elapsed < 60, (
            f"the stopped approval held the session for {elapsed:.0f}s — the "
            "wait must resolve on the interrupt, not on the approval timeout"
        )
        assert turn.record_data["permission_outcome"] == {
            "outcome": "selected", "optionId": "deny",
        }, "a stopped approval settled as anything other than a denial"
        assert turn.wrote_file is False
        assert result["interrupted"] is True
        assert result["final_response"].strip(), (
            "a stopped turn must still leave an assistant row"
        )
