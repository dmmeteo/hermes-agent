"""Packet 5 slice D — a Claude Code turn's tool activity reaches the transcript.

Through Packet 4 the ACP runtime appended exactly one message per turn
(``{"role": "assistant", "content": final_text}``), so ``agent/curator.py``,
external-memory review, background-review snapshots and ``session_search`` saw a
Claude Code turn as text with **no tool activity at all** — while the sibling
delegated runtime (``agent/codex_runtime.py``) has always spliced canonical
``(assistant tool_calls, tool)`` pairs. Measured on the reporter's live DB: 6
``role='tool'`` rows from the native agent, **0** across ~16 Bash calls under
Claude Code. This slice closes that gap.

What each class pins:

``TestToolRowsLand``          the pairs exist, are matched, name the adapter's
                              own tools, and advance ``sessions.tool_call_count``
``TestPersistedRowsAreSafe``  credential scrubbing and the size bound apply to a
                              transcript row exactly as they do to a bubble
``TestNoOrphanToolCalls``     no assistant ``tool_calls`` row ever lands without
                              its matching result — including the one shape that
                              can produce one (a stop mid-tool)
``TestNothingElseMoved``      one user row, one final assistant row, unchanged
                              display callbacks, unchanged reseed recap,
                              ``agent_persisted`` still True
``TestKillSwitch``            ``persist_tool_calls: false`` reproduces the
                              pre-slice behaviour exactly

Every test but the recap one drives a REAL fake-adapter subprocess over REAL
stdio against a REAL temporary ``SessionDB``. The failure this slice fixes was
found by querying ``state.db``, and it is what these tests query: a mock of the
persistence path would have been green the whole time.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time

import pytest
import yaml

import run_agent
from agent.transports.claude_acp_event_projector import (
    PERSISTED_RESULT_MAX_CHARS,
    UNREPORTED_TOOL_RESULT,
    deterministic_call_id,
)
from hermes_state import SessionDB
from tests.agent import _fake_claude_acp_adapter as fake

_FAKE_ADAPTER = os.path.join(os.path.dirname(__file__), "_fake_claude_acp_adapter.py")

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="The fake adapter's process-group reaping is POSIX-only; the "
           "projection itself is platform-independent.",
)


# --------------------------------------------------------------------- helpers


@pytest.fixture
def write_persist_config():
    """Write ``model.claude_code_acp.persist_tool_calls`` into config.yaml.

    Real config file, real loader: the switch is a user-facing config key with no
    env-var route (AGENTS.md §Adding Configuration). ``HERMES_HOME`` is already a
    per-test tempdir (tests/conftest.py).
    """

    def _write(value):
        from hermes_cli.config import get_config_path

        path = get_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            yaml.safe_dump(
                {"model": {"claude_code_acp": {"persist_tool_calls": value}}}
            ),
            encoding="utf-8",
        )
        return path

    return _write


class _Surfaces:
    """Captures every display callback the projection is allowed to fire."""

    def __init__(self, agent):
        self.progress: list = []
        self.starts: list = []
        self.completes: list = []
        self.deltas: list = []
        agent.tool_progress_callback = (
            lambda event, name=None, preview=None, args=None, **kw:
            self.progress.append((event, name))
        )
        agent.tool_start_callback = (
            lambda call_id, name, args: self.starts.append((call_id, name))
        )
        agent.tool_complete_callback = (
            lambda call_id, name, args, result:
            self.completes.append((call_id, name))
        )
        agent.stream_delta_callback = self.deltas.append

    @property
    def counts(self) -> tuple:
        return (
            len(self.progress), len(self.starts), len(self.completes),
            len(self.deltas),
        )


def _agent_with(tmp_path, mode, monkeypatch, *, db, session_id, record=None, **env):
    monkeypatch.setenv("FAKE_ACP_MODE", mode)
    if record is not None:
        monkeypatch.setenv("FAKE_ACP_RECORD", str(record))
    for key, value in env.items():
        monkeypatch.setenv(key, str(value))
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
    # The post-turn skill nudge forks its own agent (and its own adapter); this
    # suite counts rows and processes, so keep it off.
    agent._skill_nudge_interval = 0
    return agent


def _run_tool_turn(tmp_path, monkeypatch, *, session_id="acp-tools", **env):
    """One completed ``tools`` turn against a real temp SessionDB."""
    db = SessionDB(db_path=tmp_path / "sessions.db")
    agent = _agent_with(
        tmp_path, "tools", monkeypatch, db=db, session_id=session_id, **env
    )
    surfaces = _Surfaces(agent)
    try:
        result = agent.run_conversation("read the notes")
    finally:
        agent.close()
    return db, agent, surfaces, result


def _stored(db, session_id):
    return db.get_messages(session_id)


def _tool_calls_of(row):
    calls = row.get("tool_calls")
    if isinstance(calls, str):
        calls = json.loads(calls)
    return calls or []


def _pairs(rows):
    """``[(assistant tool_call row, tool row)]`` for every matched pair."""
    by_id = {
        row.get("tool_call_id"): row for row in rows if row.get("role") == "tool"
    }
    out = []
    for row in rows:
        if row.get("role") != "assistant":
            continue
        for call in _tool_calls_of(row):
            out.append((row, call, by_id.get(call.get("id"))))
    return out


def _dump(rows):
    return json.dumps(rows, default=repr)


def _interrupt_when(agent, record, predicate, timeout=30.0):
    """Interrupt as soon as *predicate* holds for the adapter's record."""

    def _run():
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                data = json.loads(record.read_text(encoding="utf-8"))
            except Exception:
                data = {}
            if predicate(data):
                break
            time.sleep(0.02)
        agent.interrupt("stop")

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return thread


# ------------------------------------------------------------- the rows land


class TestToolRowsLand:
    """The reporter's own query: ``SELECT role, tool_name, tool_call_id,
    tool_calls FROM messages`` returned no ``role='tool'`` row for ~16 Bash
    calls, and ``sessions.tool_call_count`` never moved."""

    def test_a_tool_using_turn_persists_tool_rows(self, tmp_path, monkeypatch):
        db, _, _, _ = _run_tool_turn(tmp_path, monkeypatch)
        rows = _stored(db, "acp-tools")
        tool_rows = [row for row in rows if row.get("role") == "tool"]
        assert tool_rows, (
            "a tool-using Claude Code turn persisted no role='tool' row: "
            f"{[row.get('role') for row in rows]}"
        )

    def test_every_completed_tool_gets_exactly_one_pair(self, tmp_path, monkeypatch):
        db, _, surfaces, _ = _run_tool_turn(tmp_path, monkeypatch)
        rows = _stored(db, "acp-tools")
        tool_rows = [row for row in rows if row.get("role") == "tool"]
        assert len(tool_rows) == len(surfaces.completes) == 4

    def test_the_pairs_correlate_by_tool_call_id(self, tmp_path, monkeypatch):
        db, _, _, _ = _run_tool_turn(tmp_path, monkeypatch)
        pairs = _pairs(_stored(db, "acp-tools"))
        assert pairs
        for _assistant, call, tool_row in pairs:
            assert tool_row is not None, f"no tool row for {call}"
            assert tool_row["tool_call_id"] == call["id"]

    def test_the_rows_name_the_adapters_own_tools(self, tmp_path, monkeypatch):
        """``Bash`` / ``Read`` / ``Grep`` / ``Glob``, not a Hermes tool they are
        not — the same names the approval prompts already use."""
        db, _, _, _ = _run_tool_turn(tmp_path, monkeypatch)
        rows = _stored(db, "acp-tools")
        assert {
            row["tool_name"] for row in rows if row.get("role") == "tool"
        } == {"Read", "Bash", "Grep", "Glob"}
        assert {
            call["function"]["name"] for _a, call, _t in _pairs(rows)
        } == {"Read", "Bash", "Grep", "Glob"}

    def test_the_call_id_is_the_adapters_own_id_namespaced(
        self, tmp_path, monkeypatch
    ):
        """Deterministic, so a re-projection of the same call carries one
        identity, and namespaced so it cannot collide with a Hermes call id."""
        db, _, _, _ = _run_tool_turn(tmp_path, monkeypatch)
        ids = {
            row["tool_call_id"] for row in _stored(db, "acp-tools")
            if row.get("role") == "tool"
        }
        assert deterministic_call_id("tc-read") in ids
        assert all(call_id.startswith("claude_acp_") for call_id in ids)

    def test_tool_call_count_advances(self, tmp_path, monkeypatch):
        db, _, _, _ = _run_tool_turn(tmp_path, monkeypatch)
        session = db.get_session("acp-tools")
        assert session["tool_call_count"] == 4

    def test_the_arguments_are_json_the_tool_loop_shape_expects(
        self, tmp_path, monkeypatch
    ):
        db, _, _, _ = _run_tool_turn(tmp_path, monkeypatch)
        read = [
            call for _a, call, _t in _pairs(_stored(db, "acp-tools"))
            if call["function"]["name"] == "Read"
        ]
        assert len(read) == 1
        assert read[0]["type"] == "function"
        assert json.loads(read[0]["function"]["arguments"])["file_path"] == "notes.txt"

    def test_a_failed_tool_row_says_so(self, tmp_path, monkeypatch):
        """A reader of this transcript — the curator, or a later native-model
        turn — must not read a failure as a success."""
        db, _, _, _ = _run_tool_turn(tmp_path, monkeypatch)
        bash = [
            row for row in _stored(db, "acp-tools")
            if row.get("tool_name") == "Bash" and row.get("role") == "tool"
        ]
        assert len(bash) == 1
        assert bash[0]["content"].startswith("[failed]")

    def test_the_result_text_is_persisted(self, tmp_path, monkeypatch):
        db, _, _, _ = _run_tool_turn(tmp_path, monkeypatch)
        read = [
            row for row in _stored(db, "acp-tools")
            if row.get("tool_name") == "Read" and row.get("role") == "tool"
        ]
        assert "42" in read[0]["content"]

    def test_session_search_finds_a_tool_result_by_its_text(
        self, tmp_path, monkeypatch
    ):
        """The point of the rows: ``session_search`` can see what the agent
        did, not only what it said."""
        db, _, _, _ = _run_tool_turn(tmp_path, monkeypatch)
        hits = db.search_messages("entries", role_filter=["tool"], limit=20)
        assert [
            (hit["session_id"], hit["tool_name"]) for hit in hits
        ] == [("acp-tools", "Glob")], (
            f"the Glob result is not searchable: {hits}"
        )


# ------------------------------------------------------------ safety of a row


class TestPersistedRowsAreSafe:
    """A transcript row is a display field's equal for scrubbing purposes: it
    reaches ``session_search`` output, a curator prompt, and a later API call."""

    @pytest.mark.parametrize(
        "secret", [fake.TOOL_INPUT_SECRET, fake.TOOL_OUTPUT_SECRET]
    )
    def test_no_credential_shaped_value_reaches_a_persisted_row(
        self, tmp_path, monkeypatch, secret
    ):
        db, _, _, _ = _run_tool_turn(tmp_path, monkeypatch)
        assert secret not in _dump(_stored(db, "acp-tools"))

    def test_the_secret_argument_is_masked_rather_than_dropped(
        self, tmp_path, monkeypatch
    ):
        """Redaction, not deletion: "the agent passed nothing" would be a
        different and false record of the call."""
        db, _, _, _ = _run_tool_turn(tmp_path, monkeypatch)
        read = [
            call for _a, call, _t in _pairs(_stored(db, "acp-tools"))
            if call["function"]["name"] == "Read"
        ][0]
        args = json.loads(read["function"]["arguments"])
        assert "api_key" in args
        assert fake.TOOL_INPUT_SECRET not in str(args["api_key"])

    def test_an_oversized_tool_result_is_bounded_with_an_explicit_marker(
        self, tmp_path, monkeypatch
    ):
        """A single tool result can be a whole file. Truncating silently would
        let a later reader mistake the head for everything the tool said — the
        byte-exact output stays in Claude Code's own transcript."""
        oversize = PERSISTED_RESULT_MAX_CHARS + 5000
        db, _, _, _ = _run_tool_turn(
            tmp_path, monkeypatch, session_id="acp-huge",
            FAKE_ACP_HUGE_TOOL_OUTPUT=oversize,
        )
        read = [
            row for row in _stored(db, "acp-huge")
            if row.get("tool_name") == "Read" and row.get("role") == "tool"
        ][0]
        content = read["content"]
        assert "chars truncated]" in content
        assert content.count(fake.HUGE_OUTPUT_FILLER) <= PERSISTED_RESULT_MAX_CHARS
        # The marker reports what was left out rather than only that something
        # was: a count is checkable against the real transcript.
        omitted = int(content.split("[+")[1].split(" ")[0])
        assert omitted >= oversize - PERSISTED_RESULT_MAX_CHARS

    def test_the_bound_is_not_applied_to_an_ordinary_result(
        self, tmp_path, monkeypatch
    ):
        db, _, _, _ = _run_tool_turn(tmp_path, monkeypatch)
        for row in _stored(db, "acp-tools"):
            if row.get("role") == "tool":
                assert "chars truncated]" not in (row.get("content") or "")


# ------------------------------------------------------------------- orphans


class TestNoOrphanToolCalls:
    """An assistant row carrying ``tool_calls`` with no matching ``tool`` row is
    what breaks ``repair_message_sequence`` — and, if this session is later
    switched to a native model, what a real API call would reject."""

    def test_no_orphan_after_a_normal_turn(self, tmp_path, monkeypatch):
        db, _, _, _ = _run_tool_turn(tmp_path, monkeypatch)
        for _assistant, call, tool_row in _pairs(_stored(db, "acp-tools")):
            assert tool_row is not None, f"orphan tool_call: {call}"

    def test_a_stop_mid_tool_leaves_a_repaired_pair_not_an_orphan(
        self, tmp_path, monkeypatch
    ):
        """The adapter never reports an outcome for a tool the user stopped, so
        the pair has to be closed by Hermes or not written at all."""
        record = tmp_path / "record.json"
        db = SessionDB(db_path=tmp_path / "sessions.db")
        agent = _agent_with(
            tmp_path, "cancel-mid-tool", monkeypatch, db=db,
            session_id="acp-cancel", record=record,
        )
        try:
            _interrupt_when(agent, record, lambda data: bool(data.get("prompt")))
            result = agent.run_conversation("search everything")
        finally:
            agent.close()

        assert result["interrupted"] is True
        rows = _stored(db, "acp-cancel")
        pairs = _pairs(rows)
        assert pairs, "the unfinished tool call was not recorded at all"
        for _assistant, call, tool_row in pairs:
            assert tool_row is not None, f"orphan tool_call after a stop: {call}"
        unfinished = [
            row for row in rows
            if row.get("role") == "tool"
            and row.get("tool_call_id")
            == deterministic_call_id(fake.UNFINISHED_TOOL_CALL_ID)
        ]
        assert len(unfinished) == 1
        assert unfinished[0]["content"] == UNREPORTED_TOOL_RESULT

    def test_a_stopped_turn_still_ends_on_the_assistant_row(
        self, tmp_path, monkeypatch
    ):
        """Role alternation: the tool rows go between the user row and the
        assistant reply, never after it."""
        record = tmp_path / "record.json"
        db = SessionDB(db_path=tmp_path / "sessions.db")
        agent = _agent_with(
            tmp_path, "cancel-mid-tool", monkeypatch, db=db,
            session_id="acp-cancel-order", record=record,
        )
        try:
            _interrupt_when(agent, record, lambda data: bool(data.get("prompt")))
            agent.run_conversation("search everything")
        finally:
            agent.close()
        roles = [row["role"] for row in _stored(db, "acp-cancel-order")]
        assert roles[0] == "user"
        assert roles[-1] == "assistant"
        assert not [a for a, b in zip(roles, roles[1:]) if a == b]

    def test_a_credential_in_an_unfinished_calls_arguments_is_still_scrubbed(
        self, tmp_path, monkeypatch
    ):
        record = tmp_path / "record.json"
        db = SessionDB(db_path=tmp_path / "sessions.db")
        agent = _agent_with(
            tmp_path, "cancel-mid-tool", monkeypatch, db=db,
            session_id="acp-cancel-secret", record=record,
        )
        try:
            _interrupt_when(agent, record, lambda data: bool(data.get("prompt")))
            agent.run_conversation("search everything")
        finally:
            agent.close()
        assert fake.TOOL_INPUT_SECRET not in _dump(_stored(db, "acp-cancel-secret"))


# ------------------------------------------------------------ nothing else moved


class TestNothingElseMoved:
    """The rows are additive. Everything the reporter could already see —
    bubbles, the reply, the user row — has to be bit-for-bit what it was."""

    def test_exactly_one_user_row_and_one_final_assistant_text_row(
        self, tmp_path, monkeypatch
    ):
        """``_flush_messages_to_session_db`` is marker-idempotent, so the user
        row this turn already wrote must not be written again (#860/#42039)."""
        db, _, _, result = _run_tool_turn(tmp_path, monkeypatch)
        rows = _stored(db, "acp-tools")
        assert [row["role"] for row in rows].count("user") == 1
        text_rows = [
            row for row in rows
            if row["role"] == "assistant" and not _tool_calls_of(row)
        ]
        assert len(text_rows) == 1
        assert text_rows[0]["content"] == result["final_response"] == "Reading. Done."
        assert rows[0]["role"] == "user"
        assert rows[-1] is text_rows[0]

    def test_the_gateway_is_still_told_not_to_write_the_turn(
        self, tmp_path, monkeypatch
    ):
        _, _, _, result = _run_tool_turn(tmp_path, monkeypatch)
        assert result["agent_persisted"] is True

    def test_role_alternation_holds_across_the_spliced_rows(
        self, tmp_path, monkeypatch
    ):
        db, _, _, result = _run_tool_turn(tmp_path, monkeypatch)
        for roles in (
            [row["role"] for row in _stored(db, "acp-tools")],
            [m["role"] for m in result["messages"]],
        ):
            assert roles[0] == "user"
            assert roles[-1] == "assistant"
            assert not [a for a, b in zip(roles, roles[1:]) if a == b]

    def test_the_display_callbacks_fire_exactly_as_many_times_as_before(
        self, tmp_path, monkeypatch, write_persist_config
    ):
        """Anti-double-bubble, asserted rather than argued: display and
        persistence are two disjoint consumers of one projector, so switching
        persistence off must not move a single callback."""
        write_persist_config(True)
        _, _, on, _ = _run_tool_turn(tmp_path, monkeypatch, session_id="acp-cb-on")
        write_persist_config(False)
        _, _, off, _ = _run_tool_turn(tmp_path, monkeypatch, session_id="acp-cb-off")
        assert on.counts == off.counts
        assert on.completes == off.completes

    def test_the_reseed_recap_is_byte_identical_with_and_without_tool_rows(self):
        """A reseed recap is the ONE thing Hermes sends back into the adapter,
        so a change here would be a change to the prompt. ``_build_recap``
        filters to string-content user/assistant rows, which excludes both the
        tool rows and the ``content=None`` assistant rows — verified, not
        assumed."""
        from agent.claude_code_runtime import _build_recap

        plain = [
            {"role": "user", "content": "an earlier question"},
            {"role": "assistant", "content": "an earlier answer"},
            {"role": "user", "content": "the new prompt"},
        ]
        with_tools = [
            plain[0],
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "claude_acp_tc-read", "type": "function",
                 "function": {"name": "Read", "arguments": "{}"}},
            ]},
            {"role": "tool", "tool_call_id": "claude_acp_tc-read",
             "tool_name": "Read", "content": "42"},
            plain[1],
            plain[2],
        ]
        assert _build_recap(with_tools) == _build_recap(plain)

    def test_the_skill_nudge_counter_is_unchanged_by_the_rows(
        self, tmp_path, monkeypatch
    ):
        """The nudge gate already advanced by ``tool_iterations``; persisting
        the same completions must not tick it twice."""
        db = SessionDB(db_path=tmp_path / "sessions.db")
        agent = _agent_with(
            tmp_path, "tools", monkeypatch, db=db, session_id="acp-nudge"
        )
        agent._iters_since_skill = 0
        try:
            agent.run_conversation("read the notes")
        finally:
            agent.close()
        assert agent._iters_since_skill == 4

    def test_the_session_activity_clock_advances(self, tmp_path, monkeypatch):
        """``last_activity_at`` was NULL for this runtime: nothing ever called
        ``_touch_activity``, so a long Claude Code turn looked idle to every
        stall consumer. (``last_activity_description`` is deliberately cleared
        when the turn ends — see ``_reset_activity_labels_after_turn`` — so the
        durable fact here is the timestamp.)"""
        db, _, _, _ = _run_tool_turn(tmp_path, monkeypatch, session_id="acp-activity")
        assert db.get_session("acp-activity")["last_activity_at"] is not None


# ---------------------------------------------------------------- kill switch


class TestKillSwitch:
    """``persist_tool_calls`` exists as a one-line retreat for a dogfooder
    whose live ``state.db`` is already 300 MB, not as a feature gate: parity
    with the sibling delegated runtime is the default."""

    def test_the_default_is_on(self, tmp_path, monkeypatch):
        from agent.claude_code_runtime import claude_acp_persist_tool_calls

        assert claude_acp_persist_tool_calls() is True

    @pytest.mark.parametrize("value", [False, "false", "no", "off", 0])
    def test_a_falsey_config_value_turns_it_off(
        self, write_persist_config, value
    ):
        from agent.claude_code_runtime import claude_acp_persist_tool_calls

        write_persist_config(value)
        assert claude_acp_persist_tool_calls() is False

    @pytest.mark.parametrize("value", [True, "true", "yes", "on", 1])
    def test_a_truthy_config_value_keeps_it_on(self, write_persist_config, value):
        from agent.claude_code_runtime import claude_acp_persist_tool_calls

        write_persist_config(value)
        assert claude_acp_persist_tool_calls() is True

    def test_an_unreadable_value_stays_on(self, write_persist_config):
        """Every rejection resolves to the parity default, and says so once."""
        from agent.claude_code_runtime import claude_acp_persist_tool_calls

        write_persist_config(["not", "a", "flag"])
        assert claude_acp_persist_tool_calls() is True

    def test_off_reproduces_the_pre_slice_transcript_exactly(
        self, tmp_path, monkeypatch, write_persist_config
    ):
        write_persist_config(False)
        db, _, _, result = _run_tool_turn(
            tmp_path, monkeypatch, session_id="acp-off"
        )
        rows = _stored(db, "acp-off")
        assert [row["role"] for row in rows] == ["user", "assistant"]
        assert rows[-1]["content"] == "Reading. Done."
        # The live list too, not only the DB: nothing spliced, so the turn adds
        # exactly the one assistant row it always did. (`_db_persisted` is the
        # flush's own idempotence marker, stamped on the live dict.)
        assert [
            (m["role"], m.get("content")) for m in result["messages"]
        ] == [("user", "read the notes"), ("assistant", "Reading. Done.")]
        session = db.get_session("acp-off")
        assert session["tool_call_count"] == 0
        assert session["last_activity_at"] is None
        assert result["completed"] is True
