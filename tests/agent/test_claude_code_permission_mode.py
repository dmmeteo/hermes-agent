"""Packet 5 slice A — the Claude Code permission mode is a profile config decision.

Packets 1–3 hardcoded ``session/set_mode`` → ``default`` on every connect, so a
profile owner's own ``~/.claude/settings.json`` ``permissions.defaultMode`` was
honoured by the adapter at session creation and then immediately overwritten.
This slice makes the posture ``model.claude_code_acp.permission_mode``, with
today's behaviour as the default, an explicit enumerated opt-in for the
self-approving modes, and ``inherit`` for "send no ``set_mode`` at all".

What each class pins:

``TestModePolicy``            the pure policy — what a configured value resolves
                              to against what the live adapter advertises, and
                              that every rejection narrows to ``default``
``TestConfiguredModeReachesTheWire``
                              the configured mode is the one negotiated, on a
                              new session and on a rebind; ``inherit`` sends
                              nothing; unknown/unadvertised/unreportable all
                              negotiate ``default``
``TestSessionIdentity``       a changed mode rebuilds the session rather than
                              silently keeping the old posture, and the session
                              can state the posture in force
``TestModeChangeProjection``  a mid-turn ``current_mode_update`` is state
                              capture only — no bubble, no callback
``TestApprovalFloorSurvives`` whatever the mode, a request that DOES arrive
                              still goes through Hermes' approval gate and a
                              denial still blocks

The wire tests drive REAL fake-adapter subprocesses over REAL stdio: the mode
is negotiated in the connect sequence, which mocks would not exercise.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time

import pytest
import yaml

import run_agent
from agent.claude_acp_permissions import (
    DEFAULT_PERMISSION_MODE,
    INHERIT,
    SELF_APPROVING_MODES,
    configured_permission_mode,
    resolve_permission_mode,
)
from agent.claude_acp_session import ClaudeAcpSession
from agent.transports.claude_acp_event_projector import ClaudeAcpEventProjector
from hermes_state import SessionDB

_FAKE_ADAPTER = os.path.join(os.path.dirname(__file__), "_fake_claude_acp_adapter.py")

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="The fake adapter's process-group reaping is POSIX-only; the mode "
           "policy itself is platform-independent.",
)


# --------------------------------------------------------------------- helpers


@pytest.fixture
def write_mode_config():
    """Write ``model.claude_code_acp.permission_mode`` into config.yaml.

    Real config file, real loader — the posture is a user-facing config key and
    deliberately has no env-var route (AGENTS.md §Adding Configuration).
    ``HERMES_HOME`` is already a per-test tempdir (tests/conftest.py).
    """

    def _write(mode):
        from hermes_cli.config import get_config_path

        config_path = get_config_path()
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            yaml.safe_dump(
                {"model": {"claude_code_acp": {"permission_mode": mode}}}
            ),
            encoding="utf-8",
        )
        return config_path

    return _write


def _agent_with(tmp_path, mode, monkeypatch, *, record=None, record_dir=None,
                store=None, db=None, session_id="", **env):
    monkeypatch.setenv("FAKE_ACP_MODE", mode)
    if record is not None:
        monkeypatch.setenv("FAKE_ACP_RECORD", str(record))
    if record_dir is not None:
        monkeypatch.setenv("FAKE_ACP_RECORD_DIR", str(record_dir))
    if store is not None:
        monkeypatch.setenv("FAKE_ACP_STORE", str(store))
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    extra = {}
    if db is not None:
        extra = {"session_db": db, "session_id": session_id or "sess-mode"}
    agent = run_agent.AIAgent(
        api_key="claude-code-acp",
        base_url="acp://claude-code",
        provider="claude-code-acp",
        api_mode="claude_code_acp",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
        **extra,
    )
    agent.acp_command = sys.executable
    agent.acp_args = [_FAKE_ADAPTER]
    agent.session_cwd = str(tmp_path)
    agent._skill_nudge_interval = 0
    return agent


def _record_data(record):
    return json.loads(record.read_text(encoding="utf-8"))


def _records_in(record_dir):
    out = []
    for path in sorted(record_dir.glob("*.json"), key=lambda p: p.stat().st_mtime):
        try:
            out.append(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            continue
    return out


def _kill(pid: int) -> None:
    import psutil

    os.kill(pid, signal.SIGKILL)
    deadline = time.time() + 10
    while time.time() < deadline and psutil.pid_exists(pid):
        time.sleep(0.02)


def _modes_of(agent):
    from agent.claude_code_runtime import claude_acp_session

    return claude_acp_session(agent)


# ------------------------------------------------------------- the pure policy


class TestModePolicy:
    """``resolve_permission_mode`` is the whole policy, and it only narrows."""

    @pytest.mark.parametrize(
        "configured,advertised,expected",
        [
            # The opt-in, honoured when the live adapter offers it.
            ("auto", ["auto", "default"], "auto"),
            ("acceptEdits", ["default", "acceptEdits"], "acceptEdits"),
            ("plan", ["default", "plan"], "plan"),
            # Today's posture, unchanged.
            ("default", ["auto", "default"], "default"),
            ("", ["auto", "default"], "default"),
            (None, ["auto", "default"], "default"),
            # A mode this adapter never advertised must not reach the wire.
            ("auto", ["default"], "default"),
            ("bypassPermissions", [], "default"),
            # An adapter that reports no mode state at all is not evidence that
            # a self-approving mode is available.
            ("auto", None, "default"),
            # Not a mode at all.
            ("nonsense", ["auto", "default"], "default"),
            ("AUTO", ["auto", "default"], "default"),
        ],
    )
    def test_resolution(self, configured, advertised, expected):
        assert resolve_permission_mode(configured, advertised) == expected

    @pytest.mark.parametrize("advertised", [None, [], ["inherit"], ["auto", "default"]])
    def test_inherit_resolves_to_no_call_whatever_the_adapter_says(self, advertised):
        """``inherit`` is the one answer that is not a mode: send nothing."""
        assert resolve_permission_mode(INHERIT, advertised) is None

    def test_the_default_is_a_mode_hermes_gates(self):
        """The conservative default must never be a self-approving one."""
        assert DEFAULT_PERMISSION_MODE not in SELF_APPROVING_MODES

    def test_every_self_approving_mode_is_reachable_only_by_naming_it(self):
        """No advertised list can produce a self-approving mode by itself."""
        for mode in SELF_APPROVING_MODES:
            assert resolve_permission_mode("", [mode]) == DEFAULT_PERMISSION_MODE
            assert resolve_permission_mode(mode, [mode]) == mode

    def test_configured_mode_defaults_when_the_key_is_absent(self):
        assert configured_permission_mode() == DEFAULT_PERMISSION_MODE

    def test_configured_mode_reads_the_key(self, write_mode_config):
        write_mode_config("auto")
        assert configured_permission_mode() == "auto"

    def test_configured_mode_survives_an_unreadable_config(self):
        from hermes_cli.config import get_config_path

        config_path = get_config_path()
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text("model: [not, a, mapping\n", encoding="utf-8")
        assert configured_permission_mode() == DEFAULT_PERMISSION_MODE

    def test_a_non_string_value_reads_as_the_default(self, write_mode_config):
        write_mode_config(["auto"])
        assert configured_permission_mode() == DEFAULT_PERMISSION_MODE


# ------------------------------------------------------------------- the wire


class TestConfiguredModeReachesTheWire:
    """What the profile configured is what the adapter is actually put into."""

    def test_default_config_still_negotiates_default(self, tmp_path, monkeypatch):
        record = tmp_path / "record.json"
        agent = _agent_with(tmp_path, "normal", monkeypatch, record=record)
        try:
            agent.run_conversation("hello")
        finally:
            agent.close()

        assert _record_data(record)["set_mode_calls"] == ["default"], (
            "with no config key the runtime must keep leaving the adapter's "
            "self-approving 'auto' for 'default'"
        )

    def test_configured_auto_mode_is_the_mode_negotiated(
        self, tmp_path, monkeypatch, write_mode_config
    ):
        write_mode_config("auto")
        record = tmp_path / "record.json"
        agent = _agent_with(tmp_path, "normal", monkeypatch, record=record)
        try:
            result = agent.run_conversation("hello")
        finally:
            agent.close()

        assert _record_data(record)["set_mode_calls"] == ["auto"], (
            "the profile's explicit opt-in must be the mode negotiated, not "
            "the hardcoded 'default'"
        )
        assert result["completed"] is True

    def test_inherit_sends_no_set_mode_at_all(
        self, tmp_path, monkeypatch, write_mode_config
    ):
        write_mode_config(INHERIT)
        record = tmp_path / "record.json"
        agent = _agent_with(tmp_path, "normal", monkeypatch, record=record)
        try:
            result = agent.run_conversation("hello")
        finally:
            agent.close()

        assert _record_data(record)["set_mode_calls"] == [], (
            "'inherit' means the adapter's own settings.json posture stands"
        )
        assert result["completed"] is True

    def test_an_unknown_mode_narrows_to_default(
        self, tmp_path, monkeypatch, write_mode_config
    ):
        write_mode_config("yolo")
        record = tmp_path / "record.json"
        agent = _agent_with(tmp_path, "normal", monkeypatch, record=record)
        try:
            agent.run_conversation("hello")
        finally:
            agent.close()

        assert _record_data(record)["set_mode_calls"] == ["default"]

    def test_an_unadvertised_mode_never_reaches_the_wire(
        self, tmp_path, monkeypatch, write_mode_config
    ):
        write_mode_config("bypassPermissions")
        record = tmp_path / "record.json"
        agent = _agent_with(
            tmp_path, "normal", monkeypatch, record=record,
            FAKE_ACP_MODES="default,plan",
        )
        try:
            agent.run_conversation("hello")
        finally:
            agent.close()

        assert _record_data(record)["set_mode_calls"] == ["default"], (
            "a mode the live session never advertised must be narrowed away, "
            "not sent and hoped for"
        )

    def test_a_session_that_advertises_no_modes_still_gets_default(
        self, tmp_path, monkeypatch, write_mode_config
    ):
        """Advertising an empty list is a statement, and it narrows too."""
        write_mode_config("dontAsk")
        record = tmp_path / "record.json"
        agent = _agent_with(
            tmp_path, "normal", monkeypatch, record=record, FAKE_ACP_MODES="",
        )
        try:
            agent.run_conversation("hello")
        finally:
            agent.close()

        assert _record_data(record)["set_mode_calls"] == ["default"]

    def test_an_adapter_that_reports_no_modes_still_gets_default(
        self, tmp_path, monkeypatch, write_mode_config
    ):
        """Never 'inherit' by accident: no mode state is not a licence."""
        write_mode_config("auto")
        record = tmp_path / "record.json"
        agent = _agent_with(
            tmp_path, "normal", monkeypatch, record=record, FAKE_ACP_NO_MODES="1",
        )
        try:
            agent.run_conversation("hello")
        finally:
            agent.close()

        assert _record_data(record)["set_mode_calls"] == ["default"]

    def test_the_configured_mode_is_renegotiated_after_a_rebind(
        self, tmp_path, monkeypatch, write_mode_config
    ):
        """The mode is session state the adapter re-derives on every connect."""
        write_mode_config("acceptEdits")
        records = tmp_path / "records"
        store = tmp_path / "store"
        workdir = tmp_path / "workspace"
        for path in (records, store, workdir):
            path.mkdir()
        db = SessionDB(db_path=tmp_path / "sessions.db")
        agent = _agent_with(
            workdir, "memory", monkeypatch, record_dir=records, store=store,
            db=db, session_id="sess-rebind",
        )
        try:
            agent.run_conversation("q-one")
            _kill(_records_in(records)[0]["adapter_pid"])
            agent.run_conversation("q-two")
        finally:
            agent.close()

        adapters = _records_in(records)
        assert len(adapters) == 2, "the second turn must have rebound"
        assert adapters[1]["session_load_calls"], "expected a rebind, not a new session"
        assert adapters[1]["set_mode_calls"] == ["acceptEdits"]

    def test_an_adapter_that_refuses_the_mode_fails_the_turn_not_the_posture(
        self, tmp_path, monkeypatch, write_mode_config
    ):
        """A refused negotiation must never leave the turn running unmoded."""
        write_mode_config("auto")
        record = tmp_path / "record.json"
        agent = _agent_with(
            tmp_path, "normal", monkeypatch, record=record,
            FAKE_ACP_REJECT_SET_MODE="1",
        )
        try:
            result = agent.run_conversation("hello")
        finally:
            agent.close()

        assert _record_data(record)["set_mode_calls"] == ["auto"]
        assert result["completed"] is False, (
            "a session whose mode could not be negotiated must not go on to "
            "prompt the model"
        )


# --------------------------------------------------------------- session state


class TestSessionIdentity:
    """A live session knows the posture it negotiated, and a change rebuilds."""

    def test_matches_is_false_when_only_the_mode_changed(self, tmp_path):
        session = ClaudeAcpSession(
            argv=["adapter"], cwd=str(tmp_path), permission_mode="default",
        )
        try:
            assert session.matches(["adapter"], str(tmp_path), [], "default")
            assert not session.matches(["adapter"], str(tmp_path), [], "auto"), (
                "a config change must rebuild the session rather than silently "
                "keep the old permission posture"
            )
        finally:
            session.close()

    def test_the_negotiated_and_effective_modes_are_the_configured_one(
        self, tmp_path, monkeypatch, write_mode_config
    ):
        write_mode_config("plan")
        agent = _agent_with(tmp_path, "normal", monkeypatch)
        try:
            agent.run_conversation("hello")
            session = _modes_of(agent)
            assert session is not None
            assert session.negotiated_permission_mode == "plan"
            assert session.effective_permission_mode == "plan"
        finally:
            agent.close()

    def test_inherit_reports_what_the_adapter_itself_was_running(
        self, tmp_path, monkeypatch, write_mode_config
    ):
        """The whole point of `inherit`: the posture is knowable off the wire."""
        write_mode_config(INHERIT)
        agent = _agent_with(
            tmp_path, "normal", monkeypatch, FAKE_ACP_CURRENT_MODE="acceptEdits",
        )
        try:
            agent.run_conversation("hello")
            session = _modes_of(agent)
            assert session is not None
            assert session.negotiated_permission_mode is None
            assert session.effective_permission_mode == "acceptEdits"
        finally:
            agent.close()

    def test_a_session_that_never_connected_reports_no_effective_mode(self, tmp_path):
        session = ClaudeAcpSession(
            argv=["adapter"], cwd=str(tmp_path), permission_mode="auto",
        )
        try:
            assert session.negotiated_permission_mode is None
            assert session.effective_permission_mode is None
        finally:
            session.close()


# ---------------------------------------------------------------- projection


class TestModeChangeProjection:
    """`current_mode_update` is state capture, exactly like `model_changed`."""

    def test_a_mode_update_projects_as_a_mode_change(self):
        projector = ClaudeAcpEventProjector()
        event = projector.project(
            {"sessionUpdate": "current_mode_update", "currentModeId": "plan"}
        )
        assert event.kind == "mode_changed"
        assert event.mode_id == "plan"
        assert projector.mode_id == "plan"

    def test_a_restatement_within_a_turn_is_ignored(self):
        projector = ClaudeAcpEventProjector()
        projector.project(
            {"sessionUpdate": "current_mode_update", "currentModeId": "plan"}
        )
        repeat = projector.project(
            {"sessionUpdate": "current_mode_update", "currentModeId": "plan"}
        )
        assert repeat.kind == "ignored"

    def test_a_malformed_mode_update_is_ignored(self):
        projector = ClaudeAcpEventProjector()
        for payload in (
            {"sessionUpdate": "current_mode_update"},
            {"sessionUpdate": "current_mode_update", "currentModeId": ""},
            {"sessionUpdate": "current_mode_update", "currentModeId": 7},
        ):
            assert projector.project(payload).kind == "ignored"
        assert projector.mode_id is None

    def test_a_mode_update_does_not_become_assistant_text(self):
        projector = ClaudeAcpEventProjector()
        projector.project(
            {"sessionUpdate": "current_mode_update", "currentModeId": "plan"}
        )
        assert projector.assistant_text == ""

    def test_a_mid_turn_switch_updates_the_effective_mode(
        self, tmp_path, monkeypatch
    ):
        agent = _agent_with(
            tmp_path, "mode-switch", monkeypatch, FAKE_ACP_SWITCH_MODE="plan",
        )
        try:
            result = agent.run_conversation("switch please")
            session = _modes_of(agent)
            assert result["completed"] is True
            assert session.negotiated_permission_mode == "default"
            assert session.effective_permission_mode == "plan", (
                "the wire is the authority on the mode actually in force"
            )
        finally:
            agent.close()

    def test_no_display_callback_fires_for_a_mode_change(self, tmp_path, monkeypatch):
        agent = _agent_with(tmp_path, "mode-switch", monkeypatch)
        progress: list = []
        agent.tool_progress_callback = lambda *a, **kw: progress.append((a, kw))
        try:
            agent.run_conversation("switch please")
        finally:
            agent.close()

        assert progress == [], "a mode change has no user-facing surface"


# ------------------------------------------------------------ the safety floor


class TestApprovalFloorSurvives:
    """The mode changes how many requests exist, never what happens to one."""

    @pytest.fixture
    def approval_state(self, monkeypatch):
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
    def cli_surface(self, monkeypatch, approval_state):
        from tools import terminal_tool

        monkeypatch.setenv("HERMES_INTERACTIVE", "1")

        def _install(choice="once"):
            class _Surface:
                def __init__(self) -> None:
                    self.prompts: list = []

                def __call__(self, command, description, **kwargs):
                    self.prompts.append((command, description))
                    return choice

            surface = _Surface()
            terminal_tool.set_approval_callback(surface)
            return surface

        yield _install
        terminal_tool.set_approval_callback(None)

    @pytest.mark.parametrize("mode", ["default", "auto", "bypassPermissions"])
    def test_a_request_that_arrives_is_still_gated(
        self, tmp_path, monkeypatch, write_mode_config, cli_surface, mode
    ):
        """Self-approving modes reduce requests; they do not disarm the gate."""
        write_mode_config(mode)
        record = tmp_path / "record.json"
        surface = cli_surface("deny")
        agent = _agent_with(tmp_path, "permission", monkeypatch, record=record)
        try:
            result = agent.run_conversation("please write a file")
        finally:
            agent.close()

        data = _record_data(record)
        assert surface.prompts, "the user must still be asked"
        assert data["permission_outcome"] == {
            "outcome": "selected", "optionId": "deny",
        }
        assert data["wrote_file"] is False
        assert not (tmp_path / "permission-probe.txt").exists()
        assert result["completed"] is True

    def test_the_sudo_stdin_floor_still_refuses_in_a_self_approving_mode(
        self, tmp_path, monkeypatch, write_mode_config, cli_surface
    ):
        """The unconditional floor is not a function of the negotiated mode."""
        write_mode_config("auto")
        record = tmp_path / "record.json"
        surface = cli_surface("once")
        agent = _agent_with(
            tmp_path, "permission-sudo-stdin", monkeypatch, record=record,
        )
        try:
            agent.run_conversation("please run it")
        finally:
            agent.close()

        data = _record_data(record)
        assert data["permission_outcome"] == {
            "outcome": "selected", "optionId": "deny",
        }
        assert surface.prompts == [], "no human may be consulted about this one"
