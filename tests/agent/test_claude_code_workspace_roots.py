"""Packet 3 slice 3C — workspace containment for the Claude Code ACP runtime.

The adapter runs Claude Code's own tools inside its own process, so Hermes
cannot contain it by intercepting `fs/*` (Packet 0, risk R4: the adapter read a
file with its native `Read` tool and never called back). Containment therefore
has to be *negotiated*: the session is created with a `cwd` and only the
directories the user explicitly allowed, and anything the agent does ask the
client for is checked against exactly those roots.

What each class pins:

``TestRootNegotiation``     `session/new` carries the workspace and ONLY the
                            explicitly-allowed extra directories; junk entries
                            never widen the session; an adapter that cannot
                            accept extra roots gets none rather than a guess
``TestOutOfRootRequests``   an agent-initiated filesystem request is refused —
                            out-of-root loudly, in-root because Hermes
                            advertises no filesystem capability at all — and
                            the turn still finishes rather than deadlocking
``TestPermissionLocations`` a declared location outside the negotiated roots is
                            marked as such in the human-facing prompt
``TestContinuityPreserved`` roots are renegotiated on rebind, and changing them
                            does not cost the conversation its Claude-side
                            session

The negotiation tests read the params the adapter actually received off the
wire; the containment tests drive an adapter that calls back into the client.
"""

from __future__ import annotations

import json
import os
import sys

import pytest
import yaml

import run_agent
from hermes_state import SessionDB
from tests.agent._fake_claude_acp_adapter import (
    FS_REQUEST_INSIDE,
    FS_REQUEST_OUTSIDE,
    OUTSIDE_WORKSPACE_LOCATION,
)

_FAKE_ADAPTER = os.path.join(os.path.dirname(__file__), "_fake_claude_acp_adapter.py")

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="The fake adapter's process-group reaping is POSIX-only; root "
           "negotiation itself is platform-independent.",
)


@pytest.fixture
def allow_directories():
    """Write ``model.claude_code_acp.additional_directories`` to config.yaml.

    Real config, real loader — the setting is a user-facing config key (never a
    new env var), so the test exercises the path a user actually has.
    ``HERMES_HOME`` is already a per-test tempdir (tests/conftest.py).
    """

    def _write(*paths):
        from hermes_cli.config import get_config_path

        config_path = get_config_path()
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            yaml.safe_dump(
                {"model": {"claude_code_acp": {
                    "additional_directories": [str(p) for p in paths]
                }}}
            ),
            encoding="utf-8",
        )
        return config_path

    return _write


def _agent_with(tmp_path, mode, monkeypatch, *, workdir=None, record=None,
                record_dir=None, store=None, db=None, session_id=""):
    monkeypatch.setenv("FAKE_ACP_MODE", mode)
    if record is not None:
        monkeypatch.setenv("FAKE_ACP_RECORD", str(record))
    if record_dir is not None:
        monkeypatch.setenv("FAKE_ACP_RECORD_DIR", str(record_dir))
    if store is not None:
        monkeypatch.setenv("FAKE_ACP_STORE", str(store))
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
    agent.session_cwd = str(workdir or tmp_path)
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


def _new_session_params(data, index=0):
    params = data.get("session_new_params") or []
    assert params, f"the adapter recorded no session/new params: {data}"
    return params[index]


def _negotiated_roots(data, index=0):
    return list(_new_session_params(data, index).get("additionalDirectories") or [])


@pytest.fixture
def workspace(tmp_path):
    """An isolated workspace directory, distinct from the tmp root itself."""
    path = tmp_path / "workspace"
    path.mkdir()
    return path


class _Surface:
    """A recording stand-in for a human approval surface."""

    def __init__(self, choice="deny"):
        self.choice = choice
        self.prompts = []

    def __call__(self, command, description, **kwargs):
        self.prompts.append({"command": command, "description": description})
        return self.choice

    @property
    def text(self) -> str:
        return "\n".join(f"{p['command']}\n{p['description']}" for p in self.prompts)


@pytest.fixture
def cli_surface(monkeypatch):
    """An interactive-CLI approval surface, installed for this thread only."""
    from tools import approval as approval_module
    from tools import terminal_tool

    monkeypatch.setenv("HERMES_INTERACTIVE", "1")
    monkeypatch.setattr(approval_module, "_YOLO_MODE_FROZEN", False)
    monkeypatch.setattr(approval_module, "_get_approval_mode", lambda: "manual")
    for table in (
        approval_module._session_approved,
        approval_module._permanent_approved,
        approval_module._pending,
    ):
        table.clear()

    def _install(choice="deny"):
        surface = _Surface(choice)
        terminal_tool.set_approval_callback(surface)
        return surface

    yield _install
    terminal_tool.set_approval_callback(None)
    for table in (
        approval_module._session_approved,
        approval_module._permanent_approved,
        approval_module._pending,
    ):
        table.clear()


class TestRootNegotiation:
    """Containment is what `session/new` says it is."""

    def test_the_workspace_is_negotiated_as_the_session_cwd(
        self, tmp_path, workspace, monkeypatch
    ):
        record = tmp_path / "record.json"
        agent = _agent_with(
            tmp_path, "normal", monkeypatch, workdir=workspace, record=record
        )
        try:
            agent.run_conversation("hello")
        finally:
            agent.close()

        assert _new_session_params(_record_data(record))["cwd"] == str(workspace)

    def test_no_extra_directories_are_negotiated_by_default(
        self, tmp_path, workspace, monkeypatch
    ):
        """The default session is the workspace and nothing else."""
        record = tmp_path / "record.json"
        agent = _agent_with(
            tmp_path, "normal", monkeypatch, workdir=workspace, record=record
        )
        try:
            agent.run_conversation("hello")
        finally:
            agent.close()

        assert _negotiated_roots(_record_data(record)) == []

    def test_only_the_explicitly_allowed_directory_is_negotiated(
        self, tmp_path, workspace, monkeypatch, allow_directories
    ):
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        unlisted = tmp_path / "unlisted"
        unlisted.mkdir()
        allow_directories(allowed)

        record = tmp_path / "record.json"
        agent = _agent_with(
            tmp_path, "normal", monkeypatch, workdir=workspace, record=record
        )
        try:
            agent.run_conversation("hello")
        finally:
            agent.close()

        roots = _negotiated_roots(_record_data(record))
        assert roots == [str(allowed)]
        assert str(unlisted) not in roots, (
            "a directory the user never allowed was handed to the adapter"
        )

    def test_a_missing_directory_is_dropped_rather_than_negotiated(
        self, tmp_path, workspace, monkeypatch, allow_directories
    ):
        """Dropping narrows the session; forwarding it widens a session against
        a path nobody verified."""
        allow_directories(tmp_path / "does-not-exist")

        record = tmp_path / "record.json"
        agent = _agent_with(
            tmp_path, "normal", monkeypatch, workdir=workspace, record=record
        )
        try:
            agent.run_conversation("hello")
        finally:
            agent.close()

        assert _negotiated_roots(_record_data(record)) == []

    def test_a_relative_path_is_not_negotiated(
        self, tmp_path, workspace, monkeypatch, allow_directories
    ):
        """A relative root resolves against whatever the process cwd happens to
        be — which is not something the user explicitly allowed."""
        allow_directories("../..")

        record = tmp_path / "record.json"
        agent = _agent_with(
            tmp_path, "normal", monkeypatch, workdir=workspace, record=record
        )
        try:
            agent.run_conversation("hello")
        finally:
            agent.close()

        assert _negotiated_roots(_record_data(record)) == []

    def test_a_file_is_not_a_root(
        self, tmp_path, workspace, monkeypatch, allow_directories
    ):
        target = tmp_path / "notes.txt"
        target.write_text("x", encoding="utf-8")
        allow_directories(target)

        record = tmp_path / "record.json"
        agent = _agent_with(
            tmp_path, "normal", monkeypatch, workdir=workspace, record=record
        )
        try:
            agent.run_conversation("hello")
        finally:
            agent.close()

        assert _negotiated_roots(_record_data(record)) == []

    def test_the_workspace_is_not_repeated_as_an_extra_root(
        self, tmp_path, workspace, monkeypatch, allow_directories
    ):
        allow_directories(workspace, workspace / "inside")
        (workspace / "inside").mkdir()

        record = tmp_path / "record.json"
        agent = _agent_with(
            tmp_path, "normal", monkeypatch, workdir=workspace, record=record
        )
        try:
            agent.run_conversation("hello")
        finally:
            agent.close()

        assert _negotiated_roots(_record_data(record)) == [], (
            "the cwd and paths already under it add no scope and only make the "
            "negotiated root list harder to audit"
        )

    def test_an_adapter_that_cannot_widen_a_session_is_sent_no_extra_roots(
        self, tmp_path, workspace, monkeypatch, allow_directories
    ):
        """Capability-gated: an unsupported field is silently ignored by some
        agents and a hard error on others, and guessing which is a containment
        claim we cannot back."""
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        allow_directories(allowed)

        record = tmp_path / "record.json"
        store = tmp_path / "store"
        store.mkdir()
        agent = _agent_with(
            tmp_path, "no-additional-dirs", monkeypatch,
            workdir=workspace, record=record, store=store,
        )
        try:
            result = agent.run_conversation("hello")
        finally:
            agent.close()

        data = _record_data(record)
        assert "additionalDirectories" not in _new_session_params(data), (
            "extra roots were sent to an adapter that never advertised support"
        )
        assert result["completed"] is True, "the turn must still run"


class TestOutOfRootRequests:
    """What the agent does ask the client for is checked against the roots."""

    def test_hermes_advertises_no_filesystem_capability(
        self, tmp_path, workspace, monkeypatch
    ):
        record = tmp_path / "record.json"
        agent = _agent_with(
            tmp_path, "normal", monkeypatch, workdir=workspace, record=record
        )
        try:
            agent.run_conversation("hello")
        finally:
            agent.close()

        caps = _record_data(record)["client_capabilities"] or {}
        assert caps.get("fs") == {"readTextFile": False, "writeTextFile": False}
        assert caps.get("terminal") is False

    def test_an_out_of_root_filesystem_request_is_refused(
        self, tmp_path, workspace, monkeypatch
    ):
        record = tmp_path / "record.json"
        agent = _agent_with(
            tmp_path, "fs-requests", monkeypatch, workdir=workspace, record=record
        )
        try:
            agent.run_conversation("touch some files")
        finally:
            agent.close()

        replies = _record_data(record)["client_request_replies"]
        outside = [r for r in replies if r["path"] == FS_REQUEST_OUTSIDE]
        assert outside, f"the outside-root request was never made: {replies}"
        assert outside[0]["error"], "an out-of-root read was not refused"
        assert "workspace" in (outside[0]["error"].get("message") or "").lower(), (
            f"the refusal did not name containment: {outside[0]['error']}"
        )

    def test_an_in_root_filesystem_request_is_refused_too(
        self, tmp_path, workspace, monkeypatch
    ):
        """Hermes advertised no filesystem capability, so an `fs/*` request is
        unsolicited whatever path it names — answering one would serve a
        capability the agent was told does not exist."""
        record = tmp_path / "record.json"
        agent = _agent_with(
            tmp_path, "fs-requests", monkeypatch, workdir=workspace, record=record
        )
        try:
            agent.run_conversation("touch some files")
        finally:
            agent.close()

        replies = _record_data(record)["client_request_replies"]
        inside = [r for r in replies if r["path"] != FS_REQUEST_OUTSIDE]
        assert inside, f"the inside-root request was never made: {replies}"
        assert inside[0]["error"], "an unsolicited fs/write_text_file was served"
        assert not (workspace / FS_REQUEST_INSIDE).exists(), (
            "Hermes wrote a file on the agent's behalf"
        )

    def test_the_out_of_root_path_is_never_created(
        self, tmp_path, workspace, monkeypatch
    ):
        agent = _agent_with(tmp_path, "fs-requests", monkeypatch, workdir=workspace)
        try:
            agent.run_conversation("touch some files")
        finally:
            agent.close()

        assert not os.path.exists(FS_REQUEST_OUTSIDE)

    def test_the_turn_still_completes_after_a_refused_request(
        self, tmp_path, workspace, monkeypatch
    ):
        """A refusal must be a reply. Silence here is the classic ACP deadlock."""
        agent = _agent_with(tmp_path, "fs-requests", monkeypatch, workdir=workspace)
        try:
            result = agent.run_conversation("touch some files")
        finally:
            agent.close()

        assert result["completed"] is True
        assert result["final_response"].strip()

    def test_a_request_is_checked_against_the_roots_the_session_negotiated(
        self, tmp_path, workspace
    ):
        """An allowed extra directory widens containment by exactly that much.

        Both requests are refused — Hermes serves no filesystem capability — but
        only the one outside the negotiated roots is refused *for being outside
        them*. That difference is the proof that the roots which went out on
        `session/new` are the roots a request is measured against.
        """
        from agent.claude_acp_session import ClaudeAcpSession

        allowed = tmp_path / "allowed"
        allowed.mkdir()
        session = ClaudeAcpSession(
            argv=[sys.executable, _FAKE_ADAPTER],
            cwd=str(workspace),
            additional_directories=[str(allowed)],
            hermes_session_id="roots-containment",
        )
        try:
            assert session.workspace_roots == [str(workspace), str(allowed)]
            with pytest.raises(PermissionError) as inside:
                session._on_request(
                    "fs/read_text_file", {"path": str(allowed / "a.txt")}
                )
            with pytest.raises(PermissionError) as outside:
                session._on_request(
                    "fs/read_text_file", {"path": str(tmp_path / "elsewhere.txt")}
                )
        finally:
            session.close()

        assert "outside the permitted workspace" not in str(inside.value)
        assert "outside the permitted workspace" in str(outside.value)

    def test_the_root_check_is_resolved_not_prefix_matched(self, tmp_path):
        """`/ws-evil` must not pass a `/ws` root, and `..` must not escape."""
        from agent.transports.claude_acp import ensure_path_within_roots

        root = tmp_path / "ws"
        root.mkdir()
        (tmp_path / "ws-evil").mkdir()

        assert ensure_path_within_roots(str(root / "a.txt"), [str(root)])
        with pytest.raises(PermissionError):
            ensure_path_within_roots(str(tmp_path / "ws-evil" / "a.txt"), [str(root)])
        with pytest.raises(PermissionError):
            ensure_path_within_roots(str(root / ".." / "a.txt"), [str(root)])


class TestPermissionLocations:
    """A path outside the negotiated roots must be visibly outside them."""

    def test_an_out_of_root_location_is_marked_in_the_prompt(
        self, tmp_path, workspace, monkeypatch, cli_surface
    ):
        surface = cli_surface("deny")
        agent = _agent_with(tmp_path, "permission", monkeypatch, workdir=workspace)
        try:
            agent.run_conversation("please write a file")
        finally:
            agent.close()

        assert surface.prompts, "the approval surface was never reached"
        assert OUTSIDE_WORKSPACE_LOCATION in surface.text
        assert "outside the workspace" in surface.text.lower(), (
            "the user was shown a path outside the negotiated roots with no "
            f"indication that it is outside them: {surface.text}"
        )

    def test_an_in_root_location_is_not_marked(self, tmp_path):
        from agent.transports.claude_acp_event_projector import (
            project_permission_request,
        )

        inside = str(tmp_path / "a.txt")
        request = project_permission_request(
            {"toolCall": {
                "_meta": {"claudeCode": {"toolName": "Write"}},
                "title": "Write a.txt",
                "locations": [{"path": inside}],
            }},
            roots=[str(tmp_path)],
        )
        assert "outside the workspace" not in request.description.lower()
        assert request.outside_roots == ()

    def test_with_no_roots_known_nothing_is_claimed_either_way(self, tmp_path):
        """Absent negotiated roots there is no basis for the claim, and
        labelling everything 'outside' would train the user to ignore it."""
        from agent.transports.claude_acp_event_projector import (
            project_permission_request,
        )

        request = project_permission_request(
            {"toolCall": {"locations": [{"path": "/anywhere/a.txt"}]}}
        )
        assert request.outside_roots == ()
        assert "outside the workspace" not in request.description.lower()

    def test_an_out_of_root_path_past_the_displayed_slice_is_still_reported(
        self, tmp_path
    ):
        """"Mark, don't refuse" makes the marker the entire protection, so it
        cannot be computed over the displayed slice only: a bulk edit whose
        ninth path leaves the workspace would be approved with no indication.
        """
        from agent.transports.claude_acp_event_projector import (
            _MAX_LOCATIONS,
            project_permission_request,
        )

        inside = [{"path": str(tmp_path / f"f{i}.py")} for i in range(_MAX_LOCATIONS)]
        request = project_permission_request(
            {"toolCall": {
                "_meta": {"claudeCode": {"toolName": "Edit"}},
                "title": "Edit many",
                "locations": inside + [{"path": "/etc/passwd"}],
            }},
            roots=[str(tmp_path)],
        )

        assert request.outside_roots == (), "the shown paths are all in-root"
        assert request.outside_roots_hidden == 1
        assert "outside the workspace" in request.description.lower(), (
            "the user was asked to approve a write outside their workspace with "
            f"no indication of it: {request.description}"
        )

    def test_shown_and_hidden_out_of_root_paths_are_both_reported(self, tmp_path):
        from agent.transports.claude_acp_event_projector import (
            _MAX_LOCATIONS,
            project_permission_request,
        )

        locations = (
            [{"path": "/etc/shadow"}]
            + [{"path": str(tmp_path / f"f{i}.py")} for i in range(_MAX_LOCATIONS - 1)]
            + [{"path": "/etc/passwd"}, {"path": "/etc/hosts"}]
        )
        request = project_permission_request(
            {"toolCall": {
                "_meta": {"claudeCode": {"toolName": "Edit"}},
                "title": "Edit many",
                "locations": locations,
            }},
            roots=[str(tmp_path)],
        )

        assert request.outside_roots == ("/etc/shadow",)
        assert request.outside_roots_hidden == 2
        description = request.description.lower()
        assert "/etc/shadow" in description
        assert "2 more outside the workspace" in description, (
            f"the overflow count must be stated: {request.description}"
        )


class TestContinuityPreserved:
    """Roots are session state: they must survive — and be re-sent on — rebind."""

    def test_a_rebind_renegotiates_the_same_roots(
        self, tmp_path, workspace, monkeypatch, allow_directories
    ):
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        allow_directories(allowed)

        records = tmp_path / "records"
        records.mkdir()
        store = tmp_path / "store"
        store.mkdir()
        db = SessionDB(db_path=tmp_path / "sessions.db")

        first = _agent_with(
            tmp_path, "memory", monkeypatch, workdir=workspace,
            record_dir=records, store=store, db=db, session_id="roots-continuity",
        )
        try:
            first.run_conversation("remember PLUM")
        finally:
            first.close()

        second = _agent_with(
            tmp_path, "memory", monkeypatch, workdir=workspace,
            record_dir=records, store=store, db=db, session_id="roots-continuity",
        )
        try:
            result = second.run_conversation("what did I say?")
        finally:
            second.close()

        loads = [
            params
            for data in _records_in(records)
            for params in (data.get("session_load_params") or [])
        ]
        assert loads, "the second process never rebound via session/load"
        assert loads[-1].get("additionalDirectories") == [str(allowed)], (
            "a rebound session was reattached without its extra roots, so the "
            "workspace scope silently differed from the first process's"
        )
        assert "PLUM" in result["final_response"], "continuity was lost"

    def test_widening_the_roots_does_not_cost_the_claude_side_session(
        self, tmp_path, workspace, monkeypatch, allow_directories
    ):
        """Allowing another directory must not read as 'different workspace'."""
        from agent.claude_acp_binding import BINDING_KEY

        records = tmp_path / "records"
        records.mkdir()
        store = tmp_path / "store"
        store.mkdir()
        db = SessionDB(db_path=tmp_path / "sessions.db")

        agent = _agent_with(
            tmp_path, "memory", monkeypatch, workdir=workspace,
            record_dir=records, store=store, db=db, session_id="roots-widen",
        )
        try:
            agent.run_conversation("remember PLUM")
            bound = (db.get_session_model_config_value(
                "roots-widen", BINDING_KEY) or {}).get("acp_session_id")

            allowed = tmp_path / "allowed"
            allowed.mkdir()
            allow_directories(allowed)

            result = agent.run_conversation("what did I say?")
        finally:
            agent.close()

        after = (db.get_session_model_config_value(
            "roots-widen", BINDING_KEY) or {}).get("acp_session_id")
        assert bound and after == bound, (
            "widening the allowed roots discarded the Claude-side session"
        )
        assert "PLUM" in result["final_response"]
