"""Packet 4 slice B — the CLI's ``/model`` handles a live Claude Code session.

No new command: ``/model`` is still ``/model``. When the session it is typed in
is a Claude Code ACP one, the shared seam
(``hermes_cli.claude_acp_model_select``) answers first — listing the models
that session advertises and applying a pick to it live. Everything it does not
claim (another provider's model, an explicit ``--provider``) falls through to
the ordinary cross-provider switch with its behaviour unchanged, which is the
property that keeps every other provider byte-for-byte as it was.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent.claude_acp_session import ClaudeAcpModelSelectionError
from hermes_cli.model_switch import ModelSwitchResult

_SONNET = "claude-sonnet-5-20260815"
_HAIKU = "claude-haiku-4-5-20251001"


class _FakeAcpSession:
    def __init__(self, *, error: str = ""):
        self.model_options = [
            {"value": _SONNET, "name": "Sonnet 5", "description": ""},
            {"value": _HAIKU, "name": "Haiku 4.5", "description": ""},
        ]
        self.current_model_id = _SONNET
        self.turn_in_flight = False
        self.closed = False
        self._error = error
        self.set_model_calls: list[str] = []

    def is_connected(self) -> bool:
        return True

    def set_model(self, model_id: str) -> str:
        self.set_model_calls.append(model_id)
        if self._error:
            raise ClaudeAcpModelSelectionError(self._error)
        self.current_model_id = model_id
        return model_id


class _StubCLI:
    """The narrow slice of HermesCLI ``/model`` touches before the seam runs."""

    model = "claude-code"
    provider = "claude-code-acp"
    requested_provider = "claude-code-acp"
    api_key = "claude-code-acp"
    _explicit_api_key = None
    base_url = "acp://claude-code"
    _explicit_base_url = None
    api_mode = "claude_code_acp"
    agent = None
    _pending_model_switch_note = None
    _pending_one_turn_model_restore = None

    def _confirm_expensive_model_switch(self, result):
        return True


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


@pytest.fixture
def printed(monkeypatch):
    import cli as cli_mod

    lines: list[str] = []
    monkeypatch.setattr(cli_mod, "_cprint", lambda s, *a, **k: lines.append(str(s)))
    monkeypatch.setattr(
        cli_mod, "save_config_value",
        lambda *a, **k: pytest.fail("a Claude Code pick must not persist config"),
    )
    return lines


def _run(stub, command: str):
    import cli as cli_mod

    cli_mod.HermesCLI._handle_model_switch(stub, command)


class TestInterceptedByTheSharedSeam:
    def test_bare_model_lists_the_sessions_own_models(self, printed, monkeypatch):
        """The cross-provider picker cannot know what a live ACP session
        advertises, so for this provider the listing replaces it — and must not
        open it."""
        import cli as cli_mod

        monkeypatch.setattr(
            cli_mod.HermesCLI, "_open_model_picker",
            lambda *a, **k: pytest.fail("the picker must not open"),
        )
        stub = _StubCLI()
        stub.agent = _acp_agent(_FakeAcpSession())

        _run(stub, "/model")

        output = "\n".join(printed)
        assert "sonnet-5" in output
        assert "haiku-4-5" in output
        assert "--provider" in output

    def test_a_pick_switches_the_live_session_without_a_provider_switch(
        self, printed, monkeypatch
    ):
        session = _FakeAcpSession()
        stub = _StubCLI()
        stub.agent = _acp_agent(session)
        monkeypatch.setattr(
            "hermes_cli.model_switch.switch_model",
            lambda **_: pytest.fail("a Claude Code pick is not a provider switch"),
        )

        _run(stub, "/model haiku-4-5")

        assert session.set_model_calls == [_HAIKU]
        # Nothing about Hermes' own route changed: agent.model, the CLI's
        # model/provider, and the switch note all stay put (gateway agent
        # caching and the status bar key off exactly these).
        assert stub.model == "claude-code"
        assert stub.provider == "claude-code-acp"
        assert stub.agent.model == "claude-code"
        assert stub._pending_model_switch_note is None
        assert "haiku-4-5" in "\n".join(printed)

    def test_a_refused_pick_is_reported_and_changes_nothing(self, printed):
        session = _FakeAcpSession(error="Claude Code is running a turn")
        stub = _StubCLI()
        stub.agent = _acp_agent(session)

        _run(stub, "/model haiku-4-5")

        assert "Claude Code is running a turn" in "\n".join(printed)
        assert stub.model == "claude-code"

    def test_refresh_still_busts_the_provider_catalog_cache(
        self, printed, monkeypatch
    ):
        """``--refresh`` means the same thing on every surface.

        The flag clears Hermes' own provider-catalog cache — a fact about disk,
        not about this session's advertised list — and the gateway clears it
        before consulting the seam. If the CLI consulted the seam first and
        returned, ``/model --refresh`` would silently do nothing in the terminal
        while working on Discord/Telegram, which is exactly the drift the shared
        seam exists to prevent.
        """
        cleared: list[bool] = []
        monkeypatch.setattr(
            "hermes_cli.models.clear_provider_models_cache",
            lambda: cleared.append(True),
        )
        stub = _StubCLI()
        stub.agent = _acp_agent(_FakeAcpSession())

        _run(stub, "/model --refresh")

        assert cleared == [True]
        # And the seam still owns the answer: the listing is this session's.
        assert "sonnet-5" in "\n".join(printed)

    def test_a_seam_failure_falls_through_instead_of_breaking_model(
        self, printed, monkeypatch
    ):
        """/model is the only way back to a working provider. A bug in this
        seam must degrade to the pre-slice-B behaviour, never break it."""
        monkeypatch.setattr(
            "hermes_cli.claude_acp_model_select.handle_claude_acp_model_command",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        _stub_ordinary_switch_path(monkeypatch)
        stub = _StubCLI()
        stub.agent = _acp_agent(_FakeAcpSession())

        _run(stub, "/model haiku-4-5")

        assert stub.model == "gpt-5.5"


class TestFallsThroughToTheOrdinaryPath:
    def test_another_providers_model_still_switches_provider(
        self, printed, monkeypatch
    ):
        session = _FakeAcpSession()
        _stub_ordinary_switch_path(monkeypatch)
        stub = _StubCLI()
        stub.agent = _acp_agent(session)

        _run(stub, "/model gpt-5.5")

        assert session.set_model_calls == []
        assert stub.model == "gpt-5.5"
        assert stub.provider == "openrouter"

    def test_a_non_claude_code_session_is_untouched(self, printed, monkeypatch):
        _stub_ordinary_switch_path(monkeypatch)
        stub = _StubCLI()
        stub.model = "old/model"
        stub.provider = "openrouter"
        stub.api_mode = "chat_completions"
        stub.agent = SimpleNamespace(
            api_mode="chat_completions", model="old/model", provider="openrouter",
            switch_model=lambda **kw: None,
        )

        _run(stub, "/model gpt-5.5")

        assert stub.model == "gpt-5.5"


def _stub_ordinary_switch_path(monkeypatch):
    """Neutralize the cross-provider switch machinery (inventory, models.dev,
    context probing) so a fall-through can be asserted without network."""
    monkeypatch.setattr(
        "hermes_cli.inventory.load_picker_context",
        lambda: SimpleNamespace(
            user_providers=None,
            custom_providers=None,
            with_overrides=lambda **_: SimpleNamespace(
                user_providers=None, custom_providers=None,
            ),
        ),
    )
    monkeypatch.setattr(
        "hermes_cli.model_switch.switch_model",
        lambda **_: ModelSwitchResult(
            success=True,
            new_model="gpt-5.5",
            target_provider="openrouter",
            api_key="sk-test",
            base_url="https://openrouter.ai/api/v1",
            api_mode="chat_completions",
            provider_label="OpenRouter",
        ),
    )
    monkeypatch.setattr(
        "hermes_cli.model_switch.resolve_display_context_length", lambda *a, **k: None
    )
