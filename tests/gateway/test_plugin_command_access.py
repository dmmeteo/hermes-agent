"""Plugin slash commands must obey the same per-platform slash-access policy
as built-in and quick commands.

The cold-path gate in ``_handle_message`` resolves access on the name the user
typed. Plugin dispatch, however, normalizes underscores to hyphens before
looking the handler up — Telegram's command menu surfaces a hyphenated plugin
command as ``/my_cmd``, and that underscored form is what the adapter sends
back. So for every hyphenated plugin command the typed name (``my_cmd``) is not
a known command while the dispatched name (``my-cmd``) is, and the command runs
unchecked: with ``allow_admin_from`` configured, a non-admin can invoke it.

These tests drive the real ``GatewayRunner._handle_message`` seam and mirror the
expectations already pinned for built-ins and quick commands in
``test_slash_access_dispatch.py``: gate off unless the operator configured it,
admins unrestricted, ``user_allowed_commands`` honoured, everyone else denied
before the handler runs.
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionEntry, SessionSource, build_session_key

ADMIN = "111"
NON_ADMIN = "999"


def _make_source(
    *,
    platform: Platform = Platform.TELEGRAM,
    user_id: str = NON_ADMIN,
    chat_type: str = "dm",
    chat_id: str = "chat-1",
) -> SessionSource:
    return SessionSource(
        platform=platform,
        user_id=user_id,
        chat_id=chat_id,
        user_name=f"name-{user_id}",
        chat_type=chat_type,
    )


def _make_event(text: str, source: SessionSource) -> MessageEvent:
    return MessageEvent(text=text, source=source, message_id="m1")


def _make_runner(
    *,
    platform_extra: dict | None = None,
    platform: Platform = Platform.TELEGRAM,
):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={
            platform: PlatformConfig(
                enabled=True, token="***", extra=platform_extra or {}
            )
        }
    )
    runner.adapters = {platform: MagicMock(send=AsyncMock())}
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
        platform=platform,
        chat_type="dm",
        total_tokens=0,
    )
    runner.session_store = MagicMock()
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
    # A denied command must never reach the agent either.
    runner._run_agent = AsyncMock(return_value=None)
    return runner


def _install_plugin_command(monkeypatch, name: str) -> list:
    """Register *name* as a plugin slash command; return its invocation log."""
    from hermes_cli import plugins as plugins_mod

    invocations: list = []

    def handler(args: str):
        invocations.append(args)
        return "PLUGIN-COMMAND-RAN"

    entries = {
        name: {
            "handler": handler,
            "description": f"{name} command",
            "plugin": "test-plugin",
            "args_hint": "",
        }
    }
    monkeypatch.setattr(plugins_mod, "get_plugin_commands", lambda: entries)
    monkeypatch.setattr(
        plugins_mod,
        "get_plugin_command_handler",
        lambda n: entries[n]["handler"] if n in entries else None,
    )
    return invocations


# ---------------------------------------------------------------------------
# Denial — the gate applies, and applies before the handler runs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_admin_denied_for_hyphenated_plugin_command_typed_underscored(
    monkeypatch,
):
    """The Telegram-menu form of a hyphenated plugin command is the live
    bypass: the typed name never resolves, so nothing gated it."""
    invocations = _install_plugin_command(monkeypatch, "my-cmd")
    runner = _make_runner(
        platform_extra={"allow_admin_from": [ADMIN], "user_allowed_commands": []}
    )

    result = await runner._handle_message(
        _make_event("/my_cmd", _make_source(user_id=NON_ADMIN))
    )

    assert result is not None
    assert "⛔" in result
    assert "/my-cmd is admin-only here" in result
    assert invocations == []
    assert "PLUGIN-COMMAND-RAN" not in result
    runner._run_agent.assert_not_called()


@pytest.mark.asyncio
async def test_non_admin_denied_for_plugin_command_typed_exactly(monkeypatch):
    """The exactly-typed form must stay denied too."""
    invocations = _install_plugin_command(monkeypatch, "mycmd")
    runner = _make_runner(
        platform_extra={"allow_admin_from": [ADMIN], "user_allowed_commands": []}
    )

    result = await runner._handle_message(
        _make_event("/mycmd", _make_source(user_id=NON_ADMIN))
    )

    assert result is not None
    assert "⛔" in result
    assert invocations == []


@pytest.mark.asyncio
async def test_denial_holds_when_the_plugin_command_takes_arguments(monkeypatch):
    invocations = _install_plugin_command(monkeypatch, "my-cmd")
    runner = _make_runner(
        platform_extra={"allow_admin_from": [ADMIN], "user_allowed_commands": []}
    )

    result = await runner._handle_message(
        _make_event("/my_cmd 10m", _make_source(user_id=NON_ADMIN))
    )

    assert "⛔" in result
    assert invocations == []


# ---------------------------------------------------------------------------
# Allow — admins, listed users, and the un-configured default
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("typed", ["/my_cmd", "/my-cmd"])
async def test_admin_runs_plugin_command_when_gating_enabled(monkeypatch, typed):
    invocations = _install_plugin_command(monkeypatch, "my-cmd")
    runner = _make_runner(
        platform_extra={"allow_admin_from": [ADMIN], "user_allowed_commands": []}
    )

    result = await runner._handle_message(
        _make_event(typed, _make_source(user_id=ADMIN))
    )

    assert result == "PLUGIN-COMMAND-RAN"
    assert invocations == [""]


@pytest.mark.asyncio
@pytest.mark.parametrize("typed", ["/my_cmd", "/my-cmd"])
@pytest.mark.parametrize("listed", ["my-cmd", "my_cmd"])
async def test_non_admin_runs_plugin_command_listed_in_user_allowed(
    monkeypatch, typed, listed
):
    """Either spelling of the allowlist entry admits either typed form.

    The registration is hyphenated, but Telegram's command menu shows the
    command as ``/my_cmd`` — so that underscored form is what an operator
    copies into ``user_allowed_commands``. The policy folds ``_`` to ``-`` on
    both sides, so the four combinations of listed × typed all resolve to the
    same command.
    """
    invocations = _install_plugin_command(monkeypatch, "my-cmd")
    runner = _make_runner(
        platform_extra={
            "allow_admin_from": [ADMIN],
            "user_allowed_commands": [listed],
        }
    )

    result = await runner._handle_message(
        _make_event(typed, _make_source(user_id=NON_ADMIN))
    )

    assert result == "PLUGIN-COMMAND-RAN"
    assert invocations == [""]


@pytest.mark.asyncio
@pytest.mark.parametrize("typed", ["/my_cmd", "/my-cmd"])
async def test_underscored_allowlist_entry_does_not_admit_other_commands(
    monkeypatch, typed
):
    """Folding must not widen the allowlist beyond the command it names."""
    invocations = _install_plugin_command(monkeypatch, "other-cmd")
    runner = _make_runner(
        platform_extra={
            "allow_admin_from": [ADMIN],
            "user_allowed_commands": ["my_cmd"],
        }
    )

    result = await runner._handle_message(
        _make_event(typed.replace("my", "other"), _make_source(user_id=NON_ADMIN))
    )

    assert "⛔" in result
    assert invocations == []


@pytest.mark.asyncio
async def test_backward_compat_no_admin_list_means_no_gating(monkeypatch):
    """Unless the operator configured ``allow_admin_from`` for the scope, the
    policy is disabled and every authorized user runs every plugin command."""
    invocations = _install_plugin_command(monkeypatch, "my-cmd")
    runner = _make_runner(platform_extra={})

    result = await runner._handle_message(
        _make_event("/my_cmd", _make_source(user_id="anyone"))
    )

    assert result == "PLUGIN-COMMAND-RAN"
    assert invocations == [""]


@pytest.mark.asyncio
async def test_separator_folding_does_not_authorize_distinct_quick_command(
    monkeypatch,
):
    """Exact-key quick commands must not inherit plugin separator aliases."""
    runner = _make_runner(
        platform_extra={
            "allow_admin_from": [ADMIN],
            "user_allowed_commands": ["safe_cmd"],
        }
    )
    runner.config.quick_commands = {
        "safe-cmd": {"type": "exec", "command": "printf PWNED"},
    }

    result = await runner._handle_message(
        _make_event("/safe-cmd", _make_source(user_id=NON_ADMIN))
    )

    assert "⛔" in result
    assert "PWNED" not in result


@pytest.mark.asyncio
async def test_group_only_gating_leaves_dm_plugin_commands_unrestricted(monkeypatch):
    """Admin lists are scope-specific: a group-scoped list must not gate DMs."""
    invocations = _install_plugin_command(monkeypatch, "my-cmd")
    runner = _make_runner(platform_extra={"group_allow_admin_from": [ADMIN]})

    result = await runner._handle_message(
        _make_event("/my_cmd", _make_source(user_id="anyone", chat_type="dm"))
    )

    assert result == "PLUGIN-COMMAND-RAN"
    assert invocations == [""]


@pytest.mark.asyncio
async def test_group_scope_gating_denies_non_admin_plugin_command(monkeypatch):
    invocations = _install_plugin_command(monkeypatch, "my-cmd")
    runner = _make_runner(
        platform_extra={
            "group_allow_admin_from": [ADMIN],
            "group_user_allowed_commands": [],
        }
    )

    result = await runner._handle_message(
        _make_event(
            "/my_cmd",
            _make_source(user_id=NON_ADMIN, chat_type="group", chat_id="g-1"),
        )
    )

    assert "⛔" in result
    assert invocations == []
