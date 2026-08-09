"""Behaviour tests for the ACP stdio client (Packet 0).

Every test drives a REAL subprocess (``_fake_acp_agent.py``) over REAL stdio
framing — no mocked pipes. That is deliberate: the failure modes this client
exists to prevent (deadlock while a server-initiated request is outstanding,
notifications delivered on the wrong thread, a wedged child surviving close)
only reproduce against a real process.
"""

from __future__ import annotations

import os
import sys
import threading

import pytest

from agent.transports.claude_acp import (
    AcpError,
    AcpProtocolError,
    ClaudeAcpClient,
    deny_permission_response,
    ensure_path_within_roots,
)

_FAKE_AGENT = os.path.join(os.path.dirname(__file__), "_fake_acp_agent.py")


@pytest.fixture
def client(tmp_path):
    c = ClaudeAcpClient(command=[sys.executable, _FAKE_AGENT], cwd=str(tmp_path))
    try:
        yield c
    finally:
        c.close()


class TestHandshake:
    def test_initialize_returns_agent_capabilities(self, client):
        result = client.initialize()
        assert result["protocolVersion"] == 1
        assert result["agentCapabilities"]["loadSession"] is True

    def test_initialize_records_capabilities_on_the_client(self, client):
        client.initialize()
        assert client.protocol_version == 1
        assert client.supports_load_session is True

    def test_session_new_returns_session_id(self, client):
        client.initialize()
        assert client.session_new() == "sess-1"


class TestPromptTurn:
    def test_prompt_streams_updates_then_returns_stop_reason(self, client, tmp_path):
        client.initialize()
        session_id = client.session_new()

        seen: list[dict] = []
        result = client.prompt(
            session_id,
            [{"type": "text", "text": "hi"}],
            on_update=seen.append,
        )

        assert result["stopReason"] == "end_turn"
        kinds = [u["sessionUpdate"] for u in seen]
        assert kinds[:2] == ["agent_message_chunk", "tool_call"]

    def test_updates_are_dispatched_on_the_calling_thread(self, client):
        """Handlers must not run on the reader thread — a slow handler there
        would stall the wire and deadlock the turn."""
        client.initialize()
        session_id = client.session_new()

        caller = threading.get_ident()
        handler_threads: list[int] = []
        client.prompt(
            session_id,
            [{"type": "text", "text": "hi"}],
            on_update=lambda _u: handler_threads.append(threading.get_ident()),
        )

        assert handler_threads
        assert set(handler_threads) == {caller}

    def test_server_initiated_request_is_answered_mid_turn(self, client, tmp_path):
        """The agent asks the client to read a file while the prompt is still
        in flight. If we did not answer it, the turn would never finish."""
        target = tmp_path / "notes.txt"
        target.write_text("workspace-content", encoding="utf-8")

        client.initialize()
        session_id = client.session_new()

        def on_request(method, params):
            assert method == "fs/read_text_file"
            return {"content": target.read_text(encoding="utf-8")}

        texts: list[str] = []

        def on_update(update):
            if update["sessionUpdate"] == "agent_message_chunk":
                texts.append(update["content"]["text"])

        result = client.prompt(
            session_id,
            [{"type": "text", "text": "read it"}],
            on_update=on_update,
            on_request=on_request,
        )

        assert result["stopReason"] == "end_turn"
        assert "workspace-content" in texts

    def test_unhandled_server_request_gets_an_error_not_a_hang(self, client):
        """With no handler the client must still reply (method-not-found) so
        the agent unblocks. Silence here is the classic ACP deadlock."""
        client.initialize()
        session_id = client.session_new()

        texts: list[str] = []

        def on_update(update):
            if update["sessionUpdate"] == "agent_message_chunk":
                texts.append(update["content"]["text"])

        result = client.prompt(session_id, [{"type": "text", "text": "hi"}], on_update=on_update)

        assert result["stopReason"] == "end_turn"
        assert any(t.startswith("error:") for t in texts)

    def test_handler_exception_becomes_an_error_reply(self, client):
        client.initialize()
        session_id = client.session_new()

        def on_request(method, params):
            raise RuntimeError("handler blew up")

        texts: list[str] = []

        def on_update(update):
            if update["sessionUpdate"] == "agent_message_chunk":
                texts.append(update["content"]["text"])

        result = client.prompt(
            session_id,
            [{"type": "text", "text": "hi"}],
            on_update=on_update,
            on_request=on_request,
        )

        assert result["stopReason"] == "end_turn"
        assert any(t.startswith("error:") for t in texts)


class TestSessionMode:
    def test_set_mode_sends_the_requested_mode(self, client):
        """Packet 1 must leave the adapter's self-approving default."""
        client.initialize()
        session_id = client.session_new()
        # The fake agent echoes the modeId it received.
        assert client.set_mode(session_id, "default") == {"modeId": "default"}


class TestCancellation:
    def test_cancel_makes_the_turn_report_cancelled(self, client):
        client.initialize()
        session_id = client.session_new()

        def on_update(update):
            # Cancel as soon as the first chunk lands, i.e. mid-turn.
            client.cancel(session_id)

        result = client.prompt(
            session_id,
            [{"type": "text", "text": "hi"}],
            on_update=on_update,
        )
        assert result["stopReason"] == "cancelled"


class TestErrors:
    def test_jsonrpc_error_is_raised_with_code_and_message(self, client):
        client.initialize()
        with pytest.raises(AcpError) as excinfo:
            client.request("boom", {})
        assert excinfo.value.code == -32000
        assert "kaboom" in str(excinfo.value)

    def test_unknown_method_raises_method_not_found(self, client):
        client.initialize()
        with pytest.raises(AcpError) as excinfo:
            client.request("session/does_not_exist", {})
        assert excinfo.value.code == -32601

    def test_request_after_close_raises_protocol_error(self, client):
        client.initialize()
        client.close()
        with pytest.raises(AcpProtocolError):
            client.request("initialize", {})


class TestLifecycle:
    def test_close_reaps_the_child_process(self, client):
        client.initialize()
        client.close()
        assert client.returncode is not None

    def test_close_is_idempotent(self, client):
        client.initialize()
        client.close()
        client.close()

    def test_context_manager_closes(self, tmp_path):
        with ClaudeAcpClient(command=[sys.executable, _FAKE_AGENT], cwd=str(tmp_path)) as c:
            c.initialize()
        assert c.returncode is not None


class TestWorkspaceContainment:
    def test_path_inside_root_is_allowed(self, tmp_path):
        (tmp_path / "a.txt").write_text("x", encoding="utf-8")
        resolved = ensure_path_within_roots(str(tmp_path / "a.txt"), [str(tmp_path)])
        assert resolved == (tmp_path / "a.txt").resolve()

    def test_path_outside_root_is_rejected(self, tmp_path):
        with pytest.raises(PermissionError):
            ensure_path_within_roots("/etc/passwd", [str(tmp_path)])

    def test_traversal_escape_is_rejected(self, tmp_path):
        escape = str(tmp_path / ".." / "elsewhere.txt")
        with pytest.raises(PermissionError):
            ensure_path_within_roots(escape, [str(tmp_path)])

    def test_sibling_prefix_directory_is_not_treated_as_inside(self, tmp_path):
        """``/ws-evil`` must not pass a ``/ws`` root check on string prefix."""
        root = tmp_path / "ws"
        root.mkdir()
        sibling = tmp_path / "ws-evil"
        sibling.mkdir()
        with pytest.raises(PermissionError):
            ensure_path_within_roots(str(sibling / "x.txt"), [str(root)])

    def test_additional_root_is_honoured(self, tmp_path):
        extra = tmp_path / "extra"
        extra.mkdir()
        resolved = ensure_path_within_roots(
            str(extra / "b.txt"), [str(tmp_path / "ws"), str(extra)]
        )
        assert resolved == (extra / "b.txt").resolve()


class TestPermissionHelper:
    def test_deny_picks_a_reject_option_when_offered(self):
        options = [
            {"optionId": "yes", "kind": "allow_once"},
            {"optionId": "no", "kind": "reject_once"},
        ]
        assert deny_permission_response(options) == {
            "outcome": {"outcome": "selected", "optionId": "no"}
        }

    def test_deny_falls_back_to_cancelled_when_no_reject_option(self):
        assert deny_permission_response([{"optionId": "yes", "kind": "allow_once"}]) == {
            "outcome": {"outcome": "cancelled"}
        }

    def test_deny_handles_empty_options(self):
        assert deny_permission_response([]) == {"outcome": {"outcome": "cancelled"}}
