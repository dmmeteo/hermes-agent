"""Packet 4 slice B — user-driven model selection for the Claude Code runtime.

Slice A made Hermes *observe* the model ACP is running. This slice lets the
user *pick* one, provider-style, without restarting anything: the live session
advertises its own selectable models (the "model" ``configOptions`` entry's
``options``, which the adapter builds from the user's own Claude
``availableModels`` allowlist), and a pick is applied with
``session/set_config_option`` on the session already in flight for this
conversation.

Two properties are the whole point and are asserted directly here rather than
left to review:

* **Nothing restarts.** Same adapter process, same ACP session id, same
  binding — so Packet 2's session/process reuse and Claude Code's own prompt
  cache survive a model switch.
* **Slice A picks it up for free.** The switch updates the same captured
  identity the footer, ``/usage`` and the SessionDB usage rows already read,
  so the display is current the moment the pick lands — no extra turn, no
  second mechanism.

Every test drives a REAL fake adapter subprocess over REAL stdio, matching the
rest of this runtime's suite: the wire shape (``configId``, the refreshed
option list in the reply, the errors) is exactly what a mock would invent
instead of exercise.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time

import pytest

import run_agent
from agent.claude_acp_session import ClaudeAcpModelSelectionError
from agent.claude_code_runtime import (
    claude_acp_display_model,
    claude_acp_model_identity,
    claude_acp_model_options,
    claude_acp_select_model,
)
from hermes_state import SessionDB

_FAKE_ADAPTER = os.path.join(os.path.dirname(__file__), "_fake_claude_acp_adapter.py")

_SONNET = "claude-sonnet-5-20260815"
_HAIKU = "claude-haiku-4-5-20251001"
_OPUS = "claude-opus-4-1-20250805"


@pytest.fixture
def acp(tmp_path, monkeypatch):
    """An agent wired to a real fake adapter, plus its recorded evidence."""
    records = tmp_path / "acp-records"
    store = tmp_path / "acp-store"
    workdir = tmp_path / "workspace"
    for path in (records, store, workdir):
        path.mkdir()
    monkeypatch.setenv("FAKE_ACP_MODE", "normal")
    monkeypatch.setenv("FAKE_ACP_RECORD_DIR", str(records))
    monkeypatch.setenv("FAKE_ACP_STORE", str(store))

    class _Env:
        def __init__(self) -> None:
            self.records = records
            self.workdir = workdir
            self.db = SessionDB(db_path=tmp_path / "sessions.db")
            self._history: dict[int, list] = {}

        def agent(self, session_id: str = "sess-acp-modelsel"):
            a = run_agent.AIAgent(
                api_key="claude-code-acp",
                base_url="acp://claude-code",
                provider="claude-code-acp",
                api_mode="claude_code_acp",
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
                session_db=self.db,
                session_id=session_id,
            )
            a.acp_command = sys.executable
            a.acp_args = [_FAKE_ADAPTER]
            a.session_cwd = str(self.workdir)
            # The post-turn background review forks its own agent (and its own
            # adapter); this suite counts adapters, so keep it out.
            a._skill_nudge_interval = 0
            return a

        def talk(self, agent, text: str):
            history = self._history.setdefault(id(agent), [])
            result = agent.run_conversation(text, conversation_history=list(history))
            self._history[id(agent)] = list(result["messages"])
            return result

        def switch_calls(self) -> list:
            """Every session/set_config_option every adapter process saw."""
            calls = []
            for path in sorted(self.records.glob("*.json")):
                try:
                    record = json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    continue
                calls.extend(record.get("set_config_option_calls") or [])
            return calls

    env = _Env()
    yield env
    try:
        from agent.claude_acp_session import close_all_sessions

        close_all_sessions()
    except Exception:
        pass


def _session(agent):
    return getattr(agent, "_claude_acp_session", None)


# --------------------------------------------------------------- what is offered


class TestAdvertisedModels:
    """Hermes offers exactly what the live ACP session advertises — no
    curated list, no catalog, no guess."""

    def test_nothing_is_offered_before_a_session_exists(self, acp):
        agent = acp.agent()
        try:
            assert claude_acp_model_options(agent) == ()
        finally:
            agent.close()

    def test_a_non_claude_code_agent_offers_nothing(self):
        agent = run_agent.AIAgent(
            api_key="k", base_url="https://example.test", provider="custom",
            model="some-model", quiet_mode=True, skip_context_files=True,
            skip_memory=True,
        )
        assert claude_acp_model_options(agent) == ()

    def test_the_offered_models_are_the_ones_the_session_advertised(self, acp):
        agent = acp.agent()
        try:
            acp.talk(agent, "hello")
            options = claude_acp_model_options(agent)
        finally:
            agent.close()
        assert [o.model_id for o in options] == [_SONNET, _HAIKU, _OPUS]

    def test_each_option_carries_a_concise_label_and_the_adapter_name(self, acp):
        agent = acp.agent()
        try:
            acp.talk(agent, "hello")
            options = claude_acp_model_options(agent)
        finally:
            agent.close()
        assert [o.label for o in options] == ["sonnet-5", "haiku-4-5", "opus-4-1"]
        assert [o.name for o in options] == ["Sonnet 5", "Haiku 4.5", "Opus 4.1"]

    def test_exactly_one_option_is_marked_current(self, acp):
        agent = acp.agent()
        try:
            acp.talk(agent, "hello")
            options = claude_acp_model_options(agent)
        finally:
            agent.close()
        current = [o for o in options if o.is_current]
        assert [o.model_id for o in current] == [_SONNET]

    def test_an_adapter_that_advertises_no_models_offers_nothing(
        self, acp, monkeypatch
    ):
        """An older adapter, or an allowlist that excludes everything, reports
        a current model but no selectable list. There is no ACP capability to
        gate on (0.66.0 advertises none), so the list itself IS the gate."""
        monkeypatch.setenv("FAKE_ACP_NO_MODEL_OPTIONS", "1")
        agent = acp.agent()
        try:
            acp.talk(agent, "hello")
            assert claude_acp_model_options(agent) == ()
            # Slice A still works: observing the identity never depended on
            # the session offering a choice.
            assert claude_acp_display_model(agent) == "sonnet-5"
        finally:
            agent.close()


# ------------------------------------------------------------------- the switch


class TestSelectingAModel:
    def test_the_pick_goes_out_as_a_model_config_option_on_the_live_session(
        self, acp
    ):
        agent = acp.agent()
        try:
            acp.talk(agent, "hello")
            acp_session_id = _session(agent).acp_session_id
            claude_acp_select_model(agent, _HAIKU)
        finally:
            agent.close()
        assert acp.switch_calls() == [
            {"configId": "model", "value": _HAIKU, "sessionId": acp_session_id},
        ]

    def test_the_captured_identity_updates_immediately(self, acp):
        """No further turn: the footer, /usage and the TUI session.info all
        read the identity slice A captured, so the pick has to land there the
        moment it is applied."""
        agent = acp.agent()
        try:
            acp.talk(agent, "hello")
            assert claude_acp_display_model(agent) == "sonnet-5"
            identity = claude_acp_select_model(agent, _HAIKU)
            assert identity.model_id == _HAIKU
            assert identity.label == "haiku-4-5"
            assert claude_acp_display_model(agent) == "haiku-4-5"
            assert claude_acp_model_identity(agent).model_id == _HAIKU
        finally:
            agent.close()

    def test_the_newly_picked_model_is_the_one_marked_current(self, acp):
        agent = acp.agent()
        try:
            acp.talk(agent, "hello")
            claude_acp_select_model(agent, _HAIKU)
            options = claude_acp_model_options(agent)
        finally:
            agent.close()
        assert [o.model_id for o in options if o.is_current] == [_HAIKU]

    def test_agent_model_is_left_untouched(self, acp):
        """agent.model stays the static pseudo-model. gateway/run.py evicts a
        cached agent whose model drifted from config on every successful turn,
        so mutating it here would respawn the adapter — exactly the reuse this
        slice must not break (see claude_code_runtime's module note)."""
        agent = acp.agent()
        try:
            acp.talk(agent, "hello")
            before = agent.model
            claude_acp_select_model(agent, _HAIKU)
            assert agent.model == before
            assert claude_acp_display_model(agent) == "haiku-4-5"
        finally:
            agent.close()

    def test_nothing_restarts_and_the_acp_session_is_the_same_one(self, acp):
        agent = acp.agent()
        try:
            acp.talk(agent, "hello")
            session = _session(agent)
            pid_before = session.adapter_pid
            acp_session_before = session.acp_session_id
            claude_acp_select_model(agent, _HAIKU)
            assert session.adapter_pid == pid_before
            assert session.acp_session_id == acp_session_before
            assert session.is_connected()
        finally:
            agent.close()

    def test_the_next_turn_continues_the_same_conversation(self, acp, monkeypatch):
        """`memory` mode answers with the prompts this ACP session already saw,
        so recalling turn 1 after the switch proves the switch did not cost the
        session (a reseed would have produced an empty recall plus a marker)."""
        monkeypatch.setenv("FAKE_ACP_MODE", "memory")
        agent = acp.agent()
        try:
            acp.talk(agent, "first question")
            claude_acp_select_model(agent, _HAIKU)
            reply = acp.talk(agent, "second question")["final_response"]
        finally:
            agent.close()
        assert "recall[first question]" in reply
        from agent.claude_code_runtime import RESEED_MARKER

        assert RESEED_MARKER not in reply

    def test_the_picked_model_is_still_current_on_the_next_turn(self, acp):
        agent = acp.agent()
        try:
            acp.talk(agent, "hello")
            claude_acp_select_model(agent, _HAIKU)
            acp.talk(agent, "and again")
            assert claude_acp_display_model(agent) == "haiku-4-5"
        finally:
            agent.close()

    def test_re_picking_the_current_model_is_still_reported_as_current(self, acp):
        agent = acp.agent()
        try:
            acp.talk(agent, "hello")
            identity = claude_acp_select_model(agent, _SONNET)
            assert identity.model_id == _SONNET
        finally:
            agent.close()


class TestUsageAccountingFollowsThePick:
    def test_a_post_switch_turn_is_priced_under_the_new_authoritative_id(
        self, acp, monkeypatch
    ):
        monkeypatch.setenv("FAKE_ACP_MODE", "tools")
        agent = acp.agent()
        seen: list[str] = []
        import agent.usage_pricing as usage_pricing

        real_estimate = usage_pricing.estimate_usage_cost

        def _spy(model_name, usage, **kw):
            seen.append(model_name)
            return real_estimate(model_name, usage, **kw)

        monkeypatch.setattr("agent.usage_pricing.estimate_usage_cost", _spy)
        try:
            acp.talk(agent, "read the notes")
            claude_acp_select_model(agent, _HAIKU)
            acp.talk(agent, "read them again")
        finally:
            agent.close()
        assert seen == [_SONNET, _HAIKU]


# ------------------------------------------------------------------- refusals


class TestRefusals:
    """Every refusal leaves the session exactly as it was."""

    def test_selecting_with_no_live_session_is_refused(self, acp):
        agent = acp.agent()
        try:
            with pytest.raises(ClaudeAcpModelSelectionError):
                claude_acp_select_model(agent, _HAIKU)
        finally:
            agent.close()

    def test_a_model_the_session_never_advertised_never_reaches_the_wire(
        self, acp
    ):
        """The advertised list is the gate on the way out too: Hermes does not
        forward a value the session did not offer."""
        agent = acp.agent()
        try:
            acp.talk(agent, "hello")
            with pytest.raises(ClaudeAcpModelSelectionError):
                claude_acp_select_model(agent, "gpt-5.5")
            assert claude_acp_display_model(agent) == "sonnet-5"
        finally:
            agent.close()
        assert acp.switch_calls() == []

    def test_an_adapter_side_rejection_keeps_the_previous_identity(
        self, acp, monkeypatch
    ):
        monkeypatch.setenv("FAKE_ACP_REJECT_SET_CONFIG", "1")
        agent = acp.agent()
        try:
            acp.talk(agent, "hello")
            with pytest.raises(ClaudeAcpModelSelectionError) as excinfo:
                claude_acp_select_model(agent, _HAIKU)
            # The adapter's own reason is preserved — it is the only thing that
            # explains WHY the pick failed.
            assert "Session has ended" in str(excinfo.value)
            assert claude_acp_display_model(agent) == "sonnet-5"
            assert claude_acp_model_identity(agent).model_id == _SONNET
        finally:
            agent.close()

    def test_a_pick_is_refused_once_the_idle_adapter_has_been_evicted(
        self, acp, monkeypatch
    ):
        """Packet 2 evicts an idle adapter between turns. Nothing can be applied
        to the session then, and nothing is sent — while the session still
        remembers its list, which is what lets the ``/model`` seam recognise the
        name instead of handing it to the cross-provider resolver."""
        import agent.claude_acp_session as session_mod

        agent = acp.agent()
        try:
            acp.talk(agent, "hello")
            monkeypatch.setattr(session_mod, "IDLE_TTL_SECONDS", 0.0)
            _session(agent)._evict_if_idle()
            assert claude_acp_model_options(agent) == ()
            assert [
                option.model_id
                for option in claude_acp_model_options(agent, require_connected=False)
            ] == [_SONNET, _HAIKU, _OPUS]
            with pytest.raises(ClaudeAcpModelSelectionError):
                claude_acp_select_model(agent, _HAIKU)
        finally:
            agent.close()
        assert acp.switch_calls() == []

    def test_a_reconnect_that_reports_no_model_state_drops_the_stale_list(
        self, acp, monkeypatch
    ):
        """A rebind can land on a different adapter build.

        ``session.matches()`` keys on argv, and an npx-pinned version change
        does not alter argv — so nothing forces a session rebuild. If the new
        build's ``session/load`` reply carries no ``configOptions`` at all,
        the list the *previous* adapter advertised is not evidence about this
        one: ``/model`` would otherwise offer models the live session never
        advertised, and the advertised list is the whole gate (there is no ACP
        capability for this).
        """
        import agent.claude_acp_session as session_mod
        from hermes_cli.claude_acp_model_select import (
            handle_claude_acp_model_command,
        )
        from hermes_cli.model_switch import parse_model_switch_args

        agent = acp.agent()
        try:
            acp.talk(agent, "hello")
            assert [o.model_id for o in claude_acp_model_options(agent)] == [
                _SONNET, _HAIKU, _OPUS,
            ]
            monkeypatch.setattr(session_mod, "IDLE_TTL_SECONDS", 0.0)
            _session(agent)._evict_if_idle()
            monkeypatch.setenv("FAKE_ACP_LOAD_NO_CONFIG_OPTIONS", "1")
            acp.talk(agent, "and again")

            assert _session(agent).is_connected()
            assert claude_acp_model_options(agent) == ()
            # ...and the seam then stays out of /model entirely rather than
            # refusing a name against a list it can no longer vouch for.
            assert handle_claude_acp_model_command(
                agent, parse_model_switch_args("haiku-4-5")
            ) is None
            with pytest.raises(ClaudeAcpModelSelectionError):
                claude_acp_select_model(agent, _HAIKU)
        finally:
            agent.close()
        assert acp.switch_calls() == []

    def test_a_reconnect_that_advertises_an_empty_list_drops_it_too(
        self, acp, monkeypatch
    ):
        """The other half of the same distinction: the reply *does* carry a
        model entry, and that entry advertises nothing (an ``availableModels``
        allowlist narrowed to nothing). Both must clear; only one of them can
        be told apart by truthiness."""
        import agent.claude_acp_session as session_mod

        agent = acp.agent()
        try:
            acp.talk(agent, "hello")
            monkeypatch.setattr(session_mod, "IDLE_TTL_SECONDS", 0.0)
            _session(agent)._evict_if_idle()
            monkeypatch.setenv("FAKE_ACP_NO_MODEL_OPTIONS", "1")
            acp.talk(agent, "and again")

            assert _session(agent).is_connected()
            assert claude_acp_model_options(agent) == ()
            with pytest.raises(ClaudeAcpModelSelectionError):
                claude_acp_select_model(agent, _HAIKU)
            # Slice A is unaffected: the entry still reports a current model.
            assert claude_acp_display_model(agent) == "sonnet-5"
        finally:
            agent.close()
        assert acp.switch_calls() == []

    def test_a_switch_is_refused_while_a_turn_is_in_flight(self, acp, monkeypatch):
        """One caller drives an ACP session at a time. A pick landing mid-turn
        would race the prompt it is meant to affect and the projector's own
        identity capture, so it is refused rather than half-applied."""
        gate = acp.workdir / "gate"
        monkeypatch.setenv("FAKE_ACP_MODE", "memory-gated")
        monkeypatch.setenv("FAKE_ACP_GATE", str(gate))
        agent = acp.agent()
        errors: list = []

        def _turn():
            try:
                agent.run_conversation("hold the line")
            except Exception as exc:  # pragma: no cover - diagnostic only
                errors.append(exc)

        worker = threading.Thread(target=_turn, daemon=True)
        worker.start()
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                session = _session(agent)
                if session is not None and session.turn_in_flight:
                    break
                time.sleep(0.05)
            assert _session(agent).turn_in_flight, "the turn never started"
            with pytest.raises(ClaudeAcpModelSelectionError):
                claude_acp_select_model(agent, _HAIKU)
        finally:
            gate.write_text("go", encoding="utf-8")
            worker.join(timeout=30)
            agent.close()
        assert errors == []
        assert acp.switch_calls() == []


# ------------------------------------------------- the RPC window is unlocked


class TestThePickCannotCommitAgainstAChangedSession:
    """``set_model`` samples its guards, then makes a blocking round trip.

    The ``_lock`` is deliberately NOT held across that RPC — ``request_cancel``
    takes the same lock, so holding it for up to 60s would swallow the user's
    ``/stop``. That makes the guards check-then-act, and the commit that
    follows has to re-establish them: everything the pick was validated
    against (this client, this ACP session, no turn) can have moved while the
    adapter was answering. Not reachable from today's surfaces — the gateway
    busy-rejects ``/model`` mid-turn and the CLI drives one turn per thread —
    but ``turn_in_flight`` exists to make that guarantee, and the next surface
    to wire ``/model`` will trust it.
    """

    def test_a_reconnect_during_the_rpc_does_not_overwrite_the_new_identity(
        self, acp
    ):
        from agent.claude_acp_session import _TurnState

        agent = acp.agent()
        try:
            acp.talk(agent, "hello")
            session = _session(agent)
            client = session._client
            real_set_config_option = client.set_config_option

            def _reconnect_after_the_reply(*args, **kwargs):
                result = real_set_config_option(*args, **kwargs)
                # The turn thread got here first: the adapter this reply came
                # from is retired and the conversation is bound to a new one.
                session._drop_client()
                session._connect(_TurnState())
                return result

            client.set_config_option = _reconnect_after_the_reply
            with pytest.raises(ClaudeAcpModelSelectionError):
                claude_acp_select_model(agent, _HAIKU)

            # The pick landed on the retired adapter, so it says nothing about
            # what the live one is running — which is still what it reported.
            assert session.current_model_id == _SONNET
            assert claude_acp_display_model(agent) == "sonnet-5"
            assert [o.model_id for o in claude_acp_model_options(agent) if o.is_current] == [
                _SONNET,
            ]
        finally:
            agent.close()

    def test_a_turn_that_starts_during_the_rpc_keeps_its_own_identity(self, acp):
        """``run_turn`` publishes its turn under the lock before connecting; a
        commit landing after that would write the pick over the identity that
        turn is capturing."""
        from agent.claude_acp_session import _TurnState

        agent = acp.agent()
        try:
            acp.talk(agent, "hello")
            session = _session(agent)
            client = session._client
            real_set_config_option = client.set_config_option

            def _start_a_turn_after_the_reply(*args, **kwargs):
                result = real_set_config_option(*args, **kwargs)
                with session._lock:
                    session._turn = _TurnState()
                return result

            client.set_config_option = _start_a_turn_after_the_reply
            with pytest.raises(ClaudeAcpModelSelectionError):
                claude_acp_select_model(agent, _HAIKU)
            assert session.current_model_id == _SONNET
            assert claude_acp_display_model(agent) == "sonnet-5"
        finally:
            with session._lock:
                session._turn = None
            agent.close()
