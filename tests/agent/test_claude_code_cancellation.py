"""Packet 3 slice 3C — a stopped Claude Code turn settles, once and cleanly.

Packets 1–2 proved a cancel reaches the adapter and the interrupt latch is
cleared. What they never pinned is how the turn *settles*: that the stop goes
out as the ACP verb, that the stopped turn is delivered exactly once (one
assistant row, persisted once, no second delivery from a second stop), that a
permission request the user was still deciding on resolves fail-closed rather
than being approved after the stop, that the lease is released, and that
nothing — adapter, session registry entry, or descendant process — is left
behind.

What each class pins:

``TestCancelReachesTheWire``    ``/stop`` becomes ``session/cancel`` and the
                               turn resolves as cancelled within bounded time
``TestSingleDelivery``         one assistant row, persisted once,
                               ``agent_persisted`` set, and a second stop
                               cannot produce a second delivery
``TestCancelDuringApproval``   a stop that lands while a human is deciding
                               denies the action and unwedges the turn
``TestNoRemainsAfterCancel``   lease released, registry empty, adapter and its
                               descendants reaped
``TestStreamOrderingAroundAPrompt``
                               text the streaming scrubber is holding back
                               reaches the user BEFORE the approval prompt

Every test but the two pure ones drives a REAL fake-adapter subprocess over
REAL stdio: the settle path is a race between three threads and a child
process, and it does not reproduce against mocks.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time

import pytest

import run_agent
from hermes_state import SessionDB

_FAKE_ADAPTER = os.path.join(os.path.dirname(__file__), "_fake_claude_acp_adapter.py")

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="Process-tree assertions use POSIX process groups; the cancellation "
           "path itself is platform-independent.",
)


def _agent_with(tmp_path, mode, monkeypatch, record=None, *, db=None, session_id=""):
    monkeypatch.setenv("FAKE_ACP_MODE", mode)
    if record is not None:
        monkeypatch.setenv("FAKE_ACP_RECORD", str(record))
    agent = run_agent.AIAgent(
        api_key="claude-code-acp",
        base_url="acp://claude-code",
        provider="claude-code-acp",
        api_mode="claude_code_acp",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
        session_db=db,
        session_id=session_id,
    )
    agent.acp_command = sys.executable
    agent.acp_args = [_FAKE_ADAPTER]
    agent.session_cwd = str(tmp_path)
    # The post-turn background review forks its own agent (and its own
    # adapter); this suite counts processes, so keep the nudge off.
    agent._skill_nudge_interval = 0
    return agent


def _record_data(record):
    try:
        return json.loads(record.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _interrupt_when(agent, record, predicate, message="stop", timeout=30.0):
    """Interrupt as soon as *predicate* holds for the adapter's record.

    A fixed sleep races turn setup: an interrupt that fires before the runtime
    has published its turn measures that race instead of the cancel path.
    """

    def _run():
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate(_record_data(record)):
                break
            time.sleep(0.02)
        agent.interrupt(message)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return thread


def _prompt_landed(data):
    return bool(data.get("prompt"))


class _Surface:
    """A recording stand-in for a human approval surface.

    Installed through ``tools.terminal_tool.set_approval_callback`` — the same
    per-thread seam the interactive CLI uses — so the real gate resolution path
    runs rather than a patched one.
    """

    def __init__(self, choice="once", on_call=None):
        self.choice = choice
        self.on_call = on_call
        self.prompts = []

    def __call__(self, command, description, **kwargs):
        if self.on_call is not None:
            self.on_call()
        self.prompts.append({"command": command, "description": description})
        return self.choice

    @property
    def text(self) -> str:
        return "\n".join(f"{p['command']}\n{p['description']}" for p in self.prompts)


@pytest.fixture
def approval_state(monkeypatch):
    """Isolate the approval module's process-global decision state."""
    from tools import approval as approval_module

    monkeypatch.setattr(approval_module, "_YOLO_MODE_FROZEN", False)
    monkeypatch.setattr(approval_module, "_get_approval_mode", lambda: "manual")
    for table in (
        approval_module._session_approved,
        approval_module._permanent_approved,
        approval_module._pending,
        approval_module._gateway_queues,
        approval_module._gateway_notify_cbs,
    ):
        table.clear()
    monkeypatch.setattr(
        approval_module, "save_permanent_allowlist", lambda *a, **k: None
    )
    yield approval_module
    for table in (
        approval_module._session_approved,
        approval_module._permanent_approved,
        approval_module._pending,
    ):
        table.clear()


@pytest.fixture
def cli_surface(monkeypatch, approval_state):
    """An interactive-CLI approval surface, installed for this thread only."""
    from tools import terminal_tool

    monkeypatch.setenv("HERMES_INTERACTIVE", "1")

    def _install(choice="once", on_call=None):
        surface = _Surface(choice, on_call)
        terminal_tool.set_approval_callback(surface)
        return surface

    yield _install
    terminal_tool.set_approval_callback(None)


@pytest.fixture
def cancelling_surface(cli_surface):
    """An approval surface where the user presses stop while deciding.

    The stop is raised from inside the callback, which is precisely where a
    real one lands: the decision runs synchronously on the turn thread while
    the adapter waits, so any stop during the prompt necessarily arrives while
    this call is outstanding.
    """

    def _install(agent, record=None, choice="once"):
        return cli_surface(
            choice, on_call=lambda: agent.interrupt("stop while deciding")
        )

    return _install


class TestCancelReachesTheWire:
    """A stop is a protocol verb, not a Hermes-side flag."""

    def test_the_stop_is_sent_as_session_cancel(self, tmp_path, monkeypatch):
        record = tmp_path / "record.json"
        agent = _agent_with(tmp_path, "slow", monkeypatch, record)
        try:
            _interrupt_when(agent, record, _prompt_landed)
            agent.run_conversation("count forever")
        finally:
            agent.close()

        data = _record_data(record)
        assert data.get("cancel_calls"), (
            "the adapter never received session/cancel — a Hermes thread flag "
            "cannot reach a model loop running inside a subprocess"
        )
        assert data["cancel_calls"][0], "session/cancel carried no sessionId"

    def test_the_turn_settles_as_cancelled_within_bounded_time(
        self, tmp_path, monkeypatch
    ):
        record = tmp_path / "record.json"
        agent = _agent_with(tmp_path, "slow", monkeypatch, record)
        started = time.time()
        try:
            _interrupt_when(agent, record, _prompt_landed)
            result = agent.run_conversation("count forever")
        finally:
            agent.close()
        elapsed = time.time() - started

        assert elapsed < 60, f"the stopped turn ran {elapsed:.0f}s"
        assert result["interrupted"] is True
        assert result["completed"] is False
        assert result["partial"] is True
        assert result["final_response"].strip()

    def test_the_interrupt_latch_is_cleared_so_the_next_turn_runs(
        self, tmp_path, monkeypatch
    ):
        record = tmp_path / "record.json"
        agent = _agent_with(tmp_path, "slow", monkeypatch, record)
        try:
            interrupter = _interrupt_when(agent, record, _prompt_landed)
            agent.run_conversation("count forever")
            interrupter.join(timeout=30)
            assert agent._interrupt_requested is False

            # From Packet 2 the SAME adapter process serves turn two, so it
            # keeps the mode it was spawned with. What matters here is that the
            # stop did not leak across the turn boundary: turn two runs, returns
            # a real response, and is not itself reported as interrupted.
            second = agent.run_conversation("second turn")
        finally:
            agent.close()

        assert second["interrupted"] is False, (
            "the previous turn's stop was still latched on the next turn"
        )
        assert second["final_response"].strip()


class TestSingleDelivery:
    """A stopped turn is delivered once — not zero times, not twice."""

    def test_exactly_one_assistant_row_is_appended(self, tmp_path, monkeypatch):
        record = tmp_path / "record.json"
        agent = _agent_with(tmp_path, "slow", monkeypatch, record)
        try:
            _interrupt_when(agent, record, _prompt_landed)
            result = agent.run_conversation("count forever")
        finally:
            agent.close()

        assistants = [m for m in result["messages"] if m.get("role") == "assistant"]
        assert len(assistants) == 1, (
            f"a cancelled turn produced {len(assistants)} assistant rows"
        )
        roles = [m["role"] for m in result["messages"] if m["role"] in ("user", "assistant")]
        assert not [a for a, b in zip(roles, roles[1:]) if a == b], (
            f"consecutive same-role messages after a cancel: {roles}"
        )

    def test_the_cancelled_turn_is_persisted_exactly_once(self, tmp_path, monkeypatch):
        record = tmp_path / "record.json"
        db = SessionDB(db_path=tmp_path / "sessions.db")
        agent = _agent_with(
            tmp_path, "slow", monkeypatch, record, db=db, session_id="cancel-once"
        )
        try:
            _interrupt_when(agent, record, _prompt_landed)
            result = agent.run_conversation("count forever")
        finally:
            agent.close()

        assert result["agent_persisted"] is True, (
            "without this the gateway writes the assistant row a second time"
        )
        stored = [
            m for m in db.get_messages("cancel-once")
            if m.get("role") in ("user", "assistant")
        ]
        assert [m["role"] for m in stored] == ["user", "assistant"], (
            f"the stopped turn was not persisted exactly once: {stored}"
        )

    def test_a_second_stop_does_not_produce_a_second_delivery(
        self, tmp_path, monkeypatch
    ):
        """Two stops in one turn are one cancellation, not two turns."""
        record = tmp_path / "record.json"
        db = SessionDB(db_path=tmp_path / "sessions.db")
        agent = _agent_with(
            tmp_path, "slow", monkeypatch, record, db=db, session_id="cancel-twice"
        )
        try:
            _interrupt_when(agent, record, _prompt_landed, message="stop")
            _interrupt_when(agent, record, _prompt_landed, message="stop again")
            result = agent.run_conversation("count forever")
        finally:
            agent.close()

        assistants = [m for m in result["messages"] if m.get("role") == "assistant"]
        assert len(assistants) == 1
        stored = [
            m for m in db.get_messages("cancel-twice")
            if m.get("role") in ("user", "assistant")
        ]
        assert [m["role"] for m in stored] == ["user", "assistant"]

    def test_a_stop_racing_a_normal_completion_still_delivers_once(
        self, tmp_path, monkeypatch
    ):
        """The adapter answers ``end_turn`` despite the stop (the real race)."""
        record = tmp_path / "record.json"
        db = SessionDB(db_path=tmp_path / "sessions.db")
        agent = _agent_with(
            tmp_path, "ignore-cancel", monkeypatch, record,
            db=db, session_id="cancel-race",
        )
        try:
            interrupter = _interrupt_when(agent, record, _prompt_landed)
            result = agent.run_conversation("first turn")
            interrupter.join(timeout=30)
        finally:
            agent.close()

        assistants = [m for m in result["messages"] if m.get("role") == "assistant"]
        assert len(assistants) == 1
        assert result["interrupted"] is True
        stored = [
            m for m in db.get_messages("cancel-race")
            if m.get("role") in ("user", "assistant")
        ]
        assert [m["role"] for m in stored] == ["user", "assistant"]


class TestCancelDuringApproval:
    """A stop that lands while a human is deciding must fail closed."""

    def test_a_stop_mid_decision_denies_the_action(
        self, tmp_path, monkeypatch, cancelling_surface
    ):
        record = tmp_path / "record.json"
        agent = _agent_with(tmp_path, "permission", monkeypatch, record)
        # The surface answers "approve" — but only after the stop has landed.
        # A stop the user has already pressed must not be turned into an
        # approval by an answer that was in flight when it arrived.
        surface = cancelling_surface(agent, record, choice="once")
        try:
            result = agent.run_conversation("please write a file")
        finally:
            agent.close()

        data = _record_data(record)
        assert surface.prompts, "the approval surface was never reached"
        assert data["permission_outcome"] == {
            "outcome": "selected", "optionId": "deny",
        }, "an approval that arrived after the stop was honoured anyway"
        assert data["wrote_file"] is False
        assert not (tmp_path / "permission-probe.txt").exists()
        assert result["interrupted"] is True

    def test_the_stopped_approval_does_not_wedge_the_turn(
        self, tmp_path, monkeypatch, cancelling_surface
    ):
        record = tmp_path / "record.json"
        agent = _agent_with(tmp_path, "permission", monkeypatch, record)
        cancelling_surface(agent, record, choice="deny")
        started = time.time()
        try:
            result = agent.run_conversation("please write a file")
        finally:
            agent.close()
        elapsed = time.time() - started

        assert elapsed < 60, f"a stopped approval wedged the turn for {elapsed:.0f}s"
        assert result["final_response"].strip()

    def test_the_lease_is_released_after_a_stopped_approval(
        self, tmp_path, monkeypatch, cancelling_surface
    ):
        from agent.claude_acp_binding import BINDING_KEY

        record = tmp_path / "record.json"
        db = SessionDB(db_path=tmp_path / "sessions.db")
        agent = _agent_with(
            tmp_path, "permission", monkeypatch, record,
            db=db, session_id="stopped-approval",
        )
        cancelling_surface(agent, record, choice="once")
        try:
            agent.run_conversation("please write a file")
        finally:
            agent.close()

        binding = db.get_session_model_config_value("stopped-approval", BINDING_KEY) or {}
        assert not binding.get("lease"), "the turn lease survived a stopped approval"

    def test_a_request_arriving_after_the_stop_is_never_shown_to_the_user(
        self, tmp_path
    ):
        """Already covered for the pre-decision case; pinned here as the pair
        to the post-decision re-check above."""
        from agent.claude_acp_session import ClaudeAcpSession, _TurnState

        session = ClaudeAcpSession(
            argv=[sys.executable, _FAKE_ADAPTER],
            cwd=str(tmp_path),
            hermes_session_id="cancel-before-asking",
        )
        try:
            turn = _TurnState()
            turn.cancel.set()
            session._turn = turn
            asked = []
            response = session._on_request(
                "session/request_permission",
                {"options": [
                    {"optionId": "allow", "kind": "allow_once"},
                    {"optionId": "deny", "kind": "reject_once"},
                ]},
                permission_handler=lambda request: asked.append(request) or True,
            )
        finally:
            session.close()

        assert asked == []
        assert response == {"outcome": {"outcome": "selected", "optionId": "deny"}}

    def test_a_stop_landing_while_the_handler_runs_denies_the_action(self, tmp_path):
        """The decisive unit case: the handler approves, the stop wins.

        The handler runs synchronously on the turn thread while the adapter
        waits, so the stop necessarily lands *during* it. Re-checking after the
        answer is what keeps `/stop` from being answered with a write.
        """
        from agent.claude_acp_session import ClaudeAcpSession, _TurnState

        session = ClaudeAcpSession(
            argv=[sys.executable, _FAKE_ADAPTER],
            cwd=str(tmp_path),
            hermes_session_id="cancel-during-decision",
        )
        try:
            turn = _TurnState()
            session._turn = turn

            def _approve_after_a_stop(request):
                turn.cancel.set()  # the user pressed stop while deciding
                return True

            response = session._on_request(
                "session/request_permission",
                {"options": [
                    {"optionId": "allow", "kind": "allow_once"},
                    {"optionId": "deny", "kind": "reject_once"},
                ]},
                permission_handler=_approve_after_a_stop,
            )
        finally:
            session.close()

        assert response == {"outcome": {"outcome": "selected", "optionId": "deny"}}, (
            "an approval that resolved after the stop was sent to the adapter"
        )


class TestNoRemainsAfterCancel:
    """A stopped turn leaves no lease, no registry entry, and no process."""

    def test_the_lease_is_released(self, tmp_path, monkeypatch):
        from agent.claude_acp_binding import BINDING_KEY

        record = tmp_path / "record.json"
        db = SessionDB(db_path=tmp_path / "sessions.db")
        agent = _agent_with(
            tmp_path, "slow", monkeypatch, record, db=db, session_id="cancel-lease"
        )
        try:
            _interrupt_when(agent, record, _prompt_landed)
            agent.run_conversation("count forever")
        finally:
            agent.close()

        binding = db.get_session_model_config_value("cancel-lease", BINDING_KEY) or {}
        assert not binding.get("lease")

    def test_no_adapter_or_descendant_survives_the_session_teardown(
        self, tmp_path, monkeypatch
    ):
        import psutil

        record = tmp_path / "record.json"
        agent = _agent_with(tmp_path, "cancel-grandchild", monkeypatch, record)
        try:
            _interrupt_when(agent, record, _prompt_landed)
            agent.run_conversation("start something long")
            data = _record_data(record)
            adapter_pid = data.get("adapter_pid")
            grandchild_pid = data.get("grandchild_pid")
            assert adapter_pid and grandchild_pid, f"incomplete record: {data}"
        finally:
            agent.close()

        for pid in (adapter_pid, grandchild_pid):
            deadline = time.time() + 5
            while time.time() < deadline and psutil.pid_exists(pid):
                time.sleep(0.05)
            assert not psutil.pid_exists(pid), (
                f"pid {pid} survived a cancelled turn's teardown — a stopped "
                f"turn must not strand the adapter or its tool children"
            )

    def test_the_session_registry_does_not_retain_the_stopped_session(
        self, tmp_path, monkeypatch
    ):
        from agent import claude_acp_session as session_module

        record = tmp_path / "record.json"
        agent = _agent_with(tmp_path, "slow", monkeypatch, record)
        try:
            _interrupt_when(agent, record, _prompt_landed)
            agent.run_conversation("count forever")
            session = agent._claude_acp_session
            assert session is not None
            key = session.key
        finally:
            agent.close()

        assert key not in session_module._SESSIONS, (
            "the interpreter-exit sweep would still be holding a closed session"
        )


class TestStreamOrderingAroundAPrompt:
    """3B risk 3 — held-back text must not surface after the prompt it precedes.

    The streaming scrubbers hold a trailing partial tag (a bare ``<`` that might
    open ``<think>``) until the next delta resolves it. An approval prompt drawn
    while that tail is held shows the user a question about text they have not
    been shown yet, and the tail then lands *after* the answer.
    """

    def test_held_back_text_reaches_the_user_before_the_prompt(
        self, tmp_path, monkeypatch, cli_surface
    ):
        events: list[str] = []
        agent = _agent_with(tmp_path, "permission-after-partial", monkeypatch)
        agent.stream_delta_callback = lambda text: events.append(f"text:{text}")

        surface = cli_surface("deny", on_call=lambda: events.append("prompt"))
        try:
            agent.run_conversation("write something")
        finally:
            agent.close()

        assert surface.prompts, "the approval surface was never reached"
        assert "prompt" in events, "the prompt was never observed"
        streamed = "".join(
            e[len("text:"):] for e in events[: events.index("prompt")]
        )
        assert streamed.endswith("<"), (
            "the scrubber's held-back tail was still unflushed when the "
            f"approval prompt was drawn; saw {events!r}"
        )
