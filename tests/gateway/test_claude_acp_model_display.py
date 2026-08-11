"""Packet 4 — the gateway's own model displays for a live Claude Code session.

Slice A replaced the static pseudo-model (``"claude-code"``) on the CLI footer,
the CLI's ``/usage`` and the desktop/TUI ``session.info``. It did not touch the
messaging gateway, which was defensible while the gateway could not change the
model. Slice B changed that: ``/model`` now works on Discord and Telegram, so
the user can be told "Claude Code model switched: sonnet-5 → haiku-4-5" and
then be told by ``/usage`` that the model is ``claude-code``.

``/context`` compounds it — it sizes the context window from the model name, so
the pseudo-model produces an occupancy figure derived from an unknown-model
default.

The other half of every test here is the regression guard: an agent that is
not a Claude Code ACP one must produce exactly what it produced before.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.claude_code_runtime import ClaudeAcpModelIdentity

_SONNET = "claude-sonnet-5-20260815"
SK = "agent:main:telegram:private:12345"


def _mock_agent(**overrides):
    """A MagicMock agent, as the rest of the gateway suites build one.

    Deliberately a MagicMock: every attribute exists on one, including
    ``_claude_acp_model_identity``. A display seam that only checks "is the
    attribute there?" would show a ``<MagicMock ...>`` on every other
    provider's ``/usage``, so this doubles as the guard for that.
    """
    agent = MagicMock()
    defaults = {
        "model": "anthropic/claude-sonnet-4.6",
        "provider": "openrouter",
        "api_mode": "chat_completions",
        "base_url": None,
        "session_id": "sess-1",
        "session_total_tokens": 50_000,
        "session_api_calls": 5,
        "session_prompt_tokens": 40_000,
        "session_completion_tokens": 10_000,
        "session_input_tokens": 35_000,
        "session_output_tokens": 10_000,
        "session_cache_read_tokens": 5_000,
        "session_cache_write_tokens": 2_000,
        "session_reasoning_tokens": 0,
    }
    defaults.update(overrides)
    for key, value in defaults.items():
        setattr(agent, key, value)
    rate_limits = MagicMock()
    rate_limits.has_data = False
    agent.get_rate_limit_state.return_value = rate_limits
    ctx = MagicMock()
    ctx.last_prompt_tokens = 30_000
    ctx.context_length = 200_000
    ctx.compression_count = 0
    ctx.threshold_tokens = 0
    ctx.threshold_percent = 0
    agent.context_compressor = ctx
    return agent


def _claude_code_agent(identity=None, **overrides):
    agent = _mock_agent(
        model="claude-code",
        provider="claude-code-acp",
        api_mode="claude_code_acp",
        **overrides,
    )
    agent._claude_acp_model_identity = identity
    return agent


def _runner(agent=None):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._agent_cache = {}
    runner._agent_cache_lock = threading.Lock()
    runner._session_db = None
    runner.session_store = MagicMock()
    # `async_session_store` is a lazily-built property keyed on `session_store`;
    # the facade only survives the identity check if it points back at it.
    facade = MagicMock()
    facade._store = runner.session_store
    runner._async_session_store = facade
    runner._session_key_for_source = MagicMock(return_value=SK)
    if agent is not None:
        runner._agent_cache[SK] = (agent, "sig")
    return runner


def _identity(model_id=_SONNET, label="sonnet-5"):
    return ClaudeAcpModelIdentity(model_id=model_id, label=label)


# ------------------------------------------------------- the seams themselves


class TestTheSeams:
    """Why every other provider's output is unchanged, stated as an invariant
    rather than inferred from three rendered strings: both seams return
    ``agent.model`` verbatim for anything that is not a Claude Code agent with
    a captured identity, so the surfaces below substitute the same value they
    always did."""

    def test_both_seams_are_the_identity_function_for_another_provider(self):
        runner = _runner()
        agent = _mock_agent()

        assert runner._display_model_for(agent) == agent.model
        assert runner._metadata_model_for(agent) == agent.model

    def test_both_seams_are_unchanged_before_a_claude_code_identity_exists(self):
        runner = _runner()
        agent = _claude_code_agent(None)

        assert runner._display_model_for(agent) == "claude-code"
        assert runner._metadata_model_for(agent) == "claude-code"

    def test_the_command_seams_are_the_shared_module_level_ones(self):
        """Packet 5 slice B moved both bodies into ``gateway/model_identity.py``
        so the runtime footer could reach them without importing a 1300-line
        turn runner. The staticmethods stay as the commands' spelling of the
        same question — delegations, never a second implementation that can
        drift from the footer."""
        from gateway.model_identity import display_model_for, metadata_model_for

        runner = _runner()
        agent = _claude_code_agent(_identity())

        assert runner._display_model_for(agent) == display_model_for(agent)
        assert runner._metadata_model_for(agent) == metadata_model_for(agent)
        assert display_model_for(agent) == "sonnet-5"
        assert metadata_model_for(agent) == _SONNET

    def test_a_selector_identity_displays_a_name_and_meters_the_pseudo_model(self):
        """The two seams answer differently, and that is the point: a picker
        selector names no model, so it can be shown but not looked up."""
        runner = _runner()
        agent = _claude_code_agent(_identity(model_id="default", label="Default"))

        assert runner._display_model_for(agent) == "Default"
        assert runner._metadata_model_for(agent) == "claude-code"


# ------------------------------------------------------------------- /usage


class TestUsage:
    @pytest.mark.asyncio
    async def test_shows_the_live_claude_code_model(self):
        runner = _runner(_claude_code_agent(_identity()))

        reply = await runner._handle_usage_command(MagicMock())

        assert "sonnet-5" in reply
        assert "claude-code" not in reply

    @pytest.mark.asyncio
    async def test_shows_the_pseudo_model_before_any_identity_is_known(self):
        runner = _runner(_claude_code_agent(None))

        reply = await runner._handle_usage_command(MagicMock())

        assert "claude-code" in reply

    @pytest.mark.asyncio
    async def test_another_providers_usage_is_unchanged(self):
        runner = _runner(_mock_agent())

        reply = await runner._handle_usage_command(MagicMock())

        assert "anthropic/claude-sonnet-4.6" in reply
        assert "MagicMock" not in reply


# ----------------------------------------------------------------- /context


class TestContext:
    async def _context(self, runner):
        event = MagicMock()
        event.get_command_args.return_value = ""
        session_entry = SimpleNamespace(session_id="sess-1", last_prompt_tokens=0)

        async def _get_or_create_session(_source):
            return session_entry

        runner._async_session_store.get_or_create_session = _get_or_create_session
        runner._context_breakdown_block = MagicMock(return_value="")
        return await runner._handle_context_command(event)

    @pytest.mark.asyncio
    async def test_shows_the_live_claude_code_model(self):
        agent = _claude_code_agent(_identity())
        reply = await self._context(_runner(agent))

        assert "sonnet-5" in reply
        assert "claude-code" not in reply

    @pytest.mark.asyncio
    async def test_sizes_the_window_from_the_live_model_not_the_pseudo_model(self):
        """The window lookup wants the model metadata knows about — the raw
        authoritative id, not the concise display label and not the
        pseudo-model."""
        agent = _claude_code_agent(_identity())
        agent.context_compressor.context_length = 0
        asked: list[str] = []

        def _context_length(model, *args, **kwargs):
            asked.append(model)
            return 900_000

        with patch(
            "agent.model_metadata.get_model_context_length", _context_length
        ):
            reply = await self._context(_runner(agent))

        assert asked == [_SONNET]
        assert "900,000" in reply

    @pytest.mark.asyncio
    async def test_another_providers_context_is_unchanged(self):
        reply = await self._context(_runner(_mock_agent()))

        assert "anthropic/claude-sonnet-4.6" in reply
        assert "MagicMock" not in reply


# ------------------------------------------------------------------ /agents


class TestAgents:
    @pytest.mark.asyncio
    async def test_a_running_claude_code_agent_lists_its_live_model(self):
        runner = _runner()
        runner._running_agents[SK] = _claude_code_agent(_identity())
        runner._running_agents_ts[SK] = 0.0
        runner._background_tasks = set()
        event = MagicMock()

        reply = await runner._handle_agents_command(event)

        assert "sonnet-5" in reply

    @pytest.mark.asyncio
    async def test_another_providers_row_is_unchanged(self):
        runner = _runner()
        runner._running_agents[SK] = _mock_agent()
        runner._running_agents_ts[SK] = 0.0
        runner._background_tasks = set()
        event = MagicMock()

        reply = await runner._handle_agents_command(event)

        assert "anthropic/claude-sonnet-4.6" in reply
        assert "MagicMock" not in reply
