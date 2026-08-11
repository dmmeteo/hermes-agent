"""Packet 4 slice B — Discord/Telegram ``/model`` on a live Claude Code session.

The gateway gets the same behaviour as the CLI from the same place: the shared
seam (``hermes_cli.claude_acp_model_select``) answers ``/model`` first when the
session's agent is a Claude Code ACP one. No platform-specific command, no
Claude-Code branch inside a platform adapter, and no second copy of the copy.

The gateway-specific properties asserted here are the ones the CLI cannot have:

* the seam runs on a worker thread, never on the event loop (it makes a
  blocking JSON-RPC call to the adapter);
* a pick does **not** create a session model override, evict the cached agent,
  or persist anything — the whole point is that the resident agent and its
  adapter keep running, which is what Packet 2's reuse depends on.
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

import gateway.slash_commands as slash_commands
from agent.claude_acp_session import ClaudeAcpModelSelectionError
from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource

_SONNET = "claude-sonnet-5-20260815"
_HAIKU = "claude-haiku-4-5-20251001"


class _FakeAcpSession:
    def __init__(self, *, error: str = ""):
        self.model_options = [
            {"value": _SONNET, "name": "Sonnet 5", "description": ""},
            {"value": _HAIKU, "name": "Haiku 4.5", "description": ""},
        ]
        self.current_model_id = _SONNET
        self.remembered_model_options = list(self.model_options)
        self.remembered_model_id = _SONNET
        self.turn_in_flight = False
        self.closed = False
        self._error = error
        self.set_model_calls: list[str] = []
        self.set_model_threads: list[str] = []

    def is_connected(self) -> bool:
        return True

    def set_model(self, model_id: str) -> str:
        self.set_model_calls.append(model_id)
        self.set_model_threads.append(threading.current_thread().name)
        if self._error:
            raise ClaudeAcpModelSelectionError(self._error)
        self.current_model_id = model_id
        return model_id


def _acp_agent(session):
    """A Claude Code ACP agent, including the ``switch_model`` the ordinary
    cross-provider path calls when a name falls through to it."""
    agent = SimpleNamespace(
        api_mode="claude_code_acp",
        model="claude-code",
        provider="claude-code-acp",
        _claude_acp_session=session,
    )

    def _switch_model(*, new_model, new_provider, **kwargs):
        agent.model = new_model
        agent.provider = new_provider

    agent.switch_model = _switch_model
    return agent


def _make_runner(agent=None):
    runner = object.__new__(GatewayRunner)
    runner.adapters = {}
    runner._voice_mode = {}
    runner._session_model_overrides = {}
    runner._running_agents = {}
    runner._agent_cache = {}
    runner._agent_cache_lock = threading.Lock()
    runner._evicted: list[str] = []
    runner._evict_cached_agent = lambda key: runner._evicted.append(key)
    if agent is not None:
        source = _source()
        runner._agent_cache[runner._session_key_for_source(source)] = (agent, None)
    return runner


def _source():
    return SessionSource(platform=Platform.TELEGRAM, chat_id="12345", chat_type="dm")


def _event(text: str):
    return MessageEvent(text=text, message_type=MessageType.TEXT, source=_source())


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    import gateway.run as gateway_run

    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        "model:\n  default: claude-code\n  provider: claude-code-acp\nproviders: {}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(gateway_run, "_hermes_home", hermes_home)
    monkeypatch.setattr("agent.models_dev.fetch_models_dev", lambda: {})
    monkeypatch.setattr(
        "hermes_cli.model_switch.switch_model",
        lambda **_: pytest.fail("a Claude Code pick must not run a provider switch"),
    )
    return hermes_home


@pytest.mark.asyncio
async def test_bare_model_lists_the_live_sessions_models(isolated_config, monkeypatch):
    """Replaces the button picker for this provider: the picker is built from
    the provider catalog, which cannot see what a live ACP session offers."""
    monkeypatch.setattr(
        "hermes_cli.model_switch.list_picker_providers",
        lambda **_: pytest.fail("the provider picker must not be built"),
    )
    session = _FakeAcpSession()

    reply = await _make_runner(_acp_agent(session))._handle_model_command(_event("/model"))

    assert reply is not None
    assert "sonnet-5" in reply and "haiku-4-5" in reply
    assert "current" in reply
    assert "--provider" in reply
    assert session.set_model_calls == []


@pytest.mark.asyncio
async def test_a_pick_switches_the_live_session_and_evicts_nothing(
    isolated_config,
):
    session = _FakeAcpSession()
    runner = _make_runner(_acp_agent(session))

    reply = await runner._handle_model_command(_event("/model haiku-4-5"))

    assert session.set_model_calls == [_HAIKU]
    assert "haiku-4-5" in reply
    # The resident agent and its adapter must survive untouched: no override to
    # rebuild a new agent from, no eviction, no cache change.
    assert runner._session_model_overrides == {}
    assert runner._evicted == []
    assert runner._agent_cache[runner._session_key_for_source(_source())][0].model == (
        "claude-code"
    )


@pytest.mark.asyncio
async def test_the_blocking_pick_is_offloaded_off_the_event_loop(isolated_config):
    """``set_model`` is a synchronous JSON-RPC round trip to the adapter. Run
    inline it would freeze every other chat the gateway is serving."""
    session = _FakeAcpSession()

    await _make_runner(_acp_agent(session))._handle_model_command(
        _event("/model haiku-4-5")
    )

    assert session.set_model_threads
    assert session.set_model_threads[0] != threading.main_thread().name


@pytest.mark.asyncio
async def test_a_refused_pick_is_reported_with_the_sessions_reason(isolated_config):
    session = _FakeAcpSession(error="Session has ended")
    runner = _make_runner(_acp_agent(session))

    reply = await runner._handle_model_command(_event("/model haiku-4-5"))

    assert "Session has ended" in reply
    assert runner._session_model_overrides == {}


@pytest.mark.asyncio
async def test_a_mid_turn_agent_is_the_one_the_pick_applies_to(isolated_config):
    """``_resident_agent_for_session`` looks in ``_running_agents`` first and
    ``_agent_cache`` second, mirroring ``/usage``. Production cannot actually
    reach the first branch for ``/model`` — the dispatcher busy-rejects it
    mid-turn (``gateway/run.py``'s ``_BUSY_REJECT_TEXT["model"]``) — so this
    pins the lookup's contract, not a live path."""
    session = _FakeAcpSession()
    runner = _make_runner()
    runner._running_agents[runner._session_key_for_source(_source())] = _acp_agent(
        session
    )

    await runner._handle_model_command(_event("/model haiku-4-5"))

    assert session.set_model_calls == [_HAIKU]


@pytest.mark.asyncio
async def test_refresh_busts_the_provider_catalog_cache_before_the_seam(
    isolated_config, monkeypatch
):
    """The gateway half of the ``--refresh`` alignment asserted in
    ``tests/cli/test_cli_model_command_claude_acp.py``: the flag clears Hermes'
    own on-disk provider catalog on both surfaces, and the seam still owns the
    answer. Pinned here so neither surface can drift from the other.
    """
    cleared: list[bool] = []
    monkeypatch.setattr(
        "hermes_cli.models.clear_provider_models_cache",
        lambda: cleared.append(True),
    )
    session = _FakeAcpSession()

    reply = await _make_runner(_acp_agent(session))._handle_model_command(
        _event("/model --refresh")
    )

    assert cleared == [True]
    assert "sonnet-5" in reply
    assert session.set_model_calls == []


@pytest.mark.asyncio
async def test_a_persistence_flag_is_refused_by_name(isolated_config):
    session = _FakeAcpSession()

    reply = await _make_runner(_acp_agent(session))._handle_model_command(
        _event("/model haiku-4-5 --global")
    )

    assert "--global" in reply
    assert session.set_model_calls == []


@pytest.mark.asyncio
async def test_another_providers_model_still_reaches_the_normal_switch(
    isolated_config, monkeypatch
):
    from hermes_cli.model_switch import ModelSwitchResult

    switched: list[str] = []

    def _switch(**kwargs):
        switched.append(kwargs["raw_input"])
        return ModelSwitchResult(
            success=True, new_model="gpt-5.5", target_provider="openrouter",
            api_key="sk-test", base_url="https://openrouter.ai/api/v1",
            api_mode="chat_completions", provider_label="OpenRouter",
        )

    monkeypatch.setattr("hermes_cli.model_switch.switch_model", _switch)
    session = _FakeAcpSession()
    runner = _make_runner(_acp_agent(session))
    runner._session_db = None
    runner._pending_model_notes = {}

    await runner._handle_model_command(_event("/model gpt-5.5"))

    assert switched == ["gpt-5.5"]
    assert session.set_model_calls == []
    # The ordinary path owns this name, so it does what it always does: sets a
    # session override and evicts the cached agent.
    assert runner._evicted == [runner._session_key_for_source(_source())]
