"""Packet 1 — the Claude Code ACP delegated runtime, driven through AIAgent.

Every test spawns a REAL fake adapter subprocess over REAL stdio. The failure
modes this runtime exists to prevent — history replay, leaked tool payloads,
orphaned child processes — do not reproduce against mocks.
"""

from __future__ import annotations

import json
import os

import sys
import threading
import time

import pytest

import run_agent

_FAKE_ADAPTER = os.path.join(os.path.dirname(__file__), "_fake_claude_acp_adapter.py")


def _make_agent(**kwargs):
    """An AIAgent pinned to the ACP runtime, contacting no real provider."""
    return run_agent.AIAgent(
        api_key="claude-code-acp",
        base_url="acp://claude-code",
        provider="claude-code-acp",
        api_mode="claude_code_acp",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
        **kwargs,
    )


@pytest.fixture
def agent(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_ACP_MODE", "normal")
    a = _make_agent()
    a.acp_command = sys.executable
    a.acp_args = [_FAKE_ADAPTER]
    a.session_cwd = str(tmp_path)
    return a


def _adapter_pid(record) -> int:
    """The pid THIS turn's adapter reported.

    Deliberately not a `pgrep` sweep: test files run in parallel subprocesses,
    so a global scan sees other files' adapters and the assertion goes flaky.
    """
    data = json.loads(record.read_text(encoding="utf-8"))
    pid = data.get("adapter_pid")
    assert pid, "fake adapter did not record its pid"
    return int(pid)


def _pid_alive(pid: int) -> bool:
    # psutil, not os.kill(pid, 0): on Windows that signals CTRL_C_EVENT to the
    # target's console group rather than probing liveness (bpo-14484).
    import psutil

    return psutil.pid_exists(pid)


def _assert_reaped(record) -> None:
    pid = _adapter_pid(record)
    deadline = time.time() + 5
    while time.time() < deadline and _pid_alive(pid):
        time.sleep(0.1)
    assert not _pid_alive(pid), f"adapter pid {pid} survived the turn"


class TestApiModeAndEarlyReturn:
    def test_api_mode_is_accepted_on_construction(self, agent):
        assert agent.api_mode == "claude_code_acp"

    def test_run_conversation_takes_the_early_return(self, agent, monkeypatch):
        """The chat-completions loop must never run on this path."""
        called = []
        monkeypatch.setattr(
            run_agent.AIAgent, "_get_transport",
            lambda self: called.append("transport"),
        )
        result = agent.run_conversation("what is the answer?")
        assert result["final_response"]
        assert "transport" not in called


class TestOneRealTurn:
    def test_final_response_is_assembled_from_message_chunks(self, agent):
        result = agent.run_conversation("what is the answer?")
        assert result["final_response"] == "The answer is 42."
        assert result["completed"] is True

    def test_no_raw_tool_payload_leaks_into_the_response(self, agent):
        result = agent.run_conversation("what is the answer?")
        text = result["final_response"]
        assert "tool_result" not in text
        assert "leaked-payload" not in text
        assert "rawOutput" not in text
        assert "tc-1" not in text

    def test_agent_thoughts_do_not_leak_into_the_response(self, agent):
        result = agent.run_conversation("what is the answer?")
        assert "thinking-should-not-leak" not in result["final_response"]

    def test_only_the_new_user_turn_is_sent(self, agent, tmp_path):
        """Prompt caching is sacred: Hermes must not replay transcript history
        into the adapter, which owns its own conversation state."""
        record = tmp_path / "prompt.json"
        os.environ["FAKE_ACP_RECORD"] = str(record)
        try:
            history = [
                {"role": "user", "content": "an earlier question"},
                {"role": "assistant", "content": "an earlier answer"},
            ]
            agent.run_conversation("what is the answer?", conversation_history=history)
        finally:
            os.environ.pop("FAKE_ACP_RECORD", None)

        # The fake adapter records the full session/prompt params under
        # "prompt"; the ACP content blocks are params["prompt"].
        sent = json.loads(record.read_text(encoding="utf-8"))["prompt"]
        blocks = sent["prompt"]
        assert len(blocks) == 1
        assert blocks[0]["text"] == "what is the answer?"
        serialized = json.dumps(sent)
        assert "an earlier question" not in serialized
        assert "an earlier answer" not in serialized

    def test_assistant_reply_is_appended_to_messages(self, agent):
        result = agent.run_conversation("what is the answer?")
        assert result["messages"][-1]["role"] == "assistant"
        assert result["messages"][-1]["content"] == "The answer is 42."

    def test_result_dict_matches_the_standard_shape(self, agent):
        result = agent.run_conversation("what is the answer?")
        for key in ("final_response", "messages", "api_calls", "completed", "partial"):
            assert key in result
        assert result["api_calls"] == 1


class TestDeterministicReaping:
    """The adapter outlives a turn from Packet 2 on, but never a session.

    Session reuse is the point of Packet 2, so "reaped at turn end" is no
    longer the contract; "reaped at every teardown path, with no orphan left
    anywhere" still is.
    """

    def test_no_orphan_after_the_session_is_closed(self, agent, tmp_path, monkeypatch):
        record = tmp_path / "record.json"
        monkeypatch.setenv("FAKE_ACP_RECORD", str(record))
        agent.run_conversation("what is the answer?")
        agent.close()
        _assert_reaped(record)

    def test_no_orphan_after_an_adapter_error(self, tmp_path, monkeypatch):
        record = tmp_path / "record.json"
        monkeypatch.setenv("FAKE_ACP_MODE", "error")
        monkeypatch.setenv("FAKE_ACP_RECORD", str(record))
        a = _make_agent()
        a.acp_command = sys.executable
        a.acp_args = [_FAKE_ADAPTER]
        a.session_cwd = str(tmp_path)

        result = a.run_conversation("what is the answer?")

        assert result["completed"] is False
        assert result["error"]
        _assert_reaped(record)

    def test_no_orphan_after_an_interrupt(self, tmp_path, monkeypatch):
        record = tmp_path / "record.json"
        monkeypatch.setenv("FAKE_ACP_MODE", "slow")
        monkeypatch.setenv("FAKE_ACP_RECORD", str(record))
        a = _make_agent()
        a.acp_command = sys.executable
        a.acp_args = [_FAKE_ADAPTER]
        a.session_cwd = str(tmp_path)

        def interrupt_soon():
            time.sleep(1.0)
            a.interrupt("stop please")

        threading.Thread(target=interrupt_soon, daemon=True).start()
        result = a.run_conversation("count forever")

        assert result["interrupted"] is True
        # A cancelled turn leaves a healthy adapter, so it is kept for the next
        # turn; closing the session is what must reap it.
        a.close()
        _assert_reaped(record)


class TestSteerIsNotLost:
    """``steer()`` reports "accepted", so the text must actually be delivered.

    The delegated early return bypasses every drain the chat-completions loop
    owns (the pre-API drain, the tool-result applier, the turn finalizer), so a
    steer parked in ``_pending_steer`` would be silently dropped even though the
    CLI, the gateway and the TUI all told the user it landed. The finalizer's own
    answer for "a steer with no tool batch left to drain into" is
    ``result["pending_steer"]``, which those three surfaces already deliver as
    the next user turn — this runtime owes the same key.
    """

    def test_a_steer_during_the_turn_comes_back_as_the_next_turn(self, agent):
        agent.steer("actually, focus on the parser")
        try:
            result = agent.run_conversation("hello")
        finally:
            agent.close()

        assert result["completed"] is True
        assert result.get("pending_steer") == "actually, focus on the parser", (
            "the steer was accepted and then dropped: no surface can deliver it"
        )
        assert agent._pending_steer is None, "the steer must be drained once"

    def test_a_turn_with_no_steer_does_not_invent_the_key(self, agent):
        try:
            result = agent.run_conversation("hello")
        finally:
            agent.close()

        assert "pending_steer" not in result

    def test_a_steer_survives_a_failed_turn(self, tmp_path):
        """A setup failure must not eat the user's guidance either."""
        a = _make_agent()
        a.acp_command = "definitely-not-a-real-adapter-binary"
        a.acp_args = []
        a.session_cwd = str(tmp_path)
        a.steer("use the other library")

        result = a.run_conversation("hello")

        assert result["completed"] is False
        assert result.get("pending_steer") == "use the other library"


class TestMissingAdapter:
    def test_missing_adapter_fails_with_an_actionable_message(self, tmp_path):
        a = _make_agent()
        a.acp_command = "definitely-not-a-real-adapter-binary"
        a.acp_args = []
        a.session_cwd = str(tmp_path)

        result = a.run_conversation("hello")

        assert result["completed"] is False
        message = result["final_response"].lower()
        assert "claude-agent-acp" in message or "adapter" in message
        assert "install" in message

    def test_missing_adapter_does_not_hang(self, tmp_path):
        a = _make_agent()
        a.acp_command = "definitely-not-a-real-adapter-binary"
        a.acp_args = []
        a.session_cwd = str(tmp_path)

        started = time.time()
        a.run_conversation("hello")
        assert time.time() - started < 30
