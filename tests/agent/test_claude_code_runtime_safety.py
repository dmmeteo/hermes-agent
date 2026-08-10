"""Packet 1 remediation — safety and correctness fixes from the independent review.

Each class maps to one reviewed finding:

F1  permission mode is negotiated to ``default`` and every request is denied
F3  a latched interrupt is cleared even when cancel races with ``end_turn``
F4  Anthropic credential vars never reach the adapter child
F7  the whole process tree is reaped, not just the direct child
F8  delegated-runtime parity for the memory / skill / background-review hooks
F9  non-success stop reasons produce an explicit failure, never a silent
    ``completed: True`` with no assistant row

All tests drive a REAL fake-adapter subprocess over REAL stdio.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time

import pytest

import run_agent

_FAKE_ADAPTER = os.path.join(os.path.dirname(__file__), "_fake_claude_acp_adapter.py")

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="Reaping/PID assertions use POSIX process groups; the runtime's "
           "Windows path is exercised by the taskkill unit test below.",
)


def _make_agent(**kwargs):
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


def _agent_with(tmp_path, mode, monkeypatch, record=None):
    monkeypatch.setenv("FAKE_ACP_MODE", mode)
    if record is not None:
        monkeypatch.setenv("FAKE_ACP_RECORD", str(record))
    a = _make_agent()
    a.acp_command = sys.executable
    a.acp_args = [_FAKE_ADAPTER]
    a.session_cwd = str(tmp_path)
    return a


@pytest.fixture
def record_env(monkeypatch):
    """Ask the fake adapter to report extra env vars in ``inherited_env``.

    The adapter records only what it was told to watch, so a test asserting
    "this secret did not reach the child" names the var itself rather than
    growing a hardcoded provider list inside the fake.
    """

    def _watch(*names):
        monkeypatch.setenv("FAKE_ACP_WATCH_ENV", ",".join(names))

    return _watch


def _pid_alive(pid: int) -> bool:
    # psutil, not os.kill(pid, 0): on Windows that signals CTRL_C_EVENT to the
    # target's console group rather than probing liveness (bpo-14484).
    import psutil

    return psutil.pid_exists(pid)


def _interrupt_once_prompt_lands(agent, record, timeout=20.0):
    """Interrupt only after the adapter has actually received the prompt.

    A fixed sleep races turn setup: if the interrupt fires before the runtime
    publishes its turn object there is nothing to cancel, and the test
    measures that race instead of the cancel path.
    """

    def _wait_and_interrupt():
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if json.loads(record.read_text(encoding="utf-8")).get("prompt"):
                    break
            except Exception:
                pass
            time.sleep(0.02)
        agent.interrupt("stop")

    thread = threading.Thread(target=_wait_and_interrupt, daemon=True)
    thread.start()
    return thread


class TestPermissionSafety:
    """F1 — the adapter must never be left on its self-approving default."""

    def test_session_mode_is_negotiated_to_default(self, tmp_path, monkeypatch):
        record = tmp_path / "record.json"
        agent = _agent_with(tmp_path, "normal", monkeypatch, record)

        agent.run_conversation("hello")

        data = json.loads(record.read_text(encoding="utf-8"))
        assert data["set_mode_calls"] == ["default"], (
            "the runtime must explicitly leave the adapter's self-approving "
            "'auto' mode before any prompt runs"
        )

    def test_permission_request_is_denied_and_write_is_blocked(self, tmp_path, monkeypatch):
        record = tmp_path / "record.json"
        agent = _agent_with(tmp_path, "permission", monkeypatch, record)

        result = agent.run_conversation("please write a file")

        data = json.loads(record.read_text(encoding="utf-8"))
        assert data["permission_outcome"] == {
            "outcome": "selected", "optionId": "deny",
        }
        assert data["wrote_file"] is False
        assert not (tmp_path / "permission-probe.txt").exists()
        # Denial is not a turn failure — the turn still completes.
        assert result["completed"] is True

    def test_unknown_agent_request_is_refused_not_ignored(self, tmp_path, monkeypatch):
        """An unhandled agent request must still get a reply, or the turn hangs."""
        from agent.claude_acp_session import ClaudeAcpSession

        session = ClaudeAcpSession(
            argv=[sys.executable, _FAKE_ADAPTER], cwd=str(tmp_path)
        )
        with pytest.raises(PermissionError):
            session._on_request("terminal/create", {})


class TestInterruptLatchCleared:
    """F3 — a cancel that races a normal completion must not poison turn N+1."""

    def test_interrupt_is_cleared_when_cancel_is_ignored(self, tmp_path, monkeypatch):
        record = tmp_path / "record.json"
        agent = _agent_with(tmp_path, "ignore-cancel", monkeypatch, record)

        _interrupt_once_prompt_lands(agent, record)
        agent.run_conversation("first turn")

        assert agent._interrupt_requested is False, (
            "a late cancel that the adapter answered with end_turn still leaves "
            "the interrupt latched, which silently drops the next user message"
        )

    def test_interrupt_latched_before_the_turn_starts_is_not_lost(
        self, tmp_path, monkeypatch
    ):
        """An interrupt can land before the runtime publishes its turn object.

        `AIAgent.interrupt()` then finds no turn to cancel. If that flag is not
        folded back in, the adapter runs to the full turn timeout while the
        user believes they stopped it.
        """
        agent = _agent_with(tmp_path, "cancel-empty", monkeypatch)
        agent.interrupt("stop before we even begin")

        started = time.time()
        result = agent.run_conversation("this should not run to the timeout")
        elapsed = time.time() - started

        assert elapsed < 60, f"pre-turn interrupt was lost; turn ran {elapsed:.0f}s"
        assert result["final_response"].strip()

    def test_pre_prompt_cancel_is_reissued_once_the_prompt_is_in_flight(
        self, tmp_path, monkeypatch
    ):
        """L2 — a `session/cancel` delivered before any prompt exists may be
        legitimately ignored by a real adapter. Re-issue it once the prompt is
        actually dispatched, or the turn runs to natural completion despite the
        user having asked to stop.

        The adapter here models exactly that: it discards any cancel seen
        before the prompt handler starts.
        """
        agent = _agent_with(tmp_path, "cancel-after-prompt-only", monkeypatch)
        agent.interrupt("stop before the prompt is dispatched")

        started = time.time()
        result = agent.run_conversation("this must not run to the timeout")
        elapsed = time.time() - started

        assert elapsed < 60, (
            f"the pre-prompt cancel was never re-issued; turn ran {elapsed:.0f}s"
        )
        assert result["interrupted"] is True
        assert result["final_response"].strip()

    def test_second_turn_still_works_after_a_late_cancel(self, tmp_path, monkeypatch):
        record = tmp_path / "record.json"
        agent = _agent_with(tmp_path, "ignore-cancel", monkeypatch, record)

        # Join the interrupter before turn two: a fixed sleep could otherwise
        # let the interrupt land in the SECOND turn and cancel that instead.
        interrupter = _interrupt_once_prompt_lands(agent, record)
        agent.run_conversation("first turn")
        interrupter.join(timeout=20)

        # From Packet 2 the SAME adapter process serves turn two (session
        # reuse), so it keeps the mode it was spawned with — what matters is
        # that the turn runs and completes rather than inheriting the latch.
        second = agent.run_conversation("second turn")
        assert second["final_response"]
        assert second["completed"] is True
        assert agent._interrupt_requested is False


class TestAdapterChildEnvironment:
    """F4 / H1 — API-billing keys are stripped, the subscription token is not.

    `CLAUDE_CODE_OAUTH_TOKEN` comes from `claude setup-token` and IS the
    subscription credential on a headless host. Stripping it (the first
    remediation did) breaks the very auth this provider advertises.
    """

    @pytest.mark.parametrize("var", ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"])
    def test_api_billing_credentials_are_stripped_from_the_child(
        self, tmp_path, monkeypatch, var
    ):
        monkeypatch.setenv(var, "sentinel-should-not-reach-adapter")
        record = tmp_path / "record.json"
        agent = _agent_with(tmp_path, "normal", monkeypatch, record)

        agent.run_conversation("hello")

        data = json.loads(record.read_text(encoding="utf-8"))
        assert data["inherited_env"][var] is None, (
            f"{var} reached the adapter; the child would bill the API account "
            f"while the UI advertises the Claude subscription"
        )

    def test_subscription_setup_token_reaches_the_child(self, tmp_path, monkeypatch):
        """H1 — this is the headless subscription credential, not an API key."""
        sentinel = "sk-ant-oat01-OBVIOUS-TEST-SENTINEL"
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", sentinel)
        record = tmp_path / "record.json"
        agent = _agent_with(tmp_path, "normal", monkeypatch, record)

        agent.run_conversation("hello")

        data = json.loads(record.read_text(encoding="utf-8"))
        assert data["inherited_env"]["CLAUDE_CODE_OAUTH_TOKEN"] == sentinel, (
            "CLAUDE_CODE_OAUTH_TOKEN is the `claude setup-token` subscription "
            "credential; stripping it breaks auth on headless gateway/cron hosts"
        )

    @pytest.mark.parametrize(
        "var", ["OPENAI_API_KEY", "OPENROUTER_API_KEY", "BRAVE_SEARCH_API_KEY"]
    )
    def test_unrelated_provider_and_tool_secrets_do_not_reach_the_child(
        self, tmp_path, monkeypatch, record_env, var
    ):
        """M2 — the adapter needs exactly one credential, not the whole wallet.

        The child is a Node process whose ``Bash`` tool executes model-authored
        commands. Every other provider key and tool secret in Hermes' env is
        reachable from that shell for no benefit: the Claude Code provider
        authenticates with the subscription token, which survives
        ``inherit_credentials=False`` because it is deliberately excluded from
        the blocklist.
        """
        record_env(var)
        monkeypatch.setenv(var, "sentinel-should-not-reach-adapter")
        record = tmp_path / "record.json"
        agent = _agent_with(tmp_path, "normal", monkeypatch, record)

        agent.run_conversation("hello")

        data = json.loads(record.read_text(encoding="utf-8"))
        assert data["inherited_env"][var] is None, (
            f"{var} reached the Claude Code adapter, whose shell tool runs "
            "model-authored commands"
        )

    def test_the_subscription_token_survives_the_narrower_posture(
        self, tmp_path, monkeypatch, record_env
    ):
        """The pair that makes M2 safe: one secret in, everything else out."""
        record_env("OPENAI_API_KEY")
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-KEEP-ME")
        monkeypatch.setenv("OPENAI_API_KEY", "sentinel-should-not-reach-adapter")
        record = tmp_path / "record.json"
        agent = _agent_with(tmp_path, "normal", monkeypatch, record)

        agent.run_conversation("hello")

        data = json.loads(record.read_text(encoding="utf-8"))
        assert data["inherited_env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-KEEP-ME"
        assert data["inherited_env"]["OPENAI_API_KEY"] is None

    def test_stripping_does_not_mutate_the_parent_environment(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "parent-must-keep-this")
        agent = _agent_with(tmp_path, "normal", monkeypatch)
        agent.run_conversation("hello")
        assert os.environ["ANTHROPIC_API_KEY"] == "parent-must-keep-this"


class TestAuthFailureDiagnostics:
    """L1/F10 — a headless auth failure must be legible, and never leak secrets."""

    def test_auth_required_produces_an_actionable_message(self, tmp_path, monkeypatch):
        agent = _agent_with(tmp_path, "auth-required", monkeypatch)
        result = agent.run_conversation("hello")

        assert result["completed"] is False
        message = result["final_response"]
        assert "claude setup-token" in message or "claude` once" in message
        assert "log in" in message.lower() or "authenticate" in message.lower()

    def test_long_diagnostic_lines_are_truncated(self):
        """L1 — bound line LENGTH too. One long JSON blob on a single stderr
        line would otherwise land whole in a Discord/Telegram message."""
        from agent.claude_code_runtime import (
            _STDERR_DIAGNOSTIC_LINE_CHARS,
            _diagnostics,
        )

        class _Turn:
            stderr_tail = ["x" * 5000]

        text = _diagnostics(_Turn())
        assert text
        longest = max(len(line) for line in text.splitlines())
        # Allow the two-space indent on top of the per-line cap.
        assert longest <= _STDERR_DIAGNOSTIC_LINE_CHARS + 8, (
            f"diagnostic line was {longest} chars"
        )
        assert len(text) < 5000

    def test_truncation_keeps_the_informative_head_of_the_line(self):
        from agent.claude_code_runtime import _diagnostics

        class _Turn:
            stderr_tail = ["ENOENT: adapter could not start " + "y" * 4000]

        text = _diagnostics(_Turn())
        assert "ENOENT: adapter could not start" in text

    def test_failure_includes_bounded_scrubbed_stderr(self, tmp_path, monkeypatch):
        agent = _agent_with(tmp_path, "stderr-noise", monkeypatch)
        result = agent.run_conversation("hello")

        message = result["final_response"]
        assert result["completed"] is False
        # A recognisable diagnostic line is surfaced ...
        assert "adapter-diagnostic-marker" in message
        # ... but the secret on an adjacent stderr line never is.
        assert "sk-ant-SUPERSECRET" not in message
        # ... and the tail is bounded, not the whole ring buffer.
        assert len(message.splitlines()) <= 12


class TestProcessTreeCleanup:
    """F7 — 'no stale subprocess under any exit path' must include descendants."""

    def test_grandchild_does_not_survive_the_session(self, tmp_path, monkeypatch):
        """Packet 2 moved the boundary from the turn to the session.

        The adapter is now long-lived and legitimately outlives a turn, so the
        reaping contract is asserted where teardown actually happens — and it
        still has to take the whole process tree with it.
        """
        record = tmp_path / "record.json"
        agent = _agent_with(tmp_path, "grandchild", monkeypatch, record)

        agent.run_conversation("hello")

        data = json.loads(record.read_text(encoding="utf-8"))
        grandchild_pid = data["grandchild_pid"]
        assert grandchild_pid, "fake adapter did not report a grandchild pid"

        agent.close()

        deadline = time.time() + 5
        while time.time() < deadline and _pid_alive(grandchild_pid):
            time.sleep(0.1)
        assert not _pid_alive(grandchild_pid), (
            "the adapter's descendant outlived the session — killing only the "
            "direct child is exactly the leak this runtime must prevent"
        )

    def test_adapter_is_spawned_in_its_own_process_group(self, tmp_path):
        """Group leadership is what makes the group kill safe and effective."""
        from agent.transports.claude_acp import ClaudeAcpClient

        client = ClaudeAcpClient(
            command=[sys.executable, _FAKE_ADAPTER], cwd=str(tmp_path)
        )
        try:
            assert os.getpgid(client.pid) == client.pid
        finally:
            client.close()


class TestWindowsTreeKill:
    """F7 / M1 — the Windows branch cannot run here, so assert argv AND order."""

    @staticmethod
    def _windows_teardown(monkeypatch):
        """Run _terminate_process_tree on a simulated Windows host.

        Returns the ordered sequence of teardown actions.
        """
        import agent.transports.claude_acp as mod

        sequence: list = []

        class _FakeProc:
            pid = 4321
            returncode = None

            def kill(self):
                sequence.append(("kill",))

            def terminate(self):
                sequence.append(("terminate",))

        monkeypatch.setattr(mod, "IS_WINDOWS", True)
        monkeypatch.setattr(
            mod.subprocess, "run",
            lambda argv, **kw: sequence.append(("run", list(argv))),
        )
        mod._terminate_process_tree(_FakeProc())
        return sequence

    def test_windows_uses_taskkill_tree(self, monkeypatch):
        sequence = self._windows_teardown(monkeypatch)
        runs = [item for item in sequence if item[0] == "run"]
        assert runs, "no taskkill issued on the Windows path"
        argv = runs[0][1]
        assert argv[0] == "taskkill"
        assert "/T" in argv and "/F" in argv
        assert "4321" in argv

    def test_taskkill_runs_before_the_direct_child_is_killed(self, monkeypatch):
        """M1 — the direct child is the .cmd shim. Kill it first and taskkill
        can no longer enumerate the node.exe grandchild, which is the exact
        leak this teardown exists to prevent."""
        sequence = self._windows_teardown(monkeypatch)
        kinds = [item[0] for item in sequence]
        assert "run" in kinds, "no taskkill issued"
        assert "kill" in kinds, "direct child was never killed as a fallback"
        assert kinds.index("run") < kinds.index("kill"), (
            f"taskkill must precede kill/terminate; got order {kinds}"
        )

    def test_close_does_not_terminate_the_shim_before_taskkill(self, tmp_path, monkeypatch):
        """M1-residual — the ordering must hold at the close() level too.

        The helper being correct is not enough: on the wedged-adapter path
        close() previously called terminate() first, killing the .cmd shim
        before taskkill could enumerate its node.exe child. Exercised against a
        REAL wedged adapter process with Windows simulated.
        """
        import agent.transports.claude_acp as mod

        monkeypatch.setenv("FAKE_ACP_MODE", "wedged")
        client = mod.ClaudeAcpClient(
            command=[sys.executable, _FAKE_ADAPTER], cwd=str(tmp_path)
        )
        real_proc = client._proc
        sequence: list = []

        class _RecordingProc:
            """Delegates to the real Popen, recording teardown calls."""

            def __getattr__(self, name):
                return getattr(real_proc, name)

            def terminate(self):
                sequence.append("terminate")
                real_proc.terminate()

            def kill(self):
                sequence.append("kill")
                real_proc.kill()

        try:
            client.initialize()
            client._proc = _RecordingProc()
            monkeypatch.setattr(mod, "IS_WINDOWS", True)
            monkeypatch.setattr(
                mod.subprocess, "run",
                lambda argv, **kw: sequence.append("taskkill"),
            )

            client.close(timeout=0.5)

            assert "taskkill" in sequence, "close() never issued a tree kill"
            first_direct = next(
                (i for i, s in enumerate(sequence) if s in ("terminate", "kill")),
                len(sequence),
            )
            assert sequence.index("taskkill") < first_direct, (
                f"close() killed the shim before taskkill could walk the tree; "
                f"got {sequence}"
            )
        finally:
            try:
                real_proc.kill()
                real_proc.wait(timeout=5)
            except Exception:
                pass

    def test_posix_close_still_reaps_a_wedged_adapter(self, tmp_path, monkeypatch):
        """The Windows branch must not regress ordinary POSIX teardown."""
        import agent.transports.claude_acp as mod

        monkeypatch.setenv("FAKE_ACP_MODE", "wedged")
        client = mod.ClaudeAcpClient(
            command=[sys.executable, _FAKE_ADAPTER], cwd=str(tmp_path)
        )
        pid = client.pid
        client.initialize()
        client.close(timeout=0.5)

        deadline = time.time() + 5
        while time.time() < deadline and _pid_alive(pid):
            time.sleep(0.1)
        assert not _pid_alive(pid), "wedged adapter survived close() on POSIX"


class TestProcessGroupFallback:
    """L4 — never signal a process group we may not own."""

    def test_pgid_is_none_when_getpgid_fails(self, tmp_path, monkeypatch):
        import agent.transports.claude_acp as mod

        monkeypatch.setattr(
            mod.os, "getpgid",
            lambda pid: (_ for _ in ()).throw(ProcessLookupError("gone")),
        )
        client = mod.ClaudeAcpClient(
            command=[sys.executable, _FAKE_ADAPTER], cwd=str(tmp_path)
        )
        try:
            assert client._pgid is None, (
                "falling back to the pid can name a group we do not own once "
                "that pid is recycled"
            )
        finally:
            client.close()

    def test_no_group_kill_when_pgid_is_unknown(self, monkeypatch):
        import agent.transports.claude_acp as mod

        killed: list = []

        class _FakeProc:
            pid = 999999
            returncode = None

            def kill(self):
                pass

        monkeypatch.setattr(mod, "IS_WINDOWS", False)
        monkeypatch.setattr(mod.os, "killpg", lambda *a: killed.append(a))
        monkeypatch.setattr(
            mod.os, "getpgid",
            lambda pid: (_ for _ in ()).throw(ProcessLookupError("gone")),
        )
        mod._terminate_process_tree(_FakeProc(), pgid=None)
        assert not killed, "signalled a process group whose ownership is unknown"


class TestDelegatedRuntimeParity:
    """F8 — the codex delegated runtime fires these; parity or it is a regression."""

    def test_background_review_fires_when_triggered(self, tmp_path, monkeypatch):
        agent = _agent_with(tmp_path, "normal", monkeypatch)
        calls = []
        monkeypatch.setattr(
            type(agent), "_spawn_background_review",
            lambda self, **kw: calls.append(kw), raising=False,
        )
        monkeypatch.setattr(type(agent), "_should_review_memory", lambda self: True,
                            raising=False)

        from agent.claude_code_runtime import run_claude_code_acp_turn

        messages = [{"role": "user", "content": "hello"}]
        run_claude_code_acp_turn(
            agent,
            user_message="hello",
            original_user_message="hello",
            messages=messages,
            effective_task_id="t1",
            should_review_memory=True,
        )
        assert calls, "background review never fired on the ACP runtime"
        assert calls[0]["review_memory"] is True

    def test_external_memory_sync_fires_on_success(self, tmp_path, monkeypatch):
        agent = _agent_with(tmp_path, "normal", monkeypatch)
        synced = []
        monkeypatch.setattr(
            type(agent), "_sync_external_memory_for_turn",
            lambda self, **kw: synced.append(kw), raising=False,
        )

        from agent.claude_code_runtime import run_claude_code_acp_turn

        run_claude_code_acp_turn(
            agent,
            user_message="hello",
            original_user_message="hello",
            messages=[{"role": "user", "content": "hello"}],
            effective_task_id="t1",
        )
        assert synced, "external memory sync never fired on the ACP runtime"
        assert synced[0]["final_response"] == "The answer is 42."

    def test_memory_sync_is_skipped_on_a_failed_turn(self, tmp_path, monkeypatch):
        agent = _agent_with(tmp_path, "error", monkeypatch)
        synced = []
        monkeypatch.setattr(
            type(agent), "_sync_external_memory_for_turn",
            lambda self, **kw: synced.append(kw), raising=False,
        )

        from agent.claude_code_runtime import run_claude_code_acp_turn

        run_claude_code_acp_turn(
            agent,
            user_message="hello",
            original_user_message="hello",
            messages=[{"role": "user", "content": "hello"}],
            effective_task_id="t1",
        )
        assert not synced, "partial transcripts must not be fed to memory"

    def test_skill_iteration_counter_advances(self, tmp_path, monkeypatch):
        agent = _agent_with(tmp_path, "normal", monkeypatch)
        agent._iters_since_skill = 0

        from agent.claude_code_runtime import run_claude_code_acp_turn

        run_claude_code_acp_turn(
            agent,
            user_message="hello",
            original_user_message="hello",
            messages=[{"role": "user", "content": "hello"}],
            effective_task_id="t1",
        )
        assert agent._iters_since_skill >= 1


class TestNonSuccessStopReasons:
    """F9 — never report success with an empty response and no assistant row."""

    def test_refusal_is_reported_as_a_failure(self, tmp_path, monkeypatch):
        agent = _agent_with(tmp_path, "refusal", monkeypatch)
        result = agent.run_conversation("do something disallowed")

        assert result["completed"] is False
        assert result["error"]
        assert result["final_response"].strip(), (
            "an empty final response leaves the transcript ending on a user "
            "message and breaks role alternation on the next turn"
        )
        assert "refusal" in result["final_response"].lower()

    def test_transcript_does_not_end_on_a_user_message(self, tmp_path, monkeypatch):
        agent = _agent_with(tmp_path, "refusal", monkeypatch)
        result = agent.run_conversation("do something disallowed")

        messages = result["messages"]
        assert messages, "no messages recorded"
        assert messages[-1]["role"] == "assistant"
        assert messages[-1]["content"].strip()

    def test_cancelled_with_zero_text_still_appends_an_assistant_row(
        self, tmp_path, monkeypatch
    ):
        """M3 — interrupting before the first chunk arrives must not leave the
        transcript ending on a user row (the next turn would then append a
        second consecutive user message)."""
        record = tmp_path / "record.json"
        agent = _agent_with(tmp_path, "cancel-empty", monkeypatch, record)

        _interrupt_once_prompt_lands(agent, record)
        result = agent.run_conversation("start something long")

        assert result["final_response"].strip(), (
            "a cancelled turn with no streamed text produced an empty response"
        )
        messages = result["messages"]
        assert messages[-1]["role"] == "assistant"
        assert messages[-1]["content"].strip()

    def test_roles_alternate_across_a_cancelled_then_normal_turn(
        self, tmp_path, monkeypatch
    ):
        record = tmp_path / "record.json"
        agent = _agent_with(tmp_path, "cancel-empty", monkeypatch, record)

        _interrupt_once_prompt_lands(agent, record)
        agent.run_conversation("first")

        monkeypatch.setenv("FAKE_ACP_MODE", "normal")
        result = agent.run_conversation("second")

        roles = [m["role"] for m in result["messages"] if m["role"] in ("user", "assistant")]
        doubled = [
            (a, b) for a, b in zip(roles, roles[1:]) if a == b
        ]
        assert not doubled, f"consecutive same-role messages: {roles}"
