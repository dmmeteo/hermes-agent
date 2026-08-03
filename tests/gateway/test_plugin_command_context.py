"""Plugin slash-command handlers must run inside the turn's bound session context.

``_handle_message`` calls ``reset_session_vars()`` at handler entry (so a task
that inherited a concurrent sibling's ContextVars starts clean) and only binds
this turn's identity via ``_set_session_env`` much later, on the agent path.
The plugin slash-command branch sits in between: it dispatches
``plugin_handler(user_args)`` — a positional string, no ``event``, no
``source`` — so a handler calling ``get_session_env("HERMES_SESSION_PLATFORM")``
finds the ContextVar unset and falls through to process-global ``os.environ``
(``gateway/session_context.get_session_env`` resolution step 2).

``os.environ`` is last-writer-wins across the whole process, so on a gateway
serving several platforms concurrently a plugin command reads whichever
session's identity was mirrored most recently — not its own. These tests drive
the real ``GatewayRunner._handle_message`` seam and assert the handler observes
the identity of the event that invoked it.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionEntry, SessionSource, build_session_key


def _make_source(
    *,
    platform: Platform = Platform.TELEGRAM,
    user_id: str = "u1",
    user_name: str = "tester",
    chat_id: str = "chat-1",
    chat_type: str = "dm",
    chat_name: str = "Chat One",
    thread_id: str | None = None,
    message_id: str | None = None,
) -> SessionSource:
    return SessionSource(
        platform=platform,
        user_id=user_id,
        user_name=user_name,
        chat_id=chat_id,
        chat_type=chat_type,
        chat_name=chat_name,
        thread_id=thread_id,
        message_id=message_id,
    )


def _make_event(text: str, source: SessionSource, *, message_id: str = "m1") -> MessageEvent:
    return MessageEvent(text=text, source=source, message_id=message_id)


def _make_runner(*platforms: Platform):
    """A bare runner wired for the cold ``_handle_message`` dispatch path."""
    from gateway.run import GatewayRunner

    platforms = platforms or (Platform.TELEGRAM,)
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={p: PlatformConfig(enabled=True, token="***") for p in platforms}
    )
    runner.adapters = {p: MagicMock(send=AsyncMock()) for p in platforms}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(
        emit=AsyncMock(),
        emit_collect=AsyncMock(return_value=[]),
        loaded_hooks=False,
    )
    session_entry = SessionEntry(
        session_key="agent:main:stub",
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=platforms[0],
        chat_type="dm",
        total_tokens=0,
    )
    runner.session_store = MagicMock()
    # Real key derivation, so tests compare against the gateway's own answer
    # instead of freezing a key format.
    runner.session_store._generate_session_key = build_session_key
    runner.session_store.get_or_create_session.return_value = session_entry
    runner.session_store.load_transcript.return_value = []
    runner.session_store.has_any_sessions.return_value = True
    runner.session_store.append_to_transcript = MagicMock()
    runner.session_store.rewrite_transcript = MagicMock()
    runner.session_store.update_session = MagicMock()
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._session_run_generation = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._session_sources = {}
    runner._session_db = MagicMock()
    runner._session_db.get_session_title.return_value = None
    runner._session_db.get_session.return_value = None
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._show_reasoning = False
    runner._is_user_authorized = lambda _source: True
    runner._should_send_voice_reply = lambda *_a, **_kw: False
    runner._send_voice_reply = AsyncMock()
    runner._capture_gateway_honcho_if_configured = lambda *_a, **_kw: None
    runner._emit_gateway_run_progress = AsyncMock()
    return runner


def _install_plugin_commands(monkeypatch, handlers: dict) -> None:
    """Register *handlers* (name → callable) as the process's plugin commands."""
    from hermes_cli import plugins as plugins_mod

    entries = {
        name: {
            "handler": handler,
            "description": f"{name} command",
            "plugin": "test-plugin",
            "args_hint": "",
        }
        for name, handler in handlers.items()
    }
    monkeypatch.setattr(plugins_mod, "get_plugin_commands", lambda: entries)
    monkeypatch.setattr(
        plugins_mod,
        "get_plugin_command_handler",
        lambda name: entries[name]["handler"] if name in entries else None,
    )


_SESSION_VARS = (
    "HERMES_SESSION_PLATFORM",
    "HERMES_SESSION_CHAT_ID",
    "HERMES_SESSION_CHAT_TYPE",
    "HERMES_SESSION_CHAT_NAME",
    "HERMES_SESSION_THREAD_ID",
    "HERMES_SESSION_USER_ID",
    "HERMES_SESSION_USER_NAME",
    "HERMES_SESSION_KEY",
    "HERMES_SESSION_MESSAGE_ID",
)


def _snapshot_session_env() -> dict:
    from gateway.session_context import get_session_env

    return {name: get_session_env(name) for name in _SESSION_VARS}


def _pollute_process_env(monkeypatch, marker: str) -> None:
    """Mirror a foreign session into process-global ``os.environ``.

    This is the real leak vector: nothing in the gateway clears these, and
    ``get_session_env`` falls back to them whenever the ContextVar is unset.
    """
    for name in _SESSION_VARS:
        monkeypatch.setenv(name, f"{marker}-{name}")


# ---------------------------------------------------------------------------
# The handler must see ITS OWN turn, not the process-global mirror
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plugin_handler_sees_bound_origin_not_process_env(monkeypatch):
    seen: dict = {}

    def handler(args: str):
        seen.update(_snapshot_session_env())
        return "handled"

    _install_plugin_commands(monkeypatch, {"probe": handler})
    _pollute_process_env(monkeypatch, "FOREIGN")

    runner = _make_runner(Platform.TELEGRAM)
    source = _make_source(
        platform=Platform.TELEGRAM,
        user_id="tg-user",
        user_name="tg-name",
        chat_id="tg-chat",
        chat_type="group",
        chat_name="TG Group",
        thread_id="tg-thread",
        message_id="mid-9",
    )

    result = await runner._handle_message(_make_event("/probe", source, message_id="mid-9"))

    assert result == "handled"
    assert seen["HERMES_SESSION_PLATFORM"] == "telegram"
    assert seen["HERMES_SESSION_CHAT_ID"] == "tg-chat"
    assert seen["HERMES_SESSION_CHAT_TYPE"] == "group"
    assert seen["HERMES_SESSION_CHAT_NAME"] == "TG Group"
    assert seen["HERMES_SESSION_THREAD_ID"] == "tg-thread"
    assert seen["HERMES_SESSION_USER_ID"] == "tg-user"
    assert seen["HERMES_SESSION_USER_NAME"] == "tg-name"
    assert seen["HERMES_SESSION_MESSAGE_ID"] == "mid-9"
    assert seen["HERMES_SESSION_KEY"] == runner._session_key_for_source(source)
    # Nothing from the foreign process-global mirror survives.
    assert not [v for v in seen.values() if "FOREIGN" in v]


@pytest.mark.asyncio
async def test_concurrent_platform_sessions_do_not_leak_into_each_other(monkeypatch):
    """Two plugin commands running concurrently on different platforms each see
    their own origin — the property ``os.environ`` cannot provide."""
    seen: dict = {}
    both_inside = asyncio.Barrier(2)

    async def handler(args: str):
        # Force real overlap: neither handler may read its context until both
        # are inside. A process-global mirror is last-writer-wins here.
        await both_inside.wait()
        snapshot = _snapshot_session_env()
        seen[snapshot["HERMES_SESSION_CHAT_ID"]] = snapshot
        return "handled"

    _install_plugin_commands(monkeypatch, {"probe": handler})
    _pollute_process_env(monkeypatch, "FOREIGN")

    runner = _make_runner(Platform.TELEGRAM, Platform.DISCORD)
    tg_source = _make_source(
        platform=Platform.TELEGRAM, user_id="tg-user", chat_id="tg-chat"
    )
    dc_source = _make_source(
        platform=Platform.DISCORD, user_id="dc-user", chat_id="dc-chat"
    )

    results = await asyncio.gather(
        runner._handle_message(_make_event("/probe", tg_source)),
        runner._handle_message(_make_event("/probe", dc_source)),
    )

    assert results == ["handled", "handled"]
    assert set(seen) == {"tg-chat", "dc-chat"}
    assert seen["tg-chat"]["HERMES_SESSION_PLATFORM"] == "telegram"
    assert seen["tg-chat"]["HERMES_SESSION_USER_ID"] == "tg-user"
    assert seen["dc-chat"]["HERMES_SESSION_PLATFORM"] == "discord"
    assert seen["dc-chat"]["HERMES_SESSION_USER_ID"] == "dc-user"


@pytest.mark.asyncio
async def test_async_plugin_handler_also_sees_bound_origin(monkeypatch):
    """Async handlers are supported (awaited at the dispatch site) and must get
    the same binding as sync ones."""
    seen: dict = {}

    async def handler(args: str):
        await asyncio.sleep(0)
        seen.update(_snapshot_session_env())
        return f"async:{args}"

    _install_plugin_commands(monkeypatch, {"probe": handler})
    _pollute_process_env(monkeypatch, "FOREIGN")

    runner = _make_runner(Platform.DISCORD)
    source = _make_source(platform=Platform.DISCORD, chat_id="dc-chat", user_id="dc-user")

    result = await runner._handle_message(_make_event("/probe now please", source))

    assert result == "async:now please"
    assert seen["HERMES_SESSION_PLATFORM"] == "discord"
    assert seen["HERMES_SESSION_CHAT_ID"] == "dc-chat"


@pytest.mark.asyncio
async def test_session_context_is_released_after_the_handler(monkeypatch):
    """The binding is scoped to the handler: once dispatch returns, the vars are
    explicitly cleared, not left set for whatever runs next in this task."""
    _install_plugin_commands(monkeypatch, {"probe": lambda args: "handled"})
    _pollute_process_env(monkeypatch, "FOREIGN")

    runner = _make_runner(Platform.TELEGRAM)
    source = _make_source(platform=Platform.TELEGRAM, chat_id="tg-chat")

    assert await runner._handle_message(_make_event("/probe", source)) == "handled"

    after = _snapshot_session_env()
    assert after["HERMES_SESSION_PLATFORM"] == ""
    assert after["HERMES_SESSION_CHAT_ID"] == ""
    # Cleared means cleared — the os.environ fallback must stay suppressed.
    assert not [v for v in after.values() if "FOREIGN" in v]


@pytest.mark.asyncio
async def test_raising_handler_still_releases_the_binding(monkeypatch):
    """A handler that raises is swallowed by the dispatch site's warning path;
    it must not leave this turn's identity bound behind it."""

    def handler(args: str):
        raise RuntimeError("plugin blew up")

    _install_plugin_commands(monkeypatch, {"probe": handler})
    _pollute_process_env(monkeypatch, "FOREIGN")

    runner = _make_runner(Platform.TELEGRAM)
    runner._run_agent = AsyncMock(return_value=None)
    source = _make_source(platform=Platform.TELEGRAM, chat_id="tg-chat")

    await runner._handle_message(_make_event("/probe", source))

    after = _snapshot_session_env()
    assert after["HERMES_SESSION_PLATFORM"] == ""
    assert not [v for v in after.values() if "FOREIGN" in v]


# ---------------------------------------------------------------------------
# Existing dispatch behaviour is preserved
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("returned", [None, ""])
async def test_falsy_handler_result_still_suppresses_the_echo(monkeypatch, returned):
    _install_plugin_commands(monkeypatch, {"probe": lambda args: returned})

    runner = _make_runner(Platform.TELEGRAM)
    result = await runner._handle_message(
        _make_event("/probe", _make_source(platform=Platform.TELEGRAM))
    )

    assert result is None


@pytest.mark.asyncio
async def test_handler_receives_raw_args_and_result_is_stringified(monkeypatch):
    got: list = []
    _install_plugin_commands(
        monkeypatch, {"probe": lambda args: got.append(args) or 42}
    )

    runner = _make_runner(Platform.TELEGRAM)
    result = await runner._handle_message(
        _make_event("/probe  10m  ", _make_source(platform=Platform.TELEGRAM))
    )

    assert got == ["10m"]
    assert result == "42"


@pytest.mark.asyncio
async def test_underscored_form_still_reaches_a_hyphenated_command(monkeypatch):
    """Telegram autocomplete sends /my_cmd for a command registered as my-cmd."""
    _install_plugin_commands(monkeypatch, {"my-cmd": lambda args: "handled"})

    runner = _make_runner(Platform.TELEGRAM)
    result = await runner._handle_message(
        _make_event("/my_cmd", _make_source(platform=Platform.TELEGRAM))
    )

    assert result == "handled"


# ---------------------------------------------------------------------------
# The extracted binding helper is the one _set_session_env itself uses
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_session_env_delegates_to_the_source_helper():
    """``_set_session_env(context)`` must be exactly
    ``_set_session_env_from_source(context.source, context.session_key)`` — the
    extraction carries no behaviour change of its own."""
    from gateway.session import SessionContext

    runner = _make_runner(Platform.TELEGRAM)
    source = _make_source(
        platform=Platform.TELEGRAM,
        chat_id="tg-chat",
        chat_type="group",
        chat_name="TG Group",
        thread_id="t-9",
        user_id="u-9",
        user_name="name-9",
    )

    async def _bind_via_context():
        context = SessionContext(
            source=source,
            connected_platforms=[Platform.TELEGRAM],
            home_channels={},
            session_key="key-from-context",
        )
        runner._set_session_env(context)
        return _snapshot_session_env()

    async def _bind_via_source():
        runner._set_session_env_from_source(source, "key-from-context")
        return _snapshot_session_env()

    via_context, via_source = await asyncio.gather(_bind_via_context(), _bind_via_source())

    assert via_context == via_source
    assert via_source["HERMES_SESSION_KEY"] == "key-from-context"
    assert via_source["HERMES_SESSION_PLATFORM"] == "telegram"
