"""Packet 5 slice B — the messaging gateway's runtime footer on a Claude Code turn.

The reported symptom was a Telegram footer reading ``claude-code · 0% ·
~/projects`` for a session that was demonstrably running Opus and was a third
of the way through its context window. Both halves of that line come from the
same four lines in ``TurnRunner.run_sync``, which read the **agent object**
(``agent.model``, ``agent.context_compressor``) where the CLI status bar,
``/usage``, ``/context`` and the TUI's ``session.info`` all read the **runtime
seam**:

* ``agent.model`` is the static pseudo-model for this runtime and deliberately
  never changes (the gateway's agent cache evicts on a changed ``model``), so
  the model field was permanently ``claude-code``;
* Hermes' compressor is deliberately not driven on this runtime (Claude Code
  owns and compacts its own window), so ``last_prompt_tokens`` was 0 against a
  non-zero window — ``round(0 / N * 100)`` renders as a real-looking ``0%``.

Meanwhile the runtime already returns the right numbers
(``context_used_tokens`` / ``context_window_tokens`` from ``usage_update``) and
``run_sync`` overwrote them.

Two invariants are asserted alongside the fix, because they are what makes it
safe rather than merely correct: ``agent.model`` is never written (a mutation
evicts the cached agent and destroys prompt caching), and the display label
never becomes the value anything *keys* on — pricing, context-window lookup
and persistence get an id, which is what ``model_id`` carries.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import gateway.run as gateway_run
from agent.claude_code_runtime import ClaudeAcpModelIdentity
from gateway.runtime_footer import build_footer_line

_OPUS = "claude-opus-5"

_FOOTER_CONFIG = {
    "display": {
        "runtime_footer": {
            "enabled": True,
            "fields": ["model", "context_pct", "cwd"],
        }
    }
}


def _metadata(agent, result=None):
    """The seam under test, reached the way ``run_sync`` reaches it."""
    return gateway_run._turn_runtime_metadata(agent, result or {})


def _compressor(last_prompt_tokens=0, context_length=200_000):
    return SimpleNamespace(
        last_prompt_tokens=last_prompt_tokens,
        context_length=context_length,
    )


def _agent(**overrides):
    """A non-ACP agent, carrying only what the metadata seam reads."""
    defaults = {
        "model": "anthropic/claude-sonnet-4.6",
        "provider": "openrouter",
        "api_mode": "chat_completions",
        "session_prompt_tokens": 40_000,
        "session_completion_tokens": 10_000,
        "context_compressor": _compressor(last_prompt_tokens=30_000),
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _claude_code_agent(identity=None, **overrides):
    # The compressor this runtime never drives: a real window, no reading.
    overrides.setdefault("context_compressor", _compressor(last_prompt_tokens=0))
    agent = _agent(
        model="claude-code",
        provider="claude-code-acp",
        api_mode="claude_code_acp",
        **overrides,
    )
    agent._claude_acp_model_identity = identity
    return agent


def _identity(model_id=_OPUS, label="opus-5"):
    return ClaudeAcpModelIdentity(model_id=model_id, label=label)


# --------------------------------------------------- the reported symptom


class TestTheFooterOnALiveClaudeCodeTurn:
    def test_gateway_footer_shows_live_claude_model_and_occupancy(
        self, tmp_path, monkeypatch
    ):
        """The whole slice in one line of user-visible text.

        The asserted string is the behaviour of fixed inputs, not a snapshot of
        a catalog: 74k of a 200k window is 37% whatever models exist.
        """
        monkeypatch.setenv("HOME", str(tmp_path))
        agent = _claude_code_agent(_identity())
        result = {
            "context_used_tokens": 74_000,
            "context_window_tokens": 200_000,
            "last_prompt_tokens": 12_345,
        }

        meta = _metadata(agent, result)

        assert meta["model"] == "opus-5"
        assert meta["context_length"] == 200_000
        assert meta["last_prompt_tokens"] == 74_000

        line = build_footer_line(
            user_config=_FOOTER_CONFIG,
            platform_key="telegram",
            model=meta["model"],
            context_tokens=meta["last_prompt_tokens"],
            context_length=meta["context_length"],
            cwd=str(tmp_path / "projects"),
        )

        assert line == "opus-5 · 37% · ~/projects"


# ------------------------------------------------------- resolution order


class TestResolutionOrder:
    def test_the_adapter_beats_the_compressor_this_runtime_never_drives(self):
        agent = _claude_code_agent(
            _identity(), context_compressor=_compressor(last_prompt_tokens=0),
        )

        meta = _metadata(agent, {"context_used_tokens": 74_000,
                                 "context_window_tokens": 190_000})

        assert meta["last_prompt_tokens"] == 74_000
        assert meta["context_length"] == 190_000

    def test_the_compressor_answers_when_the_adapter_reported_no_window(self):
        """An adapter that never emitted a ``usage_update`` leaves the previous
        behaviour in place rather than blanking the field."""
        agent = _claude_code_agent(
            _identity(), context_compressor=_compressor(context_length=123_456),
        )

        meta = _metadata(agent, {})

        assert meta["context_length"] == 123_456

    def test_the_turns_own_prompt_tokens_fill_a_gap_the_compressor_left(self):
        """No occupancy reported, and a compressor with nothing to say: the
        ``session/prompt`` reply's own prompt tokens are still a truer reading
        of this window than a rendered ``0%``."""
        agent = _claude_code_agent(_identity())

        meta = _metadata(agent, {"last_prompt_tokens": 12_345})

        assert meta["last_prompt_tokens"] == 12_345

    def test_the_adapters_window_is_not_a_measurement_of_hermes_transcript(self):
        """The session entry's ``last_prompt_tokens`` is what session hygiene
        sizes THIS session's history with before deciding to auto-compress it.
        Claude Code's occupancy counts tool traffic Hermes never stored, so it
        answers a different question and is kept out of that one — it is the
        footer's number, not the compression heuristic's."""
        agent = _claude_code_agent(_identity())

        meta = _metadata(agent, {"context_used_tokens": 190_000,
                                 "context_window_tokens": 200_000})

        assert meta["last_prompt_tokens"] == 190_000
        assert meta["transcript_prompt_tokens"] == 0

    def test_the_pseudo_model_still_shows_before_any_identity_is_known(self):
        agent = _claude_code_agent(None)

        meta = _metadata(agent, {"context_used_tokens": 1, "context_window_tokens": 2})

        assert meta["model"] == "claude-code"
        assert meta["model_id"] == "claude-code"

    def test_no_agent_at_all_reports_nothing_rather_than_raising(self):
        meta = _metadata(None, {"context_used_tokens": 74_000})

        assert meta["model"] is None
        assert meta["context_length"] == 0
        assert meta["last_prompt_tokens"] == 0


# ------------------------------------------- every other runtime unchanged


class TestNonAcpIsUnchanged:
    def test_every_field_is_what_the_agent_object_says_it_is(self):
        """Field-for-field against the four lines this replaced, so the
        anti-regression claim is an equality rather than a spot check."""
        agent = _agent()

        meta = _metadata(agent, {})

        assert meta == {
            "model": agent.model,
            "model_id": agent.model,
            "context_length": agent.context_compressor.context_length,
            "last_prompt_tokens": agent.context_compressor.last_prompt_tokens,
            "transcript_prompt_tokens": agent.context_compressor.last_prompt_tokens,
            "input_tokens": agent.session_prompt_tokens,
            "output_tokens": agent.session_completion_tokens,
        }

    def test_a_driven_compressor_outranks_the_results_own_prompt_tokens(self):
        """The Codex app-server runtime drives the compressor AND returns
        ``last_prompt_tokens``. Both come from the same usage block, but the
        compressor stays the authority so nothing about that runtime moves."""
        agent = _agent(context_compressor=_compressor(last_prompt_tokens=30_000))

        meta = _metadata(agent, {"last_prompt_tokens": 999})

        assert meta["last_prompt_tokens"] == 30_000

    def test_the_post_compaction_sentinel_survives(self):
        """``last_prompt_tokens == -1`` means "unknown until the next real
        reading"; the footer skips the field on it. Replacing it with a stale
        number would render a percentage nobody measured."""
        agent = _agent(context_compressor=_compressor(last_prompt_tokens=-1))

        meta = _metadata(agent, {"last_prompt_tokens": 999})

        assert meta["last_prompt_tokens"] == -1

    def test_a_duck_typed_agent_cannot_put_a_mock_on_another_footer(self):
        """A MagicMock has every attribute, ``_claude_acp_model_identity``
        included. A seam that only asked "is it set?" would render
        ``<MagicMock …>`` as the model on every other provider's footer."""
        agent = MagicMock()
        agent.model = "gpt-5.4"
        agent.session_prompt_tokens = 1
        agent.session_completion_tokens = 2
        agent.context_compressor = _compressor(last_prompt_tokens=10)

        meta = _metadata(agent, {})

        assert meta["model"] == "gpt-5.4"
        assert meta["model_id"] == "gpt-5.4"

    def test_an_agent_without_a_compressor_reports_no_counts(self):
        agent = SimpleNamespace(model="gpt-5.4")

        meta = _metadata(agent, {})

        assert meta["model"] == "gpt-5.4"
        assert meta["context_length"] == 0
        assert meta["input_tokens"] == 0


# ------------------------------------------------ the label leaks nowhere


class TestTheLabelStaysADisplayLabel:
    def test_the_agents_model_is_never_written(self):
        """The gateway's post-turn guard evicts a cached agent whose ``model``
        no longer matches config — a display fix that mutated it would destroy
        prompt caching for the conversation."""
        agent = _claude_code_agent(_identity())

        _metadata(agent, {"context_used_tokens": 74_000})

        assert agent.model == "claude-code"

    def test_the_metadata_id_is_an_id_not_the_label(self):
        """``model_id`` is what prices a turn, keys a usage row and looks up a
        context window; ``opus-5`` resolves in none of those tables."""
        agent = _claude_code_agent(_identity())

        meta = _metadata(agent, {})

        assert meta["model"] == "opus-5"
        assert meta["model_id"] == _OPUS

    def test_a_picker_selector_is_shown_but_never_metered(self):
        """``default`` names no model, so it can be displayed and cannot be
        looked up — the metadata id falls back to the pseudo-model."""
        agent = _claude_code_agent(_identity(model_id="default", label="Default"))

        meta = _metadata(agent, {})

        assert meta["model"] == "Default"
        assert meta["model_id"] == "claude-code"


# ----------------------------------------------------------- shared seam


class TestOneSeamForEverySurface:
    def test_the_footer_and_the_slash_commands_read_the_same_function(self):
        """``/usage``, ``/context`` and ``/agents`` already resolved the label
        correctly; the footer is the surface that drifted. One function is what
        stops it drifting again."""
        from gateway.model_identity import display_model_for, metadata_model_for
        from gateway.run import GatewayRunner

        agent = _claude_code_agent(_identity())

        meta = _metadata(agent, {})

        assert meta["model"] == display_model_for(agent)
        assert meta["model_id"] == metadata_model_for(agent)
        assert GatewayRunner._display_model_for(agent) == meta["model"]
        assert GatewayRunner._metadata_model_for(agent) == meta["model_id"]
