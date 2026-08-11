"""Packet 4 slice B — the shared ``/model`` seam for Claude Code.

``hermes_cli.claude_acp_model_select`` is the single owner of what ``/model``
*means* for a live Claude Code ACP session: which models may be offered, how a
typed name resolves against them, which flags this provider can honour, and
every line of user-visible copy. The CLI and the gateway both call it first and
render whatever it returns, so Discord, Telegram and the terminal cannot drift
apart — and neither surface grows a Claude-Code-specific command.

The one thing faked here is the ACP session itself (the process boundary).
Everything Hermes-side — ``claude_acp_model_options``,
``claude_acp_select_model``, the identity capture the footer reads — is the
real code path. The wire itself is covered end-to-end in
``tests/agent/test_claude_code_model_selection.py``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent.claude_acp_session import ClaudeAcpModelSelectionError
from agent.claude_code_runtime import claude_acp_display_model
from hermes_cli.claude_acp_model_select import handle_claude_acp_model_command
from hermes_cli.model_switch import parse_model_switch_args

_SONNET = "claude-sonnet-5-20260815"
_HAIKU = "claude-haiku-4-5-20251001"
_OPUS = "claude-opus-4-1-20250805"

_ADVERTISED = [
    {"value": _SONNET, "name": "Sonnet 5", "description": ""},
    {"value": _HAIKU, "name": "Haiku 4.5", "description": ""},
    {"value": _OPUS, "name": "Opus 4.1", "description": ""},
]


class _FakeAcpSession:
    """Stands in for the adapter process, nothing else."""

    def __init__(self, *, options=None, current=_SONNET, error: str = "",
                 connected: bool = True):
        self.model_options = list(_ADVERTISED if options is None else options)
        self.current_model_id = current
        # What a retired adapter left behind. The live list is emptied when the
        # adapter goes (idle eviction, crash, replacement) so nothing stale is
        # ever presented as live; this is the copy that survives, and it exists
        # only so a known Claude Code name can be RECOGNISED rather than handed
        # to the cross-provider resolver.
        self.remembered_model_options = list(self.model_options)
        self.remembered_model_id = current
        self.turn_in_flight = False
        self.closed = False
        self._connected = connected
        self._error = error
        self.set_model_calls: list[str] = []

    def is_connected(self) -> bool:
        return self._connected

    def set_model(self, model_id: str) -> str:
        self.set_model_calls.append(model_id)
        if self._error:
            raise ClaudeAcpModelSelectionError(self._error)
        self.current_model_id = model_id
        return model_id


def _agent(session=None, *, api_mode="claude_code_acp"):
    return SimpleNamespace(
        api_mode=api_mode,
        model="claude-code",
        provider="claude-code-acp",
        _claude_acp_session=session,
    )


def _handle(agent, raw: str):
    return handle_claude_acp_model_command(agent, parse_model_switch_args(raw))


# --------------------------------------------------------- when it stays out


class TestFallThrough:
    """Returning ``None`` means "not mine" — the ordinary cross-provider
    ``/model`` path then runs completely unchanged."""

    def test_a_non_claude_code_agent_is_not_handled(self):
        assert _handle(_agent(_FakeAcpSession(), api_mode="chat_completions"),
                       "haiku-4-5") is None

    def test_no_agent_at_all_is_not_handled(self):
        assert _handle(None, "haiku-4-5") is None

    def test_a_session_that_has_not_connected_yet_is_not_handled(self):
        """Before the first turn there is no live session, so there is no
        truthful list to offer — /model behaves exactly as it did."""
        assert _handle(_agent(None), "") is None
        assert _handle(_agent(_FakeAcpSession(connected=False)), "") is None

    def test_a_session_advertising_no_models_is_not_handled(self):
        assert _handle(_agent(_FakeAcpSession(options=[])), "") is None

    def test_bare_model_on_a_disconnected_session_is_not_handled(self):
        """A stale list must not be presented as what the session offers, so the
        ordinary picker (which can still switch provider) runs instead."""
        assert _handle(_agent(_FakeAcpSession(connected=False)), "") is None

    def test_an_explicit_provider_is_never_intercepted(self):
        """`--provider` is how the user leaves Claude Code; it must reach the
        normal switch path even when the model name looks like ours."""
        session = _FakeAcpSession()
        assert _handle(_agent(session), "--provider anthropic") is None
        assert _handle(_agent(session), "haiku-4-5 --provider anthropic") is None
        assert session.set_model_calls == []

    def test_a_name_this_session_does_not_offer_is_not_intercepted(self):
        """`/model gpt-5.5` on a Claude Code session still switches provider —
        the cross-provider resolver owns names we cannot serve."""
        session = _FakeAcpSession()
        assert _handle(_agent(session), "gpt-5.5") is None
        assert session.set_model_calls == []


# ----------------------------------------------------------------- the listing


class TestAnIdleAdapter:
    """Packet 2 evicts an idle adapter; the session object survives and still
    remembers what it advertised. A pick cannot be applied without a live
    session — but a name we KNOW is a Claude Code model must not be handed to
    the cross-provider resolver, which would silently move the conversation
    (and its billing route) to another provider."""

    def test_a_known_claude_code_model_is_refused_not_rerouted(self):
        session = _FakeAcpSession(connected=False)
        reply = _handle(_agent(session), "haiku-4-5")
        assert reply is not None
        assert reply.success is False
        assert reply.switched is False
        assert session.set_model_calls == []

    def test_the_refusal_says_how_to_get_a_live_session_back(self):
        reply = _handle(_agent(_FakeAcpSession(connected=False)), "haiku-4-5")
        assert "message" in reply.text.lower()

    def test_another_providers_model_still_falls_through(self):
        assert _handle(_agent(_FakeAcpSession(connected=False)), "gpt-5.5") is None

    def test_a_conversation_with_no_session_at_all_falls_through(self):
        """Before the first turn nothing is remembered, so there is no way to
        tell a Claude Code model from any other name — falling through is the
        only non-guessing option, and it keeps /model working as it always did.
        """
        assert _handle(_agent(None), "haiku-4-5") is None


class TestListing:
    def test_lists_every_advertised_model_with_the_current_one_marked(self):
        reply = _handle(_agent(_FakeAcpSession()), "")
        assert reply is not None
        assert reply.success is True
        assert reply.switched is False
        assert "sonnet-5" in reply.text
        assert "haiku-4-5" in reply.text
        assert "opus-4-1" in reply.text
        current_line = next(
            line for line in reply.text.splitlines() if "current" in line
        )
        assert "sonnet-5" in current_line

    def test_shows_the_adapter_supplied_display_names(self):
        reply = _handle(_agent(_FakeAcpSession()), "")
        assert "Haiku 4.5" in reply.text

    def test_points_at_the_way_out_of_claude_code(self):
        """The listing replaces the cross-provider picker for this provider, so
        it has to name the escape hatch or the user is stranded."""
        reply = _handle(_agent(_FakeAcpSession()), "")
        assert "--provider" in reply.text

    def test_reports_a_current_model_that_is_not_in_the_advertised_list(self):
        """Real case: the SDK's refusal fallback can move the session onto a
        model the user's allowlist excludes, so it is current but not
        selectable. Saying nothing would report the wrong model as current."""
        session = _FakeAcpSession(current="claude-opus-5-20260901")
        reply = _handle(_agent(session), "")
        assert "opus-5" in reply.text
        assert not any(
            "current" in line and "sonnet-5" in line
            for line in reply.text.splitlines()
        )

    def test_a_selector_row_is_listed_by_its_advertised_name(self):
        """The adapter always prepends a ``"default"`` row, and family aliases
        (``sonnet``) are advertised the same way. Those values name no model on
        their own, so the row is shown by the name the adapter gave it — and
        the token itself still selects it."""
        session = _FakeAcpSession(
            options=[
                {"value": "default", "name": "Default", "description": ""},
                {"value": _HAIKU, "name": "Haiku 4.5", "description": ""},
            ],
            current="default",
        )
        reply = _handle(_agent(session), "")
        current_line = next(
            line for line in reply.text.splitlines() if "current" in line
        )
        assert "Default" in current_line
        assert "→ default" not in reply.text

        assert _handle(_agent(session), "haiku-4-5").switched is True
        assert session.set_model_calls == [_HAIKU]

    def test_a_row_the_agent_never_named_is_listed_by_its_named_token(self):
        """The wire shape ``applyAvailableModelsAllowlist`` produces for an
        ``availableModels`` entry the SDK resolves nothing for: ``displayName``
        is the value itself, i.e. a row carrying no name. It must not appear as
        a bare token, and must not be listed as its own "— name" either — the
        value is still what selects it."""
        session = _FakeAcpSession(
            options=[
                {"value": "team-default", "name": "team-default", "description": ""},
                {"value": _HAIKU, "name": "Haiku 4.5", "description": ""},
            ],
            current="team-default",
        )
        reply = _handle(_agent(session), "")
        current_line = next(
            line for line in reply.text.splitlines() if "current" in line
        )
        assert "Team-default" in current_line
        assert "— team-default" not in reply.text
        assert "→ team-default" not in reply.text

        # And the token the agent advertised still resolves to that row.
        assert _handle(_agent(session), "team-default") is not None
        assert _handle(_agent(session), "haiku-4-5").switched is True

    def test_refresh_with_no_target_still_lists(self):
        """--refresh busts Hermes' provider-catalog cache; this list never came
        from that cache, so the flag is simply not a reason to fall through."""
        reply = _handle(_agent(_FakeAcpSession()), "--refresh")
        assert reply is not None
        assert reply.switched is False


# ------------------------------------------------------------------ resolution


class TestResolution:
    @pytest.mark.parametrize("target", [
        _HAIKU,                 # the exact authoritative id
        "haiku-4-5",            # the concise label Hermes displays
        "Haiku 4.5",            # the adapter's own display name
        "haiku 4.5",            # ... case- and spacing-insensitive
        "haiku",                # an unambiguous prefix
    ])
    def test_a_matching_name_switches_the_live_session(self, target):
        session = _FakeAcpSession()
        reply = _handle(_agent(session), target)
        assert reply is not None
        assert reply.success is True
        assert reply.switched is True
        # Always the canonical id on the wire, never the user's spelling.
        assert session.set_model_calls == [_HAIKU]

    def test_the_confirmation_names_both_the_old_and_the_new_model(self):
        reply = _handle(_agent(_FakeAcpSession()), "haiku")
        assert "sonnet-5" in reply.text
        assert "haiku-4-5" in reply.text

    def test_an_ambiguous_name_asks_instead_of_guessing(self):
        session = _FakeAcpSession()
        reply = _handle(_agent(session), "claude")
        assert reply is not None
        assert reply.success is False
        assert reply.switched is False
        assert "sonnet-5" in reply.text and "haiku-4-5" in reply.text
        assert session.set_model_calls == []

    def test_picking_the_model_already_running_is_a_no_op(self):
        session = _FakeAcpSession()
        reply = _handle(_agent(session), "sonnet-5")
        assert reply is not None
        assert reply.success is True
        assert reply.switched is False
        assert "sonnet-5" in reply.text
        assert session.set_model_calls == []


# ----------------------------------------------------------------- flag scope


class TestPersistenceScope:
    """A Claude Code pick is state inside the live ACP session. Hermes has
    nowhere to persist it that would survive as a *route*, so the flags that
    promise persistence are refused by name rather than silently ignored."""

    @pytest.mark.parametrize("raw, flag", [
        ("haiku-4-5 --global", "--global"),
        ("haiku-4-5 --once", "--once"),
    ])
    def test_persistence_flags_are_refused_without_switching(self, raw, flag):
        session = _FakeAcpSession()
        reply = _handle(_agent(session), raw)
        assert reply is not None
        assert reply.success is False
        assert reply.switched is False
        assert flag in reply.text
        assert session.set_model_calls == []

    def test_session_scope_is_exactly_what_this_path_does(self):
        session = _FakeAcpSession()
        reply = _handle(_agent(session), "haiku-4-5 --session")
        assert reply.switched is True
        assert session.set_model_calls == [_HAIKU]


# ------------------------------------------------------------------- failures


class TestFailures:
    def test_the_sessions_own_reason_is_reported_verbatim(self):
        session = _FakeAcpSession(error="Claude Code is running a turn")
        reply = _handle(_agent(session), "haiku-4-5")
        assert reply is not None
        assert reply.success is False
        assert reply.switched is False
        assert "Claude Code is running a turn" in reply.text

    def test_a_failed_switch_leaves_the_displayed_identity_alone(self):
        session = _FakeAcpSession(error="Session has ended")
        agent = _agent(session)
        agent._claude_acp_model_identity = None
        _handle(agent, "haiku-4-5")
        assert claude_acp_display_model(agent) is None


class TestSuccessUpdatesTheDisplayedIdentity:
    def test_the_footer_identity_follows_the_pick_with_no_further_turn(self):
        """Slice A's accessors are what the CLI footer, /usage and the TUI
        session.info read; a pick that did not update them would show the old
        model until the next turn."""
        agent = _agent(_FakeAcpSession())
        _handle(agent, "haiku-4-5")
        assert claude_acp_display_model(agent) == "haiku-4-5"
