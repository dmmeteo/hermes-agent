"""Packet 3 slice 3B — ``session/request_permission`` reaches Hermes' approval flow.

Packets 1–2 denied every permission request because there was no approval UX.
This slice replaces that blanket denial with the SAME human gate Hermes already
uses for dangerous commands and plugin-escalated tool calls
(``tools/approval.py``), so a Claude Code write raises a real prompt on
whichever surface the session is running on.

What each class pins:

``TestApprovalGranted``      an approved write actually happens, and the standing
                            grant lives on Hermes' side, never the adapter's
``TestDenial``              denial blocks the write and the turn still completes
``TestDefaultDeny``         every way the approval surface can fail resolves to
                            deny — no surface, a raising surface, a timeout, or
                            an adapter that offers no single-shot allow
``TestInvariantsPreserved`` ``set_mode`` → ``default``, only the new user turn is
                            sent, role alternation, no synthetic user message,
                            credential scrubbing, workspace containment
``TestGrantGrain``          a remembered grant covers the action it was given
                            for and nothing else
``TestPromptIsBounded``     the prompt states the real scope of what it asks
                            about, and stays small enough to send

Every test in the first four classes drives a REAL fake-adapter subprocess over
REAL stdio; the last two are pure projections of protocol params.
"""

from __future__ import annotations

import json
import os
import sys
import threading

import pytest

import run_agent
from tests.agent._fake_claude_acp_adapter import (
    OUTSIDE_WORKSPACE_LOCATION,
    TOOL_INPUT_SECRET,
    TOOL_OUTPUT_SECRET,
)

_FAKE_ADAPTER = os.path.join(os.path.dirname(__file__), "_fake_claude_acp_adapter.py")

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="The fake adapter's process-group reaping assertions are POSIX-only; "
           "the permission bridge itself is platform-independent.",
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
    agent = _make_agent()
    agent.acp_command = sys.executable
    agent.acp_args = [_FAKE_ADAPTER]
    agent.session_cwd = str(tmp_path)
    return agent


class _Surface:
    """A recording stand-in for a human approval surface.

    Registered through ``tools.terminal_tool.set_approval_callback``, which is
    the per-thread seam the interactive CLI uses — so this exercises the real
    resolution path rather than patching the gate.
    """

    def __init__(self, choice="once"):
        self.choice = choice
        self.prompts = []

    def __call__(self, command, description, **kwargs):
        self.prompts.append({
            "command": command,
            "description": description,
            "kwargs": kwargs,
        })
        if isinstance(self.choice, Exception):
            raise self.choice
        return self.choice

    @property
    def text(self) -> str:
        return "\n".join(
            f"{p['command']}\n{p['description']}" for p in self.prompts
        )


@pytest.fixture
def approval_state(monkeypatch):
    """Isolate the approval module's process-global decision state.

    ``manual`` mode is pinned so the test never depends on the host's
    ``approvals.mode``, and the session/permanent allowlists are cleared so a
    remembered grant cannot leak between tests in one worker process.
    """
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
    installed = []

    def _install(choice="once"):
        surface = _Surface(choice)
        terminal_tool.set_approval_callback(surface)
        installed.append(surface)
        return surface

    yield _install
    # Per-thread state: leaving it installed would leak into the next test in
    # this worker process.
    terminal_tool.set_approval_callback(None)
    del installed


def _record_data(record):
    return json.loads(record.read_text(encoding="utf-8"))


class TestApprovalGranted:
    """An approved write must actually go through."""

    def test_an_approved_write_is_allowed_and_the_turn_completes(
        self, tmp_path, monkeypatch, cli_surface
    ):
        record = tmp_path / "record.json"
        surface = cli_surface("once")
        agent = _agent_with(tmp_path, "permission", monkeypatch, record)
        try:
            result = agent.run_conversation("please write a file")
        finally:
            agent.close()

        data = _record_data(record)
        assert data["permission_outcome"]["outcome"] == "selected"
        assert data["wrote_file"] is True
        assert (tmp_path / "permission-probe.txt").exists()
        assert len(surface.prompts) == 1, "the user must be asked exactly once"
        assert result["completed"] is True
        assert result["error"] is None

    def test_the_prompt_names_the_runtime_the_tool_and_the_target(
        self, tmp_path, monkeypatch, cli_surface
    ):
        surface = cli_surface("once")
        agent = _agent_with(tmp_path, "permission", monkeypatch)
        try:
            agent.run_conversation("please write a file")
        finally:
            agent.close()

        text = surface.text
        assert "Claude Code" in text, "the user must know who is asking"
        # The qualified label, not a bare "Write" the *title* would satisfy on
        # its own: 0.66.0 sends no tool name on a top-level permission request,
        # so this asserts the name was recovered from the tool call the adapter
        # emitted first rather than falling back to the ACP kind ("edit").
        assert "Claude Code: Write" in text, (
            "the user must know which tool wants to run"
        )
        assert "permission-probe.txt" in text, "the user must see the target"

    def test_the_adapter_is_never_handed_a_standing_grant(
        self, tmp_path, monkeypatch, cli_surface
    ):
        """Even an ``[a]lways`` answer selects the single-shot ACP option.

        A standing adapter-side grant (``allow_always``) would move the policy
        out of Hermes' view: every later write would be self-approved and Hermes
        would never see it. Durable grants belong in Hermes' own allowlist.
        """
        record = tmp_path / "record.json"
        cli_surface("always")
        agent = _agent_with(tmp_path, "permission", monkeypatch, record)
        try:
            agent.run_conversation("please write a file")
        finally:
            agent.close()

        assert _record_data(record)["permission_outcome"] == {
            "outcome": "selected", "optionId": "allow",
        }

    def test_a_remembered_grant_skips_the_second_prompt(
        self, tmp_path, monkeypatch, cli_surface
    ):
        """``[s]ession`` is remembered by Hermes, not by the adapter."""
        record = tmp_path / "record.json"
        surface = cli_surface("session")
        agent = _agent_with(tmp_path, "permission", monkeypatch, record)
        try:
            agent.run_conversation("please write a file")
            agent.run_conversation("please write it again")
        finally:
            agent.close()

        data = _record_data(record)
        assert data["permission_requests"] == 2, (
            "the adapter must still ask every time — the grant is Hermes' to "
            "remember, and Hermes must stay able to see each request"
        )
        assert len(surface.prompts) == 1, (
            "the second identical request must be answered from Hermes' "
            "session allowlist instead of prompting the user again"
        )
        assert [o["optionId"] for o in data["permission_outcomes"]] == [
            "allow", "allow",
        ]


class TestDenial:
    """A denial must block the write and still leave a usable turn."""

    def test_a_denied_write_is_blocked_and_the_turn_completes_cleanly(
        self, tmp_path, monkeypatch, cli_surface
    ):
        record = tmp_path / "record.json"
        cli_surface("deny")
        agent = _agent_with(tmp_path, "permission", monkeypatch, record)
        try:
            result = agent.run_conversation("please write a file")
        finally:
            agent.close()

        data = _record_data(record)
        assert data["permission_outcome"] == {
            "outcome": "selected", "optionId": "deny",
        }
        assert data["wrote_file"] is False
        assert not (tmp_path / "permission-probe.txt").exists()
        assert result["completed"] is True, (
            "a denial is a normal outcome, not a turn failure"
        )
        assert result["error"] is None
        assert "declined" in result["final_response"].lower()

    def test_a_denial_adds_no_synthetic_user_message(
        self, tmp_path, monkeypatch, cli_surface
    ):
        cli_surface("deny")
        agent = _agent_with(tmp_path, "permission", monkeypatch)
        try:
            result = agent.run_conversation("please write a file")
        finally:
            agent.close()

        roles = [m.get("role") for m in result["messages"]]
        assert roles.count("user") == 1, (
            "the approval round-trip must not inject a mid-loop user turn"
        )
        assert roles[-1] == "assistant"
        for previous, current in zip(roles, roles[1:]):
            assert previous != current, f"role alternation broken: {roles}"


class TestDefaultDeny:
    """Every failure of the approval surface must resolve to deny."""

    def test_no_approval_surface_denies(self, tmp_path, monkeypatch, approval_state):
        """No CLI, no gateway, not cron: nobody can answer, so nobody consents."""
        from tools import terminal_tool

        terminal_tool.set_approval_callback(None)
        monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
        record = tmp_path / "record.json"
        agent = _agent_with(tmp_path, "permission", monkeypatch, record)
        try:
            result = agent.run_conversation("please write a file")
        finally:
            agent.close()

        data = _record_data(record)
        assert data["permission_outcome"] == {
            "outcome": "selected", "optionId": "deny",
        }
        assert not (tmp_path / "permission-probe.txt").exists()
        assert result["completed"] is True

    def test_a_raising_approval_surface_denies(
        self, tmp_path, monkeypatch, cli_surface
    ):
        record = tmp_path / "record.json"
        cli_surface(RuntimeError("approval surface exploded"))
        agent = _agent_with(tmp_path, "permission", monkeypatch, record)
        try:
            agent.run_conversation("please write a file")
        finally:
            agent.close()

        assert _record_data(record)["permission_outcome"] == {
            "outcome": "selected", "optionId": "deny",
        }
        assert not (tmp_path / "permission-probe.txt").exists()

    def test_a_timed_out_prompt_denies(self, tmp_path, monkeypatch, cli_surface):
        """Silence is not consent."""
        record = tmp_path / "record.json"
        cli_surface("timeout")
        agent = _agent_with(tmp_path, "permission", monkeypatch, record)
        try:
            agent.run_conversation("please write a file")
        finally:
            agent.close()

        assert _record_data(record)["permission_outcome"] == {
            "outcome": "selected", "optionId": "deny",
        }
        assert not (tmp_path / "permission-probe.txt").exists()

    def test_an_adapter_offering_no_single_shot_allow_is_denied_unasked(
        self, tmp_path, monkeypatch, cli_surface
    ):
        """No ``allow_once`` option → refuse, and do not bother the user.

        Selecting ``allow_always`` would grant more than the one action the
        prompt described, so there is nothing to ask about.
        """
        record = tmp_path / "record.json"
        surface = cli_surface("once")
        agent = _agent_with(tmp_path, "permission-no-once", monkeypatch, record)
        try:
            result = agent.run_conversation("please write a file")
        finally:
            agent.close()

        data = _record_data(record)
        assert data["permission_outcome"] == {
            "outcome": "selected", "optionId": "deny",
        }
        assert data["wrote_file"] is False
        assert surface.prompts == [], (
            "a request Hermes cannot safely permit must not be shown as an "
            "approvable action"
        )
        assert result["completed"] is True

    def test_a_stopped_turn_denies_without_prompting(self, tmp_path):
        """A user who already pressed stop is not asked to approve anything."""
        from agent.claude_acp_session import ClaudeAcpSession, _TurnState

        session = ClaudeAcpSession(
            argv=[sys.executable, _FAKE_ADAPTER],
            cwd=str(tmp_path),
            hermes_session_id="stop-during-approval",
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

        assert response == {
            "outcome": {"outcome": "selected", "optionId": "deny"},
        }
        assert asked == []


class TestInvariantsPreserved:
    """The Packet 1/2 posture must survive the new approval path."""

    def test_permission_mode_is_still_negotiated_to_default(
        self, tmp_path, monkeypatch, cli_surface
    ):
        record = tmp_path / "record.json"
        cli_surface("once")
        agent = _agent_with(tmp_path, "permission", monkeypatch, record)
        try:
            agent.run_conversation("please write a file")
        finally:
            agent.close()

        assert _record_data(record)["set_mode_calls"] == ["default"], (
            "a silent `auto` default would let the adapter self-approve writes "
            "without ever asking Hermes"
        )

    def test_only_the_new_user_turn_is_sent(
        self, tmp_path, monkeypatch, cli_surface
    ):
        """Prompt-cache stability: the approval round-trip replays nothing."""
        record = tmp_path / "record.json"
        cli_surface("once")
        agent = _agent_with(tmp_path, "permission", monkeypatch, record)
        try:
            agent.run_conversation("please write a file")
            agent.run_conversation("and now the second turn")
        finally:
            agent.close()

        prompts = _record_data(record)["prompts"]
        sent = [
            [block.get("text", "") for block in turn["prompt"]]
            for turn in prompts
        ]
        assert sent == [["please write a file"], ["and now the second turn"]], (
            "history replay would invalidate Claude Code's own prompt cache"
        )

    def test_the_prompt_is_credential_scrubbed(
        self, tmp_path, monkeypatch, cli_surface
    ):
        """The approval prompt is user-visible text, on a chat platform too."""
        surface = cli_surface("deny")
        agent = _agent_with(tmp_path, "permission", monkeypatch)
        try:
            agent.run_conversation("please write a file")
        finally:
            agent.close()

        text = surface.text
        assert surface.prompts, "the surface was never asked"
        assert TOOL_INPUT_SECRET not in text
        assert TOOL_OUTPUT_SECRET not in text

    def test_an_approved_write_stays_inside_the_workspace(
        self, tmp_path, monkeypatch, cli_surface
    ):
        """Containment: approving one action does not widen the workspace.

        The adapter reports a second location outside its workspace. Hermes only
        ever displays that path — it never opens it — so approving the write
        must not bring it into existence.
        """
        surface = cli_surface("once")
        agent = _agent_with(tmp_path, "permission", monkeypatch)
        try:
            agent.run_conversation("please write a file")
        finally:
            agent.close()

        assert (tmp_path / "permission-probe.txt").exists()
        assert not os.path.exists(OUTSIDE_WORKSPACE_LOCATION)
        assert OUTSIDE_WORKSPACE_LOCATION in surface.text, (
            "the user must be shown every location the tool declared, "
            "including one outside the workspace"
        )

    def test_the_approval_runs_on_the_turn_thread(
        self, tmp_path, monkeypatch, cli_surface
    ):
        """The gate resolves per-thread state (callback, session key, interrupt).

        Answering the adapter from some other thread would look up a different
        thread's approval callback and miss the interrupt flag the gateway sets,
        so the request must be decided on the thread driving the turn.
        """
        surface = cli_surface("deny")
        seen = []
        original = surface.__call__

        def _record_thread(command, description, **kwargs):
            seen.append(threading.current_thread().ident)
            return original(command, description, **kwargs)

        from tools import terminal_tool

        terminal_tool.set_approval_callback(_record_thread)
        agent = _agent_with(tmp_path, "permission", monkeypatch)
        try:
            agent.run_conversation("please write a file")
        finally:
            agent.close()

        assert seen == [threading.current_thread().ident]


def _params(**tool_call):
    """``session/request_permission`` params carrying *tool_call*."""
    return {"sessionId": "s1", "toolCall": tool_call, "options": []}


def _write(path, **extra):
    return _params(
        _meta={"claudeCode": {"toolName": "Write"}},
        kind="edit",
        title=f"Write {os.path.basename(path)}",
        rawInput={"file_path": path},
        locations=[{"path": path}],
        **extra,
    )


class TestUnconditionalFloor:
    """The two checks that fire BEFORE the yolo bypass must reach this runtime.

    ``check_dangerous_command`` runs ``detect_hardline_command`` and
    ``_match_user_deny_rule`` ahead of the ``--yolo`` / ``approvals.mode=off``
    bypass, and ``_user_deny_block_result`` promises the user their deny rule
    holds "not even with --yolo". A delegated runtime asks over a protocol
    instead of through Hermes' tool loop, so the floor has to be re-applied at
    the point where that ask is decided — otherwise Hermes' own shell tool
    refuses ``rm -rf /`` unconditionally while the same action routed through
    Claude Code is auto-approved.

    The floor measures what the runtime *declared*, not the scrubbed and
    truncated display text: a redacted or cut command string is not the command
    the agent asked to run.
    """

    def test_a_hardline_command_is_blocked_even_under_yolo(
        self, approval_state, monkeypatch
    ):
        monkeypatch.setattr(approval_state, "_YOLO_MODE_FROZEN", True)

        decision = approval_state.request_delegated_runtime_approval(
            "Claude Code", "Bash", "rm -rf /", declared_command="rm -rf /",
        )

        assert decision["approved"] is False
        assert decision.get("hardline") is True
        assert "--yolo" in decision["message"]

    def test_a_declared_command_matching_a_deny_rule_is_blocked_under_yolo(
        self, approval_state, monkeypatch
    ):
        monkeypatch.setattr(approval_state, "_YOLO_MODE_FROZEN", True)
        monkeypatch.setattr(
            approval_state, "_get_approval_config",
            lambda: {"deny": ["*git push --force*"]},
        )

        decision = approval_state.request_delegated_runtime_approval(
            "Claude Code", "Bash", "git push --force",
            declared_command="git push --force origin main",
        )

        assert decision["approved"] is False
        assert decision.get("user_deny") is True

    def test_a_declared_path_matching_a_deny_rule_is_blocked(
        self, approval_state, monkeypatch
    ):
        """A deny glob the user wrote against their private keys must cover a
        delegated Write of them, not just a shell command that names them."""
        monkeypatch.setattr(approval_state, "_YOLO_MODE_FROZEN", True)
        monkeypatch.setattr(
            approval_state, "_get_approval_config",
            lambda: {"deny": ["*/.ssh/*"]},
        )

        decision = approval_state.request_delegated_runtime_approval(
            "Claude Code", "Write", "edit a key",
            declared_paths=("/home/u/.ssh/authorized_keys",),
        )

        assert decision["approved"] is False
        assert decision.get("user_deny") is True

    def test_a_path_that_merely_reads_like_a_hardline_command_is_not_blocked(
        self, approval_state, monkeypatch
    ):
        """The hardline blocklist is written against shell syntax, so it is not
        applied to bare paths: ``/w/mkfs-notes.md`` matches the ``mkfs`` pattern
        and blocking it would refuse an ordinary edit with no way to override.
        """
        monkeypatch.setattr(approval_state, "_YOLO_MODE_FROZEN", True)
        monkeypatch.setattr(approval_state, "_get_approval_config", lambda: {})

        decision = approval_state.request_delegated_runtime_approval(
            "Claude Code", "Write", "write notes",
            declared_paths=("/w/mkfs-notes.md",),
        )

        assert decision["approved"] is True

    def test_the_bridge_applies_the_floor_to_a_projected_request(
        self, approval_state, monkeypatch
    ):
        """End of the chain: projection → bridge → gate, under ``--yolo``."""
        monkeypatch.setattr(approval_state, "_YOLO_MODE_FROZEN", True)
        from agent.claude_code_runtime import make_claude_acp_permission_bridge
        from agent.transports.claude_acp_event_projector import (
            project_permission_request,
        )

        handler = make_claude_acp_permission_bridge()
        request = project_permission_request(_params(
            kind="execute", title="rm -rf /",
            rawInput={"command": "rm -rf /"},
        ))

        assert handler(request) is False, (
            "the hardline floor did not reach the delegated runtime: --yolo "
            "auto-approved an unconditionally blocked command"
        )

    def test_yolo_still_bypasses_the_recoverable_approval_layer(
        self, approval_state, monkeypatch
    ):
        """The floor must not turn into a second approval prompt: an ordinary
        command under yolo still goes through without asking anyone."""
        monkeypatch.setattr(approval_state, "_YOLO_MODE_FROZEN", True)
        monkeypatch.setattr(approval_state, "_get_approval_config", lambda: {})
        from agent.claude_code_runtime import make_claude_acp_permission_bridge
        from agent.transports.claude_acp_event_projector import (
            project_permission_request,
        )

        handler = make_claude_acp_permission_bridge()
        request = project_permission_request(_params(
            kind="execute", title="pytest -x",
            rawInput={"command": "pytest -x"},
        ))

        assert handler(request) is True


class TestToolNamingIsHonest:
    """The prompt names the tool the adapter declared, or says nothing it cannot
    back.

    Adapter 0.66.0 stamps ``_meta.claudeCode.toolName`` on a permission request
    **only** when the tool call has a parent (a sub-agent's tool use). For a
    top-level call the meta block is absent — but the adapter always emits the
    referenced ``tool_call`` update *first*, and that update carries the name.
    JSON-RPC over one stdio stream is ordered, so by the time the permission
    request is decided the name is already known.
    """

    def test_a_top_level_request_is_named_from_the_declared_tool_call(self):
        from agent.transports.claude_acp_event_projector import (
            ClaudeAcpEventProjector,
            project_permission_request,
        )

        projector = ClaudeAcpEventProjector()
        projector.project({
            "sessionUpdate": "tool_call",
            "toolCallId": "tc-1",
            "kind": "execute",
            "title": "rm -rf /tmp/x",
            "rawInput": {"command": "rm -rf /tmp/x"},
            "_meta": {"claudeCode": {"toolName": "Bash"}},
        })

        request = project_permission_request(
            _params(
                toolCallId="tc-1", kind="execute", title="rm -rf /tmp/x",
                rawInput={"command": "rm -rf /tmp/x"},
            ),
            tool_name_hint=projector.declared_tool_name("tc-1"),
        )

        assert request.tool_name == "Bash", (
            "the prompt named the ACP *kind* rather than the tool the adapter "
            "said it was running"
        )

    def test_a_parented_request_still_reads_its_own_meta(self):
        from agent.transports.claude_acp_event_projector import (
            project_permission_request,
        )

        request = project_permission_request(_params(
            _meta={"claudeCode": {"toolName": "Write", "parentToolUseId": "p1"}},
            kind="edit", title="Write a.py",
        ))

        assert request.tool_name == "Write"

    def test_an_unknown_tool_call_falls_back_to_the_kind(self):
        """No declared name anywhere: the kind is what we can honestly say."""
        from agent.transports.claude_acp_event_projector import (
            project_permission_request,
        )

        request = project_permission_request(
            _params(toolCallId="tc-unseen", kind="edit", title="Write a.py"),
            tool_name_hint="",
        )

        assert request.tool_name == "edit"

    def test_a_subagent_request_names_its_tool_end_to_end(
        self, tmp_path, monkeypatch, cli_surface
    ):
        """The parented shape, driven through a real adapter subprocess."""
        surface = cli_surface("deny")
        agent = _agent_with(tmp_path, "permission-subagent", monkeypatch)
        try:
            agent.run_conversation("please write a file")
        finally:
            agent.close()

        assert surface.prompts, "the approval surface was never reached"
        assert "Write" in surface.text

    def test_a_declared_name_is_not_invented_for_an_unrelated_call(self):
        from agent.transports.claude_acp_event_projector import (
            ClaudeAcpEventProjector,
        )

        projector = ClaudeAcpEventProjector()
        projector.project({
            "sessionUpdate": "tool_call", "toolCallId": "tc-1", "kind": "read",
            "_meta": {"claudeCode": {"toolName": "Read"}},
        })

        assert projector.declared_tool_name("tc-2") == ""


class TestGrantGrain:
    """``[a]lways`` must cover the action it was answered for, and no other.

    ``TestApprovalGranted`` proves a remembered grant is reused for the SAME
    action. These pin the other half — the half that would be a security hole
    rather than a missing feature — at the two layers that decide the grain:
    the request signature, and the allowlist key derived from it.
    """

    def test_the_same_action_asked_twice_shares_one_signature(self):
        from agent.transports.claude_acp_event_projector import (
            project_permission_request,
        )

        first = project_permission_request(_write("/w/a.py"))
        second = project_permission_request(_write("/w/a.py"))
        assert first.signature == second.signature

    def test_a_different_target_gets_a_different_signature(self):
        from agent.transports.claude_acp_event_projector import (
            project_permission_request,
        )

        approved = project_permission_request(_write("/w/a.py"))
        other = project_permission_request(_write("/w/b.py"))
        assert approved.signature != other.signature

    def test_a_different_tool_on_one_target_gets_a_different_signature(self):
        """Approving a Write of a file is not approving a Bash on it."""
        from agent.transports.claude_acp_event_projector import (
            project_permission_request,
        )

        write = project_permission_request(_write("/w/a.py"))
        bash = project_permission_request(_params(
            _meta={"claudeCode": {"toolName": "Bash"}},
            kind="execute",
            title="rm -rf /w/a.py",
            rawInput={"command": "rm -rf /w/a.py"},
            locations=[{"path": "/w/a.py"}],
        ))
        assert write.signature != bash.signature

    def test_signatures_differ_past_the_displayed_locations(self):
        """Identity sees every declared path, not just the shown ones.

        Two mass edits agreeing on their first eight paths but differing later
        are different actions. If the signature only saw the displayed slice,
        answering ``[a]lways`` to one would silently blanket the other.
        """
        from agent.transports.claude_acp_event_projector import (
            _MAX_LOCATIONS,
            project_permission_request,
        )

        shared = [{"path": f"/w/f{i}.py"} for i in range(_MAX_LOCATIONS)]
        first = project_permission_request(_params(
            _meta={"claudeCode": {"toolName": "Edit"}},
            kind="edit", title="bulk edit",
            locations=shared + [{"path": "/w/tail-one.py"}],
        ))
        second = project_permission_request(_params(
            _meta={"claudeCode": {"toolName": "Edit"}},
            kind="edit", title="bulk edit",
            locations=shared + [{"path": "/w/tail-two.py"}],
        ))
        assert first.locations == second.locations, "same displayed slice"
        assert first.signature != second.signature, (
            "a grant for one bulk edit must not cover a different one"
        )

    def test_signatures_differ_past_the_display_truncation(self):
        """Identity is computed over the DECLARED paths, not their display form.

        Display paths are scrubbed and cut to ``_MAX_PREVIEW_CHARS``. Two files
        under a common prefix longer than that render identically, so hashing the
        display view makes one ``[a]lways`` answer blanket every sibling.
        """
        from agent.transports.claude_acp_event_projector import (
            _MAX_PREVIEW_CHARS,
            project_permission_request,
        )

        deep = "/workspace/" + "a" * (_MAX_PREVIEW_CHARS + 10) + "/"
        first = project_permission_request(_params(
            kind="edit", title="bulk edit",
            locations=[{"path": deep + "alpha.py"}],
        ))
        second = project_permission_request(_params(
            kind="edit", title="bulk edit",
            locations=[{"path": deep + "beta.py"}],
        ))

        assert first.locations == second.locations, "same displayed slice"
        assert first.signature != second.signature, (
            "a grant for one deep path silently blanketed its siblings"
        )

    def test_signatures_differ_past_the_argument_truncation(self):
        """Same for ``rawInput``: the declared argument, not the cut one."""
        from agent.transports.claude_acp_event_projector import (
            _MAX_ARG_CHARS,
            project_permission_request,
        )

        head = "echo " + "x" * (_MAX_ARG_CHARS + 10)
        first = project_permission_request(_params(
            kind="execute", title="a long command",
            rawInput={"command": head + " > one.txt"},
        ))
        second = project_permission_request(_params(
            kind="execute", title="a long command",
            rawInput={"command": head + " > two.txt"},
        ))

        assert first.args == second.args, "same displayed arguments"
        assert first.signature != second.signature

    def test_two_asks_differing_only_inside_a_redacted_segment_differ(self):
        """Redaction is the second way the display view collapses two actions."""
        from agent.transports.claude_acp_event_projector import (
            project_permission_request,
        )

        first = project_permission_request(_params(
            kind="edit", title="write a token file",
            locations=[{"path": "/w/sk-ant-api03-" + "A" * 40 + "/f.py"}],
        ))
        second = project_permission_request(_params(
            kind="edit", title="write a token file",
            locations=[{"path": "/w/sk-ant-api03-" + "B" * 40 + "/f.py"}],
        ))

        assert first.signature != second.signature

    def test_the_signature_carries_no_declared_material(self):
        """It is an identity, and it is derived from raw values — so it must be
        opaque. A signature that quoted its inputs would put a credential from
        ``rawInput`` into whatever a caller logs it with."""
        from agent.transports.claude_acp_event_projector import (
            project_permission_request,
        )

        request = project_permission_request(_params(
            kind="execute", title="deploy",
            rawInput={"command": f"deploy --token {TOOL_INPUT_SECRET}"},
            locations=[{"path": "/w/secrets.env"}],
        ))

        assert TOOL_INPUT_SECRET not in request.signature
        assert "/w/secrets.env" not in request.signature

    def test_a_session_grant_for_one_action_does_not_approve_another(
        self, approval_state, monkeypatch
    ):
        """End of the chain: two signatures → two allowlist keys → one prompt.

        Drives the real gate with a stub surface that approves for the session,
        then asks about a *different* action and asserts the user is asked again.
        """
        import hashlib

        monkeypatch.setenv("HERMES_INTERACTIVE", "1")

        from agent.claude_code_runtime import RUNTIME_LABEL
        from agent.transports.claude_acp_event_projector import (
            project_permission_request,
        )

        prompts = []

        def _surface(command, description, **kwargs):
            prompts.append(command)
            return "session"

        def _ask(request):
            return approval_state.request_delegated_runtime_approval(
                RUNTIME_LABEL,
                request.tool_name,
                request.description,
                rule_key=hashlib.sha256(
                    request.signature.encode("utf-8")
                ).hexdigest()[:16],
                approval_callback=_surface,
            )

        first = project_permission_request(_write("/w/a.py"))
        assert _ask(first)["approved"] is True
        # The same action again: answered from Hermes' session allowlist.
        assert _ask(project_permission_request(_write("/w/a.py")))["approved"] is True
        assert len(prompts) == 1, "the remembered grant was not reused"
        # A different action: the grant must not reach it.
        assert _ask(project_permission_request(_write("/w/b.py")))["approved"] is True
        assert len(prompts) == 2, (
            "a session grant for one action silently approved a different one"
        )


class TestPromptIsBounded:
    """The prompt must state the real scope, and stay small enough to send."""

    def test_extra_locations_are_reported_not_silently_dropped(self):
        """A user shown 8 of 400 paths has been shown the wrong action."""
        from agent.transports.claude_acp_event_projector import (
            _MAX_LOCATIONS,
            project_permission_request,
        )

        total = _MAX_LOCATIONS + 12
        request = project_permission_request(_params(
            _meta={"claudeCode": {"toolName": "Edit"}},
            kind="edit", title="bulk edit",
            locations=[{"path": f"/w/f{i}.py"} for i in range(total)],
        ))

        assert len(request.locations) == _MAX_LOCATIONS
        assert request.omitted_locations == 12
        assert "+12 more" in request.description, (
            "the prompt must not understate how much it is asking about"
        )

    def test_one_absurd_path_cannot_blow_up_the_prompt(self):
        """Paths are display-bounded: a chat platform has a message limit."""
        from agent.transports.claude_acp_event_projector import (
            _MAX_LOCATIONS,
            _MAX_PREVIEW_CHARS,
            project_permission_request,
        )

        request = project_permission_request(_write("/w/" + "x" * 50_000))
        # +1 for the "…" `_truncate` appends to mark the cut.
        assert len(request.locations[0]) <= _MAX_PREVIEW_CHARS + 1
        # Worst case the whole prompt stays in messaging-platform territory.
        assert len(request.description) < _MAX_PREVIEW_CHARS * (_MAX_LOCATIONS + 4)

    def test_a_request_with_nothing_declared_still_says_something(self):
        from agent.transports.claude_acp_event_projector import (
            project_permission_request,
        )

        request = project_permission_request({})
        assert request.tool_name == "tool"
        assert request.description.strip()
        assert request.omitted_locations == 0
